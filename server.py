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
from market_engine import MarketEngine
from structure_engine import StructureEngine, detect_event
from signal_engine import evaluate_signal, format_signal
from pattern_memory import store_pattern, resolve_patterns
from signal_tracker import store_signal, resolve_signals as tracker_resolve, print_stats as tracker_print_stats
import queue




LOGIN = "D261411120"
PASSWORD = "d4bHf"
URL = "http://www.fxcorporate.com/Hosts.jsp"
CONNECTION = "Demo"


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
    "H1": 3600
}


alerts           = {}
alert_id_counter = 1
symbols_state    = {}
clients          = {}
loop_ref         = None


market_engines    = {}
market_structures = {}


# Cooldown сигналов — не более 1 сигнала в 30 сек на пару
SIGNAL_COOLDOWN   = 30
last_signal_time  = {}

stream_listener   = None




DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)


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


    print("Загрузка истории...")


    with ForexConnect() as fx:


        fx.login(LOGIN, PASSWORD, URL, CONNECTION)


        end   = datetime.utcnow()
        start = end - timedelta(days=30)


        for symbol in SYMBOLS:


            print("History:", symbol)
            history = fx.get_history(symbol, "m1", start, end)


            state   = symbols_state[symbol]
            tf_data = state["tf_data"]
            data    = history[-HISTORY_COUNT:]


            for row in data:
                ts = to_timestamp(row["Date"])
                tf_data["M1"].append({
                    "time":  ts,
                    "open":  float(row["BidOpen"]),
                    "high":  float(row["BidHigh"]),
                    "low":   float(row["BidLow"]),
                    "close": float(row["BidClose"])
                })


            if tf_data["M1"]:
                tf_data["M1"].pop()


            build_higher_history(symbol)


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
        if sec >= 60 and tf != "M1":
            tf_data[tf] = aggregate(tf_data["M1"], sec)
            if tf_data[tf]:
                tf_data[tf].pop()
            tf_data[tf].sort(key=lambda x: x["time"])




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


    # ---- Structure ----
    structure = market_structures[symbol].update(price, ts)


    # ---- Signal — только если цена у края диапазона ----
    near_high = structure.get("near_high", False) if structure else False
    near_low  = structure.get("near_low",  False) if structure else False


    if structure and (near_high or near_low):
        signal = evaluate_signal(symbol, price, ts, analysis, structure)


        if signal and not signal.skip_reason:
            # cooldown — не чаще раза в 30 сек на пару
            if ts - last_signal_time[symbol] >= SIGNAL_COOLDOWN:
                last_signal_time[symbol] = ts
                signal_data = format_signal(symbol, price, ts, signal)
                send_signal(symbol, signal_data)


                # Трекер — записываем сигнал для отслеживания результата
                store_signal(
                    symbol    = symbol,
                    direction = signal.direction,
                    confidence= signal.confidence,
                    winrate   = signal.winrate,
                    samples   = signal.samples,
                    price     = price,
                    ts        = ts,
                    reason    = signal.reason
                )


        elif signal and signal.skip_reason:
            # SKIP сигналы тоже отправляем — терминал покажет причину
            if ts - last_signal_time[symbol] >= SIGNAL_COOLDOWN:
                last_signal_time[symbol] = ts
                send_signal(symbol, format_signal(symbol, price, ts, signal))


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


    threading.Thread(target=tick_writer, daemon=True).start()


    loop_ref = asyncio.get_running_loop()


    threading.Thread(target=fxcm_streaming, daemon=True).start()


    async with websockets.serve(handler, "0.0.0.0", PORT):
        print("Server ready")
        await asyncio.Future()




if __name__ == "__main__":
    asyncio.run(main())
