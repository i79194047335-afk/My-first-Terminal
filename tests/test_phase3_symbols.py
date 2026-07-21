"""Фаза 3, слой 6: символы с префиксом провайдера.

Запуск: python3.10 tests/test_phase3_symbols.py

Формат символа стал "provider:symbol". Причина не косметическая: у Lighter
есть USDJPY, USDCAD, AUDUSD — те же пары, что у FXCM. Пока их нет в белом
списке, поиск провайдера перебором работает, но добавь любую — и хаб начнёт
угадывать, к кому относится "USD/JPY".

Что здесь защищается:
  1. Разбор символа, включая несуществующие сочетания ("fxcm:BTC").
  2. Обратная совместимость: голый символ без префикса обязан работать —
     иначе открытые вкладки отвалятся в момент выкатки.
  3. Ключи алертов НОРМАЛИЗУЮТСЯ. Срабатывание проверяется по имени из
     шины (без префикса); клади мы ключ с префиксом — алерт создавался бы,
     но не срабатывал никогда, и это молчаливый баг.
"""

import copy
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hub as hubmod
from hub import Hub


def _make_hub():
    """Собрать хаб на временной БД с боевым белым списком.

    Returns:
        Кортеж (Hub, путь к БД).
    """
    config = copy.deepcopy(hubmod.load_config())
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    config["db_path"] = path
    return Hub(config), path


class TestResolveSymbol(unittest.TestCase):
    """Разбор символа в пару (провайдер, инструмент)."""

    def setUp(self):
        """Создать хаб.

        Returns:
            None.
        """
        self.hub, self.path = _make_hub()

    def tearDown(self):
        """Удалить временную БД.

        Returns:
            None.
        """
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.path + suffix)
            except OSError:
                pass

    def test_prefixed_symbols(self):
        """Символ с префиксом разбирается по префиксу, а не перебором."""
        self.assertEqual(self.hub.resolve_symbol("lighter:BTC"),
                         ("lighter", "BTC"))
        self.assertEqual(self.hub.resolve_symbol("fxcm:EUR/USD"),
                         ("fxcm", "EUR/USD"))

    def test_bare_symbols_still_work(self):
        """Голый символ принимается: старые вкладки не должны отвалиться."""
        self.assertEqual(self.hub.resolve_symbol("BTC"), ("lighter", "BTC"))
        self.assertEqual(self.hub.resolve_symbol("EUR/USD"), ("fxcm", "EUR/USD"))

    def test_wrong_provider_is_rejected(self):
        """Инструмент, которого у провайдера нет, не находится.

        Именно это отличает префикс от простого украшения: "fxcm:BTC" —
        не то же самое, что "BTC".
        """
        self.assertEqual(self.hub.resolve_symbol("fxcm:BTC"), (None, "BTC"))
        self.assertEqual(self.hub.resolve_symbol("lighter:EUR/USD"),
                         (None, "EUR/USD"))

    def test_unknown_symbol(self):
        """Неизвестный инструмент даёт (None, symbol)."""
        self.assertEqual(self.hub.resolve_symbol("NOPE"), (None, "NOPE"))

    def test_provider_of_matches_resolve(self):
        """_provider_of — тонкая обёртка над resolve_symbol."""
        for symbol in ("lighter:BTC", "BTC", "fxcm:EUR/USD", "NOPE"):
            self.assertEqual(self.hub._provider_of(symbol),
                             self.hub.resolve_symbol(symbol)[0])

    def test_resolve_survives_missing_config(self):
        """Разбор не падает на объекте без конфига.

        health() зовёт _provider_of, а тесты и диагностика конструируют Hub
        в обход __init__. Падение там уронило бы health-эндпоинт.
        """
        bare = Hub.__new__(Hub)
        self.assertEqual(bare.resolve_symbol("lighter:BTC"), (None, "BTC"))
        self.assertEqual(bare.resolve_symbol("BTC"), (None, "BTC"))


