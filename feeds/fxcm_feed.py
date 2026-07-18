"""
Фид FXCM: брокер → внутренняя шина. Python 3.7 (forexconnect есть только под неё).

Всё, что фид умеет:
  1. держать соединение с FXCM и слушать тики (push через table_manager);
  2. слать тики, историю и метаданные инструментов хабу через core/bus;
  3. писать сырые тики в data/*.csv — вечный архив (перенос server.py:tick_writer).

Чего фид НЕ делает: не режет свечи, не пишет в SQLite, не говорит с браузером.
Это работа хаба (hub.py, Python 3.10).

Запуск:  python3.7 -m feeds.fxcm_feed   (из корня проекта)

ВНИМАНИЕ: фид открывает СВОЮ сессию к FXCM. Пока живой server.py держит свою,
второй логин тем же аккаунтом может выбить первую сессию — не запускать
одновременно с боевым сервисом chart без ведома владельца.
"""

import asyncio
import csv
import os
import queue
import sys
import threading
import time
from datetime import datetime, timedelta

import numpy as np
from forexconnect import Common, ForexConnect

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.bus import BusClient, make_candles, make_instruments, make_tick

PROVIDER      = "fxcm"
SYMBOLS       = ["EUR/USD", "AUD/USD", "USD/CAD", "USD/JPY"]
DATA_DIR      = "data"
HISTORY_COUNT = 10000

# ТФ, которые грузятся у брокера напрямую: из 10 000 минуток (~7 дней) вышло бы
# лишь ~7 дневных баров и ~41 H4 — слишком коротко для индикаторов.
# Значение = (строка ТФ ForexConnect, глубина истории в днях).
DIRECT_LOAD_TF = {
    "H1": ("H1", 90),
    "H4": ("H4", 90),
    "D1": ("D1", 400),
}

# Пачка истории режется на куски: 10 000 минуток одним сообщением дают ~800 КБ
# JSON и упираются в лимит входящего сообщения websockets.
CHUNK_BARS = 2000

LOGIN      = os.getenv("FXCM_LOGIN")
PASSWORD   = os.getenv("FXCM_PASSWORD")
URL        = os.getenv("FXCM_URL", "http://www.fxcorporate.com/Hosts.jsp")
CONNECTION = os.getenv("FXCM_CONNECTION", "Demo")

_last_price = {}

# ── watchdog «тихого зависания» FXCM ────────────────────────────────────
# Брокерская сессия может «повиснуть»: TCP жив, исключения нет, но тики
# перестали приходить. systemd Restart=always такое не ловит (процесс не упал),
# и обычный reconnect-по-исключению — тоже. Watchdog следит за временем
# последнего тика и, если в ОТКРЫТЫЙ рынок тишина дольше порога, форсирует
# пересоздание сессии.
_last_tick_ts   = time.time()          # время последнего принятого тика
_reconnect_flag = threading.Event()    # watchdog просит стриминг переподключиться
TICK_SILENCE_SEC = 120                 # тишины столько → рынок открыт, но фид завис


def market_open(ts=None):
    """Открыт ли форекс-рынок в момент ts (UTC).

    Форекс работает с воскресенья ~22:00 UTC до пятницы ~22:00 UTC (закрытие в
    Нью-Йорке). Точные минуты у брокеров плавают; берём консервативно, чтобы
    watchdog НЕ дёргал реконнекты в честно закрытый рынок (там тиков нет по
    определению, это не зависание).

    Args:
        ts: Unix-время (None = сейчас).

    Returns:
        bool — рынок предположительно открыт.
    """
    dt  = datetime.utcfromtimestamp(time.time() if ts is None else ts)
    wd  = dt.weekday()          # 0=Пн … 6=Вс
    hr  = dt.hour
    if wd == 5:                 # суббота — закрыт весь день
        return False
    if wd == 4 and hr >= 22:    # пятница после 22:00 UTC — закрыт
        return False
    if wd == 6 and hr < 22:     # воскресенье до 22:00 UTC — закрыт
        return False
    return True


