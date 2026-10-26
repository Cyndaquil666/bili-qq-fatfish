import asyncio
import unittest
import uuid
from pathlib import Path

from tests import _bootstrap  # noqa: F401
from fatfish.config import Config
from fatfish.memory import Memory
from fatfish.policy import Policy
from fatfish.runtime import Runtime
from fatfish.state import StateStore

ROOT = Path(__file__).resolve().parent.parent
TEST_DB = ROOT / "var" / "test-db"


class FakeBili:
    logged_in = True

    def __init__(self):
        self.calls = []

    async def like(self, bvid):
        self.calls.append("like")
        return {"status": "ok", "detail": ""}

    async def coin(self, bvid, num=1):
        self.calls.append("coin")
        return {"status": "ok", "detail": ""}

    async def favorite(self, bvid, media_id):
        self.calls.append("favorite")
        return {"status": "ok", "detail": ""}

    async def danmaku(self, bvid, cid, text, at=0.0):
        self.calls.append(("danmaku", text))
        return {"status": "ok", "detail": ""}

    async def send_comment(self, bvid, aid, text):
        self.calls.append("comment")
        return {"status": "ok", "detail": ""}

    async def follow(self, up_mid):
        self.calls.append("follow")
        return {"status": "ok", "detail": ""}


class ActionTests(unittest.TestCase):
    def setUp(self):
        self.dir = TEST_DB / uuid.uuid4().hex
        self.dir.mkdir(parents=True)
        self.mem = Memory(self.dir / "fish.db")
        self.cfg = Config(
            {
                "mode": "live",
                "safety": {"live_ack": True, "quiet_hours": []},
                "bilibili": {
                    "enabled": True,
                    "favorite_media_id": 0,
                    "write": {
                        "enabled_actions": ["like", "danmaku"],
                        "daily_caps": {"like": 2, "danmaku": 2},
                        "weekly_caps": {},
                        "min_score": {"like": 70, "danmaku": 80},
                        "min_gap_minutes": {"like": 0, "danmaku": 0},
                    },
                },
                "qq": {"enabled": False},
            },
            self.dir / "config.yaml",
        )
        self.rt = Runtime.__new__(Runtime)
        self.rt.cfg = self.cfg
        self.rt.memory = self.mem
        self.rt.policy = Policy(self.cfg, self.mem)
        self.rt.bili = FakeBili()
        self.rt.state = StateStore(self.mem)
        self.rt.log = _NullLog()
        self.detail = {
            "bvid": "BV1",
            "cid": 11,
            "aid": 22,
            "up_mid": 33,
            "up": "手艺人",
            "title": "修椅子",
            "duration": 600,
            "danmaku": [],
        }

    def tearDown(self):
        self.mem.close()

    def test_executes_allowed_actions(self):
        exp = {"genuine_interest": 90, "interact": {"like": True, "danmaku": "这个榫头补得真舒服"}, "interact_reason": "好"}
        done = asyncio.run(self.rt._apply_interactions(self.detail, exp))
        self.assertEqual(done, ["like", "danmaku"])
        self.assertIn("like", self.rt.bili.calls)
        self.assertTrue(any(isinstance(c, tuple) and c[0] == "danmaku" for c in self.rt.bili.calls))
        rows = self.mem.actions_for_day(__import__("time").strftime("%Y-%m-%d"))
        self.assertTrue(any(r["action"] == "like" and r["result"] == "ok" for r in rows))

    def test_low_score_does_not_call_bili(self):
        exp = {"genuine_interest": 10, "interact": {"like": True}, "interact_reason": "标题好看"}
        done = asyncio.run(self.rt._apply_interactions(self.detail, exp))
        self.assertEqual(done, [])
        self.assertEqual(self.rt.bili.calls, [])

    def test_observe_records_would_without_calling(self):
        self.rt.policy.force_observe_bili = True
        exp = {"genuine_interest": 90, "interact": {"like": True}, "interact_reason": "好"}
        done = asyncio.run(self.rt._apply_interactions(self.detail, exp))
        self.assertEqual(done, [])
        self.assertEqual(self.rt.bili.calls, [])
        rows = self.mem.actions_for_day(__import__("time").strftime("%Y-%m-%d"))
        self.assertTrue(any(r["action"] == "like" and r["result"] == "would" for r in rows))

    def test_minor_mode_blocks_romantic_text(self):
        exp = {"genuine_interest": 90, "interact": {"danmaku": "叫老公"}, "interact_reason": "测试"}
        done = asyncio.run(self.rt._apply_interactions(self.detail, exp))
        self.assertEqual(done, [])
        self.assertEqual(self.rt.bili.calls, [])
        rows = self.mem.actions_for_day(__import__("time").strftime("%Y-%m-%d"))
        self.assertTrue(any(r["action"] == "danmaku" and r["result"] == "blocked" for r in rows))

    def test_instance_lock(self):
        import os

        rt = Runtime.__new__(Runtime)
        rt.data_dir = self.dir
        rt.lock_path = self.dir / "fish.lock"
        rt.acquire_instance_lock()
        self.assertTrue(rt.lock_path.exists())
        rt.release_instance_lock()
        self.assertFalse(rt.lock_path.exists())
        # A live foreign PID (the test runner's parent) must block a second run.
        rt.lock_path.write_text(str(os.getppid()), encoding="utf-8")
        with self.assertRaises(RuntimeError):
            rt.acquire_instance_lock()


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
