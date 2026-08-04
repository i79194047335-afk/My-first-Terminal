"""
Correlation divergence scanner v2: EURUSD vs USDJPY, M1 candles.
Extended window 13:00–17:00 UTC, 2026-08-03.
Adds: market regime classification (60-candle lookback on EURUSD).

Market regimes (based on preceding 60 candles):
  - TRENDING_UP / TRENDING_DN: efficiency ratio >= 0.25, clear slope
  - CONSOLIDATION: efficiency ratio < 0.15, range contracting (BB width < median)
  - CHOP: efficiency ratio < 0.15, range wide or expanding
  - NORMAL: everything else

Efficiency ratio = |net_displacement| / sum_of_absolute_bar_changes
"""
import sqlite3
import statistics
import math
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

DB_PATH = '/root/projects/terminal/market.db'

# ── data structures ──────────────────────────────────────────────────────────

@dataclass
class Candle:
    time: int
    o: float
    h: float
    l: float
    c: float

    @property
    def body(self) -> float:
        return abs(self.c - self.o)

    @property
    def direction(self) -> int:
        if self.c > self.o: return 1
        if self.c < self.o: return -1
        return 0

    @property
    def range_pts(self) -> float:
        return self.h - self.l

    @property
    def mid(self) -> float:
        return (self.h + self.l) / 2.0


@dataclass
class RegimeResult:
    regime: str           # TRENDING_UP, TRENDING_DN, CONSOLIDATION, CHOP, NORMAL
    efficiency_ratio: float
    slope_pct: float      # % price change over window
    bb_width: float       # current BB width relative to MA
    range_pct: float      # total range as % of price
    description: str


# ── helpers ──────────────────────────────────────────────────────────────────

def load_candles(db, symbol: str, t_start: int, t_end: int) -> List[Candle]:
    rows = db.execute("""
        SELECT time, o, h, l, c
        FROM candles
        WHERE provider='fxcm' AND symbol=? AND tf='M1'
          AND time >= ? AND time < ?
        ORDER BY time
    """, (symbol, t_start, t_end)).fetchall()
    return [Candle(r[0], r[1], r[2], r[3], r[4]) for r in rows]


def rolling_stats(candles: List[Candle]) -> dict:
    """Compute mean/stdev for body and range."""
    bodies = [c.body for c in candles]
    ranges = [c.range_pts for c in candles]
    if len(bodies) < 5:
        return {'body_mean': 0, 'body_stdev': 1, 'range_mean': 0, 'range_stdev': 1}
    return {
        'body_mean': statistics.mean(bodies),
        'body_stdev': statistics.stdev(bodies) if len(bodies) >= 2 else 1,
        'range_mean': statistics.mean(ranges),
        'range_stdev': statistics.stdev(ranges) if len(ranges) >= 2 else 1,
    }


def zscore(value: float, mean: float, stdev: float) -> float:
    if stdev == 0:
        return 0.0
    return (value - mean) / stdev


