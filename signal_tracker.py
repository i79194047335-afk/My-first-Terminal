"""
signal_tracker.py

Автоматический трекер результатов живых сигналов.

Логика:
  1. При каждом сигнале → store_signal() записывает сигнал с ценой и временем
  2. Через 240 секунд → resolve_signals() проверяет куда пошла цена
  3. Результат: WIN (цена пошла в сторону сигнала) или LOSS
  4. Статистика пишется в signal_log.json и выводится в консоль

Файлы:
  signal_log.json — полная история всех сигналов
  signal_stats.json — агрегированная статистика (обновляется при каждом resolve)
"""

import json
import os
import atexit
from datetime import datetime
from collections import defaultdict

SIGNAL_LOG_FILE   = "signal_log.json"
SIGNAL_STATS_FILE = "signal_stats.json"

# Через сколько секунд считаем результат
EXPIRY_SECONDS = 240

# Минимальное движение в пипсах чтобы считать результат значимым
# (меньше — считаем нейтральным, не WIN и не LOSS)
MIN_PIPS_RESULT = 1.0

PIP_SIZE = {
    "EUR/USD": 0.0001,
    "USD/CAD": 0.0001,
    "AUD/USD": 0.0001,
    "USD/JPY": 0.01
}

# ============================================================
# Состояние
# ============================================================

_active_signals  = []   # ждут экспирации
_signal_log      = []   # все завершённые сигналы
_signal_counter  = 0    # порядковый номер сигнала


# ============================================================
# Загрузка при старте
# ============================================================

def _load_log():
    global _signal_log, _signal_counter
    if os.path.exists(SIGNAL_LOG_FILE):
        try:
            with open(SIGNAL_LOG_FILE, "r") as f:
                data = json.load(f)
                _signal_log     = data
                _signal_counter = int(max((int(s.get("id", 0)) for s in data), default=0))
                print(f"[tracker] Загружено {len(_signal_log)} сигналов из лога")
        except:
            _signal_log     = []
            _signal_counter = 0
    else:
        print("[tracker] Лог сигналов не найден — начинаем с нуля")


def _save_log():
    with open(SIGNAL_LOG_FILE, "w") as f:
        json.dump(_signal_log, f, indent=2)


def _save_on_exit():
    if _signal_log:
        print(f"\n[tracker] Сохранение лога: {len(_signal_log)} сигналов...")
        _save_log()
        print_stats()

atexit.register(_save_on_exit)

_load_log()


# ============================================================
# Записать новый сигнал
# ============================================================

def store_signal(symbol, direction, confidence, winrate, samples, price, ts, reason=None):
    """
    Вызывается из server.py при каждом боевом сигнале.

    symbol     — пара
    direction  — "UP" или "DOWN"
    confidence — score-based уверенность (55-92)
    winrate    — реальный винрейт из памяти (float 0..1) или None
    samples    — кол-во паттернов в памяти
    price      — цена в момент сигнала
    ts         — unix timestamp
    reason     — список причин (опционально)
    """
    global _signal_counter
    _signal_counter += 1

    dt_str = datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")

    signal = {
        "id": int(_signal_counter),
        "symbol":     symbol,
        "direction":  direction,
        "confidence": confidence,
        "winrate":    round(winrate * 100, 1) if winrate is not None else None,
        "samples":    samples,
        "price":      price,
        "ts":         ts,
        "datetime":   dt_str,
        "reason":     reason or [],
        "result":     None,   # WIN / LOSS / NEUTRAL — заполнится позже
        "exit_price": None,
        "move_pips":  None,
        "resolved":   False
    }

    _active_signals.append(signal)

    #print(
        #f"[tracker] #{_signal_counter:04d} {dt_str} UTC | "
        #f"{symbol} | {direction} | "
        #f"conf={confidence}% | "
        #f"wr={f'{winrate*100:.1f}%' if winrate is not None else 'n/a'} "
        #f"({samples} паттернов) | "
        #f"price={price}"
    #)


