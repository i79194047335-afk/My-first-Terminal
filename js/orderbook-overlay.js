// ============================================================================
// OrderbookOverlay — гистограмма стакана справа, вплотную к ценовой шкале
// ============================================================================
//
// Рисует стены лимитных заявок: ask над текущей ценой, bid под ней, полосы
// растут ВЛЕВО от ценовой шкалы. Ширина полосы — объём относительно самой
// толстой стены в видимой области.
//
// Данные приходят от хаба раз в секунду срезом ±0.5% от цены (~700 уровней).
// Склейка в корзины делается ЗДЕСЬ, а не на сервере: размер корзины зависит
// от зума, и держать данные в браузере — единственный способ перерисовать
// гистограмму в том же кадре, что и график. Иначе каждый зум и каждое
// перетаскивание ценовой шкалы ждали бы круга до сервера.
//
// Стакан НЕ хранится: последний срез живёт до следующего, история не нужна.

class OrderbookOverlay {

    // Доля ширины графика под гистограмму.
    static WIDTH_RATIO = 0.14;
    // Целевая высота корзины в пикселях: ~50 полос на экран при любом зуме.
    static BUCKET_PX = 4;
    // Прозрачность заливки.
    static ALPHA = 0.5;

    constructor(paneId, chart, series, wrapper) {
        this.paneId  = paneId;
        this.chart   = chart;
        this.series  = series;
        this.wrapper = wrapper;

        this.enabled = false;
        this.book    = null;   // {ts, bids, asks} — последний срез

        this.canvas = document.createElement("canvas");
        this.ctx    = this.canvas.getContext("2d");
        this.canvas.style.position      = "absolute";
        this.canvas.style.left          = "0";
        this.canvas.style.top           = "0";
        this.canvas.style.pointerEvents = "none";
        // Под рисунками (900), над графиком: стакан — фон, а не инструмент.
        this.canvas.style.zIndex        = "850";
        wrapper.appendChild(this.canvas);

        this.resize();
        this._onResize = () => this.resize();
        window.addEventListener("resize", this._onResize);

        // Перерисовка при любом изменении масштаба или сдвиге цены —
        // требование владельца: гистограмма обязана следовать за графиком.
        chart.timeScale().subscribeVisibleTimeRangeChange(() => this.render());
        try {
            series.priceScale().subscribeSizeChange?.(() => this.render());
        } catch (e) { /* не во всех версиях LWC есть */ }
    }

    setEnabled(on) {
        this.enabled = !!on;
        if (!on) this.book = null;
        this.render();
    }

    setBook(msg) {
        this.book = msg;
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

    // Ширина ценовой шкалы в пикселях. Гистограмма рисуется левее неё, чтобы
    // не перекрывать подписи цен. Фолбэк — ноль: хуже наезд, чем пустой
    // экран, если API в этой версии LWC отсутствует.
    priceScaleWidth() {
        try {
            const w = this.series.priceScale().width();
            if (w > 0) return w;
        } catch (e) {}
        return 0;
    }

    // Нижняя граница ЦЕНОВОЙ области: гистограмма не должна залезать в паны
    // индикаторов. Та же логика, что в DrawingEngine.priceAreaBottom().
    priceAreaBottom() {
        try {
            const p0 = this.chart.panes()[0];
            const h  = p0 && p0.getHeight && p0.getHeight();
            if (h && h > 0) return h;
        } catch (e) {}
        return this.canvas.height - this.chart.timeScale().height();
    }

    // Склеить уровни в корзины по ~BUCKET_PX пикселей.
    //
    // Ключевой момент: корзина задаётся ЦЕНОВЫМ шагом, вычисленным из
    // текущего масштаба, поэтому при зуме полосы дробятся, а при отдалении
    // сливаются — число полос на экране остаётся постоянным.
    _bucketize(levels, bottomPx) {
        if (!levels || !levels.length) return [];

        // Цена, приходящаяся на один пиксель — из двух точек ценовой шкалы.
        const pTop = this.series.coordinateToPrice(0);
        const pBot = this.series.coordinateToPrice(bottomPx);
        if (pTop === null || pBot === null) return [];
        const pricePerPx = Math.abs(pTop - pBot) / Math.max(bottomPx, 1);
        if (!(pricePerPx > 0)) return [];

        const step = pricePerPx * OrderbookOverlay.BUCKET_PX;
        const buckets = new Map();
        for (const [price, size] of levels) {
            const key = Math.floor(price / step);
            buckets.set(key, (buckets.get(key) || 0) + size);
        }

        const out = [];
        for (const [key, size] of buckets) {
            out.push({ price: (key + 0.5) * step, size });
        }
        return out;
    }

    render() {
        const ctx = this.ctx;
        ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
        if (!this.enabled || !this.book) return;

        const bottom = this.priceAreaBottom();
        const bids = this._bucketize(this.book.bids, bottom);
        const asks = this._bucketize(this.book.asks, bottom);
        if (!bids.length && !asks.length) return;

        // Масштаб — по самой толстой стене В ВИДИМОЙ ОБЛАСТИ. Стены заметны
        // при любой ликвидности, ценой того, что толщина между моментами
        // времени несравнима.
        const visible = [];
        for (const b of bids.concat(asks)) {
            const y = this.series.priceToCoordinate(b.price);
            if (y === null || y < 0 || y > bottom) continue;
            visible.push({ ...b, y });
        }
        if (!visible.length) return;

        let maxSize = 0;
        for (const b of visible) if (b.size > maxSize) maxSize = b.size;
        if (!(maxSize > 0)) return;

        const fullW = Math.round(this.canvas.width * OrderbookOverlay.WIDTH_RATIO);
        // Правый край — ЛЕВЕЕ ценовой шкалы, а не по краю канваса: иначе
        // полосы наезжают на подписи цен и читать их невозможно.
        const rightX = this.canvas.width - this.priceScaleWidth();
        const h = Math.max(1, OrderbookOverlay.BUCKET_PX - 1);

        // Тот же порог, что делит стороны: цена между лучшими bid и ask.
        const bestBid = this.book.bids.length ? this.book.bids[0][0] : -Infinity;
        const bestAsk = this.book.asks.length ? this.book.asks[0][0] :  Infinity;
        const mid = (bestBid > -Infinity && bestAsk < Infinity)
            ? (bestBid + bestAsk) / 2 : null;

        ctx.save();
        ctx.globalAlpha = OrderbookOverlay.ALPHA;
        for (const b of visible) {
            const isAsk = mid !== null ? b.price > mid : false;
            const w = Math.max(1, Math.round(fullW * (b.size / maxSize)));
            ctx.fillStyle = isAsk ? "#ef5350" : "#26a69a";
            ctx.fillRect(rightX - w, Math.round(b.y) - h / 2, w, h);
        }
        ctx.restore();
    }
}

window.OrderbookOverlay = OrderbookOverlay;
