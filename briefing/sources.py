"""
Источники брифинга: новостные RSS-фиды и экономический календарь.

Слой 1 подпроекта briefing/ (см. ROADMAP, Фаза 7). Единственное место, где
брифинг ходит во внешний мир за новостями. Отделено от генерации намеренно:
источники деградируют независимо (фид умер, календарь сменил URL), и их надо
уметь чинить/диагностировать, не трогая промпт и агента.

Что здесь:
  - RSS_FEEDS: только ЖИВЫЕ фиды (проверены живьём 2026-07-18). Мёртвые Reuters
    убраны; добавлены FXStreet/ForexLive/Investing/ActionForex/MarketWatch.
  - fetch_news(): качает, фильтрует по forex-словам, ВОЗВРАЩАЕТ И ДИАГНОСТИКУ
    по каждому фиду (сколько сырых / сколько релевантных) — деградация видима.
  - fetch_calendar(): ForexFactory ff_calendar_thisweek.json (живой источник).
  - Время новостей и событий форматируется в UTC+5 (время машины владельца).

Время: unix-ts везде в UTC (внутренний стандарт), СТРОКИ для показа — в UTC+5.
"""

import time as _time
from datetime import datetime, timedelta, timezone

import feedparser
import urllib.request
import json

# Часовой пояс отображения — время машины владельца (UTC+5). Хранение везде в
# UTC (ts), сдвиг только на строках для человека.
DISPLAY_TZ = timezone(timedelta(hours=5))
DISPLAY_TZ_LABEL = "UTC+5"

# ── новостные фиды ──────────────────────────────────────────────────────
# Проверены живьём 2026-07-18: статус 200/301, непустые. Reuters убран (мёртв).
RSS_FEEDS = [
    # Форекс-специфичные — дают львиную долю релевантного (проверено: 51/75).
    {"name": "FXStreet",      "url": "https://www.fxstreet.com/rss/news"},
    {"name": "ForexLive",     "url": "https://www.forexlive.com/feed"},
    {"name": "ActionForex",   "url": "https://www.actionforex.com/feed/"},
    # Макро-контекст (риск-аппетит, ставки, политика) — реже, но по делу.
    {"name": "Bloomberg Mkt", "url": "https://feeds.bloomberg.com/markets/news.rss"},
    {"name": "Bloomberg Eco", "url": "https://feeds.bloomberg.com/economics/news.rss"},
    {"name": "WSJ Markets",   "url": "https://feeds.a.dj.com/rss/RSSMarketsMain.xml"},
    {"name": "CNBC",          "url": "https://www.cnbc.com/id/100003114/device/rss/rss.html"},
    {"name": "FT Home",       "url": "https://www.ft.com/rss/home/uk"},
    # Investing/MarketWatch убраны — общерыночные, ~0 релевантного forex.
]

# Сколько записей с фида просматривать ДО фильтра. Было [:5] — резало втрое
# (релевантные заголовки часто на позициях 6-30). 20 покрывает и богатые фиды.
FEED_SCAN_LIMIT = 20

# Порог «новостей мало»: если релевантных суммарно меньше — брифинг пометит это,
# чтобы на фронте было видно «лента худая», а не молчаливо пустая карточка.
LOW_NEWS_THRESHOLD = 8

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
_KW_LOWER = [k.lower() for k in FOREX_KEYWORDS]


def _fmt_display(ts):
    """Строка времени в UTC+5 для показа человеку.

    Args:
        ts: Unix-время в секундах (UTC).

    Returns:
        Строка "DD.MM HH:MM UTC+5" или "" при некорректном ts.
    """
    if not ts:
        return ""
    try:
        dt = datetime.fromtimestamp(ts, DISPLAY_TZ)
        return dt.strftime("%d.%m %H:%M ") + DISPLAY_TZ_LABEL
    except (ValueError, OSError, OverflowError):
        return ""


def _entry_ts(entry):
    """Unix-ts публикации записи RSS (UTC), либо None.

    feedparser кладёт разобранное время в published_parsed (struct_time в UTC).

    Args:
        entry: Запись feedparser.

    Returns:
        Int unix-секунд или None.
    """
    tm = entry.get("published_parsed") or entry.get("updated_parsed")
    if not tm:
        return None
    try:
        return int(_time.mktime(tm) - _time.timezone)  # struct_time UTC → ts
    except (ValueError, OverflowError):
        return None


