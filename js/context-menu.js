// ============================================================================
// Candle Color Context Menu
// ============================================================================

function initCandleColorMenu() {

    const ids = ["upColor", "downColor", "wickUpColor", "wickDownColor", "resetColors"];

    for (const id of ids) {
        if (!document.getElementById(id)) {
            console.warn(`Элемент #${id} не найден в DOM`);
            return;
        }
    }

    const menu = document.getElementById("contextMenu");

    ids.slice(0,4).forEach(id => {

        document.getElementById(id).oninput = () => {

            layout.colors.up       = document.getElementById("upColor").value;
            layout.colors.down     = document.getElementById("downColor").value;
            layout.colors.wickUp   = document.getElementById("wickUpColor").value;
            layout.colors.wickDown = document.getElementById("wickDownColor").value;

            applyColors();
            StorageLayer.autoSave(layout);
        };

    });

    document.getElementById("resetColors").onclick = resetColors;

}

// applyColors / resetColors определены в index.html (там версия знает про тип
// отрисовки — свечи/бары). Дубль отсюда убран, чтобы порядок загрузки скриптов
// не решал, какая из двух реализаций в силе.

function showCandleContextMenu(x, y) {
	console.log("open candle menu");

    const menu = document.getElementById("contextMenu");

    menu.style.display = "block";

    const menuRect = menu.getBoundingClientRect();
    const windowWidth = window.innerWidth;
    const windowHeight = window.innerHeight;

    let posX = x;
    let posY = y;

    if (x + menuRect.width > windowWidth) {
        posX = windowWidth - menuRect.width - 5;
    }

    if (y + menuRect.height > windowHeight) {
        posY = windowHeight - menuRect.height - 5;
    }

    menu.style.left = posX + "px";
    menu.style.top  = posY + "px";

    document.getElementById("upColor").value = layout.colors.up;
    document.getElementById("downColor").value = layout.colors.down;
    document.getElementById("wickUpColor").value = layout.colors.wickUp;
    document.getElementById("wickDownColor").value = layout.colors.wickDown;

    // Подсветить активный тип отрисовки для панели, по которой открыто меню.
    if (typeof syncChartTypeButtons === "function" &&
        typeof chartMenuPane === "number") {
        syncChartTypeButtons(chartMenuPane);
    }

    // Состояние объёма: строка гаснет на инструментах без биржевого объёма.
    if (typeof syncVolumeButtons === "function") syncVolumeButtons();

    // То же для стакана: он есть только у бирж, у форекса строка неактивна.
    if (typeof syncOrderbookButtons === "function") syncOrderbookButtons();

}
// ============================================================================
// Line Context Menu
// ============================================================================

function initLineContextMenu() {

    const colorPicker = document.getElementById("lineColorPicker");

    if (colorPicker) {

        colorPicker.oninput = e => {

            if (!contextLineObject) return;

            if (contextLineObject.type === "hline") {

                contextLineObject.color = e.target.value;

                contextLineObject.line.applyOptions({
                    color: e.target.value
                });

            } else {

                contextLineObject.color = e.target.value;

                panesState[contextLineObject.paneId]
                    .drawingEngine.render();

            }

            StorageLayer.saveDrawings(
                contextLineObject.paneId,
                layout,
                panesState,
                drawings
            );
        };
    }

    const widthSelect = document.getElementById("lineWidthSelect");

    if (widthSelect) {

        widthSelect.onchange = e => {

            if (!contextLineObject) return;

            const w = parseInt(e.target.value);

            if (contextLineObject.type === "hline") {

                contextLineObject.width = w;

                contextLineObject.line.applyOptions({
                    lineWidth: w
                });

            } else {

                contextLineObject.width = w;

                panesState[contextLineObject.paneId]
                    .drawingEngine.render();
            }

            StorageLayer.saveDrawings(
                contextLineObject.paneId,
                layout,
                panesState,
                drawings
            );
        };
    }
const fillPicker = document.getElementById("fillColorPicker");

if (fillPicker) {

    fillPicker.oninput = e => {

        if (!contextLineObject) return;

        contextLineObject.fillColor = e.target.value;

        panesState[contextLineObject.paneId]
            .drawingEngine.render();

        StorageLayer.saveDrawings(
            contextLineObject.paneId,
            layout,
            panesState,
            drawings
        );

    };

}

}

