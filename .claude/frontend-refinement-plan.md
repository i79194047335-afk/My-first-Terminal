# Plan: Indicators multi-instance + settings menu + signal markers

---

## Progress Log (update each session)

| Session | Date | Model | Completed steps | Outcome / notes |
|---|---|---|---|---|
| 1 | 2026-06-11 | Sonnet 4.6 | Planning only | Plan created, all requirements clarified |

**Current status:** Not started — plan approved, implementation pending.

**Next session should start at:** Step 1 — `js/indicators.js` refactor

---

## Model recommendations per stage

| Step | Recommended model | Why |
|---|---|---|
| Step 1 — `indicators.js` refactor | **Opus 4.8** | Complex class redesign: multiple interacting methods, backward-compat serialize/loadFrom, pane grouping logic. Needs deep reasoning to avoid subtle state bugs. |
| Step 2–3 — HTML panel + settings menu markup | **Sonnet 4.6** | Straightforward HTML/CSS template work. Well-specified. |
| Step 4 — JS indicators panel functions | **Sonnet 4.6** | Clear spec, moderate JS complexity. |
| Step 5–6 — Signal system (markers, visibility, nav) | **Sonnet 4.6** | Well-defined state machine. Many small pieces but each is simple. |
| Verification pass | **Sonnet 4.6** or **Haiku 4.5** | Reading files + grep checks; no heavy reasoning needed. |

> **DeepSeek V3 Pro note:** If you have it configured as an alternative, it performs well on Steps 1 and 5 (algorithmic JS). Not recommended for the HTML/CSS steps where strict adherence to existing style patterns matters more than raw code generation quality.

---

## Context

Three independent improvements to `index.html` / `js/indicators.js`:

1. **Multiple indicators of same type** — `IndicatorManager` currently stores one instance per type in a keyed object, preventing e.g. two EMAs with different periods. Needs a flat instance array with unique IDs.
2. **Indicator settings context menu** — current inline params in the panel are noisy. Move color + lineWidth + type-specific params into a per-instance floating context menu opened by a ⚙ button.
3. **Signal chart markers** — signal dots (green/red circles) on the price chart; smart 5-min auto-hide logic; arrow navigation between session signals in the panel; one-click pair switch from the signal window.

User answers already confirmed:
- UI pattern B (single "＋ Add ▾" dropdown, not per-type buttons)
- Same-type oscillators share one sub-pane (RSI×2 → pane 1; MACD×2 → pane 1)
- Settings opened by ⚙ button click
- New signal always auto-shows markers (even if user manually hid them)
- Dots completely hidden until activated; session-only (page reload = fresh start)
- Signal navigation: **arrow-based** (← N/M →) in the panel; clicking near a marker dot also navigates to that signal (detected via param.time in subscribeClick)
- SKIP signals: excluded from marker log (no directional color)

---

## Files modified

| File | Scope |
|---|---|
| `js/indicators.js` | Major refactor of IndicatorManager |
| `index.html` | Indicators panel markup + JS + signal system |

---

## Step 1 — `js/indicators.js`: Refactor to instance array

### Replace `_overlays` + `_oscillators` with `_instances[]`

```js
this._instances = [];
// Each entry: { id, type, params, color, lineWidth, ...seriesRefs }
// Overlay types (sma, ema, bollinger): seriesRefs = { series } or { basis, upper, lower }
// Oscillator types (rsi, macd, stochastic): seriesRefs = { _series } or { _macdSeries, _signalSeries, _histSeries } or { _kSeries, _dSeries }
```

Each instance gets `id = \`${type}_${++this._idCounter}\`` (counter on the class, initialized to 0).

### Method changes

| Method | Change |
|---|---|
| `add(type, params, color, lineWidth)` | No duplicate guard — always appends new instance. `lineWidth` defaults to 2. Calls `_addInstanceSeries(inst)`, then `_rebuildOscillators()` if oscillator. |
| `remove(id)` | Removes by id. Calls `_destroyInstanceSeries(inst)` for overlays. Triggers `_rebuildOscillators()` for oscillators. |
| `update(id, params, color, lineWidth)` | Updates by id. For overlays: `applyOptions` in place. For oscillators: `_rebuildOscillators()`. Then recompute. |
| `has(type)` | `_instances.some(i => i.type === type)` — unchanged semantics |
| `getInstances()` | Returns `_instances` (for UI rendering) |
| `recomputeAll(data)` | Iterates `_instances` |
| `_onCrosshair(param)` | Iterates `_instances` for legend |
| `serialize()` | `_instances.map(i => ({id, type, params, color, lineWidth, visible:true}))` |
| `loadFrom(configArray, data)` | Calls `add(cfg.type, cfg.params, cfg.color, cfg.lineWidth)` for each. Uses `cfg.id` if present (restore after refactor); falls back to generated id (backward-compatible with old format). |
| `destroyAll()` | Clears `_instances`, tears down all series |

