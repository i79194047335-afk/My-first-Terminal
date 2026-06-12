"""
fetch_news.py — загрузчик экономического календаря (ForexFactory, бесплатный JSON).

Качает события на текущую и следующую неделю, оставляет только high-impact
по нужным валютам, и пишет data_loaders/news_calendar.csv в формате, который
читает pattern_memory.load_news():

    ts_utc,currency,impact,event

Запуск вручную или по cron (раз в неделю достаточно):
    python3 data_loaders/fetch_news.py

Источник без API-ключа. Серверу нужен доступ в интернет.
"""

import csv
import json
import os
import sys
import tempfile
import urllib.request
from datetime import datetime

# Фиды ForexFactory (FairEconomy mirror) — текущая и следующая неделя
FEEDS = [
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "https://nfs.faireconomy.media/ff_calendar_nextweek.json",
]

# Валюты наших пар (EUR/USD, USD/CAD, AUD/USD, USD/JPY) — остальное игнорируем
CURRENCIES = {"USD", "EUR", "CAD", "AUD", "JPY"}

OUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "news_calendar.csv")

# Куда движок ждёт файл (относительно корня проекта) — пишем туда же
NEWS_FILE = OUT_FILE


def _fetch(url):
    """
    Качает один JSON-фид ForexFactory.

    Args:
        url (str): адрес фида.

    Returns:
        list: список событий (dict), либо [] при ошибке.
    """
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (news-fetch)"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"[news] Ошибка загрузки {url}: {e}")
        return []


def fetch_all():
    """
    Качает все фиды, фильтрует high-impact по нужным валютам, парсит время.

    Returns:
        list[tuple]: [(ts_utc:int, currency:str, event:str), ...] без дублей,
                     отсортировано по времени.
    """
    rows = {}
    for url in FEEDS:
        for e in _fetch(url):
            if str(e.get("impact", "")).strip().lower() != "high":
                continue
            cur = str(e.get("country", "")).strip().upper()
            if cur not in CURRENCIES:
                continue
            date_str = e.get("date")
            if not date_str:
                continue
            try:
                ts = int(datetime.fromisoformat(date_str).timestamp())
            except (ValueError, TypeError):
                continue
            title = str(e.get("title", "")).strip()
            rows[(ts, cur, title)] = True

    return sorted(rows.keys())


def write_csv(events):
    """
    Пишет события в CSV атомарно (temp + rename), чтобы сервер не прочитал
    наполовину записанный файл.

    Args:
        events (list[tuple]): результат fetch_all().
    """
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(OUT_FILE), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["ts_utc", "currency", "impact", "event"])
            for ts, cur, title in events:
                w.writerow([ts, cur, "High", title])
        os.replace(tmp, OUT_FILE)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def main():
    events = fetch_all()
    if not events:
        print("[news] Нет событий — файл не перезаписан (сеть/фид недоступны?)")
        sys.exit(1)
    write_csv(events)
    now = datetime.utcnow()
    upcoming = [e for e in events if e[0] >= now.timestamp()]
    print(f"[news] Записано {len(events)} high-impact событий -> {OUT_FILE}")
    print(f"[news] Из них впереди: {len(upcoming)}")
    for ts, cur, title in upcoming[:5]:
        print(f"   {datetime.utcfromtimestamp(ts).strftime('%m-%d %H:%M')} UTC | {cur} | {title}")


if __name__ == "__main__":
    main()
