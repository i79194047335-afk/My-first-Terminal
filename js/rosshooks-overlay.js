// ============================================================================
// RossHooksOverlay — разметка стратегии Джо Росса (свинги → 1-2-3 → RH/RHR)
// ============================================================================
//
// Это НЕ побарный детектор хаёв/лоёв, а структурная разметка по свингам:
//
//   1. ЗигЗаг по порогу (mult × ATR) → чистые свинг-пивоты H/L. Порог задаёт
//      МАСШТАБ свинга — это и есть «период» разметки.
//   2. Классификация каждого свинга относительно предыдущего одноимённого:
//      HH / HL (выше) или LH / LL (ниже). Ап-тренд = HH+HL, даун = LH+LL.
//   3. Формация 1-2-3 — смена тренда:
//        top:    1 = swing High, 2 = swing Low, 3 = Lower High;
//                пробой уровня точки 2 ВНИЗ подтверждает даун-тренд;
//        bottom: 1 = swing Low, 2 = swing High, 3 = Higher Low;
//                пробой точки 2 ВВЕРХ подтверждает ап-тренд.
//   4. RH (Ross Hook) — крюк ПРОДОЛЖЕНИЯ: трендовый экстремум внутри
//      подтверждённого тренда (в дауне — swing Low, в апе — swing High).
//      Пробой уровня в сторону тренда = сигнал продолжения.
//   5. RHR (Ross Hook Reversal) — крюк РАЗВОРОТА: точка 3 формации 1-2-3.
//
// Слои (структура / 1-2-3 / RH / RHR) рисуются независимо (params.layers).
// Ничего не хранится: всё выводится из массива свечей, пересчёт на новый бар.
// Работает на ЛЮБОМ инструменте — нужен только OHLC.

class RossHooksOverlay {

    static UP_COLOR   = "#26a69a";   // ап-тренд / бычьи элементы
    static DOWN_COLOR = "#ef5350";   // даун-тренд / медвежьи элементы
    static ZIGZAG     = "#8892a0";   // линия зигзага (нейтральная)
    static BROKEN_ALPHA = 0.30;

    constructor(paneId, chart, series, wrapper) {
        this.paneId  = paneId;
        this.chart   = chart;
        this.series  = series;
        this.wrapper = wrapper;

        this.enabled = false;
        this.data    = [];

        // Результаты пересчёта.
        this.swings   = [];   // [{type:'H'|'L', index, time, price, hl}]
        this.patterns = [];   // [{kind, p1,p2,p3, level, levelTime, breakTime, dir}]
        this.rhList   = [];   // [{dir:'up'|'down', time, price, brokenTime}]
        this.rhrList  = [];   // [{dir:'up'|'down', time, price}]

        // Параметры. swingMult × ATR(atrPeriod) — порог разворота зигзага.
        // Меньше → мельче свинги (чувствительнее), больше → крупнее.
        this.params = {
            swingMode: 'atr',       // 'atr' | 'pct'
            swingMult: 2.0,         // × ATR (режим atr)
            swingPct:  0.5,         // %     (режим pct / фолбэк до прогрева ATR)
            atrPeriod: 14,
            layers: { structure: true, pattern123: true, rh: true, rhr: true },
        };

        this.canvas = document.createElement("canvas");
        this.ctx    = this.canvas.getContext("2d");
        this.canvas.style.position      = "absolute";
        this.canvas.style.left          = "0";
        this.canvas.style.top           = "0";
        this.canvas.style.pointerEvents = "none";
        this.canvas.style.zIndex        = "880";
        wrapper.appendChild(this.canvas);

        this.resize();
        this._onResize = () => this.resize();
        window.addEventListener("resize", this._onResize);
        chart.timeScale().subscribeVisibleTimeRangeChange(() => this.render());
    }

    setEnabled(on) { this.enabled = !!on; this.render(); }

    // setData — принять свечи, пересчитать всю разметку, перерисовать.
    setData(data) {
        this.data = Array.isArray(data) ? data : [];
        this._recompute();
        this.render();
    }

