# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# Project: Terminal

Web trading terminal (TradingView-like), browser-based. Displays real-time tick
data from a broker, slices it into candles and range bars, stores history in
SQLite, and streams it to the browser. A signal/pattern detection loop exists but
is currently **disabled** (see below).

## Stack

- **Frontend:** [index.html](index.html) — UI with TradingView-style indicators
  (`js/indicators.js`), served by systemd service `chart-frontend`
  (`python3.7 -m http.server 8082`), connects to `ws://...:8765`.
  `index_split.html` (старый UI) — в `archive/` с Фазы 2.2.
- **Backend:** хаб + фид (Фаза 2.3, прод с 2026-07-15). Монолит `server.py`
  больше НЕ боевой — см. «Architecture» ниже.
- **Broker:** FXCM (ForexConnect Python SDK) — live tick streaming, no polling.
- **Data storage:** SQLite `market.db` (свечи, окно 2000 баров/ТФ, Фаза 1) +
  `data/*.csv` (сырые тики, вечный архив). JSON-хранилища сигнального контура —
  в `archive/old_data/` (Фаза 2.2).
- **Python:** хаб на **3.10**, фид и фронт на **3.7** (forexconnect требует 3.7).

## Architecture — data flow (Фаза 2.3)

Монолит разобран на два процесса + внутреннюю шину. Причина разделения:
forexconnect требует Python 3.7, а SDK Lighter (Фаза 3) — 3.8+, в одном процессе
они не уживаются. Хаб к брокеру не ходит — тики приходят через шину.

```
FXCM Broker ──(push)──> feeds/fxcm_feed.py (py3.7)
                          ├─ tick_writer() ──> data/*.csv (архив тиков)
                          └─ шина core/bus.py (WS 127.0.0.1:8766)
                                     │
                                     ▼
                          hub.py (py3.10)
                          ├─ CandleBuilder ──> свечи всех ТФ
                          ├─ RangeBarBuilder ──> рэндж-бары (лениво, из тиков)
                          ├─ db_writer() ──> market.db (закрытые свечи)
                          └─ WebSocket ──(push)──> Browser (index.html, :8765)
```

Key: `hub.py:on_bus_message()` — центральный диспетчер. Тик из шины →
`_handle_tick` → нарезка свечей (`core/candles`) + рэндж-баров
(`core/range_bars`) → очередь в SQLite → broadcast живой свечи браузеру.

**Сигнальный контур ВЫКЛЮЧЕН (Фаза 2.1):** движки `structure_engine` /
`signal_engine` / `signal_tracker` / `pattern_memory` не исполняются и не
портированы в хаб. В монолите флаг был `SIGNALS_ENABLED = False`; в хабе их
просто нет. Данные — в `archive/old_data/`. Возврат — только при явной задаче.

## Python files

### Боевые (хаб + фид)

- [hub.py](hub.py) — приёмник шины (py3.10). Нарезка свечей и рэндж-баров,
  запись в SQLite, WebSocket для браузера (:8765), алерты, брифинг. Не ходит к
  брокеру. Конфиг — `retention.json` (боевая `market.db`, порт 8765, ТФ, рынки).
- [feeds/fxcm_feed.py](feeds/fxcm_feed.py) — стример FXCM (py3.7). Сессия
  ForexConnect, тики и история → шина; сырые тики → `data/*.csv`.
- [feeds/lighter_feed.py](feeds/lighter_feed.py) — стример Lighter (py3.10,
  Фаза 3). WS `trade/{market_id}`, бэкфил REST `/api/v1/candles`, тики в CSV
  с кольцом 14 дней (`tick_retention_days`). Ключи НЕ нужны — данные публичны.
- [feeds/lighter_raw.py](feeds/lighter_raw.py) — нормализация сделки и
  дедупликация. В `feeds/`, а НЕ в `core/`: `core` обязан парситься на py3.7.
- [core/bus.py](core/bus.py) — внутренняя шина фид→хаб (WS 127.0.0.1:8766),
  контракт сообщений (tick / candles / instruments). Тик несёт `size`/`side`
  — у FXCM они None, и свеча остаётся чистым OHLC.
- [core/candles.py](core/candles.py) — `CandleBuilder` (нарезка тиков по ТФ,
  учёт сетки провайдера H4/D1 через offset) + `aggregate_higher_tf`. Объём и
  дельта копятся только когда провайдер их даёт.
- [core/range_bars.py](core/range_bars.py) — `RangeBarBuilder` (рэндж-бары из
  тиков, гэпы честные) + `backfill_tail` (быстрый бэкфил хвоста из архива).
  Единица — ПОИНТЫ (пипетты), как TradingView. См. «Range bars» ниже.
- [core/volume_profile.py](core/volume_profile.py) — профиль объёма
  (Фаза 3): логарифмическая сетка 0.05%, POC, область стоимости 70%.
  Копится только из тиков, хранится вечно.
