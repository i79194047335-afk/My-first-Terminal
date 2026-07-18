"""
Память брифинга: структурированный журнал прогнозов + самооценка.

Слой 2 подпроекта briefing/ (см. ROADMAP, Фаза 7). Заменяет плоский
briefing_context.json (5 строк на пару) на журнал с датами и полями прогноза,
и — главное — СВЕРЯЕТ прошлый прогноз с фактом из market.db.

Зачем самооценка: без неё брифинг страдает амнезией каждые 8 часов — генерит
новый прогноз, не зная, сбылся ли прошлый. С ней цикл замыкается: новый брифинг
сначала честно оценивает предыдущий (цена пошла в предсказанную сторону? дошла
до названных уровней?), и только потом строит новый. Это даёт: честность вместо
самоуверенности, связный контекст, измеримый трек-рекорд.

Факт берётся только из market.db (наша БД) — никаких внешних данных.

Формат журнала (data/briefing_journal.json):
    {"pairs": {"EUR/USD": [ {запись}, … ]}, "updated_ts": …}
    запись: {ts, session, direction, confidence, price_at,
             support:[...], resistance:[...]}
Храним до JOURNAL_DEPTH записей на пару (глубина 7-14 дней при 3 сессиях/сутки).
"""

import json
import os
import sqlite3

_HERE   = os.path.dirname(os.path.abspath(__file__))
_ROOT   = os.path.dirname(_HERE)
JOURNAL_FILE = os.path.join(_ROOT, "data", "briefing_journal.json")
DB_FILE      = os.path.join(_ROOT, "market.db")
DB_PROVIDER  = "fxcm"

JOURNAL_DEPTH = 30      # ~10 дней при 3 сессиях/сутки

# Насколько цена должна пройти в сторону прогноза, чтобы счесть его сбывшимся.
# Ниже — «нейтрально» (рынок топтался, направление не подтвердилось и не опровергнуто).
HIT_PIPS = 5


def _pip_size(symbol):
    """Размер пипса пары (0.0001, у JPY 0.01)."""
    return 0.01 if symbol.endswith("JPY") else 0.0001


# ── журнал ──────────────────────────────────────────────────────────────

