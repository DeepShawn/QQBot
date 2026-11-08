# -*- coding: utf-8 -*-
"""管理台 sanitize_config：记忆设置允许 0（不限制/永不遗忘），负数夹到 0。"""
import sys
import unittest
from pathlib import Path

BOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT))
sys.path.insert(0, str(BOT / "admin"))

import server  # noqa: E402


def sanitized_ai(values: dict) -> dict:
    cfg = {"plugins": {"ai_chat": dict(values)}}
    out = server.sanitize_config(cfg)
    return out["plugins"]["ai_chat"]


class MemorySanitizeTest(unittest.TestCase):
    def test_zero_is_allowed(self):
        ai = sanitized_ai({"max_history": 0, "history_ttl_minutes": 0})
        self.assertEqual(ai["max_history"], 0)
        self.assertEqual(ai["history_ttl_minutes"], 0)

    def test_negative_clamps_to_zero(self):
        ai = sanitized_ai({"max_history": -5, "history_ttl_minutes": -1})
        self.assertEqual(ai["max_history"], 0)
        self.assertEqual(ai["history_ttl_minutes"], 0)

    def test_upper_bound(self):
        ai = sanitized_ai({"max_history": 99999, "history_ttl_minutes": 10**9})
        self.assertEqual(ai["max_history"], 1000)
        self.assertEqual(ai["history_ttl_minutes"], 525600)

    def test_missing_keeps_defaults(self):
        ai = sanitized_ai({})
        self.assertEqual(ai["max_history"], 8)
        self.assertEqual(ai["history_ttl_minutes"], 30)


if __name__ == "__main__":
    unittest.main()