    // setParams — слить параметры (в т.ч. layers), пересчитать, перерисовать.
    setParams(p) {
        if (!p) return;
        const layers = { ...this.params.layers, ...(p.layers || {}) };
        this.params = { ...this.params, ...p, layers };
        this._recompute();
        this.render();
    }

    resize() {
        this.canvas.width  = this.wrapper.clientWidth;
        this.canvas.height = this.wrapper.clientHeight;
        this.render();
    }

    destroy() {
        window.removeEventListener("resize", this._onResize);
        this.canvas.remove();
    }

    priceAreaBottom() {
        try {
            const p0 = this.chart.panes()[0];
            const h  = p0 && p0.getHeight && p0.getHeight();
            if (h && h > 0) return h;
        } catch (e) {}
        return this.canvas.height - this.chart.timeScale().height();
    }

    // ------------------------------------------------------------------
    // Пересчёт: ATR → зигзаг → классификация → 1-2-3 / тренд → RH / RHR.
    // ------------------------------------------------------------------
    _recompute() {
        this.swings = []; this.patterns = []; this.rhList = []; this.rhrList = [];
        const d = this.data;
        if (d.length < 5) return;

        const atr = this._atr(d, this.params.atrPeriod);
        this.swings = this._zigzag(d, atr);
        if (this.swings.length < 2) return;

        this._classify(this.swings);
        this._detect(d, this.swings);
    }

    // _atr — True Range + сглаживание Уайлдера, в единицах цены (для порога
    // зигзага). null до накопления period значений.
    _atr(d, period) {
        const n = d.length;
        const out = new Array(n).fill(null);
        if (n < period + 1) return out;
        const tr = new Array(n);
        tr[0] = d[0].high - d[0].low;
        for (let i = 1; i < n; i++) {
            const h = d[i].high, l = d[i].low, pc = d[i - 1].close;
            tr[i] = Math.max(h - l, Math.abs(h - pc), Math.abs(l - pc));
        }
        let sum = 0;
        for (let i = 1; i <= period; i++) sum += tr[i];
        let prev = sum / period;
        out[period] = prev;
        for (let i = period + 1; i < n; i++) {
            prev = (prev * (period - 1) + tr[i]) / period;
            out[i] = prev;
        }
        return out;
    }

    // _threshold — порог разворота зигзага в цене на баре i.
    _threshold(d, atr, i) {
        const p = this.params;
        if (p.swingMode === 'atr' && atr[i] != null && atr[i] > 0) {
            return p.swingMult * atr[i];
        }
        // Фолбэк (режим pct или ATR ещё не прогрелся): % от цены.
        return d[i].close * (p.swingPct / 100);
    }

    // _zigzag — свинг-пивоты по порогу разворота. Возвращает чередующиеся
    // {type:'H'|'L', index, time, price}. Направление первого свинга выбирается
    // тем порогом, который сработал раньше.
    _zigzag(d, atr) {
        const n = d.length;
        const piv = [];
        let trend = null;                 // 'up' = ищем High, 'down' = ищем Low
        let hiIdx = 0, hiPrice = d[0].high;
        let loIdx = 0, loPrice = d[0].low;

        // Пересобрать противоположный экстремум на отрезке [from..to].
        const minLow = (from, to) => {
            let idx = from, val = d[from].low;
            for (let k = from + 1; k <= to; k++) if (d[k].low < val) { val = d[k].low; idx = k; }
            return { idx, val };
        };
        const maxHigh = (from, to) => {
            let idx = from, val = d[from].high;
            for (let k = from + 1; k <= to; k++) if (d[k].high > val) { val = d[k].high; idx = k; }
            return { idx, val };
        };

        for (let i = 1; i < n; i++) {
            const th = this._threshold(d, atr, i);
            if (d[i].high > hiPrice) { hiPrice = d[i].high; hiIdx = i; }
            if (d[i].low  < loPrice) { loPrice = d[i].low;  loIdx = i; }

            if (trend === 'up' || trend === null) {
                // Ищем разворот ВНИЗ: падение от текущего максимума >= порога.
                if (hiPrice - d[i].low >= th && hiIdx < i) {
                    piv.push({ type: 'H', index: hiIdx, time: d[hiIdx].time, price: hiPrice });
                    trend = 'down';
                    const lo = minLow(hiIdx, i);   // низ ноги — с вершины по текущий бар
                    loIdx = lo.idx; loPrice = lo.val;
                    continue;
                }
            }
            if (trend === 'down' || trend === null) {
                // Ищем разворот ВВЕРХ: рост от текущего минимума >= порога.
                if (d[i].high - loPrice >= th && loIdx < i) {
                    piv.push({ type: 'L', index: loIdx, time: d[loIdx].time, price: loPrice });
                    trend = 'up';
                    const hi = maxHigh(loIdx, i);
                    hiIdx = hi.idx; hiPrice = hi.val;
                    continue;
                }
            }
        }
        return piv;
    }

