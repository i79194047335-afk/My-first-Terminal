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
    base = 1_780_000_000 // 60 * 60   # выровнено по минуте
    # 30 contiguous quiet candles, then a shock candle 10 minutes later.
    for i in range(30):
        t = base + i * 60
        det.push("USD/JPY", {"time": t, "o": 150.0, "h": 150.010 + (i % 3) * 0.001,
                             "l": 150.0, "c": 150.005})
        det.push("EUR/USD", {"time": t, "o": 1.15, "h": 1.1501 + (i % 3) * 0.00001,
                             "l": 1.15, "c": 1.15005})
    gap_time = base + 40 * 60
    shock = {"time": gap_time, "o": 150.0, "h": 150.5, "l": 150.0, "c": 150.4}
    det.push("USD/JPY", shock)
    det.push("EUR/USD", {"time": gap_time, "o": 1.15, "h": 1.1501, "l": 1.15, "c": 1.15005})
    return check("gap in lookback suppresses signal", det.evaluate("USD/JPY", shock) is None)


def test_leader_never_signals():
    """EUR/USD is the reference and must never emit a shock itself."""
    print("\nleader exclusion")
    det = ShockDetector()
    base = 1_780_100_000 // 60 * 60   # выровнено по минуте
    for i in range(30):
        t = base + i * 60
        det.push("EUR/USD", {"time": t, "o": 1.15, "h": 1.1501, "l": 1.15, "c": 1.15005})
    t = base + 30 * 60
    big = {"time": t, "o": 1.15, "h": 1.16, "l": 1.15, "c": 1.159}
    det.push("EUR/USD", big)
    return check("EUR/USD produces no shock", det.evaluate("EUR/USD", big) is None)


def test_live_fires_once_and_early():
    """A forming candle signals on the tick that crosses, and only once."""
    print("\nlive candle")
    det = ShockDetector()
    base = 1_780_200_000 // 60 * 60   # выровнено по минуте
    # Ranges must vary: identical ranges give stdev 0, which range_zscore
    # correctly refuses to score. Real quiet minutes are never identical.
    for i in range(30):
        t = base + i * 60
        quiet = {"time": t, "o": 150.0, "h": 150.010 + (i % 3) * 0.001,
                 "l": 150.0, "c": 150.005}
        det.push("USD/JPY", quiet)
        det.push("EUR/USD", {"time": t, "o": 1.15, "h": 1.1501 + (i % 3) * 0.00001,
                             "l": 1.15, "c": 1.15005})

    t = base + 30 * 60
    det.push_live("EUR/USD", {"time": t, "o": 1.15, "h": 1.1501, "l": 1.15, "c": 1.15005})

    ok = True
    # Still small: no signal yet.
    small = {"time": t, "o": 150.0, "h": 150.012, "l": 150.0, "c": 150.011}
    ok &= check("small forming candle stays silent",
                det.evaluate_live("USD/JPY", small) is None)

    # Range explodes mid-minute — this is the moment the signal must fire.
    big = {"time": t, "o": 150.0, "h": 150.30, "l": 150.0, "c": 150.29}
    first = det.evaluate_live("USD/JPY", big)
    ok &= check("crossing tick fires", first is not None,
                "sigma={}".format(first["sigma"] if first else None))
    ok &= check("event marked live", bool(first and first.get("live")))

    # Range keeps growing: must NOT fire again for the same minute.
    bigger = {"time": t, "o": 150.0, "h": 150.60, "l": 150.0, "c": 150.55}
    ok &= check("same minute does not fire twice",
                det.evaluate_live("USD/JPY", bigger) is None)

    # Nor should the close-time top-up re-fire it.
    det.push("USD/JPY", bigger)
    ok &= check("close does not duplicate a live signal",
                det.evaluate("USD/JPY", bigger) is None)

    # A retrace leaving only a wick is still a shock (owner's call): the range
    # of the finished candle is what counts, not the body.
    det2 = ShockDetector()
    for i in range(30):
        t2 = base + i * 60
        det2.push("USD/JPY", {"time": t2, "o": 150.0, "h": 150.010 + (i % 3) * 0.001,
                              "l": 150.0, "c": 150.005})
        det2.push("EUR/USD", {"time": t2, "o": 1.15, "h": 1.1501 + (i % 3) * 0.00001,
                              "l": 1.15, "c": 1.15005})
    t2 = base + 30 * 60
    det2.push_live("EUR/USD", {"time": t2, "o": 1.15, "h": 1.1501, "l": 1.15, "c": 1.15005})
    wick = {"time": t2, "o": 150.0, "h": 150.30, "l": 150.0, "c": 150.002}
    ok &= check("wick-only candle still counts", det2.evaluate_live("USD/JPY", wick) is not None)
    return ok


