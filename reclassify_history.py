"""
reclassify_history.py — одноразовая миграция signal_log.json.

Пересчитывает поле "result" для всех завершённых сигналов под НОВОЕ
симметричное правило (1 пипетта), используя сохранённые price / exit_price /
direction. Затем регенерирует signal_stats.json.

Правило (совпадает с signal_tracker.py):
  WIN  — fav_pips >=  WIN_THRESHOLD_PIPS  (>= 1 пипетта в сторону прогноза)
  LOSS — fav_pips <= -LOSS_THRESHOLD_PIPS (>= 1 пипетта против прогноза)
  NEUTRAL — ровно 0

ВАЖНО: запускать только при ОСТАНОВЛЕННОМ сервисе chart, иначе работающий
server.py перезапишет файл своим состоянием из памяти.

Использование:
    python3 reclassify_history.py
"""

import json
import os
import shutil
from datetime import datetime
from collections import Counter

SIGNAL_LOG_FILE = "signal_log.json"

# Должны совпадать с signal_tracker.py
WIN_THRESHOLD_PIPS  = 0.1   # 1 пипетта в сторону прогноза
LOSS_THRESHOLD_PIPS = 0.1   # 1 пипетта против прогноза

PIP_SIZE = {
    "EUR/USD": 0.0001,
    "USD/CAD": 0.0001,
    "AUD/USD": 0.0001,
    "USD/JPY": 0.01,
}


def reclassify_result(sig):
    """
    Пересчитывает исход одного сигнала под симметричное правило 1 пипетты.

    Args:
        sig (dict): запись сигнала с полями price, exit_price, direction, symbol.

    Returns:
        str | None: "WIN" / "LOSS" / "NEUTRAL", либо None если данных не хватает
                    (тогда исход оставляем как был).
    """
    price = sig.get("price")
    exit_price = sig.get("exit_price")
    direction = sig.get("direction")
    if price is None or exit_price is None or direction not in ("UP", "DOWN"):
        return None

    pip = PIP_SIZE.get(sig.get("symbol"), 0.0001)
    move = exit_price - price
    fav = move if direction == "UP" else -move
    fav_pips = round(fav / pip, 1)

    if fav_pips >= WIN_THRESHOLD_PIPS:
        return "WIN"
    elif fav_pips <= -LOSS_THRESHOLD_PIPS:
        return "LOSS"
    else:
        return "NEUTRAL"


def main():
    if not os.path.exists(SIGNAL_LOG_FILE):
        print("[migrate] signal_log.json не найден — нечего пересчитывать")
        return

    # 1. Бэкап
    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    backup = f"{SIGNAL_LOG_FILE}.bak_{stamp}"
    shutil.copy2(SIGNAL_LOG_FILE, backup)
    print(f"[migrate] Бэкап: {backup}")

    # 2. Загрузка
    with open(SIGNAL_LOG_FILE, "r") as f:
        data = json.load(f)

    # 3. Пересчёт
    before = Counter(s.get("result") for s in data if s.get("resolved"))
    changed = 0
    skipped = 0
    for s in data:
        if not s.get("resolved"):
            continue
        new = reclassify_result(s)
        if new is None:
            skipped += 1
            continue
        if new != s.get("result"):
            changed += 1
        s["result"] = new
    after = Counter(s.get("result") for s in data if s.get("resolved"))

    # 4. Запись лога
    with open(SIGNAL_LOG_FILE, "w") as f:
        json.dump(data, f, indent=2)

    # 5. Регенерация stats через логику самого трекера (читает уже новый файл)
    import signal_tracker as st  # импорт после записи: подхватит пересчитанный лог
    st._save_stats()

    # 6. Отчёт
    def wr(c):
        t = c["WIN"] + c["LOSS"]
        return c["WIN"] / t * 100 if t else 0.0

    print(f"[migrate] Записей: {len(data)} | изменено: {changed} | пропущено (нет цен): {skipped}")
    print(f"[migrate] БЫЛО:  {dict(before)}  winrate={wr(before):.1f}%")
    print(f"[migrate] СТАЛО: {dict(after)}  winrate={wr(after):.1f}%")
    print(f"[migrate] signal_stats.json регенерирован")
    print(f"[migrate] Откат при необходимости: cp {backup} {SIGNAL_LOG_FILE}")


if __name__ == "__main__":
    main()
