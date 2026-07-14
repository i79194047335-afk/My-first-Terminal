"""
Хаб: шина → нарезка свечей → SQLite → WebSocket для браузера.

Заменяет собой ту часть монолита server.py, которая не зависит от брокера:
приём тиков, нарезка, хранение, раздача истории фронту. Сам к брокеру не ходит —
тики приходят от фидов через внутреннюю шину (core/bus.py), потому что
forexconnect требует Python 3.7, а SDK Lighter — 3.8+, и в одном процессе они
не уживаются.

Запуск (параллельно живому server.py, порт 8767 против боевого 8765):

    python3.10 hub.py

Конфиг — retention.json. Пишет в СВОЮ базу (market_hub.db): боевая market.db
принадлежит живому server.py, и второй писатель в те же ключи
(provider, symbol, tf, time) плюс независимая подрезка окна испортили бы данные.

Синтаксис намеренно ограничен Python 3.7 (хаб бежит на 3.10, но тесты гоняются
на обоих: у фида FXCM выбора нет).
"""

import asyncio
import json
import os
import queue
import threading
import time

import websockets

from core.bus import BusServer
from core.candles import CandleBuilder
from core.db import init_db, load_history, trim_window, upsert_candle, \
    upsert_instrument

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "retention.json")


def load_config(path=CONFIG_PATH):
    """Прочитать конфиг хаба.

    Args:
        path: Путь к retention.json.

    Returns:
        Dict конфигурации.

    Raises:
        FileNotFoundError: если конфига нет (запускать хаб «на умолчаниях»
            нельзя: db_path и ws_port решают, не затрём ли мы боевые данные).
    """
    if not os.path.exists(path):
        raise FileNotFoundError("нет конфига хаба: %s" % path)
    with open(path, "r") as f:
        return json.load(f)


