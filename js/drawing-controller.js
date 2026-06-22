// ============================================================================
// Drawing Controller — tool + overlay state
// ============================================================================

class DrawingControllerClass {

    constructor() {
        this.activeTool = "idle";

        this.tempLineStart = null;
        this.previewLine = null;

        // ---- Drag overlay state ----
        this.isDraggingOverlay = false;
        this.draggedLine = null;
        this.dragStartTime = null;
        this.dragStartPrice = null;
        this.draggedPointIndex = null;

        // ---- Position drag state ----
        this.posHandle = null;
        this.posOrig = null;
        this.posDragStartPrice = null;
        this.posDragStartTime = null;
    }

    // ---- Position drag ----
    startPosDrag(obj, handle) {
        this.isDraggingOverlay = true;
        this.draggedLine = obj;
        this.posHandle = handle;
        this.draggedPointIndex = null;
        this.posOrig = {
            entry: obj.entry,
            stop: obj.stop,
            target: obj.target,
            t1: obj.t1,
            barsWidth: obj.barsWidth
        };
    }

    getPosHandle() {
        return this.posHandle;
    }

    // ---- Tool ----
    setTool(tool) {
        this.activeTool = tool;
    }

    resetTool() {
        this.activeTool = "idle";
        this.clearPreview();
        this.resetDrag();
    }

    getTool() {
        return this.activeTool;
    }

    // ---- Preview ----
    setTempStart(point) {
        this.tempLineStart = point;
    }

    getTempStart() {
        return this.tempLineStart;
    }

    setPreviewLine(obj) {
        this.previewLine = obj;
    }

    getPreviewLine() {
        return this.previewLine;
    }

    clearPreview() {
        this.tempLineStart = null;
        this.previewLine = null;
    }

    // ---- Drag overlay ----
    startDrag(line, startTime, startPrice, pointIndex = null) {
    this.isDraggingOverlay = true;
    this.draggedLine = line;
    this.dragStartTime = startTime;
    this.dragStartPrice = startPrice;
    this.draggedPointIndex = pointIndex;

    // 🔴 ВАЖНО — сохраняем исходные точки
    this.originalPoints = line.points.map(p => ({
        time: p.time,
        price: p.price
    }));
}


    stopDrag() {
        this.isDraggingOverlay = false;
        this.draggedLine = null;
        this.dragStartTime = null;
        this.dragStartPrice = null;
        this.draggedPointIndex = null;
        this.posHandle = null;
        this.posOrig = null;
        this.posDragStartPrice = null;
        this.posDragStartTime = null;
    }

    isDragging() {
        return this.isDraggingOverlay;
    }

    getDraggedLine() {
        return this.draggedLine;
    }

    getDragStartTime() {
        return this.dragStartTime;
    }

    getDragStartPrice() {
        return this.dragStartPrice;
    }

    getDraggedPointIndex() {
        return this.draggedPointIndex;
    }

    resetDrag() {
        this.stopDrag();
    }
}

window.DrawingController = new DrawingControllerClass();