"""T60 人格预设：默认数据、生效逻辑（system/avatar prompt）、admin sanitize。"""
from __future__ import annotations

import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent.parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import admin.server as admin_server  # noqa: E402
import config  # noqa: E402
import imagegen  # noqa: E402
from plugins import ai_chat  # noqa: E402
from plugins import image_gen as image_gen_plugin  # noqa: E402

TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _patch(test, obj, name, value):
    old = getattr(obj, name)
    setattr(obj, name, value)
    test.addCleanup(setattr, obj, name, old)


class FakeLog:
    def __init__(self):
        self.records: list[str] = []

    def _record(self, msg, args):
        try:
            self.records.append(str(msg) % args if args else str(msg))
        except Exception:
            self.records.append(str(msg))

    def info(self, msg, *args, **kwargs):
        self._record(msg, args)

    def warning(self, msg, *args, **kwargs):
        self._record(msg, args)

    def error(self, msg, *args, **kwargs):
        self._record(msg, args)

    def exception(self, msg, *args, **kwargs):
        self._record(msg, args)


class FakeCtx:
    def __init__(self, root, cfg):
        self.data_dir = Path(root)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log = FakeLog()
        self.self_id = 999
        self.bot_name = "测试机器人"
        self._cfg = cfg
        self.replies: list[str] = []
        self.sent: list[dict] = []

    @property
    def cfg(self):
        return self._cfg

    def plugin_cfg(self, name):
        return (self._cfg.get("plugins") or {}).get(name) or {}

    def is_admin(self, user_id):
        return False

    def format_text(self, template, event, **extra):
        return str(template or "")

    async def reply(self, event, message, at=False):
        self.replies.append(str(message))
        return {"message_id": 1}

    async def send_private(self, user_id, message):
        self.sent.append({"type": "private", "user_id": user_id, "message": message})

    async def send_group(self, group_id, message):
        self.sent.append({"type": "group", "group_id": group_id, "message": message})


def _event(text="画张你自己的照片"):
    return {
        "post_type": "message",
        "message_type": "private",
        "user_id": 20002,
        "message": [{"type": "text", "data": {"text": text}}],
    }


def _presets() -> list[dict]:
    return [
        {
            "id": "whale",
            "name": "大肥鱼",
            "emoji": "🐟",
            "system_prompt": "你是傲娇鲸鱼娘本鱼。",
            "avatar_prompt": "蓝白鲸鱼娘、女仆裙、鲸耳鲸尾。",
        },
        {
            "id": "catgirl",
            "name": "软萌猫娘",
            "emoji": "🐱",
            "system_prompt": "你是软萌猫娘，喵。",
            "avatar_prompt": "猫耳猫尾、软萌少女。",
        },
    ]


class DefaultPresetTests(unittest.TestCase):
    def test_defaults_structure(self):
        ai = config.DEFAULTS["plugins"]["ai_chat"]
        self.assertEqual(ai["active_preset"], "custom")
        presets = ai["presets"]
        self.assertIsInstance(presets, list)
        ids = [item["id"] for item in presets]
        self.assertEqual(
            set(ids), {"whale", "catgirl", "sister", "tsundere"}
        )
        self.assertNotIn("custom", ids)  # custom 不是列表项
        for item in presets:
            for key in ("id", "name", "emoji", "system_prompt", "avatar_prompt"):
                self.assertIn(key, item)
            self.assertTrue(item["name"])
            self.assertTrue(item["emoji"])
            self.assertLessEqual(len(item["system_prompt"]), 4000)
            self.assertLessEqual(len(item["avatar_prompt"]), 2000)

    def test_example_matches_default_ids(self):
        example = json.loads(
            (BOT_DIR / "config.example.json").read_text(encoding="utf-8")
        )
        ai = example["plugins"]["ai_chat"]
        self.assertEqual(ai["active_preset"], "custom")
        self.assertEqual(
            {item["id"] for item in ai["presets"]},
            {"whale", "catgirl", "sister", "tsundere"},
        )


