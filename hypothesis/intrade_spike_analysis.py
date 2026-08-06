"""
Анализ устойчивости бэктеста «соло-всплеск» (UJ, 10-сек свечи).

Задачи:
  1. Где сидят no_data-события (тонкие окна?) — распределение по часу суток.
  2. Абляция фильтров для направления ПРОТИВ всплеска: результат идёт от всей
     стопки фильтров или от одного? Проверяем 0/1/2с задержки, пороги 4/6/8σ.
  3. Разбивка reversal по направлению всплеска (вверх/вниз) — эдж симметричен?

Run: python3.10 hypothesis/intrade_spike_analysis.py
"""
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from intrade_spike_10s_backtest import (  # noqa: E402
    ACCEL_MAX, DELAYS, EXPIRE_SEC, LEADER_CALM, LOOKBACK, MAX_GAP, PAYOUT,
    RETRACE_MAX, TF, build_candles, candle_sigmas, detect_events, load_ticks,
    outcome, price_at, retracement,
)


def hour_dist(times, mids, events):
    """Распределение no_data-событий по часу суток UTC.

    Args:
        times, mids: Тики UJ.
        events: События.

    Returns:
        dict {час: (всего, no_data)}.
    """
    dist = defaultdict(lambda: [0, 0])
    for ev in events:
        h = datetime.fromtimestamp(ev['time'], timezone.utc).hour
        dist[h][0] += 1
        if outcome(times, mids, ev, 0) is None:
            dist[h][1] += 1
    return dist


def ablation(times, mids, candles, sigmas, eu_sigmas, counts, dates, threshold):
    """Reversal-результат по стопке фильтров и без отдельных условий.

    Args:
        times, mids: Тики UJ.
        candles, sigmas: 10-сек свечи и их z-score.
        eu_sigmas: M1 EUR/USD z-score.
        counts: счётчики тиков (для detect_events).
        dates: дни.
        threshold: порог сигмы.

    Returns:
        dict: имя -> {delay: hit_rate}.
    """
    base_events, _ = detect_events(candles, sigmas, eu_sigmas, counts, dates)

    # варианты: полная стопка, без соло, без разгона, без отката, голый порог
    variants = {}

    def events_without(skip_solo=False, skip_accel=False, skip_retr=False):
        out = []
        for t in sorted(candles):
            sig = sigmas.get(t)
            if sig is None or sig < threshold:
                continue
            c = candles[t]
            direction, retr = retracement(c)
            if skip_retr:
                if direction == 0:
                    continue
            else:
                if direction == 0 or retr is None or retr > RETRACE_MAX:
                    continue
            minute = int(t // 60) * 60
            eu_sig = eu_sigmas.get(minute)
            if not skip_solo:
                if eu_sig is None or eu_sig >= LEADER_CALM:
                    continue
            if not skip_accel:
                bad = False
                for k in (1, 2, 3):
                    prev_sig = sigmas.get(t - k * TF)
                    if prev_sig is None or prev_sig > ACCEL_MAX:
                        bad = True
                        break
                if bad:
                    continue
            out.append({'time': t, 'close_time': t + TF, 'direction': direction})
        return out

    names = [
        ('полная стопка', events_without()),
        ('без фильтра соло', events_without(skip_solo=True)),
        ('без фильтра разгона', events_without(skip_accel=True)),
        ('без фильтра отката', events_without(skip_retr=True)),
        ('голый порог (ничего кроме σ)', events_without(skip_solo=True, skip_accel=True, skip_retr=True)),
    ]

    res = {}
    for name, evs in names:
        res[name] = {}
        for delay in DELAYS:
            n = w = 0
            for ev in evs:
                rev = dict(ev)
                rev['direction'] = -ev['direction']
                r = outcome(times, mids, rev, delay)
                if r is None:
                    continue
                n += 1
                if r == 'win':
                    w += 1
            res[name][delay] = (w, n, w / n if n else 0.0)
    return res


def main():
    """Собрать данные, напечатать распределение no_data и абляцию."""
    end = datetime(2026, 8, 6, tzinfo=timezone.utc)
    start = end - timedelta(days=61)
    dates = []
    d = start
    while d <= end:
        dates.append(d.strftime('%Y%m%d'))
        d += timedelta(days=1)

    print('Загрузка тиков {} .. {}'.format(dates[0], dates[-1]))
    uj_t, uj_p = load_ticks('USDJPY', dates)
    eu_t, eu_p = load_ticks('EURUSD', dates)
    uj_candles, uj_counts = build_candles(uj_t, uj_p, TF)
    eu_m1, _ = build_candles(eu_t, eu_p, 60)
    uj_sigmas = candle_sigmas(uj_candles, TF)
    eu_sigmas = candle_sigmas(eu_m1, 60)

    events, _ = detect_events(uj_candles, uj_sigmas, eu_sigmas, uj_counts, dates)
    events = [ev for ev in events if ev['sigma'] >= 3.0]

    print('\nNO_DATA по часу суток UTC (входов / без цены расчёта, порог 3σ):')
    dist = hour_dist(uj_t, uj_p, events)
    for h in range(24):
        tot, nd = dist.get(h, (0, 0))
        if tot:
            print('  {:>2}:00   {:>6}   no_data {:>4} ({:.0f}%)'.format(
                h, tot, nd, nd / tot * 100))

    print('\n' + '=' * 96)
    print('АБЛЯЦИЯ — направление ПРОТИВ всплеска (reversal), hit rate по задержкам')
    print('=' * 96)
    for thr in (4.0, 6.0, 8.0):
        print('\nПОРОГ {}σ'.format(thr))
        res = ablation(uj_t, uj_p, uj_candles, uj_sigmas, eu_sigmas, uj_counts, dates, thr)
        print('  {:<34} {:>8} {:>8} {:>8} {:>6}'.format('вариант', '0с', '1с', '2с', 'n@0с'))
        for name, by_delay in res.items():
            d0 = by_delay[0]
            print('  {:<34} {:>7.1f}% {:>7.1f}% {:>7.1f}% {:>6}'.format(
                name,
                d0[2] * 100, by_delay[1][2] * 100, by_delay[2][2] * 100, d0[1]))

    # разбивка по направлению всплеска, полная стопка
    print('\n' + '=' * 96)
    print('REVERSAL ПО НАПРАВЛЕНИЮ ВСПЛЕСКА (полная стопка, hit rate)')
    print('=' * 96)
    for thr in (4.0, 6.0, 8.0):
        evs = [ev for ev in events if ev['sigma'] >= thr]
        for d_sign in (1, -1):
            sel = [ev for ev in evs if ev['direction'] == d_sign]
            row = []
            for delay in DELAYS:
                n = w = 0
                for ev in sel:
                    rev = dict(ev)
                    rev['direction'] = -ev['direction']
                    r = outcome(uj_t, uj_p, rev, delay)
                    if r is None:
                        continue
                    n += 1
                    if r == 'win':
                        w += 1
                row.append((w, n))
            print('  {}σ {:>8} {:>5} событий: {}'.format(
                thr, 'вверх ▲' if d_sign == 1 else 'вниз ▼', '',
                len(sel)), end='')
            for delay, (w, n) in zip(DELAYS, row):
                print('   {}с:{}/{}={:.0f}%'.format(
                    delay, w, n, w / n * 100 if n else 0), end='')
            print()


if __name__ == '__main__':
    main()
