"""
signal_engine.py  (parallel index.html — переработанная версия)

Веса пересмотрены после реплея 11677 живых сигналов из signal_log.json.
Каждый фактор измерен по dir-hit (как часто цена реально шла в его сторону):

ОСТАВЛЕНО (реальный edge на live):
  - pressure контрарный (F5)        → dir-hit 55-56%  (+5..+6)
  - near_low → UP (F1)              → dir-hit 55.6%   (+5.6)
  - flat у сопротивления → DOWN (F2)→ dir-hit 54.9%   (+4.9)
  - микротренд истощение (F6)       → dir-hit 54-55%  (+4..+5)
  - near_high → DOWN (F1)           → dir-hit 53.7%   (+3.7)

УБРАНО / ПОНИЖЕНО (на live edge отсутствует, было переобучение Dukascopy):
  - Часовой bias (F3)               → UP-часы 49.2%, DOWN-часы 50.9% → УДАЛЁН
  - near_high + fast → UP (F2)      → dir-hit 48.6% (отриц.)         → УДАЛЁН
  - near_high + slow → DOWN (F2)    → 155 паттернов, 51.6%   → вес 3→1
  - символьный DOWN-перевес (F4)    → dir-hit 51.5%          → УДАЛЁН

РЕДИЗАЙН 2026-06-12 (OOS-анализ 11928 реализованных сделок, ничья=возврат):
  1. ПАМЯТЬ УБРАНА ИЗ ОЧКОВ. Оказалась некалиброванной (обещала 91% → факт
     54.5%; 82% → 49%) и при равной структурной силе ухудшала результат
     (sscore=5: с памятью 57% vs без 67%). Раньше давала +2 и гейтила 99%
     сделок → поток уходил в EV-отрицательную зону. Теперь только справочно.
  2. АСИММЕТРИЧНЫЕ ПОРОГИ. UP реализ. 57.5% (прибыльнее) → порог ≥4.
     DOWN реализ. 52.0% → порог ≥5. EV (payout 85%): UP≥4 +8%, DOWN≥5 +7%.
  3. ФАКТОР СКОРОСТИ ДЛЯ UP. Раньше velocity edge был только у near_high,
     из-за чего UP не дотягивал до порога. Добавлен зеркальный near_low.
  Breakeven при ничья=возврат: 54% (payout 85%), 52.6% (90%).

ВАЖНО: edge затухает — мониторить signal_stats.json на свежих данных,
перепроверять пороги ежемесячно.

MATCHING ПАТТЕРНОВ (используется только для справочного винрейта на экране):
  - символ + сессия + зона + час±1 + velocity_bin + micro_trend, с фолбэками.

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

# Пороги пересмотрены после OOS-анализа реализованных исходов (11928 сделок,
# 70/30 time-split). Память оказалась НЕкалиброванной (обещала 91% → факт 54%)
# и дилютивной — убрана из расчёта очков (см. Блок 3).
# По чистым структурным факторам реализованный винрейт (ничья=возврат, breakeven 54%):
#   UP   sscore>=4 → 58.5%  (EV+8% @85% payout)
#   DOWN sscore>=5 → 57.9%  (EV+7%);  DOWN<=4 → EV-отрицательный, отсечён
# Пороги асимметричны: UP — прибыльное направление, пускаем легче.
MIN_SCORE_UP   = 4
MIN_SCORE_DOWN = 5
MAX_SCORE = 5   # реальный максимум структурного счёта (F1+F2+F5+F6+F7)

# Часовой bias — УДАЛЁН.
# Реплей 11677 живых сигналов: UP-часы 49.2%, DOWN-часы 50.9% (live edge ~0).
# Паттерн из Dukascopy не переносится на live. Оставлен пустым → фактор не срабатывает.
HOUR_BIAS = {}

# Символьный DOWN-перевес — УДАЛЁН.
# Реплей: dir-hit 51.5% (edge +1.5, шум). Пустое множество → фактор не срабатывает.
DOWN_BIAS_SYMBOLS = set()

# Минимум паттернов для показа реального винрейта
MIN_MEMORY_SAMPLES = 15

# Фильтр новостей: не торговать в окне −15/+30 мин вокруг high-impact события
# по валюте пары. Календарь грузит data_loaders/fetch_news.py (cron).
NEWS_FILTER_ENABLED = True

# Фильтр Asia ∩ TIGHT (pos ≤ 0.05):
#   Форвард 18 дней (2026-06-15 – 2026-07-03, n=177): WR 63.8% CI[56.5–70.6].
#   Весь лог (n=1651): WR 60.1%. CSCV PBO=0.009, deflated z=3.52.
#   MID зона (0.05–0.15) — 52.4%, TIGHT вне Азии — 52.9% (breakeven).
#   Эдж живёт ТОЛЬКО в пересечении азиатской сессии с самой кромкой диапазона.
#   ~11 сигналов/день (против ~150 без фильтра).
#   Откат: поставить False → вернуться к старому поведению без рестарта.
ASIA_TIGHT_ONLY = True

# Файл памяти
MEMORY_FILE = "market_memory.json"

# Файл с сессионным bias от pre_asia_brief.py
SESSION_BIAS_FILE = "session_bias.json"

# Фильтр по сессионному bias: если True и bias задан — сигналы против bias
# отправляются в SKIP. NEUTRAL bias пропускает оба направления.
SESSION_BIAS_FILTER = True


# ============================================================
# Загрузка памяти (кешируется, перезагружается при изменении файла)
# ============================================================

_memory_cache = None
_memory_mtime = 0


def load_session_bias():
    """
    Загружает сессионный bias из pre_asia_brief.py.

    Returns:
        dict или None: {"usdjpy_bias": "UP"/"DOWN"/"NEUTRAL", ...}
        None если файл не найден, устарел (>12 часов), или bias NEUTRAL.
    """
    if not os.path.exists(SESSION_BIAS_FILE):
        return None

    try:
        with open(SESSION_BIAS_FILE, "r") as f:
            bias = json.load(f)
    except Exception:
        return None

    generated_ts = bias.get("generated_ts", 0)
    now = __import__("time").time()
    if now - generated_ts > 43200:  # 12 часов
        return None

    if bias.get("usdjpy_bias") == "NEUTRAL":
        return None

    return bias


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

    # High-impact новость по валюте пары в окне −15/+30 мин — не торгуем.
    # Календарь (data_loaders/news_calendar.csv) обновляется cron-скриптом
    # fetch_news.py. Ленивый импорт: pattern_memory уже загружен в server.py.
    if NEWS_FILTER_ENABLED:
        from pattern_memory import is_news_window
        if is_news_window(ts, symbol):
            return SignalResult(
                direction="SKIP", confidence=0, winrate=None, samples=0, reason=[],
                skip_reason="⛔ Высокоимпактная новость рядом (−15/+30 мин) — пропуск"
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

    # --- Фактор 2: velocity edge ---
    # Медленный/стоячий подход к краю → истощение импульса → разворот.
    # Раньше работал только для near_high, из-за чего UP не дотягивал до порога
    # (структурный потолок UP был 3). Теперь зеркально и для near_low,
    # чтобы прибыльное UP-направление (реализ. 57.5%) могло набрать порог.
    if near_high:
        if vbin == "slow":
            score_down += 1
            reason.append(f"🐢 Медленный подход к сопротивлению [{vbin}] → DOWN")
        elif vbin == "flat":
            score_down += 1
            reason.append(f"😴 Цена стоит у сопротивления [{vbin}] → вероятен разворот")
        elif vbin == "fast":
            reason.append(f"⚡ Быстрый импульс к сопротивлению [{vbin}] (нейтрально)")
        else:  # medium
            reason.append(f"➡️ Средняя скорость у сопротивления [{vbin}]")
    if near_low:
        if vbin == "slow":
            score_up += 1
            reason.append(f"🐢 Медленный подход к поддержке [{vbin}] → UP")
        elif vbin == "flat":
            score_up += 1
            reason.append(f"😴 Цена стоит у поддержки [{vbin}] → вероятен разворот")
        elif vbin == "fast":
            reason.append(f"⚡ Быстрый импульс к поддержке [{vbin}] (нейтрально)")
        else:  # medium
            reason.append(f"➡️ Средняя скорость у поддержки [{vbin}]")

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
    # БЛОК 3 — ПАМЯТЬ (ТОЛЬКО СПРАВОЧНО, В ОЧКИ НЕ ВХОДИТ)
    # ----------------------------------------------------------
    # OOS-проверка по реализованным исходам: память НЕкалибрована
    # (обещала 91% → факт 54.5%; 82% → 49%; 73% → 49%) и при равной
    # структурной силе сигналы С памятью проигрывали сигналам БЕЗ неё
    # (sscore=5: 57% vs 67%). Раньше давала +2 и гейтила 99% сделок,
    # затягивая поток в EV-отрицательную зону. Теперь считается только
    # для отображения на экране и НЕ влияет на score_up / score_down.

    memory_wr, memory_n = get_memory_winrate(
        symbol, ts, near_high, near_low,
        analysis.get("htf", "range"),
        current_velocity=velocity,
        current_micro=micro
    )

    if memory_wr is not None and memory_n >= MIN_MEMORY_SAMPLES:
        mem_dir = "UP" if memory_wr > 0.5 else "DOWN"
        mem_pct = round((memory_wr if mem_dir == "UP" else 1 - memory_wr) * 100, 1)
        reason.append(f"🧠 Память [{memory_n} пат.]: {mem_dir} {mem_pct}% (справочно)")

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

    # Асимметричный порог: UP прибыльнее (реализ. 57.5%) → пускаем при ≥4;
    # DOWN (52%) требует более сильного подтверждения → только ≥5.
    threshold = MIN_SCORE_UP if direction == "UP" else MIN_SCORE_DOWN
    if winning_score < threshold:
        return SignalResult(
            direction="SKIP", confidence=0, winrate=memory_wr,
            samples=memory_n, reason=reason,
            skip_reason=f"⚠️ Мало подтверждений (score={winning_score}, нужно ≥{threshold} для {direction})"
        )

    # ----------------------------------------------------------
    # БЛОК 4.5 — ФИЛЬТР Asia ∩ TIGHT (pos ≤ 0.05)
    # ----------------------------------------------------------
    # Пропускает только сигналы в азиатскую сессию (0-8 UTC) И у самой
    # кромки диапазона (≤5%). Всё остальное — SKIP.
    #
    # Логика: edge_dist = расстояние до своего края.
    #   UP   стоит у нижней границы → dist = range_pos
    #   DOWN стоит у верхней границы  → dist = 1 - range_pos
    # TIGHT: dist ≤ 0.05.  MID: 0.05 < dist ≤ 0.15.
    #
    # Без фильтра: MID 52.4% (n=7945), TIGHT вне Азии 52.9% — breakeven.
    # С фильтром: ~11 сигналов/день, WR 60.1% (лог) / 63.8% (форвард).
    if ASIA_TIGHT_ONLY:
        edge_dist = range_pos if near_low else (1.0 - range_pos)
        is_tight = edge_dist <= 0.05
        is_asia_session = (0 <= hour < 8)

        if not (is_tight and is_asia_session):
            zone_name = "TIGHT" if is_tight else ("MID" if edge_dist <= 0.15 else "OUT")
            time_name = "Asia" if is_asia_session else session
            return SignalResult(
                direction="SKIP", confidence=0, winrate=memory_wr,
                samples=memory_n, reason=reason,
                skip_reason=(
                    f"🔍 Фильтр Asia∩TIGHT: {time_name} ∩ {zone_name} "
                    f"(dist={edge_dist:.3f}) — вне целевой зоны"
                )
            )

    # ----------------------------------------------------------
    # БЛОК 4.6 — СЕССИОННЫЙ BIAS (pre_asia_brief.py)
    # ----------------------------------------------------------
    # Если перед сессией задан направленный bias на USD/JPY,
    # сигналы против bias уходят в SKIP.
    # NEUTRAL bias или отсутствие файла — пропускаем оба направления.
    if SESSION_BIAS_FILTER:
        session_bias = load_session_bias()
        if session_bias:
            bias_dir = session_bias.get("usdjpy_bias")
            bias_conf = session_bias.get("usdjpy_confidence", 0)

            if bias_dir == "UP" and direction == "DOWN":
                return SignalResult(
                    direction="SKIP", confidence=0, winrate=memory_wr,
                    samples=memory_n, reason=reason,
                    skip_reason=(
                        f"🧠 Session bias: USD/JPY UP (conf={bias_conf}) — "
                        f"DOWN сигнал заблокирован. {session_bias.get('usdjpy_reasoning', '')[:100]}"
                    )
                )
            elif bias_dir == "DOWN" and direction == "UP":
                return SignalResult(
                    direction="SKIP", confidence=0, winrate=memory_wr,
                    samples=memory_n, reason=reason,
                    skip_reason=(
                        f"🧠 Session bias: USD/JPY DOWN (conf={bias_conf}) — "
                        f"UP сигнал заблокирован. {session_bias.get('usdjpy_reasoning', '')[:100]}"
                    )
                )

    # Уверенность отражает реализованный винрейт (~57% на пороге, +4пп за очко),
    # а не раздутую память. Держим в честном диапазоне 57-75%.
    confidence = 57 + (winning_score - threshold) * 4
    confidence = max(55, min(confidence, 75))

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

    # Заголовок % = честная score-based уверенность.
    # Память НЕ используется как заголовок (она некалибрована) — она уходит
    # в поле winrate только для справочного отображения в панели.
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

