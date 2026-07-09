#!/usr/bin/env python3
"""
block_replay.py — тиковый реплей гипотезы «последнего сигнала блока» через
N-секундную тишину, С УЧЁТОМ ЗАДЕРЖКИ ВХОДА.

Контекст. Движок у края диапазона выдаёт блок сигналов (несколько подряд с
паузой ~30–180с). Замер по signal_log.json показал: «последний» сигнал блока
выигрывает заметно чаще (~57%), а приближение «последнего» через N-секундную
тишину переносит эдж (см. memory: signal-block-position-test). НО тот замер
считал исход от ИСХОДНОГО ts сигнала. В живой реализации «жди N секунд тишины
→ потом входи» вход случится на N секунд позже, и горизонт 240с отсчитывается
ОТ ВХОДА. Этот скрипт честно переоценивает исход:

    вход  = первый mid-тик на/после  ts + N
    выход = первый mid-тик на/после  ts + N + EXPIRY (240с)
    селекция: сигнал берётся, только если до СЛЕДУЮЩЕГО сигнала по той же паре
              пауза > N (т.е. он «последний» в пределах толеранса N).

Резолв 1:1 как в signal_tracker.resolve_signals:
    fav_pips = round(fav / pip, 1);  WIN >= +0.1,  LOSS <= -0.1,  иначе NEUTRAL
    ничья (возврат) исключается из WR.

N=0 — калибровочная строка (вход на самом ts, без задержки): WR должен
совпасть с тем, что лежит в логе, — проверка, что реплей читает тики верно.

Запуск:
  python3 block_replay.py                          # весь пул
  python3 block_replay.py --tight                  # только dist<=0.05
  python3 block_replay.py --symbols AUD/USD,USD/CAD
  python3 block_replay.py --ns 0,60,120,180,240,300
"""
import argparse
import bisect
import csv
import datetime as dt
import json
import os
import re

_HERE       = os.path.dirname(os.path.abspath(__file__))
LOG_FILE    = os.path.join(_HERE, "signal_log.json")
DATA_DIR    = os.path.join(_HERE, "data")
EXPIRY      = 240
WIN_PIPS    = 0.1
LOSS_PIPS   = 0.1
PIP_SIZE    = {"EUR/USD": 0.0001, "USD/CAD": 0.0001,
               "AUD/USD": 0.0001, "USD/JPY": 0.01}
GAP_TOL     = 300     # допустимая «несвежесть» удерживаемой цены (сек); больше — дыра в данных
_POS_RE     = re.compile(r"pos=([\d.]+)")

# Кэш тиков по (symbol, 'YYYYMMDD') → (times[], mids[])
_tick_cache = {}


def edge_dist(sig):
    """Нормализованное расстояние сигнала до входного края (как в pos_watcher).

    Args:
        sig: dict-сигнал из signal_log.json.
    Returns:
        float dist в [0,1] либо None, если краевого фактора нет.
    """
    m = _POS_RE.search(" ".join(sig.get("reason") or []))
    if not m:
        return None
    pos = float(m.group(1))
    return pos if sig.get("direction") == "UP" else 1.0 - pos


def _csv_path(symbol, day):
    """Путь к тиковому CSV для пары и даты ('YYYYMMDD')."""
    return os.path.join(DATA_DIR, f"{symbol.replace('/', '')}_{day}.csv")


def _load_day(symbol, day):
    """Загружает один день тиков в (times[], mids[]), кэширует. [] если файла нет.

    Args:
        symbol: пара, напр. 'USD/CAD'.
        day:    дата 'YYYYMMDD' (UTC).
    Returns:
        (times, mids) — два параллельных отсортированных списка float.
    """
    key = (symbol, day)
    if key in _tick_cache:
        return _tick_cache[key]
    times, mids = [], []
    path = _csv_path(symbol, day)
    if os.path.exists(path):
        with open(path, newline="") as f:
            r = csv.reader(f)
            next(r, None)  # header
            for row in r:
                try:
                    times.append(float(row[0]))
                    mids.append(float(row[5]))
                except (IndexError, ValueError):
                    continue
    _tick_cache[key] = (times, mids)
    return times, mids