    // _classify — проставить каждому свингу hl: HH/HL/LH/LL относительно
    // предыдущего свинга ТОГО ЖЕ типа. Первый каждого типа — null (нет опоры).
    _classify(sw) {
        let lastH = null, lastL = null;
        for (const s of sw) {
            if (s.type === 'H') {
                s.hl = lastH == null ? null : (s.price > lastH ? 'HH' : 'LH');
                lastH = s.price;
            } else {
                s.hl = lastL == null ? null : (s.price < lastL ? 'LL' : 'HL');
                lastL = s.price;
            }
        }
    }

    // _detect — проход по свингам с СОСТОЯНИЕМ ТРЕНДА. Формация 1-2-3
    // регистрируется, только если она РАЗВОРАЧИВАЕТ текущий тренд (top в апе,
    // bottom в дауне) — иначе это откат-продолжение, а не разворот. После
    // разворота тренд переключается, скан продолжается ОТ точки 3 (без
    // перекрытий). RH собираются один раз на сегмент между разворотами.
    _detect(d, sw) {
        let trend = null;                 // 'up' | 'down' | null
        let k = 0;
        while (k + 2 < sw.length) {
            const a = sw[k], b = sw[k + 1], c = sw[k + 2];
            let matched = false;

            // 1-2-3 TOP (разворот вниз): H → L → LH, пробой точки 2 вниз.
            if (trend !== 'down' &&
                a.type === 'H' && b.type === 'L' && c.type === 'H' && c.price < a.price) {
                const brk = this._breakBelow(d, c.index, b.price);
                if (brk != null) {
                    this.patterns.push({ kind: 'top', p1: a, p2: b, p3: c, p3k: k + 2,
                        level: b.price, levelTime: b.time, breakTime: d[brk].time, dir: 'down' });
                    this.rhrList.push({ dir: 'down', time: c.time, price: c.price });
                    trend = 'down'; k += 2; matched = true;
                }
            }
            // 1-2-3 BOTTOM (разворот вверх): L → H → HL, пробой точки 2 вверх.
            if (!matched && trend !== 'up' &&
                a.type === 'L' && b.type === 'H' && c.type === 'L' && c.price > a.price) {
                const brk = this._breakAbove(d, c.index, b.price);
                if (brk != null) {
                    this.patterns.push({ kind: 'bottom', p1: a, p2: b, p3: c, p3k: k + 2,
                        level: b.price, levelTime: b.time, breakTime: d[brk].time, dir: 'up' });
                    this.rhrList.push({ dir: 'up', time: c.time, price: c.price });
                    trend = 'up'; k += 2; matched = true;
                }
            }
            if (!matched) k++;
        }

        // RH по сегментам: между точкой 3 текущего разворота и точкой 1
        // следующего (или до конца). Трендовые экстремумы: даун → swing Low,
        // ап → swing High. Диапазоны не пересекаются → дублей нет.
        for (let i = 0; i < this.patterns.length; i++) {
            const pat = this.patterns[i];
            const from = pat.p3k + 1;
            const to = (i + 1 < this.patterns.length)
                ? this.patterns[i + 1].p3k - 2   // до точки 1 следующей формации
                : sw.length - 1;
            this._collectRH(d, sw, from, to, pat.dir);
        }
    }

