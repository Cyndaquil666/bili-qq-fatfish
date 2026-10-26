import unittest
import uuid
from pathlib import Path

from tests import _bootstrap  # noqa: F401
from fatfish.config import Config
from fatfish.llm import LLMBudgetExceeded, LLM, parse_json_object
from fatfish.memory import Memory

ROOT = Path(__file__).resolve().parent.parent
TEST_DB = ROOT / "var" / "test-db"


class ParseTests(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(parse_json_object('{"a":1}'), {"a": 1})

    def test_fenced(self):
        self.assertEqual(parse_json_object('```json\n{"a": 2}\n```'), {"a": 2})

    def test_trailing_comma(self):
        self.assertEqual(parse_json_object('{"a": [1,2,], "b": 3,}'), {"a": [1, 2], "b": 3})

    def test_garbage(self):
        with self.assertRaises(ValueError):
            parse_json_object("我想了想，还是算了吧")


class LLMTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tools.mock_llm as mock

        cls.server = mock.start(18082)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        self.dir = TEST_DB / uuid.uuid4().hex
        self.dir.mkdir(parents=True)
        self.mem = Memory(self.dir / "fish.db")

    def tearDown(self):
        self.mem.close()

    def make(self, budget=100000):
        return LLM(
            Config(
                {
                    "llm": {
                        "base_url": "http://127.0.0.1:18082/v1",
                        "api_key": "x",
                        "model": "mock",
                        "timeout": 10,
                        "retries": 0,
                    },
                    "safety": {"max_daily_llm_tokens": budget},
                },
                self.dir / "config.yaml",
            ),
            self.mem,
            _NullLog(),
        )

    def test_json_roundtrip(self):
        import asyncio

        llm = self.make()
        data = asyncio.run(llm.json([{"role": "user", "content": "候选如下：[0] 修椅子"}], tag="pick"))
        self.assertEqual(data["picks"][0]["score"], 88)
        asyncio.run(llm.close())

    def test_budget_stops_second_call(self):
        import asyncio

        llm = self.make(budget=100)
        asyncio.run(llm.json([{"role": "user", "content": "候选如下"}], tag="pick"))
        with self.assertRaises(LLMBudgetExceeded):
            asyncio.run(llm.json([{"role": "user", "content": "候选如下"}], tag="pick"))
        asyncio.run(llm.close())

    def test_unavailable_without_key(self):
        cfg = Config({"llm": {"api_key": ""}}, self.dir / "config.yaml")
        llm = LLM(cfg, self.mem, _NullLog())
        self.assertFalse(llm.available)
        import asyncio

        with self.assertRaises(Exception):
            asyncio.run(llm.chat([{"role": "user", "content": "hi"}]))
        asyncio.run(llm.close())


class _NullLog:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def debug(self, *a, **k):
        pass

    def exception(self, *a, **k):
        pass


if __name__ == "__main__":
    unittest.main()
