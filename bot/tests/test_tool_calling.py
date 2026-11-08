"""ai_chat 的 run_python 工具循环测试（stub 模型，真实 pytool 执行，不联网）。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent.parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import providers as shared_providers  # noqa: E402
from plugins import ai_chat  # noqa: E402

SELF_ID = 999


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
        self.fail_member = False
        self.calls: list = []

    async def call(self, action, **params):
        self.calls.append((action, params))
        if action == "get_group_member_info":
            if self.fail_member:
                raise RuntimeError("member lookup failed")
            return dict(self.member)
        return {}


class FakeCtx:
    def __init__(self, root, admins=(), ai_cfg=None, py_cfg=None):
        self.data_dir = Path(root)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "files").mkdir(parents=True, exist_ok=True)
        self.log = FakeLog()
        self.self_id = SELF_ID
        self.bot_name = "测试机器人"
        self.api = FakeApi()
        self._admins = [str(item) for item in admins]
        self._cfg = {
            "ai_chat": dict(ai_cfg or {}),
            "python_run": dict(py_cfg) if py_cfg is not None else {},
        }
        self.replies: list[str] = []

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


def default_ai_cfg(**overrides) -> dict:
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


def make_event(
    text: str,
    *,
    private: bool = True,
    mention: bool = False,
    user_id: int = 20002,
    group_id: int = 12345,
):
    segs = []
    if mention:
        segs.append({"type": "at", "data": {"qq": str(SELF_ID)}})
    if text:
        segs.append({"type": "text", "data": {"text": text}})
    event = {
        "post_type": "message",
        "message_type": "private" if private else "group",
        "user_id": user_id,
        "message": segs,
    }
    if not private:
        event["group_id"] = group_id
    return event


def tool_call(call_id: str, arguments: dict | str) -> dict:
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments, ensure_ascii=False)
    return {"id": call_id, "name": "run_python", "arguments": arguments}


class ToolCallingTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.ctx = FakeCtx(self.root, admins=[10001])
        self.ctx._cfg["ai_chat"] = default_ai_cfg()
        self._orig_tool = ai_chat.providers.chat_with_tools
        self._orig_chat = ai_chat.providers.chat
        self.addCleanup(
            setattr, ai_chat.providers, "chat_with_tools", self._orig_tool
        )
        self.addCleanup(setattr, ai_chat.providers, "chat", self._orig_chat)
        self.tool_calls: list = []
        self.plain_calls: list = []
        self.tool_script: list = []

    # ---------------- 工具 ----------------
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

    def make_plugin(self):
        return ai_chat.Plugin(self.ctx)

    async def run_event(self, event) -> bool:
        return await self.make_plugin().on_event(event)

    def tool_messages(self, call_index: int = -1) -> list:
        messages = self.tool_calls[call_index]["messages"]
        return [m for m in messages if m.get("role") == "tool"]

    def write_file(self, name: str, code: str) -> Path:
        path = self.root / "files" / name
        path.write_text(code, encoding="utf-8")
        return path


class ToolLoopTests(ToolCallingTestCase):
    async def test_tools_schema_sent(self):
        self.install_models([{"content": "直接回答", "tool_calls": []}])
        handled = await self.run_event(make_event("你好"))
        self.assertTrue(handled)
        self.assertEqual(len(self.tool_calls), 1)
        tools = self.tool_calls[0]["tools"]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["function"]["name"], "run_python")
        self.assertIn("code", tools[0]["function"]["parameters"]["properties"])
        self.assertIn("file", tools[0]["function"]["parameters"]["properties"])
        system = self.tool_calls[0]["messages"][0]["content"]
        self.assertIn(ai_chat.LOCAL_PYTHON_TOOL_HINT, system)
        self.assertIn("直接回答", self.ctx.replies[-1])

    async def test_single_tool_call_executed(self):
        self.install_models(
            [
                {
                    "content": "",
                    "tool_calls": [tool_call("call_1", {"code": "print(6 * 7)"})],
                },
                {"content": "结果是 42", "tool_calls": []},
            ]
        )
        handled = await self.run_event(make_event("帮我算 6*7"))
        self.assertTrue(handled)
        self.assertEqual(len(self.tool_calls), 2)
        second_messages = self.tool_calls[1]["messages"]
        assistant = [m for m in second_messages if m.get("role") == "assistant"]
        self.assertTrue(assistant)
        self.assertEqual(assistant[-1]["tool_calls"][0]["id"], "call_1")
        tools = [m for m in second_messages if m.get("role") == "tool"]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["tool_call_id"], "call_1")
        payload = json.loads(tools[0]["content"])
        self.assertIn("42", payload["stdout"])
        self.assertEqual(payload["exit_code"], 0)
        self.assertIn("结果是 42", self.ctx.replies[-1])

    async def test_multiple_tool_calls_in_one_round(self):
        self.install_models(
            [
                {
                    "content": "",
                    "tool_calls": [
                        tool_call("call_a", {"code": "print(1)"}),
                        tool_call("call_b", {"code": "print(2)"}),
                    ],
                },
                {"content": "两个都跑了", "tool_calls": []},
            ]
        )
        await self.run_event(make_event("跑两段代码"))
        tools = self.tool_messages()
        self.assertEqual(len(tools), 2)
        self.assertEqual({m["tool_call_id"] for m in tools}, {"call_a", "call_b"})
        outputs = [json.loads(m["content"])["stdout"].strip() for m in tools]
        self.assertEqual(outputs, ["1", "2"])
        self.assertIn("两个都跑了", self.ctx.replies[-1])

    async def test_three_rounds_then_final_answer(self):
        script = [
            {"content": "", "tool_calls": [tool_call(f"c{i}", {"code": "print(1)"})]}
            for i in range(3)
        ]
        script.append({"content": "收尾回答", "tool_calls": []})
        self.install_models(script)
        handled = await self.run_event(make_event("跑一下"))
        self.assertTrue(handled)
        self.assertEqual(len(self.tool_calls), 4)
        self.assertIsNone(self.tool_calls[-1]["tools"])
        self.assertIn("收尾回答", self.ctx.replies[-1])

    async def test_tool_error_returned_to_model(self):
        self.install_models(
            [
                {
                    "content": "",
                    "tool_calls": [tool_call("bad", "{not json")],
                },
                {"content": "工具出错了", "tool_calls": []},
            ]
        )
        await self.run_event(make_event("随便跑点东西"))
        tools = self.tool_messages()
        self.assertEqual(len(tools), 1)
        payload = json.loads(tools[0]["content"])
        self.assertIn("error", payload)
        self.assertIn("JSON", payload["error"])
        self.assertIn("工具出错了", self.ctx.replies[-1])

    async def test_tool_file_argument(self):
        self.write_file("saved.py", "print('file tool ok')")
        self.install_models(
            [
                {
                    "content": "",
                    "tool_calls": [tool_call("call_file", {"file": "saved.py"})],
                },
                {"content": "文件跑完了", "tool_calls": []},
            ]
        )
        await self.run_event(make_event("运行那个文件"))
        tools = self.tool_messages()
        payload = json.loads(tools[0]["content"])
        self.assertIn("file tool ok", payload["stdout"])

    async def test_tool_missing_arguments(self):
        self.install_models(
            [
                {"content": "", "tool_calls": [tool_call("empty", {})]},
                {"content": "参数不对", "tool_calls": []},
            ]
        )
        await self.run_event(make_event("跑"))
        payload = json.loads(self.tool_messages()[0]["content"])
        self.assertIn("error", payload)


class ToolAvailabilityTests(ToolCallingTestCase):
    async def test_group_owner_gets_tools(self):
        self.ctx.api.member = {"role": "owner"}
        self.install_models([{"content": "owner ok", "tool_calls": []}])
        await self.run_event(make_event("你好", private=False, mention=True))
        self.assertEqual(len(self.tool_calls), 1)
        self.assertEqual(self.plain_calls, [])

    async def test_bot_admin_gets_tools_without_lookup(self):
        self.install_models([{"content": "admin ok", "tool_calls": []}])
        await self.run_event(
            make_event("你好", private=False, mention=True, user_id=10001)
        )
        self.assertEqual(len(self.tool_calls), 1)
        self.assertNotIn(
            "get_group_member_info", [call[0] for call in self.ctx.api.calls]
        )

    async def test_group_non_admin_no_tools(self):
        self.ctx.api.member = {"role": "member"}
        self.install_models()
        await self.run_event(make_event("你好", private=False, mention=True))
        self.assertEqual(self.tool_calls, [])
        self.assertEqual(len(self.plain_calls), 1)
        system = self.plain_calls[0]["messages"][0]["content"]
        self.assertIn(ai_chat.LOCAL_PYTHON_NO_TOOL_HINT, system)

    async def test_group_lookup_failure_no_tools(self):
        self.ctx.api.fail_member = True
        self.install_models()
        await self.run_event(make_event("你好", private=False, mention=True))
        self.assertEqual(self.tool_calls, [])
        self.assertEqual(len(self.plain_calls), 1)

    async def test_plugin_disabled_no_tools_no_hint(self):
        self.ctx._cfg["python_run"] = {"enabled": False}
        self.install_models()
        await self.run_event(make_event("你好"))
        self.assertEqual(self.tool_calls, [])
        self.assertEqual(len(self.plain_calls), 1)
        system = self.plain_calls[0]["messages"][0]["content"]
        self.assertNotIn("【本地 Python】", system)
        self.assertIn("你是助手", system)

    async def test_ai_tool_false_no_tools(self):
        self.ctx._cfg["python_run"] = {"ai_tool": False}
        self.install_models()
        await self.run_event(make_event("你好"))
        self.assertEqual(self.tool_calls, [])
        system = self.plain_calls[0]["messages"][0]["content"]
        self.assertIn(ai_chat.LOCAL_PYTHON_NO_TOOL_HINT, system)

    async def test_non_openai_provider_no_tools(self):
        self.ctx._cfg["ai_chat"]["provider"] = "claude"
        self.install_models()
        await self.run_event(make_event("你好"))
        self.assertEqual(self.tool_calls, [])
        self.assertEqual(len(self.plain_calls), 1)

    async def test_account_mode_no_tools(self):
        self.ctx._cfg["ai_chat"].update({"auth": "claude", "api_key": ""})
        self.install_models()
        await self.run_event(make_event("你好"))
        self.assertEqual(self.tool_calls, [])
        self.assertEqual(len(self.plain_calls), 1)
        system = self.plain_calls[0]["messages"][0]["content"]
        self.assertIn(ai_chat.LOCAL_PYTHON_NO_TOOL_HINT, system)

    async def test_private_user_gets_tools(self):
        self.install_models([{"content": "private ok", "tool_calls": []}])
        await self.run_event(make_event("你好", private=True, user_id=20002))
        self.assertEqual(len(self.tool_calls), 1)

    async def test_tools_unsupported_falls_back_to_plain_chat(self):
        self.install_models()

        def raising_tool(**kwargs):
            raise shared_providers.ProviderError("tools not supported")

        ai_chat.providers.chat_with_tools = raising_tool
        handled = await self.run_event(make_event("你好", private=True))
        self.assertTrue(handled)
        self.assertEqual(len(self.plain_calls), 1)
        self.assertIn("普通回答", self.ctx.replies[-1])


class ProviderChatWithToolsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _fake_urlopen(self, captured, response: dict):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(response).encode("utf-8")

        def fake(request, timeout=None):
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            captured["headers"] = {
                key.lower(): str(value) for key, value in request.header_items()
            }
            return FakeResponse()

        original = shared_providers.urllib.request.urlopen
        shared_providers.urllib.request.urlopen = fake
        self.addCleanup(
            setattr, shared_providers.urllib.request, "urlopen", original
        )

    def test_payload_contains_tools_and_parses_tool_calls(self):
        captured: dict = {}
        self._fake_urlopen(
            captured,
            {
                "choices": [
                    {
                        "message": {
                            "content": "好的",
                            "tool_calls": [
                                {
                                    "id": "call_9",
                                    "type": "function",
                                    "function": {
                                        "name": "run_python",
                                        "arguments": json.dumps({"code": "print(1)"}),
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
        )
        result = shared_providers.chat_with_tools(
            provider="openai",
            base_url="",
            api_key="key",
            model="m",
            messages=[{"role": "user", "content": "hi"}],
            tools=[ai_chat.PYTHON_TOOL_SPEC],
        )
        payload = captured["payload"]
        self.assertEqual(payload["tools"][0]["function"]["name"], "run_python")
        self.assertEqual(payload["tool_choice"], "auto")
        self.assertEqual(payload["messages"][0]["role"], "user")
        self.assertEqual(result["content"], "好的")
        self.assertEqual(result["tool_calls"][0]["id"], "call_9")
        self.assertEqual(result["tool_calls"][0]["name"], "run_python")
        self.assertIn("print(1)", result["tool_calls"][0]["arguments"])

    def test_tool_history_payload(self):
        captured: dict = {}
        self._fake_urlopen(captured, {"choices": [{"message": {"content": "完成"}}]})
        shared_providers.chat_with_tools(
            provider="openai",
            base_url="",
            api_key="key",
            model="m",
            messages=[
                {"role": "user", "content": "hi"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "run_python", "arguments": "{}"},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "结果"},
            ],
            tools=None,
        )
        messages = captured["payload"]["messages"]
        self.assertEqual(messages[1]["tool_calls"][0]["id"], "call_1")
        self.assertEqual(messages[2]["role"], "tool")
        self.assertEqual(messages[2]["tool_call_id"], "call_1")
        self.assertNotIn("tools", captured["payload"])

    def test_non_openai_provider_rejected(self):
        with self.assertRaises(shared_providers.ProviderError):
            shared_providers.chat_with_tools(
                provider="claude",
                base_url="",
                api_key="key",
                model="m",
                messages=[{"role": "user", "content": "hi"}],
                tools=[],
            )


class HintHelperTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ctx = FakeCtx(self._tmp.name)

    def test_hint_tool_available(self):
        hint = ai_chat.local_python_hint(self.ctx, True)
        self.assertIn("run_python", hint)
        self.assertIn("不要说自己没有工具", hint)

    def test_hint_no_tool(self):
        hint = ai_chat.local_python_hint(self.ctx, False)
        self.assertIn("/py", hint)
        self.assertIn("不要说自己完全没有运行能力", hint)

    def test_hint_empty_when_disabled(self):
        self.ctx._cfg["python_run"] = {"enabled": False}
        self.assertEqual(ai_chat.local_python_hint(self.ctx, True), "")

    def test_tool_spec_shape(self):
        spec = ai_chat.PYTHON_TOOL_SPEC
        self.assertEqual(spec["type"], "function")
        self.assertEqual(spec["function"]["name"], "run_python")
        properties = spec["function"]["parameters"]["properties"]
        self.assertEqual(set(properties), {"code", "file"})


if __name__ == "__main__":
    unittest.main()
