"""
Фид Lighter: биржа → внутренняя шина. Python 3.10 (SDK Lighter требует 3.8+).

Всё, что фид умеет:
  1. держать WS-соединение с Lighter и слушать сделки (канал trade/{market_id});
  2. слать тики, историю и метаданные инструментов хабу через core/bus;
  3. писать сырые тики в data/*.csv — сырьё для рэндж-баров.

Чего фид НЕ делает: не режет свечи, не пишет в SQLite, не говорит с браузером,
не ходит в стакан. Это работа хаба (hub.py) — или не нужно вовсе.

Запуск:  python3.10 -m feeds.lighter_feed   (из корня проекта)

ОТЛИЧИЕ ОТ ФИДА FXCM — РОТАЦИЯ ТИКОВ. Архив FXCM вечный, а тики Lighter живут
кольцевым буфером TICK_RETENTION_DAYS (крипта торгуется 24/7 и даёт на порядок
больше сделок). Тики нужны только для построения рэндж-баров; свечи, объём и
профиль объёма хранятся отдельно и ротацию переживают. Чистится только
`lighter`-часть каталога data/ — файлы FXCM не трогаются никогда.

Ключи НЕ НУЖНЫ: рыночные данные Lighter публичны. Подпись ордеров (Фаза 5) —
отдельный процесс, здесь её нет.
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
import queue
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import websockets

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.bus import BusClient, make_candles, make_instruments, make_tick
from core.logfmt import setup as _log_setup
from feeds.lighter_raw import Deduper, normalize_trade

log = _log_setup("feed-lighter")

PROVIDER = "lighter"
DATA_DIR = "data"

WS_URL   = os.getenv("LIGHTER_WS_URL", "wss://mainnet.zklighter.elliot.ai/stream")
REST_URL = os.getenv("LIGHTER_REST_URL", "https://mainnet.zklighter.elliot.ai")

# Белый список: 12 инструментов, отобранных по суточному обороту (замер
# 2026-07-18). Ниже XAG обрыв ликвидности — там профиль объёма будет дырявым.
# 226 рынков биржи целиком не подписываются осознанно.
# Тикер → market_id.
MARKETS = {
    "BTC":  1,
    "ETH":  0,
    "HYPE": 24,
    "SOL":  2,
    "WTI":  145,
    "LIT":  120,
    "ZEC":  90,
    "XAU":  92,
    "BNB":  25,
    "SPCX": 194,
    "MU":   164,
    "XAG":  93,
}
MARKET_TO_SYMBOL = {mid: sym for sym, mid in MARKETS.items()}

# Кольцевой буфер сырых тиков. Согласовано с владельцем: глубже рэндж-бар
# другого размера уже не построить, это осознанный размен диска на гибкость.
TICK_RETENTION_DAYS = 14

# Бэкфил при старте: 2000 баров M1 ≈ 4 запроса по 500 (потолок API — 500).
BACKFILL_TF        = "1m"
BACKFILL_BARS      = 2000
CANDLES_PAGE_LIMIT = 500
CHUNK_BARS         = 2000

# Переподключение к WS: экспоненциальный backoff, как в боте.
RECONNECT_MIN_SEC = 1
RECONNECT_MAX_SEC = 60


# ── REST: метаданные и история ────────────────────────────────────────

def _rest_get(path, params=None):
    """Сходить в публичный REST Lighter и разобрать JSON.

    Ходим «сырым» REST, а не через SDK: `mark_price`/`index_price` есть в JSON,
    но отсутствуют в Pydantic-моделях SDK — модели молча их проглатывают.

    Args:
        path:   Путь эндпоинта, например "/api/v1/orderBooks".
        params: Dict query-параметров или None.

    Returns:
        Разобранный JSON (dict).
    """
    url = REST_URL + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read())


def fetch_instruments():
    """Забрать метаданные рынков из белого списка.

    Returns:
        Список dict'ов контракта шины: symbol, price_decimals, size_decimals,
        min_base, has_volume, meta.
    """
    data = _rest_get("/api/v1/orderBookDetails")
    details = data.get("order_book_details") or []

    out = []
    for d in details:
        symbol = d.get("symbol")
        if symbol not in MARKETS:
            continue
        out.append({
            "symbol":         symbol,
            "price_decimals": d.get("price_decimals", d.get("supported_price_decimals")),
            "size_decimals":  d.get("size_decimals", d.get("supported_size_decimals")),
            "min_base":       float(d.get("min_base_amount") or 0),
            # У Lighter объём настоящий биржевой — в отличие от FXCM, где его нет.
            "has_volume":     True,
            "meta": {
                "market_id":   d.get("market_id"),
                "market_type": d.get("market_type"),
                "mark_price":  d.get("mark_price"),
                "index_price": d.get("index_price"),
            },
        })

    missing = set(MARKETS) - {o["symbol"] for o in out}
    if missing:
        log.warning("нет метаданных для рынков: %s", sorted(missing))
    return out


def fetch_candles(symbol, market_id, resolution=BACKFILL_TF, bars=BACKFILL_BARS):
    """Загрузить историю свечей постранично.

    Пагинация идёт по `start`/`end`: параметр `count_back` ведёт себя
    непредсказуемо и не используется. `resolution` принимает ТОЛЬКО
    `1m 5m 15m 1h 4h 1d` — голые числа дают 400 invalid param.

    Args:
        symbol:     Тикер (для логов).
        market_id:  ID рынка Lighter.
        resolution: Разрешение API.
        bars:       Сколько баров нужно суммарно.

    Returns:
        Список свечей (time/open/high/low/close/vol_base/vol_quote),
        старые первыми, время в СЕКУНДАХ.
    """
    step = 60  # BACKFILL_TF = 1m
    end = int(time.time())
    collected = {}

    while len(collected) < bars:
        want = min(CANDLES_PAGE_LIMIT, bars - len(collected))
        start = end - want * step
        try:
            data = _rest_get("/api/v1/candles", {
                "market_id":       market_id,
                "resolution":      resolution,
                "start_timestamp": start * 1000,   # API ждёт миллисекунды
                "end_timestamp":   end * 1000,
                # ОБЯЗАТЕЛЕН: без count_back эндпоинт отвечает 400 invalid param.
                # При этом сам по себе он ненадёжен (count_back=0 даёт пустоту
                # на непустом диапазоне), поэтому границы задаёт start/end,
                # а count_back лишь дублирует размер страницы.
                "count_back":      want,
            })
        except Exception as err:
            log.warning("бэкфил %s: запрос упал (%s), остановлен на %d барах",
                        symbol, err, len(collected))
            break

        # Ответ короткоимённый: свечи лежат в "c" (не "candles"), поля —
        # t/o/h/l/c/v/V. Проверено живьём 2026-07-19.
        page = data.get("c") or []
        if not page:
            break

        for c in page:
            # `t` — миллисекунды. В шине время только в секундах.
            ts = int(c.get("t") or 0) // 1000
            if ts <= 0:
                continue
            collected[ts] = {
                "time":      ts,
                "open":      float(c["o"]),
                "high":      float(c["h"]),
                "low":       float(c["l"]),
                "close":     float(c["c"]),
                # v = base (BTC), V = quote (USDC) — храним оба.
                "vol_base":  float(c.get("v") or 0),
                "vol_quote": float(c.get("V") or 0),
            }

        end = start
        if len(page) < want:
            break

    bars_out = [collected[k] for k in sorted(collected)]
    log.info("бэкфил %s: %d баров %s", symbol, len(bars_out), resolution)
    return bars_out


def load_history(bus):
    """Догрузить историю по всем рынкам и отправить её хабу.

    Хаб к бирже не ходит, поэтому дыру за время простоя закрывает фид.
    Секундные ТФ (S5–S30) бэкфила не имеют: минимальное разрешение API — 1m.
    Они копятся живьём, это согласовано.

    Args:
        bus: BusClient.

    Returns:
        None.
    """
    for symbol, market_id in MARKETS.items():
        bars = fetch_candles(symbol, market_id)
        if not bars:
            continue
        for i in range(0, len(bars), CHUNK_BARS):
            bus.send_threadsafe(
                make_candles(PROVIDER, symbol, "M1", bars[i:i + CHUNK_BARS])
            )


# ── запись сырых тиков ────────────────────────────────────────────────

def _purge_old_ticks():
    """Удалить тиковые CSV Lighter старше TICK_RETENTION_DAYS.

    Кольцевой буфер: тики нужны только под рэндж-бары, глубина ограничена
    осознанно. Затрагиваются ТОЛЬКО файлы из белого списка MARKETS —
    архив FXCM вечный и здесь не фигурирует.

    Returns:
        None.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=TICK_RETENTION_DAYS)
    try:
        names = os.listdir(DATA_DIR)
    except OSError as err:
        log.warning("ротация тиков: не прочитать %s (%s)", DATA_DIR, err)
        return

    removed = 0
    for name in names:
        if not name.endswith(".csv"):
            continue
        stem = name[:-4]
        if "_" not in stem:
            continue
        sym, _, date_part = stem.rpartition("_")
        # Только наши рынки: чужие файлы (в т.ч. FXCM) не трогаем.
        if sym not in MARKETS or len(date_part) != 8 or not date_part.isdigit():
            continue
        try:
            day = datetime.strptime(date_part, "%Y%m%d").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if day < cutoff:
            try:
                os.remove(os.path.join(DATA_DIR, name))
                removed += 1
            except OSError as err:
                log.warning("ротация тиков: не удалить %s (%s)", name, err)

    if removed:
        log.info("ротация тиков: удалено файлов старше %d дней: %d",
                 TICK_RETENTION_DAYS, removed)