function showOverlayMenu(x, y, paneId, obj) {

    contextLinePane = paneId;
    contextLineObject = obj;

    const menu = document.getElementById("lineMenu");

    const colorPicker = document.getElementById("lineColorPicker");
    const widthSelect = document.getElementById("lineWidthSelect");
    const fillPicker = document.getElementById("fillColorPicker");
    const fillBlock = document.getElementById("fillColorBlock");

    if (obj.type === "rect") {
        fillBlock.style.display = "block";
    } else {
        fillBlock.style.display = "none";
    }

    if (!menu) return;

    menu.style.display = "block";

    // ===== загрузка параметров =====

    if (colorPicker && obj.color) {
        colorPicker.value = obj.color;
    }

    if (widthSelect && obj.width) {
        widthSelect.value = obj.width;
    }

    if (obj.type === "rect" && fillPicker) {
        fillPicker.value = obj.fillColor || "#2962FF";
    }

    const menuRect = menu.getBoundingClientRect();
    const windowWidth = window.innerWidth;
    const windowHeight = window.innerHeight;

    let posX = x;
    let posY = y;

    if (x + menuRect.width > windowWidth) {
        posX = windowWidth - menuRect.width - 5;
    }

    if (y + menuRect.height > windowHeight) {
        posY = windowHeight - menuRect.height - 5;
    }

    menu.style.left = posX + "px";
    menu.style.top = posY + "px";
}


function hideLineMenu() {

    const lineMenu = document.getElementById("lineMenu");
    if (lineMenu) lineMenu.style.display = "none";

    const alertMenu = document.getElementById("alertMenu");
    if (alertMenu) alertMenu.style.display = "none";
	
	const chartMenu = document.getElementById("chartMenu");
	if (chartMenu) chartMenu.style.display = "none";

    contextLineObject = null;

}


function deleteSelectedLine() {

    if (!contextLinePane || !contextLineObject) return;

    const id = contextLinePane;
    const st = panesState[id];

    if (contextLineObject.line) {
        st.series.removePriceLine(contextLineObject.line);
    }

    drawings[id] =
        drawings[id].filter(o => o !== contextLineObject);

    // Overlay-объекты (line / rect / fib) живут не в drawings[], а в движке
    // рисования — без этой чистки «Удалить» на них не срабатывало.
    if (st.drawingEngine) {
        st.drawingEngine.drawings =
            st.drawingEngine.drawings.filter(o => o !== contextLineObject);
        st.drawingEngine.render();
    }

    StorageLayer.saveDrawings(
        id,
        layout,
        panesState,
        drawings
    );

    hideLineMenu();
    hideFibMenu();
}

// ============================================================================
// Меню сетки Фибоначчи
// ============================================================================
//
// Управляет ОДНОЙ выбранной сеткой (contextLineObject): видимость и цвет
// каждого уровня по отдельности, добавление своих уровней сверх умолчания,
// автопродление вправо и заливка.

function showFibMenu(x, y, paneId, obj) {

    contextLinePane = paneId;
    contextLineObject = obj;

    const menu = document.getElementById("fibMenu");
    if (!menu) return;

    const ext  = document.getElementById("fibExtendRight");
    const fill = document.getElementById("fibShowFill");
    if (ext)  ext.checked  = obj.extendRight !== false;
    if (fill) fill.checked = obj.showFill === true;

    renderFibLevels();

    menu.style.display = "block";

    const menuRect = menu.getBoundingClientRect();
    let posX = x, posY = y;
    if (x + menuRect.width  > window.innerWidth)  posX = window.innerWidth  - menuRect.width  - 5;
    if (y + menuRect.height > window.innerHeight) posY = window.innerHeight - menuRect.height - 5;
    menu.style.left = Math.max(5, posX) + "px";
    menu.style.top  = Math.max(5, posY) + "px";
}

