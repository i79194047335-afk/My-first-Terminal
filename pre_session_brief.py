"""
pre_session_brief.py — брифинг перед торговой сессией (Asia/London/NY).

Собирает макро-контекст (новости, календарь, технику) и генерирует:
  - briefing.json — полный per-pair брифинг для фронтенда
  - session_bias.json — legacy формат (USD/JPY) для signal_engine.py

Запуск: python3 pre_session_brief.py
Крон:
  0 23 * * * cd /root/projects/terminal && python3 pre_session_brief.py   # Asia
  0  7 * * * cd /root/projects/terminal && python3 pre_session_brief.py   # London
  0 12 * * * cd /root/projects/terminal && python3 pre_session_brief.py   # NY

Зависимости: feedparser, openai, python-dotenv
"""

import json
import os
import sys
import logging
import csv
import sqlite3
import time as time_module
from datetime import datetime, timezone
from typing import List, Dict, Optional, Tuple

import feedparser
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("pre_session")
log.setLevel(logging.INFO)
log.addHandler(logging.StreamHandler())

# ─────────────────────────────────────────────
# Конфигурация
# ─────────────────────────────────────────────

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
BRIEFING_FILE = "briefing.json"
LEGACY_FILE = "session_bias.json"
CONTEXT_FILE = "briefing_context.json"  # накопленный контекст между брифингами
NEWS_CALENDAR = "data_loaders/news_calendar.csv"

# Техническая картина берётся из SQLite (Фаза 1), а не из market_memory.json:
# тот был побочным продуктом сигнального контура, который выключен в Фазе 2.1.
# Свечи живут независимо от сигналов, так что брифинг больше от них не зависит.
_HERE = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(_HERE, "market.db")
DB_PROVIDER = "fxcm"

# Все 4 пары терминала
SYMBOLS = ["EUR/USD", "USD/JPY", "AUD/USD", "USD/CAD"]

# RSS-фиды — макро/валютные, без crypto/companies
RSS_FEEDS = [
    {"name": "Reuters Business", "url": "https://feeds.reuters.com/reuters/businessNews"},
    {"name": "Reuters Markets", "url": "https://feeds.reuters.com/reuters/markets"},
    {"name": "Reuters World", "url": "https://feeds.reuters.com/Reuters/worldNews"},
    {"name": "Bloomberg Markets", "url": "https://feeds.bloomberg.com/markets/news.rss"},
    {"name": "Bloomberg Economics", "url": "https://feeds.bloomberg.com/economics/news.rss"},
    {"name": "Bloomberg Politics", "url": "https://feeds.bloomberg.com/politics/news.rss"},
    {"name": "FT", "url": "https://www.ft.com/rss/home/uk"},
    {"name": "WSJ Markets", "url": "https://feeds.a.dj.com/rss/RSSMarketsMain.xml"},
    {"name": "CNBC Top News", "url": "https://www.cnbc.com/id/100003114/device/rss/rss.html"},
]

# Ключевые слова для фильтрации новостей (все валюты + общий макро)
FOREX_KEYWORDS = [
    # USD
    "USD", "dollar", "Federal Reserve", "Fed", "Treasury", "yield", "DXY",
    # JPY
    "JPY", "yen", "BOJ", "Bank of Japan", "Japan",
    # EUR
    "EUR", "euro", "ECB", "European Central Bank", "Eurozone", "Germany", "German",
    # AUD
    "AUD", "aussie", "RBA", "Reserve Bank of Australia", "Australia",
    # CAD
    "CAD", "loonie", "BOC", "Bank of Canada", "Canada",
    # Общий макро
    "forex", "currency", "FX", "carry trade",
    "risk appetite", "safe haven", "risk-off", "risk-on",
    "inflation", "CPI", "GDP", "PMI", "employment", "NFP", "payrolls",
]

# Привязка пары → ключевые слова для новостей
PAIR_KEYWORDS = {
    "EUR/USD": ["EUR", "euro", "ECB", "European Central Bank", "Eurozone",
                "USD", "dollar", "Federal Reserve", "Fed", "Treasury"],
    "USD/JPY": ["USD", "dollar", "Federal Reserve", "Fed", "Treasury",
                "JPY", "yen", "BOJ", "Bank of Japan", "Japan"],
    "AUD/USD": ["AUD", "aussie", "RBA", "Reserve Bank of Australia", "Australia",
                "USD", "dollar", "Federal Reserve", "Fed"],
    "USD/CAD": ["USD", "dollar", "Federal Reserve", "Fed",
                "CAD", "loonie", "BOC", "Bank of Canada", "Canada", "oil", "crude"],
}

