"""AI generate_image 工具 + imagegen.generate_for_ai 的离线测试（stub，不联网）。"""
from __future__ import annotations

import base64
import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent.parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import account_core  # noqa: E402
import accounts_chatgpt  # noqa: E402
import imagegen  # noqa: E402
from plugins import ai_chat  # noqa: E402

TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
SELF_ID = 999


def _patch(test: unittest.TestCase, obj, name, value):
    old = getattr(obj, name)
    setattr(obj, name, value)
    test.addCleanup(setattr, obj, name, old)


def _chatgpt_module(test: unittest.TestCase):
    """拿 imagegen 里 import 会拿到的那个 accounts_chatgpt 模块对象。"""
    module = importlib.import_module("accounts_chatgpt")
    previous = sys.modules.get("accounts_chatgpt")
    sys.modules["accounts_chatgpt"] = module
    if previous is None:
        test.addCleanup(sys.modules.pop, "accounts_chatgpt", None)
    else:
        test.addCleanup(sys.modules.__setitem__, "accounts_chatgpt", previous)
    return module


# ---------------------------------------------------------------- generate_for_ai


class ImageGenForAiTests(unittest.TestCase):
    def test_image_cfg_key_priority(self):
        captured: dict = {}

        def fake_generate(**kwargs):
            captured.update(kwargs)
            return [TINY_PNG]

        _patch(self, imagegen, "generate", fake_generate)
        images = imagegen.generate_for_ai(
            {"auth": "chatgpt", "api_key": "ai-key", "provider": "openai", "base_url": "https://a"},
            {"api_key": "img-key", "provider": "openai", "base_url": "https://b", "model": "m"},
            "一只猫",
            size="512x512",
            timeout=30,
        )
        self.assertEqual(images, [TINY_PNG])
        self.assertEqual(captured["api_key"], "img-key")
        self.assertEqual(captured["base_url"], "https://b")
        self.assertEqual(captured["model"], "m")
        self.assertEqual(captured["size"], "512x512")
        self.assertEqual(captured["timeout"], 30.0)

    def test_follow_ai_reuses_ai_key(self):
        captured: dict = {}
        _patch(
            self,
            imagegen,
            "generate",
            lambda **kwargs: captured.update(kwargs) or [TINY_PNG],
        )
        imagegen.generate_for_ai(
            {"auth": "", "api_key": "ai-key", "base_url": "https://ai"},
            {"api_key": "", "follow_ai": True},
            "猫",
        )
        self.assertEqual(captured["api_key"], "ai-key")
        self.assertEqual(captured["base_url"], "https://ai")

    def test_chatgpt_account_uses_generate_images(self):
        captured: dict = {}

        def fake_generate(record, prompt, size="1024x1024", model=None, timeout=120):
            captured.update(
                record=record, prompt=prompt, size=size, model=model, timeout=timeout
            )
            return [TINY_PNG]

        _patch(self, imagegen, "generate", lambda **kwargs: self.fail("不应走 API Key"))
        _patch(
            self,
            account_core,
            "ensure_access",
            lambda provider: {"access": "a", "extra": {"chatgpt_account_id": "id"}},
        )
        _patch(self, _chatgpt_module(self), "generate_images", fake_generate)
        images = imagegen.generate_for_ai(
            {"auth": "claude", "api_key": ""},
            {"auth": "chatgpt", "api_key": "", "model": "gpt-image-3"},
            "一只狗",
            size="1024x1792",
        )
        self.assertEqual(images, [TINY_PNG])
        self.assertEqual(captured["prompt"], "一只狗")
        self.assertEqual(captured["size"], "1024x1792")
        self.assertEqual(captured["model"], "gpt-image-3")

    def test_chatgpt_account_default_model(self):
        captured: dict = {}

        def fake_generate(record, prompt, size="1024x1024", model=None, timeout=120):
            captured["model"] = model
            return [TINY_PNG]

        _patch(self, imagegen, "generate", lambda **kwargs: self.fail("不应走 API Key"))
        _patch(
            self,
            account_core,
            "ensure_access",
            lambda provider: {"access": "a", "extra": {"chatgpt_account_id": "id"}},
        )
        _patch(self, _chatgpt_module(self), "generate_images", fake_generate)
        imagegen.generate_for_ai(
            {"auth": ""}, {"auth": "chatgpt", "model": "dall-e-3"}, "猫"
        )
        self.assertEqual(captured["model"], imagegen.ACCOUNT_IMAGE_MODEL)

    def test_other_account_rejected(self):
        with self.assertRaises(imagegen.ImageGenError) as ctx:
            imagegen.generate_for_ai({"auth": "claude"}, {"auth": "claude"}, "猫")
        self.assertIn("只支持 ChatGPT", str(ctx.exception))

    def test_nothing_configured(self):
        with self.assertRaises(imagegen.ImageGenError) as ctx:
            imagegen.generate_for_ai({"auth": "", "api_key": ""}, {"api_key": ""}, "猫")
        self.assertIn("图片 API Key", str(ctx.exception))

    def test_empty_prompt(self):
        with self.assertRaises(imagegen.ImageGenError):
            imagegen.generate_for_ai({"api_key": "k"}, {}, "  ")

    def test_account_error_converted_without_token(self):
        def fake_ensure(provider):
            raise account_core.AccountError("network")

        _patch(self, account_core, "ensure_access", fake_ensure)
        with self.assertRaises(imagegen.ImageGenError) as ctx:
            imagegen.generate_for_ai({"auth": "chatgpt"}, {"auth": "chatgpt"}, "猫")
        self.assertEqual(str(ctx.exception), account_core.ERRORS["network"])

    def test_generate_error_propagates(self):
        def fake_generate(**kwargs):
            raise imagegen.ImageGenError("画图接口 500")

        _patch(self, imagegen, "generate", fake_generate)
        with self.assertRaises(imagegen.ImageGenError) as ctx:
            imagegen.generate_for_ai({}, {"api_key": "k"}, "猫")
        self.assertEqual(str(ctx.exception), "画图接口 500")


