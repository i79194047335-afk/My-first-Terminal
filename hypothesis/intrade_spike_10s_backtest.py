"""
Бэктест гипотезы «Соло-всплеск» для intrade-бота на 10-секундных свечах.

Гипотеза (владелец, 2026-08-07):

  1. Вход во время формирования M1, сразу после закрытия свечи-всплеска на
     10-секундном ТФ (не ждать закрытия M1), если откат на свече всплеска
     не более 10% (свеча почти без тени: close в 10% от экстремума).
  2. Режим «Соло»: ведущая EUR/USD спокойна в ту же минуту (< 2σ).
  3. Перед свечой всплеска нет свечей разгона: σ каждой из предыдущих 1-3
     10-секундных свечей не больше 2.

Всплеск = 10-секундная свеча, чей диапазон — z-score-аутлаер против своих
предыдущих LOOKBACK свечей. Порог — сетка (калибруется в прогоне).

Экспирация 60 с ровно от входа (договор intrade, выплата 82%). Три варианта
исполнения: мгновенно (0 с), задержка 1 с, задержка 2 с — модель латентности
сигнал→исполнение на площадке.

Направление ставки по умолчанию — ПО всплеску (continuation); для полноты
считается и обратное (reversal) тем же скриптом.

Данные: тиковый архив FXCM /root/projects/terminal/data/<SYM>_<YYYYMMDD>.csv —
прокси для котировок intrade по USD/JPY (другой истории по UJ у нас нет).

Run: python3.10 hypothesis/intrade_spike_10s_backtest.py [YYYYMMDD YYYYMMDD]
"""
import argparse
import csv
import os
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from uj_shock_reversal import excluded_reason  # noqa: E402

DATA_DIR = '/root/projects/terminal/data'

# ── параметры гипотезы ─────────────────────────────────────────────────────
TF = 10                  # секунд на свечу
LOOKBACK = 30            # свечей для роллинг-статистики диапазона
LEADER_CALM = 2.0        # EUR/USD ниже этой σ в ту же минуту -> «соло»
ACCEL_MAX = 2.0          # σ предыдущих 1-3 свечей не больше этого (нет разгона)
RETRACE_MAX = 0.10       # откат на свече всплеска не более 10% диапазона
EXPIRE_SEC = 60          # экспирация ровно через 60 с
DELAYS = [0, 1, 2]       # задержки исполнения, секунды
PAYOUT = 0.82            # выплата intrade по UJ
GRID = [3.0, 4.0, 5.0, 6.0, 8.0]  # сетка порогов сигмы для калибровки
MAX_GAP = float(os.environ.get('MAX_GAP', '3.0'))  # свежесть тика для цены входа/расчёта, с
EUROPE_START_UTC = 6    # европейская сессия ИСКЛЮЧАЕТСЯ из расчётов
EUROPE_END_UTC = 14     # 06:00–14:00 UTC (решение владельца, 2026-08-07)

# Торговый день FXCM заканчивается в 21:00 UTC; на intrade окно простоя
# брокера 21:00–23:00 UTC. День (UTC), на который вешать события — по метке.
FILES_TO_DATE = {}       # заполняется из каталога


def load_ticks(symbol, dates):
    """Загрузить тики за список дней в два отсортированных numpy-массива.

    Args:
        symbol: Имя файла без слэша, напр. 'USDJPY'.
        dates: Дни YYYYMMDD.

    Returns:
        (times, mids): float64 массивы, отсортированы по времени.
    """
    ts_all, p_all = [], []
    for d in dates:
        path = os.path.join(DATA_DIR, '{}_{}.csv'.format(symbol, d))
        if not os.path.exists(path):
            continue
        with open(path, 'r') as fh:
            for row in csv.DictReader(fh):
                try:
                    ts_all.append(float(row['timestamp_utc']))
                    p_all.append(float(row['mid']))
                except (KeyError, TypeError, ValueError):
                    continue
    order = np.argsort(ts_all)
    return np.array(ts_all)[order], np.array(p_all)[order]


