"""
Run the UJ-shock scan over a single day (default: today), reusing the two-week
script's logic so results stay comparable.

The 10-sigma threshold from the two-week run is kept as the headline, but a
softer 5-sigma pass is printed too: one day rarely contains a 10-sigma event, and
the softer list shows whether anything was brewing at all.

Run: python3.10 hypothesis/uj_shock_today.py [YYYYMMDD]
"""
import sys
from datetime import datetime, timezone

sys.path.insert(0, '/root/projects/terminal/hypothesis')

from uj_shock_reversal import (  # noqa: E402
    CALM_SIGMA,
    LOOKBACK,
    Candle,
    excluded_reason,
    load_series,
    rolling_range_stats,
    zscore,
)


def scan(uj, eu, shock_sigma):
    """Find UJ range shocks with a calm EUR/USD on the same minute.

    Args:
        uj: USD/JPY M1 candles, time-sorted.
        eu: EUR/USD M1 candles, time-sorted.
        shock_sigma: Range z-score on USD/JPY that qualifies as a shock.

    Returns:
        List of event dicts with the shock candle, the next candle and z-scores.
    """
    eu_by_time = {c.time: c for c in eu}
    eu_index = {c.time: i for i, c in enumerate(eu)}
    uj_by_time = {c.time: c for c in uj}

    events = []
    for i, c in enumerate(uj):
        if i < LOOKBACK:
            continue
        window = uj[i - LOOKBACK:i]
        if window[-1].time != c.time - 60 or c.time - window[0].time != LOOKBACK * 60:
            continue
        mean, stdev = rolling_range_stats(window)
        if stdev <= 0:
            continue
        uj_z = zscore(c.range_pts, mean, stdev)
        if uj_z < shock_sigma:
            continue

        eu_c = eu_by_time.get(c.time)
        eu_idx = eu_index.get(c.time)
        if eu_c is None or eu_idx is None or eu_idx < LOOKBACK:
            continue
        eu_window = eu[eu_idx - LOOKBACK:eu_idx]
        if eu_window[-1].time != c.time - 60:
            continue
        eu_mean, eu_stdev = rolling_range_stats(eu_window)
        eu_z = zscore(eu_c.range_pts, eu_mean, eu_stdev)
        if eu_z >= CALM_SIGMA:
            continue

        reason = excluded_reason(c.time)
        nxt = uj_by_time.get(c.time + 60)
        events.append({
            'candle': c,
            'next': nxt,
            'uj_z': uj_z,
            'eu_z': eu_z,
            'excluded': reason,
        })
    return events


def render(events, title):
    """Print one scan's events as a table with reversal outcomes.

    Args:
        events: Event dicts from scan().
        title: Heading describing the threshold used.
    """
    print('\n' + '=' * 96)
    print(title)
    print('=' * 96)
    if not events:
        print('  nothing')
        return

    print('{:<9} {:>7} {:>7} {:>4} {:>4} {:>11} {:>11}  {}'.format(
        'time UTC', 'UJ sig', 'EU sig', 'dir', 'next', 'shock rng', 'next rng', 'outcome'))
    print('-' * 96)
    rev = cont = 0
    for ev in events:
        c, nxt = ev['candle'], ev['next']
        t = datetime.fromtimestamp(c.time, timezone.utc).strftime('%H:%M')
        d_cur = '▲' if c.direction == 1 else ('▼' if c.direction == -1 else '─')

        if ev['excluded']:
            outcome = 'EXCLUDED (' + ev['excluded'] + ')'
            d_nxt = '?'
            nxt_rng = float('nan')
        elif nxt is None:
            outcome = 'still forming / no next candle'
            d_nxt = '?'
            nxt_rng = float('nan')
        else:
            d_nxt = '▲' if nxt.direction == 1 else ('▼' if nxt.direction == -1 else '─')
            nxt_rng = nxt.range_pts * 100000
            if c.direction == 0 or nxt.direction == 0:
                outcome = 'doji'
            elif nxt.direction != c.direction:
                outcome = 'REVERSED'
                rev += 1
            else:
                outcome = 'continued'
                cont += 1

        print('{:<9} {:>+6.1f}s {:>+6.1f}s {:>4} {:>4} {:>10.1f}p {:>10.1f}p  {}'.format(
            t, ev['uj_z'], ev['eu_z'], d_cur, d_nxt,
            c.range_pts * 100000, nxt_rng, outcome))

    if rev + cont:
        print('\n  reversed {} / decided {} = {:.0f}%'.format(rev, rev + cont, rev / (rev + cont) * 100))


def main():
    """Scan one day for UJ shocks at 10-sigma and, more loosely, 5-sigma."""
    date_str = sys.argv[1] if len(sys.argv) > 1 else datetime.now(timezone.utc).strftime('%Y%m%d')

    uj = load_series('USDJPY', [date_str])
    eu = load_series('EURUSD', [date_str])
    if not uj or not eu:
        print('No archive for {}'.format(date_str))
        return

    span = '{} .. {} UTC'.format(
        datetime.fromtimestamp(uj[0].time, timezone.utc).strftime('%H:%M'),
        datetime.fromtimestamp(uj[-1].time, timezone.utc).strftime('%H:%M'))
    print('Date {}   USD/JPY {} M1, EUR/USD {} M1   ({})'.format(
        date_str, len(uj), len(eu), span))
    print('The last candle of the day is still forming — its range is partial.')

    render(scan(uj, eu, 10.0), 'STRICT: UJ range >= 10 sigma, EUR/USD < 2 sigma')
    render(scan(uj, eu, 5.0), 'SOFT: UJ range >= 5 sigma, EUR/USD < 2 sigma')


if __name__ == '__main__':
    main()