def test_leader_labels_not_blocks():
    """The leader sets origin (solo / in_stream) and no longer suppresses."""
    print("\nleader labelling")
    det = ShockDetector()
    base = 1_780_300_000 // 60 * 60   # выровнено по минуте
    for i in range(30):
        t = base + i * 60
        det.push("USD/JPY", {"time": t, "o": 150.0, "h": 150.010 + (i % 3) * 0.001,
                             "l": 150.0, "c": 150.005})
        det.push("EUR/USD", {"time": t, "o": 1.15, "h": 1.1501 + (i % 3) * 0.00001,
                             "l": 1.15, "c": 1.15005})

    t = base + 30 * 60
    big = {"time": t, "o": 150.0, "h": 150.30, "l": 150.0, "c": 150.29}

    # Calm leader -> the move belongs to this pair alone.
    calm = ShockDetector()
    for i in range(30):
        t0 = base + i * 60
        calm.push("USD/JPY", {"time": t0, "o": 150.0, "h": 150.010 + (i % 3) * 0.001,
                              "l": 150.0, "c": 150.005})
        calm.push("EUR/USD", {"time": t0, "o": 1.15, "h": 1.1501 + (i % 3) * 0.00001,
                              "l": 1.15, "c": 1.15005})
    calm.push_live("EUR/USD", {"time": t, "o": 1.15, "h": 1.15012, "l": 1.15, "c": 1.15005})
    ev_solo = calm.evaluate_live("USD/JPY", big)
    ok = check("calm leader -> fires", ev_solo is not None)
    ok &= check("calm leader -> origin solo",
                bool(ev_solo and ev_solo.get("origin") == "solo"),
                "origin={}".format(ev_solo.get("origin") if ev_solo else None))

    # Noisy leader: the shock is still reported, but labelled as carried along.
    # Before 2026-08-04 this returned None and hid real events.
    det.push_live("EUR/USD", {"time": t, "o": 1.15, "h": 1.16, "l": 1.15, "c": 1.159})
    ev_stream = det.evaluate_live("USD/JPY", big)
    ok &= check("noisy leader -> still fires", ev_stream is not None)
    ok &= check("noisy leader -> origin in_stream",
                bool(ev_stream and ev_stream.get("origin") == "in_stream"),
                "origin={}".format(ev_stream.get("origin") if ev_stream else None))

    # No leader data at all: the origin is unknown rather than assumed.
    lone = ShockDetector()
    for i in range(30):
        t0 = base + i * 60
        lone.push("USD/JPY", {"time": t0, "o": 150.0, "h": 150.010 + (i % 3) * 0.001,
                              "l": 150.0, "c": 150.005})
    ev_unknown = lone.evaluate_live("USD/JPY", big)
    ok &= check("missing leader -> origin unknown, still fires",
                bool(ev_unknown and ev_unknown.get("origin") == "unknown"),
                "origin={}".format(ev_unknown.get("origin") if ev_unknown else None))
    return ok


