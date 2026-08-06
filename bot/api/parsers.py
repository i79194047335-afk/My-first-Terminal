"""
Разбор ответов intrade.bar. Python 3.10.

Площадка отдаёт HTML и plain text, а не JSON: открытие сделки возвращает
строку таблицы, баланс — текст вида "9 363,08 $", ошибки — голые строки.
Разбор поэтому хрупкий, и весь он собран в одном модуле, чтобы при смене
их вёрстки чинить одно место, а не искать regexp по всему проекту.

Правила, которых держится этот модуль:

1. Ни одна функция не ходит в сеть. Вход — строка ответа, выход — dataclass.
   Так тесты гоняются на сохранённых реальных ответах (tests/fixtures/intrade/).
2. Ошибку площадки распознаём ДО того, как считать сделку открытой. Пустой
   ответ и строка с подстрокой "error" — это отказ, а не пустая сделка.
3. Не угадывать. Если форма ответа неожиданная — PlatformError или outcome
   "unknown", но не додуманное значение. Сырой ответ всегда сохраняется
   в raw: при разборе сделки на реальные деньги догадка дороже отказа.

Разбор сделан регулярками, а не BeautifulSoup, намеренно: нужные значения
лежат в атрибутах тега и в аргументах вызова setInterval внутри <script>,
то есть внутри текста скрипта, куда HTML-парсер всё равно не заглядывает.
"""

from __future__ import annotations

import re
from typing import Optional

from bot.api.models import (
    STATUS_TO_DIRECTION,
    AccountProfile,
    Balance,
    PlatformError,
    Quote,
    Trade,
    TradeResult,
)

# Известные текстовые ошибки площадки. Ключ — подстрока ответа.
# Список из HAR-записи и JS торговой страницы; всё неизвестное с подстрокой
# "error" попадёт в общий случай ниже и тоже станет отказом.
ERROR_MESSAGES = {
    "error_time_night_order": "ночная сделка запрещена",
    "error_time_night": "ночью торгуется только BTC/USDT",
    "error_time_18": "крайнее время закрытия сделки — 18:00 МСК",
}

# Атрибуты строки таблицы, которую площадка возвращает при успехе.
_RE_DATA_ID = re.compile(r'data-id="(\d+)"')
_RE_DATA_RATE = re.compile(r'data-rate="([\d.]+)"')
_RE_DATA_TIMEOPEN = re.compile(r'data-timeopen="(\d+)"')
_RE_DATA_OPTION = re.compile(r'data-option="([A-Za-z/]+)"')
_RE_DATA_STATUS = re.compile(r'data-status="(\d)"')

# time_time_<id> = 60;  — длительность сделки в секундах.
_RE_DURATION = re.compile(r"time_time_\d+\s*=\s*(\d+)")

# Последний аргумент setInterval — unix-время экспирации. Аргументов семь,
# нужен седьмой; между ними бывают переводы строк, отсюда re.S.
_RE_EXPIRY = re.compile(
    r"setInterval\s*\([^)]*?,\s*(\d{9,})\s*\)", re.S
)

# Сумма ставки: graph_create_order(id, status, rate, "2 $").
_RE_INVEST = re.compile(r'graph_create_order\([^)]*?"([\d.,\s ]+)\s*\S*"\s*\)')


def check_error(text: str) -> None:
    """Проверить ответ площадки на отказ и бросить PlatformError.

    Зовётся ПЕРВОЙ строкой любого разбора. Площадка всегда отвечает HTTP 200,
    поэтому единственный признак отказа — тело ответа: пустая строка либо
    текст с подстрокой "error".

    Args:
        text: Сырой ответ площадки.

    Returns:
        None, если ответ похож на успешный.

    Raises:
        PlatformError: Ответ пуст или содержит признак ошибки.
    """
    stripped = (text or "").strip()

    if not stripped:
        raise PlatformError(
            code="empty",
            message="пустой ответ (фронт площадки показывает «Повторите попытку»)",
            raw=text or "",
        )

    lowered = stripped.lower()
    for marker, message in ERROR_MESSAGES.items():
        if marker in lowered:
            raise PlatformError(code=marker, message=message, raw=stripped)

    # Неизвестная ошибка: подстрока "error" есть, конкретный код незнаком.
    # Отдельная ветка нужна, чтобы новый код ошибки не был принят за HTML
    # успешной сделки: строка таблицы начинается с "<tr", а не со слова error.
    if "error" in lowered and "<tr" not in lowered:
        raise PlatformError(
            code="unknown_error",
            message="незнакомый код ошибки площадки",
            raw=stripped,
        )


