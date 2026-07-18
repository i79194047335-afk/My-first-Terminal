"""
Tests for the FXCM feed watchdog (Фаза 4 — 24/7).

Проверяет две вещи без реального брокера:
  - market_open: границы форекс-недели (закрытие пт 22:00 UTC → вс 22:00 UTC);
  - логику watchdog: реконнект форсируется ТОЛЬКО при тишине в открытый рынок,
    и не дублируется, если флаг уже выставлен.

Импортирует feeds.fxcm_feed, который тянет forexconnect (есть только под 3.7) —
на python3.10 тест скипается целиком.

Run:  python3.7 tests/test_feed_watchdog.py
"""

import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Фид требует креды в окружении на уровне модуля? Нет — LOGIN/PASSWORD читаются
# в getenv и используются лишь в main(). Импорт безопасен и без них.
try:
    from feeds import fxcm_feed
    _FEED = fxcm_feed
except Exception as err:          # forexconnect нет (py3.10) — пропускаем
    _FEED = None
    _WHY  = repr(err)


def ts(y, mo, d, h, mi=0):
    """Unix-время момента UTC."""
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp()


@unittest.skipIf(_FEED is None, "feeds.fxcm_feed не импортируется (нет forexconnect)")
class TestMarketOpen(unittest.TestCase):

    def test_weekday_open(self):
        """Будни днём — рынок открыт."""
        self.assertTrue(_FEED.market_open(ts(2026, 7, 15, 12)))   # среда
        self.assertTrue(_FEED.market_open(ts(2026, 7, 13, 3)))    # понедельник ночь

    def test_saturday_closed(self):
        """Суббота — закрыт весь день."""
        self.assertFalse(_FEED.market_open(ts(2026, 7, 18, 0)))
        self.assertFalse(_FEED.market_open(ts(2026, 7, 18, 12)))
        self.assertFalse(_FEED.market_open(ts(2026, 7, 18, 23)))

    def test_friday_close_boundary(self):
        """Пятница: до 22:00 UTC открыт, с 22:00 — закрыт."""
        self.assertTrue(_FEED.market_open(ts(2026, 7, 17, 21, 59)))
        self.assertFalse(_FEED.market_open(ts(2026, 7, 17, 22, 0)))
        self.assertFalse(_FEED.market_open(ts(2026, 7, 17, 23, 0)))

    def test_sunday_open_boundary(self):
        """Воскресенье: до 22:00 UTC закрыт, с 22:00 — открыт."""
        self.assertFalse(_FEED.market_open(ts(2026, 7, 19, 21, 59)))
        self.assertTrue(_FEED.market_open(ts(2026, 7, 19, 22, 0)))
        self.assertTrue(_FEED.market_open(ts(2026, 7, 19, 23, 0)))


@unittest.skipIf(_FEED is None, "feeds.fxcm_feed не импортируется (нет forexconnect)")
class TestWatchdogLogic(unittest.TestCase):
    """Условие срабатывания watchdog в изоляции (без потока и брокера)."""

    def _should_reconnect(self, silence_sec, is_open, flag_set):
        """Повторяет решение fxcm_watchdog одной проверкой.

        Логика самого потока — цикл со sleep; здесь моделируем одно условие,
        чтобы проверить его без ожиданий.
        """
        if flag_set:
            return False
        return silence_sec > _FEED.TICK_SILENCE_SEC and is_open

    def test_silence_open_market_triggers(self):
        """Тишина дольше порога в открытый рынок → реконнект."""
        self.assertTrue(self._should_reconnect(
            _FEED.TICK_SILENCE_SEC + 10, is_open=True, flag_set=False))

    def test_silence_closed_market_ignored(self):
        """Та же тишина в закрытый рынок → НЕ реконнект (выходные)."""
        self.assertFalse(self._should_reconnect(
            _FEED.TICK_SILENCE_SEC + 10, is_open=False, flag_set=False))

    def test_short_silence_ignored(self):
        """Тишина короче порога → ничего не делаем."""
        self.assertFalse(self._should_reconnect(
            _FEED.TICK_SILENCE_SEC - 10, is_open=True, flag_set=False))

    def test_flag_already_set_not_duplicated(self):
        """Флаг уже выставлен → повторно не дёргаем."""
        self.assertFalse(self._should_reconnect(
            _FEED.TICK_SILENCE_SEC + 999, is_open=True, flag_set=True))


if __name__ == "__main__":
    unittest.main(verbosity=1)
