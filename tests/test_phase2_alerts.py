"""
Тест ценовых алертов в хабе (пункт 2 пост-переключения).

Контракт с фронтом жёсткий: add_alert → alert_created (с серверным id), пересечение
уровня → alert. Одна опечатка в имени поля молча ломает фронт — поэтому проверяем
и точные поля ответов, и срабатывание именно по ПЕРЕСЕЧЕНИЮ.

Run:  python3.10 tests/test_phase2_alerts.py
"""

import asyncio
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import websockets

from core.bus import BusClient, make_tick
from hub import Hub
from test_phase2_bus import free_port, run_async
from test_phase2_hub import HubHarness, PROVIDER, SYMBOL


class TestAlertUnit(unittest.TestCase):
    """Проверка пересечения уровня — на голом Hub, без сети."""

    def _hub(self):
        cfg = {
            "db_path": ":memory:", "keep_bars": 2000, "ws_port": 0,
            "bus_host": "127.0.0.1", "bus_port": 0, "trim_every": 200,
            "tf_seconds": {"M1": 60}, "broker_tf": [],
            "markets": {PROVIDER: [SYMBOL]},
        }
        return Hub(cfg)

    def test_crossing_up_triggers(self):
        h = self._hub()
        h._alerts[SYMBOL] = [{"id": 1, "price": 1.1750, "triggered": False}]
        fired = []
        h._broadcast = lambda p: fired.append(json.loads(p))

        h._check_alerts(SYMBOL, 1.1749, 1.1751)   # пересекли снизу вверх
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0]["type"], "alert")
        self.assertEqual(fired[0]["id"], 1)
        self.assertEqual(fired[0]["price"], 1.1750)
        self.assertTrue(h._alerts[SYMBOL][0]["triggered"])

    def test_crossing_down_triggers(self):
        h = self._hub()
        h._alerts[SYMBOL] = [{"id": 1, "price": 1.1750, "triggered": False}]
        fired = []
        h._broadcast = lambda p: fired.append(json.loads(p))

        h._check_alerts(SYMBOL, 1.1751, 1.1749)   # сверху вниз — тоже срабатывает
        self.assertEqual(len(fired), 1)

    def test_no_crossing_no_trigger(self):
        h = self._hub()
        h._alerts[SYMBOL] = [{"id": 1, "price": 1.1750, "triggered": False}]
        fired = []
        h._broadcast = lambda p: fired.append(json.loads(p))

        h._check_alerts(SYMBOL, 1.1740, 1.1745)   # уровень не задет
        self.assertEqual(fired, [])
        self.assertFalse(h._alerts[SYMBOL][0]["triggered"])

    def test_triggers_once_only(self):
        h = self._hub()
        h._alerts[SYMBOL] = [{"id": 1, "price": 1.1750, "triggered": False}]
        fired = []
        h._broadcast = lambda p: fired.append(json.loads(p))

        h._check_alerts(SYMBOL, 1.1749, 1.1751)
        h._check_alerts(SYMBOL, 1.1751, 1.1749)   # снова через уровень
        self.assertEqual(len(fired), 1, "сработавший алерт не должен повторяться")

    def test_update_rearms_moved_alert(self):
        """Сдвиг уровня перезаряжает алерт (в server.py был баг — оставался немым)."""
        h = self._hub()
        h._alerts[SYMBOL] = [{"id": 1, "price": 1.1750, "triggered": True}]
        h._on_update_alert({"symbol": SYMBOL, "id": 1, "price": 1.1800})
        self.assertEqual(h._alerts[SYMBOL][0]["price"], 1.1800)
        self.assertFalse(h._alerts[SYMBOL][0]["triggered"], "перемещённый алерт снова заряжен")

    def test_remove(self):
        h = self._hub()
        h._alerts[SYMBOL] = [{"id": 1, "price": 1.17, "triggered": False},
                             {"id": 2, "price": 1.18, "triggered": False}]
        h._on_remove_alert({"symbol": SYMBOL, "id": 1})
        self.assertEqual([a["id"] for a in h._alerts[SYMBOL]], [2])


class TestAlertOverWebSocket(unittest.TestCase):
    """Полный цикл через настоящий WS: create → серверный id → trigger тиком."""

    def test_create_then_trigger(self):
        tmp = tempfile.mkdtemp()
        db  = os.path.join(tmp, "hub.db")

        async def scenario():
            h = HubHarness(db)
            await h.start()

            feed = BusClient(PROVIDER, url="ws://127.0.0.1:%d" % h.config["bus_port"])
            ftask = asyncio.ensure_future(feed.run())
            await asyncio.sleep(0.3)

            # «Прогреваем» last_price, чтобы первый тик не считался пересечением.
            feed.send_threadsafe(make_tick(PROVIDER, SYMBOL, 1784036225, 1.1700))
            await asyncio.sleep(0.3)

            url = "ws://127.0.0.1:%d" % h.config["ws_port"]
            got = []
            async with websockets.connect(url) as ws:
                await ws.send(json.dumps({"type": "add_alert",
                                          "symbol": SYMBOL, "price": 1.1750}))
                created = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
                got.append(created)

                # Двигаем цену через уровень 1.1750.
                feed.send_threadsafe(make_tick(PROVIDER, SYMBOL, 1784036226, 1.1749))
                await asyncio.sleep(0.2)
                feed.send_threadsafe(make_tick(PROVIDER, SYMBOL, 1784036227, 1.1760))
                # Ждём событие alert (пропуская возможные update).
                for _ in range(10):
                    m = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
                    if m["type"] == "alert":
                        got.append(m)
                        break

            ftask.cancel()
            await asyncio.sleep(0)
            await h.stop()
            return got

        got = run_async(scenario())

        created, alert = got[0], got[1]
        self.assertEqual(created["type"], "alert_created")
        self.assertEqual(created["symbol"], SYMBOL)
        self.assertEqual(created["price"], 1.1750)
        self.assertIsInstance(created["id"], int, "фронт подставит этот id вместо null")

        self.assertEqual(alert["type"], "alert")
        self.assertEqual(alert["id"], created["id"], "id срабатывания должен совпасть с созданным")
        self.assertEqual(alert["price"], 1.1750)
        self.assertEqual(alert["symbol"], SYMBOL)


if __name__ == "__main__":
    unittest.main(verbosity=2)
