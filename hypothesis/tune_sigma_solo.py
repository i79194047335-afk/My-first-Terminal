"""
Pick a lower sigma threshold now that the leader gate no longer blocks.

Two changes are being evaluated together:

  * the leader (EUR/USD) stops suppressing a shock and instead labels it —
    SOLO when the leader was calm, IN STREAM when it moved too;
  * per-pair sigma thresholds come down, so moves like USD/JPY 2026-08-04 11:56
    (6.3 sigma, a 90% burst) are no longer missed.

Prints, for a grid of thresholds, how many events each pair would produce and
how they split between SOLO and IN STREAM — the number that decides whether a
threshold is usable is events per day, not the sigma itself.

Run: python3.10 hypothesis/tune_sigma_solo.py [days]
"""
import csv
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.news_windows import NewsWindows  # noqa: E402
from core.shock_detector import (  # noqa: E402
    BURST_MIN_COVERAGE, LEADER_CALM_SIGMA, LOOKBACK, burst_coverage, range_zscore)

DATA_DIR = "/root/projects/terminal/data"
FILE_TO_SYMBOL = {
    "EURUSD": "EUR/USD",
    "USDJPY": "USD/JPY",
    "AUDUSD": "AUD/USD",
    "USDCAD": "USD/CAD",
}
LEADER = "EUR/USD"
FOLLOWERS = ["USD/JPY", "AUD/USD", "USD/CAD"]
GRID = [4.0, 4.5, 5.0, 5.5, 6.0, 6.5, 7.0, 8.0]


def build_minutes(date_str):
    """Build M1 candles and per-minute tick lists for every pair.

    Args:
        date_str: Day in YYYYMMDD form.

    Returns:
        Tuple (candles, ticks): candles[symbol][bucket] -> OHLC dict,
        ticks[(symbol, bucket)] -> list of (offset, price).
    """
    candles = defaultdict(dict)
    ticks = defaultdict(list)
    for file_sym, symbol in FILE_TO_SYMBOL.items():
        path = os.path.join(DATA_DIR, "{}_{}.csv".format(file_sym, date_str))
        if not os.path.exists(path):
            continue
        with open(path, "r") as fh:
            for row in csv.DictReader(fh):
                try:
                    ts = float(row["timestamp_utc"])
                    mid = float(row["mid"])
                except (KeyError, TypeError, ValueError):
                    continue
                bucket = int(ts // 60) * 60
                c = candles[symbol].get(bucket)
                if c is None:
                    candles[symbol][bucket] = {"time": bucket, "o": mid, "h": mid,
                                               "l": mid, "c": mid}
                else:
                    if mid > c["h"]:
                        c["h"] = mid
                    if mid < c["l"]:
                        c["l"] = mid
                    c["c"] = mid
                ticks[(symbol, bucket)].append((ts - bucket, mid))
    return candles, ticks


def contiguous(series, bucket):
    """Return an unbroken LOOKBACK window ending just before `bucket`.

    Args:
        series: {bucket: candle} for one symbol.
        bucket: Minute under test.

    Returns:
        List of candles, or None when the window has a gap.
    """
    window = []
    for i in range(LOOKBACK, 0, -1):
        c = series.get(bucket - i * 60)
        if c is None:
            return None
        window.append(c)
    return window


def main():
    """Scan the archive and report event counts across the threshold grid."""
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 14
    end = datetime(2026, 8, 3, tzinfo=timezone.utc)
    dates = [(end - timedelta(days=i)).strftime("%Y%m%d") for i in range(days - 1, -1, -1)]

    windows = NewsWindows()
    # rows: (symbol, bucket, sigma, leader_sigma, coverage)
    rows = []

    for date_str in dates:
        candles, ticks = build_minutes(date_str)
        leader = candles.get(LEADER, {})
        for symbol in FOLLOWERS:
            series = candles.get(symbol, {})
            for bucket, candle in series.items():
                win = contiguous(series, bucket)
                if win is None:
                    continue
                sigma = range_zscore(candle, win)
                if sigma is None or sigma < min(GRID):
                    continue

                lead_c = leader.get(bucket)
                lead_win = contiguous(leader, bucket) if lead_c else None
                lead_sigma = (range_zscore(lead_c, lead_win)
                              if lead_c and lead_win else None)

                rng = candle["h"] - candle["l"]
                cov, _ = burst_coverage(ticks.get((symbol, bucket), []), rng)

                rows.append({
                    "symbol": symbol,
                    "time": bucket,
                    "sigma": sigma,
                    "leader": lead_sigma,
                    "coverage": cov,
                    "blocked": windows.blocked(bucket, symbol),
                })

    print("Sample {} .. {} ({} days)".format(dates[0], dates[-1], days))
    print("Leader gate is now a LABEL, not a block: SOLO = leader below {:.1f} sigma."
          .format(LEADER_CALM_SIGMA))
    print("Burst = at least {:.0f}% of the range inside one 10s window.\n".format(
        BURST_MIN_COVERAGE * 100))

    for symbol in FOLLOWERS:
        print("=" * 96)
        print(symbol)
        print("{:>6}  {:>7} {:>7}  {:>6} {:>9}  {:>7} {:>9}".format(
            "sigma", "events", "/day", "SOLO", "IN STREAM", "bursts", "burst/day"))
        for thr in GRID:
            sel = [r for r in rows
                   if r["symbol"] == symbol and r["sigma"] >= thr and not r["blocked"]]
            solo = [r for r in sel if r["leader"] is not None and r["leader"] < LEADER_CALM_SIGMA]
            stream = [r for r in sel if not (r["leader"] is not None and r["leader"] < LEADER_CALM_SIGMA)]
            bursts = [r for r in sel if r["coverage"] >= BURST_MIN_COVERAGE]
            print("{:>6.1f}  {:>7} {:>7.1f}  {:>6} {:>9}  {:>7} {:>9.1f}".format(
                thr, len(sel), len(sel) / float(days), len(solo), len(stream),
                len(bursts), len(bursts) / float(days)))
        print()

    # The candle that started this: USD/JPY 2026-08-04 11:56.
    print("=" * 96)
    print("Reference candle USD/JPY 2026-08-04 11:56 measured earlier:")
    print("  sigma 6.3, leader 2.18 -> IN STREAM, coverage 91% -> burst")
    print("  It needs a USD/JPY threshold of 6.0 or lower to be reported at all.")

    print("\nCombined load at a candidate setting (JPY 6.0 / AUD 4.0 / CAD 4.0):")
    cand = {"USD/JPY": 6.0, "AUD/USD": 4.0, "USD/CAD": 4.0}
    sel = [r for r in rows if r["sigma"] >= cand[r["symbol"]] and not r["blocked"]]
    solo = [r for r in sel if r["leader"] is not None and r["leader"] < LEADER_CALM_SIGMA]
    bursts = [r for r in sel if r["coverage"] >= BURST_MIN_COVERAGE]
    solo_bursts = [r for r in solo if r["coverage"] >= BURST_MIN_COVERAGE]
    print("  all events      : {} ({:.1f}/day)".format(len(sel), len(sel) / float(days)))
    print("  SOLO            : {} ({:.1f}/day)".format(len(solo), len(solo) / float(days)))
    print("  bursts          : {} ({:.1f}/day)".format(len(bursts), len(bursts) / float(days)))
    print("  SOLO + burst    : {} ({:.1f}/day)".format(
        len(solo_bursts), len(solo_bursts) / float(days)))


if __name__ == "__main__":
    main()
