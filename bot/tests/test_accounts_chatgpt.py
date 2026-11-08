"""ChatGPT（Codex）OAuth 账号模块离线测试（全部 stub 掉网络/回调服务器）。"""
from __future__ import annotations

import base64
import io
import json
import sys
import threading
import time
import unittest
import urllib.error
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent.parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import account_core  # noqa: E402
import accounts_chatgpt  # noqa: E402


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _jwt(payload: dict) -> str:
    body = _b64url(json.dumps(payload).encode("utf-8"))
    return f"eyJhbGciOiJub25lIn0.{body}.c2ln"


def _access_token(account_id: str = "acct-1", email: str = "u@example.com") -> str:
    return _jwt({"email": email, accounts_chatgpt.JWT_CLAIM_PATH: {"chatgpt_account_id": account_id}})


def _token_data() -> dict:
    return {
        "access_token": _access_token(),
        "refresh_token": "refresh-1",
        "expires_in": 3600,
    }


class ChatGPTTestCase(unittest.TestCase):
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
        self.assertEqual(accounts_chatgpt.ID, "chatgpt")
        self.assertEqual(accounts_chatgpt.CLIENT_ID, "app_EMoamEEZ73f0CkXaXp7hrann")
        self.assertEqual(accounts_chatgpt.AUTHORIZE_URL, "https://auth.openai.com/oauth/authorize")
        self.assertEqual(accounts_chatgpt.TOKEN_URL, "https://auth.openai.com/oauth/token")
        self.assertEqual(accounts_chatgpt.CALLBACK_PORT, 1455)
        self.assertEqual(accounts_chatgpt.CALLBACK_PATH, "/auth/callback")
        self.assertEqual(
            accounts_chatgpt.REDIRECT_URI, "http://localhost:1455/auth/callback"
        )
        self.assertEqual(accounts_chatgpt.SCOPE, "openid profile email offline_access")
        self.assertEqual(
            accounts_chatgpt.DEVICE_VERIFICATION_URI, "https://auth.openai.com/codex/device"
        )
        self.assertEqual(
            accounts_chatgpt.DEVICE_REDIRECT_URI,
            "https://auth.openai.com/deviceauth/callback",
        )
        self.assertEqual(
            accounts_chatgpt.API_URL, "https://chatgpt.com/backend-api/codex/responses"
        )
        self.assertTrue(accounts_chatgpt.MODELS)
        self.assertEqual(
            accounts_chatgpt.DEFAULT_MODEL, accounts_chatgpt.MODELS[0]
        )


class JwtTests(unittest.TestCase):
    def test_account_id_and_email(self):
        token = _access_token("acct-9", "me@example.com")
        self.assertEqual(accounts_chatgpt._account_id(token), "acct-9")
        self.assertEqual(accounts_chatgpt._email(token), "me@example.com")
        self.assertEqual(accounts_chatgpt._account_id("not-a-jwt"), "")
        self.assertEqual(accounts_chatgpt._email("not-a-jwt"), "")

    def test_record_from_token(self):
        record = accounts_chatgpt._record_from_token(_token_data())
        self.assertEqual(record["kind"], "oauth")
        self.assertEqual(record["access"], _token_data()["access_token"])
        self.assertEqual(record["refresh"], "refresh-1")
        self.assertEqual(record["extra"], {"chatgpt_account_id": "acct-1"})
        self.assertEqual(record["account"], "u@example.com")
        self.assertGreater(record["expires_at"], int(time.time()) + 3000)

    def test_record_from_token_invalid(self):
        with self.assertRaises(account_core.AccountError):
            accounts_chatgpt._record_from_token({})
        with self.assertRaises(account_core.AccountError):
            accounts_chatgpt._record_from_token({"access_token": "a", "expires_in": "x"})

    def test_label(self):
        self.assertEqual(
            accounts_chatgpt.label({"account": "a@b.com"}), "a@b.com"
        )
        self.assertEqual(
            accounts_chatgpt.label({"access": _access_token(email="x@y.com")}),
            "x@y.com",
        )


