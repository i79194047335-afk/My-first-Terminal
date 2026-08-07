"""
Мартингейл по серии сигналов всплеска (UJ, 10-сек свечи).

Вопрос пользователя: при отрицательном результате первого сигнала ждём
следующий и так далее — СКОЛЬКО МИНУСОВ В РЯД БЫЛО. Это метрика выживаемости
мартингейла: максимальная серия убытков определяет, сколько удвоений ставки
нужно пережить (2^N) и хватит ли банкролла.

Расчёт на тех же событиях, что и основной бэктест:
  БЕЗ соло + откат<=10% + без разгона + БЕЗ Европы 06-14 UTC + без окон сессий.

Для каждой конфигурации (порог, направление, задержка):
  1. Хронологическая серия исходов сделок по сигналам (win/loss/refund).
  2. Распределение серий убытков: сколько раз выпало N минусов подряд.
  3. Симуляция классического мартингейла: ставка 1, после убытка *2, после
     выигрыша обратно в 1. Выплата 82% — возврат + прибыль 82% ставки.

Ключевая математика при выплате 82%: цикл «N убытков + выигрыш» даёт
net = 1 - 0.18*2^N (в единицах стартовой ставки). Положителен только при
N<=2. Уже серия из 3 убытков делает цикл минусовым, даже если следующий
выигрыш приходит. Мартингейл тут — минусовая EV, вопрос лишь глубины ямы.

Run: python3.10 hypothesis/intrade_spike_martingale.py [--no-sessions] [--no-solo]
"""
import argparse
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

os.environ.setdefault('MAX_GAP', '5.0')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import intrade_spike_10s_backtest as bt  # noqa: E402

PAYOUT = bt.PAYOUT  # 0.82: прибыль на выигрыш = 82% ставки


def trade_sequence(times, mids, events, direction, delay):
    """Серия сделок по сигналам в хронологическом порядке.

    Args:
        times, mids: Тики UJ.
        events: События (уже отфильтрованы по порогу).
        direction: +1 — по всплеску, -1 — против всплеска.
        delay: Задержка входа.

    Returns:
        Список 'win'/'loss'/'refund' (сделки без цены расчёта отброшены).
    """
    seq = []
    for ev in sorted(events, key=lambda e: e['time']):
        e = dict(ev)
        if direction == -1:
            e['direction'] = -ev['direction']
        r = bt.outcome(times, mids, e, delay)
        if r is not None:
            seq.append(r)
    return seq


def loss_streaks(seq):
    """Распределение серий убытков подряд.

    Refund не разрывает и не продолжает серию (ставка вернулась, события
    не было) — в мартингейле он прогрессию не сбрасывает, значит и «рядом
    убытков» разрывать не должен: трейдер ещё не отыгрался. Win разрывает.

    Args:
        seq: Список исходов 'win'/'loss'/'refund'.

    Returns:
        (max_streak, Counter{длина: кол-во серий}).
    """
    cnt = 0
    dist = Counter()
    mx = 0
    for r in seq:
        if r == 'loss':
            cnt += 1
        elif r == 'win':
            if cnt:
                dist[cnt] += 1
                mx = max(mx, cnt)
            cnt = 0
        # refund: серию не трогаем
    if cnt:
        dist[cnt] += 1
        mx = max(mx, cnt)
    return mx, dist


def martingale_sim(seq):
    """Симуляция мартингейла: ставка 1, после убытка *2, после win в 1.

    Args:
        seq: Список исходов 'win'/'loss'/'refund'.

    Returns:
        dict с итогами: trades, wins, net_units, max_stake, max_dd,
        blowup_at (кол-во раз ставка превысила 16/32/64/128 единиц).
    """
    stake = 1.0
    net = 0.0
    peak = 0.0
    max_dd = 0.0
    max_stake = 1.0
    wins = 0
    blowup = Counter()
    for r in seq:
        if r == 'win':
            net += PAYOUT * stake
            wins += 1
            stake = 1.0
        elif r == 'loss':
            net -= stake
            stake *= 2.0
            max_stake = max(max_stake, stake)
            for lim in (16, 32, 64, 128):
                if stake > lim:
                    blowup[lim] += 1
        # refund: ставка вернулась, прогрессия не меняется
        peak = max(peak, net)
        max_dd = max(max_dd, peak - net)
    return {
        'trades': len(seq),
        'wins': wins,
        'net_units': net,
        'max_stake': max_stake,
        'max_dd': max_dd,
        'blowup': blowup,
    }