# ─────────────────────────────────────────────
# Сессии
# ─────────────────────────────────────────────

SESSIONS = {
    "asia": {
        "label": "Asia",
        "label_ru": "Азиатская",
        "start_utc": "00:00",
        "end_utc": "08:00",
        "hours": (0, 8),
        "detect_hour_range": (22, 24, 0, 1),   # 22-01 UTC → пред-Asia
    },
    "london": {
        "label": "London",
        "label_ru": "Лондонская",
        "start_utc": "08:00",
        "end_utc": "17:00",
        "hours": (8, 17),
        "detect_hour_range": (6, 7, 8),         # 06-08 UTC → пред-London
    },
    "ny": {
        "label": "New York",
        "label_ru": "Нью-Йоркская",
        "start_utc": "13:00",
        "end_utc": "22:00",
        "hours": (13, 22),
        "detect_hour_range": (11, 12, 13),      # 11-13 UTC → пред-NY
    },
}


def detect_session():  # type: (...) -> Tuple[str, Dict]
    """
    Определяет ближайшую сессию по текущему UTC-часу.

    При запуске в cron-часы возвращает конкретную сессию.
    Вне cron-часов (ручной запуск) — определяет следующую сессию по времени.

    Returns:
        (session_key, session_config). Всегда возвращает сессию.
    """
    hour = datetime.now(timezone.utc).hour

    # Точное попадание в cron-час
    for key, cfg in SESSIONS.items():
        if hour in cfg["detect_hour_range"]:
            return key, cfg

    # Fallback: определяем ближайшую сессию по времени
    if 0 <= hour < 8:
        return "asia", SESSIONS["asia"]
    elif 8 <= hour < 13:
        return "london", SESSIONS["london"]
    else:
        return "ny", SESSIONS["ny"]


# ─────────────────────────────────────────────
# Накопленный контекст между брифингами
# ─────────────────────────────────────────────

def load_context():
    """Загружает накопленный контекст из briefing_context.json."""
    if not os.path.exists(CONTEXT_FILE):
        return {"observations": {}}
    try:
        with open(CONTEXT_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"observations": {}}


def save_context(briefing):
    """
    Сохраняет ключевые наблюдения из брифинга в briefing_context.json.
    Для каждой пары: сохраняем reasoning + watch_for как контекст для следующего брифинга.
    """
    ctx = load_context()
    obs = ctx.get("observations", {})

    pairs = briefing.get("pairs", {})
    for sym, pair in pairs.items():
        if sym not in obs:
            obs[sym] = []

        # Формируем одну строку контекста: bias + reasoning + watch_for (сокращённо)
        direction = pair.get("direction", "?")
        conf = pair.get("direction_confidence", "?")
        reasoning_short = (pair.get("reasoning", "") or "")[:200]
        watch_short = (pair.get("watch_for", "") or "")[:150]

        entry = (
            f"{briefing['meta']['session_label_ru']}: bias={direction} (conf={conf}). "
            f"{reasoning_short}. "
            f"Следить: {watch_short}"
        )
        obs[sym].append(entry)

        # Храним не более 5 последних записей на пару
        if len(obs[sym]) > 5:
            obs[sym] = obs[sym][-5:]

    ctx["observations"] = obs
    ctx["last_session"] = briefing["meta"]["session"]
    ctx["last_generated_ts"] = briefing["meta"]["generated_ts"]

    with open(CONTEXT_FILE, "w", encoding="utf-8") as f:
        json.dump(ctx, f, ensure_ascii=False, indent=2)


