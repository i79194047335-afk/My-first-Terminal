"""
Сквозной тест рэндж-баров в хабе: set_tf "R:<поинты>" → бэкфил из тикового
архива → history/update по контракту свечных ТФ → живые тики из шины.

Архив — синтетический CSV во временном каталоге (боевой data/ не читается:
тест должен быть детерминированным и быстрым). Ожидаемые бары посчитаны
руками в комментариях — тест сверяет реализацию с контрактом, а не саму
с собой.

Run:  python3.10 tests/test_range_hub.py
"""

import asyncio
import csv
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import websockets

from core.bus import BusClient, make_tick
from core.bus import BusServer  # noqa: F401  (импорт через harness)
from hub import Hub
from test_phase2_bus import free_port, run_async
from test_phase2_candles import TF_SECONDS
from test_phase2_hub import HubHarness

PROVIDER = "fxcm"
SYMBOL   = "EUR/USD"

# Тики архива (ts, mid). R:50 = 50 ПОИНТОВ = 0.0005. Ожидание руками:
#   бар1: 1.1000 → 1.1005 (диапазон ровно 50 поинтов) o=1.1000 h=1.1005 l=1.1000 c=1.1005 t=1000
#   бар2: открыт пробойщиком 1.1005; 1.1007 (h), 1.1003, 1.1002 (l, 50п — закрытие)
#         o=1.1005 h=1.1007 l=1.1002 c=1.1002 t=1002
#   живой: открыт от 1.1002; тик 1.1004 → o=1.1002 h=1.1004 l=1.1002 c=1.1004 t=1005
ARCHIVE_TICKS = [
    (1000.0, 1.1000),
    (1001.0, 1.1002),
    (1002.0, 1.1005),
    (1003.0, 1.1007),
    (1004.0, 1.1003),
    (1005.0, 1.1002),
    (1006.0, 1.1004),
]
EXPECTED_BAR1 = {"time": 1000, "open": 1.1000, "high": 1.1005,
                 "low": 1.1000, "close": 1.1005}
EXPECTED_BAR2 = {"time": 1002, "open": 1.1005, "high": 1.1007,
                 "low": 1.1002, "close": 1.1002}
EXPECTED_LIVE = {"time": 1005, "open": 1.1002, "high": 1.1004,
                 "low": 1.1002, "close": 1.1004}


def write_archive(data_dir, ticks):
    """Записать синтетический тиковый CSV в формате боевого data/*.csv.

    Args:
        data_dir: Каталог архива.
        ticks:    Список (ts, mid).

    Returns:
        None.
    """
    path = os.path.join(data_dir, "EURUSD_20260701.csv")
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp_utc", "datetime_utc", "symbol",
                    "bid", "ask", "mid"])
        for ts, mid in ticks:
            w.writerow([ts, "-", SYMBOL,
                        "%.5f" % (mid - 0.00004), "%.5f" % (mid + 0.00004),
                        "%.5f" % mid])


async def recv_typed(ws, wanted, request_id, timeout=5.0):
    """Читать сообщения, пока не придёт нужный тип с нужным requestId.

    Args:
        ws:         Клиентское соединение.
        wanted:     Тип сообщения ("history", "update").
        request_id: Ожидаемый requestId.
        timeout:    Секунд на всё ожидание.

    Returns:
        Разобранное сообщение.
    """
    loop_until = asyncio.get_event_loop().time() + timeout
    while True:
        left = loop_until - asyncio.get_event_loop().time()
        raw  = await asyncio.wait_for(ws.recv(), timeout=max(left, 0.1))
        msg  = json.loads(raw)
        if msg.get("type") == wanted and msg.get("requestId") == request_id:
            return msg


