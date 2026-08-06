"""
Тесты режимов аккаунта intrade.bar: разбор /profile и протокол переключения.

Запуск:  python3.10 tests/test_bot_profile.py    (из корня проекта)

pytest в проекте нет — файл запускается напрямую и сам печатает результат.

Почему эти тесты строже остальных. /user_real_trade.php — тумблер БЕЗ
параметра (карта API §3.1): он инвертирует тип счёта, и слепой вызов может
перевести счёт на реал. Ошибка в разборе /profile или в протоколе
переключения означает ставку реальными деньгами при конфиге "demo".
"""

import os
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.api.client import IntradeClient
from bot.api.models import AccountProfile, Credentials, PlatformError
from bot.api.parsers import parse_profile

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "fixtures", "intrade")

passed = 0
failed = 0


def check(name, condition, detail=""):
    """Проверить условие и напечатать результат.

    Args:
        name:      Название проверки.
        condition: Истина = прошло.
        detail:    Что показать при провале.

    Returns:
        None.
    """
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS {name}")
    else:
        failed += 1
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))


def load(name):
    """Прочитать фикстуру.

    Args:
        name: Имя файла в tests/fixtures/intrade/.

    Returns:
        Содержимое строкой.
    """
    with open(os.path.join(FIXTURES, name), "r") as fh:
        return fh.read()


def test_parse_profile():
    """Разбор /profile: оба состояния счёта и отказ при неоднозначности."""
    print("parse_profile")

    demo = parse_profile(load("profile_demo.html"))
    check("демо распознан", demo.account == "demo", demo.account)
    check("тип сделок sprint", demo.trade_type == "sprint", demo.trade_type)
    check("валюта usd", demo.currency == "usd", demo.currency)

    real = parse_profile(load("profile_real.html"))
    check("реал распознан", real.account == "real", real.account)

    # Ключевая ловушка разметки: onclick стоит на ПРОТИВОПОЛОЖНОМ варианте.
    # Прочитав onclick вместо checked, парсер определил бы режим наоборот.
    # Разметка настоящая (HAR 2026-08-06): у input НЕТ атрибута name,
    # только id + checked/onclick.
    check("демо-фикстура содержит onclick у РЕАЛА (ловушка на месте)",
          'id="personal-radio1" type="radio"  onclick="user_real_trade();"'
          in load("profile_demo.html"))
    check("реал-фикстура содержит onclick у ДЕМО (ловушка на месте)",
          'id="personal-radio2" type="radio"  onclick="user_demo_trade();"'
          in load("profile_real.html"))

    try:
        parse_profile(load("profile_ambiguous.html"))
        check("неоднозначность — отказ", False, "разбор не отказал")
    except PlatformError as err:
        check("неоднозначность — отказ", err.code == "profile_ambiguous",
              err.code)

    # Смена вёрстки (пары нет вовсе) — тоже отказ, а не догадка.
    try:
        parse_profile("<html><body>совсем другая страница</body></html>")
        check("чужая вёрстка — отказ", False, "разбор не отказал")
    except PlatformError as err:
        check("чужая вёрстка — отказ", err.code == "profile_layout_changed",
              err.code)


class ScriptedClient(IntradeClient):
    """Клиент со сценарием вместо сети — для проверки протокола.

    profile() отдаёт заранее заданные ответы по очереди,
    _toggle_account_mode() пишет вызовы в журнал. Ни одного сетевого вызова.
    """

    def __init__(self, profiles, toggle_response="ok"):
        """Создать клиент со сценарием.

        Args:
            profiles:        Список AccountProfile или PlatformError,
                             отдаваемых profile() по очереди.
            toggle_response: Что вернёт тумблер.
        """
        super().__init__(credentials=Credentials(user_id=1, user_hash="x" * 32))
        self._profiles = list(profiles)
        self._toggle_response = toggle_response
        self.toggle_calls = 0

    def profile(self):
        """Отдать следующий ответ сценария.

        Returns:
            AccountProfile из сценария.

        Raises:
            PlatformError: Если в сценарии на этом месте ошибка.
        """
        item = self._profiles.pop(0)
        if isinstance(item, PlatformError):
            raise item
        return item

    def _toggle_account_mode(self):
        """Посчитать вызов тумблера.

        Returns:
            Заранее заданный ответ.
        """
        self.toggle_calls += 1
        return self._toggle_response


def _profile(account, trade_type="sprint", currency="usd"):
    """Собрать AccountProfile коротко.

    Args:
        account:    "demo" / "real".
        trade_type: Тип сделок.
        currency:   Валюта.

    Returns:
        AccountProfile.
    """
    return AccountProfile(account=account, trade_type=trade_type,
                          currency=currency)