    // _collectRH — трендовые крюки-продолжения на отрезке свингов [from..to].
    // В дауне RH — swing Low, в апе — swing High. Крюк «пробит», когда цена
    // прошла его уровень в сторону тренда (в дауне ниже, в апе выше).
    _collectRH(d, sw, from, to, dir) {
        const wantType = dir === 'down' ? 'L' : 'H';
        for (let k = from; k <= to && k < sw.length; k++) {
            const s = sw[k];
            if (s.type !== wantType) continue;
            const price = s.price;
            let brokenTime = null;
            for (let j = s.index + 1; j < d.length; j++) {
                if (dir === 'down' ? d[j].low < price : d[j].high > price) { brokenTime = d[j].time; break; }
            }
            this.rhList.push({ dir, time: s.time, price, brokenTime });
        }
    }

    _breakBelow(d, fromIdx, level) {
        for (let j = fromIdx + 1; j < d.length; j++) if (d[j].low < level) return j;
        return null;
    }
    _breakAbove(d, fromIdx, level) {
        for (let j = fromIdx + 1; j < d.length; j++) if (d[j].high > level) return j;
        return null;
    }

    // ------------------------------------------------------------------
    // Отрисовка слоёв.
    // ------------------------------------------------------------------
    render() {
        const ctx = this.ctx;
        ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
        if (!this.enabled) return;

        const ts = this.chart.timeScale();
        const rightEdge = ts.width();
        const bottom = this.priceAreaBottom();
        const L = this.params.layers;

        ctx.save();
        ctx.beginPath();
        ctx.rect(0, 0, rightEdge, bottom);
        ctx.clip();

        const X = t => ts.timeToCoordinate(t);
        const Y = p => this.series.priceToCoordinate(p);

        if (L.structure)  this._drawStructure(ctx, X, Y, bottom);
        if (L.pattern123) this._drawPatterns(ctx, X, Y, rightEdge, bottom);
        if (L.rh)         this._drawRH(ctx, X, Y, rightEdge, bottom);
        if (L.rhr)        this._drawRHR(ctx, X, Y, bottom);

        ctx.restore();
        ctx.globalAlpha = 1;
    }

    // Зигзаг-линия + подписи HH/HL/LH/LL.
    _drawStructure(ctx, X, Y, bottom) {
        const sw = this.swings;
        if (sw.length < 2) return;
        ctx.strokeStyle = RossHooksOverlay.ZIGZAG;
        ctx.lineWidth = 1;
        ctx.globalAlpha = 0.7;
        ctx.beginPath();
        let started = false;
        for (const s of sw) {
            const x = X(s.time), y = Y(s.price);
            if (x == null || y == null) { started = false; continue; }
            if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
        }
        ctx.stroke();
        ctx.globalAlpha = 1;

        ctx.font = "9px sans-serif";
        for (const s of sw) {
            if (!s.hl) continue;
            const x = X(s.time), y = Y(s.price);
            if (x == null || y == null) continue;
            const up = s.type === 'H';
            ctx.fillStyle = (s.hl === 'HH' || s.hl === 'HL') ? RossHooksOverlay.UP_COLOR
                                                             : RossHooksOverlay.DOWN_COLOR;
            ctx.textBaseline = up ? "bottom" : "top";
            ctx.textAlign = "center";
            ctx.fillText(s.hl, x, y + (up ? -4 : 4));
        }
        ctx.textAlign = "start";
    }