def parse_trade_open(text: str, symbol: str, investment: float) -> Trade:
    """Разобрать ответ ajax5_new.php в объект Trade.

    Площадка возвращает строку таблицы со всеми нужными полями в атрибутах
    и в инлайн-скрипте. symbol и investment передаются снаружи: в ответе
    инструмент приходит без слэша (USDJPY), а канонический вид со слэшем
    известен вызывающему, и восстанавливать его обратной эвристикой хуже,
    чем передать.

    Args:
        text:       Сырой ответ площадки.
        symbol:     Канонический символ со слэшем ("USD/JPY").
        investment: Размер ставки, как он был отправлен.

    Returns:
        Trade с trade_id, ценой входа, временем открытия и экспирации.

    Raises:
        PlatformError: Ответ — отказ либо в нём нет обязательных полей.
    """
    check_error(text)

    trade_id = _search_int(_RE_DATA_ID, text, "data-id")
    entry_price = _search_float(_RE_DATA_RATE, text, "data-rate")
    open_ts = _search_int(_RE_DATA_TIMEOPEN, text, "data-timeopen")

    status_match = _RE_DATA_STATUS.search(text)
    direction = STATUS_TO_DIRECTION.get(int(status_match.group(1))) if status_match else None
    if direction is None:
        raise PlatformError(
            code="parse_failed",
            message="в ответе нет распознаваемого data-status (1=call, 2=put)",
            raw=text,
        )

    duration_match = _RE_DURATION.search(text)
    expiry_match = _RE_EXPIRY.search(text)

    # Экспирация и длительность подстраховывают друг друга: любое из двух
    # значений позволяет вычислить второе от времени открытия. Если нет
    # обоих — считать сделку разобранной нельзя, ждать расчёта будет нечем.
    duration = int(duration_match.group(1)) if duration_match else None
    expiry_ts = int(expiry_match.group(1)) if expiry_match else None

    if expiry_ts is None and duration is None:
        raise PlatformError(
            code="parse_failed",
            message="в ответе нет ни времени экспирации, ни длительности сделки",
            raw=text,
        )
    if expiry_ts is None:
        expiry_ts = open_ts + duration
    if duration is None:
        duration = expiry_ts - open_ts

    return Trade(
        trade_id=trade_id,
        symbol=symbol,
        direction=direction,
        investment=investment,
        entry_price=entry_price,
        open_ts=open_ts,
        expiry_ts=expiry_ts,
        duration=duration,
        raw=text,
    )


def parse_balance(text: str) -> Balance:
    """Разобрать ответ balance.php.

    Формат — "9 363,08 $": пробелы (обычные или неразрывные) как разделители
    тысяч, запятая как десятичный разделитель. Всё это надо снять до float().

    Args:
        text: Сырой ответ площадки.

    Returns:
        Balance с числом, валютой и исходной строкой.

    Raises:
        PlatformError: Ответ — отказ либо число не распознано.
    """
    check_error(text)
    raw = text.strip()

    # Валюта — всё, что не цифра, не разделитель и не пробел.
    currency = "".join(ch for ch in raw if not (ch.isdigit() or ch in ".,   \t\n"))
    currency = currency.strip()

    cleaned = raw
    for ch in (" ", " ", "\t", "\n"):
        cleaned = cleaned.replace(ch, "")
    cleaned = cleaned.replace(",", ".")
    # Оставляем только цифры, точку и минус — валюта и прочий мусор уходят.
    cleaned = re.sub(r"[^\d.\-]", "", cleaned)

    try:
        amount = float(cleaned)
    except ValueError:
        raise PlatformError(
            code="parse_failed",
            message="баланс не разобран в число",
            raw=raw,
        )

    return Balance(amount=amount, currency=currency, raw=raw)


def parse_percent(text: str) -> int:
    """Разобрать ответ ajax_percent.php — актуальный процент выплаты.

    В запрос уходит процент, известный фронту, а сервер возвращает свой,
    настоящий (наблюдалось 79 → 82). Поэтому значение обязательно
    перезапрашивается перед каждой ставкой, а не кэшируется надолго.

    Args:
        text: Сырой ответ площадки (обычно просто "82").

    Returns:
        Процент выплаты (int).

    Raises:
        PlatformError: Ответ — отказ либо число не распознано.
    """
    check_error(text)
    match = re.search(r"\d+", text)
    if not match:
        raise PlatformError(
            code="parse_failed",
            message="процент выплаты не разобран в число",
            raw=text.strip(),
        )
    return int(match.group(0))