def fetch_news():
    """Скачать заголовки из живых RSS-фидов с диагностикой по каждому.

    Каждый фид просматривается до FEED_SCAN_LIMIT записей, фильтруется по
    forex-словам. Отказ одного фида не роняет остальные.

    Args:
        None.

    Returns:
        Tuple (items, diag):
          items — список dict {source, title, summary, link, ts, time_display};
          diag  — список dict {source, raw, relevant, ok, error} по каждому фиду
                  (для лога и meta брифинга — деградация видна).
    """
    items = []
    diag = []

    for feed_info in RSS_FEEDS:
        name = feed_info["name"]
        try:
            feed = feedparser.parse(feed_info["url"])
            raw = len(feed.entries)
            relevant = 0

            for entry in feed.entries[:FEED_SCAN_LIMIT]:
                title = entry.get("title", "").strip()
                summary = entry.get("summary", entry.get("description", "")).strip()
                text = (title + " " + summary).lower()
                if not any(kw in text for kw in _KW_LOWER):
                    continue

                ts = _entry_ts(entry)
                items.append({
                    "source": name,
                    "title": title,
                    "summary": summary[:400],
                    "link": entry.get("link", ""),
                    "ts": ts,
                    "time_display": _fmt_display(ts),
                })
                relevant += 1

            diag.append({"source": name, "raw": raw, "relevant": relevant,
                         "ok": True, "error": None})
        except Exception as err:
            diag.append({"source": name, "raw": 0, "relevant": 0,
                         "ok": False, "error": repr(err)})

    return items, diag


def news_summary(diag):
    """Однострочный отчёт о фидах для лога + флаг «новостей мало».

    Args:
        diag: Диагностика из fetch_news().

    Returns:
        Tuple (line, total_relevant, low):
          line — строка "FXStreet 4/30, ForexLive 3/25, …";
          total_relevant — сумма релевантных;
          low — bool, релевантных меньше порога.
    """
    parts = []
    total = 0
    for d in diag:
        if d["ok"]:
            parts.append("%s %d/%d" % (d["source"], d["relevant"], d["raw"]))
        else:
            parts.append("%s DEAD" % d["source"])
        total += d["relevant"]
    return ", ".join(parts), total, total < LOW_NEWS_THRESHOLD


# ── экономический календарь ─────────────────────────────────────────────

# ForexFactory (faireconomy) — бесплатно, без ключа. Живой на 2026-07-18
# (thisweek = 98 событий). nextweek/thismonth на этом домене НЕ существуют
# (404) — не запрашиваем, покрытия текущей недели брифингу достаточно.
FF_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# Валюты наших пар — фильтр high-impact событий.
CALENDAR_CURRENCIES = {"USD", "JPY", "EUR", "AUD", "CAD"}


def fetch_calendar(window_hours=36):
    """Экономический календарь: high-impact события ближайшего окна.

    Берём напрямую у ForexFactory (не через промежуточный CSV) — на одном
    источнике меньше мест деградации. Окно шире суток (window_hours), чтобы
    захватить события следующего дня для пред-сессионного брифинга.

    Args:
        window_hours: Сколько часов вперёд от «сейчас» брать события.

    Returns:
        Tuple (events, error):
          events — список dict {ts_utc, time_display, currency, event, impact},
                   отсортировано по времени, только будущие high-impact;
          error  — None или строка причины (сеть/парсинг), для лога и meta.
    """
    now = datetime.now(timezone.utc).timestamp()
    horizon = now + window_hours * 3600

    try:
        req = urllib.request.Request(FF_CALENDAR_URL,
                                     headers={"User-Agent": "Mozilla/5.0 (briefing)"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
    except Exception as err:
        return [], repr(err)

    events = []
    for e in data:
        if str(e.get("impact", "")).strip().lower() != "high":
            continue
        cur = str(e.get("country", "")).strip().upper()
        if cur not in CALENDAR_CURRENCIES:
            continue
        date_str = e.get("date")
        if not date_str:
            continue
        try:
            ts = int(datetime.fromisoformat(date_str).timestamp())
        except (ValueError, TypeError):
            continue
        if not (now <= ts <= horizon):
            continue
        events.append({
            "ts_utc": ts,
            "time_display": _fmt_display(ts),
            "currency": cur,
            "event": str(e.get("title", "")).strip(),
            "impact": "High",
        })

    events.sort(key=lambda x: x["ts_utc"])
    return events, None
