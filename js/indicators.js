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

        this._overlays    = {}; // type -> { series, params, color, ... }
        this._oscillators = []; // ordered: { type, params, color, _series, ... }
        this._oscPanes    = [];

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

        // Helper: get value for a series from the crosshair param
        const val = (series) => {
            if (!series || !param.seriesData) return null;
            const d = param.seriesData.get(series);
            if (!d) return null;
            return typeof d.value === 'number' ? d.value : null;
        };

        const fmt = (v, dec) => (v === null || v === undefined) ? '—' : v.toFixed(dec);

        // Overlays
        if (this._overlays.sma) {
            const v = val(this._overlays.sma.series);
            lines.push(`<span style="color:${this._overlays.sma.color}">SMA(${this._overlays.sma.params.period}) ${fmt(v, 5)}</span>`);
        }
        if (this._overlays.ema) {
            const v = val(this._overlays.ema.series);
            lines.push(`<span style="color:${this._overlays.ema.color}">EMA(${this._overlays.ema.params.period}) ${fmt(v, 5)}</span>`);
        }
        if (this._overlays.bollinger) {
            const bb = this._overlays.bollinger;
            const u = val(bb.upper), b = val(bb.basis), l = val(bb.lower);
            lines.push(`<span style="color:${bb.color}">BB(${bb.params.period},${bb.params.mult}) U:${fmt(u,5)} M:${fmt(b,5)} L:${fmt(l,5)}</span>`);
        }

        // Oscillators
        for (const osc of this._oscillators) {
            if (osc.type === 'rsi' && osc._series) {
                const v = val(osc._series);
                lines.push(`<span style="color:${osc.color||'#e91e63'}">RSI(${osc.params.period}) ${fmt(v,2)}</span>`);
            }
            if (osc.type === 'macd' && osc._macdSeries) {
                const m = val(osc._macdSeries), s = val(osc._signalSeries), h = val(osc._histSeries);
                lines.push(`<span style="color:#2962FF">MACD ${fmt(m,5)}</span> <span style="color:#ff9800">Sig ${fmt(s,5)}</span> <span style="color:#888">H ${fmt(h,5)}</span>`);
            }
            if (osc.type === 'stochastic' && osc._kSeries) {
                const k = val(osc._kSeries), d = val(osc._dSeries);
                lines.push(`<span style="color:${osc.color||'#2962FF'}">Stoch(${osc.params.period},${osc.params.smoothK},${osc.params.smoothD}) K:${fmt(k,2)} D:${fmt(d,2)}</span>`);
            }
        }

        this._legendEl.innerHTML = lines.join('<br>');
    }

    has(type) {
        return !!(this._overlays[type] || this._oscillators.find(o => o.type === type));
    }

    add(type, params, color) {
        if (this.has(type)) return;
        if (this._isOverlay(type)) {
            this._addOverlay(type, params, color);
        } else {
            this._oscillators.push({ type, params, color });
            this._rebuildOscillators();
        }
    }

    remove(type) {
        if (this._isOverlay(type)) {
            this._removeOverlay(type);
        } else {
            this._teardownAllOscillatorSeries();
            this._oscillators = this._oscillators.filter(o => o.type !== type);
            this._rebuildOscillators();
        }
    }

    update(type, params, color) {
        if (this._isOverlay(type)) {
            if (this._overlays[type]) {
                if (params) this._overlays[type].params = params;
                if (color)  this._overlays[type].color  = color;
            }
        } else {
            const osc = this._oscillators.find(o => o.type === type);
            if (osc) {
                this._teardownAllOscillatorSeries();
                if (params) osc.params = params;
                if (color)  osc.color  = color;
                this._rebuildOscillators();
            }
        }
    }

    setColor(type, color) { this.update(type, null, color); }

    recomputeAll(data) {
        if (!data || data.length < 2) return;

        if (this._overlays.sma) {
            const { params, color } = this._overlays.sma;
            this._setFilteredData(this._overlays.sma.series, IndicatorMath.sma(data, params.period));
            this._overlays.sma.series.applyOptions({ color });
        }
        if (this._overlays.ema) {
            const { params, color } = this._overlays.ema;
            this._setFilteredData(this._overlays.ema.series, IndicatorMath.ema(data, params.period));
            this._overlays.ema.series.applyOptions({ color });
        }
        if (this._overlays.bollinger) {
            const { params, color } = this._overlays.bollinger;
            const bands = IndicatorMath.bollinger(data, params.period, params.mult);
            const c = color || '#9c27b0';
            this._setFilteredData(this._overlays.bollinger.basis, bands.basis);
            this._setFilteredData(this._overlays.bollinger.upper, bands.upper);
            this._setFilteredData(this._overlays.bollinger.lower, bands.lower);
            this._overlays.bollinger.basis.applyOptions({ color: c });
            this._overlays.bollinger.upper.applyOptions({ color: c });
            this._overlays.bollinger.lower.applyOptions({ color: c });
        }

        for (const osc of this._oscillators) {
            if (osc.type === 'rsi' && osc._series) {
                this._setFilteredData(osc._series, IndicatorMath.rsi(data, osc.params.period));
            }
            if (osc.type === 'macd' && osc._macdSeries) {
                const r = IndicatorMath.macd(data, osc.params.fast, osc.params.slow, osc.params.signal);
                this._setFilteredData(osc._macdSeries,   r.macd);
                this._setFilteredData(osc._signalSeries, r.signal);
                this._setFilteredData(osc._histSeries,   r.hist);
            }
            if (osc.type === 'stochastic' && osc._kSeries) {
                const r = IndicatorMath.stochastic(data, osc.params.period, osc.params.smoothK, osc.params.smoothD);
                this._setFilteredData(osc._kSeries, r.k);
                this._setFilteredData(osc._dSeries, r.d);
            }
        }
    }

    serialize() {
        const result = [];
        for (const [type, entry] of Object.entries(this._overlays))
            result.push({ type, params: entry.params, color: entry.color, visible: true });
        for (const osc of this._oscillators)
            result.push({ type: osc.type, params: osc.params, color: osc.color || null, visible: true });
        return result;
    }

    loadFrom(configArray, data) {
        if (!configArray || !configArray.length) return;
        for (const cfg of configArray)
            if (cfg.visible !== false) this.add(cfg.type, cfg.params, cfg.color);
        if (data && data.length) this.recomputeAll(data);
    }

    destroyAll() {
        for (const type of Object.keys(this._overlays)) this._removeOverlay(type);
        this._oscillators = [];
        this._rebuildOscillators();
        this._legendEl.innerHTML = '';
    }

    // ---- Internal ----

    _isOverlay(type) { return type === 'sma' || type === 'ema' || type === 'bollinger'; }

    _addOverlay(type, params, color) {
        const LWC = window.LightweightCharts;
        const base = { priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false };

        if (type === 'sma') {
            const c = color || '#2962FF';
            const series = this.chart.addSeries(LWC.LineSeries, { ...base, color: c, lineWidth: 2 }, 0);
            this._overlays.sma = { series, params: params || { period: 20 }, color: c };
        }
        if (type === 'ema') {
            const c = color || '#ff9800';
            const series = this.chart.addSeries(LWC.LineSeries, { ...base, color: c, lineWidth: 2 }, 0);
            this._overlays.ema = { series, params: params || { period: 21 }, color: c };
        }
        if (type === 'bollinger') {
            const c = color || '#9c27b0';
            const basis = this.chart.addSeries(LWC.LineSeries, { ...base, color: c, lineWidth: 1, lineStyle: LWC.LineStyle.Dashed }, 0);
            const upper = this.chart.addSeries(LWC.LineSeries, { ...base, color: c, lineWidth: 1 }, 0);
            const lower = this.chart.addSeries(LWC.LineSeries, { ...base, color: c, lineWidth: 1 }, 0);
            this._overlays.bollinger = { basis, upper, lower, params: params || { period: 20, mult: 2 }, color: c };
        }
    }

    _removeOverlay(type) {
        const entry = this._overlays[type];
        if (!entry) return;
        if (type === 'bollinger') {
            try { this.chart.removeSeries(entry.basis); } catch(e) {}
            try { this.chart.removeSeries(entry.upper); } catch(e) {}
            try { this.chart.removeSeries(entry.lower); } catch(e) {}
        } else {
            try { this.chart.removeSeries(entry.series); } catch(e) {}
        }
        delete this._overlays[type];
    }

    _teardownAllOscillatorSeries() {
        for (const osc of this._oscillators) {
            const keys = ['_series', '_kSeries', '_dSeries', '_macdSeries', '_signalSeries', '_histSeries'];
            for (const k of keys) {
                if (osc[k]) { try { this.chart.removeSeries(osc[k]); } catch(e) {} osc[k] = null; }
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

        this._oscillators.forEach((osc, idx) => {
            const paneIndex = idx + 1;

            if (osc.type === 'rsi') {
                const series = this.chart.addSeries(LWC.LineSeries, {
                    color: osc.color || '#e91e63', lineWidth: 2,
                    priceLineVisible: false, lastValueVisible: true,
                    autoscaleInfoProvider: fixedScale(0, 100)
                }, paneIndex);
                series.createPriceLine({ price: 80, color: '#888', lineWidth: 1, lineStyle: LWC.LineStyle.Dashed, axisLabelVisible: true });
                series.createPriceLine({ price: 20, color: '#888', lineWidth: 1, lineStyle: LWC.LineStyle.Dashed, axisLabelVisible: true });
                series.createPriceLine({ price: 50, color: '#555', lineWidth: 1, lineStyle: LWC.LineStyle.Dashed, axisLabelVisible: false });
                osc._series = series;
                const panes2 = this.chart.panes();
                if (panes2[paneIndex]) try { panes2[paneIndex].setHeight(paneH); } catch(e) {}
            }

            if (osc.type === 'macd') {
                const macdSeries   = this.chart.addSeries(LWC.LineSeries,      { color: '#2962FF', lineWidth: 2, priceLineVisible: false, lastValueVisible: true  }, paneIndex);
                const signalSeries = this.chart.addSeries(LWC.LineSeries,      { color: '#ff9800', lineWidth: 2, priceLineVisible: false, lastValueVisible: true  }, paneIndex);
                const histSeries   = this.chart.addSeries(LWC.HistogramSeries, {                                 priceLineVisible: false, lastValueVisible: false }, paneIndex);
                macdSeries.createPriceLine({ price: 0, color: '#555', lineWidth: 1, lineStyle: LWC.LineStyle.Solid, axisLabelVisible: false });
                osc._macdSeries = macdSeries; osc._signalSeries = signalSeries; osc._histSeries = histSeries;
                const panes2 = this.chart.panes();
                if (panes2[paneIndex]) try { panes2[paneIndex].setHeight(paneH); } catch(e) {}
            }

            if (osc.type === 'stochastic') {
                const p = osc.params || { period: 14, smoothK: 3, smoothD: 3 };
                const kColor = osc.color || '#2962FF';
                const dColor = '#ff9800';

                const kSeries = this.chart.addSeries(LWC.LineSeries, {
                    color: kColor, lineWidth: 2,
                    priceLineVisible: false, lastValueVisible: true,
                    autoscaleInfoProvider: fixedScale(0, 100)
                }, paneIndex);
                const dSeries = this.chart.addSeries(LWC.LineSeries, {
                    color: dColor, lineWidth: 2,
                    priceLineVisible: false, lastValueVisible: true,
                    autoscaleInfoProvider: fixedScale(0, 100)
                }, paneIndex);

                // 80/20 reference lines
                kSeries.createPriceLine({ price: 80, color: '#888', lineWidth: 1, lineStyle: LWC.LineStyle.Dashed, axisLabelVisible: true });
                kSeries.createPriceLine({ price: 20, color: '#888', lineWidth: 1, lineStyle: LWC.LineStyle.Dashed, axisLabelVisible: true });
                kSeries.createPriceLine({ price: 50, color: '#555', lineWidth: 1, lineStyle: LWC.LineStyle.Dashed, axisLabelVisible: false });

                osc._kSeries = kSeries;
                osc._dSeries = dSeries;

                const panes2 = this.chart.panes();
                if (panes2[paneIndex]) try { panes2[paneIndex].setHeight(paneH); } catch(e) {}
            }
        });
    }

    _setFilteredData(series, points) {
        if (!series) return;
        try { series.setData(points.filter(p => p.value !== null)); } catch(e) {}
    }
}

window.IndicatorManager = IndicatorManager;
