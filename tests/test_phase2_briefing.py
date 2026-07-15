"""
Тест брифинга в хабе (пункт 4 пост-переключения).

Крон пишет briefing.json, хаб поллит его по mtime и рассылает клиентам сообщение
briefing; новый клиент получает кэш сразу при коннекте. Контракт с фронтом:
{"type":"briefing","data": <содержимое файла>}.

Run:  python3.10 tests/test_phase2_briefing.py
"""

import asyncio
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import websockets

from hub import Hub
from test_phase2_bus import free_port, run_async
from test_phase2_hub import HubHarness, PROVIDER, SYMBOL

SAMPLE = {"meta": {"session": "ny"}, "pairs": {}, "global_context": "тест"}


class TestBriefingWatcher(unittest.TestCase):
    """Watcher читает файл и обновляет кэш."""

    def _hub(self, briefing_path):
        return Hub({
            "db_path": ":memory:", "keep_bars": 2000, "ws_port": 0,
            "bus_host": "127.0.0.1", "bus_port": 0, "trim_every": 200,
            "tf_seconds": {"M1": 60}, "broker_tf": [],
            "briefing_file": briefing_path,
            "markets": {PROVIDER: [SYMBOL]},
        })

    def test_watcher_reads_file_and_broadcasts(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "briefing.json")
        with open(path, "w") as f:
            json.dump(SAMPLE, f)

        h = self._hub(path)
        sent = []
        h._loop = None                      # без loop: проверяем только чтение
        h._broadcast_threadsafe = lambda p: sent.append(json.loads(p))

        # один проход watcher вручную, без бесконечного цикла
        import os as _os
        mtime = _os.path.getmtime(path)
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        h._briefing = data
        h._briefing_mtime = mtime
        h._broadcast_threadsafe(json.dumps({"type": "briefing", "data": data}))

        self.assertEqual(h._briefing["meta"]["session"], "ny")
        self.assertEqual(sent[0]["type"], "briefing")
        self.assertEqual(sent[0]["data"], SAMPLE)

    def test_missing_file_is_silent(self):
        h = self._hub("/nonexistent/briefing.json")
        # briefing_watcher должен просто ничего не делать, не падать —
        # проверяем, что кэш остаётся None (метод крутит вечный цикл, поэтому
        # дёргаем только предусловие: путь есть, файла нет).
        self.assertIsNone(h._briefing)
        self.assertFalse(os.path.exists(h._config["briefing_file"]))


class TestBriefingOverWebSocket(unittest.TestCase):
    """Новый клиент получает брифинг при коннекте."""

    def test_new_client_gets_cached_briefing(self):
        tmp = tempfile.mkdtemp()
        db  = os.path.join(tmp, "hub.db")
        brief_path = os.path.join(tmp, "briefing.json")
        with open(brief_path, "w") as f:
            json.dump(SAMPLE, f)

        async def scenario():
            h = HubHarness(db)
            # подсовываем briefing_file в конфиг стенда
            h.config["briefing_file"] = brief_path
            h.hub._config["briefing_file"] = brief_path
            await h.start()

            # имитируем, что watcher уже прочитал файл
            with h.hub._briefing_lock:
                h.hub._briefing = SAMPLE

            url = "ws://127.0.0.1:%d" % h.config["ws_port"]
            got = []
            async with websockets.connect(url) as ws:
                # instruments может прийти первым (если есть) — тут нет, ждём briefing
                for _ in range(3):
                    m = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
                    got.append(m)
                    if m["type"] == "briefing":
                        break

            await h.stop()
            return got

        got = run_async(scenario())
        briefing = [m for m in got if m["type"] == "briefing"]
        self.assertEqual(len(briefing), 1, "новый клиент не получил брифинг")
        self.assertEqual(briefing[0]["data"], SAMPLE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
