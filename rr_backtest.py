"""
Позиционный бэктест существующих сигналов с фиксированными TP/SL (R:R).

В отличие от резолва трекера (T+240, только знак), здесь по реальным тикам
(data/*.csv) считается first-touch: что цена тронет первым — тейк или стоп.

Конфигурация:
  - дедуп «одна позиция за раз»: взяли первый сигнал, держим до выхода,
    новые сигналы по символу игнорируем пока сделка открыта;
  - сетка стопов (пипсы) × сетка R:R;
  - спред вычитается с каждой сделки (типичный ретейл);
  - кап удержания — конец UTC-дня входа (без переноса через ночь/выходные);
  - метрика — экспектанси в R и винрейт (TP-first %) по каждой клетке сетки.

Скрипт автономный: только чтение signal_log.json и data/*.csv.
"""

import os
import csv
import glob
import json
from collections import defaultdict

import numpy as np

DATA_DIR    = "data"
SIGNAL_LOG  = "signal_log.json"

SL_PIPS = [5, 8, 10, 15, 20]
RR_LIST = [1.0, 1.5, 2.0, 3.0]

# Типичный ретейл-спред (пипсы) и размер пипса по символу.
SPREAD = {"EUR/USD": 1.2, "USD/JPY": 1.3, "AUD/USD": 1.4, "USD/CAD": 1.8}
PIP    = {"EUR/USD": 0.0001, "USD/JPY": 0.01, "AUD/USD": 0.0001, "USD/CAD": 0.0001}
DEFAULT_SPREAD = 1.5


