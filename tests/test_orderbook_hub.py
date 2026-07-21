"""Стакан в хабе: подписки клиентов и команды фиду.

Запуск: python3.10 tests/test_orderbook_hub.py

Хаб для стакана — чистый ретранслятор: не хранит и не пишет в БД. Вся его
работа в том, чтобы правильно посчитать, чьи стаканы сейчас нужны, и не
забыть отписаться. Забытая подписка стоит ~1.1 ГБ/сутки, поэтому проверяется
не только включение, но и все пути выключения: закрытая вкладка, смена
символа, снятый тумблер.
"""

import asyncio
import copy
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hub as hubmod
from hub import Hub


class FakeBus:
    """Заглушка шины: запоминает команды вместо отправки в сокет."""

    def __init__(self):
        """Создать заглушку.

        Returns:
            None.
        """
        self.commands = []

    async def command(self, provider, payload):
        """Записать команду.

        Args:
            provider: Имя провайдера.
            payload:  Тело команды.

        Returns:
            True — как у настоящей шины при живом фиде.
        """
        self.commands.append((provider, payload))
        return True

    @property
    def last_symbols(self):
        """Символы из последней команды.

        Returns:
            Список символов или None, если команд не было.
        """
        return self.commands[-1][1]["symbols"] if self.commands else None


def _run(coro):
    """Выполнить корутину в свежем цикле событий.

    Args:
        coro: Корутина.

    Returns:
        Её результат.
    """
    return asyncio.new_event_loop().run_until_complete(coro)


class OrderbookHubTest(unittest.TestCase):
    """Общая обвязка: хаб на временной БД с фейковой шиной."""

    def setUp(self):
        """Создать хаб и подменить шину.

        Returns:
            None.
        """
        config = copy.deepcopy(hubmod.load_config())
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.path)
        config["db_path"] = self.path
        self.hub = Hub(config)
        self.bus = FakeBus()
        self.hub._bus = self.bus

    def tearDown(self):
        """Убрать временную БД.

        Returns:
            None.
        """
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.path + suffix)
            except OSError:
                pass

    def _client(self, ws, symbol="BTC", provider="lighter",
                wire=None, orderbook=False):
        """Зарегистрировать клиента.

        Args:
            ws:        Ключ соединения (любой хешируемый объект).
            symbol:    Чистый символ.
            provider:  Провайдер.
            wire:      Имя, которым клиент зовёт символ.
            orderbook: Включён ли у него стакан.

        Returns:
            None.
        """
        self.hub._clients[ws] = {
            "provider": provider, "symbol": symbol,
            "wire": wire or ("%s:%s" % (provider, symbol)),
            "tf": "M1", "requestId": 1, "orderbook": orderbook,
        }


