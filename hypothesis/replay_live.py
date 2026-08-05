"""
Tick-by-tick replay of the live signal path.

Feeds archived ticks through ShockDetector exactly as the hub does — forming
candle scored on every tick, finished candle pushed to history and topped up —
and reports how much earlier the live path fires than waiting for the close.

Run: python3.10 hypothesis/replay_live.py [YYYYMMDD ...]
"""
import csv
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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


def load_ticks(date_str):
    """Load one day of ticks for all four pairs, merged in time order.

    Args:
        date_str: Day in YYYYMMDD form.

    Returns:
        List of (ts, symbol, mid) sorted by timestamp, leader first on ties so
        the leader's forming candle is current when followers are scored.
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


def replay(date_str):
    """Replay one day and collect live-path signals.

    Args:
        date_str: Day in YYYYMMDD form.

    Returns:
        List of event dicts with an added "seconds_into_candle" field.
    """
    ticks = load_ticks(date_str)
    if not ticks:
        return []

    det = ShockDetector()
    windows = NewsWindows()
    forming = {}   # symbol -> candle dict being built
    events = []

    for ts, symbol, mid in ticks:
        bucket = int(ts // 60) * 60
        cur = forming.get(symbol)

        # Minute rolled over: finish the previous candle exactly as the hub does.
        if cur is not None and cur["time"] != bucket:
            det.push(symbol, cur)
            late = det.evaluate(symbol, cur)
            if late is not None:
                late["blocked"] = windows.blocked(late["time"], symbol)
                late["seconds_into_candle"] = 60
                events.append(late)
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

        if symbol == LEADER:
            det.push_live(symbol, cur)
            continue

        ev = det.evaluate_live(symbol, cur)
        if ev is not None:
            ev["blocked"] = windows.blocked(ev["time"], symbol)
            ev["seconds_into_candle"] = int(ts - bucket)
            events.append(ev)

    return events


def main():
    """Replay the given days (default: the last archived week)."""
    if len(sys.argv) > 1:
        dates = sys.argv[1:]
    else:
        dates = ["20260728", "20260729", "20260730", "20260731", "20260803"]

    print("Tick-by-tick replay of the live path: {}".format(", ".join(dates)))
    print("=" * 96)

    all_events = []
    for d in dates:
        evs = replay(d)
        all_events.extend(evs)
        print("\n{}: {} events".format(d, len(evs)))
        for ev in evs:
            when = datetime.fromtimestamp(ev["time"], timezone.utc).strftime("%H:%M")
            tag = "suppressed" if ev["blocked"] else "SIGNAL"
            print("  {} {:<8} {:>6.1f}s  fired {:>2}s into the minute  {}{}".format(
                when, ev["symbol"], ev["sigma"], ev["seconds_into_candle"], tag,
                "" if not ev["blocked"] else " (" + ev["blocked"]["kind"] + ")"))

    if not all_events:
        print("\nNo events.")
        return

    live = [e for e in all_events if e.get("live")]
    late = [e for e in all_events if not e.get("live")]
    audible = [e for e in all_events if not e["blocked"]]

    print("\n" + "=" * 96)
    print("Total {} events — {} fired mid-candle, {} only at close".format(
        len(all_events), len(live), len(late)))
    print("Audible (outside quiet windows): {}".format(len(audible)))
    if live:
        secs = [e["seconds_into_candle"] for e in live]
        saved = [60 - s for s in secs]
        print("Mid-candle firing: median {}s into the minute, so ~{}s earlier "
              "than waiting for the close".format(
                  sorted(secs)[len(secs) // 2], sorted(saved)[len(saved) // 2]))
        print("  fastest {}s, slowest {}s".format(min(secs), max(secs)))


if __name__ == "__main__":
    main()
