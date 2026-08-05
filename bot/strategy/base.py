"""
Контракт стратегии: Signal + SignalSource. Python 3.10.

ЭТО ГЛАВНЫЙ ИНТЕРФЕЙС ВСЕЙ ОБОЛОЧКИ. Ядро (engine.py) знает только его и
больше ничего: ни одной ссылки на конкретную стратегию, ни одного if по
имени источника. Проверка правильности простая — чтобы подключить новую
стратегию, должно хватить нового файла в bot/strategy/ и строки в конфиге.
Если для этого пришлось править исполнителя, интерфейс спроектирован неверно.

Почему интерфейс написан раньше своего слоя (ТЗ ставит его пятым): journal.py
обязан писать Signal.meta в таблицу, а engine.py — принимать источники. Пока
контракта нет, схему журнала и ядро пришлось бы согласовывать задним числом.
Сам ТЗ в §6 требует «проектировать сразу» — следуем этому.

Стратегии здесь нет и не будет: направление сделки этот модуль не вычисляет,
он только описывает, в каком виде чужое решение попадает к исполнителю.

Как выглядит подключение (для будущего автора стратегии):

    class MyStrategy(SignalSource):
        name = "my_strategy"

        async def start(self):
            ...                       # подписаться на что нужно

        async def _produce(self):
            while True:
                if <моё условие>:
                    await self.emit(Signal(
                        ts=time.time(), symbol="EUR/USD",
                        direction="call", source=self.name,
                        meta={"sigma": 2.7},   # любой контекст
                    ))

Ядро само вызовет start(), будет читать сигналы асинхронной итерацией и
вызовет stop() при завершении.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import AsyncIterator, Literal, Optional

# Направление в терминах бота. В числа площадки (1=Call, 2=Put) это
# превращается единственный раз — в client.open_trade.
Direction = Literal["call", "put"]


@dataclass(frozen=True)
class Signal:
    """Намерение открыть сделку. Всё, что стратегия сообщает исполнителю.

    Неизменяемый: сигнал, однажды порождённый, проходит через ограничители,
    исполнителя и журнал только на чтение. Мутация по дороге означала бы,
    что в журнал попадёт не то, что решила стратегия.

    Attributes:
        ts:             Unix-время (сек) возникновения сигнала. Именно оно,
                        а не время отправки запроса: разница между ними и
                        задержка площадки (~3 с) — предмет измерения.
        symbol:         Канонический вид со слэшем ("USD/JPY").
        direction:      "call" (вверх) или "put" (вниз).
        expiry_minutes: Экспирация в минутах.
        amount:         Размер ставки; None — взять из конфига.
        source:         Кто породил сигнал ("manual", "shock", …). Источников
                        может работать несколько сразу, и в журнале они
                        должны различаться.
        meta:           Произвольный контекст стратегии (сигма, форма, что
                        угодно). ЯДРО В НЕГО НЕ ЗАГЛЯДЫВАЕТ, но целиком пишет
                        в журнал: так стратегия сможет позже разложить
                        результаты по своим признакам без миграции схемы.
    """

    ts: float
    symbol: str
    direction: Direction
    expiry_minutes: int = 1
    amount: Optional[float] = None
    source: str = ""
    meta: dict = field(default_factory=dict)

    def __post_init__(self):
        """Проверить сигнал на грубые ошибки в момент создания.

        Ошибку стратегии лучше поймать здесь, чем на отправке ставки:
        неизвестное направление или пустой символ до площадки доходить
        не должны.

        Raises:
            ValueError: Направление не call/put, символ пуст либо
                        экспирация неположительна.
        """
        if self.direction not in ("call", "put"):
            raise ValueError(
                f"направление должно быть 'call' или 'put', получено {self.direction!r}"
            )
        if not self.symbol or "/" not in self.symbol:
            raise ValueError(
                f"символ должен быть каноническим, со слэшем, получено {self.symbol!r}"
            )
        if self.expiry_minutes <= 0:
            raise ValueError(f"экспирация должна быть больше нуля: {self.expiry_minutes}")
        if self.amount is not None and self.amount <= 0:
            raise ValueError(f"ставка должна быть больше нуля: {self.amount}")

    @property
    def age(self) -> float:
        """Сколько секунд прошло с возникновения сигнала.

        Нужно исполнителю: сигнал, пролежавший дольше пары секунд, для
        минутной экспирации уже протух — площадка добавит к нему свои ~3 с
        задержки открытия.

        Returns:
            Возраст сигнала в секундах.
        """
        import time

        return time.time() - self.ts


class SignalSource:
    """Источник сигналов. Стратегия реализует ЭТОТ интерфейс и больше ничего.

    Базовый класс даёт готовую машинерию очереди, чтобы наследнику осталось
    только звать emit(). Наследоваться необязательно — ядру достаточно, чтобы
    объект имел name, start(), stop() и поддерживал асинхронную итерацию.

    Attributes:
        name: Имя источника, попадает в Signal.source и в журнал.
    """

    name: str = "unnamed"

    def __init__(self, name: Optional[str] = None, queue_size: int = 100):
        """Создать источник.

        Args:
            name:       Имя источника; по умолчанию берётся из класса.
            queue_size: Глубина очереди сигналов. Ограничена намеренно: если
                        исполнитель не успевает, лучше отбросить новые сигналы
                        и сказать об этом, чем копить очередь протухших.
        """
        if name:
            self.name = name
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=queue_size)
        self._running = False
        self.dropped = 0  # сколько сигналов отброшено из-за переполнения

    async def start(self) -> None:
        """Запустить источник.

        Наследник переопределяет и поднимает здесь свои подписки, задачи,
        соединения. Базовая реализация только отмечает состояние.

        Returns:
            None.
        """
        self._running = True

    async def stop(self) -> None:
        """Остановить источник и освободить ресурсы.

        Наследник переопределяет. Базовая реализация будит итератор, чтобы
        тот завершился, а не висел на пустой очереди.

        Returns:
            None.
        """
        self._running = False
        # None — признак конца потока для __anext__.
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            pass

    async def emit(self, signal: Signal) -> bool:
        """Отдать сигнал исполнителю.

        Зовётся наследником. Не блокирует: при переполненной очереди сигнал
        ОТБРАСЫВАЕТСЯ, а счётчик dropped растёт. Для минутной экспирации это
        верное поведение — сигнал, дождавшийся места в очереди, уже неактуален.

        Args:
            signal: Готовый сигнал.

        Returns:
            True, если сигнал принят в очередь; False, если отброшен.
        """
        try:
            self._queue.put_nowait(signal)
            return True
        except asyncio.QueueFull:
            self.dropped += 1
            return False

    def __aiter__(self) -> AsyncIterator[Signal]:
        """Вернуть себя как асинхронный итератор.

        Returns:
            Сам источник.
        """
        return self

    async def __anext__(self) -> Signal:
        """Дождаться следующего сигнала.

        Returns:
            Очередной Signal.

        Raises:
            StopAsyncIteration: Источник остановлен.
        """
        signal = await self._queue.get()
        if signal is None:
            raise StopAsyncIteration
        return signal


async def multiplex(sources: list, queue_size: int = 200) -> AsyncIterator[Signal]:
    """Слить сигналы нескольких источников в один поток.

    Источников может работать несколько одновременно (§6 ТЗ), а исполнитель
    читает один поток. Каждый сигнал уже помечен своим Signal.source, так что
    различить их в журнале можно и после слияния.

    Args:
        sources:    Список объектов SignalSource.
        queue_size: Глубина общей очереди.

    Yields:
        Сигналы всех источников в порядке поступления.
    """
    merged: asyncio.Queue = asyncio.Queue(maxsize=queue_size)
    tasks = []

    async def pump(source) -> None:
        """Перекладывать сигналы одного источника в общую очередь.

        Args:
            source: Источник сигналов.

        Returns:
            None.
        """
        try:
            async for signal in source:
                await merged.put(signal)
        except asyncio.CancelledError:
            raise
        finally:
            # Сообщаем о завершении именно этого источника.
            await merged.put(None)

    for source in sources:
        tasks.append(asyncio.create_task(pump(source)))

    finished = 0
    try:
        while finished < len(sources):
            item = await merged.get()
            if item is None:
                finished += 1
                continue
            yield item
    finally:
        for task in tasks:
            task.cancel()
