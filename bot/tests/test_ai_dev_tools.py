"""ai_chat 集成 run_shell / read_skill 工具（原生 tools + 账号意图路由）。全离线 stub。"""
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

import shelltool  # noqa: E402
from plugins import ai_chat  # noqa: E402

SELF_ID = 999


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


def _make_ctx(
    root,
    ai_cfg=None,
    dev_cfg=None,
    skills_cfg=None,
    python_cfg=None,
    image_cfg=None,
    admins=(),
) -> FakeCtx:
    return FakeCtx(
        root,
        {
            "ai_chat": ai_cfg if ai_cfg is not None else _ai_cfg(),
            "dev_tools": dev_cfg if dev_cfg is not None else {"enabled": True},
            "skills": skills_cfg if skills_cfg is not None else {"enabled": True},
            "python_run": python_cfg if python_cfg is not None else {"enabled": False},
            "image_gen": image_cfg if image_cfg is not None else {"enabled": False},
        },
        admins=admins,
    )


def _event(text: str, *, private: bool = True, mention: bool = False, user_id: int = 20002) -> dict:
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
        event["group_id"] = 12345
    return event


def _tool_call(call_id: str, name: str, arguments: dict | str) -> dict:
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments, ensure_ascii=False)
    return {"id": call_id, "name": name, "arguments": arguments}


class DevToolsAITestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.skills_dir = self.root / "skills"
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        (self.skills_dir / "deploy.md").write_text(
            "# 部署流程\n\n1. 构建 2. 上传", encoding="utf-8"
        )
        self.ctx = _make_ctx(self.root)
        self.chat_calls: list = []
        self.chat_script: list = []
        self.native_calls: list = []
        self.native_script: list = []
        self.shell_calls: list = []
        self.shell_result: dict = {
            "stdout": "shell ok\n",
            "stderr": "",
            "exit_code": 0,
            "timed_out": False,
            "duration": 0.01,
        }
        self._orig_chat = ai_chat.providers.chat
        self._orig_native = ai_chat.providers.chat_with_tools
        self._orig_shell = shelltool.run_shell
        self.addCleanup(setattr, ai_chat.providers, "chat", self._orig_chat)
        self.addCleanup(
            setattr, ai_chat.providers, "chat_with_tools", self._orig_native
        )
        self.addCleanup(setattr, shelltool, "run_shell", self._orig_shell)

    # ---------------- stub ----------------
    def install_chat(self, script=None):
        self.chat_script = list(script or [])

        def fake_chat(**kwargs):
            self.chat_calls.append(kwargs)
            if not self.chat_script:
                return "普通回答"
            return self.chat_script.pop(0)

        ai_chat.providers.chat = fake_chat

    def install_native(self, script=None):
        self.native_script = list(script or [])

        def fake_native(**kwargs):
            self.native_calls.append(kwargs)
            if not self.native_script:
                return {"content": "默认回答", "tool_calls": []}
            return self.native_script.pop(0)

        ai_chat.providers.chat_with_tools = fake_native

    def install_shell(self):
        def fake_shell(command, cwd=None, timeout=60, env_extra=None):
            self.shell_calls.append(
                {
                    "command": command,
                    "cwd": cwd,
                    "timeout": timeout,
                    "env_extra": env_extra,
                }
            )
            return dict(self.shell_result)

        shelltool.run_shell = fake_shell

    async def run_event(self, event) -> bool:
        return await ai_chat.Plugin(self.ctx).on_event(event)

    def last_reply(self) -> str:
        self.assertTrue(self.ctx.replies, "没有回复")
        return self.ctx.replies[-1]

    def native_tool_names(self, index: int = 0) -> set:
        return {t["function"]["name"] for t in self.native_calls[index]["tools"]}

    def native_tool_messages(self, call_index: int = -1) -> list:
        messages = self.native_calls[call_index]["messages"]
        return [m for m in messages if m.get("role") == "tool"]

    def chat_system_text(self, call_index: int = -1) -> str:
        return "\n".join(
            str(m.get("content") or "")
            for m in self.chat_calls[call_index]["messages"]
            if m.get("role") == "system"
        )


