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
        drawings: {}
    };

   if (!layout.drawings) {
    layout.drawings = {};
}
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