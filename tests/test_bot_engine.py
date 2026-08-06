"""
Тесты машины состояний. Python 3.10.

Запуск:  python3.10 tests/test_bot_engine.py    (из корня проекта)

Сети здесь нет: площадка подменяется заглушкой, которая считает вызовы и
умеет отказывать нужным образом. Так проверяются пути, до которых на живом
сервере не добраться по заказу — обрыв связи ровно в момент отправки,
отказ площадки, потерянный ответ.

САМЫЙ ВАЖНЫЙ ТЕСТ ФАЙЛА — test_sending_never_retries. Повтор запроса на
открытие означает двойную ставку на реальные деньги, потому что ответ мог
дойти. Если этот тест однажды покраснеет, чинить надо немедленно и в первую
очередь.
"""

import asyncio
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.api.client import TradeUnknown
from bot.api.models import PlatformError, Quote, Trade
from bot.config import BotConfig, RiskConfig
from bot.engine import Engine, State
from bot.journal import Journal, engage_kill_switch, release_kill_switch
from bot.strategy.base import Signal

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


class FakeClient:
    """Заглушка площадки: считает вызовы и отвечает по заказу.

    Attributes:
        open_calls: Сколько раз просили открыть сделку — ключевой счётчик.
    """

    def __init__(self, behaviour="ok", entry_price=157.666):
        """Создать заглушку.

        Args:
            behaviour:   "ok" / "unknown" (обрыв) / "error" (отказ площадки).
            entry_price: Цена входа в успешном ответе.
        """
        self.behaviour = behaviour
        self.entry_price = entry_price
        self.open_calls = 0
        self.check_calls = 0
        self.active_calls = 0
        self.balance_calls = 0
        self.profile_calls = 0
        self.settle_outcome = "win"

    def open_trade(self, symbol, direction, investment, expiry_minutes=1,
                   trade_type="sprint"):
        """Изобразить открытие сделки.

        Args:
            symbol:         Инструмент.
            direction:      Направление.
            investment:     Ставка.
            expiry_minutes: Экспирация.
            trade_type:     Тип сделки.

        Returns:
            Кортеж (Trade, request_ts).

        Raises:
            TradeUnknown:  При behaviour "unknown".
            PlatformError: При behaviour "error".
        """
        self.open_calls += 1
        request_ts = time.time()

        if self.behaviour == "unknown":
            raise TradeUnknown("ответ не получен (имитация обрыва)")
        if self.behaviour == "error":
            raise PlatformError(code="error_time_18",
                                message="крайнее время закрытия — 18:00 МСК",
                                raw="error_time_18")

        open_ts = request_ts + 3.1
        return (
            Trade(
                trade_id=224809704 + self.open_calls, symbol=symbol,
                direction=direction, investment=investment,
                entry_price=self.entry_price, open_ts=int(open_ts),
                expiry_ts=int(open_ts) + expiry_minutes * 60,
                duration=expiry_minutes * 60, raw="<tr data-id=…>",
            ),
            request_ts,
        )

    def check_trade(self, trade_id, investment):
        """Изобразить проверку итога сделки.

        Args:
            trade_id:   Идентификатор сделки.
            investment: Ставка.

        Returns:
            TradeResult.
        """
        from bot.api.models import TradeResult

        self.check_calls += 1
        pnl = investment * 0.82 if self.settle_outcome == "win" else -investment
        return TradeResult(trade_id=trade_id, outcome=self.settle_outcome,
                           pnl=pnl, raw="2;3.64;9363")

    def payout_percent(self, symbol, expiry_minutes=1, investment=1,
                       trade_type="Sprint", currency="USD"):
        """Изобразить запрос процента выплаты.

        Args:
            symbol:         Инструмент.
            expiry_minutes: Экспирация.
            investment:     Ставка.
            trade_type:     Тип сделки.
            currency:       Валюта.

        Returns:
            Процент выплаты.
        """
        return 82

    def balance(self):
        """Изобразить баланс ДЕМО-счёта.

        Нужен предохранителю движка: перед каждой реальной ставкой он
        сверяет баланс с порогом, потому что площадка не сообщает через
        API, демо активно или реал. По умолчанию заглушка изображает
        демо — иначе все тесты режима demo упирались бы в предохранитель.

        Returns:
            Balance с демо-суммой.
        """
        from bot.api.models import Balance

        self.balance_calls += 1
        return Balance(amount=9363.20, currency="$", raw="9 363,20 $")

    def profile(self):
        """Изобразить /profile с демо-счётом.

        Нужен предохранителю движка: с 2026-08-06 перед каждой ставкой
        первой линией сверяется /profile (авторитетный источник типа
        счёта), и только второй — эвристика по балансу. По умолчанию
        заглушка изображает демо/sprint/usd — иначе все тесты режима demo
        упирались бы в предохранитель.

        Returns:
            AccountProfile демо-счёта.
        """
        from bot.api.models import AccountProfile

        self.profile_calls += 1
        return AccountProfile(account="demo", trade_type="sprint",
                              currency="usd")

    def active_trades(self):
        """Изобразить список активных сделок.

        Returns:
            Сырой текст ответа.
        """
        self.active_calls += 1
        return "" if self.behaviour != "unknown_found" else "USDJPY 224809704"