def test_ensure_protocol():
    """ensure_account_mode: читает до и после, не дёргает тумблер зря."""
    print("ensure_account_mode")

    # Совпадает — тумблер НЕ трогается. Это главное свойство протокола.
    client = ScriptedClient([_profile("demo")])
    result = client.ensure_account_mode("demo")
    check("совпадение: тумблер не тронут", client.toggle_calls == 0,
          f"вызовов {client.toggle_calls}")
    check("совпадение: вернул профиль", result.account == "demo")

    # Расхождение — один вызов и обязательная перечитка.
    client = ScriptedClient([_profile("real"), _profile("demo")])
    result = client.ensure_account_mode("demo")
    check("расхождение: ровно один вызов", client.toggle_calls == 1,
          f"вызовов {client.toggle_calls}")
    check("расхождение: состояние подтверждено", result.account == "demo")

    # Площадка отклонила (незакрытые сделки) — ошибка, не тихий успех.
    client = ScriptedClient([_profile("real")], toggle_response="error")
    try:
        client.ensure_account_mode("demo")
        check("отказ площадки — исключение", False, "не поднялось")
    except PlatformError as err:
        check("отказ площадки — исключение", err.code == "toggle_refused",
              err.code)

    # Перечитка показала НЕ целевое состояние — ошибка.
    client = ScriptedClient([_profile("real"), _profile("real")])
    try:
        client.ensure_account_mode("demo")
        check("несхождение после — исключение", False, "не поднялось")
    except PlatformError as err:
        check("несхождение после — исключение", err.code == "toggle_failed",
              err.code)

    # Неизвестная цель — ValueError до любых запросов.
    client = ScriptedClient([])
    try:
        client.ensure_account_mode("прод")
        check("кривая цель — ValueError", False, "не поднялось")
    except ValueError:
        check("кривая цель — ValueError", True)


def test_active_trades_endpoint():
    """Сверка UNKNOWN не должна ходить на тумблер счёта.

    Регрессия: до 2026-08-06 active_trades() дёргал /user_real_trade.php —
    каждая сверка потерянного ответа молча переключала бы тип счёта.
    """
    print("active_trades: эндпоинт")

    captured = {}

    class CapturingClient(IntradeClient):
        """Клиент, перехватывающий путь POST вместо сети."""

        def _post(self, path, data, retries=2):
            """Записать путь и вернуть заглушку.

            Args:
                path:    Путь запроса.
                data:    Поля формы.
                retries: Не используется.

            Returns:
                Пустая строка-заглушка.
            """
            captured["path"] = path
            captured["data"] = dict(data)
            return ""

    client = CapturingClient(
        credentials=Credentials(user_id=1, user_hash="x" * 32))
    client.active_trades()
    check("ходит на trade_load_more2.php",
          captured.get("path") == "/trade_load_more2.php", captured.get("path"))
    check("НЕ ходит на user_real_trade.php",
          captured.get("path") != "/user_real_trade.php")
    check("передаёт last", captured.get("data", {}).get("last") == "0")


def test_profile_auth_cookies():
    """/profile авторизуется куками user_id/user_hash — бот обязан их слать.

    Регрессия 2026-08-06: profile() ходил на GET /profile БЕЗ кук, площадка
    отвечала JS-редиректом на главную, и живая проверка счёта была
    невозможна: тест на фикстурах проходил, а на боевом профиле движок
    каждый раз останавливал торговлю «не прочитать /profile».

    Проверяется САМ механизм авторизации: куки должны попасть в заголовок
    Cookie запроса к торговому домену и НЕ попасть в запросы к
    котировочному домену (user_hash — ключ от аккаунта).
    """
    print("profile: куки авторизации")

    html = load("profile_demo.html")

    class FakeResponse:
        """Минимальный ответ, нужный profile(): текст + raise_for_status."""

        def __init__(self, text):
            self.text = text

        def raise_for_status(self):
            """Ничего не делать — запрос «успешен»."""
            return None

    captured = {}

    client = IntradeClient(
        base_url="https://intrade35.bar",
        quotes_url="https://intrade.bar/price_now",
        credentials=Credentials(user_id=30169, user_hash="b" * 32),
    )

    def fake_get(url, timeout=None, **kwargs):
        """Записать Cookie-заголовок, который ушёл бы в запрос, и ответить."""
        captured["url"] = url
        prepared = requests.PreparedRequest()
        prepared.prepare(
            method="GET", url=url, cookies=client.session.cookies)
        captured["cookie_header"] = prepared.headers.get("Cookie", "")
        return FakeResponse(html)

    client.session.get = fake_get

    # Торговый домен: куки ушли, профиль разобран.
    prof = client.profile()
    check("profile читает /profile", captured["url"].endswith("/profile"),
          captured["url"])
    cookie = captured["cookie_header"]
    check("кука user_id ушла на /profile", "user_id=30169" in cookie, cookie)
    check("кука user_hash ушла на /profile", f"user_hash={'b' * 32}" in cookie)
    check("профиль разобран (demo)", prof.account == "demo")

    # Котировочный домен: user_hash уходить не должен.
    prepared = requests.PreparedRequest()
    prepared.prepare(method="GET", url="https://intrade.bar/price_now",
                     cookies=client.session.cookies)
    quotes_cookie = prepared.headers.get("Cookie", "")
    check("хеш НЕ уходит на котировочный домен",
          "user_hash=" not in quotes_cookie, quotes_cookie)

    # Куки не мешают обычному пути полей формы.
    fields = client._auth_fields()
    check("поля формы на месте", fields.get("user_id") == "30169"
          and fields.get("user_hash") == "b" * 32)


def main():
    """Прогнать все тесты и вернуть код возврата.

    Returns:
        0 — всё прошло, 1 — есть провалы.
    """
    for test in (test_parse_profile, test_ensure_protocol,
                 test_active_trades_endpoint, test_profile_auth_cookies):
        test()
        print()

    print(f"итого: {passed} прошло, {failed} провалено")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