class TestAlertKeyNormalisation(unittest.TestCase):
    """Ключи алертов приводятся к чистому символу."""

    def setUp(self):
        """Создать хаб.

        Returns:
            None.
        """
        self.hub, self.path = _make_hub()

    def tearDown(self):
        """Удалить временную БД.

        Returns:
            None.
        """
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.path + suffix)
            except OSError:
                pass

    def test_update_alert_finds_prefixed_symbol(self):
        """update_alert по префиксованному имени находит алерт."""
        self.hub._alerts["BTC"] = [{"id": 7, "price": 100.0, "triggered": True}]
        self.hub._on_update_alert({"symbol": "lighter:BTC", "id": 7,
                                   "price": 200.0})
        alert = self.hub._alerts["BTC"][0]
        self.assertEqual(alert["price"], 200.0)
        self.assertFalse(alert["triggered"])

    def test_remove_alert_finds_prefixed_symbol(self):
        """remove_alert по префиксованному имени удаляет алерт."""
        self.hub._alerts["BTC"] = [{"id": 7, "price": 100.0, "triggered": False}]
        self.hub._on_remove_alert({"symbol": "lighter:BTC", "id": 7})
        self.assertEqual(self.hub._alerts["BTC"], [])

    def test_alert_fires_on_bus_symbol(self):
        """Алерт, созданный под префиксом, срабатывает по имени из шины.

        Шина шлёт голый "BTC" — если ключ остался префиксованным, алерт
        молча никогда не сработает.
        """
        self.hub._alerts["BTC"] = [{"id": 1, "price": 64000.0,
                                    "triggered": False}]
        sent = []
        self.hub._clients[object()] = {"wire": "lighter:BTC"}
        self.hub._send = lambda ws, payload: sent.append(payload)

        self.hub._check_alerts("BTC", 63990.0, 64010.0)

        self.assertTrue(self.hub._alerts["BTC"][0]["triggered"])
        self.assertEqual(len(sent), 1)

    def _fire_and_collect(self, wires):
        """Уронить алерт по BTC на клиентов с заданными форматами имени.

        Args:
            wires: Список значений "wire" (как клиент назвал инструмент).

        Returns:
            Список разобранных JSON-сообщений, по одному на клиента.
        """
        sent = []
        for i, wire in enumerate(wires):
            self.hub._clients[i] = {"wire": wire}
        self.hub._send = lambda ws, payload: sent.append(json.loads(payload))
        self.hub._alerts["BTC"] = [{"id": 1, "price": 64000.0,
                                    "triggered": False}]
        self.hub._check_alerts("BTC", 63990.0, 64010.0)
        return sent

    def test_fired_alert_matches_each_client_format(self):
        """Каждый клиент получает символ в СВОЁМ формате.

        Стык, на котором ловилось молчание алертов. Фронт сверяет
        msg.symbol с currentSymbol: обновлённая вкладка ждёт "lighter:BTC",
        старая — голое "BTC". Общий на всех JSON промахнётся мимо одной из
        них, и промахнётся МОЛЧА — алерт при этом уже помечен triggered и
        второй раз не выстрелит. Факта рассылки для проверки недостаточно.
        """
        sent = self._fire_and_collect(["lighter:BTC", "BTC"])
        self.assertEqual([m["symbol"] for m in sent], ["lighter:BTC", "BTC"])

    def test_alert_reaches_client_watching_another_symbol(self):
        """Смотрящий другой инструмент получает каноничное имя, а не своё.

        Алерты глобальны — фронт принимает их независимо от подписки. Но
        подставить сюда wire зрителя ETH значило бы прислать событие BTC
        под именем "lighter:ETH".
        """
        sent = self._fire_and_collect(["lighter:ETH"])
        self.assertEqual(sent[0]["symbol"], "lighter:BTC")

    def test_display_symbol_roundtrip(self):
        """display_symbol обратно к resolve_symbol и идемпотентен."""
        self.assertEqual(self.hub.display_symbol("BTC"), "lighter:BTC")
        self.assertEqual(self.hub.display_symbol("EUR/USD"), "fxcm:EUR/USD")
        # Уже префиксованный не удваивается.
        self.assertEqual(self.hub.display_symbol("lighter:BTC"), "lighter:BTC")
        # Неизвестный отдаётся как есть, без выдуманного провайдера.
        self.assertEqual(self.hub.display_symbol("NOPE"), "NOPE")


class TestKeepBarsByTf(unittest.TestCase):
    """Глубина окна, настраиваемая по таймфрейму."""

    def setUp(self):
        """Создать хаб.

        Returns:
            None.
        """
        self.hub, self.path = _make_hub()

    def tearDown(self):
        """Удалить временную БД.

        Returns:
            None.
        """
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.path + suffix)
            except OSError:
                pass

    def test_override_applies(self):
        """ТФ из keep_bars_by_tf получает свою глубину.

        Одно число на все ТФ неудобно: 2000 баров — это 5 лет на D1 и
        всего 33 часа на M1.
        """
        self.hub._keep_bars_by_tf = {"M1": 10080}
        self.assertEqual(self.hub.keep_bars_for("M1"), 10080)

    def test_fallback_to_global(self):
        """ТФ без переопределения живёт на общем keep_bars."""
        self.hub._keep_bars_by_tf = {"M1": 10080}
        self.assertEqual(self.hub.keep_bars_for("H1"), self.hub._keep_bars)
        self.assertEqual(self.hub.keep_bars_for("S5"), self.hub._keep_bars)

    def test_missing_section_is_safe(self):
        """Конфиг без keep_bars_by_tf не ломает хаб."""
        self.hub._keep_bars_by_tf = {}
        self.assertEqual(self.hub.keep_bars_for("M1"), self.hub._keep_bars)


class TestHealthSymbolsAge(unittest.TestCase):
    """symbols_age отдаётся под обоими именами."""

    def setUp(self):
        """Создать хаб.

        Returns:
            None.
        """
        self.hub, self.path = _make_hub()

    def tearDown(self):
        """Удалить временную БД.

        Returns:
            None.
        """
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.path + suffix)
            except OSError:
                pass

    def test_both_keys_present(self):
        """И чистый ключ, и с префиксом — оба фронта видят возраст тика.

        Отдай мы только один вариант, у половины клиентов индикатор рынка
        застыл бы в положении «закрыт».
        """
        import time

        self.hub._last_tick_by_symbol["BTC"] = time.time()
        age = self.hub.health()["symbols_age"]
        self.assertIn("BTC", age)
        self.assertIn("lighter:BTC", age)
        self.assertEqual(age["BTC"], age["lighter:BTC"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