class AvatarPromptPresetTests(unittest.TestCase):
    def cfg(self, active="whale"):
        return {
            "active_preset": active,
            "presets": _presets(),
            "avatar": {"prompt": "顶层形象提示词", "reference_image": ""},
        }

    def test_active_preset_wins(self):
        self.assertEqual(
            imagegen.avatar_prompt(self.cfg("whale")), "蓝白鲸鱼娘、女仆裙、鲸耳鲸尾。"
        )
        self.assertEqual(
            imagegen.active_preset(self.cfg("catgirl"))["id"], "catgirl"
        )

    def test_custom_or_unknown_falls_back_to_top(self):
        for active in ("custom", "", "unknown"):
            with self.subTest(active=active):
                self.assertEqual(
                    imagegen.avatar_prompt(self.cfg(active)), "顶层形象提示词"
                )
                self.assertIsNone(imagegen.active_preset(self.cfg(active)))

    def test_preset_without_avatar_prompt_falls_back(self):
        presets = _presets()
        presets[0]["avatar_prompt"] = ""
        cfg = {
            "active_preset": "whale",
            "presets": presets,
            "avatar": {"prompt": "顶层形象提示词"},
        }
        self.assertEqual(imagegen.avatar_prompt(cfg), "顶层形象提示词")


class AiChatPresetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def make_plugin(self, active="whale"):
        ai_cfg = {
            "enabled": True,
            "provider": "openai",
            "base_url": "",
            "api_key": "key",
            "auth": "",
            "model": "m",
            "system_prompt": "顶层人格提示词",
            "active_preset": active,
            "presets": _presets(),
            "avatar": {"prompt": "顶层形象提示词", "reference_image": ""},
            "trigger": {
                "private": True,
                "group_at": True,
                "group_followup_seconds": 0,
                "group_prefix": [],
            },
        }
        ctx = FakeCtx(
            self.root,
            {
                "plugins": {
                    "ai_chat": ai_cfg,
                    "python_run": {"enabled": False},
                    "dev_tools": {"enabled": False},
                    "skills": {"enabled": False},
                    "image_gen": {
                        "enabled": True,
                        "provider": "openai",
                        "api_key": "img-key",
                        "auth": "",
                        "model": "gpt-image-1",
                        "size": "1024x1024",
                        "timeout_seconds": 10,
                        "caption_ai": False,
                        "triggers": [],
                    },
                }
            },
        )
        return ai_chat.Plugin(ctx), ctx, ai_cfg

    async def test_active_preset_system_prompt(self):
        plugin, _ctx, _cfg = self.make_plugin("whale")
        cfg = plugin._profile_settings()
        self.assertEqual(cfg["system_prompt"], "你是傲娇鲸鱼娘本鱼。")
        plugin, _ctx, _cfg = self.make_plugin("custom")
        self.assertEqual(
            plugin._profile_settings()["system_prompt"], "顶层人格提示词"
        )
        plugin, _ctx, _cfg = self.make_plugin("unknown")
        self.assertEqual(
            plugin._profile_settings()["system_prompt"], "顶层人格提示词"
        )

    async def test_profile_override_still_works(self):
        plugin, _ctx, ai_cfg = self.make_plugin("whale")
        ai_cfg["active_profile"] = "p1"
        ai_cfg["profiles"] = [
            {"id": "p1", "auth": "chatgpt", "model": "gpt-6.1-sol"}
        ]
        cfg = plugin._profile_settings()
        self.assertEqual(cfg["auth"], "chatgpt")
        self.assertEqual(cfg["model"], "gpt-6.1-sol")
        self.assertEqual(cfg["system_prompt"], "你是傲娇鲸鱼娘本鱼。")

    async def test_image_tool_uses_preset_avatar_prompt(self):
        plugin, ctx, cfg = self.make_plugin("whale")
        calls: list = []
        _patch(
            self,
            imagegen,
            "generate_for_ai",
            lambda *args, **kwargs: calls.append((args, kwargs)) or [TINY_PNG],
        )
        output = await plugin._perform_image_tool(
            _event(),
            cfg,
            {"prompt": "自拍", "size": "1024x1024"},
            user_text="画张你自己的照片",
        )
        self.assertTrue(json.loads(output)["ok"])
        prompt = calls[0][0][2]
        self.assertTrue(prompt.startswith("蓝白鲸鱼娘、女仆裙、鲸耳鲸尾。"))
        self.assertNotIn("顶层形象提示词", prompt)


class ImageGenPluginPresetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    async def test_shortcut_uses_preset_avatar_prompt(self):
        ai_cfg = {
            "provider": "openai",
            "api_key": "ai-key",
            "auth": "",
            "model": "m",
            "active_preset": "catgirl",
            "presets": _presets(),
            "avatar": {"prompt": "顶层形象提示词", "reference_image": ""},
            "vision": {"enabled": False},
        }
        image_cfg = {
            "enabled": True,
            "provider": "openai",
            "base_url": "https://api.openai.com/v1",
            "api_key": "img-key",
            "auth": "",
            "model": "gpt-image-1",
            "size": "1024x1024",
            "timeout_seconds": 10,
            "triggers": ["画张"],
            "caption_ai": False,
            "at_sender": True,
            "cooldown_seconds": 0,
        }
        ctx = FakeCtx(
            self.root,
            {"plugins": {"ai_chat": ai_cfg, "image_gen": image_cfg}},
        )
        calls: list = []
        _patch(
            self,
            imagegen,
            "generate",
            lambda *args, **kwargs: calls.append(kwargs) or [TINY_PNG],
        )
        await image_gen_plugin.Plugin(ctx).on_event(_event())
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]["prompt"].startswith("猫耳猫尾、软萌少女。"))
        self.assertNotIn("顶层形象提示词", calls[0]["prompt"])


class SanitizePresetTests(unittest.TestCase):
    def test_valid_presets_and_active(self):
        cfg = {
            "plugins": {
                "ai_chat": {
                    "presets": _presets(),
                    "active_preset": "catgirl",
                }
            }
        }
        ai = admin_server.sanitize_config(cfg)["plugins"]["ai_chat"]
        self.assertEqual([p["id"] for p in ai["presets"]], ["whale", "catgirl"])
        self.assertEqual(ai["active_preset"], "catgirl")

    def test_invalid_items_dropped_and_truncated(self):
        cfg = {
            "plugins": {
                "ai_chat": {
                    "presets": [
                        "bad",
                        {"id": "Bad ID!", "name": "x"},
                        {"id": "ok", "name": "", "emoji": "🐟",
                         "system_prompt": "长" * 5000, "avatar_prompt": "长" * 3000},
                        {"id": "ok", "name": "重复", "emoji": "🐱"},
                        {"id": "catgirl", "name": "猫", "emoji": "🐱"},
                    ],
                    "active_preset": "not-exist",
                }
            }
        }
        ai = admin_server.sanitize_config(cfg)["plugins"]["ai_chat"]
        ids = [p["id"] for p in ai["presets"]]
        self.assertEqual(ids, ["ok", "catgirl"])
        ok = ai["presets"][0]
        self.assertEqual(ok["name"], "ok")  # 空 name 回退 id
        self.assertEqual(len(ok["system_prompt"]), 4000)
        self.assertEqual(len(ok["avatar_prompt"]), 2000)
        self.assertEqual(ai["active_preset"], "custom")

    def test_non_list_presets_fall_back_to_defaults(self):
        cfg = {"plugins": {"ai_chat": {"presets": "nope"}}}
        ai = admin_server.sanitize_config(cfg)["plugins"]["ai_chat"]
        self.assertEqual(
            {p["id"] for p in ai["presets"]},
            {"whale", "catgirl", "sister", "tsundere"},
        )
        self.assertEqual(ai["active_preset"], "custom")

    def test_active_equals_preset_kept(self):
        cfg = {"plugins": {"ai_chat": {"presets": _presets(), "active_preset": " Whale "}}}
        ai = admin_server.sanitize_config(cfg)["plugins"]["ai_chat"]
        self.assertEqual(ai["active_preset"], "whale")


if __name__ == "__main__":
    unittest.main()
