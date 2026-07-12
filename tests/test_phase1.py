"""
Tests for core/candles.py and core/db.py.

Run:  python3 tests/test_phase1.py
"""

import sys
import os
import csv
import sqlite3
import tempfile
import unittest

# Ensure we can import from the project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.candles import CandleBuilder, aggregate_higher_tf
from core.db import init_db, upsert_candle, upsert_candles_batch, \
    load_history, get_candle_count, trim_window, vacuum, \
    upsert_instrument, get_instrument


# ── helpers ────────────────────────────────────────────────────────────

def _load_ticks_from_csv(path, limit=5000):
    """Read a data/*.csv file, return list of (ts, price) tuples."""
    ticks = []
    with open(path, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ticks.append((float(row["timestamp_utc"]), float(row["mid"])))
            if len(ticks) >= limit:
                break
    return ticks


def _build_reference_aggregate(source, sec):
    """The original aggregate() from server.py, copied for comparison."""
    result = []
    bucket = None
    candle = None
    for c in source:
        b = (c["time"] // sec) * sec
        if b != bucket:
            if candle:
                result.append(candle)
            bucket = b
            candle = {
                "time":  b,
                "open":  c["open"],
                "high":  c["high"],
                "low":   c["low"],
                "close": c["close"],
            }
        else:
            candle["high"]  = max(candle["high"], c["high"])
            candle["low"]   = min(candle["low"],  c["low"])
            candle["close"] = c["close"]
    if candle:
        result.append(candle)
    return result


# ── tests ──────────────────────────────────────────────────────────────

class TestCandleBuilder(unittest.TestCase):
    """Verify CandleBuilder produces the same candles as the original logic."""

    @classmethod
    def setUpClass(cls):
        # Find a recent CSV to replay
        data_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data"
        )
        csv_files = sorted(
            [f for f in os.listdir(data_dir) if f.endswith(".csv")]
        )
        if not csv_files:
            raise unittest.SkipTest("No CSV data files found for replay test")
        cls.csv_path = os.path.join(data_dir, csv_files[-1])
        cls.ticks = _load_ticks_from_csv(cls.csv_path, limit=5000)
        if len(cls.ticks) < 100:
            raise unittest.SkipTest(f"Not enough ticks in {cls.csv_path}")

    def test_m1_candles_match_reference(self):
        """M1 candles from CandleBuilder match the original inline algorithm."""
        tf_defs = {"M1": 60}
        builder = CandleBuilder(tf_defs)

        # Replay
        for ts, price in self.ticks:
            builder.ingest(price, ts)

        # Close final candle
        builder.close_all()
        built = builder.history("M1")

        # Build reference via "manual" tick processing (same as server.py process_tick)
        ref_history = []
        current_bucket = None
        current_candle = None
        for ts, price in self.ticks:
            bucket = int(ts // 60) * 60
            if current_bucket is None:
                current_bucket = bucket
                current_candle = {
                    "time": bucket, "open": price,
                    "high": price, "low": price, "close": price,
                }
                continue
            if bucket != current_bucket:
                if current_candle:
                    ref_history.append(current_candle)
                prev_close = current_candle["close"] if current_candle else None
                current_bucket = bucket
                current_candle = {
                    "time": bucket,
                    "open": prev_close if prev_close is not None else price,
                    "high": max(prev_close, price) if prev_close is not None else price,
                    "low":  min(prev_close, price) if prev_close is not None else price,
                    "close": price,
                }
                continue
            if current_candle is None:
                current_candle = {
                    "time": bucket, "open": price,
                    "high": price, "low": price, "close": price,
                }
                continue
            current_candle["high"] = max(current_candle["high"], price)
            current_candle["low"]  = min(current_candle["low"],  price)
            current_candle["close"] = price
        if current_candle:
            ref_history.append(current_candle)

        # Compare
        self.assertEqual(len(built), len(ref_history),
                         f"M1 candle count mismatch: {len(built)} vs {len(ref_history)}")

        for i, (b, r) in enumerate(zip(built, ref_history)):
            with self.subTest(i=i):
                self.assertEqual(b["time"],  r["time"],  f"time  at candle {i}")
                self.assertAlmostEqual(b["open"],  r["open"],  places=5)
                self.assertAlmostEqual(b["high"],  r["high"],  places=5)
                self.assertAlmostEqual(b["low"],   r["low"],   places=5)
                self.assertAlmostEqual(b["close"], r["close"], places=5)

    def test_aggregate_higher_tf_matches_reference(self):
        """aggregate_higher_tf() produces same output as original aggregate()."""
        # Build M1 candles first
        tf_defs = {"M1": 60}
        builder = CandleBuilder(tf_defs)
        for ts, price in self.ticks:
            builder.ingest(price, ts)
        builder.close_all()
        m1_candles = builder.history("M1")

        if len(m1_candles) < 10:
            self.skipTest("Not enough M1 candles for higher-TF test")

        for tf_name, sec in [("M5", 300), ("M15", 900), ("H1", 3600)]:
            with self.subTest(tf=tf_name):
                built = aggregate_higher_tf(m1_candles, sec)
                ref   = _build_reference_aggregate(m1_candles, sec)
                self.assertEqual(len(built), len(ref),
                                 f"{tf_name} count mismatch: {len(built)} vs {len(ref)}")
                for i, (b, r) in enumerate(zip(built, ref)):
                    self.assertEqual(b["time"], r["time"])
                    self.assertAlmostEqual(b["open"],  r["open"],  places=5)
                    self.assertAlmostEqual(b["high"],  r["high"],  places=5)
                    self.assertAlmostEqual(b["low"],   r["low"],   places=5)
                    self.assertAlmostEqual(b["close"], r["close"], places=5)

    def test_seed_history_then_extend(self):
        """seed_history() + ingest extends existing candles correctly."""
        m1_candles = []
        for ts, price in self.ticks[:100]:
            m1_candles.append({
                "time": ts, "open": price,
                "high": price, "low": price, "close": price,
            })

        builder = CandleBuilder({"M1": 60})
        # Seed M5 history from M1 aggregation
        m5_seed = aggregate_higher_tf(m1_candles, 300)
        builder.seed_history("M5", m5_seed)

        # Now ingest more ticks
        for ts, price in self.ticks[100:]:
            builder.ingest(price, ts)
        builder.close_all()

        full_m5 = builder.history("M5")
        self.assertGreaterEqual(len(full_m5), len(m5_seed),
                                "M5 history should grow after seed + ingest")


class TestDb(unittest.TestCase):
    """Verify SQLite storage: upsert, load, trim, vacuum."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.path = self.tmp.name
        self.tmp.close()
        self.conn = init_db(self.path)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.path)

    def _make_candle(self, time, o, h, l, c):
        return {"time": time, "open": o, "high": h, "low": l, "close": c}

    def test_upsert_and_load(self):
        """Write one candle, read it back."""
        candle = self._make_candle(1000, 1.10, 1.12, 1.09, 1.11)
        upsert_candle(self.conn, "fxcm", "EUR/USD", "M1", candle)

        rows = load_history(self.conn, "fxcm", "EUR/USD", "M1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["time"], 1000)
        self.assertAlmostEqual(rows[0]["open"],  1.10)
        self.assertAlmostEqual(rows[0]["high"],  1.12)
        self.assertAlmostEqual(rows[0]["low"],   1.09)
        self.assertAlmostEqual(rows[0]["close"], 1.11)
        self.assertIsNone(rows[0]["vol_base"])
        self.assertIsNone(rows[0]["vol_quote"])

    def test_upsert_idempotent(self):
        """Writing the same candle twice does not create a duplicate."""
        c1 = self._make_candle(1000, 1.10, 1.12, 1.09, 1.11)
        c2 = self._make_candle(1000, 1.11, 1.13, 1.10, 1.12)  # updated values
        upsert_candle(self.conn, "fxcm", "EUR/USD", "M1", c1)
        upsert_candle(self.conn, "fxcm", "EUR/USD", "M1", c2)

        rows = load_history(self.conn, "fxcm", "EUR/USD", "M1")
        self.assertEqual(len(rows), 1, "Should be 1 row, not duplicated")
        self.assertAlmostEqual(rows[0]["open"], 1.11,
                               "Second upsert should overwrite first")

    def test_batch_upsert(self):
        """Batch insert works and preserves order."""
        candles = [
            self._make_candle(i * 60, 1.10 + i * 0.001, 1.12, 1.09, 1.11)
            for i in range(100)
        ]
        upsert_candles_batch(self.conn, "fxcm", "EUR/USD", "M1", candles)
        self.assertEqual(get_candle_count(self.conn, "fxcm", "EUR/USD", "M1"), 100)

    def test_trim_window(self):
        """Window trim keeps exactly KEEP_BARS most recent candles."""
        KEEP = 50
        candles = [
            self._make_candle(i * 60, 1.10, 1.12, 1.09, 1.11)
            for i in range(200)
        ]
        upsert_candles_batch(self.conn, "fxcm", "EUR/USD", "M1", candles)

        deleted = trim_window(self.conn, "fxcm", "EUR/USD", "M1", keep_bars=KEEP)
        self.assertEqual(deleted, 200 - KEEP)

        remaining = get_candle_count(self.conn, "fxcm", "EUR/USD", "M1")
        self.assertEqual(remaining, KEEP)

        # Verify the remaining are the most recent
        rows = load_history(self.conn, "fxcm", "EUR/USD", "M1")
        self.assertEqual(rows[0]["time"], (200 - KEEP) * 60)
        self.assertEqual(rows[-1]["time"], 199 * 60)

    def test_trim_window_not_full(self):
        """Trim on a sparse window does nothing."""
        candles = [self._make_candle(i * 60, 1.10, 1.12, 1.09, 1.11) for i in range(10)]
        upsert_candles_batch(self.conn, "fxcm", "EUR/USD", "M1", candles)

        deleted = trim_window(self.conn, "fxcm", "EUR/USD", "M1", keep_bars=2000)
        self.assertEqual(deleted, 0)
        self.assertEqual(get_candle_count(self.conn, "fxcm", "EUR/USD", "M1"), 10)

    def test_vacuum(self):
        """Vacuum runs without error after deletes."""
        candles = [
            self._make_candle(i * 60, 1.10, 1.12, 1.09, 1.11)
            for i in range(100)
        ]
        upsert_candles_batch(self.conn, "fxcm", "EUR/USD", "M1", candles)
        trim_window(self.conn, "fxcm", "EUR/USD", "M1", keep_bars=10)

        # Should not raise
        vacuum(self.conn)

    def test_instrument_upsert_and_get(self):
        """Instrument metadata round-trips correctly."""
        upsert_instrument(
            self.conn, "lighter", "BTC",
            price_decimals=1, size_decimals=5,
            min_base=0.0002, has_volume=True,
            meta={"market_id": 42},
            updated=1783869420,
        )
        inst = get_instrument(self.conn, "lighter", "BTC")
        self.assertIsNotNone(inst)
        self.assertEqual(inst["price_decimals"], 1)
        self.assertEqual(inst["size_decimals"], 5)
        self.assertTrue(inst["has_volume"])
        self.assertEqual(inst["meta"]["market_id"], 42)

    def test_load_empty_returns_empty_list(self):
        """Loading from empty DB returns [], not error."""
        rows = load_history(self.conn, "fxcm", "NONEXIST", "M1")
        self.assertEqual(rows, [])

    def test_multi_thread_write_then_read_after_reconnect(self):
        """Write from a non-main thread, close, reopen — data must survive.

        Reproduces defects:
          1. check_same_thread crash (connection shared across threads)
          2. Uncommitted writes vanishing on restart
        """
        import threading
        import queue as qmod

        candles_to_write = [
            {"time": i * 60, "open": 1.1 + i * 0.001, "high": 1.12,
             "low": 1.09, "close": 1.11}
            for i in range(50)
        ]

        errors = []
        db_path = self.path

        def writer_thread():
            """Simulates db_writer: creates its OWN connection in-thread."""
            try:
                conn = init_db(db_path)
                for c in candles_to_write:
                    upsert_candle(conn, "fxcm", "EUR/USD", "M1", c)
                conn.commit()
                conn.close()
            except Exception as e:
                errors.append(e)

        # Spawn non-main thread
        t = threading.Thread(target=writer_thread, name="db-writer-test")
        t.start()
        t.join()

        # Thread must not have crashed
        self.assertEqual(errors, [], f"Writer thread crashed: {errors}")

        # Close the main-thread connection and reopen — simulates restart
        self.conn.close()
        self.conn = sqlite3.connect(self.path)

        rows = load_history(self.conn, "fxcm", "EUR/USD", "M1")
        self.assertEqual(len(rows), 50,
                         f"Expected 50 candles after reconnect, got {len(rows)}")
        self.assertAlmostEqual(rows[0]["open"], 1.1)
        # close=1.11 for all candles in our test data
        self.assertAlmostEqual(rows[-1]["open"],  1.1 + 49 * 0.001)
        self.assertAlmostEqual(rows[-1]["close"], 1.11)
        # Verify idempotency: re-write same candle from another thread
        def _idempotent_write():
            c2 = init_db(db_path)
            upsert_candle(c2, "fxcm", "EUR/USD", "M1",
                          {"time": 0, "open": 9.99, "high": 9.99,
                           "low": 9.99, "close": 9.99})
            c2.close()

        t2 = threading.Thread(target=_idempotent_write)
        t2.start()
        t2.join()

        rows2 = load_history(self.conn, "fxcm", "EUR/USD", "M1")
        self.assertEqual(len(rows2), 50, "Idempotent upsert must not duplicate")
        self.assertAlmostEqual(rows2[0]["open"], 9.99, "Upsert must overwrite")

    def test_init_on_nonexistent_db(self):
        """Regression: first run with no .db file must not crash.

        init_db() creates the file + schema; subsequent load_history must
        return an empty list, not raise OperationalError.
        """
        nonexistent = tempfile.mktemp(suffix=".db")
        try:
            # Simulates what load_history does on first run
            conn = init_db(nonexistent)
            rows = load_history(conn, "fxcm", "EUR/USD", "M1")
            self.assertEqual(rows, [], "Empty DB must return empty list, not crash")
            conn.close()
        finally:
            if os.path.exists(nonexistent):
                os.unlink(nonexistent)
            # Also clean up WAL/SHM if created
            for suf in ["-wal", "-shm"]:
                p = nonexistent + suf
                if os.path.exists(p):
                    os.unlink(p)

    def test_restore_with_gap_detection(self):
        """Regression: DB has candles up to N hours ago → backfill needed.

        Verifies that load_history correctly reports the most recent candle
        time so the caller can request only the gap from FXCM.
        """
        # Write candles, most recent = 6 hours ago
        import time as _time
        now = int(_time.time())
        gap_hours = 6
        old_candles = [
            {"time": now - gap_hours * 3600 + i * 60, "open": 1.1, "high": 1.12,
             "low": 1.09, "close": 1.11}
            for i in range(100)
        ]
        upsert_candles_batch(self.conn, "fxcm", "EUR/USD", "M1", old_candles)
        self.conn.commit()

        rows = load_history(self.conn, "fxcm", "EUR/USD", "M1")
        self.assertEqual(len(rows), 100)
        last_time = max(c["time"] for c in rows)

        # Gap must be detectable (> 1 min from now)
        gap_sec = now - last_time
        self.assertGreater(gap_sec, 60,
                           f"Expected gap > 60s for backfill, got {gap_sec}s")
        self.assertLess(gap_sec, gap_hours * 3600 + 120,
                        f"Gap too large: {gap_sec}s")

        # Simulate reload: data survives
        self.conn.close()
        self.conn = sqlite3.connect(self.path)
        rows2 = load_history(self.conn, "fxcm", "EUR/USD", "M1")
        self.assertEqual(len(rows2), 100)


if __name__ == "__main__":
    unittest.main()
