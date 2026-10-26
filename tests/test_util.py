import os
import unittest
from datetime import datetime

from tests import _bootstrap  # noqa: F401
from fatfish.util import TZ, in_quiet_hours, in_window, pid_alive, split_messages


class UtilTests(unittest.TestCase):
    def test_window_normal(self):
        dt = datetime(2026, 10, 19, 12, 0, tzinfo=TZ)
        self.assertTrue(in_window(dt, "09:30", "23:30"))
        self.assertFalse(in_window(dt, "13:00", "18:00"))

    def test_window_wraps_midnight(self):
        late = datetime(2026, 10, 19, 23, 45, tzinfo=TZ)
        early = datetime(2026, 10, 19, 7, 0, tzinfo=TZ)
        self.assertTrue(in_window(late, "22:00", "08:00"))
        self.assertTrue(in_window(early, "22:00", "08:00"))
        self.assertFalse(in_window(datetime(2026, 10, 19, 12, 0, tzinfo=TZ), "22:00", "08:00"))

    def test_quiet_hours(self):
        dt = datetime(2026, 10, 19, 3, 0, tzinfo=TZ)
        self.assertTrue(in_quiet_hours(dt, [["00:30", "08:30"]]))
        self.assertFalse(in_quiet_hours(dt, []))

    def test_split_messages(self):
        parts = split_messages("先看榫头松没松，别急着上胶。再看看是不是原装的。", 20)
        self.assertGreaterEqual(len(parts), 1)
        self.assertTrue(all(len(p) <= 20 for p in parts))

    def test_pid_alive(self):
        self.assertTrue(pid_alive(os.getpid()))
        self.assertFalse(pid_alive(99999999))


if __name__ == "__main__":
    unittest.main()
