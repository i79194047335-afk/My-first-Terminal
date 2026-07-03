# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# Project: Terminal

Web trading terminal (TradingView-like), browser-based. Displays real-time tick data from a broker, runs signal/pattern detection, and tracks live signal performance.

## Stack

- **Frontends (two, same backend):**
  - [index_split.html](index_split.html) — original UI
  - [index.html](index.html) — parallel UI with TradingView-style indicators (`js/indicators.js`)
  - Both served by systemd service `chart-frontend` (`python3.7 -m http.server 8082`) and both connect to the same WebSocket (`ws://...:8765`)
- **Backend:** [server.py](server.py) — Python WebSocket server (port 8765), runs as systemd service `chart`
- **Broker:** FXCM (ForexConnect Python SDK) — live tick streaming, no polling
- **Data storage:** JSON files on disk (market_memory.json, signal_log.json, signal_stats.json)
- **Python version:** 3.7 (see `__pycache__`)

## Architecture — data flow

```
FXCM Broker ──(push)──> server.py
                          │
                          ├─ tick_writer() ──> data/*.csv (one file per symbol/day)
                          ├─ MarketEngine.update_tick() ──> velocity, pressure, micro_trend, volatility
                          ├─ StructureEngine.update() ──> near_high / near_low detection
                          ├─ signal_engine.evaluate_signal() ──> UP/DOWN/SKIP decision
                          ├─ signal_tracker (store + resolve at T+240s) ──> signal_log.json
                          ├─ pattern_memory (store + resolve at T+240s) ──> market_memory.json
                          └─ WebSocket ──(push)──> Browser (index_split.html + index.html)
```

Key: `server.py:process_tick()` is the central dispatch — every tick flows through market engine → structure engine → signal engine → pattern memory → tracker → WebSocket broadcast.
**Signal filter active:** `signal_engine.ASIA_TIGHT_ONLY=True` — only Asia session + tight edge (pos≤0.05) signals are sent. SKIP signals are silenced (commented out in server.py).

## Python files (backend)

- [server.py](server.py) — WebSocket server + FXCM stream listener. Owns per-symbol state (`symbols_state`) and the main tick processing loop. Does NOT use asyncio for tick processing — ticks arrive via FXCM callback in a daemon thread.
- [market_engine.py](market_engine.py) — `MarketEngine` class. Computes velocity (smoothed price change over 3s window), tick pressure/imbalance, acceleration, tick rate, micro-trend, HTF trend (M5, 30 candles), range position, and volatility. Returns analysis dict. Has a state-change filter (skips if delta < 0.05 across key metrics).
- [structure_engine.py](structure_engine.py) — `StructureEngine` class. Tracks 300 recent prices, detects when price is within 15% of range high/low (`near_high`/`near_low`).
- [signal_engine.py](signal_engine.py) — Scoring-based signal generator. Hard filters (minute ±3 of hour change, low volatility, anomalous vol_ratio > 3.0), then accumulates UP/DOWN scores from contrarian factors (range position, velocity edge, contrarian pressure, micro-trend exhaustion, London session amplification) plus a pattern-memory lookup via `market_memory.json`. Returns `SignalResult` dataclass. **Reweighted from live-log validation:** hourly bias and symbol bias removed (no live edge), velocity-fast→UP removed, score threshold ≥4 (UP) / ≥5 (DOWN). **Asia∩TIGHT filter active (2026-07-04):** `ASIA_TIGHT_ONLY=True` — only fires signals in Asia session (0-8 UTC) AND at the tightest range edge (pos≤0.05). Forward 18d: WR 63.8% (n=177). Full log: 60.1% (n=1651). CSCV PBO=0.009. ~11 signals/day. Откат: `ASIA_TIGHT_ONLY=False`.
- [signal_tracker.py](signal_tracker.py) — Tracks live signals. Stores signal at creation time, resolves after 240s (checks if price moved in signal direction, ≥1 pip threshold). Writes `signal_log.json`, computes aggregate stats to `signal_stats.json`. Module-level `_active_signals` list.
- [pattern_memory.py](pattern_memory.py) — Pattern storage for learning. Stores patterns with T+60s entry delay, T+240s expiry, 2-pip noise filter. Checks news calendar (`data_loaders/news_calendar.csv`) for high-impact events in window. Writes `market_memory.json`.

