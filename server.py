import asyncio
import json
import time
import threading
from datetime import datetime, timedelta
from forexconnect import ForexConnect, Common
import websockets
import numpy as np
import os
import csv
from core.db import init_db, upsert_candle, upsert_candles_batch, trim_window, \
    KEEP_BARS, load_history as db_load_history
from core.candles import aggregate_higher_tf
from market_engine import MarketEngine
from structure_engine import StructureEngine, detect_event
from signal_engine import evaluate_signal, format_signal, get_hour_utc, minutes_in_hour, get_session
import queue
from collections import deque

# Load .env file if present
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

LOGIN = os.getenv("FXCM_LOGIN")
PASSWORD = os.getenv("FXCM_PASSWORD")
URL = os.getenv("FXCM_URL", "http://www.fxcorporate.com/Hosts.jsp")
CONNECTION = os.getenv("FXCM_CONNECTION", "Demo")


PORT = 8765
HISTORY_COUNT = 10000


SYMBOLS = [
    "AUD/USD",
    "EUR/USD",
    "USD/CAD",
    "USD/JPY"
]


TF_SECONDS = {
    "S5": 5,
    "S10": 10,
    "S15": 15,
    "S30": 30,
    "M1": 60,
    "M3": 180,
    "M5": 300,
    "M15": 900,
    "H1": 3600,
    "H4": 14400,
    "D1": 86400
}

# Таймфреймы, которые грузятся напрямую у брокера (а не агрегацией из M1).
# D1: из 10000 M1-баров (~7 дней) получилось бы всего ~7 дневных свечей.
# H4: из тех же ~7 дней вышло бы лишь ~41 бар — тоже слишком коротко.
# H1: из тех же ~7 дней вышло бы ~167 баров, а индикаторам (js/indicators.js,
#     _tailLen) нужно 300–400 хвоста. 90 дней дают ~2160 баров.
# Ключ = наше имя TF, значение = (строка TF ForexConnect, глубина истории в днях).
DIRECT_LOAD_TF = {
    "H1": ("H1", 90),
    "H4": ("H4", 90),
    "D1": ("D1", 400),
}


alerts           = {}
alert_id_counter = 1
symbols_state    = {}
clients          = {}
loop_ref         = None


market_engines    = {}
market_structures = {}


# ── Сигнальный контур (Фаза 2.1) ───────────────────────────────────────────
# Выключен флагом. Код движков НЕ удалён: вернуть контур = SIGNALS_ENABLED=True.
#
# Импорт pattern_memory / signal_tracker тоже под флагом — не из чистоплюйства:
# оба читают свои JSON на уровне модуля (market_memory.json ≈ 73 МБ,
# signal_log.json ≈ 26 МБ), т.е. просто импорт стоит ~100 МБ RSS, а
# pattern_memory вдобавок вешает atexit-сохранение — ещё одна полная
# перезапись файла при каждой остановке сервера.
SIGNALS_ENABLED = False

if SIGNALS_ENABLED:
    from pattern_memory import store_pattern, resolve_patterns
    from signal_tracker import (
        store_signal,
        resolve_signals as tracker_resolve,
        print_stats as tracker_print_stats,
    )

# Cooldown сигналов — не более 1 сигнала в 30 сек на пару
SIGNAL_COOLDOWN   = 30
last_signal_time  = {}

# ── Брифинг (pre_session_brief.py → briefing.json) ──
BRIEFING_FILE = "briefing.json"
_briefing_cache = None       # последний прочитанный briefing.json
_briefing_mtime = 0          # mtime при последнем чтении
_briefing_lock = threading.Lock()

# ── ФОРВАРД-ТЕСТ: фильтр «последнего сигнала блока» через N-секундную тишину ──
# Гипотеза (см. memory: signal-block-position-test): из блока сигналов у края
# (post-cooldown, идут с паузой >=30с) реальный эдж несёт ПОСЛЕДНЕЕ касание
# перед разворотом. Приближаем «последний» так: сигнал не отправляется сразу,
# а буферизуется; если за BLOCK_FILTER_N секунд не пришёл новый post-cooldown
# сигнал по той же паре — буферизованный считается последним и отправляется
# СЕЙЧАС (вход с задержкой ~N сек, по текущей цене). Иначе старый буфер
# вытесняется новым (блок продолжился).
#   BLOCK_FILTER_N = 0  → фильтр ВЫКЛЮЧЕН, поведение ровно как раньше.
#   > 0 (напр. 60)      → включён; кулдаун 30с при этом НЕ трогается.
# Отправленные с задержкой сигналы помечаются тегом в reason — block_watcher.py
# меряет по ним форвард-винрейт (baseline-механика как у pos_watcher).
BLOCK_FILTER_N        = 0
_pending_block_signal = {}   # symbol → {"price","ts","signal"} ждущий подтверждения тишины


