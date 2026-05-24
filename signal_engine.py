"""
signal_engine.py

Основан на реальной статистике из analyze_memory (5780 паттернов Dukascopy):

РАБОТАЕТ:
  - Часовой bias (02/07 UTC → UP, 11/12/13/21 UTC → DOWN)
  - near_high + slow velocity → DOWN 63.9% (сильный edge)
  - near_high + fast velocity → UP 53.1% (слабый edge)
  - Позиция у края диапазона
  - AUD/USD и EUR/USD имеют DOWN перевес ~59%

НЕ РАБОТАЕТ (убрано):
  - HTF тренд (trend_up 50.5% vs trend_down 49.9% — шум)
  - near_low + velocity (нет edge)
  - pressure (слабый фактор)

MATCHING ПАТТЕРНОВ:
  - Основной поиск: символ + сессия + зона + час±1 + velocity_bin + micro_trend
  - Fallback (если < MIN_MEMORY_SAMPLES): убираем micro_trend
  - Fallback-2 (если всё ещё мало): убираем velocity_bin
  Так сохраняем edge при достаточном числе паттернов, но не теряем сигнал
  при малой базе.

ФИКС:
  - ts всегда приводится к float корректно (FXCM даёт float, Dukascopy теперь тоже)
"""

import json
import os
from dataclasses import dataclass
from typing import Optional, List
from collections import defaultdict


# ============================================================
# Результат
# ============================================================

@dataclass
class SignalResult:
    direction:   str
    confidence:  int              # score-based уверенность 55-92%
    winrate:     Optional[float]  # реальный винрейт из памяти (0..1) или None
    samples:     int              # сколько похожих паттернов в памяти
    reason:      List[str]
    skip_reason: Optional[str] = None


# ============================================================
# Константы
# ============================================================

PIP_SIZE = {
    "EUR/USD": 0.0001,
    "USD/CAD": 0.0001,
    "AUD/USD": 0.0001,
    "USD/JPY": 0.01
}

MIN_SCORE = 3
MAX_SCORE = 9

# Часовой bias из analyze_memory (Dukascopy 5780 паттернов)
HOUR_BIAS = {
    2:  ("UP",   2),   # UP 62.8%  — сильный
    4:  ("UP",   1),   # UP 59.0%  — средний
    7:  ("UP",   2),   # UP 60.6%  — сильный
    20: ("UP",   1),   # UP 56.2%  — слабый
    11: ("DOWN", 1),   # DOWN 56.9% — средний
    12: ("DOWN", 1),   # DOWN 57.1% — средний
    13: ("DOWN", 2),   # DOWN 58.2% — сильный
    14: ("DOWN", 1),   # DOWN 56.0% — слабый
    21: ("DOWN", 2),   # DOWN 60.0% — сильный
}

# Пары с историческим DOWN перевесом ~59%
DOWN_BIAS_SYMBOLS = {"AUD/USD", "EUR/USD"}

# Минимум паттернов для показа реального винрейта
MIN_MEMORY_SAMPLES = 15

# Файл памяти
MEMORY_FILE = "market_memory.json"


# ============================================================
# Загрузка памяти (кешируется, перезагружается при изменении файла)
# ============================================================

_memory_cache = None
_memory_mtime = 0


def load_memory():
    global _memory_cache, _memory_mtime

    if not os.path.exists(MEMORY_FILE):
        return []

    mtime = os.path.getmtime(MEMORY_FILE)

    if _memory_cache is None or mtime > _memory_mtime:
        try:
            with open(MEMORY_FILE, "r") as f:
                data = json.load(f)
            _memory_cache = [p for p in data if p.get("resolved") and p.get("result")]
            _memory_mtime = mtime
        except Exception:
            _memory_cache = []

    return _memory_cache


# ============================================================
# Вспомогательные функции
# Принимают ts как float или int — оба работают корректно
# ============================================================

def get_session(ts) -> str:
    """Определяет торговую сессию по UTC timestamp (float или int)."""
    hour = int(float(ts) % 86400) // 3600
    if 8 <= hour < 13:
        return "london"
    elif 13 <= hour < 21:
        return "ny"
    elif 0 <= hour < 8:
        return "asia"
    else:
        return "asia_late"


