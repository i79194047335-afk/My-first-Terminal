"""Стакан в контракте шины.

Запуск: python3.7 tests/test_orderbook_bus.py  (и на 3.10 — core общий)

Стакан отличается от тика и свечи тем, что НЕ хранится: это состояние
«сейчас», живущее одну секунду. Поэтому валидация здесь строже обычного —
ошибку в срезе не поймать потом по расхождению с БД, её просто не с чем
сравнить. Что защищается:

  1. Формат уровней — пара [цена, объём], а не dict и не тройка.
  2. Нулевой объём НЕ проходит: у Lighter это команда «снять уровень»,
     она применяется в фиде; в срезе он выглядел бы пустой полосой.
  3. Стороны не пересекаются: bid ниже ask. Пересечение означает, что
     стороны перепутаны местами — на графике стены встали бы зеркально.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.bus import BusError, make_orderbook, validate


def _book(bids=None, asks=None, ts=1784500000):
    """Собрать корректный срез стакана с возможностью подмены частей.

    Args:
        bids: Заявки на покупку или None для значения по умолчанию.
        asks: Заявки на продажу или None для значения по умолчанию.
        ts:   Время среза.

    Returns:
        Dict сообщения шины.
    """
    if bids is None:
        bids = [[64000.0, 1.5], [63999.0, 2.0]]
    if asks is None:
        asks = [[64001.0, 1.2], [64002.0, 0.8]]
    return make_orderbook("lighter", "BTC", ts, bids, asks)


class TestOrderbookAccepted(unittest.TestCase):
    """Корректный стакан проходит валидацию."""

    def test_valid_book(self):
        """Обычный срез принимается и возвращается как есть."""
        msg = _book()
        self.assertIs(validate(msg), msg)
        self.assertEqual(msg["type"], "orderbook")

    def test_empty_sides_allowed(self):
        """Пустая сторона — не ошибка.

        На тонком рынке одна сторона книги реально бывает пустой, и ронять
        из-за этого весь срез нельзя.
        """
        validate(_book(bids=[], asks=[]))
        validate(_book(bids=[]))

    def test_integer_values_allowed(self):
        """Целые числа — тоже числа (цена 64000, а не 64000.0)."""
        validate(_book(bids=[[64000, 2]], asks=[[64001, 3]]))


class TestOrderbookRejected(unittest.TestCase):
    """Типовые ошибки формата отбиваются с внятным сообщением."""

    def _assert_rejected(self, msg, fragment):
        """Проверить, что сообщение отвергнуто и текст ошибки объясняет почему.

        Args:
            msg:      Сообщение шины.
            fragment: Подстрока, обязанная быть в тексте ошибки.

        Returns:
            None.
        """
        with self.assertRaises(BusError) as ctx:
            validate(msg)
        self.assertIn(fragment, str(ctx.exception))

    def test_level_as_dict_rejected(self):
        """Уровень словарём — формат биржи, а не шины.

        Lighter отдаёт {"price": ..., "size": ...}; конвертация обязана
        произойти в фиде, иначе фронт получит неожиданную форму.
        """
        self._assert_rejected(
            _book(bids=[{"price": 64000.0, "size": 1.0}]), "парой")

    def test_triple_rejected(self):
        """Тройка вместо пары — лишнее поле не должно молча проехать."""
        self._assert_rejected(_book(asks=[[64001.0, 1.0, 7]]), "парой")

    def test_zero_size_rejected(self):
        """Нулевой объём = снятие уровня, наружу выходить не должен."""
        self._assert_rejected(_book(bids=[[64000.0, 0.0]]), "снятие")

    def test_negative_price_rejected(self):
        """Отрицательная цена невозможна."""
        self._assert_rejected(_book(asks=[[-1.0, 1.0]]), "цена")

    def test_crossed_book_rejected(self):
        """Пересечение сторон — признак перепутанных bid/ask.

        Лучший bid обязан быть НИЖЕ лучшего ask: иначе заявки исполнились бы
        друг о друга. Молча пропустив это, мы нарисовали бы стены зеркально.
        """
        self._assert_rejected(
            _book(bids=[[64100.0, 1.0]], asks=[[64000.0, 1.0]]),
            "перепутаны")

    def test_milliseconds_rejected(self):
        """Миллисекунды не проходят — внутри шины время в секундах."""
        self._assert_rejected(_book(ts=1784500000000), "МИЛЛИСЕКУНД")

    def test_string_values_rejected(self):
        """Строки вместо чисел — Lighter отдаёт цены строками."""
        self._assert_rejected(_book(bids=[["64000.0", "1.5"]]), "нечислов")

    def test_sides_must_be_lists(self):
        """bids/asks — списки, не что-то другое."""
        self._assert_rejected(_book(bids={}), "списком")


class TestOtherTypesUnaffected(unittest.TestCase):
    """Добавление orderbook не сломало остальной контракт."""

    def test_unknown_type_still_rejected(self):
        """Неизвестный тип по-прежнему отбивается."""
        with self.assertRaises(BusError) as ctx:
            validate({"type": "nope", "provider": "lighter"})
        self.assertIn("orderbook", str(ctx.exception),
                      "текст ошибки должен перечислять допустимые типы")


if __name__ == "__main__":
    unittest.main(verbosity=2)
