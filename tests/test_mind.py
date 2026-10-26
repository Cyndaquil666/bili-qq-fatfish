import unittest
import uuid
from pathlib import Path

from tests import _bootstrap  # noqa: F401
from fatfish.config import Config
from fatfish.memory import Memory
from fatfish.mind import Brain
from fatfish.persona import Persona
from fatfish.state import StateStore

ROOT = Path(__file__).resolve().parent.parent
TEST_DB = ROOT / "var" / "test-db"


class FakeLLM:
    available = True

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def json(self, messages, tag="", **kwargs):
        self.calls.append(tag)
        return self.responses[tag]

    async def chat(self, messages, tag="", **kwargs):
        return self.responses.get(tag, "今天还行。")

    async def close(self):
        pass


def candidate(i=0, **kw):
    c = {
        "bvid": f"BV{i}",
        "title": "独立动画短片：夜路",
        "up": "小鱼干",
        "up_mid": 1,
        "duration": 600,
        "view": 10000,
        "like": 900,
        "reply": 10,
        "danmaku": 5,
        "pubdate": 0,
        "desc": "逐帧手书，讲一个女孩走夜路回家",
        "tags": ["动画"],
        "reason": "",
        "source": "推荐",
    }
    c.update(kw)
    return c


class BrainTests(unittest.TestCase):
    def setUp(self):
        self.dir = TEST_DB / uuid.uuid4().hex
        self.dir.mkdir(parents=True)
        self.mem = Memory(self.dir / "fish.db")
        self.cfg = Config(
            {
                "llm": {"api_key": "x"},
                "bilibili": {"browse": {"min_pick_score": 56, "exploration_rate": 0}},
                "qq": {"max_chars_per_msg": 58, "max_parts": 2, "min_confidence": 0.75, "min_confidence_at": 0.60},
                "safety": {"quiet_hours": []},
            },
            self.dir / "config.yaml",
        )
        self.persona = Persona.load(ROOT / "persona.example.yaml")
        self.persona.seed_interests(self.mem)
        self.state = StateStore(self.mem)
        self.state.state.mood = "平静"
        self.state.state.energy = 80

    def tearDown(self):
        self.mem.close()

    def brain(self, responses):
        return Brain(self.cfg, self.persona, self.mem, self.state, FakeLLM(responses), _NullLog())

    def test_pick_filters_low_scores(self):
        b = self.brain({"pick": {"picks": [{"i": 0, "score": 90, "why": "有过程"}, {"i": 1, "score": 20, "why": "水"}]}})
        picks = _run(b.pick([candidate(0), candidate(1, title="震惊！必看")]))
        self.assertEqual(len(picks), 1)
        self.assertEqual(picks[0]["bvid"], "BV0")
        self.assertEqual(picks[0]["pick_score"], 90)

    def test_pick_can_refuse_everything(self):
        b = self.brain({"pick": {"picks": [], "skip_reason": "都不想看"}})
        self.assertEqual(_run(b.pick([candidate(0)])), [])

    def test_experience_caps_interest_without_evidence(self):
        b = self.brain(
            {
                "experience": {
                    "what_it_is": "一个独立动画短片",
                    "how_much": "一半",
                    "genuine_interest": 95,
                    "reaction": "转场很顺",
                    "evidence": [],
                    "topics": ["动画与手书"],
                    "interact": {"like": True, "danmaku": "这个转场我逐帧看"},
                    "interact_reason": "好看",
                }
            }
        )
        exp = _run(b.experience({"title": "x", "up": "u", "duration": 600, "desc": "", "tags": [], "comments": [], "danmaku": []}))
        self.assertLessEqual(exp["genuine_interest"], 60)
        self.assertEqual(exp["interact"]["danmaku"], "这个转场我逐帧看")

    def test_group_stays_silent_when_unsure(self):
        b = self.brain({"group": {"speak": True, "confidence": 0.2, "why": "随便", "messages": ["确实"]}})
        d = _run(b.decide_group("1", "测试群", [_msg()]))
        self.assertFalse(d["speak"])

    def test_group_speaks_with_confidence_and_sanitizes(self):
        b = self.brain({"group": {"speak": True, "confidence": 0.9, "why": "懂", "messages": ["先看电源 @全体成员"]}})
        d = _run(b.decide_group("1", "测试群", [_msg()]))
        self.assertTrue(d["speak"])
        self.assertNotIn("@全体成员", d["messages"][0])

    def test_special_user_gets_a_lower_bar(self):
        # 0.5 is below the normal 0.58 floor but above the special 0.42 floor.
        b = self.brain({"group": {"speak": True, "confidence": 0.65, "why": "熟人", "messages": ["？咋了"]}})
        d = _run(b.decide_group("1", "测试群", [_msg(name="主人")]))
        self.assertTrue(d["speak"])
        normal = _run(b.decide_group("1", "测试群", [_msg(name="路人")]))
        self.assertFalse(normal["speak"])

    def test_group_drops_cringe_comeback(self):
        b = self.brain({"group": {"speak": True, "confidence": 0.95, "why": "x", "messages": ["你才糖"]}})
        d = _run(b.decide_group("1", "测试群", [_msg()]))
        self.assertFalse(d["speak"])

    def test_group_drops_instruction_echo(self):
        b = self.brain({"group": {"speak": True, "confidence": 0.95, "why": "x", "messages": ["作为AI我已经完成修改"]}})
        d = _run(b.decide_group("1", "测试群", [_msg()]))
        self.assertFalse(d["speak"])


class _NullLog:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def debug(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass

    def exception(self, *a, **k):
        pass


def _msg(name="老王"):
    return {"time": "12:00", "name": name, "user_id": "2", "text": "这个转场是怎么做的", "direct_at": False}


def _run(coro):
    import asyncio

    return asyncio.run(coro)


if __name__ == "__main__":
    unittest.main()


