"""ai_chat / config 的账号模式配置测试（不启动机器人）。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent.parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import config  # noqa: E402
import providers  # noqa: E402
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


class ConfigDefaultTests(unittest.TestCase):
    def test_auth_default(self):
        ai_cfg = config.DEFAULTS["plugins"]["ai_chat"]
        self.assertIn("auth", ai_cfg)
        self.assertEqual(ai_cfg["auth"], "")
        self.assertEqual(ai_cfg["profiles"], [])


class AiChatProfileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _plugin(self, cfg: dict):
        return ai_chat.Plugin(FakeCtx(cfg, self._tmp.name))

    def test_profile_fields_include_auth(self):
        self.assertIn("auth", ai_chat.Plugin.PROFILE_FIELDS)

    def test_account_profile_uses_profile_model_only(self):
        cfg = {
            "profiles": [
                {
                    "id": "p1",
                    "provider": "openai",
                    "base_url": "https://api.example.com/v1",
                    "api_key": "api-key",
                    "auth": "claude",
                    "model": "",
                }
            ],
            "active_profile": "p1",
            "provider": "openai",
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "top-key",
            "auth": "",
            "model": "deepseek-chat",
        }
        plugin = self._plugin(cfg)
        effective = plugin._profile_settings()
        self.assertEqual(effective["auth"], "claude")
        self.assertEqual(effective["model"], "")
        self.assertEqual(plugin.effective_cfg(), effective)
        self.assertEqual(effective["base_url"], "https://api.example.com/v1")

    def test_account_profile_with_model(self):
        cfg = {
            "profiles": [
                {"id": "p1", "auth": "chatgpt", "model": "gpt-6.1-sol"},
            ],
            "active_profile": "p1",
            "auth": "",
            "model": "deepseek-chat",
        }
        effective = self._plugin(cfg)._profile_settings()
        self.assertEqual(effective["auth"], "chatgpt")
        self.assertEqual(effective["model"], "gpt-6.1-sol")

    def test_api_key_profile_clears_top_level_auth(self):
        cfg = {
            "profiles": [
                {"id": "p1", "api_key": "key", "auth": "", "model": "deepseek-chat"},
            ],
            "active_profile": "p1",
            "auth": "claude",
            "model": "deepseek-chat",
        }
        effective = self._plugin(cfg)._profile_settings()
        self.assertEqual(effective["auth"], "")
        self.assertEqual(effective["model"], "deepseek-chat")

    def test_sync_chat_passes_auth(self):
        cfg = {
            "provider": "openai",
            "base_url": "",
            "api_key": "",
            "auth": "claude",
            "model": "",
            "temperature": 0.6,
            "max_tokens": 512,
            "timeout_seconds": 20,
        }
        plugin = self._plugin(cfg)
        captured: dict = {}
        old = providers.chat

        def fake_chat(**kwargs):
            captured.update(kwargs)
            return "答"

        providers.chat = fake_chat
        try:
            self.assertEqual(
                plugin._sync_chat(plugin._profile_settings(), [{"role": "user", "content": "hi"}]),
                "答",
            )
        finally:
            providers.chat = old
        self.assertEqual(captured["auth"], "claude")
        self.assertEqual(captured["model"], "")
        self.assertEqual(captured["max_tokens"], 512)

    def test_sync_chat_api_key_mode_keeps_model(self):
        cfg = {
            "provider": "openai",
            "base_url": "",
            "api_key": "key",
            "auth": "",
            "model": "",
            "temperature": 0.7,
            "max_tokens": 600,
            "timeout_seconds": 60,
        }
        plugin = self._plugin(cfg)
        captured: dict = {}
        old = providers.chat

        def fake_chat(**kwargs):
            captured.update(kwargs)
            return "答"

        providers.chat = fake_chat
        try:
            plugin._sync_chat(plugin._profile_settings(), [{"role": "user", "content": "hi"}])
        finally:
            providers.chat = old
        self.assertEqual(captured["auth"], "")
        self.assertEqual(captured["model"], "deepseek-chat")


if __name__ == "__main__":
    unittest.main()