class TestSubscriptionAccounting(OrderbookHubTest):
    """Что хаб просит у фида."""

    def test_enable_asks_feed(self):
        """Включение стакана шлёт фиду команду с этим символом."""
        self._client("a")
        _run(self.hub._on_orderbook_toggle("a", {"enabled": True}))
        self.assertEqual(self.bus.last_symbols, ["BTC"])

    def test_disable_sends_empty_set(self):
        """Выключение шлёт ПУСТОЙ набор — так фид узнаёт об отписке.

        Промолчать здесь нельзя: фид продолжал бы держать подписку, а это
        ~1.1 ГБ/сутки за инструмент, которого никто не смотрит.
        """
        self._client("a")
        _run(self.hub._on_orderbook_toggle("a", {"enabled": True}))
        _run(self.hub._on_orderbook_toggle("a", {"enabled": False}))
        self.assertEqual(self.bus.last_symbols, [])
        self.assertEqual(len(self.bus.commands), 2,
                         "ровно две команды: подписка и отписка")

    def test_no_command_when_set_unchanged(self):
        """Повторное включение того же символа команду НЕ шлёт.

        Иначе фид получал бы команду на каждый чих клиента.
        """
        self._client("a")
        _run(self.hub._on_orderbook_toggle("a", {"enabled": True}))
        count = len(self.bus.commands)
        _run(self.hub._on_orderbook_toggle("a", {"enabled": True}))
        self.assertEqual(len(self.bus.commands), count)

    def test_two_clients_same_symbol_one_subscription(self):
        """Две вкладки на одном символе — одна подписка."""
        self._client("a", orderbook=True)
        self._client("b", orderbook=True)
        _run(self.hub._sync_orderbook_subs())
        self.assertEqual(self.bus.last_symbols, ["BTC"])

    def test_second_client_leaving_keeps_subscription(self):
        """Ушла одна из двух вкладок — подписка остаётся.

        Считать надо по всем клиентам, а не декрементом счётчика.
        """
        self._client("a", orderbook=True)
        self._client("b", orderbook=True)
        _run(self.hub._sync_orderbook_subs())
        del self.hub._clients["b"]
        _run(self.hub._sync_orderbook_subs())
        self.assertEqual(self.bus.last_symbols, ["BTC"])

    def test_last_client_leaving_unsubscribes(self):
        """Ушёл последний зритель — отписка."""
        self._client("a", orderbook=True)
        _run(self.hub._sync_orderbook_subs())
        del self.hub._clients["a"]
        _run(self.hub._sync_orderbook_subs())
        self.assertEqual(self.bus.last_symbols, [])

    def test_different_symbols_both_requested(self):
        """Разные символы — оба в наборе, отсортированы."""
        self._client("a", symbol="BTC", orderbook=True)
        self._client("b", symbol="ETH", orderbook=True)
        _run(self.hub._sync_orderbook_subs())
        self.assertEqual(self.bus.last_symbols, ["BTC", "ETH"])

    def test_client_without_orderbook_ignored(self):
        """Клиент с выключенным стаканом в набор не попадает."""
        self._client("a", symbol="BTC", orderbook=True)
        self._client("b", symbol="ETH", orderbook=False)
        _run(self.hub._sync_orderbook_subs())
        self.assertEqual(self.bus.last_symbols, ["BTC"])

    def test_no_command_for_idle_provider(self):
        """Клиент без стакана НЕ вызывает команду с пустым набором.

        Провайдеры подключённых клиентов попадают в рассмотрение (иначе
        терялась бы отписка), но пустой набор равен уже отправленному
        «ничего» — команды быть не должно, иначе каждое открытие вкладки
        дёргало бы фид впустую.
        """
        self._client("a", orderbook=False)
        _run(self.hub._sync_orderbook_subs())
        self.assertEqual(self.bus.commands, [])

    def test_symbol_switch_moves_subscription(self):
        """Смена символа переносит подписку, а не добавляет вторую.

        set_tf пересоздаёт запись клиента, и флаг стакана переносится вручную.
        Ошибись там — либо индикатор молча гас бы при переключении пары, либо
        подписка на старый символ висела бы вечно.
        """
        self._client("a", symbol="BTC")
        _run(self.hub._on_orderbook_toggle("a", {"enabled": True}))
        self.assertEqual(self.bus.last_symbols, ["BTC"])

        # Как это делает _on_set_tf: запись пересоздаётся с переносом флага.
        prev = self.hub._clients["a"]
        self.hub._clients["a"] = {
            "provider": "lighter", "symbol": "ETH", "wire": "lighter:ETH",
            "tf": "M1", "requestId": 2,
            "orderbook": prev.get("orderbook", False),
        }
        _run(self.hub._sync_orderbook_subs())
        self.assertEqual(self.bus.last_symbols, ["ETH"],
                         "подписка обязана переехать на новый символ")

    def test_missing_bus_is_safe(self):
        """Без шины синхронизация не падает.

        health() и тесты конструируют Hub в обход main(), где шина ставится.
        """
        self.hub._bus = None
        self._client("a", orderbook=True)
        _run(self.hub._sync_orderbook_subs())   # не должно падать


