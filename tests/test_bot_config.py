"""
Тесты конфигурации бота. Python 3.10.

Запуск:  python3.10 tests/test_bot_config.py    (из корня проекта)

Проверяется главным образом безопасное поведение по умолчанию: бот без
конфига обязан подниматься в режиме dry, а режим live — не включаться ни
при каких обстоятельствах без явного разрешения в окружении. Эти правила
легко ослабить случайной правкой, поэтому они закреплены тестом.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import config as config_module

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


def write_config(data):
    """Записать временный bot_config.json.

    Args:
        data: Словарь конфигурации.

    Returns:
        Путь к временному файлу.
    """
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8")
    json.dump(data, handle)
    handle.close()
    return handle.name


def test_defaults():
    """Без файла конфигурации бот поднимается в безопасном режиме."""
    print("умолчания")
    cfg = config_module.load("/nonexistent/bot_config.json", env_path="/nonexistent/.env")
    check("режим dry", cfg.mode == "dry", cfg.mode)
    check("не ходит к площадке", cfg.touches_platform is False)
    check("не live", cfg.is_live is False)
    check("торговый домен", cfg.base_url == "https://intrade35.bar", cfg.base_url)
    # Домены РАЗНЫЕ — частая правка «привести к одному» ломает котировки.
    check("котировки на другом домене",
          "intrade.bar/price_now" in cfg.quotes_url and "intrade35" not in cfg.quotes_url,
          cfg.quotes_url)
    check("журнал не market.db", cfg.db_path != "market.db", cfg.db_path)
    check("ограничители на месте", cfg.risk.max_concurrent == 1)


def test_unknown_mode_falls_back():
    """Опечатка в режиме сводится к dry, а не к торговле."""
    print("неизвестный режим")
    path = write_config({"mode": "demoo"})
    try:
        cfg = config_module.load(path, env_path="/nonexistent/.env")
        check("опечатка → dry", cfg.mode == "dry", cfg.mode)
        check("к площадке не ходит", cfg.touches_platform is False)
    finally:
        os.unlink(path)


def test_live_blocked():
    """Режим live запрещён без явного разрешения в окружении."""
    print("режим live")
    path = write_config({"mode": "live"})
    saved = os.environ.pop("INTRADE_ALLOW_LIVE", None)
    try:
        try:
            config_module.load(path, env_path="/nonexistent/.env")
            check("live без разрешения отвергнут", False, "конфиг принят")
        except ValueError as err:
            check("live без разрешения отвергнут", "live" in str(err).lower())

        # С разрешением конфиг обязан загрузиться — иначе проверить режим
        # будет нельзя вовсе. Третий рубеж (подтверждение в панели) — в
        # исполнителе, здесь его нет.
        os.environ["INTRADE_ALLOW_LIVE"] = "yes"
        cfg = config_module.load(path, env_path="/nonexistent/.env")
        check("live с разрешением загружается", cfg.is_live is True)
    finally:
        os.environ.pop("INTRADE_ALLOW_LIVE", None)
        if saved is not None:
            os.environ["INTRADE_ALLOW_LIVE"] = saved
        os.unlink(path)


def test_env_overrides():
    """Секреты из окружения перекрывают файл конфигурации."""
    print("окружение поверх конфига")
    path = write_config({"user_id": 1, "user_hash": "from_config"})
    saved_id = os.environ.pop("INTRADE_USER_ID", None)
    saved_hash = os.environ.pop("INTRADE_USER_HASH", None)
    try:
        os.environ["INTRADE_USER_ID"] = "30169"
        os.environ["INTRADE_USER_HASH"] = "from_env"
        cfg = config_module.load(path, env_path="/nonexistent/.env")
        check("user_id из окружения", cfg.user_id == 30169, cfg.user_id)
        check("user_hash из окружения", cfg.user_hash == "from_env", cfg.user_hash)
    finally:
        os.environ.pop("INTRADE_USER_ID", None)
        os.environ.pop("INTRADE_USER_HASH", None)
        if saved_id is not None:
            os.environ["INTRADE_USER_ID"] = saved_id
        if saved_hash is not None:
            os.environ["INTRADE_USER_HASH"] = saved_hash
        os.unlink(path)


def test_validation():
    """Бессмысленные значения отвергаются на старте."""
    print("валидация")
    for data, what in (
        ({"default_investment": 0}, "нулевая ставка"),
        ({"default_investment": -5}, "отрицательная ставка"),
        ({"default_expiry_minutes": 0}, "нулевая экспирация"),
    ):
        path = write_config(data)
        try:
            config_module.load(path, env_path="/nonexistent/.env")
            check(f"{what} отвергнута", False, "конфиг принят")
        except ValueError:
            check(f"{what} отвергнута", True)
        finally:
            os.unlink(path)


def test_unknown_risk_key_ignored():
    """Опечатка в ограничителях не валит старт и не становится настройкой."""
    print("незнакомый ключ ограничителя")
    path = write_config({"risk": {"max_concurent": 99, "max_concurrent": 2}})
    try:
        cfg = config_module.load(path, env_path="/nonexistent/.env")
        check("правильный ключ применён", cfg.risk.max_concurrent == 2,
              cfg.risk.max_concurrent)
        check("опечатка проигнорирована", not hasattr(cfg.risk, "max_concurent"))
    finally:
        os.unlink(path)


def test_touches_platform():
    """Свойство touches_platform различает режимы верно."""
    print("touches_platform по режимам")
    expected = {"dry": False, "shadow": False, "demo": True}
    for mode, should_touch in expected.items():
        path = write_config({"mode": mode})
        try:
            cfg = config_module.load(path, env_path="/nonexistent/.env")
            check(f"{mode} → {should_touch}", cfg.touches_platform is should_touch)
        finally:
            os.unlink(path)


def main():
    """Прогнать все тесты и вернуть код возврата.

    Returns:
        0 — всё прошло, 1 — есть провалы.
    """
    for test in (test_defaults, test_unknown_mode_falls_back, test_live_blocked,
                 test_env_overrides, test_validation, test_unknown_risk_key_ignored,
                 test_touches_platform):
        test()
        print()

    print(f"итого: {passed} прошло, {failed} провалено")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