def load_journal():
    """Загрузить журнал прогнозов.

    Returns:
        Dict {"pairs": {symbol: [записи]}, ...}; пустой каркас, если файла нет
        или он битый (память не должна ронять брифинг).
    """
    if not os.path.exists(JOURNAL_FILE):
        return {"pairs": {}}
    try:
        with open(JOURNAL_FILE, encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("pairs", {})
        return data
    except Exception:
        return {"pairs": {}}


def record_briefing(briefing):
    """Дописать прогнозы текущего брифинга в журнал.

    Сохраняем ровно то, что потом можно СВЕРИТЬ с фактом: направление,
    уверенность, цену на момент прогноза и названные уровни. reasoning/watch_for
    в журнал не тянем — это для человека, а не для арифметической сверки.

    Args:
        briefing: Готовый dict брифинга (meta + pairs).

    Returns:
        None. Пишет JOURNAL_FILE (создаёт data/ при нужде).
    """
    journal = load_journal()
    pairs   = journal["pairs"]
    meta    = briefing.get("meta", {})
    ts      = meta.get("generated_ts")
    session = meta.get("session", "?")

    for sym, pair in briefing.get("pairs", {}).items():
        entry = {
            "ts":         ts,
            "session":    session,
            "direction":  pair.get("direction", "?"),
            "confidence": pair.get("direction_confidence", "?"),
            "price_at":   _price_from_summary(pair.get("technical_summary", "")),
            "support":    pair.get("support_levels", []),
            "resistance": pair.get("resistance_levels", []),
        }
        pairs.setdefault(sym, []).append(entry)
        if len(pairs[sym]) > JOURNAL_DEPTH:
            pairs[sym] = pairs[sym][-JOURNAL_DEPTH:]

    journal["updated_ts"] = ts
    os.makedirs(os.path.dirname(JOURNAL_FILE), exist_ok=True)
    tmp = JOURNAL_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(journal, f, ensure_ascii=False, indent=2)
    os.replace(tmp, JOURNAL_FILE)


def _price_from_summary(summary):
    """Вытащить цену из technical_summary («Цена 1.14379, …»).

    Цену кладёт в текст наш же промпт, поэтому парсинг предсказуем. Возвращает
    None, если не нашли — самооценка тогда пропустит уровневую часть.

    Args:
        summary: Строка technical_summary.

    Returns:
        Float цены или None.
    """
    import re
    m = re.search(r"[Цц]ена\s+([0-9]+\.[0-9]+)", summary or "")
    return float(m.group(1)) if m else None


# ── самооценка ──────────────────────────────────────────────────────────

def assess_previous(symbol, now_ts):
    """Сверить последний прогноз по паре с фактом из market.db.

    Args:
        symbol: Пара ("EUR/USD").
        now_ts: Текущее время (unix-секунды) — верхняя граница факта.

    Returns:
        Dict оценки или None (нет прошлого прогноза / нет данных):
          {session, direction, confidence, price_at, price_now, moved_pips,
           verdict, level_note}
          verdict: "сбылось" | "не сбылось" | "нейтрально".
    """
    journal = load_journal()
    hist = journal.get("pairs", {}).get(symbol, [])
    if not hist:
        return None
    prev = hist[-1]

    price_at = prev.get("price_at")
    t0 = prev.get("ts")
    if price_at is None or t0 is None:
        return None

    fact = _fact_since(symbol, int(t0), int(now_ts))
    if not fact:
        return None
    price_now, hi, lo = fact

    pip = _pip_size(symbol)
    moved = (price_now - price_at) / pip          # знак = направление
    direction = prev.get("direction", "?")

    # Вердикт по направлению.
    if direction == "UP" and moved >= HIT_PIPS:
        verdict = "сбылось"
    elif direction == "DOWN" and moved <= -HIT_PIPS:
        verdict = "сбылось"
    elif abs(moved) < HIT_PIPS:
        verdict = "нейтрально"
    else:
        verdict = "не сбылось"

    return {
        "session":    prev.get("session"),
        "direction":  direction,
        "confidence": prev.get("confidence"),
        "price_at":   price_at,
        "price_now":  price_now,
        "moved_pips": round(moved, 1),
        "verdict":    verdict,
        "level_note": _level_note(symbol, prev, hi, lo, pip),
    }


def _fact_since(symbol, t0, t1):
    """Фактическое движение цены пары за [t0; t1] из market.db.

    Args:
        symbol: Пара.
        t0:     Начало окна (unix-секунды).
        t1:     Конец окна.

    Returns:
        Tuple (price_now, max_high, min_low) или None, если данных нет.
        price_now — close последней свечи в окне.
    """
    if not os.path.exists(DB_FILE):
        return None
    conn = sqlite3.connect("file:%s?mode=ro" % DB_FILE, uri=True)
    try:
        row = conn.execute(
            """SELECT c FROM candles WHERE provider=? AND symbol=? AND tf='M1'
               AND time<=? ORDER BY time DESC LIMIT 1""",
            (DB_PROVIDER, symbol, t1)).fetchone()
        if not row:
            return None
        price_now = row[0]
        hl = conn.execute(
            """SELECT MAX(h), MIN(l) FROM candles WHERE provider=? AND symbol=?
               AND tf='M1' AND time>=? AND time<=?""",
            (DB_PROVIDER, symbol, t0, t1)).fetchone()
    finally:
        conn.close()
    if hl is None or hl[0] is None:
        return None
    return price_now, hl[0], hl[1]


def _level_note(symbol, prev, hi, lo, pip):
    """Короткая заметка: тестировались ли названные уровни за период.

    Args:
        symbol: Пара.
        prev:   Прошлая запись журнала (с support/resistance).
        hi, lo: Max high / min low факта за период.
        pip:    Размер пипса.

    Returns:
        Строка-заметка ("" если уровней не было).
    """
    near = pip * 3   # «дошла до уровня» = в пределах 3 пипсов
    notes = []
    for r in (prev.get("resistance") or [])[:2]:
        if hi >= r - near:
            broke = "пробито" if hi > r + near else "тест"
            notes.append("R %.5f %s" % (r, broke))
    for s in (prev.get("support") or [])[:2]:
        if lo <= s + near:
            broke = "пробито" if lo < s - near else "тест"
            notes.append("S %.5f %s" % (s, broke))
    return "; ".join(notes)


def format_assessment_for_prompt(symbol, now_ts):
    """Человекочитаемый блок самооценки для промпта DeepSeek.

    Args:
        symbol: Пара.
        now_ts: Текущее время (unix-секунды).

    Returns:
        Строка блока или "" (нет прошлого прогноза — первая сессия).
    """
    a = assess_previous(symbol, now_ts)
    if not a:
        return ""
    line = ("[Проверка прошлого прогноза (%s, conf=%s): предсказано %s, "
            "цена %s → %s (%+.1f пипс) — %s"
            % (a["session"], a["confidence"], a["direction"],
               a["price_at"], a["price_now"], a["moved_pips"], a["verdict"]))
    if a["level_note"]:
        line += "; уровни: %s" % a["level_note"]
    return line + "]"
