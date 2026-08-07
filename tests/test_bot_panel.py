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

from bot.api.models import AccountProfile, Balance, PlatformError
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


def make_panel(mode="dry", client=None, token=None):
    """Собрать панель на свободном порту.

    Args:
        mode:   Режим бота.
        client: Заглушка клиента площадки (для demo/live).
        token:  Токен авторизации панели; None — без пароля.

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
        panel_token=token,
    )

    manual = ManualSource(default_symbol="USD/JPY")
    risk = RiskManager(config, journal)
    panel = Panel(config=config, journal=journal, risk=risk,
                  manual_source=manual, quotes=FakeQuotes(), client=client)
    return panel, journal, manual, risk, port, (handle.name, stop_path)


class FakeClient:
    """Заглушка клиента площадки для панели.

    Считает вызовы ensure_account_mode и умеет отказывать по заказу.
    """

    def __init__(self, profile=None, ensure_error=None):
        """Создать заглушку.

        Args:
            profile:      AccountProfile, отдаваемый profile().
            ensure_error: PlatformError, поднимаемый ensure_account_mode.
        """
        self.profile_state = profile or AccountProfile(
            account="real", trade_type="sprint", currency="usd")
        self.ensure_error = ensure_error
        self.ensure_calls = []

    def profile(self):
        """Вернуть состояние счёта.

        Returns:
            AccountProfile.
        """
        return self.profile_state

    def ensure_account_mode(self, target):
        """Записать цель и вернуть/поднять заданное.

        Args:
            target: Желаемый тип счёта.

        Returns:
            AccountProfile.

        Raises:
            PlatformError: Если настроено.
        """
        self.ensure_calls.append(target)
        if self.ensure_error:
            raise self.ensure_error
        return self.profile_state

    def balance(self):
        """Вернуть демо-баланс.

        Returns:
            Balance.
        """
        return Balance(amount=9363.20, currency="$", raw="9 363,20 $")

    def payout_percent(self, *args, **kwargs):
        """Вернуть процент выплаты.

        Returns:
            Процент.
        """
        return 82


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
        check("есть кнопка счёт-демо", "счёт → демо" in text)
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


def test_profile_in_snapshot():
    """Состояние счёта площадки приходит в кадре панели."""
    print("состояние счёта в кадре")
    client = FakeClient(profile=AccountProfile(
        account="real", trade_type="sprint", currency="usd"))
    panel, journal, manual, risk, port, paths = make_panel(
        mode="demo", client=client)

    async def scenario():
        """Подключиться и получить первый срез.

        Returns:
            Разобранное состояние.
        """
        await panel.start()
        try:
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                for _ in range(10):
                    message = json.loads(
                        await asyncio.wait_for(ws.recv(), timeout=10))
                    if message.get("type") == "state":
                        return message
        finally:
            await panel.stop()

    state = asyncio.run(asyncio.wait_for(scenario(), timeout=40))
    try:
        check("профиль в кадре", state["profile"] is not None, state)
        check("тип счёта настоящий",
              state["profile"]["account"] == "real",
              state["profile"])
        check("валюта настоящая",
              state["profile"]["currency"] == "usd", state["profile"])
        check("touches_platform передан",
              state["touches_platform"] is True, state["touches_platform"])
    finally:
        journal.close()
        cleanup(paths)


def test_ensure_demo_success():
    """Команда «демо» приводит счёт к демо через ensure_account_mode."""
    print("кнопка «счёт → демо»")
    client = FakeClient(profile=AccountProfile(
        account="real", trade_type="sprint", currency="usd"))
    panel, journal, manual, risk, port, paths = make_panel(
        mode="demo", client=client)

    async def scenario():
        """Нажать «демо» и забрать ответ.

        Returns:
            Ответ панели.
        """
        await panel.start()
        try:
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                await asyncio.wait_for(ws.recv(), timeout=10)  # первый срез
                await ws.send(json.dumps({"cmd": "ensure_demo"}))
                for _ in range(8):
                    message = json.loads(
                        await asyncio.wait_for(ws.recv(), timeout=10))
                    if message.get("type") == "reply":
                        return message
        finally:
            await panel.stop()

    reply = asyncio.run(asyncio.wait_for(scenario(), timeout=40))
    try:
        check("ответ ok", reply and reply["status"] == "ok", reply)
        check("вызов именно demo",
              client.ensure_calls == ["demo"], client.ensure_calls)
        check("НЕ существует вызова real",
              "real" not in client.ensure_calls)
        info = [e["message"] for e in journal.recent_events(kind="info")]
        check("приведение записано в журнал",
              any("demo" in m and "счёт" in m for m in info), info)
    finally:
        journal.close()
        cleanup(paths)


def test_ensure_demo_error():
    """Отказ площадки при переключении — внятная ошибка, не тихий успех."""
    print("кнопка «демо» при отказе площадки")
    client = FakeClient(
        profile=AccountProfile(account="real", trade_type="sprint",
                               currency="usd"),
        ensure_error=PlatformError(code="toggle_refused",
                                   message="есть незакрытые сделки"),
    )
    panel, journal, manual, risk, port, paths = make_panel(
        mode="demo", client=client)

    async def scenario():
        """Нажать «демо» и забрать ответ.

        Returns:
            Ответ панели.
        """
        await panel.start()
        try:
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                await asyncio.wait_for(ws.recv(), timeout=10)
                await ws.send(json.dumps({"cmd": "ensure_demo"}))
                for _ in range(8):
                    message = json.loads(
                        await asyncio.wait_for(ws.recv(), timeout=10))
                    if message.get("type") == "reply":
                        return message
        finally:
            await panel.stop()

    reply = asyncio.run(asyncio.wait_for(scenario(), timeout=40))
    try:
        check("ответ error", reply and reply["status"] == "error", reply)
        check("причина названа",
              "не привести" in (reply or {}).get("message", ""), reply)
        risks = [e["message"] for e in journal.recent_events(kind="risk")]
        check("отказ записан в журнал",
              any("счёт" in m for m in risks), risks)
    finally:
        journal.close()
        cleanup(paths)


def test_no_to_real_command():
    """Пути «переключить на реал» в панели НЕТ — неизвестная команда."""
    print("нет команды «на реал»")
    client = FakeClient(profile=AccountProfile(
        account="demo", trade_type="sprint", currency="usd"))
    panel, journal, manual, risk, port, paths = make_panel(
        mode="demo", client=client)

    async def scenario():
        """Послать ensure_real.

        Returns:
            Ответ панели.
        """
        await panel.start()
        try:
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                await asyncio.wait_for(ws.recv(), timeout=10)
                await ws.send(json.dumps({"cmd": "ensure_real"}))
                for _ in range(8):
                    message = json.loads(
                        await asyncio.wait_for(ws.recv(), timeout=10))
                    if message.get("type") == "reply":
                        return message
        finally:
            await panel.stop()

    reply = asyncio.run(asyncio.wait_for(scenario(), timeout=40))
    try:
        check("ответ error", reply and reply["status"] == "error", reply)
        check("команда не узнана",
              "неизвестная" in (reply or {}).get("message", ""), reply)
        check("клиент НЕ вызывался",
              client.ensure_calls == [], client.ensure_calls)
    finally:
        journal.close()
        cleanup(paths)


def test_set_account_two_way():
    """Команда set_account переключает счёт в обе стороны через target.

    Осознанное отличие от защитного «только в демо»: владелец взял риск
    на себя, поэтому целевое состояние передаётся явно, а не выводится
    из инверсии тумблера.
    """
    print("set_account: демо ⇄ реал")
    client = FakeClient(profile=AccountProfile(
        account="demo", trade_type="sprint", currency="usd"))
    panel, journal, manual, risk, port, paths = make_panel(
        mode="demo", client=client)

    async def scenario():
        """Послать set_account на реал и на демо.

        Returns:
            Список ответов панели.
        """
        await panel.start()
        replies = []
        try:
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                await asyncio.wait_for(ws.recv(), timeout=10)
                for target in ("real", "demo"):
                    await ws.send(json.dumps({"cmd": "set_account",
                                              "target": target}))
                    for _ in range(8):
                        message = json.loads(
                            await asyncio.wait_for(ws.recv(), timeout=10))
                        if message.get("type") == "reply":
                            replies.append(message)
                            break
        finally:
            await panel.stop()
        return replies

    replies = asyncio.run(asyncio.wait_for(scenario(), timeout=40))
    try:
        check("две реплики", len(replies) == 2, replies)
        check("обе ok",
              all(r and r["status"] == "ok" for r in replies), replies)
        check("клиент звал и real, и demo",
              client.ensure_calls == ["real", "demo"], client.ensure_calls)
    finally:
        journal.close()
        cleanup(paths)


def test_set_account_bad_target():
    """set_account с незнакомым target не трогает площадку."""
    print("set_account: незнакомый target")
    client = FakeClient(profile=AccountProfile(
        account="demo", trade_type="sprint", currency="usd"))
    panel, journal, manual, risk, port, paths = make_panel(
        mode="demo", client=client)

    async def scenario():
        """Послать set_account с битым target.

        Returns:
            Ответ панели.
        """
        await panel.start()
        try:
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                await asyncio.wait_for(ws.recv(), timeout=10)
                await ws.send(json.dumps({"cmd": "set_account",
                                          "target": "supersafe"}))
                for _ in range(8):
                    message = json.loads(
                        await asyncio.wait_for(ws.recv(), timeout=10))
                    if message.get("type") == "reply":
                        return message
        finally:
            await panel.stop()

    reply = asyncio.run(asyncio.wait_for(scenario(), timeout=40))
    try:
        check("ответ error", reply and reply["status"] == "error", reply)
        check("target в причине",
              "target" in (reply or {}).get("message", ""), reply)
        check("клиент НЕ вызывался",
              client.ensure_calls == [], client.ensure_calls)
    finally:
        journal.close()
        cleanup(paths)


def test_set_account_blocked_in_dry():
    """В dry set_account к площадке не ходит, как и «демо»."""
    print("set_account в режиме dry")
    panel, journal, manual, risk, port, paths = make_panel(mode="dry")

    async def scenario():
        """Нажать set_account без клиента.

        Returns:
            Ответ панели.
        """
        await panel.start()
        try:
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                await asyncio.wait_for(ws.recv(), timeout=10)
                await ws.send(json.dumps({"cmd": "set_account",
                                          "target": "real"}))
                for _ in range(8):
                    message = json.loads(
                        await asyncio.wait_for(ws.recv(), timeout=10))
                    if message.get("type") == "reply":
                        return message
        finally:
            await panel.stop()

    reply = asyncio.run(asyncio.wait_for(scenario(), timeout=40))
    try:
        check("ответ error", reply and reply["status"] == "error", reply)
        check("причина — режим не ходит к площадке",
              "не ходит" in (reply or {}).get("message", ""), reply)
    finally:
        journal.close()
        cleanup(paths)


def test_ensure_demo_blocked_in_dry():
    """В dry панель к площадке не ходит: «демо» без клиента не работает."""
    print("«демо» в режиме dry")
    panel, journal, manual, risk, port, paths = make_panel(mode="dry")

    async def scenario():
        """Нажать «демо» без клиента.

        Returns:
            Ответ панели.
        """
        await panel.start()
        try:
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                await asyncio.wait_for(ws.recv(), timeout=10)
                await ws.send(json.dumps({"cmd": "ensure_demo"}))
                for _ in range(8):
                    message = json.loads(
                        await asyncio.wait_for(ws.recv(), timeout=10))
                    if message.get("type") == "reply":
                        return message
        finally:
            await panel.stop()

    reply = asyncio.run(asyncio.wait_for(scenario(), timeout=40))
    try:
        check("ответ error", reply and reply["status"] == "error", reply)
        check("причина — режим не ходит к площадке",
              "не ходит" in (reply or {}).get("message", ""), reply)
    finally:
        journal.close()
        cleanup(paths)


def _auth_wrong_token(panel, journal, port, paths):
    """Прогнать сценарий неверного токена.

    Args:
        panel:   Панель.
        journal: Журнал.
        port:    Порт панели.
        paths:   Пути для очистки.

    Returns:
        None.
    """

    async def scenario():
        """Послать неверный токен и собрать всё, что придёт.

        Returns:
            Список сообщений клиента до закрытия.
        """
        await panel.start()
        try:
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                await ws.send(json.dumps({"cmd": "auth", "token": "не тот"}))
                messages = []
                for _ in range(5):
                    try:
                        messages.append(json.loads(
                            await asyncio.wait_for(ws.recv(), timeout=10)))
                    except (asyncio.TimeoutError, websockets.ConnectionClosed):
                        break
                return messages
        finally:
            await panel.stop()

    messages = asyncio.run(asyncio.wait_for(scenario(), timeout=40))
    try:
        replies = [m for m in messages if m.get("type") == "reply"]
        check("есть ответ error", replies and replies[0]["status"] == "error",
              messages)
        check("причина — неверный токен",
              "неверный токен" in replies[0].get("message", ""), replies)
        check("соединение закрыто после отказа", len(messages) < 5, messages)
    finally:
        journal.close()
        cleanup(paths)


def test_auth_wrong_token_rejected():
    """Неверный токен не пускает: отказ и закрытие соединения."""
    print("неверный токен отклонён")
    panel, journal, manual, risk, port, paths = make_panel(token="секрет")
    _auth_wrong_token(panel, journal, port, paths)


def test_auth_no_leak_without_token():
    """Без авторизации панель молчит: ни состояния, ни команд."""
    print("до авторизации — тишина")
    panel, journal, manual, risk, port, paths = make_panel(token="секрет")
    stop_path = paths[1]

    async def scenario():
        """Подключиться, постучаться и убедиться, что ничего не утекло.

        Returns:
            (что пришло само, что ответили на команду, взведён ли стоп).
        """
        await panel.start()
        try:
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                leaked = []
                try:
                    leaked.append(json.loads(
                        await asyncio.wait_for(ws.recv(), timeout=0.7)))
                except asyncio.TimeoutError:
                    pass  # тишина до авторизации — это правильно

                await ws.send(json.dumps({"cmd": "stop"}))
                replies = []
                for _ in range(5):
                    try:
                        replies.append(json.loads(
                            await asyncio.wait_for(ws.recv(), timeout=10)))
                    except (asyncio.TimeoutError, websockets.ConnectionClosed):
                        break
                return leaked, replies, kill_switch_active(stop_path)
        finally:
            await panel.stop()

    leaked, replies, stopped = asyncio.run(
        asyncio.wait_for(scenario(), timeout=40))
    try:
        check("до авторизации состояние НЕ утекло", leaked == [], leaked)
        errors = [m for m in replies if m.get("type") == "reply"
                  and m.get("status") == "error"]
        check("команда получила отказ", bool(errors), replies)
        check("причина — неверный токен",
              errors and "неверный токен" in errors[0].get("message", ""),
              errors)
        check("kill-switch НЕ взведён", stopped is False, stopped)
    finally:
        journal.close()
        cleanup(paths)


def test_auth_correct_token_gets_state():
    """Правильный токен открывает доступ: состояние и команды."""
    print("правильный токен пускает")
    panel, journal, manual, risk, port, paths = make_panel(token="секрет")
    stop_path = paths[1]

    async def scenario():
        """Авторизоваться, получить срез и выполнить команду.

        Returns:
            Кортеж (первое сообщение, ответ на stop, взведён ли стоп).
        """
        await panel.start()
        try:
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                await ws.send(json.dumps({"cmd": "auth", "token": "секрет"}))
                first = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                await ws.send(json.dumps({"cmd": "stop"}))
                for _ in range(8):
                    message = json.loads(
                        await asyncio.wait_for(ws.recv(), timeout=10))
                    if message.get("type") == "reply":
                        return first, message, kill_switch_active(stop_path)
        finally:
            await panel.stop()

    first, reply, stopped = asyncio.run(asyncio.wait_for(scenario(), timeout=40))
    try:
        check("после auth сразу состояние", first.get("type") == "state", first)
        check("в состоянии есть режим", "mode" in first, first)
        check("команда выполнилась", reply and reply["status"] == "ok", reply)
        check("kill-switch взведён", stopped is True, stopped)
    finally:
        release_kill_switch(stop_path)
        journal.close()
        cleanup(paths)


def main():
    """Прогнать все тесты и вернуть код возврата.

    Returns:
        0 — всё прошло, 1 — есть провалы.
    """
    for test in (test_http_serves_page, test_http_404, test_websocket_state,
                 test_manual_button_fires_signal, test_stop_and_resume,
                 test_unknown_command, test_listens_locally_only,
                 test_profile_in_snapshot, test_ensure_demo_success,
                 test_ensure_demo_error, test_no_to_real_command,
                 test_set_account_two_way, test_set_account_bad_target,
                 test_set_account_blocked_in_dry,
                 test_ensure_demo_blocked_in_dry,
                 test_auth_wrong_token_rejected,
                 test_auth_no_leak_without_token,
                 test_auth_correct_token_gets_state):
        test()
        print()

    print(f"итого: {passed} прошло, {failed} провалено")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
