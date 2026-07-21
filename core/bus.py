"""
Внутренняя шина: фиды → хаб (WebSocket на 127.0.0.1:8766).

Зачем: forexconnect требует Python 3.7, SDK Lighter — 3.8+. В одном процессе
они несовместимы, поэтому фиды живут отдельными процессами и толкают тики хабу
через локальный WebSocket. Бонус: падение одного фида не роняет другой.

    hub.py            ──> BusServer  (слушает 8766, режет свечи, пишет SQLite)
    feeds/fxcm_feed   ──> BusClient  (py3.7, тики от брокера)
    feeds/lighter_feed──> BusClient  (py3.10)

Контракт (ROADMAP, Фаза 2.4):

    {"type":"tick","provider":"lighter","symbol":"BTC",
     "ts":1783869420,"price":64132.6,"size":0.0123}

    {"type":"instruments","provider":"lighter","data":[
     {"symbol":"BTC","price_decimals":1,"size_decimals":5,
      "min_base":0.0002,"has_volume":true,"meta":{}}]}

FXCM шлёт то же с "size": null. ВРЕМЯ ВЕЗДЕ В СЕКУНДАХ — Lighter отдаёт
миллисекунды, конвертация на границе фида; validate() ловит мс как ошибку.

Совместимость: файл обязан работать и на py3.7 (websockets 11), и на py3.10
(websockets 16) — используется только общее подмножество API (websockets.serve /
websockets.connect, handler с одним аргументом, `async for` по сокету).
"""

import asyncio
import json
import logging
import threading
import time

import websockets

# Логгер шины. Настройку root-логгера делает процесс-хозяин (hub/feed) через
# core.logfmt.setup при импорте; здесь только берём именованный логгер.
log = logging.getLogger("bus")

BUS_HOST = "127.0.0.1"
BUS_PORT = 8766
BUS_URL  = "ws://127.0.0.1:8766"

# Потолок входящего сообщения. У websockets он по умолчанию 1 МБ, а пачка
# истории — тысячи баров: 10 000 минуток дают ~800 КБ JSON и упёрлись бы в него
# (хаб просто оборвал бы соединение). Фиды режут историю на куски, но запас
# нужен: пачки крипты с объёмами толще форексных.
MAX_MESSAGE_BYTES = 8 * 1024 * 1024

# Сетевые сбои, после которых клиент переподключается. CancelledError сюда
# попасть не должен: на py3.7 он наследуется от Exception (в 3.8+ — от
# BaseException), поэтому широкий `except Exception` проглотил бы отмену задачи.
_NET_ERRORS = (websockets.WebSocketException, OSError, asyncio.TimeoutError)


class BusError(Exception):
    """Сообщение не соответствует контракту шины."""


# ── конструкторы сообщений ─────────────────────────────────────────────

def make_tick(provider, symbol, ts, price, size=None, side=None):
    """Собрать сообщение 'tick'.

    Args:
        provider: Источник ("fxcm", "lighter").
        symbol:   Инструмент ("EUR/USD", "BTC").
        ts:       Время в unix-СЕКУНДАХ (int/float).
        price:    Цена (mid для форекса, last trade для крипты).
        size:     Объём сделки или None, если провайдер его не даёт (FXCM).
        side:     Сторона АГРЕССОРА ("buy"/"sell") или None. Нужна для дельты
                  свечи: OHLC не говорит, кто продавил цену внутри бара.
                  У FXCM стороны нет — там поток котировок, а не сделок.

    Returns:
        Dict сообщения шины.
    """
    return {
        "type":     "tick",
        "provider": provider,
        "symbol":   symbol,
        "ts":       ts,
        "price":    price,
        "size":     size,
        "side":     side,
    }


