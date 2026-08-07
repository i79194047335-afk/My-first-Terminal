"""
Верификация reversal-сигнала с ослабленным MAX_GAP (5 с).

Проверки для полной стопки фильтров пользователя:
  1. Hit rate по задержкам и порогам 4/6/8σ (continuation и reversal).
  2. Для reversal 6σ+2с: разбивка по дням (не склеено ли в 2-3 дня).
  3. Значимость: сколько стандартных ошибок от безубытка 55% и от baseline.
  4. Разбивка по направлению всплеска.

Run: MAX_GAP=5 python3.10 hypothesis/intrade_spike_verify.py
"""
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

os.environ.setdefault('MAX_GAP', '5.0')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import intrade_spike_10s_backtest as bt  # noqa: E402


def hit_rev(times, mids, events, delay, direction, split_by_day=False):
    """Hit rate reversal (bet against spike) по задержке.

    Args:
        times, mids: Тики.
        events: События.
        delay: Задержка.
        direction: +1 (бет против ▲-всплеска = вниз) / -1 (против ▼ = вверх).
        split_by_day: вернуть по-дневную статистику.

    Returns:
        (win, n) или dict {day: (win, n)}.
    """
    if split_by_day:
        out = defaultdict(lambda: [0, 0])
        for ev in events:
            if direction is not None and ev['direction'] != direction:
                continue
            rev = dict(ev)
            rev['direction'] = -ev['direction']
            r = bt.outcome(times, mids, rev, delay)
            if r is None:
                continue
            out[ev['day']][0] += r == 'win'
            out[ev['day']][1] += 1
        return out
    w = n = 0
    for ev in events:
        if direction is not None and ev['direction'] != direction:
            continue
        rev = dict(ev)
        rev['direction'] = -ev['direction']
        r = bt.outcome(times, mids, rev, delay)
        if r is None:
            continue
        n += 1
        w += r == 'win'
    return w, n


def main():
    """Собрать события и напечатать верификацию."""
    import argparse
    parser = argparse.ArgumentParser(description='Верификация reversal-сигнала')
    parser.add_argument('--no-solo', action='store_true',
                        help='не проверять «соло» (EUR/USD спокойна)')
    args = parser.parse_args()

    end = datetime(2026, 8, 6, tzinfo=timezone.utc)
    start = end - timedelta(days=61)
    dates = []
    d = start
    while d <= end:
        dates.append(d.strftime('%Y%m%d'))
        d += timedelta(days=1)

    uj_t, uj_p = bt.load_ticks('USDJPY', dates)
    eu_t, eu_p = bt.load_ticks('EURUSD', dates)
    uj_candles, uj_counts = bt.build_candles(uj_t, uj_p, bt.TF)
    eu_m1, _ = bt.build_candles(eu_t, eu_p, 60)
    uj_sigmas = bt.candle_sigmas(uj_candles, bt.TF)
    eu_sigmas = bt.candle_sigmas(eu_m1, 60)
    events, _ = bt.detect_events(uj_candles, uj_sigmas, eu_sigmas, uj_counts, dates,
                                 use_sessions=False, use_solo=not args.no_solo)
    solo_txt = 'БЕЗ соло' if args.no_solo else 'с соло'

    print('MAX_GAP = {}   ({})'.format(bt.MAX_GAP, solo_txt))
    print('\nСводка по полной стопке фильтров (все события >= 3σ):')
    for thr in (4.0, 6.0, 8.0):
        sel = [ev for ev in events if ev['sigma'] >= thr]
        print('\nПОРОГ {}σ   событий: {}'.format(thr, len(sel)))
        for mode in ('continuation', 'reversal'):
            row = []
            for delay in bt.DELAYS:
                w = n = 0
                for ev in sel:
                    e = dict(ev)
                    if mode == 'reversal':
                        e['direction'] = -ev['direction']
                    r = bt.outcome(uj_t, uj_p, e, delay)
                    if r is None:
                        continue
                    n += 1
                    w += r == 'win'
                row.append((w, n))
            print('  {:<14}'.format(mode), end='')
            for delay, (w, n) in zip(bt.DELAYS, row):
                print('   {}с {}/{} = {:.1f}%'.format(delay, w, n, w / n * 100 if n else 0), end='')
            print()

    # Верификация reversal 6σ 2с
    print('\n' + '=' * 90)
    print('REVERSAL 6σ, задержка 2с — по дням (проверка склеивания)')
    print('=' * 90)
    sel6 = [ev for ev in events if ev['sigma'] >= 6.0]
    by_day = hit_rev(uj_t, uj_p, sel6, 2, None, split_by_day=True)
    total_w = total_n = 0
    for day in sorted(by_day):
        w, n = by_day[day]
        total_w += w
        total_n += n
        print('  {}  {:>3}/{:>3}  {:.0f}%'.format(day, w, n, w / n * 100 if n else 0))
    print('  ИТОГО      {:>3}/{:>3}  {:.1f}%'.format(total_w, total_n, total_w / total_n * 100 if total_n else 0))

    # Значимость
    p = total_w / total_n if total_n else 0.0
    for ref, label in ((0.55, 'безубыток 55%'), (0.514, 'baseline 51.4%'), (0.5, 'монета 50%')):
        se = (ref * (1 - ref) / total_n) ** 0.5 if total_n else 0.0
        print('  vs {}: {:+.1f} с.о.'.format(label, (p - ref) / se if se else 0))

    # Направление всплеска
    print('\nReversal 6σ 2с по направлению всплеска:')
    for d_sign, name in ((1, 'вверх ▲ (ставка вниз)'), (-1, 'вниз ▼ (ставка вверх)')):
        w, n = hit_rev(uj_t, uj_p, sel6, 2, d_sign)
        print('  {}: {}/{} = {:.1f}%'.format(name, w, n, w / n * 100 if n else 0))


if __name__ == '__main__':
    main()
