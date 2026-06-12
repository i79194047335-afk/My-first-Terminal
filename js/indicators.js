// ============================================================================
// IndicatorMath — pure OHLC indicator calculations
// ============================================================================

const IndicatorMath = (() => {

    function sma(data, period) {
        const result = [];
        for (let i = 0; i < data.length; i++) {
            if (i < period - 1) { result.push({ time: data[i].time, value: null }); continue; }
            let sum = 0;
            for (let j = i - period + 1; j <= i; j++) sum += data[j].close;
            result.push({ time: data[i].time, value: sum / period });
        }
        return result;
    }

    function ema(data, period) {
        const result = [];
        const k = 2 / (period + 1);
        let prev = null;
        for (let i = 0; i < data.length; i++) {
            if (i < period - 1) { result.push({ time: data[i].time, value: null }); continue; }
            if (i === period - 1) {
                let sum = 0;
                for (let j = 0; j < period; j++) sum += data[j].close;
                prev = sum / period;
                result.push({ time: data[i].time, value: prev }); continue;
            }
            prev = data[i].close * k + prev * (1 - k);
            result.push({ time: data[i].time, value: prev });
        }
        return result;
    }

    function bollinger(data, period, mult) {
        const basis = [], upper = [], lower = [];
        for (let i = 0; i < data.length; i++) {
            if (i < period - 1) {
                basis.push({ time: data[i].time, value: null });
                upper.push({ time: data[i].time, value: null });
                lower.push({ time: data[i].time, value: null });
                continue;
            }
            let sum = 0;
            for (let j = i - period + 1; j <= i; j++) sum += data[j].close;
            const mean = sum / period;
            let variance = 0;
            for (let j = i - period + 1; j <= i; j++) { const d = data[j].close - mean; variance += d * d; }
            const stdev = Math.sqrt(variance / period);
            basis.push({ time: data[i].time, value: mean });
            upper.push({ time: data[i].time, value: mean + mult * stdev });
            lower.push({ time: data[i].time, value: mean - mult * stdev });
        }
        return { basis, upper, lower };
    }

    function rsi(data, period) {
        if (data.length < period + 1) return data.map(c => ({ time: c.time, value: null }));
        const result = [];
        let avgGain = 0, avgLoss = 0;
        for (let i = 1; i <= period; i++) {
            const change = data[i].close - data[i - 1].close;
            if (change > 0) avgGain += change; else avgLoss += Math.abs(change);
        }
        avgGain /= period; avgLoss /= period;
        for (let i = 0; i < period; i++) result.push({ time: data[i].time, value: null });
        result.push({ time: data[period].time, value: avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss) });
        for (let i = period + 1; i < data.length; i++) {
            const change = data[i].close - data[i - 1].close;
            avgGain = (avgGain * (period - 1) + (change > 0 ? change : 0)) / period;
            avgLoss = (avgLoss * (period - 1) + (change < 0 ? Math.abs(change) : 0)) / period;
            result.push({ time: data[i].time, value: avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss) });
        }
        return result;
    }

    function macd(data, fast, slow, signalPeriod) {
        const fastEma = ema(data, fast);
        const slowEma = ema(data, slow);
        const macdLine = data.map((c, i) => {
            const f = fastEma[i].value, s = slowEma[i].value;
            return { time: c.time, value: (f !== null && s !== null) ? f - s : null };
        });
        const signalLine = [], histLine = [];
        const k = 2 / (signalPeriod + 1);
        let sigPrev = null, sigCount = 0, sigSum = 0;
        for (let i = 0; i < macdLine.length; i++) {
            const mv = macdLine[i].value;
            if (mv === null) { signalLine.push({ time: macdLine[i].time, value: null }); histLine.push({ time: macdLine[i].time, value: null }); continue; }
            if (sigPrev === null) {
                sigSum += mv; sigCount++;
                if (sigCount < signalPeriod) { signalLine.push({ time: macdLine[i].time, value: null }); histLine.push({ time: macdLine[i].time, value: null }); }
                else {
                    sigPrev = sigSum / signalPeriod;
                    signalLine.push({ time: macdLine[i].time, value: sigPrev });
                    const h = mv - sigPrev;
                    histLine.push({ time: macdLine[i].time, value: h, color: h >= 0 ? '#26a69a' : '#ef5350' });
                }
            } else {
                sigPrev = mv * k + sigPrev * (1 - k);
                signalLine.push({ time: macdLine[i].time, value: sigPrev });
                const h = mv - sigPrev;
                histLine.push({ time: macdLine[i].time, value: h, color: h >= 0 ? '#26a69a' : '#ef5350' });
            }
        }
        return { macd: macdLine, signal: signalLine, hist: histLine };
    }

    // Stochastic — returns { k, d } arrays of {time, value}
    // k = smoothed %K (SMA of raw %K over smoothK bars)
    // d = SMA of k over smoothD bars
    function stochastic(data, period, smoothK, smoothD) {
        // Raw %K
        const rawK = [];
        for (let i = 0; i < data.length; i++) {
            if (i < period - 1) { rawK.push(null); continue; }
            const slice = data.slice(i - period + 1, i + 1);
            const lowest  = Math.min(...slice.map(c => c.low));
            const highest = Math.max(...slice.map(c => c.high));
            if (highest === lowest) { rawK.push(0); continue; }
            rawK.push(100 * (data[i].close - lowest) / (highest - lowest));
        }

        // Smooth %K (SMA of rawK over smoothK)
        const smK = _smaSeries(rawK, smoothK);

        // %D = SMA of smoothed %K over smoothD
        const smD = _smaSeries(smK, smoothD);

        const k = data.map((c, i) => ({ time: c.time, value: smK[i] }));
        const d = data.map((c, i) => ({ time: c.time, value: smD[i] }));
        return { k, d };
    }

    // Helper: SMA of a plain number array (nulls propagate)
    function _smaSeries(arr, period) {
        const result = [];
        for (let i = 0; i < arr.length; i++) {
            if (i < period - 1) { result.push(null); continue; }
            const slice = arr.slice(i - period + 1, i + 1);
            if (slice.some(v => v === null)) { result.push(null); continue; }
            result.push(slice.reduce((a, b) => a + b, 0) / period);
        }
        return result;
    }

    return { sma, ema, bollinger, rsi, macd, stochastic };
})();

