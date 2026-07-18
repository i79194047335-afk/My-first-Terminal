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
from core.candles import CandleBuilder, aggregate_higher_tf
from core.db import init_db, load_history, load_instruments, trim_window, \
    upsert_candle, upsert_candles_batch, upsert_instrument
from core.logfmt import setup as _log_setup
from core.market_hours import forex_open as market_open
from core.range_bars import RangeBarBuilder, backfill_tail

log = _log_setup("hub")

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

        # Ценовые алерты. Ключ — symbol (как шлёт фронт, без провайдера); фронт
        # различает алерты только по symbol+id. last_price нужен для проверки
        # ПЕРЕСЕЧЕНИЯ уровня (не «цена выше»): алерты живут в памяти, как в
        # server.py — при рестарте их заново создаёт фронт из localStorage.
        self._alerts      = {}   # symbol -> [{"id","price","triggered"}]
        self._alert_id    = 1
        self._last_price  = {}   # symbol -> последняя цена (для пересечения)

        # Брифинг: pre_session_brief.py (крон 3×/сутки) пишет briefing.json, хаб
        # поллит его mtime и рассылает клиентам. Путь абсолютный в конфиге —
        # файл живёт в боевом дереве, а хаб запущен из worktree.
        self._briefing        = None
        self._briefing_mtime  = 0
        self._briefing_lock   = threading.Lock()

        # Рэндж-бары: билдеры создаются ЛЕНИВО по set_tf "R:<поинты>" и живут
        # только в памяти — источник истины у них один, тиковый архив, и после
        # рестарта история честно перестраивается из него же (персист в candles
        # намеренно НЕ делаем: произвольные R замусорили бы таблицу, а шов
        # «бары из БД + недостроенный из тиков» точно не восстановить —
        # у рэндж-бара нет вычислимого по часам бакета).
        self._data_dir       = config.get("data_dir", "data")
        self._range_builders = {}   # (provider, symbol, pips) -> RangeBarBuilder
        self._range_pending  = {}   # (provider, symbol, pips) -> {"buffer": [...]}

        self._db_queue = queue.Queue(maxsize=5000)

        self.ticks_received = 0
        self.ticks_ignored  = 0
        self.closed_candles = 0
        self.db_dropped     = 0

        # Для health-эндпоинта: когда стартовали и когда пришёл последний тик.
        self._started_ts   = time.time()
        self._last_tick_ts = 0.0
        self._last_tick_by_symbol = {}   # symbol -> ts последнего РЕАЛЬНОГО тика

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
            # Инструменты — из БД: фид шлёт их один раз при логине, и рестарт
            # хаба иначе терял бы их до следующего рестарта фида (селектор и
            # precision на фронте остались бы на HTML-fallback).
            saved = load_instruments(self._conn, provider)
            if saved:
                self._instruments[provider] = saved
                log.info("[restore] %s — инструменты: %s",
                     provider, ", ".join(i["symbol"] for i in saved))

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
                log.info("[restore] %s:%s — %d баров, смещения %s",
                     provider, symbol, total, offsets or "{}")

    def _restore_forming(self, provider, symbol, now_ts, quiet=False):
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

        if not quiet:
            log.info("[restore] %s:%s — живая свеча восстановлена на %d ТФ",
                 provider, symbol, seeded)

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
            elif msg["type"] == "candles":
                self._handle_candles(msg)
            elif msg["type"] == "instruments":
                self._handle_instruments(msg)
        except Exception as err:
            log.error("ошибка на сообщении шины: %r", err)

    def _handle_candles(self, msg):
        """Влить историческую пачку баров от провайдера.

        Хаб к брокеру не ходит, поэтому дыру за время простоя закрывает фид: он
        грузит историю и шлёт её сюда (в монолите это делал load_history).

        Три места, где легко испортить данные, и все три обработаны:
          1. Последний бар пачки может быть НЕЗАКРЫТЫМ (H4/D1 брокер отдаёт баром
             текущего бакета). Записать его как закрытый — получить фальшивый
             спайк, поэтому бары текущего бакета отбрасываются: живую свечу
             соберёт _restore_forming.
          2. Смещение сетки берётся из брокерских ТФ (H1/H4/D1) — только они
             несут его сетку. Это единственный источник смещения на пустой БД.
          3. M3/M5/M15 брокер не отдаёт — они выводятся из M1, как в монолите
             (build_higher_history).

        Args:
            msg: Сообщение шины типа "candles".

        Returns:
            None.
        """
        provider = msg["provider"]
        symbol   = msg["symbol"]
        tf       = msg["tf"]
        key      = (provider, symbol)

        builder = self._builders.get(key)
        if builder is None or tf not in self._tf_seconds:
            return

        data = msg["data"]
        if not data:
            return

        # (2) Смещение сетки — до расчёта бакета, иначе отсечём не то.
        if tf in self._broker_tf:
            builder.set_offsets(
                CandleBuilder.detect_offsets({tf: data}, self._tf_seconds))

        # (1) Бары текущего бакета — не закрытые, в историю им нельзя.
        sec        = self._tf_seconds[tf]
        offset     = builder.offsets.get(tf, 0)
        now        = int(time.time())
        cur_bucket = ((now - offset) // sec) * sec + offset
        bars       = [c for c in data if c["time"] < cur_bucket]
        if not bars:
            return

        touched = [tf]
        self._merge_history(key, tf, bars)

        # (3) Производные ТФ из M1: брокер их не отдаёт.
        if tf == "M1":
            m1 = self._history[key]["M1"]
            for other, other_sec in self._tf_seconds.items():
                if other_sec <= 60 or other in self._broker_tf:
                    continue
                derived = aggregate_higher_tf(m1, other_sec)
                derived = [c for c in derived if c["time"] < cur_bucket]
                if derived:
                    self._merge_history(key, other, derived)
                    touched.append(other)

        # История поменялась — билдер должен увидеть новую и пересобрать живую
        # свечу от неё, иначе он останется со старой и откроет бар от прежнего
        # close.
        for name in touched:
            builder.seed_history(name, self._history[key][name])
        self._restore_forming(provider, symbol, now, quiet=True)

        total = sum(len(self._history[key][t]) for t in touched)
        log.info("%s:%s догружено %s → %d баров",
             provider, symbol, ", ".join(touched), total)

    def _merge_history(self, key, tf, bars):
        """Влить бары в историю: upsert по времени, порядок и окно сохраняются.

        Args:
            key:  (provider, symbol).
            tf:   Таймфрейм.
            bars: Список закрытых свечей.

        Returns:
            None.
        """
        merged = {c["time"]: c for c in self._history[key].get(tf, [])}
        for c in bars:
            merged[c["time"]] = c

        out = [merged[t] for t in sorted(merged)]
        if len(out) > self._keep_bars:
            out = out[-self._keep_bars:]
        self._history[key][tf] = out

        # В БД — той же коннекцией главного потока: пачка приходит раз в старт,
        # через очередь db_writer (maxsize=5000) 10 000 минуток не пролезли бы.
        upsert_candles_batch(self._conn, key[0], key[1], tf, out)
        trim_window(self._conn, key[0], key[1], tf, self._keep_bars)

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
        now = time.time()
        self._last_tick_ts = now
        # Свежесть по символу — для индикатора «рынок открыт» на фронте. Только
        # реальные тики из шины; init-update при подписке сюда НЕ попадает.
        self._last_tick_by_symbol[symbol] = now
        price = msg["price"]

        # Алерты — до нарезки: нужен prev_price для проверки пересечения уровня.
        self._check_alerts(symbol, self._last_price.get(symbol, price), price)
        self._last_price[symbol] = price

        closed = builder.ingest(price, msg["ts"])

        for tf, candle in closed.items():
            self.closed_candles += 1
            bars = self._history[key].setdefault(tf, [])
            bars.append(candle)
            # Держим в памяти ровно окно ретеншена: в server.py этот список рос
            # без границ.
            if len(bars) > self._keep_bars:
                del bars[:len(bars) - self._keep_bars]
            self._enqueue_closed(provider, symbol, tf, candle)

        self._range_ingest(provider, symbol, price, msg["ts"])
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

        self._broadcast(json.dumps({"type": "instruments",
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
            log.warning("очередь БД полна — свеча %s %s %s потеряна", provider, symbol, tf)

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
                log.warning("db_writer: %r — свеча пропущена", err)

    # ── брифинг ─────────────────────────────────────────────────────────

    def briefing_watcher(self):
        """Поток-демон: поллить briefing.json и рассылать при обновлении.

        Файл пишет крон (pre_session_brief.py) 3×/сутки. Поллинг по mtime раз в
        5 с — как в server.py. Рассылка идёт из потока-демона, поэтому через
        loop.call_soon_threadsafe (в отличие от алертов, которые уже в потоке
        event loop).

        Args:
            None.

        Returns:
            None (бесконечный цикл). Тихо простаивает, если файла нет.
        """
        path = self._config.get("briefing_file")
        if not path:
            return

        while True:
            try:
                if os.path.exists(path):
                    mtime = os.path.getmtime(path)
                    if mtime > self._briefing_mtime:
                        with open(path, encoding="utf-8") as f:
                            data = json.load(f)
                        with self._briefing_lock:
                            self._briefing       = data
                            self._briefing_mtime = mtime
                        session = data.get("meta", {}).get("session", "?")
                        log.info("брифинг обновлён (session=%s)", session)
                        self._broadcast_threadsafe(json.dumps(
                            {"type": "briefing", "data": data}))
            except Exception as err:
                log.error("briefing_watcher: %r", err)
            time.sleep(5)

    def _broadcast_threadsafe(self, payload):
        """Разослать JSON всем клиентам ИЗ ЛЮБОГО потока.

        _broadcast трогает клиентские сокеты и должен исполняться в потоке event
        loop; watcher живёт в своём потоке, поэтому перекладываем туда.

        Args:
            payload: Строка JSON.

        Returns:
            None.
        """
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(self._broadcast, payload)

    # ── рэндж-бары ──────────────────────────────────────────────────────

    @staticmethod
    def parse_range_tf(tf):
        """Распознать рэндж-ТФ вида "R:<поинты>".

        Args:
            tf: Строка ТФ из set_tf ("M1", "R:5", "R:2.5", …).

        Returns:
            Диапазон в поинтах (float) или None, если это не рэндж-ТФ либо
            значение вне здравого смысла (<= 0 или > 10000).
        """
        if not isinstance(tf, str) or not tf.startswith("R:"):
            return None
        try:
            pips = float(tf[2:])
        except ValueError:
            return None
        if pips <= 0 or pips > 10000:
            return None
        return pips

    def _point_size(self, provider, symbol):
        """Размер ОДНОГО ПОИНТА (пипетты) инструмента в единицах цены.

        Поинт — последний знак котировки, минимальный тик: 10^-price_decimals.
        EUR/USD (5 знаков) → 0.00001, USD/JPY (3 знака) → 0.001. Единица
        рэндж-баров задаётся в поинтах, как в TradingView («10R» = 10 поинтов),
        а не в пипсах — чтобы наши значения совпадали с общепринятыми.

        Args:
            provider: Провайдер.
            symbol:   Инструмент.

        Returns:
            Float; 0.00001, если инструмент в _instruments не найден
            (форекс-мажоры с 5 знаками — подавляющий случай).
        """
        for item in self._instruments.get(provider, []):
            if item.get("symbol") == symbol and item.get("price_decimals"):
                return 10.0 ** -item["price_decimals"]
        return 0.00001

    def _range_ingest(self, provider, symbol, price, ts):
        """Прогнать живой тик через рэндж-контур этой пары.

        Пока идёт бэкфил, тики копятся в буфере ожидания — иначе окно бэкфила
        (секунды чтения архива) стало бы дырой между историей и живым потоком.

        Args:
            provider: Провайдер.
            symbol:   Инструмент.
            price:    Цена тика.
            ts:       Unix-время тика (float).

        Returns:
            None.
        """
        for key, entry in self._range_pending.items():
            if key[0] == provider and key[1] == symbol:
                entry["buffer"].append((price, ts))
        for key, rb in self._range_builders.items():
            if key[0] == provider and key[1] == symbol:
                closed = rb.ingest(price, ts)
                # У рэндж-бара тик-пробойщик МУТИРУЕТ закрывающийся бар и сразу
                # открывает новый — финальное состояние закрытого без этой
                # отправки не ушло бы клиенту никогда (broadcast шлёт current).
                if closed is not None:
                    self._broadcast_range_closed(provider, symbol, key[2],
                                                 closed)

    def _broadcast_range_closed(self, provider, symbol, pips, closed):
        """Разослать финальное состояние закрытого рэндж-бара его подписчикам.

        Уходит ПЕРЕД update с новым current того же тика (ensure_future
        сохраняет порядок постановки, websockets — порядок отправки).

        Args:
            provider: Провайдер.
            symbol:   Инструмент.
            pips:     Диапазон подписки.
            closed:   Закрытый бар.

        Returns:
            None.
        """
        for ws, info in list(self._clients.items()):
            if (info["provider"] == provider and info["symbol"] == symbol
                    and self.parse_range_tf(info["tf"]) == pips):
                self._send(ws, json.dumps({
                    "type":      "update",
                    "symbol":    symbol,
                    "tf":        info["tf"],
                    "requestId": info["requestId"],
                    "candle":    closed,
                }))

    async def _on_set_range_tf(self, ws, provider, symbol, tf, pips,
                               request_id):
        """Подписать клиента на рэндж-ТФ; при первом запросе — бэкфил в фоне.

        Готовый билдер → история сразу; бэкфил уже идёт → клиент просто ждёт
        (история уедет всем подписчикам из _finish_range_backfill); иначе —
        запускаем поток бэкфила, event loop не блокируется.

        Args:
            ws:         Соединение клиента.
            provider:   Провайдер.
            symbol:     Инструмент.
            tf:         Строка ТФ как прислал фронт ("R:5").
            pips:       Разобранный диапазон в поинтах.
            request_id: requestId клиента.

        Returns:
            None.
        """
        self._clients[ws] = {"provider": provider, "symbol": symbol,
                             "tf": tf, "requestId": request_id}
        key = (provider, symbol, pips)

        rb = self._range_builders.get(key)
        if rb is not None:
            await self._send_range_history(ws, symbol, tf, request_id, rb)
            return

        if key in self._range_pending:
            return

        self._range_pending[key] = {"buffer": []}
        threading.Thread(target=self._range_backfill_worker,
                         args=(key,), daemon=True).start()
        log.info("рэндж %s:%s R=%s — бэкфил из тикового архива…",
                 provider, symbol, pips)

    def _range_backfill_worker(self, key):
        """Поток: построить историю рэндж-баров из тикового архива.

        Args:
            key: (provider, symbol, pips).

        Returns:
            None. Результат уходит в _finish_range_backfill через
            call_soon_threadsafe (билдер трогает event loop — только оттуда).
        """
        provider, symbol, points = key
        rsize   = points * self._point_size(provider, symbol)
        builder = RangeBarBuilder(rsize, max_bars=self._keep_bars)
        last_ts = None
        try:
            # backfill_tail читает только нужный хвост архива, а не все месяцы
            # тиков (R:10 — доли секунды вместо ~11 с полного прогона).
            builder, last_ts = backfill_tail(self._data_dir, symbol, rsize,
                                             max_bars=self._keep_bars)
        except Exception as err:
            # Архива может не быть (свежий инстанс) — отдаём что успели.
            log.error("бэкфил рэндж %s:%s R=%s: %r", provider, symbol, points, err)

        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(self._finish_range_backfill,
                                      key, builder, last_ts)

    def _finish_range_backfill(self, key, builder, last_ts):
        """Влить буфер живых тиков, активировать билдер, раздать историю.

        Исполняется в потоке event loop. CSV пишется тем же потоком данных с
        лагом меньше секунды, поэтому хвост буфера может дублировать хвост
        архива — дубли отсекаются по ts (у фида ts тика в CSV и в шине один).

        Args:
            key:     (provider, symbol, pips).
            builder: RangeBarBuilder с историей из архива.
            last_ts: ts последнего тика, прочитанного из архива (или None).

        Returns:
            None.
        """
        provider, symbol, pips = key
        entry = self._range_pending.pop(key, {"buffer": []})
        for price, ts in entry["buffer"]:
            if last_ts is None or ts > last_ts:
                builder.ingest(price, ts)
        self._range_builders[key] = builder

        log.info("рэндж %s:%s R=%s готов: %d баров (буфер %d тиков)",
                 provider, symbol, pips, len(builder.history()),
                 len(entry["buffer"]))

        for ws, info in list(self._clients.items()):
            if (info["provider"] == provider and info["symbol"] == symbol
                    and self.parse_range_tf(info["tf"]) == pips):
                asyncio.ensure_future(self._send_range_history(
                    ws, symbol, info["tf"], info["requestId"], builder))

    async def _send_range_history(self, ws, symbol, tf, request_id, builder):
        """Отдать клиенту историю рэндж-баров и живой бар (контракт set_tf).

        Args:
            ws:         Соединение клиента.
            symbol:     Инструмент.
            tf:         Строка ТФ клиента.
            request_id: requestId клиента.
            builder:    Активный RangeBarBuilder.

        Returns:
            None. Мёртвый сокет молча выбрасывается из клиентов.
        """
        try:
            await ws.send(json.dumps({
                "type":      "history",
                "symbol":    symbol,
                "tf":        tf,
                "requestId": request_id,
                "data":      builder.history(),
            }))
            current = builder.current()
            if current:
                await ws.send(json.dumps({
                    "type":      "update",
                    "symbol":    symbol,
                    "tf":        tf,
                    "requestId": request_id,
                    "candle":    current,
                }))
        except Exception:
            self._clients.pop(ws, None)

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

            # Текущий брифинг — сразу новому клиенту (иначе он ждал бы следующего
            # обновления файла, т.е. до следующей крон-сессии).
            with self._briefing_lock:
                cached = self._briefing
            if cached is not None:
                await ws.send(json.dumps({"type": "briefing", "data": cached}))

            async for raw in ws:
                data = json.loads(raw)
                mtype = data.get("type")

                if mtype == "set_tf":
                    await self._on_set_tf(ws, data)

                elif mtype == "get_instruments":
                    await ws.send(json.dumps({"type": "instruments",
                                              "data": self._instruments}))

                elif mtype == "add_alert":
                    await self._on_add_alert(ws, data)

                elif mtype == "update_alert":
                    self._on_update_alert(data)

                elif mtype == "remove_alert":
                    self._on_remove_alert(data)
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
        if provider is None:
            log.warning("set_tf на неизвестный символ %s — игнор", symbol)
            return

        pips = self.parse_range_tf(tf)
        if pips is not None:
            await self._on_set_range_tf(ws, provider, symbol, tf, pips,
                                        request_id)
            return

        if tf not in self._tf_seconds:
            log.warning("set_tf на неизвестный %s %s — игнор", symbol, tf)
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

    async def _on_add_alert(self, ws, data):
        """Создать алерт и вернуть клиенту серверный id.

        Фронт создаёт алерт с id=null и ждёт alert_created, чтобы подставить
        настоящий id (index.html: obj.id = msg.id) — без ответа алерт нельзя
        будет ни обновить, ни удалить.

        Args:
            ws:   Соединение клиента.
            data: Сообщение add_alert (symbol, price).

        Returns:
            None.
        """
        symbol = data["symbol"]
        price  = round(float(data["price"]), 5)
        alert  = {"id": self._alert_id, "price": price, "triggered": False}
        self._alerts.setdefault(symbol, []).append(alert)
        self._alert_id += 1

        await ws.send(json.dumps({
            "type":   "alert_created",
            "symbol": symbol,
            "price":  price,
            "id":     alert["id"],
        }))

    def _on_update_alert(self, data):
        """Передвинуть уровень алерта.

        Args:
            data: Сообщение update_alert (symbol, id, price).

        Returns:
            None.
        """
        symbol   = data.get("symbol")
        alert_id = data.get("id")
        price    = data.get("price")
        if symbol is None or alert_id is None or price is None:
            return
        price = round(float(price), 5)
        for alert in self._alerts.get(symbol, []):
            if alert["id"] == alert_id:
                alert["price"]     = price
                # Сдвинули уровень — алерт снова «заряжен».
                alert["triggered"] = False
                break

    def _on_remove_alert(self, data):
        """Удалить алерт.

        Args:
            data: Сообщение remove_alert (symbol, id).

        Returns:
            None.
        """
        symbol   = data.get("symbol")
        alert_id = data.get("id")
        if symbol is None or alert_id is None:
            return
        alert_id = int(alert_id)
        if symbol in self._alerts:
            self._alerts[symbol] = [a for a in self._alerts[symbol]
                                    if a["id"] != alert_id]

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
            pips = self.parse_range_tf(info["tf"])
            if pips is not None:
                rb     = self._range_builders.get((provider, symbol, pips))
                candle = rb.current() if rb else None
            else:
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

    def _check_alerts(self, symbol, prev_price, price):
        """Сработать алерты, чьи уровни цена ПЕРЕСЕКЛА этим тиком.

        Перенос server.py:check_alerts. Условие — пересечение уровня в любую
        сторону (prev_price <= level <= price или наоборот), а не «цена выше».
        Сработавший алерт помечается triggered и больше не срабатывает — фронт
        рисует его 🔕 и сбрасывает только пересозданием.

        Args:
            symbol:     Инструмент (как шлёт фронт).
            prev_price: Цена предыдущего тика.
            price:      Цена текущего тика.

        Returns:
            None.
        """
        for alert in self._alerts.get(symbol, []):
            if alert["triggered"]:
                continue
            level = alert["price"]
            if (prev_price <= level <= price) or (price <= level <= prev_price):
                alert["triggered"] = True
                log.info("ALERT %s id=%s level=%s tick=%s",
                     symbol, alert["id"], level, price)
                # Событие срабатывания — ВСЕМ клиентам (фронт принимает его
                # независимо от requestId и подписки на символ).
                self._broadcast(json.dumps({
                    "type":   "alert",
                    "symbol": symbol,
                    "price":  level,
                    "id":     alert["id"],
                }))

    def _broadcast(self, payload):
        """Разослать готовый JSON всем клиентам (алерты, инструменты).

        Args:
            payload: Строка JSON.

        Returns:
            None.
        """
        for ws in list(self._clients):
            self._send(ws, payload)

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
            "range":     len(self._range_builders),
            "db_queue":  self._db_queue.qsize(),
            "db_dropped": self.db_dropped,
        }

    def health(self):
        """Снимок здоровья контура для health-эндпоинта и heartbeat.

        `status`: "ok" — данные свежие или рынок закрыт (тишина легитимна);
        "stale" — рынок открыт, но тиков давно нет (фид/брокер завис).
        `data_age` — секунд с последнего тика (None, если тиков ещё не было).

        Args:
            None.

        Returns:
            Dict статуса (JSON-сериализуемый).
        """
        now = time.time()
        age = (now - self._last_tick_ts) if self._last_tick_ts else None

        # «Свежесть» оцениваем только в открытый рынок: на выходных тиков нет
        # по определению, и это не деградация. Порог щедрый — даже тихая пара
        # тикает чаще; 180 с покрывает и паузу реконнекта фида (watchdog 120 с).
        open_now = market_open(now)
        stale    = bool(open_now and (age is None or age > 180))

        # Возраст последнего РЕАЛЬНОГО тика по каждому символу — фронт красит по
        # нему индикатор рынка выбранного инструмента (крипта 24/7 сама покажет
        # «открыт», форекс на выходных — «закрыт»).
        symbols_age = {sym: round(now - ts, 1)
                       for sym, ts in self._last_tick_by_symbol.items()}

        return {
            "status":    "stale" if stale else "ok",
            "uptime":    int(now - self._started_ts),
            "market_open": open_now,
            "data_age":  None if age is None else round(age, 1),
            "symbols_age": symbols_age,
            "clients":   len(self._clients),
            "ticks":     self.ticks_received,
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
        log.info("%s", hub.stats)


async def _heartbeat_loop(hub, every=10):
    """Слать всем клиентам heartbeat со снимком здоровья.

    Браузер по нему понимает, что хаб жив и данные свежие; отсутствие
    heartbeat дольше нескольких периодов = связь потеряна (фронт покажет это).
    Снимок тот же, что у /health — вкладке не нужен отдельный HTTP-запрос.

    Args:
        hub:   Экземпляр Hub.
        every: Период в секундах.

    Returns:
        None (бесконечный цикл).
    """
    while True:
        await asyncio.sleep(every)
        payload = json.dumps({"type": "heartbeat", "data": hub.health()})
        hub._broadcast(payload)


async def start_health_server(hub, port):
    """Поднять HTTP health-эндпоинт на отдельном порту.

    Отдельный лёгкий сервер (aiohttp), НЕ трогает боевой WS 8765 — health не
    может задеть поток данных. `/health` отдаёт JSON hub.health(); код 200 при
    status="ok", 503 при "stale" (рынок открыт, а тиков нет) — чтобы внешний
    мониторинг/аптайм-чекер видел деградацию по HTTP-коду.

    Args:
        hub:  Экземпляр Hub.
        port: Порт health-сервера.

    Returns:
        None.
    """
    from aiohttp import web

    async def handle(_request):
        h    = hub.health()
        code = 200 if h["status"] == "ok" else 503
        return web.json_response(h, status=code)

    app = web.Application()
    app.router.add_get("/health", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("health-эндпоинт: http://0.0.0.0:%d/health", port)


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
    threading.Thread(target=hub.briefing_watcher, daemon=True).start()

    bus = BusServer(hub.on_bus_message, config["bus_host"], config["bus_port"])
    await bus.start()

    await websockets.serve(hub.ws_handler, "0.0.0.0", config["ws_port"])
    log.info("WebSocket для браузера: 0.0.0.0:%d, база %s",
             config["ws_port"], config["db_path"])

    await start_health_server(hub, config.get("health_port", 8787))

    asyncio.ensure_future(_stats_loop(hub))
    asyncio.ensure_future(_heartbeat_loop(hub))
    await asyncio.Future()


if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(main())