def tick_writer(tick_queue):
    """Поток-демон: писать сырые тики в data/<SYMBOL>_<YYYYMMDD>.csv.

    Формат отличается от FXCM осознанно: у биржи есть объём и сторона
    агрессора, а bid/ask в потоке сделок нет.

    Args:
        tick_queue: Очередь dict'ов symbol/ts/price/size/side/tid.

    Returns:
        None (бесконечный цикл).
    """
    open_files = {}
    last_purge = 0.0

    def get_writer(symbol, date_str):
        """Открыть (или переиспользовать) CSV нужного дня.

        Args:
            symbol:   Тикер.
            date_str: Дата YYYYMMDD (UTC).

        Returns:
            csv.writer для этого файла.
        """
        key = (symbol, date_str)
        if key not in open_files:
            stale = [k for k in open_files if k[0] == symbol and k[1] != date_str]
            for k in stale:
                open_files[k]["file"].close()
                del open_files[k]

            filename = os.path.join(DATA_DIR, "%s_%s.csv" % (symbol, date_str))
            is_new = not os.path.exists(filename)
            f = open(filename, "a", newline="", buffering=1)
            w = csv.writer(f)
            if is_new:
                w.writerow(["timestamp_utc", "datetime_utc", "symbol",
                            "price", "size", "side", "trade_id"])
            open_files[key] = {"file": f, "writer": w}

        return open_files[key]["writer"]

    while True:
        tick = tick_queue.get()
        dt = datetime.fromtimestamp(tick["ts"], tz=timezone.utc)
        writer = get_writer(tick["symbol"], dt.strftime("%Y%m%d"))
        writer.writerow([
            "%.3f" % tick["ts"],
            dt.strftime("%d/%m/%Y %H:%M:%S.%f")[:-3],
            tick["symbol"],
            repr(tick["price"]),
            repr(tick["size"]),
            tick["side"],
            tick["tid"],
        ])

        # Ротацию гоняем раз в час прямо здесь: отдельный поток ради одного
        # os.listdir раз в час не нужен.
        now = time.time()
        if now - last_purge > 3600:
            last_purge = now
            _purge_old_ticks()


