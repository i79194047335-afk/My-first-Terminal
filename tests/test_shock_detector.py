"""
Verify ShockDetector against the archive events found during research.

Run: python3.10 tests/test_shock_detector.py
(pytest is not used in this project — tests run directly.)
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hypothesis"))

from core.news_windows import NewsWindows  # noqa: E402
from core.shock_detector import ShockDetector, range_zscore  # noqa: E402
from uj_shock_reversal import load_series  # noqa: E402

FILE_TO_SYMBOL = {
    "EURUSD": "EUR/USD",
    "USDJPY": "USD/JPY",
    "AUDUSD": "AUD/USD",
    "USDCAD": "USD/CAD",
}


def as_dict(candle):
    """Convert a research Candle into the mapping the detector expects.

    Args:
        candle: Candle dataclass from the hypothesis scripts.

    Returns:
        Dict with time/o/h/l/c keys.
    """
    return {"time": candle.time, "o": candle.o, "h": candle.h,
            "l": candle.l, "c": candle.c}


def check(label, condition, detail=""):
    """Print a pass/fail line and return the condition.

    Args:
        label: Test name.
        condition: Truthy when the test passes.
        detail: Extra context printed after the label.

    Returns:
        Bool of the condition.
    """
    print("  {} {}{}".format("PASS" if condition else "FAIL", label,
                             (" — " + detail) if detail else ""))
    return bool(condition)


def test_zscore_basics():
    """Cover the guard rails of range_zscore."""
    print("\nrange_zscore guards")
    ok = True
    flat = [{"h": 1.0, "l": 1.0} for _ in range(10)]
    ok &= check("zero variance returns None", range_zscore({"h": 2.0, "l": 1.0}, flat) is None)
    ok &= check("short history returns None", range_zscore({"h": 2.0, "l": 1.0}, flat[:3]) is None)

    hist = [{"h": 1.0 + (i % 3) * 0.001, "l": 1.0} for i in range(30)]
    z = range_zscore({"h": 1.05, "l": 1.0}, hist)
    ok &= check("outlier scores high", z is not None and z > 10, "z={}".format(round(z, 1) if z else None))
    return ok


def test_gap_rejection():
    """A lookback broken by a market gap must not produce a signal."""
    print("\ngap handling")
    det = ShockDetector()
    base = 1_780_000_000
    # 30 contiguous quiet candles, then a shock candle 10 minutes later.
    for i in range(30):
        t = base + i * 60
        det.push("USD/JPY", {"time": t, "o": 150.0, "h": 150.01, "l": 150.0, "c": 150.005})
        det.push("EUR/USD", {"time": t, "o": 1.15, "h": 1.1501, "l": 1.15, "c": 1.15005})
    gap_time = base + 40 * 60
    shock = {"time": gap_time, "o": 150.0, "h": 150.5, "l": 150.0, "c": 150.4}
    det.push("USD/JPY", shock)
    det.push("EUR/USD", {"time": gap_time, "o": 1.15, "h": 1.1501, "l": 1.15, "c": 1.15005})
    return check("gap in lookback suppresses signal", det.evaluate("USD/JPY", shock) is None)


def test_leader_never_signals():
    """EUR/USD is the reference and must never emit a shock itself."""
    print("\nleader exclusion")
    det = ShockDetector()
    base = 1_780_100_000
    for i in range(30):
        t = base + i * 60
        det.push("EUR/USD", {"time": t, "o": 1.15, "h": 1.1501, "l": 1.15, "c": 1.15005})
    t = base + 30 * 60
    big = {"time": t, "o": 1.15, "h": 1.16, "l": 1.15, "c": 1.159}
    det.push("EUR/USD", big)
    return check("EUR/USD produces no shock", det.evaluate("EUR/USD", big) is None)


def test_archive_replay():
    """Replay two weeks of archive and compare against known findings."""
    print("\narchive replay 2026-07-21..2026-08-03")
    end = datetime(2026, 8, 3, tzinfo=timezone.utc)
    dates = [(end - timedelta(days=i)).strftime("%Y%m%d") for i in range(13, -1, -1)]

    series = {}
    for file_sym, symbol in FILE_TO_SYMBOL.items():
        series[symbol] = load_series(file_sym, dates)

    # Merge all pairs into one time-ordered stream, leader first within a minute
    # so the leader's candle is in history before followers are evaluated.
    stream = []
    for symbol, candles in series.items():
        for c in candles:
            stream.append((c.time, 0 if symbol == "EUR/USD" else 1, symbol, c))
    stream.sort(key=lambda x: (x[0], x[1]))

    det = ShockDetector()
    windows = NewsWindows()
    fired = []
    for _t, _order, symbol, candle in stream:
        cd = as_dict(candle)
        det.push(symbol, cd)
        hit = det.evaluate(symbol, cd)
        if hit:
            hit["blocked"] = windows.blocked(hit["time"], symbol)
            fired.append(hit)

    unblocked = [h for h in fired if not h["blocked"]]
    by_symbol = {}
    for h in unblocked:
        by_symbol.setdefault(h["symbol"], []).append(h)

    print("    raw shocks: {}, after windows: {}".format(len(fired), len(unblocked)))
    for sym in sorted(by_symbol):
        print("      {}: {}".format(sym, len(by_symbol[sym])))

    ok = True
    # The two-week USD/JPY study at 10 sigma found these; at the pinned 8 sigma
    # threshold they must still be present.
    expected_uj = {"2026-07-21 01:29", "2026-07-30 15:08", "2026-07-31 15:08", "2026-08-03 11:50"}
    got_uj = set()
    for h in by_symbol.get("USD/JPY", []):
        got_uj.add(datetime.fromtimestamp(h["time"], timezone.utc).strftime("%Y-%m-%d %H:%M"))
    missing = expected_uj - got_uj
    ok &= check("known USD/JPY shocks detected", not missing,
                "missing: {}".format(sorted(missing)) if missing else "all 4 present")

    # Every fired event must clear its own pair's threshold and a calm leader.
    bad = [h for h in fired if h["sigma"] < h["threshold"] or h["leader_sigma"] >= 2.0]
    ok &= check("all events respect thresholds and calm leader", not bad,
                "{} violations".format(len(bad)) if bad else "")

    # Calibration predicted a comparable per-pair rate; USD/CAD must not be mute.
    ok &= check("USD/CAD is not silent at its own threshold",
                len(by_symbol.get("USD/CAD", [])) > 0,
                "{} events".format(len(by_symbol.get("USD/CAD", []))))
    return ok


def main():
    """Run all checks and exit non-zero on failure."""
    results = [
        test_zscore_basics(),
        test_gap_rejection(),
        test_leader_never_signals(),
        test_archive_replay(),
    ]
    print("\n{}".format("ALL PASS" if all(results) else "FAILURES PRESENT"))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
