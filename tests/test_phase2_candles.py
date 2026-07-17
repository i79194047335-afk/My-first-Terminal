"""
Tests for the grid-offset support in core/candles.py (Фаза 2.3).

Проверяет главное: CandleBuilder режет тики так же, как боевой server.py, и
уважает сетку брокера (FXCM H4 = 01/05/09/13/17/21 UTC, D1 = 21:00 UTC).
Нарезка сверяется на РЕАЛЬНЫХ тиках из data/*.csv и реальных барах market.db.

Run:  python3.7 tests/test_phase2_candles.py
"""

import csv
import glob
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.candles import CandleBuilder, aggregate_higher_tf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Тики тоже под .gitignore: в worktree лежат обрезки на пару килобайт, полные
# дни — только в проде. Ищем по обеим папкам, берём самый крупный файл.
DATA_DIRS = [
    os.path.join(ROOT, "data"),
    "/root/projects/terminal/data",
]

# market.db под .gitignore → в worktree её нет. Читаем боевую, СТРОГО read-only
# (mode=ro ниже): сверять сетку надо на настоящих барах брокера, синтетика тут
# ничего не докажет.
DB_CANDIDATES = [
    os.path.join(ROOT, "market.db"),
    "/root/projects/terminal/market.db",
]
DB_PATH = next((p for p in DB_CANDIDATES if os.path.exists(p)), DB_CANDIDATES[0])

# Копия боевой конфигурации server.py — если она разъедется, тест это покажет.
TF_SECONDS = {
    "S5": 5, "S10": 10, "S15": 15, "S30": 30,
    "M1": 60, "M3": 180, "M5": 300, "M15": 900,
    "H1": 3600, "H4": 14400, "D1": 86400,
}
DIRECT_LOAD_TF = ("H1", "H4", "D1")


# ── эталон: нарезка, скопированная из server.py:process_tick ────────────

