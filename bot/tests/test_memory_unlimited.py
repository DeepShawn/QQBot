"""对话记忆：max_history / history_ttl_minutes 的 0、负数语义（不裁剪 / 不过期）。

离线测试，不启动机器人、不联网。对应用户要求：
- 轮数不限制（max_history<=0 保留全部历史，换模型也记得）；
- 可以选择不遗忘（history_ttl_minutes<=0 永不过期）；
- 正常配置（8 轮 / 30 分钟）仍然照旧裁剪、过期。
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

BOT_DIR = Path(__file__).resolve().parent.parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from plugins import ai_chat  # noqa: E402


class _FakeLog:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass

    def exception(self, *args, **kwargs):
        pass


class FakeCtx:
    """只提供 ai_chat 记忆逻辑用得到的上下文。"""

    def __init__(self, cfg: dict, root: str):
        self._cfg = cfg
        self.data_dir = Path(root)
        self.log = _FakeLog()
        self.self_id = 10000
        self.bot_name = "测试机器人"

    def plugin_cfg(self, name: str) -> dict:
        return self._cfg

    def format_text(self, text, event, group_id=None) -> str:
        return text


class MemoryBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.key = "private:10001"

    def _plugin(self, cfg: dict):
        # 每次给一份新 dict：模拟改配置（换模型）不会重建插件实例。
        return ai_chat.Plugin(FakeCtx(cfg, self._tmp.name))

    @staticmethod
    def _rounds(plugin, key, count, prefix="轮"):
        for i in range(count):
            plugin._remember(key, "user", f"{prefix}{i}-问")
            plugin._remember(key, "assistant", f"{prefix}{i}-答")

    @staticmethod
    def _pairs(messages):
        return [(m["role"], m["content"]) for m in messages]


class UnlimitedHistoryTests(MemoryBase):
    def test_zero_max_history_keeps_all_turns(self):
        cfg = {"history_ttl_minutes": 30, "max_history": 0}
        plugin = self._plugin(cfg)
        self._rounds(plugin, self.key, 30)
        messages = plugin._history(self.key)
        self.assertEqual(len(messages), 60)
        self.assertEqual(self._pairs(messages)[0], ("user", "轮0-问"))
        self.assertEqual(self._pairs(messages)[-1], ("assistant", "轮29-答"))

    def test_negative_max_history_keeps_all_turns(self):
        cfg = {"history_ttl_minutes": 30, "max_history": -5}
        plugin = self._plugin(cfg)
        self._rounds(plugin, self.key, 20)
        self.assertEqual(len(plugin._history(self.key)), 40)

    def test_zero_max_history_single_round_not_cleared(self):
        # 老实现 del messages[:-0] 会把历史清空，这里守住回归。
        cfg = {"history_ttl_minutes": 30, "max_history": 0}
        plugin = self._plugin(cfg)
        plugin._remember(self.key, "user", "你好")
        plugin._remember(self.key, "assistant", "在的")
        self.assertEqual(self._pairs(plugin._history(self.key)), [("user", "你好"), ("assistant", "在的")])

    def test_positive_max_history_still_trims(self):
        cfg = {"history_ttl_minutes": 30, "max_history": 8}
        plugin = self._plugin(cfg)
        self._rounds(plugin, self.key, 30)
        messages = plugin._history(self.key)
        self.assertEqual(len(messages), 16)
        # 最后 8 轮 = 第 22~29 轮
        self.assertEqual(messages[0]["content"], "轮22-问")
        self.assertEqual(messages[-1]["content"], "轮29-答")

    def test_max_history_one_turn(self):
        cfg = {"history_ttl_minutes": 30, "max_history": 1}
        plugin = self._plugin(cfg)
        self._rounds(plugin, self.key, 3)
        self.assertEqual(len(plugin._history(self.key)), 2)

    def test_invalid_max_history_falls_back_to_default(self):
        cfg = {"history_ttl_minutes": "不是数字", "max_history": "不是数字"}
        plugin = self._plugin(cfg)
        self._rounds(plugin, self.key, 20)
        # 默认 8 轮（16 条）而不是崩掉 / 清空
        self.assertEqual(len(plugin._history(self.key)), 16)


class ForeverMemoryTests(MemoryBase):
    def test_zero_ttl_never_expires(self):
        cfg = {"history_ttl_minutes": 0, "max_history": 0}
        plugin = self._plugin(cfg)
        plugin._remember(self.key, "user", "很久以前的话")
        plugin._remember(self.key, "assistant", "很久以前的回答")
        future = time.time() + 10 * 24 * 3600
        with mock.patch.object(ai_chat.time, "time", return_value=future):
            messages = plugin._history(self.key)
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["content"], "很久以前的话")

    def test_negative_ttl_never_expires(self):
        cfg = {"history_ttl_minutes": -30, "max_history": 0}
        plugin = self._plugin(cfg)
        plugin._remember(self.key, "user", "hi")
        future = time.time() + 10 * 24 * 3600
        with mock.patch.object(ai_chat.time, "time", return_value=future):
            self.assertEqual(len(plugin._history(self.key)), 1)

    def test_positive_ttl_still_expires(self):
        cfg = {"history_ttl_minutes": 30, "max_history": 0}
        plugin = self._plugin(cfg)
        plugin._remember(self.key, "user", "hi")
        future = time.time() + 31 * 60
        with mock.patch.object(ai_chat.time, "time", return_value=future):
            self.assertEqual(plugin._history(self.key), [])
        self.assertNotIn(self.key, plugin.history)

    def test_unlimited_history_survives_reload_without_expiry(self):
        cfg = {"history_ttl_minutes": 0, "max_history": 0}
        plugin = self._plugin(cfg)
        self._rounds(plugin, self.key, 5)
        future = time.time() + 10 * 24 * 3600
        with mock.patch.object(ai_chat.time, "time", return_value=future):
            reopened = self._plugin(dict(cfg))
            self.assertEqual(len(reopened._history(self.key)), 10)


class ModelSwitchTests(MemoryBase):
    def _cfg(self):
        return {
            "history_ttl_minutes": 0,
            "max_history": 0,
            "provider": "openai",
            "model": "deepseek-chat",
            "active_profile": "",
            "profiles": [],
        }

    def test_switching_model_keeps_history(self):
        cfg = self._cfg()
        plugin = self._plugin(cfg)
        self._rounds(plugin, self.key, 3)
        before = self._pairs(plugin._history(self.key))

        cfg["model"] = "gemini-2.5-pro"
        after_switch = self._pairs(plugin._history(self.key))
        self.assertEqual(after_switch, before)
        self.assertEqual(len(after_switch), 6)

        # 切换 profile（认证方式/接口）也不影响历史 key
        cfg["profiles"] = [{"id": "p2", "auth": "gemini", "model": "gemini-2.5-pro"}]
        cfg["active_profile"] = "p2"
        self.assertEqual(self._pairs(plugin._history(self.key)), before)

        # 换模型后新增一轮，历史继续累加而不是从零开始
        plugin._remember(self.key, "user", "换模型后还记得吗")
        self.assertEqual(len(plugin._history(self.key)), 7)

    def test_group_and_private_keys_are_separate(self):
        cfg = self._cfg()
        plugin = self._plugin(cfg)
        cfg["model"] = "model-a"
        plugin._remember("group12345:67890", "user", "群里的问题")
        cfg["model"] = "model-b"
        plugin._remember("private:67890", "user", "私聊的问题")
        self.assertEqual(
            [m["content"] for m in plugin._history("group12345:67890")], ["群里的问题"]
        )
        self.assertEqual(
            [m["content"] for m in plugin._history("private:67890")], ["私聊的问题"]
        )

    def test_history_persisted_to_context_json(self):
        cfg = self._cfg()
        plugin = self._plugin(cfg)
        self._rounds(plugin, self.key, 2)
        data = json.loads((Path(self._tmp.name) / "context.json").read_text(encoding="utf-8"))
        self.assertIn(self.key, data)
        self.assertEqual(len(data[self.key]["messages"]), 4)
        # 换模型后重新加载（管理台改配置），历史仍在
        cfg["model"] = "another-model"
        reopened = self._plugin(dict(cfg))
        self.assertEqual(len(reopened._history(self.key)), 4)


if __name__ == "__main__":
    unittest.main()