def _fire_signal(symbol, price, ts, signal, delay=0):
    """Единая точка выхода боевого сигнала: терминал + реплей-буфер + трекер.

    Используется и для немедленной отправки, и для отложенной block-фильтром.

    Args:
        symbol: пара.
        price:  цена входа на момент отправки.
        ts:     время входа (для отложенного — момент подтверждения тишины).
        signal: SignalResult из signal_engine.evaluate_signal.
        delay:  если >0 — сигнал отправлен block-фильтром с задержкой delay сек,
                в reason добавляется тег "⏳block_filter N=…" для block_watcher.py.
    Returns:
        None.
    """
    signal_data = format_signal(symbol, price, ts, signal)
    send_signal(symbol, signal_data)
    recent_signals.append(_signal_entry(ts, symbol, signal_data))

    reason = list(signal.reason or [])
    if delay > 0:
        reason.append(f"⏳block_filter N={delay}")

    store_signal(
        symbol    = symbol,
        direction = signal.direction,
        confidence= signal.confidence,
        winrate   = signal.winrate,
        samples   = signal.samples,
        price     = price,
        ts        = ts,
        reason    = reason
    )

# Реплей сигналов: кольцевой буфер последних боевых сигналов в памяти.
# Отдаётся новому клиенту по запросу get_signals, чтобы стрелки сигналов
# переживали перезагрузку страницы в течение сессии.
RECENT_SIGNALS_MAX = 1500
recent_signals     = deque(maxlen=RECENT_SIGNALS_MAX)

stream_listener   = None




DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = "market.db"

# ── SQLite writer queue ──
# process_tick enqueues closed candles here; db_writer thread persists them.
# Max 5000 items — at ~100 closed candles/min this is ~50 min of buffer.
db_queue = queue.Queue(maxsize=5000)
DB_TRIM_EVERY = 200  # trim retention window every N writes (not every candle)


tick_queue = queue.Queue(maxsize=200000)


# ================= TICK FILTER =================

last_price     = {}


def should_emit(symbol, price, ts):

    if symbol not in last_price:
        last_price[symbol] = price
        return True

    if price != last_price[symbol]:
        last_price[symbol] = price
        return True

    return False

# ================= SYMBOL INIT =================


def init_symbol(symbol):
    symbols_state[symbol] = {
        "tf_data":        {tf: [] for tf in TF_SECONDS},
        "current_bucket": {tf: None for tf in TF_SECONDS},
        "current_candle": {tf: None for tf in TF_SECONDS}
    }
    market_engines[symbol]    = MarketEngine()
    market_structures[symbol] = StructureEngine()
    last_signal_time[symbol]  = 0
    _pending_block_signal[symbol] = None




# ================= TICK STORAGE =================


def tick_writer():

    # Словарь: (symbol, date_str) → {file, writer}
    open_files = {}

    def get_writer(symbol, date_str):
        key = (symbol, date_str)
        if key not in open_files:
            # Закрываем старые файлы этого символа если дата сменилась
            old_keys = [k for k in open_files if k[0] == symbol and k[1] != date_str]
            for k in old_keys:
                open_files[k]["file"].close()
                del open_files[k]

            sym_clean = symbol.replace("/", "")
            filename  = os.path.join(DATA_DIR, f"{sym_clean}_{date_str}.csv")
            new_file  = not os.path.exists(filename)
            f         = open(filename, "a", newline="", buffering=1)
            w         = csv.writer(f)
            if new_file:
                w.writerow(["timestamp_utc", "datetime_utc", "symbol", "bid", "ask", "mid"])
            open_files[key] = {"file": f, "writer": w}

        return open_files[key]["writer"]

    while True:
        tick     = tick_queue.get()
        dt       = tick["dt"]
        date_str = dt.strftime("%Y%m%d")
        symbol   = tick["symbol"]

        writer = get_writer(symbol, date_str)

        formatted_dt = dt.strftime("%d/%m/%Y %H:%M:%S.%f")[:-3]

        writer.writerow([
            tick["ts"],
            formatted_dt,
            symbol,
            f"{tick['bid']:.5f}",
            f"{tick['ask']:.5f}",
            f"{tick['mid']:.5f}"
        ])