def get_hour_utc(ts) -> int:
    """Возвращает час UTC (0-23). Всегда int."""
    return int(int(float(ts) % 86400) // 3600)


def minutes_in_hour(ts) -> int:
    """Минуты от начала текущего часа (0-59). Всегда int."""
    return int(int(float(ts) % 3600) // 60)


def minutes_to_next_hour(ts) -> int:
    """Минут до смены часа (1-60). Всегда int."""
    return 60 - minutes_in_hour(ts)


# ============================================================
# Velocity bin
# ============================================================

def velocity_bin(v) -> str:
    av = abs(v)
    if av < 0.00002:
        return "flat"
    elif av < 0.00005:
        return "slow"
    elif av < 0.00015:
        return "medium"
    else:
        return "fast"


# ============================================================
# Поиск похожих паттернов в памяти
# ============================================================

def _count_matches(memory, symbol, session, hour, zone, vbin=None, micro=None):
    """
    Внутренняя функция подсчёта UP/DOWN по набору фильтров.
    vbin и micro — опциональные фильтры.
    """
    up = 0
    down = 0

    for p in memory:
        s = p.get("state", {})

        if p.get("symbol") != symbol:
            continue

        # Зона
        p_near_high = s.get("near_high", False)
        p_zone = "near_high" if p_near_high else "near_low"
        if p_zone != zone:
            continue

        # Сессия
        p_ts = p.get("time", 0)
        if get_session(p_ts) != session:
            continue

        # Час ±1
        if abs(get_hour_utc(p_ts) - hour) > 1:
            continue

        # Velocity bin (опционально)
        if vbin is not None:
            if velocity_bin(s.get("velocity", 0)) != vbin:
                continue

        # Micro trend (опционально)
        if micro is not None:
            if s.get("micro_trend", "flat") != micro:
                continue

        if p["result"] == "up":
            up += 1
        else:
            down += 1

    return up, down


def get_memory_winrate(symbol, ts, near_high, near_low, htf,
                       current_velocity=None, current_micro=None):
    """
    Ищет похожие паттерны в памяти. Возвращает (winrate, samples).

    Стратегия matching (от строгого к мягкому):
      1. символ + сессия + зона + час±1 + velocity_bin + micro_trend
      2. символ + сессия + зона + час±1 + velocity_bin  (без micro)
      3. символ + сессия + зона + час±1                 (базовый)

    Каждый уровень используется только если предыдущий дал < MIN_MEMORY_SAMPLES.
    Это сохраняет edge при богатой базе и не теряет сигнал при малой.
    """
    memory = load_memory()
    if not memory:
        return None, 0

    session = get_session(ts)
    hour    = get_hour_utc(ts)
    zone    = "near_high" if near_high else "near_low"

    vbin  = velocity_bin(current_velocity) if current_velocity is not None else None
    micro = current_micro  # "up" / "down" / "flat" / None

    # --- Уровень 1: полный matching ---
    if vbin is not None and micro is not None:
        up, down = _count_matches(memory, symbol, session, hour, zone, vbin, micro)
        total = up + down
        if total >= MIN_MEMORY_SAMPLES:
            return up / total, total

    # --- Уровень 2: без micro_trend ---
    if vbin is not None:
        up, down = _count_matches(memory, symbol, session, hour, zone, vbin, None)
        total = up + down
        if total >= MIN_MEMORY_SAMPLES:
            return up / total, total

    # --- Уровень 3: базовый (символ + сессия + зона + час) ---
    up, down = _count_matches(memory, symbol, session, hour, zone, None, None)
    total = up + down

    if total < MIN_MEMORY_SAMPLES:
        return None, total

    return up / total, total


# ============================================================
# Главная функция
# ============================================================

def evaluate_signal(
    symbol:    str,
    price:     float,
    ts,                # float или int — оба поддерживаются
    analysis:  dict,
    structure: dict
) -> Optional[SignalResult]:

    if not analysis or not structure:
        return None

    near_high = structure.get("near_high", False)
    near_low  = structure.get("near_low",  False)

    if not near_high and not near_low:
        return None

    hour    = get_hour_utc(ts)
    session = get_session(ts)
    min_in  = minutes_in_hour(ts)
    min_out = minutes_to_next_hour(ts)

    range_pos  = analysis.get("range_pos",  0.5)
    pressure   = analysis.get("pressure",   0.5)
    micro      = analysis.get("micro_trend", "flat")
    volatility = analysis.get("volatility", "normal")
    vol_ratio  = analysis.get("vol_ratio",  1.0)
    velocity   = analysis.get("velocity",   0.0)
    vbin       = velocity_bin(velocity)

    reason = []

    # ----------------------------------------------------------
    # БЛОК 1 — ЖЁСТКИЕ ФИЛЬТРЫ
    # ----------------------------------------------------------

    # ±3 минуты от смены часа — выплата 60%
    if min_in <= 3:
        return SignalResult(
            direction="SKIP", confidence=0, winrate=None, samples=0, reason=[],
            skip_reason=f"⛔ Начало часа ({min_in} мин) — выплата 60%"
        )

    if min_out <= 3:
        return SignalResult(
            direction="SKIP", confidence=0, winrate=None, samples=0, reason=[],
            skip_reason=f"⛔ Конец часа ({min_out} мин) — выплата 60%"
        )

    # Рынок стоит
    if volatility == "low":
        return SignalResult(
            direction="SKIP", confidence=0, winrate=None, samples=0, reason=[],
            skip_reason="⛔ Волатильность низкая — рынок не движется"
        )

    # Аномальный скачок — вероятно новость
    if vol_ratio > 3.0:
        return SignalResult(
            direction="SKIP", confidence=0, winrate=None, samples=0, reason=[],
            skip_reason=f"⛔ Аномальная волатильность x{vol_ratio:.1f} — возможна новость"
        )

    # ----------------------------------------------------------
    # БЛОК 2 — НАКОПЛЕНИЕ ФАКТОРОВ
    # ----------------------------------------------------------

    score_up   = 0
    score_down = 0

    # --- Фактор 1: позиция у края ---
    if near_high:
        score_down += 1
        reason.append(f"📍 Верхняя граница (pos={range_pos:.2f})")
    if near_low:
        score_up += 1
        reason.append(f"📍 Нижняя граница (pos={range_pos:.2f})")

    # --- Фактор 2: velocity edge (только для near_high) ---
    # near_high + slow → DOWN 63.9% (Δ-13.4 — сильный)
    # near_high + fast → UP  53.1% (Δ+3.6  — слабый)
    if near_high:
        if vbin == "slow":
            score_down += 3
            reason.append(f"🐢 Медленный подход к сопротивлению [{vbin}] → DOWN 63.9% исторически")
        elif vbin == "fast":
            score_up += 1
            reason.append(f"⚡ Быстрый импульс к сопротивлению [{vbin}] → возможно продолжение UP")
        elif vbin == "flat":
            score_down += 1
            reason.append(f"😴 Цена стоит у сопротивления [{vbin}] → вероятен разворот")
        else:  # medium
            reason.append(f"➡️ Средняя скорость у сопротивления [{vbin}]")

    # --- Фактор 3: часовой bias ---
    if hour in HOUR_BIAS:
        bias_dir, bias_weight = HOUR_BIAS[hour]
        if bias_dir == "UP":
            score_up += bias_weight
            strength = "сильный" if bias_weight >= 2 else "слабый"
            reason.append(f"🕐 {hour:02d}:00 UTC — исторический UP {strength}")
        else:
            score_down += bias_weight
            strength = "сильный" if bias_weight >= 2 else "слабый"
            reason.append(f"🕐 {hour:02d}:00 UTC — исторический DOWN {strength}")

    # --- Фактор 4: символьный перевес ---
    if symbol in DOWN_BIAS_SYMBOLS:
        score_down += 1
        reason.append(f"📊 {symbol} — исторический DOWN перевес 59%")

    # --- Фактор 5: pressure контрарный (слабый) ---
    if pressure > 0.53:
        score_down += 1
        reason.append(f"⚡ Давление покупок ({pressure:.2f}) → контрарно DOWN")
    elif pressure < 0.47:
        score_up += 1
        reason.append(f"⚡ Давление продаж ({pressure:.2f}) → контрарно UP")

    # --- Фактор 6: микротренд у границы ---
    if micro == "up" and near_high:
        score_down += 1
        reason.append("🔺 Микроимпульс вверх у сопротивления → истощение")
    elif micro == "down" and near_low:
        score_up += 1
        reason.append("🔻 Микроимпульс вниз у поддержки → истощение")

    # --- Фактор 7: Лондон усиливает лидирующий ---
    if session == "london":
        if score_down > score_up:
            score_down += 1
            reason.append("🏙️ Лондон — усиление DOWN")
        elif score_up > score_down:
            score_up += 1
            reason.append("🏙️ Лондон — усиление UP")

    # ----------------------------------------------------------
    # БЛОК 3 — РЕАЛЬНЫЙ ВИНРЕЙТ ИЗ ПАМЯТИ
    # Передаём velocity и micro для точного matching
    # ----------------------------------------------------------

    memory_wr, memory_n = get_memory_winrate(
        symbol, ts, near_high, near_low,
        analysis.get("htf", "range"),
        current_velocity=velocity,
        current_micro=micro
    )

    if memory_wr is not None and memory_n >= MIN_MEMORY_SAMPLES:
        memory_direction = "UP" if memory_wr > 0.5 else "DOWN"
        memory_edge      = abs(memory_wr - 0.5)

        if memory_edge > 0.1:   # >10% от нейтрали — сильный сигнал
            if memory_direction == "UP":
                score_up += 2
                reason.append(
                    f"🧠 Память [{memory_n} пат., vbin={vbin}, micro={micro}]: "
                    f"UP {round(memory_wr * 100, 1)}%"
                )
            else:
                score_down += 2
                reason.append(
                    f"🧠 Память [{memory_n} пат., vbin={vbin}, micro={micro}]: "
                    f"DOWN {round((1 - memory_wr) * 100, 1)}%"
                )
        elif memory_edge > 0.05:  # 5-10% — слабый сигнал
            if memory_direction == "UP":
                score_up += 1
                reason.append(
                    f"🧠 Память [{memory_n} пат., vbin={vbin}]: "
                    f"UP {round(memory_wr * 100, 1)}% (слабый)"
                )
            else:
                score_down += 1
                reason.append(
                    f"🧠 Память [{memory_n} пат., vbin={vbin}]: "
                    f"DOWN {round((1 - memory_wr) * 100, 1)}% (слабый)"
                )

    # ----------------------------------------------------------
    # БЛОК 4 — РЕШЕНИЕ
    # ----------------------------------------------------------

    if score_up > score_down:
        direction     = "UP"
        winning_score = score_up
    elif score_down > score_up:
        direction     = "DOWN"
        winning_score = score_down
    else:
        return SignalResult(
            direction="SKIP", confidence=0, winrate=memory_wr,
            samples=memory_n, reason=reason,
            skip_reason=f"⚠️ Равный счёт UP={score_up} DOWN={score_down}"
        )

    if winning_score < MIN_SCORE:
        return SignalResult(
            direction="SKIP", confidence=0, winrate=memory_wr,
            samples=memory_n, reason=reason,
            skip_reason=f"⚠️ Мало подтверждений (score={winning_score}, нужно ≥{MIN_SCORE})"
        )

    confidence = int(55 + (winning_score - MIN_SCORE) / (MAX_SCORE - MIN_SCORE) * 37)
    confidence = max(55, min(confidence, 92))

    return SignalResult(
        direction=direction,
        confidence=confidence,
        winrate=memory_wr,
        samples=memory_n,
        reason=reason
    )


# ============================================================
# Форматирование для WebSocket
# ============================================================

def format_signal(symbol: str, price: float, ts, result: SignalResult) -> dict:

    hour    = get_hour_utc(ts)
    min_in  = minutes_in_hour(ts)
    session = get_session(ts)

    if result.skip_reason:
        return {
            "type":        "signal",
            "symbol":      symbol,
            "price":       round(price, 5),
            "direction":   "SKIP",
            "confidence":  0,
            "winrate":     None,
            "samples":     0,
            "skip_reason": result.skip_reason,
            "hour_utc":    hour,
            "min_in_hour": min_in,
            "session":     session
        }

    display_pct = None
    if result.winrate is not None and result.samples >= MIN_MEMORY_SAMPLES:
        wr = result.winrate if result.direction == "UP" else (1 - result.winrate)
        display_pct = round(wr * 100, 1)
    else:
        display_pct = result.confidence

    return {
        "type":        "signal",
        "symbol":      symbol,
        "price":       round(price, 5),
        "direction":   result.direction,
        "confidence":  result.confidence,
        "winrate":     round(result.winrate * 100, 1) if result.winrate is not None else None,
        "display_pct": display_pct,
        "samples":     result.samples,
        "reason":      result.reason,
        "hour_utc":    hour,
        "min_in_hour": min_in,
        "session":     session
    }

