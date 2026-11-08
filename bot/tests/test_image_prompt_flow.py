"""T31 主模型自写生图提示词 + 画图后由主模型总结回复。全离线 stub。"""
from __future__ import annotations

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

TINY_PNG = b"\x89PNG\r\n\x1a\n" + b"png-body"
SELF_ID = 999
LONG_PROMPT = "一只毛茸茸的橘猫坐在窗台上，水彩风格，暖色阳光，竖构图，柔和阴影"


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
        self.member = {"role": "member"}
        self.calls: list = []

    async def call(self, action, **params):
        self.calls.append((action, params))
        if action == "get_group_member_info":
            return dict(self.member)
        return {}


class FakeCtx:
    def __init__(self, root, plugin_cfg=None):
        self.data_dir = Path(root)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log = FakeLog()
        self.self_id = SELF_ID
        self.bot_name = "测试机器人"
        self.api = FakeApi()
        self._cfg = plugin_cfg or {}
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


def _make_ctx(root, ai_cfg=None) -> FakeCtx:
    return FakeCtx(
        root,
        {
            "ai_chat": ai_cfg if ai_cfg is not None else _ai_cfg(),
            "image_gen": {
                "enabled": True,
                "auth": "chatgpt",
                "api_key": "",
                "follow_ai": False,
                "size": "1024x1024",
                "timeout_seconds": 30,
            },
            "python_run": {"enabled": False},
            "dev_tools": {"enabled": False},
            "skills": {"enabled": False},
        },
    )


def _event(text: str = "画一只橘猫") -> dict:
    return {
        "post_type": "message",
        "message_type": "private",
        "user_id": 20002,
        "message": [{"type": "text", "data": {"text": text}}],
    }


def _tool_call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": call_id,
        "name": name,
        "arguments": json.dumps(arguments, ensure_ascii=False),
    }


class PromptFlowBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.ctx = _make_ctx(self.root)
        self.chat_calls: list = []
        self.chat_script: list = []
        self.native_calls: list = []
        self.native_script: list = []
        self.image_calls: list = []
        self._orig_chat = ai_chat.providers.chat
        self._orig_native = ai_chat.providers.chat_with_tools
        self._orig_generate = imagegen.generate_for_ai
        self.addCleanup(setattr, ai_chat.providers, "chat", self._orig_chat)
        self.addCleanup(
            setattr, ai_chat.providers, "chat_with_tools", self._orig_native
        )
        self.addCleanup(setattr, imagegen, "generate_for_ai", self._orig_generate)

    def install_chat(self, script=None):
        self.chat_script = list(script or [])

        def fake_chat(**kwargs):
            self.chat_calls.append(kwargs)
            if not self.chat_script:
                return "普通回答"
            item = self.chat_script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        ai_chat.providers.chat = fake_chat

    def install_native(self, script=None):
        self.native_script = list(script or [])

        def fake_native(**kwargs):
            self.native_calls.append(kwargs)
            if not self.native_script:
                return {"content": "默认回答", "tool_calls": []}
            return self.native_script.pop(0)

        ai_chat.providers.chat_with_tools = fake_native

    def install_image(self):
        def fake_generate(*args, **kwargs):
            self.image_calls.append((args, kwargs))
            return [TINY_PNG]

        imagegen.generate_for_ai = fake_generate

    async def run_event(self, event=None) -> bool:
        return await ai_chat.Plugin(self.ctx).on_event(event or _event())

    def last_reply(self) -> str:
        self.assertTrue(self.ctx.replies, "没有回复")
        return self.ctx.replies[-1]

    def chat_system_text(self, call_index: int = -1) -> str:
        return "\n".join(
            str(m.get("content") or "")
            for m in self.chat_calls[call_index]["messages"]
            if m.get("role") == "system"
        )

    def account_ctx(self):
        self.ctx = _make_ctx(self.root, ai_cfg=_ai_cfg(auth="chatgpt", api_key=""))


# ---------------------------------------------------------------- 工具描述


