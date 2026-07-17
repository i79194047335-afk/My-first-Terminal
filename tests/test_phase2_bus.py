"""
Tests for core/bus.py — внутренняя шина фиды → хаб (Фаза 2.3).

Гоняются НА ОБОИХ питонах, и это не перестраховка: фид FXCM живёт на 3.7
(websockets 11), хаб — на 3.10 (websockets 16). Реализации библиотеки разные,
и часть багов видна только на одной из версий.

Run:  python3.7  tests/test_phase2_bus.py
      python3.10 tests/test_phase2_bus.py
"""

import asyncio
import json
import os
import socket
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import websockets

from core.bus import (BusClient, BusError, BusServer, make_instruments,
                      make_tick, validate)


def free_port():
    """Pick a free TCP port.

    Боевой 8766 в тестах не занимаем: на машине может крутиться живой хаб.

    Args:
        None.

    Returns:
        Int port number.
    """
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def run_async(coro):
    """Run a coroutine on a fresh event loop (py3.7 + py3.10 compatible).

    Args:
        coro: Coroutine to run.

    Returns:
        Whatever the coroutine returns.
    """
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


class TestValidate(unittest.TestCase):
    """Контракт шины: что проходит, что нет."""

    def test_good_tick(self):
        msg = make_tick("fxcm", "EUR/USD", 1784036225, 1.1735)
        self.assertIs(validate(msg), msg)
        self.assertIsNone(msg["size"], "у FXCM объёма нет → size = null")

    def test_good_tick_with_size(self):
        validate(make_tick("lighter", "BTC", 1784036225, 64132.6, 0.0123))

    def test_good_instruments(self):
        validate(make_instruments("lighter", [{"symbol": "BTC",
                                               "price_decimals": 1}]))

    def test_milliseconds_rejected(self):
        """Главная защита контракта: мс внутрь шины не пускаем."""
        ms = make_tick("lighter", "BTC", 1784036225000, 64132.6)
        with self.assertRaises(BusError) as ctx:
            validate(ms)
        self.assertIn("МИЛЛИСЕКУНД", str(ctx.exception))

    def test_bool_is_not_a_number(self):
        """isinstance(True, int) == True — цена True не должна стать 1.0."""
        with self.assertRaises(BusError):
            validate(make_tick("fxcm", "EUR/USD", 1784036225, True))
        with self.assertRaises(BusError):
            validate(make_tick("fxcm", "EUR/USD", True, 1.1735))

    def test_bad_values(self):
        cases = [
            make_tick("fxcm", "", 1784036225, 1.17),           # пустой symbol
            make_tick("", "EUR/USD", 1784036225, 1.17),        # пустой provider
            make_tick("fxcm", "EUR/USD", 0, 1.17),             # ts = 0
            make_tick("fxcm", "EUR/USD", -5, 1.17),            # ts < 0
            make_tick("fxcm", "EUR/USD", 1784036225, 0),       # price = 0
            make_tick("fxcm", "EUR/USD", 1784036225, -1.17),   # price < 0
            make_tick("fxcm", "EUR/USD", 1784036225, 1.17, -1),  # size < 0
            make_tick("fxcm", "EUR/USD", 1784036225, "1.17"),  # price строкой
            {"type": "trade", "provider": "fxcm"},             # чужой type
            {"type": "instruments", "provider": "x", "data": {}},        # data не список
            {"type": "instruments", "provider": "x", "data": [{"s": 1}]},  # без symbol
            "не словарь",
        ]
        for msg in cases:
            with self.assertRaises(BusError, msg="прошло, а не должно: %r" % (msg,)):
                validate(msg)


class TestBusRoundTrip(unittest.TestCase):
    """Сквозная проверка через настоящий сокет."""

    def test_tick_from_daemon_thread_reaches_server(self):
        """Тики FXCM приходят в демон-потоке — шина обязана это выдержать."""
        got = []

        async def scenario():
            port = free_port()
            server = BusServer(got.append, port=port)
            client = BusClient("fxcm", url="ws://127.0.0.1:%d" % port)

            await server.start()
            task = asyncio.ensure_future(client.run())
            await asyncio.sleep(0.2)

            def feed():
                for i in range(50):
                    client.send_threadsafe(
                        make_tick("fxcm", "EUR/USD", 1784036225 + i, 1.1735 + i * 1e-5))

            t = threading.Thread(target=feed, daemon=True)
            t.start()
            t.join()
            await asyncio.sleep(0.5)

            stats = (server.stats, client.stats)
            task.cancel()
            await asyncio.sleep(0)
            await server.close()
            return stats

        srv, cli = run_async(scenario())

        self.assertEqual(len(got), 50, "дошли не все тики")
        self.assertEqual(srv["received"], 50)
        self.assertEqual(srv["dropped"], 0)
        self.assertEqual(cli["sent"], 50)
        self.assertEqual(cli["dropped"], 0)
        self.assertEqual([m["ts"] for m in got],
                         [1784036225 + i for i in range(50)],
                         "порядок тиков нарушен")

    def test_send_before_run_is_dropped_not_crashed(self):
        """До старта run() очереди нет — сообщение теряется, но фид не падает."""
        client = BusClient("fxcm")
        ok = client.send_threadsafe(make_tick("fxcm", "EUR/USD", 1784036225, 1.17))
        self.assertFalse(ok)
        self.assertEqual(client.stats["dropped"], 1)

    def test_invalid_message_never_reaches_the_wire(self):
        client = BusClient("lighter")
        ok = client.send_threadsafe(make_tick("lighter", "BTC", 1784036225000, 64000.0))
        self.assertFalse(ok, "тик в миллисекундах не должен уходить в шину")
        self.assertEqual(client.stats["sent"], 0)


