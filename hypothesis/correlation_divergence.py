"""
Correlation divergence scanner: EURUSD vs USDJPY, M1 candles, US session.

Target: 2026-08-03 16:15–16:30 UTC.
For each minute: direction, body/range vs rolling 30-candle stats, flags.
"""
import sqlite3
import statistics
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import List, Tuple

DB_PATH = '/root/projects/terminal/market.db'

# ── helpers ──────────────────────────────────────────────────────────────────

@dataclass
class Candle:
    time: int
    o: float
    h: float
    l: float
    c: float

    @property
    def body(self) -> float:
        """Absolute body in points (pipettes)."""
        return abs(self.c - self.o)

    @property
    def direction(self) -> int:
        """+1 up (green), -1 down (red), 0 flat."""
        if self.c > self.o:
            return 1
        if self.c < self.o:
            return -1
        return 0

    @property
    def range_pts(self) -> float:
        return self.h - self.l

    @property
    def upper_wick(self) -> float:
        return self.h - max(self.o, self.c)

    @property
    def lower_wick(self) -> float:
        return min(self.o, self.c) - self.l


def load_candles(db, symbol: str, t_start: int, t_end: int) -> List[Candle]:
    rows = db.execute("""
        SELECT time, o, h, l, c
        FROM candles
        WHERE provider='fxcm' AND symbol=? AND tf='M1'
          AND time >= ? AND time < ?
        ORDER BY time
    """, (symbol, t_start, t_end)).fetchall()
    return [Candle(r[0], r[1], r[2], r[3], r[4]) for r in rows]


def rolling_stats(candles: List[Candle], window: int = 30):
    """Compute rolling mean/stdev for body and range over the last `window` candles."""
    bodies = [c.body for c in candles[-window:]]
    ranges = [c.range_pts for c in candles[-window:]]
    return {
        'body_mean': statistics.mean(bodies),
        'body_stdev': statistics.stdev(bodies) if len(bodies) >= 2 else 0,
        'range_mean': statistics.mean(ranges),
        'range_stdev': statistics.stdev(ranges) if len(ranges) >= 2 else 0,
    }


def zscore(value: float, mean: float, stdev: float) -> float:
    if stdev == 0:
        return 0.0
    return (value - mean) / stdev


# ── main analysis ────────────────────────────────────────────────────────────

