"""
UJ shock → next-minute reversal test. Two weeks of M1, rebuilt from tick archive.

Hypothesis under test
---------------------
When USD/JPY prints a range spike of >= SHOCK_SIGMA (default 10σ vs its own
trailing 30-candle range mean) while EUR/USD stays quiet (< CALM_SIGMA), the
shock candle itself reverses on the very next minute: a red spike is followed
by a green candle and vice versa.

Baseline for comparison: over the same sample, how often does *any* M1 USD/JPY
candle get followed by an opposite-colour candle? Without that number the hit
rate of the setup means nothing — M1 forex alternates colour close to half the
time by construction.

Data
----
market.db only keeps a 2000-bar window per timeframe (~33h of M1), so M1 here is
sliced from the eternal tick archive (data/<SYMBOL>_<YYYYMMDD>.csv) by the same
rule the hub uses: bucket = floor(ts / 60), OHLC over mid.

Exclusions
----------
Session opens and rollovers are cut with a +-EXCLUDE_MIN window, because the
liquidity gap around them produces range spikes that have nothing to do with
the effect being tested. Economic-news exclusion is NOT applied: the project's
data_loaders/news_calendar.csv only covers 2026-07-14..15 and does not reach the
sample period. Surviving events are printed with their timestamps so news can be
checked by hand.

Run: python3.10 hypothesis/uj_shock_reversal.py
"""
import csv
import os
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

DATA_DIR = '/root/projects/terminal/data'

# ── tunables ─────────────────────────────────────────────────────────────────
SHOCK_SIGMA = 10.0   # USD/JPY range z-score that counts as a shock
CALM_SIGMA = 2.0     # EUR/USD range z-score must stay below this
LOOKBACK = 30        # candles for the rolling range mean/stdev
EXCLUDE_MIN = 30     # +- minutes cut around session opens / rollover

# Session boundaries in UTC. FXCM's trading day rolls at 21:00 UTC (see the H4/D1
# grid note in CLAUDE.md), which is also the thinnest book of the day.
SESSION_EVENTS_UTC = [
    (21, 0, 'rollover / Sydney open'),
    (0, 0, 'Tokyo open'),
    (7, 0, 'London open'),
    (12, 30, 'US data slot'),
    (13, 30, 'NY open'),
    (20, 0, 'NY close'),
]


@dataclass
class Candle:
    time: int
    o: float
    h: float
    l: float
    c: float

    @property
    def direction(self) -> int:
        """+1 green, -1 red, 0 doji."""
        if self.c > self.o:
            return 1
        if self.c < self.o:
            return -1
        return 0

    @property
    def range_pts(self) -> float:
        return self.h - self.l

    @property
    def body(self) -> float:
        return abs(self.c - self.o)


# ── tick -> M1 ───────────────────────────────────────────────────────────────

