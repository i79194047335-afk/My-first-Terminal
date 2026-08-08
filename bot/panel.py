"""
Панель наблюдения за ботом: HTTP + WebSocket. Python 3.10.

Отдельный процесс-сервер на своём порту (8788 по умолчанию). В index.html
и hub.py не встраивается — по ТЗ их вообще нельзя трогать, да и незачем:
терминал показывает рынок, панель показывает бота.

Что делает:
  * отдаёт bot/static/panel.html по HTTP;
  * держит WebSocket и раз в секунду шлёт состояние (режим, баланс, выплата,
    открытые сделки, сводка ограничителей, лента последних сделок);
  * принимает команды: ручной вход Call/Put, STOP, снятие стопа,
    приведение счёта площадки к ДЕМО (только в безопасную сторону, см.
    _ensure_demo).

Почему состояние шлётся целиком, а не дельтами: панель одна, зрителей мало,
объём мизерный. Дельты здесь — сложность без выгоды, а полный срез
переживает любое переподключение без синхронизации.

ОГРАНИЧЕНИЕ ДОСТУПА. Панель умеет открывать сделки, то есть тратить деньги.
По умолчанию она слушает 127.0.0.1 — снаружи не достучаться, смотреть надо
через ssh-туннель:

    ssh -L 8788:127.0.0.1:8788 root@<vps>

Наружу порт открывается ТОЛЬКО вместе с токеном (config.panel_token, из
окружения INTRADE_PANEL_TOKEN): соединение принимается после {cmd: "auth",
token: ...} в первые AUTH_TIMEOUT секунд, до этого ни состояния, ни команд.
config.validate() не даст поднять публичный panel_host без токена вовсе.
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
from bot.api.models import PlatformError
from bot.journal import engage_kill_switch, kill_switch_active, release_kill_switch
from core.logfmt import setup as _log_setup

log = _log_setup("bot-panel")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# Период рассылки состояния. Секунда — как опрос котировок: чаще смысла нет,
# реже таймер до экспирации начнёт дёргаться.
BROADCAST_INTERVAL = 1.0

# Окно на авторизацию (panel_token). Короткое, чтобы на публичном порту не
# копились висящие бездельники: не успел представиться — соединение закрыто.
AUTH_TIMEOUT = 10.0

# Через сколько секунд после экспирации незакрытая сделка считается
# «зависшей». Штатный расчёт укладывается в SETTLE_ATTEMPTS × SETTLE_INTERVAL
# (~30 с) плюс задержка площадки; запас берём вдвое.
STALE_AFTER = 90.0


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

        # Баланс, выплата и профиль счёта кэшируются: дёргать площадку на
        # каждый кадр панели незачем, рейт-лимиты неизвестны.
        self._balance = None
        self._balance_ts = 0.0
        self._payout = None
        self._payout_ts = 0.0
        self._profile = None
        self._profile_ts = 0.0

        # Сериализует приведение счёта: тумблер инвертирует, и два
        # параллельных вызова могли бы переключить счёт туда-обратно.
        self._ensure_lock = asyncio.Lock()

        # Судьба сигнала решается в движке асинхронно, уже после ответа на
        # команду call/put. Без этой подписки человек видит «сигнал
        # отправлен» и никогда не узнаёт, что сделку отклонили.
        if self.engine is not None:
            self.engine.on_outcome = self._on_engine_outcome

    # ── жизненный цикл ─────────────────────────────────────────────────

    async def start(self) -> None:
        """Поднять сервер и запустить рассылку состояния.

        Returns:
            None.
        """
        self._running = True
        host = getattr(self.config, "panel_host", "127.0.0.1")

        self._server = await websockets.serve(
            self._handle_client,
            host,
            self.config.panel_port,
            process_request=self._serve_static,
        )
        self._task = asyncio.create_task(self._broadcast_loop())

        if host in ("127.0.0.1", "localhost", "::1"):
            log.info("панель слушает http://127.0.0.1:%d (только локально; "
                     "снаружи — через ssh -L %d:127.0.0.1:%d)",
                     self.config.panel_port, self.config.panel_port,
                     self.config.panel_port)
        else:
            # Панель умеет открывать сделки. Наружу — только осознанно:
            # validate() требует panel_token для не-localhost хоста, так что
            # пароль здесь есть всегда; ветка «без пароля» — на всякий случай,
            # если кто-то собрал Panel в обход validate().
            token = getattr(self.config, "panel_token", None)
            if token:
                log.warning("ПАНЕЛЬ ОТКРЫТА НАРУЖУ (%s:%d) — доступ по токену "
                            "panel_token. Call/Put видит только тот, у кого "
                            "есть токен. Режим сейчас: %s",
                            host, self.config.panel_port, self.config.mode)
            else:
                log.warning("ПАНЕЛЬ ОТКРЫТА НАРУЖУ (%s:%d) БЕЗ ПАРОЛЯ — "
                            "кнопки Call/Put доступны всем, кто знает адрес. "
                            "Режим сейчас: %s",
                            host, self.config.panel_port, self.config.mode)
            if self.config.touches_platform:
                log.warning("ВНИМАНИЕ: режим %s тратит средства счёта. "
                            "Токен держит доступ к панели, но сам счёт "
                            "защищает только балансовый рубеж",
                            self.config.mode)

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

        Токен-авторизация (panel_token): при заданном токене первое
        сообщение обязано быть {cmd: "auth", token: ...} — в пределах
        AUTH_TIMEOUT, иначе соединение закрывается. До авторизации клиент
        НЕ попадает в self.clients: состояние ему не рассылается, команды
        не исполняются. Без токена поведение прежнее — доступ без пароля
        (штатно для localhost).

        Args:
            websocket: Соединение.

        Returns:
            None.
        """
        token = getattr(self.config, "panel_token", None)
        authed = not bool(token)

        if authed:
            self.clients.add(websocket)
            log.info("панель: клиент подключился (всего %d)", len(self.clients))

        try:
            if authed:
                # Сразу шлём состояние, чтобы страница не ждала общего тика.
                await websocket.send(json.dumps(await self.snapshot()))
            else:
                # Ждём авторизацию. Окно ограничено, чтобы не копить
                # бездельников на порту.
                try:
                    raw = await asyncio.wait_for(websocket.recv(), AUTH_TIMEOUT)
                except asyncio.TimeoutError:
                    await self._reply(websocket, "error", "время на авторизацию истекло")
                    return
                except ConnectionClosed:
                    return
                try:
                    msg = json.loads(raw)
                except ValueError:
                    return
                if msg.get("cmd") != "auth" or msg.get("token") != token:
                    await self._reply(websocket, "error", "неверный токен")
                    log.warning("панель: отклонён вход с неверным токеном")
                    return
                authed = True
                self.clients.add(websocket)
                log.info("панель: клиент авторизован (всего %d)", len(self.clients))
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

        elif command == "ensure_demo":
            await self._ensure_demo(websocket)

        elif command == "set_account":
            await self._set_account(websocket, message)

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

        # Это подтверждение ПРИЁМА, а не открытия: решение примет движок и
        # пришлёт его отдельным сообщением (_on_engine_outcome).
        await self._reply(websocket, "ok",
                          f"{direction.upper()} {symbol} принят, проверяю…")

    async def _ensure_demo(self, websocket) -> None:
        """Привести счёт площадки к ДЕМО — команда «демо» в панели.

        Единственное легальное направление переключения счёта из интерфейса
        бота. Тумблер /user_real_trade.php не имеет параметра и только
        ИНВЕРТИРУЕТ счёт (карта API §3.1): кнопка «на реал» означала бы
        риск перевести торговлю на живые деньги, поэтому её здесь нет
        вовсе. Приведение идёт строго через ensure_account_mode — прочитать
        /profile, сравнить, дёрнуть только при расхождении, перечитать.

        Args:
            websocket: Соединение, откуда пришла команда.

        Returns:
            None.
        """
        if not self.client or not self.config.touches_platform:
            # В dry/shadow бот к площадке не ходит вовсе — и счёт на
            # площадке приводит владелец в кабинете, а не кнопка панели.
            await self._reply(
                websocket, "error",
                "в режиме dry/shadow бот к площадке не ходит — счёт "
                "приводится владельцем в кабинете")
            return

        async with self._ensure_lock:
            loop = asyncio.get_running_loop()
            try:
                profile = await loop.run_in_executor(
                    None, lambda: self.client.ensure_account_mode("demo"))
            except PlatformError as err:
                self.journal.event(
                    "risk", f"счёт: приведение к demo не удалось: {err}")
                await self._reply(websocket, "error",
                                  f"не привести счёт к demo: {err}")
                return

            # Сбросить кэш профиля: следующий кадр покажет актуальное
            # состояние, а не старое «реал».
            self._profile = None
            self._profile_ts = 0.0
            self.journal.event(
                "info",
                f"счёт площадки приведён к demo (валюта {profile.currency})")
            await self._reply(websocket, "ok",
                              f"счёт приведён к demo: {profile.account}")

    async def _set_account(self, websocket, message: dict) -> None:
        """Переключить тип счёта площадки в обе стороны (демо ⇄ реал).

        Двусторонний аналог _ensure_demo: тумблер площадки без параметра и
        только инвертирует счёт, поэтому целевое состояние передаётся явно
        в target, а протокол строгий — прочитать /profile → сравнить →
        переключить только при расхождении → перечитать и убедиться
        (client.ensure_account_mode).

        Сознательно опаснее «только в демо»: «на реал» означает живые
        средства. Защит здесь нет — решение за владельцем, подтверждение
        берёт фронт перед отправкой.

        Args:
            websocket: Соединение.
            message:   Разобранное сообщение с полем target ("demo"/"real").

        Returns:
            None.
        """
        if not self.client or not self.config.touches_platform:
            await self._reply(
                websocket, "error",
                "в режиме dry/shadow бот к площадке не ходит — счёт "
                "переключается владельцем в кабинете")
            return

        target = message.get("target")
        if target not in ("demo", "real"):
            await self._reply(websocket, "error", "target: demo|real")
            return

        async with self._ensure_lock:
            loop = asyncio.get_running_loop()
            try:
                profile = await loop.run_in_executor(
                    None, lambda: self.client.ensure_account_mode(target))
            except Exception as err:
                self.journal.event(
                    "risk", f"счёт: переключение на {target} не удалось: {err}")
                await self._reply(websocket, "error",
                                  f"не переключить счёт на {target}: {err}")
                return

            self._profile = None
            self._profile_ts = 0.0
            self.journal.event(
                "info",
                f"счёт площадки переключён на {target} "
                f"(валюта {profile.currency})")
            await self._reply(
                websocket, "ok",
                f"счёт: {profile.account} ({profile.trade_type})")

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

    def _on_engine_outcome(self, kind: str, text: str) -> None:
        """Разослать в браузеры судьбу сигнала, решённую движком.

        Движок зовёт это синхронно из своего цикла, поэтому рассылка
        ставится задачей, а не ожидается здесь.

        Args:
            kind: "rejected" или "opened".
            text: Причина отказа либо описание открытой сделки.

        Returns:
            None.
        """
        status = "error" if kind == "rejected" else "ok"
        message = f"отказ: {text}" if kind == "rejected" else text
        asyncio.create_task(self._broadcast_reply(status, message))

    async def _broadcast_reply(self, status: str, message: str) -> None:
        """Отправить одинаковый ответ всем подключённым браузерам.

        Args:
            status:  "ok" или "error".
            message: Текст для показа человеку.

        Returns:
            None.
        """
        payload = json.dumps({
            "type": "reply", "status": status, "message": message,
        })
        for websocket in list(self.clients):
            try:
                await websocket.send(payload)
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
                # Экспирация давно прошла, а итога нет: сделку не сопровождает
                # ни одна задача (бота останавливали между открытием и
                # расчётом). Панель обязана показать это как «ждёт итога», а
                # не как живую позицию с отсчётом «0 с».
                "stale": bool(row["expiry_ts"]
                              and row["expiry_ts"] + STALE_AFTER < now),
            }
            for row in self.journal.open_positions()
        ]

        closed_trades = [
            {
                "id": row["id"],
                "trade_id": row["trade_id"],
                "symbol": row["symbol"],
                "direction": row["direction"],
                "investment": row["investment"],
                "entry_price": row["entry_price"],
                "result": row["result"],
                "pnl": row["pnl"],
                "source": row["source"],
                "mode": row["mode"],
                "created_ts": row["created_ts"],
            }
            for row in self.journal.closed_trades(limit=30)
        ]

        payout_percent = await self._cached_payout()

        state = {
            "type": "state",
            "ts": now,
            "mode": self.config.mode,
            "account": self.config.account,
            "touches_platform": self.config.touches_platform,
            "profile": await self._cached_profile(),
            "balance": await self._cached_balance(),
            "payout_percent": payout_percent,
            "payout_breakeven": (payout.breakeven_winrate(payout_percent)
                                 if payout_percent else None),
            "hour_edge": payout.is_hour_edge(),
            "minutes_to_hour_edge": round(payout.minutes_until_hour_edge(), 1),
            "broker_down": payout.is_broker_down(),
            "minutes_to_broker_down": round(payout.minutes_until_broker_down(), 1),
            "kill_switch": kill_switch_active(self.config.stop_file),
            "symbols": self.config.symbol_whitelist,
            "default_investment": self.config.default_investment,
            "quotes_fresh": self.quotes.fresh if self.quotes else None,
            "engine": self.engine.summary() if self.engine else None,
            "risk": self.risk.status() if self.risk else None,
            # Статистика ТЕКУЩЕГО режима — та же, на которую смотрят
            # ограничители. Показывать общую по всем режимам значило бы
            # рисовать в панели одни цифры, а лимиты считать по другим.
            "stats": self.journal.stats_today(mode=self.config.mode),
            "open_positions": open_positions,
            "closed_trades": closed_trades,
            "trades": trades,
            "events": [
                {"ts": row["ts"], "kind": row["kind"], "message": row["message"]}
                for row in self.journal.recent_events(limit=12)
            ],
        }
        return state

    async def _cached_profile(self) -> Optional[dict]:
        """Состояние счёта на площадке с кэшем на 10 секунд.

        Только чтение /profile — тумблер не дёргается. Это последний рубеж
        перед кнопкой Call: если на площадке активен реал, панель должна
        показать его сразу и красным, а не по косвенным признакам.

        Returns:
            Словарь с account/trade_type/currency либо None.
        """
        if not self.client or not self.config.touches_platform:
            return None
        if self._profile and (time.time() - self._profile_ts) < 10:
            return self._profile

        loop = asyncio.get_running_loop()
        try:
            profile = await loop.run_in_executor(None, self.client.profile)
        except Exception as err:
            log.warning("панель: не прочитать профиль счёта (%s)", err)
            return self._profile

        self._profile = {
            "account": profile.account,
            "trade_type": profile.trade_type,
            "currency": profile.currency,
        }
        self._profile_ts = time.time()
        return self._profile

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
