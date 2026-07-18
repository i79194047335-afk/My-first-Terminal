"""
Техническая картина для брифинга — из market.db (свечи).

Слой 3 подпроекта briefing/. Перенос get_technical_context() из
pre_session_brief.py БЕЗ изменения логики: определения htf_trend / range_pos /
volatility повторяют market_engine.py (окно 30 свечей M5), near_high/near_low —
structure_engine.py (порог 15%), micro_trend — направление 5 закрытий M1. Всё
считается по свечам, а не по тикам (сигнальный контур выключен, Фаза 2.1).

Читатель: открывает market.db строго read-only (mode=ro), в боевую БД не пишет.
"""

import os
import sqlite3
from datetime import datetime, timezone

_HERE       = os.path.dirname(os.path.abspath(__file__))
_ROOT       = os.path.dirname(_HERE)
DB_FILE     = os.path.join(_ROOT, "market.db")
DB_PROVIDER = "fxcm"

SYMBOLS = ["EUR/USD", "USD/JPY", "AUD/USD", "USD/CAD"]


def _read_candles(conn, symbol, tf, limit):
    """Прочитать последние `limit` свечей пары из market.db, старые первыми.

    Args:
        conn:   Read-only соединение с market.db.
        symbol: Пара ("EUR/USD").
        tf:     Таймфрейм ("M1"/"M5").
        limit:  Сколько последних свечей вернуть.

    Returns:
        Список dict {time, open, high, low, close} по возрастанию времени; []
        если данных нет.
    """
    rows = conn.execute(
        """SELECT time, o, h, l, c FROM candles
           WHERE provider=? AND symbol=? AND tf=?
           ORDER BY time DESC LIMIT ?""",
        (DB_PROVIDER, symbol, tf, limit),
    ).fetchall()
    rows.reverse()
    return [{"time": r[0], "open": r[1], "high": r[2],
             "low": r[3], "close": r[4]} for r in rows]


def get_technical_context():
    """Технический контекст для всех SYMBOLS из market.db.

    Returns:
        {"symbols": {"EUR/USD": {price, htf_trend, day_high, …}, …}}.
        Пары без данных в БД в словаре отсутствуют.
    """
    ctx = {"symbols": {}}
    if not os.path.exists(DB_FILE):
        return ctx
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % DB_FILE, uri=True)
    except Exception:
        return ctx

    try:
        for sym in SYMBOLS:
            m5 = _read_candles(conn, sym, "M5", 30)
            m1 = _read_candles(conn, sym, "M1", 1440)   # сутки
            if not m1 or len(m5) < 2:
                continue

            pip_div = 0.01 if "JPY" in sym else 0.0001
            price = m1[-1]["close"]

            # Дневной диапазон: последние 24ч по M1.
            day_high = max(c["high"] for c in m1)
            day_low = min(c["low"] for c in m1)
            day_close = price

            # HTF-тренд и позиция в диапазоне: 30 свечей M5.
            closes = [c["close"] for c in m5]
            if closes[-1] > closes[0]:
                htf = "trend_up"
            elif closes[-1] < closes[0]:
                htf = "trend_down"
            else:
                htf = "range"

            hi, lo = max(closes), min(closes)
            if hi != lo:
                range_pos = max(0.0, min(1.0, (price - lo) / (hi - lo)))
            else:
                range_pos = 0.5

            near_high = range_pos > 0.85
            near_low = range_pos < 0.15

            # Волатильность: размах последней M5 против средней.
            ranges = [c["high"] - c["low"] for c in m5[-20:]]
            if len(ranges) >= 2:
                avg_range = sum(ranges[:-1]) / (len(ranges) - 1)
                vol_ratio = ranges[-1] / avg_range if avg_range > 0 else 1.0
                if vol_ratio > 1.5:
                    volatility = "high"
                elif vol_ratio < 0.6:
                    volatility = "low"
                else:
                    volatility = "normal"
            else:
                volatility = "normal"

            # Micro-trend: направление последних 5 закрытий M1.
            tail = [c["close"] for c in m1[-6:]]
            if len(tail) >= 2:
                ups = sum(1 for a, b in zip(tail, tail[1:]) if b > a)
                downs = sum(1 for a, b in zip(tail, tail[1:]) if b < a)
                micro_trend = ("up" if ups > downs + 1
                               else "down" if downs > ups + 1
                               else "flat")
            else:
                micro_trend = "flat"

            # Диапазон текущей сессии: бары с начала суток UTC.
            now = datetime.now(timezone.utc)
            sess_start = now.replace(hour=0, minute=0, second=0,
                                     microsecond=0).timestamp()
            sess_bars = [c for c in m1 if c["time"] >= sess_start]
            if sess_bars:
                session_high = max(c["high"] for c in sess_bars)
                session_low = min(c["low"] for c in sess_bars)
                session_range = session_high - session_low
            else:
                session_high = session_low = session_range = 0

            last = m1[-1]
            velocity = (last["close"] - last["open"]) / 60.0

            ctx["symbols"][sym] = {
                "price": price,
                "htf_trend": htf,
                "day_high": day_high,
                "day_low": day_low,
                "day_close": day_close,
                "day_range_pips": abs(day_high - day_low) / pip_div,
                "session_high": session_high,
                "session_low": session_low,
                "session_range_pips": abs(session_range) / pip_div,
                "range_pos": round(range_pos, 3),
                "near_high": near_high,
                "near_low": near_low,
                "velocity": velocity,
                "micro_trend": micro_trend,
                "volatility": volatility,
            }
    finally:
        conn.close()

    return ctx
