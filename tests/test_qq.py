import unittest

from tests import _bootstrap  # noqa: F401
from fatfish.qq import normalize_event


def event(**kw):
    base = {
        "post_type": "message",
        "message_type": "group",
        "self_id": 10000,
        "user_id": 20000,
        "group_id": 123456,
        "message": [{"type": "text", "data": {"text": "这个榫头怎么补"}}],
        "sender": {"card": "老王", "nickname": "老王"},
        "time": 1770000000,
        "message_id": 5,
    }
    base.update(kw)
    return base


class QQNormalizeTests(unittest.TestCase):
    def test_basic_message(self):
        m = normalize_event(event())
        self.assertIsNotNone(m)
        self.assertEqual(m["group_id"], "123456")
        self.assertEqual(m["user_id"], "20000")
        self.assertEqual(m["name"], "老王")
        self.assertEqual(m["text"], "这个榫头怎么补")
        self.assertFalse(m["direct_at"])

    def test_at_self(self):
        e = event(message=[{"type": "text", "data": {"text": "在吗"}}, {"type": "at", "data": {"qq": "10000"}}])
        m = normalize_event(e)
        self.assertTrue(m["direct_at"])

    def test_at_all_not_self(self):
        e = event(message=[{"type": "text", "data": {"text": "通知"}}, {"type": "at", "data": {"qq": "all"}}])
        m = normalize_event(e)
        self.assertFalse(m["direct_at"])
        self.assertIn("@全体成员", m["text"])

    def test_ignores_self(self):
        self.assertIsNone(normalize_event(event(user_id=10000)))

    def test_private_message_is_accepted(self):
        m = normalize_event(event(message_type="private"))
        self.assertIsNotNone(m)
        self.assertTrue(m["private"])
        self.assertEqual(m["group_id"], "private:20000")
        self.assertTrue(m["direct_at"])

    def test_media_segments_become_placeholders(self):
        e = event(
            message=[
                {"type": "image", "data": {"file": "x.jpg"}},
                {"type": "text", "data": {"text": "看图"}},
                {"type": "face", "data": {"id": "1"}},
            ]
        )
        m = normalize_event(e)
        self.assertIn("[图片]", m["text"])
        self.assertIn("[表情]", m["text"])
        self.assertEqual(m["images"], ["x.jpg"])


if __name__ == "__main__":
    unittest.main()