def should_emit(symbol, price):
    """Изменилась ли цена с прошлого тика.

    Гейтит ТОЛЬКО запись в CSV. В шину тик уходит всегда: иначе в мёртвом рынке
    (цена не меняется минутами) хаб не получил бы ни одного тика и не открыл бы
    новую свечу — в истории появились бы пропущенные минуты.

    Args:
        symbol: Инструмент.
        price:  Текущая mid-цена.

    Returns:
        True, если цена отличается от предыдущей.
    """
    if symbol not in _last_price:
        _last_price[symbol] = price
        return True
    if price != _last_price[symbol]:
        _last_price[symbol] = price
        return True
    return False


def to_timestamp(dt):
    """Перевести время FXCM в unix-секунды.

    МИНА (не выстреливает, но заряжена): ветка `isinstance(dt, datetime)` зовёт
    .timestamp() на naive-времени, а он трактует его как ЛОКАЛЬНОЕ — на сервере
    это UTC+5, т.е. сдвиг на 5 часов. Живьём не срабатывает: FXCM отдаёт
    numpy.datetime64 и уходит во вторую ветку. При подключении другого источника
    (Lighter, Фаза 3) — обезвредить.

    Args:
        dt: numpy.datetime64, datetime или число.

    Returns:
        Int, unix-секунды.
    """
    if isinstance(dt, datetime):
        return int(dt.timestamp())
    if isinstance(dt, np.datetime64):
        return int(dt.astype("datetime64[s]").astype(int))
    return int(dt)


def _rows_to_candles(rows):
    """Превратить ответ ForexConnect в список свечей.

    Args:
        rows: numpy-массив от fx.get_history (столбцы Date/BidOpen/BidHigh/…).

    Returns:
        List of candle dicts (time/open/high/low/close), старые первыми.
    """
    out = []
    for row in rows:
        out.append({
            "time":  to_timestamp(row["Date"]),
            "open":  float(row["BidOpen"]),
            "high":  float(row["BidHigh"]),
            "low":   float(row["BidLow"]),
            "close": float(row["BidClose"]),
        })
    return out


def _send_candles(bus, symbol, tf, bars):
    """Отправить историю хабу, порезав на куски по CHUNK_BARS.

    Порядок кусков сохраняется (WebSocket его гарантирует), а хаб вливает их
    upsert'ом по времени — пересечения безопасны.

    Args:
        bus:    BusClient.
        symbol: Инструмент.
        tf:     Таймфрейм.
        bars:   Список свечей, старые первыми.

    Returns:
        Int — сколько баров отправлено.
    """
    sent = 0
    for i in range(0, len(bars), CHUNK_BARS):
        chunk = bars[i:i + CHUNK_BARS]
        if bus.send_threadsafe(make_candles(PROVIDER, symbol, tf, chunk)):
            sent += len(chunk)
    return sent


def tick_writer(tick_queue):
    """Поток-демон: писать сырые тики в data/<SYMBOL>_<YYYYMMDD>.csv.

    Точный перенос server.py:tick_writer — формат колонок и ротация по дате те же:
    из этих файлов при нужде восстанавливается M1, это вечный архив.

    Args:
        tick_queue: Очередь dict'ов ts/dt/symbol/bid/ask/mid.

    Returns:
        None (бесконечный цикл).
    """
    open_files = {}

    def get_writer(symbol, date_str):
        """Открыть (или переиспользовать) CSV нужного дня.

        Args:
            symbol:   Инструмент.
            date_str: Дата в формате YYYYMMDD.

        Returns:
            csv.writer для этого файла.
        """
        key = (symbol, date_str)
        if key not in open_files:
            stale = [k for k in open_files if k[0] == symbol and k[1] != date_str]
            for k in stale:
                open_files[k]["file"].close()
                del open_files[k]

            sym_clean = symbol.replace("/", "")
            filename  = os.path.join(DATA_DIR, "%s_%s.csv" % (sym_clean, date_str))
            is_new    = not os.path.exists(filename)
            f = open(filename, "a", newline="", buffering=1)
            w = csv.writer(f)
            if is_new:
                w.writerow(["timestamp_utc", "datetime_utc", "symbol",
                            "bid", "ask", "mid"])
            open_files[key] = {"file": f, "writer": w}

        return open_files[key]["writer"]

    while True:
        tick   = tick_queue.get()
        dt     = tick["dt"]
        writer = get_writer(tick["symbol"], dt.strftime("%Y%m%d"))
        writer.writerow([
            tick["ts"],
            dt.strftime("%d/%m/%Y %H:%M:%S.%f")[:-3],
            tick["symbol"],
            "%.5f" % tick["bid"],
            "%.5f" % tick["ask"],
            "%.5f" % tick["mid"],
        ])