class FakeQuotes:
    """Заглушка потока котировок с управляемой ценой."""

    def __init__(self, price=157.666):
        """Создать заглушку.

        Args:
            price: Текущая середина спреда.
        """
        self.price = price
        self.fresh = True

    def mid(self, symbol):
        """Вернуть середину спреда.

        Args:
            symbol: Инструмент.

        Returns:
            Цена.
        """
        return self.price

    def get(self, symbol):
        """Вернуть котировку.

        Args:
            symbol: Инструмент.

        Returns:
            Quote.
        """
        return Quote(symbol=symbol, bid=self.price, ask=self.price,
                     updated=int(time.time()))


def make_setup(mode="demo", behaviour="ok", **config_overrides):
    """Собрать движок с журналом на временной БД.

    Args:
        mode:              Режим бота.
        behaviour:         Поведение заглушки площадки.
        config_overrides:  Переопределения полей BotConfig.

    Returns:
        Кортеж (Engine, Journal, FakeClient, путь к БД).
    """
    handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    handle.close()
    journal = Journal(handle.name)

    config = BotConfig(
        mode=mode,
        symbol_whitelist=["USD/JPY", "EUR/USD"],
        default_investment=2,
        stop_file=os.path.join(tempfile.gettempdir(), "bot_test_STOP_absent"),
        risk=RiskConfig(),
    )
    for key, value in config_overrides.items():
        setattr(config, key, value)

    client = FakeClient(behaviour=behaviour)
    engine = Engine(config=config, journal=journal, client=client,
                    quotes=FakeQuotes())
    return engine, journal, client, handle.name


def cleanup(path):
    """Удалить временный файл БД.

    Args:
        path: Путь к файлу.

    Returns:
        None.
    """
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(path + suffix)
        except OSError:
            pass


def signal(direction="call", symbol="USD/JPY", **kwargs):
    """Создать сигнал для теста.

    Args:
        direction: Направление.
        symbol:    Инструмент.
        kwargs:    Прочие поля Signal.

    Returns:
        Signal.
    """
    return Signal(ts=time.time(), symbol=symbol, direction=direction,
                  source="test", **kwargs)


def test_happy_path():
    """Успешная сделка проходит весь путь до CLOSED."""
    print("успешный путь IDLE → OPEN → CLOSED")
    engine, journal, client, path = make_setup()
    try:
        async def scenario():
            """Открыть сделку и дождаться расчёта.

            Returns:
                Локальный id записи.
            """
            row_id = await engine.handle(signal("call"))
            # Ждём фоновую задачу расчёта.
            await asyncio.gather(*engine._settle_tasks, return_exceptions=True)
            return row_id

        row_id = asyncio.run(asyncio.wait_for(scenario(), timeout=90))
        row = journal.get_trade(row_id)

        check("запрос отправлен ровно один раз", client.open_calls == 1,
              client.open_calls)
        check("сделка записана", row is not None)
        check("trade_id от площадки", row["trade_id"] == 224809705, row["trade_id"])
        check("цена входа", row["entry_price"] == 157.666)
        # Площадка отдаёт data-timeopen ЦЕЛЫМИ секундами, поэтому замер
        # задержки принципиально огрублён до ±1 с: заглушка «открывает»
        # через 3.1 с, а в журнал попадает 2.9–3.1 с в зависимости от того,
        # куда пришлась граница секунды. Точнее измерить нечем.
        check("задержка в пределах секунды от 3.1 с",
              2100 <= row["latency_ms"] <= 3200, row["latency_ms"])
        check("итог записан", row["result"] == "win", row["result"])
        check("состояние CLOSED", engine.state == State.CLOSED, engine.state)
    finally:
        journal.close()
        cleanup(path)