# ── WS: поток сделок ──────────────────────────────────────────────────

async def _consume(ws, bus, tick_queue, dedupers, stats):
    """Обработать поток кадров одного WS-соединения.

    Args:
        ws:         Открытый websocket.
        bus:        BusClient.
        tick_queue: Очередь на запись в CSV.
        dedupers:   Dict market_id → Deduper.
        stats:      Dict счётчиков для периодического лога.

    Returns:
        None (выходит, когда соединение закрылось).
    """
    for mid in MARKETS.values():
        await ws.send(json.dumps({"type": "subscribe", "channel": "trade/%d" % mid}))
    log.info("подписка на %d рынков", len(MARKETS))

    unknown_types = set()

    async for raw in ws:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError as err:
            log.warning("не-JSON кадр от Lighter: %s", err)
            continue

        mtype = msg.get("type", "")

        # Без ответа на ping биржа рвёт соединение примерно через 2 минуты.
        if mtype == "ping":
            await ws.send(json.dumps({"type": "pong"}))
            continue
        if mtype == "connected":
            continue

        # `subscribed/trade` — снапшот последних ~50 сделок. Они придут ещё раз
        # через `update/trade`, поэтому снапшот пропускается целиком: иначе на
        # каждом переподключении получали бы дубли.
        if mtype.startswith("subscribed/"):
            continue

        if not mtype.startswith("update/trade"):
            if mtype not in unknown_types:
                unknown_types.add(mtype)
                log.warning("неизвестный тип кадра: %r", mtype)
            continue

        channel = msg.get("channel", "")
        try:
            market_id = int(channel.split(":")[1])
        except (IndexError, ValueError):
            log.warning("кривой channel: %r", channel)
            continue

        symbol = MARKET_TO_SYMBOL.get(market_id)
        if symbol is None:
            continue

        trades = msg.get("trades") or msg.get("data") or []
        if not isinstance(trades, list):
            trades = [trades]

        dedup = dedupers[market_id]
        for t in trades:
            rec = normalize_trade(market_id, t)
            if rec is None:
                stats["bad"] += 1
                continue
            if not dedup.is_new(rec["tid"]):
                stats["dup"] += 1
                continue

            # Граница мс→сек. Внутри шины время ТОЛЬКО в секундах:
            # core.bus.validate() заворачивает миллисекунды как ошибку.
            ts_sec = rec["t"] / 1000.0

            bus.send_threadsafe(
                make_tick(PROVIDER, symbol, ts_sec, rec["p"], size=rec["s"])
            )
            try:
                tick_queue.put_nowait({
                    "symbol": symbol,
                    "ts":     ts_sec,
                    "price":  rec["p"],
                    "size":   rec["s"],
                    "side":   rec["side"],
                    "tid":    rec["tid"],
                })
            except queue.Full:
                stats["queue_full"] += 1
            stats["ok"] += 1


