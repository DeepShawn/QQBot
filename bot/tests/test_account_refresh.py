"""T40 账号无感续期：429 busy / 刷新冷却 / 主动 keeper 测试（全离线）。"""
from __future__ import annotations

import asyncio
import io
import sys
import tempfile
import time
import types
import unittest
import urllib.error
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent.parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import account_core  # noqa: E402
import accounts_claude  # noqa: E402
import main as main_module  # noqa: E402


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


class Http429Tests(unittest.TestCase):
    def test_429_maps_busy(self):
        error = urllib.error.HTTPError(
            "https://example.com/token", 429, "rate_limit_error", {}, io.BytesIO(b"{}")
        )
        original = account_core.urllib.request.urlopen

        def boom(*args, **kwargs):
            raise error

        account_core.urllib.request.urlopen = boom
        try:
            with self.assertRaises(account_core.AccountError) as ctx:
                account_core.http_json("https://example.com/token", {"a": 1})
        finally:
            account_core.urllib.request.urlopen = original
        self.assertEqual(ctx.exception.code, "busy")
        self.assertIn("限流", ctx.exception.message)
        self.assertIn("稍后", ctx.exception.message)

    def test_error_map_still_overrides(self):
        error = urllib.error.HTTPError(
            "https://example.com/x", 429, "rate_limit_error", {}, io.BytesIO(b"{}")
        )
        original = account_core.urllib.request.urlopen

        def boom(*args, **kwargs):
            raise error

        account_core.urllib.request.urlopen = boom
        try:
            with self.assertRaises(account_core.AccountError) as ctx:
                account_core.http_json(
                    "https://example.com/x", {"a": 1}, error_map={429: ("generic", "额度用完")}
                )
        finally:
            account_core.urllib.request.urlopen = original
        self.assertEqual(ctx.exception.code, "generic")
        self.assertEqual(ctx.exception.message, "额度用完")


class ClaudeRefreshTests(unittest.TestCase):
    def test_busy_reraised(self):
        def fake_http(*args, **kwargs):
            raise account_core.AccountError("busy")

        _patch(self, account_core, "http_json", fake_http)
        with self.assertRaises(account_core.AccountError) as ctx:
            accounts_claude.refresh({"refresh": "r"})
        self.assertEqual(ctx.exception.code, "busy")

    def test_network_and_timeout_reraised(self):
        for code in ("network", "timeout"):
            with self.subTest(code=code):
                def fake_http(*args, **kwargs):
                    raise account_core.AccountError(code)

                _patch(self, account_core, "http_json", fake_http)
                with self.assertRaises(account_core.AccountError) as ctx:
                    accounts_claude.refresh({"refresh": "r"})
                self.assertEqual(ctx.exception.code, code)

    def test_generic_returns_none(self):
        def fake_http(*args, **kwargs):
            raise account_core.AccountError("generic")

        _patch(self, account_core, "http_json", fake_http)
        self.assertIsNone(accounts_claude.refresh({"refresh": "r"}))


class CooldownTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_accounts_file = account_core.ACCOUNTS_FILE
        account_core.ACCOUNTS_FILE = Path(self._tmp.name) / "accounts.json"
        self.addCleanup(setattr, account_core, "ACCOUNTS_FILE", self._old_accounts_file)
        account_core._module_cache.clear()
        with account_core._cooldown_lock:
            account_core._refresh_cooldowns.clear()
        self.addCleanup(self._cleanup)

        module = types.ModuleType("accounts_claude")
        module.__stub__ = True
        module.ID = "claude"
        module.NAME = "Claude"
        module.MODELS = ["m1"]
        module.DEFAULT_MODEL = "m1"
        module.begin = lambda attempt: {}
        module.refresh = lambda record: None
        module.label = lambda record: None
        self.module = module
        sys.modules["accounts_claude"] = module
        account_core._module_cache.pop("claude", None)
        self.addCleanup(sys.modules.pop, "accounts_claude", None)
        self.addCleanup(account_core._module_cache.pop, "claude", None)

    def _cleanup(self):
        with account_core._cooldown_lock:
            account_core._refresh_cooldowns.clear()
        account_core._module_cache.clear()

    def save_expired(self):
        account_core._save_record(
            "claude",
            {
                "access": "old-access",
                "refresh": "old-refresh",
                "expires_at": int(time.time()) - 10,
                "account": "a@b.com",
            },
        )


