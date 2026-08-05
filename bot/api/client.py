"""
HTTP-клиент intrade.bar. Python 3.10.

Единственный модуль, который ходит к площадке. Всё, что он умеет, — послать
запрос и отдать разобранный parsers.py результат; решений он не принимает.

Три вещи, которые здесь важнее остального:

1. ОТКРЫТИЕ СДЕЛКИ НЕ РЕТРАИТСЯ. Никогда. Ответ мог дойти до площадки и
   потеряться по дороге обратно — повтор означает двойную ставку на реальные
   деньги. При сетевом сбое на open_trade поднимается TradeUnknown, и
   разбираться с этим должен исполнитель сверкой через active_trades(),
   а не слепым повтором. Ретраи разрешены только на чтении (баланс, процент,
   котировки), где повтор безвреден.

2. Домены смешаны. Торговля на base_url (intrade35.bar), котировки на
   quotes_url (intrade.bar, без «35»). Проверено живьём — не «исправлять».

3. Символы в двух формах. В котировках со слэшем ("USD/JPY"), в торговых
   запросах без ("USDJPY"). Преобразование — to_platform_symbol.

Cookie не используются: площадке достаточно пары user_id + user_hash в теле
формы (проверено HAR-записью). Сессия requests держится ради keep-alive.
"""

from __future__ import annotations

import time
from typing import Optional

import requests

from bot.api.models import (
    DIRECTION_TO_STATUS,
    Balance,
    Credentials,
    PlatformError,
    Quote,
    Trade,
    TradeResult,
    mask_hash,
)
from bot.api import parsers
from core.logfmt import setup as _log_setup

log = _log_setup("bot-api")

# Заголовки повторяют то, что шлёт их собственный фронт. X-Requested-With
# обязателен: без него ajax-эндпоинты отвечают иначе.
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "*/*",
}


class TradeUnknown(Exception):
    """Судьба запроса на открытие сделки неизвестна.

    Поднимается, когда запрос ушёл, а ответ не получен (таймаут, обрыв).
    Отдельный тип нужен, чтобы исполнитель НЕ МОГ перепутать этот случай с
    обычной ошибкой и не повторил ставку: сделка могла открыться.
    """