def build_candles(times, mids, step):
    """Собрать свечи из тиков.

    Args:
        times: Массив времен тиков.
        mids: Массив mid-цен.
        step: Секунд на свечу (10).

    Returns:
        (candles, tick_counts): dict {time: {o,h,l,c}} и dict {time: кол-во тиков}.
    """
    candles = {}
    counts = defaultdict(int)
    for ts, price in zip(times, mids):
        bucket = int(ts // step) * step
        c = candles.get(bucket)
        if c is None:
            candles[bucket] = {'time': bucket, 'o': price, 'h': price,
                               'l': price, 'c': price}
        else:
            if price > c['h']:
                c['h'] = price
            if price < c['l']:
                c['l'] = price
            c['c'] = price
        counts[bucket] += 1
    return candles, counts


def range_zscore(candle, window):
    """Z-score диапазона свечи против предыдущих свечей.

    Args:
        candle: Свеча {o,h,l,c}.
        window: Предыдущие свечи (минимум 5).

    Returns:
        Z-score, или None если окно короткое / вырожденное.
    """
    if len(window) < 5:
        return None
    ranges = [c['h'] - c['l'] for c in window]
    mean = statistics.mean(ranges)
    try:
        stdev = statistics.stdev(ranges)
    except statistics.StatisticsError:
        return None
    if stdev <= 0:
        return None
    return (candle['h'] - candle['l'] - mean) / stdev


def candle_sigmas(candles, step):
    """Предсчитать z-score для каждой свечи с непрерывным окном LOOKBACK.

    Args:
        candles: dict {time: свеча}.
        step: Секунд на свечу.

    Returns:
        dict {time: z-score} — только свечи с полным непрерывным окном.
    """
    times = sorted(candles)
    n = len(times)
    if n == 0:
        return {}
    contig = [0] * n
    for i in range(1, n):
        contig[i] = contig[i - 1] + 1 if times[i] - times[i - 1] == step else 0

    sigmas = {}
    for i in range(n):
        if contig[i] < LOOKBACK:
            continue
        window = [candles[times[j]] for j in range(i - LOOKBACK, i)]
        sig = range_zscore(candles[times[i]], window)
        if sig is not None:
            sigmas[times[i]] = sig
    return sigmas


def retracement(candle):
    """Откат свечи от экстремума к close, в долях диапазона.

    Args:
        candle: Свеча {o,h,l,c}.

    Returns:
        (direction, retr): direction +1/-1/0, retr 0..0.5+ (None для doji).
        Доявщая свеча: откат = (h - c)/(h - l); падающая: (c - l)/(h - l).
    """
    rng = candle['h'] - candle['l']
    if rng <= 0:
        return 0, None
    if candle['c'] > candle['o']:
        return 1, (candle['h'] - candle['c']) / rng
    if candle['c'] < candle['o']:
        return -1, (candle['c'] - candle['l']) / rng
    return 0, None


def price_at(times, mids, t):
    """Последняя цена на или до времени t.

    Args:
        times: Массив времен тиков.
        mids: Массив цен.
        t: Момент времени (unix).

    Returns:
        (price, gap): цена и свежесть (t - время тика); None при отсутствии.
    """
    i = np.searchsorted(times, t, side='right') - 1
    if i < 0:
        return None, float('inf')
    return float(mids[i]), float(t - times[i])


def in_europe(ts):
    """Попадает ли момент в европейскую сессию (06:00–14:00 UTC).

    Args:
        ts: unix-время.

    Returns:
        True, если час UTC лежит в [EUROPE_START_UTC, EUROPE_END_UTC).
    """
    h = datetime.fromtimestamp(ts, timezone.utc).hour
    return EUROPE_START_UTC <= h < EUROPE_END_UTC


def detect_events(uj_candles, uj_sigmas, eu_sigma_m1, counts, dates,
                  use_sessions=True, use_solo=True):
    """Найти события «соло-всплеска» по условиям гипотезы.

    Европейская сессия (06:00–14:00 UTC) из расчётов исключается всегда.
    Новостные окна не применяются (календарь не покрывает период).

    Args:
        uj_candles: 10-сек свечи USD/JPY.
        uj_sigmas:  z-score по каждой свече UJ.
        eu_sigma_m1: dict {минута: z-score M1 EUR/USD}.
        counts: кол-во тиков по 10-сек бакету (для диагностики пустот).
        dates: список дней для метки дня.
        use_sessions: исключать ли окна сессий ±30м. False — без начала сессий.
        use_solo: проверять ли «соло» (EUR/USD спокойна). False — без соло.

    Returns:
        Список dict-событий: time, direction, close_time, sigma, retr,
        eu_sigma, day; пропуски — отдельно в счётчике.
    """
    times = sorted(uj_candles)
    events = []
    skipped = defaultdict(int)

    for t in times:
        sig = uj_sigmas.get(t)
        if sig is None or sig < min(GRID):
            continue  # ниже нижнего порога сетки — неинтересно
        c = uj_candles[t]
        direction, retr = retracement(c)
        if direction == 0 or retr is None or retr > RETRACE_MAX:
            skipped['откат >10% / doji'] += 1
            continue

        # соло (опционально): EUR/USD спокойна в ту же минуту
        eu_sig = None
        if use_solo:
            minute = int(t // 60) * 60
            eu_sig = eu_sigma_m1.get(minute)
            if eu_sig is None:
                skipped['нет данных EUR/USD'] += 1
                continue
            if eu_sig >= LEADER_CALM:
                skipped['ведущая шумная (не соло)'] += 1
                continue

        # без разгона: σ предыдущих 1-3 свечей не больше ACCEL_MAX
        accel_bad = False
        for k in (1, 2, 3):
            pt = t - k * TF
            prev_sig = uj_sigmas.get(pt)
            if prev_sig is None:
                accel_bad = True
                skipped['нет σ предыдущей свечи'] += 1
                break
            if prev_sig > ACCEL_MAX:
                accel_bad = True
                skipped['свеча разгона перед входом'] += 1
                break
        if accel_bad:
            continue

        if use_sessions and excluded_reason(t):
            skipped['окно сессии ±30м'] += 1
            continue

        # европейская сессия исключена из расчётов
        if in_europe(t):
            skipped['европейская сессия 06-14 UTC'] += 1
            continue

        events.append({
            'time': t,
            'close_time': t + TF,
            'direction': direction,
            'sigma': sig,
            'retr': retr,
            'eu_sigma': eu_sig,
            'day': datetime.fromtimestamp(t, timezone.utc).strftime('%Y-%m-%d'),
        })
    return events, skipped


def main():
    """Собрать серию за период, пройти сетку порогов и отчитаться.

    Flags:
        --no-sessions: не исключать окна начал сессий ±30 мин.
    """
    parser = argparse.ArgumentParser(description='Бэктест соло-всплеска UJ')
    parser.add_argument('dates', nargs='*', help='[start YYYYMMDD] [end YYYYMMDD]')
    parser.add_argument('--no-sessions', action='store_true',
                        help='не исключать окна начал сессий ±30м')
    parser.add_argument('--no-solo', action='store_true',
                        help='не проверять «соло» (EUR/USD спокойна)')
    args = parser.parse_args()

    if args.dates:
        start_d = args.dates[0]
        end_d = args.dates[1] if len(args.dates) > 1 else args.dates[0]
    else:
        # Два месяца, последний полный день 2026-08-06.
        end = datetime(2026, 8, 6, tzinfo=timezone.utc)
        start = end - timedelta(days=61)
        start_d = start.strftime('%Y%m%d')
        end_d = end.strftime('%Y%m%d')

    dates = []
    d = datetime.strptime(start_d, '%Y%m%d')
    end = datetime.strptime(end_d, '%Y%m%d')
    while d <= end:
        dates.append(d.strftime('%Y%m%d'))
        d += timedelta(days=1)

    print('Загрузка тиков {} .. {} ({} дней)'.format(dates[0], dates[-1], len(dates)))
    uj_t, uj_p = load_ticks('USDJPY', dates)
    eu_t, eu_p = load_ticks('EURUSD', dates)
    print('  USD/JPY тиков: {}   EUR/USD тиков: {}'.format(len(uj_t), len(eu_p)))

    uj_candles, uj_counts = build_candles(uj_t, uj_p, TF)
    eu_m1, _ = build_candles(eu_t, eu_p, 60)
    print('  10-сек свечей USD/JPY: {}'.format(len(uj_candles)))
    print('  M1 свечей EUR/USD: {}'.format(len(eu_m1)))

    uj_sigmas = candle_sigmas(uj_candles, TF)
    eu_sigmas = candle_sigmas(eu_m1, 60)
    print('  свечей UJ с окном: {}   M1 EUR/USD с окном: {}'.format(
        len(uj_sigmas), len(eu_sigmas)))

    # пустые 10-сек бакеты (без тиков) среди непрерывных серий
    times = sorted(uj_candles)
    empty = sum(1 for i in range(1, len(times)) if times[i] - times[i - 1] == 2 * TF)
    print('  соседних 10-сек бакетов с пропуском одного: {}'.format(empty))

    events, skipped = detect_events(
        uj_candles, uj_sigmas, eu_sigmas, uj_counts, dates,
        use_sessions=not args.no_sessions,
        use_solo=not args.no_solo)

    solo_txt = 'БЕЗ соло' if args.no_solo else 'соло(EUR/USD<{}σ)'.format(LEADER_CALM)
    print('\nКонфиг: {} + откат<={:.0f}% + без разгона(σ<={:.0f})'
          ' + БЕЗ Европы {:.0f}:00–{:.0f}:00 UTC + сессии±30м: {}'
          .format(solo_txt, RETRACE_MAX * 100, ACCEL_MAX,
                  EUROPE_START_UTC, EUROPE_END_UTC,
                  'ВЫКЛ' if args.no_sessions else 'вкл'))

    # ── калибровка: сетка порогов ──
    print('\n' + '=' * 96)
    print('КАЛИБРОВКА: события ({} + откат<=10% + без разгона + без Европы)'.format(solo_txt))
    print('=' * 96)
    print('{:>6}  {:>7} {:>8} {:>10} {:>10} {:>8}'.format(
        'порог', 'событий', 'за 2 мес', 'за день', 'входов*', 'continuation'))
    by_thr = defaultdict(list)
    for ev in events:
        for thr in GRID:
            if ev['sigma'] >= thr:
                by_thr[thr].append(ev)
    n_days = len(dates)
    for thr in GRID:
        lst = by_thr[thr]
        # hit rate continuation, мгновенное исполнение
        hit = cont_hit_rate(uj_t, uj_p, lst, 0)
        print('{:>6.1f}  {:>7} {:>8} {:>10.2f} {:>10} {:>8.1f}%'.format(
            thr, len(lst), '—', len(lst) / n_days, len(lst), hit * 100 if hit else 0))

    # ── основной прогон ──
    print('\n' + '=' * 96)
    print('ОСНОВНОЙ ПРОГОН')
    print('=' * 96)
    print('Пропущено по фильтрам (порог {}, соло, откат, разгон, Европа, сессии):'.format(min(GRID)))
    for k, v in sorted(skipped.items(), key=lambda kv: -kv[1]):
        print('  {:<42} {}'.format(k, v))

    # baseline: безусловная частота «цена выше через 60 с» на закрытиях свечей
    base = baseline(uj_t, uj_p, [ev['close_time'] for ev in events])
    print('\nBaseline по входам гипотезы (шанс выигрыша ставки «вверх»): {:.1f}%'.format(
        base * 100))

    for thr in GRID:
        lst = by_thr[thr]
        if not lst:
            continue
        print('\n' + '-' * 96)
        print('ПОРОГ {}σ   событий: {}   ({:.1f}/день)'.format(
            thr, len(lst), len(lst) / n_days))
        print('  {}'.format(', '.join(
            '{}: {}'.format(k, sum(1 for e in lst if e['day'] == k))
            for k in sorted(set(e['day'] for e in lst))[:12])))
        report(uj_t, uj_p, lst, 'continuation')
        report(uj_t, uj_p, lst, 'reversal')


def baseline(times, mids, entry_times):
    """Шанс «цена через 60 с выше входа» на заданных точках входа.

    Args:
        times, mids: Тики UJ.
        entry_times: Моменты входа (закрытие свечи всплеска).

    Returns:
        Доля входов, где цена через 60 с выше цены входа.
    """
    n = good = 0
    for t0 in entry_times:
        p0, g0 = price_at(times, mids, t0)
        p1, g1 = price_at(times, mids, t0 + EXPIRE_SEC)
        if p0 is None or p1 is None or g0 > MAX_GAP or g1 > MAX_GAP:
            continue
        n += 1
        good += (p1 > p0)
    return good / n if n else 0.0


def cont_hit_rate(times, mids, events, delay):
    """Hit rate continuation при заданной задержке (для калибровки).

    Args:
        times, mids: Тики UJ.
        events: Список событий.
        delay: Задержка исполнения, с.

    Returns:
        Доля выигранных ставок (без учёта возвратов), или 0 при пустом.
    """
    n = w = 0
    for ev in events:
        r = outcome(times, mids, ev, delay)
        if r is None:
            continue
        n += 1
        if r == 'win':
            w += 1
    return w / n if n else 0.0


def outcome(times, mids, ev, delay):
    """Исход одной ставки.

    Args:
        times, mids: Тики UJ.
        ev: Событие (вход после закрытия свечи + delay).
        delay: Задержка исполнения, с.

    Returns:
        'win'/'loss'/'refund', или None если цена недоступна/несвежа.
    """
    entry_t = ev['close_time'] + delay
    settle_t = entry_t + EXPIRE_SEC
    p0, g0 = price_at(times, mids, entry_t)
    p1, g1 = price_at(times, mids, settle_t)
    if p0 is None or p1 is None or g0 > MAX_GAP or g1 > MAX_GAP:
        return None
    if p1 > p0:
        return 'win' if ev['direction'] == 1 else 'loss'
    if p1 < p0:
        return 'win' if ev['direction'] == -1 else 'loss'
    return 'refund'


def report(times, mids, events, mode):
    """Напечатать сводку по направлению и задержкам.

    Args:
        times, mids: Тики UJ.
        events: События.
        mode: 'continuation' или 'reversal'.
    """
    print('\n  Направление: {}'.format(
        'ПО всплеску' if mode == 'continuation' else 'ПРОТИВ всплеска'))
    print('  {:>4}  {:>6} {:>6} {:>7} {:>7} {:>8} {:>8}'.format(
        'задерж', 'n', 'win', 'loss', 'refund', 'hit', 'EV(82%)'))
    for delay in DELAYS:
        res = defaultdict(int)
        for ev in events:
            # для reversal направление переворачивается
            ev_copy = dict(ev)
            if mode == 'reversal':
                ev_copy['direction'] = -ev['direction']
            r = outcome(times, mids, ev_copy, delay)
            if r is None:
                res['no_data'] += 1
                continue
            res[r] += 1
        n = res['win'] + res['loss']
        hit = res['win'] / n if n else 0.0
        ev_pnl = (res['win'] * PAYOUT - res['loss']) / (res['win'] + res['loss']) \
            if n else 0.0
        print('  {:>4}  {:>6} {:>6} {:>7} {:>7} {:>7.1f}% {:>+8.1f}%'.format(
            '{}с'.format(delay), res['win'] + res['loss'], res['win'],
            res['loss'], res['refund'], hit * 100, ev_pnl * 100))
        if res.get('no_data'):
            print('      (без данных по цене расчёта: {} — конец дня/разрыв)'.format(
                res['no_data']))


if __name__ == '__main__':
    main()
