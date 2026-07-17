"""
Сквозной тест хаба (Фаза 2.3): шина → нарезка → SQLite → WebSocket браузера.

Гоняет РЕАЛЬНЫЕ тики из data/*.csv через настоящий BusClient в настоящий Hub и
сверяет результат с эталонной нарезкой, скопированной из боевого server.py.
Проверяется и то, ради чего всё затевалось: рестарт хаба не должен рвать свечу.

Run:  python3.10 tests/test_phase2_hub.py
"""

import asyncio
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import websockets

from core.bus import BusClient, BusServer, make_candles, make_instruments, make_tick
from core.db import init_db, load_history
from hub import Hub
from test_phase2_candles import TF_SECONDS, load_ticks, reference_slice
from test_phase2_bus import free_port, run_async

PROVIDER = "fxcm"
SYMBOL   = "EUR/USD"

# Хватает, чтобы закрылись M1/M3/M5/M15 и накопилась история; полный день (94k)
# гонять через сокет в тесте незачем — нарезка на всех 11 ТФ уже проверена
# в test_phase2_candles.
TICK_LIMIT = 6000


def make_config(db_path, ws_port, bus_port):
    """Собрать конфиг хаба для теста.

    Args:
        db_path:  Путь к временной БД (боевую не трогаем).
        ws_port:  Порт браузерного WS.
        bus_port: Порт шины.

    Returns:
        Dict конфигурации в формате retention.json.
    """
    return {
        "db_path":    db_path,
        "keep_bars":  2000,
        "ws_port":    ws_port,
        "bus_host":   "127.0.0.1",
        "bus_port":   bus_port,
        "trim_every": 200,
        "tf_seconds": TF_SECONDS,
        "broker_tf":  ["H1", "H4", "D1"],
        "markets":    {PROVIDER: [SYMBOL]},
    }


class HubHarness:
    """Поднятый хаб + шина + WS, всё на временных портах и временной БД."""

    def __init__(self, db_path):
        """Создать стенд.

        Args:
            db_path: Путь к временной БД.

        Returns:
            None.
        """
        self.config   = make_config(db_path, free_port(), free_port())
        self.hub      = Hub(self.config)
        self._bus     = None
        self._ws      = None
        self._db_task = None

    async def start(self):
        """Поднять хаб так же, как это делает hub.main(), но без вечного цикла.

        Returns:
            None.
        """
        self.hub.restore()
        self.hub._loop = asyncio.get_event_loop()

        self._bus = BusServer(self.hub.on_bus_message,
                              self.config["bus_host"], self.config["bus_port"])
        await self._bus.start()

        self._ws = await websockets.serve(self.hub.ws_handler,
                                          "127.0.0.1", self.config["ws_port"])

    async def drain_db(self):
        """Дописать очередь свечей в SQLite синхронно, без потока-демона.

        В тесте поток-писатель не поднимаем: он вечный и его нельзя дождаться.
        Пишем ровно то, что накопилось, тем же upsert_candle.

        Returns:
            Int — сколько свечей записано.
        """
        from core.db import upsert_candle
        conn  = init_db(self.config["db_path"])
        count = 0
        while not self.hub._db_queue.empty():
            provider, symbol, tf, candle = self.hub._db_queue.get()
            upsert_candle(conn, provider, symbol, tf, candle)
            count += 1
        conn.close()
        return count

    async def stop(self):
        """Погасить стенд.

        Returns:
            None.
        """
        await self._bus.close()
        self._ws.close()
        await self._ws.wait_closed()