class TestRangeHub(unittest.TestCase):

    def test_parse_range_tf(self):
        """Валидные и мусорные строки рэндж-ТФ."""
        self.assertEqual(Hub.parse_range_tf("R:5"), 5.0)
        self.assertEqual(Hub.parse_range_tf("R:2.5"), 2.5)
        for bad in ("M1", "R:", "R:0", "R:-3", "R:abc", "R:99999", None, 5):
            self.assertIsNone(Hub.parse_range_tf(bad), repr(bad))

    def test_point_size_from_instruments(self):
        """Поинт (мин. тик) = 10^-price_decimals; без instruments — дефолт 0.00001."""
        hub = Hub.__new__(Hub)          # без запуска: метод трогает только поле
        hub._instruments = {PROVIDER: [
            {"symbol": "EUR/USD", "price_decimals": 5},
            {"symbol": "USD/JPY", "price_decimals": 3},
        ]}
        self.assertAlmostEqual(hub._point_size(PROVIDER, "EUR/USD"), 0.00001)
        self.assertAlmostEqual(hub._point_size(PROVIDER, "USD/JPY"), 0.001)
        self.assertAlmostEqual(hub._point_size(PROVIDER, "AUD/USD"), 0.00001)

    def test_backfill_history_live_flow(self):
        """Полный путь: set_tf → бэкфил CSV → history+update → живые тики."""
        tmp      = tempfile.mkdtemp()
        db       = os.path.join(tmp, "hub.db")
        data_dir = os.path.join(tmp, "data")
        os.makedirs(data_dir)
        write_archive(data_dir, ARCHIVE_TICKS)

        async def scenario():
            h = HubHarness(db)
            h.config["data_dir"] = data_dir
            h.hub._data_dir      = data_dir
            await h.start()

            bus  = BusClient(PROVIDER,
                             url="ws://127.0.0.1:%d" % h.config["bus_port"])
            task = asyncio.ensure_future(bus.run())
            await asyncio.sleep(0.3)

            url = "ws://127.0.0.1:%d" % h.config["ws_port"]
            async with websockets.connect(url) as ws:
                await ws.send(json.dumps({"type": "set_tf", "symbol": SYMBOL,
                                          "tf": "R:50", "requestId": 7}))

                history = await recv_typed(ws, "history", 7)
                live    = await recv_typed(ws, "update", 7)

                # Живой тик-пробойщик: low 1.0999 добивает диапазон живого
                # бара (h=1.1004) → закрытие, следом update нового current.
                bus.send_threadsafe(
                    make_tick(PROVIDER, SYMBOL, 2000.0, 1.0999))
                closed_upd = await recv_typed(ws, "update", 7)
                new_cur    = await recv_typed(ws, "update", 7)

                # Повторный set_tf того же диапазона — билдер уже в кэше,
                # история приходит сразу и включает закрытый живьём бар.
                await ws.send(json.dumps({"type": "set_tf", "symbol": SYMBOL,
                                          "tf": "R:50", "requestId": 8}))
                history2 = await recv_typed(ws, "history", 8)

            stats = h.hub.stats
            task.cancel()
            await asyncio.sleep(0)
            await h.stop()
            return history, live, closed_upd, new_cur, history2, stats

        history, live, closed_upd, new_cur, history2, stats = \
            run_async(scenario())

        # Бэкфил: ровно два закрытых бара, посчитанных руками, и живой бар.
        self.assertEqual(history["tf"], "R:50")
        self.assertEqual(len(history["data"]), 2)
        self.assertEqual(history["data"][0], EXPECTED_BAR1)
        self.assertEqual(history["data"][1], EXPECTED_BAR2)
        self.assertEqual(live["candle"], EXPECTED_LIVE)

        # Закрытие живого бара тиком шины: финальное состояние с пробойщиком…
        self.assertEqual(closed_upd["candle"]["time"],  1005)
        self.assertEqual(closed_upd["candle"]["low"],   1.0999)
        self.assertEqual(closed_upd["candle"]["close"], 1.0999)
        # …и новый current с непрерывным open.
        self.assertEqual(new_cur["candle"]["time"], 2000)
        self.assertEqual(new_cur["candle"]["open"], 1.0999)

        # Кэш: во второй истории 3 закрытых бара (третий закрыт живьём).
        self.assertEqual(len(history2["data"]), 3)
        self.assertEqual(history2["data"][2]["close"], 1.0999)

        self.assertEqual(stats["range"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=1)
