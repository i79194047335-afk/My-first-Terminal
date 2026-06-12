"""
pattern_memory.py

Хранение паттернов с правильным окном результата:
  - Вход: T+60 сек от сигнала
  - Экспирация: T+240 сек от сигнала (180 сек после входа)
  - Результат: движение цены между T+60 и T+240
  - Фильтр шума: минимум 2 пипса движения
  - Новостной флаг: is_news=True если в окне -15/+30 мин есть high impact
"""

import json
import os
import atexit
from datetime import datetime

MEMORY_FILE    = "market_memory.json"
NEWS_FILE      = "data_loaders/news_calendar.csv"
AUTOSAVE_EVERY = 200

PIP_SIZE = {
    "EUR/USD": 0.0001,
    "USD/CAD": 0.0001,
    "AUD/USD": 0.0001,
    "USD/JPY": 0.01
}

# Валюты входящие в каждую пару
PAIR_CURRENCIES = {
    "EUR/USD": {"EUR", "USD"},
    "USD/CAD": {"USD", "CAD"},
    "AUD/USD": {"AUD", "USD"},
    "USD/JPY": {"USD", "JPY"},
}

# Окно результата
ENTRY_DELAY  = 60   # секунд от сигнала до входа
EXPIRY_DELAY = 240  # секунд от сигнала до экспирации
MIN_PIPS     = 2.0  # минимальное движение для засчитывания

# Новостное окно
NEWS_BEFORE = 900   # 15 минут до новости
NEWS_AFTER  = 1800  # 30 минут после новости

memory          = []
active_patterns = []
_unsaved_count  = 0

# ============================================================
# ЗАГРУЗКА НОВОСТЕЙ
# ============================================================

_news_cache = None   # [(ts_utc, currency), ...]
_news_mtime = 0      # mtime файла на момент последней загрузки

def load_news():
    """
    Загружает high-impact новости из NEWS_FILE с кешем по mtime.

    Перечитывает файл, если он изменился (недельный cron fetch_news.py),
    поэтому сервер подхватывает свежий календарь без перезапуска.

    Returns:
        list[tuple]: [(ts_utc:int, currency:str), ...] только high-impact.
    """
    global _news_cache, _news_mtime

    if not os.path.exists(NEWS_FILE):
        if _news_cache is None:
            print(f"[memory] Новостной календарь не найден: {NEWS_FILE}")
        _news_cache = []
        return _news_cache

    mtime = os.path.getmtime(NEWS_FILE)
    if _news_cache is not None and mtime <= _news_mtime:
        return _news_cache

    _news_cache = []
    _news_mtime = mtime

    import csv
    try:
        with open(NEWS_FILE, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    if row["impact"].strip().lower() != "high":
                        continue
                    ts  = int(row["ts_utc"])
                    cur = row["currency"].strip().upper()
                    _news_cache.append((ts, cur))
                except (ValueError, KeyError):
                    continue
        print(f"[memory] Загружено {len(_news_cache)} high impact новостей")
    except Exception as e:
        print(f"[memory] Ошибка загрузки новостей: {e}")

    return _news_cache


def is_news_window(ts, symbol):
    """
    Возвращает True если в окне -15/+30 мин от ts
    есть high impact новость по валюте пары.
    """
    news   = load_news()
    currs  = PAIR_CURRENCIES.get(symbol, set())
    ts_int = int(ts)

    for news_ts, news_cur in news:
        if news_cur not in currs:
            continue
        if (ts_int - NEWS_AFTER) <= news_ts <= (ts_int + NEWS_BEFORE):
            return True

    return False


# ============================================================
# ЗАГРУЗКА / СОХРАНЕНИЕ ПАМЯТИ
# ============================================================

def load_memory():
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, "r") as f:
            try:
                data = json.load(f)
                print(f"[memory] Загружено {len(data)} паттернов из файла")
                return data
            except:
                print("[memory] Файл повреждён — начинаем с нуля")
                return []
    print("[memory] Файл не найден — начинаем с нуля")
    return []


