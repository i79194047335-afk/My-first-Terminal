"""Фаза 3, слой 5: лента крупных сделок.

Запуск: python3.10 tests/test_phase3_large_trades.py
        python3.7  tests/test_phase3_large_trades.py

Главное, что защищается, — ФИЛЬТР МОЛЧИТ, ПОКА НЕ НАБРАЛ СТАТИСТИКУ.
Процентиль по десятку сделок объявил бы «крупной» случайную мелочь; на
неликвидном инструменте в выходные (XAU в субботу: 2841 сделка за сутки)
это была бы вся лента целиком.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.large_trades import (MIN_SAMPLES, LargeTradeFilter, bucket_ceiling,
                               bucket_floor, size_bucket)


class TestSizeGrid(unittest.TestCase):
    """Логарифмическая сетка размеров."""

    def test_bucket_is_monotonic(self):
        """Больший размер — не меньший индекс корзины."""
        prev = size_bucket(1e-6)
        for size in (1e-5, 1e-4, 0.001, 0.03, 1.0, 37.99):
            current = size_bucket(size)
            self.assertGreaterEqual(current, prev)
            prev = current

    def test_size_lands_at_or_above_bucket_floor(self):
        """Размер не меньше нижней границы своей корзины."""
        for size in (0.00117, 0.03, 0.1998, 37.99):
            self.assertLessEqual(bucket_floor(size_bucket(size)), size)

    def test_non_positive_size_does_not_crash(self):
        """Странный размер не роняет ленту — рыночные данные важнее."""
        self.assertEqual(size_bucket(0.0), 0)
        self.assertEqual(size_bucket(-1.0), 0)


class TestWarmup(unittest.TestCase):
    """Поведение до накопления статистики."""

    def test_silent_until_min_samples(self):
        """Пока окно не набрано, крупных сделок не бывает."""
        flt = LargeTradeFilter()
        for i in range(MIN_SAMPLES - 1):
            # Даже заведомо гигантская сделка не должна пройти.
            self.assertFalse(flt.add(1000.0, 1000.0 + i)[0])
        self.assertIsNone(flt.threshold())

    def test_threshold_appears_after_warmup(self):
        """Порог появляется, когда данных достаточно."""
        flt = LargeTradeFilter()
        for i in range(MIN_SAMPLES + 50):
            flt.add(0.001, 1000.0 + i)
        self.assertIsNotNone(flt.threshold())


class TestPercentileBehaviour(unittest.TestCase):
    """Отбор крупных сделок."""

    def _warm(self, flt, base_size=0.001, count=None, start=1000.0):
        """Наполнить окно однородными сделками.

        Args:
            flt:       Фильтр.
            base_size: Размер каждой сделки.
            count:     Сколько сделок (по умолчанию MIN_SAMPLES + 100).
            start:     Время первой сделки.

        Returns:
            Время последней добавленной сделки.
        """
        count = count or (MIN_SAMPLES + 100)
        for i in range(count):
            flt.add(base_size, start + i)
        return start + count

    def test_outlier_is_flagged(self):
        """Сделка много крупнее фона попадает в ленту."""
        flt = LargeTradeFilter()
        ts = self._warm(flt)
        self.assertTrue(flt.add(10.0, ts)[0])

    def test_typical_trade_is_not_flagged(self):
        """Рядовая сделка в ленту не попадает."""
        flt = LargeTradeFilter()
        ts = self._warm(flt)
        self.assertFalse(flt.add(0.001, ts)[0])

    def test_threshold_adapts_to_regime_change(self):
        """Порог поднимается, когда рынок разгоняется.

        Ради этого и выбран относительный порог: абсолютный пришлось бы
        подкручивать руками при каждой смене режима.
        """
        flt = LargeTradeFilter()
        ts = self._warm(flt, base_size=0.001)
        quiet = flt.threshold()

        for i in range(2000):
            flt.add(1.0, ts + i)
        busy = flt.threshold()

        self.assertGreater(busy, quiet)

    def test_uniform_window_does_not_flag_everything(self):
        """Окно из одинаковых сделок не делает крупной каждую.

        Размеры на бирже дискретны: 23.7% всех сделок BTC имеют размер ровно
        0.0002 (боты с фиксированным лотом). Если брать порогом НИЖНЮЮ
        границу корзины, каждая такая сделка формально её превышает и лента
        заливается целиком. Регрессия, найденная тестом.
        """
        flt = LargeTradeFilter()
        ts = self._warm(flt, base_size=0.0002, count=500)
        for i in range(50):
            self.assertFalse(flt.add(0.0002, ts + i)[0])

    def test_min_size_floor_applies(self):
        """Абсолютный минимум страхует от «крупной» мелочи."""
        flt = LargeTradeFilter(min_size=5.0)
        ts = self._warm(flt, base_size=0.001)
        self.assertGreaterEqual(flt.threshold(), 5.0)
        self.assertFalse(flt.add(1.0, ts)[0])
        self.assertTrue(flt.add(50.0, ts + 1)[0])


class TestReportedThreshold(unittest.TestCase):
    """Порог, возвращаемый вместе с решением."""

    def test_returned_threshold_justifies_the_decision(self):
        """Для крупной сделки всегда size > возвращённого порога.

        Порог обязан возвращаться из add(), а не браться потом через
        threshold(): кеш обновляется раз в THRESHOLD_REFRESH сделок, и между
        решением и отчётом значение успевает измениться. В живом прогоне это
        дало событие с size <= threshold — выглядит как ошибка фильтра,
        хотя фильтр отработал верно.
        """
        import random

        random.seed(20260719)
        flt = LargeTradeFilter()
        checked = 0
        for i in range(30000):
            size = random.lognormvariate(-6.0, 2.0)
            is_large, level = flt.add(size, 1000.0 + i * 0.1)
            if is_large:
                checked += 1
                self.assertIsNotNone(level)
                self.assertGreater(size, level)
        self.assertGreater(checked, 100)

    def test_threshold_is_none_before_warmup(self):
        """До разогрева порог не выдумывается."""
        flt = LargeTradeFilter()
        is_large, level = flt.add(1000.0, 1000.0)
        self.assertFalse(is_large)
        self.assertIsNone(level)


class TestSlidingWindow(unittest.TestCase):
    """Скольжение окна."""

    def test_old_trades_leave_the_window(self):
        """Сделки старше окна перестают учитываться."""
        flt = LargeTradeFilter(window_sec=100, min_samples=10)
        for i in range(50):
            flt.add(0.001, 1000.0 + i)
        self.assertEqual(flt.samples, 50)

        flt.add(0.001, 2000.0)   # далеко за окном
        self.assertEqual(flt.samples, 1)

    def test_empty_buckets_are_dropped(self):
        """Опустевшие корзины удаляются, словарь не растёт вечно."""
        flt = LargeTradeFilter(window_sec=10, min_samples=5)
        for i in range(100):
            flt.add(0.001 * (i + 1), 1000.0 + i)
        # За окно ушло большинство сделок — счётчиков должно остаться мало.
        self.assertLessEqual(len(flt._counts), 20)

    def test_threshold_refreshes_as_window_moves(self):
        """Кеш порога не залипает: окно сдвинулось — порог пересчитан."""
        flt = LargeTradeFilter(window_sec=3600, min_samples=10)
        for i in range(500):
            flt.add(0.001, 1000.0 + i)
        low = flt.threshold()

        for i in range(500):
            flt.add(5.0, 1600.0 + i)
        high = flt.threshold()

        self.assertGreater(high, low)


class TestAccuracy(unittest.TestCase):
    """Точность приближения."""

    def test_close_to_exact_percentile(self):
        """Гистограмма близка к честной сортировке.

        Ошибка ограничена шириной корзины (~1%); систематического смещения
        быть не должно.
        """
        import random

        random.seed(20260719)
        sizes = [random.lognormvariate(-6.0, 2.0) for _ in range(20000)]

        flt = LargeTradeFilter()
        for i, size in enumerate(sizes):
            flt.add(size, 1000.0 + i * 0.1)

        exact = sorted(sizes)[int(len(sizes) * 0.95)]
        approx = flt.threshold()
        self.assertLess(abs(approx - exact) / exact, 0.02)


if __name__ == "__main__":
    unittest.main(verbosity=2)
