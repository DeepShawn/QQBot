"""T58 画图后角色化描述 + 永不承认是 AI（离线）。"""
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

import imagegen  # noqa: E402
import providers  # noqa: E402
from plugins import ai_chat  # noqa: E402
from plugins import image_gen as image_gen_plugin  # noqa: E402

TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
SELF_ID = 999
BANNED = ("画好了", "AI", "机器人", "模型", "生成")


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
        self.self_id = SELF_ID
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


def _event(text="你好", *, message_type="private", mention=False, user_id=20002):
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
    return event


def _assert_no_banned(test, text):
    for word in BANNED:
        test.assertNotIn(word, text)


class ImageGenCaptionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.ai_cfg = {
            "provider": "openai",
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "ai-key",
            "auth": "",
            "model": "deepseek-chat",
        }
        self.image_cfg = {
            "enabled": True,
            "provider": "openai",
            "base_url": "https://api.openai.com/v1",
            "api_key": "img-key",
            "auth": "",
            "model": "gpt-image-1",
            "size": "1024x1024",
            "timeout_seconds": 10,
            "triggers": ["画一张"],
            "push_groups": [],
            "caption": "🎨 画好了",
            "caption_ai": True,
            "at_sender": True,
            "cooldown_seconds": 0,
        }
        self.ctx = FakeCtx(
            self.root,
            {"plugins": {"ai_chat": self.ai_cfg, "image_gen": self.image_cfg}},
        )
        self.caption_calls: list = []
        self.caption_reply = "山间日落被我揉进画里啦，云是蜜桃色，风都慢下来了，快夸我～"
        self.caption_error: Exception | None = None
        self.chat_calls: list = []
        self.describe_calls: list = []
        self.describe_text = ""
        self._orig_chat = providers.chat
        self._orig_describe = providers.describe_images
        self._orig_generate = imagegen.generate
        self.addCleanup(setattr, providers, "chat", self._orig_chat)
        self.addCleanup(setattr, providers, "describe_images", self._orig_describe)
        self.addCleanup(setattr, imagegen, "generate", self._orig_generate)
        imagegen.generate = lambda **kwargs: [TINY_PNG]
        providers.describe_images = self.fake_describe

        def fake_chat(**kwargs):
            self.chat_calls.append(kwargs)
            if self.caption_error is not None:
                raise self.caption_error
            return self.caption_reply

        providers.chat = fake_chat

    def fake_describe(self, **kwargs):
        self.describe_calls.append(kwargs)
        return self.describe_text

    async def run_event(self, text="画一张山间日落"):
        return await image_gen_plugin.Plugin(self.ctx).on_event(_event(text))

    def sent_text(self):
        message = self.ctx.sent[-1]["message"]
        return "".join(
            str((seg.get("data") or {}).get("text") or "")
            for seg in message
            if seg.get("type") == "text"
        )

    async def test_caption_ai_uses_roleplay_text(self):
        self.describe_text = "山间日落，云被染成蜜桃色，风很慢。"
        self.assertTrue(await self.run_event())
        self.assertEqual(self.sent_text(), self.caption_reply)
        self.assertNotEqual(self.sent_text(), self.image_cfg["caption"])
        self.assertEqual(len(self.chat_calls), 1)
        system = self.chat_calls[0]["messages"][0]["content"]
        self.assertIn("角色", system)
        self.assertIn("绝不承认", system)
        self.assertIn("画好了", system)  # 提示词里明确禁止
        self.assertEqual(self.chat_calls[0]["auth"], "")
        self.assertEqual(self.chat_calls[0]["model"], "deepseek-chat")
        user = self.chat_calls[0]["messages"][1]["content"]
        self.assertIn("山间日落", user)
        self.assertIn("画面实际内容参考", user)
        self.assertIn("云被染成蜜桃色", user)
        # 发的是「角色描述 + 图片」
        self.assertTrue(any(seg.get("type") == "image" for seg in self.ctx.sent[-1]["message"]))

    async def test_caption_ai_uses_active_profile(self):
        self.ai_cfg.update(
            {
                "api_key": "",
                "auth": "",
                "model": "",
                "active_profile": "p1",
                "profiles": [
                    {
                        "id": "p1",
                        "provider": "openai",
                        "base_url": "",
                        "api_key": "",
                        "auth": "chatgpt",
                        "model": "gpt-6.1-sol",
                    }
                ],
            }
        )
        await self.run_event()
        self.assertTrue(self.chat_calls)
        self.assertEqual(self.chat_calls[0]["auth"], "chatgpt")
        self.assertEqual(self.chat_calls[0]["model"], "gpt-6.1-sol")
        self.assertEqual(self.sent_text(), self.caption_reply)

    async def test_caption_ai_off_keeps_old_caption(self):
        self.image_cfg["caption_ai"] = False
        await self.run_event()
        self.assertEqual(self.sent_text(), "🎨 画好了")
        self.assertEqual(self.chat_calls, [])

    async def test_caption_ai_failure_uses_roleplay_fallback(self):
        self.caption_error = providers.ProviderError("model down")
        await self.run_event()
        text = self.sent_text()
        self.assertTrue(text)
        self.assertNotEqual(text, self.image_cfg["caption"])
        _assert_no_banned(self, text)
        self.assertTrue(
            any(text == item for item in image_gen_plugin.CAPTION_FALLBACKS)
        )

    async def test_caption_ai_no_config_uses_fallback(self):
        self.ai_cfg["api_key"] = ""
        self.ai_cfg["auth"] = ""
        await self.run_event()
        text = self.sent_text()
        self.assertTrue(text)
        self.assertEqual(self.chat_calls, [])
        _assert_no_banned(self, text)

    async def test_caption_ai_banned_output_uses_fallback(self):
        self.caption_reply = "画好了，本 AI 给你生成了一张图。"
        await self.run_event()
        text = self.sent_text()
        self.assertTrue(any(text == item for item in image_gen_plugin.CAPTION_FALLBACKS))
        _assert_no_banned(self, text)

    async def test_describe_images_failure_falls_back_to_text_only(self):
        def bad_describe(**kwargs):
            raise providers.ProviderError("no vision")

        providers.describe_images = bad_describe
        await self.run_event()
        self.assertEqual(self.sent_text(), self.caption_reply)
        user = self.chat_calls[0]["messages"][1]["content"]
        self.assertNotIn("画面实际内容参考", user)


