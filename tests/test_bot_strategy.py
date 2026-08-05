"""
Тесты контракта стратегии. Python 3.10.

Запуск:  python3.10 tests/test_bot_strategy.py    (из корня проекта)

Главная проверка файла — та, что в §6 ТЗ названа критерием правильности:
СТОРОННЯЯ стратегия, о существовании которой оболочка не знает, должна
подключаться без единой правки в bot/ вне каталога strategy/. Ниже она
изображена классом FakeStrategy, объявленным прямо в тесте — если для его
работы понадобится что-то поменять в ядре, интерфейс спроектирован неверно.
"""

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.strategy.base import Signal, SignalSource, multiplex
from bot.strategy.manual import ManualSource

passed = 0
failed = 0


def check(name, condition, detail=""):
    """Проверить условие и напечатать результат.

    Args:
        name:      Название проверки.
        condition: Результат проверки.
        detail:    Что показать при провале.

    Returns:
        None.
    """
    global passed, failed
    if condition:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed += 1
        print(f"  FAIL {name} {detail}")


def test_signal_validation():
    """Signal отвергает грубые ошибки стратегии в момент создания."""
    print("проверка Signal")
    good = Signal(ts=time.time(), symbol="USD/JPY", direction="call")
    check("корректный сигнал создаётся", good.direction == "call")
    check("экспирация по умолчанию 1 мин", good.expiry_minutes == 1)
    check("meta по умолчанию пустой словарь", good.meta == {})
    check("возраст считается", good.age < 1.0, good.age)

    cases = [
        ({"direction": "up"}, "направление 'up' вместо call/put"),
        ({"direction": "CALL"}, "регистр направления"),
        ({"symbol": "USDJPY"}, "символ без слэша"),
        ({"symbol": ""}, "пустой символ"),
        ({"expiry_minutes": 0}, "нулевая экспирация"),
        ({"amount": 0}, "нулевая ставка"),
        ({"amount": -5}, "отрицательная ставка"),
    ]
    for override, what in cases:
        kwargs = {"ts": time.time(), "symbol": "USD/JPY", "direction": "call"}
        kwargs.update(override)
        try:
            Signal(**kwargs)
            check(f"отвергнуто: {what}", False, "сигнал создан")
        except ValueError:
            check(f"отвергнуто: {what}", True)


def test_signal_frozen():
    """Signal неизменяемый — по дороге в журнал его не подменить."""
    print("неизменяемость Signal")
    signal = Signal(ts=time.time(), symbol="EUR/USD", direction="put")
    try:
        signal.direction = "call"
        check("мутация запрещена", False, "поле изменилось")
    except Exception:
        check("мутация запрещена", True)


def test_manual_source():
    """Ручной источник порождает сигналы и помечает их своим именем."""
    print("ручной источник")

    async def scenario():
        """Прогнать сценарий ручного входа.

        Returns:
            Список полученных сигналов.
        """
        source = ManualSource(default_symbol="USD/JPY")
        await source.start()
        await source.fire("call", note="проверка")
        await source.fire("put", symbol="EUR/USD", expiry_minutes=3)
        await source.stop()

        received = []
        async for signal in source:
            received.append(signal)
        return received

    signals = asyncio.run(scenario())
    check("получено 2 сигнала", len(signals) == 2, len(signals))
    check("первый call", signals[0].direction == "call")
    check("источник помечен", signals[0].source == "manual", signals[0].source)
    check("meta несёт пометку", signals[0].meta.get("note") == "проверка",
          signals[0].meta)
    check("второй put/EUR-USD", signals[1].symbol == "EUR/USD"
          and signals[1].direction == "put")
    check("экспирация второго 3 мин", signals[1].expiry_minutes == 3)


def test_queue_overflow_drops():
    """Переполненная очередь отбрасывает сигналы, а не копит протухшие."""
    print("переполнение очереди")

    async def scenario():
        """Забить очередь сверх ёмкости.

        Returns:
            Кортеж (принято, отброшено).
        """
        source = SignalSource(name="flood", queue_size=3)
        accepted = 0
        for _ in range(10):
            ok = await source.emit(
                Signal(ts=time.time(), symbol="USD/JPY", direction="call")
            )
            accepted += 1 if ok else 0
        return accepted, source.dropped

    accepted, dropped = asyncio.run(scenario())
    check("принято ровно по ёмкости", accepted == 3, accepted)
    check("остальные отброшены", dropped == 7, dropped)
    # Для минутной экспирации отбросить верно: сигнал, дождавшийся места,
    # уже неактуален. Молча копить очередь было бы хуже.
    check("счётчик потерь виден", dropped > 0)


