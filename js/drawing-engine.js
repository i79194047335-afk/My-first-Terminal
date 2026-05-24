
// ============================================================================
// DrawingEngine — TIME based overlay
// ============================================================================

class DrawingEngine {
	
    constructor(paneId, chart, series, wrapper) {

        this.paneId = paneId;
        this.chart = chart;
        this.series = series;
        this.wrapper = wrapper;

        this.timeMapper = new TimeMapper(chart, series);

        this.canvas = document.createElement("canvas");
        this.ctx = this.canvas.getContext("2d");

        this.canvas.style.position = "absolute";
        this.canvas.style.left = "0";
        this.canvas.style.top = "0";
        this.canvas.style.pointerEvents = "none";
        this.canvas.style.zIndex = "900";

        wrapper.appendChild(this.canvas);

        this.drawings = [];

        this.resize();
        window.addEventListener("resize", () => this.resize());

        chart.timeScale().subscribeVisibleTimeRangeChange(() => this.render());
        chart.subscribeCrosshairMove(() => this.render());
		this.renderPending = false;

		    }

    resize() {
        this.canvas.width = this.wrapper.clientWidth;
        this.canvas.height = this.wrapper.clientHeight;
        this.render();
    }

    setTimeframe(tfSeconds) {
        this.timeMapper.setTimeframe(tfSeconds);
    }

    addDrawing(obj) {
        obj.selected = false;
        this.drawings.push(obj);
        this.render();
    }