class NativeDevToolTests(DevToolsAITestCase):
    async def test_tools_schema_and_skills_in_prompt(self):
        self.install_native([{"content": "好的", "tool_calls": []}])
        await self.run_event(_event("你好"))
        names = self.native_tool_names()
        self.assertIn("run_shell", names)
        self.assertIn("read_skill", names)
        spec = next(
            t for t in self.native_calls[0]["tools"]
            if t["function"]["name"] == "run_shell"
        )
        self.assertIn("shell", spec["function"]["description"])
        self.assertIn("command", spec["function"]["parameters"]["properties"])
        system = self.native_calls[0]["messages"][0]["content"]
        self.assertIn("【技能】", system)
        self.assertIn("deploy", system)
        self.assertIn("部署流程", system)
        self.assertIn("【开发者工具】", system)

    async def test_native_run_shell_executes(self):
        self.install_shell()
        self.install_native(
            [
                {
                    "content": "",
                    "tool_calls": [
                        _tool_call("sh1", "run_shell", {"command": "echo hi", "timeout": 30})
                    ],
                },
                {"content": "跑好了", "tool_calls": []},
            ]
        )
        handled = await self.run_event(_event("帮我 echo 一下"))
        self.assertTrue(handled)
        self.assertEqual(len(self.shell_calls), 1)
        self.assertEqual(self.shell_calls[0]["command"], "echo hi")
        self.assertEqual(self.shell_calls[0]["timeout"], 30.0)
        self.assertIsNone(self.shell_calls[0]["cwd"])
        self.assertIsNone(self.shell_calls[0]["env_extra"])
        tool_messages = self.native_tool_messages()
        payload = json.loads(tool_messages[0]["content"])
        self.assertIn("shell ok", payload["stdout"])
        self.assertIn("跑好了", self.last_reply())

    async def test_native_read_skill(self):
        self.install_native(
            [
                {
                    "content": "",
                    "tool_calls": [_tool_call("sk1", "read_skill", {"name": "deploy"})],
                },
                {"content": "按技能做完了", "tool_calls": []},
            ]
        )
        await self.run_event(_event("按部署技能来"))
        payload = json.loads(self.native_tool_messages()[0]["content"])
        self.assertEqual(payload["name"], "deploy")
        self.assertIn("构建", payload["content"])
        self.assertIn("按技能做完了", self.last_reply())

    async def test_native_read_skill_missing(self):
        self.install_native(
            [
                {
                    "content": "",
                    "tool_calls": [_tool_call("sk2", "read_skill", {"name": "nope"})],
                },
                {"content": "没找到", "tool_calls": []},
            ]
        )
        await self.run_event(_event("读个技能"))
        payload = json.loads(self.native_tool_messages()[0]["content"])
        self.assertIn("error", payload)

    async def test_git_token_and_repo_path_passed_to_shell(self):
        self.ctx = _make_ctx(
            self.root,
            dev_cfg={
                "enabled": True,
                "git_token": "SECRET-TOKEN",
                "repo_path": "/tmp/repo-x",
                "timeout_seconds": 20,
            },
        )
        self.install_shell()
        self.install_native(
            [
                {
                    "content": "",
                    "tool_calls": [_tool_call("sh2", "run_shell", {"command": "git status"})],
                },
                {"content": "好了", "tool_calls": []},
            ]
        )
        await self.run_event(_event("看看 git 状态"))
        call = self.shell_calls[0]
        self.assertEqual(call["cwd"], "/tmp/repo-x")
        self.assertEqual(call["timeout"], 20.0)
        self.assertEqual(
            call["env_extra"],
            {"GITHUB_TOKEN": "SECRET-TOKEN", "GH_TOKEN": "SECRET-TOKEN"},
        )
        self.assertNotIn(
            "SECRET-TOKEN", "\n".join(self.ctx.log.records)
        )

    async def test_group_member_has_skills_but_not_shell(self):
        self.ctx.api.member = {"role": "member"}
        self.install_native([{"content": "好的", "tool_calls": []}])
        await self.run_event(_event("你好", private=False, mention=True))
        names = self.native_tool_names()
        self.assertIn("read_skill", names)
        self.assertNotIn("run_shell", names)

    async def test_group_owner_has_shell(self):
        self.ctx.api.member = {"role": "owner"}
        self.install_native([{"content": "好的", "tool_calls": []}])
        await self.run_event(_event("你好", private=False, mention=True))
        self.assertIn("run_shell", self.native_tool_names())

    async def test_dev_tools_disabled_no_shell(self):
        self.ctx = _make_ctx(self.root, dev_cfg={"enabled": False})
        self.install_native([{"content": "好的", "tool_calls": []}])
        await self.run_event(_event("你好"))
        names = self.native_tool_names()
        self.assertNotIn("run_shell", names)
        system = self.native_calls[0]["messages"][0]["content"]
        self.assertNotIn("【开发者工具】", system)

    async def test_dev_tools_ai_tool_false_no_shell(self):
        self.ctx = _make_ctx(self.root, dev_cfg={"enabled": True, "ai_tool": False})
        self.install_native([{"content": "好的", "tool_calls": []}])
        await self.run_event(_event("你好"))
        self.assertNotIn("run_shell", self.native_tool_names())

    async def test_skills_disabled_no_skill_tool(self):
        self.ctx = _make_ctx(self.root, skills_cfg={"enabled": False})
        self.install_native([{"content": "好的", "tool_calls": []}])
        await self.run_event(_event("你好"))
        names = self.native_tool_names()
        self.assertNotIn("read_skill", names)
        system = self.native_calls[0]["messages"][0]["content"]
        self.assertNotIn("【技能】", system)

    async def test_empty_skills_dir_has_tool_but_no_hint(self):
        for path in self.skills_dir.glob("*.md"):
            path.unlink()
        self.ctx = _make_ctx(self.root)
        self.install_native([{"content": "好的", "tool_calls": []}])
        await self.run_event(_event("你好"))
        names = self.native_tool_names()
        # 只要 skills 没被禁用就提供 read_skill；目录为空时 system prompt 不列技能
        self.assertIn("read_skill", names)
        system = self.native_calls[0]["messages"][0]["content"]
        self.assertNotIn("【技能】", system)


