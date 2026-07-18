"""
pre_asia_brief.py — утренний брифинг перед азиатской сессией.

Собирает макро-контекст (новости, календарь, технику) и генерирует
session_bias.json с direction bias для USD, JPY, и USD/JPY.

Запуск: python3 pre_asia_brief.py
Крон:   45 23 * * * cd /root/projects/terminal && python3 pre_asia_brief.py

Зависимости: feedparser, openai, python-dotenv
"""

import json
import os
import sys
import logging
import csv
from datetime import datetime, timezone
from typing import List, Dict, Optional

import feedparser
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("pre_asia")
log.setLevel(logging.INFO)
log.addHandler(logging.StreamHandler())

# ─────────────────────────────────────────────
# Конфигурация
# ─────────────────────────────────────────────

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
OUTPUT_FILE = "session_bias.json"
MEMORY_FILE = "market_memory.json"
NEWS_CALENDAR = "data_loaders/news_calendar.csv"

# RSS-фиды — только макро/валютные, без crypto/companies
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

# Валюты которые нас интересуют
FOREX_KEYWORDS = [
    "USD", "dollar", "Federal Reserve", "Fed", "Treasury", "yield",
    "JPY", "yen", "BOJ", "Bank of Japan", "Japan",
    "forex", "currency", "FX", "carry trade",
    "risk appetite", "safe haven", "risk-off", "risk-on",
]

# Символы для технического анализа
SYMBOLS = ["USD/JPY"]

# ─────────────────────────────────────────────
# Промпт для DeepSeek
# ─────────────────────────────────────────────

SYSTEM_PROMPT = """Ты — макро-аналитик азиатской торговой сессии (00:00–08:00 UTC).

Твоя задача: на основе новостного фона, экономического календаря и технической картины
дать direction bias для USD, JPY и USD/JPY на ближайшую азиатскую сессию.

КОНТЕКСТ:
- Asia-сессия низковолатильна, движения обычно 20-40 пипсов по USD/JPY
- Основные драйверы JPY: BOJ rhetoric, Japan data, risk-on/off, US-JP yield spread, carry trade flows
- Основные драйверы USD: Fed policy expectations, US data surprises, DXY trend, risk appetite
- В Asia Tokyo-трейдеры active часы 00:00-06:00 UTC
- Новости после закрытия US рынка (21:00 UTC) уже в цене к открытию Asia

ФОРМАТ ОТВЕТА — строгий JSON (без markdown, без ```json):

{
  "usd_bias": "BULLISH" | "BEARISH" | "NEUTRAL",
  "usd_confidence": 1-5,
  "usd_reasoning": "2-3 предложения: главные факторы за/против USD сегодня",

  "jpy_bias": "BULLISH" | "BEARISH" | "NEUTRAL",
  "jpy_confidence": 1-5,
  "jpy_reasoning": "2-3 предложения: главные факторы за/против JPY сегодня",

  "usdjpy_bias": "UP" | "DOWN" | "NEUTRAL",
  "usdjpy_confidence": 1-5,
  "usdjpy_reasoning": "2-3 предложения: синтез USD и JPY bias, ключевые уровни, чего ждать",

  "key_events_today": ["событие 1 (UTC время)", "событие 2"] или [],
  "risk_factors": ["фактор который может сломать bias"] или [],
  "session_volatility": "LOW" | "NORMAL" | "HIGH",
  "recommendation": "одно предложение: торговать или нет, в каком направлении"
}

ВАЖНО:
- Если новостей мало или картина неясная — честно ставь NEUTRAL с confidence 2-3
- Не выдумывай новости которых нет в подборке
- Если economic calendar пустой — так и пиши
- Учитывай что Asia — thin market, возможны ложные пробои
- JPY_BEARISH + USD_BULLISH → USD/JPY UP (long)
- JPY_BULLISH + USD_BEARISH → USD/JPY DOWN (short)
- Разнонаправленные bias → NETURAL или слабый bias"""


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

                # Фильтр: новость должна касаться валют / макро
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


