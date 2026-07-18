"""Профиль объёма: распределение наторгованного по ценам.

Отвечает на вопрос, которого НЕ ЗНАЕТ свеча: на каких ценах внутри бара
реально прошёл объём. OHLC хранит только границы и сумму, поэтому профиль
приходится копить из тиков в момент их прихода — задним числом из свечей
он не восстанавливается.

Зачем именно исполненный объём, а не заявки: сделку отменить нельзя, а
заявку снимают за секунду до подхода цены. Уровень, где реально наторговали,
надёжнее «стены» в стакане.

ШАГ КОРЗИНЫ — В ПРОЦЕНТАХ от цены (согласовано с владельцем). Причина:
в одном списке BTC за $64 000 и LIT за $2.2 — фиксированный шаг в единицах
цены дал бы либо одну корзину на весь LIT, либо миллион корзин на BTC.
0.05% даёт ~$32 на BTC и ~$0.001 на LIT — осмысленно на обоих концах.

СЕТКА ЛОГАРИФМИЧЕСКАЯ, а не «процент от текущей цены». Если считать шаг от
текущей цены, границы корзин поедут при движении рынка, и профили за разные
дни не сложатся. Логарифмическая сетка привязана к фиксированной опорной
точке: индекс корзины зависит ТОЛЬКО от самой цены, а не от того, когда её
посчитали. Цена x попадает в корзину floor(log(x / REF) / log(1 + step)).

Хранится ВЕЧНО, в отличие от тиков (кольцевой буфер 14 дней): корзина — это
десяток чисел на день и инструмент, а восстановить её потом будет неоткуда.
"""

import math

# Опорная цена сетки. Значение произвольно (важна лишь фиксированность), но
# менять его НЕЛЬЗЯ: сменишь — и все ранее посчитанные корзины поедут
# относительно новых, а старые данные станут несопоставимы.
REFERENCE_PRICE = 1.0

# Шаг корзины по умолчанию, доля от цены. 0.0005 = 0.05%.
DEFAULT_STEP = 0.0005


def bucket_index(price, step=DEFAULT_STEP):
    """Номер корзины, в которую попадает цена.

    Args:
        price: Цена сделки (> 0).
        step:  Шаг корзины как доля от цены (0.0005 = 0.05%).

    Returns:
        Целочисленный индекс корзины. Устойчив во времени: одна и та же цена
        всегда даёт один и тот же индекс, независимо от текущего рынка.

    Raises:
        ValueError: если цена не положительна — логарифм от неё не определён,
            а молча вернуть 0 значило бы свалить весь объём в одну корзину.
    """
    if price <= 0:
        raise ValueError("цена должна быть > 0, получено %r" % (price,))
    return int(math.floor(math.log(price / REFERENCE_PRICE) / math.log(1.0 + step)))


def bucket_bounds(index, step=DEFAULT_STEP):
    """Границы корзины по её номеру.

    Args:
        index: Индекс корзины.
        step:  Шаг корзины как доля от цены.

    Returns:
        Кортеж (low, high) — цены нижней и верхней границы.
    """
    ratio = 1.0 + step
    low = REFERENCE_PRICE * (ratio ** index)
    return low, low * ratio


