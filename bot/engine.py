"""
Ядро бота: машина состояний сделки. Python 3.10.

    IDLE
     └─ сигнал ──> VALIDATING      проверка ограничителей
                      ├─ отказ ──> REJECTED
                      └─ ok ────> SENDING       POST /ajax5_new.php
                                     ├─ ошибка ─> FAILED
                                     ├─ обрыв ──> UNKNOWN   ← сверка, НЕ повтор
                                     └─ ok ────> OPEN
                                                  │ ждём экспирацию
                                                  v
                                              SETTLING
                                                  │ опрос trade_check2.php
                                                  v
                                              CLOSED

ГЛАВНОЕ ПРАВИЛО ЭТОГО МОДУЛЯ: из SENDING нельзя отправить второй запрос по
тому же сигналу. Никогда. Если ответ не получен — сделка МОГЛА открыться,
и повтор означает двойную ставку на реальные деньги. Вместо ретрая сделка
уходит в UNKNOWN и разрешается сверкой через trade_load_more2.php
(НЕ user_real_trade.php — это тумблер Реал⇄Демо, см. карту API §3.1).

Ядро не знает ни одной конкретной стратегии: только SignalSource и Signal
(bot/strategy/base.py). Ни одного if по имени источника здесь быть не должно
— иначе «подключение стратегии одним файлом» перестанет работать.

Режимы (§8 ТЗ):
  dry    — к площадке не ходит вовсе, сделки имитируются по котировкам,
           задержка открытия моделируется;
  shadow — котировки настоящие, но ставка НЕ отправляется: пишем, что
           сделали бы;
  demo   — настоящие запросы на демо-счёт;
  live   — заблокирован тремя рубежами, здесь не включается.
"""

from __future__ import annotations

import asyncio
import random
import time
from enum import Enum
from typing import Optional

from bot.api.client import IntradeClient, TradeUnknown
from bot.api.models import PlatformError, Trade
from bot.journal import Journal, kill_switch_active
from bot.quotes import QuoteFeed
from bot.strategy.base import Signal
from core.logfmt import setup as _log_setup

log = _log_setup("bot-engine")

# Модель задержки открытия для режима dry. Числа из живых замеров площадки
# (2910 и 3559 мс при разведке). Имитация обязана быть честной: если в dry
# сделка открывается мгновенно, вся отладка пройдёт в условиях, которых
# в реальности не бывает.
DRY_LATENCY_RANGE = (2.8, 3.6)

# Сколько ждать после экспирации, прежде чем спрашивать итог. Площадке
# нужно время, чтобы рассчитать сделку; спросив ровно в expiry_ts, получим
# пустой ответ и лишний запрос.
SETTLE_DELAY = 2.0

# Сколько раз опрашивать итог, прежде чем признать его неизвестным.
SETTLE_ATTEMPTS = 10
SETTLE_INTERVAL = 3.0

# Предельный возраст сигнала. Площадка добавит свои ~3 секунды, поэтому
# сигнал, пролежавший дольше, для минутной экспирации уже бессмысленен.
MAX_SIGNAL_AGE = 5.0


class State(str, Enum):
    """Состояние сделки в машине состояний."""

    IDLE = "IDLE"
    VALIDATING = "VALIDATING"
    REJECTED = "REJECTED"
    SENDING = "SENDING"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    OPEN = "OPEN"
    SETTLING = "SETTLING"
    CLOSED = "CLOSED"


