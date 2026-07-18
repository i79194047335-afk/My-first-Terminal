"""
Tests for briefing/agent.py и prompt.py (Фаза 7, слой 3).

Без реального DeepSeek: проверяем разбор ответа (снятие markdown-обёртки,
ошибки) и что промпт содержит ключевые требования новой структуры.

Run:  python3 tests/test_briefing_agent.py
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from briefing import agent, prompt


class TestJsonParse(unittest.TestCase):

    def test_plain_json(self):
        self.assertEqual(agent._parse_json('{"a": 1}'), {"a": 1})

    def test_strips_markdown_fence(self):
        raw = '```json\n{"pairs": {}}\n```'
        self.assertEqual(agent._parse_json(raw), {"pairs": {}})

    def test_strips_bare_fence(self):
        self.assertEqual(agent._parse_json('```\n{"x": 2}\n```'), {"x": 2})

    def test_bad_json_raises(self):
        with self.assertRaises(agent.AgentError):
            agent._parse_json("не json вовсе")


class TestPromptStructure(unittest.TestCase):

    def test_system_prompt_demands_two_sides(self):
        """Системный промпт требует раздельно консенсус и мысль модели."""
        p = prompt.build_system_prompt("london", "Лондонской")
        self.assertIn("consensus_direction", p)
        self.assertIn("consensus_view", p)
        self.assertIn("deepseek_view", p)
        self.assertIn("КОНСЕНСУС", p)
        self.assertIn("НЕ давать торговый совет", p)

    def test_user_prompt_has_data_and_utc5(self):
        """Пользовательский промпт вкладывает технику, ленту и календарь."""
        technical = {"symbols": {"EUR/USD": {
            "price": 1.1, "day_high": 1.11, "day_low": 1.09,
            "day_range_pips": 20, "htf_trend": "trend_up",
            "micro_trend": "flat", "range_pos": 0.5, "volatility": "low"}}}
        news = [{"source": "FXStreet", "title": "USD strong",
                 "time_display": "18.07 03:00 UTC+5", "ts": 1}]
        diag = [{"source": "FXStreet", "raw": 30, "relevant": 4,
                 "ok": True, "error": None}]
        cal = [{"time_display": "18.07 15:30 UTC+5", "currency": "USD",
                "event": "CPI"}]
        p = prompt.build_user_prompt(technical, news, diag, cal, assessments={})
        self.assertIn("EUR/USD", p)
        self.assertIn("FXStreet", p)
        self.assertIn("UTC+5", p)
        self.assertIn("CPI", p)

    def test_assessments_included(self):
        """Блоки самооценки попадают в промпт (если есть)."""
        p = prompt.build_user_prompt(
            {"symbols": {}}, [], [], [],
            assessments={"EUR/USD": "[Проверка прошлого прогноза: сбылось]"})
        self.assertIn("ПРОВЕРКА ПРОШЛЫХ ПРОГНОЗОВ", p)
        self.assertIn("сбылось", p)


if __name__ == "__main__":
    unittest.main(verbosity=1)
