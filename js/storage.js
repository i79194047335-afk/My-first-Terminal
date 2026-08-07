// ============================================================================
// Storage Layer — layout + drawings persistence
// ============================================================================

// --- Инициализация layout ---
function loadLayout(DEFAULT_COLORS) {

    let savedLayout = null;

    try {
        savedLayout = JSON.parse(localStorage.getItem("layoutState"));
    } catch (e) {
        console.warn("layoutState поврежден, сбрасываем");
        localStorage.removeItem("layoutState");
    }

    const layout = savedLayout || {
        split: "split",
        colors: { ...DEFAULT_COLORS },
        pane1: { tf: "M1" },
        pane2: { tf: "M1" },
        drawings: {},
        autoJump: false
    };

   if (!layout.drawings) {
    layout.drawings = {};
}
    // Достраиваем обязательные поля: сохранённый layout мог прийти из более
    // старой версии или быть частично перезаписан. Раньше значения по
    // умолчанию подставлялись ТОЛЬКО когда layoutState отсутствовал целиком,
    // и объект без colors ронял applyColors на первом же кадре.
    if (!layout.colors) layout.colors = { ...DEFAULT_COLORS };
    else for (const k in DEFAULT_COLORS)
        if (layout.colors[k] === undefined) layout.colors[k] = DEFAULT_COLORS[k];
    if (!layout.pane1) layout.pane1 = { tf: "M1" };
    if (!layout.pane2) layout.pane2 = { tf: "M1" };
    if (!layout.split) layout.split = "split";
    // Автопереход по сигналу: по умолчанию выключен, чтобы случайный всплеск
    // не уводил график, пока владелец смотрит другую пару.
    if (layout.autoJump === undefined) layout.autoJump = false;

    return layout;
}

function autoSave(layout) {
    localStorage.setItem("layoutState", JSON.stringify(layout));
}

function saveDrawings(paneId, layout, panesState, drawings) {

    const st = panesState[paneId];
    if (!st) return;

    const filtered = [];

    drawings[paneId].forEach(d => {

    if (d.type === "hline") {
        filtered.push({
            type: "hline",
            price: d.price,
            color: d.color,
            width: d.width
        });
    }

    if (d.type === "alert") {
    filtered.push({
        type: "alert",
        price: d.price,
        id: d.id || null,
        triggered: d.triggered || false
    });
}

    // Маркер всплеска. Сохраняется как рисунок (просьба владельца): маркеры
    // переживают F5 и переключение инструмента наравне с линиями. Хаб держит
    // свой журнал только сутки, а этот — столько, сколько живёт layout.
    if (d.type === "shock") {
        filtered.push({
            type: "shock",
            time: d.time,
            price: d.price,
            high: d.high ?? null,
            low: d.low ?? null,
            direction: d.direction,
            sigma: d.sigma,
            blocked: d.blocked || null,
            // Форма всплеска: "burst" — рывок за ≤10 с, "spread" — размазан.
            shapeKind: d.shapeKind || null,
            coverage: d.coverage ?? null,
            origin: d.origin || null,
            audible: !!d.audible
        });
    }


});;

    // --- OVERLAY ---
if (st.drawingEngine) {
    st.drawingEngine.drawings.forEach(d => {

        if (d.type === "line") {
            filtered.push({
                type: "line",
                points: JSON.parse(JSON.stringify(d.points)),
                color: d.color,
                width: d.width
            });
        }

       if (d.type === "rect") {
    filtered.push({
        type: "rect",
        points: JSON.parse(JSON.stringify(d.points)),
        color: d.color,
        width: d.width,
        fillColor: d.fillColor,

        showMidline: d.showMidline,
        midColor: d.midColor,
        midWidth: d.midWidth,
		extendRight: d.extendRight,
		showMidline: d.showMidline,
		midColor: d.midColor,
		midWidth: d.midWidth,
		fillColor: d.fillColor,
    });
}

        if (d.type === "fib") {
            filtered.push({
                type: "fib",
                points: JSON.parse(JSON.stringify(d.points)),
                // Уровни копируем поштучно: у каждого свой цвет и видимость,
                // и пользователь мог добавить свои сверх умолчания.
                levels: (d.levels || []).map(l => ({
                    value: l.value, color: l.color, visible: l.visible !== false
                })),
                color: d.color,
                width: d.width,
                extendRight: d.extendRight,
                showFill: d.showFill,
                priceDecimals: d.priceDecimals
            });
        }

        if (d.type === "position") {
            filtered.push({
                type: "position",
                side: d.side,
                t1: d.t1,
                entry: d.entry,
                stop: d.stop,
                target: d.target,
                barsWidth: d.barsWidth
            });
        }

    });
}

    if (!layout.drawings[currentSymbol]) {
    layout.drawings[currentSymbol] = { 1: [], 2: [] };
}

	layout.drawings[currentSymbol][paneId] = filtered;


    autoSave(layout);

    console.log("SAVED FINAL", paneId, filtered);
}

