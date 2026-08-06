"""
Тесты сетки выплат и окна пониженной выплаты. Python 3.10.

Запуск:  python3.10 tests/test_bot_payout.py    (из корня проекта)

Окно у начала часа — самое дорогое правило площадки из известных: выплата
падает с 82% до 60%, а безубыточный винрейт подскакивает с 54.9% до 62.5%.
Ошибка в границах окна означает систематический вход в заведомо худшие
условия, поэтому границы проверяются поминутно, включая переход через
полночь и края ночного интервала.
"""

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.payout import (
    HOUR_EDGE_PERCENT,
    LARGE_STAKE,
    MSK,
    breakeven_winrate,
    describe,
    expected_percent,
    is_hour_edge,
    minutes_until_hour_edge,
)

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


def msk(hour, minute, day=5):
    """Собрать момент времени в МСК.

    Args:
        hour:   Час.
        minute: Минута.
        day:    День августа 2026.

    Returns:
        datetime с зоной МСК.
    """
    return datetime(2026, 8, day, hour, minute, tzinfo=MSK)


def test_breakeven():
    """Безубыточный винрейт считается верно для всей сетки."""
    print("безубыточный винрейт")
    check("85% → 54.1%", abs(breakeven_winrate(85) - 54.05) < 0.1, breakeven_winrate(85))
    check("82% → 54.9%", abs(breakeven_winrate(82) - 54.95) < 0.1, breakeven_winrate(82))
    check("79% → 55.9%", abs(breakeven_winrate(79) - 55.87) < 0.1, breakeven_winrate(79))
    check("60% → 62.5%", abs(breakeven_winrate(60) - 62.5) < 0.1, breakeven_winrate(60))
    # Нулевая выплата — не деление на ноль, а «выиграть невозможно».
    check("0% → 100%", breakeven_winrate(0) == 100.0, breakeven_winrate(0))

    # Ключевой факт: окно у часа поднимает порог на 7.6 пункта.
    delta = breakeven_winrate(HOUR_EDGE_PERCENT) - breakeven_winrate(82)
    check("окно дороже обычного на ~7.6 п.п.", abs(delta - 7.55) < 0.2, delta)


def test_expected_percent():
    """Сетка выплат по экспирации и размеру ставки."""
    print("сетка выплат")
    check("1 мин, мелкая ставка → 82", expected_percent(1, 1) == 82)
    check("3 мин, мелкая ставка → 82", expected_percent(3, 1) == 82)
    check("1 мин, крупная ставка → 85", expected_percent(1, LARGE_STAKE) == 85)
    check("4 мин, мелкая ставка → 79", expected_percent(4, 1) == 79)
    check("500 мин, мелкая ставка → 79", expected_percent(500, 1) == 79)
    check("4 мин, крупная ставка → 85", expected_percent(4, LARGE_STAKE) == 85)


def test_large_stake_threshold():
    """Порог крупной ставки — 80 включительно (проверено на площадке)."""
    print("порог крупной ставки")
    # Граница найдена перебором с точностью до цента: 79.99 → 82, 80 → 85.
    check("порог равен 80", LARGE_STAKE == 80.0, LARGE_STAKE)
    check("79 → 82%", expected_percent(1, 79) == 82, expected_percent(1, 79))
    check("79.99 → 82%", expected_percent(1, 79.99) == 82, expected_percent(1, 79.99))
    check("80.00 → 85% (включительно)", expected_percent(1, 80) == 85,
          expected_percent(1, 80))
    check("80.01 → 85%", expected_percent(1, 80.01) == 85, expected_percent(1, 80.01))
    # Тот же порог действует и на длинных экспирациях: 79% → 85%.
    check("4 мин, 79 → 79%", expected_percent(4, 79) == 79)
    check("4 мин, 80 → 85%", expected_percent(4, 80) == 85)


def test_hour_edge_boundaries():
    """Границы окна ±3 минуты у начала часа."""
    print("границы окна у начала часа")
    # Ночной интервал: окно действует.
    check("22:56 — вне окна", is_hour_edge(msk(22, 56)) is False)
    check("22:57 — в окне", is_hour_edge(msk(22, 57)) is True)
    check("22:59 — в окне", is_hour_edge(msk(22, 59)) is True)
    check("23:00 — в окне", is_hour_edge(msk(23, 0)) is True)
    check("23:02 — в окне", is_hour_edge(msk(23, 2)) is True)
    check("23:03 — вне окна", is_hour_edge(msk(23, 3)) is False)
    check("23:30 — вне окна", is_hour_edge(msk(23, 30)) is False)