def format_context_for_prompt(symbol):
    """
    Формирует накопленный контекст для пары в стиле BRIEFING_LOG.md:
    [+предыдущий] ключевые наблюдения из прошлых сессий.
    """
    ctx = load_context()
    obs = ctx.get("observations", {}).get(symbol, [])
    if not obs:
        return "(нет накопленного контекста — первая сессия)"

    lines = ["[Накопленный контекст из предыдущих сессий:]"]
    for i, entry in enumerate(obs):
        lines.append(f"  [+сессия {i+1} назад] {entry}")
    return "\n".join(lines)

    # Fallback: определяем ближайшую сессию по времени
    # Asia:   00:00 → cron 23 (пред.)
    # London: 08:00 → cron 07
    # NY:     13:00 → cron 12
    if 0 <= hour < 8:
        return "asia", SESSIONS["asia"]
    elif 8 <= hour < 13:
        return "london", SESSIONS["london"]
    else:
        return "ny", SESSIONS["ny"]


# ─────────────────────────────────────────────
# Системные промпты (сессионно-зависимые)
# ─────────────────────────────────────────────

def build_system_prompt(session_key, session_cfg):  # type: (str, Dict) -> str
    """
    Строит системный промпт для DeepSeek с учётом сессии.

    Args:
        session_key: "asia" / "london" / "ny"
        session_cfg: конфиг сессии из SESSIONS

    Returns:
        Строка системного промпта.
    """
    session_contexts = {
        "asia": """
ХАРАКТЕРИСТИКИ АЗИАТСКОЙ СЕССИИ:
- Низкая волатильность, движения 20-40 пипсов по основным парам
- Tokyo трейдеры активны 00:00-06:00 UTC
- Thin market — возможны ложные пробои и расширенные спреды
- Основные драйверы: данные Японии, BOJ rhetoric, carry trade flows, overnight gaps
- Новости после закрытия US рынка (21:00 UTC) уже в цене к открытию Asia""",

        "london": """
ХАРАКТЕРИСТИКИ ЛОНДОНСКОЙ СЕССИИ:
- Высокая волатильность, самый ликвидный период дня
- Пересечение с Asia (08:00-09:00) и NY (13:00-17:00)
- Основные драйверы: UK/Europe данные, US pre-market, ECB communication
- Часто задаёт направление на весь день
- Открытие европейских бирж в 08:00 UTC — ключевой момент для импульса""",

        "ny": """
ХАРАКТЕРИСТИКИ НЬЮ-ЙОРКСКОЙ СЕССИИ:
- Высокая волатильность, пересечение с London (13:00-17:00)
- Публикация US макро-данных (CPI, GDP, NFP, FOMC, unemployment)
- Основные драйверы: US data releases, Fed speakers, DXY flows, oil/crude
- Часто развороты от London-максимумов/минимумов
- После 17:00 UTC ликвидность падает, остаётся US-only""",
    }

    context = session_contexts.get(session_key, session_contexts["asia"])

    return f"""Ты — макро-аналитик {session_cfg['label_ru']} торговой сессии
({session_cfg['start_utc']}–{session_cfg['end_utc']} UTC).

Твоя задача: написать РАЗВЁРНУТЫЙ АНАЛИТИЧЕСКИЙ БРИФИНГ для КАЖДОЙ из 4 валютных пар
на ближайшую сессию. Стиль — как у профессионального валютного аналитика:
живой язык, конкретные катализаторы, нарратив, а не буллет-пойнты.

{context}

Ты анализируешь пары: {", ".join(SYMBOLS)}.

ВСЕ ТЕКСТОВЫЕ ПОЛЯ ДОЛЖНЫ БЫТЬ НА РУССКОМ ЯЗЫКЕ.

ВАЖНЫЕ ПРИНЦИПЫ:
- Называй КОНКРЕТНЫЕ катализаторы по именам (Goldman, Reuters, FOMC, NFP, BOJ, ECB и т.д.)
- Если есть накопленный контекст — УЧИТЫВАЙ его. Строй историю, а не одноразовый прогноз.
- Разделяй: сначала bias по КАЖДОЙ валюте отдельно (USD, EUR, JPY, AUD, CAD),
  потом синтез в направление пары.
- Если технический уровень сильнее фундаментала — ЧЕСТНО скажи об этом.
- Указывай confidence 1-5, где 5 = только при совпадении фундаментала И техники.

ФОРМАТ ОТВЕТА — строгий JSON (без markdown, без ```json):

{{
  "currency_bias": {{
    "USD": "BULLISH" | "BEARISH" | "NEUTRAL",
    "EUR": "BULLISH" | "BEARISH" | "NEUTRAL",
    "JPY": "BULLISH" | "BEARISH" | "NEUTRAL",
    "AUD": "BULLISH" | "BEARISH" | "NEUTRAL",
    "CAD": "BULLISH" | "BEARISH" | "NEUTRAL"
  }},
  "pairs": {{
    "EUR/USD": {{
      "direction": "UP" | "DOWN" | "NEUTRAL",
      "direction_confidence": 1-5,
      "reasoning": "РАЗВЁРНУТЫЙ нарратив 300-400 символов: почему этот bias: макро-факторы → названные катализаторы → техническая картина → синтез. Как в аналитической записке.",
      "technical_summary": "ТЕХНИЧЕСКАЯ картина 200-250 символов: цена, D1 диапазон, H4 тренд, позиция, ключевые уровни, волатильность",
      "support_levels": [уровень1, уровень2],
      "resistance_levels": [уровень1, уровень2],
      "trend": "bullish" | "bearish" | "neutral",
      "key_events": ["конкретное событие (UTC время)", ...],
      "watch_for": "СЦЕНАРИЙ 250-350 символов: что будет если пробьём уровень X? А если отскочим? Какие новости могут перевернуть картину? Конкретные триггеры."
    }},
    "USD/JPY": {{ ... }},
    "AUD/USD": {{ ... }},
    "USD/CAD": {{ ... }}
  }},
  "accumulated_context": {{
    "EUR/USD": ["ключевое наблюдение которое перейдёт в следующий брифинг", ...],
    "USD/JPY": [...],
    "AUD/USD": [...],
    "USD/CAD": [...]
  }},
  "global_context": {{
    "key_events_today": ["главные события с временем UTC"],
    "risk_factors": ["РАЗВЁРНУТО: конкретные риски, геополитика, неожиданные данные которые могут сломать bias"],
    "risk_appetite": "risk_on" | "risk_off" | "neutral",
    "session_volatility": "LOW" | "NORMAL" | "HIGH",
    "recommendation": "ИТОГОВАЯ рекомендация 200-300 символов: что делать, какие пары трогать, какие не трогать, где самый сильный conviction"
  }}
}}

ТРЕБОВАНИЯ К ОБЪЁМУ (строже чем раньше):
- reasoning: МИНИМУМ 300 символов (иначе брифинг бесполезен)
- technical_summary: 200-250 символов
- watch_for: МИНИМУМ 250 символов (с конкретными сценариями)
- recommendation: МИНИМУМ 200 символов
- accumulated_context: 2-4 предложения НА КАЖДУЮ ПАРУ — что запомнить для следующего брифинга"""