function restoreDrawings(paneId, layout, panesState, drawings) {

    const st = panesState[paneId];
    if (!st) return;
	
	// ─── Полная очистка перед восстановлением ───

// 1️⃣ Удаляем реальные priceLine с графика
if (drawings[paneId]?.length) {
    drawings[paneId].forEach(obj => {
        if ((obj.type === "hline" || obj.type === "alert") && obj.line) {
		st.series.removePriceLine(obj.line);
}
    });
}

// 2️⃣ Очищаем массив hline
drawings[paneId] = [];

// 3️⃣ Очищаем overlay
if (st.drawingEngine) {
    st.drawingEngine.drawings = [];
}

    const saved = layout.drawings?.[currentSymbol]?.[paneId];
    if (!saved || !saved.length) return;

    saved.forEach(d => {

        // ---------- SHOCK MARKER ----------
        // Маркеры не создают priceLine: их рисует плагин createSeriesMarkers
        // одним слоем. Здесь только восстанавливаем объект, а перерисовку
        // делает refreshShockMarkers() в index.html.
        if (d.type === "shock") {
            drawings[paneId].push({
                paneId,
                type: "shock",
                time: d.time,
                price: d.price,
                high: d.high ?? null,
                low: d.low ?? null,
                direction: d.direction,
                sigma: d.sigma,
                blocked: d.blocked || null,
                shapeKind: d.shapeKind || null,
                coverage: d.coverage ?? null,
                origin: d.origin || null,
                audible: !!d.audible
            });
            return;
        }

        // ---------- HLINE ----------
        if (d.type === "hline") {

            const line = st.series.createPriceLine({
                price: d.price,
                color: d.color,
                lineWidth: d.width,
                lineStyle: LightweightCharts.LineStyle.Solid,
                axisLabelVisible: true
            });

            drawings[paneId].push({
                paneId,
                type: "hline",
                price: d.price,
                line,
                color: d.color,
                width: d.width
            });

            return;
        }
		
		// ---------- ALERT ----------
if (d.type === "alert") {
	const exists = drawings[paneId].find(
		o => o.type === "alert" && Math.abs(o.price - d.price) < 0.0000001
	);

	if (exists) return;


    const line = st.series.createPriceLine({
    price: d.price,
    color: d.triggered ? "#ff0000" : "#000000",
    lineWidth: 1,
    lineStyle: LightweightCharts.LineStyle.Dashed,
    axisLabelVisible: true,
    title: d.triggered ? "🔕" : "🔔"
});


    drawings[paneId].push({
    paneId,
    type: "alert",
    price: d.price,
    id: d.id || null,
	triggered: false,
    line
});

    return;
}

        // ---------- OVERLAY ----------
        if (d.type === "line" && st.drawingEngine) {

            st.drawingEngine.drawings.push({
                type: "line",
                paneId: paneId,
                points: d.points,
                color: d.color,
                width: d.width,
                selected: false
            });
        }
		if (d.type === "rect" && st.drawingEngine) {

    st.drawingEngine.drawings.push({
        type: "rect",
        paneId: paneId,
        points: d.points,
        color: d.color,
        width: d.width,
        fillColor: d.fillColor || "rgba(41,98,255,0.15)",
        showMidline: d.showMidline ?? true,
        midColor: d.midColor || "#000000",
        midWidth: d.midWidth || 1,
		extendRight: d.extendRight ?? false,
        selected: false
    });
}
		if (d.type === "fib" && st.drawingEngine) {

    st.drawingEngine.drawings.push({
        type: "fib",
        paneId: paneId,
        points: d.points,
        // Пустой список уровней в сохранённых данных означал бы невидимую
        // сетку — подстраховываемся дефолтом.
        levels: (d.levels && d.levels.length)
            ? d.levels.map(l => ({ value: l.value, color: l.color, visible: l.visible !== false }))
            : (window.FIB_DEFAULT_LEVELS || []).map(l => ({ ...l })),
        color: d.color || "#787b86",
        width: d.width || 1,
        extendRight: d.extendRight ?? true,
        showFill: d.showFill ?? false,
        priceDecimals: d.priceDecimals ?? 5,
        selected: false
    });
}

		if (d.type === "position" && st.drawingEngine) {

    st.drawingEngine.drawings.push({
        type: "position",
        paneId: paneId,
        side: d.side || "long",
        t1: d.t1,
        entry: d.entry,
        stop: d.stop,
        target: d.target,
        barsWidth: d.barsWidth || 40,
        selected: false
    });
}
    });

    if (st.drawingEngine) {
        st.drawingEngine.render();
    }
	if (st.chart) {
    const div = document.getElementById(`chart${paneId}`);
    st.chart.resize(div.clientWidth, div.clientHeight);
}

}

window.StorageLayer = {
    loadLayout,
    autoSave,
    saveDrawings,
    restoreDrawings
};