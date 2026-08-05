"""
Тесты ограничителей входа. Python 3.10.

Запуск:  python3.10 tests/test_bot_risk.py    (из корня проекта)

По ТЗ каждый ограничитель покрывается ОТДЕЛЬНЫМ тестом: это тот код, где
пропущенная ветка означает не косметическую ошибку, а сделку, которой не
должно было быть.

Время и окно выплаты подменяются, а не выжидаются: тест, зависящий от
реального часа, либо не запустится днём, либо будет врать ночью.
"""

import os
import sys
import tempfile
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import payout as payout_module
from bot import risk as risk_module
from bot.config import BotConfig, RiskConfig
from bot.journal import Journal, engage_kill_switch, release_kill_switch
from bot.risk import RiskManager
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


def make_manager(**risk_overrides):
    """Собрать ограничители с журналом на временной БД.

    Окно у начала часа по умолчанию отключается: почти все тесты проверяют
    другие ограничители, и попадание реального времени в окно ломало бы их
    непредсказуемо.

    Args:
        risk_overrides: Переопределения полей RiskConfig.

    Returns:
        Кортеж (RiskManager, Journal, путь к БД, путь к kill-switch).
    """
    handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    handle.close()
    journal = Journal(handle.name)

    stop_path = os.path.join(tempfile.gettempdir(),
                             f"bot_risk_STOP_{os.getpid()}_{time.time()}")
    risk_config = RiskConfig(
        max_trades_per_day=20, max_concurrent=1, cooldown_sec=60,
        max_consecutive_losses=3, max_daily_loss=20, min_payout_percent=75,
        allowed_hours=[],   # по умолчанию без ограничения по времени
    )
    for key, value in risk_overrides.items():
        setattr(risk_config, key, value)

    config = BotConfig(mode="demo", symbol_whitelist=["USD/JPY", "EUR/USD"],
                       stop_file=stop_path, risk=risk_config)
    return RiskManager(config, journal), journal, handle.name, stop_path


def cleanup(db_path, stop_path=None):
    """Удалить временные файлы.

    Args:
        db_path:   Путь к файлу БД.
        stop_path: Путь к kill-switch.

    Returns:
        None.
    """
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(db_path + suffix)
        except OSError:
            pass
    if stop_path:
        try:
            os.unlink(stop_path)
        except OSError:
            pass


def no_hour_edge(func):
    """Выполнить функцию так, будто окна у начала часа сейчас нет.

    Args:
        func: Функция без аргументов.

    Returns:
        Результат функции.
    """
    original = risk_module.payout.is_hour_edge
    risk_module.payout.is_hour_edge = lambda moment=None: False
    try:
        return func()
    finally:
        risk_module.payout.is_hour_edge = original


def signal(symbol="USD/JPY", direction="call"):
    """Создать сигнал для теста.

    Args:
        symbol:    Инструмент.
        direction: Направление.

    Returns:
        Signal.
    """
    return Signal(ts=time.time(), symbol=symbol, direction=direction,
                  source="test")


def test_clean_pass():
    """Без нарушений сигнал проходит."""
    print("чистый проход")
    manager, journal, db, stop = make_manager()
    try:
        verdict = no_hour_edge(lambda: manager.check(signal()))
        check("сигнал разрешён", verdict is None, verdict)
    finally:
        journal.close()
        cleanup(db, stop)


def test_kill_switch():
    """Ограничитель: kill-switch."""
    print("ограничитель kill-switch")
    manager, journal, db, stop = make_manager()
    try:
        engage_kill_switch(stop, reason="тест")
        verdict = no_hour_edge(lambda: manager.check(signal()))
        check("вход запрещён", verdict is not None)
        check("причина названа", "kill-switch" in (verdict or ""), verdict)

        release_kill_switch(stop)
        verdict = no_hour_edge(lambda: manager.check(signal()))
        check("после снятия разрешён", verdict is None, verdict)
    finally:
        journal.close()
        cleanup(db, stop)


def test_hour_edge_blocks():
    """Ограничитель: окно пониженной выплаты у начала часа.

    Правило подтверждено живым замером 2026-08-05 22:57 МСК: выплата
    упала с 82% до 60%.
    """
    print("ограничитель окна у начала часа")
    manager, journal, db, stop = make_manager()
    original = risk_module.payout.is_hour_edge
    try:
        risk_module.payout.is_hour_edge = lambda moment=None: True
        verdict = manager.check(signal())
        check("вход в окне запрещён", verdict is not None)
        check("причина про окно", "окно" in (verdict or ""), verdict)
        check("указан безубыточный винрейт", "62.5" in (verdict or ""), verdict)

        risk_module.payout.is_hour_edge = lambda moment=None: False
        check("вне окна разрешён", manager.check(signal()) is None)
    finally:
        risk_module.payout.is_hour_edge = original
        journal.close()
        cleanup(db, stop)