class TestBrokerTfPerProvider(OrderbookHubTest):
    """Какие ТФ провайдер отдаёт готовыми, а какие хаб собирает сам."""

    def test_fxcm_gives_h1_h4_d1(self):
        """У FXCM старшие ТФ приходят от брокера со своей сеткой."""
        self.assertEqual(self.hub.broker_tf_for("fxcm"), ("H1", "H4", "D1"))

    def test_lighter_gives_nothing(self):
        """У Lighter старшие ТФ хаб собирает из M1.

        Фид грузит только 1m. Пока broker_tf был общим списком, H1/H4/D1
        числились «брокерскими» для ВСЕХ — их никто не поставлял и никто не
        собирал, и на крипте в БД лежало по 3 свечи H1 вместо тысяч.
        """
        self.assertEqual(self.hub.broker_tf_for("lighter"), ())

    def test_unknown_provider_builds_everything(self):
        """Незнакомый провайдер — считаем, что готового не даёт ничего."""
        self.assertEqual(self.hub.broker_tf_for("nope"), ())

    def test_legacy_flat_list_still_works(self):
        """Старый формат (общий список) применяется ко всем провайдерам.

        Конфиг мог остаться непереписанным — тогда поведение обязано быть
        прежним, а не «никто ничего не отдаёт».
        """
        config = copy.deepcopy(hubmod.load_config())
        config["broker_tf"] = ["H1", "H4"]
        config["db_path"] = self.path + ".legacy"
        legacy = Hub(config)
        self.assertEqual(legacy.broker_tf_for("fxcm"), ("H1", "H4"))
        self.assertEqual(legacy.broker_tf_for("lighter"), ("H1", "H4"))


class TestBroadcast(OrderbookHubTest):
    """Раздача среза браузерам."""

    def setUp(self):
        """Подменить отправку, чтобы ловить payload.

        Returns:
            None.
        """
        OrderbookHubTest.setUp(self)
        self.sent = []
        self.hub._send = lambda ws, payload: self.sent.append((ws, payload))

    def _book(self, symbol="BTC"):
        """Сообщение шины со срезом стакана.

        Args:
            symbol: Инструмент.

        Returns:
            Dict сообщения.
        """
        return {"type": "orderbook", "provider": "lighter", "symbol": symbol,
                "ts": 1784500000, "bids": [[64000.0, 1.0]],
                "asks": [[64001.0, 2.0]]}

    def test_only_subscribed_receive(self):
        """Срез уходит только тем, у кого стакан включён."""
        self._client("on", orderbook=True)
        self._client("off", orderbook=False)
        self.hub._handle_orderbook(self._book())
        self.assertEqual([ws for ws, _ in self.sent], ["on"])

    def test_other_symbol_not_notified(self):
        """Смотрящий другой инструмент среза не получает."""
        self._client("eth", symbol="ETH", orderbook=True)
        self.hub._handle_orderbook(self._book("BTC"))
        self.assertEqual(self.sent, [])

    def test_each_client_gets_own_name(self):
        """Каждому уходит ЕГО имя символа.

        Тот же класс бага, что убивал алерты: фронт сверяет symbol со своим
        currentSymbol, и общий на всех payload отдал бы части клиентов чужое
        имя — срез был бы отброшен молча.
        """
        self._client("new", wire="lighter:BTC", orderbook=True)
        self._client("old", wire="BTC", orderbook=True)
        self.hub._handle_orderbook(self._book())
        names = {ws: json.loads(p)["symbol"] for ws, p in self.sent}
        self.assertEqual(names, {"new": "lighter:BTC", "old": "BTC"})

    def test_levels_pass_through(self):
        """Уровни доезжают без искажения."""
        self._client("a", orderbook=True)
        self.hub._handle_orderbook(self._book())
        payload = json.loads(self.sent[0][1])
        self.assertEqual(payload["bids"], [[64000.0, 1.0]])
        self.assertEqual(payload["asks"], [[64001.0, 2.0]])

    def test_nothing_sent_without_subscribers(self):
        """Без зрителей не сериализуем ничего."""
        self._client("off", orderbook=False)
        self.hub._handle_orderbook(self._book())
        self.assertEqual(self.sent, [])

    def test_not_persisted(self):
        """Стакан НЕ попадает в очередь записи в БД.

        Он живёт секунду; писать его — бессмысленно жечь диск.
        """
        self._client("a", orderbook=True)
        before = self.hub._db_queue.qsize()
        self.hub._handle_orderbook(self._book())
        self.assertEqual(self.hub._db_queue.qsize(), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
