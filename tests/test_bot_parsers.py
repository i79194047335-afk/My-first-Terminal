"""
Тесты разбора ответов intrade.bar. Python 3.10.

Запуск:  python3.10 tests/test_bot_parsers.py    (из корня проекта)

pytest в проекте нет — файл запускается напрямую и сам печатает результат.

Главное правило этих тестов: разбор проверяется на СОХРАНЁННЫХ РЕАЛЬНЫХ
ответах площадки (tests/fixtures/intrade/), а не на выдуманных строках.
Выдуманный ответ проверяет только то, что автор теста и автор парсера
одинаково фантазируют.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.api.client import to_canonical_symbol, to_platform_symbol, _format_amount
from bot.api.models import PlatformError
from bot.api.parsers import (
    check_error,
    extract_credentials,
    parse_balance,
    parse_percent,
    parse_quotes,
    parse_trade_check,
    parse_trade_open,
)

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "intrade")

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


def load_fixture(name):
    """Прочитать сохранённый ответ площадки.

    Args:
        name: Имя файла в tests/fixtures/intrade/.

    Returns:
        Содержимое файла строкой.
    """
    with open(os.path.join(FIXTURES, name), "r", encoding="utf-8") as handle:
        return handle.read()


def test_trade_open():
    """Разбор реального ответа на открытие сделки."""
    print("parse_trade_open (реальный ответ площадки)")
    trade = parse_trade_open(load_fixture("trade_open_ok.html"), "USD/JPY", 2)

    check("trade_id", trade.trade_id == 224809704, trade.trade_id)
    check("цена входа", trade.entry_price == 157.666, trade.entry_price)
    check("время открытия", trade.open_ts == 1785953081, trade.open_ts)
    check("время экспирации", trade.expiry_ts == 1785953141, trade.expiry_ts)
    check("длительность", trade.duration == 60, trade.duration)
    check("направление call", trade.direction == "call", trade.direction)
    check("символ канонический", trade.symbol == "USD/JPY", trade.symbol)
    check("сырой ответ сохранён", trade.raw.strip().startswith("<tr"))
    # Экспирация ровно через минуту после открытия — важное свойство Sprint.
    check("экспирация = открытие + 60", trade.expiry_ts - trade.open_ts == 60)


def test_errors():
    """Ошибки площадки распознаются ДО разбора сделки."""
    print("check_error (отказы площадки)")
    cases = {
        "": "empty",
        "   ": "empty",
        "error_time_18": "error_time_18",
        "error_time_night": "error_time_night",
        "error_time_night_order": "error_time_night_order",
        "error_something_new": "unknown_error",
    }
    for text, expected_code in cases.items():
        try:
            check_error(text)
            check(f"отказ {text!r}", False, "исключение не поднято")
        except PlatformError as err:
            check(f"отказ {text!r} → {expected_code}",
                  err.code == expected_code, err.code)

    # Успешный ответ не должен приниматься за ошибку.
    try:
        check_error(load_fixture("trade_open_ok.html"))
        check("успешный HTML не считается ошибкой", True)
    except PlatformError as err:
        check("успешный HTML не считается ошибкой", False, err.code)

    # Открытие сделки на ошибочном ответе обязано падать, а не возвращать
    # полупустую сделку: иначе бот сочтёт ставку сделанной.
    for text in ("", "error_time_18"):
        try:
            parse_trade_open(text, "USD/JPY", 2)
            check(f"parse_trade_open({text!r}) падает", False, "вернул сделку")
        except PlatformError:
            check(f"parse_trade_open({text!r}) падает", True)


def test_balance():
    """Разбор баланса с неразрывными пробелами и запятой."""
    print("parse_balance")
    # Реальный формат из HAR: неразрывный пробел + запятая.
    balance = parse_balance("9 363,08 $")
    check("сумма", abs(balance.amount - 9363.08) < 0.001, balance.amount)
    check("валюта", balance.currency == "$", repr(balance.currency))
    check("сырая строка", balance.raw == "9 363,08 $")

    check("обычный пробел", abs(parse_balance("1 000,50 $").amount - 1000.50) < 0.001)
    check("без разделителей", abs(parse_balance("42.5").amount - 42.5) < 0.001)

    try:
        parse_balance("совсем не число")
        check("мусор отвергается", False, "разобрал")
    except PlatformError as err:
        check("мусор отвергается", err.code == "parse_failed", err.code)


def test_percent():
    """Разбор процента выплаты."""
    print("parse_percent")
    check("82", parse_percent("82") == 82)
    check("с пробелами", parse_percent(" 79 \n") == 79)
    try:
        parse_percent("нет числа")
        check("мусор отвергается", False, "разобрал")
    except PlatformError:
        check("мусор отвергается", True)


def test_quotes():
    """Разбор котировок price_now."""
    print("parse_quotes")
    payload = {
        "AUD/USD": {"Updates": 1785950970, "ask": 0.70529, "bid": 0.70527},
        "USD/JPY": {"Updates": 1785950971, "ask": 157.669, "bid": 157.666},
        "BROKEN": {"Updates": 1785950971},           # без bid/ask
        "ALSO_BROKEN": "не словарь",
    }
    quotes = parse_quotes(payload)
    check("разобрано 2 из 4", len(quotes) == 2, len(quotes))
    check("bid", quotes["USD/JPY"].bid == 157.666)
    check("mid", abs(quotes["AUD/USD"].mid - 0.70528) < 1e-6, quotes["AUD/USD"].mid)
    check("битые записи пропущены", "BROKEN" not in quotes)
    check("пустой payload не падает", parse_quotes({}) == {})
    check("None не падает", parse_quotes(None) == {})


def test_trade_check():
    """Разбор итога сделки; формат живьём НЕ подтверждён."""
    print("parse_trade_check")
    # Ставка 2, вернулось 3.64 (выплата 82%) — выигрыш.
    win = parse_trade_check("2;3.64;9363.08", 224809704, 2)
    check("выигрыш", win.outcome == "win", win.outcome)
    check("pnl выигрыша", abs(win.pnl - 1.64) < 0.001, win.pnl)

    loss = parse_trade_check("2;0;9359.56", 224809704, 2)
    check("проигрыш", loss.outcome == "loss", loss.outcome)
    check("pnl проигрыша", abs(loss.pnl + 2) < 0.001, loss.pnl)

    refund = parse_trade_check("2;2;9361.56", 224809704, 2)
    check("возврат", refund.outcome == "refund", refund.outcome)

    # Пустой ответ = ещё не рассчитано. Это НЕ ошибка, в отличие от открытия.
    pending = parse_trade_check("", 224809704, 2)
    check("пусто → unknown", pending.outcome == "unknown", pending.outcome)

    # Неизвестный формат не должен превращаться в выдуманный результат.
    weird = parse_trade_check("совершенно другой формат", 224809704, 2)
    check("мусор → unknown", weird.outcome == "unknown", weird.outcome)
    check("сырой ответ сохранён", weird.raw == "совершенно другой формат")


def test_credentials():
    """Извлечение user_id/user_hash из HTML страницы."""
    print("extract_credentials")
    html = '''
      <script>
        var user_id = 30169;
        var user_hash = "0123456789abcdef0123456789abcdef";
      </script>
    '''
    user_id, user_hash = extract_credentials(html)
    check("user_id", user_id == 30169, user_id)
    check("user_hash", user_hash == "0123456789abcdef0123456789abcdef", user_hash)

    empty_id, empty_hash = extract_credentials("<html>ничего нет</html>")
    check("нет данных → None", empty_id is None and empty_hash is None)


def test_symbols():
    """Преобразование символов между двумя формами площадки."""
    print("символы и суммы")
    check("USD/JPY → USDJPY", to_platform_symbol("USD/JPY") == "USDJPY")
    check("уже без слэша", to_platform_symbol("USDJPY") == "USDJPY")
    check("USDJPY → USD/JPY", to_canonical_symbol("USDJPY") == "USD/JPY")
    check("уже со слэшем", to_canonical_symbol("USD/JPY") == "USD/JPY")
    # Целые суммы без ".0" — так шлёт их фронт.
    check("сумма 2", _format_amount(2) == "2", _format_amount(2))
    check("сумма 2.0", _format_amount(2.0) == "2", _format_amount(2.0))
    check("сумма 2.5", _format_amount(2.5) == "2.50", _format_amount(2.5))


def main():
    """Прогнать все тесты и вернуть код возврата.

    Returns:
        0 — всё прошло, 1 — есть провалы.
    """
    for test in (test_trade_open, test_errors, test_balance, test_percent,
                 test_quotes, test_trade_check, test_credentials, test_symbols):
        test()
        print()

    print(f"итого: {passed} прошло, {failed} провалено")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