class IntradeClient:
    """Клиент HTTP API площадки.

    Хранит учётные данные и сессию requests. Потокобезопасностью не обладает:
    предполагается один экземпляр в одном asyncio-цикле, сетевые вызовы
    выполняются в executor'е (см. engine.py).
    """

    def __init__(
        self,
        base_url: str = "https://intrade35.bar",
        quotes_url: str = "https://intrade.bar/price_now",
        credentials: Optional[Credentials] = None,
        timeout: float = 15.0,
    ):
        """Создать клиент.

        Args:
            base_url:    Торговый домен без хвостового слэша.
            quotes_url:  Полный URL котировок (ДРУГОЙ домен).
            credentials: Пара user_id/user_hash; можно задать позже.
            timeout:     Таймаут одного запроса, секунды.
        """
        self.base_url = base_url.rstrip("/")
        self.quotes_url = quotes_url
        self.credentials = credentials
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)

    # ── вспомогательное ────────────────────────────────────────────────

    def _auth_fields(self) -> dict:
        """Собрать поля авторизации для формы.

        Returns:
            Словарь с user_id и user_hash.

        Raises:
            PlatformError: Учётные данные не заданы.
        """
        if not self.credentials:
            raise PlatformError(
                code="no_credentials",
                message="не заданы user_id/user_hash — сначала авторизация",
            )
        return {
            "user_id": str(self.credentials.user_id),
            "user_hash": self.credentials.user_hash,
        }

    def _post(self, path: str, data: dict, retries: int = 2) -> str:
        """Выполнить POST к торговому домену.

        ВНИМАНИЕ: сюда можно отдавать только идемпотентные вызовы. Открытие
        сделки идёт мимо этого метода (см. open_trade) именно потому, что
        здесь есть повторы.

        Args:
            path:    Путь вида "/balance.php".
            data:    Поля формы.
            retries: Сколько раз повторить при сетевой ошибке.

        Returns:
            Тело ответа строкой.

        Raises:
            PlatformError: Сеть не отдала ответ за все попытки.
        """
        url = f"{self.base_url}{path}"
        last_error = None

        for attempt in range(retries + 1):
            try:
                response = self.session.post(url, data=data, timeout=self.timeout)
                response.raise_for_status()
                return response.text
            except requests.RequestException as err:
                last_error = err
                if attempt < retries:
                    # Линейная пауза: площадка маленькая, рейт-лимиты
                    # неизвестны, агрессивно долбить её нельзя.
                    time.sleep(0.5 * (attempt + 1))

        raise PlatformError(
            code="network",
            message=f"{path}: сеть не ответила ({last_error})",
        )

    # ── чтение ─────────────────────────────────────────────────────────

    def balance(self) -> Balance:
        """Запросить баланс счёта.

        Returns:
            Balance с числом и исходной строкой.

        Raises:
            PlatformError: Отказ площадки или неразобранный ответ.
        """
        text = self._post("/balance.php", self._auth_fields())
        return parsers.parse_balance(text)

    def payout_percent(
        self,
        symbol: str,
        expiry_minutes: int = 1,
        investment: float = 1,
        trade_type: str = "Sprint",
        currency: str = "USD",
    ) -> int:
        """Запросить актуальный процент выплаты по инструменту.

        Значение плавает (наблюдалось 79 → 82), поэтому перезапрашивается
        перед каждой ставкой, а не кэшируется. При выплате 82% безубыточный
        винрейт равен 100/182 = 54.9% — ограничитель min_payout_percent
        существует именно поэтому.

        Args:
            symbol:         Канонический символ со слэшем ("USD/JPY").
            expiry_minutes: Экспирация в минутах.
            investment:     Размер ставки.
            trade_type:     "Sprint" или "Classic".
            currency:       Валюта счёта.

        Returns:
            Процент выплаты (int).

        Raises:
            PlatformError: Отказ площадки или неразобранный ответ.
        """
        data = {
            "type": trade_type,
            "time": str(expiry_minutes),
            "currency_name": currency,
            "investment": str(investment),
            # Площадка ждёт процент, известный фронту, и возвращает свой.
            # Ноль означает «мне нечего предложить, скажи актуальный».
            "percent": "0",
            "option": to_platform_symbol(symbol),
        }
        return parsers.parse_percent(self._post("/ajax_percent.php", data))

    def quotes(self) -> dict:
        """Скачать текущие котировки всех пар.

        Ходит на ДРУГОЙ домен (quotes_url) и не требует авторизации.
        Опрашивать не чаще раза в секунду — как это делает их фронт.

        Returns:
            {символ со слэшем: Quote}.

        Raises:
            PlatformError: Сеть или неразобранный JSON.
        """
        try:
            response = self.session.get(self.quotes_url, timeout=self.timeout)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as err:
            raise PlatformError(code="network", message=f"котировки: {err}")
        return parsers.parse_quotes(payload)

    def active_trades(self) -> str:
        """Запросить список активных сделок сырым текстом.

        Нужен для разрешения состояния UNKNOWN: если ответ на открытие
        потерялся, сверяемся здесь, а не повторяем ставку. Разбор HTML не
        делается намеренно — формат живьём не изучен, а для сверки по
        подстроке trade_id хватает сырого текста.

        Returns:
            Сырой ответ площадки.

        Raises:
            PlatformError: Отказ площадки.
        """
        return self._post("/user_real_trade.php", self._auth_fields())

    def check_trade(self, trade_id: int, investment: float) -> TradeResult:
        """Узнать итог сделки после экспирации.

        Формат ответа живьём не подтверждён, поэтому при неожиданной форме
        parsers вернёт outcome "unknown" — это штатный ответ «ещё не
        рассчитано либо формат другой», а не ошибка.

        Args:
            trade_id:   Идентификатор сделки.
            investment: Размер ставки — нужен для отличения возврата.

        Returns:
            TradeResult.

        Raises:
            PlatformError: Отказ площадки.
        """
        data = self._auth_fields()
        data["trade_id"] = str(trade_id)
        text = self._post("/trade_check2.php", data)
        return parsers.parse_trade_check(text, trade_id, investment)

    # ── торговля ───────────────────────────────────────────────────────

    def open_trade(
        self,
        symbol: str,
        direction: str,
        investment: float,
        expiry_minutes: int = 1,
        trade_type: str = "sprint",
    ) -> tuple:
        """Открыть сделку. БЕЗ РЕТРАЕВ — см. модульный docstring.

        Возвращает не только сделку, но и момент отправки запроса: разница
        между открытием по версии площадки и этим моментом и есть latency_ms,
        ключевая метрика задачи (замеры при разведке — 3559 и 2910 мс).

        Args:
            symbol:         Канонический символ со слэшем ("USD/JPY").
            direction:      "call" (вверх) или "put" (вниз).
            investment:     Размер ставки.
            expiry_minutes: Экспирация в минутах.
            trade_type:     "sprint" или "classic".

        Returns:
            Кортеж (Trade, request_ts) — сделка и unix-время отправки.

        Raises:
            PlatformError: Площадка отказала (разобранный код ошибки).
            TradeUnknown:  Ответ не получен — судьба ставки неизвестна.
            ValueError:    Неизвестное направление.
        """
        status = DIRECTION_TO_STATUS.get(direction)
        if status is None:
            raise ValueError(f"неизвестное направление: {direction!r}")

        data = self._auth_fields()
        data.update(
            {
                "option": to_platform_symbol(symbol),
                "investment": _format_amount(investment),
                "time": str(expiry_minutes),
                "date": "0",  # 0 для Sprint; для Classic — дата закрытия.
                "trade_type": trade_type,
                "status": str(status),
            }
        )

        log.info(
            "открытие сделки: %s %s %s, ставка %s, экспирация %d мин, hash %s",
            symbol,
            direction,
            trade_type,
            _format_amount(investment),
            expiry_minutes,
            mask_hash(self.credentials.user_hash if self.credentials else None),
        )

        request_ts = time.time()
        try:
            response = self.session.post(
                f"{self.base_url}/ajax5_new.php", data=data, timeout=self.timeout
            )
            response.raise_for_status()
        except requests.RequestException as err:
            # НЕ повторяем: запрос мог дойти. Пусть исполнитель сверяется.
            raise TradeUnknown(
                f"ответ на открытие сделки не получен ({err}); "
                "повтор запрещён — сверяйтесь через active_trades()"
            ) from err

        trade = parsers.parse_trade_open(response.text, symbol, investment)
        # round, а не int — как в journal.open_trade: цифра в логе и в
        # журнале должна быть одна и та же.
        latency_ms = round((trade.open_ts - request_ts) * 1000)
        log.info(
            "сделка %d открыта по %s, задержка %d мс, экспирация в %d",
            trade.trade_id,
            trade.entry_price,
            latency_ms,
            trade.expiry_ts,
        )
        return trade, request_ts


