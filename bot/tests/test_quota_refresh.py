"""T50 5 小时限额刷新（内部发「你好」）离线测试。"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

BOT_DIR = Path(__file__).resolve().parent.parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import account_core  # noqa: E402
import providers  # noqa: E402
import quota_refresh  # noqa: E402
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


class FakeCtx:
    def __init__(self, root, cfg=None, admins=(10001,)):
        self.data_dir = Path(root)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log = FakeLog()
        self.self_id = SELF_ID
        self.bot_name = "测试机器人"
        self.api = SimpleNamespace(connected=None)
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


def _event(text: str, user_id: int = 10001) -> dict:
    return {
        "post_type": "message",
        "message_type": "private",
        "user_id": user_id,
        "message": [{"type": "text", "data": {"text": text}}],
    }


def _quota_cfg(**overrides) -> dict:
    cfg = {
        "enabled": True,
        "interval_minutes": 301,
        "providers": ["claude", "chatgpt"],
        "message": "你好",
    }
    cfg.update(overrides)
    return cfg


class QuotaBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.ctx = FakeCtx(self.root)
        self.ctx._cfg = {"plugins": {"quota_refresh": _quota_cfg()}}
        self.chat_calls: list = []
        self.chat_error: Exception | None = None
        self.ensure_error: dict[str, Exception] = {}
        self.records = {
            "claude": {"access": "a", "refresh": "r"},
            "chatgpt": {"access": "a", "refresh": "r"},
            "gemini": {"access": "a", "refresh": "r"},
            "deepseek": {"access": "a", "refresh": "r"},
        }
        with quota_refresh._log_lock:
            quota_refresh._last_log.clear()
        self.addCleanup(self._cleanup_logs)
        self._orig_get_record = account_core.get_record
        self._orig_ensure = account_core.ensure_access
        self._orig_chat = providers.chat
        self.addCleanup(setattr, account_core, "get_record", self._orig_get_record)
        self.addCleanup(setattr, account_core, "ensure_access", self._orig_ensure)
        self.addCleanup(setattr, providers, "chat", self._orig_chat)

    def _cleanup_logs(self):
        with quota_refresh._log_lock:
            quota_refresh._last_log.clear()

    def install_accounts(self):
        def fake_get_record(pid):
            value = self.records.get(pid)
            return dict(value) if isinstance(value, dict) else value

        def fake_ensure(pid):
            error = self.ensure_error.get(pid)
            if error is not None:
                raise error
            return self.records.get(pid)

        account_core.get_record = fake_get_record
        account_core.ensure_access = fake_ensure

    def install_chat(self):
        def fake_chat(**kwargs):
            self.chat_calls.append(kwargs)
            if self.chat_error is not None:
                raise self.chat_error
            return "你好呀"

        providers.chat = fake_chat

    def set_cfg(self, **overrides):
        self.ctx._cfg["plugins"]["quota_refresh"] = _quota_cfg(**overrides)

    def write_state(self, last_sent: float):
        quota_refresh.state_path(self.ctx).write_text(
            json.dumps({"last_sent": last_sent}), encoding="utf-8"
        )

    def read_state(self) -> dict:
        return json.loads(quota_refresh.state_path(self.ctx).read_text(encoding="utf-8"))


class TickTests(QuotaBase):
    async def test_disabled(self):
        self.set_cfg(enabled=False)
        self.install_accounts()
        self.install_chat()
        result = await quota_refresh.tick(self.ctx, now=time.time())
        self.assertEqual(result["skipped"], "disabled")
        self.assertEqual(self.chat_calls, [])
        self.assertEqual(self.ctx.sent, [])

    async def test_not_due(self):
        now = time.time()
        self.write_state(now - 100)
        self.install_accounts()
        self.install_chat()
        result = await quota_refresh.tick(self.ctx, now=now)
        self.assertEqual(result["skipped"], "not-due")
        self.assertEqual(self.chat_calls, [])

    async def test_due_only_signed_in_claude_chatgpt(self):
        now = time.time()
        self.install_accounts()
        self.install_chat()
        result = await quota_refresh.tick(self.ctx, now=now)
        self.assertEqual(result["skipped"], "")
        self.assertEqual([c["auth"] for c in self.chat_calls], ["claude", "chatgpt"])
        self.assertEqual(result["refreshed"], ["claude", "chatgpt"])
        for call in self.chat_calls:
            self.assertEqual(call["provider"], "openai")
            self.assertEqual(call["base_url"], "")
            self.assertEqual(call["api_key"], "")
            self.assertEqual(call["model"], "")
            self.assertEqual(call["max_tokens"], 16)
            self.assertEqual(call["timeout"], 45)
            self.assertEqual(call["messages"], [{"role": "user", "content": "你好"}])
        self.assertAlmostEqual(self.read_state()["last_sent"], now, places=1)
        self.assertEqual(self.ctx.sent, [])  # 不往 QQ 发消息
        self.assertEqual(self.ctx.replies, [])

    async def test_unsigned_and_unsupported_skipped(self):
        now = time.time()
        self.records.pop("chatgpt", None)
        self.set_cfg(providers=["claude", "gemini", "deepseek", "unknown"])
        # gemini/deepseek 已登录，但只有 providers 里列出的会被处理；
        # unknown 不受支持。这里验证只调用 claude。
        self.install_accounts()
        self.install_chat()
        result = await quota_refresh.tick(self.ctx, now=now)
        self.assertEqual([c["auth"] for c in self.chat_calls], ["claude"])
        self.assertEqual(result["refreshed"], ["claude"])

    async def test_default_providers_skip_gemini_deepseek_even_if_signed_in(self):
        now = time.time()
        self.install_accounts()
        self.install_chat()
        await quota_refresh.tick(self.ctx, now=now)
        self.assertEqual([c["auth"] for c in self.chat_calls], ["claude", "chatgpt"])

    async def test_success_updates_state_failure_does_not(self):
        now = time.time()
        self.install_accounts()
        self.install_chat()
        self.chat_error = providers.ProviderError("HTTP 429 限流")
        result = await quota_refresh.tick(self.ctx, now=now)
        self.assertFalse(result["updated"])
        self.assertEqual(len(result["failed"]), 2)
        self.assertFalse(quota_refresh.state_path(self.ctx).exists())
        # 下一轮：成功才写状态
        self.chat_error = None
        result2 = await quota_refresh.tick(self.ctx, now=now + 10)
        self.assertTrue(result2["updated"])
        self.assertAlmostEqual(self.read_state()["last_sent"], now + 10, places=1)

    async def test_partial_success_updates_state(self):
        now = time.time()
        self.ensure_error["chatgpt"] = account_core.AccountError("busy")
        self.install_accounts()
        self.install_chat()
        result = await quota_refresh.tick(self.ctx, now=now)
        self.assertEqual(result["refreshed"], ["claude"])
        self.assertEqual(result["failed"][0]["provider"], "chatgpt")
        self.assertTrue(result["updated"])
        self.assertAlmostEqual(self.read_state()["last_sent"], now, places=1)

    async def test_provider_429_does_not_crash(self):
        now = time.time()
        self.install_accounts()
        self.install_chat()
        self.chat_error = providers.ProviderError("429 rate_limit_error")
        result = await quota_refresh.tick(self.ctx, now=now)
        self.assertFalse(result["updated"])
        self.assertEqual(len(result["failed"]), 2)
        self.assertEqual(self.ctx.sent, [])

    async def test_force_ignores_interval(self):
        now = time.time()
        self.write_state(now - 10)
        self.install_accounts()
        self.install_chat()
        result = await quota_refresh.tick(self.ctx, now=now, force=True)
        self.assertEqual(result["refreshed"], ["claude", "chatgpt"])
        self.assertTrue(result["updated"])

    async def test_interval_clamped(self):
        self.assertEqual(
            quota_refresh._interval_seconds({"interval_minutes": 1}), 60 * 60
        )
        self.assertEqual(
            quota_refresh._interval_seconds({"interval_minutes": 99999}),
            1440 * 60,
        )
        self.assertEqual(
            quota_refresh._interval_seconds({"interval_minutes": "bad"}),
            301 * 60,
        )
        now = time.time()
        self.set_cfg(interval_minutes=1)
        self.write_state(now - 120)  # 2 分钟 < 60 分钟下限 -> 还没到
        self.install_accounts()
        self.install_chat()
        result = await quota_refresh.tick(self.ctx, now=now)
        self.assertEqual(result["skipped"], "not-due")
        self.assertEqual(self.chat_calls, [])

    async def test_all_unsigned_no_accounts(self):
        now = time.time()
        self.records.clear()
        self.install_accounts()
        self.install_chat()
        result = await quota_refresh.tick(self.ctx, now=now)
        self.assertEqual(result["skipped"], "no-accounts")
        self.assertEqual(self.chat_calls, [])

    async def test_state_file_atomic_and_valid(self):
        now = time.time()
        self.install_accounts()
        self.install_chat()
        await quota_refresh.tick(self.ctx, now=now)
        data = json.loads(quota_refresh.state_path(self.ctx).read_text(encoding="utf-8"))
        self.assertAlmostEqual(data["last_sent"], now, places=1)
        leftovers = list(self.root.glob(".quota-refresh-*.tmp"))
        self.assertEqual(leftovers, [])

    async def test_failure_logs_throttled(self):
        now = time.time()
        self.install_accounts()
        self.install_chat()
        self.chat_error = providers.ProviderError("429")
        await quota_refresh.tick(self.ctx, now=now)
        count_first = len([r for r in self.ctx.log.records if "限额刷新失败" in r])
        await quota_refresh.tick(self.ctx, now=now + 1)
        count_second = len([r for r in self.ctx.log.records if "限额刷新失败" in r])
        self.assertEqual(count_first, count_second)  # 限频：不重复刷
        self.assertGreaterEqual(count_first, 1)

    async def test_logs_do_not_contain_tokens(self):
        now = time.time()
        self.records["claude"] = {
            "access": "SECRET-ACCESS-TOKEN",
            "refresh": "SECRET-REFRESH-TOKEN",
        }
        self.install_accounts()
        self.install_chat()
        self.chat_error = providers.ProviderError("429")
        await quota_refresh.tick(self.ctx, now=now)
        joined = "\n".join(self.ctx.log.records)
        self.assertNotIn("SECRET-ACCESS-TOKEN", joined)
        self.assertNotIn("SECRET-REFRESH-TOKEN", joined)


class RegistrationTests(unittest.TestCase):
    def test_config_defaults(self):
        import config

        cfg = config.DEFAULTS["plugins"]["quota_refresh"]
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["interval_minutes"], 301)
        self.assertEqual(cfg["providers"], ["claude", "chatgpt"])
        self.assertEqual(cfg["message"], "你好")

    def test_main_registers_loop(self):
        source = (BOT_DIR / "main.py").read_text(encoding="utf-8")
        self.assertIn("quota_refresh.tick", source)
        self.assertIn('name="quota-refresh"', source)
        self.assertIn("QUOTA_REFRESH_FIRST_DELAY = 90.0", source)
        self.assertIn("QUOTA_REFRESH_INTERVAL = 60.0", source)


class CommandTests(QuotaBase):
    def make_plugin(self):
        return commands.Plugin(self.ctx)

    def install_tick(self, result: dict):
        calls: list = []

        async def fake_tick(ctx, now=None, force=False):
            calls.append({"ctx": ctx, "force": force})
            return dict(result)

        _patch(self, quota_refresh, "tick", fake_tick)
        return calls

    async def test_refresh_command_reports_accounts(self):
        plugin = self.make_plugin()
        calls = self.install_tick(
            {
                "skipped": "",
                "refreshed": ["claude"],
                "failed": [{"provider": "chatgpt", "error": "HTTP 429"}],
                "updated": True,
            }
        )
        await plugin.on_event(_event("/刷新限额"))
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]["force"])
        reply = self.ctx.replies[-1]
        self.assertIn("✅ claude", reply)
        self.assertIn("⚠️ chatgpt", reply)
        self.assertIn("不会往 QQ 发消息", reply)

    async def test_refresh_command_disabled(self):
        plugin = self.make_plugin()
        self.install_tick(
            {"skipped": "disabled", "refreshed": [], "failed": [], "updated": False}
        )
        await plugin.on_event(_event("/刷新限额"))
        self.assertIn("关闭", self.ctx.replies[-1])

    async def test_refresh_command_no_accounts(self):
        plugin = self.make_plugin()
        self.install_tick(
            {"skipped": "no-accounts", "refreshed": [], "failed": [], "updated": False}
        )
        await plugin.on_event(_event("/刷新限额"))
        self.assertIn("没有已登录", self.ctx.replies[-1])

    async def test_refresh_command_admin_only(self):
        plugin = self.make_plugin()
        await plugin.on_event(_event("/刷新限额", user_id=20002))
        self.assertIn("只有管理员", self.ctx.replies[-1])

    async def test_help_mentions_refresh(self):
        plugin = self.make_plugin()
        await plugin.on_event(_event("/help"))
        self.assertIn("刷新限额", self.ctx.replies[-1])


if __name__ == "__main__":
    unittest.main()
