# Implementation Plan — Parallel `index.html` with TradingView-style indicators

> **For the implementing agent (Sonnet 4.6).** This is a complete spec. Build from it directly.
> Do NOT re-scope; the decisions below are already approved by the user.

## 0. Goal & hard constraints

Create a **new** front-end file `index.html` (parallel to the existing `index_split.html`, which
stays the live terminal). Same WebSocket backend, no backend changes. Replace the separate
stochastic sub-panes with **TradingView-style indicators**:

- **Overlay indicators** drawn on the price axis: **SMA, EMA, Bollinger Bands**.
- **Oscillator indicators** in their own bottom sub-panes (LightweightCharts v5 native panes):
  **RSI, MACD**.
- All indicators toggle on/off and are configurable, tucked behind an **"Indicators ▾"** button
  in each price pane's toolbar.
- Config persists in `localStorage`.

### Do NOT touch
- `index_split.html` (the live terminal) — leave it exactly as is.
- `server.py`, `*.json`, `data/*.csv`, `vel_log/*` — backend & data untouched. No server restart.
- The existing `js/` modules — **reuse them unchanged** (`time-mapper.js`, `drawing-engine.js`,
  `drawing-controller.js`, `storage.js`, `ui-tools.js`, `context-menu.js`).

### Key backend fact
Candles are **OHLC only — no volume** (`server.py:244-250`). So no VWAP / volume indicators.
All five chosen indicators are OHLC-derived, so we're covered. Candle shape:
`{ time:<unix s>, open, high, low, close }`. WebSocket: `ws://89.107.10.204:8765`,
message types `history` / `update` / `analysis` / `signal` / `alert` / `alert_created`.

---

## 1. Files

| File | Action |
|---|---|
| `index.html` | **New.** Copy of `index_split.html`, then edits in §2. |
| `js/indicators.js` | **New.** Indicator math + `IndicatorManager`. See §4–§5. |
| existing `js/*.js` | Reuse unchanged. `index.html` loads them with the same `<script>` tags. |

---

## 2. `index.html` — how it differs from `index_split.html`

Start by copying `index_split.html` to `index.html`, then:

1. **Remove the stochastic sub-panes.** Delete the `indicator-pane` blocks (`#pane3` / `#pane4`,
   `#chart3` / `#chart4`, the `enginePanel*` divs) from the markup. The `price-pane` should now
   fill its column. Adjust the column CSS so the price pane is `flex:1` full height.
2. **Delete dead stochastic code** in the inline `<script>`: `calculateStochastic()`,
   `updateIndicator()`, `createIndicatorPane()`, the `indicatorState` object, the
   `indicatorPanes` array, the price→indicator `subscribeVisibleLogicalRangeChange` sync block
   inside `createChartInternal` (`index_split.html:1056-1085`), and the `bindHorizontalSync()`
   helper. Remove their call sites in `initialize()`.
3. **Add `<script src="js/indicators.js"></script>`** after `drawing-engine.js` and before the
   inline `<script>` (it depends on nothing but `LightweightCharts`).
4. **Refactor the duplicated WebSocket handler.** `index_split.html` has TWO near-identical
   `onmessage` handlers (`createPane` ~L831 and `switchSymbol` ~L1949) that have already drifted
   (the `analysis`/`signal` dispatch is wrongly nested inside the `update` block at L880). In the
   new file, extract **one** `handleMessage(id, state, msg)` function and use it in both places.
   This is where indicator recompute hooks live (§6) — doing it once avoids the drift bug.
5. **Toolbar button.** Add an **"Indicators ▾"** button to *each* price pane's toolbar
   (pane 1 toolbar already has the drawing tools; pane 2's toolbar currently only has `#tf2` —
   add the button there too). Clicking opens the indicators panel for that pane (§3).
6. Add the indicators panel markup (§3) — one panel, reused, scoped to whichever pane opened it.

Everything else (drawing tools, alerts, symbol switch, mobile tabs, signal panel,
`updateMarketEngine`) carries over unchanged.

---

## 3. Indicators panel UI

A single dropdown panel (hidden by default), positioned under the clicked "Indicators ▾" button.
Track which pane (1 or 2) opened it in a module variable, e.g. `indicatorPanelPane`.

Rows (checkbox + inline config), minimal but functional:

| Indicator | Controls |
|---|---|
| **SMA** | on/off · period (default 20) · color |
| **EMA** | on/off · period (default 21) · color |
| **Bollinger** | on/off · period (default 20) · mult (default 2) · color |
| **RSI** | on/off · period (default 14) |
| **MACD** | on/off · fast (12) · slow (26) · signal (9) |

On any change → call the pane's `IndicatorManager` (`add`/`remove`/`update`), then persist (§7).
Reuse the styling conventions already in the file (the existing context menus / dropdowns).
Close on outside-click, matching the existing menu pattern in `context-menu.js`.