def test_sending_never_retries():
    """КРИТИЧНО: при обрыве связи запрос НЕ повторяется.

    Ответ мог дойти до площадки — повтор означал бы двойную ставку.
    Вместо этого сделка уходит в UNKNOWN и разрешается сверкой.
    """
    print("обрыв связи не приводит к повторной ставке")
    engine, journal, client, path = make_setup(behaviour="unknown")
    try:
        async def scenario():
            """Отправить сигнал при оборванной связи.

            Returns:
                Локальный id записи.
            """
            row_id = await engine.handle(signal("call"))
            await asyncio.gather(*engine._settle_tasks, return_exceptions=True)
            return row_id

        row_id = asyncio.run(asyncio.wait_for(scenario(), timeout=60))

        # Главная проверка всего файла.
        check("ставка отправлена РОВНО ОДИН раз", client.open_calls == 1,
              f"было {client.open_calls} попыток — ДВОЙНАЯ СТАВКА")
        check("сделка записана в журнал", row_id > 0)
        check("сверка вызвана", client.active_calls >= 1, client.active_calls)

        events = [e["message"] for e in journal.recent_events()]
        check("переход в UNKNOWN записан",
              any("UNKNOWN" in message for message in events), events)

        row = journal.get_trade(row_id)
        # Сверка не нашла сделку → ставка не прошла.
        check("итог failed после сверки", row["result"] == "failed", row["result"])
    finally:
        journal.close()
        cleanup(path)


def test_unknown_found_by_reconciliation():
    """UNKNOWN разрешается в OPEN, если сверка нашла сделку."""
    print("сверка нашла сделку — ставка была принята")
    engine, journal, client, path = make_setup(behaviour="unknown")
    try:
        client.behaviour = "unknown"

        async def scenario():
            """Отправить сигнал, затем изобразить найденную сделку.

            Returns:
                Локальный id записи.
            """
            row_id = await engine.handle(signal("call"))
            # Площадка «показывает» сделку при следующей сверке.
            client.behaviour = "unknown_found"
            await asyncio.gather(*engine._settle_tasks, return_exceptions=True)
            return row_id

        row_id = asyncio.run(asyncio.wait_for(scenario(), timeout=60))
        check("повтора ставки не было", client.open_calls == 1, client.open_calls)

        events = [e["message"] for e in journal.recent_events()]
        check("зафиксировано UNKNOWN → OPEN",
              any("UNKNOWN → OPEN" in message for message in events), events)

        row = journal.get_trade(row_id)
        check("сделка НЕ помечена как failed", row["result"] != "failed",
              row["result"])
    finally:
        journal.close()
        cleanup(path)


def test_platform_error_is_not_unknown():
    """Внятный отказ площадки — это FAILED, а не UNKNOWN.

    Разница принципиальна: при отказе ставки точно нет и сверять нечего.
    """
    print("отказ площадки → FAILED без сверки")
    engine, journal, client, path = make_setup(behaviour="error")
    try:
        row_id = asyncio.run(engine.handle(signal("call")))
        check("запрос был один", client.open_calls == 1)
        check("сверка НЕ вызывалась", client.active_calls == 0, client.active_calls)
        check("состояние FAILED", engine.state == State.FAILED, engine.state)

        row = journal.get_trade(row_id)
        check("итог failed", row["result"] == "failed", row["result"])
        check("сырой ответ сохранён", "error_time_18" in (row["raw_response"] or ""),
              row["raw_response"])
    finally:
        journal.close()
        cleanup(path)


