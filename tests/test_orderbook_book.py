"""Локальная книга заявок: применение дельт и срез.

Запуск: python3.10 tests/test_orderbook_book.py

Главная ловушка стакана — размер `0`. У Lighter это не «уровень с нулевым
объёмом», а команда СНЯТЬ уровень. Приняв его буквально, книга за минуты
забьётся мёртвыми ценами, и стены на графике встанут там, где заявок давно
нет. Проверяется живым форматом биржи (строки, не числа — Lighter отдаёт
цены строками).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from feeds.orderbook import OrderBook


def _lv(price, size):
    """Уровень в формате биржи: строки, а не числа.

    Args:
        price: Цена.
        size:  Объём.

    Returns:
        Dict уровня, как его шлёт Lighter.
    """
    return {"price": str(price), "size": str(size)}


class TestSnapshot(unittest.TestCase):
    """Снапшот заменяет книгу целиком."""

    def test_not_ready_before_snapshot(self):
        """До снапшота книга не отдаёт срез.

        Дельты описывают изменения относительно состояния, которого ещё
        нет — срез был бы вымышленным.
        """
        book = OrderBook()
        self.assertFalse(book.ready)
        book.apply_delta([_lv(64000, 1)], [])
        self.assertEqual(book.snapshot(), ([], []))

    def test_snapshot_replaces_not_merges(self):
        """Второй снапшот ЗАМЕНЯЕТ книгу, а не дополняет.

        После переподключения биржа шлёт актуальную книгу; слив её со
        старой оставил бы уровни, которых давно нет.
        """
        book = OrderBook()
        book.apply_snapshot([_lv(64000, 1)], [_lv(64001, 1)])
        book.apply_snapshot([_lv(63000, 2)], [_lv(63001, 2)])
        self.assertEqual(set(book.bids), {63000.0})
        self.assertEqual(set(book.asks), {63001.0})

    def test_reset_clears(self):
        """reset() возвращает книгу в неготовое состояние."""
        book = OrderBook()
        book.apply_snapshot([_lv(64000, 1)], [_lv(64001, 1)])
        book.reset()
        self.assertFalse(book.ready)
        self.assertEqual(book.bids, {})


class TestDelta(unittest.TestCase):
    """Применение дельт, включая снятие уровней."""

    def setUp(self):
        """Книга с тремя уровнями на сторону.

        Returns:
            None.
        """
        self.book = OrderBook()
        self.book.apply_snapshot(
            [_lv(64000, 1.0), _lv(63999, 2.0), _lv(63998, 3.0)],
            [_lv(64001, 1.0), _lv(64002, 2.0), _lv(64003, 3.0)])

    def test_zero_size_removes_level(self):
        """size=0 СНИМАЕТ уровень, а не ставит нулевой объём.

        Это главная ловушка формата: приняв 0 буквально, книга копила бы
        мёртвые цены, и стены встали бы там, где заявок нет.
        """
        self.book.apply_delta([_lv(63999, 0)], [])
        self.assertNotIn(63999.0, self.book.bids)
        self.assertEqual(len(self.book.bids), 2)

    def test_existing_level_updated(self):
        """Известный уровень обновляется новым объёмом."""
        self.book.apply_delta([_lv(64000, 9.5)], [])
        self.assertEqual(self.book.bids[64000.0], 9.5)

    def test_new_level_added(self):
        """Неизвестный уровень добавляется."""
        self.book.apply_delta([], [_lv(64004, 4.0)])
        self.assertEqual(self.book.asks[64004.0], 4.0)

    def test_removing_absent_level_is_safe(self):
        """Снятие несуществующего уровня не падает."""
        self.book.apply_delta([_lv(1.0, 0)], [])
        self.assertEqual(len(self.book.bids), 3)

    def test_broken_level_skipped(self):
        """Кривой уровень пропускается, книга живёт дальше."""
        self.book.apply_delta([{"price": "нет"}, _lv(64000, 5.0)], [])
        self.assertEqual(self.book.bids[64000.0], 5.0)


class TestSnapshotSlice(unittest.TestCase):
    """Срез вокруг цены — то, что уходит на фронт."""

    def test_filters_by_percent(self):
        """Уровни дальше ±pct% от mid не попадают в срез.

        Замер на BTC: стакан тянется на 79% вниз и 366% вверх, ±0.5% режет
        ~4000 уровней до ~700. Без фильтра гоняли бы на фронт то, что
        никогда не окажется на экране.
        """
        book = OrderBook()
        # mid = 1000. В ±0.5% попадают 995…1005.
        book.apply_snapshot(
            [_lv(999, 1), _lv(990, 1), _lv(500, 1)],
            [_lv(1001, 1), _lv(1010, 1), _lv(2000, 1)])
        bids, asks = book.snapshot(pct=0.5)
        self.assertEqual([lv[0] for lv in bids], [999.0])
        self.assertEqual([lv[0] for lv in asks], [1001.0])

    def test_sorted_outward_from_market(self):
        """bids по убыванию, asks по возрастанию — обе стороны от рынка."""
        book = OrderBook()
        book.apply_snapshot(
            [_lv(998, 1), _lv(1000, 1), _lv(999, 1)],
            [_lv(1003, 1), _lv(1001, 1), _lv(1002, 1)])
        bids, asks = book.snapshot(pct=5)
        self.assertEqual([lv[0] for lv in bids], [1000.0, 999.0, 998.0])
        self.assertEqual([lv[0] for lv in asks], [1001.0, 1002.0, 1003.0])

    def test_max_levels_caps_each_side(self):
        """Потолок уровней срабатывает на каждую сторону отдельно."""
        book = OrderBook()
        book.apply_snapshot(
            [_lv(1000 - i, 1) for i in range(50)],
            [_lv(1001 + i, 1) for i in range(50)])
        bids, asks = book.snapshot(pct=50, max_levels=10)
        self.assertEqual(len(bids), 10)
        self.assertEqual(len(asks), 10)
        # Обрезается ДАЛЬНЕЕ, ближнее к рынку остаётся.
        self.assertEqual(bids[0][0], 1000.0)

    def test_empty_side_gives_no_mid(self):
        """Без одной стороны mid не считается и срез пуст."""
        book = OrderBook()
        book.apply_snapshot([_lv(1000, 1)], [])
        self.assertIsNone(book.mid())
        self.assertEqual(book.snapshot(), ([], []))

    def test_sizes_preserved(self):
        """Объёмы доезжают до среза без искажения."""
        book = OrderBook()
        book.apply_snapshot([_lv(1000, 2.5)], [_lv(1001, 3.5)])
        bids, asks = book.snapshot(pct=5)
        self.assertEqual(bids[0][1], 2.5)
        self.assertEqual(asks[0][1], 3.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