---

## 4. `js/indicators.js` — math (pure functions)

Expose `window.IndicatorMath`. Input `data` is the candle array (`{time,open,high,low,close}`),
compute on `close`. Output arrays are **time-aligned to `data`**, with `null`/`{time,value:null}`
for the warm-up region (LightweightCharts skips `null` values — use `{ time, value: undefined }`
is NOT allowed; use whitespace by **omitting** those points OR push `{time, value:null}` — verify
which the loaded v5 build accepts; `value:null` is the safe choice for line series gaps).

```
IndicatorMath = {
  sma(data, period)                     -> [{time, value}]
  ema(data, period)                     -> [{time, value}]
  bollinger(data, period, mult)         -> { basis:[...], upper:[...], lower:[...] }   // basis=SMA, ±mult*stdev (population stdev over window)
  rsi(data, period)                     -> [{time, value}]   // Wilder's smoothing; values 0..100
  macd(data, fast, slow, signal)        -> { macd:[...], signal:[...], hist:[...] }    // macd=EMA(fast)-EMA(slow); signal=EMA(signal) of macd; hist=macd-signal
}
```

Formulas (standard):
- **SMA(p):** mean of last `p` closes.
- **EMA(p):** `k = 2/(p+1)`; seed with SMA(p) at index `p-1`, then `ema = close*k + prevEma*(1-k)`.
- **Bollinger:** basis = SMA(p); upper/lower = basis ± mult·σ where σ is population stdev of the
  same window.
- **RSI (Wilder):** first avgGain/avgLoss = SMA of gains/losses over `p`; then
  `avgGain = (prevAvgGain*(p-1)+gain)/p` (same for loss); `RS = avgGain/avgLoss`;
  `RSI = 100 - 100/(1+RS)`. Guard `avgLoss==0 → RSI=100`.
- **MACD:** as annotated above. `hist` points carry a color for the histogram (green ≥0, red <0):
  emit `{time, value, color}`.

Keep these allocation-light but correctness first; the visible series is at most a few thousand
points, so full recompute per update is fine (§6).

---

## 5. `js/indicators.js` — `IndicatorManager`

One instance **per price pane**, created right after that pane's chart + candle series exist.

```
class IndicatorManager {
  constructor(paneId, chart, candleSeries)   // chart = the LWC chart, candleSeries on pane 0

  // lifecycle
  add(type, params)        // create series, register, then recompute
  remove(type)             // remove this indicator's series, then rebuild oscillator panes
  update(type, params)     // change params -> recompute (and rebuild panes if it's an oscillator)
  setColor(type, color)    // overlay color
  has(type)                // bool

  recomputeAll(data)       // called on history + on each candle update; recompute every active indicator and setData
  serialize()              // -> [{type, params, color, visible}]   for localStorage
  loadFrom(configArray, data)  // recreate indicators from persisted config, then recomputeAll
  destroyAll()             // remove every series + extra panes (used if ever needed)
}
```

### Series creation per indicator

**Overlays — pane 0 (share price axis automatically):**
- **SMA / EMA:** one `chart.addSeries(LightweightCharts.LineSeries, {color, lineWidth:2, priceLineVisible:false, lastValueVisible:false})`.
- **Bollinger:** three `LineSeries` on pane 0 — basis (dashed via `lineStyle: LightweightCharts.LineStyle.Dashed`), upper, lower (solid, same color, thinner). No band fill (LWC has no native between-lines fill; skip it).

**Oscillators — own bottom panes (v5 native panes):**
- The pane index is the **third argument** to `addSeries`:
  `chart.addSeries(SeriesDef, options, paneIndex)`.
- **RSI:** one `LineSeries` on its pane. Add reference lines with
  `series.createPriceLine({price:70,...})`, `30`, and `50` (dashed, grey). Fix the scale to 0..100
  — set the series' `autoscaleInfoProvider` to return `{priceRange:{minValue:0,maxValue:100}}`,
  or apply `priceScale` scaleMargins; pick whichever the loaded v5 build supports (verify).
- **MACD:** on its pane — one `HistogramSeries` (the `hist`, per-point `color`) + two `LineSeries`
  (macd, signal).

### Pane-index management (keep it simple & robust)
There are at most 2 oscillators. Index juggling on removal is error-prone, so:
**maintain an ordered list of active oscillators; on ANY oscillator add/remove/param-change,
tear down all oscillator series + their panes and rebuild from scratch** in order
(RSI before MACD, say). Overlays (pane 0) are managed incrementally and never torn down by this.