def test_kill_switch_blocks():
    """Kill-switch запрещает новые входы мгновенно."""
    print("kill-switch останавливает вход")
    stop_path = os.path.join(tempfile.gettempdir(), "bot_test_STOP_active")
    engine, journal, client, path = make_setup(stop_file=stop_path)
    try:
        engage_kill_switch(stop_path, reason="тест")
        row_id = asyncio.run(engine.handle(signal("call")))

        check("сделка не открыта", row_id is None)
        check("запрос НЕ отправлялся", client.open_calls == 0, client.open_calls)
        check("состояние REJECTED", engine.state == State.REJECTED)

        risks = journal.recent_events(kind="risk")
        check("отказ записан в журнал",
              any("kill-switch" in e["message"] for e in risks),
              [e["message"] for e in risks])
    finally:
        release_kill_switch(stop_path)
        journal.close()
        cleanup(path)


def test_whitelist_and_stale():
    """Инструмент вне списка и протухший сигнал отклоняются."""
    print("белый список и протухший сигнал")
    engine, journal, client, path = make_setup()
    try:
        # Инструмента нет в whitelist.
        row_id = asyncio.run(engine.handle(signal("call", symbol="GBP/JPY")))
        check("чужой инструмент отклонён", row_id is None)
        check("запроса не было", client.open_calls == 0)

        # Сигнал возрастом больше MAX_SIGNAL_AGE.
        old = Signal(ts=time.time() - 30, symbol="USD/JPY", direction="call",
                     source="test")
        row_id = asyncio.run(engine.handle(old))
        check("протухший сигнал отклонён", row_id is None)
        check("запроса по-прежнему не было", client.open_calls == 0)

        risks = [e["message"] for e in journal.recent_events(kind="risk")]
        check("обе причины в журнале", len(risks) == 2, risks)
    finally:
        journal.close()
        cleanup(path)


def test_dry_mode_never_touches_platform():
    """Режим dry не ходит к площадке вовсе."""
    print("режим dry — без сети")
    engine, journal, client, path = make_setup(mode="dry")
    try:
        async def scenario():
            """Провести сделку в режиме dry.

            Returns:
                Локальный id записи.
            """
            row_id = await engine.handle(signal("call"))
            await asyncio.gather(*engine._settle_tasks, return_exceptions=True)
            return row_id

        # Экспирация 1 минута + задержка: ждём с запасом.
        row_id = asyncio.run(asyncio.wait_for(scenario(), timeout=90))

        check("к площадке не обращались", client.open_calls == 0, client.open_calls)
        check("проверок итога не было", client.check_calls == 0, client.check_calls)

        row = journal.get_trade(row_id)
        check("сделка записана", row is not None)
        check("режим dry в записи", row["mode"] == "dry")
        check("задержка смоделирована", 2700 <= row["latency_ms"] <= 3700,
              row["latency_ms"])
        check("итог посчитан", row["result"] in ("win", "loss", "refund"),
              row["result"])
    finally:
        journal.close()
        cleanup(path)


def test_shadow_mode_records_without_betting():
    """Режим shadow пишет намерение, но ставку не делает."""
    print("режим shadow — без ставки")
    engine, journal, client, path = make_setup(mode="shadow")
    try:
        row_id = asyncio.run(engine.handle(signal("put")))

        check("ставка НЕ отправлялась", client.open_calls == 0, client.open_calls)
        row = journal.get_trade(row_id)
        check("намерение записано", row is not None)
        check("режим shadow", row["mode"] == "shadow")
        check("итог помечен как shadow", row["result"] == "shadow", row["result"])
        check("pnl нулевой", row["pnl"] == 0.0)
    finally:
        journal.close()
        cleanup(path)


def test_dry_settlement_direction():
    """В dry расчёт учитывает направление сделки."""
    print("расчёт имитации по направлению")
    engine, journal, client, path = make_setup(mode="dry")
    try:
        async def scenario(direction, price_after):
            """Провести сделку и подменить цену к экспирации.

            Args:
                direction:   Направление сделки.
                price_after: Цена на момент экспирации.

            Returns:
                Запись о сделке.
            """
            row_id = await engine.handle(signal(direction))
            engine.quotes.price = price_after
            await asyncio.gather(*engine._settle_tasks, return_exceptions=True)
            return journal.get_trade(row_id)

        # Call и цена выросла — выигрыш.
        row = asyncio.run(asyncio.wait_for(scenario("call", 158.0), timeout=90))
        check("call + рост = win", row["result"] == "win", row["result"])
        check("pnl по выплате 82%", abs(row["pnl"] - 1.64) < 0.01, row["pnl"])

        engine.quotes.price = 157.666
        # Put и цена выросла — проигрыш.
        row = asyncio.run(asyncio.wait_for(scenario("put", 158.0), timeout=90))
        check("put + рост = loss", row["result"] == "loss", row["result"])
        check("pnl равен ставке", row["pnl"] == -2.0, row["pnl"])
    finally:
        journal.close()
        cleanup(path)