def test_symbol_whitelist():
    """Ограничитель: белый список инструментов."""
    print("ограничитель белого списка")
    manager, journal, db, stop = make_manager()
    try:
        verdict = no_hour_edge(lambda: manager.check(signal(symbol="GBP/JPY")))
        check("чужой инструмент запрещён", verdict is not None)
        check("причина названа", "белом списке" in (verdict or ""), verdict)
        check("свой инструмент разрешён",
              no_hour_edge(lambda: manager.check(signal("EUR/USD"))) is None)
    finally:
        journal.close()
        cleanup(db, stop)


def test_allowed_hours():
    """Ограничитель: разрешённые часы."""
    print("ограничитель времени суток")
    manager, journal, db, stop = make_manager(allowed_hours=[[6, 18]])
    try:
        # Внутри окна.
        check("10:00 UTC разрешено",
              manager.check_hours(datetime(2026, 8, 5, 10, 0)) is None)
        check("06:00 UTC разрешено (граница включена)",
              manager.check_hours(datetime(2026, 8, 5, 6, 0)) is None)
        # Снаружи.
        verdict = manager.check_hours(datetime(2026, 8, 5, 3, 0))
        check("03:00 UTC запрещено", verdict is not None, verdict)
        check("18:00 UTC запрещено (граница исключена)",
              manager.check_hours(datetime(2026, 8, 5, 18, 0)) is not None)

        # Окно через полночь.
        manager.risk.allowed_hours = [[22, 6]]
        check("23:00 в окне через полночь",
              manager.check_hours(datetime(2026, 8, 5, 23, 0)) is None)
        check("03:00 в окне через полночь",
              manager.check_hours(datetime(2026, 8, 5, 3, 0)) is None)
        check("12:00 вне окна через полночь",
              manager.check_hours(datetime(2026, 8, 5, 12, 0)) is not None)

        # Пустой список — без ограничений.
        manager.risk.allowed_hours = []
        check("пустой список не ограничивает",
              manager.check_hours(datetime(2026, 8, 5, 3, 0)) is None)
    finally:
        journal.close()
        cleanup(db, stop)


def test_max_concurrent():
    """Ограничитель: одновременно открытые сделки."""
    print("ограничитель одновременных сделок")
    manager, journal, db, stop = make_manager(max_concurrent=1, cooldown_sec=0)
    try:
        check("первая разрешена",
              no_hour_edge(lambda: manager.check(signal())) is None)

        manager.register_open()
        verdict = no_hour_edge(lambda: manager.check(signal()))
        check("вторая запрещена", verdict is not None)
        check("причина названа", "открыто" in (verdict or ""), verdict)

        manager.register_close()
        check("после закрытия разрешена",
              no_hour_edge(lambda: manager.check(signal())) is None)
    finally:
        journal.close()
        cleanup(db, stop)


def test_cooldown():
    """Ограничитель: пауза между сделками."""
    print("ограничитель кулдауна")
    manager, journal, db, stop = make_manager(cooldown_sec=60, max_concurrent=5)
    try:
        manager.register_open()
        manager.register_close()

        verdict = no_hour_edge(lambda: manager.check(signal()))
        check("сразу после сделки запрещено", verdict is not None)
        check("причина названа", "кулдаун" in (verdict or ""), verdict)

        # Сдвигаем время последней сделки в прошлое.
        manager.last_trade_ts = time.time() - 61
        check("после паузы разрешено",
              no_hour_edge(lambda: manager.check(signal())) is None)
    finally:
        journal.close()
        cleanup(db, stop)


def test_max_trades_per_day():
    """Ограничитель: дневной лимит числа сделок."""
    print("ограничитель дневного лимита сделок")
    manager, journal, db, stop = make_manager(max_trades_per_day=3,
                                              cooldown_sec=0)
    try:
        for _ in range(3):
            journal.open_trade(mode="demo", symbol="USD/JPY", direction="call",
                               investment=1, expiry_minutes=1)

        verdict = no_hour_edge(lambda: manager.check(signal()))
        check("лимит сработал", verdict is not None)
        check("причина названа", "лимит сделок" in (verdict or ""), verdict)
    finally:
        journal.close()
        cleanup(db, stop)


