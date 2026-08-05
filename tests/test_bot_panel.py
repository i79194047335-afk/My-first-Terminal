"""
Тесты панели наблюдения. Python 3.10.

Запуск:  python3.10 tests/test_bot_panel.py    (из корня проекта)

Панель поднимается на свободном порту, к ней подключается настоящий
WebSocket-клиент. Площадка при этом подменена заглушкой — сеть наружу не
идёт.

Отдельное внимание — двум вещам, которые легко испортить незаметно:
  * страница отдаётся НЕ пустой (на этом уже спотыкались: websockets
    принимает тело ответа только конструктором, и присваивание
    response.body молча ничего не делало — панель открывалась пустой);
  * панель слушает только 127.0.0.1: она умеет открывать сделки, и
    открывать её наружу без пароля нельзя.
"""

import asyncio
import json
import os
import socket
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import websockets

from bot.config import BotConfig, RiskConfig
from bot.journal import Journal, kill_switch_active, release_kill_switch
from bot.panel import Panel
from bot.risk import RiskManager
from bot.strategy.manual import ManualSource

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


class FakeQuotes:
    """Заглушка котировок."""

    fresh = True

    def mid(self, symbol):
        """Вернуть цену.

        Args:
            symbol: Инструмент.

        Returns:
            Цена.
        """
        return 157.666


def make_panel(mode="dry"):
    """Собрать панель на свободном порту.

    Args:
        mode: Режим бота.

    Returns:
        Кортеж (Panel, Journal, ManualSource, RiskManager, порт, пути).
    """
    handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    handle.close()
    journal = Journal(handle.name)

    stop_path = os.path.join(tempfile.gettempdir(),
                             f"panel_STOP_{os.getpid()}_{int(time.time()*1000)}")
    port = free_port()
    config = BotConfig(
        mode=mode, symbol_whitelist=["USD/JPY", "EUR/USD"],
        default_investment=1, panel_port=port, stop_file=stop_path,
        db_path=handle.name, risk=RiskConfig(allowed_hours=[]),
    )

    manual = ManualSource(default_symbol="USD/JPY")
    risk = RiskManager(config, journal)
    panel = Panel(config=config, journal=journal, risk=risk,
                  manual_source=manual, quotes=FakeQuotes())
    return panel, journal, manual, risk, port, (handle.name, stop_path)


def cleanup(paths):
    """Удалить временные файлы.

    Args:
        paths: Кортеж (путь к БД, путь к kill-switch).

    Returns:
        None.
    """
    db_path, stop_path = paths
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(db_path + suffix)
        except OSError:
            pass
    try:
        os.unlink(stop_path)
    except OSError:
        pass


def test_http_serves_page():
    """Страница панели отдаётся целиком, а не пустым телом."""
    print("HTTP отдаёт страницу")
    panel, journal, manual, risk, port, paths = make_panel()

    async def scenario():
        """Поднять панель и запросить страницу по HTTP.

        Returns:
            Кортеж (код ответа, тело).
        """
        await panel.start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET / HTTP/1.1\r\nHost: localhost\r\n"
                         b"Connection: close\r\n\r\n")
            await writer.drain()
            raw = await asyncio.wait_for(reader.read(200000), timeout=10)
            writer.close()
            return raw
        finally:
            await panel.stop()

    raw = asyncio.run(asyncio.wait_for(scenario(), timeout=30))
    text = raw.decode("utf-8", errors="replace")

    try:
        check("код 200", "200" in text.split("\r\n")[0], text.split("\r\n")[0])
        # Главная проверка: тело НЕ пустое. Ровно здесь был баг.
        check("тело не пустое", len(raw) > 5000, len(raw))
        check("это HTML панели", "<title>Бот intrade.bar" in text)
        check("тип содержимого", "text/html" in text)
        check("есть кнопка CALL", "CALL" in text)
        check("есть кнопка STOP", "STOP" in text)
    finally:
        journal.close()
        cleanup(paths)


def test_http_404():
    """Несуществующий файл даёт 404, а не пустой 200."""
    print("HTTP 404 на неизвестный путь")
    panel, journal, manual, risk, port, paths = make_panel()

    async def scenario():
        """Запросить несуществующий файл.

        Returns:
            Сырой ответ.
        """
        await panel.start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET /nope.js HTTP/1.1\r\nHost: localhost\r\n"
                         b"Connection: close\r\n\r\n")
            await writer.drain()
            raw = await asyncio.wait_for(reader.read(65536), timeout=10)
            writer.close()
            return raw
        finally:
            await panel.stop()

    raw = asyncio.run(asyncio.wait_for(scenario(), timeout=30))
    try:
        check("код 404", b"404" in raw.split(b"\r\n")[0], raw.split(b"\r\n")[0])
    finally:
        journal.close()
        cleanup(paths)


def test_websocket_state():
    """По WebSocket приходит полный срез состояния."""
    print("состояние по WebSocket")
    panel, journal, manual, risk, port, paths = make_panel()

    async def scenario():
        """Подключиться и получить первый срез.

        Returns:
            Разобранное состояние.
        """
        await panel.start()
        try:
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                return json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        finally:
            await panel.stop()

    state = asyncio.run(asyncio.wait_for(scenario(), timeout=30))
    try:
        check("тип сообщения", state["type"] == "state", state.get("type"))
        check("режим передан", state["mode"] == "dry", state["mode"])
        check("инструменты переданы", state["symbols"] == ["USD/JPY", "EUR/USD"])
        check("сводка ограничителей есть", state["risk"] is not None)
        check("статистика есть", "stats" in state)
        check("состояние окна есть", "hour_edge" in state)
        check("kill-switch виден", state["kill_switch"] is False)
        check("список сделок есть", isinstance(state["trades"], list))
    finally:
        journal.close()
        cleanup(paths)


