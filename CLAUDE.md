# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# Project: Terminal

Web trading terminal (TradingView-like), browser-based. Displays real-time tick data from a broker, runs signal/pattern detection, and tracks live signal performance.

## Stack

- **Frontend:** [index.html](index.html) — UI with TradingView-style indicators (`js/indicators.js`),
  served by systemd service `chart-frontend` (`python3.7 -m http.server 8082`), connects to `ws://...:8765`.
  `index_split.html` (старый UI) убран в `archive/` в Фазе 2.2.
- **Backend:** [server.py](server.py) — Python WebSocket server (port 8765), runs as systemd service `chart`
- **Broker:** FXCM (ForexConnect Python SDK) — live tick streaming, no polling
- **Data storage:** SQLite `market.db` (свечи, Фаза 1) + `data/*.csv` (сырые тики).
  JSON-хранилища сигнального контура (`market_memory.json`, `signal_log.json`,
  `signal_stats.json`) — в `archive/old_data/`, см. Фазу 2.2.
- **Python version:** 3.7 (see `__pycache__`)

## Architecture — data flow

```
FXCM Broker ──(push)──> server.py
                          │
                          ├─ tick_writer() ──> data/*.csv (one file per symbol/day)
                          ├─ MarketEngine.update_tick() ──> velocity, pressure, micro_trend, volatility
                          ├─ db_writer() ──> market.db (закрытые свечи, окно 2000 баров/ТФ)
                          └─ WebSocket ──(push)──> Browser (index.html)

                          [ выключено флагом SIGNALS_ENABLED=False, Фаза 2.1:
                            StructureEngine → signal_engine → signal_tracker → pattern_memory ]
```

Key: `server.py:process_tick()` is the central dispatch — every tick flows through market engine →
candle slicing → db_writer → WebSocket broadcast.

**Сигнальный контур ВЫКЛЮЧЕН (Фаза 2.1):** `server.py:SIGNALS_ENABLED = False`. Код движков не удалён,
возврат = флаг в `True`. Под флагом не только вызов в `process_tick`, но и **импорт** `pattern_memory` /
`signal_tracker`: оба читают свои JSON на уровне модуля (~100 МБ RSS), а `pattern_memory` вешает
`atexit`-сохранение (перезапись 73-МБ файла при каждой остановке). Их данные — в `archive/old_data/`.

## Python files (backend)

- [server.py](server.py) — WebSocket server + FXCM stream listener. Owns per-symbol state (`symbols_state`) and the main tick processing loop. Does NOT use asyncio for tick processing — ticks arrive via FXCM callback in a daemon thread.
- [market_engine.py](market_engine.py) — `MarketEngine` class. Computes velocity (smoothed price change over 3s window), tick pressure/imbalance, acceleration, tick rate, micro-trend, HTF trend (M5, 30 candles), range position, and volatility. Returns analysis dict. Has a state-change filter (skips if delta < 0.05 across key metrics).
- [structure_engine.py](structure_engine.py) — `StructureEngine` class. Tracks 300 recent prices, detects when price is within 15% of range high/low (`near_high`/`near_low`).
Модули ниже (`structure_engine`, `signal_engine`, `signal_tracker`, `pattern_memory`) **не исполняются**
с Фазы 2.1 — `SIGNALS_ENABLED = False`. Описания сохранены на случай возврата контура.

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

- `market.db` — SQLite: свечи, окно 2000 баров на каждый (provider, symbol, tf). Боевой файл.
- `data/*.csv` — tick data (one file per symbol per day, e.g. `EURUSD_20260525.csv`).
  Вечный архив: из тиков M1 восстановим точно, из БД — уже нет.
- `vel_log/velocity_*.csv` — velocity logs per day
- `briefing.json` — брифинг сессии, пишет `pre_session_brief.py` (крон), читает и шлёт на фронт `server.py`

Заархивировано в Фазе 2.2 (`archive/old_data/`, **не удалять**): `market_memory.json`,
`signal_log.json`, `signal_stats.json`, `session_bias.json`, `briefing_context.json`, `History/`.

## Брифинг

`pre_session_brief.py` — крон 3×/сутки (04/12/17 локального = UTC+5), пред-Азия / пред-Лондон / пред-NY.
RSS + `data_loaders/news_calendar.csv` + техническая картина **из `market.db`** (не из `market_memory.json`
— Фаза 2.1) → DeepSeek → `briefing.json`. Требует `DEEPSEEK_API_KEY` в `.env`.
`pre_asia_brief.py` — старая версия того же, в кроне **нет**, не используется.

Известное: RSS-фиды наполовину мертвы (Reuters закрыл публичные RSS) — новостей приходит мало.

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

## In-progress: intrade.bar trading bot

See [intrade_bot_plan.md](intrade_bot_plan.md) for full plan.

**Status:** Step 1 — waiting for cURL of trade-open request from intrade.bar (Ivan to capture via Chrome DevTools F12 → Network). Next session picks up from there.

Summary: bot listens to our WebSocket signals (Asia∩TIGHT) and opens binary option trades on intrade.bar via HTTP requests (no official API — reverse-engineered from browser traffic).

## Downtime window — ОТМЕНЕНО (2026-07-14)

Ежедневного окна простоя **больше нет**: крон-остановка сервиса `chart` снята.

Причина: история свечей теперь живёт в SQLite (`market.db`, Фаза 1), и каждая
остановка сервера — это дыра в секундных ТФ (S5–S30 копятся только с живых тиков,
из M1 не выводятся). Крон был главным источником таких дыр — по два часа в сутки.

- Строка `0 2 * * 2-6 systemctl stop chart` в crontab **закомментирована**.
- Строка `0 4 * * 1-5 systemctl start chart` **оставлена** как страховка: если
  процесс упадёт ночью, утренний `start` его подберёт. Снять её можно будет после
  Фазы 4 (watchdog + health-эндпоинт).
- Крон — в **локальном времени = UTC+5**.

Правки живых файлов теперь согласовывать с владельцем по времени, а не полагаться
на окно.

