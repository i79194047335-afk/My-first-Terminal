"""
Типы данных площадки intrade.bar. Python 3.10.

Здесь только формы данных — ни сети, ни разбора HTML. Разбор живёт в
parsers.py, запросы в client.py. Разделение нужно, чтобы тесты парсеров
гонялись на сохранённых ответах площадки без единого сетевого вызова.

Все dataclass'ы неизменяемые (frozen): запись о сделке, однажды разобранная
из ответа, потом только читается и пишется в журнал. Случайная мутация в
машине состояний — источник трудноуловимых ошибок, поэтому она запрещена
на уровне типа.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

# Направление сделки в терминах площадки: status=1 Call (вверх), 2 Put (вниз).
# В коде везде оперируем словами, число появляется только в момент отправки
# запроса (client.open_trade) — иначе 1 и 2 расползутся по всей кодовой базе
# и перепутать их станет вопросом времени.
Direction = Literal["call", "put"]

DIRECTION_TO_STATUS = {"call": 1, "put": 2}
STATUS_TO_DIRECTION = {1: "call", 2: "put"}


@dataclass(frozen=True)
class Quote:
    """Котировка одной пары из price_now.

    Attributes:
        symbol:  Канонический вид со слэшем ("USD/JPY").
        bid:     Цена покупателя.
        ask:     Цена продавца.
        updated: Unix-время (сек) последнего обновления по версии площадки.
    """

    symbol: str
    bid: float
    ask: float
    updated: int

    @property
    def mid(self) -> float:
        """Середина спреда.

        Наш терминал строит рэндж-бары по mid, и для сопоставления сигнала с
        ценой площадки удобнее та же величина. Расчёт опциона при этом идёт
        по цене площадки, а не по mid — см. entry_price в Trade.

        Returns:
            Полусумма bid и ask (float).
        """
        return (self.bid + self.ask) / 2.0


@dataclass(frozen=True)
class Balance:
    """Баланс счёта.

    Площадка отдаёт баланс строкой вида "9 363,08 $" — с неразрывными
    пробелами и запятой как разделителем дробной части. Храним и разобранное
    число, и исходную строку: если разбор однажды сломается на смене формата,
    сырой текст в журнале покажет, что именно пришло.

    Attributes:
        amount:   Число (float).
        currency: Валюта, если её удалось выделить ("$", "RUB", …).
        raw:      Исходная строка ответа целиком.
    """

    amount: float
    currency: str
    raw: str


@dataclass(frozen=True)
class Trade:
    """Открытая сделка — то, что вернула площадка в ответ на ajax5_new.php.

    Ключевое поле — open_ts: цена входа фиксируется в момент ОТКРЫТИЯ, а не
    отправки запроса, и между ними ~3 секунды. Разница open_ts − request_ts
    и есть latency_ms, главная метрика этой задачи.

    Attributes:
        trade_id:    Идентификатор площадки (data-id).
        symbol:      Канонический вид со слэшем ("USD/JPY").
        direction:   "call" / "put".
        investment:  Размер ставки.
        entry_price: Цена входа (data-rate), зафиксирована площадкой.
        open_ts:     Unix-время открытия (data-timeopen).
        expiry_ts:   Unix-время экспирации (последний аргумент setInterval).
        duration:    Длительность в секундах (time_time_<id>).
        raw:         Сырой ответ площадки целиком — для журнала.
    """

    trade_id: int
    symbol: str
    direction: Direction
    investment: float
    entry_price: float
    open_ts: int
    expiry_ts: int
    duration: int
    raw: str = ""


@dataclass(frozen=True)
class TradeResult:
    """Итог рассчитанной сделки — разбор ответа trade_check2.php.

    Формат ответа: "invest_close;total_close;total". Живьём на расчёте пока
    не подтверждён (см. «Что осталось выяснить» в INTRADE_API_MAP.md), поэтому
    parsers.parse_trade_check при неожиданной форме возвращает outcome
    "unknown", а не гадает.

    Attributes:
        trade_id:     Идентификатор площадки.
        outcome:      "win" / "loss" / "refund" / "unknown".
        invest_close: Возврат по ставке, как его называет площадка.
        total_close:  Итог по сделке.
        total:        Баланс после расчёта.
        pnl:          Прибыль/убыток относительно ставки.
        raw:          Сырой ответ целиком.
    """

    trade_id: int
    outcome: Literal["win", "loss", "refund", "unknown"]
    invest_close: Optional[float] = None
    total_close: Optional[float] = None
    total: Optional[float] = None
    pnl: Optional[float] = None
    raw: str = ""


@dataclass(frozen=True)
class Credentials:
    """Пара, которой авторизуются все вызовы кроме /login.

    Cookie площадке не нужны — проверено HAR-записью: заголовка Cookie в
    запросах нет вовсе. user_hash это фактически ключ от аккаунта, поэтому
    __repr__ переопределён: иначе хеш утечёт в лог при первом же
    логировании структуры целиком.

    Attributes:
        user_id:   Числовой идентификатор пользователя.
        user_hash: 32-символьный hex.
    """

    user_id: int
    user_hash: str

    def __repr__(self) -> str:
        """Представление без утечки хеша.

        Returns:
            Строка с user_id и первыми 6 символами хеша.
        """
        return f"Credentials(user_id={self.user_id}, user_hash={mask_hash(self.user_hash)})"


def mask_hash(value: Optional[str]) -> str:
    """Обрезать секрет до безопасного для логов вида.

    По ТЗ в логи попадают только первые 6 символов user_hash. Функция живёт
    здесь, а не в client.py, потому что маскировать хеш приходится и при
    разборе, и при записи в журнал, и в панели.

    Args:
        value: Секрет или None.

    Returns:
        Строка вида "a1b2c3…" либо "<none>", если значения нет.
    """
    if not value:
        return "<none>"
    return f"{value[:6]}…"


@dataclass(frozen=True)
class PlatformError(Exception):
    """Отказ площадки, распознанный по её собственному ответу.

    Площадка сообщает об ошибке не кодом HTTP (там всегда 200), а телом:
    пустой строкой либо текстом с подстрокой "error". Поэтому обычные
    исключения requests тут ни при чём — нужен свой тип.

    Attributes:
        code:    Машинный код: "empty", "error_time_18", "error_time_night", …
        message: Человеческое описание.
        raw:     Сырой ответ целиком.
    """

    code: str
    message: str
    raw: str = ""

    def __str__(self) -> str:
        """Текст исключения.

        Returns:
            Строка вида "error_time_18: крайнее время закрытия — 18:00 МСК".
        """
        return f"{self.code}: {self.message}"


@dataclass(frozen=True)
class AccountProfile:
    """Состояние настроек аккаунта, прочитанное из /profile.

    Единственный способ узнать тип счёта: API площадки — набор «голых»
    тумблеров без параметра (см. INTRADE_API_MAP.md §3.1), и спросить
    «какой счёт активен» больше негде. Сервер рендерит checked="checked"
    на АКТИВНОМ варианте пары radio, а onclick вешает на противоположный.

    Значения строго из закрытого набора; при любой неоднозначности разметки
    парсер обязан поднять PlatformError, а не заполнять поле догадкой —
    перепутанный режим счёта означает ставку реальными деньгами.

    Attributes:
        account:    "real" либо "demo" — тип активного счёта.
        trade_type: "classic" либо "sprint" — тип сделок уровня аккаунта.
        currency:   "usd" либо "rub" — валюта счёта.
    """

    account: Literal["real", "demo"]
    trade_type: Literal["classic", "sprint"]
    currency: Literal["usd", "rub"]