def test_third_party_strategy_needs_no_core_change():
    """КРИТЕРИЙ ТЗ: чужая стратегия подключается без правок ядра.

    FakeStrategy объявлена здесь, в тесте. Оболочка о ней ничего не знает:
    ни импорта, ни ветки по имени. Если этот тест проходит, значит новую
    стратегию действительно можно подключить одним файлом.
    """
    print("подключение сторонней стратегии")

    class FakeStrategy(SignalSource):
        """Стратегия-заглушка, о которой оболочка не подозревает."""

        name = "fake_strategy"

        def __init__(self, plan):
            """Создать заглушку.

            Args:
                plan: Список направлений, которые надо выдать.
            """
            super().__init__()
            self.plan = plan

        async def start(self):
            """Выдать все запланированные сигналы.

            Returns:
                None.
            """
            await super().start()
            for index, direction in enumerate(self.plan):
                await self.emit(
                    Signal(
                        ts=time.time(),
                        symbol="AUD/USD",
                        direction=direction,
                        source=self.name,
                        # Произвольный контекст: ядро в него не смотрит,
                        # но обязано донести до журнала как есть.
                        meta={"index": index, "sigma": 2.7, "nested": {"a": [1, 2]}},
                    )
                )
            await self.stop()

    async def scenario():
        """Прогнать чужую стратегию через тот же интерфейс.

        Returns:
            Список полученных сигналов.
        """
        strategy = FakeStrategy(["call", "put", "call"])
        await strategy.start()
        return [signal async for signal in strategy]

    signals = asyncio.run(scenario())
    check("получены все 3 сигнала", len(signals) == 3, len(signals))
    check("имя источника своё", signals[0].source == "fake_strategy",
          signals[0].source)
    check("meta донесён целиком", signals[1].meta["sigma"] == 2.7, signals[1].meta)
    check("вложенный meta не потерян",
          signals[2].meta["nested"] == {"a": [1, 2]}, signals[2].meta)
    check("порядок сохранён",
          [s.direction for s in signals] == ["call", "put", "call"],
          [s.direction for s in signals])


def test_multiplex():
    """Несколько источников сливаются в один поток с сохранением меток."""
    print("мультиплексирование источников")

    class Ticker(SignalSource):
        """Источник, выдающий заданное число сигналов."""

        def __init__(self, name, count, direction):
            """Создать источник.

            Args:
                name:      Имя источника.
                count:     Сколько сигналов выдать.
                direction: Направление сигналов.
            """
            super().__init__(name=name)
            self.count = count
            self.direction = direction

        async def start(self):
            """Выдать сигналы и завершиться.

            Returns:
                None.
            """
            await super().start()
            for _ in range(self.count):
                await self.emit(
                    Signal(ts=time.time(), symbol="USD/CAD",
                           direction=self.direction, source=self.name)
                )
                await asyncio.sleep(0.01)
            await self.stop()

    async def scenario():
        """Слить два источника в один поток.

        Returns:
            Список полученных сигналов.
        """
        first = Ticker("alpha", 3, "call")
        second = Ticker("beta", 2, "put")
        asyncio.create_task(first.start())
        asyncio.create_task(second.start())

        received = []
        async for signal in multiplex([first, second]):
            received.append(signal)
        return received

    signals = asyncio.run(asyncio.wait_for(scenario(), timeout=5))
    check("получены все 5 сигналов", len(signals) == 5, len(signals))
    sources = {s.source for s in signals}
    check("оба источника представлены", sources == {"alpha", "beta"}, sources)
    check("alpha дал 3", sum(1 for s in signals if s.source == "alpha") == 3)
    check("beta дал 2", sum(1 for s in signals if s.source == "beta") == 2)


def test_core_does_not_import_strategies():
    """Ядро не должно импортировать конкретные стратегии.

    Проверка из §12 ТЗ. Модули оболочки вне strategy/ не имеют права
    ссылаться на manual/hub_feed и им подобных: иначе «подключение одним
    файлом» превратится в правку исполнителя.
    """
    print("ядро не знает о конкретных стратегиях")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bot_dir = os.path.join(root, "bot")

    offenders = []
    for folder, _, files in os.walk(bot_dir):
        if os.path.basename(folder) == "strategy":
            continue  # внутри strategy/ ссылаться друг на друга можно
        for filename in files:
            if not filename.endswith(".py"):
                continue
            path = os.path.join(folder, filename)
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
            for forbidden in ("strategy.manual", "strategy.hub_feed",
                              "ManualSource", "HubFeedSource"):
                # base.py в комментариях упоминать можно, важен именно импорт.
                if f"import {forbidden}" in text or f"from bot.strategy.{forbidden}" in text:
                    offenders.append(f"{filename}: {forbidden}")

    check("ни один модуль ядра не импортирует стратегию",
          not offenders, offenders)


def main():
    """Прогнать все тесты и вернуть код возврата.

    Returns:
        0 — всё прошло, 1 — есть провалы.
    """
    for test in (test_signal_validation, test_signal_frozen, test_manual_source,
                 test_queue_overflow_drops,
                 test_third_party_strategy_needs_no_core_change,
                 test_multiplex, test_core_does_not_import_strategies):
        test()
        print()

    print(f"итого: {passed} прошло, {failed} провалено")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
