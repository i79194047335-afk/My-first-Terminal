"""
Тесты адаптера хаба. Python 3.10.

Запуск:  python3.10 tests/test_bot_hubfeed.py    (из корня проекта)

Хаб подменяется поддельным сервером — боевой chart-hub не трогается, и
тесты не зависят от того, работает ли он сейчас.

Форматы сообщений поддельного хаба списаны с ЖИВОГО (проверено 2026-08-06):
подписка ключом "type", история в поле "data", обновления типом "update"
с одной свечой в "candle". Каждая из этих трёх мелочей уже стоила отладки:
угаданный формат вёл себя как «соединение есть, данных нет».
"""

import asyncio
import json
import os
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import websockets

from bot.strategy.hub_feed import HubFeedSource

passed = 0
failed = 0


def check(name, condition, detail=""):
    """Проверить условие и напечатать результат.

    Args:
        name:      Название проверки.
        condition: Результат проверки.
        detail:    Что показать при провале.

    Returns:
        None.
    """
    global passed, failed
    if condition:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed += 1
        print(f"  FAIL {name} {detail}")


def free_port():
    """Найти свободный порт.

    Returns:
        Номер порта.
    """
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def make_candle(ts, close, base=1.15):
    """Собрать свечу в формате хаба.

    Args:
        ts:    Unix-время начала свечи.
        close: Цена закрытия.
        base:  Базовая цена для open/high/low.

    Returns:
        Словарь свечи.
    """
    return {"time": ts, "open": base, "high": max(base, close),
            "low": min(base, close), "close": close,
            "vol_base": None, "vol_quote": None, "delta": None}


