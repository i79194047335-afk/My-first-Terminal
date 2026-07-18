"""
Сборка промпта для DeepSeek — новый формат: картина + консенсус + мысль модели.

Слой 3 подпроекта briefing/. Концепция (согласовано с владельцем): брифинг — не
советник. Ценность в общей картине и в том, ЧТО говорят мировые агентства, а
прогностика — побочный блок. Поэтому по каждой паре просим у модели ТРИ вещи
раздельно:
  1. картина (technical_summary) — что происходит;
  2. КОНСЕНСУС АНАЛИТИКОВ — bias, который агентства закладывают в своих текстах
     (consensus_direction + consensus_view), извлечённый из новостной ленты;
  3. МЫСЛЬ DEEPSEEK — где модель согласна с консенсусом, где расходится и почему
     (direction = мнение модели + deepseek_view).

Так внизу брифинга можно вести трек-рекорд обеих сторон (memory.track_record).

Формат вывода — расширение прежнего (старые поля сохранены, фронт не ломается).
"""

SESSION_CONTEXTS = {
    "asia": (
        "АЗИАТСКАЯ СЕССИЯ: низкая волатильность (20-40 пипсов), Tokyo 00-06 UTC, "
        "thin market (ложные пробои), драйверы — данные Японии, BOJ, carry trade. "
        "Новости после закрытия US (21 UTC) уже в цене."),
    "london": (
        "ЛОНДОНСКАЯ СЕССИЯ: высокая волатильность, самый ликвидный период, "
        "пересечения с Asia (08-09) и NY (13-17), драйверы — UK/EU данные, "
        "ECB, US pre-market. Часто задаёт направление на день."),
    "ny": (
        "НЬЮ-ЙОРКСКАЯ СЕССИЯ: высокая волатильность, US макро (CPI/GDP/NFP/FOMC), "
        "Fed speakers, DXY flows. Часто развороты от London-экстремумов. "
        "После 17 UTC ликвидность падает."),
}

SYMBOLS = ["EUR/USD", "USD/JPY", "AUD/USD", "USD/CAD"]


def build_system_prompt(session_key, session_label_ru):
    """Системный промпт: роль, сессия, СТРУКТУРА картина/консенсус/мысль.

    Args:
        session_key:      "asia"/"london"/"ny".
        session_label_ru: Русское имя сессии для роли.

    Returns:
        Строка системного промпта.
    """
    ctx = SESSION_CONTEXTS.get(session_key, SESSION_CONTEXTS["asia"])
    pairs = ", ".join(SYMBOLS)

    return """Ты — макро-аналитик %s торговой сессии. Пары: %s.

%s

ТВОЯ ЗАДАЧА — НЕ давать торговый совет, а дать КАРТИНУ и разделить два мнения:
что говорят МИРОВЫЕ АГЕНТСТВА (консенсус) и что думаешь ТЫ (где согласен, где нет).

Для КАЖДОЙ пары дай раздельно:
  1. technical_summary — техкартина по данным (цена, диапазоны, тренд, уровни).
  2. КОНСЕНСУС АНАЛИТИКОВ (consensus_direction + consensus_view): какое
     направление закладывают агентства (FXStreet, Bloomberg, WSJ, ForexLive и
     т.д.) в СВОИХ текстах из ленты ниже. Если ленты по паре нет — consensus_direction
     = "NEUTRAL", consensus_view объясни что источников мало.
  3. МЫСЛЬ DEEPSEEK (direction + deepseek_view): твоё направление и главное —
     СОГЛАСЕН ли ты с консенсусом. Если расходишься — прямо скажи «расхождение» и
     объясни, чего аналитики не учитывают (обычно техническую структуру).

ВСЕ ТЕКСТЫ — НА РУССКОМ. Называй катализаторы по именам (FOMC, BOJ, ECB, NFP,
конкретные агентства). Если дан блок «проверка прошлого прогноза» — учти его
честно: признай, если предыдущий прогноз не сбылся.

ФОРМАТ — строгий JSON (без markdown):

{
  "currency_bias": {"USD":"BULLISH|BEARISH|NEUTRAL", "EUR":"...", "JPY":"...", "AUD":"...", "CAD":"..."},
  "pairs": {
    "EUR/USD": {
      "technical_summary": "техкартина 180-240 симв: цена, D1/сессия диапазон, H4 тренд, позиция, уровни, волатильность",
      "consensus_direction": "UP|DOWN|NEUTRAL",
      "consensus_view": "150-250 симв: что говорят агентства, КОГО именно цитируешь из ленты, какой у них bias и почему",
      "direction": "UP|DOWN|NEUTRAL",
      "direction_confidence": 1-5,
      "deepseek_view": "150-250 симв: СОГЛАСЕН/РАСХОЖДЕНИЕ с консенсусом и почему; чего аналитики не видят",
      "reasoning": "200-350 симв: синтез картины — макро → катализаторы → техника",
      "support_levels": [x, y],
      "resistance_levels": [x, y],
      "trend": "bullish|bearish|neutral",
      "key_events": ["событие (время UTC+5)", ...],
      "watch_for": "200-300 симв: сценарии — пробой уровня X → ...; отскок → ...; что перевернёт картину"
    },
    "USD/JPY": {...}, "AUD/USD": {...}, "USD/CAD": {...}
  },
  "accumulated_context": {"EUR/USD": ["наблюдение для след. брифинга"], "USD/JPY":[...], "AUD/USD":[...], "USD/CAD":[...]},
  "global_context": {
    "key_events_today": ["главные события (время UTC+5)"],
    "risk_factors": ["конкретные риски, геополитика, что сломает bias"],
    "risk_appetite": "risk_on|risk_off|neutral",
    "session_volatility": "LOW|NORMAL|HIGH",
    "recommendation": "180-280 симв: общая картина сессии, где сильнее конвикшен, что игнорировать"
  }
}""" % (session_label_ru, pairs, ctx)


