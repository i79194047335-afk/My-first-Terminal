"""
Tests for the hub health snapshot (Фаза 4 — 24/7).

Проверяет hub.health() без поднятия HTTP-сервера: структуру ответа и переходы
ok/stale в зависимости от свежести тиков и часов рынка. Плюс core.market_hours
(гоняется и без forexconnect — чистый модуль).

Run:  python3.10 tests/test_hub_health.py
      python3.7  tests/test_hub_health.py
"""

import os
import sys
import time
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.market_hours import forex_open
from hub import Hub


def utc(y, mo, d, h, mi=0):
    """Unix-время момента UTC."""
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp()


class TestForexOpen(unittest.TestCase):

    def test_boundaries(self):
        """Границы форекс-недели (пт 22:00 UTC → вс 22:00 UTC закрыт)."""
        self.assertTrue(forex_open(utc(2026, 7, 15, 12)))    # среда
        self.assertFalse(forex_open(utc(2026, 7, 18, 12)))   # суббота
        self.assertTrue(forex_open(utc(2026, 7, 17, 21)))    # пт до 22
        self.assertFalse(forex_open(utc(2026, 7, 17, 22)))   # пт после 22
        self.assertFalse(forex_open(utc(2026, 7, 19, 21)))   # вс до 22
        self.assertTrue(forex_open(utc(2026, 7, 19, 22)))    # вс после 22


class TestHubHealth(unittest.TestCase):

    def _hub(self):
        """Hub без запуска (health не трогает сеть/БД)."""
        hub = Hub.__new__(Hub)
        hub._clients        = {}
        hub._range_builders = {}
        hub.ticks_received  = 0
        hub.db_dropped      = 0
        hub._started_ts     = time.time() - 10
        hub._last_tick_ts   = 0.0
        hub._last_tick_by_symbol = {}

        class _Q:
            def qsize(self):
                return 0
        hub._db_queue = _Q()
        return hub

    def test_structure(self):
        """health() отдаёт ожидаемые поля."""
        h = self._hub().health()
        for key in ("status", "uptime", "market_open", "data_age",
                    "clients", "ticks", "db_queue", "db_dropped"):
            self.assertIn(key, h)
        self.assertGreaterEqual(h["uptime"], 10)

    def test_no_ticks_yet(self):
        """Тиков ещё не было → data_age=None."""
        h = self._hub().health()
        self.assertIsNone(h["data_age"])

    def test_fresh_tick_ok(self):
        """Свежий тик → status ok, data_age мал."""
        hub = self._hub()
        hub._last_tick_ts = time.time() - 2
        h = hub.health()
        self.assertEqual(h["status"], "ok")
        self.assertLess(h["data_age"], 10)

    def test_symbols_age_per_symbol(self):
        """symbols_age отдаёт возраст последнего тика по каждому символу."""
        hub = self._hub()
        now = time.time()
        hub._last_tick_by_symbol = {"EUR/USD": now - 3, "USD/JPY": now - 100}
        h = hub.health()
        self.assertIn("EUR/USD", h["symbols_age"])
        self.assertLess(h["symbols_age"]["EUR/USD"], 10)
        self.assertGreater(h["symbols_age"]["USD/JPY"], 50)
        # символ без тиков в словаре отсутствует (фронт трактует как «закрыт»)
        self.assertNotIn("AUD/USD", h["symbols_age"])

    def test_stale_only_when_market_open(self):
        """Старый тик: stale в открытый рынок, ok в закрытый.

        market_open() внутри health() смотрит на РЕАЛЬНОЕ время, поэтому
        сверяем ветку с тем, открыт ли рынок прямо сейчас — детерминированно
        для обоих случаев.
        """
        hub = self._hub()
        hub._last_tick_ts = time.time() - 600   # 10 минут тишины
        h = hub.health()
        if forex_open():
            self.assertEqual(h["status"], "stale")
        else:
            self.assertEqual(h["status"], "ok")   # выходные — тишина легитимна


if __name__ == "__main__":
    unittest.main(verbosity=1)