def get_technical_context():  # type: (...) -> Dict
    """
    Извлекает технический контекст для USD/JPY из market_memory.json.
    Возвращает D1/H4 уровни, последний диапазон, тренд.
    """
    ctx = {"symbols": {}}

    if not os.path.exists(MEMORY_FILE):
        log.warning("market_memory.json не найден")
        return ctx

    try:
        with open(MEMORY_FILE) as f:
            memory = json.load(f)
    except Exception as e:
        log.warning("Ошибка чтения market_memory.json: %s", e)
        return ctx

    for sym in SYMBOLS:
        # Последние 200 паттернов по этому символу
        sym_patterns = [p for p in memory if p.get("symbol") == sym and p.get("resolved")]
        if not sym_patterns:
            continue

        # Сортируем по времени
        sym_patterns.sort(key=lambda p: p.get("time", 0))
        recent = sym_patterns[-200:]

        # D1/H4 уровни — из самых свежих паттернов
        latest = sym_patterns[-1]
        state = latest.get("state", {})

        # Ищем дневной диапазон (high/low за последние 24h)
        now = datetime.now(timezone.utc).timestamp()
        day_ago = now - 86400
        day_patterns = [p for p in sym_patterns if p.get("time", 0) > day_ago]

        if day_patterns:
            day_high = max(p.get("price", 0) for p in day_patterns)
            day_low = min(p.get("price", 0) for p in day_patterns)
            day_close = day_patterns[-1].get("price", 0)
        else:
            day_high = state.get("range_high", 0)
            day_low = state.get("range_low", 0)
            day_close = latest.get("price", 0)

        # H4 тренд — из state
        htf = state.get("htf", "unknown")

        # Последний Asia-диапазон
        asia_patterns = [p for p in sym_patterns
                         if p.get("session") == "asia" and p.get("time", 0) > day_ago]
        if asia_patterns:
            asia_high = max(p.get("price", 0) for p in asia_patterns)
            asia_low = min(p.get("price", 0) for p in asia_patterns)
            asia_range = asia_high - asia_low
        else:
            asia_high = asia_low = asia_range = 0

        ctx["symbols"][sym] = {
            "price": latest.get("price", 0),
            "htf_trend": htf,
            "day_high": day_high,
            "day_low": day_low,
            "day_close": day_close,
            "day_range_pips": abs(day_high - day_low) / (0.01 if "JPY" in sym else 0.0001),
            "asia_high": asia_high,
            "asia_low": asia_low,
            "asia_range_pips": abs(asia_range) / (0.01 if "JPY" in sym else 0.0001),
            "range_pos": state.get("range_pos", 0.5),
            "near_high": state.get("near_high", False),
            "near_low": state.get("near_low", False),
            "velocity": state.get("velocity", 0),
            "micro_trend": state.get("micro_trend", "unknown"),
            "volatility": state.get("volatility", "unknown"),
        }

    return ctx


def get_calendar_events():  # type: (...) -> List[Dict]
    """Читает экономический календарь на сегодня."""
    events = []
    if not os.path.exists(NEWS_CALENDAR):
        return events

    now = datetime.now(timezone.utc).timestamp()
    today_end = now + 86400

    try:
        import csv
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