def load_ticks(symbol):
    """
    Загружает все тики символа из data/*.csv в numpy-массивы.

    Args:
        symbol: пара в формате "EUR/USD".

    Returns:
        (ts, mid, day_id): отсортированные по времени массивы unix-времени,
        mid-цены и id UTC-дня (ts // 86400). Пустые массивы, если файлов нет.
    """
    code = symbol.replace("/", "")
    files = sorted(glob.glob(os.path.join(DATA_DIR, f"{code}_*.csv")))
    ts_all, mid_all = [], []
    for fn in files:
        with open(fn, newline="") as f:
            rd = csv.reader(f)
            next(rd, None)  # header
            for row in rd:
                if len(row) < 6:
                    continue
                try:
                    ts_all.append(float(row[0]))
                    mid_all.append(float(row[5]))
                except ValueError:
                    continue
    if not ts_all:
        return np.array([]), np.array([]), np.array([])
    ts  = np.asarray(ts_all, dtype=np.float64)
    mid = np.asarray(mid_all, dtype=np.float64)
    order = np.argsort(ts, kind="mergesort")
    ts, mid = ts[order], mid[order]
    day_id = (ts // 86400).astype(np.int64)
    return ts, mid, day_id


def load_signals():
    """
    Читает сигналы из signal_log.json, сгруппированные по символу и
    отсортированные по времени.

    Returns:
        dict symbol -> list of (ts, direction).
    """
    with open(SIGNAL_LOG) as f:
        log = json.load(f)
    by_sym = defaultdict(list)
    for s in log:
        ts = s.get("ts")
        d  = s.get("direction")
        sym = s.get("symbol")
        if ts is None or d not in ("UP", "DOWN") or sym is None:
            continue
        by_sym[sym].append((float(ts), d))
    for sym in by_sym:
        by_sym[sym].sort(key=lambda x: x[0])
    return by_sym


def simulate_trade(ts, mid, day_id, entry_idx, direction, sl_pips, tp_pips, pip):
    """
    Симулирует одну сделку first-touch от entry_idx до конца UTC-дня входа.

    Args:
        ts, mid, day_id: массивы тиков символа.
        entry_idx: индекс тика входа.
        direction: "UP" (лонг) или "DOWN" (шорт).
        sl_pips, tp_pips: дистанции стопа и тейка в пипсах.
        pip: размер пипса символа.

    Returns:
        (outcome_pips, exit_ts):
        outcome_pips — знаковый ход в пипсах (gross, без спреда);
        exit_ts — время выхода (для дедупа).
    """
    entry = mid[entry_idx]
    # конец дня входа: последний индекс с тем же day_id
    end_idx = np.searchsorted(day_id, day_id[entry_idx], side="right") - 1
    if end_idx <= entry_idx:
        return 0.0, ts[entry_idx]

    seg = mid[entry_idx + 1: end_idx + 1]
    seg_ts = ts[entry_idx + 1: end_idx + 1]

    if direction == "UP":
        tp_price = entry + tp_pips * pip
        sl_price = entry - sl_pips * pip
        hit_tp = seg >= tp_price
        hit_sl = seg <= sl_price
    else:  # DOWN
        tp_price = entry - tp_pips * pip
        sl_price = entry + sl_pips * pip
        hit_tp = seg <= tp_price
        hit_sl = seg >= sl_price

    i_tp = int(np.argmax(hit_tp)) if hit_tp.any() else None
    i_sl = int(np.argmax(hit_sl)) if hit_sl.any() else None

    if i_tp is not None and (i_sl is None or i_tp <= i_sl):
        return +tp_pips, seg_ts[i_tp]
    if i_sl is not None and (i_tp is None or i_sl < i_tp):
        return -sl_pips, seg_ts[i_sl]

    # ни тейк, ни стоп — закрываем по концу дня (знаковый ход)
    move = (mid[end_idx] - entry) / pip
    if direction == "DOWN":
        move = -move
    return move, ts[end_idx]


def run_cell(ticks, signals, sl_pips, rr):
    """
    Прогоняет одну клетку сетки (sl_pips, rr) по всем символам с дедупом
    «одна позиция за раз».

    Returns:
        dict с агрегатами: n, wins, losses, sum_R, gross_pips_sum, per_symbol.
    """
    tp_pips = sl_pips * rr
    n = 0
    wins = 0
    losses = 0
    sum_R = 0.0
    per_sym = defaultdict(lambda: [0, 0, 0.0])  # n, wins, sum_R

    for sym, sigs in signals.items():
        ts, mid, day_id = ticks[sym]
        if ts.size == 0:
            continue
        pip = PIP.get(sym, 0.0001)
        spread = SPREAD.get(sym, DEFAULT_SPREAD)
        tmin, tmax = ts[0], ts[-1]
        open_until = -1.0

        for sig_ts, direction in sigs:
            if sig_ts < tmin or sig_ts > tmax:
                continue                 # нет тикового покрытия
            if sig_ts < open_until:
                continue                 # позиция ещё открыта — пропуск
            entry_idx = int(np.searchsorted(ts, sig_ts, side="left"))
            if entry_idx >= ts.size:
                continue

            outcome_pips, exit_ts = simulate_trade(
                ts, mid, day_id, entry_idx, direction, sl_pips, tp_pips, pip
            )
            net_pips = outcome_pips - spread
            R = net_pips / sl_pips

            n += 1
            sum_R += R
            if outcome_pips >= tp_pips - 1e-9:
                wins += 1
            elif outcome_pips <= -sl_pips + 1e-9:
                losses += 1
            per_sym[sym][0] += 1
            per_sym[sym][1] += 1 if outcome_pips >= tp_pips - 1e-9 else 0
            per_sym[sym][2] += R

            open_until = exit_ts

    return {"n": n, "wins": wins, "losses": losses,
            "sum_R": sum_R, "per_sym": per_sym}


def main():
    """Загружает данные, прогоняет сетку SL×RR и печатает таблицы."""
    print("Загрузка тиков...")
    signals = load_signals()
    ticks = {}
    for sym in signals:
        ticks[sym] = load_ticks(sym)
        ts = ticks[sym][0]
        cov = "нет тиков" if ts.size == 0 else \
            f"{ts.size} тиков"
        print(f"  {sym}: {cov}")

    print("\nГлавная метрика — экспектанси (E) в R на сделку, net после спреда.")
    print("Breakeven по винрейту: 1:1=50%  1.5:1=40%  2:1=33%  3:1=25%\n")

    # Заголовок сетки
    hdr = "SL\\RR  " + "".join(f"{rr:>10}:1" for rr in RR_LIST)
    print("=== E (R/сделку) ===")
    print(hdr)
    grids = {}
    for sl in SL_PIPS:
        row = f"{sl:>4}п  "
        for rr in RR_LIST:
            res = run_cell(ticks, signals, sl, rr)
            grids[(sl, rr)] = res
            e = res["sum_R"] / res["n"] if res["n"] else 0.0
            row += f"{e:>+12.3f}"
        print(row)

    print("\n=== Винрейт (TP-first %) / n ===")
    print(hdr)
    for sl in SL_PIPS:
        row = f"{sl:>4}п  "
        for rr in RR_LIST:
            res = grids[(sl, rr)]
            wr = res["wins"] / res["n"] * 100 if res["n"] else 0.0
            row += f"{wr:>7.1f}%/{res['n']:<4}"
        print(row)

    # Лучшая клетка по E
    best = max(grids.items(), key=lambda kv: (kv[1]["sum_R"] / kv[1]["n"]) if kv[1]["n"] else -9)
    (bsl, brr), bres = best
    be = bres["sum_R"] / bres["n"]
    print(f"\nЛучшая клетка: SL={bsl}п, R:R={brr}:1  →  E={be:+.3f} R/сделку, "
          f"n={bres['n']}, total={bres['sum_R']:+.1f}R")
    print("По символам в лучшей клетке (n | wr | E):")
    for sym, (cnt, w, sr) in sorted(bres["per_sym"].items()):
        wr = w / cnt * 100 if cnt else 0
        print(f"  {sym:<8} n={cnt:<5} wr={wr:5.1f}%  E={sr/cnt:+.3f}R")


if __name__ == "__main__":
    main()