def send_instruments(fx, bus):
    """Отправить хабу метаданные инструментов (число знаков в цене).

    Отсюда фронт возьмёт precision вместо захардкоженных 5 знаков: у USD/JPY их
    на самом деле 3. Объёма у FXCM нет — has_volume=False, size у тиков всегда None.

    Args:
        fx:  Активная сессия ForexConnect.
        bus: BusClient.

    Returns:
        Int — сколько инструментов отправлено.
    """
    data = []
    try:
        offers = fx.get_table(ForexConnect.OFFERS)
        for row in offers:
            symbol = row.instrument
            if symbol not in SYMBOLS:
                continue
            digits = getattr(row, "digits", None)
            data.append({
                "symbol":         symbol,
                "price_decimals": int(digits) if digits else 5,
                "size_decimals":  None,
                "min_base":       None,
                "has_volume":     False,
                "meta":           {},
            })
    except Exception as err:
        # Метаданные — не повод ронять соединение: без них фронт просто
        # останется на прежнем хардкоде.
        print("[feed] не смог прочитать офферы: %r" % (err,))
        return 0

    if data:
        bus.send_threadsafe(make_instruments(PROVIDER, data))
        print("[feed] инструменты: %s"
              % ", ".join("%s(%d)" % (d["symbol"], d["price_decimals"]) for d in data))
    return len(data)


def load_history(fx, bus):
    """Догрузить историю у брокера и отправить её хабу.

    Хаб к брокеру не ходит, поэтому дыру за время простоя закрывает фид. Что
    именно резать (незакрытый бар текущего бакета) — решает хаб: он знает время
    и сетку. Фид шлёт всё, что дал брокер.

    Args:
        fx:  Активная сессия ForexConnect.
        bus: BusClient.

    Returns:
        None.
    """
    now = datetime.utcnow()
    print("[feed] загрузка истории…")

    for symbol in SYMBOLS:
        # M1: 30 дней — как в монолите; лишнее подрежет окно ретеншена в хабе.
        raw  = fx.get_history(symbol, "m1", now - timedelta(days=30), now)
        bars = _rows_to_candles(raw[-HISTORY_COUNT:])
        sent = _send_candles(bus, symbol, "M1", bars)
        print("[feed] %s M1: %d баров" % (symbol, sent))

        for tf, (fx_tf, days) in DIRECT_LOAD_TF.items():
            raw  = fx.get_history(symbol, fx_tf, now - timedelta(days=days), now)
            bars = _rows_to_candles(raw)
            sent = _send_candles(bus, symbol, tf, bars)
            print("[feed] %s %s: %d баров" % (symbol, tf, sent))

    print("[feed] история отправлена в шину")