def main():
    db = sqlite3.connect(DB_PATH)

    # Window: 16:00–16:35 UTC (target 16:15–16:30, plus context before & after)
    # Context for rolling stats: 15:25 onward (30 candles before 16:00)
    t_context = int(datetime(2026, 8, 3, 15, 25, 0, tzinfo=timezone.utc).timestamp())
    t_end     = int(datetime(2026, 8, 3, 16, 35, 0, tzinfo=timezone.utc).timestamp())
    t_target_start = int(datetime(2026, 8, 3, 16, 15, 0, tzinfo=timezone.utc).timestamp())
    t_target_end   = int(datetime(2026, 8, 3, 16, 31, 0, tzinfo=timezone.utc).timestamp())

    eu = load_candles(db, 'EUR/USD', t_context, t_end)
    uj = load_candles(db, 'USD/JPY', t_context, t_end)

    print(f"Loaded EUR/USD: {len(eu)} candles ({datetime.utcfromtimestamp(eu[0].time)} → {datetime.utcfromtimestamp(eu[-1].time)})")
    print(f"Loaded USD/JPY: {len(uj)} candles ({datetime.utcfromtimestamp(uj[0].time)} → {datetime.utcfromtimestamp(uj[-1].time)})")

    # Build lookup
    eu_map = {c.time: c for c in eu}
    uj_map = {c.time: c for c in uj}

    # Filter target candles
    target_times = sorted(set(eu_map.keys()) & set(uj_map.keys()))
    target_times = [t for t in target_times if t_target_start <= t < t_target_end]

    print(f"\n{'='*100}")
    print(f"CORRELATION DIVERGENCE ANALYSIS")
    print(f"Date: 2026-08-03 | Window: 16:15–16:30 UTC | EUR/USD vs USD/JPY | M1")
    print(f"{'='*100}")

    print(f"\n{'Time':<8} {'EU':>4} {'UJ':>4} {'Corr':>6} {'│'} "
          f"{'EU body':>9} {'EU σ':>6} {'UJ body':>9} {'UJ σ':>6} {'│'} "
          f"{'EU range':>8} {'EU rσ':>6} {'UJ range':>8} {'UJ rσ':>6} {'│'} "
          f"FLAGS")
    print(f"{'─'*8} {'─'*4} {'─'*4} {'─'*6} {'┼'} "
          f"{'─'*9} {'─'*6} {'─'*9} {'─'*6} {'┼'} "
          f"{'─'*8} {'─'*6} {'─'*8} {'─'*6} {'┼'} "
          f"{'─'*40}")

    # For each target candle, we need rolling stats from candles BEFORE it
    # Index into the full list to get the 30 preceding candles

    for t in target_times:
        c_eu = eu_map[t]
        c_uj = uj_map[t]

        # Find indices in the full lists
        eu_idx = next(i for i, c in enumerate(eu) if c.time == t)
        uj_idx = next(i for i, c in enumerate(uj) if c.time == t)

        # Rolling stats from 30 candles BEFORE this one
        eu_ctx = eu[max(0, eu_idx - 30):eu_idx]
        uj_ctx = uj[max(0, uj_idx - 30):uj_idx]

        eu_stats = rolling_stats(eu_ctx) if len(eu_ctx) >= 5 else {'body_mean': 0, 'body_stdev': 1, 'range_mean': 0, 'range_stdev': 1}
        uj_stats = rolling_stats(uj_ctx) if len(uj_ctx) >= 5 else {'body_mean': 0, 'body_stdev': 1, 'range_mean': 0, 'range_stdev': 1}

        # Z-scores
        eu_body_z = zscore(c_eu.body, eu_stats['body_mean'], eu_stats['body_stdev'])
        uj_body_z = zscore(c_uj.body, uj_stats['body_mean'], uj_stats['body_stdev'])
        eu_range_z = zscore(c_eu.range_pts, eu_stats['range_mean'], eu_stats['range_stdev'])
        uj_range_z = zscore(c_uj.range_pts, uj_stats['range_mean'], uj_stats['range_stdev'])

        # Direction strings
        eu_dir = '▲' if c_eu.direction == 1 else ('▼' if c_eu.direction == -1 else '─')
        uj_dir = '▲' if c_uj.direction == 1 else ('▼' if c_uj.direction == -1 else '─')

        # Correlation type
        # Standard: EURUSD inverse to USDJPY (EU up → UJ down, EU down → UJ up)
        if c_eu.direction == 0 or c_uj.direction == 0:
            corr_type = 'FLAT'
        elif c_eu.direction != c_uj.direction:
            corr_type = 'STD'   # standard inverse
        else:
            corr_type = 'DIVERGE'  # both same direction — break

        # Flags
        flags = []
        SIGMA = 2.0  # threshold for "significant"

        if corr_type == 'DIVERGE':
            flags.append('DIVERGE')

        if abs(eu_body_z) >= SIGMA:
            flags.append(f'euBody{eu_body_z:+.1f}σ')
        if abs(uj_body_z) >= SIGMA:
            flags.append(f'ujBody{uj_body_z:+.1f}σ')
        if abs(eu_range_z) >= SIGMA:
            flags.append(f'euRange{eu_range_z:+.1f}σ')
        if abs(uj_range_z) >= SIGMA:
            flags.append(f'ujRange{uj_range_z:+.1f}σ')

        # Amplitude asymmetry: one pair moves much more than the other
        # Compare body ratio to the typical ratio over the last 30
        if eu_stats['body_mean'] > 0 and uj_stats['body_mean'] > 0:
            typical_ratio = eu_stats['body_mean'] / uj_stats['body_mean']
            current_ratio = c_eu.body / c_uj.body if c_uj.body > 0 else 999
            ratio_change = current_ratio / typical_ratio if typical_ratio > 0 else 1.0
            if ratio_change >= 3.0:
                flags.append(f'EUdom({ratio_change:.1f}x)')
            elif ratio_change <= 0.33:
                flags.append(f'UJdom({1/ratio_change:.1f}x)')

        flag_str = ' '.join(flags) if flags else '—'

        dt = datetime.utcfromtimestamp(t).strftime('%H:%M:%S')

        # Color the correlation column
        corr_display = {
            'STD':     ' STD  ',
            'DIVERGE': '⚠DIVERGE',
            'FLAT':    ' FLAT ',
        }.get(corr_type, corr_type)

        print(f"{dt:<8} {eu_dir:>4} {uj_dir:>4} {corr_display:>9} {'│'} "
              f"{c_eu.body*10000:>7.1f}p {eu_body_z:>+5.1f}σ "
              f"{c_uj.body*100000:>7.1f}p {uj_body_z:>+5.1f}σ {'│'} "
              f"{c_eu.range_pts*10000:>6.1f}p {eu_range_z:>+5.1f}σ "
              f"{c_uj.range_pts*100000:>6.1f}p {uj_range_z:>+5.1f}σ {'│'} "
              f"{flag_str}")

    # Summary stats
    print(f"\n{'='*100}")
    print("SUMMARY")
    divergences = []
    for t in target_times:
        c_eu = eu_map[t]
        c_uj = uj_map[t]
        if c_eu.direction != 0 and c_uj.direction != 0 and c_eu.direction == c_uj.direction:
            divergences.append(t)

    print(f"Total minutes in window: {len(target_times)}")
    print(f"Divergences (same direction): {len(divergences)}")
    if divergences:
        for t in divergences:
            dt = datetime.utcfromtimestamp(t).strftime('%H:%M:%S')
            c_eu = eu_map[t]
            c_uj = uj_map[t]
            eu_d = '▲' if c_eu.direction == 1 else '▼'
            uj_d = '▲' if c_uj.direction == 1 else '▼'
            print(f"  {dt}: EU {eu_d} UJ {uj_d}")

    db.close()


if __name__ == '__main__':
    main()
