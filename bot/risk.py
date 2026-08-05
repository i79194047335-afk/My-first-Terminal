"""
Ограничители входа. Python 3.10.

Всё, что может запретить сделку, собрано здесь. Ни одной проверки «по месту»
в движке: иначе через месяц никто не скажет, при каких условиях бот точно
не войдёт, а именно это надо знать про торгового робота в первую очередь.

Каждая проверка возвращает СТРОКУ С ПРИЧИНОЙ отказа либо None (разрешено).
Строка идёт в журнал и в панель — «отказ» без причины бесполезен, когда
разбираешься, почему бот молчал весь день.

Порядок проверок в check() — от безусловных к статистическим:
  1. kill-switch      — выключатель человека, важнее любой логики;
  2. окно выплаты     — вход при 60% требует WR 62.5% вместо 54.9%;
  3. белый список     — торгуем только то, что разрешено;
  4. время суток      — allowed_hours;
  5. одновременность  — max_concurrent;
  6. кулдаун          — cooldown_sec;
  7. лимит за сутки   — max_trades_per_day;
  8. серия убытков    — max_consecutive_losses;
  9. просадка за день — max_daily_loss.

Сработавший стоп-ограничитель (серия убытков, дневная просадка) НЕ снимается
сам: по ТЗ требуется перезапуск или явное действие человека. Это защита от
бота, который «отыгрывается» после серии убытков.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Optional

from bot import payout
from bot.journal import Journal, kill_switch_active
from bot.strategy.base import Signal
from core.logfmt import setup as _log_setup

log = _log_setup("bot-risk")


class RiskManager:
    """Ограничители входа поверх конфигурации и журнала.

    Состояние (время последней сделки, взведённые стопы) держится в памяти
    процесса, а статистика берётся из журнала — она переживает перезапуск.
    """

    def __init__(self, config, journal: Journal):
        """Создать ограничители.

        Args:
            config:  BotConfig с секцией risk.
            journal: Журнал — источник статистики за сутки.
        """
        self.config = config
        self.risk = config.risk
        self.journal = journal

        self.last_trade_ts = 0.0
        self.open_count = 0
        # Взведённый стоп: причина, по которой торговля остановлена совсем.
        # Снимается только через release() — то есть руками.
        self.halted_reason: Optional[str] = None

    # ── основная проверка ──────────────────────────────────────────────

    def check(self, signal: Signal) -> Optional[str]:
        """Проверить сигнал всеми ограничителями.

        Args:
            signal: Сигнал стратегии.

        Returns:
            Причина отказа либо None, если вход разрешён.
        """
        if self.halted_reason:
            return f"торговля остановлена: {self.halted_reason}"

        if kill_switch_active(self.config.stop_file):
            return "kill-switch взведён"

        # Окно пониженной выплаты. Проверяется до сетевых вызовов: незачем
        # спрашивать процент, если входить всё равно нельзя.
        if payout.is_hour_edge():
            return ("окно у начала часа: выплата 60%, "
                    "безубыточный винрейт 62.5%")

        if signal.symbol not in self.config.symbol_whitelist:
            return f"инструмент {signal.symbol} не в белом списке"

        verdict = self.check_hours()
        if verdict:
            return verdict

        if self.open_count >= self.risk.max_concurrent:
            return (f"уже открыто {self.open_count} сделок "
                    f"(лимит {self.risk.max_concurrent})")

        since_last = time.time() - self.last_trade_ts
        if self.last_trade_ts and since_last < self.risk.cooldown_sec:
            return (f"кулдаун: осталось "
                    f"{self.risk.cooldown_sec - since_last:.0f} с")

        stats = self.journal.stats_today()

        if stats["trades"] >= self.risk.max_trades_per_day:
            return (f"дневной лимит сделок исчерпан "
                    f"({stats['trades']}/{self.risk.max_trades_per_day})")

        if stats["consecutive_losses"] >= self.risk.max_consecutive_losses:
            # Это стоп, а не отказ: он взводится и держится до вмешательства.
            reason = (f"{stats['consecutive_losses']} убытков подряд "
                      f"(лимит {self.risk.max_consecutive_losses})")
            self.halt(reason)
            return f"торговля остановлена: {reason}"

        if stats["pnl"] <= -abs(self.risk.max_daily_loss):
            reason = (f"дневная просадка {stats['pnl']:.2f} "
                      f"(лимит {self.risk.max_daily_loss})")
            self.halt(reason)
            return f"торговля остановлена: {reason}"

        return None

    def check_payout(self, percent: Optional[int]) -> Optional[str]:
        """Проверить процент выплаты отдельно.

        Зовётся движком после запроса актуального процента у площадки —
        то есть уже зная настоящее число, а не ожидаемое по сетке.

        Args:
            percent: Процент выплаты либо None, если узнать не удалось.

        Returns:
            Причина отказа либо None.
        """
        if percent is None:
            # Не знаем выплату — не входим. Ставить вслепую нельзя: при 60%
            # вместо 82% экономика сделки меняется принципиально.
            return "процент выплаты неизвестен"

        if percent <= 0:
            # Ноль — способ площадки сказать «сейчас не торгуется».
            return "выплата 0%: инструмент или экспирация недоступны"

        if percent < self.risk.min_payout_percent:
            breakeven = payout.breakeven_winrate(percent)
            return (f"выплата {percent}% ниже порога "
                    f"{self.risk.min_payout_percent}% "
                    f"(нужен винрейт {breakeven:.1f}%)")

        return None

    def check_hours(self, moment: Optional[datetime] = None) -> Optional[str]:
        """Проверить, разрешена ли торговля в это время суток.

        Окна заданы в часах UTC: allowed_hours = [[6, 18]] означает с 06:00
        до 18:00 UTC. Пустой список означает «без ограничений».

        Args:
            moment: Момент времени; None — сейчас.

        Returns:
            Причина отказа либо None.
        """
        windows = self.risk.allowed_hours or []
        if not windows:
            return None

        moment = moment or datetime.utcnow()
        hour = moment.hour

        for window in windows:
            if len(window) != 2:
                continue
            start, end = window
            if start <= end:
                if start <= hour < end:
                    return None
            else:
                # Окно через полночь: [22, 6] — это 22:00–23:59 и 00:00–05:59.
                if hour >= start or hour < end:
                    return None

        return f"час {hour}:00 UTC вне разрешённых окон {windows}"

    # ── учёт сделок ────────────────────────────────────────────────────

    def register_open(self) -> None:
        """Отметить, что сделка открыта.

        Зовётся движком после успешной отправки. Влияет на кулдаун и на
        счётчик одновременных сделок.

        Returns:
            None.
        """
        self.last_trade_ts = time.time()
        self.open_count += 1

    def register_close(self) -> None:
        """Отметить, что сделка закрыта.

        Returns:
            None.
        """
        self.open_count = max(0, self.open_count - 1)

    # ── стоп ───────────────────────────────────────────────────────────

    def halt(self, reason: str) -> None:
        """Остановить торговлю до вмешательства человека.

        Args:
            reason: Причина остановки.

        Returns:
            None.
        """
        if self.halted_reason:
            return
        self.halted_reason = reason
        log.warning("ТОРГОВЛЯ ОСТАНОВЛЕНА: %s", reason)
        self.journal.event("risk", f"стоп: {reason}")

    def release(self) -> bool:
        """Снять стоп вручную.

        Отдельный метод, а не автоматика: сработавший стоп по ТЗ не должен
        сниматься сам, иначе бот продолжит серию убытков после паузы.

        Returns:
            True, если стоп был взведён и снят.
        """
        if not self.halted_reason:
            return False
        log.info("стоп снят вручную (было: %s)", self.halted_reason)
        self.journal.event("risk", f"стоп снят вручную (было: {self.halted_reason})")
        self.halted_reason = None
        return True

    # ── сводка ─────────────────────────────────────────────────────────

    def status(self) -> dict:
        """Текущее состояние ограничителей — для панели.

        Returns:
            Словарь с лимитами, расходом и причиной стопа.
        """
        stats = self.journal.stats_today()
        since_last = time.time() - self.last_trade_ts if self.last_trade_ts else None
        cooldown_left = None
        if since_last is not None and since_last < self.risk.cooldown_sec:
            cooldown_left = round(self.risk.cooldown_sec - since_last)

        return {
            "halted": self.halted_reason,
            "kill_switch": kill_switch_active(self.config.stop_file),
            "hour_edge": payout.is_hour_edge(),
            "minutes_to_hour_edge": round(payout.minutes_until_hour_edge(), 1),
            "trades_today": stats["trades"],
            "max_trades_per_day": self.risk.max_trades_per_day,
            "consecutive_losses": stats["consecutive_losses"],
            "max_consecutive_losses": self.risk.max_consecutive_losses,
            "pnl_today": round(stats["pnl"], 2),
            "max_daily_loss": self.risk.max_daily_loss,
            "open_count": self.open_count,
            "max_concurrent": self.risk.max_concurrent,
            "cooldown_left_sec": cooldown_left,
        }