- [core/large_trades.py](core/large_trades.py) — лента крупных сделок
  (Фаза 3): порог = 95-й процентиль за скользящий час, гистограмма вместо
  сортировки. Не хранится — живой поток.
- [core/db.py](core/db.py) — SQLite: `candles` (PK provider/symbol/tf/time,
  + `vol_base`/`vol_quote`/`delta`) + `instruments` + `volume_profile`,
  upsert / чтение / подрезка окна.
- [market_engine.py](market_engine.py) — `MarketEngine`: velocity, pressure,
  micro-trend, HTF trend, volatility. Использовался монолитом; в хаб НЕ
  портирован (analysis-панель отключена вместе с сигналами).

### Отключённый сигнальный контур (не исполняется с Фазы 2.1)

Описания сохранены на случай возврата. `structure_engine.py`, `signal_engine.py`
(scoring, Asia∩TIGHT-фильтр, WR 60.1% лог / 63.8% форвард, CSCV PBO=0.009),
`signal_tracker.py`, `pattern_memory.py`.

### Монолит (архивный, не боевой)

- [server.py](server.py) — старый единый процесс (FXCM + нарезка + WS в одном).
  Сервис `chart` **disabled/inactive** с 2026-07-15. Оставлен для истории/отката.

## JavaScript files (frontend)

- [index.html](index.html) — боевой UI. LightweightCharts v5 (CDN). Индикаторы
  ([js/indicators.js](js/indicators.js): SMA/EMA/Bollinger/RSI/MACD +
  `IndicatorManager`), рэндж-бары, тип отрисовки свечи↔бары (тумблер в
  контекстном меню), тёмная тема (CSS-переменные + `data-theme`), иконочный
  тулбар. Адрес хаба — `WS_URL` (переопределяется `?ws=` для отладки).
- [js/time-mapper.js](js/time-mapper.js) — `TimeMapper`, unix-время → X.
- [js/drawing-engine.js](js/drawing-engine.js) — `DrawingEngine`, canvas-оверлей
  для линий/прямоугольников, hit-test, клип по ценовой пане.
- [js/drawing-controller.js](js/drawing-controller.js) — `DrawingController`,
  состояние активного инструмента.
- [js/storage.js](js/storage.js) — localStorage: layout, цвета, рисунки, алерты.
- [js/ui-tools.js](js/ui-tools.js) — подсветка активного инструмента в тулбаре.
- [js/context-menu.js](js/context-menu.js) — правый клик: цвета свечей, тип
  графика, тема, свойства линий/прямоугольников, алерты.

## Range bars

Рэндж-бары строятся в хабе **лениво** по `set_tf` с `tf="R:<N>"`, где **N —
поинты (пипетты, минимальный тик)**, как в TradingView (их «10R» = наш R10;
пипс EUR/USD = 10 поинтов). Только из тиков (из свечей внутрибарный путь не
восстановить). Кэш по (provider,symbol,points), в БД НЕ персистятся. Бэкфил —
`backfill_tail` (хвост архива). Гэпы честные: тик со скачком > R закрывает бар
и оставляет разрыв, без раздутых баров. Строятся по **mid** (не bid, как TV).

## Data files (never delete)

- `market.db` — SQLite: свечи, окно 2000 баров/(provider,symbol,tf). Боевой файл.
- `data/*.csv` — тики (файл на символ/день, напр. `EURUSD_20260525.csv`).
  Вечный архив: из тиков M1 восстановим точно, из БД — уже нет.
- `vel_log/velocity_*.csv` — velocity-логи (наследие монолита).
- `briefing.json` — брифинг, пишет `pre_session_brief.py` (крон), читает и шлёт
  на фронт **хаб** (`briefing_watcher`).

Заархивировано (`archive/old_data/`, **не удалять**): `market_memory.json`,
`signal_log.json`, `signal_stats.json`, `session_bias.json`,
`briefing_context.json`, `History/`.

## Брифинг

`pre_session_brief.py` — крон 3×/сутки (04/12/17 локального = UTC+5), пред-Азия /
пред-Лондон / пред-NY. RSS + `data_loaders/news_calendar.csv` + техника из
`market.db` → DeepSeek → `briefing.json`. Хаб поллит файл и шлёт на фронт.
Требует `DEEPSEEK_API_KEY` в `.env`. `pre_asia_brief.py` — старая версия, в кроне
нет. Известное: RSS-фиды наполовину мертвы (Reuters закрыл публичные RSS).

## Broker connection

- FXCM demo, пакет `forexconnect`. FXCM_* креды — в `.env`
  (`EnvironmentFile` фида), НЕ хардкод.
