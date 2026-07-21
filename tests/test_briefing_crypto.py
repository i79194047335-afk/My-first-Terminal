"""Крипта в брифинге: фильтр новостей и отбор в промпт.

Запуск: python3.10 tests/test_briefing_crypto.py

Главная ловушка — короткие тикеры. Поиск подстрокой находит "ETH" внутри
"Hegseth", "SEC" внутри "Secretary", "SOL" внутри "solar". Живая проверка
2026-07-22 дала 85 ложных крипто-совпадений на 174 новостях: заголовки про
нефть, Apple и немецкий инвест-климат помечались как крипта, и модель
получала мусор вместо ленты.

Второе, что здесь защищается, — доля крипты в промпте. Форекс-фиды идут
первыми в списке источников, и простое [:40] отдало бы им всё место.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from briefing.prompt import build_user_prompt
from briefing.sources import _CRYPTO_RE, _FOREX_RE, _VENUE_RE


class TestKeywordBoundaries(unittest.TestCase):
    """Тикеры ищутся как слова, а не как подстроки."""

    def test_short_tickers_not_matched_inside_words(self):
        """ETH/SEC/SOL не срабатывают внутри обычных слов.

        Каждый пример взят из реальной ленты 2026-07-22.
        """
        for text in ("secretary of war hegseth needs more money",
                     "solar panel makers rally on subsidy news",
                     "the ethics committee approved the measure"):
            self.assertFalse(_CRYPTO_RE.search(text),
                             "ложное срабатывание на %r" % text)

    def test_real_tickers_matched(self):
        """Настоящие упоминания активов ловятся."""
        for text in ("eth breaks $4000 on etf inflows",
                     "sol/usd forecast for the week",
                     "bitcoin rallies after clarity act progress",
                     "zcash privacy upgrade ships"):
            self.assertTrue(_CRYPTO_RE.search(text),
                            "пропущено: %r" % text)

    def test_venue_matched(self):
        """Упоминание нашей биржи ловится отдельно от общей крипты."""
        self.assertTrue(_VENUE_RE.search("lighter exchange reports outage"))
        self.assertTrue(_VENUE_RE.search("zklighter mainnet upgrade"))
        self.assertFalse(_VENUE_RE.search("bitcoin rallies"))

    def test_forex_still_works(self):
        """Форекс-фильтр не сломан добавлением крипты."""
        self.assertTrue(_FOREX_RE.search("ecb holds rates, euro slips"))
        self.assertFalse(_FOREX_RE.search("bitcoin miners expand capacity"))


def _news(title, **flags):
    """Собрать запись новости с нужными флагами.

    Args:
        title: Заголовок.
        flags: forex/crypto/venue.

    Returns:
        Dict записи, как его отдаёт sources.fetch_news().
    """
    item = {"source": "T", "title": title, "summary": "", "link": "",
            "ts": 1784500000, "time_display": "22.07 00:00 UTC+5",
            "forex": False, "crypto": False, "venue": False}
    item.update(flags)
    return item


class TestPromptSelection(unittest.TestCase):
    """Что из ленты доезжает до модели."""

    def _prompt(self, items):
        """Собрать пользовательский промпт на заданной ленте.

        Args:
            items: Список новостей.

        Returns:
            Строка промпта.
        """
        return build_user_prompt(
            technical={"symbols": {}, "session": {}},
            news_items=items,
            news_diag=[{"source": "T", "raw": len(items),
                        "relevant": len(items), "ok": True, "error": None}],
            calendar=[], assessments=None)

    def test_crypto_reaches_prompt_despite_forex_flood(self):
        """Крипта доезжает, даже когда форекса вчетверо больше.

        Простое [:40] отдало бы всё место форексу: его фиды идут первыми в
        списке источников, и до крипты очередь не дошла бы.
        """
        items = ([_news("forex %d" % i, forex=True) for i in range(60)]
                 + [_news("btc news %d" % i, crypto=True) for i in range(15)])
        prompt = self._prompt(items)
        self.assertIn("btc news 0", prompt)
        self.assertIn("[крипта]", prompt)

    def test_venue_news_never_dropped(self):
        """Новость про биржу попадает в промпт вне всяких квот.

        Она бьёт по всем нашим крипто-инструментам разом — потерять её
        из-за переполнения ленты нельзя.
        """
        items = ([_news("forex %d" % i, forex=True) for i in range(60)]
                 + [_news("crypto %d" % i, crypto=True) for i in range(60)]
                 + [_news("lighter outage", venue=True)])
        prompt = self._prompt(items)
        self.assertIn("lighter outage", prompt)
        self.assertIn("[БИРЖА LIGHTER]", prompt)

    def test_unflagged_news_not_lost(self):
        """Новость без флагов не теряется.

        Так выглядят записи из старого кэша или от стороннего вызова: до
        появления категорий они попадали в промпт наравне со всеми.
        """
        prompt = self._prompt([_news("plain headline")])
        self.assertIn("plain headline", prompt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
