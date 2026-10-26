import unittest
import uuid
from pathlib import Path

from tests import _bootstrap  # noqa: F401
from fatfish.memory import Memory
from fatfish.state import StateStore

ROOT = Path(__file__).resolve().parent.parent
TEST_DB = ROOT / "var" / "test-db"


class StateTests(unittest.TestCase):
    def setUp(self):
        self.dir = TEST_DB / uuid.uuid4().hex
        self.dir.mkdir(parents=True)
        self.mem = Memory(self.dir / "fish.db")
        self.store = StateStore(self.mem)
        self.store.state.energy = 80
        self.store.state.sociability = 60
        self.store.state.mood = "平静"

    def tearDown(self):
        self.mem.close()

    def test_high_score_energises(self):
        self.store.after_watch(92, interacted=True)
        self.assertEqual(self.store.state.mood, "上头")
        self.assertLess(self.store.state.energy, 80)

    def test_speaking_drains_social_battery(self):
        before = self.store.state.sociability
        self.store.after_speak(30)
        self.assertLess(self.store.state.sociability, before)

    def test_state_persists(self):
        self.store.state.mood = "有点懒"
        self.store.save()
        again = StateStore(self.mem)
        self.assertEqual(again.state.mood, "有点懒")

    def test_bias_is_bounded(self):
        self.store.state.energy = 10
        self.store.state.mood = "有点丧"
        self.assertGreater(self.store.state.bili_bias(), 0)
        self.assertGreater(self.store.state.qq_bias(), 0)


if __name__ == "__main__":
    unittest.main()
