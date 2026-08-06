"""
Адаптер сигналов из хаба терминала. Python 3.10.

Каркас (Слой 8 ТЗ). Подключается к WS хаба (:8765) как обычный клиент,
подписывается на свечи и складывает их в память. **Логики направления
здесь НЕТ и в этой задаче не будет.**

Это осознанное ограничение, а не недоделка. Модуль обязан уметь всё, кроме
одного — решать, куда пойдёт цена:

  * держать соединение и переподключаться при обрывах;
  * подписываться на нужные пары и таймфрейм;
  * держать последние свечи, чтобы будущей стратегии было на чём считать;
  * звать `_evaluate()` на каждой закрытой свече.

`_evaluate()` — единственная точка, куда стратегия допишется позже. Сейчас
она возвращает None всегда. Когда появится стратегия, она либо переопределит
этот метод в наследнике, либо будет передана в `decide` при создании — и
ничего, кроме этого файла, править не придётся.

Почему не тянем детектор всплесков с ветки hypothesis: §16 ТЗ прямо
запрещает, и правильно — сначала оболочка должна доказать, что работает,
и надо измерить задержку площадки. Если она на волатильности растёт, любая
минутная стратегия окажется неприменимой, и подключать к ней детектор было
бы пустой работой.

Символы: хаб знает FXCM-пары как "EUR/USD" и крипту с префиксом
("lighter:BTC"). Площадка intrade.bar торгует форекс, поэтому берём пары
хаба как есть — они уже в каноническом виде со слэшем.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Callable, Optional

import websockets
from websockets.exceptions import ConnectionClosed

from bot.strategy.base import Signal, SignalSource
from core.logfmt import setup as _log_setup

log = _log_setup("bot-hubfeed")

# Сколько последних свечей держать по каждой паре. Полсотни хватает
# скользящим средним и любой оценке волатильности; больше — незачем
# занимать память, история и так лежит в market.db.
CANDLE_BUFFER = 50

# Пауза перед переподключением, растёт до минуты.
RECONNECT_DELAY = 2.0
RECONNECT_MAX = 60.0


class HubFeedSource(SignalSource):
    """Источник сигналов на основе данных хаба.

    Сам по себе сигналов НЕ порождает: `_evaluate` всегда возвращает None,
    пока стратегия не подключена. Это рабочий канал данных с заглушкой
    вместо решения.

    Attributes:
        name:    Имя источника, попадает в Signal.source и в журнал.
        candles: {символ: список последних закрытых свечей}.
    """

    name = "hub"

    def __init__(
        self,
        ws_url: str = "ws://127.0.0.1:8765",
        symbols: Optional[list] = None,
        timeframe: str = "M1",
        expiry_minutes: int = 1,
        decide: Optional[Callable] = None,
    ):
        """Создать адаптер.

        Args:
            ws_url:         Адрес WS хаба.
            symbols:        Пары для подписки в каноническом виде.
            timeframe:      Таймфрейм подписки ("M1", "S15", …).
            expiry_minutes: Экспирация будущих сигналов.
            decide:         Функция принятия решения (symbol, candles) →
                            "call"/"put"/None. Это и есть точка подключения
                            стратегии. None — сигналов не будет.
        """
        super().__init__()
        self.ws_url = ws_url
        self.symbols = symbols or ["EUR/USD"]
        self.timeframe = timeframe
        self.expiry_minutes = expiry_minutes
        self.decide = decide

        self.candles: dict = {symbol: [] for symbol in self.symbols}
        self.connected = False
        self.messages = 0
        self.last_message_ts = 0.0
        # По задаче на каждую пару: подписка у хаба живёт на соединении.
        self._tasks: list = []

    async def start(self) -> None:
        """Запустить подключения к хабу — по одному на пару.

        Returns:
            None.
        """
        await super().start()
        if self._tasks:
            return
        for symbol in self.symbols:
            self._tasks.append(asyncio.create_task(self._run_symbol(symbol)))
        log.info("адаптер хаба запущен: %s, пары %s, ТФ %s%s",
                 self.ws_url, ", ".join(self.symbols), self.timeframe,
                 "" if self.decide else " (стратегия НЕ подключена — "
                                        "сигналов не будет)")

    async def stop(self) -> None:
        """Остановить адаптер и закрыть все соединения.

        Returns:
            None.
        """
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks = []
        self.connected = False
        await super().stop()

    async def _run_symbol(self, symbol: str) -> None:
        """Держать соединение для ОДНОЙ пары, переподключаясь при обрывах.

        Соединение на пару, а не одно на всех: хаб хранит подписку НА
        СОЕДИНЕНИЕ, и второй set_tf в том же сокете просто заменяет первый
        (hub.py:_on_set_tf). Фронт делает так же — вторая панель открывает
        свой сокет.

        Args:
            symbol: Инструмент.

        Returns:
            None.
        """
        delay = RECONNECT_DELAY
        while True:
            try:
                # max_size=None снимает лимит на размер кадра. Умолчание
                # websockets — 1 МБ, а хаб на set_tf отдаёт историю окном
                # 2000 свечей, и она в него не влезает: соединение рвётся
                # с 1009 (message too big) ДО того, как придёт первая свеча.
                # Симптом обманчив — выглядит как нестабильная сеть.
                async with websockets.connect(self.ws_url, open_timeout=15,
                                              max_size=None) as ws:
                    self.connected = True
                    delay = RECONNECT_DELAY
                    # Ключ именно "type", а не "action" — так подписывается
                    # фронт (index.html), и так это разбирает hub.py.
                    # Перепутав, получаешь молчание: хаб не отвечает ошибкой,
                    # он просто игнорирует запрос, а широковещательный поток
                    # продолжает идти как ни в чём не бывало.
                    await ws.send(json.dumps({
                        "type": "set_tf",
                        "symbol": symbol,
                        "tf": self.timeframe,
                        "requestId": f"bot-{symbol}-{int(time.time())}",
                    }))
                    log.info("хаб: подписка на %s (%s)", symbol, self.timeframe)

                    async for raw in ws:
                        self.messages += 1
                        self.last_message_ts = time.time()
                        await self._on_message(raw)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                self.connected = False
                log.warning("хаб (%s): обрыв (%s), переподключение через %.0f с",
                            symbol, err, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, RECONNECT_MAX)

    async def _on_message(self, raw: str) -> None:
        """Разобрать сообщение хаба и обновить буфер свечей.

        Args:
            raw: Сырое сообщение.

        Returns:
            None.
        """
        try:
            message = json.loads(raw)
        except (TypeError, ValueError):
            return

        kind = message.get("type")

        if kind == "history":
            symbol = message.get("symbol")
            # История лежит в поле "data" (проверено на живом хабе: ключи
            # type/symbol/tf/requestId/data, ~10 тыс. свечей на M1).
            # "candles" оставлен запасным вариантом на случай смены формата.
            candles = message.get("data") or message.get("candles") or []
            if symbol in self.candles:
                self.candles[symbol] = list(candles)[-CANDLE_BUFFER:]
                log.info("хаб: история %s — %d свечей, оставлено %d",
                         symbol, len(candles), len(self.candles[symbol]))
            return

        if kind != "update":
            # heartbeat, instruments, ticker и прочее адаптеру не нужны.
            return

        symbol = message.get("symbol")
        candle = message.get("candle")
        if not candle or symbol not in self.candles:
            return

        buffer = self.candles[symbol]
        # Хаб шлёт и формирующуюся свечу, и закрытую. Одну и ту же минуту
        # обновляем на месте, новую добавляем в хвост.
        if buffer and buffer[-1].get("time") == candle.get("time"):
            buffer[-1] = candle
            return

        buffer.append(candle)
        del buffer[:-CANDLE_BUFFER]

        # Решение принимается на ЗАКРЫТОЙ свече: предыдущая теперь
        # окончательна. Внутри формирующейся свечи цена ещё изменится, и
        # сигнал по ней означал бы подглядывание в незавершённые данные.
        if len(buffer) >= 2:
            await self._evaluate(symbol, buffer[:-1])

    async def _evaluate(self, symbol: str, candles: list) -> Optional[Signal]:
        """Решить, нужен ли сигнал по закрытой свече.

        ТОЧКА ПОДКЛЮЧЕНИЯ СТРАТЕГИИ. Сейчас решение принимает функция
        `decide`, переданная при создании; если её нет — сигналов не будет
        никогда, и это штатное поведение каркаса.

        Args:
            symbol:  Инструмент.
            candles: Закрытые свечи, последняя — самая свежая.

        Returns:
            Порождённый Signal либо None.
        """
        if not self.decide:
            return None

        try:
            direction = self.decide(symbol, candles)
        except Exception as err:
            # Ошибка чужой стратегии не должна ронять канал данных.
            log.exception("стратегия упала на %s: %s", symbol, err)
            return None

        if direction not in ("call", "put"):
            return None

        signal = Signal(
            ts=time.time(),
            symbol=symbol,
            direction=direction,
            expiry_minutes=self.expiry_minutes,
            source=self.name,
            meta={
                "tf": self.timeframe,
                "last_close": candles[-1].get("close") if candles else None,
                "candle_time": candles[-1].get("time") if candles else None,
            },
        )
        await self.emit(signal)
        return signal

    def status(self) -> dict:
        """Состояние адаптера — для панели и диагностики.

        Returns:
            Словарь с состоянием соединения и наполнением буферов.
        """
        return {
            "connected": self.connected,
            "messages": self.messages,
            "seconds_since_message": (round(time.time() - self.last_message_ts, 1)
                                      if self.last_message_ts else None),
            "strategy_attached": self.decide is not None,
            "candles": {symbol: len(buffer)
                        for symbol, buffer in self.candles.items()},
        }