function hideFibMenu() {
    const menu = document.getElementById("fibMenu");
    if (menu) menu.style.display = "none";
}

/**
 * Перерисовать список уровней в меню из текущей сетки.
 * Каждая строка: галочка видимости, свой цвет, значение, кнопка удаления.
 * @returns {void}
 */
function renderFibLevels() {

    const box = document.getElementById("fibLevelList");
    if (!box || !contextLineObject) return;

    const levels = contextLineObject.levels || [];

    box.innerHTML = levels.map((l, i) => `
        <div style="display:flex; align-items:center; gap:6px; padding:2px 0;">
            <input type="checkbox" ${l.visible !== false ? "checked" : ""}
                   onchange="setFibLevelVisible(${i}, this.checked)" style="cursor:pointer;">
            <input type="color" value="${l.color || '#888888'}"
                   oninput="setFibLevelColor(${i}, this.value)"
                   style="width:26px; height:20px; padding:0; border:1px solid var(--btn-border);
                          background:none; cursor:pointer;">
            <span style="flex:1; font-variant-numeric:tabular-nums;">${(l.value * 100).toFixed(1)}%</span>
            <button onclick="removeFibLevel(${i})" title="Удалить уровень"
                    style="border:0; background:none; color:var(--muted); cursor:pointer;
                           font-size:14px; line-height:1;">✕</button>
        </div>`).join("");
}

/** Сохранить изменения сетки и перерисовать пану. */
function _fibCommit() {
    if (!contextLinePane) return;
    const st = panesState[contextLinePane];
    if (st?.drawingEngine) st.drawingEngine.render();
    StorageLayer.saveDrawings(contextLinePane, layout, panesState, drawings);
}

function setFibFlag(name, on) {
    if (!contextLineObject) return;
    contextLineObject[name] = !!on;
    _fibCommit();
}

function setFibLevelVisible(i, on) {
    if (!contextLineObject?.levels?.[i]) return;
    contextLineObject.levels[i].visible = !!on;
    _fibCommit();
}

function setFibLevelColor(i, color) {
    if (!contextLineObject?.levels?.[i]) return;
    contextLineObject.levels[i].color = color;
    _fibCommit();
}

function removeFibLevel(i) {
    if (!contextLineObject?.levels) return;
    contextLineObject.levels.splice(i, 1);
    renderFibLevels();
    _fibCommit();
}

/**
 * Добавить свой уровень сверх умолчания. Значение — ДОЛЯ хода (0.886), но
 * ввод в процентах (88.6) тоже принимается: всё, что по модулю больше 5,
 * трактуется как проценты — на практике доли выше 5 не используют.
 * @returns {void}
 */
function addFibLevel() {

    if (!contextLineObject) return;

    const input = document.getElementById("fibNewLevel");
    if (!input) return;

    let v = parseFloat(input.value);
    if (!isFinite(v)) return;
    if (Math.abs(v) > 5) v = v / 100;

    if (!contextLineObject.levels) contextLineObject.levels = [];

    // Дубли не добавляем — уровень уже есть, просто включим его.
    const exist = contextLineObject.levels.find(l => Math.abs(l.value - v) < 1e-9);
    if (exist) {
        exist.visible = true;
    } else {
        contextLineObject.levels.push({ value: v, color: "#2962ff", visible: true });
        contextLineObject.levels.sort((a, b) => a.value - b.value);
    }

    input.value = "";
    renderFibLevels();
    _fibCommit();
}