def classify_regime(candles: List[Candle], lookback: int = 60) -> RegimeResult:
    """
    Classify market regime based on the last `lookback` candles.
    Uses efficiency ratio + Bollinger-like bandwidth + range analysis.
    """
    window = candles[-lookback:] if len(candles) >= lookback else candles
    if len(window) < 30:
        return RegimeResult('INSUFFICIENT_DATA', 0, 0, 0, 0, 'need >=30 candles')

    mids = [c.mid for c in window]

    # Net displacement vs total path
    net_disp = abs(mids[-1] - mids[0])
    total_path = sum(abs(mids[i] - mids[i-1]) for i in range(1, len(mids)))
    eff_ratio = net_disp / total_path if total_path > 0 else 0

    # Slope: linear regression on mid prices
    n = len(mids)
    x_mean = (n - 1) / 2.0
    y_mean = statistics.mean(mids)
    num = sum((i - x_mean) * (mids[i] - y_mean) for i in range(n))
    den = sum((i - x_mean) ** 2 for i in range(n))
    slope = num / den if den != 0 else 0
    slope_pct = (slope * n / y_mean) * 100  # % price change over window

    # Bollinger Band width: (2σ / MA)
    ma = statistics.mean(mids)
    stdev_mid = statistics.stdev(mids) if len(mids) >= 2 else 0
    bb_width = (2 * stdev_mid) / ma if ma > 0 else 0

    # Total range as % of price
    hh = max(c.h for c in window)
    ll = min(c.l for c in window)
    range_pct = (hh - ll) / ma * 100 if ma > 0 else 0

    # Also compute BB width for the first half of the window (is range contracting?)
    half = len(window) // 2
    early_mids = mids[:half]
    early_ma = statistics.mean(early_mids)
    early_stdev = statistics.stdev(early_mids) if len(early_mids) >= 2 else 0
    early_bb = (2 * early_stdev) / early_ma if early_ma > 0 else 0
    bb_contracting = bb_width < early_bb * 0.85  # 15%+ contraction

    # ── classify ──
    if eff_ratio >= 0.25:
        if slope_pct > 0.05:
            regime = 'TRENDING_UP'
            desc = f'up trend, eff={eff_ratio:.2f}, slope=+{slope_pct:.2f}%'
        elif slope_pct < -0.05:
            regime = 'TRENDING_DN'
            desc = f'down trend, eff={eff_ratio:.2f}, slope={slope_pct:.2f}%'
        else:
            regime = 'NORMAL'
            desc = f'efficient but flat, eff={eff_ratio:.2f}'
    elif eff_ratio < 0.15:
        if bb_contracting:
            regime = 'CONSOLIDATION'
            desc = f'contracting range, eff={eff_ratio:.2f}, BB↓'
        else:
            regime = 'CHOP'
            desc = f'choppy, eff={eff_ratio:.2f}, BB wide'
    else:
        # eff_ratio 0.15-0.25
        if abs(slope_pct) > 0.03:
            regime = 'NORMAL'
            desc = f'mild direction, eff={eff_ratio:.2f}, slope={slope_pct:+.2f}%'
        elif bb_contracting:
            regime = 'CONSOLIDATION'
            desc = f'mild contraction, eff={eff_ratio:.2f}'
        else:
            regime = 'NORMAL'
            desc = f'mixed, eff={eff_ratio:.2f}'

    return RegimeResult(
        regime=regime,
        efficiency_ratio=eff_ratio,
        slope_pct=slope_pct,
        bb_width=bb_width,
        range_pct=range_pct,
        description=desc,
    )


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    db = sqlite3.connect(DB_PATH)

    # Window: 12:00–17:05 UTC (1h context for regime before 13:00)
    t_start   = int(datetime(2026, 8, 3, 12, 0, 0, tzinfo=timezone.utc).timestamp())
    t_end     = int(datetime(2026, 8, 3, 17, 5, 0, tzinfo=timezone.utc).timestamp())

    # Target window for reporting
    t_rpt_start = int(datetime(2026, 8, 3, 13, 0, 0, tzinfo=timezone.utc).timestamp())
    t_rpt_end   = int(datetime(2026, 8, 3, 17, 1, 0, tzinfo=timezone.utc).timestamp())

    eu = load_candles(db, 'EUR/USD', t_start, t_end)
    uj = load_candles(db, 'USD/JPY', t_start, t_end)

    print(f"EUR/USD: {len(eu)} candles ({datetime.utcfromtimestamp(eu[0].time)} → {datetime.utcfromtimestamp(eu[-1].time)})")
    print(f"USD/JPY: {len(uj)} candles ({datetime.utcfromtimestamp(uj[0].time)} → {datetime.utcfromtimestamp(uj[-1].time)})")

    eu_map = {c.time: c for c in eu}
    uj_map = {c.time: c for c in uj}

    # ── scan every minute ──
    all_times = sorted(set(eu_map.keys()) & set(uj_map.keys()))
    target_times = [t for t in all_times if t_rpt_start <= t < t_rpt_end]

    print(f"\n{'='*120}")
    print(f"CORRELATION DIVERGENCE SCAN — 2026-08-03 13:00–17:00 UTC — EUR/USD vs USD/JPY M1")
    print(f"{'='*120}")

    # Collect events for summary
    events = []  # (time, event_type, details)

    # Print only candles with notable events to keep output manageable
    print(f"\n{'─'*120}")
    print(f"LEGEND: STD = standard inverse corr | DIV = both same direction")
    print(f"        σ = z-score vs 30-candle rolling | REGIME = 60-candle lookback on EURUSD")
    print(f"{'─'*120}")

    notable_count = 0
    total_printed = 0

    for t in target_times:
        c_eu = eu_map[t]
        c_uj = uj_map[t]

        # Find indices
        eu_idx = next(i for i, c in enumerate(eu) if c.time == t)
        uj_idx = next(i for i, c in enumerate(uj) if c.time == t)

        # Rolling stats (30 candles)
        eu_ctx30 = eu[max(0, eu_idx - 30):eu_idx]
        uj_ctx30 = uj[max(0, uj_idx - 30):uj_idx]
        eu_st = rolling_stats(eu_ctx30) if len(eu_ctx30) >= 5 else {'body_mean': 0, 'body_stdev': 1, 'range_mean': 0, 'range_stdev': 1}
        uj_st = rolling_stats(uj_ctx30) if len(uj_ctx30) >= 5 else {'body_mean': 0, 'body_stdev': 1, 'range_mean': 0, 'range_stdev': 1}

        # Z-scores
        eu_body_z = zscore(c_eu.body, eu_st['body_mean'], eu_st['body_stdev'])
        uj_body_z = zscore(c_uj.body, uj_st['body_mean'], uj_st['body_stdev'])
        eu_range_z = zscore(c_eu.range_pts, eu_st['range_mean'], eu_st['range_stdev'])
        uj_range_z = zscore(c_uj.range_pts, uj_st['range_mean'], uj_st['range_stdev'])

        # Correlation
        if c_eu.direction == 0 or c_uj.direction == 0:
            corr_type = 'FLAT'
        elif c_eu.direction != c_uj.direction:
            corr_type = 'STD'
        else:
            corr_type = 'DIVERGE'

        # Regime (60 candles before this candle)
        eu_idx_in_full = eu_idx
        eu_ctx60 = eu[max(0, eu_idx_in_full - 60):eu_idx_in_full]
        regime = classify_regime(eu_ctx60, lookback=60)

        # Flags
        flags = []
        SIGMA = 2.0

        if corr_type == 'DIVERGE':
            flags.append('DIV')

        if abs(eu_body_z) >= SIGMA:
            flags.append(f'euB{eu_body_z:+.1f}σ')
        if abs(uj_body_z) >= SIGMA:
            flags.append(f'ujB{uj_body_z:+.1f}σ')
        if abs(eu_range_z) >= SIGMA:
            flags.append(f'euR{eu_range_z:+.1f}σ')
        if abs(uj_range_z) >= SIGMA:
            flags.append(f'ujR{uj_range_z:+.1f}σ')

        # Amplitude asymmetry
        if eu_st['body_mean'] > 0 and uj_st['body_mean'] > 0:
            typical_ratio = eu_st['body_mean'] / uj_st['body_mean']
            current_ratio = c_eu.body / c_uj.body if c_uj.body > 0 else 999
            ratio_change = current_ratio / typical_ratio if typical_ratio > 0 else 1.0
            if ratio_change >= 3.0:
                flags.append(f'EUdom{ratio_change:.0f}x')
            elif 0 < ratio_change <= 0.33:
                flags.append(f'UJdom{1/ratio_change:.0f}x')

        # Is this notable? (has flags OR is divergence)
        is_notable = len(flags) > 0

        if is_notable:
            notable_count += 1
            total_printed += 1
            dt = datetime.utcfromtimestamp(t).strftime('%H:%M:%S')

            eu_dir = '▲' if c_eu.direction == 1 else ('▼' if c_eu.direction == -1 else '─')
            uj_dir = '▲' if c_uj.direction == 1 else ('▼' if c_uj.direction == -1 else '─')

            corr_display = ' DIV ' if corr_type == 'DIVERGE' else (' FLAT' if corr_type == 'FLAT' else ' STD ')

            flag_str = ' '.join(flags) if flags else '—'

            # Compress: only print regime when it changes or on events
            regime_short = regime.regime[:4].ljust(4)  # T_UP, T_DN, CONS, CHOP, NORM

            print(f"{dt} {eu_dir} {uj_dir} {corr_display} │ "
                  f"euB={c_eu.body*10000:4.1f}p({eu_body_z:+.1f}σ) ujB={c_uj.body*100000:5.0f}p({uj_body_z:+.1f}σ) │ "
                  f"euR={c_eu.range_pts*10000:4.1f}p({eu_range_z:+.1f}σ) ujR={c_uj.range_pts*100000:5.0f}p({uj_range_z:+.1f}σ) │ "
                  f"regime={regime_short} eff={regime.efficiency_ratio:.2f} slope={regime.slope_pct:+.2f}% rng={regime.range_pct:.2f}% │ "
                  f"{flag_str}")

            # Store event
            events.append({
                'time': t,
                'dt': dt,
                'corr': corr_type,
                'eu_dir': eu_dir,
                'uj_dir': uj_dir,
                'flags': flags,
                'regime': regime.regime,
                'eu_body_z': eu_body_z,
                'uj_body_z': uj_body_z,
                'eu_range_z': eu_range_z,
                'uj_range_z': uj_range_z,
            })

            # No output limit for comprehensive scan

    # ── summary ──
    print(f"\n{'='*120}")
    print(f"SUMMARY: {notable_count} notable minutes out of {len(target_times)} total")

    # Divergence clusters (consecutive divergences)
    div_times = [e for e in events if e['corr'] == 'DIVERGE']
    print(f"\nDivergences: {len(div_times)} total")

    if div_times:
        # Group into clusters
        clusters = []
        current_cluster = [div_times[0]]
        for i in range(1, len(div_times)):
            if div_times[i]['time'] - div_times[i-1]['time'] == 60:  # consecutive minutes
                current_cluster.append(div_times[i])
            else:
                clusters.append(current_cluster)
                current_cluster = [div_times[i]]
        clusters.append(current_cluster)

        print(f"Divergence clusters (>=2 consecutive):")
        for cl in clusters:
            if len(cl) >= 2:
                start_dt = cl[0]['dt']
                end_dt = cl[-1]['dt']
                regime_str = cl[0]['regime']
                print(f"  {start_dt}–{end_dt}: {len(cl)} min, regime={regime_str}")
                for e in cl:
                    print(f"    {e['dt']}: EU {e['eu_dir']} UJ {e['uj_dir']} — {' '.join(e['flags'])}")

    # Amplitude anomalies (spikes, not necessarily divergences)
    spikes = [e for e in events if any('ujR' in f or 'euR' in f for f in e['flags'])]
    print(f"\nRange spikes (>=2σ): {len(spikes)}")
    for e in spikes:
        flags_with_range = [f for f in e['flags'] if 'R' in f and ('uj' in f or 'eu' in f)]
        print(f"  {e['dt']}: EU {e['eu_dir']} UJ {e['uj_dir']} — {', '.join(flags_with_range)} — regime={e['regime']}")

    # Dominance events
    doms = [e for e in events if any('dom' in f.lower() for f in e['flags'])]
    print(f"\nDominance anomalies (one pair >3x typical ratio): {len(doms)}")
    for e in doms:
        dom_flags = [f for f in e['flags'] if 'dom' in f.lower()]
        print(f"  {e['dt']}: EU {e['eu_dir']} UJ {e['uj_dir']} — {', '.join(dom_flags)} — regime={e['regime']}")

    # Regime distribution during divergences
    if div_times:
        from collections import Counter
        regime_counts = Counter(e['regime'] for e in div_times)
        print(f"\nDivergence regime distribution:")
        for reg, count in regime_counts.most_common():
            print(f"  {reg}: {count} ({count/len(div_times)*100:.0f}%)")

    db.close()


if __name__ == '__main__':
    main()