def test_hub_candle_dialect():
    """Candles from CandleBuilder (open/high/low/close) must work as well.

    Regression: the detector originally read only o/h/l/c, so every live candle
    from the hub scored a zero range and no signal could ever fire. Production
    was silent for 11 hours before this was noticed.
    """
    print("\nhub candle dialect")
    from core.shock_detector import _range_of, direction

    ok = True
    hub_candle = {"time": 1785000000, "open": 150.0, "high": 150.5,
                  "low": 149.5, "close": 150.4}
    db_candle = {"time": 1785000000, "o": 150.0, "h": 150.5,
                 "l": 149.5, "c": 150.4}

    ok &= check("long keys give a real range", _range_of(hub_candle) == 1.0,
                "range={}".format(_range_of(hub_candle)))
    ok &= check("short keys still work", _range_of(db_candle) == 1.0)
    ok &= check("long keys give direction", direction(hub_candle) == 1)
    ok &= check("short keys give direction", direction(db_candle) == 1)
    ok &= check("missing keys stay safe", _range_of({"time": 1}) == 0.0)

    # End to end: feed the detector nothing but hub-dialect candles.
    det = ShockDetector()
    base = 1_780_500_000 // 60 * 60
    for i in range(30):
        t = base + i * 60
        det.push("USD/JPY", {"time": t, "open": 150.0,
                             "high": 150.010 + (i % 3) * 0.001,
                             "low": 150.0, "close": 150.005})
        det.push("EUR/USD", {"time": t, "open": 1.15,
                             "high": 1.1501 + (i % 3) * 0.00001,
                             "low": 1.15, "close": 1.15005})
    t = base + 30 * 60
    det.push_live("EUR/USD", {"time": t, "open": 1.15, "high": 1.15012,
                              "low": 1.15, "close": 1.15005})
    ev = det.evaluate_live("USD/JPY", {"time": t, "open": 150.0, "high": 150.30,
                                       "low": 150.0, "close": 150.29})
    ok &= check("hub-dialect candles produce a signal", ev is not None,
                "sigma={}".format(ev["sigma"] if ev else None))
    ok &= check("signal carries a direction", bool(ev and ev["direction"] == 1))
    return ok


def test_burst_shape():
    """Concentration separates a single jump from evenly spread movement."""
    print("\nburst shape")
    from core.shock_detector import BURST_MIN_COVERAGE, burst_coverage

    ok = True

    # All the movement inside a 4-second span, the rest of the minute flat.
    jump = [(float(i), 150.0) for i in range(0, 20)]
    jump += [(20.0, 150.0), (22.0, 150.30), (24.0, 150.30)]
    jump += [(float(i), 150.30) for i in range(25, 60)]
    cov, start = burst_coverage(jump, 0.30)
    ok &= check("single jump reads as concentrated", cov >= BURST_MIN_COVERAGE,
                "coverage={:.0f}%".format(cov * 100))

    # Same total range, but walked evenly across the whole minute.
    even = [(float(i), 150.0 + 0.30 * (i / 59.0)) for i in range(60)]
    cov2, _ = burst_coverage(even, 0.30)
    ok &= check("even walk reads as spread", cov2 < BURST_MIN_COVERAGE,
                "coverage={:.0f}%".format(cov2 * 100))

    # A burst straddling a 10s boundary must still be caught: this is exactly
    # what fixed slots got wrong on 4 of 6 real events.
    straddle = [(float(i), 150.0) for i in range(0, 8)]
    straddle += [(8.0, 150.0), (12.0, 150.30)]
    straddle += [(float(i), 150.30) for i in range(13, 60)]
    cov3, start3 = burst_coverage(straddle, 0.30)
    ok &= check("burst across a slot boundary still counts",
                cov3 >= BURST_MIN_COVERAGE,
                "coverage={:.0f}% starting at {}s".format(cov3 * 100, start3))

    # Degenerate inputs must not raise.
    ok &= check("empty ticks handled", burst_coverage([], 0.30) == (0.0, None))
    ok &= check("zero range handled", burst_coverage(jump, 0.0) == (0.0, None))

    # End to end through the detector. The base must be minute-aligned: candle
    # times always are in production, and push_tick derives its bucket from the
    # tick timestamp, so an unaligned base would mismatch the two.
    det = ShockDetector()
    base = 1_780_400_000 // 60 * 60
    for i in range(30):
        t = base + i * 60
        det.push("USD/JPY", {"time": t, "o": 150.0, "h": 150.010 + (i % 3) * 0.001,
                             "l": 150.0, "c": 150.005})
        det.push("EUR/USD", {"time": t, "o": 1.15, "h": 1.1501 + (i % 3) * 0.00001,
                             "l": 1.15, "c": 1.15005})
    t = base + 30 * 60
    det.push_live("EUR/USD", {"time": t, "o": 1.15, "h": 1.15012, "l": 1.15, "c": 1.15005})
    for off, price in jump:
        det.push_tick("USD/JPY", t + off, price)
    big = {"time": t, "o": 150.0, "h": 150.30, "l": 150.0, "c": 150.30}
    ev = det.evaluate_live("USD/JPY", big)
    ok &= check("event carries burst shape", bool(ev and ev.get("shape") == "burst"),
                "shape={}".format(ev.get("shape") if ev else None))

    # Without ticks the shape is unknown, not a false "burst".
    det2 = ShockDetector()
    for i in range(30):
        t2 = base + i * 60
        det2.push("USD/JPY", {"time": t2, "o": 150.0, "h": 150.010 + (i % 3) * 0.001,
                              "l": 150.0, "c": 150.005})
        det2.push("EUR/USD", {"time": t2, "o": 1.15, "h": 1.1501 + (i % 3) * 0.00001,
                              "l": 1.15, "c": 1.15005})
    t2 = base + 30 * 60
    det2.push_live("EUR/USD", {"time": t2, "o": 1.15, "h": 1.15012, "l": 1.15, "c": 1.15005})
    ev2 = det2.evaluate_live("USD/JPY", {"time": t2, "o": 150.0, "h": 150.30,
                                         "l": 150.0, "c": 150.30})
    ok &= check("no ticks -> shape unknown, still signals",
                bool(ev2 and ev2.get("shape") == "unknown"))
    return ok