class AiChatRoleplayTests(unittest.IsolatedAsyncioTestCase):
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
            "system_prompt": "你是小助手，说话简短。",
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
            {
                "plugins": {
                    "ai_chat": self.ai_cfg,
                    "python_run": {"enabled": False},
                    "dev_tools": {"enabled": False},
                    "skills": {"enabled": False},
                    "image_gen": {"enabled": False},
                }
            },
        )
        self.chat_calls: list = []
        self.chat_reply = "哼哼，山风都被我收进画里了，云是蜜桃色，慢下来看看嘛～"
        self._orig_chat = ai_chat.providers.chat
        self.addCleanup(setattr, ai_chat.providers, "chat", self._orig_chat)

        def fake_chat(**kwargs):
            self.chat_calls.append(kwargs)
            return self.chat_reply

        ai_chat.providers.chat = fake_chat
        self.plugin = ai_chat.Plugin(self.ctx)

    async def test_system_prompt_has_roleplay_rule(self):
        await self.plugin.on_event(_event("你好"))
        self.assertTrue(self.chat_calls)
        system = self.chat_calls[0]["messages"][0]["content"]
        self.assertIn("你是小助手，说话简短。", system)
        self.assertIn("绝不承认自己是 AI、机器人、模型或程序", system)
        self.assertIn("被问到时用角色身份打岔", system)

    async def test_force_image_final_instruction_roleplay(self):
        async def fake_write(*args, **kwargs):
            return "山间日落，云蜜桃色"

        async def fake_perform(*args, **kwargs):
            return json.dumps(
                {"ok": True, "count": 1, "prompt": "山间日落", "note": "图片已发送"},
                ensure_ascii=False,
            )

        plugin = self.plugin
        plugin._write_image_prompt_from_history = fake_write
        plugin._perform_image_tool = fake_perform
        cfg = self.ai_cfg
        answer = await plugin._force_image_chat(
            _event("画个山间日落"),
            cfg,
            [{"role": "system", "content": "人格"}],
            [],
            "画个山间日落",
        )
        self.assertEqual(answer, self.chat_reply)
        instruction = self.chat_calls[-1]["messages"][-1]["content"]
        self.assertIn("角色口吻", instruction)
        self.assertIn("禁止说「画好了」", instruction)
        self.assertIn("AI/机器人/模型", instruction)

    async def test_route_image_final_instruction_roleplay(self):
        def fake_route(*args, **kwargs):
            return '{"tool":"generate_image","prompt":"山间日落，云蜜桃色"}'

        async def fake_perform(*args, **kwargs):
            return json.dumps({"ok": True, "count": 1}, ensure_ascii=False)

        plugin = self.plugin
        plugin._sync_route_chat = fake_route
        plugin._perform_image_tool = fake_perform
        answer = await plugin._route_tool_chat(
            _event("画个山间日落"),
            self.ai_cfg,
            [{"role": "system", "content": "人格"}],
            [],
            "画个山间日落",
            True,
            False,
        )
        self.assertEqual(answer, self.chat_reply)
        instruction = self.chat_calls[-1]["messages"][-1]["content"]
        self.assertIn("角色口吻", instruction)
        self.assertIn("禁止说「画好了」", instruction)

    async def test_fallback_text_is_roleplay(self):
        text = ai_chat.IMAGE_PROMPT_FALLBACK_REPLY
        self.assertTrue(text)
        _assert_no_banned(self, text)

    async def test_tool_result_contains_roleplay_style(self):
        _patch(
            self,
            imagegen,
            "generate_for_ai",
            lambda *args, **kwargs: [TINY_PNG],
        )
        self.ai_cfg["image_gen"] = {"enabled": True, "auth": "chatgpt"}
        output = await self.plugin._perform_image_tool(
            _event(), self.ai_cfg, {"prompt": "山间日落"}
        )
        data = json.loads(output)
        self.assertTrue(data.get("ok"))
        self.assertIn("角色口吻", data.get("reply_style", ""))
        self.assertIn("禁止说「画好了」", data.get("reply_style", ""))


class FallbackConstantsTests(unittest.TestCase):
    def test_image_gen_fallbacks_have_no_mechanical_words(self):
        self.assertTrue(image_gen_plugin.CAPTION_FALLBACKS)
        for item in image_gen_plugin.CAPTION_FALLBACKS:
            _assert_no_banned(self, item)

    def test_caption_system_forbids_identity(self):
        system = image_gen_plugin.CAPTION_AI_SYSTEM
        self.assertIn("绝不承认", system)
        for word in ("画好了", "AI", "机器人", "模型", "生成"):
            self.assertIn(word, system)  # 提示词里明确禁止这些词


if __name__ == "__main__":
    unittest.main()