def parse_quotes(payload: dict) -> dict:
    """Преобразовать JSON price_now в словарь котировок.

    Единственный ответ площадки, который приходит нормальным JSON, поэтому
    разбор тривиален. Символы здесь СО СЛЭШЕМ ("USD/JPY") — в торговых
    запросах они без него, маппинг делает client.

    Args:
        payload: Разобранный JSON: {"AUD/USD": {"Updates":…, "ask":…, "bid":…}}.

    Returns:
        {символ: Quote}. Записи без bid/ask пропускаются молча — площадка
        изредка отдаёт неполные строки, и падать из-за одной пары нельзя.
    """
    quotes = {}
    for symbol, row in (payload or {}).items():
        if not isinstance(row, dict):
            continue
        try:
            quotes[symbol] = Quote(
                symbol=symbol,
                bid=float(row["bid"]),
                ask=float(row["ask"]),
                updated=int(row.get("Updates", 0)),
            )
        except (KeyError, TypeError, ValueError):
            continue
    return quotes


def parse_trade_check(text: str, trade_id: int, investment: float) -> TradeResult:
    """Разобрать ответ trade_check2.php — итог рассчитанной сделки.

    Формат "invest_close;total_close;total" известен из JS площадки, но
    ЖИВЬЁМ НЕ ПОДТВЕРЖДЁН: в HAR-записи сессия кончилась раньше экспирации.
    Поэтому при любой неожиданности возвращается outcome "unknown" с сырым
    ответом внутри — гадать на деньгах нельзя. Первая же реальная сделка
    в demo покажет настоящий формат, и функцию надо будет сверить с ним.

    Args:
        text:       Сырой ответ площадки.
        trade_id:   Идентификатор сделки.
        investment: Размер ставки — нужен, чтобы отличить возврат от выигрыша.

    Returns:
        TradeResult. outcome "unknown", если формат не распознан.
    """
    raw = (text or "").strip()

    # Сделка ещё не рассчитана — пустой ответ здесь НЕ ошибка, в отличие от
    # открытия: площадке просто нечего сказать до экспирации.
    if not raw:
        return TradeResult(trade_id=trade_id, outcome="unknown", raw=raw)

    parts = raw.split(";")
    numbers = []
    for part in parts:
        cleaned = part.strip().replace(",", ".")
        cleaned = re.sub(r"[^\d.\-]", "", cleaned)
        try:
            numbers.append(float(cleaned))
        except ValueError:
            numbers.append(None)

    if len(numbers) < 2 or numbers[0] is None or numbers[1] is None:
        return TradeResult(trade_id=trade_id, outcome="unknown", raw=raw)

    invest_close, total_close = numbers[0], numbers[1]
    total = numbers[2] if len(numbers) > 2 else None

    # total_close — то, что вернулось на баланс по этой сделке.
    # Больше ставки — выигрыш, равно ставке — возврат, меньше — проигрыш.
    # Сравнение с допуском: суммы дробные, точное равенство ненадёжно.
    pnl = total_close - investment
    if abs(pnl) < 0.005:
        outcome = "refund"
    elif pnl > 0:
        outcome = "win"
    else:
        outcome = "loss"

    return TradeResult(
        trade_id=trade_id,
        outcome=outcome,
        invest_close=invest_close,
        total_close=total_close,
        total=total,
        pnl=round(pnl, 2),
        raw=raw,
    )


def extract_credentials(html: str) -> tuple:
    """Выковырять user_id и user_hash из HTML торговой страницы.

    После входа площадка вшивает их в страницу JS-переменными:
        var user_id = 30169;
        var user_hash = "…32 hex…";
    Этой пары достаточно для всех прочих вызовов — cookie не нужны.

    Args:
        html: HTML торговой страницы целиком.

    Returns:
        Кортеж (user_id, user_hash); элемент None, если не найден.
    """
    id_match = re.search(r"user_id\s*=\s*[\"']?(\d+)", html or "")
    hash_match = re.search(r"user_hash\s*=\s*[\"']([0-9a-fA-F]{16,})[\"']", html or "")
    user_id = int(id_match.group(1)) if id_match else None
    user_hash = hash_match.group(1) if hash_match else None
    return user_id, user_hash


