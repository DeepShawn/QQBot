"""/model 套餐模型切换 + /task 长任务代理（bot/agent.py）离线测试。"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent.parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import accounts_chatgpt  # noqa: E402
import agent  # noqa: E402
import providers  # noqa: E402
import shelltool  # noqa: E402
from plugins import commands  # noqa: E402

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


class FakeConfig:
    def __init__(self, path):
        self.path = Path(path)
        self.reloads = 0

    def reload_if_changed(self):
        self.reloads += 1
        return True

    def load(self):
        return {}


class FakeCtx:
    def __init__(self, root, config_path=None, ai_cfg=None, admins=()):
        self.data_dir = Path(root)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log = FakeLog()
        self.self_id = SELF_ID
        self.bot_name = "测试机器人"
        self.api = FakeApi()
        self._cfg = {
            "bot": {"admins": list(admins)},
            "plugins": {
                "ai_chat": dict(
                    ai_cfg or {"provider": "openai", "api_key": "key", "model": "m"}
                )
            },
        }
        self.config = FakeConfig(config_path or (self.data_dir / "config.json"))
        self.started_at = time.time()
        self.replies: list[str] = []

    @property
    def cfg(self) -> dict:
        return self._cfg

    def plugin_cfg(self, name: str) -> dict:
        return (self._cfg.get("plugins") or {}).get(name) or {}

    def is_admin(self, user_id) -> bool:
        return str(user_id) in {str(a) for a in (self._cfg["bot"]["admins"] or [])}

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


def _event(text: str, *, private: bool = True, mention: bool = False, user_id: int = 10001):
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


# ---------------------------------------------------------------- /model


class ModelCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.config_path = self.root / "config.json"
        self.ctx = FakeCtx(self.root, self.config_path, admins=[10001])
        self.plugin = commands.Plugin(self.ctx)

    def write_config(self, ai_chat: dict):
        self.config_path.write_text(
            json.dumps(
                {"plugins": {"ai_chat": ai_chat}},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def read_config(self) -> dict:
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    async def run_cmd(self, text: str, event=None) -> bool:
        return await self.plugin.on_event(event or _event(text))

    def last_reply(self) -> str:
        return self.ctx.replies[-1]

    async def test_account_model_list(self):
        self.write_config(
            {
                "active_profile": "p2",
                "profiles": [
                    {
                        "id": "p1",
                        "name": "DeepSeek",
                        "provider": "openai",
                        "api_key": "k",
                        "model": "deepseek-chat",
                    },
                    {
                        "id": "p2",
                        "name": "ChatGPT",
                        "auth": "chatgpt",
                        "api_key": "",
                        "model": accounts_chatgpt.MODELS[0],
                    },
                ],
            }
        )
        await self.run_cmd("/model")
        reply = self.last_reply()
        self.assertIn("当前模型", reply)
        self.assertIn("可用模型", reply)
        self.assertIn(accounts_chatgpt.MODELS[0], reply)
        self.assertIn("✅", reply)
        self.assertIn("切换模型", reply)
        self.assertIn("接口列表", reply)

    async def test_account_model_switch_by_index(self):
        self.write_config(
            {
                "active_profile": "p2",
                "profiles": [
                    {
                        "id": "p2",
                        "name": "ChatGPT",
                        "auth": "chatgpt",
                        "model": accounts_chatgpt.MODELS[0],
                    }
                ],
            }
        )
        await self.run_cmd("/model 2")
        data = self.read_config()
        self.assertEqual(
            data["plugins"]["ai_chat"]["profiles"][0]["model"],
            accounts_chatgpt.MODELS[1],
        )
        self.assertIn("已切换模型", self.last_reply())
        self.assertGreaterEqual(self.ctx.config.reloads, 1)

    async def test_account_model_switch_by_name(self):
        self.write_config(
            {
                "active_profile": "p2",
                "profiles": [
                    {
                        "id": "p2",
                        "name": "ChatGPT",
                        "auth": "chatgpt",
                        "model": accounts_chatgpt.MODELS[0],
                    }
                ],
            }
        )
        await self.run_cmd(f"/model {accounts_chatgpt.MODELS[2]}")
        data = self.read_config()
        self.assertEqual(
            data["plugins"]["ai_chat"]["profiles"][0]["model"],
            accounts_chatgpt.MODELS[2],
        )

    async def test_account_model_no_profiles_writes_top_level(self):
        self.write_config({"auth": "chatgpt", "model": ""})
        await self.run_cmd("/model 1")
        data = self.read_config()
        self.assertEqual(
            data["plugins"]["ai_chat"]["model"], accounts_chatgpt.MODELS[0]
        )

    async def test_account_module_missing_message(self):
        self.write_config({"auth": "unknown", "model": ""})
        sys.modules["accounts_unknown"] = None
        self.addCleanup(sys.modules.pop, "accounts_unknown", None)
        await self.run_cmd("/model")
        self.assertIn("账号模块", self.last_reply())

    async def test_profile_switch_regression(self):
        self.write_config(
            {
                "active_profile": "p1",
                "profiles": [
                    {"id": "p1", "name": "DeepSeek", "model": "deepseek-chat", "api_key": "k"},
                    {"id": "p2", "name": "Claude", "model": "claude-x", "api_key": "k2"},
                ],
            }
        )
        await self.run_cmd("/model 2")
        data = self.read_config()
        self.assertEqual(data["plugins"]["ai_chat"]["active_profile"], "p2")
        self.assertIn("已切换到", self.last_reply())

    async def test_profile_switch_by_name(self):
        self.write_config(
            {
                "active_profile": "p1",
                "profiles": [
                    {"id": "p1", "name": "DeepSeek", "model": "deepseek-chat", "api_key": "k"},
                    {"id": "p2", "name": "Claude", "model": "claude-x", "api_key": "k2"},
                ],
            }
        )
        await self.run_cmd("/model Claude")
        data = self.read_config()
        self.assertEqual(data["plugins"]["ai_chat"]["active_profile"], "p2")

    async def test_no_profiles_no_auth(self):
        self.write_config({})
        await self.run_cmd("/model")
        self.assertIn("还没有配置多套接口", self.last_reply())

    async def test_help_contains_new_commands(self):
        await self.run_cmd("/help")
        reply = self.last_reply()
        self.assertIn("/model", reply)
        self.assertIn("/task", reply)

    async def test_non_admin_denied(self):
        self.write_config({})
        await self.run_cmd("/model", _event("/model", user_id=20002))
        self.assertIn("只有管理员", self.last_reply())


# ---------------------------------------------------------------- /task 命令


class TaskCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.ctx = FakeCtx(self.root, admins=[10001])
        self.calls: list = []

    def make_plugin(self):
        return commands.Plugin(self.ctx)

    async def test_private_start(self):
        plugin = self.make_plugin()

        async def fake_run_task(ctx, event, prompt):
            self.calls.append(prompt)
            return {"ok": True, "summary": "完成"}

        _patch(self, agent, "run_task", fake_run_task)
        handled = await plugin.on_event(_event("/task 写个脚本"))
        self.assertTrue(handled)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual(self.calls, ["写个脚本"])
        self.assertIn("任务已开始", self.ctx.replies[0])

    async def test_group_member_denied(self):
        plugin = self.make_plugin()
        self.ctx.api.member = {"role": "member"}

        async def fake_run_task(ctx, event, prompt):
            self.calls.append(prompt)
            return {"ok": True}

        _patch(self, agent, "run_task", fake_run_task)
        await plugin.on_event(_event("/task 写个脚本", private=False, mention=True, user_id=20002))
        self.assertEqual(self.calls, [])
        self.assertIn("只有管理员", self.ctx.replies[-1])

    async def test_group_owner_allowed(self):
        plugin = self.make_plugin()
        self.ctx.api.member = {"role": "owner"}

        async def fake_run_task(ctx, event, prompt):
            self.calls.append(prompt)
            return {"ok": True}

        _patch(self, agent, "run_task", fake_run_task)
        await plugin.on_event(_event("/task 写个脚本", private=False, mention=True, user_id=20002))
        await asyncio.sleep(0)
        self.assertEqual(self.calls, ["写个脚本"])

    async def test_stop_without_task(self):
        plugin = self.make_plugin()
        await plugin.on_event(_event("/task stop"))
        self.assertIn("没有正在运行", self.ctx.replies[-1])

    async def test_stop_running_task(self):
        plugin = self.make_plugin()
        started = asyncio.Event()

        async def fake_run_task(ctx, event, prompt):
            started.set()
            await asyncio.Event().wait()

        _patch(self, agent, "run_task", fake_run_task)
        await plugin.on_event(_event("/task 长任务"))
        await started.wait()
        await plugin.on_event(_event("/task stop"))
        self.assertIn("已停止", self.ctx.replies[-1])
        await asyncio.sleep(0)

    async def test_duplicate_start_while_running(self):
        plugin = self.make_plugin()
        started = asyncio.Event()

        async def fake_run_task(ctx, event, prompt):
            started.set()
            await asyncio.Event().wait()

        _patch(self, agent, "run_task", fake_run_task)
        await plugin.on_event(_event("/task 第一个"))
        await started.wait()
        await plugin.on_event(_event("/task 第二个"))
        self.assertIn("已有任务在跑", self.ctx.replies[-1])
        await plugin.on_event(_event("/task stop"))

    async def test_empty_task_usage(self):
        plugin = self.make_plugin()
        await plugin.on_event(_event("/task"))
        self.assertIn("用法", self.ctx.replies[-1])


# ---------------------------------------------------------------- agent 循环


class AgentLoopTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.ctx = FakeCtx(
            self.root,
            ai_cfg={"provider": "openai", "api_key": "key", "model": "m", "auth": ""},
            admins=[10001],
        )
        self.native_calls: list = []
        self.native_script: list = []
        self.chat_calls: list = []
        self.chat_script: list = []
        self.shell_calls: list = []
        self.shell_result = {
            "stdout": "hi\n",
            "stderr": "",
            "exit_code": 0,
            "timed_out": False,
            "duration": 0.01,
        }
        self._orig_native = agent.providers.chat_with_tools
        self._orig_chat = agent.providers.chat
        self._orig_shell = agent.shelltool.run_shell
        self.addCleanup(setattr, agent.providers, "chat_with_tools", self._orig_native)
        self.addCleanup(setattr, agent.providers, "chat", self._orig_chat)
        self.addCleanup(setattr, agent.shelltool, "run_shell", self._orig_shell)

    def install_native(self, script):
        self.native_script = list(script)

        def fake(**kwargs):
            self.native_calls.append(kwargs)
            if not self.native_script:
                return {"content": "默认结束", "tool_calls": []}
            return self.native_script.pop(0)

        agent.providers.chat_with_tools = fake

    def install_chat(self, script):
        self.chat_script = list(script)

        def fake(**kwargs):
            self.chat_calls.append(kwargs)
            if not self.chat_script:
                return '{"action":"final","summary":"默认结束"}'
            return self.chat_script.pop(0)

        agent.providers.chat = fake

    def install_shell(self, result=None):
        def fake(command, cwd=None, timeout=60, env_extra=None):
            self.shell_calls.append(
                {"command": command, "cwd": cwd, "timeout": timeout}
            )
            return dict(result or self.shell_result)

        agent.shelltool.run_shell = fake

    def tool_call(self, call_id: str, name: str, arguments: dict) -> dict:
        return {
            "id": call_id,
            "name": name,
            "arguments": json.dumps(arguments, ensure_ascii=False),
        }

    async def run_agent(self, prompt="帮我做点事"):
        return await agent.run_task(self.ctx, _event("/task " + prompt), prompt)

    async def test_native_loop_shell_install_final(self):
        self.install_native(
            [
                {
                    "content": "",
                    "tool_calls": [
                        self.tool_call("c1", "run_shell", {"command": "echo hi"})
                    ],
                },
                {
                    "content": "",
                    "tool_calls": [
                        self.tool_call(
                            "c2",
                            "install_package",
                            {"manager": "apt", "name": "ffmpeg"},
                        )
                    ],
                },
                {"content": "全部完成", "tool_calls": []},
            ]
        )
        self.install_shell()
        result = await self.run_agent()
        self.assertTrue(result["ok"])
        self.assertEqual(result["summary"], "全部完成")
        self.assertEqual(len(self.shell_calls), 2)
        self.assertEqual(self.shell_calls[0]["command"], "echo hi")
        self.assertIn("apt-get install -y ffmpeg", self.shell_calls[1]["command"])
        replies = "\n".join(self.ctx.replies)
        self.assertIn("🛠 第1步", replies)
        self.assertIn("✅ 第1步完成", replies)
        self.assertIn("📦 正在安装 ffmpeg", replies)
        self.assertIn("🎉 任务完成：全部完成", replies)
        # 第一步结果回给了模型
        messages = self.native_calls[1]["messages"]
        tool_messages = [m for m in messages if m.get("role") == "tool"]
        self.assertTrue(any("hi" in m["content"] for m in tool_messages))

    async def test_json_loop_account_mode(self):
        self.ctx = FakeCtx(
            self.root,
            ai_cfg={"provider": "openai", "api_key": "", "model": "", "auth": "chatgpt"},
            admins=[10001],
        )
        self.install_chat(
            [
                '{"action":"run_shell","command":"echo hi"}',
                '{"action":"install_package","manager":"pip","name":"Pillow"}',
                '{"action":"final","summary":"全部做完了"}',
            ]
        )
        self.install_shell()
        result = await self.run_agent()
        self.assertTrue(result["ok"])
        self.assertEqual(result["summary"], "全部做完了")
        self.assertEqual(len(self.shell_calls), 2)
        self.assertIn("pip install --no-input Pillow", self.shell_calls[1]["command"])
        self.assertEqual(len(self.chat_calls), 3)
        # 工具结果回传
        last_messages = self.chat_calls[2]["messages"]
        self.assertTrue(any("工具执行结果" in str(m.get("content")) for m in last_messages))
        self.assertIn("🎉 任务完成：全部做完了", "\n".join(self.ctx.replies))
        self.assertEqual(self.native_calls, [])

    async def test_tool_error_fed_back(self):
        self.install_native(
            [
                {
                    "content": "",
                    "tool_calls": [self.tool_call("c1", "run_shell", {})],
                },
                {"content": "做完了", "tool_calls": []},
            ]
        )
        self.install_shell()
        result = await self.run_agent()
        self.assertTrue(result["ok"])
        replies = "\n".join(self.ctx.replies)
        self.assertIn("失败", replies)
        tool_messages = [
            m for m in self.native_calls[1]["messages"] if m.get("role") == "tool"
        ]
        self.assertIn("缺少 command", tool_messages[0]["content"])

    async def test_max_steps_limit(self):
        script = [
            {
                "content": "",
                "tool_calls": [
                    self.tool_call(f"c{i}", "run_shell", {"command": "echo x"})
                ],
            }
            for i in range(agent.MAX_STEPS + 3)
        ]
        self.install_native(script)
        self.install_shell()
        result = await self.run_agent()
        self.assertFalse(result["ok"])
        self.assertIn("步上限", result["reason"])
        self.assertEqual(len(self.native_calls), agent.MAX_STEPS)
        self.assertIn("⚠️ 任务未完成", "\n".join(self.ctx.replies))

    async def test_total_timeout(self):
        _patch(self, agent, "MAX_TOTAL_SECONDS", 0)
        self.install_native([])
        self.install_shell()
        result = await self.run_agent()
        self.assertFalse(result["ok"])
        self.assertIn("总时长", result["reason"])

    async def test_progress_messages_capped(self):
        script = [
            {
                "content": "",
                "tool_calls": [
                    self.tool_call(f"c{i}", "run_shell", {"command": "echo x"})
                ],
            }
            for i in range(agent.MAX_STEPS - 1)
        ]
        script.append({"content": "完成", "tool_calls": []})
        self.install_native(script)
        self.install_shell()
        await self.run_agent()
        step_messages = [
            r
            for r in self.ctx.replies
            if r.startswith("🛠") or r.startswith("✅") or r.startswith("📦")
        ]
        self.assertLessEqual(len(step_messages), agent.MAX_PROGRESS_MESSAGES)
        self.assertTrue(any(r.startswith("🎉") for r in self.ctx.replies))

    async def test_cancellation_reports_stopped(self):
        event = asyncio.Event()

        async def fake_native(*args, **kwargs):
            await event.wait()
            return {"ok": False}

        _patch(self, agent, "_run_native", fake_native)
        task = asyncio.create_task(
            agent.run_task(self.ctx, _event("/task x"), "x")
        )
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIn("任务已停止", "\n".join(self.ctx.replies))

    async def test_missing_env_hint(self):
        self.install_native(
            [
                {
                    "content": "",
                    "tool_calls": [
                        self.tool_call("c1", "run_shell", {"command": "ffmpeg -version"})
                    ],
                },
                {"content": "完成", "tool_calls": []},
            ]
        )
        self.install_shell(
            {"stdout": "", "stderr": "bash: ffmpeg: command not found", "exit_code": 127}
        )
        await self.run_agent()
        tool_messages = [
            m for m in self.native_calls[1]["messages"] if m.get("role") == "tool"
        ]
        self.assertIn("install_package", tool_messages[0]["content"])

    async def test_native_tools_include_install(self):
        self.install_native([{"content": "完成", "tool_calls": []}])
        await self.run_agent()
        names = {t["function"]["name"] for t in self.native_calls[0]["tools"]}
        self.assertEqual(names, {"run_shell", "run_python", "install_package"})


class InstallAndPermissionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.ctx = FakeCtx(self.root, admins=[10001])
        self.shell_calls: list = []
        self._orig_shell = agent.shelltool.run_shell
        self.addCleanup(setattr, agent.shelltool, "run_shell", self._orig_shell)

    def install_shell(self, result=None):
        def fake(command, cwd=None, timeout=60, env_extra=None):
            self.shell_calls.append({"command": command, "timeout": timeout})
            return dict(result or {"stdout": "ok", "stderr": "", "exit_code": 0})

        agent.shelltool.run_shell = fake

    def test_install_package_commands(self):
        self.install_shell()
        agent._install_package("apt", "ffmpeg")
        agent._install_package("pip", "Pillow")
        self.assertIn("apt-get install -y ffmpeg", self.shell_calls[0]["command"])
        self.assertEqual(self.shell_calls[0]["timeout"], agent.INSTALL_TIMEOUT)
        self.assertIn("pip install --no-input Pillow", self.shell_calls[1]["command"])

    def test_install_package_rejects_bad_input(self):
        self.install_shell()
        self.assertIn("error", agent._install_package("apt", "a; rm -rf /"))
        self.assertIn("error", agent._install_package("dpkg", "ffmpeg"))
        self.assertIn("error", agent._install_package("apt", ""))
        self.assertEqual(self.shell_calls, [])

    async def test_can_use(self):
        event = _event("/task x", private=False, mention=True, user_id=20002)
        self.ctx.api.member = {"role": "member"}
        self.assertFalse(await agent.can_use(self.ctx, event))
        self.ctx.api.member = {"role": "owner"}
        self.assertTrue(await agent.can_use(self.ctx, event))
        self.assertTrue(await agent.can_use(self.ctx, _event("/task x")))
        self.ctx.api.member = {"role": "member"}
        self.assertTrue(await agent.can_use(self.ctx, _event("/task x", user_id=10001)))

    def test_task_key(self):
        self.assertEqual(agent.task_key(_event("/task x")), "private:10001")
        self.assertEqual(
            agent.task_key(_event("/task x", private=False, mention=True)),
            "group:12345",
        )


class AgentConfiguredTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ctx = FakeCtx(self._tmp.name, ai_cfg={"provider": "openai", "api_key": "", "auth": ""})

    async def test_not_configured_message(self):
        result = await agent.run_task(self.ctx, _event("/task x"), "x")
        self.assertFalse(result["ok"])
        self.assertIn("还没配置", self.ctx.replies[-1])


if __name__ == "__main__":
    unittest.main()