def fxcm_streaming(bus, tick_queue):
    """Поток-демон: вечный цикл соединения с FXCM и приём тиков.

    Колбэк брокера исполняется в ЕГО демон-потоке, не в asyncio — поэтому
    оттуда можно только bus.send_threadsafe() и tick_queue.put_nowait().

    Args:
        bus:        BusClient.
        tick_queue: Очередь для CSV-писателя.

    Returns:
        None (бесконечный цикл).
    """

    def on_offer_changed(_, row_id, row_data, *args):
        """Колбэк FXCM на каждое изменение цены.

        Args:
            row_data: Строка таблицы офферов (instrument/bid/ask).

        Returns:
            None.
        """
        try:
            symbol = row_data.instrument
            if symbol not in SYMBOLS:
                return

            bid = row_data.bid
            ask = row_data.ask
            if bid is None or ask is None or bid <= 0 or ask <= 0:
                return

            mid = round((bid + ask) / 2, 5)
            ts  = time.time()
            dt  = datetime.utcnow()

            # Отметка для watchdog: сессия жива, тики идут.
            global _last_tick_ts
            _last_tick_ts = ts

            # В шину — КАЖДЫЙ тик, без фильтра: иначе в мёртвом рынке хаб не
            # получит тика и не откроет новую свечу (см. should_emit).
            bus.send_threadsafe(make_tick(PROVIDER, symbol, ts, mid))

            if should_emit(symbol, mid):
                try:
                    tick_queue.put_nowait({
                        "ts": ts, "dt": dt, "symbol": symbol,
                        "bid": bid, "ask": ask, "mid": mid,
                    })
                except queue.Full:
                    pass

        except Exception as err:
            print("[feed] ошибка обработки тика: %r" % (err,))

    global _last_tick_ts
    while True:
        try:
            with ForexConnect() as fx:
                fx.login(LOGIN, PASSWORD, URL, CONNECTION)
                print("[feed] подключён к FXCM (%s)" % CONNECTION)

                send_instruments(fx, bus)
                load_history(fx, bus)

                offers = fx.get_table(ForexConnect.OFFERS)
                Common.subscribe_table_updates(
                    offers, on_change_callback=on_offer_changed)

                # Свежая сессия — сбрасываем счётчик тишины и флаг реконнекта,
                # иначе watchdog сработал бы сразу на «старую» тишину.
                _last_tick_ts = time.time()
                _reconnect_flag.clear()
                print("[feed] стриминг активен, ждём тики")

                # Держим поток живым, пока watchdog не попросит переподключиться
                # (тихое зависание сессии) — тогда выходим из with, ForexConnect
                # закрывает сессию, и цикл создаёт новую.
                _reconnect_flag.wait()
                print("[feed] watchdog: форсированное переподключение FXCM")

        except Exception as err:
            print("[feed] обрыв соединения с FXCM: %r — переподключение через 5 с"
                  % (err,))
            time.sleep(5)


def fxcm_watchdog():
    """Поток-демон: ловить «тихое зависание» FXCM-сессии.

    Раз в 30 с проверяет, сколько прошло с последнего тика. Если рынок ОТКРЫТ,
    а тишина дольше TICK_SILENCE_SEC — сессия жива по TCP, но данных нет:
    выставляем флаг, стриминг-цикл пересоздаёт сессию. В закрытый рынок
    (выходные) тишина легитимна — не трогаем.

    Args:
        None.

    Returns:
        None (бесконечный цикл).
    """
    while True:
        time.sleep(30)
        if _reconnect_flag.is_set():
            continue   # переподключение уже запрошено — ждём, не дублируем
        silence = time.time() - _last_tick_ts
        if silence > TICK_SILENCE_SEC and market_open():
            print("[feed] watchdog: тишина %.0f с в открытый рынок — реконнект"
                  % silence)
            _reconnect_flag.set()


async def _stats_loop(bus, every=60):
    """Печатать статистику шины раз в минуту.

    Args:
        bus:   BusClient.
        every: Период, секунды.

    Returns:
        None (бесконечный цикл).
    """
    while True:
        await asyncio.sleep(every)
        print("[feed] %s" % bus.stats)


async def main():
    """Поднять фид: CSV-писатель, стриминг FXCM, соединение с шиной.

    Returns:
        None (работает вечно).
    """
    if not LOGIN or not PASSWORD:
        raise RuntimeError("нет FXCM_LOGIN / FXCM_PASSWORD в окружении")

    os.makedirs(DATA_DIR, exist_ok=True)

    tick_queue = queue.Queue(maxsize=200000)
    bus        = BusClient(PROVIDER)

    threading.Thread(target=tick_writer, args=(tick_queue,), daemon=True).start()
    threading.Thread(target=fxcm_streaming, args=(bus, tick_queue),
                     daemon=True).start()
    threading.Thread(target=fxcm_watchdog, daemon=True).start()

    asyncio.ensure_future(_stats_loop(bus))
    await bus.run()   # вечный цикл: коннект к хабу + отправка очереди


if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(main())
