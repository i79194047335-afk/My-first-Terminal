"""
Measure how concentrated each M1 shock actually is, tick by tick.

The owner's rule: an M1 candle only counts when at least 80% of its range was
covered by a single burst lasting no more than 10 seconds. A wide candle built
from evenly spread movement is not a signal.

"Covered by a burst" is measured as the range (high-low) of the ticks inside the
window, divided by the range of the whole minute. Two window definitions are
compared, because they disagree on bursts that straddle a boundary:

  fixed   - the minute is cut into six 10s slots (0-10, 10-20, ...);
  sliding - any 10s span, wherever it starts.

Run: python3.10 hypothesis/burst_concentration.py [days]
"""
import csv
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.news_windows import NewsWindows  # noqa: E402
from core.shock_detector import ShockDetector  # noqa: E402

DATA_DIR = "/root/projects/terminal/data"
FILE_TO_SYMBOL = {
    "EURUSD": "EUR/USD",
    "USDJPY": "USD/JPY",
    "AUDUSD": "AUD/USD",
    "USDCAD": "USD/CAD",
}
LEADER = "EUR/USD"
BURST_SECONDS = 10


def load_day_ticks(date_str):
    """Load one day of ticks for all pairs, sorted by time.

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


def fixed_slot_coverage(ticks, minute_range):
    """Largest share of the minute's range covered by one fixed 10s slot.

    Args:
        ticks: List of (offset_seconds, price) within the minute.
        minute_range: High-low of the whole minute.

    Returns:
        Coverage in 0..1, plus the winning slot index.
    """
    if minute_range <= 0:
        return 0.0, -1
    slots = defaultdict(list)
    for off, price in ticks:
        slots[int(off // BURST_SECONDS)].append(price)
    best, best_slot = 0.0, -1
    for slot, prices in slots.items():
        if len(prices) < 2:
            continue
        cover = (max(prices) - min(prices)) / minute_range
        if cover > best:
            best, best_slot = cover, slot
    return best, best_slot


def sliding_coverage(ticks, minute_range):
    """Largest share of the minute's range covered by any 10s span.

    Args:
        ticks: List of (offset_seconds, price) within the minute, time-sorted.
        minute_range: High-low of the whole minute.

    Returns:
        Coverage in 0..1, plus the start offset of the best window.
    """
    if minute_range <= 0 or len(ticks) < 2:
        return 0.0, -1
    best, best_start = 0.0, -1
    # Each tick starts a candidate window; the span is walked forward while it
    # stays within BURST_SECONDS.
    for i in range(len(ticks)):
        start = ticks[i][0]
        hi = lo = ticks[i][1]
        for j in range(i + 1, len(ticks)):
            if ticks[j][0] - start > BURST_SECONDS:
                break
            price = ticks[j][1]
            if price > hi:
                hi = price
            if price < lo:
                lo = price
        cover = (hi - lo) / minute_range
        if cover > best:
            best, best_start = cover, start
    return best, best_start


def main():
    """Replay the archive and report concentration for every shock."""
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 14
    end = datetime(2026, 8, 3, tzinfo=timezone.utc)
    dates = [(end - timedelta(days=i)).strftime("%Y%m%d") for i in range(days - 1, -1, -1)]

    det = ShockDetector()
    windows = NewsWindows()
    # (symbol, minute) -> list of (offset, price), kept only for the current and
    # previous minute so memory stays flat across two weeks.
    minute_ticks = defaultdict(list)
    forming = {}
    results = []

    for date_str in dates:
        for ts, symbol, mid in load_day_ticks(date_str):
            bucket = int(ts // 60) * 60
            cur = forming.get(symbol)

            if cur is not None and cur["time"] != bucket:
                det.push(symbol, cur)
                ev = det.evaluate(symbol, cur)
                if ev is not None:
                    key = (symbol, cur["time"])
                    tk = minute_ticks.get(key, [])
                    rng = cur["h"] - cur["l"]
                    fixed, slot = fixed_slot_coverage(tk, rng)
                    slide, start = sliding_coverage(tk, rng)
                    ev["blocked"] = windows.blocked(ev["time"], symbol)
                    ev["fixed"] = fixed
                    ev["slide"] = slide
                    ev["slot"] = slot
                    ev["start"] = start
                    ev["ticks"] = len(tk)
                    results.append(ev)
                minute_ticks.pop((symbol, cur["time"]), None)
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

            minute_ticks[(symbol, bucket)].append((ts - bucket, mid))
            if symbol == LEADER:
                det.push_live(symbol, cur)

    print("Shocks over {} .. {}: {}".format(dates[0], dates[-1], len(results)))
    print("Coverage = share of the minute's range done inside one {}s burst\n".format(
        BURST_SECONDS))
    print("{:<17} {:<8} {:>6} {:>7} {:>8} {:>8} {:>6}  {}".format(
        "candle (UTC)", "pair", "sigma", "ticks", "fixed", "sliding", "start", "verdict@80%"))
    print("-" * 100)

    pass_fixed = pass_slide = 0
    for ev in results:
        dt = datetime.fromtimestamp(ev["time"], timezone.utc).strftime("%Y-%m-%d %H:%M")
        ok_f = ev["fixed"] >= 0.80
        ok_s = ev["slide"] >= 0.80
        pass_fixed += ok_f
        pass_slide += ok_s
        verdict = "both" if (ok_f and ok_s) else ("sliding only" if ok_s else
                                                  ("fixed only" if ok_f else "rejected"))
        print("{:<17} {:<8} {:>6.1f} {:>7} {:>7.0f}% {:>7.0f}% {:>5.0f}s  {}{}".format(
            dt, ev["symbol"], ev["sigma"], ev["ticks"],
            ev["fixed"] * 100, ev["slide"] * 100, ev["start"], verdict,
            "" if not ev["blocked"] else "  (in quiet window)"))

    total = len(results)
    print("\n" + "=" * 100)
    print("Pass at 80%: fixed slots {}/{}, sliding window {}/{}".format(
        pass_fixed, total, pass_slide, total))
    audible = [e for e in results if not e["blocked"]]
    a_slide = sum(1 for e in audible if e["slide"] >= 0.80)
    print("Audible shocks (outside quiet windows): {} -> {} would survive sliding@80%".format(
        len(audible), a_slide))
    print("Rate after the filter: {:.1f} per day".format(a_slide / float(days)))

    print("\nSensitivity of the sliding window:")
    for thr in (0.60, 0.70, 0.80, 0.90):
        n = sum(1 for e in audible if e["slide"] >= thr)
        print("  >= {:.0f}%: {} audible ({:.1f}/day)".format(thr * 100, n, n / float(days)))


if __name__ == "__main__":
    main()