def run_config(times, mids, events, direction, delay, label):
    """Прогнать одну конфигурацию и напечатать отчёт.

    Args:
        times, mids: Тики UJ.
        events: События по порогу.
        direction: +1 по всплеску / -1 против.
        delay: Задержка.
        label: Строка заголовка.
    """
    seq = trade_sequence(times, mids, events, direction, delay)
    if not seq:
        print('  {}: сделок нет'.format(label))
        return
    mx, dist = loss_streaks(seq)
    sim = martingale_sim(seq)
    n = len(seq)
    wins = seq.count('win')
    losses = seq.count('loss')
    refunds = seq.count('refund')
    hit = wins / n * 100

    print('\n  {}'.format(label))
    print('    сделок {:>4}   win {:>3} ({:.1f}%)   loss {:>3}   refund {:>2}'
          .format(n, wins, hit, losses, refunds))
    print('    MAX МИНУСОВ ПОДРЯД: {}'.format(mx))
    if dist:
        items = sorted(dist.items())
        print('    распределение серий убытков: ' + ', '.join(
            '{} минусов × {} раз'.format(k, v) for k, v in items))
    print('    мартингейл: итог {:+.1f} ставок   макс. ставка {:.0f}x   '
          'макс. просадка {:.1f} ставок   выигрыш-циклов {}'
          .format(sim['net_units'], sim['max_stake'], sim['max_dd'], sim['wins']))
    if sim['blowup']:
        parts = ', '.join('>{}=раз {}'.format(lim, sim['blowup'][lim])
                          for lim in sorted(sim['blowup']) if sim['blowup'][lim])
        print('    раз ставка превышала банкролл: ' + parts)


def main():
    """Собрать события и прогнать мартингейл по конфигурациям."""
    parser = argparse.ArgumentParser(description='Мартингейл по серии сигналов всплеска')
    parser.add_argument('--no-sessions', action='store_true',
                        help='не исключать окна начал сессий ±30м')
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

    print('Загрузка тиков {} .. {} ({} дней)'.format(dates[0], dates[-1], len(dates)))
    uj_t, uj_p = bt.load_ticks('USDJPY', dates)
    uj_candles, uj_counts = bt.build_candles(uj_t, uj_p, bt.TF)
    uj_sigmas = bt.candle_sigmas(uj_candles, bt.TF)
    # без соло EUR/USD не нужна — пустой dict, detect_events её не читает
    eu_sigmas = {}
    events, _ = bt.detect_events(uj_candles, uj_sigmas, eu_sigmas, uj_counts, dates,
                                 use_sessions=not args.no_sessions,
                                 use_solo=not args.no_solo)

    print('\nКонфиг: {} + откат<=10% + без разгона + БЕЗ Европы + сессии: {}'
          .format('БЕЗ соло' if args.no_solo else 'соло<{}σ'.format(bt.LEADER_CALM),
                  'ВЫКЛ' if args.no_sessions else 'вкл'))

    for thr in (4.0, 6.0, 8.0):
        sel = [ev for ev in events if ev['sigma'] >= thr]
        print('\n' + '=' * 92)
        print('ПОРОГ {}σ    событий: {}'.format(thr, len(sel)))
        print('=' * 92)
        for mode, d_sign, tag in (('ПО всплеску', 1, 'cont'),
                                  ('ПРОТИВ всплеска', -1, 'rev')):
            for delay in (0, 2):
                run_config(uj_t, uj_p, sel, d_sign, delay,
                           '{}  {}с  [{}]'.format(mode, delay, tag))


if __name__ == '__main__':
    main()