# ─────────────────────────────────────────────
# Данные
# ─────────────────────────────────────────────

def fetch_news():  # type: (...) -> List[Dict]
    """Скачивает заголовки из RSS-фидов, фильтрует по forex-ключевым словам."""
    items = []
    for feed_info in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_info["url"])
            for entry in feed.entries[:5]:
                title = entry.get("title", "").strip()
                summary = entry.get("summary", entry.get("description", "")).strip()
                text = (title + " " + summary).lower()

                if not any(kw.lower() in text for kw in FOREX_KEYWORDS):
                    continue

                items.append({
                    "source": feed_info["name"],
                    "title": title,
                    "summary": summary[:400],
                    "link": entry.get("link", ""),
                    "time": entry.get("published", ""),
                })
        except Exception as e:
            log.warning("Ошибка чтения %s: %s", feed_info["name"], e)

    log.info("Скачано %d релевантных заголовков из RSS", len(items))
    return items


def _read_candles(conn, symbol, tf, limit):  # type: (...) -> List[Dict]
    """
    Читает последние `limit` свечей пары из market.db, старые первыми.

    Args:
        conn:   Открытое read-only соединение с market.db.
        symbol: Пара, напр. "EUR/USD".
        tf:     Таймфрейм, напр. "M1" / "M5".
        limit:  Сколько последних свечей вернуть.

    Returns:
        Список dict'ов {time, open, high, low, close} в порядке возрастания времени.
        Пустой список, если данных нет.
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


def get_technical_context():  # type: (...) -> Dict
    """
    Извлекает технический контекст для всех SYMBOLS из market.db (свечи).

    Раньше источником был market_memory.json — побочный продукт сигнального
    контура, выключенного в Фазе 2.1. Определения htf_trend / range_pos /
    volatility повторяют market_engine.py (окно 30 свечей M5), near_high и
    near_low — structure_engine.py (порог 15% от ширины диапазона), но считаются
    по свечам, а не по тикам. micro_trend — свечной аналог тикового: направление
    последних 5 закрытий M1.

    Returns:
        {"symbols": {"EUR/USD": {price, htf_trend, day_high, ...}, ...}}
        Пары без данных в БД просто отсутствуют в словаре.
    """
    ctx = {"symbols": {}}

    if not os.path.exists(DB_FILE):
        log.warning("market.db не найден: %s", DB_FILE)
        return ctx

    try:
        # read-only: брифинг — читатель, писать в боевую БД он не должен.
        # WAL позволяет читать параллельно с пишущим server.py.
        conn = sqlite3.connect("file:%s?mode=ro" % DB_FILE, uri=True)
    except Exception as e:
        log.warning("Не открылся market.db: %s", e)
        return ctx

    try:
        for sym in SYMBOLS:
            m5 = _read_candles(conn, sym, "M5", 30)
            m1 = _read_candles(conn, sym, "M1", 1440)   # сутки

            if not m1 or len(m5) < 2:
                log.warning("Нет данных для %s в market.db", sym)
                continue

            pip_div = 0.01 if "JPY" in sym else 0.0001
            price = m1[-1]["close"]

            # ── Дневной диапазон: последние 24ч по M1 ──
            day_high = max(c["high"] for c in m1)
            day_low = min(c["low"] for c in m1)
            day_close = price

            # ── HTF-тренд и позиция в диапазоне: 30 свечей M5 (как в market_engine) ──
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

            # near_* — порог 15% от границы, как в structure_engine
            near_high = range_pos > 0.85
            near_low = range_pos < 0.15

            # ── Волатильность: размах последней M5 против средней (как в market_engine) ──
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

            # ── Micro-trend: направление последних 5 закрытий M1 ──
            tail = [c["close"] for c in m1[-6:]]
            if len(tail) >= 2:
                ups = sum(1 for a, b in zip(tail, tail[1:]) if b > a)
                downs = sum(1 for a, b in zip(tail, tail[1:]) if b < a)
                micro_trend = ("up" if ups > downs + 1
                               else "down" if downs > ups + 1
                               else "flat")
            else:
                micro_trend = "flat"

            # ── Диапазон текущей сессии: бары с её начала (UTC) ──
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

            # velocity: тиковой скорости в свечах нет — берём смещение последней
            # минутной свечи в единицах цены за секунду (грубый аналог).
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


def get_calendar_events():  # type: (...) -> List[Dict]
    """Читает экономический календарь на сегодня."""
    events = []
    if not os.path.exists(NEWS_CALENDAR):
        return events

    now = datetime.now(timezone.utc).timestamp()
    today_end = now + 86400

    try:
        with open(NEWS_CALENDAR) as f:
            reader = csv.DictReader(f)
            for row in reader:
                ts = int(row.get("ts_utc", 0))
                if now <= ts <= today_end:
                    events.append({
                        "ts_utc": ts,
                        "time_str": datetime.utcfromtimestamp(ts).strftime("%H:%M UTC"),
                        "currency": row.get("currency", ""),
                        "event": row.get("event", ""),
                        "impact": row.get("impact", "High"),
                    })
    except Exception as e:
        log.warning("Ошибка чтения календаря: %s", e)

    return events


# ─────────────────────────────────────────────
# Хелперы фильтрации
# ─────────────────────────────────────────────

def get_news_for_pair(news_items, symbol):  # type: (List[Dict], str) -> List[Dict]
    """Фильтрует новости, релевантные конкретной паре."""
    keywords = PAIR_KEYWORDS.get(symbol, [])
    return [item for item in news_items
            if any(kw.lower() in (item["title"] + " " + item["summary"]).lower()
                   for kw in keywords)]


def get_calendar_for_pair(calendar, symbol):  # type: (List[Dict], str) -> List[Dict]
    """Фильтрует события календаря по валютам пары."""
    currencies = symbol.split("/")
    return [e for e in calendar if e["currency"] in currencies]


# ─────────────────────────────────────────────
# Промпт
# ─────────────────────────────────────────────

def build_prompt(news_items, technical, calendar, session_key, session_cfg):  # type: (...) -> str
    """Собирает все данные в промпт для DeepSeek."""
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    today_str = datetime.now(timezone.utc).strftime("%d %B %Y")

    # ── Новости (общие + per-pair) ──
    news_text = "ОБЩИЕ ЗАГОЛОВКИ:\n"
    for i, item in enumerate(news_items[:20], 1):
        news_text += f"{i}. [{item['source']}] {item['title']}\n"
        if item['summary']:
            news_text += f"   {item['summary'][:200]}\n"

    if len(news_items) == 0:
        news_text += "(новостей не найдено — выходной или проблемы с RSS)\n"

    # Per-pair новости
    news_text += "\nНОВОСТИ ПО ПАРАМ:\n"
    for sym in SYMBOLS:
        sym_news = get_news_for_pair(news_items, sym)
        if sym_news:
            news_text += f"\n{sym} ({len(sym_news)} новостей):\n"
            for item in sym_news[:5]:
                news_text += f"  - [{item['source']}] {item['title']}\n"

    # ── Технический контекст ──
    tech_text = ""
    for sym, data in technical.get("symbols", {}).items():
        pos_desc = ("у верхней границы" if data['near_high']
                    else "у нижней границы" if data['near_low']
                    else "в середине")
        tech_text += f"""
{sym}:
  Цена: {data['price']:.5f}
  D1 high/low: {data['day_high']:.5f} / {data['day_low']:.5f} (range: {data['day_range_pips']:.0f} pips)
  H4 тренд: {data['htf_trend']}
  Позиция в диапазоне: {data['range_pos']:.2f} ({pos_desc})
  Micro-trend: {data['micro_trend']}
  Волатильность: {data['volatility']}
