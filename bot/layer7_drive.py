"""
Драйвер Слоя 7: гоняет ручные сигналы через панель для замера задержки.

Python 3.10. Запускать ПОСЛЕ `python3.10 -m bot.run serve` (режим demo):
настоящий бот открывает сделки, а этот скрипт нажимает кнопки за человека.

    python3.10 -m bot.layer7_drive --trades 22

Как устроено. Подключается к WebSocket панели (127.0.0.1:8788), шлёт
команды call/put через протокол панели — ровно тот путь, что описывает
приёмка Слоя 7 («ручной сигнал из панели»). Направление чередуется:
стратегии нет, цена направления не несёт. Темп задаётся СОСТОЯНИЕМ, а не
таймером: следующий сигнал уходит только когда открытых позиций нет, то
есть прошлая сделка расчитана. Иначе движок отклонил бы её по
max_concurrent, и попытка сгорела бы впустую.

Сделки открываются по одной (min 60 с на цикл из-за экспирации), поэтому
полный прогон на 22 сделки занимает ~25 минут. Скрипт можно прервать —
прогресс уже в журнале, отчёт собирается `bot.run latency --mode demo`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

import websockets

PANEL_URL = "ws://127.0.0.1:8788"
SYMBOL = "USD/JPY"
AMOUNT = 1

# Не долбить площадку: после отправки ждём реакцию состоянием, а не
# шлём следующий сигнал пачкой.
OPEN_WAIT = 40        # сколько ждать, пока сделка перейдёт в «открыта»
GLOBAL_TIMEOUT = 60 * 60   # предохранитель на весь прогон
MAX_ATTEMPTS = 50     # отказы не должны крутить цикл вечно


def ts() -> str:
    """Текущее время локальное, для лога.

    Returns:
        Строка ЧЧ:ММ:СС.
    """
    return time.strftime("%H:%M:%S")


async def drive(trades_target: int) -> int:
    """Прогнать заданное число открытых сделок через панель.

    Args:
        trades_target: Сколько ОТКРЫТЫХ сделок нужно (отказы не считаются).

    Returns:
        Сколько сделок открыто фактически.
    """
    opened = 0
    attempts = 0
    started = time.time()

    async with websockets.connect(PANEL_URL) as ws:
        latest: dict = {}

        async def reader() -> None:
            """Читать ленту панели: кадры состояния и ответы на команды."""
            async for raw in ws:
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                if message.get("type") == "reply":
                    print(f"[{ts()}] ответ: {message['status']} — "
                          f"{message['message']}", flush=True)
                elif message.get("type") == "state":
                    latest.update(message)

        read_task = asyncio.create_task(reader())

        # Дождаться первого кадра состояния.
        while not latest.get("type"):
            if time.time() - started > 30:
                print("нет кадра состояния от панели — она жива?",
                      file=sys.stderr)
                return opened
            await asyncio.sleep(0.2)

        print(f"[{ts()}] панель на связи, режим {latest.get('mode')}, "
              f"счёт {latest.get('account')}", flush=True)
        print(f"[{ts()}] цель: {trades_target} открытых сделок по {AMOUNT} $ "
              f"на {SYMBOL}", flush=True)

        direction = "put"
        last_send = 0.0

        while opened < trades_target:
            if time.time() - started > GLOBAL_TIMEOUT:
                print(f"[{ts()}] превышен общий таймаут, останавливаюсь",
                      flush=True)
                break
            if attempts >= MAX_ATTEMPTS:
                print(f"[{ts()}] исчерпаны попытки ({MAX_ATTEMPTS}), стоп",
                      flush=True)
                break

            open_positions = latest.get("open_positions") or []

            # Прошлая сделка ещё считается — ждём расчёта.
            if open_positions:
                await asyncio.sleep(0.5)
                continue

            # Пауза после отправки, чтобы открытие/отказ успели отразиться
            # в состоянии и не слать следующий сигнал мгновенно за прошлым.
            if time.time() - last_send < 5:
                await asyncio.sleep(0.5)
                continue

            attempts += 1
            await ws.send(json.dumps({
                "cmd": direction, "symbol": SYMBOL, "amount": AMOUNT,
            }))
            print(f"[{ts()}] отправлен {direction} (попытка {attempts})",
                  flush=True)
            last_send = time.time()
            direction = "put" if direction == "call" else "call"

            # Дождаться, что сделка реально открылась (open_positions стал
            # непустым). Не открылась — значит отказ (выплата, счёт, лимит):
            # попытка не считается, ждём следующую.
            deadline = time.time() + OPEN_WAIT
            while time.time() < deadline:
                if latest.get("open_positions"):
                    break
                await asyncio.sleep(0.5)

            if latest.get("open_positions"):
                opened += 1
                print(f"[{ts()}] сделка #{opened} открыта — жду расчёта",
                      flush=True)
                # Расчёт займёт ~60 с; цикл сверху сам дождётся, пока
                # open_positions очистится.
            else:
                print(f"[{ts()}] не открылась за {OPEN_WAIT} с "
                      f"(отказ) — продолжаю", flush=True)

        read_task.cancel()
        return opened


def main() -> int:
    """Разобрать аргументы и запустить драйвер.

    Returns:
        0 — цель достигнута, 1 — нет.
    """
    parser = argparse.ArgumentParser(prog="bot.layer7_drive",
                                     description="Драйвер Слоя 7")
    parser.add_argument("--trades", type=int, default=22,
                        help="сколько открытых сделок нужно (по умолчанию 22)")
    args = parser.parse_args()

    opened = asyncio.run(drive(args.trades))
    print(f"\n[{ts()}] итог: открыто {opened} из {args.trades}")
    print("отчёт по задержке: python3.10 -m bot.run latency --mode demo")
    return 0 if opened >= args.trades else 1


if __name__ == "__main__":
    sys.exit(main())