    render() {
		
		if (this.renderPending) return;

    this.renderPending = true;

    requestAnimationFrame(() => {

        const ctx = this.ctx;
        ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);

        for (let obj of this.drawings) {

            if (obj.type === "line") {

            const p1 = obj.points[0];
            const p2 = obj.points[1];

            let x1 = this.timeMapper.getX(p1.time);
            let y1 = this.series.priceToCoordinate(p1.price);

            let x2 = this.timeMapper.getX(p2.time);
            let y2 = this.series.priceToCoordinate(p2.price);

            if (y1 === null || y2 === null) continue;
		
            // если линия ушла за временной диапазон — просто не рисуем,
            // но НЕ обрываем полностью
            if (x1 === null && x2 === null) continue;
			
			// ---- ограничение по правой границе ----
const rightLimit = this.canvas.width - 60;

// если обе точки правее границы — вообще не рисуем
if (x1 > rightLimit && x2 > rightLimit) {
    continue;
}

// если вторая точка ушла за границу
if (x2 > rightLimit) {
    const ratio = (rightLimit - x1) / (x2 - x1);
    x2 = rightLimit;
    y2 = y1 + (y2 - y1) * ratio;
}

// если первая точка ушла за границу
if (x1 > rightLimit) {
    const ratio = (rightLimit - x2) / (x1 - x2);
    x1 = rightLimit;
    y1 = y2 + (y1 - y2) * ratio;
}

            // если одна из точек вне диапазона — всё равно пробуем рисовать

					ctx.beginPath();
			ctx.strokeStyle = obj.selected
			? "#2962FF"
			: (obj.color || "#000000");

			ctx.lineWidth = obj.selected
				? (obj.width || 2) + 1
				: (obj.width || 2);

			const drawX1 = x1 ?? -10000;
			const drawX2 = x2 ?? -10000;

			ctx.moveTo(drawX1, y1);
			ctx.lineTo(drawX2, y2);

// ───────── ПРОДЛЕНИЕ ВПРАВО С ОБРЕЗКОЙ ─────────
if (
    x1 !== null &&
    x2 !== null &&
    x1 !== x2
) {

    const slope = (y2 - y1) / (x2 - x1);
    const rightX = this.chart.timeScale().width();

    const top = 0;
    const bottom =
        this.canvas.height - this.chart.timeScale().height();

    let targetX = rightX;
    let targetY = y1 + slope * (rightX - x1);

    if (targetY > bottom) {
        const xAtBottom =
            x1 + (bottom - y1) / slope;
        targetX = xAtBottom;
        targetY = bottom;
    }

    if (targetY < top) {
        const xAtTop =
            x1 + (top - y1) / slope;
        targetX = xAtTop;
        targetY = top;
    }

    ctx.lineTo(targetX, targetY);
}
// ────────────────────────────────────────────────

ctx.stroke();

            if (obj.selected) {

                const radius = 4;

                ctx.fillStyle = "#2962FF";

                ctx.beginPath();
                ctx.arc(x1, y1, radius, 0, Math.PI * 2);
                ctx.fill();

                ctx.beginPath();
                ctx.arc(x2, y2, radius, 0, Math.PI * 2);
                ctx.fill();
            }
			} 
// ================= RECTANGLE =================
if (obj.type === "rect") {

    const p1 = obj.points[0];
    const p2 = obj.points[1];

    let x1 = this.timeMapper.getX(p1.time);
    let y1 = this.series.priceToCoordinate(p1.price);
    let x2 = this.timeMapper.getX(p2.time);
    let y2 = this.series.priceToCoordinate(p2.price);

    if (x1 === null || x2 === null || y1 === null || y2 === null)
        continue;

    const left   = Math.min(x1, x2);
    const right  = Math.max(x1, x2);
    const top    = Math.min(y1, y2);
    const bottom = Math.max(y1, y2);

    // ---- Продление вправо ----
    let extendedRight = right;
    if (obj.extendRight) {
        extendedRight = this.canvas.width;
    }

    // ---- CLIP ----
    ctx.save();

    const paneWidth = this.chart.timeScale().width();
    const paneHeight = this.canvas.height;

    ctx.beginPath();
    ctx.rect(0, 0, paneWidth, paneHeight);
    ctx.clip();

    // ---- ЗАЛИВКА (НЕ продлевается) ----
    ctx.fillStyle = obj.fillColor || "rgba(41,98,255,0.15)";
    ctx.fillRect(
    left,
    top,
    (obj.extendRight ? extendedRight : right) - left,
    bottom - top
);


    // ---- ГРАНИЦЫ ----
    ctx.strokeStyle = obj.selected
        ? "#2962FF"
        : (obj.color || "#000000");

    ctx.lineWidth = obj.selected
        ? (obj.width || 2) + 1
        : (obj.width || 2);

    ctx.beginPath();

    // Левая
    ctx.moveTo(left, top);
    ctx.lineTo(left, bottom);

    // Правая
    ctx.moveTo(right, top);
    ctx.lineTo(right, bottom);

    // Верхняя (может продлеваться)
    ctx.moveTo(left, top);
    ctx.lineTo(extendedRight, top);

    // Нижняя (может продлеваться)
    ctx.moveTo(left, bottom);
    ctx.lineTo(extendedRight, bottom);

    ctx.stroke();

    // ---- Средняя линия (ВНУТРИ clip!) ----
    if (obj.showMidline) {

        const midY = (top + bottom) / 2;

        ctx.beginPath();
        ctx.strokeStyle = obj.midColor || "#888888";
        ctx.lineWidth = obj.midWidth || 1;
        ctx.setLineDash([4, 4]);

        ctx.moveTo(left, midY);
        ctx.lineTo(obj.extendRight ? extendedRight : right, midY);

        ctx.stroke();
        ctx.setLineDash([]);
    }

    // ---- Точки редактирования ----
    if (obj.selected) {

        const radius = 4;
        ctx.fillStyle = "#2962FF";

        ctx.beginPath();
        ctx.arc(x1, y1, radius, 0, Math.PI * 2);
        ctx.fill();

        ctx.beginPath();
        ctx.arc(x2, y2, radius, 0, Math.PI * 2);
        ctx.fill();
    }

    ctx.restore();   // ✅ В САМОМ КОНЦЕ
}


		
		}

        this.renderPending = false;

    });

}


   hitTestLine(x, y) {

    const threshold = 6;

    for (let obj of this.drawings) {
		
		  // ===== RECT HIT TEST =====
if (obj.type === "rect") {

    const p1 = obj.points[0];
    const p2 = obj.points[1];

    const x1 = this.timeMapper.getX(p1.time);
    const y1 = this.series.priceToCoordinate(p1.price);
    const x2 = this.timeMapper.getX(p2.time);
    const y2 = this.series.priceToCoordinate(p2.price);

    if (x1 === null || x2 === null || y1 === null || y2 === null)
        continue;

    const left   = Math.min(x1, x2);
    const right  = Math.max(x1, x2);
    const top    = Math.min(y1, y2);
    const bottom = Math.max(y1, y2);

    let testRight = right;

    if (obj.extendRight) {
        testRight = this.canvas.width;
    }

    // проверяем попадание внутрь зоны
    if (
        x >= left &&
        x <= testRight &&
        y >= top &&
        y <= bottom
    ) {
        return obj;
    }

    continue;
}


        // ---------- LINE ----------
        if (obj.type === "line") {

            const p1 = obj.points[0];
            const p2 = obj.points[1];

            const x1 = this.timeMapper.getX(p1.time);
            const y1 = this.series.priceToCoordinate(p1.price);
            const x2 = this.timeMapper.getX(p2.time);
            const y2 = this.series.priceToCoordinate(p2.price);

            if (
                x1 === null || x2 === null ||
                y1 === null || y2 === null
            ) continue;

            const A = x - x1;
            const B = y - y1;
            const C = x2 - x1;
            const D = y2 - y1;

            const dot = A * C + B * D;
            const lenSq = C * C + D * D;
            const param = lenSq !== 0 ? dot / lenSq : -1;

            let xx, yy;

            if (param < 0) {
                xx = x1;
                yy = y1;
            } else {
                xx = x1 + param * C;
                yy = y1 + param * D;
            }

            const dx = x - xx;
            const dy = y - yy;
            const dist = Math.sqrt(dx * dx + dy * dy);

            if (dist < threshold) return obj;
        }

        // ---------- RECTANGLE ----------
        if (obj.type === "rect") {

            const p1 = obj.points[0];
            const p2 = obj.points[1];

            const x1 = this.timeMapper.getX(p1.time);
            const y1 = this.series.priceToCoordinate(p1.price);
            const x2 = this.timeMapper.getX(p2.time);
            const y2 = this.series.priceToCoordinate(p2.price);

            if (
                x1 === null || x2 === null ||
                y1 === null || y2 === null
            ) continue;

            const left   = Math.min(x1, x2);
            const right  = Math.max(x1, x2);
            const top    = Math.min(y1, y2);
            const bottom = Math.max(y1, y2);

            const nearLeft   = Math.abs(x - left)   < threshold && y >= top && y <= bottom;
            const nearRight  = Math.abs(x - right)  < threshold && y >= top && y <= bottom;
            const nearTop    = Math.abs(y - top)    < threshold && x >= left && x <= right;
            const nearBottom = Math.abs(y - bottom) < threshold && x >= left && x <= right;

            if (nearLeft || nearRight || nearTop || nearBottom) {
                return obj;
            }
        }
    }

    return null;
}

    hitTestPoint(x, y) {

        const radius = 6;

        for (let obj of this.drawings) {

           if (obj.type !== "line" && obj.type !== "rect") continue;

            for (let i = 0; i < obj.points.length; i++) {

                const p = obj.points[i];

                const px = this.timeMapper.getX(p.time);
                const py = this.series.priceToCoordinate(p.price);

                if (px === null || py === null) continue;

                const dx = x - px;
                const dy = y - py;
                const dist = Math.sqrt(dx * dx + dy * dy);

                if (dist < radius) {
                    return {
                        obj: obj,
                        pointIndex: i
                    };
                }
            }
        }

        return null;
    }

    moveLine(obj, deltaTime, deltaPrice) {

        if (!obj || obj.type !== "line") return;

        obj.points = obj.points.map(p => ({
            time: p.time + deltaTime,
            price: p.price + deltaPrice
        }));
    }

getProjectionOnLine(obj, x, y) {

    const p1 = obj.points[0];
    const p2 = obj.points[1];

    const x1 = this.timeMapper.getX(p1.time);
    const y1 = this.series.priceToCoordinate(p1.price);

    const x2 = this.timeMapper.getX(p2.time);
    const y2 = this.series.priceToCoordinate(p2.price);

    if (x1 === null || x2 === null || y1 === null || y2 === null)
        return null;

    const A = x - x1;
    const B = y - y1;
    const C = x2 - x1;
    const D = y2 - y1;

    const lenSq = C * C + D * D;
    if (lenSq === 0) return null;

    const param = (A * C + B * D) / lenSq;

    const projX = x1 + param * C;
    const projY = y1 + param * D;

    return { projX, projY };
}

}

window.DrawingEngine = DrawingEngine;