class Engine:
    """Исполнитель сигналов: от намерения стратегии до записи итога.

    Один экземпляр на процесс. Все сетевые вызовы уходят в executor, чтобы
    не блокировать цикл asyncio.
    """

    def __init__(
        self,
        config,
        journal: Journal,
        client: Optional[IntradeClient] = None,
        quotes: Optional[QuoteFeed] = None,
        risk=None,
        clock=None,
        on_outcome=None,
    ):
        """Создать движок.

        Args:
            config:     BotConfig.
            journal:    Журнал сделок.
            client:     Клиент площадки; не нужен в режиме dry.
            quotes:     Поток котировок.
            risk:       Ограничители (Слой 4); None — проверок нет.
            clock:      Часы площадки; None — локальное время.
            on_outcome: Необязательный колбэк (kind, text) — судьба сигнала:
                        "rejected" с причиной либо "opened". Нужен панели:
                        кнопка Call только КЛАДЁТ сигнал в очередь, а решение
                        принимается здесь, асинхронно, и без этого канала
                        человек видит «отправлен» и никогда не узнаёт об
                        отказе. Ядро при этом о панели не знает.
        """
        self.config = config
        self.journal = journal
        self.client = client
        self.quotes = quotes
        self.risk = risk
        self.clock = clock
        self.on_outcome = on_outcome

        self.state = State.IDLE
        self.open_trades: dict = {}      # row_id -> задача сопровождения
        self.processed = 0
        self.rejected = 0
        self.failed = 0
        self._settle_tasks: set = set()

    # ── время ──────────────────────────────────────────────────────────

    def now(self) -> float:
        """Текущее время по версии площадки.

        Экспирация минутная, и ждать расчёта надо по часам площадки, а не
        по локальным: расхождение в пару секунд даёт опрос до расчёта.

        Returns:
            Unix-время (float).
        """
        if self.clock:
            return self.clock.now()
        return time.time()

    # ── основной цикл ──────────────────────────────────────────────────

    async def reconcile_orphans(self) -> int:
        """Разобрать сделки, оставшиеся незакрытыми от прошлого запуска.

        Сопровождение сделки (`_settle`) живёт задачей в памяти процесса:
        если бота остановили между открытием и расчётом, задача умирает
        вместе с ним, и запись навсегда остаётся с result=None. Панель
        показывает такую сделку в «Открытых» вечно, с отсчётом «0 с», хотя
        на площадке она давно рассчитана. Проверено живьём: сделка от
        2026-08-06 висела в открытых 46 часов.

        Ставок здесь не делается — только чтение итога, поэтому вызов
        безопасен при любом старте. Записи без trade_id (до площадки не
        дошли) закрываются как failed: спрашивать о них нечего.

        Returns:
            Сколько записей удалось закрыть.
        """
        rows = self.journal.open_positions()
        if not rows:
            return 0

        now = self.now()
        closed = 0
        loop = asyncio.get_running_loop()

        for row in rows:
            expiry_ts = row["expiry_ts"]
            # Не трогаем то, что ещё живёт: экспирация впереди — сделку
            # сопровождает своя задача этого же запуска.
            if expiry_ts and expiry_ts + SETTLE_DELAY > now:
                continue

            row_id = row["id"]
            trade_id = row["trade_id"]

            if not trade_id:
                self.journal.settle_trade(
                    row_id, "failed", pnl=0.0,
                    raw_settle="осталась без trade_id от прошлого запуска")
                self.journal.event("state", "осиротевшая сделка → FAILED",
                                   trade_ref=row_id)
                closed += 1
                continue

            if not self.config.touches_platform or not self.client:
                # Имитационные режимы к площадке не ходят: достоверно
                # рассчитать их прерванные записи уже нечем.
                self.journal.settle_trade(
                    row_id, "unknown", pnl=None,
                    raw_settle="имитация прервана остановкой бота")
                self.journal.event("state", "осиротевшая имитация → UNKNOWN",
                                   trade_ref=row_id)
                closed += 1
                continue

            try:
                result = await loop.run_in_executor(
                    None,
                    lambda tid=trade_id, inv=row["investment"]:
                        self.client.check_trade(tid, inv),
                )
            except PlatformError as err:
                # Не смогли узнать — оставляем как есть. Выдумывать итог
                # сделки, которая могла быть выигрышной, нельзя.
                log.warning("не разобрать сделку #%s (%s): %s",
                            row_id, trade_id, err)
                continue

            if result.outcome == "unknown":
                log.warning("площадка не дала итог по сделке #%s (%s)",
                            row_id, trade_id)
                continue

            self.journal.settle_trade(row_id, result.outcome, pnl=result.pnl,
                                      raw_settle=result.raw)
            self.journal.event(
                "state", f"осиротевшая сделка → CLOSED ({result.outcome})",
                trade_ref=row_id)
            closed += 1

        if closed:
            log.info("разобрано незакрытых сделок с прошлого запуска: %d", closed)
            self.journal.event("info", f"разобрано осиротевших сделок: {closed}")
        return closed

    async def run(self, source) -> None:
        """Читать сигналы источника и исполнять их до остановки.

        Args:
            source: Объект SignalSource (или результат multiplex).

        Returns:
            None.
        """
        log.info("движок запущен, режим %s", self.config.mode)
        self.journal.event("info", f"движок запущен, режим {self.config.mode}")

        # Сделки, оставшиеся незакрытыми от прошлого запуска, разбираем до
        # первого сигнала: иначе они вечно висят в «Открытых».
        try:
            await self.reconcile_orphans()
        except Exception as err:
            # Разбор старых записей не должен мешать работе движка.
            log.exception("разбор осиротевших сделок не удался: %s", err)

        async for signal in source:
            try:
                await self.handle(signal)
            except Exception as err:
                # Ни одна ошибка обработки сигнала не должна валить цикл:
                # следующий сигнал ещё может быть исполнен.
                log.exception("необработанная ошибка на сигнале: %s", err)
                self.journal.event("error", f"ошибка обработки сигнала: {err}")

        # Дожидаемся сопровождения уже открытых сделок.
        if self._settle_tasks:
            log.info("ожидание расчёта %d сделок", len(self._settle_tasks))
            await asyncio.gather(*self._settle_tasks, return_exceptions=True)

    def _notify(self, kind: str, text: str) -> None:
        """Сообщить наблюдателю о судьбе сигнала.

        Ошибка наблюдателя не должна ронять обработку сигнала: канал
        уведомлений вспомогательный.

        Args:
            kind: "rejected" или "opened".
            text: Человекочитаемая причина или описание.

        Returns:
            None.
        """
        if not self.on_outcome:
            return
        try:
            self.on_outcome(kind, text)
        except Exception as err:
            log.warning("наблюдатель исхода упал: %s", err)

    async def handle(self, signal: Signal) -> Optional[int]:
        """Провести сигнал через машину состояний.

        Args:
            signal: Сигнал стратегии.

        Returns:
            Локальный id записи в журнале либо None, если сделка не открыта.
        """
        self.processed += 1
        self.state = State.VALIDATING

        rejection = self._validate(signal)
        if rejection:
            self.state = State.REJECTED
            self.rejected += 1
            log.info("сигнал отклонён (%s): %s %s", rejection,
                     signal.symbol, signal.direction)
            self.journal.event("risk", f"отказ: {rejection}",
                               detail=f"{signal.symbol} {signal.direction} "
                                      f"от {signal.source}")
            self._notify("rejected", rejection)
            return None

        investment = signal.amount or self.config.default_investment
        quote_at_signal = self.quotes.mid(signal.symbol) if self.quotes else None

        # Процент выплаты перезапрашиваем перед каждой ставкой: он плавает,
        # а в окне у начала часа падает с 82% до 60%.
        payout_percent = await self._current_payout(signal, investment)

        if self.risk and payout_percent is not None:
            verdict = self.risk.check_payout(payout_percent)
            if verdict:
                self.state = State.REJECTED
                self.rejected += 1
                log.info("сигнал отклонён (%s): выплата %s%%", verdict, payout_percent)
                self.journal.event("risk", f"отказ: {verdict}",
                                   detail=f"выплата {payout_percent}%")
                self._notify("rejected", verdict)
                return None

        self.state = State.SENDING
        if self.config.touches_platform:
            # ПОСЛЕДНИЙ РУБЕЖ перед реальной ставкой: убедиться, что активен
            # ожидаемый счёт. Площадка не сообщает тип счёта через API —
            # см. _verify_account.
            refusal = await self._verify_account()
            if refusal:
                self.state = State.REJECTED
                self.rejected += 1
                log.error("ставка отменена: %s", refusal)
                self.journal.event("risk", f"отказ: {refusal}")
                self._notify("rejected", refusal)
                return None
            return await self._send_real(signal, investment, quote_at_signal,
                                         payout_percent)
        return await self._send_simulated(signal, investment, quote_at_signal,
                                          payout_percent)

    async def _verify_account(self) -> Optional[str]:
        """Проверить, что активен тот счёт, на котором мы согласны торговать.

        Авторитетный источник — /profile (INTRADE_API_MAP.md §3.1, разбор
        2026-08-06): сервер рендерит checked на активном варианте каждой
        настройки. До этого единственным признаком была эвристика по величине
        баланса; она оставлена ВТОРОЙ линией обороны — /profile парсится из
        HTML, и при смене вёрстки хрупки оба источника, но по-разному.

        Расхождение с конфигом — ЖЁСТКИЙ СТОП, а не переключение на лету
        (требование §3.1: тумблер без параметра слишком опасен, чтобы
        дёргать его в торговом цикле). Приведение счёта к конфигу — только
        явное действие оператора (ensure_account_mode при старте).

        Проверка делается перед КАЖДОЙ ставкой, а не раз при старте:
        переключить счёт можно в любой момент, в том числе посреди прогона.

        Returns:
            Причина отказа либо None, если счёт соответствует конфигу.
        """
        if not self.client:
            return None

        loop = asyncio.get_running_loop()

        # ── первая линия: /profile, авторитетное состояние ──
        try:
            profile = await loop.run_in_executor(None, self.client.profile)
        except PlatformError as err:
            # Не смогли прочитать профиль — не ставим. «Не смог проверить»
            # и «проверил, всё в порядке» — принципиально разные состояния.
            return f"не прочитать /profile: {err}"

        if profile.account != self.config.account:
            return (
                f"на площадке активен счёт «{profile.account}», конфиг требует "
                f"«{self.config.account}» — жёсткий стоп. Переключение на лету "
                f"запрещено; приведите счёт осознанно (ensure_account_mode "
                f"на старте или в кабинете)"
            )
        # Тип сделок уровня аккаунта обязан совпадать с тем, что мы шлём в
        # ajax5_new.php ("sprint"): поведение при расхождении неизвестно,
        # а проверять его ставкой — не наш метод (§3.1, п.3).
        if profile.trade_type != "sprint":
            return (
                f"тип сделок аккаунта «{profile.trade_type}», бот отправляет "
                f"sprint — поведение площадки при расхождении неизвестно"
            )
        # При RUB другие суммы и лимиты, а ajax_percent зависит от валюты
        # (§3.1, п.4). Конфиг и ограничители рассчитаны на USD.
        if profile.currency != "usd":
            return (
                f"валюта счёта «{profile.currency}», ограничители и ставки "
                f"рассчитаны на usd"
            )

        # ── вторая линия: эвристика по балансу (только для демо) ──
        if self.config.account != "demo":
            # Явно объявленная торговля на реале эвристику не проходит —
            # там решение принимает человек, а не порог по балансу.
            return None

        try:
            balance = await loop.run_in_executor(None, self.client.balance)
        except PlatformError as err:
            # Не смогли узнать баланс — не ставим. Слепая ставка при
            # неизвестном счёте недопустима.
            return f"не проверить счёт: {err}"

        threshold = getattr(self.config, "min_balance_for_demo", 1000.0)
        if balance.amount < threshold:
            return (
                f"баланс {balance.amount:.2f} {balance.currency} ниже порога "
                f"{threshold:.0f} — похоже, на площадке активен РЕАЛЬНЫЙ счёт, "
                f"а конфиг требует демо (и /profile разошёлся с балансом — "
                f"один из источников лжёт). Разберитесь вручную"
            )
        return None

    # ── проверки ───────────────────────────────────────────────────────

    def _validate(self, signal: Signal) -> Optional[str]:
        """Проверить, можно ли исполнять сигнал.

        Порядок проверок — от самых дешёвых и категоричных к остальным.
        Kill-switch первым: он должен срабатывать мгновенно и безусловно.

        Args:
            signal: Сигнал стратегии.

        Returns:
            Причина отказа либо None, если сигнал допущен.
        """
        if kill_switch_active(self.config.stop_file):
            return "kill-switch взведён"

        if signal.symbol not in self.config.symbol_whitelist:
            return f"инструмент {signal.symbol} не в белом списке"

        age = time.time() - signal.ts
        if age > MAX_SIGNAL_AGE:
            # Площадка добавит свои ~3 секунды: протухший сигнал приведёт
            # к входу совсем не там, где решала стратегия.
            return f"сигнал протух ({age:.1f} с)"

        if self.quotes and not self.quotes.fresh:
            return "котировки протухли"

        if self.risk:
            verdict = self.risk.check(signal)
            if verdict:
                return verdict

        return None

    async def _current_payout(self, signal: Signal,
                              investment: float) -> Optional[int]:
        """Узнать актуальный процент выплаты.

        В режимах без выхода к площадке берётся ожидаемое по сетке значение
        — сеть для этого дёргать не нужно.

        Args:
            signal:     Сигнал стратегии.
            investment: Размер ставки.

        Returns:
            Процент выплаты либо None, если узнать не удалось.
        """
        if not self.config.touches_platform or not self.client:
            from bot import payout

            return payout.expected_percent(signal.expiry_minutes, investment)

        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(
                None,
                lambda: self.client.payout_percent(
                    signal.symbol,
                    expiry_minutes=signal.expiry_minutes,
                    investment=investment,
                ),
            )
        except PlatformError as err:
            log.warning("не узнать процент выплаты: %s", err)
            self.journal.event("error", "не узнать процент выплаты", detail=str(err))
            return None

    # ── отправка ───────────────────────────────────────────────────────

    async def _send_real(self, signal: Signal, investment: float,
                         quote_at_signal: Optional[float],
                         payout_percent: Optional[int]) -> Optional[int]:
        """Отправить настоящий запрос на открытие сделки.

        РЕТРАЕВ ЗДЕСЬ НЕТ И БЫТЬ НЕ МОЖЕТ. При обрыве связи сделка могла
        открыться на стороне площадки, и повтор дал бы двойную ставку.

        Args:
            signal:          Сигнал стратегии.
            investment:      Размер ставки.
            quote_at_signal: Котировка в момент сигнала.
            payout_percent:  Процент выплаты.

        Returns:
            Локальный id записи в журнале либо None.
        """
        quote_at_request = self.quotes.mid(signal.symbol) if self.quotes else None
        loop = asyncio.get_running_loop()

        try:
            trade, request_ts = await loop.run_in_executor(
                None,
                lambda: self.client.open_trade(
                    symbol=signal.symbol,
                    direction=signal.direction,
                    investment=investment,
                    expiry_minutes=signal.expiry_minutes,
                ),
            )
        except TradeUnknown as err:
            # Ответ не получен — судьба ставки неизвестна. Записываем как
            # есть и разрешаем сверкой, а НЕ повтором.
            self.state = State.UNKNOWN
            self.failed += 1
            log.error("сделка в состоянии UNKNOWN: %s", err)
            row_id = self.journal.open_trade(
                mode=self.config.mode, symbol=signal.symbol,
                direction=signal.direction, investment=investment,
                expiry_minutes=signal.expiry_minutes, source=signal.source,
                signal_ts=signal.ts, request_ts=time.time(),
                quote_at_signal=quote_at_signal,
                quote_at_request=quote_at_request,
                payout_percent=payout_percent, raw_response=str(err),
                meta=signal.meta,
            )
            self.journal.event("state", "SENDING → UNKNOWN", detail=str(err),
                               trade_ref=row_id)
            task = asyncio.create_task(self._resolve_unknown(row_id, signal))
            self._settle_tasks.add(task)
            task.add_done_callback(self._settle_tasks.discard)
            return row_id
        except PlatformError as err:
            # Площадка внятно отказала — ставки нет, сверять нечего.
            self.state = State.FAILED
            self.failed += 1
            log.warning("площадка отказала: %s", err)
            row_id = self.journal.open_trade(
                mode=self.config.mode, symbol=signal.symbol,
                direction=signal.direction, investment=investment,
                expiry_minutes=signal.expiry_minutes, source=signal.source,
                signal_ts=signal.ts, request_ts=time.time(),
                quote_at_signal=quote_at_signal,
                quote_at_request=quote_at_request,
                payout_percent=payout_percent, raw_response=err.raw or str(err),
                meta=signal.meta,
            )
            self.journal.settle_trade(row_id, "failed", pnl=0.0,
                                      raw_settle=str(err))
            self.journal.event("error", f"отказ площадки: {err.code}",
                               detail=err.raw or str(err), trade_ref=row_id)
            self._notify("rejected", f"площадка отказала: {err}")
            return row_id

        self.state = State.OPEN
        row_id = self.journal.open_trade(
            mode=self.config.mode, symbol=signal.symbol,
            direction=signal.direction, investment=investment,
            expiry_minutes=signal.expiry_minutes, source=signal.source,
            signal_ts=signal.ts, request_ts=request_ts,
            open_ts=trade.open_ts, expiry_ts=trade.expiry_ts,
            trade_id=trade.trade_id, entry_price=trade.entry_price,
            quote_at_signal=quote_at_signal, quote_at_request=quote_at_request,
            payout_percent=payout_percent, raw_response=trade.raw,
            meta=signal.meta,
        )
        self.journal.event("state", "SENDING → OPEN", trade_ref=row_id)
        self._notify("opened",
                     f"{signal.direction.upper()} {signal.symbol} открыта, "
                     f"ставка {investment}")
        # Кулдаун и лимит одновременных сделок считаются от ФАКТА открытия,
        # а не от попытки: отказ площадки паузу начинать не должен.
        if self.risk:
            self.risk.register_open()

        task = asyncio.create_task(self._settle(row_id, trade, investment))
        self._settle_tasks.add(task)
        task.add_done_callback(self._settle_tasks.discard)
        return row_id

    async def _send_simulated(self, signal: Signal, investment: float,
                              quote_at_signal: Optional[float],
                              payout_percent: Optional[int]) -> Optional[int]:
        """Имитировать сделку: режимы dry и shadow.

        Задержка открытия моделируется по живым замерам площадки: если в
        имитации сделка открывается мгновенно, отладка пройдёт в условиях,
        которых в реальности не существует.

        Args:
            signal:          Сигнал стратегии.
            investment:      Размер ставки.
            quote_at_signal: Котировка в момент сигнала.
            payout_percent:  Процент выплаты.

        Returns:
            Локальный id записи в журнале.
        """
        request_ts = time.time()
        latency = random.uniform(*DRY_LATENCY_RANGE)

        if self.config.mode == "shadow":
            # shadow: ставку НЕ делаем, только записываем намерение.
            row_id = self.journal.open_trade(
                mode=self.config.mode, symbol=signal.symbol,
                direction=signal.direction, investment=investment,
                expiry_minutes=signal.expiry_minutes, source=signal.source,
                signal_ts=signal.ts, request_ts=request_ts,
                quote_at_signal=quote_at_signal,
                quote_at_request=quote_at_signal,
                payout_percent=payout_percent,
                raw_response="[shadow] запрос не отправлялся",
                meta=signal.meta,
            )
            self.journal.settle_trade(row_id, "shadow", pnl=0.0,
                                      raw_settle="[shadow] ставка не делалась")
            self.journal.event("state", "SENDING → SHADOW (без ставки)",
                               trade_ref=row_id)
            log.info("[shadow] сделал бы: %s %s, ставка %s",
                     signal.symbol, signal.direction, investment)
            self.state = State.CLOSED
            return row_id

        # dry: ждём смоделированную задержку и «открываемся» по текущей цене.
        await asyncio.sleep(latency)
        open_ts = request_ts + latency
        entry_price = (self.quotes.mid(signal.symbol) if self.quotes
                       else quote_at_signal)
        expiry_ts = open_ts + signal.expiry_minutes * 60

        self.state = State.OPEN
        row_id = self.journal.open_trade(
            mode=self.config.mode, symbol=signal.symbol,
            direction=signal.direction, investment=investment,
            expiry_minutes=signal.expiry_minutes, source=signal.source,
            signal_ts=signal.ts, request_ts=request_ts, open_ts=open_ts,
            expiry_ts=expiry_ts, entry_price=entry_price,
            quote_at_signal=quote_at_signal, quote_at_request=entry_price,
            payout_percent=payout_percent,
            raw_response="[dry] сделка имитирована",
            meta=signal.meta,
        )
        self.journal.event("state", "SENDING → OPEN (имитация)", trade_ref=row_id)
        if self.risk:
            self.risk.register_open()

        fake_trade = Trade(
            trade_id=0, symbol=signal.symbol, direction=signal.direction,
            investment=investment, entry_price=entry_price or 0.0,
            open_ts=int(open_ts), expiry_ts=int(expiry_ts),
            duration=signal.expiry_minutes * 60, raw="[dry]",
        )
        task = asyncio.create_task(self._settle(row_id, fake_trade, investment))
        self._settle_tasks.add(task)
        task.add_done_callback(self._settle_tasks.discard)
        return row_id

    # ── расчёт ─────────────────────────────────────────────────────────

    async def _settle(self, row_id: int, trade: Trade,
                      investment: float) -> None:
        """Дождаться экспирации и записать итог сделки.

        Счётчик открытых сделок освобождается в finally: если расчёт упадёт
        с исключением, место в лимите max_concurrent обязано вернуться,
        иначе бот замолчит навсегда после первой же ошибки.

        Args:
            row_id:     Локальный id записи в журнале.
            trade:      Открытая сделка.
            investment: Размер ставки.

        Returns:
            None.
        """
        try:
            await self._settle_inner(row_id, trade, investment)
        finally:
            if self.risk:
                self.risk.register_close()

    async def _settle_inner(self, row_id: int, trade: Trade,
                            investment: float) -> None:
        """Собственно ожидание экспирации и запись итога.

        Args:
            row_id:     Локальный id записи в журнале.
            trade:      Открытая сделка.
            investment: Размер ставки.

        Returns:
            None.
        """
        self.state = State.SETTLING

        # Ждём по времени ПЛОЩАДКИ, а не по локальному.
        wait = trade.expiry_ts - self.now() + SETTLE_DELAY
        if wait > 0:
            await asyncio.sleep(wait)

        if not self.config.touches_platform:
            await self._settle_simulated(row_id, trade, investment)
            return

        loop = asyncio.get_running_loop()
        for attempt in range(SETTLE_ATTEMPTS):
            try:
                result = await loop.run_in_executor(
                    None, lambda: self.client.check_trade(trade.trade_id, investment)
                )
            except PlatformError as err:
                log.warning("проверка сделки %d не удалась: %s", trade.trade_id, err)
                await asyncio.sleep(SETTLE_INTERVAL)
                continue

            if result.outcome != "unknown":
                self.journal.settle_trade(row_id, result.outcome, pnl=result.pnl,
                                          raw_settle=result.raw)
                self.journal.event("state", f"SETTLING → CLOSED ({result.outcome})",
                                   trade_ref=row_id)
                self.state = State.CLOSED
                return

            await asyncio.sleep(SETTLE_INTERVAL)

        # Итог так и не получен: записываем честно, а не выдумываем.
        self.journal.settle_trade(row_id, "unknown", pnl=None,
                                  raw_settle="итог не получен за все попытки")
        self.journal.event("error", "итог сделки не получен", trade_ref=row_id)
        self.state = State.CLOSED

    async def _settle_simulated(self, row_id: int, trade: Trade,
                                investment: float) -> None:
        """Рассчитать имитированную сделку по котировкам площадки.

        Сравнивается цена входа с ценой на момент экспирации. Ровное
        равенство — возврат (площадка так и делает при неизменной цене).

        Args:
            row_id:     Локальный id записи.
            trade:      Имитированная сделка.
            investment: Размер ставки.

        Returns:
            None.
        """
        final_price = (self.quotes.mid(trade.symbol) if self.quotes else None)
        row = self.journal.get_trade(row_id)
        payout_percent = (row["payout_percent"] if row else None) or 82

        if final_price is None or not trade.entry_price:
            self.journal.settle_trade(row_id, "unknown", pnl=None,
                                      raw_settle="[dry] нет котировки для расчёта")
            self.state = State.CLOSED
            return

        delta = final_price - trade.entry_price
        if abs(delta) < 1e-9:
            outcome, pnl = "refund", 0.0
        elif (delta > 0) == (trade.direction == "call"):
            outcome = "win"
            pnl = round(investment * payout_percent / 100.0, 2)
        else:
            outcome, pnl = "loss", -float(investment)

        self.journal.settle_trade(
            row_id, outcome, pnl=pnl,
            raw_settle=f"[dry] вход {trade.entry_price} → выход {final_price}",
        )
        self.journal.event("state", f"SETTLING → CLOSED ({outcome}, имитация)",
                           trade_ref=row_id)
        self.state = State.CLOSED

    async def _resolve_unknown(self, row_id: int, signal: Signal) -> None:
        """Разрешить состояние UNKNOWN сверкой, а не повтором ставки.

        Ответ на открытие потерялся, но сделка могла открыться. Спрашиваем
        у площадки список активных сделок и ищем в нём наш инструмент.
        Повторять ставку нельзя ни при каких обстоятельствах.

        Args:
            row_id: Локальный id записи в журнале.
            signal: Исходный сигнал.

        Returns:
            None.
        """
        if not self.client:
            return

        from bot.api.client import to_platform_symbol

        loop = asyncio.get_running_loop()
        await asyncio.sleep(5.0)  # даём площадке время показать сделку

        try:
            raw = await loop.run_in_executor(None, self.client.active_trades)
        except PlatformError as err:
            log.error("сверка UNKNOWN не удалась: %s", err)
            self.journal.event("error", "сверка UNKNOWN не удалась",
                               detail=str(err), trade_ref=row_id)
            return

        needle = to_platform_symbol(signal.symbol)
        if needle in (raw or ""):
            log.warning("сделка по %s НАШЛАСЬ при сверке — ставка была принята",
                        signal.symbol)
            self.journal.event("state", "UNKNOWN → OPEN (найдена сверкой)",
                               detail=raw[:2000], trade_ref=row_id)
            self.journal.update_trade(row_id, raw_response=raw[:5000])
        else:
            log.info("сделка по %s при сверке не найдена — ставка не прошла",
                     signal.symbol)
            self.journal.settle_trade(row_id, "failed", pnl=0.0,
                                      raw_settle="сверкой не найдена")
            self.journal.event("state", "UNKNOWN → FAILED (сверкой не найдена)",
                               trade_ref=row_id)

    # ── сводка ─────────────────────────────────────────────────────────

    def summary(self) -> dict:
        """Сводка работы движка — для панели и логов.

        Returns:
            Словарь со счётчиками и текущим состоянием.
        """
        return {
            "state": self.state.value,
            "mode": self.config.mode,
            "processed": self.processed,
            "rejected": self.rejected,
            "failed": self.failed,
            "settling": len(self._settle_tasks),
        }
