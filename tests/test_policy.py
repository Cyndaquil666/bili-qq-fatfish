import time
import unittest
import uuid
from pathlib import Path

from tests import _bootstrap  # noqa: F401
from fatfish.config import Config
from fatfish.memory import Memory
from fatfish.policy import Policy

TEST_DB = Path("var/test-db")


def make(tmp: str, **overrides):
    raw = {
        "mode": "live",
        "safety": {"live_ack": True, "quiet_hours": [], "max_daily_llm_tokens": 1000},
        "bilibili": {
            "enabled": True,
            "write": {
                "enabled_actions": ["like", "danmaku"],
                "daily_caps": {"like": 2, "danmaku": 1},
                "weekly_caps": {},
                "min_score": {"like": 70, "danmaku": 80},
                "min_gap_minutes": {"like": 0, "danmaku": 0},
            },
        },
        "qq": {
            "enabled": True,
            "groups": ["123"],
            "max_msgs_per_day_per_group": 2,
            "max_msgs_per_day_total": 3,
            "min_gap_seconds": 0,
        },
    }
    for k, v in overrides.items():
        raw[k] = v
    return Config(raw, Path(tmp) / "config.yaml")


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.dir = TEST_DB / uuid.uuid4().hex
        self.dir.mkdir(parents=True)
        self.mem = Memory(self.dir / "fish.db")
        self.cfg = make(str(self.dir))
        self.policy = Policy(self.cfg, self.mem)

    def tearDown(self):
        self.mem.close()

    def test_low_score_blocked(self):
        v = self.policy.check_bili_write("like", "BV1", 60)
        self.assertFalse(v.allowed)
        self.assertIn("低于", v.reason)

    def test_daily_cap(self):
        for _ in range(2):
            v = self.policy.check_bili_write("like", "BV" + str(time.time()), 90)
            self.assertTrue(v.allowed)
            self.mem.record_action("bilibili", "like", "x")
        v = self.policy.check_bili_write("like", "BV2", 90)
        self.assertFalse(v.allowed)
        self.assertIn("额度", v.reason)

    def test_same_video_not_twice(self):
        self.assertTrue(self.policy.check_bili_write("like", "BVx", 90))
        self.mem.record_action("bilibili", "like", "BVx")
        v = self.policy.check_bili_write("like", "BVx", 90)
        self.assertFalse(v.allowed)
        self.assertIn("已经做过", v.reason)

    def test_observe_mode_never_writes(self):
        cfg = make(str(self.dir), mode="observe")
        p = Policy(cfg, self.mem)
        v = p.check_bili_write("like", "BV", 99)
        self.assertTrue(v.allowed)
        self.assertTrue(v.dry_run)
        low = p.check_bili_write("like", "BV2", 10)
        self.assertFalse(low.allowed)  # 门槛仍然生效，observe 不是“什么都记”
        q = p.check_qq_send("123", "这个我不懂")
        self.assertTrue(q.allowed)
        self.assertTrue(q.dry_run)

    def test_qq_allowlist_and_cap(self):
        v = self.policy.check_qq_send("123", "这个我不懂")
        self.assertTrue(v.allowed)
        self.mem.record_action("qq", "qq_msg", target="123")
        self.mem.record_action("qq", "qq_msg", target="123")
        v = self.policy.check_qq_send("123", "再说一句")
        self.assertFalse(v.allowed)
        self.assertIn("够了", v.reason)

    def test_qq_rejects_unknown_group(self):
        v = self.policy.check_qq_send("999", "你好")
        self.assertFalse(v.allowed)
        self.assertIn("白名单", v.reason)

    def test_second_line_of_one_reply_ignores_gap(self):
        cfg = make(str(self.dir))
        cfg.raw["qq"]["min_gap_seconds"] = 600
        p = Policy(cfg, self.mem)
        self.mem.record_action("qq", "qq_msg", target="123")  # first line just sent
        v = p.check_qq_send("123", "第二句", ignore_gap=True)
        self.assertTrue(v.allowed)
        blocked = p.check_qq_send("123", "下一轮")
        self.assertFalse(blocked.allowed)

    def test_bili_observe_does_not_silence_qq(self):
        self.policy.force_observe_bili = True
        v = self.policy.check_bili_write("like", "BV", 99)
        self.assertTrue(v.dry_run)
        q = self.policy.check_qq_send("123", "这个我不懂")
        self.assertTrue(q.allowed)

    def test_minor_mode_blocks_in_policy(self):
        v = self.policy.check_qq_send("123", "叫老公")
        self.assertFalse(v.allowed)
        self.assertIn("风险", v.reason)
        b = self.policy.check_bili_write("danmaku", "BV1", 90, text="想和你约会")
        self.assertFalse(b.allowed)
        self.assertIn("风险", b.reason)

    def test_risk_pause(self):
        self.policy.pause_for_risk(1)
        v = self.policy.check_bili_write("like", "BV", 99)
        self.assertFalse(v.allowed)
        self.assertIn("风控", v.reason)


if __name__ == "__main__":
    unittest.main()


