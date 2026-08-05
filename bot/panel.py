"""
Панель наблюдения за ботом: HTTP + WebSocket. Python 3.10.

Отдельный процесс-сервер на своём порту (8788 по умолчанию). В index.html
и hub.py не встраивается — по ТЗ их вообще нельзя трогать, да и незачем:
терминал показывает рынок, панель показывает бота.

Что делает:
  * отдаёт bot/static/panel.html по HTTP;
  * держит WebSocket и раз в секунду шлёт состояние (режим, баланс, выплата,
    открытые сделки, сводка ограничителей, лента последних сделок);
  * принимает команды: ручной вход Call/Put, STOP, снятие стопа.

Почему состояние шлётся целиком, а не дельтами: панель одна, зрителей мало,
объём мизерный. Дельты здесь — сложность без выгоды, а полный срез
переживает любое переподключение без синхронизации.

ОГРАНИЧЕНИЕ ДОСТУПА. Панель умеет открывать сделки, то есть тратить деньги.
По умолчанию она слушает 127.0.0.1 — снаружи не достучаться, смотреть надо
через ssh-туннель:

    ssh -L 8788:127.0.0.1:8788 root@<vps>

Открывать её наружу без пароля нельзя: любой, кто найдёт порт, сможет
нажать Call.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Optional

import websockets
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Response

from bot import payout
from bot.journal import engage_kill_switch, kill_switch_active, release_kill_switch
from core.logfmt import setup as _log_setup

log = _log_setup("bot-panel")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# Период рассылки состояния. Секунда — как опрос котировок: чаще смысла нет,
# реже таймер до экспирации начнёт дёргаться.
BROADCAST_INTERVAL = 1.0


class Panel:
    """HTTP+WS сервер панели наблюдения.

    Держит набор подключённых браузеров и раз в секунду шлёт им состояние.
    """

    def __init__(self, config, journal, engine=None, risk=None,
                 manual_source=None, client=None, quotes=None):
        """Создать панель.

        Args:
            config:        BotConfig.
            journal:       Журнал сделок.
            engine:        Движок (для сводки состояния).
            risk:          Ограничители (для сводки лимитов).
            manual_source: Источник ручных сигналов — кнопки Call/Put.
            client:        Клиент площадки (баланс, выплата).
            quotes:        Поток котировок.
        """
        self.config = config
        self.journal = journal
        self.engine = engine
        self.risk = risk
        self.manual_source = manual_source
        self.client = client
        self.quotes = quotes

        self.clients: set = set()
        self._server = None
        self._task: Optional[asyncio.Task] = None
        self._running = False

        # Баланс и выплата кэшируются: дёргать площадку на каждый кадр
        # панели незачем, рейт-лимиты неизвестны.
        self._balance = None
        self._balance_ts = 0.0
        self._payout = None
        self._payout_ts = 0.0

    # ── жизненный цикл ─────────────────────────────────────────────────

    async def start(self) -> None:
        """Поднять сервер и запустить рассылку состояния.

        Returns:
            None.
        """
        self._running = True
        self._server = await websockets.serve(
            self._handle_client,
            "127.0.0.1",
            self.config.panel_port,
            process_request=self._serve_static,
        )
        self._task = asyncio.create_task(self._broadcast_loop())
        log.info("панель слушает http://127.0.0.1:%d (только локально; "
                 "снаружи — через ssh -L %d:127.0.0.1:%d)",
                 self.config.panel_port, self.config.panel_port,
                 self.config.panel_port)

    async def stop(self) -> None:
        """Остановить сервер и рассылку.

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
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    # ── HTTP ───────────────────────────────────────────────────────────

    async def _serve_static(self, connection, request):
        """Отдать статику для обычных HTTP-запросов.

        WebSocket-рукопожатие пропускается дальше — его обрабатывает
        websockets. Всё остальное это запрос страницы.

        Args:
            connection: Соединение websockets.
            request:    Запрос.

        Returns:
            Ответ HTTP либо None, чтобы продолжить как WebSocket.
        """
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return None

        path = request.path.split("?")[0]
        if path in ("/", "/index.html", "/panel.html"):
            filename = "panel.html"
        else:
            # Никаких путей наружу каталога: панель отдаёт только своё.
            filename = os.path.basename(path)

        full_path = os.path.join(STATIC_DIR, filename)
        if not os.path.isfile(full_path):
            return connection.respond(404, "не найдено\n")

        with open(full_path, "rb") as handle:
            body = handle.read()

        content_type = "text/html; charset=utf-8"
        if filename.endswith(".js"):
            content_type = "application/javascript; charset=utf-8"
        elif filename.endswith(".css"):
            content_type = "text/css; charset=utf-8"

        # Response принимает тело ТОЛЬКО конструктором: connection.respond()
        # собирает ответ сразу, и присваивание response.body после этого
        # молча ничего не делает — страница уходит пустой (проверено).
        headers = Headers({
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
            "Cache-Control": "no-store",
        })
        return Response(200, "OK", headers, body)

    # ── WebSocket ──────────────────────────────────────────────────────

    async def _handle_client(self, websocket) -> None:
        """Обслужить одно подключение браузера.

        Args:
            websocket: Соединение.

        Returns:
            None.
        """
        self.clients.add(websocket)
        log.info("панель: клиент подключился (всего %d)", len(self.clients))
        try:
            # Сразу шлём состояние, чтобы страница не ждала общего тика.
            await websocket.send(json.dumps(await self.snapshot()))
            async for raw in websocket:
                await self._handle_command(websocket, raw)
        except ConnectionClosed:
            pass
        finally:
            self.clients.discard(websocket)
            log.info("панель: клиент отключился (осталось %d)", len(self.clients))

    async def _handle_command(self, websocket, raw: str) -> None:
        """Разобрать и выполнить команду из панели.

        Args:
            websocket: Соединение, приславшее команду.
            raw:       Сырое сообщение.

        Returns:
            None.
        """
        try:
            message = json.loads(raw)
        except ValueError:
            return

        command = message.get("cmd")

        if command == "stop":
            # Kill-switch: мгновенный запрет новых входов.
            engage_kill_switch(self.config.stop_file, reason="кнопка STOP в панели")
            self.journal.event("risk", "STOP нажат в панели")
            await self._reply(websocket, "ok", "торговля остановлена")

        elif command == "resume":
            released = release_kill_switch(self.config.stop_file)
            if self.risk:
                self.risk.release()
            self.journal.event("info", "STOP снят из панели")
            await self._reply(websocket, "ok",
                              "торговля возобновлена" if released
                              else "стоп не был взведён")

        elif command in ("call", "put"):
            await self._manual_trade(websocket, command, message)

        else:
            await self._reply(websocket, "error", f"неизвестная команда {command!r}")

    async def _manual_trade(self, websocket, direction: str,
                            message: dict) -> None:
        """Открыть сделку по кнопке в панели.

        Args:
            websocket: Соединение.
            direction: "call" или "put".
            message:   Разобранное сообщение с параметрами.

        Returns:
            None.
        """
        if not self.manual_source:
            await self._reply(websocket, "error", "ручной источник не подключён")
            return

        symbol = message.get("symbol") or (
            self.config.symbol_whitelist[0] if self.config.symbol_whitelist
            else "USD/JPY"
        )
        amount = message.get("amount")

        try:
            signal = await self.manual_source.fire(
                direction,
                symbol=symbol,
                amount=float(amount) if amount else None,
                note="кнопка панели",
            )
        except ValueError as err:
            await self._reply(websocket, "error", str(err))
            return

        if signal is None:
            await self._reply(websocket, "error", "сигнал отброшен (очередь полна)")
            return

        await self._reply(websocket, "ok",
                          f"сигнал {direction.upper()} {symbol} отправлен")

    async def _reply(self, websocket, status: str, message: str) -> None:
        """Ответить панели на команду.

        Args:
            websocket: Соединение.
            status:    "ok" или "error".
            message:   Текст для показа человеку.

        Returns:
            None.
        """
        try:
            await websocket.send(json.dumps({
                "type": "reply", "status": status, "message": message,
            }))
        except ConnectionClosed:
            pass

    # ── состояние ──────────────────────────────────────────────────────

    async def snapshot(self) -> dict:
        """Собрать полный срез состояния для панели.

        Returns:
            Словарь, готовый к отправке в браузер.
        """
        now = time.time()

        trades = []
        for row in self.journal.recent_trades(limit=15):
            trades.append({
                "id": row["id"],
                "trade_id": row["trade_id"],
                "symbol": row["symbol"],
                "direction": row["direction"],
                "investment": row["investment"],
                "entry_price": row["entry_price"],
                "latency_ms": row["latency_ms"],
                "result": row["result"],
                "pnl": row["pnl"],
                "expiry_ts": row["expiry_ts"],
                "source": row["source"],
                "mode": row["mode"],
                "created_ts": row["created_ts"],
            })

        open_positions = [
            {
                "id": row["id"],
                "trade_id": row["trade_id"],
                "symbol": row["symbol"],
                "direction": row["direction"],
                "investment": row["investment"],
                "entry_price": row["entry_price"],
                "expiry_ts": row["expiry_ts"],
                "seconds_left": (row["expiry_ts"] - now) if row["expiry_ts"] else None,
            }
            for row in self.journal.open_positions()
        ]

        payout_percent = await self._cached_payout()

        state = {
            "type": "state",
            "ts": now,
            "mode": self.config.mode,
            "account": self.config.account,
            "balance": await self._cached_balance(),
            "payout_percent": payout_percent,
            "payout_breakeven": (payout.breakeven_winrate(payout_percent)
                                 if payout_percent else None),
            "hour_edge": payout.is_hour_edge(),
            "minutes_to_hour_edge": round(payout.minutes_until_hour_edge(), 1),
            "kill_switch": kill_switch_active(self.config.stop_file),
            "symbols": self.config.symbol_whitelist,
            "default_investment": self.config.default_investment,
            "quotes_fresh": self.quotes.fresh if self.quotes else None,
            "engine": self.engine.summary() if self.engine else None,
            "risk": self.risk.status() if self.risk else None,
            "stats": self.journal.stats_today(),
            "open_positions": open_positions,
            "trades": trades,
            "events": [
                {"ts": row["ts"], "kind": row["kind"], "message": row["message"]}
                for row in self.journal.recent_events(limit=12)
            ],
        }
        return state

    async def _cached_balance(self) -> Optional[dict]:
        """Баланс с кэшем на 10 секунд.

        Returns:
            Словарь с суммой и валютой либо None.
        """
        if not self.client or not self.config.touches_platform:
            return None
        if self._balance and (time.time() - self._balance_ts) < 10:
            return self._balance

        loop = asyncio.get_running_loop()
        try:
            balance = await loop.run_in_executor(None, self.client.balance)
        except Exception as err:
            log.warning("панель: не получить баланс (%s)", err)
            return self._balance

        self._balance = {"amount": balance.amount, "currency": balance.currency}
        self._balance_ts = time.time()
        return self._balance

    async def _cached_payout(self) -> Optional[int]:
        """Процент выплаты с кэшем на 20 секунд.

        В окне у начала часа кэш короче: там значение меняется резко, и
        показывать устаревшие 82% вместо 60% нельзя — это вводит в
        заблуждение ровно в тот момент, когда точность важнее всего.

        Returns:
            Процент выплаты либо None.
        """
        symbol = (self.config.symbol_whitelist[0]
                  if self.config.symbol_whitelist else "USD/JPY")

        if not self.client or not self.config.touches_platform:
            return payout.expected_percent(self.config.default_expiry_minutes,
                                           self.config.default_investment)

        ttl = 5 if payout.is_hour_edge() else 20
        if self._payout is not None and (time.time() - self._payout_ts) < ttl:
            return self._payout

        loop = asyncio.get_running_loop()
        try:
            percent = await loop.run_in_executor(
                None,
                lambda: self.client.payout_percent(
                    symbol,
                    expiry_minutes=self.config.default_expiry_minutes,
                    investment=self.config.default_investment,
                ),
            )
        except Exception as err:
            log.warning("панель: не получить процент выплаты (%s)", err)
            return self._payout

        self._payout = percent
        self._payout_ts = time.time()
        return percent

    async def _broadcast_loop(self) -> None:
        """Раз в секунду рассылать состояние всем подключённым.

        Returns:
            None.
        """
        while self._running:
            await asyncio.sleep(BROADCAST_INTERVAL)
            if not self.clients:
                continue

            try:
                payload = json.dumps(await self.snapshot())
            except Exception as err:
                log.error("панель: не собрать состояние (%s)", err)
                continue

            dead = set()
            for client in self.clients:
                try:
                    await client.send(payload)
                except ConnectionClosed:
                    dead.add(client)
                except Exception:
                    dead.add(client)
            self.clients -= dead
