"""Локальная книга лимитных заявок (стакан) одного рынка.

Биржа шлёт снапшот при подписке, дальше только ДЕЛЬТЫ: изменившиеся уровни.
Размер `0` означает не «нулевой объём», а «уровень снят» — если применять
такие записи как обычные, книга забьётся пустыми уровнями и стены поедут.

Здесь только состояние и его обновление; сеть — в lighter_feed.py, чтобы
логику применения дельт можно было проверить без биржи.

Модуль лежит в `feeds/`, а не в `core/`: он нужен только фиду биржи, а
`core` обязан парситься на Python 3.7 (там живёт фид FXCM).
"""


class OrderBook:
    """Книга заявок одного рынка: цена → объём, отдельно по сторонам.

    Хранится как dict цена→объём, а не отсортированным списком: дельты
    приходят ~17 раз в секунду и адресуют произвольные уровни, а срез
    наружу отдаётся раз в секунду. Сортировать раз в секунду дешевле, чем
    держать список упорядоченным на каждой дельте.
    """

    def __init__(self):
        """Создать пустую книгу.

        Returns:
            None.
        """
        self.bids = {}
        self.asks = {}
        self._ready = False

    @property
    def ready(self):
        """Пришёл ли снапшот.

        До снапшота книга неполна: дельты описывают изменения относительно
        состояния, которого у нас ещё нет, и срез был бы вымышленным.

        Returns:
            True, если снапшот применён.
        """
        return self._ready

    def reset(self):
        """Забыть книгу целиком (обрыв связи, переподписка).

        Returns:
            None.
        """
        self.bids.clear()
        self.asks.clear()
        self._ready = False

    def apply_snapshot(self, bids, asks):
        """Заменить книгу снапшотом биржи.

        Args:
            bids: Список dict'ов {"price": str, "size": str}.
            asks: То же для стороны продажи.

        Returns:
            None.
        """
        self.bids.clear()
        self.asks.clear()
        self._apply_levels(self.bids, bids)
        self._apply_levels(self.asks, asks)
        self._ready = True

    def apply_delta(self, bids, asks):
        """Применить дельту к книге.

        Args:
            bids: Изменившиеся уровни покупки.
            asks: Изменившиеся уровни продажи.

        Returns:
            None.
        """
        self._apply_levels(self.bids, bids)
        self._apply_levels(self.asks, asks)

    @staticmethod
    def _apply_levels(side, levels):
        """Наложить список уровней на одну сторону книги.

        Args:
            side:   Dict цена→объём, изменяется на месте.
            levels: Список dict'ов {"price": ..., "size": ...}.

        Returns:
            None.
        """
        for level in levels or ():
            try:
                price = float(level["price"])
                size  = float(level["size"])
            except (KeyError, TypeError, ValueError):
                # Кривой уровень не должен стоить нам всей книги.
                continue
            if size > 0:
                side[price] = size
            else:
                # size == 0 — СНЯТИЕ уровня, а не нулевой объём.
                side.pop(price, None)

    def best(self):
        """Лучшие цены обеих сторон.

        Returns:
            Кортеж (best_bid, best_ask); элемент None, если сторона пуста.
        """
        best_bid = max(self.bids) if self.bids else None
        best_ask = min(self.asks) if self.asks else None
        return best_bid, best_ask

    def mid(self):
        """Средняя цена между лучшими bid и ask.

        Returns:
            Float или None, если одна из сторон пуста.
        """
        best_bid, best_ask = self.best()
        if best_bid is None or best_ask is None:
            return None
        return (best_bid + best_ask) / 2.0

    def snapshot(self, pct=2.0, max_levels=1200):
        """Срез книги вокруг текущей цены — то, что уходит на фронт.

        Отсекается по проценту от mid, а не по числу уровней: стакан Lighter
        тянется на 79% вниз и 366% вверх (замер на BTC), и подавляющая часть
        уровней никогда не попадёт на экран.

        Замер на BTC, сколько уровней даёт полуширина: ±0.5% → 576,
        ±1% → 758, ±2% → 1238, ±3% → 1597. Рост НЕ линейный — ликвидность
        концентрируется у цены, поэтому расширение вчетверо стоит лишь
        удвоения трафика. ±2% выбрано как покрытие типичного зума с запасом:
        ±0.5% закрывало меньше половины видимой шкалы, и стены жались к
        центру экрана.

        Args:
            pct:        Полуширина окна в процентах от mid.
            max_levels: Жёсткий потолок на сторону — страховка от книги с
                        сотнями тысяч уровней (тонкий рынок, мелкий тик).

        Returns:
            Кортеж (bids, asks): списки пар [цена, объём]. bids по убыванию
            цены, asks по возрастанию — обе стороны «от рынка наружу».
            Пустые списки, если снапшота ещё не было.
        """
        if not self._ready:
            return [], []
        mid = self.mid()
        if mid is None:
            return [], []

        lo = mid * (1.0 - pct / 100.0)
        hi = mid * (1.0 + pct / 100.0)

        bids = [[p, self.bids[p]] for p in self.bids if p >= lo]
        asks = [[p, self.asks[p]] for p in self.asks if p <= hi]
        bids.sort(key=lambda lv: lv[0], reverse=True)
        asks.sort(key=lambda lv: lv[0])
        return bids[:max_levels], asks[:max_levels]