# ================= TIME =================


def to_timestamp(dt):
    if isinstance(dt, datetime):
        return int(dt.timestamp())
    if isinstance(dt, np.datetime64):
        return int(dt.astype('datetime64[s]').astype(int))
    return int(dt)




# ================= HISTORY =================


def load_history():
    """Restore candle history from DB, backfill gap from FXCM.

    - First run (empty DB):        full 30-day FXCM load.
    - Restart (DB has candles):    restore from DB, request only the gap
                                   (last_db_time → now) from FXCM.
    - Higher TFs are always rebuilt from M1 after merging.
    """

    print("Загрузка истории...")

    # Ensure DB file + schema exist (handles first-run: no .db file at all).
    # Close immediately — each component opens its own connection in-thread.
    init_db(DB_PATH).close()

    # ── Step 1: restore M1 + direct-load TFs from SQLite ──
    for sym in SYMBOLS:
        if _load_from_db(sym):
            m1_count = len(symbols_state[sym]["tf_data"]["M1"])
            print(f"History [{sym}]: restored from SQLite (M1: {m1_count} bars)")
        else:
            print(f"History [{sym}]: no data in SQLite")

    # ── Step 2: FXCM backfill (gap for restored, full for empty) ──
    with ForexConnect() as fx:

        fx.login(LOGIN, PASSWORD, URL, CONNECTION)

        now = datetime.utcnow()
        default_start = now - timedelta(days=30)

        for symbol in SYMBOLS:

            state   = symbols_state[symbol]
            tf_data = state["tf_data"]

            # Snapshot which M1 times came from DB (before gap backfill).
            # Used later to rebuild higher TFs ONLY from the gap portion.
            db_m1_times = {c["time"] for c in tf_data.get("M1", [])}

            # Determine start time for this symbol
            if tf_data.get("M1"):
                last_db = max(c["time"] for c in tf_data["M1"])
                start   = datetime.utcfromtimestamp(last_db)
                tag     = f"gap [{start.strftime('%H:%M')} → now]"
            else:
                start = default_start
                tag   = "full 30d"

            # Only call FXCM if gap is > 1 M1 candle (60s)
            gap_sec = (now - start).total_seconds()
            if gap_sec <= 60:
                print(f"History [{symbol}]: gap < 1 min, skipped FXCM")
            else:
                print(f"History [{symbol}]: FXCM {tag}")
                raw = fx.get_history(symbol, "m1", start, now)
                data = raw[-HISTORY_COUNT:]

                # Append gap candles to existing M1 (or populate from scratch)
                for row in data:
                    ts = to_timestamp(row["Date"])
                    tf_data.setdefault("M1", []).append({
                        "time":  ts,
                        "open":  float(row["BidOpen"]),
                        "high":  float(row["BidHigh"]),
                        "low":   float(row["BidLow"]),
                        "close": float(row["BidClose"])
                    })

                # Deduplicate by time (DB + gap overlap at boundary)
                if tf_data.get("M1"):
                    seen = set()
                    deduped = []
                    for c in tf_data["M1"]:
                        if c["time"] not in seen:
                            seen.add(c["time"])
                            deduped.append(c)
                    deduped.sort(key=lambda x: x["time"])
                    tf_data["M1"] = deduped

                # Last M1 candle is incomplete — live ticks will extend it
                if tf_data["M1"]:
                    tf_data["M1"].pop()

            # ── Higher TFs: rebuild from gap M1 only, merge into DB data ──
            if db_m1_times and gap_sec > 60:
                # DB had data — only aggregate the NEW (gap) M1 candles
                gap_m1 = [c for c in tf_data.get("M1", [])
                          if c["time"] not in db_m1_times]
                if gap_m1:
                    gap_start = min(c["time"] for c in gap_m1)
                    for tf, sec in TF_SECONDS.items():
                        if tf == "M1" or tf in DIRECT_LOAD_TF:
                            continue
                        if sec < 60:
                            # Seconds TFs (S5–S30) derive from ticks, not M1
                            continue
                        # Align start to bucket boundary so the previous
                        # (partial) bucket is covered fully and the correct
                        # DB candle is NOT overwritten by an incomplete one.
                        bucket_start = (gap_start // sec) * sec
                        boundary = [c for c in tf_data["M1"]
                                    if c["time"] >= bucket_start]
                        if not boundary:
                            continue
                        gap_higher = aggregate_higher_tf(boundary, sec)
                        # Merge into DB-restored data (dedup by time)
                        existing = {c["time"]: c for c in tf_data.get(tf, [])}
                        for c in gap_higher:
                            existing[c["time"]] = c
                        tf_data[tf] = sorted(existing.values(),
                                             key=lambda x: x["time"])
            elif not db_m1_times:
                # No DB data — full rebuild from M1 (first run)
                build_higher_history(symbol)

            # ── Direct-load TFs (H4, D1) ──
            for tf, (fx_tf, days) in DIRECT_LOAD_TF.items():
                existing = tf_data.get(tf, [])
                if existing and isinstance(existing, list) and existing:
                    last_tf = max(c["time"] for c in existing)
                    start_tf = datetime.utcfromtimestamp(
                        max(last_tf, to_timestamp(default_start)))
                else:
                    start_tf = now - timedelta(days=days)

                gap_tf = (now - start_tf).total_seconds()
                if gap_tf <= 3600:   # < 1h gap for H4/D1 is trivial
                    continue

                hist = fx.get_history(symbol, fx_tf, start_tf, now)
                if not tf_data.get(tf):
                    tf_data[tf] = []

                existing_times = {c["time"] for c in tf_data[tf]}
                for row in hist:
                    ts_c = to_timestamp(row["Date"])
                    if ts_c not in existing_times:
                        tf_data[tf].append({
                            "time":  ts_c,
                            "open":  float(row["BidOpen"]),
                            "high":  float(row["BidHigh"]),
                            "low":   float(row["BidLow"]),
                            "close": float(row["BidClose"])
                        })
                        existing_times.add(ts_c)

                if tf_data[tf]:
                    tf_data[tf].sort(key=lambda x: x["time"])
                    tf_data[tf].pop()

            _persist_all(symbol)

    print("История загружена.")




def build_higher_history(symbol):


    state   = symbols_state[symbol]
    tf_data = state["tf_data"]


    def aggregate(source, sec):
        result = []
        bucket = None
        candle = None
        for c in source:
            b = (c["time"] // sec) * sec
            if b != bucket:
                if candle:
                    result.append(candle)
                bucket = b
                candle = {
                    "time":  b,
                    "open":  c["open"],
                    "high":  c["high"],
                    "low":   c["low"],
                    "close": c["close"]
                }
            else:
                candle["high"]  = max(candle["high"], c["high"])
                candle["low"]   = min(candle["low"],  c["low"])
                candle["close"] = c["close"]
        if candle:
            result.append(candle)
        return result


    for tf, sec in TF_SECONDS.items():
        if sec >= 60 and tf != "M1" and tf not in DIRECT_LOAD_TF:
            tf_data[tf] = aggregate(tf_data["M1"], sec)
            if tf_data[tf]:
                tf_data[tf].pop()
            tf_data[tf].sort(key=lambda x: x["time"])




# ================= SQLite PERSIST =================


def db_writer():
    """Daemon thread: persists closed candles from db_queue to SQLite.

    Creates its OWN sqlite3.Connection inside this thread — avoids
    ProgrammingError from cross-thread access (check_same_thread=True).
    Each upsert_candle commits on its own (`with conn:`), so a hard kill
    of the process loses nothing already dequeued.

    Trims the retention window every DB_TRIM_EVERY writes rather than on
    every candle — the trim is a SELECT+DELETE per (symbol, tf).
    """
    conn = init_db(DB_PATH)
    count = 0

    while True:
        symbol, tf, candle = db_queue.get()

        try:
            upsert_candle(conn, "fxcm", symbol, tf, candle)
            count += 1

            if count % DB_TRIM_EVERY == 0:
                for sym in SYMBOLS:
                    for tf_name in TF_SECONDS:
                        trim_window(conn, "fxcm", sym, tf_name, KEEP_BARS)
        except Exception as e:
            print(f"[db_writer] ERROR: {e} — candle dropped, continuing")


def _enqueue_closed(symbol, tf, candle):
    """Enqueue a closed candle for persistence. Never blocks.

    If the queue is full (db_writer stalled or crashed), the candle is
    dropped: the FXCM callback thread must never stall on the DB.

    Args:
        symbol: Trading pair.
        tf:     Timeframe string.
        candle: Closed candle dict.
    """
    try:
        db_queue.put_nowait((symbol, tf, candle))
    except queue.Full:
        print(f"[db_queue] FULL — dropped {symbol} {tf} @ {candle['time']}")


def _load_from_db(symbol):
    """Restore ALL timeframes for one symbol from SQLite.

    Each TF has its own 2000-bar sliding window in the DB — restoring
    all of them preserves the full indicator tail (EMA/RSI/MACD need
    300–400 bars).  Higher TFs are NOT rebuilt from M1 on restore;
    only the gap (new FXCM data) is aggregated and merged.

    Args:
        symbol: Trading pair.

    Returns:
        True if at least M1 data was found and restored, False otherwise.
    """
    # init_db is idempotent (CREATE TABLE IF NOT EXISTS) — safe to call
    # even if the schema already exists.  No implicit dependency on a
    # prior init_db call elsewhere.
    conn = init_db(DB_PATH)
    tf_data = symbols_state[symbol]["tf_data"]

    m1 = db_load_history(conn, "fxcm", symbol, "M1")
    if not m1:
        conn.close()
        return False

    # Restore every TF that has data in the DB (S5…S30, M1, M3…H1, H4, D1)
    for tf in TF_SECONDS:
        candles = db_load_history(conn, "fxcm", symbol, tf)
        if candles:
            tf_data[tf] = candles

    conn.close()
    return True


def _persist_all(symbol):
    """Bulk-persist all in-memory candles for one symbol after FXCM load.

    Uses a dedicated temporary connection — runs in the main thread during
    startup, before streaming begins, so there is no concurrent access.
    """
    conn = init_db(DB_PATH)
    state = symbols_state[symbol]
    total = 0
    for tf, candles in state["tf_data"].items():
        if candles:
            upsert_candles_batch(conn, "fxcm", symbol, tf, candles)
            total += len(candles)
            trim_window(conn, "fxcm", symbol, tf, KEEP_BARS)
    conn.commit()
    conn.close()
    print(f"  [persist] {symbol}: {total} candles written")



# ================= LIVE =================


def process_tick(symbol, price, ts):


    state      = symbols_state[symbol]
    prev_price = state.get("last_price") or price


    tf_data        = state["tf_data"]
    current_bucket = state["current_bucket"]
    current_candle = state["current_candle"]


    for tf, sec in TF_SECONDS.items():


        bucket = (ts // sec) * sec


        if current_bucket[tf] is None:
            current_bucket[tf] = bucket
            current_candle[tf] = {
                "time": bucket, "open": price,
                "high": price,  "low":  price, "close": price
            }
            continue


        if bucket != current_bucket[tf]:
            prev_close = None
            if current_candle[tf]:
                prev_close = current_candle[tf]["close"]
                tf_data[tf].append(current_candle[tf])
                _enqueue_closed(symbol, tf, current_candle[tf])


            current_bucket[tf] = bucket


            if sec >= 60 and prev_close is not None:
                current_candle[tf] = {
                    "time": bucket,
                    "open": prev_close,
                    "high": max(prev_close, price),
                    "low":  min(prev_close, price),
                    "close": price
                }
            else:
                current_candle[tf] = {
                    "time": bucket, "open": price,
                    "high": price,  "low":  price, "close": price
                }
            continue


        if current_candle[tf] is None:
            current_candle[tf] = {
                "time": bucket, "open": price,
                "high": price,  "low":  price, "close": price
            }
            continue


        c          = current_candle[tf]
        c["high"]  = max(c["high"], price)
        c["low"]   = min(c["low"],  price)
        c["close"] = price


    push_updates(symbol)
    check_alerts(symbol, prev_price, price)
    state["last_price"] = price


    # ---- MarketEngine ----
    engine   = market_engines[symbol]
    analysis = engine.update_tick(
        symbol=symbol, price=price, ts=ts,
        state=state, tf_data=state["tf_data"]
    )


    if not analysis:
        return
    


    # ===== VELOCITY STATS =====
    v = analysis.get("velocity", 0)
    av = abs(v)


    if not hasattr(process_tick, "slow"):
        process_tick.slow = 0
        process_tick.medium = 0
        process_tick.fast = 0
        process_tick.total = 0


    process_tick.total += 1


    if av < 0.00005:
        process_tick.slow += 1
    elif av < 0.00015:
        process_tick.medium += 1
    else:
        process_tick.fast += 1


    # печать каждые 100 тиков
    if process_tick.total % 100 == 0:
        total = process_tick.total
        print(
            f"[VEL] total={total} | "
            f"slow={process_tick.slow/total*100:.1f}% | "
            f"medium={process_tick.medium/total*100:.1f}% | "
            f"fast={process_tick.fast/total*100:.1f}%"
        )


    if SIGNALS_ENABLED:
        # ---- Structure ----
        structure = market_structures[symbol].update(price, ts)


        # ---- block-фильтр: если тишина >= N, буферизованный сигнал признаётся
        #      «последним в блоке» и отправляется СЕЙЧАС (вход с задержкой ~N сек) ----
        if BLOCK_FILTER_N > 0:
            pend = _pending_block_signal.get(symbol)
            if pend and ts - pend["ts"] >= BLOCK_FILTER_N:
                _fire_signal(symbol, price, ts, pend["signal"], delay=BLOCK_FILTER_N)
                _pending_block_signal[symbol] = None


        # ---- Signal — только если цена у края диапазона ----
        near_high = structure.get("near_high", False) if structure else False
        near_low  = structure.get("near_low",  False) if structure else False


        if structure and (near_high or near_low):
            signal = evaluate_signal(symbol, price, ts, analysis, structure)


            if signal and not signal.skip_reason:
                # cooldown — не чаще раза в 30 сек на пару
                if ts - last_signal_time[symbol] >= SIGNAL_COOLDOWN:
                    last_signal_time[symbol] = ts
                    if BLOCK_FILTER_N > 0:
                        # буферизуем кандидата в «последний сигнал блока»;
                        # прежний невыстреливший буфер вытесняется (блок продолжился)
                        _pending_block_signal[symbol] = {
                            "price": price, "ts": ts, "signal": signal
                        }
                    else:
                        _fire_signal(symbol, price, ts, signal)


            # SKIP-сигналы во фронт НЕ отправляем — только боевые (Asia∩TIGHT).
            # Причина: фильтр отсекает ~90% сигналов, поток SKIP'ов бесполезен.
            # Раскомментировать ниже для отладки.
            # elif signal and signal.skip_reason:
            #     if ts - last_signal_time[symbol] >= SIGNAL_COOLDOWN:
            #         last_signal_time[symbol] = ts
            #         send_signal(symbol, format_signal(symbol, price, ts, signal))


        # ---- Трекер — закрываем истёкшие сигналы ----
        tracker_resolve(symbol, price, ts)


        # ---- Паттерны для обучения ----
        event = detect_event(structure) if structure else None
        if structure and event:
            combined = {
                **analysis,
                **structure,
                "event":  event,
                "symbol": symbol
            }
            store_pattern(symbol, combined, price, ts)


        resolve_patterns(symbol, price, ts)


    # ---- Analysis в терминал (не трогаем) ----
    send_analysis(symbol, analysis)




# ================= ALERTS =================


def check_alerts(symbol, prev_price, price):
    if symbol not in alerts:
        return
    for alert in alerts[symbol]:
        if alert["triggered"]:
            continue
        level = alert["price"]
        if (prev_price <= level <= price) or (price <= level <= prev_price):
            alert["triggered"] = True
            print(f"ALERT TRIGGERED {symbol} ID {alert['id']} PRICE {alert['price']} TICK {price}")
            send_alert(symbol, level, alert["id"])




def send_alert(symbol, price, alert_id):
    print("SEND ALERT START", symbol, price)
    for ws in list(clients.keys()):
        try:
            asyncio.run_coroutine_threadsafe(
                ws.send(json.dumps({
                    "type":   "alert",
                    "symbol": symbol,
                    "price":  price,
                    "id":     alert_id
                })),
                loop_ref
            )
        except Exception as e:
            print("WS ALERT ERROR:", e)
            clients.pop(ws, None)


def _broadcast_briefing():
    """
    Шлёт текущий брифинг ВСЕМ подключённым клиентам (без фильтра по символу).
    Клиент сам фильтрует по currentSymbol.
    """
    global _briefing_cache
    if _briefing_cache is None:
        return
    msg = json.dumps({"type": "briefing", "data": _briefing_cache})
    for ws in list(clients.keys()):
        try:
            asyncio.run_coroutine_threadsafe(ws.send(msg), loop_ref)
        except Exception:
            clients.pop(ws, None)


def briefing_watcher():
    """
    Поток: поллит briefing.json mtime каждые 5 секунд.
    При изменении — перечитывает и broadcast всем клиентам.
    """
    global _briefing_cache, _briefing_mtime
    while True:
        try:
            if os.path.exists(BRIEFING_FILE):
                mtime = os.path.getmtime(BRIEFING_FILE)
                with _briefing_lock:
                    if mtime > _briefing_mtime:
                        with open(BRIEFING_FILE, encoding="utf-8") as f:
                            _briefing_cache = json.load(f)
                        _briefing_mtime = mtime
                        print(f"[briefing] reloaded (mtime={mtime}, "
                              f"session={_briefing_cache.get('meta', {}).get('session', '?')})", flush=True)
                        _broadcast_briefing()
        except Exception as e:
            print(f"[briefing] watch error: {e}", flush=True)
        time.sleep(5)




def send_analysis(symbol, data):
    for ws, info in list(clients.items()):
        if info["symbol"] != symbol:
            continue
        try:
            asyncio.run_coroutine_threadsafe(
                ws.send(json.dumps({
                    "type":   "analysis",
                    "symbol": symbol,
                    **data
                })),
                loop_ref
            )
        except:
            clients.pop(ws, None)




def _signal_entry(ts, symbol, signal_data):
    """
    Построить запись реплея, зеркалящую живое сообщение 'signal'.

    Args:
        ts: unix timestamp сигнала.
        symbol: торговая пара.
        signal_data: dict из format_signal (боевой, не SKIP).

    Returns:
        dict: {"time", "symbol", "direction", "data"} для буфера recent_signals.
    """
    return {
        "time":      int(ts),
        "symbol":    symbol,
        "direction": signal_data["direction"],
        "data":      signal_data,
    }


def seed_recent_signals():
    """
    Посеять буфер recent_signals последними сигналами из signal_log.json.

    Берёт сигналы за последние 24 часа и нормализует их к форме живого
    сообщения 'signal' (до-вычисляет session/hour_utc/min_in_hour из ts),
    чтобы панель сигнала на фронте рендерилась идентично живым. Best-effort:
    любые ошибки чтения/парсинга не должны мешать старту сервера.

    Args:
        None.

    Returns:
        None. Наполняет глобальный recent_signals.
    """
    try:
        with open("signal_log.json", encoding="utf-8") as f:
            log = json.load(f)
    except Exception as e:
        print("[signals] seed skipped:", e)
        return

    cutoff = time.time() - 24 * 3600
    recent = [s for s in log
              if isinstance(s.get("ts"), (int, float)) and s["ts"] >= cutoff]
    recent.sort(key=lambda s: s["ts"])

    for s in recent:
        ts = s["ts"]
        data = {
            "type":        "signal",
            "symbol":      s.get("symbol"),
            "price":       s.get("price"),
            "direction":   s.get("direction"),
            "confidence":  s.get("confidence"),
            "winrate":     s.get("winrate"),
            "display_pct": s.get("confidence"),
            "samples":     s.get("samples", 0),
            "reason":      s.get("reason", []),
            "hour_utc":    get_hour_utc(ts),
            "min_in_hour": minutes_in_hour(ts),
            "session":     get_session(ts),
        }
        recent_signals.append(_signal_entry(ts, s.get("symbol"), data))

    print(f"[signals] seeded {len(recent_signals)} recent signals (<=24h)")


def send_signal(symbol, data):
    for ws, info in list(clients.items()):
        if info["symbol"] != symbol:
            continue
        try:
            asyncio.run_coroutine_threadsafe(
                ws.send(json.dumps(data)),
                loop_ref
            )
        except:
            clients.pop(ws, None)




def push_updates(symbol):
    if not loop_ref:
        return
    state = symbols_state[symbol]
    for ws, info in list(clients.items()):
        if info["symbol"] != symbol:
            continue
        tf         = info["tf"]
        request_id = info["requestId"]
        candle     = state["current_candle"].get(tf)
        if not candle:
            continue
        try:
            asyncio.run_coroutine_threadsafe(
                ws.send(json.dumps({
                    "type":      "update",
                    "symbol":    symbol,
                    "tf":        tf,
                    "requestId": request_id,
                    "candle":    candle
                })),
                loop_ref
            )
        except:
            clients.pop(ws, None)




# ================= FXCM STREAM =================


def fxcm_streaming():
    """
    Настоящий push-streaming через ForexConnect table_manager.
    FXCM сам вызывает on_offer_changed при каждом изменении цены —
    никакого polling, никакого sleep, никакого Java.
    """

    def on_offer_changed(_, row_id, row_data, *args):
        try:
            symbol = row_data.instrument
            if symbol not in symbols_state:
                return

            bid = row_data.bid
            ask = row_data.ask
            if bid is None or ask is None:
                return
            if bid <= 0 or ask <= 0:
                return

            mid = round((bid + ask) / 2, 5)
            ts  = time.time()
            dt  = datetime.utcnow()

            process_tick(symbol, mid, ts)

            if should_emit(symbol, mid, ts):
                try:
                    tick_queue.put_nowait({
                        "ts":     ts,
                        "dt":     dt,
                        "symbol": symbol,
                        "bid":    bid,
                        "ask":    ask,
                        "mid":    mid
                    })
                except queue.Full:
                    pass

        except Exception as e:
            print(f"[STREAM] Ошибка обработки тика: {e}")

    while True:
        try:
            with ForexConnect() as fx:
                fx.login(LOGIN, PASSWORD, URL, CONNECTION)

                offers = fx.get_table(ForexConnect.OFFERS)
                global stream_listener
                stream_listener = Common.subscribe_table_updates(
                    offers,
                    on_change_callback=on_offer_changed
                )

                print("[STREAM] Подключён. Ожидаем тики от FXCM...")

                # Держим поток живым — все тики приходят через callback
                stop_event = threading.Event()
                stop_event.wait()

        except Exception as e:
            print(f"[STREAM] Ошибка подключения: {e}. Переподключение через 5 сек...")
            time.sleep(5)


# ================= WS =================


async def handler(ws):


    clients[ws] = {"symbol": None, "tf": None, "requestId": 0}


    try:
        async for msg in ws:
            data = json.loads(msg)


            if data["type"] == "set_tf":
                symbol     = data["symbol"]
                tf         = data["tf"]
                request_id = data.get("requestId", 0)
                clients[ws] = {"symbol": symbol, "tf": tf, "requestId": request_id}
                history = symbols_state[symbol]["tf_data"][tf]
                await ws.send(json.dumps({
                    "type":      "history",
                    "symbol":    symbol,
                    "tf":        tf,
                    "requestId": request_id,
                    "data":      history
                }))

                # Отправляем текущий брифинг новому клиенту
                with _briefing_lock:
                    if _briefing_cache is not None:
                        await ws.send(json.dumps({
                            "type": "briefing",
                            "data": _briefing_cache
                        }))


            if data["type"] == "get_signals":
                await ws.send(json.dumps({
                    "type": "signals_history",
                    "data": list(recent_signals),
                }))

            if data["type"] == "add_alert":
                global alert_id_counter
                symbol = data["symbol"]
                price  = round(float(data["price"]), 5)
                if symbol not in alerts:
                    alerts[symbol] = []
                alert = {"id": alert_id_counter, "price": price, "triggered": False}
                alerts[symbol].append(alert)
                print("ADD ALERT", symbol, price, "ID", alert_id_counter)
                await ws.send(json.dumps({
                    "type":   "alert_created",
                    "symbol": symbol,
                    "price":  price,
                    "id":     alert_id_counter
                }))
                alert_id_counter += 1


            if data["type"] == "update_alert":
                symbol   = data.get("symbol")
                alert_id = data.get("id")
                price    = data.get("price")
                if symbol is None or alert_id is None or price is None:
                    return
                price = round(float(price), 5)
                if symbol not in alerts:
                    return
                for a in alerts[symbol]:
                    if a["id"] == alert_id:
                        a["price"] = price
                        break


            if data["type"] == "remove_alert":
                symbol   = data.get("symbol")
                alert_id = data.get("id")
                if symbol is None or alert_id is None:
                    return
                alert_id = int(alert_id)
                if symbol not in alerts:
                    return
                alerts[symbol] = [a for a in alerts[symbol] if a["id"] != alert_id]
                print("REMOVE ALERT", symbol, "ID", alert_id)


    finally:
        clients.pop(ws, None)




# ================= MAIN =================


async def main():
    global loop_ref


    for s in SYMBOLS:
        init_symbol(s)


    load_history()

    if SIGNALS_ENABLED:
        seed_recent_signals()


    threading.Thread(target=tick_writer, daemon=True).start()
    threading.Thread(target=db_writer, daemon=True).start()
    threading.Thread(target=briefing_watcher, daemon=True).start()


    loop_ref = asyncio.get_running_loop()


    threading.Thread(target=fxcm_streaming, daemon=True).start()


    async with websockets.serve(handler, "0.0.0.0", PORT):
        print("Server ready")
        await asyncio.Future()




if __name__ == "__main__":
    asyncio.run(main())