function copyLine() {

    if (!contextLinePane || !contextLineObject) return;

    const sourceId = contextLinePane;
    const targetId = sourceId === 1 ? 2 : 1;

    const targetSeries = panesState[targetId]?.series;

    if (!targetSeries) return;

    const source = contextLineObject;
	
	// ===== COPY OVERLAY OBJECTS (line / rect) =====
if (source.type === "rect" || source.type === "line") {

    const copy = JSON.parse(JSON.stringify(source));

    copy.paneId = targetId;

    panesState[targetId].drawingEngine.addDrawing(copy);

    StorageLayer.saveDrawings(
        targetId,
        layout,
        panesState,
        drawings
    );

    hideLineMenu();
    return;
}


    const newLine = targetSeries.createPriceLine({

        price: source.price,
        color: source.color,
        lineWidth: source.width,
        lineStyle: LightweightCharts.LineStyle.Solid,
        axisLabelVisible: true

    });

    const newObj = {

        paneId: targetId,
        type: "hline",
        price: source.price,
        line: newLine,
        color: source.color,
        width: source.width

    };

    drawings[targetId].push(newObj);

    StorageLayer.saveDrawings(
        targetId,
        layout,
        panesState,
        drawings
    );

if (source.type === "rect" || source.type === "line") {

    const copy = JSON.parse(JSON.stringify(source));

    copy.paneId = targetId;

    panesState[targetId].drawingEngine.addDrawing(copy);

    StorageLayer.saveDrawings(
        targetId,
        layout,
        panesState,
        drawings
    );

    hideLineMenu();
    return;
}

    hideLineMenu();
}

// ============================================================================
// Position Context Menu
// ============================================================================

/**
 * Показать меню позиции (депозит, риск, флип, удаление).
 *
 * Args:
 *   x, y (number): экранные координаты.
 *   paneId (number): id панели.
 *   obj (Object): объект позиции.
 */
function showPositionMenu(x, y, paneId, obj) {

    contextLinePane = paneId;
    contextLineObject = obj;

    const menu = document.getElementById("positionMenu");
    if (!menu) return;

    const settings = PositionCalc.getSettings();
    const depInput  = document.getElementById("posDeposit");
    const riskInput = document.getElementById("posRisk");
    if (depInput)  depInput.value  = settings.deposit;
    if (riskInput) riskInput.value = settings.riskPct;

    // обработчики ввода — глобальные настройки + перерисовка
    if (depInput) {
        depInput.oninput = () => {
            const s = PositionCalc.getSettings();
            const v = parseFloat(depInput.value);
            if (!isNaN(v)) { s.deposit = v; PositionCalc.saveSettings(s); rerenderPositions(); }
        };
    }
    if (riskInput) {
        riskInput.oninput = () => {
            const s = PositionCalc.getSettings();
            const v = parseFloat(riskInput.value);
            if (!isNaN(v)) { s.riskPct = v; PositionCalc.saveSettings(s); rerenderPositions(); }
        };
    }

    menu.style.display = "block";

    const menuRect = menu.getBoundingClientRect();
    let posX = x, posY = y;
    if (x + menuRect.width  > window.innerWidth)  posX = window.innerWidth  - menuRect.width  - 5;
    if (y + menuRect.height > window.innerHeight) posY = window.innerHeight - menuRect.height - 5;
    menu.style.left = posX + "px";
    menu.style.top  = posY + "px";
}

/**
 * Перерисовать все панели (после смены глобальных настроек депозита/риска).
 */
function rerenderPositions() {
    Object.values(panesState).forEach(p => {
        if (p.drawingEngine) p.drawingEngine.render();
    });
}

/**
 * Поменять направление позиции (long ⇄ short), зеркалируя stop/target.
 */
function flipPosition() {
    if (!contextLineObject || contextLineObject.type !== "position") return;
    const obj = contextLineObject;
    obj.side = obj.side === "long" ? "short" : "long";
    // зеркалируем stop/target относительно entry
    const newStop   = 2 * obj.entry - obj.stop;
    const newTarget = 2 * obj.entry - obj.target;
    obj.stop   = newStop;
    obj.target = newTarget;
    panesState[obj.paneId].drawingEngine.render();
    StorageLayer.saveDrawings(obj.paneId, layout, panesState, drawings);
    hidePositionMenu();
}

/**
 * Удалить позицию из overlay и сохранить.
 */
