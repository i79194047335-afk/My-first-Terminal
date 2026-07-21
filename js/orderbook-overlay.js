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
    // Минимальная высота корзины в пикселях — из неё выводится ценовой шаг
    // сетки. 20 подобрано так, чтобы шаг совпадал с интервалом между
    // подписями ценовой шкалы: на масштабе 571–579 / 620px это даёт 0.5,
    // ровно как рисует LWC. Меньше — сетка мельче шкалы и столбцы дробятся
    // на волоски, больше — грубее, и соседние уровни цен слипаются.
    static MIN_BUCKET_PX = 20;
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

    // Ценовой шаг сетки — интервал между соседними подписями на шкале.
    //
    // LWC его не отдаёт, поэтому считаем тем же приёмом, что используют сами
    // шкалы: берём «сырой» шаг из масштаба и округляем вверх до ближайшего
    // красивого числа (1/2/5 × 10^n). Для шкалы 571.5 / 572.0 / 572.5 это
    // даёт ровно 0.5.
    _gridStep(bottomPx) {
        const pTop = this.series.coordinateToPrice(0);
        const pBot = this.series.coordinateToPrice(bottomPx);
        if (pTop === null || pBot === null) return null;

        const pricePerPx = Math.abs(pTop - pBot) / Math.max(bottomPx, 1);
        if (!(pricePerPx > 0)) return null;

        const raw = pricePerPx * OrderbookOverlay.MIN_BUCKET_PX;
        const mag = Math.pow(10, Math.floor(Math.log10(raw)));
        const norm = raw / mag;
        const nice = norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10;
        return nice * mag;
    }

    // Склеить уровни в корзины ПО СЕТКЕ ЦЕНОВОЙ ШКАЛЫ.
    //
    // Одна полоса = один интервал между соседними подписями цен, и в ней
    // суммарный объём всех лимиток этого диапазона. Так столбец читается
    // против шкалы напрямую: видно, сколько заявок стоит между 572.0 и 572.5.
    _bucketize(levels, bottomPx) {
        if (!levels || !levels.length) return [];
        const step = this._gridStep(bottomPx);
        if (!step) return [];

        const buckets = new Map();
        for (const [price, size] of levels) {
            const key = Math.floor(price / step);
            buckets.set(key, (buckets.get(key) || 0) + size);
        }

        const out = [];
        for (const [key, size] of buckets) {
            // Границы корзины — чтобы полоса заняла интервал целиком, а не
            // висела тонкой чертой по центру.
            out.push({
                price: (key + 0.5) * step,
                lo:    key * step,
                hi:    (key + 1) * step,
                size,
            });
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

        // Сторона запоминается ЗДЕСЬ, при сборке, а не выводится потом из
        // цены. Граничная корзина накрывает и bid, и ask одновременно —
        // определяя сторону по цене, мы рисовали их друг поверх друга, и
        // столбцы на стыке слипались в один сплошной блок.
        const visible = [];
        for (const [side, list] of [["bid", bids], ["ask", asks]]) {
            for (const b of list) {
                const y = this.series.priceToCoordinate(b.price);
                if (y === null || y < 0 || y > bottom) continue;
                visible.push({ ...b, y, side });
            }
        }
        if (!visible.length) return;

        let maxSize = 0;
        for (const b of visible) if (b.size > maxSize) maxSize = b.size;
        if (!(maxSize > 0)) return;

        const fullW = Math.round(this.canvas.width * OrderbookOverlay.WIDTH_RATIO);
        // Правый край — ЛЕВЕЕ ценовой шкалы, а не по краю канваса: иначе
        // полосы наезжают на подписи цен и читать их невозможно.
        const rightX = this.canvas.width - this.priceScaleWidth();

        // Спред: граница между сторонами. Корзина, попавшая на него, режется
        // по нему же — иначе bid и ask рисовались бы на одной высоте.
        const bestBid = this.book.bids.length ? this.book.bids[0][0] : null;
        const bestAsk = this.book.asks.length ? this.book.asks[0][0] : null;

        ctx.save();
        ctx.globalAlpha = OrderbookOverlay.ALPHA;
        for (const b of visible) {
            const isAsk = b.side === "ask";
            const w = Math.max(1, Math.round(fullW * (b.size / maxSize)));

            // Ценовые границы полосы. Граничную корзину подрезаем по лучшей
            // цене своей стороны: bid не должен заходить выше лучшего bid,
            // ask — ниже лучшего ask. Без этого столбцы на стыке накрывают
            // друг друга и сливаются в сплошной блок.
            let lo = b.lo, hi = b.hi;
            if (isAsk && bestAsk !== null) lo = Math.max(lo, bestAsk);
            if (!isAsk && bestBid !== null) hi = Math.min(hi, bestBid);
            if (hi <= lo) continue;   // корзина целиком по ту сторону спреда

            // Высота полосы — весь её ценовой интервал, минус пиксель на
            // просвет. Столбец занимает строку между подписями шкалы, а не
            // висит тонкой чертой по центру.
            const yHi = this.series.priceToCoordinate(hi);
            const yLo = this.series.priceToCoordinate(lo);
            let top = Math.round(yHi);
            let h   = Math.max(1, Math.round(Math.abs(yLo - yHi)) - 1);
            if (!isFinite(top)) { top = Math.round(b.y); h = 1; }

            ctx.fillStyle = isAsk ? "#ef5350" : "#26a69a";
            ctx.fillRect(rightX - w, top, w, h);
        }
        ctx.restore();
    }
}

window.OrderbookOverlay = OrderbookOverlay;
