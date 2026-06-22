
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

// ================= POSITION =================
if (obj.type === "position") {

    const sym = (typeof currentSymbol !== "undefined") ? currentSymbol : "";

    const xL = this.timeMapper.getX(obj.t1);
    if (xL === null) continue;

    const barSpacing = this.chart.timeScale().options().barSpacing || 6;
    const paneWidth  = this.chart.timeScale().width();

    let xR = xL + (obj.barsWidth || 40) * barSpacing;
    if (xR > paneWidth) xR = paneWidth;

    const left  = Math.min(xL, xR);
    const right = Math.max(xL, xR);

    const yE = this.series.priceToCoordinate(obj.entry);
    const yS = this.series.priceToCoordinate(obj.stop);
    const yT = this.series.priceToCoordinate(obj.target);
    if (yE === null || yS === null || yT === null) continue;

    ctx.save();
    ctx.beginPath();
    ctx.rect(0, 0, paneWidth, this.canvas.height);
    ctx.clip();

    // ---- Зона прибыли (entry → target), зелёная ----
    ctx.fillStyle = "rgba(8,153,129,0.18)";
    ctx.fillRect(left, Math.min(yE, yT), right - left, Math.abs(yT - yE));

    // ---- Зона риска (entry → stop), красная ----
    ctx.fillStyle = "rgba(242,54,69,0.18)";
    ctx.fillRect(left, Math.min(yE, yS), right - left, Math.abs(yS - yE));

    // ---- Уровни ----
    const drawLevel = (y, color) => {
        ctx.beginPath();
        ctx.strokeStyle = color;
        ctx.lineWidth = obj.selected ? 2 : 1;
        ctx.moveTo(left, y);
        ctx.lineTo(right, y);
        ctx.stroke();
    };
    drawLevel(yT, "#089981");
    drawLevel(yE, obj.selected ? "#2962FF" : "#787b86");
    drawLevel(yS, "#f23645");

    // ---- Хэндлы при выделении ----
    if (obj.selected) {
        const r = 4;
        ctx.fillStyle = "#2962FF";
        [yT, yE, yS].forEach(y => {
            ctx.beginPath();
            ctx.arc(right, y, r, 0, Math.PI * 2);
            ctx.fill();
        });
        ctx.beginPath();
        ctx.arc(left, yE, r, 0, Math.PI * 2);
        ctx.fill();
    }

    ctx.restore();

    // ---- Плашка с расчётом ----
    const m = (window.PositionCalc) ? window.PositionCalc.compute(obj, sym) : null;
    if (m) {
        const rows = [
            (obj.side === "long" ? "LONG" : "SHORT"),
            "R:R   " + m.rr.toFixed(2),
            "Risk  " + m.riskPips.toFixed(1) + "p  −$" + m.riskUSD.toFixed(0),
            "Prof  " + m.rewardPips.toFixed(1) + "p  +$" + m.rewardUSD.toFixed(0),
            "Size  " + m.lots.toFixed(2) + " lot"
        ];

        ctx.font = "11px sans-serif";
        let boxW = 0;
        rows.forEach(t => { boxW = Math.max(boxW, ctx.measureText(t).width); });
        boxW += 14;

        const lineH = 15;
        const boxH  = rows.length * lineH + 8;

        let bx = left + 6;
        let by = Math.min(yE, yT, yS) - boxH - 6;
        if (by < 2) by = Math.max(yE, yT, yS) + 6;
        if (bx + boxW > paneWidth) bx = paneWidth - boxW - 4;
        if (bx < 2) bx = 2;

        ctx.fillStyle = "rgba(20,22,28,0.85)";
        ctx.fillRect(bx, by, boxW, boxH);
        ctx.strokeStyle = obj.side === "long" ? "#089981" : "#f23645";
        ctx.lineWidth = 1;
        ctx.strokeRect(bx, by, boxW, boxH);

        ctx.textBaseline = "top";
        rows.forEach((t, i) => {
            ctx.fillStyle = i === 0
                ? (obj.side === "long" ? "#089981" : "#f23645")
                : "#e6e6e6";
            ctx.fillText(t, bx + 7, by + 5 + i * lineH);
        });
        ctx.textBaseline = "alphabetic";
    }
}



		}

        this.renderPending = false;

    });

}


   hitTestLine(x, y) {

    const threshold = 6;

    for (let obj of this.drawings) {

        // ===== POSITION HIT TEST (тело) =====
        if (obj.type === "position") {

            const xL = this.timeMapper.getX(obj.t1);
            if (xL === null) continue;

            const barSpacing = this.chart.timeScale().options().barSpacing || 6;
            const paneWidth  = this.chart.timeScale().width();
            let xR = xL + (obj.barsWidth || 40) * barSpacing;
            if (xR > paneWidth) xR = paneWidth;

            const left  = Math.min(xL, xR);
            const right = Math.max(xL, xR);

            const yE = this.series.priceToCoordinate(obj.entry);
            const yS = this.series.priceToCoordinate(obj.stop);
            const yT = this.series.priceToCoordinate(obj.target);
            if (yE === null || yS === null || yT === null) continue;

            const top    = Math.min(yE, yS, yT);
            const bottom = Math.max(yE, yS, yT);

            if (x >= left && x <= right && y >= top - 4 && y <= bottom + 4) {
                return obj;
            }
            continue;
        }

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

    hitTestPositionHandle(x, y) {

        const threshold = 8;

        for (let obj of this.drawings) {

            if (obj.type !== "position") continue;

            const xL = this.timeMapper.getX(obj.t1);
            if (xL === null) continue;

            const barSpacing = this.chart.timeScale().options().barSpacing || 6;
            const paneWidth  = this.chart.timeScale().width();
            let xR = xL + (obj.barsWidth || 40) * barSpacing;
            if (xR > paneWidth) xR = paneWidth;

            const left  = Math.min(xL, xR);
            const right = Math.max(xL, xR);

            const yE = this.series.priceToCoordinate(obj.entry);
            const yS = this.series.priceToCoordinate(obj.stop);
            const yT = this.series.priceToCoordinate(obj.target);
            if (yE === null || yS === null || yT === null) continue;

            // правый край: уровневые хэндлы имеют приоритет
            if (Math.abs(x - right) < threshold) {
                if (Math.abs(y - yT) < threshold) return { obj, handle: "target" };
                if (Math.abs(y - yE) < threshold) return { obj, handle: "entry"  };
                if (Math.abs(y - yS) < threshold) return { obj, handle: "stop"   };

                const top    = Math.min(yE, yS, yT);
                const bottom = Math.max(yE, yS, yT);
                if (y > top && y < bottom) return { obj, handle: "width" };
            }

            // уровневые линии в любом месте бара
            if (x >= left && x <= right) {
                if (Math.abs(y - yT) < threshold) return { obj, handle: "target" };
                if (Math.abs(y - yE) < threshold) return { obj, handle: "entry"  };
                if (Math.abs(y - yS) < threshold) return { obj, handle: "stop"   };
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