window.IndicatorMath = IndicatorMath;


// ============================================================================
// IndicatorManager — per-price-pane series lifecycle + crosshair legend
// ============================================================================

class IndicatorManager {

    constructor(paneId, chart, candleSeries, chartDiv) {
        this.paneId       = paneId;
        this.chart        = chart;
        this.candleSeries = candleSeries;
        this.chartDiv     = chartDiv;

        this._instances  = []; // flat: { id, type, params, color, lineWidth, ...seriesRefs }
        this._idCounter  = 0;
        this._oscPanes   = [];

        // Legend div — sits in top-left of chartDiv, pointer-events none
        this._legendEl = this._createLegend(chartDiv);

        // Subscribe crosshair for legend updates
        this._crosshairHandler = param => this._onCrosshair(param);
        chart.subscribeCrosshairMove(this._crosshairHandler);
    }

    _createLegend(chartDiv) {
        const el = document.createElement('div');
        el.style.cssText = [
            'position:absolute', 'left:8px', 'top:4px',
            'z-index:1000', 'pointer-events:none',
            'font-size:11px', 'line-height:1.6',
            'font-family:Arial,sans-serif'
        ].join(';');
        chartDiv.appendChild(el);
        return el;
    }

    _onCrosshair(param) {
        const lines = [];

        const val = (series) => {
            if (!series || !param.seriesData) return null;
            const d = param.seriesData.get(series);
            if (!d) return null;
            return typeof d.value === 'number' ? d.value : null;
        };

        const fmt = (v, dec) => (v === null || v === undefined) ? '—' : v.toFixed(dec);

        for (const inst of this._instances) {
            if (inst.type === 'sma') {
                const v = val(inst.series);
                lines.push(`<span style="color:${inst.color}">SMA(${inst.params.period}) ${fmt(v, 5)}</span>`);
            }
            if (inst.type === 'ema') {
                const v = val(inst.series);
                lines.push(`<span style="color:${inst.color}">EMA(${inst.params.period}) ${fmt(v, 5)}</span>`);
            }
            if (inst.type === 'bollinger') {
                const u = val(inst.upper), b = val(inst.basis), l = val(inst.lower);
                lines.push(`<span style="color:${inst.color}">BB(${inst.params.period},${inst.params.mult}) U:${fmt(u,5)} M:${fmt(b,5)} L:${fmt(l,5)}</span>`);
            }
            if (inst.type === 'rsi' && inst._series) {
                const v = val(inst._series);
                lines.push(`<span style="color:${inst.color}">RSI(${inst.params.period}) ${fmt(v,2)}</span>`);
            }
            if (inst.type === 'macd' && inst._macdSeries) {
                const m = val(inst._macdSeries), s = val(inst._signalSeries), h = val(inst._histSeries);
                lines.push(`<span style="color:#2962FF">MACD ${fmt(m,5)}</span> <span style="color:#ff9800">Sig ${fmt(s,5)}</span> <span style="color:#888">H ${fmt(h,5)}</span>`);
            }
            if (inst.type === 'stochastic' && inst._kSeries) {
                const k = val(inst._kSeries), d = val(inst._dSeries);
                lines.push(`<span style="color:${inst.color}">Stoch(${inst.params.period},${inst.params.smoothK},${inst.params.smoothD}) K:${fmt(k,2)} D:${fmt(d,2)}</span>`);
            }
        }

        this._legendEl.innerHTML = lines.join('<br>');
    }