def build_m1_from_ticks(symbol: str, date_str: str) -> Dict[int, Candle]:
    """Slice one day of archived ticks into M1 candles keyed by bucket start.

    Args:
        symbol: File-name symbol without a slash, e.g. "USDJPY".
        date_str: Day in YYYYMMDD form, matching the archive file name.

    Returns:
        Mapping of minute-bucket unix time to the Candle built from that
        minute's mid prices. Empty if the archive file is absent.
    """
    path = os.path.join(DATA_DIR, '{}_{}.csv'.format(symbol, date_str))
    if not os.path.exists(path):
        return {}

    candles: Dict[int, Candle] = {}
    with open(path, 'r') as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            try:
                ts = float(row['timestamp_utc'])
                mid = float(row['mid'])
            except (KeyError, TypeError, ValueError):
                continue
            bucket = int(ts // 60) * 60
            existing = candles.get(bucket)
            if existing is None:
                candles[bucket] = Candle(bucket, mid, mid, mid, mid)
            else:
                if mid > existing.h:
                    existing.h = mid
                if mid < existing.l:
                    existing.l = mid
                existing.c = mid
    return candles


def load_series(symbol: str, dates: List[str]) -> List[Candle]:
    """Build a continuous, time-sorted M1 series across several archive days.

    Args:
        symbol: File-name symbol without a slash, e.g. "EURUSD".
        dates: Day strings in YYYYMMDD form, any order.

    Returns:
        Candles sorted by time across all days that had archive files.
    """
    merged: Dict[int, Candle] = {}
    for d in dates:
        merged.update(build_m1_from_ticks(symbol, d))
    return [merged[t] for t in sorted(merged)]


# ── exclusion windows ────────────────────────────────────────────────────────

def excluded_reason(ts: int) -> Optional[str]:
    """Report why a timestamp falls inside a session-open exclusion window.

    Args:
        ts: Candle start time (unix seconds, UTC).

    Returns:
        Label of the nearest session event within EXCLUDE_MIN, else None.
    """
    dt = datetime.fromtimestamp(ts, timezone.utc)
    for hh, mm, label in SESSION_EVENTS_UTC:
        event = dt.replace(hour=hh, minute=mm, second=0, microsecond=0)
        for shifted in (event - timedelta(days=1), event, event + timedelta(days=1)):
            if abs((dt - shifted).total_seconds()) <= EXCLUDE_MIN * 60:
                return label
    return None


def zscore(value: float, mean: float, stdev: float) -> float:
    if stdev <= 0:
        return 0.0
    return (value - mean) / stdev


def rolling_range_stats(window: List[Candle]) -> Tuple[float, float]:
    """Mean and stdev of candle range over a lookback window.

    Args:
        window: Candles preceding the one under test.

    Returns:
        (mean, stdev) of range; (0.0, 0.0) when the window is too short.
    """
    if len(window) < 5:
        return 0.0, 0.0
    ranges = [c.range_pts for c in window]
    return statistics.mean(ranges), statistics.stdev(ranges)


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    """Scan the archive for UJ shocks and measure next-minute reversal rate."""
    # Two weeks back from the last complete session (2026-08-03).
    end = datetime(2026, 8, 3, tzinfo=timezone.utc)
    dates = [(end - timedelta(days=i)).strftime('%Y%m%d') for i in range(13, -1, -1)]

    print('Building M1 from tick archive: {} .. {}'.format(dates[0], dates[-1]))
    uj = load_series('USDJPY', dates)
    eu = load_series('EURUSD', dates)
    print('  USD/JPY: {} M1 candles'.format(len(uj)))
    print('  EUR/USD: {} M1 candles'.format(len(eu)))

    eu_by_time = {c.time: c for c in eu}
    uj_by_time = {c.time: c for c in uj}
    uj_index = {c.time: i for i, c in enumerate(uj)}

    # ── baseline: unconditional next-candle colour flip on USD/JPY ──
    base_total = 0
    base_flip = 0
    for i in range(len(uj) - 1):
        cur, nxt = uj[i], uj[i + 1]
        if nxt.time - cur.time != 60:
            continue  # skip weekend / feed-gap seams
        if cur.direction == 0 or nxt.direction == 0:
            continue
        base_total += 1
        if nxt.direction != cur.direction:
            base_flip += 1
    base_rate = base_flip / base_total * 100 if base_total else 0.0

    # ── scan for shocks ──
    events = []
    excluded = defaultdict(int)

    for i, c in enumerate(uj):
        if i < LOOKBACK:
            continue

        window = uj[i - LOOKBACK:i]
        # Require a contiguous lookback, otherwise the stats span a market gap.
        if window[-1].time != c.time - 60 or c.time - window[0].time != LOOKBACK * 60:
            continue

        mean, stdev = rolling_range_stats(window)
        if stdev <= 0:
            continue
        uj_z = zscore(c.range_pts, mean, stdev)
        if uj_z < SHOCK_SIGMA:
            continue

        # EUR/USD must be quiet on the same minute.
        eu_c = eu_by_time.get(c.time)
        if eu_c is None:
            excluded['no EUR/USD candle'] += 1
            continue
        eu_idx = next((j for j, x in enumerate(eu) if x.time == c.time), None)
        if eu_idx is None or eu_idx < LOOKBACK:
            excluded['no EUR/USD lookback'] += 1
            continue
        eu_window = eu[eu_idx - LOOKBACK:eu_idx]
        if eu_window[-1].time != c.time - 60:
            excluded['EUR/USD lookback gap'] += 1
            continue
        eu_mean, eu_stdev = rolling_range_stats(eu_window)
        eu_z = zscore(eu_c.range_pts, eu_mean, eu_stdev)
        if eu_z >= CALM_SIGMA:
            excluded['EUR/USD not calm'] += 1
            continue

        reason = excluded_reason(c.time)
        if reason:
            excluded['session window: ' + reason] += 1
            continue

        # Next minute must exist and be contiguous.
        nxt = uj_by_time.get(c.time + 60)
        if nxt is None:
            excluded['no next candle'] += 1
            continue

        events.append({
            'candle': c,
            'next': nxt,
            'uj_z': uj_z,
            'eu_z': eu_z,
            'eu_candle': eu_c,
        })

    # ── report ───────────────────────────────────────────────────────────────
    print('\n' + '=' * 108)
    print('UJ SHOCK -> NEXT-MINUTE REVERSAL   |   USD/JPY range >= {:.0f}s, EUR/USD < {:.0f}s   |   {} .. {}'
          .format(SHOCK_SIGMA, CALM_SIGMA, dates[0], dates[-1]))
    print('=' * 108)

    if not events:
        print('\nNo qualifying events.')
    else:
        print('\n{:<17} {:>7} {:>7} {:>4} {:>4} {:>10} {:>10}  {}'.format(
            'shock candle (UTC)', 'UJ sig', 'EU sig', 'dir', 'next', 'shock rng', 'next rng', 'outcome'))
        print('-' * 108)

        reversed_n = 0
        continued_n = 0
        doji_n = 0
        for ev in events:
            c, nxt = ev['candle'], ev['next']
            dt = datetime.fromtimestamp(c.time, timezone.utc).strftime('%Y-%m-%d %H:%M')
            d_cur = '▲' if c.direction == 1 else ('▼' if c.direction == -1 else '─')
            d_nxt = '▲' if nxt.direction == 1 else ('▼' if nxt.direction == -1 else '─')

            if c.direction == 0 or nxt.direction == 0:
                outcome = 'DOJI (no colour)'
                doji_n += 1
            elif nxt.direction != c.direction:
                outcome = 'REVERSED'
                reversed_n += 1
            else:
                outcome = 'continued'
                continued_n += 1

            print('{:<17} {:>+6.1f}s {:>+6.1f}s {:>4} {:>4} {:>9.1f}p {:>9.1f}p  {}'.format(
                dt, ev['uj_z'], ev['eu_z'], d_cur, d_nxt,
                c.range_pts * 100000, nxt.range_pts * 100000, outcome))

        decided = reversed_n + continued_n
        print('\n' + '-' * 108)
        print('Events: {}   reversed: {}   continued: {}   doji: {}'.format(
            len(events), reversed_n, continued_n, doji_n))
        if decided:
            hit = reversed_n / decided * 100
            print('Reversal rate: {}/{} = {:.1f}%'.format(reversed_n, decided, hit))
            print('Baseline (any UJ M1 candle): {}/{} = {:.1f}%'.format(base_flip, base_total, base_rate))
            print('Edge vs baseline: {:+.1f} pp'.format(hit - base_rate))
            # Binomial sanity check: stdev of the baseline over this sample size.
            se = (base_rate / 100 * (1 - base_rate / 100) / decided) ** 0.5 * 100
            print('1 s.e. at n={} is {:.1f} pp -> deviation is {:.1f} s.e.'.format(
                decided, se, abs(hit - base_rate) / se if se else 0))

    if excluded:
        print('\nFiltered out:')
        for k, v in sorted(excluded.items(), key=lambda kv: -kv[1]):
            print('  {:<40} {}'.format(k, v))

    print('\nNote: economic-news exclusion NOT applied — data_loaders/news_calendar.csv')
    print('covers only 2026-07-14..15 and does not reach this sample. Check the')
    print('timestamps above against a calendar by hand before trusting the rate.')


if __name__ == '__main__':
    main()
