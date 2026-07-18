"""Фаза 3, слой 2: объём и дельта в свече.

Запуск: python3.10 tests/test_phase3_volume.py
        python3.7  tests/test_phase3_volume.py   (core обязан жить на обеих)

Главное, что здесь проверяется, — свечи FXCM НЕ ИЗМЕНИЛИСЬ. Нарезчик общий,
и через него идёт живой форекс-поток: лишний ключ в свече или выдуманный
нулевой объём сломали бы то, что работает с Фазы 1.
"""

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.bus import BusError, make_tick, validate
from core.candles import CandleBuilder, aggregate_higher_tf
from core.db import init_db, load_history, upsert_candle

OHLC_KEYS = {"time", "open", "high", "low", "close"}


class TestFxcmUnchanged(unittest.TestCase):
    """Провайдер без объёма обязан получать ровно те же свечи, что и раньше."""

    def test_candle_has_no_volume_keys(self):
        """Свеча без size не обрастает объёмными ключами."""
        b = CandleBuilder({"M1": 60})
        b.ingest(1.1000, 0)
        b.ingest(1.1005, 30)
        closed = b.ingest(1.1002, 60)
        self.assertEqual(set(closed["M1"]), OHLC_KEYS)

    def test_aggregate_has_no_volume_keys(self):
        """Агрегация M1→M5 без объёма не создаёт нулевых счётчиков."""
        src = [{"time": i * 60, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5}
               for i in range(5)]
        out = aggregate_higher_tf(src, 300)
        self.assertEqual(set(out[0]), OHLC_KEYS)

    def test_tick_without_side_is_valid(self):
        """Тик FXCM (без size/side) проходит валидацию шины."""
        validate(make_tick("fxcm", "EUR/USD", 1784402486.8, 1.17))


class TestVolumeAccumulation(unittest.TestCase):
    """Накопление объёма и дельты по сделкам."""

    def test_vol_quote_is_per_trade_not_vol_times_close(self):
        """vol_quote считается посделочно, а не как vol_base * close.

        Сделки внутри бара идут по разным ценам, поэтому произведение итогов
        не равно настоящему обороту. Это главная ловушка слоя.
        """
        b = CandleBuilder({"M1": 60})
        b.ingest(100.0, 0, size=2.0, side="buy")     # 200
        b.ingest(110.0, 30, size=1.0, side="sell")   # 110
        closed = b.ingest(120.0, 60, size=5.0, side="buy")
        c = closed["M1"]

        self.assertEqual(c["vol_base"], 3.0)
        self.assertEqual(c["vol_quote"], 310.0)
        self.assertNotEqual(c["vol_quote"], c["vol_base"] * c["close"])

    def test_delta_is_aggressor_imbalance(self):
        """Дельта = покупки минус продажи в базовых единицах."""
        b = CandleBuilder({"M1": 60})
        b.ingest(100.0, 0, size=2.0, side="buy")
        b.ingest(100.0, 10, size=0.5, side="sell")
        closed = b.ingest(100.0, 60, size=1.0, side="buy")
        self.assertAlmostEqual(closed["M1"]["delta"], 1.5)

    def test_closing_tick_volume_belongs_to_next_candle(self):
        """Объём тика, закрывшего бар, попадает в НОВУЮ свечу.

        Тик торговался уже в следующем бакете: приписать его закрытой свече —
        значит завысить её оборот и занизить оборот новой.
        """
        b = CandleBuilder({"M1": 60})
        b.ingest(100.0, 0, size=2.0, side="buy")
        closed = b.ingest(110.0, 60, size=7.0, side="buy")

        self.assertEqual(closed["M1"]["vol_base"], 2.0)
        self.assertEqual(b.current("M1")["vol_base"], 7.0)

    def test_size_without_side_gives_volume_but_no_delta(self):
        """Без стороны объём копится, а дельта не выдумывается."""
        b = CandleBuilder({"M1": 60})
        b.ingest(50.0, 0, size=1.0)
        c = b.current("M1")
        self.assertEqual(c["vol_base"], 1.0)
        self.assertNotIn("delta", c)

    def test_aggregate_sums_volume(self):
        """M1→M5 суммирует объём и дельту."""
        src = [{"time": i * 60, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5,
                "vol_base": 2.0, "vol_quote": 200.0,
                "delta": 1.0 if i % 2 else -1.0}
               for i in range(5)]
        out = aggregate_higher_tf(src, 300)
        self.assertEqual(out[0]["vol_base"], 10.0)
        self.assertEqual(out[0]["vol_quote"], 1000.0)
        self.assertEqual(out[0]["delta"], -1.0)