"""

    if not tech_text:
        tech_text = "(технические данные недоступны)\n"

    # ── Календарь (per-pair) ──
    cal_text = ""
    if calendar:
        for sym in SYMBOLS:
            sym_cal = get_calendar_for_pair(calendar, sym)
            if sym_cal:
                cal_text += f"\n{sym} события сегодня:\n"
                for e in sym_cal:
                    cal_text += f"  {e['time_str']} [{e['impact']}] — {e['event']} ({e['currency']})\n"

    if not cal_text:
        cal_text = "(нет high-impact событий на сегодня)\n"

    # ── Накопленный контекст (из предыдущих брифингов) ──
    ctx_text = ""
    for sym in SYMBOLS:
        ctx_text += f"\n{sym}:\n{format_context_for_prompt(sym)}\n"

    return f"""Время сейчас: {now_utc}
Сессия: {session_cfg['label_ru']} ({session_cfg['start_utc']}–{session_cfg['end_utc']} UTC), начинается в течение часа.

=== НАКОПЛЕННЫЙ КОНТЕКСТ (из предыдущих брифингов) ===
{ctx_text}

=== НОВОСТНОЙ ФОН ===
{news_text}

=== ТЕХНИЧЕСКАЯ КАРТИНА ==={tech_text}

=== ЭКОНОМИЧЕСКИЙ КАЛЕНДАРЬ ===
{cal_text}

