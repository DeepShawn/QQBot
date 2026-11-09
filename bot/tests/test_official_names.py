"""T53 官方通道昵称绑定、GROUP_MESSAGE_CREATE 图片、顶层人格提示词测试。"""
from __future__ import annotations

import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

BOT_DIR = Path(__file__).resolve().parent.parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import official  # noqa: E402
from plugins import ai_chat, commands  # noqa: E402

TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
SELF_ID = 999
GROUP_D = {
    "id": "MSG-G1",
    "group_openid": "GROUP-OPENID-1",
    "content": "帮我看看",
    "timestamp": "1700000000",
    "author": {"member_openid": "MEMBER-OPENID-1", "id": "MEMBER-OPENID-1"},
}
C2C_D = {
    "id": "MSG-C1",
    "content": "私聊你好",
    "timestamp": "1700000001",
    "author": {"user_openid": "USER-OPENID-1"},
}


def _patch(test: unittest.TestCase, obj, name, value):
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


class FakeApi:
    def __init__(self):
        self.kind = "official"
        self.connected = None

    async def call(self, action, **params):
        return {}


class FakeCtx:
    def __init__(self, root, cfg=None, admins=()):
        self.data_dir = Path(root)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log = FakeLog()
        self.self_id = SELF_ID
        self.bot_name = "测试机器人"
        self.api = FakeApi()
        self._cfg = cfg or {"plugins": {}}
        self._admins = [str(item) for item in admins]
        self.config = SimpleNamespace(path=self.data_dir / "config.json")
        self.replies: list[str] = []
        self.sent: list[dict] = []

    @property
    def cfg(self) -> dict:
        return self._cfg

    def plugin_cfg(self, name: str) -> dict:
        return (self._cfg.get("plugins") or {}).get(name) or {}

    def is_admin(self, user_id) -> bool:
        return str(user_id) in self._admins

    def format_text(self, template, event, **extra):
        return str(template or "")

    async def reply(self, event, message, at: bool = False):
        self.replies.append(str(message))
        return {"message_id": len(self.replies)}

    async def send_private(self, user_id, message):
        self.sent.append({"type": "private", "user_id": user_id, "message": message})

    async def send_group(self, group_id, message):
        self.sent.append({"type": "group", "group_id": group_id, "message": message})


def _event(text: str, *, message_type="private", mention=False, user_id=20002, official_meta=None):
    segs = []
    if mention:
        segs.append({"type": "at", "data": {"qq": str(SELF_ID)}})
    segs.append({"type": "text", "data": {"text": text}})
    event = {
        "post_type": "message",
        "message_type": message_type,
        "user_id": user_id,
        "message": segs,
    }
    if message_type == "group":
        event["group_id"] = 12345
    if official_meta is not None:
        event["_official"] = official_meta
    return event


class NamesStorageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_set_get_load_atomic(self):
        saved = official.set_nickname(self.root, "M1", "小明")
        self.assertEqual(saved, "小明")
        self.assertEqual(official.get_nickname(self.root, "M1"), "小明")
        names = official.load_names(self.root)
        self.assertEqual(names["M1"], "小明")
        data = json.loads(official.names_path(self.root).read_text(encoding="utf-8"))
        self.assertEqual(data["names"]["M1"], "小明")
        self.assertEqual(list(self.root.glob("*.tmp")), [])
        self.assertEqual(official.get_nickname(self.root, "unknown"), "")

    def test_empty_name_raises(self):
        with self.assertRaises(ValueError):
            official.set_nickname(self.root, "M1", "   ")
        with self.assertRaises(ValueError):
            official.set_nickname(self.root, "", "小明")


class NamesTranslateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.client = official.OfficialClient(
            "app", "secret", data_dir=self.root
        )
        self.client.self_id = SELF_ID

    async def test_group_event_uses_nickname(self):
        official.set_nickname(self.root, "MEMBER-OPENID-1", "小明")
        event = self.client._translate_event("GROUP_AT_MESSAGE_CREATE", GROUP_D)
        self.assertEqual(event["sender"]["nickname"], "小明")
        self.assertEqual(event["sender"]["user_id"], event["user_id"])

    async def test_unknown_user_falls_back(self):
        group_event = self.client._translate_event("GROUP_AT_MESSAGE_CREATE", GROUP_D)
        self.assertEqual(group_event["sender"]["nickname"], "群友")
        c2c_event = self.client._translate_event("C2C_MESSAGE_CREATE", C2C_D)
        self.assertEqual(c2c_event["sender"]["nickname"], "朋友")

    async def test_c2c_event_uses_nickname(self):
        official.set_nickname(self.root, "USER-OPENID-1", "小红")
        event = self.client._translate_event("C2C_MESSAGE_CREATE", C2C_D)
        self.assertEqual(event["sender"]["nickname"], "小红")

    async def test_group_message_create_attachments_images(self):
        data = dict(
            GROUP_D,
            content="看这个图",
            attachments=[
                {"content_type": "image/png", "url": "https://example.com/a.png"},
                {"content_type": "file/pdf", "url": "https://example.com/a.pdf"},
            ],
        )
        official.set_nickname(self.root, "MEMBER-OPENID-1", "小明")
        event = self.client._translate_event("GROUP_MESSAGE_CREATE", data)
        self.assertEqual(event["message_type"], "group")
        self.assertEqual(event["sender"]["nickname"], "小明")
        self.assertEqual(event["message"][0]["type"], "text")
        self.assertEqual(len(event["message"]), 2)  # 只有图片
        image = event["message"][1]
        self.assertEqual(image["type"], "image")
        self.assertEqual(image["data"]["file"], "https://example.com/a.png")
        self.assertEqual(event["_official"]["kind"], "group")

    async def test_group_at_event_attachments_images(self):
        data = dict(
            GROUP_D,
            attachments=[
                {"content_type": "image/jpeg", "url": "https://example.com/b.jpg"}
            ],
        )
        event = self.client._translate_event("GROUP_AT_MESSAGE_CREATE", data)
        self.assertEqual(event["message"][0]["type"], "at")
        self.assertTrue(any(seg.get("type") == "image" for seg in event["message"]))


class SetNameCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.ctx = FakeCtx(
            self.root,
            cfg={"plugins": {"ai_chat": {"enabled": True}}},
            admins=(),
        )
        self.plugin = commands.Plugin(self.ctx)

    def official_event(self, text: str, kind="group") -> dict:
        event = _event(
            text,
            message_type="group" if kind == "group" else "private",
            mention=(kind == "group"),
            official_meta=(
                {"kind": "group", "group_openid": "G1", "member_openid": "M1", "msg_id": "x"}
                if kind == "group"
                else {"kind": "c2c", "user_openid": "U1", "msg_id": "x"}
            ),
        )
        return event

    async def test_any_member_can_set_name(self):
        await self.plugin.on_event(self.official_event("/我叫 小明"))
        self.assertIn("小明", self.ctx.replies[-1])
        self.assertEqual(official.get_nickname(self.root, "M1"), "小明")

    async def test_c2c_set_name(self):
        await self.plugin.on_event(self.official_event("/我叫 小红", kind="c2c"))
        self.assertEqual(official.get_nickname(self.root, "U1"), "小红")

    async def test_name_too_long(self):
        await self.plugin.on_event(self.official_event("/我叫 " + "长" * 30))
        self.assertIn("太长", self.ctx.replies[-1])
        self.assertEqual(official.get_nickname(self.root, "M1"), "")

    async def test_usage_without_name(self):
        await self.plugin.on_event(self.official_event("/我叫"))
        self.assertIn("用法", self.ctx.replies[-1])

    async def test_non_official_channel_hint(self):
        await self.plugin.on_event(_event("/我叫 小明"))
        self.assertIn("只在官方通道", self.ctx.replies[-1])

    async def test_name_max_length_20(self):
        await self.plugin.on_event(self.official_event("/我叫 " + "名" * 20))
        self.assertEqual(official.get_nickname(self.root, "M1"), "名" * 20)
        await self.plugin.on_event(self.official_event("/我叫 " + "名" * 21))
        self.assertIn("太长", self.ctx.replies[-1])

    async def test_empty_shows_current_name(self):
        await self.plugin.on_event(self.official_event("/我叫 小明"))
        await self.plugin.on_event(self.official_event("/我叫"))
        self.assertIn("小明", self.ctx.replies[-1])

    async def test_help_mentions_set_name(self):
        self.ctx._admins = ["10001"]
        await self.plugin.on_event(_event("/help", user_id=10001))
        self.assertIn("/我叫", self.ctx.replies[-1])


class AiChatPromptTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.ai_cfg = {
            "enabled": True,
            "provider": "openai",
            "base_url": "",
            "api_key": "key",
            "auth": "",
            "model": "m",
            "system_prompt": "你叫小助手，说话简短。",
            "active_profile": "p1",
            "profiles": [
                {
                    "id": "p1",
                    "name": "ChatGPT 账号",
                    "auth": "chatgpt",
                    "model": "gpt-6.1-sol",
                    "system_prompt": "",
                }
            ],
            "trigger": {
                "private": True,
                "group_at": True,
                "group_followup_seconds": 0,
                "group_prefix": [],
            },
            "cooldown_seconds": 0,
        }
        self.ctx = FakeCtx(
            self.root,
            cfg={
                "plugins": {
                    "ai_chat": self.ai_cfg,
                    "python_run": {"enabled": False},
                    "dev_tools": {"enabled": False},
                    "skills": {"enabled": False},
                    "image_gen": {"enabled": False},
                }
            },
            admins=(),
        )
        self._orig_chat = ai_chat.providers.chat
        self.addCleanup(setattr, ai_chat.providers, "chat", self._orig_chat)

        def fake_chat(**kwargs):
            return "你好呀～"

        ai_chat.providers.chat = fake_chat

    async def test_top_level_system_prompt_wins_over_profile(self):
        plugin = ai_chat.Plugin(self.ctx)
        cfg = plugin.effective_cfg()
        self.assertEqual(cfg["system_prompt"], "你叫小助手，说话简短。")
        self.assertEqual(cfg["auth"], "chatgpt")
        self.assertEqual(cfg["model"], "gpt-6.1-sol")

    async def test_official_group_ai_trigger_logs_metadata(self):
        plugin = ai_chat.Plugin(self.ctx)
        event = _event(
            "你好",
            message_type="group",
            mention=True,
            official_meta={
                "kind": "group",
                "group_openid": "GROUP-OPENID-9",
                "member_openid": "M1",
                "msg_id": "m1",
            },
        )
        handled = await plugin.on_event(event)
        self.assertTrue(handled)
        joined = "\n".join(self.ctx.log.records)
        self.assertIn("AI 官方群触发", joined)
        self.assertIn("group_openid=GROUP-OPENID-9", joined)
        self.assertIn("addressed=True", joined)
        self.assertIn("system_prompt_len=", joined)


if __name__ == "__main__":
    unittest.main()