def _fmt_technical(sym, t):
    """Одна строка техкартины пары для промпта.

    Args:
        sym: Пара.
        t:   Dict из technical.get_technical_context()["symbols"][sym].

    Returns:
        Строка.
    """
    return ("%s: цена %.5f, D1 %.5f/%.5f (%.0f пипс), HTF %s, микротренд %s, "
            "поз %.2f, волат %s"
            % (sym, t["price"], t["day_high"], t["day_low"],
               t["day_range_pips"], t["htf_trend"], t["micro_trend"],
               t["range_pos"], t["volatility"]))


def build_user_prompt(technical, news_items, news_diag, calendar,
                      assessments):
    """Пользовательский промпт: техника + лента + календарь + самооценка.

    Args:
        technical:   get_technical_context() → {"symbols": {...}}.
        news_items:  Список новостей из sources.fetch_news()[0].
        news_diag:   Диагностика фидов (для флага «мало новостей»).
        calendar:    События из sources.fetch_calendar()[0].
        assessments: Dict {symbol: строка format_assessment_for_prompt} —
                     блоки самооценки прошлых прогнозов.

    Returns:
        Строка пользовательского промпта.
    """
    from .sources import news_summary

    parts = ["ТЕХНИЧЕСКАЯ КАРТИНА (из нашей БД):"]
    for sym in SYMBOLS:
        t = technical.get("symbols", {}).get(sym)
        if t:
            parts.append("  " + _fmt_technical(sym, t))

    # Проверка прошлых прогнозов — сразу за техникой, чтобы модель учла честно.
    prev = [a for a in (assessments or {}).values() if a]
    if prev:
        parts.append("\nПРОВЕРКА ПРОШЛЫХ ПРОГНОЗОВ (факт из БД):")
        for a in prev:
            parts.append("  " + a)

    # Новостная лента с источником и временем (UTC+5).
    line, total, low = news_summary(news_diag)
    parts.append("\nНОВОСТНАЯ ЛЕНТА (%d заголовков, %s):"
                 % (total, "ЛЕНТА ХУДАЯ — мало источников" if low else "ок"))
    for it in news_items[:40]:
        when = it.get("time_display") or "?"
        parts.append("  [%s] %s: %s" % (when, it["source"], it["title"]))

    # Экономический календарь.
    if calendar:
        parts.append("\nКАЛЕНДАРЬ (high-impact, время UTC+5):")
        for e in calendar:
            parts.append("  %s | %s | %s" % (e["time_display"], e["currency"], e["event"]))
    else:
        parts.append("\nКАЛЕНДАРЬ: high-impact событий в окне нет.")

    parts.append("\nДай брифинг строго в JSON-формате из системного промпта.")
    return "\n".join(parts)
