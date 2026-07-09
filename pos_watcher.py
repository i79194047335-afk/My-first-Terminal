#!/usr/bin/env python3
"""
pos_watcher.py — форвард-watcher гипотезы "узкий край диапазона" (pos<=0.05).

Читает signal_log.json (живые resolved-сигналы) и проверяет, даёт ли
УЖЕСТОЧЕНИЕ зоны входа реальный эдж на СВЕЖИХ данных. Движок сейчас
стреляет при near_high/near_low в пределах 15% диапазона (structure_engine
NEAR=0.15). Гипотеза: эдж сидит только в самой кромке (<=5%), а полоса
0.05–0.15 — это breakeven-шум.

  edge_dist — нормализованное расстояние сигнала до ВХОДНОГО края:
    UP стоит у нижней границы → dist = pos
    DOWN стоит у верхней границы → dist = 1 - pos
  (извлекается из reason "pos=X.XX", уже логируется движком).

Зоны:
  TIGHT  dist<=0.05         — кандидат на оставление (гипотеза: эдж тут)
  MID    0.05<dist<=0.15    — кандидат на отбрасывание
  ALL    dist<=0.15         — текущее поведение движка

Главный тест — DISJOINT z(TIGHT vs MID): реально ли отбрасываемые сигналы
хуже оставляемых. Это НЕ subset-vs-superset (та ошибка завышает значимость).

Особенности (как в asia_watcher.py):
  - Конвенция исхода: ничья (NEUTRAL) = возврат → исключается из WR.
  - Baseline: при первом запуске фиксирует точку отсчёта (макс. ts в логе)
    в pos_watcher_baseline.txt — последующие запуски мерят ТОЛЬКО новые
    сигналы (истинный форвард). Сбросить: --reset-baseline.
  - Wilson 95% CI — выборки маленькие, точечный WR обманчив.

ВАЖНО (контекст, почему этот watcher вообще нужен): на пуле апр–июн
TIGHT vs MID даёт z=5.19, НО в чистом окне 12–17 июня (после reweight)
эдж исчезал (51.9% vs 53.4% baseline). Пуловая значимость может быть
наследием месяцев, где это работало. Поэтому — форвард, а не внедрение.

Запуск:
  python3 pos_watcher.py                 # полный отчёт
  python3 pos_watcher.py --reset-baseline
Можно вешать на cron / /loop для периодического опроса.
"""
import json
import os
import re
import sys
import math
import datetime as dt

_HERE         = os.path.dirname(os.path.abspath(__file__))
LOG_FILE      = os.path.join(_HERE, "signal_log.json")
BASELINE_FILE = os.path.join(_HERE, "pos_watcher_baseline.txt")
BREAKEVEN     = 54.0    # % при выплате 85%, ничья=возврат

TIGHT_MAX = 0.05        # кромка диапазона — кандидат на оставление
WIDE_MAX  = 0.15        # текущий порог движка (structure_engine NEAR)

_POS_RE = re.compile(r"pos=([\d.]+)")


def edge_dist(sig):
    """Нормализованное расстояние сигнала до его входного края.

    UP стоит у нижней границы (dist = pos), DOWN — у верхней (dist = 1-pos).
    Значение берётся из reason-строки "pos=X.XX", которую пишет движок.

    Args:
        sig: dict-сигнал из signal_log.json.
    Returns:
        float dist в [0,1], либо None если краевого фактора в сигнале нет.
    """
    txt = " ".join(sig.get("reason") or [])
    m = _POS_RE.search(txt)
    if not m:
        return None
    pos = float(m.group(1))
    return pos if sig.get("direction") == "UP" else 1.0 - pos


def wilson(w, n, z=1.96):
    """Wilson 95% доверительный интервал для доли. Returns (low%, high%)."""
    if n == 0:
        return 0.0, 0.0
    p = w / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return round(100 * (c - m) / d, 1), round(100 * (c + m) / d, 1)


def two_prop_z(w1, n1, w2, n2):
    """z-статистика разности долей для ДВУХ НЕЗАВИСИМЫХ (непересекающихся) групп.

    Args:
        w1,n1: победы/всего в группе 1. w2,n2: в группе 2.
    Returns:
        float z. Положительный → группа 2 выше группы 1. None если выборка пуста.
    """
    if n1 == 0 or n2 == 0:
        return None
    p1, p2 = w1 / n1, w2 / n2
    pp = (w1 + w2) / (n1 + n2)
    se = math.sqrt(pp * (1 - pp) * (1 / n1 + 1 / n2))
    return (p2 - p1) / se if se > 0 else None


def summarize(rows):
    """Считает WR(ничья=возврат) и Wilson CI для набора сигналов.

    Args:
        rows: список dict-сигналов с полем result (WIN/LOSS/NEUTRAL).
    Returns:
        dict со счётчиками, WR и интервалом.
    """
    w = sum(1 for s in rows if s.get("result") == "WIN")
    l = sum(1 for s in rows if s.get("result") == "LOSS")
    nu = sum(1 for s in rows if s.get("result") == "NEUTRAL")
    dec = w + l
    wr = round(100 * w / dec, 1) if dec else 0.0
    lo, hi = wilson(w, dec)
    return {"w": w, "l": l, "neutral": nu, "decided": dec,
            "wr": wr, "ci": (lo, hi)}