# ============================================================
# Закрыть сигналы через 240 сек
# ============================================================

def resolve_signals(symbol, current_price, ts):
    """
    Вызывается из server.py на каждом тике.
    Закрывает сигналы той же пары у которых истекло время.
    """
    pip          = PIP_SIZE.get(symbol, 0.0001)
    still_active = []

    for sig in _active_signals:

        if sig["symbol"] != symbol:
            still_active.append(sig)
            continue

        if ts - sig["ts"] >= EXPIRY_SECONDS:

            move      = current_price - sig["price"]
            move_pips = round(abs(move) / pip, 1)

            # Определяем результат
            if move_pips < MIN_PIPS_RESULT:
                result = "NEUTRAL"
            elif sig["direction"] == "UP":
                result = "WIN" if move > 0 else "LOSS"
            else:  # DOWN
                result = "WIN" if move < 0 else "LOSS"

            sig["result"]     = result
            sig["exit_price"] = round(current_price, 5)
            sig["move_pips"]  = move_pips
            sig["resolved"]   = True

            _signal_log.append(sig)

            # Лог результата
            icon = "✅" if result == "WIN" else "❌" if result == "LOSS" else "⚪"
            #print(
                #f"[tracker] #{sig['id']:04d} {icon} {result:7} | "
                #f"{symbol} {sig['direction']} | "
                #f"entry={sig['price']} exit={sig['exit_price']} | "
                #f"move={move_pips}п | "
                #f"wr_pred={str(sig['winrate'])+'%' if sig['winrate'] is not None else 'n/a'}"
            #)

            # Автосохранение каждые 10 resolved сигналов
            if len(_signal_log) % 10 == 0:
                _save_log()
                _save_stats()

        else:
            still_active.append(sig)

    _active_signals[:] = still_active


# ============================================================
# Статистика
# ============================================================

def _calc_stats():
    """Считает агрегированную статистику по логу."""

    resolved = [s for s in _signal_log if s.get("resolved") and s["result"] != "NEUTRAL"]

    if not resolved:
        return None

    total = len(resolved)
    wins  = sum(1 for s in resolved if s["result"] == "WIN")
    losses= sum(1 for s in resolved if s["result"] == "LOSS")
    wr    = round(wins / total * 100, 1) if total > 0 else 0

    # По парам
    by_symbol = defaultdict(lambda: {"win": 0, "loss": 0, "neutral": 0})
    for s in _signal_log:
        if not s.get("resolved"): continue
        by_symbol[s["symbol"]][s["result"].lower()] += 1

    # По направлению
    by_dir = defaultdict(lambda: {"win": 0, "loss": 0})
    for s in resolved:
        by_dir[s["direction"]][s["result"].lower()] += 1

    # По часам UTC
    by_hour = defaultdict(lambda: {"win": 0, "loss": 0})
    for s in resolved:
        hour = (s["ts"] % 86400) // 3600
        by_hour[hour][s["result"].lower()] += 1

    # По confidence диапазонам
    by_conf = defaultdict(lambda: {"win": 0, "loss": 0})
    for s in resolved:
        c = s.get("confidence", 0)
        if c < 60:   band = "55-59%"
        elif c < 65: band = "60-64%"
        elif c < 70: band = "65-69%"
        elif c < 75: band = "70-74%"
        else:        band = "75%+"
        by_conf[band][s["result"].lower()] += 1

    # Средний ход
    moves = [s["move_pips"] for s in resolved if s.get("move_pips")]
    avg_move = round(sum(moves) / len(moves), 1) if moves else 0

    return {
        "total":     total,
        "wins":      wins,
        "losses":    losses,
        "neutral":   sum(1 for s in _signal_log if s.get("result") == "NEUTRAL"),
        "winrate":   wr,
        "avg_move":  avg_move,
        "by_symbol": dict(by_symbol),
        "by_dir":    dict(by_dir),
        "by_hour":   dict(by_hour),
        "by_conf":   dict(by_conf),
        "last_updated": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    }


