"""
Точка входа брифинга — связывает источники, технику, память и агента.

Слой 3 подпроекта briefing/. Заменяет pre_session_brief.py в кроне. Порядок:
  1. определить сессию (по UTC-часу, как раньше);
  2. собрать данные — техника (market.db), новости+календарь (sources), блоки
     самооценки прошлых прогнозов (memory);
  3. промпт → DeepSeek (agent) → JSON брифинга;
  4. записать briefing.json (для хаба/фронта) + дописать журнал (memory);
  5. посчитать трек-рекорд и вложить в брифинг.

Запуск:  DEEPSEEK_API_KEY=... python3 -m briefing.run   (из корня проекта)

Пишет briefing.json атомарно (tmp+rename), чтобы хаб не прочитал полуфайл.
"""

import json
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

from briefing import memory, sources
from briefing.agent import AgentError, generate
from briefing.prompt import build_system_prompt, build_user_prompt
from briefing.technical import get_technical_context, SYMBOLS
from core.market_hours import forex_open

load_dotenv()

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
BRIEFING_FILE = os.path.join(_ROOT, "briefing.json")

SESSIONS = {
    "asia":   {"label_ru": "Азиатская",    "start_utc": "00:00", "end_utc": "08:00",
               "detect": (22, 23, 0, 1)},
    "london": {"label_ru": "Лондонская",   "start_utc": "08:00", "end_utc": "17:00",
               "detect": (6, 7, 8)},
    "ny":     {"label_ru": "Нью-Йоркская", "start_utc": "13:00", "end_utc": "22:00",
               "detect": (11, 12, 13)},
}


def detect_session():
    """Определить ближайшую сессию по текущему UTC-часу.

    Returns:
        Tuple (session_key, session_cfg).
    """
    hour = datetime.now(timezone.utc).hour
    for key, cfg in SESSIONS.items():
        if hour in cfg["detect"]:
            return key, cfg
    if 0 <= hour < 8:
        return "asia", SESSIONS["asia"]
    if 8 <= hour < 13:
        return "london", SESSIONS["london"]
    return "ny", SESSIONS["ny"]


def _write_json(path, data):
    """Атомарная запись JSON (tmp + rename)."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def main():
    """Сгенерировать брифинг и записать briefing.json + журнал.

    Returns:
        None. Код выхода 1 при фатальной ошибке (нет данных/ключа/ответа).
    """
    session_key, cfg = detect_session()
    now = datetime.now(timezone.utc)
    now_ts = now.timestamp()
    is_open = forex_open(now_ts)
    # В закрытый рынок (выходные) «текущей сессии» нет — брифинг становится
    # обзором К ОТКРЫТИЮ, а не описанием идущей сессии. detect_session даёт
    # ближайшую сессию по часу для контекста, но помечаем реальность.
    session_note = ("" if is_open
                    else " (рынок закрыт — обзор к открытию)")
    print("[brief] сессия: %s%s" % (cfg["label_ru"], session_note))

    # 1. Данные.
    technical = get_technical_context()
    if not technical["symbols"]:
        print("[brief] нет технических данных в market.db — выход")
        sys.exit(1)

    news, diag = sources.fetch_news()
    line, total, low = sources.news_summary(diag)
    print("[brief] новости: %s (всего %d, мало=%s)" % (line, total, low))

    calendar, cal_err = sources.fetch_calendar()
    print("[brief] календарь: %d событий%s"
          % (len(calendar), " (ошибка: %s)" % cal_err if cal_err else ""))

    # Глубокая аналитика (полнотекстовые разборы) — необязательная, с fallback.
    analysis, an_diag = sources.fetch_analysis()
    an_line = ", ".join("%s %s/%s" % (d["source"], d["fetched"], d["found"])
                        for d in an_diag)
    print("[brief] аналитика: %s (всего %d статей)" % (an_line, len(analysis)))

    # 2. Самооценка прошлых прогнозов (до генерации — модель учтёт).
    assessments = {sym: memory.format_assessment_for_prompt(sym, now_ts)
                   for sym in SYMBOLS}

    # 3. Промпт → DeepSeek.
    system_prompt = build_system_prompt(session_key, cfg["label_ru"],
                                        market_open=is_open)
    user_prompt = build_user_prompt(technical, news, diag, calendar,
                                    assessments, analysis_items=analysis)
    print("[brief] промпт %d символов, вызываю DeepSeek…" % len(user_prompt))
    try:
        data = generate(system_prompt, user_prompt)
    except AgentError as err:
        print("[brief] агент: %s" % err)
        sys.exit(1)

    if "pairs" not in data:
        print("[brief] в ответе нет 'pairs' — выход")
        sys.exit(1)

    # 4. Мета + флаги источников (фронт покажет «лента худая»).
    data["meta"] = {
        "session":        session_key,
        "session_label_ru": cfg["label_ru"],
        "session_start_utc": cfg["start_utc"],
        "session_end_utc":   cfg["end_utc"],
        "generated_at":   now.strftime("%Y-%m-%d %H:%M UTC"),
        "generated_ts":   now_ts,
        "market_open":    is_open,
        "session_note":   session_note.strip(" ()") or None,
        "news_total":     total,
        "news_low":       low,
        "news_by_feed":   line,
        "analysis_count": len(analysis),
        "analysis_by_source": an_line,
        "calendar_events": len(calendar),
        "calendar_error": cal_err,
        "display_tz":     sources.DISPLAY_TZ_LABEL,
    }

    # 5. Журнал + трек-рекорд (оба прогноза уже в data.pairs).
    memory.record_briefing(data)
    data["track_record"] = memory.track_record(SYMBOLS, now_ts, days=7)

    _write_json(BRIEFING_FILE, data)
    print("[brief] briefing.json записан (%d пар, трек-рекорд DS %s / консенсус %s)"
          % (len(data["pairs"]),
             data["track_record"]["deepseek"], data["track_record"]["consensus"]))


if __name__ == "__main__":
    main()
