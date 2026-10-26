import unittest
import uuid
from pathlib import Path

from tests import _bootstrap  # noqa: F401
from fatfish.memory import Memory
from fatfish.report import daily_report
from fatfish.state import StateStore

ROOT = Path(__file__).resolve().parent.parent
TEST_DB = ROOT / "var" / "test-db"


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.dir = TEST_DB / uuid.uuid4().hex
        self.dir.mkdir(parents=True)
        self.mem = Memory(self.dir / "fish.db")

    def tearDown(self):
        self.mem.close()

    def test_report_contains_both_lives(self):
        self.mem.add_episode(
            "bilibili", "watch", "老师傅修椅子", title="修椅子", ref="BV1", reaction="榫头补得舒服",
            score=91, topics=["手工艺与修复"], meta={"up": "手艺人", "duration": 600, "pick_why": "有过程",
                                                      "evidence": ["拆开时编号"], "how_much": "大半"},
            day="2026-10-19",
        )
        self.mem.record_action("bilibili", "like", "BV1", result="would", dry_run=True, day="2026-10-19")
        self.mem.add_episode("qq", "say", "先看榫头松没松", ref="123", title="测试群", day="2026-10-19")
        self.mem.add_episode("qq", "lurk", "只是哈哈，没必要", ref="123", day="2026-10-19")
        self.mem.set_journal("2026-10-19", "今天看了个修椅子的，舒服。")
        text = daily_report(self.mem, StateStore(self.mem).state, "2026-10-19")
        self.assertIn("修椅子", text)
        self.assertIn("手艺人", text)
        self.assertIn("先看榫头松没松", text)
        self.assertIn("只是哈哈", text)
        self.assertIn("今天看了个修椅子的", text)
        self.assertIn("点赞", text)


if __name__ == "__main__":
    unittest.main()