function deletePosition() {
    if (!contextLinePane || !contextLineObject) return;
    const id = contextLinePane;
    const st = panesState[id];
    if (st.drawingEngine) {
        st.drawingEngine.drawings = st.drawingEngine.drawings.filter(o => o !== contextLineObject);
        st.drawingEngine.render();
    }
    StorageLayer.saveDrawings(id, layout, panesState, drawings);
    hidePositionMenu();
}

/**
 * Скрыть меню позиции.
 */
function hidePositionMenu() {
    const menu = document.getElementById("positionMenu");
    if (menu) menu.style.display = "none";
    contextLineObject = null;
}

// ============================================================================
// Alert Context Menu
// ============================================================================

function showAlertMenu(x, y, paneId, obj) {

    contextLinePane = paneId;
    contextLineObject = obj;

    const menu = document.getElementById("alertMenu");

    if (!menu) return;

    menu.style.display = "block";

    const menuRect = menu.getBoundingClientRect();
    const windowWidth = window.innerWidth;
    const windowHeight = window.innerHeight;

    let posX = x;
    let posY = y;

    if (x + menuRect.width > windowWidth) {
        posX = windowWidth - menuRect.width - 5;
    }

    if (y + menuRect.height > windowHeight) {
        posY = windowHeight - menuRect.height - 5;
    }

    menu.style.left = posX + "px";
    menu.style.top = posY + "px";
}


function deleteAlert() {

    if (!contextLineObject) return;

    const paneId = contextLinePane;
    const obj = contextLineObject;

    const st = panesState[paneId];

    if (obj.line) {
        st.series.removePriceLine(obj.line);
    }

    drawings[paneId] = drawings[paneId].filter(o => o !== obj);

    if (obj.id) {

        panesState[paneId].ws.send(JSON.stringify({
            type: "remove_alert",
            symbol: obj.symbol,
            id: obj.id
        }));

    }

    StorageLayer.saveDrawings(
        paneId,
        layout,
        panesState,
        drawings
    );

    hideLineMenu();

}

document.addEventListener("click", e => {

    const lineMenu = document.getElementById("lineMenu");
    const alertMenu = document.getElementById("alertMenu");
    const chartMenu = document.getElementById("chartMenu");
    const candleMenu = document.getElementById("contextMenu");
    const positionMenu = document.getElementById("positionMenu");

    if (positionMenu && !positionMenu.contains(e.target)) {
        positionMenu.style.display = "none";
    }

    if (lineMenu && !lineMenu.contains(e.target)) {
        lineMenu.style.display = "none";
    }

    if (alertMenu && !alertMenu.contains(e.target)) {
        alertMenu.style.display = "none";
    }

    if (chartMenu && !chartMenu.contains(e.target)) {
        chartMenu.style.display = "none";
    }

    if (candleMenu && !candleMenu.contains(e.target)) {
        candleMenu.style.display = "none";
    }

});


// ============================================================================
// Chart Context Menu
// ============================================================================

let chartMenuPane = null;
let chartMenuPrice = null;
let chartMenuX = 0;
let chartMenuY = 0;


function showChartMenu(x, y, paneId, price) {
	
	chartMenuX = x;
	chartMenuY = y;

    chartMenuPane = paneId;
    chartMenuPrice = price;

    const menu = document.getElementById("chartMenu");

    if (!menu) return;

    menu.style.display = "block";

    const rect = menu.getBoundingClientRect();

    const windowWidth = window.innerWidth;
    const windowHeight = window.innerHeight;

    let posX = x;
    let posY = y;

    if (x + rect.width > windowWidth) {
        posX = windowWidth - rect.width - 5;
    }

    if (y + rect.height > windowHeight) {
        posY = windowHeight - rect.height - 5;
    }

    menu.style.left = posX + "px";
    menu.style.top = posY + "px";
}

function addAlertFromChartMenu() {

    if (!chartMenuPane) return;

    addAlertLine(chartMenuPane, chartMenuPrice);

    hideLineMenu();
}

function openChartSettings() {

    const chartMenu = document.getElementById("chartMenu");
    if (chartMenu) chartMenu.style.display = "none";

    showCandleContextMenu(chartMenuX, chartMenuY);

}




