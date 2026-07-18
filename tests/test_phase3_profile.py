"""Фаза 3, слой 4: профиль объёма.

Запуск: python3.10 tests/test_phase3_profile.py
        python3.7  tests/test_phase3_profile.py

Ключевое свойство, которое здесь защищается, — УСТОЙЧИВОСТЬ СЕТКИ. Индекс
корзины обязан зависеть только от цены. Стоит привязать шаг к «текущей
цене», и границы поедут при движении рынка: профили за разные дни станут
несопоставимы, а накопленное за сегодня — несовместимо с тем, что записали
утром.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db import init_db, load_volume_profile, save_volume_profile
from core.volume_profile import (DEFAULT_STEP, VolumeProfile, bucket_bounds,
                                 bucket_index)


class TestBucketGrid(unittest.TestCase):
    """Логарифмическая сетка корзин."""

    def test_index_depends_only_on_price(self):
        """Одна цена всегда даёт один индекс, сколько ни считай."""
        self.assertEqual(bucket_index(64000.0), bucket_index(64000.0))

    def test_step_is_constant_percentage_at_any_scale(self):
        """Ширина корзины — заданный процент и для BTC, и для монеты за $2."""
        for price in (64000.0, 1857.0, 59.88, 2.2073, 0.0001):
            low, high = bucket_bounds(bucket_index(price))
            self.assertAlmostEqual((high - low) / low, DEFAULT_STEP, places=9)

    def test_price_falls_inside_its_own_bucket(self):
        """Цена лежит внутри границ своей корзины."""
        for price in (64000.0, 2.2073, 0.5):
            low, high = bucket_bounds(bucket_index(price))
            self.assertLessEqual(low, price)
            self.assertLess(price, high)

    def test_adjacent_buckets_are_contiguous(self):
        """Верх корзины совпадает с низом следующей — дыр в сетке нет."""
        idx = bucket_index(64000.0)
        _, high = bucket_bounds(idx)
        next_low, _ = bucket_bounds(idx + 1)
        self.assertAlmostEqual(high, next_low, places=6)

    def test_non_positive_price_rejected(self):
        """Цена <= 0 отвергается, а не сваливает объём в корзину 0."""
        for bad in (0.0, -1.0):
            with self.assertRaises(ValueError):
                bucket_index(bad)


class TestProfileAccumulation(unittest.TestCase):
    """Накопление объёма по корзинам."""

    def test_totals_match_input_trades(self):
        """Сумма по корзинам равна сумме по сделкам."""
        profile = VolumeProfile()
        trades = [(64000.0, 1.5, "buy"), (64010.0, 0.5, "sell"),
                  (63000.0, 2.0, "buy")]
        for price, size, side in trades:
            profile.add(price, size, side)

        self.assertAlmostEqual(profile.total_base(),
                               sum(t[1] for t in trades))
        self.assertAlmostEqual(sum(r[5] for r in profile.to_rows()),
                               sum(t[0] * t[1] for t in trades))

    def test_sides_are_separated(self):
        """Покупки и продажи копятся раздельно."""
        profile = VolumeProfile()
        profile.add(64000.0, 3.0, "buy")
        profile.add(64000.0, 1.0, "sell")
        row = profile.to_rows()[0]
        self.assertEqual(row[3], 3.0)
        self.assertEqual(row[4], 1.0)

    def test_trade_without_side_counts_in_volume_only(self):
        """Сделка без стороны попадает в объём, но не в buy/sell."""
        profile = VolumeProfile()
        profile.add(64000.0, 2.0, None)
        row = profile.to_rows()[0]
        self.assertEqual(row[3], 0.0)
        self.assertEqual(row[4], 0.0)
        self.assertAlmostEqual(row[5], 128000.0)

    def test_poc_is_the_heaviest_bucket(self):
        """POC указывает на корзину с наибольшим объёмом."""
        profile = VolumeProfile()
        profile.add(64000.0, 1.0, "buy")
        profile.add(50000.0, 9.0, "buy")
        poc = profile.poc()
        self.assertEqual(poc[0], bucket_index(50000.0))
        self.assertAlmostEqual(poc[3], 9.0)

    def test_value_area_contains_poc_and_target_share(self):
        """Область стоимости накрывает POC и набирает заданную долю объёма."""
        profile = VolumeProfile()
        for i in range(20):
            # Основная масса — в узком диапазоне, хвосты — редкие.
            profile.add(64000.0 + i, 10.0 if 5 <= i <= 9 else 0.2, "buy")

        poc = profile.poc()
        low, high = profile.value_area(0.7)
        self.assertLessEqual(low, poc[2])
        self.assertGreaterEqual(high, poc[1])

        inside = sum(r[3] + r[4] for r in profile.to_rows()
                     if low <= r[1] and r[2] <= high)
        self.assertGreaterEqual(inside / profile.total_base(), 0.7)

    def test_empty_profile_has_no_poc(self):
        """Пустой профиль не выдумывает POC."""
        profile = VolumeProfile()
        self.assertIsNone(profile.poc())
        self.assertIsNone(profile.value_area())


class TestProfileRoundtrip(unittest.TestCase):
    """Выгрузка, загрузка и хранение."""

    def setUp(self):
        """Создать путь к временной БД.

        Returns:
            None.
        """
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.path)

    def tearDown(self):
        """Удалить временные файлы БД.

        Returns:
            None.
        """
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.path + suffix)
            except OSError:
                pass

    def test_load_rows_restores_state(self):
        """Профиль, выгруженный и загруженный обратно, идентичен исходному."""
        original = VolumeProfile()
        for price, size, side in ((64000.0, 1.5, "buy"), (63000.0, 2.0, "sell")):
            original.add(price, size, side)

        restored = VolumeProfile()
        restored.load_rows([(r[0], r[3], r[4], r[5]) for r in original.to_rows()])

        self.assertAlmostEqual(restored.total_base(), original.total_base())
        self.assertEqual(restored.poc()[0], original.poc()[0])

    def test_db_roundtrip(self):
        """Профиль переживает запись в БД и чтение обратно."""
        conn = init_db(self.path)
        profile = VolumeProfile()
        profile.add(64000.0, 1.5, "buy")
        profile.add(63000.0, 2.0, "sell")

        save_volume_profile(conn, "lighter", "BTC", "20260718", profile.to_rows())
        rows = load_volume_profile(conn, "lighter", "BTC", "20260718")

        self.assertEqual(len(rows), len(profile))
        self.assertAlmostEqual(sum(r["vol_buy"] + r["vol_sell"] for r in rows),
                               profile.total_base())

    def test_save_is_idempotent(self):
        """Повторная запись того же периода заменяет строки, а не множит их.

        Профиль текущих суток пишется многократно по мере накопления —
        накапливались бы копии, объём удваивался бы на каждом сбросе.
        """
        conn = init_db(self.path)
        profile = VolumeProfile()
        profile.add(64000.0, 1.0, "buy")
        rows = profile.to_rows()

        save_volume_profile(conn, "lighter", "BTC", "20260718", rows)
        save_volume_profile(conn, "lighter", "BTC", "20260718", rows)

        stored = load_volume_profile(conn, "lighter", "BTC", "20260718")
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["vol_buy"], 1.0)

    def test_restart_continues_from_saved_state(self):
        """Профиль после рестарта продолжает накопленное, а не начинает с нуля.

        Воспроизводит боевой сценарий: хаб перезапустили в середине суток,
        профиль текущего периода обязан подхватиться из БД.
        """
        conn = init_db(self.path)

        before = VolumeProfile()
        before.add(64000.0, 5.0, "buy")
        save_volume_profile(conn, "lighter", "BTC", "20260718", before.to_rows())

        # «Рестарт»: новый объект, состояние только из БД.
        after = VolumeProfile()
        saved = load_volume_profile(conn, "lighter", "BTC", "20260718")
        after.load_rows([(r["bucket"], r["vol_buy"], r["vol_sell"], r["vol_quote"])
                         for r in saved])
        after.add(64000.0, 3.0, "buy")

        self.assertAlmostEqual(after.total_base(), 8.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