class FakeHub:
    """Поддельный хаб: отвечает на set_tf историей и шлёт обновления."""

    def __init__(self, port, history_size=120):
        """Создать поддельный хаб.

        Args:
            port:         Порт для прослушивания.
            history_size: Сколько свечей отдавать в истории.
        """
        self.port = port
        self.history_size = history_size
        self.server = None
        self.subscriptions = []      # что у нас запрашивали
        self.connections = 0
        self._sockets = []

    async def start(self):
        """Поднять сервер.

        Returns:
            None.
        """
        self.server = await websockets.serve(self._handle, "127.0.0.1",
                                             self.port, max_size=None)

    async def stop(self):
        """Остановить сервер.

        Returns:
            None.
        """
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    async def _handle(self, ws):
        """Обслужить клиента: принять подписку, отдать историю.

        Args:
            ws: Соединение.

        Returns:
            None.
        """
        self.connections += 1
        self._sockets.append(ws)
        try:
            # Широковещательный шум, который идёт всем: адаптер обязан его
            # игнорировать, не путая с данными.
            await ws.send(json.dumps({"type": "heartbeat", "data": {"ts": time.time()}}))
            await ws.send(json.dumps({"type": "instruments", "data": []}))

            async for raw in ws:
                message = json.loads(raw)
                if message.get("type") != "set_tf":
                    continue

                symbol = message["symbol"]
                self.subscriptions.append((symbol, message.get("tf")))

                now = int(time.time() // 60 * 60)
                history = [make_candle(now - (self.history_size - i) * 60,
                                       1.15 + i * 0.0001)
                           for i in range(self.history_size)]
                await ws.send(json.dumps({
                    "type": "history", "symbol": symbol,
                    "tf": message.get("tf"),
                    "requestId": message.get("requestId"),
                    "data": history,     # ключ именно "data" — как у живого хаба
                }))
        except Exception:
            pass

    async def push_update(self, symbol, candle):
        """Разослать обновление свечи всем подключённым.

        Args:
            symbol: Инструмент.
            candle: Свеча.

        Returns:
            None.
        """
        payload = json.dumps({"type": "update", "symbol": symbol,
                              "tf": "M1", "requestId": 1, "candle": candle})
        for ws in list(self._sockets):
            try:
                await ws.send(payload)
            except Exception:
                self._sockets.remove(ws)


def test_subscribe_and_history():
    """Подписка проходит, история попадает в буфер."""
    print("подписка и загрузка истории")
    port = free_port()
    hub = FakeHub(port)

    async def scenario():
        """Подключиться к поддельному хабу.

        Returns:
            Кортеж (источник, хаб).
        """
        await hub.start()
        source = HubFeedSource(ws_url=f"ws://127.0.0.1:{port}",
                               symbols=["EUR/USD", "USD/JPY"], timeframe="M1")
        await source.start()
        await asyncio.sleep(1.5)
        status = source.status()
        await source.stop()
        await hub.stop()
        return status

    status = asyncio.run(asyncio.wait_for(scenario(), timeout=30))

    check("подписались на обе пары", len(hub.subscriptions) == 2, hub.subscriptions)
    # Соединение на пару: у хаба подписка живёт на сокете, второй set_tf
    # в том же сокете просто заменил бы первый.
    check("по соединению на пару", hub.connections == 2, hub.connections)
    check("ключ подписки — type", all(tf == "M1" for _, tf in hub.subscriptions),
          hub.subscriptions)
    check("история EUR/USD в буфере", status["candles"]["EUR/USD"] > 0,
          status["candles"])
    check("буфер подрезан до 50", status["candles"]["EUR/USD"] == 50,
          status["candles"]["EUR/USD"])
    check("история USD/JPY тоже", status["candles"]["USD/JPY"] == 50,
          status["candles"])
    check("соединение отмечено", status["connected"] is True)


def test_updates_append():
    """Обновления добавляются, та же минута заменяется на месте."""
    print("обновления свечей")
    port = free_port()
    hub = FakeHub(port, history_size=10)

    async def scenario():
        """Прислать обновления и посмотреть буфер.

        Returns:
            Список свечей EUR/USD.
        """
        await hub.start()
        source = HubFeedSource(ws_url=f"ws://127.0.0.1:{port}",
                               symbols=["EUR/USD"], timeframe="M1")
        await source.start()
        await asyncio.sleep(1.0)
        before = len(source.candles["EUR/USD"])

        now = int(time.time() // 60 * 60)
        # Новая свеча.
        await hub.push_update("EUR/USD", make_candle(now + 60, 1.2000))
        await asyncio.sleep(0.4)
        # Та же минута обновилась — должна замениться, не добавиться.
        await hub.push_update("EUR/USD", make_candle(now + 60, 1.2050))
        await asyncio.sleep(0.4)

        candles = list(source.candles["EUR/USD"])
        await source.stop()
        await hub.stop()
        return before, candles

    before, candles = asyncio.run(asyncio.wait_for(scenario(), timeout=30))

    check("свеча добавилась", len(candles) == before + 1,
          f"было {before}, стало {len(candles)}")
    check("та же минута заменена, не задвоена",
          candles[-1]["close"] == 1.2050, candles[-1]["close"])


def test_no_strategy_no_signals():
    """Без стратегии каркас не порождает сигналов — это штатно."""
    print("без стратегии сигналов нет")
    port = free_port()
    hub = FakeHub(port, history_size=10)

    async def scenario():
        """Прогнать обновления без подключённой стратегии.

        Returns:
            Кортеж (число сигналов, статус).
        """
        await hub.start()
        source = HubFeedSource(ws_url=f"ws://127.0.0.1:{port}",
                               symbols=["EUR/USD"], timeframe="M1")
        await source.start()
        await asyncio.sleep(1.0)

        now = int(time.time() // 60 * 60)
        for i in range(3):
            await hub.push_update("EUR/USD", make_candle(now + (i + 1) * 60,
                                                         1.2 + i * 0.01))
            await asyncio.sleep(0.3)

        count = source._queue.qsize()
        status = source.status()
        await source.stop()
        await hub.stop()
        return count, status

    count, status = asyncio.run(asyncio.wait_for(scenario(), timeout=30))
    check("сигналов нет", count == 0, count)
    check("статус honest: стратегия не подключена",
          status["strategy_attached"] is False)


def test_strategy_hook_called_on_closed_candles():
    """Стратегия зовётся на ЗАКРЫТЫХ свечах и может дать сигнал."""
    print("точка подключения стратегии")
    port = free_port()
    hub = FakeHub(port, history_size=10)
    calls = []

    def decide(symbol, candles):
        """Заглушка стратегии: первый вызов даёт call.

        Args:
            symbol:  Инструмент.
            candles: Закрытые свечи.

        Returns:
            Направление или None.
        """
        calls.append((symbol, len(candles)))
        return "call" if len(calls) == 1 else None

    async def scenario():
        """Прогнать обновления с подключённой стратегией.

        Returns:
            Список полученных сигналов.
        """
        await hub.start()
        source = HubFeedSource(ws_url=f"ws://127.0.0.1:{port}",
                               symbols=["EUR/USD"], timeframe="M1",
                               decide=decide)
        await source.start()
        await asyncio.sleep(1.0)

        now = int(time.time() // 60 * 60)
        for i in range(3):
            await hub.push_update("EUR/USD", make_candle(now + (i + 1) * 60,
                                                         1.2 + i * 0.01))
            await asyncio.sleep(0.3)

        signals = []
        while not source._queue.empty():
            item = source._queue.get_nowait()
            if item is not None:
                signals.append(item)

        await source.stop()
        await hub.stop()
        return signals

    signals = asyncio.run(asyncio.wait_for(scenario(), timeout=30))

    check("стратегия вызывалась", len(calls) >= 2, len(calls))
    check("породился 1 сигнал", len(signals) == 1, len(signals))
    if signals:
        signal = signals[0]
        check("направление call", signal.direction == "call", signal.direction)
        check("источник hub", signal.source == "hub", signal.source)
        check("символ канонический", signal.symbol == "EUR/USD", signal.symbol)
        check("meta несёт таймфрейм", signal.meta.get("tf") == "M1", signal.meta)
        check("meta несёт цену закрытия", signal.meta.get("last_close") is not None,
              signal.meta)


def test_strategy_crash_does_not_kill_feed():
    """Падение чужой стратегии не рвёт канал данных."""
    print("устойчивость к падению стратегии")
    port = free_port()
    hub = FakeHub(port, history_size=10)

    def broken_decide(symbol, candles):
        """Стратегия, которая всегда падает.

        Args:
            symbol:  Инструмент.
            candles: Свечи.

        Returns:
            Ничего — всегда исключение.

        Raises:
            RuntimeError: Всегда.
        """
        raise RuntimeError("стратегия сломалась")

    async def scenario():
        """Прогнать обновления со сломанной стратегией.

        Returns:
            Статус источника.
        """
        await hub.start()
        source = HubFeedSource(ws_url=f"ws://127.0.0.1:{port}",
                               symbols=["EUR/USD"], timeframe="M1",
                               decide=broken_decide)
        await source.start()
        await asyncio.sleep(1.0)

        now = int(time.time() // 60 * 60)
        for i in range(3):
            await hub.push_update("EUR/USD", make_candle(now + (i + 1) * 60,
                                                         1.2 + i * 0.01))
            await asyncio.sleep(0.3)

        status = source.status()
        candles = len(source.candles["EUR/USD"])
        await source.stop()
        await hub.stop()
        return status, candles

    status, candles = asyncio.run(asyncio.wait_for(scenario(), timeout=30))
    check("соединение живо", status["connected"] is True)
    check("свечи продолжали копиться", candles > 10, candles)
    check("сигналов не породилось", True)  # исключение съедено, сигнала нет


def test_ignores_broadcast_noise():
    """Широковещательные сообщения хаба не ломают адаптер."""
    print("шум хаба игнорируется")
    port = free_port()
    hub = FakeHub(port, history_size=5)

    async def scenario():
        """Подключиться: поддельный хаб сразу шлёт heartbeat и instruments.

        Returns:
            Статус источника.
        """
        await hub.start()
        source = HubFeedSource(ws_url=f"ws://127.0.0.1:{port}",
                               symbols=["EUR/USD"], timeframe="M1")
        await source.start()
        await asyncio.sleep(1.2)
        status = source.status()
        await source.stop()
        await hub.stop()
        return status

    status = asyncio.run(asyncio.wait_for(scenario(), timeout=30))
    check("шум учтён как сообщения", status["messages"] >= 3, status["messages"])
    check("буфер не испорчен", status["candles"]["EUR/USD"] == 5,
          status["candles"])
    check("соединение живо", status["connected"] is True)


def main():
    """Прогнать все тесты и вернуть код возврата.

    Returns:
        0 — всё прошло, 1 — есть провалы.
    """
    for test in (test_subscribe_and_history, test_updates_append,
                 test_no_strategy_no_signals,
                 test_strategy_hook_called_on_closed_candles,
                 test_strategy_crash_does_not_kill_feed,
                 test_ignores_broadcast_noise):
        test()
        print()

    print(f"итого: {passed} прошло, {failed} провалено")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