- Price chart is pane **0**.
- When rebuilding, assign oscillator pane indices `1, 2, …` in list order.
- To size them TradingView-style, after creation set heights via the pane API
  (`chart.panes()[i].setHeight(px)` — verify method name against the CDN build; v5 exposes
  `chart.panes()` returning pane objects with height controls). Give each oscillator pane roughly
  20–25% of chart height; price pane keeps the rest.
- Removing the last oscillator must leave only pane 0 (no empty pane). Use `chart.removePane(index)`
  if the build requires explicit removal after removing its series (verify; some v5 builds
  auto-collapse empty panes).

> ⚠️ **Verify the exact v5 pane API against the CDN build actually loaded**
> (`unpkg.com/lightweight-charts@5/.../standalone.production.js`). The third-arg `paneIndex` on
> `addSeries`, `chart.panes()`, `pane.setHeight()`, and `chart.removePane()` are the v5 surface,
> but confirm names/signatures before relying on them — open the page console and inspect, or
> check the matching version's docs. If a method differs, adapt.

---

## 6. Wiring into the data flow

In the single `handleMessage(id, state, msg)`:

- **`history`** (full reset): after `state.series.setData(state.data)`, call
  `state.indicatorManager.recomputeAll(state.data)`.
- **`update`** (new/extended candle): after `state.series.update(msg.candle)` and updating
  `state.data`, call `state.indicatorManager.recomputeAll(state.data)`.
  (Full recompute each tick is acceptable here — bounded data, cheap. Optimize to incremental
  last-bar update only if profiling shows a problem.)

Create the manager where the chart is created (`createChartInternal`), store it on the pane state:
`state.indicatorManager = new IndicatorManager(id, st.chart, st.series)`. Then load persisted
config once history first arrives: `state.indicatorManager.loadFrom(layout.indicators?.[id] || [], state.data)`.

- **TF change / symbol switch:** the chart object persists, so series persist; the fresh `history`
  message triggers `recomputeAll`, which re-renders against the new data. Indicators are **global
  per price-pane** (NOT per-symbol — TradingView keeps indicators across symbol switches), so do
  **not** clear them on symbol switch. Just let recompute run.

---

## 7. Persistence

Extend the existing `layout` object (already saved via `StorageLayer.autoSave(layout)`):

```js
layout.indicators = {
  "1": [ {type:"sma", params:{period:20}, color:"#2962FF", visible:true}, ... ],
  "2": [ ... ]
};
```

- Save after every panel change: `layout.indicators[paneId] = manager.serialize(); StorageLayer.autoSave(layout);`
- Load in `loadFrom` when the pane's first `history` arrives.
- Global (not keyed by symbol), matching TradingView behaviour.
- No change needed to `js/storage.js` — `layout` is a free-form object already persisted whole.

---

## 8. Build order (incremental, verify at each step)

1. **Copy** `index_split.html` → `index.html`; strip stochastic panes/code (§2.1–2.4); confirm the
   two price charts + drawings + alerts still work via `python -m http.server 8080`.
2. **`js/indicators.js` math** (§4) — unit-sanity in console against a known series.
3. **Overlays first** (SMA/EMA/Bollinger) via a minimal `IndicatorManager` on pane 0; hardcode one
   on to confirm it overlays and tracks the candles on update + TF switch.
4. **Indicators panel UI** (§3) wired to add/remove/update overlays; persistence (§7); reload test.
5. **Oscillator panes** (RSI, then MACD) with the tear-down/rebuild strategy (§5); verify pane
   appears, time axis stays synced, reference lines show, pane collapses when toggled off.
6. **Full pass:** both panes, switch symbol & TF, reload (persistence), mobile tab view, ensure
   drawing tools / alerts / signal panel still behave.

---

## 9. Acceptance checklist

- [ ] `index_split.html`, `server.py`, data files untouched; no server restart.
- [ ] `index.html` loads over HTTP, both price charts stream live.
- [ ] "Indicators ▾" in each pane toggles SMA / EMA / Bollinger / RSI / MACD.
- [ ] Overlays sit on the price axis and update every candle; oscillators sit in synced bottom
      panes that appear on enable and collapse on disable.
- [ ] Params (periods, mult, MACD fast/slow/signal, colors) apply live.
- [ ] Config survives reload (localStorage) and persists across symbol/TF switches.
- [ ] Existing features (drawings, alerts, signal panel, mobile tabs) still work.

---

## 10. Known risks / notes for the implementer
- **v5 pane API names** — verify against the loaded CDN build (§5 warning).
- **Warm-up gaps** — feed line series `{time, value:null}` (or omit points) for indices before the
  indicator is defined; don't feed `0`.
- **Duplicated handler** — fix by extracting one `handleMessage`; don't copy the existing
  `update`/`analysis` nesting bug.
- If live-update recompute ever flickers or lags, switch that hot path to incremental last-bar
  update — but only if it's actually a problem.
