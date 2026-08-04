"""
Preview what the shock indicator would have emitted over the archive.

Replays two weeks of ticks through the exact classes the hub uses
(core.shock_detector + core.news_windows) and prints the payloads that would
have reached the browser, so the signal rate can be judged before the hub is
restarted.

Run: python3.10 hypothesis/preview_signals.py [days]
"""
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.news_windows import NewsWindows  # noqa: E402
from core.shock_detector import SHOCK_SIGMA, ShockDetector  # noqa: E402
from uj_shock_reversal import load_series  # noqa: E402

FILE_TO_SYMBOL = {
    "EURUSD": "EUR/USD",
    "USDJPY": "USD/JPY",
    "AUDUSD": "AUD/USD",
    "USDCAD": "USD/CAD",
}


def main():
    """Replay the archive and print the resulting signals."""
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 14
    end = datetime(2026, 8, 3, tzinfo=timezone.utc)
    dates = [(end - timedelta(days=i)).strftime("%Y%m%d") for i in range(days - 1, -1, -1)]

    print("Replay {} .. {}   thresholds: {}".format(
        dates[0], dates[-1],
        ", ".join("{} {}s".format(k, v) for k, v in sorted(SHOCK_SIGMA.items()))))

    stream = []
    for file_sym, symbol in FILE_TO_SYMBOL.items():
        for c in load_series(file_sym, dates):
            # Leader first within a minute so followers see it in history.
            stream.append((c.time, 0 if symbol == "EUR/USD" else 1, symbol, c))
    stream.sort(key=lambda x: (x[0], x[1]))

    det = ShockDetector()
    windows = NewsWindows()
    fired, suppressed = [], []

    for _t, _order, symbol, candle in stream:
        cd = {"time": candle.time, "o": candle.o, "h": candle.h,
              "l": candle.l, "c": candle.c}
        det.push(symbol, cd)
        ev = det.evaluate(symbol, cd)
        if not ev:
            continue
        ev["blocked"] = windows.blocked(ev["time"], symbol)
        (suppressed if ev["blocked"] else fired).append(ev)

    print("\nSIGNALS (sound + popup + coloured marker): {}".format(len(fired)))
    print("-" * 92)
    for ev in fired:
        dt = datetime.fromtimestamp(ev["time"], timezone.utc).strftime("%Y-%m-%d %H:%M")
        arrow = "up" if ev["direction"] > 0 else ("down" if ev["direction"] < 0 else "flat")
        print("  {}  {:<8} {:>5.1f}s (thr {:>4.1f})  leader {:>4.1f}s  {:<4}".format(
            dt, ev["symbol"], ev["sigma"], ev["threshold"], ev["leader_sigma"], arrow))

    print("\nSUPPRESSED (grey marker only, no sound): {}".format(len(suppressed)))
    print("-" * 92)
    for ev in suppressed:
        dt = datetime.fromtimestamp(ev["time"], timezone.utc).strftime("%Y-%m-%d %H:%M")
        b = ev["blocked"]
        print("  {}  {:<8} {:>5.1f}s  {}: {}".format(
            dt, ev["symbol"], ev["sigma"], b["kind"], b["label"]))

    print("\n" + "=" * 92)
    per_symbol = Counter(e["symbol"] for e in fired)
    print("Audible signals per pair: {}".format(
        ", ".join("{} {}".format(s, n) for s, n in sorted(per_symbol.items())) or "none"))
    print("Rate: {:.1f} audible/day over {} days ({} suppressed)".format(
        len(fired) / float(days), days, len(suppressed)))
    kinds = Counter(e["blocked"]["kind"] for e in suppressed)
    if kinds:
        print("Suppression reasons: {}".format(
            ", ".join("{} {}".format(k, n) for k, n in kinds.items())))


if __name__ == "__main__":
    main()
