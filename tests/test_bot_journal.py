"""
Тесты журнала и kill-switch. Python 3.10.

Запуск:  python3.10 tests/test_bot_journal.py    (из корня проекта)

Работают на временной БД, боевой bot_journal.db не трогают.

Особое внимание — latency_ms: это главный практический результат всей
задачи (задержка открытия у площадки ~3 с против ~200 мс на обычных
вызовах). Формула считается в одном месте, и тест закрепляет её вместе
с пересчётом при разрешении состояния UNKNOWN.
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.journal import (
    Journal,
    engage_kill_switch,
    kill_switch_active,
    release_kill_switch,
)

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


def temp_journal():
    """Создать журнал на временном файле.

    Returns:
        Кортеж (Journal, путь к файлу).
    """
    handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    handle.close()
    return Journal(handle.name), handle.name


def cleanup(path):
    """Удалить временный файл БД вместе с WAL-хвостами.

    Args:
        path: Путь к файлу БД.

    Returns:
        None.
    """
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(path + suffix)
        except OSError:
            pass


def test_schema_and_insert():
    """Схема создаётся, сделка пишется и читается обратно."""
    print("запись и чтение сделки")
    journal, path = temp_journal()
    try:
        row_id = journal.open_trade(
            mode="demo", symbol="USD/JPY", direction="call",
            investment=2, expiry_minutes=1, source="manual",
            signal_ts=1785953078.0, request_ts=1785953078.0,
            open_ts=1785953081.0, expiry_ts=1785953141.0,
            trade_id=224809704, entry_price=157.666,
            payout_percent=82, raw_response="<tr data-id=…>",
            meta={"manual": True, "note": "проба"},
        )
        check("сделка записана", row_id > 0, row_id)

        row = journal.get_trade(row_id)
        check("trade_id сохранён", row["trade_id"] == 224809704)
        check("цена входа", row["entry_price"] == 157.666)
        check("режим", row["mode"] == "demo")
        check("источник", row["source"] == "manual")
        check("сырой ответ сохранён", row["raw_response"] == "<tr data-id=…>")
        check("meta сохранён целиком", '"note": "проба"' in row["meta_json"],
              row["meta_json"])
        check("итога пока нет", row["result"] is None)
    finally:
        journal.close()
        cleanup(path)


def test_latency_computed():
    """latency_ms считается из open_ts − request_ts."""
    print("расчёт задержки открытия")
    journal, path = temp_journal()
    try:
        # Реальные числа из HAR-записи: запрос 18:04:38.3, открытие 18:04:41.
        row_id = journal.open_trade(
            mode="demo", symbol="USD/JPY", direction="call",
            investment=2, expiry_minutes=1,
            request_ts=1785953078.441, open_ts=1785953082.0,
        )
        row = journal.get_trade(row_id)
        check("задержка ~3559 мс", row["latency_ms"] == 3559, row["latency_ms"])

        # В dry-режиме открытия по версии площадки нет — задержки тоже.
        dry_id = journal.open_trade(
            mode="dry", symbol="USD/JPY", direction="put",
            investment=1, expiry_minutes=1, request_ts=time.time(),
        )
        check("без open_ts задержка не считается",
              journal.get_trade(dry_id)["latency_ms"] is None)
    finally:
        journal.close()
        cleanup(path)


def test_settle_and_stats():
    """Итог проставляется, статистика считается."""
    print("расчёт сделок и статистика")
    journal, path = temp_journal()
    try:
        first = journal.open_trade(mode="demo", symbol="USD/JPY",
                                   direction="call", investment=2,
                                   expiry_minutes=1)
        second = journal.open_trade(mode="demo", symbol="EUR/USD",
                                    direction="put", investment=2,
                                    expiry_minutes=1)

        check("обе в открытых", len(journal.open_positions()) == 2)

        journal.settle_trade(first, "win", pnl=1.64, raw_settle="2;3.64;9363")
        journal.settle_trade(second, "loss", pnl=-2.0, raw_settle="2;0;9359")

        check("открытых не осталось", len(journal.open_positions()) == 0)
        check("итог первой", journal.get_trade(first)["result"] == "win")
        check("pnl второй", journal.get_trade(second)["pnl"] == -2.0)

        stats = journal.stats_today()
        check("сделок за сутки 2", stats["trades"] == 2, stats)
        check("побед 1", stats["wins"] == 1, stats)
        check("убытков 1", stats["losses"] == 1, stats)
        check("pnl суммарный -0.36", abs(stats["pnl"] + 0.36) < 0.001, stats["pnl"])
    finally:
        journal.close()
        cleanup(path)


def test_closed_trades():
    """closed_trades отдаёт только рассчитанные, новые первыми."""
    print("закрытые сделки")
    journal, path = temp_journal()
    try:
        one = journal.open_trade(mode="demo", symbol="USD/JPY",
                                 direction="call", investment=2,
                                 expiry_minutes=1)
        two = journal.open_trade(mode="demo", symbol="EUR/USD",
                                 direction="put", investment=2,
                                 expiry_minutes=1)
        three = journal.open_trade(mode="demo", symbol="AUD/USD",
                                   direction="call", investment=1,
                                   expiry_minutes=1)

        # two рассчитана, one и three ещё открыты.
        journal.settle_trade(two, "win", pnl=1.64, raw_settle="2;3.64;9363")

        closed = journal.closed_trades()
        check("только рассчитанная", len(closed) == 1, closed)
        check("та самая сделка",
              closed and closed[0]["id"] == two, closed)

        journal.settle_trade(three, "loss", pnl=-1.0, raw_settle="2;0;9359")

        closed = journal.closed_trades()
        check("две закрытые", len(closed) == 2, closed)
        check("новые первыми",
              closed and closed[0]["id"] == three and closed[1]["id"] == two,
              [r["id"] for r in closed])

        closed_one = journal.closed_trades(limit=1)
        check("лимит работает", len(closed_one) == 1 and
              closed_one[0]["id"] == three, closed_one)

        # Открытая не утекла ни в один из списков закрытых.
        check("открытая не в закрытых",
              all(r["id"] != one for r in closed), closed)
    finally:
        journal.close()
        cleanup(path)


def test_consecutive_losses():
    """Серия убытков считается верно; refund её не рвёт."""
    print("серия убытков подряд")
    journal, path = temp_journal()
    try:
        def add(result):
            """Добавить рассчитанную сделку.

            Args:
                result: Итог сделки.

            Returns:
                None.
            """
            row_id = journal.open_trade(mode="demo", symbol="USD/JPY",
                                        direction="call", investment=1,
                                        expiry_minutes=1)
            journal.settle_trade(row_id, result, pnl=-1 if result == "loss" else 1)

        check("пустой журнал — серия 0", journal.consecutive_losses() == 0)

        add("loss"); add("loss")
        check("два убытка подряд", journal.consecutive_losses() == 2,
              journal.consecutive_losses())

        # Возврат ставку не тронул — серию не продолжает и не рвёт.
        add("refund")
        check("refund серию не рвёт", journal.consecutive_losses() == 2,
              journal.consecutive_losses())

        add("loss")
        check("после refund серия росла", journal.consecutive_losses() == 3,
              journal.consecutive_losses())

        # Победа обнуляет.
        add("win")
        check("победа обнуляет серию", journal.consecutive_losses() == 0,
              journal.consecutive_losses())
    finally:
        journal.close()
        cleanup(path)


def test_streak_resets_on_day_and_mode():
    """Серия убытков не тянется со вчера и не смешивает режимы.

    Найдено на Слое 7: живой прогон остановился по лимиту «3 убытка
    подряд», хотя реальных убытков в тот день не было — счётчик тянул
    ВЧЕРАШНИЕ имитационные dry-сделки. Оба изъяна проверяются здесь.
    """
    print("серия убытков: границы суток и режима")
    journal, path = temp_journal()
    try:
        def add(result, mode="demo", days_ago=0):
            """Добавить рассчитанную сделку, при желании — в прошлые сутки.

            created_ts проставляется журналом, поэтому «вчерашнее» время
            выставляется прямым UPDATE: так проверяется настоящий SQL-фильтр,
            а не подставленный в вызов аргумент.

            Args:
                result:   Итог сделки.
                mode:     Режим прогона.
                days_ago: На сколько суток сдвинуть назад.

            Returns:
                Локальный id записи.
            """
            row_id = journal.open_trade(mode=mode, symbol="USD/JPY",
                                        direction="call", investment=1,
                                        expiry_minutes=1)
            journal.settle_trade(row_id, result,
                                 pnl=-1 if result == "loss" else 1)
            if days_ago:
                journal.conn.execute(
                    "UPDATE trades SET created_ts = created_ts - ? WHERE id = ?",
                    (days_ago * 86400, row_id))
                journal.conn.commit()
            return row_id

        # Вчерашние убытки той же серии сегодня не считаются.
        add("loss", days_ago=1)
        add("loss", days_ago=1)
        add("loss", days_ago=1)
        check("вчерашние убытки не в серии", journal.consecutive_losses() == 0,
              journal.consecutive_losses())
        check("вчерашние не в дневной статистике",
              journal.stats_today()["trades"] == 0,
              journal.stats_today())

        # Сегодняшний убыток начинает серию заново.
        add("loss")
        check("сегодняшний убыток считается", journal.consecutive_losses() == 1,
              journal.consecutive_losses())

        # Имитационные dry-сделки не должны останавливать демо-прогон.
        add("loss", mode="dry")
        add("loss", mode="dry")
        check("dry-убытки не входят в серию demo",
              journal.consecutive_losses(mode="demo") == 1,
              journal.consecutive_losses(mode="demo"))
        check("серия dry считается отдельно",
              journal.consecutive_losses(mode="dry") == 2,
              journal.consecutive_losses(mode="dry"))
        check("без фильтра видно все режимы",
              journal.consecutive_losses() == 3,
              journal.consecutive_losses())

        # Дневная статистика тоже режимная — лимит сделок и просадка.
        demo_stats = journal.stats_today(mode="demo")
        dry_stats = journal.stats_today(mode="dry")
        check("demo: 1 сделка", demo_stats["trades"] == 1, demo_stats)
        check("dry: 2 сделки", dry_stats["trades"] == 2, dry_stats)
        check("demo: серия в сводке 1",
              demo_stats["consecutive_losses"] == 1, demo_stats)
    finally:
        journal.close()
        cleanup(path)


def test_update_recomputes_latency():
    """Разрешение UNKNOWN: появился open_ts — пересчиталась задержка."""
    print("пересчёт задержки при разрешении UNKNOWN")
    journal, path = temp_journal()
    try:
        # Ответ на открытие потерялся: есть только время отправки.
        row_id = journal.open_trade(
            mode="demo", symbol="USD/JPY", direction="call",
            investment=2, expiry_minutes=1, request_ts=1000.0,
        )
        check("задержки пока нет", journal.get_trade(row_id)["latency_ms"] is None)

        # Сделка нашлась сверкой через user_real_trade.php.
        journal.update_trade(row_id, trade_id=224809704, open_ts=1003.2,
                             entry_price=157.666)
        row = journal.get_trade(row_id)
        check("trade_id проставлен", row["trade_id"] == 224809704)
        check("задержка пересчитана", row["latency_ms"] == 3200, row["latency_ms"])

        # Незнакомые поля игнорируются, а не валят запрос.
        journal.update_trade(row_id, nonexistent_column="боом")
        check("незнакомое поле проигнорировано",
              journal.get_trade(row_id)["trade_id"] == 224809704)
    finally:
        journal.close()
        cleanup(path)


def test_events():
    """События пишутся и фильтруются по виду."""
    print("журнал событий")
    journal, path = temp_journal()
    try:
        journal.event("state", "IDLE → VALIDATING")
        journal.event("risk", "отказ: кулдаун", detail="осталось 42 с")
        journal.event("error", "пустой ответ площадки", detail="<сырой текст>")

        check("записано 3 события", len(journal.recent_events()) == 3)
        check("фильтр по виду", len(journal.recent_events(kind="risk")) == 1)
        risk = journal.recent_events(kind="risk")[0]
        check("текст события", "кулдаун" in risk["message"])
        check("подробности сохранены", risk["detail"] == "осталось 42 с")
        # Новые события идут первыми — панель показывает свежее сверху.
        check("порядок от новых", journal.recent_events()[0]["kind"] == "error")
    finally:
        journal.close()
        cleanup(path)


def test_latency_report():
    """Отчёт по задержке — главный результат Слоя 7."""
    print("отчёт по задержке")
    journal, path = temp_journal()
    try:
        empty = journal.report_latency()
        check("пустой журнал — count 0", empty["count"] == 0, empty)

        # Пять сделок с известными задержками.
        base = 1785953078.0
        for delay in (2910, 3559, 3100, 2800, 4200):
            journal.open_trade(
                mode="demo", symbol="USD/JPY", direction="call",
                investment=2, expiry_minutes=1,
                request_ts=base, open_ts=base + delay / 1000.0,
            )

        report = journal.report_latency()
        check("учтено 5 сделок", report["count"] == 5, report["count"])
        check("минимум 2800", report["min_ms"] == 2800, report["min_ms"])
        check("максимум 4200", report["max_ms"] == 4200, report["max_ms"])
        check("медиана 3100", report["median_ms"] == 3100, report["median_ms"])
        check("разрез по часам есть", len(report["by_hour"]) >= 1, report["by_hour"])
    finally:
        journal.close()
        cleanup(path)


def test_kill_switch():
    """Kill-switch: файл мгновенно запрещает новые входы."""
    print("kill-switch")
    folder = tempfile.mkdtemp()
    path = os.path.join(folder, "STOP")
    try:
        check("изначально снят", kill_switch_active(path) is False)

        engage_kill_switch(path, reason="проверка")
        check("после взведения активен", kill_switch_active(path) is True)

        with open(path, "r", encoding="utf-8") as handle:
            content = handle.read()
        check("причина записана в файл", "проверка" in content, content)

        check("снятие возвращает True", release_kill_switch(path) is True)
        check("после снятия неактивен", kill_switch_active(path) is False)
        # Повторное снятие не должно падать.
        check("повторное снятие — False", release_kill_switch(path) is False)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
        os.rmdir(folder)


def test_journal_survives_errors():
    """Ошибка БД не роняет процесс — журнал важен, но не важнее торговли."""
    print("устойчивость журнала")
    journal, path = temp_journal()
    try:
        journal.close()  # соединение закрыто, любые запросы теперь падают

        # Все методы обязаны проглотить ошибку и вернуть безопасное значение.
        check("open_trade вернул 0", journal.open_trade(
            mode="demo", symbol="USD/JPY", direction="call",
            investment=1, expiry_minutes=1) == 0)
        check("get_trade вернул None", journal.get_trade(1) is None)
        check("open_positions вернул []", journal.open_positions() == [])
        check("recent_events вернул []", journal.recent_events() == [])
        check("consecutive_losses вернул 0", journal.consecutive_losses() == 0)
        check("report_latency вернул count 0",
              journal.report_latency()["count"] == 0)
        stats = journal.stats_today()
        check("stats_today вернул нули", stats["trades"] == 0, stats)
        # settle_trade и event не возвращают значений — важно, что не падают.
        journal.settle_trade(1, "win", pnl=1.0)
        journal.event("info", "после закрытия")
        check("settle_trade и event не упали", True)
    finally:
        cleanup(path)


def main():
    """Прогнать все тесты и вернуть код возврата.

    Returns:
        0 — всё прошло, 1 — есть провалы.
    """
    for test in (test_schema_and_insert, test_latency_computed,
                 test_settle_and_stats, test_closed_trades,
                 test_consecutive_losses,
                 test_streak_resets_on_day_and_mode,
                 test_update_recomputes_latency, test_events,
                 test_latency_report, test_kill_switch,
                 test_journal_survives_errors):
        test()
        print()

    print(f"итого: {passed} прошло, {failed} провалено")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