Сделай анализ и дай bias для ВСЕХ 4 пар на ближайшую {session_cfg['label_ru']} сессию ({today_str})."""


# ─────────────────────────────────────────────
# Основной пайплайн
# ─────────────────────────────────────────────

def main():
    # 0. Определяем сессию
    session_key, session_cfg = detect_session()
    # detect_session() всегда возвращает сессию (cron-час или fallback)
    assert session_key is not None, "detect_session() вернул None"

    log.info("=== Pre-Session Brief: %s ===", session_cfg['label_ru'])
    log.info("Сессия: %s (%s–%s UTC)", session_key, session_cfg['start_utc'], session_cfg['end_utc'])

    if not DEEPSEEK_API_KEY:
        log.error("DEEPSEEK_API_KEY не задан в .env")
        sys.exit(1)

    # 1. Новости
    log.info("Скачиваю RSS...")
    news_items = fetch_news()

    # 2. Технический контекст
    log.info("Извлекаю технический контекст для %d пар...", len(SYMBOLS))
    technical = get_technical_context()
    log.info("Технический контекст получен для: %s",
             list(technical.get("symbols", {}).keys()))

    # 3. Календарь
    log.info("Читаю экономический календарь...")
    calendar = get_calendar_events()
    log.info("Событий на сегодня: %d", len(calendar))

    # 4. Промпт → DeepSeek
    system_prompt = build_system_prompt(session_key, session_cfg)
    prompt = build_prompt(news_items, technical, calendar, session_key, session_cfg)
    log.info("Промпт: %d символов. Отправляю в DeepSeek...", len(prompt))

    client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")

    try:
        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            max_tokens=4000,   # больше токенов для нарративных брифингов
            temperature=0.3,
        )
        raw = response.choices[0].message.content.strip()
    except Exception as e:
        log.error("DeepSeek API error: %s", e)
        sys.exit(1)

    # 5. Парсим JSON
    try:
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        log.error("JSON parse error: %s\nRaw: %s", e, raw[:500])
        sys.exit(1)

    # 6. Валидация
    if "pairs" not in data:
        log.error("Отсутствует 'pairs' в ответе")
        sys.exit(1)

    # Валидируем accumulated_context (может отсутствовать — не фатально)
    if "accumulated_context" not in data:
        data["accumulated_context"] = {}
        for sym in SYMBOLS:
            pair = data["pairs"].get(sym, {})
            data["accumulated_context"][sym] = [
                f"{pair.get('direction', '?')}: {pair.get('reasoning', '')[:200]}"
            ]

    for sym in SYMBOLS:
        if sym not in data.get("pairs", {}):
            log.error("Отсутствует пара %s в ответе", sym)
            sys.exit(1)
        pair = data["pairs"][sym]
        required = ["direction", "direction_confidence", "reasoning",
                     "technical_summary", "support_levels", "resistance_levels",
                     "trend", "key_events", "watch_for"]
        for field in required:
            if field not in pair:
                log.error("В паре %s отсутствует поле: %s", sym, field)
                sys.exit(1)

    # 7. Добавляем метаданные
    now = datetime.now(timezone.utc)
    briefing = {
        "meta": {
            "session": session_key,
            "session_label": session_cfg['label'],
            "session_label_ru": session_cfg['label_ru'],
            "session_start_utc": session_cfg['start_utc'],
            "session_end_utc": session_cfg['end_utc'],
            "generated_at": now.strftime("%Y-%m-%d %H:%M UTC"),
            "generated_ts": now.timestamp(),
            "valid_until_utc": session_cfg['end_utc'],
            "news_fetched": len(news_items),
            "calendar_events": len(calendar),
            "symbols_analyzed": SYMBOLS,
        },
        "pairs": data["pairs"],
        "global_context": data.get("global_context", {
            "key_events_today": [],
            "risk_factors": [],
            "risk_appetite": "neutral",
            "session_volatility": "NORMAL",
            "recommendation": "",
        }),
    }

    # 8. Пишем briefing.json (новый формат)
    with open(BRIEFING_FILE, "w") as f:
        json.dump(briefing, f, ensure_ascii=False, indent=2)
    log.info("✅ briefing.json записан (%d байт)", os.path.getsize(BRIEFING_FILE))

    # 8b. Сохраняем накопленный контекст для следующего брифинга
    save_context(briefing)
    log.info("✅ briefing_context.json обновлён")

    # 9. session_bias.json больше не пишется (Фаза 2.1).
    # Его единственным читателем был signal_engine.py, а сигнальный контур
    # выключен флагом SIGNALS_ENABLED. Вернётся вместе с сигналами, если
    # понадобится: код лежит в истории git, формат см. LEGACY_FILE.

    # 10. Логируем результат
    log.info("─── Брифинг: %s ───", session_cfg['label_ru'])
    for sym in SYMBOLS:
        pair = data["pairs"][sym]
        log.info("  %s: %s (conf=%s) | trend=%s",
                 sym, pair.get("direction"), pair.get("direction_confidence"),
                 pair.get("trend"))
    log.info("  Волатильность: %s", data.get("global_context", {}).get("session_volatility"))
    log.info("  Риск-аппетит: %s", data.get("global_context", {}).get("risk_appetite"))
    log.info("  Рекомендация: %s", data.get("global_context", {}).get("recommendation", "")[:100])


if __name__ == "__main__":
    main()