def make_candles(provider, symbol, tf, data):
    """Собрать сообщение 'candles' — историческая пачка баров от провайдера.

    Нужна потому, что хаб к брокеру не ходит: историю за время простоя может
    догрузить только фид (в монолите это делал load_history). Без неё после
    каждого рестарта в свечах оставалась бы дыра.

    Args:
        provider: Источник.
        symbol:   Инструмент.
        tf:       Таймфрейм ("M1", "H4", …).
        data:     Список закрытых свечей (time/open/high/low/close), старые первыми.

    Returns:
        Dict сообщения шины.
    """
    return {
        "type":     "candles",
        "provider": provider,
        "symbol":   symbol,
        "tf":       tf,
        "data":     data,
    }


def make_instruments(provider, data):
    """Собрать сообщение 'instruments' (метаданные инструментов).

    Args:
        provider: Источник.
        data:     Список dict'ов: symbol, price_decimals, size_decimals,
                  min_base, has_volume, meta.

    Returns:
        Dict сообщения шины.
    """
    return {
        "type":     "instruments",
        "provider": provider,
        "data":     data,
    }


def make_orderbook(provider, symbol, ts, bids, asks):
    """Собрать сообщение 'orderbook' (срез стакана лимитных заявок).

    Стакан принципиально отличается от тика и свечи: он НЕ хранится и не
    накапливается — это состояние «сейчас», которое устаревает за секунду.
    Хаб его не пишет в БД, а раздаёт подписанным браузерам и забывает.

    Уровни идут массивами пар [цена, объём], а не списком dict'ов: на
    ~700 уровней разница в размере JSON почти двукратная, а шлём мы это
    раз в секунду на каждую смотрящую вкладку.

    Args:
        provider: Источник (только биржи; у форекса стакана нет).
        symbol:   Инструмент.
        ts:       Время среза, unix-секунды.
        bids:     Заявки на покупку: [[price, size], …], цена по убыванию.
        asks:     Заявки на продажу: [[price, size], …], цена по возрастанию.

    Returns:
        Dict сообщения шины.
    """
    return {
        "type":     "orderbook",
        "provider": provider,
        "symbol":   symbol,
        "ts":       ts,
        "bids":     bids,
        "asks":     asks,
    }


def make_ticker(provider, data):
    """Собрать сообщение 'ticker' (сводка по инструментам).

    Mark/index price, суточные объём и изменение, открытый интерес, ставка
    фандинга — то, что рисуется в карточке инструмента. Как и стакан, НЕ
    хранится: это состояние «сейчас», обновляемое раз в несколько секунд.

    Args:
        provider: Источник (только биржи; у форекса этих данных нет).
        data:     Dict symbol → dict полей сводки.

    Returns:
        Dict сообщения шины.
    """
    return {
        "type":     "ticker",
        "provider": provider,
        "data":     data,
    }