def verdict(s):
    """Текстовый вердикт по статистике зоны."""
    if s["decided"] < 30:
        return "⏳ копим выборку"
    lo = s["ci"][0]
    if lo >= BREAKEVEN:
        return "✅ значимо выше breakeven"
    if s["wr"] >= BREAKEVEN:
        return "🟡 выше breakeven, но CI задевает его"
    return "❌ не выше breakeven"


def line(label, s):
    return (f"  {label:24s} WR={s['wr']:5.1f}%  CI[{s['ci'][0]}–{s['ci'][1]}]  "
            f"n={s['decided']:4d} (W{s['w']}/L{s['l']}/ничья{s['neutral']})  {verdict(s)}")


def fmt(ts):
    return dt.datetime.utcfromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")


def zone_label(d):
    """Имя зоны по edge_dist."""
    if d <= TIGHT_MAX:
        return "tight"
    if d <= WIDE_MAX:
        return "mid"
    return "out"


def main():
    if "--reset-baseline" in sys.argv:
        if os.path.exists(BASELINE_FILE):
            os.remove(BASELINE_FILE)
        print("baseline сброшен")

    if not os.path.exists(LOG_FILE):
        print(f"нет файла {LOG_FILE}")
        return

    log = json.load(open(LOG_FILE))
    resolved = [s for s in log if s.get("resolved") and "ts" in s]
    # только сигналы с краевым фактором (near_high/near_low) — гипотеза про них
    edged = []
    for s in resolved:
        d = edge_dist(s)
        if d is not None:
            s["_dist"] = d
            edged.append(s)
    max_ts = max(s["ts"] for s in resolved) if resolved else 0

    # baseline: фиксируем точку отсчёта при первом запуске
    if not os.path.exists(BASELINE_FILE):
        with open(BASELINE_FILE, "w") as f:
            f.write(str(max_ts))
        baseline = max_ts
        first_run = True
    else:
        baseline = float(open(BASELINE_FILE).read().strip())
        first_run = False

    now = max_ts
    tight = [s for s in edged if s["_dist"] <= TIGHT_MAX]
    mid   = [s for s in edged if TIGHT_MAX < s["_dist"] <= WIDE_MAX]
    allz  = [s for s in edged if s["_dist"] <= WIDE_MAX]

    print("=" * 74)
    print("ФОРВАРД-WATCHER · узкий край pos<=0.05 · ничья=возврат")
    print(f"лог: {len(resolved)} resolved ({len(edged)} с краевым фактором) · "
          f"диапазон {fmt(min(s['ts'] for s in resolved))} .. {fmt(max_ts)}")
    print(f"baseline: {fmt(baseline)}" + ("  (зафиксирован сейчас, форвард копится с этого момента)"
                                          if first_run else ""))
    print("=" * 74)

    print("\nСПРАВОЧНО (весь лог) — зоны входа:")
    st, sm, sa = summarize(tight), summarize(mid), summarize(allz)
    print(line("TIGHT  dist<=0.05", st))
    print(line("MID    0.05–0.15", sm))
    print(line("ALL    dist<=0.15 (движок)", sa))
    z = two_prop_z(sm["w"], sm["decided"], st["w"], st["decided"])
    if z is not None:
        ok = "✅ TIGHT реально выше MID" if z >= 1.96 else "❌ разница в пределах шума"
        print(f"\n  DISJOINT z(TIGHT vs MID) = {z:+.2f}   {ok}")
        print("  (это и есть критерий: стоит ли отбрасывать полосу 0.05–0.15)")

    print("\nФОРВАРД (новые сигналы после baseline) — ГЛАВНАЯ МЕТРИКА:")
    f_tight = [s for s in tight if s["ts"] > baseline]
    f_mid   = [s for s in mid   if s["ts"] > baseline]
    if not f_tight and not f_mid:
        print("  пока нет новых краевых сигналов после baseline — запусти позже")
    else:
        ft, fm = summarize(f_tight), summarize(f_mid)
        print(line("TIGHT форвард", ft))
        print(line("MID   форвард", fm))
        z = two_prop_z(fm["w"], fm["decided"], ft["w"], ft["decided"])
        if z is not None:
            print(f"  DISJOINT z(forward) = {z:+.2f}")

        print("\nПО СИМВОЛАМ (TIGHT форвард после baseline):")
        if not f_tight:
            print("  пока нет новых TIGHT-сигналов после baseline")
        else:
            for sym in sorted({s["symbol"] for s in f_tight}):
                print(line(sym, summarize([s for s in f_tight if s["symbol"] == sym])))

    print("\nСКОЛЬЗЯЩИЕ ОКНА (TIGHT dist<=0.05):")
    for days in (7, 3, 1):
        cut = now - days * 86400
        print(line(f"последние {days}д", summarize([s for s in tight if s["ts"] >= cut])))

    print("\nПО СИМВОЛАМ (TIGHT dist<=0.05, весь лог):")
    for sym in sorted({s["symbol"] for s in tight}):
        print(line(sym, summarize([s for s in tight if s["symbol"] == sym])))


if __name__ == "__main__":
    main()