# ---------------------------------------------------------------- AI 工具循环


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
        self.member = {"role": "member"}
        self.calls: list = []

    async def call(self, action, **params):
        self.calls.append((action, params))
        if action == "get_group_member_info":
            return dict(self.member)
        return {}


class FakeCtx:
    def __init__(self, root, plugin_cfg: dict):
        self.data_dir = Path(root)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "files").mkdir(parents=True, exist_ok=True)
        self.log = FakeLog()
        self.self_id = SELF_ID
        self.bot_name = "测试机器人"
        self.api = FakeApi()
        self._cfg = plugin_cfg
        self.replies: list[str] = []
        self.sent: list[dict] = []

    def plugin_cfg(self, name: str) -> dict:
        return self._cfg.get(name) or {}

    def is_admin(self, user_id) -> bool:
        return False

    def format_text(self, template, event, **extra):
        return str(template or "")

    async def reply(self, event, message, at: bool = False):
        if isinstance(message, list):
            text = "".join(
                str((seg.get("data") or {}).get("text", ""))
                for seg in message
                if isinstance(seg, dict) and seg.get("type") == "text"
            )
        else:
            text = str(message or "")
        self.replies.append(text)
        return {"message_id": len(self.replies)}

    async def send_private(self, user_id, message):
        self.sent.append({"target": "private", "user_id": user_id, "message": message})

    async def send_group(self, group_id, message):
        self.sent.append({"target": "group", "group_id": group_id, "message": message})


def _ai_cfg(**overrides) -> dict:
    cfg = {
        "enabled": True,
        "provider": "openai",
        "base_url": "",
        "api_key": "key",
        "auth": "",
        "model": "m",
        "temperature": 0.7,
        "max_tokens": 500,
        "timeout_seconds": 10,
        "system_prompt": "你是助手",
        "trigger": {
            "private": True,
            "group_at": True,
            "group_quote": True,
            "group_followup_seconds": 0,
            "group_prefix": [],
            "group_name_aliases": [],
        },
    }
    cfg.update(overrides)
    return cfg


