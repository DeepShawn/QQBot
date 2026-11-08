"""task-25：识图 auth 接线、图片工具权限门、账号/非 OpenAI 模式的意图路由。全离线 stub。"""
from __future__ import annotations

import asyncio
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
import pytool  # noqa: E402
from plugins import ai_chat  # noqa: E402

TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
SELF_ID = 999
IMAGE_URL = "http://example.com/x.png"


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
    def __init__(self, root, plugin_cfg=None, admins=()):
        self.data_dir = Path(root)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "files").mkdir(parents=True, exist_ok=True)
        self.log = FakeLog()
        self.self_id = SELF_ID
        self.bot_name = "测试机器人"
        self.api = FakeApi()
        self._cfg = plugin_cfg or {}
        self._admins = [str(item) for item in admins]
        self.replies: list[str] = []
        self.sent: list[dict] = []

    def plugin_cfg(self, name: str) -> dict:
        return self._cfg.get(name) or {}

    def is_admin(self, user_id) -> bool:
        return str(user_id) in self._admins

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
        "vision": {
            "enabled": True,
            "provider": "",
            "base_url": "",
            "api_key": "",
            "model": "",
            "timeout_seconds": 60,
            "prompt": "请描述图片",
        },
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
    }
    cfg.update(overrides)
    return cfg


def _python_cfg(**overrides) -> dict:
    cfg = {"enabled": True}
    cfg.update(overrides)
    return cfg


