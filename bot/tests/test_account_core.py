"""account_core 离线单元测试（不联网、不启动机器人/管理台）。"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent.parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import account_core  # noqa: E402


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


class _FakeHTTPResponse:
    """够 http_json / post_sse 用的假 HTTP 响应（上下文管理器 + 按行迭代）。"""

    def __init__(self, body: bytes = b"{}", lines=()):
        self._body = body
        self._lines = list(lines)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._body

    def close(self):
        pass

    def __iter__(self):
        return iter(self._lines)


def _request_user_agent(request) -> str:
    for key, value in request.header_items():
        if key.lower() == "user-agent":
            return str(value)
    return ""


class CoreTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_accounts_file = account_core.ACCOUNTS_FILE
        account_core.ACCOUNTS_FILE = Path(self._tmp.name) / "accounts.json"
        account_core.dispose()
        account_core._module_cache.clear()
        self.addCleanup(self._restore)

    def _restore(self):
        account_core.dispose()
        account_core._module_cache.clear()
        account_core.ACCOUNTS_FILE = self._old_accounts_file
        for pid in account_core.PROVIDER_IDS:
            name = f"accounts_{pid}"
            module = sys.modules.get(name)
            if module is not None and getattr(module, "__stub__", False):
                sys.modules.pop(name, None)

    def _install_stub(self, pid: str, **attrs):
        module = types.ModuleType(f"accounts_{pid}")
        module.__stub__ = True
        module.ID = pid
        module.NAME = account_core.PROVIDER_NAMES[pid]
        module.MODELS = ["model-a"]
        module.DEFAULT_MODEL = "model-a"
        module.begin = lambda attempt: {
            "access": "access",
            "refresh": "refresh",
            "expires_at": int(time.time()) + 3600,
            "account": "stub@example.com",
            "extra": {},
        }
        module.refresh = lambda record: None
        module.chat = lambda *a, **k: "stub"
        module.label = lambda record: str(record.get("account") or "") or None
        for key, value in attrs.items():
            setattr(module, key, value)
        sys.modules[f"accounts_{pid}"] = module
        account_core._module_cache.pop(pid, None)
        self.addCleanup(sys.modules.pop, f"accounts_{pid}", None)
        self.addCleanup(account_core._module_cache.pop, pid, None)
        return module

    def _wait_phase(self, attempt_id: str, phases, timeout: float = 5.0) -> dict:
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            last = account_core.poll(attempt_id)["attempt"]
            if last["phase"] in phases:
                return last
            time.sleep(0.01)
        self.fail(f"等待阶段 {phases} 超时，最后状态 {last}")


class StoreTests(CoreTestCase):
    def test_save_load_atomic_and_permissions(self):
        saved = account_core._save_record(
            "claude",
            {
                "access": "access-token",
                "refresh": "refresh-token",
                "expires_at": 1794000000,
                "account": "a@b.com",
                "extra": {"chatgpt_account_id": "x"},
            },
        )
        self.assertEqual(saved["kind"], "oauth")
        path = Path(account_core.ACCOUNTS_FILE)
        self.assertTrue(path.exists())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(raw["version"], 1)
        self.assertEqual(raw["accounts"]["claude"]["access"], "access-token")
        self.assertEqual(raw["accounts"]["claude"]["expires_at"], 1794000000)
        self.assertEqual(
            sorted(p.name for p in path.parent.iterdir()), ["accounts.json"]
        )
        record = account_core.get_record("claude")
        self.assertEqual(record["account"], "a@b.com")

    def test_corrupt_file_treated_as_empty(self):
        path = Path(account_core.ACCOUNTS_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(account_core.get_record("claude"))
        ids = [item["id"] for item in account_core.list_view()]
        self.assertEqual(ids, list(account_core.PROVIDER_IDS))

    def test_delete_record(self):
        account_core._save_record("chatgpt", {"access": "a", "expires_at": 1})
        account_core.signout("chatgpt")
        self.assertIsNone(account_core.get_record("chatgpt"))
        view = {item["id"]: item for item in account_core.list_view()}
        self.assertFalse(view["chatgpt"]["signedIn"])


class PkceTests(unittest.TestCase):
    def test_s256(self):
        verifier, challenge = account_core.generate_pkce()
        self.assertRegex(verifier, r"^[A-Za-z0-9_-]{43}$")
        self.assertRegex(challenge, r"^[A-Za-z0-9_-]{43}$")
        expected = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
        self.assertEqual(challenge, expected)
        other_verifier, other_challenge = account_core.generate_pkce()
        self.assertNotEqual(verifier, other_verifier)
        self.assertNotEqual(challenge, other_challenge)


class RedactTests(unittest.TestCase):
    def test_redacts_tokens(self):
        jwt = (
            "eyJhbGciOiJIUzI1NiJ9."
            + _b64url(json.dumps({"email": "a@b.com"}).encode())
            + ".signature"
        )
        text = (
            f"Bearer abcdefghijklmnop access_token=secret123 "
            f"sk-ant-oat-abcdefghijkl {jwt} code=abcdef"
        )
        cleaned = account_core.redact(text)
        for secret in ("abcdefghijklmnop", "secret123", "sk-ant-oat-abcdefghijkl", jwt, "abcdef"):
            self.assertNotIn(secret, cleaned)
        self.assertIn("***", cleaned)
        self.assertEqual(account_core.redact("nothing here"), "nothing here")


class AttemptTests(unittest.TestCase):
    def test_phases_and_view(self):
        attempt = account_core.Attempt("claude")
        self.assertEqual(attempt.view()["phase"], "starting")
        attempt.notify_url("https://claude.ai/oauth/authorize?x=1", "在浏览器里完成登录")
        view = attempt.view()
        self.assertEqual(view["phase"], "open")
        self.assertEqual(view["url"], "https://claude.ai/oauth/authorize?x=1")
        attempt.notify_device("https://auth.openai.com/codex/device", "ABCD-1234")
        view = attempt.view()
        self.assertEqual(view["phase"], "device")
        self.assertEqual(view["userCode"], "ABCD-1234")
        with self.assertRaises(account_core.AccountError):
            attempt.notify_url("http://evil.example/callback")

    def test_prompt_answer(self):
        attempt = account_core.Attempt("claude")
        result: dict = {}

        def worker():
            try:
                result["value"] = attempt.prompt("粘贴网址")
            except Exception as e:  # pragma: no cover
                result["error"] = e

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        deadline = time.time() + 3
        while attempt.view()["phase"] != "prompt" and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(attempt.view()["placeholder"], "粘贴网址")
        self.assertTrue(attempt.submit_answer("code-123"))
        thread.join(timeout=3)
        self.assertEqual(result.get("value"), "code-123")
        self.assertNotIn("error", result)

    def test_prompt_timeout(self):
        attempt = account_core.Attempt("claude", timeout=0.15)
        with self.assertRaises(account_core.AccountError) as ctx:
            attempt.prompt("粘贴网址")
        self.assertEqual(ctx.exception.code, "timeout")

    def test_cancel_raises_cancelled(self):
        attempt = account_core.Attempt("claude")
        errors: list = []

        def worker():
            try:
                attempt.prompt("粘贴网址")
            except Exception as e:
                errors.append(e)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        deadline = time.time() + 3
        while attempt.view()["phase"] != "prompt" and time.time() < deadline:
            time.sleep(0.01)
        attempt.cancel()
        thread.join(timeout=3)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], account_core.AttemptCancelled)
        self.assertEqual(attempt.view()["phase"], "cancelled")
        # 取消后不再接受答案更新
        self.assertFalse(attempt.submit_answer("late"))

    def test_expire_marks_failed_timeout(self):
        attempt = account_core.Attempt("claude")
        attempt.expire_now()
        view = attempt.view()
        self.assertEqual(view["phase"], "failed")
        self.assertEqual(view["error"], account_core.ERRORS["timeout"])


class HttpHelpersTests(unittest.TestCase):
    def test_post_sse_parses_events(self):
        payloads = [
            'data: {"type":"response.created"}\n\n',
            'data: {"type":"response.output_text.delta","delta":"你"}\n\n',
            "data: [DONE]\n\n",
        ]

        class FakeResponse:
            def __init__(self):
                self._lines = "".join(payloads).encode("utf-8").splitlines(keepends=True)

            def __iter__(self):
                return iter(self._lines)

            def close(self):
                pass

        original = account_core.urllib.request.urlopen
        account_core.urllib.request.urlopen = lambda *a, **k: FakeResponse()
        try:
            events = account_core.post_sse("https://example.com/x", {"a": 1})
        finally:
            account_core.urllib.request.urlopen = original
        self.assertEqual(
            [event["type"] for event in events],
            ["response.created", "response.output_text.delta"],
        )

    def test_post_sse_maps_401_to_expired(self):
        error = urllib.error.HTTPError(
            "https://example.com/x", 401, "auth", {}, io.BytesIO(b'{"error":"bad"}')
        )
        original = account_core.urllib.request.urlopen

        def boom(*args, **kwargs):
            raise error

        account_core.urllib.request.urlopen = boom
        try:
            with self.assertRaises(account_core.AccountError) as ctx:
                account_core.post_sse(
                    "https://example.com/x",
                    {"a": 1},
                    error_map={401: ("expired", None)},
                )
        finally:
            account_core.urllib.request.urlopen = original
        self.assertEqual(ctx.exception.code, "expired")

    def test_default_user_agent_is_browser(self):
        self.assertIn("BROWSER_USER_AGENT", account_core.__all__)
        self.assertIn("Mozilla/5.0", account_core.BROWSER_USER_AGENT)
        self.assertIn("Android", account_core.BROWSER_USER_AGENT)
        self.assertIn("Chrome/", account_core.BROWSER_USER_AGENT)
        captured: list = []
        original = account_core.urllib.request.urlopen
        account_core.urllib.request.urlopen = lambda request, *a, **k: (
            captured.append(request) or _FakeHTTPResponse(b'{"ok":true}')
        )
        try:
            data = account_core.http_json("https://example.com/x", {"a": 1})
        finally:
            account_core.urllib.request.urlopen = original
        self.assertEqual(data, {"ok": True})
        self.assertEqual(
            _request_user_agent(captured[0]), account_core.BROWSER_USER_AGENT
        )

    def test_caller_user_agent_wins(self):
        original = account_core.urllib.request.urlopen
        try:
            for header_key in ("User-Agent", "user-agent"):
                captured: list = []
                account_core.urllib.request.urlopen = lambda request, *a, **k: (
                    captured.append(request) or _FakeHTTPResponse(b'{"ok":true}')
                )
                account_core.http_json(
                    "https://example.com/x",
                    {"a": 1},
                    headers={header_key: "my-agent/9.9"},
                )
                self.assertEqual(
                    _request_user_agent(captured[0]),
                    "my-agent/9.9",
                    f"调用方传入的 {header_key} 应该优先",
                )
        finally:
            account_core.urllib.request.urlopen = original

    def test_post_sse_default_user_agent_is_browser(self):
        captured: list = []
        lines = [b'data: {"type":"response.completed"}\n', b"\n"]
        original = account_core.urllib.request.urlopen
        account_core.urllib.request.urlopen = lambda request, *a, **k: (
            captured.append(request) or _FakeHTTPResponse(lines=lines)
        )
        try:
            account_core.post_sse("https://example.com/x", {"a": 1})
        finally:
            account_core.urllib.request.urlopen = original
        self.assertEqual(
            _request_user_agent(captured[0]), account_core.BROWSER_USER_AGENT
        )

    def test_http_json_network_error(self):
        original = account_core.urllib.request.urlopen

        def boom(*args, **kwargs):
            raise urllib.error.URLError("no route")

        account_core.urllib.request.urlopen = boom
        try:
            with self.assertRaises(account_core.AccountError) as ctx:
                account_core.http_json("https://example.com/x", {"a": 1})
        finally:
            account_core.urllib.request.urlopen = original
        self.assertEqual(ctx.exception.code, "network")


class CallbackServerTests(unittest.TestCase):
    def test_callback_once(self):
        server = account_core.CallbackServer(port=0)
        try:
            self.assertTrue(server.redirect_uri.startswith("http://localhost:"))
            self.assertTrue(server.redirect_uri.endswith("/callback"))
            url = server.redirect_uri.replace("localhost", "127.0.0.1")

            # 错误路径不会认领
            with self.assertRaises(urllib.error.HTTPError) as wrong:
                urllib.request.urlopen(url.replace("/callback", "/other"), timeout=3)
            self.assertEqual(wrong.exception.code, 404)

            with urllib.request.urlopen(url + "?code=abc&state=s1", timeout=3) as response:
                body = response.read().decode("utf-8")
                self.assertEqual(response.status, 200)
            self.assertIn("登录完成", body)

            query = server.wait(2)
            self.assertEqual(query["code"], "abc")
            self.assertEqual(query["state"], "s1")

            # 只接受一次
            with self.assertRaises(urllib.error.HTTPError) as duplicate:
                urllib.request.urlopen(url + "?code=again", timeout=3)
            self.assertEqual(duplicate.exception.code, 409)
        finally:
            server.close()

    def test_bind_conflict(self):
        first = account_core.CallbackServer(port=0)
        try:
            with self.assertRaises(account_core.AccountError) as ctx:
                account_core.CallbackServer(port=first.port)
            self.assertEqual(ctx.exception.code, "generic")
        finally:
            first.close()

    def test_wait_timeout(self):
        server = account_core.CallbackServer(port=0)
        try:
            with self.assertRaises(account_core.AccountError) as ctx:
                server.wait(0.15)
            self.assertEqual(ctx.exception.code, "timeout")
        finally:
            server.close()


class RegistryTests(CoreTestCase):
    def test_begin_poll_answer_done(self):
        def begin(attempt):
            attempt.set_phase("starting", "正在准备登录…")
            attempt.notify_url("https://example.com/oauth?x=1")
            value = attempt.prompt("粘贴地址栏里的完整网址")
            if value != "good":
                raise account_core.AccountError("generic", "输入不对")
            return {
                "access": "access-1",
                "refresh": "refresh-1",
                "expires_at": int(time.time()) + 3600,
                "account": "tester@example.com",
                "extra": {},
            }

        self._install_stub("claude", begin=begin)
        started = account_core.begin("claude")
        attempt_id = started["attempt"]["id"]
        self._wait_phase(attempt_id, ("prompt",))
        account_core.answer(attempt_id, "good")
        view = self._wait_phase(attempt_id, ("done", "failed"))
        self.assertEqual(view["phase"], "done")
        self.assertEqual(view["account"], "tester@example.com")
        record = account_core.get_record("claude")
        self.assertEqual(record["access"], "access-1")
        self.assertEqual(Path(account_core.ACCOUNTS_FILE).stat().st_mode & 0o777, 0o600)

    def test_begin_again_cancels_old(self):
        self._install_stub(
            "claude",
            begin=lambda attempt: attempt.prompt("粘贴网址") or None,
        )
        first_id = account_core.begin("claude")["attempt"]["id"]
        self._wait_phase(first_id, ("prompt",))
        second_id = account_core.begin("claude")["attempt"]["id"]
        self.assertNotEqual(first_id, second_id)
        old = self._wait_phase(first_id, ("cancelled", "failed", "done"))
        self.assertEqual(old["phase"], "cancelled")
        account_core.cancel(second_id)

    def test_missing_module_is_unsupported(self):
        sys.modules["accounts_gemini"] = None
        account_core._module_cache.pop("gemini", None)
        try:
            view = {item["id"]: item for item in account_core.list_view()}
            self.assertFalse(view["gemini"]["signedIn"])
            self.assertEqual(view["gemini"]["error"], "unsupported")
            with self.assertRaises(account_core.AccountError) as ctx:
                account_core.begin("gemini")
            self.assertEqual(ctx.exception.code, "unsupported")
        finally:
            sys.modules.pop("accounts_gemini", None)
            account_core._module_cache.pop("gemini", None)

    def test_invalid_provider(self):
        with self.assertRaises(account_core.AccountError) as ctx:
            account_core.begin("not-a-provider")
        self.assertEqual(ctx.exception.code, "unsupported")
        with self.assertRaises(account_core.AccountError):
            account_core.poll("missing-attempt-id")

    def test_answer_validation(self):
        self._install_stub(
            "claude",
            begin=lambda attempt: attempt.prompt("粘贴网址") or None,
        )
        attempt_id = account_core.begin("claude")["attempt"]["id"]
        self._wait_phase(attempt_id, ("prompt",))
        with self.assertRaises(account_core.AccountError):
            account_core.answer(attempt_id, "   ")
        account_core.cancel(attempt_id)

    def test_list_view_signed_in_stub(self):
        self._install_stub("chatgpt")
        account_core._save_record(
            "chatgpt",
            {"access": "a", "refresh": "r", "expires_at": int(time.time()) + 3600,
             "account": "u@example.com"},
        )
        view = {item["id"]: item for item in account_core.list_view()}
        self.assertTrue(view["chatgpt"]["signedIn"])
        self.assertEqual(view["chatgpt"]["account"], "u@example.com")
        self.assertEqual(view["chatgpt"]["models"], ["model-a"])
        self.assertEqual(view["chatgpt"]["default_model"], "model-a")
        self.assertIsNone(view["chatgpt"]["error"])

    def test_ensure_access_refresh(self):
        calls: list = []

        def refresh(record):
            calls.append(dict(record))
            return {
                "access": "new-access",
                "refresh": "new-refresh",
                "expires_at": int(time.time()) + 3600,
                "account": record.get("account") or "",
            }

        self._install_stub("claude", refresh=refresh)
        account_core._save_record(
            "claude",
            {"access": "old", "refresh": "old-refresh",
             "expires_at": int(time.time()) - 10, "account": "a@b.com"},
        )
        record = account_core.ensure_access("claude")
        self.assertEqual(record["access"], "new-access")
        self.assertEqual(record["account"], "a@b.com")
        self.assertEqual(len(calls), 1)
        # 第二次不再刷新
        self.assertEqual(account_core.ensure_access("claude")["access"], "new-access")
        self.assertEqual(len(calls), 1)

    def test_ensure_access_no_record_and_bad_refresh(self):
        self._install_stub("claude")
        with self.assertRaises(account_core.AccountError) as ctx:
            account_core.ensure_access("claude")
        self.assertEqual(ctx.exception.code, "not-signed-in")

        self._install_stub("chatgpt", refresh=lambda record: None)
        account_core._save_record(
            "chatgpt",
            {"access": "old", "refresh": "r", "expires_at": int(time.time()) - 10},
        )
        with self.assertRaises(account_core.AccountError) as ctx2:
            account_core.ensure_access("chatgpt")
        self.assertEqual(ctx2.exception.code, "expired")

    def test_ensure_access_network_error_propagates(self):
        def refresh(record):
            raise account_core.AccountError("network")

        self._install_stub("claude", refresh=refresh)
        account_core._save_record(
            "claude",
            {"access": "old", "refresh": "r", "expires_at": int(time.time()) - 10},
        )
        with self.assertRaises(account_core.AccountError) as ctx:
            account_core.ensure_access("claude")
        self.assertEqual(ctx.exception.code, "network")

    def test_dispose_cancels_inflight(self):
        self._install_stub(
            "claude",
            begin=lambda attempt: attempt.prompt("粘贴网址") or None,
        )
        attempt_id = account_core.begin("claude")["attempt"]["id"]
        self._wait_phase(attempt_id, ("prompt",))
        account_core.dispose()
        self.assertEqual(account_core._attempts, {})
        self.assertEqual(account_core._active, {})


if __name__ == "__main__":
    unittest.main()