def price_at(symbol, t):
    """Mid-цена, ДЕЙСТВУЮЩАЯ в момент t (step-and-hold).

    CSV — лог ИЗМЕНЕНИЙ цены (сервер пишет тик только при price != prev,
    see server.should_emit), т.е. ступенчатая функция. Цена в момент t —
    это последняя строка с ts <= t. Так же резолвится сервер: на ближайшем
    реальном тике >= t действует именно эта (удерживаемая) mid.

    Args:
        symbol: пара.
        t:      unix-время, на которое нужна действующая цена.
    Returns:
        float mid либо None, если t попал в дыру данных (несвежесть > GAP_TOL
        с обеих сторон) или истории до t нет.
    """
    day = dt.datetime.utcfromtimestamp(t).strftime("%Y%m%d")
    times, mids = _load_day(symbol, day)
    i = bisect.bisect_right(times, t) - 1 if times else -1

    if i < 0:
        # t раньше первой строки дня → перенос последней цены прошлого дня
        pday = dt.datetime.utcfromtimestamp(t - 86400).strftime("%Y%m%d")
        pt, pm = _load_day(symbol, pday)
        if pt and t - pt[-1] <= GAP_TOL:
            return pm[-1]
        return None

    prev_t = times[i]
    if i + 1 < len(times):
        next_t = times[i + 1]
    else:
        # t на/после последней строки дня → следующее изменение ищем в след. дне
        nday = dt.datetime.utcfromtimestamp(t + 86400).strftime("%Y%m%d")
        nt, _ = _load_day(symbol, nday)
        next_t = nt[0] if nt else float("inf")

    # дыра в данных: ни до, ни после t нет тика ближе GAP_TOL → цена ненадёжна
    if (t - prev_t) > GAP_TOL and (next_t - t) > GAP_TOL:
        return None
    return mids[i]


def resolve(symbol, direction, entry, exit_):
    """Исход сделки по правилам трекера. Returns 'WIN'/'LOSS'/'NEUTRAL'."""
    pip = PIP_SIZE.get(symbol, 0.0001)
    move = exit_ - entry
    fav_pips = round((move if direction == "UP" else -move) / pip, 1)
    if fav_pips >= WIN_PIPS:
        return "WIN"
    if fav_pips <= -LOSS_PIPS:
        return "LOSS"
    return "NEUTRAL"


def wr(rows):
    """(WR%, W, L, decided) по списку исходов; ничья исключена."""
    w = rows.count("WIN")
    l = rows.count("LOSS")
    d = w + l
    return (100 * w / d if d else 0.0), w, l, d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tight", action="store_true", help="только dist<=0.05")
    ap.add_argument("--symbols", default="", help="фильтр пар через запятую")
    ap.add_argument("--ns", default="0,60,120,180,240,300",
                    help="значения тишины N (сек) через запятую")
    args = ap.parse_args()

    ns = [int(x) for x in args.ns.split(",") if x.strip()]
    sym_filter = {s.strip() for s in args.symbols.split(",") if s.strip()}

    log = json.load(open(LOG_FILE))
    sigs = [s for s in log if s.get("resolved") and "ts" in s and "price" in s]
    if sym_filter:
        sigs = [s for s in sigs if s["symbol"] in sym_filter]
    if args.tight:
        sigs = [s for s in sigs
                if (lambda d: d is not None and d <= 0.05)(edge_dist(s))]
    sigs.sort(key=lambda s: (s["symbol"], s["ts"]))

    # next_gap: пауза до следующего сигнала по той же паре
    INF = float("inf")
    for i, s in enumerate(sigs):
        nxt = INF
        if i + 1 < len(sigs) and sigs[i + 1]["symbol"] == s["symbol"]:
            nxt = sigs[i + 1]["ts"] - s["ts"]
        s["_nextgap"] = nxt

    scope = "TIGHT dist<=0.05" if args.tight else "весь пул"
    if sym_filter:
        scope += " · " + ",".join(sorted(sym_filter))
    print("=" * 70)
    print(f"ТИКОВЫЙ РЕПЛЕЙ · задержка входа = N · {scope}")
    print(f"сигналов в выборке: {len(sigs)}  ·  горизонт {EXPIRY}с  ·  "
          f"ничья=возврат  ·  GAP_TOL={GAP_TOL}с")
    print("=" * 70)
    print("  N      WR     W/L         выстрелов  пропущено(нет тика)")

    for N in ns:
        sel = [s for s in sigs if s["_nextgap"] > N]
        outs, skipped = [], 0
        for s in sel:
            sym = s["symbol"]
            entry = price_at(sym, s["ts"] + N)
            if entry is None:
                skipped += 1
                continue
            exit_ = price_at(sym, s["ts"] + N + EXPIRY)
            if exit_ is None:
                skipped += 1
                continue
            outs.append(resolve(sym, s["direction"], entry, exit_))
        p, w, l, d = wr(outs)
        tag = "  ✅" if p >= 54.0 and d >= 30 else ""
        note = "  ← калибровка (≈лог)" if N == 0 else ""
        print(f"  {N:4d}  {p:5.1f}%  {w:5d}/{l:-5d}   n={d:-5d}     "
              f"skip={skipped}{tag}{note}")


if __name__ == "__main__":
    main()
