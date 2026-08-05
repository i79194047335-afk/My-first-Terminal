"""
Точка входа бота. Python 3.10.

Запуск:  python3.10 -m bot.run <команда>

Команды Слоя 1 (клиент API):
    check    — проверка связи с площадкой: котировки, время сервера, и, если
               заданы учётные данные, баланс и процент выплаты.
    quotes   — показать котировки разрешённых инструментов.
    clock    — измерить расхождение часов с площадкой.

Торговых команд здесь пока нет: машина состояний, ограничители и панель —
это Слои 3–6. Открывать сделки из CLI до появления ограничителей нельзя.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import datetime

from bot import config as config_module
from bot import payout
from bot.api.auth import MANUAL_STEPS, credentials_from_config, probe
from bot.api.client import IntradeClient
from bot.api.models import PlatformError, mask_hash
from bot.clock import ServerClock
from core.logfmt import setup as _log_setup

log = _log_setup("bot")


def build_client(cfg) -> IntradeClient:
    """Создать клиент площадки по конфигурации.

    Args:
        cfg: BotConfig.

    Returns:
        IntradeClient с учётными данными, если они заданы.
    """
    return IntradeClient(
        base_url=cfg.base_url,
        quotes_url=cfg.quotes_url,
        credentials=credentials_from_config(cfg),
    )


def cmd_check(cfg) -> int:
    """Проверить связь с площадкой — приёмка Слоя 1.

    Проверяются по очереди: котировки (без авторизации), время сервера,
    затем баланс и процент выплаты (требуют учётных данных). Отсутствие
    учётных данных не считается провалом: публичная часть проверяется всё
    равно, а человеку печатается инструкция, как добыть хеш.

    Args:
        cfg: BotConfig.

    Returns:
        Код возврата процесса: 0 — успех, 1 — что-то не работает.
    """
    print(f"режим: {cfg.mode}   счёт: {cfg.account}")
    print(f"торговый домен: {cfg.base_url}")
    print(f"котировки:      {cfg.quotes_url}")
    print(f"user_id: {cfg.user_id or '<не задан>'}   "
          f"user_hash: {mask_hash(cfg.user_hash)}")
    print()

    client = build_client(cfg)
    failed = False

    # 1. Котировки — публичные, авторизация не нужна.
    try:
        started = time.time()
        quotes = client.quotes()
        elapsed = (time.time() - started) * 1000
        print(f"котировки: {len(quotes)} пар за {elapsed:.0f} мс")
        for symbol in cfg.symbol_whitelist:
            quote = quotes.get(symbol)
            if quote:
                age = time.time() - quote.updated
                print(f"    {symbol}: bid {quote.bid} / ask {quote.ask} "
                      f"(возраст {age:.0f} с)")
            else:
                print(f"    {symbol}: НЕТ В КОТИРОВКАХ площадки")
    except PlatformError as err:
        print(f"котировки: ОШИБКА — {err}")
        failed = True

    # 2. Время сервера — критично для минутной экспирации.
    offset = asyncio.run(ServerClock(cfg.time_ws_url).sync_once())
    if offset is None:
        print("время сервера: НЕДОСТУПНО (будут использованы локальные часы)")
        failed = True
    else:
        print(f"время сервера: расхождение {offset:+.2f} с "
              f"({'приемлемо' if abs(offset) < 2 else 'ВЕЛИКО'})")

    # 3. Приватная часть — только при наличии учётных данных.
    if not client.credentials:
        print("\nбаланс и выплата не проверены: нет user_id/user_hash")
        print(MANUAL_STEPS)
        return 1 if failed else 0

    alive, description = probe(client)
    if alive:
        print(f"баланс: {description}")
    else:
        print(f"баланс: ОШИБКА — {description}")
        print("хеш мог протухнуть — добыть заново:")
        print(MANUAL_STEPS)
        return 1

    symbol = cfg.symbol_whitelist[0] if cfg.symbol_whitelist else "USD/JPY"
    try:
        percent = client.payout_percent(
            symbol,
            expiry_minutes=cfg.default_expiry_minutes,
            investment=cfg.default_investment,
        )
        print(f"выплата {symbol}: {payout.describe(percent)}")

        # Расхождение с ожидаемой сеткой означает, что площадка поменяла
        # правила — это надо заметить сразу, а не по итогам месяца ставок.
        expected = payout.expected_percent(cfg.default_expiry_minutes,
                                           cfg.default_investment)
        if percent != expected and not payout.is_hour_edge():
            print(f"    ВНИМАНИЕ: ожидалось {expected}% по известной сетке — "
                  f"правила площадки могли измениться")

        if percent <= 0:
            # Ноль — это способ площадки сказать «сейчас не торгуется»
            # (ночью так отвечают Classic-экспирации), а не сбой разбора.
            print("    выплата 0% — инструмент/экспирация сейчас недоступны")
        elif percent < cfg.risk.min_payout_percent:
            print(f"    ВНИМАНИЕ: ниже порога min_payout_percent="
                  f"{cfg.risk.min_payout_percent}")
    except PlatformError as err:
        print(f"выплата: ОШИБКА — {err}")
        failed = True

    # Окно у начала часа: выплата падает до 60%, безубыточный винрейт
    # прыгает с 54.9% до 62.5%. Показываем всегда, а не только при входе.
    now_msk = datetime.now(payout.MSK)
    if payout.is_hour_edge():
        print(f"время МСК {now_msk:%H:%M} — ОКНО У НАЧАЛА ЧАСА, выплата урезана")
    else:
        until = payout.minutes_until_hour_edge()
        if now_msk.hour >= payout.HOUR_EDGE_FROM_HOUR or now_msk.hour < payout.HOUR_EDGE_TO_HOUR:
            print(f"время МСК {now_msk:%H:%M} — до окна у начала часа {until:.0f} мин")
        else:
            print(f"время МСК {now_msk:%H:%M} — день, окон пониженной выплаты нет")

    return 1 if failed else 0


def cmd_quotes(cfg) -> int:
    """Показать котировки разрешённых инструментов.

    Args:
        cfg: BotConfig.

    Returns:
        Код возврата процесса.
    """
    client = build_client(cfg)
    try:
        quotes = client.quotes()
    except PlatformError as err:
        print(f"ОШИБКА: {err}")
        return 1

    symbols = cfg.symbol_whitelist or sorted(quotes)
    for symbol in symbols:
        quote = quotes.get(symbol)
        if quote:
            print(f"{symbol:10s} bid {quote.bid:<12} ask {quote.ask:<12} "
                  f"mid {quote.mid:.6f}")
        else:
            print(f"{symbol:10s} нет в котировках")
    return 0


def cmd_clock(cfg) -> int:
    """Измерить расхождение часов с площадкой несколько раз подряд.

    Args:
        cfg: BotConfig.

    Returns:
        Код возврата процесса.
    """

    async def measure() -> list:
        """Снять серию замеров расхождения.

        Returns:
            Список расхождений в секундах.
        """
        clock = ServerClock(cfg.time_ws_url)
        await clock.start()
        samples = []
        for _ in range(5):
            await asyncio.sleep(1.2)
            if clock.synced:
                samples.append(clock.offset)
        await clock.stop()
        return samples

    samples = asyncio.run(measure())
    if not samples:
        print("время сервера недоступно")
        return 1

    for index, value in enumerate(samples, 1):
        print(f"замер {index}: {value:+.3f} с")
    print(f"среднее: {sum(samples) / len(samples):+.3f} с")
    return 0


def main() -> int:
    """Разобрать аргументы и выполнить команду.

    Returns:
        Код возврата процесса.
    """
    parser = argparse.ArgumentParser(
        prog="bot.run", description="Бот intrade.bar — Слой 1 (клиент API)"
    )
    parser.add_argument(
        "command",
        choices=("check", "quotes", "clock"),
        help="check — проверка связи, quotes — котировки, clock — часы",
    )
    parser.add_argument(
        "--config",
        default=config_module.DEFAULT_CONFIG_PATH,
        help="путь к bot_config.json",
    )
    args = parser.parse_args()

    try:
        cfg = config_module.load(args.config)
    except ValueError as err:
        print(f"конфигурация: {err}")
        return 2

    if cfg.mode == "live":
        # Двойная перестраховка: config.validate уже это проверил, но цена
        # ошибки здесь такова, что лишний рубеж дешевле сожалений.
        print("режим live в этой задаче запрещён")
        return 2

    handlers = {"check": cmd_check, "quotes": cmd_quotes, "clock": cmd_clock}
    return handlers[args.command](cfg)


if __name__ == "__main__":
    sys.exit(main())