class TestHubPipeline(unittest.TestCase):
    """Тики, пришедшие по шине, обязаны стать теми же свечами, что в server.py."""

    @classmethod
    def setUpClass(cls):
        cls.ticks = load_ticks()[:TICK_LIMIT]

    def test_ticks_through_bus_match_reference_slicing(self):
        if not self.ticks:
            self.skipTest("нет data/*.csv")

        tmp = tempfile.mkdtemp()
        db  = os.path.join(tmp, "hub.db")

        async def scenario():
            h = HubHarness(db)
            await h.start()

            client = BusClient(PROVIDER, url="ws://127.0.0.1:%d" % h.config["bus_port"])
            task   = asyncio.ensure_future(client.run())
            await asyncio.sleep(0.3)

            for price, ts in self.ticks:
                client.send_threadsafe(make_tick(PROVIDER, SYMBOL, ts, price))

            # Ждём, пока хаб переварит всё, что мы налили в шину.
            for _ in range(100):
                await asyncio.sleep(0.1)
                if h.hub.ticks_received >= len(self.ticks):
                    break

            written = await h.drain_db()
            stats   = h.hub.stats
            hist    = {tf: list(h.hub._history[(PROVIDER, SYMBOL)].get(tf, []))
                       for tf in TF_SECONDS}
            current = {tf: h.hub._builders[(PROVIDER, SYMBOL)].current(tf)
                       for tf in TF_SECONDS}

            task.cancel()
            await asyncio.sleep(0)
            await h.stop()
            return stats, hist, current, written

        stats, hist, current, written = run_async(scenario())

        self.assertEqual(stats["ticks"], len(self.ticks), "хаб получил не все тики")
        self.assertEqual(stats["ignored"], 0)

        ref_closed, ref_current = reference_slice(self.ticks, TF_SECONDS, {})

        for tf in TF_SECONDS:
            self.assertEqual(hist[tf], ref_closed[tf],
                             "%s: свечи хаба разошлись с эталоном server.py" % tf)
            self.assertEqual(current[tf], ref_current[tf],
                             "%s: живая свеча хаба разошлась с эталоном" % tf)

        self.assertGreater(written, 0, "в SQLite ничего не записалось")

        # То, что легло в БД, обязано совпасть с тем, что хаб держит в памяти.
        conn = init_db(db)
        for tf in ("M1", "M5", "M15"):
            from_db = load_history(conn, PROVIDER, SYMBOL, tf)
            self.assertEqual(len(from_db), len(ref_closed[tf]),
                             "%s: в БД другое число баров" % tf)
            for got, ref in zip(from_db, ref_closed[tf]):
                self.assertEqual(
                    (got["time"], got["open"], got["high"], got["low"], got["close"]),
                    (ref["time"], ref["open"], ref["high"], ref["low"], ref["close"]),
                    "%s: бар в БД не совпал с эталоном" % tf)
        conn.close()


class TestHubWebSocket(unittest.TestCase):
    """Протокол с браузером: history, следом живая свеча тем же requestId."""

    def test_set_tf_returns_history_then_live_candle(self):
        ticks = load_ticks()[:3000]
        if not ticks:
            self.skipTest("нет data/*.csv")

        tmp = tempfile.mkdtemp()
        db  = os.path.join(tmp, "hub.db")

        async def scenario():
            h = HubHarness(db)
            await h.start()

            client = BusClient(PROVIDER, url="ws://127.0.0.1:%d" % h.config["bus_port"])
            task   = asyncio.ensure_future(client.run())
            await asyncio.sleep(0.3)

            client.send_threadsafe(make_instruments(PROVIDER, [
                {"symbol": SYMBOL, "price_decimals": 5, "has_volume": False},
            ]))
            for price, ts in ticks:
                client.send_threadsafe(make_tick(PROVIDER, SYMBOL, ts, price))

            for _ in range(100):
                await asyncio.sleep(0.1)
                if h.hub.ticks_received >= len(ticks):
                    break

            # Прикидываемся браузером.
            url = "ws://127.0.0.1:%d" % h.config["ws_port"]
            got = []
            async with websockets.connect(url) as ws:
                await ws.send(json.dumps({"type": "set_tf", "symbol": SYMBOL,
                                          "tf": "M1", "requestId": 7}))
                for _ in range(3):
                    raw = await asyncio.wait_for(ws.recv(), timeout=3)
                    got.append(json.loads(raw))

            task.cancel()
            await asyncio.sleep(0)
            await h.stop()
            return got

        got = run_async(scenario())

        types = [m["type"] for m in got]
        self.assertEqual(types[0], "instruments", "instruments должны прийти при коннекте")
        self.assertEqual(types[1], "history")
        self.assertEqual(types[2], "update", "живая свеча обязана идти сразу за историей")

        hist, upd = got[1], got[2]
        self.assertEqual(hist["requestId"], 7)
        self.assertEqual(upd["requestId"], 7, "requestId живой свечи должен совпасть")
        self.assertEqual(hist["tf"], "M1")
        self.assertGreater(len(hist["data"]), 0, "история пуста")

        last_closed = hist["data"][-1]
        self.assertEqual(upd["candle"]["open"], last_closed["close"],
                         "живая свеча открылась с разрывом от последней закрытой")


