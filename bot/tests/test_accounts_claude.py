"""Claude OAuth 账号模块离线测试（全部 stub 掉网络/回调服务器）。"""
from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent.parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import account_core  # noqa: E402
import accounts_claude  # noqa: E402


class ClaudeTestCase(unittest.TestCase):
    def _patch(self, obj, name, value):
        old = getattr(obj, name)
        setattr(obj, name, value)
        self.addCleanup(setattr, obj, name, old)

    def _wait_phase(self, attempt, phase: str, timeout: float = 3.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if attempt.view()["phase"] == phase:
                return
            time.sleep(0.01)
        self.fail(f"等待 {phase} 超时，当前 {attempt.view()}")


class ConstantsTests(unittest.TestCase):
    def test_protocol_constants(self):
        self.assertEqual(accounts_claude.ID, "claude")
        self.assertEqual(accounts_claude.CLIENT_ID, "9d1c250a-e61b-44d9-88ed-5944d1962f5e")
        self.assertEqual(accounts_claude.AUTHORIZE_URL, "https://claude.ai/oauth/authorize")
        self.assertEqual(
            accounts_claude.TOKEN_URL, "https://platform.claude.com/v1/oauth/token"
        )
        self.assertEqual(accounts_claude.CALLBACK_PORT, 53692)
        self.assertEqual(accounts_claude.CALLBACK_PATH, "/callback")
        self.assertEqual(
            accounts_claude.REDIRECT_URI, "http://localhost:53692/callback"
        )
        self.assertIn("user:inference", accounts_claude.SCOPES)
        self.assertTrue(accounts_claude.MODELS)
        self.assertEqual(accounts_claude.DEFAULT_MODEL, accounts_claude.MODELS[0])
        self.assertEqual(accounts_claude.API_URL, "https://api.anthropic.com/v1/messages")


class RecordTests(unittest.TestCase):
    def test_record_from_token(self):
        record = accounts_claude._record_from_token(
            {"access_token": "access", "refresh_token": "refresh", "expires_in": 3600}
        )
        self.assertEqual(record["kind"], "oauth")
        self.assertEqual(record["access"], "access")
        self.assertEqual(record["refresh"], "refresh")
        self.assertGreater(record["expires_at"], int(time.time()) + 3000)
        self.assertEqual(record["extra"], {})

    def test_record_from_token_invalid(self):
        with self.assertRaises(account_core.AccountError):
            accounts_claude._record_from_token({})

    def test_label(self):
        self.assertIsNone(accounts_claude.label({}))
        self.assertEqual(accounts_claude.label({"account": "a@b.com"}), "a@b.com")


class RefreshTests(ClaudeTestCase):
    def test_refresh_success(self):
        captured: dict = {}

        def fake_http_json(url, payload=None, **kwargs):
            captured["url"] = url
            captured["payload"] = payload
            return {
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": 3600,
            }

        self._patch(account_core, "http_json", fake_http_json)
        record = accounts_claude.refresh({"refresh": "old-refresh"})
        self.assertEqual(captured["url"], accounts_claude.TOKEN_URL)
        self.assertEqual(captured["payload"]["grant_type"], "refresh_token")
        self.assertEqual(captured["payload"]["client_id"], accounts_claude.CLIENT_ID)
        self.assertEqual(captured["payload"]["refresh_token"], "old-refresh")
        self.assertEqual(record["access"], "new-access")
        self.assertEqual(record["refresh"], "new-refresh")

    def test_refresh_request_failure_returns_none(self):
        def fake_http_json(*args, **kwargs):
            raise account_core.AccountError("generic")

        self._patch(account_core, "http_json", fake_http_json)
        self.assertIsNone(accounts_claude.refresh({"refresh": "r"}))

    def test_refresh_network_raises(self):
        def fake_http_json(*args, **kwargs):
            raise account_core.AccountError("network")

        self._patch(account_core, "http_json", fake_http_json)
        with self.assertRaises(account_core.AccountError) as ctx:
            accounts_claude.refresh({"refresh": "r"})
        self.assertEqual(ctx.exception.code, "network")

    def test_refresh_without_refresh_token(self):
        self.assertIsNone(accounts_claude.refresh({}))


class BeginTests(ClaudeTestCase):
    def test_begin_with_callback_server(self):
        captured: dict = {}

        class FakeServer:
            def __init__(self, *args, **kwargs):
                self.redirect_uri = "http://localhost:53692/callback"

            def wait(self, timeout=None, attempt=None):
                return {"code": "the-code", "state": "verifier-1"}

            def close(self):
                captured["closed"] = True

        def fake_http_json(url, payload=None, **kwargs):
            captured["url"] = url
            captured["payload"] = payload
            return {
                "access_token": "access-1",
                "refresh_token": "refresh-1",
                "expires_in": 3600,
            }

        self._patch(account_core, "CallbackServer", FakeServer)
        self._patch(account_core, "generate_pkce", lambda: ("verifier-1", "challenge-1"))
        self._patch(account_core, "http_json", fake_http_json)

        attempt = account_core.Attempt("claude", timeout=10)
        record = accounts_claude.begin(attempt)
        self.assertEqual(record["access"], "access-1")
        self.assertEqual(captured["payload"]["grant_type"], "authorization_code")
        self.assertEqual(captured["payload"]["code"], "the-code")
        self.assertEqual(captured["payload"]["code_verifier"], "verifier-1")
        self.assertEqual(captured["payload"]["state"], "verifier-1")
        self.assertEqual(
            captured["payload"]["redirect_uri"], "http://localhost:53692/callback"
        )
        self.assertTrue(captured.get("closed"))

    def test_begin_prompt_fallback(self):
        captured: dict = {}
        result: dict = {}

        class NoServer:
            def __init__(self, *args, **kwargs):
                raise account_core.AccountError("generic")

        def fake_http_json(url, payload=None, **kwargs):
            captured["payload"] = payload
            return {
                "access_token": "access-2",
                "refresh_token": "refresh-2",
                "expires_in": 3600,
            }

        self._patch(account_core, "CallbackServer", NoServer)
        self._patch(account_core, "generate_pkce", lambda: ("verifier-2", "challenge-2"))
        self._patch(account_core, "http_json", fake_http_json)

        attempt = account_core.Attempt("claude", timeout=10)

        def worker():
            try:
                result["record"] = accounts_claude.begin(attempt)
            except Exception as e:  # pragma: no cover
                result["error"] = e

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        self._wait_phase(attempt, "prompt")
        self.assertTrue(
            attempt.submit_answer(
                "http://localhost:53692/callback?code=c0de&state=verifier-2"
            )
        )
        thread.join(timeout=5)
        self.assertNotIn("error", result)
        self.assertEqual(result["record"]["access"], "access-2")
        self.assertEqual(captured["payload"]["code"], "c0de")
        self.assertEqual(captured["payload"]["state"], "verifier-2")

    def test_begin_denied(self):
        class FakeServer:
            def __init__(self, *args, **kwargs):
                self.redirect_uri = "http://localhost:53692/callback"

            def wait(self, timeout=None, attempt=None):
                return {"error": "access_denied"}

            def close(self):
                pass

        self._patch(account_core, "CallbackServer", FakeServer)
        self._patch(account_core, "generate_pkce", lambda: ("v", "c"))
        attempt = account_core.Attempt("claude", timeout=10)
        with self.assertRaises(account_core.AccountError) as ctx:
            accounts_claude.begin(attempt)
        self.assertEqual(ctx.exception.code, "denied")

    def test_begin_state_mismatch(self):
        class FakeServer:
            def __init__(self, *args, **kwargs):
                self.redirect_uri = "http://localhost:53692/callback"

            def wait(self, timeout=None, attempt=None):
                return {"code": "c", "state": "other"}

            def close(self):
                pass

        self._patch(account_core, "CallbackServer", FakeServer)
        self._patch(account_core, "generate_pkce", lambda: ("v", "c"))
        attempt = account_core.Attempt("claude", timeout=10)
        with self.assertRaises(account_core.AccountError) as ctx:
            accounts_claude.begin(attempt)
        self.assertEqual(ctx.exception.code, "generic")


class IntegrationTests(ClaudeTestCase):
    """core.begin -> accounts_claude.begin -> poll done，验证两边的接线。"""

    def setUp(self):
        super().setUp()
        self._tmp_store = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp_store.cleanup)
        self._old_accounts_file = account_core.ACCOUNTS_FILE
        account_core.ACCOUNTS_FILE = Path(self._tmp_store.name) / "accounts.json"
        account_core.dispose()
        self.addCleanup(account_core.dispose)
        self.addCleanup(setattr, account_core, "ACCOUNTS_FILE", self._old_accounts_file)

    def test_begin_to_done(self):
        class FakeServer:
            def __init__(self, *args, **kwargs):
                self.redirect_uri = "http://localhost:53692/callback"

            def wait(self, timeout=None, attempt=None):
                return {"code": "integration-code", "state": "verifier-i"}

            def close(self):
                pass

        self._patch(account_core, "CallbackServer", FakeServer)
        self._patch(account_core, "generate_pkce", lambda: ("verifier-i", "challenge-i"))
        self._patch(
            account_core,
            "http_json",
            lambda url, payload=None, **kwargs: {
                "access_token": "integration-access",
                "refresh_token": "integration-refresh",
                "expires_in": 3600,
            },
        )
        started = account_core.begin("claude")
        attempt_id = started["attempt"]["id"]
        deadline = time.time() + 5
        view = None
        while time.time() < deadline:
            view = account_core.poll(attempt_id)["attempt"]
            if view["phase"] in ("done", "failed", "cancelled"):
                break
            time.sleep(0.01)
        self.assertEqual(view["phase"], "done", view)
        record = account_core.get_record("claude")
        self.assertEqual(record["access"], "integration-access")
        accounts = {item["id"]: item for item in account_core.list_view()}
        self.assertTrue(accounts["claude"]["signedIn"])


