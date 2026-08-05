"""
Ручной источник сигналов. Python 3.10.

Сигнал возникает по команде человека — кнопкой Call/Put в панели наблюдения
(Слой 6) или вызовом fire() из кода. Это ЭТАЛОННАЯ реализация SignalSource:
самая простая из возможных, и по ней проверяется, что контракт годен.

Заодно это рабочий инструмент: именно им на Слое 7 будут открываться первые
20 сделок на демо-счёте для замера задержки. Стратегии для этого не нужно —
направление выбирает человек, а измеряется поведение площадки.

Логики принятия решения здесь нет и быть не может: модуль только упаковывает
чужое решение в Signal.
"""

from __future__ import annotations

import time
from typing import Optional

from bot.strategy.base import Signal, SignalSource
from core.logfmt import setup as _log_setup

log = _log_setup("bot-manual")


class ManualSource(SignalSource):
    """Источник, порождающий сигналы по команде человека.

    Attributes:
        name: Всегда "manual" — под этим именем сигналы попадут в журнал.
    """

    name = "manual"

    def __init__(
        self,
        default_symbol: str = "USD/JPY",
        default_expiry_minutes: int = 1,
        default_amount: Optional[float] = None,
    ):
        """Создать ручной источник.

        Args:
            default_symbol:         Инструмент, если не указан при вызове.
            default_expiry_minutes: Экспирация по умолчанию, минуты.
            default_amount:         Ставка по умолчанию; None — из конфига.
        """
        super().__init__()
        self.default_symbol = default_symbol
        self.default_expiry_minutes = default_expiry_minutes
        self.default_amount = default_amount
        self.fired = 0

    async def fire(
        self,
        direction: str,
        symbol: Optional[str] = None,
        expiry_minutes: Optional[int] = None,
        amount: Optional[float] = None,
        note: str = "",
    ) -> Optional[Signal]:
        """Породить сигнал прямо сейчас.

        Зовётся обработчиком кнопки в панели либо из теста.

        Args:
            direction:      "call" или "put".
            symbol:         Инструмент; None — умолчание источника.
            expiry_minutes: Экспирация; None — умолчание источника.
            amount:         Ставка; None — умолчание источника (затем конфиг).
            note:           Произвольная пометка человека, уйдёт в meta и
                            в журнал. Полезно помечать, зачем сделка.

        Returns:
            Порождённый Signal либо None, если очередь переполнена.

        Raises:
            ValueError: Направление или параметры не прошли проверку Signal.
        """
        signal = Signal(
            ts=time.time(),
            symbol=symbol or self.default_symbol,
            direction=direction,
            expiry_minutes=expiry_minutes or self.default_expiry_minutes,
            amount=amount if amount is not None else self.default_amount,
            source=self.name,
            # meta целиком уходит в журнал: по ней потом видно, что это была
            # ручная сделка человека, а не срабатывание стратегии.
            meta={"manual": True, "note": note} if note else {"manual": True},
        )

        accepted = await self.emit(signal)
        if not accepted:
            log.warning("ручной сигнал отброшен: очередь переполнена")
            return None

        self.fired += 1
        log.info("ручной сигнал: %s %s, экспирация %d мин%s",
                 signal.symbol, signal.direction, signal.expiry_minutes,
                 f", пометка: {note}" if note else "")
        return signal
