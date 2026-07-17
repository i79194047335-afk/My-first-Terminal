"""
Tests for core/range_bars.py (рэндж-бары, подготовка к внедрению в хаб).

Синтетика проверяет контракт билдера (пробой, непрерывность, уникальность
времени, гэп, потолок истории, seed); интеграционный тест гоняет реальный
торговый день EUR/USD из тикового архива и проверяет инварианты на всём
результате.

Run:  python3.7 tests/test_range_bars.py
      python3.10 tests/test_range_bars.py
"""

import glob
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.range_bars import RangeBarBuilder, backfill, iter_ticks

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Тики под .gitignore: в worktree data — симлинк на боевой архив, но на чужой
# машине его может не быть — интеграционные тесты тогда скипаются.
DATA_DIRS = [
    os.path.join(ROOT, "data"),
    "/root/projects/terminal/data",
]

R = 0.0010  # 10 пипсов EUR/USD — диапазон для синтетики


def walk(builder, steps, t0=1000.0, dt=1.0):
    """Прогнать цепочку цен через билдер с шагом времени dt.

    Args:
        builder: RangeBarBuilder.
        steps:   Список цен.
        t0:      Время первого тика.
        dt:      Шаг времени между тиками.

    Returns:
        Список закрытых баров, отданных ingest по ходу прогона.
    """
    closed = []
    for i, price in enumerate(steps):
        bar = builder.ingest(price, t0 + i * dt)
        if bar:
            closed.append(bar)
    return closed


class TestRangeBarBuilder(unittest.TestCase):

    def test_no_close_below_range(self):
        """Пока high-low < R, бар не закрывается, current копит OHLC."""
        b = RangeBarBuilder(R)
        closed = walk(b, [1.1000, 1.1004, 1.0998, 1.1003])
        self.assertEqual(closed, [])
        cur = b.current()
        self.assertEqual(cur["open"],  1.1000)
        self.assertEqual(cur["high"],  1.1004)
        self.assertEqual(cur["low"],   1.0998)
        self.assertEqual(cur["close"], 1.1003)

    def test_closes_on_breach_inclusive(self):
        """Бар закрывается тем тиком, что довёл диапазон до R, и включает его."""
        b = RangeBarBuilder(R)
        closed = walk(b, [1.1000, 1.1005, 1.1010])   # ровно R от low
        self.assertEqual(len(closed), 1)
        bar = closed[0]
        self.assertEqual(bar["close"], 1.1010)
        self.assertAlmostEqual(bar["high"] - bar["low"], R, places=10)

    def test_open_equals_prev_close(self):
        """Непрерывность: open нового бара = close закрытого."""
        b = RangeBarBuilder(R)
        closed = walk(b, [1.1000, 1.1010, 1.1015, 1.1020])
        self.assertEqual(len(closed), 2)
        self.assertEqual(b.current()["open"], closed[-1]["close"])
        self.assertEqual(closed[1]["open"], closed[0]["close"])

    def test_gap_single_honest_bar(self):
        """Гэп в 5R — ОДИН широкий бар, без фантомной лесенки."""
        b = RangeBarBuilder(R)
        closed = walk(b, [1.1000, 1.1002, 1.1052])   # прыжок 50 пипсов
        self.assertEqual(len(closed), 1)
        bar = closed[0]
        self.assertAlmostEqual(bar["high"] - bar["low"], 0.0052, places=10)
        self.assertEqual(bar["close"], 1.1052)
        # следующий бар открыт от цены пробойщика
        self.assertEqual(b.current()["open"], 1.1052)

    def test_time_unique_ascending_on_burst(self):
        """Несколько закрытий в одну секунду → время бампится, LWC доволен."""
        b = RangeBarBuilder(R)
        # 4 пробоя подряд с dt=0 (одна и та же секунда)
        prices = [1.1000, 1.1010, 1.1020, 1.1030, 1.1040]
        closed = walk(b, prices, t0=5000.0, dt=0.0)
        self.assertEqual(len(closed), 4)
        times = [bar["time"] for bar in closed]
        self.assertEqual(times, sorted(set(times)), "время неуникально/убывает")

    def test_max_bars_cap(self):
        """History не растёт выше max_bars."""
        b = RangeBarBuilder(R, max_bars=3)
        prices = [1.1000]
        for i in range(1, 11):
            prices.append(1.1000 + i * R)            # каждый тик — пробой
        walk(b, prices)
        self.assertEqual(len(b.history()), 3)

    def test_seed_history_continuity(self):
        """После seed первый тик открывает бар от close последнего из истории."""
        b = RangeBarBuilder(R)
        b.seed_history([{"time": 100, "open": 1.1000, "high": 1.1010,
                         "low": 1.1000, "close": 1.1010}])
        b.ingest(1.1013, 200.0)
        cur = b.current()
        self.assertEqual(cur["open"], 1.1010)
        self.assertEqual(cur["high"], 1.1013)

    def test_rejects_bad_range(self):
        """range_size <= 0 — ошибка сразу, а не мусор в данных."""
        for bad in (0, -1, None):
            with self.assertRaises((ValueError, TypeError)):
                RangeBarBuilder(bad)


class TestBackfillRealTicks(unittest.TestCase):
    """Инварианты на реальном торговом дне из архива (skip, если архива нет)."""

    @classmethod
    def setUpClass(cls):
        files = []
        for d in DATA_DIRS:
            files.extend(glob.glob(os.path.join(d, "EURUSD_*.csv")))
        if not files:
            raise unittest.SkipTest("тикового архива EURUSD нет")
        cls.data_dir = os.path.dirname(max(files, key=os.path.getsize))

    def test_real_day_invariants(self):
        """R=5 пипсов на реальных тиках: диапазон, непрерывность, время."""
        r = 0.0005
        builder = backfill(self.data_dir, "EUR/USD", r, max_bars=2000)
        bars = builder.history()
        self.assertGreater(len(bars), 10, "слишком мало баров — что-то не так")

        prev = None
        for bar in bars:
            self.assertGreaterEqual(bar["high"] - bar["low"], r - 1e-12)
            self.assertGreaterEqual(bar["high"], max(bar["open"], bar["close"]))
            self.assertLessEqual(bar["low"], min(bar["open"], bar["close"]))
            if prev is not None:
                self.assertGreater(bar["time"], prev["time"])
                self.assertEqual(bar["open"], prev["close"])
            prev = bar

        # Живой бар открыт и непрерывен с последним закрытым
        cur = builder.current()
        if cur is not None and bars:
            self.assertEqual(cur["open"], bars[-1]["close"])

    def test_since_ts_filters(self):
        """since_ts режет старые тики (и целые файлы — по имени)."""
        all_ticks = iter_ticks(self.data_dir, "EUR/USD")
        first_price, first_ts = next(all_ticks)
        later = iter_ticks(self.data_dir, "EUR/USD",
                           since_ts=first_ts + 3600)
        _, ts = next(later)
        self.assertGreaterEqual(ts, first_ts + 3600)


if __name__ == "__main__":
    unittest.main(verbosity=1)