def test_balance_safeguard_blocks_real_account():
    """Низкий баланс останавливает ставку: похоже на реальный счёт.

    Площадка НЕ сообщает через API, демо активно или реал: balance.php
    один на оба счёта, user_hash при переключении не меняется. Значит,
    конфиг с account="demo" сам по себе не гарантирует ничего — и
    единственный доступный признак это величина баланса.

    Случай не выдуманный: 2026-08-06 владелец переключился на реальный
    счёт в кабинете, и баланс по тому же хешу сменился с 9363 $ на
    11.56 $. Запусти бот прогон по плану — ставки ушли бы с реальных
    денег при конфиге, где написано «демо».
    """
    print("предохранитель по балансу")
    engine, journal, client, path = make_setup(mode="demo")
    try:
        engine.config.account = "demo"
        engine.config.min_balance_for_demo = 1000.0

        # Баланс реального счёта — ниже порога.
        class LowBalanceClient(FakeClient):
            """Заглушка, отдающая баланс реального счёта."""

            def balance(self):
                """Вернуть низкий баланс.

                Returns:
                    Balance.
                """
                from bot.api.models import Balance

                return Balance(amount=11.56, currency="$", raw="11,56 $")

        low = LowBalanceClient()
        engine.client = low

        row_id = asyncio.run(engine.handle(signal("call")))
        check("сделка отклонена", row_id is None)
        check("ставка НЕ отправлялась", low.open_calls == 0,
              f"было {low.open_calls} попыток — ДЕНЬГИ УШЛИ БЫ")
        check("состояние REJECTED", engine.state == State.REJECTED, engine.state)

        risks = [e["message"] for e in journal.recent_events(kind="risk")]
        check("причина в журнале",
              any("РЕАЛЬНЫЙ" in message for message in risks), risks)

        # А при балансе демо ставка проходит.
        class DemoBalanceClient(FakeClient):
            """Заглушка, отдающая баланс демо-счёта."""

            def balance(self):
                """Вернуть высокий баланс.

                Returns:
                    Balance.
                """
                from bot.api.models import Balance

                return Balance(amount=9363.20, currency="$", raw="9 363,20 $")

        demo = DemoBalanceClient()
        engine.client = demo
        engine.risk = None

        async def scenario():
            """Открыть сделку при демо-балансе.

            Returns:
                Локальный id записи.
            """
            row = await engine.handle(signal("call"))
            await asyncio.gather(*engine._settle_tasks, return_exceptions=True)
            return row

        row_id = asyncio.run(asyncio.wait_for(scenario(), timeout=90))
        check("при демо-балансе ставка прошла", row_id is not None)
        check("запрос отправлен один раз", demo.open_calls == 1, demo.open_calls)
    finally:
        journal.close()
        cleanup(path)


def test_balance_unknown_blocks_bet():
    """Если баланс не узнать — не ставим. Слепая ставка недопустима."""
    print("неизвестный баланс останавливает ставку")
    engine, journal, client, path = make_setup(mode="demo")
    try:
        engine.config.account = "demo"

        class BrokenBalanceClient(FakeClient):
            """Заглушка, у которой баланс не читается."""

            def balance(self):
                """Изобразить сбой запроса баланса.

                Raises:
                    PlatformError: Всегда.
                """
                raise PlatformError(code="network", message="сеть не ответила")

        broken = BrokenBalanceClient()
        engine.client = broken

        row_id = asyncio.run(engine.handle(signal("call")))
        check("сделка отклонена", row_id is None)
        check("ставка НЕ отправлялась", broken.open_calls == 0, broken.open_calls)
    finally:
        journal.close()
        cleanup(path)