class RoutingDevToolTests(DevToolsAITestCase):
    def account_ctx(self, **overrides):
        self.ctx = _make_ctx(
            self.root,
            ai_cfg=_ai_cfg(auth="chatgpt", api_key="", **overrides),
        )

    async def test_route_none_plain_chat(self):
        self.account_ctx()
        self.install_chat(['{"tool":"none"}', "普通回答"])
        await self.run_event(_event("你好"))
        self.assertEqual(len(self.chat_calls), 2)
        self.assertIn("普通回答", self.last_reply())

    async def test_route_run_shell(self):
        self.account_ctx()
        self.install_shell()
        self.install_chat(
            ['```json\n{"tool":"run_shell","command":"df -h"}\n```', "看完了"]
        )
        await self.run_event(_event("看看磁盘空间"))
        self.assertEqual(len(self.shell_calls), 1)
        self.assertEqual(self.shell_calls[0]["command"], "df -h")
        self.assertEqual(len(self.chat_calls), 2)
        final_system = self.chat_system_text()
        self.assertIn("工具执行结果", final_system)
        self.assertIn("run_shell", final_system)
        self.assertIn("看完了", self.last_reply())

    async def test_route_read_skill(self):
        self.account_ctx()
        self.install_chat(
            ['{"tool":"read_skill","name":"deploy"}', "按技能做完了"]
        )
        await self.run_event(_event("按部署技能做"))
        final_system = self.chat_system_text()
        self.assertIn("read_skill", final_system)
        self.assertIn("构建", final_system)
        self.assertIn("按技能做完了", self.last_reply())

    async def test_route_prompt_lists_available_tools(self):
        self.account_ctx()
        self.install_chat(['{"tool":"none"}', "普通回答"])
        await self.run_event(_event("你好"))
        route_system = str(self.chat_calls[0]["messages"][0]["content"])
        self.assertIn("工具路由器", route_system)
        self.assertIn("run_shell", route_system)
        self.assertIn("read_skill", route_system)
        self.assertEqual(self.chat_calls[0]["temperature"], 0.0)

    async def test_route_group_member_hides_run_shell(self):
        self.account_ctx()
        self.ctx.api.member = {"role": "member"}
        self.install_chat(['{"tool":"none"}', "普通回答"])
        await self.run_event(_event("你好", private=False, mention=True))
        route_system = str(self.chat_calls[0]["messages"][0]["content"])
        self.assertNotIn("run_shell", route_system)
        self.assertIn("read_skill", route_system)

    async def test_route_run_shell_group_member_ignored(self):
        self.account_ctx()
        self.ctx.api.member = {"role": "member"}
        self.install_shell()
        self.install_chat(
            ['{"tool":"run_shell","command":"rm -rf /"}', "普通回答"]
        )
        await self.run_event(_event("你好", private=False, mention=True))
        self.assertEqual(self.shell_calls, [])
        self.assertIn("普通回答", self.last_reply())

    async def test_api_key_mode_uses_native_not_routing(self):
        self.install_chat()
        self.install_native([{"content": "好的", "tool_calls": []}])
        await self.run_event(_event("你好"))
        self.assertEqual(self.chat_calls, [])
        self.assertEqual(len(self.native_calls), 1)


if __name__ == "__main__":
    unittest.main()
