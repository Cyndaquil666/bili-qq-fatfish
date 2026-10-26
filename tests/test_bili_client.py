import json
import unittest
import uuid
from pathlib import Path

from tests import _bootstrap  # noqa: F401
from fatfish.bilibili import BiliClient, _duration_seconds, _extract_ai
from fatfish.config import Config
from fatfish.memory import Memory
from fatfish.persona import Persona

ROOT = Path(__file__).resolve().parent.parent
TEST_DB = ROOT / "var" / "test-db"


class BiliClientTests(unittest.TestCase):
    def setUp(self):
        self.dir = TEST_DB / uuid.uuid4().hex
        self.dir.mkdir(parents=True)
        self.mem = Memory(self.dir / "fish.db")
        self.persona = Persona.load(ROOT / "persona.example.yaml")

    def tearDown(self):
        self.mem.close()

    def test_cookie_file_loads_credential(self):
        cookie = self.dir / "bili.json"
        cookie.write_text(
            json.dumps(
                {"sessdata": "s", "bili_jct": "j", "buvid3": "b", "dedeuserid": "12345", "ac_time_value": "a"}
            ),
            encoding="utf-8",
        )
        cfg = Config({"bilibili": {"enabled": True, "cookie_file": str(cookie)}}, self.dir / "config.yaml")
        client = BiliClient(cfg, self.mem, self.persona, _NullLog())
        self.assertTrue(client.logged_in)
        self.assertEqual(client.uid, 12345)

    def test_without_cookie_is_read_only(self):
        cfg = Config({"bilibili": {"enabled": True, "cookie_file": str(self.dir / "none.json")}}, self.dir / "config.yaml")
        client = BiliClient(cfg, self.mem, self.persona, _NullLog())
        self.assertFalse(client.logged_in)

    def test_duration_parser(self):
        self.assertEqual(_duration_seconds("1:02:03"), 3723)
        self.assertEqual(_duration_seconds("12:34"), 754)
        self.assertEqual(_duration_seconds(90), 90)

    def test_item_normalisation_strips_search_markup(self):
        cfg = Config({"bilibili": {"enabled": True}}, self.dir / "config.yaml")
        client = BiliClient(cfg, self.mem, self.persona, _NullLog())
        cand = client._from_item(
            {
                "bvid": "BV1",
                "title": '修<em class="keyword">椅子</em>',
                "author": "手艺人",
                "mid": 9,
                "duration": "12:34",
                "play": 10000,
                "description": "榫卯",
                "tag": "木工,修复",
            },
            "搜索",
        )
        self.assertEqual(cand["title"], "修椅子")
        self.assertEqual(cand["duration"], 754)
        self.assertEqual(cand["tags"], ["木工", "修复"])

    def test_ai_summary_extraction(self):
        text = _extract_ai({"result": {"summary": "这是一个关于修复的总结。", "outline": [{"text": "过程"}]}})
        self.assertIn("修复", text)


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
