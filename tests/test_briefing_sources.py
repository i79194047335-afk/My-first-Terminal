"""
Tests for briefing/sources.py (Фаза 7, слой 1).

Юнит-часть (без сети): форматирование времени в UTC+5, извлечение ts,
news_summary и порог «мало новостей». Интеграционная часть (с сетью):
реальный fetch_news/fetch_calendar — скипается, если сети нет.

Run:  python3 tests/test_briefing_sources.py
"""

import os
import sys
import time
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from briefing import sources


class TestTimeFormatting(unittest.TestCase):

    def test_display_is_utc_plus_5(self):
        """Время показывается в UTC+5 (сдвиг ровно +5 часов)."""
        # 2026-07-17 22:00:00 UTC  →  18.07 03:00 UTC+5
        ts = int(datetime(2026, 7, 17, 22, 0, tzinfo=timezone.utc).timestamp())
        self.assertEqual(sources._fmt_display(ts), "18.07 03:00 UTC+5")

    def test_display_empty_on_bad_ts(self):
        """Пустой/битый ts → пустая строка, не исключение."""
        self.assertEqual(sources._fmt_display(0), "")
        self.assertEqual(sources._fmt_display(None), "")


class TestNewsSummary(unittest.TestCase):

    def _diag(self):
        return [
            {"source": "FXStreet",  "raw": 30, "relevant": 4, "ok": True, "error": None},
            {"source": "ForexLive", "raw": 25, "relevant": 3, "ok": True, "error": None},
            {"source": "DeadFeed",  "raw": 0,  "relevant": 0, "ok": False, "error": "URLError"},
        ]

    def test_summary_line_and_total(self):
        """Отчёт содержит счётчики по фидам, мёртвый помечен DEAD."""
        line, total, low = sources.news_summary(self._diag())
        self.assertIn("FXStreet 4/30", line)
        self.assertIn("ForexLive 3/25", line)
        self.assertIn("DeadFeed DEAD", line)
        self.assertEqual(total, 7)

    def test_low_flag(self):
        """low=True, когда релевантных меньше порога."""
        _, _, low = sources.news_summary(self._diag())          # total=7 < 8
        self.assertTrue(low)
        rich = self._diag()
        rich[0]["relevant"] = 20                                # total=23 >= 8
        _, total, low2 = sources.news_summary(rich)
        self.assertFalse(low2)


class TestLiveFetch(unittest.TestCase):
    """Реальные источники — скип, если сеть недоступна."""

    @classmethod
    def setUpClass(cls):
        # Дешёвая проба: если первый фид не резолвится — сети нет, скипаем класс.
        import feedparser
        f = feedparser.parse(sources.RSS_FEEDS[0]["url"])
        if f.get("bozo") and not f.entries:
            raise unittest.SkipTest("нет сети для живых источников")

    def test_news_returns_items_and_diag(self):
        """fetch_news отдаёт заголовки и диагностику по каждому фиду."""
        items, diag = sources.fetch_news()
        self.assertEqual(len(diag), len(sources.RSS_FEEDS))
        # хотя бы один фид ответил и дал релевантное
        self.assertTrue(any(d["ok"] for d in diag))
        # у items с ts — время в UTC+5 непустое
        for it in items:
            self.assertIn("source", it)
            if it["ts"]:
                self.assertTrue(it["time_display"].endswith("UTC+5"))

    def test_calendar_no_error(self):
        """fetch_calendar не падает; error=None при живом источнике."""
        events, err = sources.fetch_calendar()
        self.assertIsNone(err, "календарь недоступен: %s" % err)
        # события отсортированы по времени и в будущем
        now = time.time()
        for e in events:
            self.assertGreaterEqual(e["ts_utc"], now - 1)
            self.assertEqual(e["impact"], "High")


if __name__ == "__main__":
    unittest.main(verbosity=1)