async def ws_loop(bus, tick_queue):
    """Вечный цикл: соединение с Lighter, слушать сделки, переподключаться.

    Backoff экспоненциальный (1→60 с), попытки бесконечны. При каждом
    переподключении дедупликаторы создаются заново — снапшот всё равно
    пропускается, а свежие сделки дублями не будут.

    Args:
        bus:        BusClient.
        tick_queue: Очередь на запись в CSV.

    Returns:
        None (работает вечно).
    """
    delay = RECONNECT_MIN_SEC
    stats = {"ok": 0, "dup": 0, "bad": 0, "queue_full": 0}

    while True:
        try:
            async with websockets.connect(WS_URL, max_size=8 * 1024 * 1024,
                                          ping_interval=20, ping_timeout=20) as ws:
                log.info("подключён к %s", WS_URL)
                delay = RECONNECT_MIN_SEC
                dedupers = {mid: Deduper() for mid in MARKETS.values()}
                await _consume(ws, bus, tick_queue, dedupers, stats)
            log.warning("WS закрыт биржей, переподключение через %.0f с", delay)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            log.warning("WS оборвался (%s), переподключение через %.0f с", err, delay)

        await asyncio.sleep(delay)
        delay = min(delay * 2, RECONNECT_MAX_SEC)


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
        log.info("%s", bus.stats)


async def main():
    """Поднять фид: метаданные, бэкфил, CSV-писатель, WS, шина.

    Returns:
        None (работает вечно).
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    _purge_old_ticks()

    tick_queue = queue.Queue(maxsize=200000)
    bus = BusClient(PROVIDER)

    threading.Thread(target=tick_writer, args=(tick_queue,), daemon=True).start()

    # Метаданные и бэкфил — блокирующий REST, поэтому в отдельном потоке:
    # event loop должен сразу заняться шиной и WS.
    def _bootstrap():
        """Отправить хабу метаданные и историю (блокирующий REST).

        Returns:
            None.
        """
        try:
            instruments = fetch_instruments()
            if instruments:
                bus.send_threadsafe(make_instruments(PROVIDER, instruments))
                log.info("метаданные отправлены: %d инструментов", len(instruments))
        except Exception as err:
            log.error("метаданные не загружены: %r", err)
        try:
            load_history(bus)
        except Exception as err:
            log.error("бэкфил упал: %r", err)

    threading.Thread(target=_bootstrap, daemon=True).start()

    asyncio.ensure_future(_stats_loop(bus))
    asyncio.ensure_future(ws_loop(bus, tick_queue))
    await bus.run()   # вечный цикл: коннект к хабу + отправка очереди


if __name__ == "__main__":
    asyncio.run(main())