## JavaScript files (frontend)

- [index_split.html](index_split.html) — original UI, includes all JS inline and from `js/` directory. Uses LightweightCharts CDN.
- [index.html](index.html) — parallel UI with TradingView-style indicators. Uses [js/indicators.js](js/indicators.js) (`IndicatorMath`: SMA, EMA, Bollinger, RSI Wilder, MACD + `IndicatorManager`). Same WebSocket backend as index_split.html.
- [js/time-mapper.js](js/time-mapper.js) — `TimeMapper` class, maps unix time to canvas X coordinate via LightweightCharts API
- [js/drawing-engine.js](js/drawing-engine.js) — `DrawingEngine` class, canvas overlay on top of chart for line/rectangle drawing tools, hit testing, drag preview
- [js/drawing-controller.js](js/drawing-controller.js) — `DrawingController` singleton, manages active tool state (idle/lineOverlay/rectOverlay/hline/alert) and drag state
- [js/storage.js](js/storage.js) — localStorage persistence layer for layout, chart colors, drawings, and alerts. `restoreDrawings()` clears and reconstructs all price lines + overlay objects on symbol switch.
- [js/ui-tools.js](js/ui-tools.js) — Tool button UI state management (highlighting active tool)
- [js/context-menu.js](js/context-menu.js) — Right-click context menus: candle color picker, line/rectangle properties (color, width, fill), alert management, chart context menu (add alert, open settings)

## Data files (never delete)

- `market_memory.json` — resolved pattern history (for signal_engine memory lookups)
- `signal_log.json` — all resolved live signals with outcomes
- `signal_stats.json` — aggregate signal statistics
- `data/*.csv` — tick data (one file per symbol per day, e.g. `EURUSD_20260525.csv`)
- `vel_log/velocity_*.csv` — velocity logs per day

## Broker connection

- FXCM demo account, uses `forexconnect` Python package
- Credentials in [server.py](server.py#L21-L24) (hardcoded — do not extract)
- Symbols: AUD/USD, EUR/USD, USD/CAD, USD/JPY
- Timeframes: S5, S10, S15, S30, M1, M3, M5, M15, H1 (seconds defined in `TF_SECONDS`)

## How to run

Both backend and frontend run as systemd services (auto-start on boot, auto-restart on crash):

```bash
# Backend (WebSocket server, port 8765) — DO NOT restart without permission
systemctl {start|stop|restart|status} chart

# Frontend (HTTP server, port 8082) — serves both index.html and index_split.html
systemctl {start|stop|restart|status} chart-frontend
```

Service units: `/etc/systemd/system/chart.service`, `/etc/systemd/system/chart-frontend.service`

URLs:
- `http://<server-ip>:8082/index.html` — parallel UI
- `http://<server-ip>:8082/index_split.html` — original UI

Note: port 8080 is occupied by `code-server` (not the frontend). The frontend uses **8082**.

## Rules

- NEVER restart or kill server.py (`chart` service) without explicit instruction
- Always add docstrings to Python functions (format: summary, Args, Returns)
- Comments in code: Russian or English, be consistent
- Before any edit to a live file: confirm with user first

## Known issues

- Both frontends break via `file://` in Chrome/Opera — must serve via HTTP (use the `chart-frontend` service on port 8082)
- Python 3.7 compiled `.pyc` files in `__pycache__/` — do not upgrade Python without checking `forexconnect` compatibility
- Port 8080 is taken by `code-server`, not the frontend — the frontend HTTP server runs on **8082**

## Downtime window (safe to edit)

- 21:00–23:00 UTC daily
- 21:00 Fri – 23:00 Sun UTC
- Cron handles stop/start of the `chart` service automatically (crontab is in **local time = UTC+5**: stop at 02:00, start at 04:00 local)

