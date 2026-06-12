# Frontend Refinement — Progress Log

**Plan:** `.claude/frontend-refinement-plan.md`  
**Canonical plan file:** `/root/.claude/plans/1-b-2-merge-shiny-balloon.md`  
**Goal:** Multi-instance indicators + per-instance settings menu + signal chart markers

---

## Session 1 — 2026-06-11

**Model:** Sonnet 4.6  
**Outcome:** Planning only

- User requirements gathered and clarified (UI pattern B, pane grouping, signal marker behavior, nav arrows)
- Full implementation plan written and approved
- No code written

---

## Session 2 — 2026-06-12

**Model:** Opus 4.8 (Step 1) · Sonnet 4.6 (Steps 2–4 + review)  
**Outcome:** Steps 1–4 complete · 3 bugs found and fixed post-step · code uncommitted

---

### Step 1 — `js/indicators.js` refactor

**Method:** Opus 4.8 agent produced the complete rewrite; verified with `node --check` and grep.

**What changed:**
- Replaced `_overlays{}` + `_oscillators[]` with flat `_instances[]` array + `_idCounter`
- `add()` no longer blocks duplicates — always appends, returns the new `id`
- `remove(id)` / `update(id, ...)` are now id-based instead of type-based
- New helpers: `_addInstanceSeries`, `_destroyInstanceSeries`, `_isOscillator`
- `_rebuildOscillators` groups same-type oscillators into one sub-pane (RSI×2 → pane 1, etc.); reference lines only on first instance per pane
- `serialize()` includes `id` + `lineWidth`; `loadFrom()` restores id, defaults `lineWidth` to 2 for old saves
- `IndicatorMath` block untouched

**Verification:** `node --check` passed · no `_overlays`/`_oscillators` references remain · both `window.*` exports present

---

### Step 2 — `index.html`: indicators panel HTML/CSS

**Method:** Direct edit.

**What changed:**
- Removed all 6 static checkbox rows (SMA/EMA/Bollinger/RSI/MACD/Stoch) from `#indicatorsPanel`
- Removed old CSS: `.ind-label`, `.ind-param`, `.ind-color`, `.ind-section-title`
- Added new CSS: `#addIndBtn`, `#addIndDropdown button`, `#indList .ind-row`, `.ind-swatch`, `.ind-name`, `.ind-settings-btn`, `.ind-del-btn`
- New panel shell: header with "Indicators" + "＋ Add ▾" button, empty `#indList` div

---

### Step 3 — `index.html`: `#indSettingsMenu`

**Method:** Direct edit, inserted before `#indicatorsPanel`.

**What changed:**
- Added `#indSettingsMenu` floating div: title, `#indSM_colorRow` (color picker), `#indSM_widthBtns` (1–4), `#indSM_params` (dynamic), Cancel/Apply buttons
- `z-index: 8500` (above indicators panel at 7000)

---

### Step 4 — `index.html`: indicators panel JS

**Method:** Direct replacement of the entire `INDICATORS PANEL` JS block.

**Old functions removed:** `getIndParams`, `getIndColor`, `onIndToggle`, `onIndParam`, `onIndColor`, `syncIndicatorPanelToState` (inline), old outside-click handler

**New functions added:**
- `IND_DEFAULTS` / `IND_LABELS` constants
- `saveIndicators(paneId)` — unchanged semantics
- `renderIndicatorList(paneId)` — builds `#indList` from `mgr.getInstances()`
- `_instParamStr(inst)` — formats param string for label
- `toggleAddIndDropdown(e)` — populates and shows/hides `#addIndDropdown`
- `addIndicator(type, paneId)` — adds with defaults, recomputes, re-renders, saves
- `removeIndicator(id, paneId)` — removes by id, recomputes, re-renders, saves
- `openIndSettings(instanceId, paneId, btnEl)` — populates and positions `#indSettingsMenu`
- `applyIndSettings()` — reads menu, calls `mgr.update`, recomputes, re-renders, saves
- `closeIndSettings()` — hides menu
- `openIndicatorsPanel` updated to call `renderIndicatorList` instead of old checkbox sync
- `syncIndicatorPanelToState` reduced to `renderIndicatorList(paneId)` delegate
- Width button `click` handler on `#indSM_widthBtns`
- Outside-click handler closes both panel and settings menu

---

### Post-Step 4 Logic Review

**Method:** Full read of `js/indicators.js` + indicators JS block in `index.html`. **Opus 4.8.**

**Three issues found and fixed:**

#### 🔴 HIGH — `_idCounter` desync after `loadFrom` → duplicate IDs

**Root cause:** `loadFrom` calls `add()` (bumps `_idCounter` by N), then overwrites generated ids with saved `cfg.id` values. If saved ids have higher suffixes than `_idCounter`, a later `add()` could generate an id already present (e.g. restored `sma_3`, counter at 1 → next two adds generate `sma_2` then `sma_3`, collision).

**Consequence:** `remove()` uses `filter(id !== x)` so a collision deletes two instances; `update()`/`openIndSettings()` use `.find()` so they silently edit the wrong one.

**Fix (indicators.js `loadFrom`):** After restoring all instances, advance `_idCounter` past every numeric suffix in `_instances`:
```js
for (const inst of this._instances) {
    const n = parseInt(String(inst.id).split('_').pop(), 10);
    if (Number.isFinite(n) && n > this._idCounter) this._idCounter = n;
}
```

#### 🟡 MEDIUM — MACD color picker misleading

**Root cause:** `_rebuildOscillators` hardcodes MACD line colors (`#2962FF` / `#ff9800`). The swatch and color picker both read/write `inst.color`, so changing MACD color turns the swatch but not the chart line.

**Fix:** Remove color selection for MACD entirely — decision to keep fixed conventional colors.
- Added `id="indSM_colorRow"` to the color row in `#indSettingsMenu`
- `openIndSettings`: hides `#indSM_colorRow` when `inst.type === 'macd'`
- `applyIndSettings`: skips color input for MACD, keeps `inst.color` unchanged

#### 🟢 LOW — Dead `setColor` method with broken oscillator behavior

**Root cause:** `setColor(type, color)` called `update()` for oscillators (which tears down + rebuilds series **empty** — no `recomputeAll` after). No callers remained after Step 4, making it dead but trappable code.

**Fix:** Removed `setColor` entirely from `IndicatorManager`. Confirmed zero callers in `js/` and `index.html`.

---

## Status

| Step | Status | Notes |
|---|---|---|
| 1 — `indicators.js` refactor | ✅ Done | Post-review fixes applied |
| 2 — Panel HTML/CSS | ✅ Done | |
| 3 — Settings menu HTML | ✅ Done | |
| 4 — Panel JS | ✅ Done | Post-review fixes applied |
| 5 — Signal markers system | ⬜ Pending | |
| 6 — Wire `refreshSignalMarkers` | ⬜ Pending | |
| Verification | ⬜ Pending | 11-item manual checklist |

**All changes are uncommitted.** Files modified: `js/indicators.js`, `index.html`