class VolumeProfile:
    """Накопитель профиля по одному инструменту за один период.

    Период — сутки UTC либо торговая сессия; чем он является, решает
    вызывающий, передавая ключ периода. Профиль сам по себе не знает
    календаря.

    Объём разделяется на покупки и продажи по стороне агрессора: видно не
    только ГДЕ торговали, но и КТО продавливал уровень.
    """

    def __init__(self, step=DEFAULT_STEP):
        """Создать пустой профиль.

        Args:
            step: Шаг корзины как доля от цены.

        Returns:
            None.
        """
        self.step = step
        # bucket_index -> {"buy": base, "sell": base, "quote": quote}
        self._buckets = {}

    def add(self, price, size, side=None):
        """Учесть одну сделку.

        Args:
            price: Цена сделки.
            size:  Объём в базовых единицах.
            side:  Сторона агрессора ("buy"/"sell") или None. При None объём
                   попадает в корзину, но не относится ни к одной стороне —
                   выдумывать сторону нельзя, это исказило бы картину.

        Returns:
            None.
        """
        idx = bucket_index(price, self.step)
        bucket = self._buckets.get(idx)
        if bucket is None:
            bucket = {"buy": 0.0, "sell": 0.0, "quote": 0.0}
            self._buckets[idx] = bucket

        if side in ("buy", "sell"):
            bucket[side] += size
        # Оборот копится посделочно: сделки в корзине идут по разным ценам
        # внутри её диапазона, поэтому base * средняя цена дало бы не то.
        bucket["quote"] += size * price

    def total_base(self):
        """Суммарный объём в базовых единицах.

        Returns:
            Float: сумма покупок и продаж по всем корзинам.
        """
        return sum(b["buy"] + b["sell"] for b in self._buckets.values())

    def poc(self):
        """Point of Control — корзина с наибольшим объёмом.

        Это и есть «справедливая цена» периода: уровень, вокруг которого
        рынок провёл больше всего оборота.

        Returns:
            Кортеж (index, low, high, base_volume) либо None, если профиль пуст.
        """
        if not self._buckets:
            return None
        idx = max(self._buckets,
                  key=lambda i: self._buckets[i]["buy"] + self._buckets[i]["sell"])
        low, high = bucket_bounds(idx, self.step)
        return idx, low, high, self._buckets[idx]["buy"] + self._buckets[idx]["sell"]

    def value_area(self, fraction=0.7):
        """Диапазон цен, вобравший заданную долю объёма.

        Стандартная область стоимости — 70%: считается, что за её пределами
        рынок торговался «не по справедливой» цене. Корзины набираются от
        POC наружу, к более объёмному соседу на каждом шаге.

        Args:
            fraction: Доля объёма, которую должна покрыть область (0..1].

        Returns:
            Кортеж (low, high) — границы области, либо None для пустого профиля.
        """
        if not self._buckets:
            return None

        total = self.total_base()
        if total <= 0:
            return None

        indices = sorted(self._buckets)
        vol = {i: self._buckets[i]["buy"] + self._buckets[i]["sell"] for i in indices}
        poc_idx = max(vol, key=lambda i: vol[i])

        lo = hi = indices.index(poc_idx)
        acc = vol[poc_idx]
        target = total * fraction

        while acc < target and (lo > 0 or hi < len(indices) - 1):
            below = vol[indices[lo - 1]] if lo > 0 else -1.0
            above = vol[indices[hi + 1]] if hi < len(indices) - 1 else -1.0
            if above >= below:
                hi += 1
                acc += vol[indices[hi]]
            else:
                lo -= 1
                acc += vol[indices[lo]]

        low, _ = bucket_bounds(indices[lo], self.step)
        _, high = bucket_bounds(indices[hi], self.step)
        return low, high

    def to_rows(self):
        """Выгрузить корзины для записи в БД.

        Returns:
            Список кортежей (bucket_index, price_low, price_high,
            vol_buy, vol_sell, vol_quote), отсортированный по цене.
        """
        rows = []
        for idx in sorted(self._buckets):
            b = self._buckets[idx]
            low, high = bucket_bounds(idx, self.step)
            rows.append((idx, low, high, b["buy"], b["sell"], b["quote"]))
        return rows

    def load_rows(self, rows):
        """Влить ранее сохранённые корзины обратно в профиль.

        Нужно после рестарта: профиль текущего периода не должен начинаться
        с нуля только потому, что хаб перезапустили в середине дня.

        Args:
            rows: Последовательность (bucket_index, vol_buy, vol_sell, vol_quote).

        Returns:
            None.
        """
        for idx, buy, sell, quote in rows:
            bucket = self._buckets.get(idx)
            if bucket is None:
                bucket = {"buy": 0.0, "sell": 0.0, "quote": 0.0}
                self._buckets[idx] = bucket
            bucket["buy"] += buy or 0.0
            bucket["sell"] += sell or 0.0
            bucket["quote"] += quote or 0.0

    def __len__(self):
        """Число непустых корзин.

        Returns:
            Int.
        """
        return len(self._buckets)