class EnsureAccessCooldownTests(CooldownTestCase):
    def test_busy_sets_cooldown_and_skips_http(self):
        calls: list = []

        def busy_refresh(record):
            calls.append(1)
            raise account_core.AccountError("busy")

        self.module.refresh = busy_refresh
        self.save_expired()
        with self.assertRaises(account_core.AccountError) as ctx:
            account_core.ensure_access("claude")
        self.assertEqual(ctx.exception.code, "busy")
        self.assertIn("限流", ctx.exception.message)
        self.assertIn("分钟后", ctx.exception.message)
        self.assertEqual(len(calls), 1)
        remaining = account_core.refresh_cooldown_remaining("claude")
        self.assertGreater(remaining, 500)
        self.assertLessEqual(remaining, account_core.REFRESH_COOLDOWN_SECONDS)

        # 冷却期内：不再触发 refresh（不发 HTTP）
        with self.assertRaises(account_core.AccountError) as ctx2:
            account_core.ensure_access("claude")
        self.assertEqual(ctx2.exception.code, "busy")
        self.assertIn("分钟后", ctx2.exception.message)
        self.assertEqual(len(calls), 1)

    def test_cooldown_expired_allows_retry_and_success_clears(self):
        calls: list = []

        def busy_refresh(record):
            calls.append(1)
            raise account_core.AccountError("busy")

        self.module.refresh = busy_refresh
        self.save_expired()
        with self.assertRaises(account_core.AccountError):
            account_core.ensure_access("claude")
        self.assertEqual(len(calls), 1)
        # 模拟冷却结束
        with account_core._cooldown_lock:
            account_core._refresh_cooldowns["claude"] = time.time() - 1

        def ok_refresh(record):
            calls.append(2)
            return {
                "access": "new-access",
                "refresh": "new-refresh",
                "expires_at": int(time.time()) + 3600,
                "account": record.get("account") or "",
            }

        self.module.refresh = ok_refresh
        record = account_core.ensure_access("claude")
        self.assertEqual(record["access"], "new-access")
        self.assertEqual(len(calls), 2)
        self.assertEqual(account_core.refresh_cooldown_remaining("claude"), 0)

    def test_network_error_does_not_set_cooldown(self):
        def net_refresh(record):
            raise account_core.AccountError("network")

        self.module.refresh = net_refresh
        self.save_expired()
        with self.assertRaises(account_core.AccountError) as ctx:
            account_core.ensure_access("claude")
        self.assertEqual(ctx.exception.code, "network")
        self.assertEqual(account_core.refresh_cooldown_remaining("claude"), 0)

    def test_timeout_does_not_set_cooldown(self):
        def timeout_refresh(record):
            raise account_core.AccountError("timeout")

        self.module.refresh = timeout_refresh
        self.save_expired()
        with self.assertRaises(account_core.AccountError) as ctx:
            account_core.ensure_access("claude")
        self.assertEqual(ctx.exception.code, "timeout")
        self.assertEqual(account_core.refresh_cooldown_remaining("claude"), 0)


class FakeAccountCore:
    PROVIDER_IDS = ("chatgpt", "claude", "gemini", "deepseek")

    def __init__(self, view, records, ensure_impl=None):
        self.view = view
        self.records = records
        self.ensure_calls: list = []
        self.ensure_impl = ensure_impl

    def list_view(self):
        return [dict(item) for item in self.view]

    def get_record(self, provider):
        return dict(self.records[provider]) if provider in self.records else None

    def ensure_access(self, provider):
        self.ensure_calls.append(provider)
        if self.ensure_impl is not None:
            return self.ensure_impl(provider)
        return {"access": "new"}


class KeeperTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._old_core = main_module.account_core
        self.log = FakeLog()
        self.addCleanup(setattr, main_module, "account_core", self._old_core)

    async def test_near_expiry_refreshes(self):
        now = 100000.0
        core = FakeAccountCore(
            [
                {"id": "claude", "signedIn": True},
                {"id": "chatgpt", "signedIn": False},
                {"id": "gemini", "signedIn": True},
            ],
            {
                "claude": {"expires_at": now + 600},  # 10 分钟 < 30 分钟
                "gemini": {"expires_at": now + 3600},  # 1 小时，不需要
            },
        )
        main_module.account_core = core
        result = await main_module.keeper_tick(now=now, logger=self.log)
        self.assertEqual(core.ensure_calls, ["claude"])
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]["ok"])

    async def test_expired_also_refreshes(self):
        now = 100000.0
        core = FakeAccountCore(
            [{"id": "claude", "signedIn": True}],
            {"claude": {"expires_at": now - 100}},
        )
        main_module.account_core = core
        await main_module.keeper_tick(now=now, logger=self.log)
        self.assertEqual(core.ensure_calls, ["claude"])

    async def test_exception_does_not_break_loop(self):
        now = 100000.0

        def ensure_impl(provider):
            if provider == "claude":
                raise account_core.AccountError("busy")
            return {"access": "ok"}

        core = FakeAccountCore(
            [
                {"id": "claude", "signedIn": True},
                {"id": "gemini", "signedIn": True},
            ],
            {
                "claude": {"expires_at": now + 60},
                "gemini": {"expires_at": now + 60},
            },
            ensure_impl=ensure_impl,
        )
        main_module.account_core = core
        result = await main_module.keeper_tick(now=now, logger=self.log)
        self.assertEqual(core.ensure_calls, ["claude", "gemini"])
        self.assertEqual([item["ok"] for item in result], [False, True])
        joined = "\n".join(self.log.records)
        self.assertIn("续期失败", joined)

    async def test_no_core(self):
        main_module.account_core = None
        self.assertEqual(await main_module.keeper_tick(logger=self.log), [])

    async def test_account_keeper_task_registered(self):
        source = (BOT_DIR / "main.py").read_text(encoding="utf-8")
        self.assertIn('name="account-keeper"', source)
        self.assertIn("keeper_tick", source)


if __name__ == "__main__":
    unittest.main()