    has(type) {
        return this._instances.some(i => i.type === type);
    }

    getInstances() {
        return this._instances;
    }

    add(type, params, color, lineWidth) {
        const defaults = { sma: '#2962FF', ema: '#ff9800', bollinger: '#9c27b0', rsi: '#e91e63', macd: '#2962FF', stochastic: '#2962FF' };
        const id = `${type}_${++this._idCounter}`;
        const inst = {
            id,
            type,
            params: params || {},
            color: color || defaults[type] || '#2962FF',
            lineWidth: (lineWidth === undefined || lineWidth === null) ? 2 : lineWidth
        };
        this._instances.push(inst);

        if (this._isOverlay(type)) {
            this._addInstanceSeries(inst);
        } else {
            this._rebuildOscillators();
        }
        return id;
    }

    remove(id) {
        const inst = this._instances.find(i => i.id === id);
        if (!inst) return;
        if (this._isOverlay(inst.type)) {
            this._destroyInstanceSeries(inst);
            this._instances = this._instances.filter(i => i.id !== id);
        } else {
            this._teardownAllOscillatorSeries();
            this._instances = this._instances.filter(i => i.id !== id);
            this._rebuildOscillators();
        }
    }

    update(id, params, color, lineWidth) {
        const inst = this._instances.find(i => i.id === id);
        if (!inst) return;
        if (this._isOverlay(inst.type)) {
            if (params) inst.params = params;
            if (color)  inst.color  = color;
            if (lineWidth !== undefined && lineWidth !== null) inst.lineWidth = lineWidth;
            if (inst.type === 'bollinger') {
                const opts = { color: inst.color, lineWidth: inst.lineWidth };
                try { inst.basis.applyOptions(opts); } catch(e) {}
                try { inst.upper.applyOptions(opts); } catch(e) {}
                try { inst.lower.applyOptions(opts); } catch(e) {}
            } else {
                try { inst.series.applyOptions({ color: inst.color, lineWidth: inst.lineWidth }); } catch(e) {}
            }
        } else {
            this._teardownAllOscillatorSeries();
            if (params) inst.params = params;
            if (color)  inst.color  = color;
            if (lineWidth !== undefined && lineWidth !== null) inst.lineWidth = lineWidth;
            this._rebuildOscillators();
        }
    }

    recomputeAll(data) {
        if (!data || data.length < 2) return;

        for (const inst of this._instances) {
            if (inst.type === 'sma' && inst.series) {
                this._setFilteredData(inst.series, IndicatorMath.sma(data, inst.params.period));
                inst.series.applyOptions({ color: inst.color });
            }
            if (inst.type === 'ema' && inst.series) {
                this._setFilteredData(inst.series, IndicatorMath.ema(data, inst.params.period));
                inst.series.applyOptions({ color: inst.color });
            }
            if (inst.type === 'bollinger' && inst.basis) {
                const bands = IndicatorMath.bollinger(data, inst.params.period, inst.params.mult);
                const c = inst.color || '#9c27b0';
                this._setFilteredData(inst.basis, bands.basis);
                this._setFilteredData(inst.upper, bands.upper);
                this._setFilteredData(inst.lower, bands.lower);
                inst.basis.applyOptions({ color: c });
                inst.upper.applyOptions({ color: c });
                inst.lower.applyOptions({ color: c });
            }
            if (inst.type === 'rsi' && inst._series) {
                this._setFilteredData(inst._series, IndicatorMath.rsi(data, inst.params.period));
            }
            if (inst.type === 'macd' && inst._macdSeries) {
                const r = IndicatorMath.macd(data, inst.params.fast, inst.params.slow, inst.params.signal);
                this._setFilteredData(inst._macdSeries,   r.macd);
                this._setFilteredData(inst._signalSeries, r.signal);
                this._setFilteredData(inst._histSeries,   r.hist);
            }
            if (inst.type === 'stochastic' && inst._kSeries) {
                const r = IndicatorMath.stochastic(data, inst.params.period, inst.params.smoothK, inst.params.smoothD);
                this._setFilteredData(inst._kSeries, r.k);
                this._setFilteredData(inst._dSeries, r.d);
            }
        }
    }

