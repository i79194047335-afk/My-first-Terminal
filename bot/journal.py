"""
Журнал бота: SQLite. Python 3.10.

Отдельный файл БД (bot_journal.db). Боевую market.db не трогаем ни при
каких обстоятельствах — там свечи терминала, и смешивать их со сделками
незачем.

Две таблицы:
  * trades — сделки целиком, от сигнала до расчёта;
  * events — переходы состояний, ошибки, срабатывания ограничителей.

ЗАЧЕМ ЭТО ВСЁ, кроме бухгалтерии: главный практический результат Слоя 7 —
распределение latency_ms. Задержка открытия у площадки ~3 секунды (два
замера при разведке: 2910 и 3559 мс), тогда как обычные вызовы отвечают за
~200 мс. Если на волатильности задержка растёт, минутная экспирация может
оказаться неприменимой в принципе — и узнать это надо ДО написания
стратегии, а не после. Поэтому latency_ms пишется по каждой сделке, а
report_latency() строит по нему разрез.

Правила, которых держится модуль:
  1. Сырой ответ площадки сохраняется ВСЕГДА — и при успехе, и при сбое.
     Парсинг HTML хрупкий; когда он однажды сломается, восстанавливать
     историю будет не из чего, если сырой текст не сохранён.
  2. Signal.meta пишется целиком, как есть. Ядро в него не заглядывает,
     но стратегия потом разложит результаты по своим признакам без
     миграции схемы.
  3. Запись в журнал не должна ронять торговлю. Любая ошибка БД
     логируется и проглатывается: потерять запись хуже, чем потерять
     сделку, но уронить процесс из-за журнала — хуже всего.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Optional

from core.logfmt import setup as _log_setup

log = _log_setup("bot-journal")

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id        INTEGER,           -- id площадки; NULL в dry/shadow
    mode            TEXT NOT NULL,     -- dry / shadow / demo / live
    source          TEXT,              -- Signal.source
    symbol          TEXT NOT NULL,
    direction       TEXT NOT NULL,     -- call / put
    investment      REAL NOT NULL,
    expiry_minutes  INTEGER NOT NULL,

    signal_ts       REAL,              -- когда возник сигнал
    request_ts      REAL,              -- когда ушёл POST
    open_ts         REAL,              -- data-timeopen от площадки
    expiry_ts       REAL,              -- время экспирации

    entry_price     REAL,              -- data-rate: цена, зафиксированная площадкой
    quote_at_signal REAL,              -- price_now в момент сигнала
    quote_at_request REAL,             -- price_now в момент отправки

    latency_ms      INTEGER,           -- open_ts - request_ts: КЛЮЧЕВАЯ МЕТРИКА
    payout_percent  INTEGER,

    result          TEXT,              -- win / loss / refund / unknown
    pnl             REAL,
    settled_ts      REAL,              -- когда узнали итог

    raw_response    TEXT,              -- сырой ответ на открытие
    raw_settle      TEXT,              -- сырой ответ на проверку итога
    meta_json       TEXT,              -- Signal.meta как есть
    created_ts      REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trades_trade_id ON trades(trade_id);
CREATE INDEX IF NOT EXISTS idx_trades_created ON trades(created_ts);
CREATE INDEX IF NOT EXISTS idx_trades_result ON trades(result);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL,
    kind       TEXT NOT NULL,   -- state / error / risk / info
    trade_ref  INTEGER,         -- trades.id, если событие про сделку
    message    TEXT NOT NULL,
    detail     TEXT,            -- сырой ответ, стек, контекст
    created_ts REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind);
"""


