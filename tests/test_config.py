import os
import unittest
import uuid
from pathlib import Path

from tests import _bootstrap  # noqa: F401
from fatfish.config import Config, ConfigError

ROOT = Path(__file__).resolve().parent.parent
TEST_DB = ROOT / "var" / "test-db"


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.dir = TEST_DB / uuid.uuid4().hex
        self.dir.mkdir(parents=True)

    def test_defaults_and_deep_merge(self):
        cfg = Config({"qq": {"groups": ["1"]}}, self.dir / "config.yaml")
        self.assertEqual(cfg.get("mode"), "observe")
        self.assertEqual(cfg.get("qq.groups"), ["1"])
        self.assertEqual(cfg.get("qq.max_msgs_per_day_total"), 200)
        self.assertEqual(cfg.get("bilibili.write.daily_caps.like"), 3)

    def test_secret_env(self):
        os.environ["FISH_TEST_KEY"] = "abc123"
        try:
            cfg = Config({"llm": {"api_key": "env:FISH_TEST_KEY"}}, self.dir / "config.yaml")
            self.assertEqual(cfg.secret("llm.api_key"), "abc123")
        finally:
            os.environ.pop("FISH_TEST_KEY", None)

    def test_secret_plain(self):
        cfg = Config({"llm": {"api_key": "sk-plain"}}, self.dir / "config.yaml")
        self.assertEqual(cfg.secret("llm.api_key"), "sk-plain")

    def test_live_requires_ack(self):
        cfg = Config({"mode": "live", "safety": {"live_ack": False}}, self.dir / "config.yaml")
        self.assertFalse(cfg.live)
        cfg2 = Config({"mode": "live", "safety": {"live_ack": True}, "llm": {"api_key": "x"}}, self.dir / "config.yaml")
        self.assertTrue(cfg2.live)

    def test_path_resolves_relative_to_config(self):
        cfg = Config({"paths": {"db_file": "var/x.db"}}, self.dir / "config.yaml")
        self.assertEqual(cfg.path("paths.db_file"), self.dir / "var" / "x.db")

    def test_validate_mentions_missing_key(self):
        cfg = Config({"mode": "live", "safety": {"live_ack": True}, "llm": {"api_key": ""}}, self.dir / "config.yaml")
        self.assertTrue(any("LLM" in p for p in cfg.validate()))

    def test_missing_file_raises(self):
        with self.assertRaises(ConfigError):
            Config.load(self.dir / "nope.yaml")


if __name__ == "__main__":
    unittest.main()