- Symbols FXCM: AUD/USD, EUR/USD, USD/CAD, USD/JPY.
- Symbols Lighter (Фаза 3, крипта 24/7): BTC, ETH, HYPE, SOL, WTI, LIT, ZEC,
  XAU, BNB, SPCX, MU, XAG. Фронт шлёт их с префиксом (`lighter:BTC`) — у
  Lighter ЕСТЬ свои USDJPY/USDCAD/AUDUSD, по голому тикеру не различить.
- Timeframes: S5, S10, S15, S30, M1, M3, M5, M15, H1, H4, D1 (`tf_seconds` в
  `retention.json`). H1/H4/D1 грузятся у брокера напрямую (несут его сетку).

## How to run

**Четыре** systemd-сервиса (autostart on boot, `Restart=always`). Все смотрят
в это дерево (`/root/projects/terminal`) с 2026-07-18.

```bash
# Хаб (шина → свечи → SQLite → браузер WS :8765) — НЕ рестартить без разрешения
systemctl {start|stop|restart|status} chart-hub

# Фид FXCM (тики → шина :8766) — НЕ рестартить без разрешения
systemctl {start|stop|restart|status} chart-feed

# Фид Lighter (сделки крипты → шина :8766) — с 2026-07-21, py3.10, ключи не нужны
systemctl {start|stop|restart|status} chart-feed-lighter

# Фронт (HTTP :8082, отдаёт index.html)
systemctl {start|stop|restart|status} chart-frontend
```

Юниты: `/etc/systemd/system/chart-{hub,feed,feed-lighter,frontend}.service`.
Копии — в `deploy/`. Порядок: хаб первым (фиды зависят от его шины).

URL: `http://<server-ip>:8082/index.html`. Порт **8080** занят `code-server`,
не фронтом; фронт — **8082**.

## Rules

- **NEVER restart or kill `chart-hub` / `chart-feed` / `chart-feed-lighter`
  without explicit instruction.** Рестарт хаба безопасен для истории (SQLite
  переживает), но рестарт фида в рабочие часы форекса теряет тики за паузу.
  Рестарт вне сессии (выходные) — без потерь. Крипта торгуется 24/7, поэтому
  у `chart-feed-lighter` «безопасного окна» нет вообще.
- Правки живых файлов — согласовывать с владельцем.
- **Конец сессии — `git push` всех затронутых веток, включая master.**
  Репозиторий — единственный канал синхронизации с ассистентом в вебе:
  он читает код и LOG.md с GitHub и планирует по ним. Незапушенный коммит
  для планирования не существует, а при потере VPS исчезает совсем.
  Отдельно следить за master: фиксы, вынесенные из рабочей ветки туда,
  легко забыть — они не попадают в пуш ветки.
- **Делегировать подходящее в `cheap-llm` (`use_pro=true`).** Общая политика —
  в `~/.claude/CLAUDE.md`; здесь окупается конкретно: чтение и пересказ длинных
  логов (`journalctl`, `soak/hub2.log`), выжимка фактов из документации API
  Lighter, разбор больших дампов `data/*.csv`, карта вхождений символа по
  `index.html` (3.5 тыс. строк). НЕ делегировать: диагностику багов, правки
  живого кода, решения по архитектуре — там нужен контекст сессии, которого
  у DeepSeek нет. Точечная правка в файле, уже открытом в контексте, дешевле
  своими руками, чем описанием задачи вовне.
- Docstrings ко всем Python-функциям (summary, Args, Returns).
- Комментарии: русский или английский, последовательно.
- Синтаксис `core/` и фида — совместимый с **Python 3.7** (хаб бежит на 3.10, но
  тесты гоняются на обоих). pytest нет — тест-файлы запускаются напрямую
  (`python3.7 tests/<file>.py` / `python3.10 ...`, версия — в шапке файла).

## Known issues

- Фронт ломается через `file://` в Chrome/Opera — только по HTTP (сервис
  `chart-frontend`, :8082).
- Python 3.7 `.pyc` в `__pycache__/` — не апгрейдить Python без проверки
  совместимости `forexconnect`.
- Порт 8080 — `code-server`, не фронт (фронт на **8082**).

## In-progress: intrade.bar trading bot

См. [intrade_bot_plan.md](intrade_bot_plan.md). **Status:** Step 1 — ждём cURL
запроса открытия сделки с intrade.bar (Ivan через DevTools F12 → Network).
Бот слушает сигналы нашего WS (Asia∩TIGHT) и открывает бинарные опционы на
intrade.bar по HTTP (без официального API). Зависит от возврата сигналов.

## Downtime window — ОТМЕНЕНО (2026-07-14)

Ежедневного окна простоя нет: крон-остановка `chart` снята (история свечей в
SQLite, каждая остановка — дыра в секундных ТФ). Правки живых файлов
согласовывать по времени с владельцем. Крон — в локальном времени (UTC+5).