class DeviceFlowTests(ChatGPTTestCase):
    def test_begin_device_flow(self):
        calls: list = []

        def fake_http_json(url, payload=None, **kwargs):
            calls.append((url, payload, kwargs))
            if url == accounts_chatgpt.DEVICE_USER_CODE_URL:
                return {
                    "device_auth_id": "dev-1",
                    "user_code": "ABCD-1234",
                    "interval": "1",
                }
            if url == accounts_chatgpt.TOKEN_URL:
                return _token_data()
            raise AssertionError(f"unexpected url {url}")

        self._patch(account_core, "http_json", fake_http_json)
        self._patch(
            accounts_chatgpt,
            "_device_poll_once",
            lambda device_id, user_code: {"code": "auth-code", "verifier": "code-verifier"},
        )
        attempt = account_core.Attempt("chatgpt", timeout=10)
        record = accounts_chatgpt.begin(attempt)
        self.assertEqual(record["extra"]["chatgpt_account_id"], "acct-1")
        self.assertEqual(record["account"], "u@example.com")
        urls = [call[0] for call in calls]
        self.assertIn(accounts_chatgpt.DEVICE_USER_CODE_URL, urls)
        token_call = next(call for call in calls if call[0] == accounts_chatgpt.TOKEN_URL)
        payload = token_call[1]
        self.assertEqual(payload["grant_type"], "authorization_code")
        self.assertEqual(payload["code"], "auth-code")
        self.assertEqual(payload["code_verifier"], "code-verifier")
        self.assertEqual(payload["redirect_uri"], accounts_chatgpt.DEVICE_REDIRECT_URI)
        self.assertTrue(token_call[2].get("form"))

    def test_begin_browser_fallback(self):
        captured: dict = {}

        class FakeServer:
            def __init__(self, *args, **kwargs):
                self.redirect_uri = "http://localhost:1455/auth/callback"

            def wait(self, timeout=None, attempt=None):
                return {"code": "browser-code", "state": "state-3"}

            def close(self):
                captured["closed"] = True

        def fake_http_json(url, payload=None, **kwargs):
            captured["url"] = url
            captured["payload"] = payload
            return _token_data()

        self._patch(
            accounts_chatgpt,
            "_device_request",
            lambda: (_ for _ in ()).throw(accounts_chatgpt._DeviceUnavailable("no device")),
        )
        self._patch(account_core, "CallbackServer", FakeServer)
        self._patch(account_core, "generate_pkce", lambda: ("verifier-3", "challenge-3"))
        self._patch(account_core, "random_state", lambda: "state-3")
        self._patch(account_core, "http_json", fake_http_json)

        attempt = account_core.Attempt("chatgpt", timeout=10)
        record = accounts_chatgpt.begin(attempt)
        self.assertEqual(record["access"], _token_data()["access_token"])
        self.assertEqual(captured["payload"]["code"], "browser-code")
        self.assertEqual(captured["payload"]["code_verifier"], "verifier-3")
        self.assertEqual(
            captured["payload"]["redirect_uri"], "http://localhost:1455/auth/callback"
        )
        self.assertTrue(captured.get("closed"))

    def test_begin_browser_prompt_fallback(self):
        result: dict = {}

        class NoServer:
            def __init__(self, *args, **kwargs):
                raise account_core.AccountError("generic")

        def fake_http_json(url, payload=None, **kwargs):
            return _token_data()

        self._patch(
            accounts_chatgpt,
            "_device_request",
            lambda: (_ for _ in ()).throw(accounts_chatgpt._DeviceUnavailable("no device")),
        )
        self._patch(account_core, "CallbackServer", NoServer)
        self._patch(account_core, "generate_pkce", lambda: ("v4", "c4"))
        self._patch(account_core, "random_state", lambda: "state-4")
        self._patch(account_core, "http_json", fake_http_json)

        attempt = account_core.Attempt("chatgpt", timeout=10)

        def worker():
            try:
                result["record"] = accounts_chatgpt.begin(attempt)
            except Exception as e:  # pragma: no cover
                result["error"] = e

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        self._wait_phase(attempt, "prompt")
        self.assertTrue(
            attempt.submit_answer(
                "http://localhost:1455/auth/callback?code=pasted-code&state=state-4"
            )
        )
        thread.join(timeout=5)
        self.assertNotIn("error", result)
        self.assertEqual(result["record"]["extra"]["chatgpt_account_id"], "acct-1")

    def test_device_poll_status_mapping(self):
        original = accounts_chatgpt.urllib.request.urlopen

        def as_http_error(status: int, body: bytes):
            def boom(*args, **kwargs):
                raise urllib.error.HTTPError(
                    accounts_chatgpt.DEVICE_TOKEN_URL, status, "err", {}, io.BytesIO(body)
                )

            return boom

        cases = [
            (as_http_error(403, b""), None),
            (as_http_error(404, b""), None),
            (
                as_http_error(400, b'{"error":{"code":"deviceauth_authorization_pending"}}'),
                None,
            ),
            (as_http_error(400, b'{"error":"slow_down"}'), "slow_down"),
        ]
        try:
            for fake, expected in cases:
                accounts_chatgpt.urllib.request.urlopen = fake
                self.assertEqual(
                    accounts_chatgpt._device_poll_once("dev", "code"), expected
                )
        finally:
            accounts_chatgpt.urllib.request.urlopen = original

    def test_device_poll_success(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(
                    {"authorization_code": "ac", "code_verifier": "cv"}
                ).encode("utf-8")

        original = accounts_chatgpt.urllib.request.urlopen
        accounts_chatgpt.urllib.request.urlopen = lambda *a, **k: FakeResponse()
        try:
            self.assertEqual(
                accounts_chatgpt._device_poll_once("dev", "code"),
                {"code": "ac", "verifier": "cv"},
            )
        finally:
            accounts_chatgpt.urllib.request.urlopen = original


class RefreshTests(ChatGPTTestCase):
    def test_refresh_success(self):
        captured: dict = {}

        def fake_http_json(url, payload=None, **kwargs):
            captured["url"] = url
            captured["payload"] = payload
            captured["form"] = kwargs.get("form")
            return _token_data()

        self._patch(account_core, "http_json", fake_http_json)
        record = accounts_chatgpt.refresh({"refresh": "old-refresh"})
        self.assertEqual(captured["url"], accounts_chatgpt.TOKEN_URL)
        self.assertEqual(captured["payload"]["grant_type"], "refresh_token")
        self.assertEqual(captured["payload"]["client_id"], accounts_chatgpt.CLIENT_ID)
        self.assertTrue(captured["form"])
        self.assertEqual(record["extra"]["chatgpt_account_id"], "acct-1")

    def test_refresh_failure(self):
        def fake_http_json(*args, **kwargs):
            raise account_core.AccountError("generic")

        self._patch(account_core, "http_json", fake_http_json)
        self.assertIsNone(accounts_chatgpt.refresh({"refresh": "r"}))
        self.assertIsNone(accounts_chatgpt.refresh({}))

    def test_refresh_network_raises(self):
        def fake_http_json(*args, **kwargs):
            raise account_core.AccountError("network")

        self._patch(account_core, "http_json", fake_http_json)
        with self.assertRaises(account_core.AccountError):
            accounts_chatgpt.refresh({"refresh": "r"})


class ChatTests(ChatGPTTestCase):
    def test_chat_request_and_sse(self):
        captured: dict = {}

        def fake_post_sse(url, payload, **kwargs):
            captured["url"] = url
            captured["payload"] = payload
            captured["headers"] = kwargs.get("headers") or {}
            return [
                {"type": "response.created"},
                {"type": "response.output_text.delta", "delta": "你"},
                {"type": "response.output_text.delta", "delta": "好"},
                {"type": "response.completed"},
            ]

        self._patch(account_core, "post_sse", fake_post_sse)
        record = {"access": _access_token("acct-7"), "extra": {}}
        text = accounts_chatgpt.chat(
            record,
            "",
            [
                {"role": "system", "content": "系统提示"},
                {"role": "user", "content": "在吗"},
                {"role": "assistant", "content": "在"},
                {"role": "user", "content": "聊聊"},
            ],
            0.7,
            900,
            60,
        )
        self.assertEqual(text, "你好")
        self.assertEqual(captured["url"], accounts_chatgpt.API_URL)
        payload = captured["payload"]
        self.assertEqual(payload["model"], accounts_chatgpt.DEFAULT_MODEL)
        self.assertFalse(payload["store"])
        self.assertTrue(payload["stream"])
        self.assertEqual(payload["instructions"], "系统提示")
        self.assertEqual(payload["max_output_tokens"], 900)
        self.assertEqual(payload["input"][0]["role"], "user")
        self.assertEqual(payload["input"][1]["type"], "message")
        headers = captured["headers"]
        self.assertEqual(headers["chatgpt-account-id"], "acct-7")
        self.assertEqual(headers["OpenAI-Beta"], "responses=experimental")
        self.assertEqual(headers["Authorization"], f"Bearer {record['access']}")

    def test_chat_prefers_final_message(self):
        self._patch(
            account_core,
            "post_sse",
            lambda *a, **k: [
                {"type": "response.output_text.delta", "delta": "半"},
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "完整回答"}],
                    },
                },
            ],
        )
        text = accounts_chatgpt.chat(
            {"access": _access_token(), "extra": {}},
            "",
            [{"role": "user", "content": "hi"}],
            0.7,
            100,
            10,
        )
        self.assertEqual(text, "完整回答")

    def test_chat_response_failed(self):
        self._patch(
            account_core,
            "post_sse",
            lambda *a, **k: [{"type": "response.failed", "response": {}}],
        )
        with self.assertRaises(account_core.AccountError):
            accounts_chatgpt.chat(
                {"access": _access_token(), "extra": {}},
                "",
                [{"role": "user", "content": "hi"}],
                0.7,
                100,
                10,
            )

    def test_chat_missing_account_id(self):
        with self.assertRaises(account_core.AccountError):
            accounts_chatgpt.chat(
                {"access": _jwt({"email": "a@b.com"}), "extra": {}},
                "",
                [{"role": "user", "content": "hi"}],
                0.7,
                100,
                10,
            )

    def test_chat_expired_marks_record(self):
        marked: list = []

        def fake_post_sse(*args, **kwargs):
            raise account_core.AccountError("expired")

        self._patch(account_core, "post_sse", fake_post_sse)
        self._patch(account_core, "mark_expired", lambda pid: marked.append(pid))
        with self.assertRaises(account_core.AccountError) as ctx:
            accounts_chatgpt.chat(
                {"access": _access_token(), "extra": {}},
                "",
                [{"role": "user", "content": "hi"}],
                0.7,
                100,
                10,
            )
        self.assertEqual(ctx.exception.code, "expired")
        self.assertEqual(marked, ["chatgpt"])

    def test_extract_text_empty(self):
        self.assertEqual(accounts_chatgpt._extract_text([]), "")
        self.assertEqual(
            accounts_chatgpt._extract_text([{"type": "response.created"}]), ""
        )


if __name__ == "__main__":
    unittest.main()
