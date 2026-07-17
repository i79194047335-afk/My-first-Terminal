"""
Range bars: нарезка тикового потока по ДИАПАЗОНУ цены, а не по времени.

Provides:
  - RangeBarBuilder: конечный автомат на один (инструмент, диапазон) —
    .ingest() на каждый тик, закрытые бары наружу.
  - iter_ticks / backfill: построение истории из вечного тикового архива
    data/<SYMBOL>_<YYYYMMDD>.csv (рэндж-бары честно строятся ТОЛЬКО из тиков —
    из готовых свечей внутрибарный путь цены не восстановить).

Зафиксированные решения (менять здесь, в одном месте):

  1. Закрытие «на пробое»: бар закрывается тем тиком, который довёл high-low
     до range_size и ВКЛЮЧАЕТ его (close = цена тика). На спокойном потоке
     диапазон закрытого бара ≈ range_size (превышение — доли пипса от
     дискретности тиков). Фантомную «лесенку» в стиле TradingView не рисуем:
     каждый close — реально наторгованная цена, а не синтетика.

  2. Гэпы разрешены: новый бар открывается от ЦЕНЫ ТИКА-пробойщика, а НЕ
     подтягивается к close предыдущего. На закрытии рынка (выходные, новостной
     скачок) цена прыгает через порог одним тиком — этот разрыв остаётся
     видимым гэпом между барами, а не всасывается в один раздутый бар (раньше
     подтяжка open=prev_close давала бары по 30-50 пипсов при R=10).

  3. Время бара = unix-секунды его открытия, СТРОГО уникальное и растущее:
     LightweightCharts не принимает две точки с одним time. При закрытии
     нескольких баров в одну секунду время бампится на +1с — на всплесках
     бары чуть «уезжают в будущее», это цена совместимости с LWC.

range_size — в АБСОЛЮТНЫХ единицах цены (0.0005, не «5 пипсов»): пересчёт
пипсов в цену — забота вызывающего, у него есть instruments.price_decimals.
"""

import csv
import glob
import os


class RangeBarBuilder:
    """Стейт-машина одного (инструмент, диапазон): тики → рэндж-бары.

    Usage::

        builder = RangeBarBuilder(0.0005)          # 5 пипсов EUR/USD
        for price, ts in ticks:
            bar = builder.ingest(price, ts)
            if bar:
                persist(bar)                        # закрытый бар
        live = builder.current()                    # незакрытый (для WS update)
    """

    def __init__(self, range_size, max_bars=None):
        """Создать билдер.

        Args:
            range_size: Диапазон бара в абсолютных единицах цены (> 0).
            max_bars:   Потолок длины history (None = без ограничения);
                        зеркалит keep_bars хаба, чтобы память не текла
                        на долгом прогоне.

        Returns:
            None.

        Raises:
            ValueError: если range_size не положительный.
        """
        if not range_size or range_size <= 0:
            raise ValueError("range_size должен быть > 0, получен %r"
                             % (range_size,))
        self.range_size = float(range_size)
        self._max_bars  = max_bars
        self._current   = None
        self._history   = []
        self._last_time = None   # время последнего ЗАКРЫТОГО бара (для бампа)

    # ── public API ──────────────────────────────────────────────────────

    def ingest(self, price, ts):
        """Обработать один тик.

        Args:
            price: Цена тика (mid).
            ts:    Unix-время тика в секундах (float допустим).

        Returns:
            Закрытый бар (dict time/open/high/low/close), если этот тик
            довёл диапазон до range_size, иначе None.
        """
        if self._current is None:
            self._open_bar(price, ts)
            return None

        c = self._current

        # Гэп: ОДИН тик двигает цену от текущего close бара БОЛЬШЕ чем на R —
        # перепрыгивает весь диапазон разом (закрытие рынка на выходные,
        # новостной разрыв: цена скачком на 30-50п при R=10). Обычный тик
        # двигает цену на доли пипса, поэтому скачок > R — надёжный признак
        # разрыва, в любом состоянии бара (не только «точки»). Влить его как
        # расширение — растянуть бар на десятки пипсов (тот самый баг). Вместо
        # этого закрываем текущий бар КАК ЕСТЬ (до скачка) и открываем новый от
        # цены тика — разрыв остаётся честным гэпом, а бары держат размах ≈ R.
        if abs(price - c["close"]) > self.range_size + 1e-12:
            return self._close_and_reopen(price, ts)

        c["high"]  = max(c["high"], price)
        c["low"]   = min(c["low"],  price)
        c["close"] = price

        # Порог с эпсилоном: диапазон копится сложением float-цен, и ровно
        # набранный R иначе повисал бы на 1e-18 ниже порога.
        if c["high"] - c["low"] < self.range_size - 1e-12:
            return None

        closed = c
        self._history.append(closed)
        if self._max_bars is not None and len(self._history) > self._max_bars:
            del self._history[:len(self._history) - self._max_bars]
        self._last_time = closed["time"]

        # Тик-пробойщик открывает следующий бар от СВОЕЙ цены (не от close
        # закрытого) — разрыв рынка остаётся честным гэпом, см. _open_bar.
        self._open_bar(price, ts)
        return closed

    def _close_and_reopen(self, price, ts):
        """Закрыть текущий бар без гэп-тика, открыть новый от цены тика.

        Args:
            price: Цена гэп-тика (open нового бара).
            ts:    Его unix-время.

        Returns:
            Закрытый бар (тот, что был живым до гэпа).
        """
        closed = self._current
        self._history.append(closed)
        if self._max_bars is not None and len(self._history) > self._max_bars:
            del self._history[:len(self._history) - self._max_bars]
        self._last_time = closed["time"]
        self._open_bar(price, ts)
        return closed

    def current(self):
        """Незакрытый (живой) бар или None."""
        return self._current

    def history(self):
        """Список закрытых баров, старые первыми."""
        return self._history

    def seed_history(self, bars):
        """Подложить готовую историю (из кэша/БД) перед живым потоком.

        Живой бар НЕ восстанавливается: у рэндж-бара нет «текущего бакета»,
        который можно вычислить из часов, — недостроенный бар честно
        перестраивается только повторным прогоном тиков (см. backfill).
        Первый живой тик откроет бар от СВОЕЙ цены (как и любой первый тик
        нового бара); накопленный до рестарта диапазон потеряется.

        Args:
            bars: Список закрытых баров, старые первыми.

        Returns:
            None.
        """
        self._history   = list(bars)
        self._last_time = bars[-1]["time"] if bars else None
        self._current   = None

    # ── internals ───────────────────────────────────────────────────────

    def _open_bar(self, price, ts):
        """Открыть новый бар от ЦЕНЫ ТИКА-пробойщика (с бампом времени).

        Бар открывается ровно от цены тика, а НЕ подтягивается к close
        предыдущего. Разрыв между барами — честный гэп: на закрытии рынка
        (выходные, новостной скачок) цена прыгает через порог одним тиком, и
        раньше подтяжка open=prev_close всасывала весь скачок в один бар,
        раздувая его размах до десятков пипсов при R=10. Теперь скачок остаётся
        видимым гэпом, а каждый бар держит свой диапазон ≈ R.

        Args:
            price: Цена открывающего тика.
            ts:    Его unix-время в секундах.

        Returns:
            None.
        """
        t = int(ts)
        if self._last_time is not None and t <= self._last_time:
            t = self._last_time + 1   # уникальность времени для LWC

        self._current = {"time": t, "open": price,
                         "high": price, "low": price, "close": price}


