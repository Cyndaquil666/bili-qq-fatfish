import sqlite3
import unittest
import uuid
from pathlib import Path

from tests import _bootstrap  # noqa: F401
from fatfish.memory import Memory

ROOT = Path(__file__).resolve().parent.parent
TEST_DB = ROOT / "var" / "test-db"


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.dir = TEST_DB / uuid.uuid4().hex
        self.dir.mkdir(parents=True)

    def test_kv_roundtrip(self):
        mem = Memory(self.dir / "fish.db")
        mem.kv_set("x", {"a": [1, 2]})
        self.assertEqual(mem.kv_get("x"), {"a": [1, 2]})
        mem.close()

    def test_my_comments_roundtrip(self):
        mem = Memory(self.dir / "fish.db")
        mem.add_my_comment("BV1xx", 123, 456, "这个转场不错")
        rows = mem.my_comments()
        self.assertEqual(rows[0]["rpid"], 456)
        self.assertEqual(rows[0]["text"], "这个转场不错")
        mem.mark_comment_checked(456, 999)
        self.assertEqual(mem.my_comments()[0]["checked_ts"], 999)
        mem.close()
        mem = Memory(self.dir / "fish.db")
        mem.record_action("bilibili", "like", "BV1")
        mem.record_action("bilibili", "like", "BV2", dry_run=True)
        self.assertEqual(mem.count_actions("like", channel="bilibili"), 1)
        self.assertEqual(mem.count_actions("like", channel="bilibili", include_dry=True), 2)
        mem.close()

    def test_episodes_and_journal(self):
        mem = Memory(self.dir / "fish.db")
        mem.add_episode("bilibili", "watch", "修椅子", title="椅子", ref="BV1", score=90, topics=["木工"])
        rows = mem.recent_episodes()
        self.assertEqual(rows[0]["topics"], ["木工"])
        mem.set_journal("2026-10-19", "今天看了修椅子。")
        self.assertIn("修椅子", mem.get_journal("2026-10-19"))
        mem.close()

    def test_group_message_and_peer(self):
        mem = Memory(self.dir / "fish.db")
        mem.add_group_msg("1", "2", "老王", "在吗", direct_at=True)
        rows = mem.recent_group_msgs("1")
        self.assertTrue(rows[0]["direct_at"])
        mem.touch_peer("qq", "2", "老王", delta=0.1)
        self.assertEqual(len(mem.peers("qq")), 1)
        mem.close()

    def test_migrates_old_group_table(self):
        path = self.dir / "old.db"
        con = sqlite3.connect(path)
        con.execute(
            "CREATE TABLE group_msgs (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, group_id TEXT, "
            "user_id TEXT, name TEXT, text TEXT)"
        )
        con.commit()
        con.close()
        mem = Memory(path)
        cols = {r[1] for r in mem.db.execute("PRAGMA table_info(group_msgs)")}
        self.assertIn("direct_at", cols)
        mem.add_group_msg("1", "2", "a", "hi", direct_at=True)
        mem.close()


if __name__ == "__main__":
    unittest.main()
