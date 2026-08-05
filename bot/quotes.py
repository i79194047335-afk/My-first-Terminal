"""
Поток котировок площадки. Python 3.10.

Опрашивает price_now и держит последний срез в памяти. Опрос, а не подписка:
у площадки нет WS с котировками, её собственный фронт тоже опрашивает — раз
в секунду. Чаще нельзя: рейт-лимиты неизвестны, а долбить чужой сервер
незачем.

Зачем боту котировки, если цену входа всё равно фиксирует площадка:

  1. quote_at_signal / quote_at_request в журнале. Между сигналом и открытием
     проходит ~3 секунды, и цена за это время уходит. Только сравнив три
     точки (сигнал → запрос → фактический вход), можно понять, сколько
     стоит эта задержка в пунктах, а не в миллисекундах.
  2. Режим dry: сделки имитируются целиком по этим котировкам, к площадке
     бот не ходит вовсе.

Расчёт опциона идёт по цене ПЛОЩАДКИ, а не по нашему FXCM-фиду: сигнал
считается по одному источнику, исход — по другому. Это надо помнить, когда
дойдёт до сверки результатов со стратегией.
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional

from bot.api.client import IntradeClient
from bot.api.models import PlatformError, Quote
from core.logfmt import setup as _log_setup

log = _log_setup("bot-quotes")

# Период опроса. Ровно как у их фронта — чаще не нужно и невежливо.
POLL_INTERVAL = 1.0

# Через сколько секунд котировка считается протухшей. Площадка обновляет
# пары неравномерно (у неликвидных «возраст» доходит до десятков секунд),
# поэтому порог щедрый: он ловит обрыв опроса, а не медленную пару.
STALE_AFTER = 15.0


class QuoteFeed:
    """Кэш котировок с фоновым опросом.

    Держит последний известный срез по всем парам. Читатели (движок,
    панель) берут котировку мгновенно, без сетевого вызова.
    """

    def __init__(self, client: IntradeClient, interval: float = POLL_INTERVAL):
        """Создать поток котировок.

        Args:
            client:   Клиент площадки.
            interval: Период опроса в секундах.
        """
        self.client = client
        self.interval = interval
        self.quotes: dict = {}
        self.last_update = 0.0
        self.errors = 0
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self) -> None:
        """Запустить фоновый опрос.

        Первый срез забирается сразу, чтобы движок не стартовал вслепую.

        Returns:
            None.
        """
        if self._task:
            return
        self._running = True
        await self.refresh()
        self._task = asyncio.create_task(self._run())
        log.info("поток котировок запущен, период %.1f с", self.interval)

    async def stop(self) -> None:
        """Остановить опрос.

        Returns:
            None.
        """
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def refresh(self) -> bool:
        """Забрать свежий срез котировок.

        Сетевой вызов синхронный (requests), поэтому уходит в executor —
        иначе он заблокировал бы весь цикл asyncio на время запроса.

        Returns:
            True, если срез получен.
        """
        loop = asyncio.get_running_loop()
        try:
            quotes = await loop.run_in_executor(None, self.client.quotes)
        except PlatformError as err:
            self.errors += 1
            # Не спамим логом при затяжном обрыве: первая ошибка и дальше
            # каждая десятая.
            if self.errors == 1 or self.errors % 10 == 0:
                log.warning("котировки недоступны (%d-я ошибка подряд): %s",
                            self.errors, err)
            return False

        self.quotes = quotes
        self.last_update = time.time()
        self.errors = 0
        return True

    def get(self, symbol: str) -> Optional[Quote]:
        """Взять последнюю котировку по инструменту.

        Args:
            symbol: Канонический символ со слэшем ("USD/JPY").

        Returns:
            Quote либо None, если пары нет в срезе.
        """
        return self.quotes.get(symbol)

    def mid(self, symbol: str) -> Optional[float]:
        """Взять середину спреда по инструменту.

        Args:
            symbol: Канонический символ со слэшем.

        Returns:
            Середина спреда либо None.
        """
        quote = self.get(symbol)
        return quote.mid if quote else None

    @property
    def fresh(self) -> bool:
        """Свежи ли котировки.

        Returns:
            True, если последний удачный опрос был недавно.
        """
        return self.last_update > 0 and (time.time() - self.last_update) < STALE_AFTER

    async def _run(self) -> None:
        """Фоновый цикл опроса.

        Returns:
            None.
        """
        while self._running:
            await asyncio.sleep(self.interval)
            if not self._running:
                return
            await self.refresh()