class Journal:
    """Журнал сделок и событий в SQLite.

    Соединение держится открытым: бот однопоточный (asyncio), сетевые
    вызовы уходят в executor, а запись в журнал быстрая и делается из
    основного цикла.
    """

    def __init__(self, path: str = "bot_journal.db"):
        """Открыть журнал, создав схему при необходимости.

        Args:
            path: Путь к файлу БД. Каталог создаётся, если его нет.
        """
        self.path = path
        folder = os.path.dirname(os.path.abspath(path))
        if folder and not os.path.isdir(folder):
            os.makedirs(folder, exist_ok=True)

        self.conn = sqlite3.connect(path, timeout=10)
        self.conn.row_factory = sqlite3.Row
        # WAL: журнал пишется параллельно с чтением из панели, не блокируя её.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        log.info("журнал открыт: %s", path)

    def close(self) -> None:
        """Закрыть соединение с БД.

        Returns:
            None.
        """
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

    # ── сделки ─────────────────────────────────────────────────────────

    def open_trade(
        self,
        mode: str,
        symbol: str,
        direction: str,
        investment: float,
        expiry_minutes: int,
        source: str = "",
        signal_ts: Optional[float] = None,
        request_ts: Optional[float] = None,
        open_ts: Optional[float] = None,
        expiry_ts: Optional[float] = None,
        trade_id: Optional[int] = None,
        entry_price: Optional[float] = None,
        quote_at_signal: Optional[float] = None,
        quote_at_request: Optional[float] = None,
        payout_percent: Optional[int] = None,
        raw_response: str = "",
        meta: Optional[dict] = None,
    ) -> int:
        """Записать открытую (или сымитированную) сделку.

        latency_ms считается здесь, а не вызывающим: формула одна на весь
        проект, и дублировать её нельзя. Считается только когда есть обе
        метки — в dry-режиме открытия по версии площадки не существует.

        Args:
            mode:             Режим бота.
            symbol:           Инструмент со слэшем.
            direction:        "call" / "put".
            investment:       Размер ставки.
            expiry_minutes:   Экспирация в минутах.
            source:           Signal.source.
            signal_ts:        Время возникновения сигнала.
            request_ts:       Время отправки POST.
            open_ts:          Время открытия по версии площадки.
            expiry_ts:        Время экспирации.
            trade_id:         Идентификатор площадки.
            entry_price:      Цена входа.
            quote_at_signal:  Котировка в момент сигнала.
            quote_at_request: Котировка в момент отправки.
            payout_percent:   Процент выплаты на входе.
            raw_response:     Сырой ответ площадки.
            meta:             Signal.meta.

        Returns:
            Локальный id записи (trades.id); 0 при ошибке записи.
        """
        latency_ms = None
        if open_ts and request_ts:
            # round, а НЕ int: int усекает вниз, и на потере точности float
            # (base + 2.8 даёт 2799.9997) задержка систематически занижалась
            # бы на миллисекунду. Метрика ключевая — врать не должна.
            #
            # ТОЧНОСТЬ САМОЙ МЕТРИКИ — ±1 секунда, и лучше не будет: площадка
            # отдаёт data-timeopen целыми секундами. Поэтому осмысленны
            # только выводы вроде «медиана около 3 с» и «на волатильности
            # выросла вдвое»; отличить 3.1 с от 3.4 с на одной сделке нельзя,
            # но на выборке из 20+ медиана уже показательна.
            latency_ms = round((open_ts - request_ts) * 1000)

        try:
            cursor = self.conn.execute(
                """INSERT INTO trades (
                       trade_id, mode, source, symbol, direction, investment,
                       expiry_minutes, signal_ts, request_ts, open_ts, expiry_ts,
                       entry_price, quote_at_signal, quote_at_request,
                       latency_ms, payout_percent, result, raw_response,
                       meta_json, created_ts)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    trade_id, mode, source, symbol, direction, investment,
                    expiry_minutes, signal_ts, request_ts, open_ts, expiry_ts,
                    entry_price, quote_at_signal, quote_at_request,
                    latency_ms, payout_percent, None, raw_response,
                    json.dumps(meta or {}, ensure_ascii=False), time.time(),
                ),
            )
            self.conn.commit()
            row_id = cursor.lastrowid
        except sqlite3.Error as err:
            # Журнал не имеет права ронять торговлю.
            log.error("не записать сделку в журнал: %s", err)
            return 0

        if latency_ms is not None:
            log.info("сделка %s записана (#%d), задержка открытия %d мс",
                     trade_id or "—", row_id, latency_ms)
        return row_id

    def settle_trade(
        self,
        row_id: int,
        result: str,
        pnl: Optional[float] = None,
        raw_settle: str = "",
    ) -> None:
        """Проставить итог сделки после расчёта.

        Args:
            row_id:     Локальный id записи из open_trade.
            result:     "win" / "loss" / "refund" / "unknown".
            pnl:        Прибыль/убыток.
            raw_settle: Сырой ответ проверки итога.

        Returns:
            None.
        """
        try:
            self.conn.execute(
                """UPDATE trades
                      SET result = ?, pnl = ?, raw_settle = ?, settled_ts = ?
                    WHERE id = ?""",
                (result, pnl, raw_settle, time.time(), row_id),
            )
            self.conn.commit()
        except sqlite3.Error as err:
            log.error("не записать итог сделки #%d: %s", row_id, err)
            return
        log.info("сделка #%d рассчитана: %s, pnl %s", row_id, result, pnl)

    def update_trade(self, row_id: int, **fields) -> None:
        """Обновить произвольные поля записи о сделке.

        Нужно для разрешения состояния UNKNOWN: когда сделка нашлась
        сверкой через trade_load_more2.php, у неё появляется trade_id и
        время открытия, которых при записи не было.

        Args:
            row_id: Локальный id записи.
            fields: Пары имя_столбца=значение.

        Returns:
            None.
        """
        allowed = {
            "trade_id", "open_ts", "expiry_ts", "entry_price", "latency_ms",
            "payout_percent", "result", "pnl", "raw_response", "raw_settle",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return

        # Пересчитываем задержку, если появилось время открытия.
        if "open_ts" in updates and "latency_ms" not in updates:
            row = self.get_trade(row_id)
            if row and row["request_ts"] and updates["open_ts"]:
                # round по той же причине, что и в open_trade.
                updates["latency_ms"] = round(
                    (updates["open_ts"] - row["request_ts"]) * 1000
                )

        assignments = ", ".join(f"{name} = ?" for name in updates)
        try:
            self.conn.execute(
                f"UPDATE trades SET {assignments} WHERE id = ?",
                list(updates.values()) + [row_id],
            )
            self.conn.commit()
        except sqlite3.Error as err:
            log.error("не обновить сделку #%d: %s", row_id, err)

    def get_trade(self, row_id: int) -> Optional[sqlite3.Row]:
        """Прочитать запись о сделке.

        Args:
            row_id: Локальный id записи.

        Returns:
            sqlite3.Row либо None.
        """
        try:
            cursor = self.conn.execute("SELECT * FROM trades WHERE id = ?", (row_id,))
            return cursor.fetchone()
        except sqlite3.Error as err:
            log.error("не прочитать сделку #%d: %s", row_id, err)
            return None

    def open_positions(self) -> list:
        """Список сделок, по которым итог ещё не известен.

        Returns:
            Список sqlite3.Row.
        """
        try:
            cursor = self.conn.execute(
                "SELECT * FROM trades WHERE result IS NULL ORDER BY id"
            )
            return cursor.fetchall()
        except sqlite3.Error as err:
            log.error("не прочитать открытые сделки: %s", err)
            return []

    def recent_trades(self, limit: int = 20) -> list:
        """Последние сделки — для панели наблюдения.

        Args:
            limit: Сколько записей вернуть.

        Returns:
            Список sqlite3.Row, новые первыми.
        """
        try:
            cursor = self.conn.execute(
                "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
            )
            return cursor.fetchall()
        except sqlite3.Error as err:
            log.error("не прочитать последние сделки: %s", err)
            return []

    # ── события ────────────────────────────────────────────────────────

    def event(
        self,
        kind: str,
        message: str,
        detail: str = "",
        trade_ref: Optional[int] = None,
    ) -> None:
        """Записать событие: переход состояния, ошибку, отказ ограничителя.

        Args:
            kind:      "state" / "error" / "risk" / "info".
            message:   Короткое человеческое описание.
            detail:    Подробности: сырой ответ, стек, контекст.
            trade_ref: trades.id, если событие относится к сделке.

        Returns:
            None.
        """
        try:
            self.conn.execute(
                """INSERT INTO events (ts, kind, trade_ref, message, detail, created_ts)
                   VALUES (?,?,?,?,?,?)""",
                (time.time(), kind, trade_ref, message, detail, time.time()),
            )
            self.conn.commit()
        except sqlite3.Error as err:
            log.error("не записать событие (%s: %s): %s", kind, message, err)

    def recent_events(self, limit: int = 50, kind: Optional[str] = None) -> list:
        """Последние события.

        Args:
            limit: Сколько записей вернуть.
            kind:  Фильтр по виду события; None — все.

        Returns:
            Список sqlite3.Row, новые первыми.
        """
        try:
            if kind:
                cursor = self.conn.execute(
                    "SELECT * FROM events WHERE kind = ? ORDER BY id DESC LIMIT ?",
                    (kind, limit),
                )
            else:
                cursor = self.conn.execute(
                    "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
                )
            return cursor.fetchall()
        except sqlite3.Error as err:
            log.error("не прочитать события: %s", err)
            return []

    # ── статистика и отчёты ────────────────────────────────────────────

    def stats_today(self, now: Optional[float] = None) -> dict:
        """Сводка за текущие сутки — для ограничителей и панели.

        Сутки считаются по UTC. Ограничитель max_trades_per_day опирается
        на это же определение, поэтому оно должно быть одно на весь проект.

        Args:
            now: Момент отсчёта; None — сейчас.

        Returns:
            Словарь: trades, wins, losses, pnl, consecutive_losses.
        """
        now = now or time.time()
        day_start = now - (now % 86400)

        try:
            cursor = self.conn.execute(
                """SELECT COUNT(*) AS trades,
                          SUM(CASE WHEN result = 'win'  THEN 1 ELSE 0 END) AS wins,
                          SUM(CASE WHEN result = 'loss' THEN 1 ELSE 0 END) AS losses,
                          COALESCE(SUM(pnl), 0) AS pnl
                     FROM trades
                    WHERE created_ts >= ?""",
                (day_start,),
            )
            row = cursor.fetchone()
        except sqlite3.Error as err:
            log.error("не собрать статистику за сутки: %s", err)
            return {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0,
                    "consecutive_losses": 0}

        return {
            "trades": row["trades"] or 0,
            "wins": row["wins"] or 0,
            "losses": row["losses"] or 0,
            "pnl": row["pnl"] or 0.0,
            "consecutive_losses": self.consecutive_losses(),
        }

    def consecutive_losses(self) -> int:
        """Сколько убытков подряд идёт прямо сейчас.

        Считается по последним рассчитанным сделкам, от новых к старым, до
        первого не-убытка. Возврат (refund) серию НЕ продолжает и НЕ рвёт —
        он просто пропускается: ставка вернулась, ничего не произошло.

        Returns:
            Длина текущей серии убытков.
        """
        try:
            cursor = self.conn.execute(
                """SELECT result FROM trades
                    WHERE result IS NOT NULL AND result != 'unknown'
                    ORDER BY id DESC LIMIT 100"""
            )
            rows = cursor.fetchall()
        except sqlite3.Error as err:
            log.error("не посчитать серию убытков: %s", err)
            return 0

        streak = 0
        for row in rows:
            if row["result"] == "loss":
                streak += 1
            elif row["result"] == "refund":
                continue
            else:
                break
        return streak

    def report_latency(self, mode: Optional[str] = None) -> dict:
        """Отчёт по задержке открытия — главный результат Слоя 7.

        Задержка площадки при разведке составила 2910 и 3559 мс, тогда как
        обычные вызовы отвечают за ~200 мс. Если она растёт на волатильности
        или в сессионные часы, минутная экспирация может оказаться
        неприменимой — отчёт существует, чтобы увидеть это на данных, а не
        на ощущениях.

        Args:
            mode: Ограничить отчёт одним режимом ("demo", "dry", ...).
                  Без фильтра имитационные сделки (dry/shadow) разбавляют
                  замер: их задержка смоделирована, а не измерена.

        Returns:
            Словарь: count, median_ms, p90_ms, min_ms, max_ms, by_hour.
            При отсутствии данных count равен нулю.
        """
        query = ("SELECT latency_ms, open_ts FROM trades "
                 "WHERE latency_ms IS NOT NULL")
        params: tuple = ()
        if mode:
            query += " AND mode = ?"
            params = (mode,)
        query += " ORDER BY latency_ms"
        try:
            cursor = self.conn.execute(query, params)
            rows = cursor.fetchall()
        except sqlite3.Error as err:
            log.error("не построить отчёт по задержке: %s", err)
            return {"count": 0}

        values = [row["latency_ms"] for row in rows]
        if not values:
            return {"count": 0}

        def percentile(sorted_values: list, share: float) -> float:
            """Взять процентиль из отсортированного списка.

            Args:
                sorted_values: Отсортированные значения.
                share:         Доля от 0 до 1.

            Returns:
                Значение процентиля.
            """
            if not sorted_values:
                return 0.0
            index = min(int(len(sorted_values) * share), len(sorted_values) - 1)
            return float(sorted_values[index])

        # Разрез по часам UTC: видно, растёт ли задержка в сессионные часы.
        by_hour = {}
        for row in rows:
            if not row["open_ts"]:
                continue
            hour = time.gmtime(row["open_ts"]).tm_hour
            by_hour.setdefault(hour, []).append(row["latency_ms"])

        return {
            "count": len(values),
            "median_ms": percentile(values, 0.5),
            "p90_ms": percentile(values, 0.9),
            "min_ms": float(values[0]),
            "max_ms": float(values[-1]),
            "by_hour": {
                hour: {
                    "count": len(items),
                    "median_ms": percentile(sorted(items), 0.5),
                }
                for hour, items in sorted(by_hour.items())
            },
        }


# ── kill-switch ────────────────────────────────────────────────────────


def kill_switch_active(path: str = "bot/STOP") -> bool:
    """Проверить, стоит ли запрет на новые входы.

    Наличие файла мгновенно запрещает открывать сделки; уже открытые
    доводятся до расчёта. Проверяется перед КАЖДОЙ отправкой, а не раз при
    старте: смысл выключателя в том, чтобы сработать немедленно.

    Файл, а не переменная или эндпоинт, выбран намеренно: остановить бота
    можно из любой ssh-сессии одной командой `touch bot/STOP`, не имея
    доступа к панели и не зная состояния процесса.

    Args:
        path: Путь к файлу-выключателю.

    Returns:
        True, если файл существует и новые входы запрещены.
    """
    return os.path.exists(path)


def engage_kill_switch(path: str = "bot/STOP", reason: str = "") -> None:
    """Взвести выключатель, запретив новые входы.

    Args:
        path:   Путь к файлу-выключателю.
        reason: Причина; записывается в файл, чтобы потом было понятно,
                кто и почему остановил бота.

    Returns:
        None.
    """
    folder = os.path.dirname(os.path.abspath(path))
    if folder and not os.path.isdir(folder):
        os.makedirs(folder, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {reason}\n")
    log.warning("KILL-SWITCH ВЗВЕДЁН: %s", reason or "без указания причины")


def release_kill_switch(path: str = "bot/STOP") -> bool:
    """Снять выключатель.

    Снимается только вручную — по ТЗ сработавший стоп не должен сниматься
    сам собой.

    Args:
        path: Путь к файлу-выключателю.

    Returns:
        True, если файл был и удалён; False, если его не было.
    """
    if not os.path.exists(path):
        return False
    os.remove(path)
    log.info("kill-switch снят")
    return True