def reference_slice(ticks, tf_defs, tf_offsets):
    """Reference tick slicing, copied verbatim from server.py:process_tick.

    Держится отдельно от core/candles.py намеренно: тест сравнивает две
    независимые реализации, а не вызывает одну и ту же дважды.

    Args:
        ticks:      List of (price, ts) tuples, oldest first.
        tf_defs:    Dict {tf: seconds}.
        tf_offsets: Dict {tf: offset_seconds}.

    Returns:
        Tuple (closed, current): {tf: [closed candles]} и {tf: current candle}.
    """
    current_bucket = {tf: None for tf in tf_defs}
    current_candle = {tf: None for tf in tf_defs}
    tf_data = {tf: [] for tf in tf_defs}

    for price, ts in ticks:
        for tf, sec in tf_defs.items():
            offset = tf_offsets.get(tf, 0)
            bucket = ((int(ts) - offset) // sec) * sec + offset

            if current_bucket[tf] is None:
                current_bucket[tf] = bucket
                current_candle[tf] = {"time": bucket, "open": price,
                                      "high": price, "low": price, "close": price}
                continue

            if bucket != current_bucket[tf]:
                prev_close = None
                if current_candle[tf]:
                    prev_close = current_candle[tf]["close"]
                    tf_data[tf].append(current_candle[tf])

                current_bucket[tf] = bucket

                if sec >= 60 and prev_close is not None:
                    current_candle[tf] = {
                        "time": bucket, "open": prev_close,
                        "high": max(prev_close, price),
                        "low": min(prev_close, price), "close": price,
                    }
                else:
                    current_candle[tf] = {"time": bucket, "open": price,
                                          "high": price, "low": price, "close": price}
                continue

            if current_candle[tf] is None:
                current_candle[tf] = {"time": bucket, "open": price,
                                      "high": price, "low": price, "close": price}
                continue

            c = current_candle[tf]
            c["high"] = max(c["high"], price)
            c["low"] = min(c["low"], price)
            c["close"] = price

    return tf_data, current_candle


# ── загрузка реальных данных ────────────────────────────────────────────

def load_ticks():
    """Load real ticks from the BIGGEST data/*.csv (a full trading day).

    Берём самый крупный файл, а не самый свежий: свежий — это сегодняшний,
    начатый минуту назад, и на нём не закрывается ни одной свечи выше M1,
    т.е. проверка старших ТФ (ради которых и делалась сетка) вырождается
    в пустой прогон.

    Args:
        None.

    Returns:
        List of (mid_price, ts) tuples, oldest first; [] if no CSV is available.
    """
    files = []
    for d in DATA_DIRS:
        files.extend(glob.glob(os.path.join(d, "*.csv")))
    if not files:
        return []
    biggest = max(files, key=os.path.getsize)

    ticks = []
    with open(biggest, "r") as f:
        for row in csv.DictReader(f):
            try:
                ticks.append((float(row["mid"]), float(row["timestamp_utc"])))
            except (KeyError, ValueError):
                continue
    return ticks


def load_broker_bars(tf, limit=50):
    """Load real broker bars from market.db.

    Args:
        tf:    Timeframe name.
        limit: How many of the most recent bars to fetch.

    Returns:
        List of candle dicts (time only matters here), oldest first; [] if
        the DB or the rows are missing.
    """
    if not os.path.exists(DB_PATH):
        return []
    conn = sqlite3.connect("file:%s?mode=ro" % DB_PATH, uri=True)
    try:
        rows = conn.execute(
            "SELECT time FROM candles WHERE tf=? ORDER BY time DESC LIMIT ?",
            (tf, limit),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return [{"time": r[0]} for r in reversed(rows)]


# ── тесты ───────────────────────────────────────────────────────────────

class TestOffsetBucket(unittest.TestCase):
    """Бакет считается по сетке провайдера, а не по UTC-полуночи."""

    def test_zero_offset_is_utc_midnight(self):
        b = CandleBuilder(TF_SECONDS)
        # 2026-07-14 13:37:05 UTC
        ts = 1784036225
        self.assertEqual(b._bucket_of("H1", ts), (ts // 3600) * 3600)
        self.assertEqual(b._bucket_of("D1", ts), (ts // 86400) * 86400)

    def test_h4_follows_broker_grid(self):
        # FXCM: H4 идёт по 01/05/09/13/17/21 UTC → offset = 3600
        b = CandleBuilder(TF_SECONDS, {"H4": 3600})
        # 13:37 UTC должен попасть в бакет 13:00, а не 12:00
        ts = 1784036225
        bucket = b._bucket_of("H4", ts)
        self.assertEqual(bucket % 14400, 3600)
        self.assertEqual((ts - bucket) < 14400, True)
        self.assertLessEqual(bucket, ts)

    def test_d1_follows_trading_day(self):
        # D1 закрывается в 21:00 UTC (закрытие Нью-Йорка), offset = 75600
        b = CandleBuilder(TF_SECONDS, {"D1": 75600})
        ts = 1784036225  # 13:37 UTC
        bucket = b._bucket_of("D1", ts)
        self.assertEqual(bucket % 86400, 75600)
        self.assertLessEqual(bucket, ts)
        self.assertLess(ts - bucket, 86400)

    def test_set_offsets_merges(self):
        b = CandleBuilder(TF_SECONDS, {"H4": 3600})
        b.set_offsets({"D1": 75600})
        self.assertEqual(b.offsets["H4"], 3600)
        self.assertEqual(b.offsets["D1"], 75600)


class TestDetectOffsets(unittest.TestCase):
    """Смещение учится из данных, а не хардкодится."""

    def test_detect_from_synthetic_bars(self):
        bars = {"H4": [{"time": 1784023200}], "D1": [{"time": 1783976400}]}
        got = CandleBuilder.detect_offsets(bars, TF_SECONDS)
        self.assertEqual(got["H4"], 1784023200 % 14400)
        self.assertEqual(got["D1"], 1783976400 % 86400)

    def test_empty_bars_skipped(self):
        got = CandleBuilder.detect_offsets({"H4": [], "D1": []}, TF_SECONDS)
        self.assertEqual(got, {})

    def test_detect_matches_real_market_db(self):
        """На боевой БД смещения обязаны совпасть с известной сеткой FXCM."""
        bars = {tf: load_broker_bars(tf) for tf in DIRECT_LOAD_TF}
        if not any(bars.values()):
            self.skipTest("market.db недоступна")

        offsets = CandleBuilder.detect_offsets(bars, TF_SECONDS)

        if bars.get("H1"):
            self.assertEqual(offsets["H1"], 0, "H1 выровнен по часу")
        if bars.get("H4"):
            self.assertIn(offsets["H4"], (3600, 7200),
                          "H4 у FXCM: 01/05/09... UTC (зимой 02/06/10...)")
        if bars.get("D1"):
            self.assertIn(offsets["D1"], (75600, 79200),
                          "D1 у FXCM: 21:00 UTC летом, 22:00 зимой")

    def test_every_real_bar_sits_on_detected_grid(self):
        """Все реальные бары брокера лежат на одной сетке — без второй сетки."""
        for tf in ("H4", "D1"):
            bars = load_broker_bars(tf, limit=200)
            if len(bars) < 2:
                continue
            offsets = CandleBuilder.detect_offsets({tf: bars}, TF_SECONDS)
            off = offsets[tf]
            bad = [b["time"] for b in bars if b["time"] % TF_SECONDS[tf] != off]
            self.assertEqual(bad, [], "%s: бары вне сетки offset=%s" % (tf, off))


class TestReplayRealTicks(unittest.TestCase):
    """Реплей реальных тиков: CandleBuilder == эталон из server.py."""

    @classmethod
    def setUpClass(cls):
        cls.ticks = load_ticks()

    def _replay(self, offsets):
        if not self.ticks:
            self.skipTest("нет data/*.csv для реплея")

        builder = CandleBuilder(TF_SECONDS, offsets)
        got_closed = {tf: [] for tf in TF_SECONDS}
        for price, ts in self.ticks:
            for tf, candle in builder.ingest(price, ts).items():
                got_closed[tf].append(candle)

        ref_closed, ref_current = reference_slice(self.ticks, TF_SECONDS, offsets)

        for tf in TF_SECONDS:
            self.assertEqual(got_closed[tf], ref_closed[tf],
                             "%s: закрытые свечи разошлись с эталоном" % tf)
            self.assertEqual(builder.current(tf), ref_current[tf],
                             "%s: текущая свеча разошлась с эталоном" % tf)
            self.assertEqual(builder.history(tf), ref_closed[tf],
                             "%s: history() разошлась с эталоном" % tf)
        return builder, got_closed

    def test_replay_is_not_vacuous(self):
        """Страховка: реплей обязан закрывать свечи на старших ТФ.

        Иначе прогон «зелёный», но сетку H4/D1 он не проверял вовсе — именно
        так и вышло на первом заходе (взяли свежий CSV на 33 тика).
        """
        _, closed = self._replay({"H4": 3600, "D1": 75600})
        for tf in ("M1", "M3", "M5", "M15", "H1", "H4"):
            self.assertGreater(len(closed[tf]), 0,
                               "%s: ни одной закрытой свечи — реплей слишком короткий" % tf)

    def test_replay_with_broker_grid(self):
        self._replay({"H4": 3600, "D1": 75600})

    def test_replay_without_offsets(self):
        self._replay({})

    def test_no_gaps_on_minute_and_above(self):
        """open каждой M1+ свечи = close предыдущей (то, что чинили в 50e63ac)."""
        builder, closed = self._replay({"H4": 3600, "D1": 75600})
        for tf, sec in TF_SECONDS.items():
            if sec < 60:
                continue  # секундные ТФ открываются от цены тика — так задумано
            bars = closed[tf]
            for prev, cur in zip(bars, bars[1:]):
                self.assertEqual(cur["open"], prev["close"],
                                 "%s: разрыв на баре %s" % (tf, cur["time"]))

    def test_ohlc_is_consistent(self):
        _, closed = self._replay({"H4": 3600, "D1": 75600})
        for tf, bars in closed.items():
            for c in bars:
                self.assertLessEqual(c["low"], c["open"], "%s low>open" % tf)
                self.assertLessEqual(c["low"], c["close"], "%s low>close" % tf)
                self.assertGreaterEqual(c["high"], c["open"], "%s high<open" % tf)
                self.assertGreaterEqual(c["high"], c["close"], "%s high<close" % tf)

    def test_closed_bars_sit_on_grid(self):
        _, closed = self._replay({"H4": 3600, "D1": 75600})
        offsets = {"H4": 3600, "D1": 75600}
        for tf, bars in closed.items():
            off = offsets.get(tf, 0)
            for c in bars:
                self.assertEqual(c["time"] % TF_SECONDS[tf], off,
                                 "%s: бар %s вне сетки" % (tf, c["time"]))

    def test_bar_times_are_int(self):
        """Время бара — целое: в проде ts = time.time() (float) и утекал в БД."""
        _, closed = self._replay({"H4": 3600})
        for tf, bars in closed.items():
            for c in bars:
                self.assertIsInstance(c["time"], int, "%s: время бара не int" % tf)


class TestSeedCurrent(unittest.TestCase):
    """Восстановление незакрытой свечи после рестарта."""

    def test_first_tick_extends_seeded_candle(self):
        b = CandleBuilder({"M1": 60})
        seeded = {"time": 1784036160, "open": 1.1700,
                  "high": 1.1700, "low": 1.1700, "close": 1.1700}
        b.seed_current("M1", seeded)

        b.ingest(1.1705, 1784036190)  # тик в том же бакете

        cur = b.current("M1")
        self.assertEqual(cur["time"], 1784036160)
        self.assertEqual(cur["open"], 1.1700, "open не должен переоткрыться от тика")
        self.assertEqual(cur["high"], 1.1705)
        self.assertEqual(cur["close"], 1.1705)

    def test_seed_is_copied_not_aliased(self):
        b = CandleBuilder({"M1": 60})
        src = {"time": 1784036160, "open": 1.17, "high": 1.17, "low": 1.17, "close": 1.17}
        b.seed_current("M1", src)
        b.ingest(1.18, 1784036190)
        self.assertEqual(src["close"], 1.17, "seed_current не должен править исходник")


class TestAggregateOffset(unittest.TestCase):
    """aggregate_higher_tf со смещением."""

    def _m1(self, start, count):
        return [{"time": start + i * 60, "open": 1.0, "high": 1.1,
                 "low": 0.9, "close": 1.0} for i in range(count)]

    def test_offset_zero_unchanged(self):
        src = self._m1(1784023200, 600)
        self.assertEqual(aggregate_higher_tf(src, 14400),
                         aggregate_higher_tf(src, 14400, 0))

    def test_offset_shifts_grid(self):
        src = self._m1(1784023200, 600)
        bars = aggregate_higher_tf(src, 14400, 3600)
        for c in bars:
            self.assertEqual(c["time"] % 14400, 3600)

    def test_offset_changes_bucketing(self):
        """Со смещением бары ложатся иначе — иначе тест ничего не проверяет."""
        src = self._m1(1784023200, 600)
        self.assertNotEqual([c["time"] for c in aggregate_higher_tf(src, 14400)],
                            [c["time"] for c in aggregate_higher_tf(src, 14400, 3600)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
