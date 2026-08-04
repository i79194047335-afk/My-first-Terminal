"""
Does the shape measured at signal time match the shape of the finished minute?

The live signal fires mid-candle, when only part of the minute's ticks exist.
Concentration computed at that instant can look artificially high simply because
little has happened yet. This replays the archive tick by tick, records the
shape at the moment the signal fires, then recomputes it over the complete
minute and compares.

Run: python3.10 hypothesis/verify_shape_live.py [days]
"""
import csv
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.news_windows import NewsWindows  # noqa: E402
from core.shock_detector import (  # noqa: E402
    BURST_MIN_COVERAGE, ShockDetector, burst_coverage)

DATA_DIR = "/root/projects/terminal/data"
FILE_TO_SYMBOL = {
    "EURUSD": "EUR/USD",
    "USDJPY": "USD/JPY",
    "AUDUSD": "AUD/USD",
    "USDCAD": "USD/CAD",
}
LEADER = "EUR/USD"


def load_day(date_str):
    """Load one day of ticks for all pairs, time-sorted.

    Args:
        date_str: Day in YYYYMMDD form.

    Returns:
        List of (ts, symbol, mid), leader first on equal timestamps.
    """
    ticks = []
    for file_sym, symbol in FILE_TO_SYMBOL.items():
        path = os.path.join(DATA_DIR, "{}_{}.csv".format(file_sym, date_str))
        if not os.path.exists(path):
            continue
        with open(path, "r") as fh:
            for row in csv.DictReader(fh):
                try:
                    ticks.append((float(row["timestamp_utc"]), symbol, float(row["mid"])))
                except (KeyError, TypeError, ValueError):
                    continue
    ticks.sort(key=lambda x: (x[0], 0 if x[1] == LEADER else 1))
    return ticks


def main():
    """Replay and compare live-time shape against final-minute shape."""
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 14
    end = datetime(2026, 8, 3, tzinfo=timezone.utc)
    dates = [(end - timedelta(days=i)).strftime("%Y%m%d") for i in range(days - 1, -1, -1)]

    det = ShockDetector()
    windows = NewsWindows()
    forming = {}
    all_ticks = defaultdict(list)   # (symbol, bucket) -> [(offset, price)]
    fired = {}                      # (symbol, bucket) -> event as fired
    finished = []

    for date_str in dates:
        for ts, symbol, mid in load_day(date_str):
            bucket = int(ts // 60) * 60
            cur = forming.get(symbol)

            if cur is not None and cur["time"] != bucket:
                det.push(symbol, cur)
                late = det.evaluate(symbol, cur)
                if late is not None:
                    fired[(symbol, cur["time"])] = late
                key = (symbol, cur["time"])
                if key in fired:
                    finished.append((key, dict(cur), list(all_ticks.get(key, []))))
                all_ticks.pop(key, None)
                cur = None

            if cur is None:
                cur = {"time": bucket, "o": mid, "h": mid, "l": mid, "c": mid}
                forming[symbol] = cur
            else:
                if mid > cur["h"]:
                    cur["h"] = mid
                if mid < cur["l"]:
                    cur["l"] = mid
                cur["c"] = mid

            all_ticks[(symbol, bucket)].append((ts - bucket, mid))
            det.push_tick(symbol, ts, mid)

            if symbol == LEADER:
                det.push_live(symbol, cur)
                continue

            ev = det.evaluate_live(symbol, cur)
            if ev is not None:
                fired[(symbol, bucket)] = ev

    print("Shape at signal time vs shape of the finished minute")
    print("threshold {:.0f}% coverage in a 10s sliding window\n".format(
        BURST_MIN_COVERAGE * 100))
    print("{:<17} {:<8} {:>6} {:>7} {:>9} {:>7} {:>9}  {}".format(
        "candle (UTC)", "pair", "sigma", "at fire", "shape@fire", "final", "shape@end",
        "agreement"))
    print("-" * 104)

    agree = disagree = 0
    audible_final_burst = 0
    for key, candle, ticks in finished:
        symbol, bucket = key
        ev = fired[key]
        final_cov, _ = burst_coverage(ticks, candle["h"] - candle["l"])
        final_shape = "burst" if final_cov >= BURST_MIN_COVERAGE else "spread"
        live_shape = ev.get("shape", "unknown")
        live_cov = ev.get("coverage")
        same = (live_shape == final_shape)
        if same:
            agree += 1
        else:
            disagree += 1
        blocked = windows.blocked(bucket, symbol)
        if not blocked and final_shape == "burst":
            audible_final_burst += 1

        dt = datetime.fromtimestamp(bucket, timezone.utc).strftime("%Y-%m-%d %H:%M")
        print("{:<17} {:<8} {:>6.1f} {:>6.0f}% {:>9} {:>6.0f}% {:>9}  {}{}".format(
            dt, symbol, ev["sigma"],
            (live_cov or 0) * 100, live_shape,
            final_cov * 100, final_shape,
            "same" if same else "CHANGED",
            "" if not blocked else "  (quiet window)"))

    total = agree + disagree
    print("\n" + "=" * 104)
    print("Shape agreed with the finished minute: {}/{}".format(agree, total))
    if disagree:
        print("Changed after the fact: {} — the live label is a snapshot, not a verdict".format(
            disagree))
    print("Audible shocks whose FINAL shape is burst: {} ({:.1f}/day)".format(
        audible_final_burst, audible_final_burst / float(days)))


if __name__ == "__main__":
    main()
