"""
Tests for briefing/memory.py (Фаза 7, слой 2 — журнал + самооценка).

Журнал пишется во ВРЕМЕННЫЙ файл (боевой data/briefing_journal.json не трогаем).
Самооценка сверяется с market.db — если её нет, живые тесты скипаются, а
чистая логика вердиктов проверяется на подставном факте через monkeypatch.

Run:  python3 tests/test_briefing_memory.py
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from briefing import memory


class TestPriceParsing(unittest.TestCase):

    def test_extracts_price(self):
        self.assertEqual(memory._price_from_summary("Цена 1.14379, D1 …"), 1.14379)
        self.assertEqual(memory._price_from_summary("цена 162.4055 сейчас"), 162.4055)

    def test_none_when_absent(self):
        self.assertIsNone(memory._price_from_summary("нет цены"))
        self.assertIsNone(memory._price_from_summary(""))


class TestJournal(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._orig = memory.JOURNAL_FILE
        memory.JOURNAL_FILE = os.path.join(self._tmp, "journal.json")

    def tearDown(self):
        memory.JOURNAL_FILE = self._orig

    def _brief(self, ts, direction="UP", price=1.1000):
        return {
            "meta": {"generated_ts": ts, "session": "asia"},
            "pairs": {"EUR/USD": {
                "direction": direction, "direction_confidence": 4,
                "technical_summary": "Цена %.5f тест" % price,
                "support_levels": [1.0990], "resistance_levels": [1.1010],
            }},
        }

    def test_record_and_depth_cap(self):
        """Журнал пишет прогнозы и держит окно JOURNAL_DEPTH."""
        for i in range(memory.JOURNAL_DEPTH + 5):
            memory.record_briefing(self._brief(1000 + i))
        j = memory.load_journal()
        self.assertEqual(len(j["pairs"]["EUR/USD"]), memory.JOURNAL_DEPTH)
        # осталось окно последних записей
        self.assertEqual(j["pairs"]["EUR/USD"][-1]["ts"], 1000 + memory.JOURNAL_DEPTH + 4)

    def test_load_missing_returns_empty(self):
        """Нет файла → пустой каркас, не исключение."""
        memory.JOURNAL_FILE = os.path.join(self._tmp, "nope.json")
        self.assertEqual(memory.load_journal(), {"pairs": {}})


class TestAssessmentVerdicts(unittest.TestCase):
    """Вердикты на ПОДСТАВНОМ факте (без market.db) — чистая логика."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._origf = memory.JOURNAL_FILE
        self._origfact = memory._fact_since
        memory.JOURNAL_FILE = os.path.join(self._tmp, "j.json")

    def tearDown(self):
        memory.JOURNAL_FILE = self._origf
        memory._fact_since = self._origfact

    def _setup(self, direction, price_at, price_now):
        b = {
            "meta": {"generated_ts": 1000, "session": "ny"},
            "pairs": {"USD/JPY": {
                "direction": direction, "direction_confidence": 3,
                "technical_summary": "Цена %.5f" % price_at,
                "support_levels": [], "resistance_levels": [],
            }},
        }
        memory.record_briefing(b)
        # подставляем факт: price_now и диапазон вокруг него
        memory._fact_since = lambda sym, t0, t1: (price_now, price_now, price_now)

    def test_hit_up(self):
        self._setup("UP", 162.00, 162.20)      # +20 пипс (JPY)
        self.assertEqual(memory.assess_previous("USD/JPY", 9999)["verdict"], "сбылось")

    def test_miss_up(self):
        self._setup("UP", 162.20, 162.00)      # −20 пипс, а ждали рост
        self.assertEqual(memory.assess_previous("USD/JPY", 9999)["verdict"], "не сбылось")

    def test_hit_down(self):
        self._setup("DOWN", 162.20, 162.00)    # −20, ждали падение
        self.assertEqual(memory.assess_previous("USD/JPY", 9999)["verdict"], "сбылось")

    def test_neutral(self):
        self._setup("UP", 162.00, 162.02)      # +2 пипс < порога
        self.assertEqual(memory.assess_previous("USD/JPY", 9999)["verdict"], "нейтрально")

    def test_no_history(self):
        self.assertIsNone(memory.assess_previous("EUR/USD", 9999))


if __name__ == "__main__":
    unittest.main(verbosity=1)