def _save_stats():
    stats = _calc_stats()
    if stats:
        with open(SIGNAL_STATS_FILE, "w") as f:
            json.dump(stats, f, indent=2)


def print_stats():
    """Выводит текущую статистику в консоль."""

    resolved = [s for s in _signal_log if s.get("resolved") and s["result"] != "NEUTRAL"]

    if not resolved:
        print("[tracker] Нет завершённых сигналов")
        return

    total  = len(resolved)
    wins   = sum(1 for s in resolved if s["result"] == "WIN")
    losses = sum(1 for s in resolved if s["result"] == "LOSS")
    wr     = round(wins / total * 100, 1)

    print(f"\n{'='*55}")
    print(f"СТАТИСТИКА ЖИВЫХ СИГНАЛОВ")
    print(f"{'='*55}")
    print(f"  Всего сигналов:  {len(_signal_log)}")
    print(f"  Закрытых:        {total}")
    print(f"  WIN:             {wins}  ({wr}%)")
    print(f"  LOSS:            {losses}  ({round(100-wr,1)}%)")
    neutral = sum(1 for s in _signal_log if s.get("result") == "NEUTRAL")
    print(f"  NEUTRAL (<1п):   {neutral}")

    # По парам
    by_symbol = defaultdict(lambda: {"win": 0, "loss": 0})
    for s in resolved:
        by_symbol[s["symbol"]][s["result"].lower()] += 1

    print(f"\n  По парам:")
    for sym, v in sorted(by_symbol.items()):
        t  = v["win"] + v["loss"]
        wr_sym = round(v["win"] / t * 100, 1) if t > 0 else 0
        print(f"    {sym:10} | {t:3} сигналов | WIN {wr_sym}%")

    # По направлению
    by_dir = defaultdict(lambda: {"win": 0, "loss": 0})
    for s in resolved:
        by_dir[s["direction"]][s["result"].lower()] += 1

    print(f"\n  По направлению:")
    for d, v in sorted(by_dir.items()):
        t  = v["win"] + v["loss"]
        wr_d = round(v["win"] / t * 100, 1) if t > 0 else 0
        print(f"    {d:6} | {t:3} сигналов | WIN {wr_d}%")

    # По confidence
    by_conf = defaultdict(lambda: {"win": 0, "loss": 0})
    for s in resolved:
        c = s.get("confidence", 0)
        if c < 60:   band = "55-59%"
        elif c < 65: band = "60-64%"
        elif c < 70: band = "65-69%"
        elif c < 75: band = "70-74%"
        else:        band = "75%+"
        by_conf[band][s["result"].lower()] += 1

    print(f"\n  По уверенности (confidence):")
    for band in ["55-59%", "60-64%", "65-69%", "70-74%", "75%+"]:
        v = by_conf.get(band)
        if not v: continue
        t    = v["win"] + v["loss"]
        wr_c = round(v["win"] / t * 100, 1) if t > 0 else 0
        print(f"    {band} | {t:3} сигналов | WIN {wr_c}%")

    print(f"{'='*55}\n")


# ============================================================
# Получить статистику для WebSocket (отправка в терминал)
# ============================================================

def get_stats_for_ws():
    """Возвращает краткую статистику для отправки в браузер."""
    resolved = [s for s in _signal_log if s.get("resolved") and s["result"] != "NEUTRAL"]
    if not resolved:
        return None

    total = len(resolved)
    wins  = sum(1 for s in resolved if s["result"] == "WIN")
    wr    = round(wins / total * 100, 1)

    return {
        "type":    "tracker_stats",
        "total":   total,
        "wins":    wins,
        "losses":  total - wins,
        "winrate": wr,
        "active":  len(_active_signals)
    }