class ImageToolSpecTests(unittest.TestCase):
    def test_description_requires_self_written_prompt(self):
        description = ai_chat.IMAGE_TOOL_SPEC["function"]["description"]
        self.assertIn("提示词", description)
        self.assertIn("不要只复述", description)
        prompt_desc = ai_chat.IMAGE_TOOL_SPEC["function"]["parameters"]["properties"][
            "prompt"
        ]["description"]
        self.assertIn("不是照抄用户原话", prompt_desc)


class WeakPromptTests(unittest.TestCase):
    def test_short_or_repeated_prompt_is_weak(self):
        self.assertTrue(ai_chat.Plugin._image_prompt_weak("", "画一只猫"))
        self.assertTrue(ai_chat.Plugin._image_prompt_weak("猫", "画一只猫"))
        self.assertTrue(ai_chat.Plugin._image_prompt_weak("画一只猫", "画一只猫"))
        self.assertTrue(ai_chat.Plugin._image_prompt_weak("一只猫", "画一只猫"))
        self.assertTrue(
            ai_chat.Plugin._image_prompt_weak("画一只猫在窗台", "画一只猫")
        )

    def test_specific_prompt_is_not_weak(self):
        self.assertFalse(
            ai_chat.Plugin._image_prompt_weak(LONG_PROMPT, "画一只橘猫")
        )


# ---------------------------------------------------------------- 账号路由模式


class RoutePromptRewriteTests(PromptFlowBase):
    async def test_route_prompt_requires_self_written_prompt(self):
        self.account_ctx()
        self.install_chat(['{"tool":"none"}', "普通回答"])
        await self.run_event(_event("你好"))
        system = str(self.chat_calls[0]["messages"][0]["content"])
        self.assertIn("自己", system)
        self.assertIn("提示词", system)
        self.assertIn("不要照抄用户原话", system)

    async def test_specific_route_prompt_not_rewritten(self):
        self.account_ctx()
        self.install_chat(
            [
                json.dumps({"tool": "generate_image", "prompt": LONG_PROMPT}),
                "画好啦，一只橘猫在窗台上晒太阳",
            ]
        )
        self.install_image()
        await self.run_event(_event("画一只橘猫"))
        self.assertEqual(len(self.image_calls), 1)
        self.assertEqual(self.image_calls[0][0][2], LONG_PROMPT)
        self.assertEqual(len(self.chat_calls), 2)  # 路由 + 最终回复，无改写
        self.assertIn("图片已发送", self.chat_system_text())

    async def test_short_route_prompt_rewritten(self):
        self.account_ctx()
        rewritten = "一只橘色小猫趴在木窗台上，水彩风格，暖色阳光，竖幅构图"
        self.install_chat(
            [
                '{"tool":"generate_image","prompt":"猫"}',
                rewritten,
                "画好啦",
            ]
        )
        self.install_image()
        await self.run_event(_event("画一只橘猫"))
        self.assertEqual(len(self.chat_calls), 3)
        rewrite_system = str(self.chat_calls[1]["messages"][0]["content"])
        self.assertIn("改写", rewrite_system)
        self.assertEqual(self.chat_calls[1]["temperature"], 0.7)
        self.assertEqual(self.chat_calls[1]["max_tokens"], 300)
        self.assertEqual(self.image_calls[0][0][2], rewritten)
        tool_output = self.chat_system_text()
        self.assertIn('"prompt"', tool_output)
        self.assertIn('"count"', tool_output)

    async def test_repeated_prompt_rewritten(self):
        self.account_ctx()
        rewritten = "一只猫坐在洒满阳光的窗台上，水彩插画风格，暖色调，竖构图"
        self.install_chat(
            [
                '{"tool":"generate_image","prompt":"画一只猫"}',
                rewritten,
                "画好啦",
            ]
        )
        self.install_image()
        await self.run_event(_event("画一只猫"))
        self.assertEqual(self.image_calls[0][0][2], rewritten)

    async def test_rewrite_failure_falls_back_to_user_text(self):
        self.account_ctx()
        self.install_chat(
            [
                '{"tool":"generate_image","prompt":"猫"}',
                "",  # 改写返回空
                "画好啦",
            ]
        )
        self.install_image()
        await self.run_event(_event("画一只橘猫"))
        self.assertEqual(self.image_calls[0][0][2], "画一只橘猫")

    async def test_rewrite_exception_falls_back(self):
        self.account_ctx()
        self.install_chat(
            [
                '{"tool":"generate_image","prompt":"猫"}',
                providers.ProviderError("改写失败"),
                "画好啦",
            ]
        )
        self.install_image()
        await self.run_event(_event("画一只橘猫"))
        self.assertEqual(self.image_calls[0][0][2], "画一只橘猫")

    async def test_empty_final_reply_falls_back_text(self):
        self.account_ctx()
        self.install_chat(
            [
                json.dumps({"tool": "generate_image", "prompt": LONG_PROMPT}),
                "",  # 最终回复为空
            ]
        )
        self.install_image()
        await self.run_event(_event("画一只橘猫"))
        self.assertEqual(len(self.ctx.sent), 1)  # 只发一次图
        self.assertEqual(self.last_reply(), ai_chat.IMAGE_PROMPT_FALLBACK_REPLY)

    async def test_final_reply_used_when_present(self):
        self.account_ctx()
        self.install_chat(
            [
                json.dumps({"tool": "generate_image", "prompt": LONG_PROMPT}),
                "画好啦，是一只晒太阳的橘猫",
            ]
        )
        self.install_image()
        await self.run_event(_event("画一只橘猫"))
        self.assertIn("晒太阳的橘猫", self.last_reply())