class ChatTests(ClaudeTestCase):
    def test_chat_request_and_parse(self):
        captured: dict = {}

        def fake_http_json(url, payload=None, **kwargs):
            captured["url"] = url
            captured["payload"] = payload
            captured["headers"] = kwargs.get("headers") or {}
            return {"content": [{"type": "text", "text": "你好"}]}

        self._patch(account_core, "http_json", fake_http_json)
        text = accounts_claude.chat(
            {"access": "access-token"},
            "",
            [
                {"role": "system", "content": "系统提示"},
                {"role": "user", "content": "在吗"},
                {"role": "assistant", "content": "在"},
                {"role": "user", "content": "聊聊"},
            ],
            0.7,
            800,
            30,
        )
        self.assertEqual(text, "你好")
        self.assertEqual(captured["url"], accounts_claude.API_URL)
        payload = captured["payload"]
        self.assertEqual(payload["model"], accounts_claude.DEFAULT_MODEL)
        self.assertEqual(payload["max_tokens"], 800)
        self.assertNotIn("temperature", payload)
        self.assertEqual(payload["system"][0]["text"], accounts_claude.CLAUDE_CODE_SYSTEM)
        self.assertEqual(payload["system"][1]["text"], "系统提示")
        self.assertEqual(len(payload["messages"]), 3)
        headers = captured["headers"]
        self.assertEqual(headers["Authorization"], "Bearer access-token")
        self.assertIn("oauth-2025-04-20", headers["anthropic-beta"])
        self.assertEqual(headers["x-app"], "cli")

    def test_chat_empty_conversation(self):
        with self.assertRaises(account_core.AccountError):
            accounts_claude.chat({"access": "a"}, "", [{"role": "system", "content": "x"}], 0.7, 100, 10)

    def test_chat_expired_marks_record(self):
        marked: list = []

        def fake_http_json(*args, **kwargs):
            raise account_core.AccountError("expired")

        self._patch(account_core, "http_json", fake_http_json)
        self._patch(account_core, "mark_expired", lambda pid: marked.append(pid))
        with self.assertRaises(account_core.AccountError) as ctx:
            accounts_claude.chat(
                {"access": "a"}, "", [{"role": "user", "content": "hi"}], 0.7, 100, 10
            )
        self.assertEqual(ctx.exception.code, "expired")
        self.assertEqual(marked, ["claude"])

    def test_chat_bad_response_shape(self):
        self._patch(account_core, "http_json", lambda *a, **k: {"content": "oops"})
        with self.assertRaises(account_core.AccountError):
            accounts_claude.chat(
                {"access": "a"}, "", [{"role": "user", "content": "hi"}], 0.7, 100, 10
            )


if __name__ == "__main__":
    unittest.main()
