"""
Тест инструментов в хабе (Фаза 2.5 + структурный фикс).

Главное, что проверяем: рестарт хаба ВОССТАНАВЛИВАЕТ instruments из БД. Фид шлёт
их один раз при логине, и без восстановления каждый рестарт хаба терял бы их до
следующего рестарта фида — селектор и precision на фронте остались бы пустыми.
Плюс контракт get_instruments и структура сообщения для селектора.

Run:  python3.10 tests/test_phase2_instruments.py
"""

import asyncio
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import websockets

from core.bus import BusClient, make_instruments
from core.db import init_db, load_instruments
from hub import Hub
from test_phase2_bus import free_port, run_async
from test_phase2_hub import HubHarness, PROVIDER, SYMBOL

INSTRUMENTS = [
    {"symbol": "EUR/USD", "price_decimals": 5, "size_decimals": None,
     "min_base": None, "has_volume": False, "meta": {}},
    {"symbol": "USD/JPY", "price_decimals": 3, "size_decimals": None,
     "min_base": None, "has_volume": False, "meta": {}},
]


class TestLoadInstruments(unittest.TestCase):
    """core.db.load_instruments читает всех инструментов провайдера."""

    def test_roundtrip(self):
        from core.db import upsert_instrument
        tmp = tempfile.mkdtemp()
        conn = init_db(os.path.join(tmp, "t.db"))
        for i in INSTRUMENTS:
            upsert_instrument(conn, PROVIDER, i["symbol"],
                              price_decimals=i["price_decimals"])
        got = load_instruments(conn, PROVIDER)
        conn.close()
        self.assertEqual([g["symbol"] for g in got], ["EUR/USD", "USD/JPY"])
        jpy = [g for g in got if g["symbol"] == "USD/JPY"][0]
        self.assertEqual(jpy["price_decimals"], 3)

    def test_empty_provider(self):
        tmp = tempfile.mkdtemp()
        conn = init_db(os.path.join(tmp, "t.db"))
        self.assertEqual(load_instruments(conn, "nobody"), [])
        conn.close()


class TestInstrumentsSurviveRestart(unittest.TestCase):
    """Рестарт хаба восстанавливает instruments из БД — структурный фикс."""

    def test_restart_reloads_from_db(self):
        tmp = tempfile.mkdtemp()
        db  = os.path.join(tmp, "hub.db")

        async def first_run():
            h = HubHarness(db)
            await h.start()
            feed = BusClient(PROVIDER, url="ws://127.0.0.1:%d" % h.config["bus_port"])
            task = asyncio.ensure_future(feed.run())
            await asyncio.sleep(0.3)
            feed.send_threadsafe(make_instruments(PROVIDER, INSTRUMENTS))
            await asyncio.sleep(0.5)
            got = dict(h.hub._instruments)
            task.cancel()
            await asyncio.sleep(0)
            await h.stop()
            return got

        async def second_run():
            # Новый Hub на той же БД, БЕЗ фида — как рестарт chart-hub, пока фид
            # молчит (instruments он больше не шлёт до своего рестарта).
            h = HubHarness(db)
            await h.start()
            got = dict(h.hub._instruments)

            # И через WS: новый клиент должен получить instruments.
            url = "ws://127.0.0.1:%d" % h.config["ws_port"]
            ws_got = []
            async with websockets.connect(url) as ws:
                await ws.send(json.dumps({"type": "get_instruments"}))
                for _ in range(3):
                    m = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
                    if m["type"] == "instruments":
                        ws_got.append(m); break
            await h.stop()
            return got, ws_got

        first = run_async(first_run())
        self.assertIn(PROVIDER, first, "фид не долил instruments в первый прогон")

        mem, ws_got = run_async(second_run())
        self.assertIn(PROVIDER, mem, "рестарт хаба потерял instruments (не восстановил из БД)")
        syms = [i["symbol"] for i in mem[PROVIDER]]
        self.assertEqual(sorted(syms), ["EUR/USD", "USD/JPY"])

        self.assertEqual(len(ws_got), 1, "новый клиент после рестарта не получил instruments")
        got_syms = [i["symbol"] for p in ws_got[0]["data"].values() for i in p]
        self.assertIn("USD/JPY", got_syms)


if __name__ == "__main__":
    unittest.main(verbosity=2)