def test_max_consecutive_losses():
    """Ограничитель: серия убытков подряд, со взведением стопа."""
    print("ограничитель серии убытков")
    manager, journal, db, stop = make_manager(max_consecutive_losses=3,
                                              cooldown_sec=0)
    try:
        for _ in range(3):
            row = journal.open_trade(mode="demo", symbol="USD/JPY",
                                     direction="call", investment=1,
                                     expiry_minutes=1)
            journal.settle_trade(row, "loss", pnl=-1)

        verdict = no_hour_edge(lambda: manager.check(signal()))
        check("серия остановила торговлю", verdict is not None)
        check("причина названа", "убытк" in (verdict or ""), verdict)

        # Стоп взведён и НЕ снимается сам — даже если статистика изменится.
        check("стоп взведён", manager.halted_reason is not None)
        win = journal.open_trade(mode="demo", symbol="USD/JPY", direction="call",
                                 investment=1, expiry_minutes=1)
        journal.settle_trade(win, "win", pnl=0.82)
        check("после победы стоп ДЕРЖИТСЯ",
              no_hour_edge(lambda: manager.check(signal())) is not None)

        # Снимается только вручную.
        check("release снимает", manager.release() is True)
        check("после снятия разрешено",
              no_hour_edge(lambda: manager.check(signal())) is None)
        check("повторный release — False", manager.release() is False)
    finally:
        journal.close()
        cleanup(db, stop)


def test_max_daily_loss():
    """Ограничитель: дневная просадка."""
    print("ограничитель дневной просадки")
    manager, journal, db, stop = make_manager(max_daily_loss=5, cooldown_sec=0,
                                              max_consecutive_losses=99)
    try:
        # Три убытка по 2 доллара — просадка 6 при лимите 5.
        for _ in range(3):
            row = journal.open_trade(mode="demo", symbol="USD/JPY",
                                     direction="call", investment=2,
                                     expiry_minutes=1)
            journal.settle_trade(row, "loss", pnl=-2)

        verdict = no_hour_edge(lambda: manager.check(signal()))
        check("просадка остановила торговлю", verdict is not None)
        check("причина названа", "просадка" in (verdict or ""), verdict)
        check("стоп взведён", manager.halted_reason is not None)
    finally:
        journal.close()
        cleanup(db, stop)


def test_min_payout_percent():
    """Ограничитель: минимальный процент выплаты."""
    print("ограничитель процента выплаты")
    manager, journal, db, stop = make_manager(min_payout_percent=75)
    try:
        check("82% проходит", manager.check_payout(82) is None)
        check("85% проходит", manager.check_payout(85) is None)

        verdict = manager.check_payout(60)
        check("60% отклоняется", verdict is not None)
        check("указан нужный винрейт", "62.5" in (verdict or ""), verdict)

        # Ноль — это «не торгуется», отдельная причина.
        zero = manager.check_payout(0)
        check("0% отклоняется", zero is not None)
        check("причина про недоступность", "недоступны" in (zero or ""), zero)

        # Неизвестная выплата — не входим вслепую.
        unknown = manager.check_payout(None)
        check("None отклоняется", unknown is not None)
        check("причина про неизвестность", "неизвестен" in (unknown or ""), unknown)
    finally:
        journal.close()
        cleanup(db, stop)


def test_status_summary():
    """Сводка ограничителей для панели."""
    print("сводка ограничителей")
    manager, journal, db, stop = make_manager(max_trades_per_day=20)
    try:
        row = journal.open_trade(mode="demo", symbol="USD/JPY", direction="call",
                                 investment=2, expiry_minutes=1)
        journal.settle_trade(row, "loss", pnl=-2)
        manager.register_open()

        status = manager.status()
        check("сделок сегодня", status["trades_today"] == 1, status)
        check("лимит виден", status["max_trades_per_day"] == 20)
        check("серия убытков", status["consecutive_losses"] == 1, status)
        check("pnl за день", abs(status["pnl_today"] + 2) < 0.01, status)
        check("открытых сделок", status["open_count"] == 1)
        check("стоп не взведён", status["halted"] is None)
        check("состояние окна есть", "hour_edge" in status)
        check("минуты до окна есть", "minutes_to_hour_edge" in status)
    finally:
        journal.close()
        cleanup(db, stop)


def main():
    """Прогнать все тесты и вернуть код возврата.

    Returns:
        0 — всё прошло, 1 — есть провалы.
    """
    for test in (test_clean_pass, test_kill_switch, test_hour_edge_blocks,
                 test_symbol_whitelist, test_allowed_hours, test_max_concurrent,
                 test_cooldown, test_max_trades_per_day,
                 test_max_consecutive_losses, test_max_daily_loss,
                 test_min_payout_percent, test_status_summary):
        test()
        print()

    print(f"итого: {passed} прошло, {failed} провалено")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