def test_hour_edge_midnight():
    """Окно работает через полночь — интервал пересекает сутки."""
    print("переход через полночь")
    check("23:58 — в окне", is_hour_edge(msk(23, 58)) is True)
    check("00:00 — в окне", is_hour_edge(msk(0, 0, day=6)) is True)
    check("00:02 — в окне", is_hour_edge(msk(0, 2, day=6)) is True)
    check("00:05 — вне окна", is_hour_edge(msk(0, 5, day=6)) is False)
    check("03:30 — вне окна", is_hour_edge(msk(3, 30, day=6)) is False)
    check("03:59 — в окне", is_hour_edge(msk(3, 59, day=6)) is True)


def test_hour_edge_daytime():
    """Днём (09:00–17:00 МСК) окна нет вовсе."""
    print("дневной интервал без окна")
    check("09:00 — вне окна", is_hour_edge(msk(9, 0)) is False)
    check("09:59 — вне окна", is_hour_edge(msk(9, 59)) is False)
    check("12:00 — вне окна", is_hour_edge(msk(12, 0)) is False)
    check("16:58 — вне окна", is_hour_edge(msk(16, 58)) is False)
    # 17:00 — начало ночного интервала, окно снова действует.
    check("17:00 — в окне", is_hour_edge(msk(17, 0)) is True)
    check("17:02 — в окне", is_hour_edge(msk(17, 2)) is True)
    check("17:30 — вне окна", is_hour_edge(msk(17, 30)) is False)
    # 08:58 ещё ночь (интервал до 09:00), окно есть.
    check("08:58 — в окне", is_hour_edge(msk(8, 58)) is True)


def test_minutes_until():
    """Расчёт времени до ближайшего окна."""
    print("минуты до окна")
    check("22:09 → 48 мин", abs(minutes_until_hour_edge(msk(22, 9)) - 48) < 0.1,
          minutes_until_hour_edge(msk(22, 9)))
    check("22:50 → 7 мин", abs(minutes_until_hour_edge(msk(22, 50)) - 7) < 0.1,
          minutes_until_hour_edge(msk(22, 50)))
    check("внутри окна → 0", minutes_until_hour_edge(msk(22, 58)) == 0.0)
    check("23:00 → 0", minutes_until_hour_edge(msk(23, 0)) == 0.0)


def test_broker_downtime():
    """Ежедневное окно простоя брокера: 21:00–23:00 UTC."""
    print("окно простоя брокера")
    from datetime import timezone as tz

    from bot.payout import is_broker_down, minutes_until_broker_down

    def utc(hour, minute=0):
        """Собрать момент в UTC.

        Args:
            hour:   Час UTC.
            minute: Минута.

        Returns:
            datetime с зоной UTC.
        """
        return datetime(2026, 8, 6, hour, minute, tzinfo=tz.utc)

    check("20:59 — работает", is_broker_down(utc(20, 59)) is False)
    check("21:00 — простой", is_broker_down(utc(21, 0)) is True)
    check("22:30 — простой", is_broker_down(utc(22, 30)) is True)
    check("22:59 — простой", is_broker_down(utc(22, 59)) is True)
    check("23:00 — снова работает", is_broker_down(utc(23, 0)) is False)
    check("13:00 — работает", is_broker_down(utc(13, 0)) is False)
    check("03:00 — работает", is_broker_down(utc(3, 0)) is False)

    # Сколько осталось до закрытия — нужно, чтобы не начинать долгий прогон
    # за полчаса до простоя.
    check("в 20:00 остался час", abs(minutes_until_broker_down(utc(20, 0)) - 60) < 0.1,
          minutes_until_broker_down(utc(20, 0)))
    check("в 20:30 осталось 30 мин",
          abs(minutes_until_broker_down(utc(20, 30)) - 30) < 0.1,
          minutes_until_broker_down(utc(20, 30)))
    check("внутри окна — 0", minutes_until_broker_down(utc(22, 0)) == 0.0)
    check("в 13:00 остаётся 8 часов",
          abs(minutes_until_broker_down(utc(13, 0)) - 480) < 0.1,
          minutes_until_broker_down(utc(13, 0)))


def test_describe():
    """Человеческое описание выплаты."""
    print("описание для панели")
    text = describe(82, msk(12, 30))
    check("обычное время без пометки", "окно" not in text, text)
    check("процент в тексте", "82%" in text, text)
    check("безубыток в тексте", "54.9" in text, text)

    edge_text = describe(60, msk(22, 58))
    check("в окне есть пометка", "окно" in edge_text, edge_text)
    check("безубыток 62.5", "62.5" in edge_text, edge_text)


def main():
    """Прогнать все тесты и вернуть код возврата.

    Returns:
        0 — всё прошло, 1 — есть провалы.
    """
    for test in (test_breakeven, test_expected_percent, test_large_stake_threshold,
                 test_hour_edge_boundaries,
                 test_hour_edge_midnight, test_hour_edge_daytime,
                 test_minutes_until, test_broker_downtime, test_describe):
        test()
        print()

    print(f"итого: {passed} прошло, {failed} провалено")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
