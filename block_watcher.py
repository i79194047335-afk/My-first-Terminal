#!/usr/bin/env python3
"""
block_watcher.py — форвард-watcher фильтра «последнего сигнала блока».

Меряет, даёт ли block-фильтр (server.BLOCK_FILTER_N: отправлять из блока
сигналов у края только «последний», с задержкой входа ~N сек) реальный эдж
на ЖИВЫХ данных. Это единственный честный способ проверить эффект ЗАДЕРЖКИ
ВХОДА — офлайн-реплей по data/*.csv его восстановить не может (порог WIN/LOSS
= 1 пункт тоньше, чем разрешение тикового CSV; см. block_replay.py и memory:
signal-block-position-test).

Как отличаются сигналы в signal_log.json:
  - block-фильтрованные помечены тегом "⏳block_filter N=…" в reason
    (его добавляет server._fire_signal при отложенной отправке);
  - всё, что без тега, — обычные (немедленная отправка) → КОНТРОЛЬ.

Метрики (ничья=возврат, исключается; breakeven=54% при выплате 85%):
  ФОРВАРД   — block-сигналы ПОСЛЕ baseline (главная метрика, ради неё всё);
  КОНТРОЛЬ  — обычные сигналы тех же пар за тот же период (для сравнения);
  по символам — где эдж локализован.

Baseline фиксируется при первом запуске (макс. ts в логе) в
block_watcher_baseline.txt — мерим только то, что накопилось после включения
фильтра. Сбросить: --reset-baseline. Вешать на cron / /loop.

ВАЖНО: пока server.BLOCK_FILTER_N = 0, помеченных сигналов в логе нет —
watcher честно скажет «фильтр ещё не включён». Включение фильтра требует
правки server.py + рестарта сервиса chart (в окно даунтайма).
"""
import json
import os
import re
import sys
import math
import datetime as dt

_HERE         = os.path.dirname(os.path.abspath(__file__))
LOG_FILE      = os.path.join(_HERE, "signal_log.json")
BASELINE_FILE = os.path.join(_HERE, "block_watcher_baseline.txt")
BREAKEVEN     = 54.0

_TAG_RE = re.compile(r"block_filter N=(\d+)")


def is_filtered(sig):
    """True, если сигнал отправлен block-фильтром (есть тег в reason)."""
    return bool(_TAG_RE.search(" ".join(sig.get("reason") or [])))


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
    """z-статистика разности долей двух независимых групп (g2 выше g1 → +)."""
    if n1 == 0 or n2 == 0:
        return None
    p1, p2 = w1 / n1, w2 / n2
    pp = (w1 + w2) / (n1 + n2)
    se = math.sqrt(pp * (1 - pp) * (1 / n1 + 1 / n2))
    return (p2 - p1) / se if se > 0 else None


def summarize(rows):
    """Считает WR (ничья=возврат) и Wilson CI. Returns dict со счётчиками."""
    w = sum(1 for s in rows if s.get("result") == "WIN")
    l = sum(1 for s in rows if s.get("result") == "LOSS")
    nu = sum(1 for s in rows if s.get("result") == "NEUTRAL")
    dec = w + l
    wr = round(100 * w / dec, 1) if dec else 0.0
    lo, hi = wilson(w, dec)
    return {"w": w, "l": l, "neutral": nu, "decided": dec, "wr": wr, "ci": (lo, hi)}


def verdict(s):
    """Текстовый вердикт по статистике зоны."""
    if s["decided"] < 30:
        return "⏳ копим выборку"
    if s["ci"][0] >= BREAKEVEN:
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
    if "--reset-baseline" in sys.argv and os.path.exists(BASELINE_FILE):
        os.remove(BASELINE_FILE)
        print("baseline сброшен")

    if not os.path.exists(LOG_FILE):
        print(f"нет файла {LOG_FILE}")
        return

    log = json.load(open(LOG_FILE))
    resolved = [s for s in log if s.get("resolved") and "ts" in s]
    if not resolved:
        print("в логе нет resolved-сигналов")
        return

    max_ts = max(s["ts"] for s in resolved)
    filtered = [s for s in resolved if is_filtered(s)]

    print("=" * 74)
    print("ФОРВАРД-WATCHER · фильтр «последний сигнал блока» · ничья=возврат")
    print(f"лог: {len(resolved)} resolved · {len(filtered)} помечены block_filter · "
          f"диапазон {fmt(min(s['ts'] for s in resolved))} .. {fmt(max_ts)}")

    if not filtered:
        print("=" * 74)
        print("\nПомеченных block_filter сигналов в логе НЕТ.")
        print("→ server.BLOCK_FILTER_N всё ещё 0 (фильтр выключен) либо сервис")
        print("  chart не перезапущен после включения. Включить и ждать накопления.")
        print("  (baseline зафиксируется при появлении первого block-сигнала)")
        return

    # baseline = момент появления первого block-сигнала (= включения фильтра)
    if not os.path.exists(BASELINE_FILE):
        first_block_ts = min(s["ts"] for s in filtered)
        with open(BASELINE_FILE, "w") as f:
            f.write(str(first_block_ts))
        baseline, first_run = first_block_ts, True
    else:
        baseline, first_run = float(open(BASELINE_FILE).read().strip()), False

    print(f"baseline: {fmt(baseline)}" +
          ("  (1-й block-сигнал — зафиксирован сейчас)" if first_run else ""))
    print("=" * 74)

    syms = sorted({s["symbol"] for s in filtered})
    f_fwd = [s for s in filtered if s["ts"] > baseline]
    # контроль: обычные сигналы тех же пар после baseline
    ctrl = [s for s in resolved
            if not is_filtered(s) and s["ts"] > baseline and s["symbol"] in syms]

    print("\nФОРВАРД (block-сигналы после baseline) — ГЛАВНАЯ МЕТРИКА:")
    ff = summarize(f_fwd)
    print(line("block-фильтр", ff))
    print(line("КОНТРОЛЬ (без фильтра, те же пары)", summarize(ctrl))
          if ctrl else "  (контрольных сигналов после baseline пока нет)")
    cc = summarize(ctrl)
    z = two_prop_z(cc["w"], cc["decided"], ff["w"], ff["decided"])
    if z is not None:
        print(f"  z(фильтр vs контроль) = {z:+.2f}  "
              f"{'✅ фильтр выше' if z >= 1.96 else 'разница в пределах шума'}")

    print("\nПО СИМВОЛАМ (block-фильтр после baseline):")
    for sym in syms:
        rows = [s for s in f_fwd if s["symbol"] == sym]
        if rows:
            print(line(sym, summarize(rows)))

    print("\nСПРАВОЧНО (все block-сигналы, весь лог):")
    print(line("block-фильтр (весь лог)", summarize(filtered)))


if __name__ == "__main__":
    main()