    // Точки 1-2-3 + горизонтальный уровень точки 2 (SELL/BUY) до бара пробоя.
    _drawPatterns(ctx, X, Y, rightEdge, bottom) {
        ctx.font = "bold 11px sans-serif";
        for (const pat of this.patterns) {
            const color = pat.dir === 'down' ? RossHooksOverlay.DOWN_COLOR : RossHooksOverlay.UP_COLOR;
            // Уровень точки 2.
            const y = Y(pat.level);
            let x1 = X(pat.levelTime), x2 = X(pat.breakTime);
            if (y != null) {
                const sx = x1 == null ? 0 : x1;
                let ex = x2 == null ? rightEdge : x2;
                ex = Math.min(Math.max(ex, 0), rightEdge);
                if (ex > 0) {
                    ctx.strokeStyle = color; ctx.lineWidth = 1.2;
                    ctx.setLineDash([5, 3]);
                    ctx.beginPath(); ctx.moveTo(Math.max(0, sx), y); ctx.lineTo(ex, y); ctx.stroke();
                    ctx.setLineDash([]);
                    ctx.fillStyle = color; ctx.font = "9px sans-serif";
                    ctx.textBaseline = pat.dir === 'down' ? "bottom" : "top";
                    ctx.fillText(pat.dir === 'down' ? "SELL" : "BUY", Math.max(2, sx) + 2, y + (pat.dir === 'down' ? -2 : 2));
                }
            }
            // Метки 1 / 2 / 3.
            ctx.font = "bold 11px sans-serif";
            ctx.fillStyle = color;
            for (const [lbl, p] of [["1", pat.p1], ["2", pat.p2], ["3", pat.p3]]) {
                const px = X(p.time), py = Y(p.price);
                if (px == null || py == null) continue;
                const up = p.type === 'H';
                ctx.textBaseline = up ? "bottom" : "top";
                ctx.textAlign = "center";
                ctx.fillText(lbl, px, py + (up ? -12 : 12));
            }
        }
        ctx.textAlign = "start";
    }

    // Уровни RH: сплошной до правого края (активный) / бледный до пробоя.
    _drawRH(ctx, X, Y, rightEdge, bottom) {
        ctx.font = "9px sans-serif";
        for (const h of this.rhList) {
            const y = Y(h.price);
            if (y == null || y < 0 || y > bottom) continue;
            const active = h.brokenTime == null;
            const px = X(h.time);
            let ex = active ? rightEdge : X(h.brokenTime);
            if (ex == null) ex = active ? rightEdge : (px == null ? 0 : px);
            if (ex < 0) continue;
            ex = Math.min(ex, rightEdge);
            const startX = px == null ? 0 : px;

            const color = h.dir === 'up' ? RossHooksOverlay.UP_COLOR : RossHooksOverlay.DOWN_COLOR;
            ctx.globalAlpha = active ? 1 : RossHooksOverlay.BROKEN_ALPHA;
            ctx.strokeStyle = color;
            ctx.lineWidth = active ? 1.5 : 1;
            ctx.beginPath(); ctx.moveTo(Math.max(0, startX), y); ctx.lineTo(ex, y); ctx.stroke();

            if (active && px != null && px >= 0 && px <= rightEdge) {
                ctx.fillStyle = color;
                ctx.textBaseline = h.dir === 'up' ? "bottom" : "top";
                ctx.fillText("RH", px + 4, y + (h.dir === 'up' ? -3 : 3));
            }
        }
        ctx.globalAlpha = 1;
    }

    // Метка RHR у точки разворота (кружок + подпись).
    _drawRHR(ctx, X, Y, bottom) {
        ctx.font = "bold 9px sans-serif";
        for (const r of this.rhrList) {
            const x = X(r.time), y = Y(r.price);
            if (x == null || y == null || y < 0 || y > bottom) continue;
            const color = r.dir === 'up' ? RossHooksOverlay.UP_COLOR : RossHooksOverlay.DOWN_COLOR;
            ctx.strokeStyle = color; ctx.lineWidth = 1.5;
            ctx.beginPath(); ctx.arc(x, y, 5, 0, Math.PI * 2); ctx.stroke();
            ctx.fillStyle = color;
            ctx.textBaseline = r.dir === 'down' ? "bottom" : "top";
            ctx.fillText("RHR", x + 7, y + (r.dir === 'down' ? -4 : 4));
        }
    }
}

window.RossHooksOverlay = RossHooksOverlay;