def _search_int(pattern: re.Pattern, text: str, field: str) -> int:
    """Найти целое по регулярке или сообщить, какого поля не хватило.

    Args:
        pattern: Скомпилированная регулярка с одной группой.
        text:    Текст ответа.
        field:   Имя поля для сообщения об ошибке.

    Returns:
        Найденное целое.

    Raises:
        PlatformError: Поле не найдено.
    """
    match = pattern.search(text)
    if not match:
        raise PlatformError(
            code="parse_failed",
            message=f"в ответе нет поля {field}",
            raw=text,
        )
    return int(match.group(1))


def _search_float(pattern: re.Pattern, text: str, field: str) -> float:
    """Найти дробное по регулярке или сообщить, какого поля не хватило.

    Args:
        pattern: Скомпилированная регулярка с одной группой.
        text:    Текст ответа.
        field:   Имя поля для сообщения об ошибке.

    Returns:
        Найденное число.

    Raises:
        PlatformError: Поле не найдено.
    """
    match = pattern.search(text)
    if not match:
        raise PlatformError(
            code="parse_failed",
            message=f"в ответе нет поля {field}",
            raw=text,
        )
    return float(match.group(1))


# ── профиль аккаунта ────────────────────────────────────────────────────
#
# /profile — единственное место, где виден тип счёта (см. INTRADE_API_MAP.md
# §3.1). Каждая настройка — пара radio с фиксированными id; сервер рендерит
# checked="checked" на АКТИВНОМ варианте, а onclick вешает на противоположный
# (тот, куда переключит). Перепутать эти два признака = определить режим
# наоборот и открыть сделку на реале, считая её демо. Поэтому разбор смотрит
# ТОЛЬКО на checked и требует ровно одного отмеченного в паре.
#
# id пар (из разметки /profile, разбор 2026-08-06):
#   personal-radio1 / personal-radio2  — Реал / Демо
#   personal-radio3 / personal-radio4  — Classic / Sprint
#   personal-radio9 / personal-radio10 — USD / RUB
_PROFILE_RADIO_PAIRS = {
    "account": (("personal-radio1", "real"), ("personal-radio2", "demo")),
    "trade_type": (("personal-radio3", "classic"), ("personal-radio4", "sprint")),
    "currency": (("personal-radio9", "usd"), ("personal-radio10", "rub")),
}


def _radio_checked(html: str, radio_id: str) -> Optional[bool]:
    """Определить, отмечен ли radio с данным id.

    Args:
        html:     HTML страницы /profile.
        radio_id: Значение атрибута id тега input.

    Returns:
        True/False, либо None, если тега с таким id в разметке нет.
    """
    # Тег ищем по id; атрибуты внутри тега могут идти в любом порядке.
    match = re.search(
        r'<input\b[^>]*\bid="%s"[^>]*>' % re.escape(radio_id), html)
    if not match:
        return None
    return "checked" in match.group(0)


def parse_profile(html: str) -> "AccountProfile":
    """Разобрать /profile: тип счёта, тип сделок, валюта.

    Не угадывает. Для каждой пары radio требуется РОВНО ОДИН отмеченный
    вариант: оба пустых, оба отмеченных или отсутствие тегов — отказ.
    Цена догадки здесь — ставка реальными деньгами при конфиге "demo",
    поэтому любая неожиданность в разметке останавливает торговлю.

    Args:
        html: HTML страницы /profile.

    Returns:
        AccountProfile.

    Raises:
        PlatformError: Разметка не позволяет определить состояние однозначно.
    """
    resolved = {}
    for field, ((id_a, val_a), (id_b, val_b)) in _PROFILE_RADIO_PAIRS.items():
        checked_a = _radio_checked(html, id_a)
        checked_b = _radio_checked(html, id_b)

        if checked_a is None or checked_b is None:
            raise PlatformError(
                code="profile_layout_changed",
                message=(
                    f"в /profile нет пары radio для «{field}» "
                    f"({id_a}/{id_b}) — вёрстка изменилась, разбор ненадёжен"
                ),
                raw=html[:2000],
            )
        if checked_a == checked_b:
            # Оба отмечены или оба пусты — состояние неопределимо.
            raise PlatformError(
                code="profile_ambiguous",
                message=(
                    f"состояние «{field}» неоднозначно: "
                    f"{id_a}={checked_a}, {id_b}={checked_b}"
                ),
                raw=html[:2000],
            )
        resolved[field] = val_a if checked_a else val_b

    return AccountProfile(
        account=resolved["account"],
        trade_type=resolved["trade_type"],
        currency=resolved["currency"],
    )