    serialize() {
        return this._instances.map(i => ({
            id: i.id, type: i.type, params: i.params,
            color: i.color, lineWidth: i.lineWidth, visible: true
        }));
    }

    loadFrom(configArray, data) {
        if (!configArray || !configArray.length) return;
        for (const cfg of configArray) {
            if (cfg.visible === false) continue;
            const newId = this.add(cfg.type, cfg.params, cfg.color, cfg.lineWidth ?? 2);
            if (cfg.id) {
                const inst = this._instances.find(i => i.id === newId);
                if (inst) inst.id = cfg.id;
            }
        }
        // Advance the counter past any restored id suffix so freshly added
        // instances can never collide with a restored one (e.g. restored "sma_3"
        // while counter sits at 1).
        for (const inst of this._instances) {
            const n = parseInt(String(inst.id).split('_').pop(), 10);
            if (Number.isFinite(n) && n > this._idCounter) this._idCounter = n;
        }
        if (data && data.length) this.recomputeAll(data);
    }

    destroyAll() {
        for (const inst of this._instances)
            if (this._isOverlay(inst.type)) this._destroyInstanceSeries(inst);
        this._teardownAllOscillatorSeries();
        this._instances = [];
        this._legendEl.innerHTML = '';
    }

    // ---- Internal ----

    _isOverlay(type)    { return type === 'sma' || type === 'ema' || type === 'bollinger'; }
    _isOscillator(type) { return type === 'rsi' || type === 'macd' || type === 'stochastic'; }

    _addInstanceSeries(inst) {
        const LWC = window.LightweightCharts;
        const base = { priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false };
        const lw = inst.lineWidth;

        if (inst.type === 'sma') {
            inst.series = this.chart.addSeries(LWC.LineSeries, { ...base, color: inst.color, lineWidth: lw }, 0);
        }
        if (inst.type === 'ema') {
            inst.series = this.chart.addSeries(LWC.LineSeries, { ...base, color: inst.color, lineWidth: lw }, 0);
        }
        if (inst.type === 'bollinger') {
            inst.basis = this.chart.addSeries(LWC.LineSeries, { ...base, color: inst.color, lineWidth: lw, lineStyle: LWC.LineStyle.Dashed }, 0);
            inst.upper = this.chart.addSeries(LWC.LineSeries, { ...base, color: inst.color, lineWidth: lw }, 0);
            inst.lower = this.chart.addSeries(LWC.LineSeries, { ...base, color: inst.color, lineWidth: lw }, 0);
        }
    }

    _destroyInstanceSeries(inst) {
        if (inst.type === 'bollinger') {
            try { this.chart.removeSeries(inst.basis); } catch(e) {}
            try { this.chart.removeSeries(inst.upper); } catch(e) {}
            try { this.chart.removeSeries(inst.lower); } catch(e) {}
            inst.basis = inst.upper = inst.lower = null;
        } else {
            try { this.chart.removeSeries(inst.series); } catch(e) {}
            inst.series = null;
        }
    }

    _teardownAllOscillatorSeries() {
        for (const inst of this._instances) {
            if (!this._isOscillator(inst.type)) continue;
            const keys = ['_series', '_kSeries', '_dSeries', '_macdSeries', '_signalSeries', '_histSeries'];
            for (const k of keys) {
                if (inst[k]) { try { this.chart.removeSeries(inst[k]); } catch(e) {} inst[k] = null; }
            }
        }
        const panes = this.chart.panes();
        for (let i = panes.length - 1; i >= 1; i--)
            try { this.chart.removePane(panes[i]); } catch(e) {}
        this._oscPanes = [];
    }

