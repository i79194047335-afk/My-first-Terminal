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

from core.range_bars import RangeBarBuilder, backfill, backfill_tail, iter_ticks

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

    def test_open_from_breaker_price(self):
        """Новый бар открывается от цены тика-пробойщика (= close закрытого)."""
        b = RangeBarBuilder(R)
        closed = walk(b, [1.1000, 1.1010, 1.1015, 1.1020])
        self.assertEqual(len(closed), 2)
        # Пробой ровный — open совпадает с close предыдущего, но по механике
        # это цена тика-пробойщика, а не подтяжка (проверяется в gap-тесте).
        self.assertEqual(b.current()["open"], closed[-1]["close"])

    def test_gap_tick_closes_bar_and_leaves_visible_gap(self):
        """Гэп-тик (скачок > R) закрывает бар КАК ЕСТЬ и оставляет разрыв.

        Раньше такой тик вливался в бар и раздувал его размах до десятков
        пипсов (бары по 30-50п при R=10). Теперь бар закрывается до скачка,
        а разрыв виден как гэп close→open между барами.
        """
        b = RangeBarBuilder(R)
        # 1.1000→1.1010 закрывает bar0 (o=1.1000 c=1.1010), новый бар — точка
        # 1.1010; тик 1.1060 отстоит от close на 50п (> R) → ГЭП: закрывает
        # бар-точку как есть (размах 0), открывает новый от 1.1060.
        closed = walk(b, [1.1000, 1.1010, 1.1060])
        self.assertEqual(len(closed), 2)
        self.assertEqual(closed[0]["close"], 1.1010)
        # Второй закрытый — «точка» до гэпа, НЕ растянут скачком.
        gap_bar = closed[1]
        self.assertEqual(gap_bar["high"] - gap_bar["low"], 0.0)
        self.assertEqual(gap_bar["close"], 1.1010)
        # Разрыв виден: close закрытого 1.1010, а новый бар открыт от 1.1060.
        self.assertEqual(b.current()["open"], 1.1060)

    def test_fast_move_within_R_not_treated_as_gap(self):
        """Быстрый тик В ПРЕДЕЛАХ R — обычное расширение, не гэп."""
        b = RangeBarBuilder(R)
        # шаги по 4-5п (< R) — нормальный ход, гэп-ветка не срабатывает
        closed = walk(b, [1.1000, 1.1004, 1.1009, 1.1013])
        self.assertEqual(len(closed), 1)
        bar = closed[0]
        # закрылся расширением, размах >= R (не нулевой бар-точка), и не
        # раздут гэпом: превышение над R меньше R (шаги в тесте < R → размах < 2R).
        self.assertGreaterEqual(bar["high"] - bar["low"], R - 1e-12)
        self.assertLess(bar["high"] - bar["low"], 2 * R)

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

    def test_seed_history_then_first_tick_opens_from_tick(self):
        """После seed первый тик открывает бар от СВОЕЙ цены (не от close истории)."""
        b = RangeBarBuilder(R)
        b.seed_history([{"time": 100, "open": 1.1000, "high": 1.1010,
                         "low": 1.1000, "close": 1.1010}])
        b.ingest(1.1013, 200.0)
        cur = b.current()
        # Бар открыт от цены тика 1.1013 (одна точка), а не подтянут к 1.1010.
        self.assertEqual(cur["open"], 1.1013)
        self.assertEqual(cur["high"], 1.1013)
        self.assertEqual(cur["low"], 1.1013)

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
        """R=5 пипсов на реальных тиках: диапазон, OHLC, время, размах ≈ R."""
        r = 0.0005
        builder = backfill(self.data_dir, "EUR/USD", r, max_bars=2000)
        bars = builder.history()
        self.assertGreater(len(bars), 10, "слишком мало баров — что-то не так")

        prev = None
        gaps = 0
        for bar in bars:
            # OHLC-консистентность обязана держаться на ЛЮБОМ баре.
            self.assertGreaterEqual(bar["high"], max(bar["open"], bar["close"]))
            self.assertLessEqual(bar["low"], min(bar["open"], bar["close"]))
            # Размах закрытого бара не превышает R больше чем на «один тик»
            # (дискретность котировки). Гэп-бары («точки» перед разрывом) имеют
            # размах ~0 — это норма; раздутых баров (десятки пипсов) быть НЕ
            # должно, ради чего и вводилась гэп-логика.
            span = (bar["high"] - bar["low"]) / 0.0001
            self.assertLess(span, 20, "раздутый бар %.1fп — гэп не отсечён" % span)
            if prev is not None:
                self.assertGreater(bar["time"], prev["time"])
                if bar["open"] != prev["close"]:
                    gaps += 1   # разрыв — норма на выходных/новостях
            prev = bar

        # Гэпы редки: на месяце тиков их единицы, не большинство.
        self.assertLess(gaps, len(bars) * 0.05,
                        "слишком много разрывов — что-то не так с логикой гэпа")

    def test_since_ts_filters(self):
        """since_ts режет старые тики (и целые файлы — по имени)."""
        # iter_ticks отдаёт (price, ts, size, side): size/side есть в архиве
        # СДЕЛОК (Lighter) и равны None в архиве котировок (FXCM).
        all_ticks = iter_ticks(self.data_dir, "EUR/USD")
        first_price, first_ts, first_size, first_side = next(all_ticks)
        self.assertIsNone(first_size)
        self.assertIsNone(first_side)
        later = iter_ticks(self.data_dir, "EUR/USD",
                           since_ts=first_ts + 3600)
        _, ts, _, _ = next(later)
        self.assertGreaterEqual(ts, first_ts + 3600)

    def test_backfill_tail_matches_full(self):
        """backfill_tail даёт тот же хвост, что полный backfill, но читая меньше.

        Хвост (закрытые бары) обязан совпасть до тика: путь-зависимость якоря
        затухает на нескольких днях. Живой бар может отличаться на 1 тик
        (срез архива по границе суток) — он всё равно тут же обновится потоком,
        поэтому сверяем только закрытую историю.
        """
        r = 0.0010   # R:100 поинтов — хвост в несколько дней, ветка «прицела»
        full = backfill(self.data_dir, "EUR/USD", r, max_bars=500)
        tail, last_ts = backfill_tail(self.data_dir, "EUR/USD", r, max_bars=500)

        fb, tb = full.history(), tail.history()
        self.assertEqual(len(tb), min(len(fb), 500))
        self.assertIsNotNone(last_ts)

        # Последние 200 закрытых баров совпадают полностью.
        n = min(len(fb), len(tb), 200)
        for i in range(1, n + 1):
            self.assertEqual(tb[-i]["time"],  fb[-i]["time"])
            self.assertEqual(tb[-i]["open"],  fb[-i]["open"])
            self.assertEqual(tb[-i]["close"], fb[-i]["close"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