class Hub:
    """Приёмник шины: режет тики в свечи, хранит их и раздаёт браузеру."""

    def __init__(self, config):
        """Создать хаб.

        Args:
            config: Dict из load_config().

        Returns:
            None.
        """
        self._config     = config
        self._tf_seconds = config["tf_seconds"]
        self._broker_tf  = tuple(config.get("broker_tf", ("H1", "H4", "D1")))
        self._keep_bars  = config["keep_bars"]

        self._conn        = None
        self._builders    = {}   # (provider, symbol) -> CandleBuilder
        self._history     = {}   # (provider, symbol) -> {tf: [закрытые свечи]}
        self._clients     = {}   # ws -> {"provider","symbol","tf","requestId"}
        self._instruments = {}   # provider -> [метаданные инструментов]
        self._loop        = None

        self._db_queue = queue.Queue(maxsize=5000)

        self.ticks_received = 0
        self.ticks_ignored  = 0
        self.closed_candles = 0
        self.db_dropped     = 0

    # ── восстановление после рестарта ───────────────────────────────────

    def restore(self):
        """Поднять историю из SQLite, создать билдеры, восстановить живые свечи.

        Args:
            None.

        Returns:
            None.
        """
        self._conn = init_db(self._config["db_path"])
        now = int(time.time())

        for provider, symbols in self._config["markets"].items():
            for symbol in symbols:
                key  = (provider, symbol)
                hist = {}
                for tf in self._tf_seconds:
                    bars = load_history(self._conn, provider, symbol, tf)
                    if bars:
                        hist[tf] = bars
                self._history[key] = hist

                # Смещение сетки учим ТОЛЬКО по ТФ, которые грузятся у брокера
                # напрямую (H1/H4/D1): только они несут его сетку. Остальные
                # режем сами по UTC — там смещение 0.
                broker_bars = {tf: hist[tf] for tf in self._broker_tf if hist.get(tf)}
                offsets = CandleBuilder.detect_offsets(broker_bars, self._tf_seconds)

                builder = CandleBuilder(self._tf_seconds, offsets)
                for tf, bars in hist.items():
                    builder.seed_history(tf, bars)

                self._builders[key] = builder
                self._restore_forming(provider, symbol, now)

                total = sum(len(b) for b in hist.values())
                print("[restore] %s:%s — %d баров, смещения %s"
                      % (provider, symbol, total, offsets or "{}"))

    def _restore_forming(self, provider, symbol, now_ts):
        """Восстановить незакрытую свечу текущего бакета для всех ТФ >= 1 мин.

        Перенос server.py:seed_current_candles(). Без этого первый живой тик
        открыл бы свечу от своей цены: на H4/D1 это фальшивый спайк, на M1 —
        разрыв (баги 65b856b и 50e63ac).

        Args:
            provider: Провайдер.
            symbol:   Инструмент.
            now_ts:   Текущее время, unix-секунды.

        Returns:
            None. Заполняет billder.seed_current() и чистит дубль из истории.
        """
        key     = (provider, symbol)
        builder = self._builders[key]
        hist    = self._history[key]
        m1      = hist.get("M1") or []

        seeded = 0
        for tf, sec in self._tf_seconds.items():
            if sec < 60:
                # Секундные ТФ из M1 не восстановить, и в живом потоке они
                # открываются от цены тика — гэп там норма, а не дефект.
                continue

            offset = builder.offsets.get(tf, 0)
            bucket = ((int(now_ts) - offset) // sec) * sec + offset

            bars = [c for c in m1 if c["time"] >= bucket]

            # open = close последней ЗАКРЫТОЙ свечи ЭТОГО ЖЕ ТФ, строго раньше
            # bucket. Не из M1: иначе свеча повиснет с разрывом от предыдущей.
            # Строго раньше — потому что H4/D1 брокер отдаёт баром ТЕКУЩЕГО
            # бакета, и это тот самый бар, который мы сейчас пересобираем.
            past       = [c for c in (hist.get(tf) or []) if c["time"] < bucket]
            prev_close = past[-1]["close"] if past else None

            # Для M1 bars ВСЕГДА пуст: его бакет — текущая минута, закрытой
            # минутки для неё в истории быть не может. Раньше на этом ТФ и рвало
            # сильнее всего. Если prev_close известен — открываем свечу прямо от
            # него, всю в одной точке; первый тик её дополнит.
            if not bars and prev_close is None:
                continue

            if bars:
                open_p  = prev_close if prev_close is not None else bars[0]["open"]
                high_p  = max(open_p, max(c["high"] for c in bars))
                low_p   = min(open_p, min(c["low"]  for c in bars))
                close_p = bars[-1]["close"]
            else:
                open_p = high_p = low_p = close_p = prev_close

            builder.seed_current(tf, {
                "time":  bucket,
                "open":  open_p,
                "high":  high_p,
                "low":   low_p,
                "close": close_p,
            })

            # Тот же бакет мог остаться в истории как «закрытый» (H4/D1 от
            # брокера) — теперь он живёт в current_candle, иначе фронт получит
            # его дважды.
            if hist.get(tf):
                hist[tf] = [c for c in hist[tf] if c["time"] != bucket]

            seeded += 1

        print("[restore] %s:%s — живая свеча восстановлена на %d ТФ"
              % (provider, symbol, seeded))

    # ── приём из шины ───────────────────────────────────────────────────

    def on_bus_message(self, msg):
        """Колбэк BusServer: тик или метаданные инструментов.

        Синхронный, исполняется в потоке event loop. Исключение здесь не должно
        стоить нам потока данных — ловим и продолжаем.

        Args:
            msg: Валидированное сообщение шины.

        Returns:
            None.
        """
        try:
            if msg["type"] == "tick":
                self._handle_tick(msg)
            elif msg["type"] == "instruments":
                self._handle_instruments(msg)
        except Exception as err:
            print("[hub] ошибка на сообщении шины: %r" % (err,))

    def _handle_tick(self, msg):
        """Нарезать тик в свечи, сохранить закрытые, разослать живую.

        Args:
            msg: Сообщение шины типа "tick".

        Returns:
            None.
        """
        provider = msg["provider"]
        symbol   = msg["symbol"]
        key      = (provider, symbol)

        builder = self._builders.get(key)
        if builder is None:
            # Пара не из белого списка retention.json. Проверяем по ПАРЕ
            # (provider, symbol), а не по одному symbol: "BTC" у двух разных
            # провайдеров — это два разных инструмента.
            self.ticks_ignored += 1
            return

        self.ticks_received += 1
        closed = builder.ingest(msg["price"], msg["ts"])

        for tf, candle in closed.items():
            self.closed_candles += 1
            bars = self._history[key].setdefault(tf, [])
            bars.append(candle)
            # Держим в памяти ровно окно ретеншена: в server.py этот список рос
            # без границ.
            if len(bars) > self._keep_bars:
                del bars[:len(bars) - self._keep_bars]
            self._enqueue_closed(provider, symbol, tf, candle)

        self._broadcast_update(provider, symbol)

    def _handle_instruments(self, msg):
        """Сохранить метаданные инструментов и разослать их браузерам.

        Args:
            msg: Сообщение шины типа "instruments".

        Returns:
            None.
        """
        provider = msg["provider"]
        data     = msg["data"]
        self._instruments[provider] = data

        for item in data:
            upsert_instrument(
                self._conn, provider, item["symbol"],
                price_decimals=item.get("price_decimals"),
                size_decimals=item.get("size_decimals"),
                min_base=item.get("min_base"),
                has_volume=item.get("has_volume", False),
                meta=item.get("meta"),
                updated=int(time.time()),
            )

        self._send_all(json.dumps({"type": "instruments",
                                   "data": self._instruments}))

    # ── запись в SQLite (отдельный поток) ───────────────────────────────

    def _enqueue_closed(self, provider, symbol, tf, candle):
        """Поставить закрытую свечу в очередь на запись. Никогда не блокирует.

        Args:
            provider: Провайдер.
            symbol:   Инструмент.
            tf:       Таймфрейм.
            candle:   Закрытая свеча.

        Returns:
            None.
        """
        try:
            self._db_queue.put_nowait((provider, symbol, tf, candle))
        except queue.Full:
            self.db_dropped += 1
            print("[hub] очередь БД полна — свеча %s %s %s потеряна"
                  % (provider, symbol, tf))

    def db_writer(self):
        """Поток-демон: писать закрытые свечи в SQLite.

        Своя коннекция внутри потока: sqlite3 не терпит межпоточного доступа
        (check_same_thread). Подрезка окна — раз в trim_every записей, а не на
        каждой свече: она стоит SELECT+DELETE по каждой паре и ТФ.

        Args:
            None.

        Returns:
            None (бесконечный цикл).
        """
        conn  = init_db(self._config["db_path"])
        count = 0

        while True:
            provider, symbol, tf, candle = self._db_queue.get()
            try:
                upsert_candle(conn, provider, symbol, tf, candle)
                count += 1

                if count % self._config["trim_every"] == 0:
                    for prov, syms in self._config["markets"].items():
                        for sym in syms:
                            for tf_name in self._tf_seconds:
                                trim_window(conn, prov, sym, tf_name,
                                            self._keep_bars)
            except Exception as err:
                print("[hub] db_writer: %r — свеча пропущена" % (err,))

    # ── WebSocket для браузера ──────────────────────────────────────────

    def _provider_of(self, symbol):
        """Найти провайдера, у которого есть такой символ.

        Временная мера на период миграции: фронт пока шлёт голый "EUR/USD" без
        провайдера. После Фазы 2.5 символы станут "fxcm:EUR/USD".

        Args:
            symbol: Инструмент.

        Returns:
            Имя провайдера или None.
        """
        for provider, symbols in self._config["markets"].items():
            if symbol in symbols:
                return provider
        return None

    async def ws_handler(self, ws):
        """Обслужить одно браузерное подключение.

        Протокол совпадает с server.py — фронт не меняем:
          set_tf → history, следом update с незакрытой свечой (тем же requestId);
          get_instruments → instruments.

        Args:
            ws: WebSocket-соединение (один аргумент — общий API websockets 11/16).

        Returns:
            None.
        """
        self._clients[ws] = {"provider": None, "symbol": None,
                             "tf": None, "requestId": 0}
        try:
            if self._instruments:
                await ws.send(json.dumps({"type": "instruments",
                                          "data": self._instruments}))

            async for raw in ws:
                data = json.loads(raw)
                mtype = data.get("type")

                if mtype == "set_tf":
                    await self._on_set_tf(ws, data)

                elif mtype == "get_instruments":
                    await ws.send(json.dumps({"type": "instruments",
                                              "data": self._instruments}))
        except websockets.ConnectionClosed:
            pass
        finally:
            self._clients.pop(ws, None)

    async def _on_set_tf(self, ws, data):
        """Отдать историю и живую свечу по запросу фронта.

        Args:
            ws:   Соединение клиента.
            data: Разобранное сообщение set_tf.

        Returns:
            None.
        """
        symbol     = data["symbol"]
        tf         = data["tf"]
        request_id = data.get("requestId", 0)

        provider = self._provider_of(symbol)
        if provider is None or tf not in self._tf_seconds:
            print("[hub] set_tf на неизвестный %s %s — игнор" % (symbol, tf))
            return

        self._clients[ws] = {"provider": provider, "symbol": symbol,
                             "tf": tf, "requestId": request_id}

        key     = (provider, symbol)
        history = (self._history.get(key) or {}).get(tf, [])

        await ws.send(json.dumps({
            "type":      "history",
            "symbol":    symbol,
            "tf":        tf,
            "requestId": request_id,
            "data":      history,
        }))

        # Живая свеча уходит СРАЗУ за историей, тем же requestId. Иначе она
        # появится только со следующим тиком — на M1 это до нескольких секунд
        # пустоты на графике (правка 854f652).
        builder = self._builders.get(key)
        current = builder.current(tf) if builder else None
        if current:
            await ws.send(json.dumps({
                "type":      "update",
                "symbol":    symbol,
                "tf":        tf,
                "requestId": request_id,
                "candle":    current,
            }))

    def _broadcast_update(self, provider, symbol):
        """Разослать живую свечу подписанным клиентам.

        Зовётся из on_bus_message, т.е. уже в потоке event loop — поэтому
        ensure_future, а не run_coroutine_threadsafe.

        Args:
            provider: Провайдер.
            symbol:   Инструмент.

        Returns:
            None.
        """
        builder = self._builders.get((provider, symbol))
        if builder is None:
            return

        for ws, info in list(self._clients.items()):
            if info["symbol"] != symbol or info["provider"] != provider:
                continue
            candle = builder.current(info["tf"])
            if not candle:
                continue
            self._send(ws, json.dumps({
                "type":      "update",
                "symbol":    symbol,
                "tf":        info["tf"],
                "requestId": info["requestId"],
                "candle":    candle,
            }))

    def _send(self, ws, payload):
        """Отправить клиенту готовый JSON, не роняя хаб на мёртвом сокете.

        Args:
            ws:      Соединение клиента.
            payload: Строка JSON.

        Returns:
            None.
        """
        try:
            asyncio.ensure_future(ws.send(payload))
        except Exception:
            self._clients.pop(ws, None)

    def _send_all(self, payload):
        """Разослать готовый JSON всем клиентам.

        Args:
            payload: Строка JSON.

        Returns:
            None.
        """
        for ws in list(self._clients):
            self._send(ws, payload)

    @property
    def stats(self):
        """Счётчики хаба.

        Args:
            None.

        Returns:
            Dict со счётчиками и размером очереди БД.
        """
        return {
            "ticks":     self.ticks_received,
            "ignored":   self.ticks_ignored,
            "closed":    self.closed_candles,
            "clients":   len(self._clients),
            "db_queue":  self._db_queue.qsize(),
            "db_dropped": self.db_dropped,
        }


async def _stats_loop(hub, every=60):
    """Печатать статистику хаба раз в минуту.

    Args:
        hub:   Экземпляр Hub.
        every: Период в секундах.

    Returns:
        None (бесконечный цикл).
    """
    while True:
        await asyncio.sleep(every)
        print("[hub] %s" % hub.stats)


async def main():
    """Поднять хаб: восстановление, писатель БД, шина, WS для браузера.

    Returns:
        None (работает вечно).
    """
    config = load_config()
    hub    = Hub(config)

    hub.restore()
    hub._loop = asyncio.get_event_loop()

    threading.Thread(target=hub.db_writer, daemon=True).start()

    bus = BusServer(hub.on_bus_message, config["bus_host"], config["bus_port"])
    await bus.start()

    await websockets.serve(hub.ws_handler, "0.0.0.0", config["ws_port"])
    print("[hub] WebSocket для браузера: 0.0.0.0:%d, база %s"
          % (config["ws_port"], config["db_path"]))

    asyncio.ensure_future(_stats_loop(hub))
    await asyncio.Future()


if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(main())