def test_manual_button_fires_signal():
    """Кнопка Call порождает сигнал ручного источника."""
    print("кнопка CALL порождает сигнал")
    panel, journal, manual, risk, port, paths = make_panel()

    async def scenario():
        """Нажать CALL и забрать сигнал из источника.

        Returns:
            Кортеж (ответ панели, полученный сигнал).
        """
        await panel.start()
        await manual.start()
        try:
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                await asyncio.wait_for(ws.recv(), timeout=10)  # первый срез
                await ws.send(json.dumps({"cmd": "call", "symbol": "EUR/USD",
                                          "amount": 3}))
                reply = None
                for _ in range(8):
                    message = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                    if message.get("type") == "reply":
                        reply = message
                        break
                signal = await asyncio.wait_for(manual.__anext__(), timeout=5)
                return reply, signal
        finally:
            await panel.stop()

    reply, signal = asyncio.run(asyncio.wait_for(scenario(), timeout=40))
    try:
        check("панель ответила ok", reply and reply["status"] == "ok", reply)
        check("направление call", signal.direction == "call", signal.direction)
        check("инструмент из формы", signal.symbol == "EUR/USD", signal.symbol)
        check("ставка из формы", signal.amount == 3, signal.amount)
        check("источник manual", signal.source == "manual", signal.source)
        check("помечен как кнопка панели",
              "кнопка" in signal.meta.get("note", ""), signal.meta)
    finally:
        journal.close()
        cleanup(paths)


def test_stop_and_resume():
    """Кнопки STOP и «снять стоп» управляют kill-switch."""
    print("кнопки STOP и снятия стопа")
    panel, journal, manual, risk, port, paths = make_panel()
    stop_path = paths[1]

    async def scenario():
        """Нажать STOP, затем снять.

        Returns:
            Кортеж (состояние после стопа, состояние после снятия).
        """
        await panel.start()
        try:
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                await asyncio.wait_for(ws.recv(), timeout=10)

                await ws.send(json.dumps({"cmd": "stop"}))
                for _ in range(8):
                    message = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                    if message.get("type") == "reply":
                        break
                after_stop = kill_switch_active(stop_path)

                await ws.send(json.dumps({"cmd": "resume"}))
                for _ in range(8):
                    message = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                    if message.get("type") == "reply":
                        break
                after_resume = kill_switch_active(stop_path)
                return after_stop, after_resume
        finally:
            await panel.stop()

    after_stop, after_resume = asyncio.run(asyncio.wait_for(scenario(), timeout=40))
    try:
        check("STOP взвёл kill-switch", after_stop is True)
        check("снятие убрало kill-switch", after_resume is False)

        risks = [e["message"] for e in journal.recent_events(kind="risk")]
        check("нажатие STOP записано в журнал",
              any("STOP" in message for message in risks), risks)
    finally:
        release_kill_switch(stop_path)
        journal.close()
        cleanup(paths)


def test_unknown_command():
    """Неизвестная команда получает внятный отказ."""
    print("неизвестная команда")
    panel, journal, manual, risk, port, paths = make_panel()

    async def scenario():
        """Послать чепуху.

        Returns:
            Ответ панели.
        """
        await panel.start()
        try:
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                await asyncio.wait_for(ws.recv(), timeout=10)
                await ws.send(json.dumps({"cmd": "выдумка"}))
                for _ in range(8):
                    message = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                    if message.get("type") == "reply":
                        return message
        finally:
            await panel.stop()

    reply = asyncio.run(asyncio.wait_for(scenario(), timeout=30))
    try:
        check("статус error", reply and reply["status"] == "error", reply)
        check("причина названа", "неизвестная" in (reply or {}).get("message", ""),
              reply)
    finally:
        journal.close()
        cleanup(paths)


def test_listens_locally_only():
    """Панель слушает только 127.0.0.1 — она умеет тратить деньги."""
    print("доступ только локальный")
    panel, journal, manual, risk, port, paths = make_panel()

    async def scenario():
        """Проверить адреса, на которых висит сервер.

        Returns:
            Список адресов.
        """
        await panel.start()
        try:
            return [sock.getsockname()[0] for sock in panel._server.sockets]
        finally:
            await panel.stop()

    addresses = asyncio.run(asyncio.wait_for(scenario(), timeout=30))
    try:
        check("только localhost", all(addr in ("127.0.0.1", "::1")
                                      for addr in addresses), addresses)
        check("не на 0.0.0.0", "0.0.0.0" not in addresses, addresses)
    finally:
        journal.close()
        cleanup(paths)


def main():
    """Прогнать все тесты и вернуть код возврата.

    Returns:
        0 — всё прошло, 1 — есть провалы.
    """
    for test in (test_http_serves_page, test_http_404, test_websocket_state,
                 test_manual_button_fires_signal, test_stop_and_resume,
                 test_unknown_command, test_listens_locally_only):
        test()
        print()

    print(f"итого: {passed} прошло, {failed} провалено")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