    _rebuildOscillators() {
        const LWC = window.LightweightCharts;
        this._teardownAllOscillatorSeries();

        const chartHeight = this.chart.panes()[0]?.getHeight?.() || 300;
        const paneH = Math.round(chartHeight * 0.22);
        const fixedScale = (lo, hi) => () => ({ priceRange: { minValue: lo, maxValue: hi } });

        const oscInstances = this._instances.filter(i => this._isOscillator(i.type));
        const oscOrder = [...new Set(oscInstances.map(i => i.type))];

        oscOrder.forEach((type, idx) => {
            const paneIndex = idx + 1;
            const ofType = oscInstances.filter(i => i.type === type);

            ofType.forEach((inst, k) => {
                const isFirst = k === 0;

                if (type === 'rsi') {
                    const series = this.chart.addSeries(LWC.LineSeries, {
                        color: inst.color, lineWidth: inst.lineWidth,
                        priceLineVisible: false, lastValueVisible: true, crosshairMarkerVisible: false,
                        autoscaleInfoProvider: fixedScale(0, 100)
                    }, paneIndex);
                    if (isFirst) {
                        series.createPriceLine({ price: 100, color: '#555', lineWidth: 1, lineStyle: LWC.LineStyle.Solid, axisLabelVisible: true });
                        series.createPriceLine({ price: 80,  color: '#888', lineWidth: 1, lineStyle: LWC.LineStyle.Dashed, axisLabelVisible: true });
                        series.createPriceLine({ price: 50,  color: '#555', lineWidth: 1, lineStyle: LWC.LineStyle.Dashed, axisLabelVisible: false });
                        series.createPriceLine({ price: 20,  color: '#888', lineWidth: 1, lineStyle: LWC.LineStyle.Dashed, axisLabelVisible: true });
                        series.createPriceLine({ price: 0,   color: '#555', lineWidth: 1, lineStyle: LWC.LineStyle.Solid, axisLabelVisible: true });
                    }
                    inst._series = series;
                }

                if (type === 'macd') {
                    const macdSeries   = this.chart.addSeries(LWC.LineSeries,      { color: '#2962FF', lineWidth: inst.lineWidth, priceLineVisible: false, lastValueVisible: true,  crosshairMarkerVisible: false }, paneIndex);
                    const signalSeries = this.chart.addSeries(LWC.LineSeries,      { color: '#ff9800', lineWidth: inst.lineWidth, priceLineVisible: false, lastValueVisible: true,  crosshairMarkerVisible: false }, paneIndex);
                    const histSeries   = this.chart.addSeries(LWC.HistogramSeries, {                                              priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false }, paneIndex);
                    if (isFirst) {
                        macdSeries.createPriceLine({ price: 0, color: '#555', lineWidth: 1, lineStyle: LWC.LineStyle.Solid, axisLabelVisible: false });
                    }
                    inst._macdSeries = macdSeries; inst._signalSeries = signalSeries; inst._histSeries = histSeries;
                }

                if (type === 'stochastic') {
                    const kColor = inst.color;
                    const dColor = '#ff9800';
                    const kSeries = this.chart.addSeries(LWC.LineSeries, {
                        color: kColor, lineWidth: inst.lineWidth,
                        priceLineVisible: false, lastValueVisible: true, crosshairMarkerVisible: false,
                        autoscaleInfoProvider: fixedScale(0, 100)
                    }, paneIndex);
                    const dSeries = this.chart.addSeries(LWC.LineSeries, {
                        color: dColor, lineWidth: inst.lineWidth,
                        priceLineVisible: false, lastValueVisible: true, crosshairMarkerVisible: false,
                        autoscaleInfoProvider: fixedScale(0, 100)
                    }, paneIndex);
                    if (isFirst) {
                        kSeries.createPriceLine({ price: 100, color: '#555', lineWidth: 1, lineStyle: LWC.LineStyle.Solid, axisLabelVisible: true });
                        kSeries.createPriceLine({ price: 80,  color: '#888', lineWidth: 1, lineStyle: LWC.LineStyle.Dashed, axisLabelVisible: true });
                        kSeries.createPriceLine({ price: 50,  color: '#555', lineWidth: 1, lineStyle: LWC.LineStyle.Dashed, axisLabelVisible: false });
                        kSeries.createPriceLine({ price: 20,  color: '#888', lineWidth: 1, lineStyle: LWC.LineStyle.Dashed, axisLabelVisible: true });
                        kSeries.createPriceLine({ price: 0,   color: '#555', lineWidth: 1, lineStyle: LWC.LineStyle.Solid, axisLabelVisible: true });
                    }
                    inst._kSeries = kSeries;
                    inst._dSeries = dSeries;
                }
            });

            const panes2 = this.chart.panes();
            if (panes2[paneIndex]) try { panes2[paneIndex].setHeight(paneH); } catch(e) {}
        });
    }

    _setFilteredData(series, points) {
        if (!series) return;
        try { series.setData(points.filter(p => p.value !== null)); } catch(e) {}
    }
}

window.IndicatorManager = IndicatorManager;
