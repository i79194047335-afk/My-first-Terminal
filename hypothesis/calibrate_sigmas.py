"""
Calibrate per-pair shock thresholds over the last two weeks of M1.

USD/JPY is pinned at 8 sigma by the owner's decision. This script measures what
that threshold actually yields on USD/JPY, then reports the range-z distribution
for AUD/USD and USD/CAD so their thresholds can be set to a comparable event
rate rather than an arbitrary number.

EUR/USD is the leader: it is never a shock candidate here, it only has to be
calm (< CALM_SIGMA) for a followers' shock to count.

Run: python3.10 hypothesis/calibrate_sigmas.py
"""
import statistics
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, '/root/projects/terminal/hypothesis')

from uj_shock_reversal import (  # noqa: E402
    CALM_SIGMA,
    LOOKBACK,
    excluded_reason,
    load_series,
    rolling_range_stats,
    zscore,
)

FOLLOWERS = [('USDJPY', 'USD/JPY'), ('AUDUSD', 'AUD/USD'), ('USDCAD', 'USD/CAD')]
PERCENTILES = [50, 90, 99, 99.5, 99.9]


def collect_z(series, eu, eu_index):
    """Collect range z-scores for every candle with a calm EUR/USD counterpart.

    Args:
        series: Follower pair M1 candles, time-sorted.
        eu: EUR/USD M1 candles, time-sorted.
        eu_index: Map of EUR/USD candle time to its list index.

    Returns:
        List of (z, candle, next_candle_or_None) for qualifying minutes,
        excluding session windows.
    """
    by_time = {c.time: c for c in series}
    eu_by_time = {c.time: c for c in eu}
    out = []

    for i, c in enumerate(series):
        if i < LOOKBACK:
            continue
        window = series[i - LOOKBACK:i]
        if window[-1].time != c.time - 60 or c.time - window[0].time != LOOKBACK * 60:
            continue
        mean, stdev = rolling_range_stats(window)
        if stdev <= 0:
            continue

        eu_c = eu_by_time.get(c.time)
        eu_idx = eu_index.get(c.time)
        if eu_c is None or eu_idx is None or eu_idx < LOOKBACK:
            continue
        eu_window = eu[eu_idx - LOOKBACK:eu_idx]
        if eu_window[-1].time != c.time - 60:
            continue
        eu_mean, eu_stdev = rolling_range_stats(eu_window)
        if zscore(eu_c.range_pts, eu_mean, eu_stdev) >= CALM_SIGMA:
            continue

        if excluded_reason(c.time):
            continue

        out.append((zscore(c.range_pts, mean, stdev), c, by_time.get(c.time + 60)))
    return out


def percentile(sorted_vals, pct):
    """Nearest-rank percentile of an already-sorted list.

    Args:
        sorted_vals: Ascending values.
        pct: Percentile in 0..100.

    Returns:
        The value at that rank, or 0.0 for an empty list.
    """
    if not sorted_vals:
        return 0.0
    k = int(round(pct / 100.0 * (len(sorted_vals) - 1)))
    return sorted_vals[k]


def reversal_rate(rows, threshold):
    """Count next-minute reversals among candles at or above a z threshold.

    Args:
        rows: (z, candle, next_candle) tuples.
        threshold: Minimum z-score to include.

    Returns:
        (events, reversed_count, decided_count).
    """
    ev = rev = dec = 0
    for z, c, nxt in rows:
        if z < threshold:
            continue
        ev += 1
        if nxt is None or c.direction == 0 or nxt.direction == 0:
            continue
        dec += 1
        if nxt.direction != c.direction:
            rev += 1
    return ev, rev, dec


def baseline_flip(series):
    """Unconditional next-candle colour-flip rate for a pair.

    Args:
        series: M1 candles, time-sorted.

    Returns:
        (flip_count, decided_count, percent).
    """
    flip = total = 0
    for i in range(len(series) - 1):
        cur, nxt = series[i], series[i + 1]
        if nxt.time - cur.time != 60 or cur.direction == 0 or nxt.direction == 0:
            continue
        total += 1
        if nxt.direction != cur.direction:
            flip += 1
    return flip, total, (flip / total * 100 if total else 0.0)


def main():
    """Report z-score distributions and candidate thresholds per pair."""
    end = datetime(2026, 8, 3, tzinfo=timezone.utc)
    dates = [(end - timedelta(days=i)).strftime('%Y%m%d') for i in range(13, -1, -1)]
    days = len(dates)

    print('Sample: {} .. {} (tick archive -> M1)'.format(dates[0], dates[-1]))
    eu = load_series('EURUSD', dates)
    eu_index = {c.time: i for i, c in enumerate(eu)}
    print('EUR/USD (leader): {} M1 candles\n'.format(len(eu)))

    for file_sym, disp in FOLLOWERS:
        series = load_series(file_sym, dates)
        rows = collect_z(series, eu, eu_index)
        zs = sorted(z for z, _, _ in rows)

        flip, dec_base, base_pct = baseline_flip(series)

        print('=' * 92)
        print('{}   {} M1 candles, {} scored minutes (EUR/USD calm, sessions excluded)'.format(
            disp, len(series), len(rows)))
        print('=' * 92)
        print('  baseline colour flip: {}/{} = {:.1f}%'.format(flip, dec_base, base_pct))
        print('  range-z percentiles: ' + '  '.join(
            'p{}={:.1f}'.format(p, percentile(zs, p)) for p in PERCENTILES))
        print('  observed max z: {:.1f}'.format(zs[-1] if zs else 0.0))

        print('  {:>7}  {:>7}  {:>7}  {:>9}  {}'.format(
            'thresh', 'events', '/day', 'reversed', 'rate'))
        for thr in (5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 12.0):
            ev, rev, dec = reversal_rate(rows, thr)
            rate = '{:.0f}% ({}/{})'.format(rev / dec * 100, rev, dec) if dec else '-'
            mark = '   <- pinned' if (file_sym == 'USDJPY' and thr == 8.0) else ''
            print('  {:>7.0f}  {:>7}  {:>7.1f}  {:>9}  {}{}'.format(
                thr, ev, ev / days, rev if dec else '-', rate, mark))
        print()

    print('Note: reversal rates here are descriptive only. At these sample sizes')
    print('none of them separates from the baseline flip rate — see the two-week run.')


if __name__ == '__main__':
    main()