def test_profile_safeguard_hard_stop():
    """Расхождение /profile с конфигом — жёсткий стоп, не переключение.

    Первая линия предохранителя (с 2026-08-06): /profile — авторитетный
    источник типа счёта. Требование карты API §3.1: при расхождении бот
    останавливается, а НЕ приводит счёт к конфигу на лету — тумблер без
    параметра слишком опасен для торгового цикла.
    """
    print("предохранитель по /profile")
    from bot.api.models import AccountProfile

    engine, journal, client, path = make_setup(mode="demo")
    try:
        engine.config.account = "demo"

        # Площадка на РЕАЛЕ при конфиге demo.
        class RealProfileClient(FakeClient):
            """Заглушка: /profile говорит «реал»."""

            def profile(self):
                """Вернуть профиль реального счёта.

                Returns:
                    AccountProfile.
                """
                self.profile_calls += 1
                return AccountProfile(account="real", trade_type="sprint",
                                      currency="usd")

        real = RealProfileClient()
        engine.client = real
        row_id = asyncio.run(engine.handle(signal("call")))
        check("реал при конфиге demo: отклонено", row_id is None)
        check("ставка НЕ отправлялась", real.open_calls == 0, real.open_calls)
        check("тумблер НЕ дёргался (нет такого метода в пути ставки)",
              not hasattr(real, "toggle_calls") or real.toggle_calls == 0)

        # Тип сделок аккаунта classic при отправляемом sprint.
        class ClassicProfileClient(FakeClient):
            """Заглушка: аккаунт в режиме classic."""

            def profile(self):
                """Вернуть профиль с classic.

                Returns:
                    AccountProfile.
                """
                return AccountProfile(account="demo", trade_type="classic",
                                      currency="usd")

        classic = ClassicProfileClient()
        engine.client = classic
        row_id = asyncio.run(engine.handle(signal("call")))
        check("classic на аккаунте: отклонено", row_id is None)
        check("ставка НЕ отправлялась (classic)", classic.open_calls == 0)

        # Валюта RUB — лимиты и суммы другие.
        class RubProfileClient(FakeClient):
            """Заглушка: счёт в рублях."""

            def profile(self):
                """Вернуть профиль с RUB.

                Returns:
                    AccountProfile.
                """
                return AccountProfile(account="demo", trade_type="sprint",
                                      currency="rub")

        rub = RubProfileClient()
        engine.client = rub
        row_id = asyncio.run(engine.handle(signal("call")))
        check("RUB на счёте: отклонено", row_id is None)

        # /profile не читается — не ставим («не смог проверить» ≠ «всё ок»).
        class BrokenProfileClient(FakeClient):
            """Заглушка: /profile не отвечает."""

            def profile(self):
                """Изобразить сбой чтения профиля.

                Raises:
                    PlatformError: Всегда.
                """
                raise PlatformError(code="network", message="сеть не ответила")

        broken = BrokenProfileClient()
        engine.client = broken
        row_id = asyncio.run(engine.handle(signal("call")))
        check("профиль не читается: отклонено", row_id is None)
        check("ставка НЕ отправлялась (сбой)", broken.open_calls == 0)
    finally:
        journal.close()
        cleanup(path)


def test_summary():
    """Сводка движка отражает счётчики."""
    print("сводка движка")
    engine, journal, client, path = make_setup()
    try:
        asyncio.run(engine.handle(signal("call", symbol="GBP/JPY")))  # отказ
        summary = engine.summary()
        check("обработан 1 сигнал", summary["processed"] == 1, summary)
        check("отклонён 1", summary["rejected"] == 1, summary)
        check("режим в сводке", summary["mode"] == "demo", summary)
    finally:
        journal.close()
        cleanup(path)


def main():
    """Прогнать все тесты и вернуть код возврата.

    Returns:
        0 — всё прошло, 1 — есть провалы.
    """
    for test in (test_happy_path, test_sending_never_retries,
                 test_unknown_found_by_reconciliation,
                 test_platform_error_is_not_unknown, test_kill_switch_blocks,
                 test_whitelist_and_stale, test_dry_mode_never_touches_platform,
                 test_shadow_mode_records_without_betting,
                 test_dry_settlement_direction,
                 test_balance_safeguard_blocks_real_account,
                 test_balance_unknown_blocks_bet,
                 test_profile_safeguard_hard_stop, test_summary):
        test()
        print()

    print(f"итого: {passed} прошло, {failed} провалено")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
