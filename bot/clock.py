"""
Синхронизация со временем сервера площадки. Python 3.10.

Зачем отдельный модуль на такую мелочь: экспирация минутная, и решения
«пора опрашивать результат» принимаются по времени ПЛОЩАДКИ, а не по
локальным часам. Расхождение в пару секунд означает опрос до расчёта и
outcome "unknown" на ровном месте.

Источник — wss://intrade35.bar/req_info, push раз в секунду:
    {"Time_server": 1785955633}

Замер с VPS 2026-08-05: расхождение с локальными часами около нуля (целые
секунды совпадали точно). Это не повод выкинуть модуль — часы VPS могут
уплыть, а площадка может жить по своему времени; но это значит, что при
недоступности WS локальные часы остаются приемлемым запасным вариантом.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Optional

import websockets

from core.logfmt import setup as _log_setup

log = _log_setup("bot-clock")


class ServerClock:
    """Часы площадки: offset между её временем и локальным.

    Держит WS-соединение и обновляет offset. Пока соединения нет, offset
    равен нулю, то есть возвращается локальное время — сознательная
    деградация вместо отказа работать.
    """

    def __init__(self, url: str = "wss://intrade35.bar/req_info"):
        """Создать часы.

        Args:
            url: Адрес WS времени сервера.
        """
        self.url = url
        self.offset = 0.0            # server_time − local_time, секунды
        self.last_update = 0.0       # локальное время последнего сообщения
        self._task: Optional[asyncio.Task] = None
        self._running = False

    def now(self) -> float:
        """Текущее время по версии площадки.

        Returns:
            Unix-время (float). При отсутствии синхронизации — локальное.
        """
        return time.time() + self.offset

    @property
    def synced(self) -> bool:
        """Свежа ли синхронизация.

        Пять секунд — три пропущенных сообщения при частоте раз в секунду.

        Returns:
            True, если время сервера получено недавно.
        """
        return self.last_update > 0 and (time.time() - self.last_update) < 5.0

    async def start(self) -> None:
        """Запустить фоновое поддержание синхронизации.

        Returns:
            None.
        """
        if self._task:
            return
        self._running = True
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Остановить синхронизацию и закрыть соединение.

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

    async def sync_once(self, timeout: float = 15.0) -> Optional[float]:
        """Один раз подключиться, взять время сервера и отключиться.

        Нужно для проверки Слоя 1 и для разового замера расхождения часов,
        когда держать постоянное соединение незачем.

        Args:
            timeout: Таймаут подключения и ожидания сообщения, секунды.

        Returns:
            Расхождение server − local в секундах либо None при неудаче.
        """
        try:
            async with websockets.connect(self.url, open_timeout=timeout) as ws:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                return self._apply(raw)
        except Exception as err:
            log.warning("время сервера недоступно: %s", err)
            return None

    async def _run(self) -> None:
        """Держать соединение и обновлять offset, переподключаясь при обрывах.

        Returns:
            None.
        """
        delay = 1.0
        while self._running:
            try:
                async with websockets.connect(self.url, open_timeout=15) as ws:
                    log.info("часы площадки: соединение установлено")
                    delay = 1.0
                    while self._running:
                        raw = await asyncio.wait_for(ws.recv(), timeout=30)
                        self._apply(raw)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                if not self._running:
                    return
                log.warning("часы площадки: обрыв (%s), переподключение через %.0f с",
                            err, delay)
                await asyncio.sleep(delay)
                # Наращиваем паузу до минуты: рейт-лимиты площадки неизвестны.
                delay = min(delay * 2, 60.0)

    def _apply(self, raw: str) -> Optional[float]:
        """Разобрать сообщение и обновить offset.

        Args:
            raw: Сырое сообщение WS.

        Returns:
            Новое расхождение либо None, если сообщение не о времени.
        """
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return None

        server_ts = payload.get("Time_server")
        if server_ts is None:
            return None

        local = time.time()
        # Секундная гранулярность: площадка шлёт целые секунды, поэтому
        # честнее считать, что метка относится к середине своей секунды.
        self.offset = (float(server_ts) + 0.5) - local
        self.last_update = local
        return self.offset