class TestHubBackfill(unittest.TestCase):
    """Фид догружает историю у брокера и шлёт её хабу сообщением candles."""

    def test_backfill_fills_grid_derives_tfs_and_drops_forming_bar(self):
        import time as _time

        now = int(_time.time())

        # M1 брокера: 300 минуток, последняя — ТЕКУЩАЯ минута (ещё не закрыта).
        cur_m1 = (now // 60) * 60
        m1 = [{"time": cur_m1 - 60 * i, "open": 1.17 + i * 1e-4,
               "high": 1.1705 + i * 1e-4, "low": 1.1695 + i * 1e-4,
               "close": 1.17 + i * 1e-4}
              for i in range(299, -1, -1)]

        # H4 по сетке FXCM (01/05/09/13/17/21 UTC → offset 3600), последний бар —
        # текущий бакет, брокер отдаёт его незакрытым.
        cur_h4 = ((now - 3600) // 14400) * 14400 + 3600
        h4 = [{"time": cur_h4 - 14400 * i, "open": 1.16, "high": 1.18,
               "low": 1.15, "close": 1.17} for i in range(9, -1, -1)]

        tmp = tempfile.mkdtemp()
        db  = os.path.join(tmp, "hub.db")

        async def scenario():
            h = HubHarness(db)
            await h.start()
            client = BusClient(PROVIDER, url="ws://127.0.0.1:%d" % h.config["bus_port"])
            task   = asyncio.ensure_future(client.run())
            await asyncio.sleep(0.3)

            client.send_threadsafe(make_candles(PROVIDER, SYMBOL, "M1", m1))
            client.send_threadsafe(make_candles(PROVIDER, SYMBOL, "H4", h4))
            await asyncio.sleep(1.0)

            # Живой тик поверх догруженной истории.
            client.send_threadsafe(make_tick(PROVIDER, SYMBOL, now, 1.2000))
            await asyncio.sleep(0.5)

            builder = h.hub._builders[(PROVIDER, SYMBOL)]
            out = {
                "offsets": builder.offsets,
                "hist":    {tf: list(bars) for tf, bars
                            in h.hub._history[(PROVIDER, SYMBOL)].items()},
                "current": {tf: builder.current(tf) for tf in ("M1", "M5", "H4")},
            }
            task.cancel()
            await asyncio.sleep(0)
            await h.stop()
            return out

        out = run_async(scenario())

        # Смещение сетки выучено из брокерских баров.
        self.assertEqual(out["offsets"].get("H4"), 3600,
                         "смещение сетки H4 не выучено из пачки брокера")

        # Незакрытые бары текущего бакета в историю не попали.
        m1_times = [c["time"] for c in out["hist"]["M1"]]
        self.assertNotIn(cur_m1, m1_times, "текущая (незакрытая) минутка попала в историю")
        self.assertIn(cur_m1 - 60, m1_times, "предыдущая минутка потерялась")

        h4_times = [c["time"] for c in out["hist"].get("H4", [])]
        self.assertNotIn(cur_h4, h4_times, "незакрытый бар H4 попал в закрытые")

        # Производные ТФ выведены из M1 (брокер их не отдаёт).
        for tf in ("M3", "M5", "M15"):
            self.assertGreater(len(out["hist"].get(tf, [])), 0,
                               "%s не выведен из M1" % tf)
            for c in out["hist"][tf]:
                self.assertEqual(c["time"] % TF_SECONDS[tf], 0,
                                 "%s вне сетки" % tf)

        # Живая свеча собрана из догруженной истории, и тик не открыл её заново.
        self.assertIsNotNone(out["current"]["M1"])
        self.assertEqual(out["current"]["M1"]["open"], out["hist"]["M1"][-1]["close"],
                         "живая M1 открылась с разрывом от догруженной истории")
        self.assertEqual(out["current"]["M1"]["close"], 1.2000, "тик не долетел")
        self.assertEqual(out["current"]["M1"]["high"], 1.2000)

        # В БД записано ровно то же.
        conn = init_db(db)
        from_db = load_history(conn, PROVIDER, SYMBOL, "M1")
        conn.close()
        self.assertEqual([c["time"] for c in from_db], m1_times,
                         "в БД другая история M1")


class TestHubRestart(unittest.TestCase):
    """Рестарт хаба: живая свеча восстанавливается из БД, без разрыва."""

    def test_forming_candle_survives_restart(self):
        ticks = load_ticks()
        if not ticks:
            self.skipTest("нет data/*.csv")

        # Берём тики так, чтобы «сейчас» приходилось на середину минуты: свеча
        # текущего бакета должна быть НЕЗАКРЫТОЙ на момент рестарта.
        import time as _time
        now  = int(_time.time())
        base = (now // 60) * 60
        # Переклеиваем реальные цены на свежие времена: -20 мин от текущей минуты.
        prices = [p for p, _ in ticks[:1200]]
        live   = [(p, base - 1200 + i) for i, p in enumerate(prices)]

        tmp = tempfile.mkdtemp()
        db  = os.path.join(tmp, "hub.db")

        async def first_run():
            h = HubHarness(db)
            await h.start()
            client = BusClient(PROVIDER, url="ws://127.0.0.1:%d" % h.config["bus_port"])
            task   = asyncio.ensure_future(client.run())
            await asyncio.sleep(0.3)

            for price, ts in live:
                client.send_threadsafe(make_tick(PROVIDER, SYMBOL, ts, price))
            for _ in range(100):
                await asyncio.sleep(0.1)
                if h.hub.ticks_received >= len(live):
                    break

            await h.drain_db()
            builder = h.hub._builders[(PROVIDER, SYMBOL)]
            m1_hist = list(h.hub._history[(PROVIDER, SYMBOL)].get("M1", []))

            task.cancel()
            await asyncio.sleep(0)
            await h.stop()
            return m1_hist

        async def second_run():
            # Новый Hub на той же БД — как после systemctl restart.
            h = HubHarness(db)
            await h.start()
            builder = h.hub._builders[(PROVIDER, SYMBOL)]
            current = {tf: builder.current(tf) for tf in ("M1", "M5", "M15", "H1")}
            hist_m1 = list(h.hub._history[(PROVIDER, SYMBOL)].get("M1", []))
            await h.stop()
            return current, hist_m1

        m1_before = run_async(first_run())
        current, m1_after = run_async(second_run())

        self.assertGreater(len(m1_before), 0, "первый прогон не закрыл ни одной M1")

        # Главное: после рестарта живая свеча существует и открыта от close
        # последней закрытой — тот самый разрыв, который чинили в 50e63ac.
        self.assertIsNotNone(current["M1"], "после рестарта нет живой свечи M1")
        last_closed = m1_after[-1]
        self.assertEqual(current["M1"]["open"], last_closed["close"],
                         "M1 после рестарта открылась с разрывом")
        self.assertGreaterEqual(current["M1"]["high"], current["M1"]["open"])
        self.assertLessEqual(current["M1"]["low"], current["M1"]["open"])

        # Бар текущего бакета не должен остаться и в истории — иначе фронт
        # получит его дважды.
        forming_time = current["M1"]["time"]
        self.assertNotIn(forming_time, [c["time"] for c in m1_after],
                         "бар текущего бакета продублирован в истории")


if __name__ == "__main__":
    unittest.main(verbosity=2)