def build_prompt(news_items, technical, calendar):  # type: (...) -> str
    """Собирает все данные в промпт для DeepSeek."""

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Новостная подборка
    news_text = ""
    for i, item in enumerate(news_items[:15], 1):
        news_text += f"{i}. [{item['source']}] {item['title']}\n"
        if item['summary']:
            news_text += f"   {item['summary'][:200]}\n"

    if not news_text:
        news_text = "(новостей не найдено — выходной или проблемы с RSS)\n"

    # Технический контекст
    tech_text = ""
    for sym, data in technical.get("symbols", {}).items():
        tech_text += f"""
{sym}:
  Цена: {data['price']:.5f}
  D1 high/low: {data['day_high']:.5f} / {data['day_low']:.5f} (range: {data['day_range_pips']:.0f} pips)
  H4 тренд: {data['htf_trend']}
  Позиция в диапазоне: {data['range_pos']:.2f} ({"у верхней границы" if data['near_high'] else "у нижней границы" if data['near_low'] else "в середине"})
  Micro-trend: {data['micro_trend']}
  Волатильность: {data['volatility']}
"""

    if not tech_text:
        tech_text = "(технические данные недоступны)\n"

    # Экономический календарь
    cal_text = ""
    if calendar:
        usd_events = [e for e in calendar if e['currency'] == 'USD']
        jpy_events = [e for e in calendar if e['currency'] == 'JPY']
        other_events = [e for e in calendar if e['currency'] not in ('USD', 'JPY')]

        if usd_events:
            cal_text += "USD события сегодня:\n"
            for e in usd_events:
                cal_text += f"  {e['time_str']} — {e['event']}\n"
        if jpy_events:
            cal_text += "JPY события сегодня:\n"
            for e in jpy_events:
                cal_text += f"  {e['time_str']} — {e['event']}\n"
        if other_events:
            cal_text += "Другие:\n"
            for e in other_events:
                cal_text += f"  {e['time_str']} [{e['currency']}] — {e['event']}\n"

    if not cal_text:
        cal_text = "(нет high-impact событий на сегодня)\n"

    return f"""Время сейчас: {now_utc}
Сессия: Азиатская (00:00–08:00 UTC) начинается через 15 минут.

=== НОВОСТНОЙ ФОН ===
{news_text}

=== ТЕХНИЧЕСКАЯ КАРТИНА ==={tech_text}

=== ЭКОНОМИЧЕСКИЙ КАЛЕНДАРЬ ===
{cal_text}

Сделай анализ и дай bias на ближайшую Asia-сессию (08 июля 2026)."""


def main():
    log.info("=== Pre-Asia Brief: %s ===", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))

    if not DEEPSEEK_API_KEY:
        log.error("DEEPSEEK_API_KEY не задан в .env")
        sys.exit(1)

    # 1. Новости
    log.info("Скачиваю RSS...")
    news_items = fetch_news()

    # 2. Технический контекст
    log.info("Извлекаю технический контекст...")
    technical = get_technical_context()

    # 3. Календарь
    log.info("Читаю экономический календарь...")
    calendar = get_calendar_events()

    # 4. Промпт → DeepSeek
    prompt = build_prompt(news_items, technical, calendar)
    log.info("Промпт: %d символов. Отправляю в DeepSeek...", len(prompt))

    client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")

    try:
        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            max_tokens=800,
            temperature=0.3,
        )
        raw = response.choices[0].message.content.strip()
    except Exception as e:
        log.error("DeepSeek API error: %s", e)
        sys.exit(1)

    # 5. Парсим JSON
    try:
        # Убираем возможные markdown-обёртки
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        bias = json.loads(raw)
    except json.JSONDecodeError as e:
        log.error("JSON parse error: %s\nRaw: %s", e, raw[:500])
        sys.exit(1)

    # 6. Валидация
    required = ["usdjpy_bias", "usd_bias", "jpy_bias"]
    for field in required:
        if field not in bias:
            log.error("Отсутствует обязательное поле: %s", field)
            sys.exit(1)

    # 7. Добавляем метаданные
    bias["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    bias["generated_ts"] = datetime.now(timezone.utc).timestamp()
    bias["valid_until"] = "08:00 UTC"  # конец Asia
    bias["news_count"] = len(news_items)
    bias["calendar_events"] = len(calendar)
    bias["symbols_analyzed"] = list(technical.get("symbols", {}).keys())

    # 8. Пишем результат
    with open(OUTPUT_FILE, "w") as f:
        json.dump(bias, f, ensure_ascii=False, indent=2)

    log.info("✅ session_bias.json записан:")
    log.info("   USD:     %s (confidence=%s)", bias.get("usd_bias"), bias.get("usd_confidence"))
    log.info("   JPY:     %s (confidence=%s)", bias.get("jpy_bias"), bias.get("jpy_confidence"))
    log.info("   USD/JPY: %s (confidence=%s)", bias.get("usdjpy_bias"), bias.get("usdjpy_confidence"))
    log.info("   Событий сегодня: %d", len(calendar))
    log.info("   Волатильность: %s", bias.get("session_volatility"))
    if bias.get("key_events_today"):
        for e in bias["key_events_today"]:
            log.info("   📅 %s", e)
    if bias.get("risk_factors"):
        for r in bias["risk_factors"]:
            log.info("   ⚠️ %s", r)


if __name__ == "__main__":
    main()