# ---------------------------------------------------------------- 原生 tools 模式


class NativeImagePromptTests(PromptFlowBase):
    async def test_native_tool_description_requires_prompt(self):
        self.install_native([{"content": "好的", "tool_calls": []}])
        await self.run_event(_event("你好"))
        tools = self.native_calls[0]["tools"]
        spec = next(
            t for t in tools if t["function"]["name"] == "generate_image"
        )
        self.assertIn("提示词", spec["function"]["description"])
        self.assertIn("不要只复述", spec["function"]["description"])

    async def test_native_image_tool_result_has_prompt_and_count(self):
        prompt = "一只橘猫在窗台上，油画风格，暖色阳光，横幅构图"
        self.install_native(
            [
                {
                    "content": "",
                    "tool_calls": [
                        _tool_call("img", "generate_image", {"prompt": prompt})
                    ],
                },
                {"content": "画好啦", "tool_calls": []},
            ]
        )
        self.install_image()
        await self.run_event(_event("画一只橘猫"))
        tool_messages = [
            m
            for m in self.native_calls[1]["messages"]
            if m.get("role") == "tool"
        ]
        payload = json.loads(tool_messages[0]["content"])
        self.assertEqual(payload["prompt"], prompt)
        self.assertEqual(payload["count"], 1)
        self.assertIn("画好啦", self.last_reply())
        self.assertEqual(len(self.ctx.sent), 1)

    async def test_native_empty_final_after_image_falls_back(self):
        self.install_native(
            [
                {
                    "content": "",
                    "tool_calls": [
                        _tool_call("img", "generate_image", {"prompt": LONG_PROMPT})
                    ],
                },
                {"content": "", "tool_calls": []},
            ]
        )
        self.install_image()
        await self.run_event(_event("画一只橘猫"))
        self.assertEqual(self.last_reply(), ai_chat.IMAGE_PROMPT_FALLBACK_REPLY)
        self.assertEqual(len(self.ctx.sent), 1)
        self.assertEqual(self.chat_calls, [])  # 原生模式不额外走 providers.chat


class RewriteHelperTests(PromptFlowBase):
    async def test_rewrite_strips_fences_and_quotes(self):
        self.account_ctx()
        self.install_chat(
            ['```\n"一只猫，水彩风格，暖色光，竖构图"\n```']
        )
        plugin = ai_chat.Plugin(self.ctx)
        text = await plugin._rewrite_image_prompt(
            self.ctx.plugin_cfg("ai_chat"), "画一只猫"
        )
        self.assertEqual(text, "一只猫，水彩风格，暖色光，竖构图")

    async def test_rewrite_empty_returns_empty(self):
        self.account_ctx()
        self.install_chat(["  "])
        plugin = ai_chat.Plugin(self.ctx)
        text = await plugin._rewrite_image_prompt(
            self.ctx.plugin_cfg("ai_chat"), "画一只猫"
        )
        self.assertEqual(text, "")


if __name__ == "__main__":
    unittest.main()