def test_warmup_from_ticks():
    """Warmup fills history from the archive so no cold start is needed."""
    print("\nwarmup from ticks")
    from core.shock_detector import LOOKBACK, warmup_from_ticks

    ok = True
    data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

    # Anchor to a time the archive definitely covers rather than "now", so the
    # test does not depend on the market being open when it runs.
    anchor = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc).timestamp()
    det = ShockDetector()
    restored = warmup_from_ticks(det, data_dir,
                                 ["EUR/USD", "USD/JPY", "AUD/USD", "USD/CAD"],
                                 now_ts=anchor)

    ok &= check("every pair restored something", all(n > 0 for n in restored.values()),
                str(restored))
    ok &= check("history reaches the lookback",
                all(len(det.history(s)) >= LOOKBACK for s in restored),
                str({s: len(det.history(s)) for s in restored}))

    # The still-forming final minute must be excluded from history.
    last_times = [det.history(s)[-1]["time"] for s in restored if det.history(s)]
    ok &= check("forming minute left out of history",
                all(t < int(anchor // 60) * 60 for t in last_times))

    # A warmed detector must score immediately — that is the entire point.
    # Aim at the minute right after the restored history: warmup deliberately
    # drops the still-forming minute, so `anchor` itself is one minute further
    # on. In production that gap closes as soon as the live minute finishes.
    hist = det.history("USD/JPY")
    bucket = hist[-1]["time"] + 60
    big_range = max(c["h"] - c["l"] for c in hist) * 8
    ref = hist[-1]["c"]
    det.push_live("EUR/USD", {"time": bucket, "o": ref, "h": ref,
                              "l": ref, "c": ref})
    ev = det.evaluate_live("USD/JPY", {"time": bucket, "o": ref,
                                       "h": ref + big_range, "l": ref,
                                       "c": ref + big_range})
    ok &= check("warmed detector scores at once", ev is not None,
                "sigma={}".format(ev["sigma"] if ev else None))

    # Missing archive must not raise.
    det2 = ShockDetector()
    empty = warmup_from_ticks(det2, "/nonexistent-dir", ["EUR/USD"], now_ts=anchor)
    ok &= check("missing archive handled", empty == {"EUR/USD": 0}, str(empty))
    return ok


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
    bad = [h for h in fired if h["sigma"] < h["threshold"]]
    ok &= check("all events clear their own threshold", not bad,
                "{} violations".format(len(bad)) if bad else "")
    unlabelled = [h for h in fired if h.get("origin") not in ("solo", "in_stream", "unknown")]
    ok &= check("every event carries an origin label", not unlabelled,
                "{} missing".format(len(unlabelled)) if unlabelled else "")

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
        test_live_fires_once_and_early(),
        test_leader_labels_not_blocks(),
        test_hub_candle_dialect(),
        test_burst_shape(),
        test_warmup_from_ticks(),
        test_archive_replay(),
    ]
    print("\n{}".format("ALL PASS" if all(results) else "FAILURES PRESENT"))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
