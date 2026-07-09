#!/usr/bin/env python3
"""
asia_watcher.py — форвард-watcher винрейта азиатских сигналов.

Читает signal_log.json (живые resolved-сигналы) и считает винрейт по
азиатской сессии (час 0-8 UTC) — тот карман, что выжил на OOS-реплее
(~61%). Задача: подтвердить эдж на СВЕЖИХ данных по мере накопления,
вне backtest-файла market_memory.json.

Особенности:
  - Конвенция исхода: ничья (NEUTRAL) = возврат → исключается из WR.
  - Фильтр "новая логика": сигналы старой логики содержали в reason
    удалённые факторы ("исторический ... UP/DOWN", "перевес"). Их
    отсутствие = сигнал сгенерирован уже текущим движком.
  - Baseline: при первом запуске фиксирует точку отсчёта (макс. ts в
    логе) в asia_watcher_baseline.txt, чтобы последующие запуски мерили
    ТОЛЬКО новые сигналы (истинный форвард). Сбросить: --reset-baseline.
  - Wilson 95% CI — выборки маленькие, точечный WR обманчив.

Запуск:
  python3 asia_watcher.py                 # полный отчёт
  python3 asia_watcher.py --reset-baseline
Можно вешать на cron / /loop для периодического опроса.
"""
import json
import os
import sys
import math
import datetime as dt

_HERE         = os.path.dirname(os.path.abspath(__file__))
LOG_FILE      = os.path.join(_HERE, "signal_log.json")
BASELINE_FILE = os.path.join(_HERE, "asia_watcher_baseline.txt")
BREAKEVEN     = 54.0   # % при выплате 85%, ничья=возврат

# строки-маркеры удалённых факторов старой логики
OLD_MARKERS = ("исторический", "перевес")


def is_asia(ts):
    """True, если ts попадает в азиатскую сессию (0-8 UTC)."""
    h = int(float(ts) % 86400) // 3600
    return 0 <= h < 8


def is_new_logic(sig):
    """True, если сигнал сгенерирован текущим движком (нет удалённых факторов)."""
    txt = " ".join(sig.get("reason") or [])
    return not any(m in txt for m in OLD_MARKERS)


def wilson(w, n, z=1.96):
    """Wilson 95% доверительный интервал для доли. Returns (low%, high%)."""
    if n == 0:
        return 0.0, 0.0
    p = w / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return round(100 * (c - m) / d, 1), round(100 * (c + m) / d, 1)


def summarize(rows):
    """Считает WR(ничья=возврат) и Wilson CI для набора сигналов.

    Args:
        rows: список dict-сигналов с полем result (WIN/LOSS/NEUTRAL).
    Returns:
        dict со счётчиками и интервалом.
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
    """Текстовый вердикт по статистике кармана."""
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
    asia = [s for s in resolved if is_asia(s["ts"])]
    asia_new = [s for s in asia if is_new_logic(s)]

    print("=" * 74)
    print("ФОРВАРД-WATCHER · азиатская сессия (0-8 UTC) · ничья=возврат")
    print(f"лог: {len(resolved)} resolved · диапазон {fmt(min(s['ts'] for s in resolved))}"
          f" .. {fmt(max_ts)}")
    print(f"baseline: {fmt(baseline)}" + ("  (зафиксирован сейчас, форвард копится с этого момента)"
                                          if first_run else ""))
    print("=" * 74)

    print("\nСПРАВОЧНО (весь лог, смешанная логика):")
    print(line("Asia · вся логика", summarize(asia)))
    print(line("Asia · новая логика", summarize(asia_new)))

    print("\nФОРВАРД (новые сигналы после baseline) — главная метрика:")
    fwd     = [s for s in asia     if s["ts"] > baseline]
    fwd_new = [s for s in asia_new if s["ts"] > baseline]
    if not fwd:
        print("  пока нет новых азиатских сигналов после baseline — запусти позже")
    else:
        print(line("Asia форвард · вся", summarize(fwd)))
        print(line("Asia форвард · новая", summarize(fwd_new)))

    # скользящие окна (новая логика)
    print("\nСКОЛЬЗЯЩИЕ ОКНА (новая логика):")
    for days in (7, 3, 1):
        cut = now - days * 86400
        win = [s for s in asia_new if s["ts"] >= cut]
        print(line(f"последние {days}д", summarize(win)))

    # разбивка по символам (новая логика, весь период)
    print("\nПО СИМВОЛАМ (Asia, новая логика, весь лог):")
    syms = sorted({s["symbol"] for s in asia_new})
    for sym in syms:
        print(line(sym, summarize([s for s in asia_new if s["symbol"] == sym])))


if __name__ == "__main__":
    main()