def to_platform_symbol(symbol: str) -> str:
    """Привести символ к виду торговых запросов (без слэша).

    В котировках площадки символы со слэшем ("USD/JPY"), в торговых запросах
    без него ("USDJPY"). Перепутать — получить отказ без внятного объяснения.

    Args:
        symbol: Символ в любом из двух видов.

    Returns:
        Символ без слэша, в верхнем регистре.
    """
    return symbol.replace("/", "").upper()


def to_canonical_symbol(symbol: str) -> str:
    """Привести символ к каноническому виду со слэшем.

    Обратная to_platform_symbol. Работает для шестибуквенных пар
    ("USDJPY" → "USD/JPY"); символы, где слэш уже есть, возвращаются как есть.

    Args:
        symbol: Символ в любом из двух видов.

    Returns:
        Символ со слэшем, в верхнем регистре.
    """
    value = symbol.upper()
    if "/" in value:
        return value
    if len(value) == 6:
        return f"{value[:3]}/{value[3:]}"
    return value


def _format_amount(value: float) -> str:
    """Отформатировать сумму для формы площадки.

    Целые суммы отправляем без дробной части ("2", а не "2.0") — именно так
    делает их фронт, а лишний ".0" на чужом парсере может дать отказ.

    Args:
        value: Сумма.

    Returns:
        Строка для поля формы.
    """
    if float(value).is_integer():
        return str(int(value))
    return f"{float(value):.2f}"