def _image_cfg(**overrides) -> dict:
    cfg = {
        "enabled": True,
        "provider": "openai",
        "base_url": "",
        "api_key": "img-key",
        "model": "gpt-image-1",
        "size": "1024x1024",
        "timeout_seconds": 30,
        "triggers": ["画一张"],
    }
    cfg.update(overrides)
    return cfg


def _python_cfg(**overrides) -> dict:
    cfg = {"enabled": True}
    cfg.update(overrides)
    return cfg


def _make_ctx(root, ai_cfg=None, image_cfg=None, python_cfg=None) -> FakeCtx:
    return FakeCtx(
        root,
        {
            "ai_chat": ai_cfg if ai_cfg is not None else _ai_cfg(),
            "image_gen": image_cfg if image_cfg is not None else _image_cfg(),
            "python_run": python_cfg if python_cfg is not None else _python_cfg(enabled=False),
            "dev_tools": {"enabled": False},
            "skills": {"enabled": False},
        },
    )


def _event(text: str, *, private: bool = True, mention: bool = False) -> dict:
    segs = []
    if mention:
        segs.append({"type": "at", "data": {"qq": str(SELF_ID)}})
    if text:
        segs.append({"type": "text", "data": {"text": text}})
    event = {
        "post_type": "message",
        "message_type": "private" if private else "group",
        "user_id": 20002,
        "message": segs,
    }
    if not private:
        event["group_id"] = 12345
    return event


def _tool_call(call_id: str, name: str, arguments: dict | str) -> dict:
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments, ensure_ascii=False)
    return {"id": call_id, "name": name, "arguments": arguments}


class AiImageToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.ctx = _make_ctx(self.root)
        self._orig_tool = ai_chat.providers.chat_with_tools
        self._orig_chat = ai_chat.providers.chat
        self.addCleanup(
            setattr, ai_chat.providers, "chat_with_tools", self._orig_tool
        )
        self.addCleanup(setattr, ai_chat.providers, "chat", self._orig_chat)
        self.tool_calls: list = []
        self.plain_calls: list = []
        self.tool_script: list = []
        self.generate_calls: list = []
        self.generate_error: Exception | None = None
        self.generate_images: list = [TINY_PNG]
        self._orig_generate_for_ai = imagegen.generate_for_ai
        self.addCleanup(
            setattr, imagegen, "generate_for_ai", self._orig_generate_for_ai
        )

    def install_models(self, script=None):
        self.tool_script = list(script or [])

        def fake_tool(**kwargs):
            self.tool_calls.append(kwargs)
            if self.tool_script:
                return self.tool_script.pop(0)
            return {"content": "默认回答", "tool_calls": []}

        def fake_plain(**kwargs):
            self.plain_calls.append(kwargs)
            return "普通回答"

        ai_chat.providers.chat_with_tools = fake_tool
        ai_chat.providers.chat = fake_plain

    def install_generate(self):
        def fake_generate(*args, **kwargs):
            self.generate_calls.append((args, kwargs))
            if self.generate_error is not None:
                raise self.generate_error
            return list(self.generate_images)

        imagegen.generate_for_ai = fake_generate

    async def run_event(self, event) -> bool:
        return await ai_chat.Plugin(self.ctx).on_event(event)

    # ---------------- schema / 可用性 ----------------
    async def test_tool_schema_and_hint(self):
        self.install_models([{"content": "好的", "tool_calls": []}])
        await self.run_event(_event("画个猫"))
        tools = self.tool_calls[0]["tools"]
        names = [tool["function"]["name"] for tool in tools]
        self.assertIn("generate_image", names)
        spec = next(t for t in tools if t["function"]["name"] == "generate_image")
        self.assertIn("不要用代码", spec["function"]["description"])
        self.assertIn("prompt", spec["function"]["parameters"]["properties"])
        self.assertIn("size", spec["function"]["parameters"]["properties"])
        self.assertEqual(
            spec["function"]["parameters"]["properties"]["size"]["enum"],
            ["1024x1024", "1024x1792", "1792x1024"],
        )
        system = self.tool_calls[0]["messages"][0]["content"]
        self.assertIn(ai_chat.LOCAL_IMAGE_TOOL_HINT, system)

    async def test_image_gen_disabled_no_image_tool(self):
        self.ctx._cfg["image_gen"]["enabled"] = False
        self.install_models()
        await self.run_event(_event("画个猫"))
        # 没有 run_python 也没有 generate_image -> 走普通聊天
        self.assertEqual(self.tool_calls, [])
        self.assertEqual(len(self.plain_calls), 1)
        system = self.plain_calls[0]["messages"][0]["content"]
        self.assertNotIn(ai_chat.LOCAL_IMAGE_TOOL_HINT, system)

    async def test_no_credentials_no_tools(self):
        self.ctx._cfg["ai_chat"].update({"api_key": "", "auth": ""})
        self.ctx._cfg["image_gen"]["api_key"] = ""
        self.install_models()
        handled = await self.run_event(_event("画个猫"))
        self.assertTrue(handled)
        self.assertEqual(self.plain_calls, [])
        self.assertEqual(self.tool_calls, [])
        self.assertIn("AI 未配置", self.ctx.replies[-1])

    async def test_chatgpt_account_uses_intent_routing_instead_of_native_tools(self):
        # 账号模式没有原生 function calling，改为意图路由（详见 test_ai_tool_routing.py）
        self.ctx._cfg["ai_chat"].update({"auth": "chatgpt", "api_key": ""})
        self.ctx._cfg["image_gen"]["api_key"] = ""
        self.install_models()
        await self.run_event(_event("画个猫"))
        self.assertEqual(self.tool_calls, [])
        self.assertGreaterEqual(len(self.plain_calls), 1)

    async def test_other_account_no_image_tool(self):
        self.ctx._cfg["ai_chat"].update({"auth": "claude", "api_key": ""})
        self.ctx._cfg["image_gen"]["api_key"] = ""
        self.install_models()
        await self.run_event(_event("画个猫"))
        self.assertEqual(self.tool_calls, [])
        self.assertEqual(len(self.plain_calls), 1)

    async def test_both_tools_available(self):
        self.ctx._cfg["python_run"] = _python_cfg(enabled=True)
        self.install_models([{"content": "好的", "tool_calls": []}])
        await self.run_event(_event("画个猫"))
        names = {t["function"]["name"] for t in self.tool_calls[0]["tools"]}
        self.assertEqual(names, {"run_python", "generate_image"})

    # ---------------- 执行 ----------------
    async def test_generate_image_call_sends_photo_and_returns_to_model(self):
        self.install_generate()
        self.install_models(
            [
                {
                    "content": "",
                    "tool_calls": [
                        _tool_call(
                            "img_1",
                            "generate_image",
                            {"prompt": "一只橘猫", "size": "1024x1792"},
                        )
                    ],
                },
                {"content": "画好啦", "tool_calls": []},
            ]
        )
        handled = await self.run_event(_event("画一只橘猫"))
        self.assertTrue(handled)
        self.assertEqual(len(self.generate_calls), 1)
        args, _kwargs = self.generate_calls[0]
        self.assertEqual(args[2], "一只橘猫")  # prompt
        self.assertEqual(args[3], "1024x1792")  # size
        self.assertEqual(len(self.ctx.sent), 1)
        self.assertEqual(self.ctx.sent[0]["target"], "private")
        self.assertEqual(self.ctx.sent[0]["user_id"], 20002)
        segments = self.ctx.sent[0]["message"]
        self.assertTrue(segments)
        self.assertTrue(all(seg["type"] == "image" for seg in segments))
        self.assertTrue(segments[0]["data"]["file"].startswith("base64://"))
        tool_messages = [
            m
            for m in self.tool_calls[1]["messages"]
            if m.get("role") == "tool"
        ]
        self.assertEqual(len(tool_messages), 1)
        payload = json.loads(tool_messages[0]["content"])
        self.assertEqual(payload["ok"], True)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["note"], "图片已发送")
        self.assertIn("画好啦", self.ctx.replies[-1])

    async def test_group_image_sent_to_group(self):
        self.ctx.api.member = {"role": "owner"}
        self.install_generate()
        self.install_models(
            [
                {
                    "content": "",
                    "tool_calls": [
                        _tool_call("g1", "generate_image", {"prompt": "猫"})
                    ],
                },
                {"content": "发群里了", "tool_calls": []},
            ]
        )
        await self.run_event(_event("画个猫", private=False, mention=True))
        self.assertEqual(self.ctx.sent[0]["target"], "group")
        self.assertEqual(self.ctx.sent[0]["group_id"], 12345)

    async def test_two_images_sent_count(self):
        self.generate_images = [TINY_PNG, TINY_PNG]
        self.install_generate()
        self.install_models(
            [
                {
                    "content": "",
                    "tool_calls": [
                        _tool_call("i2", "generate_image", {"prompt": "猫"})
                    ],
                },
                {"content": "两张", "tool_calls": []},
            ]
        )
        await self.run_event(_event("画两只猫"))
        segments = self.ctx.sent[0]["message"]
        self.assertEqual(len(segments), 2)
        payload = json.loads(
            [
                m
                for m in self.tool_calls[1]["messages"]
                if m.get("role") == "tool"
            ][0]["content"]
        )
        self.assertEqual(payload["count"], 2)

    async def test_generation_error_returned_to_model(self):
        self.generate_error = imagegen.ImageGenError("画图接口 500")
        self.install_generate()
        self.install_models(
            [
                {
                    "content": "",
                    "tool_calls": [
                        _tool_call("bad", "generate_image", {"prompt": "猫"})
                    ],
                },
                {"content": "没画出来", "tool_calls": []},
            ]
        )
        await self.run_event(_event("画个猫"))
        self.assertEqual(self.ctx.sent, [])
        tool_messages = [
            m for m in self.tool_calls[1]["messages"] if m.get("role") == "tool"
        ]
        payload = json.loads(tool_messages[0]["content"])
        self.assertEqual(payload["error"], "画图接口 500")
        self.assertIn("没画出来", self.ctx.replies[-1])

    async def test_missing_prompt_argument(self):
        self.install_generate()
        self.install_models(
            [
                {
                    "content": "",
                    "tool_calls": [_tool_call("empty", "generate_image", {})],
                },
                {"content": "参数不对", "tool_calls": []},
            ]
        )
        await self.run_event(_event("画图"))
        self.assertEqual(self.generate_calls, [])
        tool_messages = [
            m for m in self.tool_calls[1]["messages"] if m.get("role") == "tool"
        ]
        payload = json.loads(tool_messages[0]["content"])
        self.assertIn("prompt", payload["error"])

    async def test_invalid_size_falls_back_to_default(self):
        self.install_generate()
        self.install_models(
            [
                {
                    "content": "",
                    "tool_calls": [
                        _tool_call(
                            "s1", "generate_image", {"prompt": "猫", "size": "999x999"}
                        )
                    ],
                },
                {"content": "好了", "tool_calls": []},
            ]
        )
        await self.run_event(_event("画个猫"))
        args, _ = self.generate_calls[0]
        self.assertEqual(args[3], "1024x1024")

    async def test_run_python_and_image_tools_dispatch_by_name(self):
        self.ctx._cfg["python_run"] = _python_cfg(enabled=True)
        self.install_generate()
        self.install_models(
            [
                {
                    "content": "",
                    "tool_calls": [
                        _tool_call(
                            "py1", "run_python", {"code": "print('tool py ok')"}
                        )
                    ],
                },
                {"content": "跑完了", "tool_calls": []},
            ]
        )
        await self.run_event(_event("帮我算一下"))
        # imagegen 没有被调用，run_python 正常执行
        self.assertEqual(self.generate_calls, [])
        tool_messages = [
            m for m in self.tool_calls[1]["messages"] if m.get("role") == "tool"
        ]
        payload = json.loads(tool_messages[0]["content"])
        self.assertIn("tool py ok", payload["stdout"])
        self.assertIn("跑完了", self.ctx.replies[-1])


if __name__ == "__main__":
    unittest.main()
