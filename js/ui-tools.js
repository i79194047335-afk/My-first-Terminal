// ============================================================================
// Tool UI controller
// ============================================================================
//
// Подсветка активного инструмента — через классы (.on / .on-long / .on-short),
// а не inline-стили: кнопки тулбара стали иконочными (.tb-ic), и заливка фона
// через element.style ломала бы их вид. Классы описаны в CSS index.html.

// Кнопки панели 1, которые подсвечивает setTool. Панель 2 исторически не
// подсвечивается (у неё свои id hlineBtn2 и т.д.) — поведение не меняем.
const TOOL_BTN_IDS = ["lineOverlayBtn", "hlineBtn1", "rectOverlayBtn", "posBtn1"];

function clearToolHighlights() {
    for (const id of TOOL_BTN_IDS) {
        const el = document.getElementById(id);
        if (el) el.classList.remove("on", "on-long", "on-short");
    }
    const alertBtn = document.getElementById("AlertBtn");
    if (alertBtn) alertBtn.classList.remove("on");
}

function setTool(tool) {

    DrawingController.setTool(tool);

    clearToolHighlights();

    const posBtn = document.getElementById("posBtn1");

    if (tool === "posLong") {
        DrawingController.clearPreview();
        if (posBtn) posBtn.classList.add("on-long");
        return;
    }

    if (tool === "posShort") {
        DrawingController.clearPreview();
        if (posBtn) posBtn.classList.add("on-short");
        return;
    }

    if (tool === "lineOverlay") {
        DrawingController.clearPreview();
        const el = document.getElementById("lineOverlayBtn");
        if (el) el.classList.add("on");
        return;
    }

    if (tool === "rectOverlay") {
        DrawingController.clearPreview();
        const el = document.getElementById("rectOverlayBtn");
        if (el) el.classList.add("on");
        return;
    }

    if (tool === "hline") {
        const el = document.getElementById("hlineBtn1");
        if (el) el.classList.add("on");
        return;
    }

    if (tool === "alert") {
        const alertBtn = document.getElementById("AlertBtn");
        if (alertBtn) alertBtn.classList.add("on");
        return;
    }

    DrawingController.resetTool();
}


function updateToolButtonState(isActive) {
    const btn = document.getElementById("hlineBtn1");
    if (!btn) return;
    btn.classList.toggle("on", !!isActive);
}
