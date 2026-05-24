// ============================================================================
// Tool UI controller
// ============================================================================

function setTool(tool) {

    DrawingController.setTool(tool);

    const overlayBtn = document.getElementById("lineOverlayBtn");
    const hlineBtn   = document.getElementById("hlineBtn1");
    const rectBtn    = document.getElementById("rectOverlayBtn");

    if (overlayBtn) {
        overlayBtn.style.background = "";
        overlayBtn.style.color = "";
    }

    if (rectBtn) {
        rectBtn.style.background = "";
        rectBtn.style.color = "";
    }

    if (hlineBtn) {
        hlineBtn.style.background = "";
        hlineBtn.style.color = "";
    }

    if (tool === "lineOverlay") {

        DrawingController.clearPreview();

        if (overlayBtn) {
            overlayBtn.style.background = "#2962FF";
            overlayBtn.style.color = "#fff";
        }

        return;
    }

    if (tool === "rectOverlay") {

        DrawingController.clearPreview();

        if (rectBtn) {
            rectBtn.style.background = "#2962FF";
            rectBtn.style.color = "#fff";
        }

        return;
    }

    if (tool === "hline") {

        if (hlineBtn) {
            hlineBtn.style.background = "#2962FF";
            hlineBtn.style.color = "#fff";
        }

        return;
    }

    if (tool === "alert") {

        const alertBtn = document.getElementById("AlertBtn");

        if (alertBtn) {
            alertBtn.style.background = "#2962FF";
            alertBtn.style.color = "#fff";
        }

        return;
    }

    DrawingController.resetTool();
}


function updateToolButtonState(isActive) {

    const btn = document.getElementById("hlineBtn1");

    if (!btn) return;

    if (isActive) {
        btn.style.background = "#2962FF";
        btn.style.color = "#fff";
    } else {
        btn.style.background = "";
        btn.style.color = "";
    }

}