### Oscillator pane assignment (same-type merge)

```js
_rebuildOscillators() {
    this._teardownAllOscillatorSeries();
    // Unique types in order of first appearance
    const oscOrder = [...new Set(
        this._instances.filter(i => this._isOscillator(i.type)).map(i => i.type)
    )];
    oscOrder.forEach((type, idx) => {
        const paneIndex = idx + 1;
        this._instances.filter(i => i.type === type).forEach(inst => {
            // create series on paneIndex, store refs on inst
        });
    });
}
```

`_teardownAllOscillatorSeries()` stays the same (loops all instances' osc series refs + removes panes).

### `_addInstanceSeries(inst)` — replaces `_addOverlay()`

```js
_addInstanceSeries(inst) {
    // same LWC series creation logic as current _addOverlay
    // but reads lineWidth from inst.lineWidth
    // for bollinger: all three lines use inst.lineWidth
    // for overlays: stores series ref on inst.series (or inst.basis/upper/lower for bollinger)
}
```

### Legend — `_onCrosshair`

Iterate `_instances` instead of checking fixed keys. For each instance, build a labelled span:
- SMA: `SMA(20) 1.08234`
- EMA: `EMA(21) 1.08234` — if two EMAs, shows two lines
- BB: `BB(20,2) U:... M:... L:...`
- RSI(14): `RSI(14) 62.3` — if two RSIs on same pane, shows two lines
- etc.

---

## Step 2 — `index.html`: Indicators panel markup

Replace the entire `<div id="indicatorsPanel">` static content with:

```html
<div id="indicatorsPanel">
  <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:8px;">
    <span style="font-weight:bold; font-size:13px;">Indicators</span>
    <div style="position:relative;">
      <button id="addIndBtn" onclick="toggleAddIndDropdown(event)">＋ Add ▾</button>
      <div id="addIndDropdown" style="display:none; position:absolute; right:0; top:100%; background:#fff; border:1px solid #ccc; border-radius:4px; min-width:140px; z-index:8000; box-shadow:0 2px 8px rgba(0,0,0,0.15);">
        <!-- JS fills this: one item per type -->
      </div>
    </div>
  </div>
  <div id="indList">
    <!-- JS fills this: one row per active instance -->
  </div>
</div>
```

Add new CSS for `.ind-swatch` (8×8px colored square), `.ind-name` (flex-grow), `.ind-settings-btn`, `.ind-del-btn` (small icon buttons):

```css
#indList .ind-row {
    display: flex; align-items: center; gap: 6px; padding: 4px 0;
    border-bottom: 1px solid #f0f0f0; font-size: 12px;
}
#indList .ind-row:last-child { border-bottom: none; }
.ind-swatch { width: 10px; height: 10px; border-radius: 2px; flex-shrink: 0; }
.ind-name   { flex: 1; }
.ind-settings-btn, .ind-del-btn {
    background: none; border: 1px solid #ddd; border-radius: 3px;
    cursor: pointer; font-size: 11px; padding: 1px 5px;
}
.ind-settings-btn:hover { background: #f0f0f0; }
.ind-del-btn:hover { background: #ffeaea; }
```

---

## Step 3 — `index.html`: Indicator settings context menu

Add after the existing `#lineMenu`:

```html
<div id="indSettingsMenu"
     style="position:fixed; display:none; background:#fff; border:1px solid #aaa;
            border-radius:4px; padding:10px 14px; z-index:8500; font-size:12px;
            min-width:200px; box-shadow:0 4px 16px rgba(0,0,0,0.18);">
  <div style="margin-bottom:6px; font-weight:bold;" id="indSM_title">Settings</div>
  <div style="display:flex; align-items:center; gap:8px; margin-bottom:6px;">
    <span>Color</span>
    <input type="color" id="indSM_color" style="width:32px; height:24px; padding:0; border:1px solid #ccc;">
  </div>
  <div style="display:flex; align-items:center; gap:6px; margin-bottom:6px;">
    <span>Width</span>
    <div id="indSM_widthBtns">
      <button data-w="1">1</button><button data-w="2">2</button>
      <button data-w="3">3</button><button data-w="4">4</button>
    </div>
  </div>
  <div id="indSM_params">
    <!-- JS injects type-specific param inputs here -->
  </div>
  <div style="margin-top:10px; display:flex; justify-content:flex-end; gap:6px;">
    <button onclick="closeIndSettings()">Cancel</button>
    <button onclick="applyIndSettings()" style="background:#2962FF; color:#fff; border:none;">Apply</button>
  </div>
</div>
```

---

## Step 4 — `index.html`: JS — indicators panel functions (replacing existing)

### New/changed functions

```js
let _indSettingsTarget = null; // { paneId, instanceId }
let _indSettingsLineWidth = 2; // tracks selected width in menu

function renderIndicatorList(paneId) { /* builds #indList HTML */ }
function toggleAddIndDropdown(e) { /* shows/hides #addIndDropdown */ }
function addIndicator(type, paneId) { /* mgr.add, recompute, re-render, save */ }
function removeIndicator(id, paneId) { /* mgr.remove, re-render, save */ }

function openIndSettings(instanceId, paneId, btnEl) {
    // populate #indSettingsMenu with instance's current color/lineWidth/params
    // position menu near btnEl using getBoundingClientRect()
    _indSettingsTarget = { paneId, instanceId };
    // show menu
}
function applyIndSettings() {
    // read color, lineWidth, params from menu
    // mgr.update(id, params, color, lineWidth)
    // recompute, re-render list, save
    closeIndSettings();
}
function closeIndSettings() { document.getElementById('indSettingsMenu').style.display = 'none'; }
```

`openIndicatorsPanel(paneId, e)` — already exists, add `renderIndicatorList(paneId)` call inside it and also fill `#addIndDropdown` with type buttons.

`syncIndicatorPanelToState(paneId)` — update to just call `renderIndicatorList(paneId)` (old checkbox sync is gone).

Width buttons in `#indSM_widthBtns`: clicking highlights active one (blue border), sets `_indSettingsLineWidth`.

Close `#indSettingsMenu` on outside-click (add to existing `document.addEventListener("click", ...)` handler).

### Save format backward compat

`layout.indicators[paneId]` = `[{id, type, params, color, lineWidth, visible:true}, ...]`. `lineWidth` defaults to 2 in `loadFrom` if missing (old saves have no lineWidth field).

---

## Step 5 — `index.html`: Signal markers system

### Data structures (add to global state section)

```js
const _signalLog = [];    // [{id, time, direction, symbol, fullData}]
let _signalIdCounter = 0;
let _signalsVisible = false;
let _signalHideTimer = null;
let _signalPanelIndex = 0; // index in _signalLog for panel display
```

### `addSignalToLog(data)` — called from `showSignal()` for non-SKIP signals

```js
function addSignalToLog(data) {
    if (data.direction === 'SKIP') return;
    const now = Math.floor(Date.now() / 1000);
    _signalLog.push({
        id: ++_signalIdCounter,
        time: now,
        direction: data.direction,
        symbol: data.symbol,
        fullData: data
    });
    _signalPanelIndex = _signalLog.length - 1; // point to newest
    showSignalMarkers();          // always auto-activate
    renderSignalInPanel(_signalPanelIndex);
}
```

### `getMarkersForPane(paneId)`

```js
function getMarkersForPane(paneId) {
    const st = panesState[paneId];
    if (!st?.data?.length) return [];
    const minTime = st.data[0].time;
    const maxTime = st.data[st.data.length - 1].time;
    const tf = st.tfSeconds;

    return _signalLog
        .filter(s => s.symbol === currentSymbol)
        .map(s => {
            const snapped = Math.floor(s.time / tf) * tf;
            if (snapped < minTime || snapped > maxTime + tf) return null;
            return {
                time: snapped,
                position: s.direction === 'UP' ? 'belowBar' : 'aboveBar',
                color:    s.direction === 'UP' ? '#26a69a' : '#ef5350',
                shape:    'circle',
                // size: 1 keeps dots as small as a candle body, preventing overlap on dense signals
                size:     1,
                id:       String(s.id),
            };
        })
        .filter(Boolean)
        // LWC requires markers sorted by time
        .sort((a, b) => a.time - b.time);
}
```

### `refreshSignalMarkers()`

```js
function refreshSignalMarkers() {
    [1, 2].forEach(paneId => {
        const st = panesState[paneId];
        if (!st?.series) return;
        const markers = _signalsVisible ? getMarkersForPane(paneId) : [];
        try { st.series.setMarkers(markers); } catch(e) {}
    });
}
```

Called from:
- `showSignalMarkers()` / `hideSignalMarkers()`
- `handleMessage` after `history` setData (catches server-restart reload)
- `handleMessage` after each `update` candle (keeps markers valid as data grows)
- `switchSymbol()` after reconnection setup

### `showSignalMarkers()` / `hideSignalMarkers()` / `toggleSignalPanel()`

```js
function showSignalMarkers() {
    _signalsVisible = true;
    const btn = document.getElementById('signalLogBtn');
    if (btn) { btn.style.background = '#2962FF'; btn.style.color = '#fff'; }
    refreshSignalMarkers();
    resetSignalHideTimer();
}

function hideSignalMarkers() {
    _signalsVisible = false;
    const btn = document.getElementById('signalLogBtn');
    if (btn) { btn.style.background = ''; btn.style.color = ''; }
    refreshSignalMarkers();   // clears markers (empty array)
    clearSignalHideTimer();
    document.getElementById('signalPanel').style.display = 'none';
}

function resetSignalHideTimer() {
    clearSignalHideTimer();
    _signalHideTimer = setTimeout(hideSignalMarkers, 5 * 60 * 1000); // 5 min
}
function clearSignalHideTimer() {
    if (_signalHideTimer) { clearTimeout(_signalHideTimer); _signalHideTimer = null; }
}

function toggleSignalPanel() {
    if (_signalsVisible) {
        hideSignalMarkers();
    } else {
        if (_signalLog.length) renderSignalInPanel(_signalPanelIndex);
        showSignalMarkers();
    }
}
```

### `renderSignalInPanel(index)` — replaces `showSignal()` panel rendering

Renders signal at `_signalLog[index]`. Re-renders whole `#signalPanel` HTML.

**Symbol text**: `<span style="cursor:pointer;text-decoration:underline;pointer-events:all;" onclick="switchSymbol('${data.symbol}')">${data.symbol}</span>`

**Nav arrows** (if `_signalLog.length > 1`):
```html
<div class="sig-nav" style="display:flex;align-items:center;justify-content:space-between;
     margin-top:6px; border-top:1px solid #333; padding-top:4px; pointer-events:all;">
  <button onclick="navSignal(-1)" style="background:none;border:none;color:#aaa;cursor:pointer;font-size:16px;">←</button>
  <span style="font-size:11px;color:#888;">${index+1} / ${_signalLog.length}</span>
  <button onclick="navSignal(+1)" style="background:none;border:none;color:#aaa;cursor:pointer;font-size:16px;">→</button>
</div>
```

`navSignal(delta)`:
```js
function navSignal(delta) {
    _signalPanelIndex = Math.max(0, Math.min(_signalLog.length - 1, _signalPanelIndex + delta));
    renderSignalInPanel(_signalPanelIndex);
}
```

### Signal panel CSS update

Change `pointer-events: none` → keep it on the container (charts pass through) but individual interactive elements use `pointer-events: all`:
- `.sig-close` — already `pointer-events: all`
- symbol span — add `pointer-events: all`
- `.sig-nav` and its buttons — add `pointer-events: all`

### Marker click → navigate to that signal

In `setupInteractionHandlers`, inside `st.chart.subscribeClick(param =>` at the end of the handler:

```js
// If signals visible and there's a time param, check if click is near a marker
if (_signalsVisible && typeof param.time === 'number') {
    const tf = st.tfSeconds;
    const clickedBucket = Math.floor(param.time / tf) * tf;
    const matchIdx = _signalLog.findLastIndex(s => {
        const snapped = Math.floor(s.time / tf) * tf;
        return s.symbol === currentSymbol && snapped === clickedBucket;
    });
    if (matchIdx !== -1) {
        _signalPanelIndex = matchIdx;
        renderSignalInPanel(_signalPanelIndex);
        document.getElementById('signalPanel').style.display = 'block';
    }
}
```

`findLastIndex` is available in modern browsers; can polyfill with `[...arr].reverse().findIndex()` if needed.

### `showSignal(data)` — simplified

The existing `showSignal(data)` function is refactored to:
1. Call `addSignalToLog(data)` for non-SKIP signals
2. For SKIP signals: still show a brief panel (no markers, no log entry), 15-sec auto-hide unchanged
3. **Remove** the `_lastSignalTs` 60-second dedup entirely — every non-SKIP signal the server sends is logged and gets a marker. Also delete the `_lastSignalTs` global variable.

---

## Step 6 — `index.html`: Wiring `refreshSignalMarkers` into data flow

Add `refreshSignalMarkers()` calls:

1. In `handleMessage`, after `state.series.setData(state.data)` in the `history` branch
2. In `handleMessage`, after `state.indicatorManager?.recomputeAll(state.data)` in the `update` branch  
   *(only when `_signalsVisible` to avoid per-tick overhead; the filter check is O(n_signals) not O(n_candles))*
3. In `switchSymbol()` — after state reset

---

## Edge cases handled

| Case | Handling |
|---|---|
| Server restart mid-session — history starts later than stored signals | `getMarkersForPane` filters out signals with snapped time < `data[0].time` |
| Fast TF (S5/S10) — signal time doesn't snap to an existing bar | Snapped bucket falls within data range → LWC places marker on nearest bar |
| Page reload | `_signalLog` is in-memory, starts empty — no stale session markers |
| Symbol switch | `refreshSignalMarkers()` re-filters by `currentSymbol` |
| Two RSIs on same pane | `_rebuildOscillators` groups by type, both on pane 1 |
| Old localStorage format (no `id`, no `lineWidth`) | `loadFrom` generates id; defaults `lineWidth` to 2 |

---

## Implementation order

1. `js/indicators.js` — full refactor (instances array, pane grouping, serialize/load)
2. `index.html` — indicators panel HTML + CSS (new markup)
3. `index.html` — JS: `renderIndicatorList`, `addIndicator`, `removeIndicator`, `openIndSettings`, `applyIndSettings`, `closeIndSettings`, update `openIndicatorsPanel` / `syncIndicatorPanelToState`
4. `index.html` — JS: signal system (`_signalLog`, `addSignalToLog`, `getMarkersForPane`, `refreshSignalMarkers`, `showSignalMarkers`, `hideSignalMarkers`, `toggleSignalPanel`, `renderSignalInPanel`, `navSignal`)
5. `index.html` — wire `refreshSignalMarkers` into `handleMessage` + `switchSymbol`
6. `index.html` — add marker-click detection in `subscribeClick` handler
7. Test: verify each feature, then verify no regressions in drawing tools / alerts / existing indicators from localStorage

---

## Verification

```bash
# Reload frontend (no server restart needed)
systemctl status chart-frontend   # confirm port 8082 still serving

# Manual checks:
# 1. Open index.html, open Indicators panel → "＋ Add" dropdown appears with all types
# 2. Add EMA twice with different periods → two EMA lines on chart, both in legend
# 3. Add RSI twice → both appear in the same oscillator sub-pane
# 4. ⚙ button on any indicator → settings menu appears, change color/width/period → Apply → updates live
# 5. ✕ button removes indicator cleanly
# 6. Reload page → indicators restored from localStorage
# 7. Сигналы button: click once → blank (no signals yet); wait for signal → panel + dots appear; 5 min later → auto-hide
# 8. Signal panel: ← → arrows cycle through session signals
# 9. Click on symbol text in signal panel → switches pair
# 10. Click on chart near a marker dot → panel shows that signal
# 11. Existing features: drawings, alerts, TF switch, symbol switch — no regressions
```

---

## Step checklist (tick off as each session completes)

- [ ] **Step 1** — `js/indicators.js` full refactor (`_instances[]`, `add/remove/update/serialize/loadFrom`, pane grouping, legend, backward compat)
- [ ] **Step 2** — `index.html` indicators panel HTML/CSS redesign (replace static rows with `#indList` shell + `#addIndDropdown`)
- [ ] **Step 3** — `index.html` `#indSettingsMenu` context menu div + CSS
- [ ] **Step 4** — `index.html` JS: `renderIndicatorList`, `addIndicator`, `removeIndicator`, `openIndSettings`, `applyIndSettings`, `closeIndSettings`; update `openIndicatorsPanel` / `syncIndicatorPanelToState`; outside-click close
- [ ] **Step 5** — `index.html` JS: signal system (`_signalLog`, `addSignalToLog`, `getMarkersForPane`, `refreshSignalMarkers`, `showSignalMarkers`, `hideSignalMarkers`, `toggleSignalPanel`, `renderSignalInPanel`, `navSignal`); refactor `showSignal()`; remove `_lastSignalTs` dedup
- [ ] **Step 6** — Wire `refreshSignalMarkers` into `handleMessage` (history + update) + `switchSymbol`; add marker-click detection in `subscribeClick`
- [ ] **Verification** — Manual test all 11 checklist items above; confirm no regressions