def _make_ctx(root, ai_cfg=None, image_cfg=None, python_cfg=None, admins=()) -> FakeCtx:
    return FakeCtx(
        root,
        {
            "ai_chat": ai_cfg if ai_cfg is not None else _ai_cfg(),
            "image_gen": image_cfg if image_cfg is not None else _image_cfg(),
            "python_run": python_cfg
            if python_cfg is not None
            else _python_cfg(enabled=False),
            "dev_tools": {"enabled": False},
            "skills": {"enabled": False},
        },
        admins=admins,
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


class RoutingTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.ctx = _make_ctx(self.root)
        self.chat_calls: list = []
        self.chat_script: list = []
        self.tool_calls: list = []
        self.image_calls: list = []
        self.image_result: list = [TINY_PNG]
        self.image_error: Exception | None = None
        self.python_calls: list = []
        self.python_result: dict | None = None
        self._orig_chat = ai_chat.providers.chat
        self._orig_tool = ai_chat.providers.chat_with_tools
        self._orig_generate_for_ai = imagegen.generate_for_ai
        self._orig_execute_python = pytool.execute_python
        self.addCleanup(setattr, ai_chat.providers, "chat", self._orig_chat)
        self.addCleanup(
            setattr, ai_chat.providers, "chat_with_tools", self._orig_tool
        )
        self.addCleanup(
            setattr, imagegen, "generate_for_ai", self._orig_generate_for_ai
        )
        self.addCleanup(
            setattr, pytool, "execute_python", self._orig_execute_python
        )

    # ---------------- stub 工具 ----------------
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

    def install_native_tools(self, script=None):
        self.native_script = list(script or [])

        def fake_tool(**kwargs):
            self.tool_calls.append(kwargs)
            if not self.native_script:
                return {"content": "默认回答", "tool_calls": []}
            return self.native_script.pop(0)

        ai_chat.providers.chat_with_tools = fake_tool

    def install_image(self):
        def fake_generate(*args, **kwargs):
            self.image_calls.append((args, kwargs))
            if self.image_error is not None:
                raise self.image_error
            return list(self.image_result)

        imagegen.generate_for_ai = fake_generate

    def install_python(self):
        def fake_execute(code=None, file=None, timeout=30, data_dir=None):
            self.python_calls.append(
                {"code": code, "file": file, "timeout": timeout, "data_dir": data_dir}
            )
            if self.python_result is not None:
                return dict(self.python_result)
            return {
                "stdout": "42\n",
                "stderr": "",
                "exit_code": 0,
                "timed_out": False,
                "duration": 0.01,
            }

        pytool.execute_python = fake_execute

    async def run_event(self, event) -> bool:
        return await ai_chat.Plugin(self.ctx).on_event(event)

    def last_chat_system_text(self, call_index: int = -1) -> str:
        messages = self.chat_calls[call_index]["messages"]
        return "\n".join(
            str(m.get("content") or "")
            for m in messages
            if m.get("role") == "system"
        )


class IntentRoutingTests(RoutingTestCase):
    def _account_ctx(self, **overrides):
        self.ctx = _make_ctx(
            self.root,
            ai_cfg=_ai_cfg(auth="chatgpt", api_key="", **overrides),
            image_cfg=_image_cfg(api_key="", auth="chatgpt"),
            python_cfg=_python_cfg(enabled=False),
        )

    async def test_route_none_falls_back_to_plain_chat(self):
        self._account_ctx()
        self.install_chat(['{"tool":"none"}', "普通回答"])
        handled = await self.run_event(_event("你好"))
        self.assertTrue(handled)
        self.assertEqual(len(self.chat_calls), 2)
        self.assertEqual(self.ctx.sent, [])
        self.assertIn("普通回答", self.ctx.replies[-1])

    async def test_route_generate_image_executes_and_replies(self):
        self._account_ctx()
        long_prompt = "一只毛茸茸的橘猫坐在窗台上，水彩风格，暖色阳光，竖构图"
        self.install_chat(
            [
                '好的，判断如下：\n```json\n'
                '{"tool":"generate_image","prompt":"'
                + long_prompt
                + '","size":"1024x1792"}\n```',
                "画好啦",
            ]
        )
        self.install_image()
        handled = await self.run_event(_event("画一只橘猫"))
        self.assertTrue(handled)
        self.assertEqual(len(self.image_calls), 1)
        args, _ = self.image_calls[0]
        self.assertEqual(args[2], long_prompt)  # 模型自己写的 prompt，未被改写
        self.assertEqual(args[3], "1024x1792")  # size
        self.assertEqual(len(self.chat_calls), 2)  # 路由 + 最终回复，无额外改写
        self.assertEqual(len(self.ctx.sent), 1)
        self.assertEqual(self.ctx.sent[0]["target"], "private")
        segments = self.ctx.sent[0]["message"]
        self.assertTrue(segments and all(seg["type"] == "image" for seg in segments))
        self.assertEqual(len(self.chat_calls), 2)
        # 第一次是路由调用：temperature=0、max_tokens=300、带账号 auth
        route_call = self.chat_calls[0]
        self.assertEqual(route_call["temperature"], 0.0)
        self.assertEqual(route_call["max_tokens"], ai_chat.ROUTE_MAX_TOKENS)
        self.assertEqual(route_call["auth"], "chatgpt")
        system = str(route_call["messages"][0]["content"])
        self.assertIn("工具路由器", system)
        self.assertIn("generate_image", system)
        self.assertNotIn("run_python", system)
        # 第二次带上工具执行结果
        final_system = self.last_chat_system_text()
        self.assertIn("工具执行结果", final_system)
        self.assertIn("图片已发送", final_system)
        self.assertIn("画好啦", self.ctx.replies[-1])

    async def test_route_run_python_executes_and_replies(self):
        self.ctx = _make_ctx(
            self.root,
            ai_cfg=_ai_cfg(auth="chatgpt", api_key=""),
            image_cfg=_image_cfg(api_key=""),
            python_cfg=_python_cfg(enabled=True),
        )
        self.install_chat(['{"tool":"run_python","code":"print(6*7)"}', "算好了"])
        self.install_python()
        handled = await self.run_event(_event("帮我算 6*7"))
        self.assertTrue(handled)
        self.assertEqual(len(self.python_calls), 1)
        self.assertEqual(self.python_calls[0]["code"], "print(6*7)")
        self.assertEqual(self.ctx.sent, [])
        final_system = self.last_chat_system_text()
        self.assertIn("run_python", final_system)
        self.assertIn("42", final_system)
        self.assertIn("算好了", self.ctx.replies[-1])

    async def test_route_run_python_by_file(self):
        self.ctx = _make_ctx(
            self.root,
            ai_cfg=_ai_cfg(auth="chatgpt", api_key=""),
            image_cfg=_image_cfg(api_key=""),
            python_cfg=_python_cfg(enabled=True),
        )
        self.install_chat(['{"tool":"run_python","file":"a.py"}', "跑了"])
        self.install_python()
        await self.run_event(_event("运行 a.py"))
        self.assertEqual(self.python_calls[0]["file"], "a.py")

    async def test_bad_route_json_falls_back_to_plain_chat(self):
        self._account_ctx()
        self.install_chat(["这不是 JSON", "普通回答"])
        self.install_image()
        handled = await self.run_event(_event("画个猫"))
        self.assertTrue(handled)
        self.assertEqual(self.image_calls, [])
        self.assertEqual(len(self.chat_calls), 2)
        self.assertIn("普通回答", self.ctx.replies[-1])

    async def test_tool_error_appended_to_final_chat(self):
        self._account_ctx()
        self.install_chat(
            [
                '{"tool":"generate_image","prompt":"猫"}',
                "一只猫，水彩风格，暖色阳光，简洁背景",
                "画图失败了",
            ]
        )
        self.install_image()
        self.image_error = imagegen.ImageGenError("画图接口 500")
        await self.run_event(_event("画个猫"))
        self.assertEqual(self.ctx.sent, [])
        final_system = self.last_chat_system_text()
        self.assertIn("画图接口 500", final_system)
        self.assertIn("画图失败了", self.ctx.replies[-1])

    async def test_unavailable_tool_in_route_ignored(self):
        self._account_ctx()  # 只有 image 可用，python 被禁用
        self.install_chat(['{"tool":"run_python","code":"print(1)"}', "普通回答"])
        self.install_python()
        await self.run_event(_event("跑一下代码"))
        self.assertEqual(self.python_calls, [])
        self.assertIn("普通回答", self.ctx.replies[-1])

    async def test_route_only_when_not_native(self):
        # API Key + OpenAI：走原生 tools，不应触发路由（providers.chat 不被调用）
        self.ctx = _make_ctx(
            self.root,
            ai_cfg=_ai_cfg(),
            image_cfg=_image_cfg(api_key="img-key"),
            python_cfg=_python_cfg(enabled=False),
        )
        self.install_chat()
        self.install_native_tools(
            [
                {"content": "好的", "tool_calls": []},
            ]
        )
        self.install_image()
        await self.run_event(_event("画个猫"))
        self.assertEqual(self.chat_calls, [])
        self.assertEqual(len(self.tool_calls), 1)
        names = [t["function"]["name"] for t in self.tool_calls[0]["tools"]]
        self.assertEqual(names, ["generate_image"])

    async def test_group_member_denied_image_routing(self):
        self._account_ctx()
        self.ctx.api.member = {"role": "member"}
        self.install_chat(["普通回答"])
        self.install_image()
        handled = await self.run_event(_event("画个猫", private=False, mention=True))
        self.assertTrue(handled)
        self.assertEqual(self.image_calls, [])
        self.assertEqual(len(self.chat_calls), 1)  # 没有路由调用，直接普通聊天
        self.assertIn("普通回答", self.ctx.replies[-1])

    async def test_group_owner_allowed_image_routing(self):
        self._account_ctx()
        self.ctx.api.member = {"role": "owner"}
        self.install_chat(['{"tool":"generate_image","prompt":"猫"}', "画好啦"])
        self.install_image()
        await self.run_event(_event("画个猫", private=False, mention=True))
        self.assertEqual(len(self.image_calls), 1)
        self.assertEqual(self.ctx.sent[0]["target"], "group")

    async def test_route_history_limits_and_json_parser(self):
        plugin = ai_chat.Plugin(self.ctx)
        history = [
            {"role": "user" if index % 2 == 0 else "assistant", "content": f"历史{index}"}
            for index in range(12)
        ]
        captured: dict = {}

        def fake_chat(**kwargs):
            captured.update(kwargs)
            return '{"tool":"none"}'

        _patch(self, ai_chat.providers, "chat", fake_chat)
        _patch(self, ai_chat.providers, "chat_with_tools", lambda **k: self.fail("不应走原生工具"))
        result = await plugin._route_tool_chat(
            _event("你好"),
            _ai_cfg(auth="chatgpt", api_key=""),
            [{"role": "user", "content": "你好"}],
            history,
            "你好",
            True,
            False,
        )
        self.assertIsNone(result)
        messages = captured["messages"]
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(len(messages), 1 + ai_chat.ROUTE_MAX_HISTORY + 1)
        self.assertEqual(messages[-1], {"role": "user", "content": "你好"})
        self.assertEqual(messages[1]["content"], "历史6")
        # 解析容错
        self.assertEqual(
            ai_chat.Plugin._parse_route_json(
                "前言 ```json\n{\"tool\":\"none\"}\n``` 后记"
            ),
            {"tool": "none"},
        )
        self.assertIsNone(ai_chat.Plugin._parse_route_json("不是 json"))
        self.assertIsNone(ai_chat.Plugin._parse_route_json(""))
        self.assertEqual(
            ai_chat.Plugin._parse_route_json('{"tool":"generate_image","prompt":"a{b}c"}'),
            {"tool": "generate_image", "prompt": "a{b}c"},
        )


class ImagePermissionGateTests(RoutingTestCase):
    def _native_ctx(self, admins=()):
        self.ctx = _make_ctx(
            self.root,
            ai_cfg=_ai_cfg(),
            image_cfg=_image_cfg(api_key="img-key"),
            python_cfg=_python_cfg(enabled=False),
            admins=admins,
        )

    async def test_private_user_gets_image_tool(self):
        self._native_ctx()
        self.install_chat()
        self.install_native_tools([{"content": "好的", "tool_calls": []}])
        await self.run_event(_event("画个猫"))
        names = [t["function"]["name"] for t in self.tool_calls[0]["tools"]]
        self.assertIn("generate_image", names)

    async def test_group_member_has_no_image_tool(self):
        self._native_ctx()
        self.ctx.api.member = {"role": "member"}
        self.install_chat(["普通回答"])
        self.install_native_tools()
        await self.run_event(_event("画个猫", private=False, mention=True))
        self.assertEqual(self.tool_calls, [])
        self.assertGreaterEqual(len(self.chat_calls), 1)

    async def test_group_owner_gets_image_tool(self):
        self._native_ctx()
        self.ctx.api.member = {"role": "owner"}
        self.install_chat()
        self.install_native_tools([{"content": "好的", "tool_calls": []}])
        await self.run_event(_event("画个猫", private=False, mention=True))
        names = [t["function"]["name"] for t in self.tool_calls[0]["tools"]]
        self.assertIn("generate_image", names)

    async def test_group_bot_admin_gets_image_tool(self):
        self._native_ctx(admins=[20002])
        self.install_chat()
        self.install_native_tools([{"content": "好的", "tool_calls": []}])
        await self.run_event(_event("画个猫", private=False, mention=True))
        names = [t["function"]["name"] for t in self.tool_calls[0]["tools"]]
        self.assertIn("generate_image", names)


class VisionAuthTests(RoutingTestCase):
    def setUp(self):
        super().setUp()
        self.describe_calls: list = []
        self.ctx = _make_ctx(self.root)
        self._orig_fetch = ai_chat.media.fetch
        self.addCleanup(setattr, ai_chat.media, "fetch", self._orig_fetch)
        ai_chat.media.fetch = lambda url, max_bytes=0, **kwargs: (TINY_PNG, "image/png")
        self._orig_describe = ai_chat.providers.describe_images
        self.addCleanup(
            setattr, ai_chat.providers, "describe_images", self._orig_describe
        )

    def install_describe(self):
        def fake_describe(**kwargs):
            self.describe_calls.append(kwargs)
            return "图片里是一只猫"

        ai_chat.providers.describe_images = fake_describe

    def _attachments(self):
        return [{"type": "image", "data": {"url": IMAGE_URL}, "quoted": False}]

    def _cfg(self, **overrides) -> dict:
        cfg = _ai_cfg()
        cfg.update(overrides)
        return cfg

    async def test_vision_api_key_uses_api_key_mode(self):
        self.install_describe()
        cfg = self._cfg(auth="chatgpt", api_key="")
        cfg["vision"]["api_key"] = "vision-key"
        cfg["vision"]["model"] = "vision-model"
        plugin = ai_chat.Plugin(self.ctx)
        text = await plugin._attachment_context(_event("看图"), self._attachments(), cfg)
        self.assertIn("图片里是一只猫", text)
        call = self.describe_calls[0]
        self.assertEqual(call["auth"], "")
        self.assertEqual(call["api_key"], "vision-key")
        self.assertEqual(call["model"], "vision-model")

    async def test_vision_falls_back_to_account_auth(self):
        self.install_describe()
        cfg = self._cfg(auth="chatgpt", api_key="", model="cfg-model")
        cfg["vision"]["api_key"] = ""
        cfg["vision"]["model"] = ""
        plugin = ai_chat.Plugin(self.ctx)
        await plugin._attachment_context(_event("看图"), self._attachments(), cfg)
        call = self.describe_calls[0]
        self.assertEqual(call["auth"], "chatgpt")
        self.assertEqual(call["api_key"], "")
        self.assertEqual(call["model"], "cfg-model")

    async def test_vision_uses_ai_key_when_no_auth(self):
        self.install_describe()
        cfg = self._cfg(auth="", api_key="ai-key", model="ai-model")
        cfg["vision"]["api_key"] = ""
        plugin = ai_chat.Plugin(self.ctx)
        await plugin._attachment_context(_event("看图"), self._attachments(), cfg)
        call = self.describe_calls[0]
        self.assertEqual(call["auth"], "")
        self.assertEqual(call["api_key"], "ai-key")
        self.assertEqual(call["model"], "ai-model")


if __name__ == "__main__":
    unittest.main()