class TestBusResilience(unittest.TestCase):
    """Шина не должна разваливаться от кривых данных и обрывов."""

    def test_garbage_does_not_kill_the_server(self):
        """Битый JSON и мусорное сообщение: сервер жив, соединение цело."""
        got = []

        async def scenario():
            port = free_port()
            server = BusServer(got.append, port=port)
            await server.start()

            async with websockets.connect("ws://127.0.0.1:%d" % port) as ws:
                await ws.send("{это не json")
                await ws.send(json.dumps({"type": "trade", "provider": "x"}))
                await ws.send(json.dumps(make_tick("fxcm", "EUR/USD", 1784036225000, 1.17)))
                # …а следом валидный тик — он обязан дойти
                await ws.send(json.dumps(make_tick("fxcm", "EUR/USD", 1784036225, 1.1735)))
                await asyncio.sleep(0.3)

            stats = server.stats
            await server.close()
            return stats

        srv = run_async(scenario())

        self.assertEqual(len(got), 1, "валидный тик после мусора не дошёл")
        self.assertEqual(srv["received"], 1)
        self.assertEqual(srv["dropped"], 3)

    def test_consumer_exception_does_not_kill_the_server(self):
        """Падение хаба на одном сообщении не рвёт поток данных."""
        seen = []

        def on_message(msg):
            seen.append(msg)
            if len(seen) == 1:
                raise RuntimeError("хаб споткнулся")

        async def scenario():
            port = free_port()
            server = BusServer(on_message, port=port)
            await server.start()

            async with websockets.connect("ws://127.0.0.1:%d" % port) as ws:
                for i in range(3):
                    await ws.send(json.dumps(
                        make_tick("fxcm", "EUR/USD", 1784036225 + i, 1.17)))
                await asyncio.sleep(0.3)

            stats = server.stats
            await server.close()
            return stats

        srv = run_async(scenario())
        self.assertEqual(len(seen), 3, "после исключения в хабе поток должен продолжиться")
        self.assertEqual(srv["dropped"], 1)

    def test_client_reconnects_and_delivers(self):
        """Хаб перезапустили — фид переподключился, накопленное дошло."""
        got = []

        async def scenario():
            port = free_port()
            server1 = BusServer(got.append, port=port)
            client  = BusClient("fxcm", url="ws://127.0.0.1:%d" % port)

            await server1.start()
            task = asyncio.ensure_future(client.run())
            await asyncio.sleep(0.2)

            client.send_threadsafe(make_tick("fxcm", "EUR/USD", 1784036225, 1.17))
            await asyncio.sleep(0.2)

            # Хаб падает
            await server1.close()
            await asyncio.sleep(0.2)

            # Тик во время простоя — копится в очереди
            client.send_threadsafe(make_tick("fxcm", "EUR/USD", 1784036226, 1.18))

            # Хаб поднимается заново на том же порту
            server2 = BusServer(got.append, port=port)
            await server2.start()
            await asyncio.sleep(3.0)  # backoff 1→2 c

            stats = client.stats
            task.cancel()
            await asyncio.sleep(0)
            await server2.close()
            return stats

        cli = run_async(scenario())

        self.assertEqual(len(got), 2, "тик из простоя не доехал после переподключения")
        self.assertEqual(got[1]["ts"], 1784036226)
        self.assertGreaterEqual(cli["reconnects"], 1)
        self.assertTrue(cli["connected"])

    def test_queue_overflow_drops_oldest(self):
        """Переполнение: теряем СТАРЫЕ тики, а не свежие и не память."""
        got = []

        async def scenario():
            port = free_port()
            client = BusClient("fxcm", url="ws://127.0.0.1:%d" % port, max_queue=5)

            # Хаба нет — очередь копится и переполняется.
            task = asyncio.ensure_future(client.run())
            await asyncio.sleep(0.1)

            for i in range(20):
                client.send_threadsafe(
                    make_tick("fxcm", "EUR/USD", 1784036225 + i, 1.17))
            await asyncio.sleep(0.1)

            queued = client.stats["queued"]
            dropped = client.stats["dropped"]

            # Теперь поднимаем хаб — должны прийти только 5 ПОСЛЕДНИХ тиков.
            server = BusServer(got.append, port=port)
            await server.start()
            await asyncio.sleep(3.0)

            task.cancel()
            await asyncio.sleep(0)
            await server.close()
            return queued, dropped

        queued, dropped = run_async(scenario())

        self.assertEqual(queued, 5, "очередь должна упереться в max_queue")
        self.assertEqual(dropped, 15, "лишние 15 тиков должны быть отброшены")
        self.assertEqual(len(got), 5)
        self.assertEqual([m["ts"] for m in got],
                         [1784036225 + i for i in range(15, 20)],
                         "выжить должны САМЫЕ СВЕЖИЕ тики")


if __name__ == "__main__":
    unittest.main(verbosity=2)