def _is_number(value):
    """Число ли это (bool — не число).

    isinstance(True, int) == True, поэтому bool отсекается явно: иначе
    price=True прошёл бы валидацию как цена 1.0.

    Args:
        value: Проверяемое значение.

    Returns:
        True, если int/float и не bool.
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate(msg):
    """Проверить сообщение на соответствие контракту шины.

    Args:
        msg: Разобранный dict сообщения.

    Returns:
        Тот же msg, если всё в порядке.

    Raises:
        BusError: с описанием того, что именно нарушено.
    """
    if not isinstance(msg, dict):
        raise BusError("сообщение должно быть dict, получено %s" % type(msg).__name__)

    mtype = msg.get("type")
    if mtype not in ("tick", "candles", "instruments", "orderbook", "ticker"):
        raise BusError("неизвестный type: %r "
                       "(ожидается tick/candles/instruments/orderbook/ticker)"
                       % (mtype,))

    provider = msg.get("provider")
    if not isinstance(provider, str) or not provider.strip():
        raise BusError("пустой или нестроковый provider: %r" % (provider,))

    if mtype == "tick":
        symbol = msg.get("symbol")
        if not isinstance(symbol, str) or not symbol.strip():
            raise BusError("tick: пустой symbol: %r" % (symbol,))

        ts = msg.get("ts")
        if not _is_number(ts):
            raise BusError("tick[%s]: ts не число: %r" % (symbol, ts))
        if ts <= 0:
            raise BusError("tick[%s]: ts должен быть > 0, получено %r" % (symbol, ts))
        if ts > 1e11:
            # 1e11 сек ≈ год 5138 — столько не бывает, значит это миллисекунды.
            raise BusError(
                "tick[%s]: ts=%r похож на МИЛЛИСЕКУНДЫ; конвертируй на границе "
                "фида (ts // 1000) — внутри шины время только в секундах" % (symbol, ts)
            )

        price = msg.get("price")
        if not _is_number(price):
            raise BusError("tick[%s]: price не число: %r" % (symbol, price))
        if price <= 0:
            raise BusError("tick[%s]: price должен быть > 0, получено %r" % (symbol, price))

        size = msg.get("size")
        if size is not None:
            if not _is_number(size):
                raise BusError("tick[%s]: size не число и не None: %r" % (symbol, size))
            if size < 0:
                raise BusError("tick[%s]: size должен быть >= 0, получено %r" % (symbol, size))

        # Сторона агрессора проверяется строго: опечатка ("BUY", "b", 1) не
        # уронила бы нарезку, а молча перекосила дельту — такой баг ловится
        # уже по кривому индикатору, а не по логу.
        side = msg.get("side")
        if side is not None and side not in ("buy", "sell"):
            raise BusError("tick[%s]: side должен быть 'buy'/'sell'/None, получено %r"
                           % (symbol, side))

    elif mtype == "candles":
        symbol = msg.get("symbol")
        if not isinstance(symbol, str) or not symbol.strip():
            raise BusError("candles: пустой symbol: %r" % (symbol,))

        tf = msg.get("tf")
        if not isinstance(tf, str) or not tf.strip():
            raise BusError("candles[%s]: пустой tf: %r" % (symbol, tf))

        data = msg.get("data")
        if not isinstance(data, list):
            raise BusError("candles[%s %s]: data должен быть списком, получено %s"
                           % (symbol, tf, type(data).__name__))

        for i, bar in enumerate(data):
            if not isinstance(bar, dict):
                raise BusError("candles[%s %s]: data[%d] не dict" % (symbol, tf, i))
            for field in ("time", "open", "high", "low", "close"):
                if not _is_number(bar.get(field)):
                    raise BusError("candles[%s %s]: data[%d] без числового %s: %r"
                                   % (symbol, tf, i, field, bar.get(field)))
            if bar["time"] > 1e11:
                raise BusError(
                    "candles[%s %s]: data[%d] time=%r похож на МИЛЛИСЕКУНДЫ; "
                    "внутри шины время только в секундах"
                    % (symbol, tf, i, bar["time"]))

    elif mtype == "orderbook":
        symbol = msg.get("symbol")
        if not isinstance(symbol, str) or not symbol.strip():
            raise BusError("orderbook: пустой symbol: %r" % (symbol,))

        ts = msg.get("ts")
        if not _is_number(ts):
            raise BusError("orderbook[%s]: ts не число: %r" % (symbol, ts))
        if ts > 1e11:
            raise BusError(
                "orderbook[%s]: ts=%r похож на МИЛЛИСЕКУНДЫ; конвертируй на "
                "границе фида — внутри шины время только в секундах"
                % (symbol, ts))

        for side_name in ("bids", "asks"):
            levels = msg.get(side_name)
            if not isinstance(levels, list):
                raise BusError("orderbook[%s]: %s должен быть списком, получено %s"
                               % (symbol, side_name, type(levels).__name__))
            for i, level in enumerate(levels):
                if not isinstance(level, (list, tuple)) or len(level) != 2:
                    raise BusError(
                        "orderbook[%s]: %s[%d] должен быть парой [цена, объём], "
                        "получено %r" % (symbol, side_name, i, level))
                if not _is_number(level[0]) or not _is_number(level[1]):
                    raise BusError("orderbook[%s]: %s[%d] нечисловая пара: %r"
                                   % (symbol, side_name, i, level))
                if level[0] <= 0:
                    raise BusError("orderbook[%s]: %s[%d] цена должна быть > 0: %r"
                                   % (symbol, side_name, i, level[0]))
                if level[1] <= 0:
                    # Нулевой размер у Lighter означает СНЯТИЕ уровня. Такие
                    # записи применяются к книге в фиде и наружу выходить не
                    # должны: в срезе они выглядели бы как пустые полосы.
                    raise BusError(
                        "orderbook[%s]: %s[%d] объём должен быть > 0 (нулевой "
                        "уровень = снятие, применяется в фиде): %r"
                        % (symbol, side_name, i, level[1]))

        # Стороны не должны пересекаться: лучший bid ниже лучшего ask, иначе
        # заявки исполнились бы друг о друга. Пересечение = перепутаны местами.
        if msg["bids"] and msg["asks"]:
            best_bid = msg["bids"][0][0]
            best_ask = msg["asks"][0][0]
            if best_bid >= best_ask:
                raise BusError(
                    "orderbook[%s]: лучший bid %r >= лучшего ask %r — стороны "
                    "перепутаны или книга не отсортирована"
                    % (symbol, best_bid, best_ask))

    elif mtype == "ticker":
        data = msg.get("data")
        if not isinstance(data, dict):
            raise BusError("ticker: data должен быть dict symbol→поля, "
                           "получено %s" % type(data).__name__)
        for sym, fields in data.items():
            if not isinstance(sym, str) or not sym.strip():
                raise BusError("ticker: пустой symbol в ключе: %r" % (sym,))
            if not isinstance(fields, dict):
                raise BusError("ticker[%s]: поля должны быть dict, получено %s"
                               % (sym, type(fields).__name__))

    else:
        data = msg.get("data")
        if not isinstance(data, list):
            raise BusError("instruments: data должен быть списком, получено %s"
                           % type(data).__name__)
        for i, item in enumerate(data):
            if not isinstance(item, dict):
                raise BusError("instruments: data[%d] не dict: %s" % (i, type(item).__name__))
            sym = item.get("symbol")
            if not isinstance(sym, str) or not sym.strip():
                raise BusError("instruments: data[%d] без symbol: %r" % (i, sym))

    return msg


# ── сторона хаба ───────────────────────────────────────────────────────

class BusServer:
    """Серверная сторона шины — живёт в хабе, слушает 127.0.0.1:8766.

    Принимает подключения фидов, валидирует входящие сообщения и отдаёт их
    синхронному колбэку. Битое сообщение НЕ роняет сервер и не рвёт соединение:
    один кривой тик не должен стоить нам всего потока данных.
    """

    def __init__(self, on_message, host=BUS_HOST, port=BUS_PORT):
        """Создать сервер шины.

        Args:
            on_message: callable(dict) — синхронный обработчик валидного
                        сообщения, вызывается в потоке event loop.
            host:       Адрес прослушивания.
            port:       Порт прослушивания.

        Returns:
            None.
        """
        self._on_message = on_message
        self._host       = host
        self._port       = port
        self._server     = None
        self._received   = 0
        self._dropped    = 0
        self._clients    = 0
        # Сокет каждого подключённого фида по имени провайдера — для КОМАНД
        # хаб→фид (подписка на стакан). Основной поток остаётся односторонним:
        # команды это редкие управляющие сообщения, а не данные.
        self._feeds      = {}

    async def command(self, provider, payload):
        """Послать фиду управляющую команду (например, подписку на стакан).

        Тихо ничего не делает, если фид этого провайдера не подключён:
        стакан — не критичные данные, а фид может быть в перезапуске.

        Args:
            provider: Имя провайдера ("lighter").
            payload:  Dict команды, уходит как есть.

        Returns:
            True, если команда отправлена; False, если фида нет.
        """
        ws = self._feeds.get(provider)
        if ws is None:
            return False
        try:
            await ws.send(json.dumps(payload))
            return True
        except Exception as err:
            log.warning("команда фиду %s не ушла: %s", provider, err)
            return False

    async def _handler(self, ws):
        """Обслужить одно подключение фида.

        Args:
            ws: WebSocket-соединение (один аргумент — общий для websockets 11/16).

        Returns:
            None.
        """
        self._clients += 1
        provider = None
        try:
            async for raw in ws:
                try:
                    msg = validate(json.loads(raw))
                except (ValueError, BusError) as err:
                    # ValueError покрывает json.JSONDecodeError.
                    self._dropped += 1
                    log.warning("отброшено: %s", err)
                    continue

                # Провайдер узнаётся из первого же валидного сообщения:
                # отдельного рукопожатия в контракте нет и заводить его ради
                # команд не нужно.
                if provider is None:
                    provider = msg["provider"]
                    self._feeds[provider] = ws

                self._received += 1
                try:
                    self._on_message(msg)
                except Exception as err:
                    # Ошибка ПОТРЕБИТЕЛЯ (хаба), не шины: логируем и живём дальше.
                    self._dropped += 1
                    log.error("on_message упал: %r", err)
        except websockets.ConnectionClosed:
            log.info("фид отключился")
        finally:
            self._clients -= 1
            # Снять из реестра ТОЛЬКО свой сокет: при переподключении фида
            # новый _handler уже мог записать себя, и слепой pop убил бы
            # живую запись, оставив хаб без канала команд до следующего
            # обрыва.
            if provider is not None and self._feeds.get(provider) is ws:
                del self._feeds[provider]

    async def start(self):
        """Забиндить порт и вернуть управление (сервер уже принимает фиды).

        Отдельно от serve(), потому что вызывающему нужно ЗНАТЬ момент, когда
        порт занят: хаб рядом поднимает второй WS-сервер (для браузера), а фид,
        стартовавший раньше хаба, иначе получит ConnectionRefused и уйдёт в
        backoff на секунду.

        Args:
            None.

        Returns:
            None.
        """
        self._server = await websockets.serve(self._handler, self._host, self._port,
                                              max_size=MAX_MESSAGE_BYTES)
        log.info("слушаю %s:%d", self._host, self._port)

    async def serve(self):
        """Поднять сервер и работать вечно.

        Args:
            None.

        Returns:
            None (не возвращается штатно; завершается отменой задачи).
        """
        await self.start()
        await asyncio.Future()

    async def close(self):
        """Закрыть слушающий сокет (для тестов и штатной остановки).

        Args:
            None.

        Returns:
            None.
        """
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    @property
    def stats(self):
        """Счётчики сервера.

        Args:
            None.

        Returns:
            Dict: received (принято валидных), dropped (отброшено),
            clients (подключённых фидов сейчас).
        """
        return {
            "received": self._received,
            "dropped":  self._dropped,
            "clients":  self._clients,
        }


# ── сторона фида ───────────────────────────────────────────────────────

class BusClient:
    """Клиентская сторона шины — живёт в фиде, шлёт тики хабу.

    Потокобезопасен по отправке: тики FXCM приходят в ДЕМОН-ПОТОКЕ (колбэк
    брокера), а не в asyncio, поэтому send_threadsafe() можно звать из любого
    потока.

    Очередь ОГРАНИЧЕНА и при переполнении выбрасывает САМОЕ СТАРОЕ сообщение:
    рыночные данные протухают, и копить их во время обрыва бессмысленно —
    лучше потерять старые тики, чем съесть память.
    """

    def __init__(self, provider, url=BUS_URL, max_queue=10000, on_command=None):
        """Создать клиента шины.

        Args:
            provider:   Имя фида ("fxcm", "lighter").
            url:        Адрес хаба.
            max_queue:  Потолок очереди на время обрыва связи.
            on_command: callable(dict) — обработчик КОМАНД хаба (подписка на
                        стакан). Может быть корутиной. None (по умолчанию)
                        оставляет соединение односторонним, как у фида FXCM.

        Returns:
            None.
        """
        self._provider   = provider
        self._url        = url
        self._max_queue  = max_queue

        # Очередь и loop создаются ЛЕНИВО, уже внутри run(). На py3.7
        # asyncio.Queue() запоминает event loop в момент СОЗДАНИЯ, а asyncio.run()
        # заводит новый — и первый же await get() на пустой очереди (штатное
        # состояние между тиками) падает с "got Future attached to a different
        # loop". На py3.10 этого не происходит, т.е. баг был бы невидим в тестах
        # на 3.10 и выстрелил бы только в проде на фиде FXCM.
        self._queue = None
        self._loop  = None

        # Сообщение, снятое с очереди, но ещё не подтверждённое отправкой.
        # Без него тик, пришедший в момент обрыва, терялся бы навсегда: _pump
        # висит на queue.get(), об оборванном сокете не знает, и первый же
        # ws.send() падает уже ПОСЛЕ того, как сообщение снято с очереди.
        self._pending = None

        self._sent       = 0
        self._dropped    = 0
        self._reconnects = 0
        self._connected  = False
        self._on_command = on_command

    async def _listen(self, ws):
        """Читать команды хаба и отдавать их обработчику фида.

        Обратный канал нужен для подписок, которыми управляет хаб: на стакан
        подписываются только когда его кто-то смотрит, и знает об этом хаб,
        а ходит к бирже фид. Поток данных при этом остаётся односторонним.

        Битая команда НЕ рвёт соединение: потерять из-за неё поток тиков было
        бы несоразмерно.

        Args:
            ws: Открытое WebSocket-соединение.

        Returns:
            None. Выходит через ConnectionClosed при обрыве.
        """
        async for raw in ws:
            try:
                cmd = json.loads(raw)
            except ValueError as err:
                log.warning("[%s] не-JSON команда: %s", self._provider, err)
                continue
            try:
                result = self._on_command(cmd)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as err:
                log.error("[%s] обработчик команды упал: %r", self._provider, err)

    # -- отправка ------------------------------------------------------

    def send_threadsafe(self, msg):
        """Поставить сообщение в очередь отправки. Можно звать из любого потока.

        Невалидное сообщение отбрасывается (счётчик dropped), исключение наружу
        не летит: колбэк брокера не должен падать из-за одного кривого тика.
        Если run() ещё не стартовал — сообщение тоже отбрасывается.

        Args:
            msg: Dict сообщения (результат make_tick / make_instruments).

        Returns:
            True, если сообщение принято в очередь; иначе False.
        """
        try:
            validate(msg)
        except BusError as err:
            self._dropped += 1
            log.warning("[%s] не отправлено: %s", self._provider, err)
            return False

        loop = self._loop
        if loop is None or self._queue is None:
            self._dropped += 1
            return False

        loop.call_soon_threadsafe(self._enqueue, msg)
        return True

    def _enqueue(self, msg):
        """Положить сообщение в очередь, вытеснив старейшее при переполнении.

        Исполняется строго в потоке event loop (через call_soon_threadsafe),
        поэтому get_nowait/put_nowait здесь безопасны.

        Args:
            msg: Dict сообщения.

        Returns:
            None.
        """
        while self._queue.full():
            try:
                self._queue.get_nowait()
                self._dropped += 1
            except asyncio.QueueEmpty:
                break
        try:
            self._queue.put_nowait(msg)
        except asyncio.QueueFull:
            self._dropped += 1

    # -- цикл соединения -----------------------------------------------

    async def run(self):
        """Вечный цикл: подключиться к хабу, слать очередь, переподключаться.

        Backoff растёт 1→2→4→…→30 с и сбрасывается после успешного коннекта.

        Args:
            None.

        Returns:
            None (не возвращается штатно; завершается отменой задачи).
        """
        self._loop  = asyncio.get_event_loop()
        self._queue = asyncio.Queue(maxsize=self._max_queue)

        backoff = 1
        while True:
            try:
                async with websockets.connect(self._url) as ws:
                    self._connected = True
                    backoff = 1
                    log.info("[%s] подключён к %s", self._provider, self._url)
                    if self._on_command is None:
                        await self._pump(ws)
                    else:
                        # Отправка и приём команд — параллельно: _pump вечный,
                        # и последовательно читать команды было бы негде.
                        # Падение любой из задач валит обе, дальше обычное
                        # переподключение с backoff.
                        pump = asyncio.ensure_future(self._pump(ws))
                        recv = asyncio.ensure_future(self._listen(ws))
                        try:
                            done, pending = await asyncio.wait(
                                [pump, recv],
                                return_when=asyncio.FIRST_EXCEPTION)
                            for task in pending:
                                task.cancel()
                            for task in done:
                                task.result()   # пробросить исключение наружу
                        finally:
                            pump.cancel()
                            recv.cancel()
            except _NET_ERRORS as err:
                log.warning("[%s] обрыв (%s), переподключение через %d с",
                            self._provider, type(err).__name__, backoff)
            finally:
                if self._connected:
                    self._reconnects += 1
                self._connected = False

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    async def _pump(self, ws):
        """Гнать сообщения из очереди в сокет, пока соединение живо.

        Сообщение считается отправленным только после успешного ws.send().
        Если send упал — оно остаётся в _pending и уйдёт первым же делом после
        переподключения, не ломая порядок тиков.

        Args:
            ws: Открытое WebSocket-соединение.

        Returns:
            None. Выходит через исключение ConnectionClosed при обрыве.
        """
        while True:
            if self._pending is None:
                self._pending = await self._queue.get()

            await ws.send(json.dumps(self._pending))
            self._sent += 1
            self._pending = None

    @property
    def stats(self):
        """Счётчики клиента.

        Args:
            None.

        Returns:
            Dict: sent, dropped, reconnects, connected, queued.
        """
        return {
            "sent":       self._sent,
            "dropped":    self._dropped,
            "reconnects": self._reconnects,
            "connected":  self._connected,
            "queued":     self._queue.qsize() if self._queue is not None else 0,
        }


# ── самопроверка: python3.7 core/bus.py / python3.10 core/bus.py ───────

if __name__ == "__main__":
    received = []

    async def _demo():
        """Поднять шину, послать тики из отдельного потока, показать stats."""
        server = BusServer(received.append)
        client = BusClient("demo", max_queue=5)

        await server.start()  # порт занят ДО старта клиента — без гонки
        client_task = asyncio.ensure_future(client.run())
        await asyncio.sleep(0.3)

        def _feed():
            """Демон-поток: так же, как колбэк FXCM, шлёт тики не из asyncio."""
            client.send_threadsafe(make_tick("demo", "EUR/USD", time.time(), 1.1735))
            client.send_threadsafe(make_tick("demo", "BTC", time.time(), 64132.6, 0.0123))
            client.send_threadsafe(make_instruments("demo", [{"symbol": "BTC"}]))
            client.send_threadsafe(make_tick("demo", "BTC", time.time() * 1000, 1.0))  # мс → дроп

        threading.Thread(target=_feed, daemon=True).start()
        await asyncio.sleep(0.5)

        print("server:", server.stats)
        print("client:", client.stats)
        print("принято:", len(received), "сообщений")
        assert len(received) == 3, "ожидалось 3 валидных сообщения, а не %d" % len(received)
        assert client.stats["sent"] == 3, "клиент должен был отправить 3"
        assert client.stats["dropped"] == 1, "тик в миллисекундах должен быть отброшен"
        assert server.stats["clients"] == 1, "фид должен быть подключён"
        print("OK")

        client_task.cancel()
        await asyncio.sleep(0)
        await server.close()

    asyncio.get_event_loop().run_until_complete(_demo())