class TestBusSide(unittest.TestCase):
    """Сторона агрессора в контракте шины."""

    def test_valid_sides_pass(self):
        """buy/sell/None принимаются."""
        for side in ("buy", "sell", None):
            validate(make_tick("lighter", "BTC", 1784402486.8, 64527.8,
                               size=0.0031, side=side))

    def test_bad_sides_rejected(self):
        """Опечатка в стороне отбивается на границе, а не портит дельту молча."""
        for bad in ("BUY", "b", "long", 1, True):
            with self.assertRaises(BusError):
                validate(make_tick("lighter", "BTC", 1784402486.8, 64527.8,
                                   size=0.0031, side=bad))


class TestDeltaPersistence(unittest.TestCase):
    """Хранение дельты и миграция существующей базы."""

    def setUp(self):
        """Создать временный файл БД.

        Returns:
            None.
        """
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.path)

    def tearDown(self):
        """Удалить временный файл БД.

        Returns:
            None.
        """
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.path + suffix)
            except OSError:
                pass

    def test_delta_roundtrip(self):
        """Дельта переживает запись и чтение."""
        conn = init_db(self.path)
        upsert_candle(conn, "lighter", "BTC", "M1",
                      {"time": 60, "open": 1.0, "high": 2.0, "low": 0.5,
                       "close": 1.5, "vol_base": 3.0, "vol_quote": 310.0,
                       "delta": -1.5})
        row = load_history(conn, "lighter", "BTC", "M1")[0]
        self.assertEqual(row["delta"], -1.5)

    def test_fxcm_row_has_no_delta_key(self):
        """Свеча без дельты читается в прежней форме, без delta=None."""
        conn = init_db(self.path)
        upsert_candle(conn, "fxcm", "EUR/USD", "M1",
                      {"time": 60, "open": 1.1, "high": 1.2,
                       "low": 1.0, "close": 1.15})
        row = load_history(conn, "fxcm", "EUR/USD", "M1")[0]
        self.assertNotIn("delta", row)

    def test_migration_adds_column_and_keeps_rows(self):
        """База, созданная без delta, мигрирует без потери свечей.

        Воспроизводит боевой случай: market.db создана до Фазы 3, а
        CREATE TABLE IF NOT EXISTS существующую таблицу не трогает.
        """
        legacy = sqlite3.connect(self.path)
        legacy.executescript("""
            CREATE TABLE candles (
                provider TEXT NOT NULL, symbol TEXT NOT NULL,
                tf TEXT NOT NULL, time INTEGER NOT NULL,
                o REAL NOT NULL, h REAL NOT NULL,
                l REAL NOT NULL, c REAL NOT NULL,
                vol_base REAL, vol_quote REAL,
                PRIMARY KEY (provider, symbol, tf, time)
            ) WITHOUT ROWID;
        """)
        legacy.execute(
            "INSERT INTO candles VALUES ('fxcm','EUR/USD','M1',60,1.1,1.2,1.0,1.15,NULL,NULL)")
        legacy.commit()
        legacy.close()

        conn = init_db(self.path)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(candles)")}
        self.assertIn("delta", columns)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM candles").fetchone()[0], 1)

        # Старая свеча читается и остаётся без ключа delta.
        row = load_history(conn, "fxcm", "EUR/USD", "M1")[0]
        self.assertNotIn("delta", row)


if __name__ == "__main__":
    unittest.main(verbosity=2)