# ── бэкфил из тикового архива ───────────────────────────────────────────

def iter_ticks(data_dir, symbol, since_ts=None):
    """Пройти тиковый архив инструмента в хронологическом порядке.

    Файлы data/<SYMBOL>_<YYYYMMDD>.csv лексикографически = хронологически,
    внутри файла тики уже отсортированы (пишутся потоком).

    Args:
        data_dir: Каталог архива (data/).
        symbol:   Инструмент как в потоке ("EUR/USD").
        since_ts: Отдавать только тики с ts >= since_ts (None = все).
                  Целые файлы старше отсекаются по дате в имени — дёшево.

    Yields:
        Кортежи (mid_price, ts), старые первыми. Битые строки молча
        пропускаются (архив пишется живым потоком, обрывы возможны).
    """
    prefix = symbol.replace("/", "")
    files  = sorted(glob.glob(os.path.join(data_dir, "%s_*.csv" % prefix)))

    for path in files:
        if since_ts is not None:
            # Имя = ...._YYYYMMDD.csv; файл целиком старше начала суток
            # since_ts — читать нечего. День на границе читаем и фильтруем.
            day = os.path.basename(path).rsplit("_", 1)[-1][:8]
            if day < _utc_day(since_ts):
                continue

        with open(path, "r") as f:
            for row in csv.DictReader(f):
                try:
                    ts = float(row["timestamp_utc"])
                    if since_ts is not None and ts < since_ts:
                        continue
                    yield float(row["mid"]), ts
                except (KeyError, ValueError, TypeError):
                    continue


def backfill(data_dir, symbol, range_size, max_bars=2000, since_ts=None):
    """Построить историю рэндж-баров из тикового архива.

    Рэндж-бары path-dependent: строятся только вперёд, от якоря к настоящему.
    Якорь — начало доступного архива (или since_ts); наружу отдаётся хвост
    max_bars, как у свечных ТФ хаба.

    Args:
        data_dir:   Каталог тикового архива.
        symbol:     Инструмент ("EUR/USD").
        range_size: Диапазон в абсолютных единицах цены.
        max_bars:   Сколько закрытых баров держать (окно ретеншена).
        since_ts:   Начать с этого unix-времени (None = весь архив).

    Returns:
        RangeBarBuilder с заполненной history и живым недостроенным баром —
        готов принимать тики шины без шва.
    """
    builder = RangeBarBuilder(range_size, max_bars=max_bars)
    for price, ts in iter_ticks(data_dir, symbol, since_ts=since_ts):
        builder.ingest(price, ts)
    return builder


def _utc_day(ts):
    """YYYYMMDD (UTC) для unix-времени — сравнение с датой в имени файла.

    Args:
        ts: Unix-секунды.

    Returns:
        Строка "YYYYMMDD".
    """
    import datetime
    return datetime.datetime.utcfromtimestamp(ts).strftime("%Y%m%d")
