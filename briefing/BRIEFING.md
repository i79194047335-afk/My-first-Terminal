# Briefing — подпроект

Пред-сессионный аналитический брифинг: **не советник**, а общая картина +
что говорят мировые агентства (консенсус) + мнение DeepSeek раздельно.
Крон 3×/сутки (04/12/17 локального = UTC+5) → `briefing.json` → хаб → панель.

## Модули

- `sources.py` — три вида источников:
  - **RSS** (`fetch_news`): живые фиды FXStreet/ForexLive/ActionForex + Bloomberg/
    WSJ/CNBC/FT, диагностика по каждому + порог «мало новостей». Заголовки.
  - **Аналитика** (`fetch_analysis`): полнотекстовые разборы через **newspaper3k**
    из разделов /analysis/ (FXStreet weekly forecasts, ING THINK house views) —
    невидимы для RSS. Даётся модели ВЫШЕ ленты как основа консенсуса. Точечно
    (3 статьи/источник), с fallback: сбой скрапинга → работаем на RSS. +~10с.
  - **Календарь** (`fetch_calendar`): ForexFactory `ff_calendar_thisweek.json`.
  Время везде в **UTC+5**.
- `technical.py` — техкартина из `market.db` (цена, диапазоны, тренд, уровни,
  волатильность). Read-only, перенос из старого движка 1:1.
- `memory.py` — журнал прогнозов (`data/briefing_journal.json`, окно 30 записей)
  + **самооценка**: сверка прошлого прогноза с фактом из `market.db`. Хранит и
  сверяет ДВЕ стороны — консенсус аналитиков и DeepSeek. `track_record` — счёт
  «кто чаще прав».
- `prompt.py` — сборка промпта. По каждой паре просит РАЗДЕЛЬНО: техкартину,
  консенсус аналитиков (`consensus_direction`+`consensus_view`), мысль DeepSeek
  (`direction`+`deepseek_view`, согласен/расхождение).
- `agent.py` — вызов DeepSeek (openai SDK, `api.deepseek.com`, ключ
  `DEEPSEEK_API_KEY`) + разбор JSON. Модель — **`deepseek-v4-pro`** (умнее flash:
  тоньше видит расхождения, глубже reasoning; ~80с/прогон, `max_tokens`=7500).
  Смена модели/канала — только здесь.
- `run.py` — точка входа. Запуск: `python3 -m briefing.run` из корня проекта.

## Формат briefing.json (ключевое)

`pairs[SYM]`: `technical_summary`, `consensus_direction`/`consensus_view`,
`direction`/`direction_confidence`/`deepseek_view`, `reasoning`,
`support_levels`/`resistance_levels`, `trend`, `key_events`, `watch_for`.
Плюс `currency_bias`, `global_context`, `track_record`, `meta` (в т.ч.
`news_by_feed`, `news_low`, `display_tz`).

## Как самооценка работает

Новый брифинг СНАЧАЛА сверяет прошлый прогноз с фактом (цена пошла в
предсказанную сторону? порог 5 пипс; уровни тестировались?), блок подаётся в
промпт — модель обязана честно признать несбывшееся. Вердикты копятся в журнал,
`track_record` за 7 дней показывает точность обеих сторон внизу панели.

## Тесты

`tests/test_briefing_{sources,memory,agent}.py` — 24 теста (юнит + живые
источники, скип без сети). Гонять: `python3 tests/test_briefing_*.py`.

## Известное

- Календарь ForexFactory: только `thisweek` (nextweek/thismonth = 404). При
  частых запросах — HTTP 429 (rate-limit); не критично, брифинг продолжает с
  events=[], ошибка в meta.calendar_error.
- Модель/канал/Telegram/набор пар — настройка «под себя» отложена (ROADMAP 7.2).
