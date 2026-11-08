# -*- coding: utf-8 -*-
"""Config 热加载：不依赖 stat（proot 下可能 ENOSYS），按内容摘要判断变化。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import Config  # noqa: E402


class ConfigReloadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump({"bot": {"name": "A"}}, self.tmp)
        self.tmp.close()
        self.cfg = Config(self.tmp.name)
        self.cfg.load()

    def tearDown(self):
        Path(self.tmp.name).unlink(missing_ok=True)

    def _write(self, name):
        with open(self.tmp.name, "w", encoding="utf-8") as f:
            json.dump({"bot": {"name": name}}, f)

    def test_initial_load_and_unchanged(self):
        self.assertEqual(self.cfg.get("bot", "name"), "A")
        self.assertFalse(self.cfg.reload_if_changed())

    def test_reload_on_change(self):
        self._write("B")
        self.assertTrue(self.cfg.reload_if_changed())
        self.assertEqual(self.cfg.get("bot", "name"), "B")
        self.assertFalse(self.cfg.reload_if_changed())

    def test_reload_without_stat_call(self):
        # 人为让 path.stat 抛 ENOSYS，热加载仍应工作（模拟 proot）
        orig = Path.stat

        def broken(self, *a, **k):
            raise OSError(38, "Function not implemented")

        Path.stat = broken
        try:
            self._write("C")
            self.assertTrue(self.cfg.reload_if_changed())
            self.assertEqual(self.cfg.get("bot", "name"), "C")
        finally:
            Path.stat = orig

    def test_broken_json_keeps_old_config(self):
        self._write("B")
        self.assertTrue(self.cfg.reload_if_changed())
        Path(self.tmp.name).write_text("{bad json", encoding="utf-8")
        self.assertFalse(self.cfg.reload_if_changed())
        self.assertEqual(self.cfg.get("bot", "name"), "B")


if __name__ == "__main__":
    unittest.main()