def save_memory():
    global _unsaved_count
    with open(MEMORY_FILE, "w") as f:
        json.dump(memory, f, indent=2)
    _unsaved_count = 0
    print(f"[memory] Сохранено {len(memory)} паттернов → {MEMORY_FILE}")


def _save_on_exit():
    if memory:
        print(f"\n[memory] Аварийное сохранение: {len(memory)} паттернов...")
        save_memory()

atexit.register(_save_on_exit)

memory = load_memory()


# ============================================================
# СОХРАНИТЬ ПАТТЕРН
# ============================================================

def store_pattern(symbol, state, price, ts):
    """
    Сохраняет паттерн с предвычисленными полями для анализа.
    price_at_60 будет заполнен позже через update_entry_price.
    """
    dt_utc   = datetime.utcfromtimestamp(float(ts))
    hour_utc = dt_utc.hour
    minute   = dt_utc.minute
    weekday  = dt_utc.weekday()  # 0=пн, 6=вс
    month    = dt_utc.month
    year     = dt_utc.year

    # Сессия
    if 8 <= hour_utc < 13:
        session = "london"
    elif 13 <= hour_utc < 21:
        session = "ny"
    elif 0 <= hour_utc < 8:
        session = "asia"
    else:
        session = "asia_late"

    # Ширина диапазона в пипсах
    pip = PIP_SIZE.get(symbol, 0.0001)
    range_width_pips = round(state.get("range_width", 0) / pip, 1)

    # Новостное окно
    news = is_news_window(ts, symbol)

    active_patterns.append({
        "symbol":           symbol,
        "time":             float(ts),
        "price":            price,         # цена сигнала T+0
        "price_at_60":      None,          # цена входа T+60 — заполняется позже
        "state":            state,
        "resolved":         False,
        # предвычисленные поля для анализа
        "hour_utc":         hour_utc,
        "minute_utc":       minute,
        "session":          session,
        "weekday":          weekday,
        "month":            month,
        "year":             year,
        "range_width_pips": range_width_pips,
        "vol_ratio":        round(state.get("vol_ratio", 1.0), 3),
        "is_news":          news,
    })


# ============================================================
# ОБНОВИТЬ ЦЕНУ ВХОДА (T+60)
# ============================================================

def update_entry_price(symbol, current_price, ts):
    """
    Записывает цену входа (T+60) для паттернов которые
    ещё не получили её и у которых прошло >= 60 сек.
    """
    for p in active_patterns:
        if p["symbol"] != symbol:
            continue
        if p["price_at_60"] is not None:
            continue
        if ts - p["time"] >= ENTRY_DELAY:
            p["price_at_60"] = current_price


# ============================================================
# ЗАКРЫТЬ ПАТТЕРНЫ (T+240)
# ============================================================

def resolve_patterns(symbol, current_price, ts):
    """
    Закрывает паттерны через 240 сек.
    Результат считается от price_at_60 до цены на T+240.
    Движение < MIN_PIPS → result = "noise".
    """
    global _unsaved_count

    pip          = PIP_SIZE.get(symbol, 0.0001)
    still_active = []

    for p in active_patterns:

        if p["symbol"] != symbol:
            still_active.append(p)
            continue

        if ts - p["time"] >= EXPIRY_DELAY:

            # Если price_at_60 не записалась — используем цену сигнала
            entry_price = p["price_at_60"] if p["price_at_60"] else p["price"]

            move      = current_price - entry_price
            move_pips = abs(move) / pip

            p["move_pips"] = round(move_pips, 1)
            p["resolved"]  = True

            if move_pips < MIN_PIPS:
                p["result"] = "noise"
            else:
                p["result"] = "up" if move > 0 else "down"

            memory.append(p)
            _unsaved_count += 1

            if _unsaved_count >= AUTOSAVE_EVERY:
                save_memory()
        else:
            still_active.append(p)

    active_patterns[:] = still_active

