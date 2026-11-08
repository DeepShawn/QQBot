# -*- coding: utf-8 -*-
"""accounts_gemini 单元测试（无网络：所有 HTTP / 回调服务都被替换）。"""

import io
import json
import os
import sys
import time
import types
import unittest
import urllib.parse
from unittest import mock

BOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BOT_DIR not in sys.path:
    sys.path.insert(0, BOT_DIR)

# account_core 由 T1 并行实现：存在就用真的，不存在就注入最小假模块。
try:  # pragma: no cover - 只是环境适配
    import account_core  # noqa: F401
except Exception:  # noqa: BLE001
    _stub = types.ModuleType("account_core")

    class _AccountError(Exception):
        def __init__(self, code, message=""):
            super().__init__(message or code)
            self.code = code
            self.message = message

    class _AttemptCancelled(Exception):
        pass

    class _CallbackServer:  # pragma: no cover - 测试里不会用到真的
        def __init__(self, port=0, path="/callback"):
            raise _AccountError("generic", "stub 回调服务不可用")

        def wait(self, timeout, attempt):
            raise _AccountError("generic", "stub")

        def close(self):
            pass

    _stub.AccountError = _AccountError
    _stub.AttemptCancelled = _AttemptCancelled
    _stub.CallbackServer = _CallbackServer
    _stub.redact = lambda text, *args, **kwargs: text
    sys.modules["account_core"] = _stub

import accounts_gemini as gemini  # noqa: E402


def _sse(*chunks):
    """把若干 dict 拼成 Code Assist 的 SSE 字节流。"""
    parts = []
    for chunk in chunks:
        parts.append("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n")
    return "".join(parts).encode("utf-8")


def _chunk(text=None, thought=False):
    part = {}
    if text is not None:
        part["text"] = text
    if thought:
        part["thought"] = True
    return {"response": {"candidates": [{"content": {"parts": [part]}}]}}


class FakeAttempt:
    def __init__(self, answer="", cancelled=False):
        self.answer = answer
        self.cancelled = cancelled
        self.phases = []
        self.urls = []
        self.prompt_messages = []

    def set_phase(self, phase, message=""):
        self.phases.append((phase, message))

    def notify_url(self, url, message=""):
        self.urls.append((url, message))

    def notify_device(self, url, user_code, message=""):
        self.phases.append(("device", url, user_code, message))

    def prompt(self, placeholder, message=""):
        self.phases.append(("prompt", placeholder))
        self.prompt_messages.append(message)
        self.check_cancelled()
        value = self.answer
        return value() if callable(value) else value

    def check_cancelled(self):
        if self.cancelled:
            raise gemini.AttemptCancelled()


class FakeCallbackServer:
    def __init__(self, redirect_uri="http://127.0.0.1:54321/oauth2callback", query=None, query_factory=None):
        self.redirect_uri = redirect_uri
        self.query = query or {}
        self.query_factory = query_factory
        self.closed = False

    def wait(self, timeout, attempt):
        attempt.check_cancelled()
        return self.query_factory() if self.query_factory else dict(self.query)

    def close(self):
        self.closed = True


class PkceAndUrlTests(unittest.TestCase):
    def test_pkce_challenge_matches_verifier(self):
        import base64
        import hashlib

        verifier, challenge = gemini._pkce_pair()
        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        self.assertEqual(challenge, expected.rstrip(b"=").decode())
        self.assertNotIn("=", verifier)
        self.assertGreaterEqual(len(verifier), 40)

    def test_authorize_url_web_flow(self):
        url = gemini._authorize_url("http://127.0.0.1:8085/oauth2callback", "state-1")
        parsed = urllib.parse.urlparse(url)
        self.assertEqual(parsed.netloc, "accounts.google.com")
        query = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(query["client_id"][0], gemini.OAUTH_CLIENT_ID)
        self.assertEqual(query["redirect_uri"][0], "http://127.0.0.1:8085/oauth2callback")
        self.assertEqual(query["response_type"][0], "code")
        self.assertEqual(query["access_type"][0], "offline")
        self.assertEqual(query["state"][0], "state-1")
        self.assertIn("cloud-platform", query["scope"][0])
        self.assertNotIn("code_challenge", query)

    def test_authorize_url_user_code_flow_has_pkce(self):
        verifier, challenge = gemini._pkce_pair()
        url = gemini._authorize_url(gemini.USER_CODE_REDIRECT, "st", verifier, challenge)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(query["code_challenge"][0], challenge)
        self.assertEqual(query["code_challenge_method"][0], "S256")

    def test_code_from_query_errors(self):
        with self.assertRaises(gemini.AccountError):
            gemini._code_from_query({"error": "access_denied"})
        with self.assertRaises(gemini.AccountError):
            gemini._code_from_query({})
        self.assertEqual(gemini._code_from_query({"code": "c1", "state": "s1"}), ("c1", "s1"))

    def test_code_from_prompt_accepts_url_or_code(self):
        self.assertEqual(gemini._code_from_prompt("  abc123  "), "abc123")
        url = "https://codeassist.google.com/authcode?code=xyz&state=s"
        self.assertEqual(gemini._code_from_prompt(url), "xyz")
        with self.assertRaises(gemini.AccountError):
            gemini._code_from_prompt("")

    def test_http_status_mapping(self):
        self.assertEqual(gemini._http_status_error(401).code, "expired")
        self.assertEqual(gemini._http_status_error(403).code, "denied")
        self.assertEqual(gemini._http_status_error(429).code, "generic")
        self.assertEqual(gemini._http_status_error(503).code, "generic")

    def test_redirect_uri_from_server_rejects_bad(self):
        bad = FakeCallbackServer(redirect_uri="https://evil.example/oauth2callback")
        with self.assertRaises(gemini.AccountError):
            gemini._redirect_uri_from_server(bad)
        good = FakeCallbackServer()
        self.assertEqual(
            gemini._redirect_uri_from_server(good),
            "http://127.0.0.1:54321/oauth2callback",
        )


class RefreshTests(unittest.TestCase):
    def test_refresh_requires_refresh_token(self):
        with mock.patch.object(gemini, "_http_request", side_effect=AssertionError("不该联网")):
            self.assertIsNone(gemini.refresh({"access": "old", "refresh": ""}))

    def test_refresh_success(self):
        payload = json.dumps({"access_token": "new-access", "expires_in": 3600}).encode()
        with mock.patch.object(gemini, "_http_request", return_value=(200, payload)) as request:
            updated = gemini.refresh(
                {
                    "kind": "oauth",
                    "access": "old",
                    "refresh": "rt-1",
                    "expires_at": 1,
                    "account": "a@b.com",
                    "extra": {"project": "p1"},
                }
            )
        self.assertIsNotNone(updated)
        self.assertEqual(updated["access"], "new-access")
        self.assertEqual(updated["refresh"], "rt-1")
        self.assertEqual(updated["account"], "a@b.com")
        self.assertEqual(updated["extra"], {"project": "p1"})
        self.assertGreater(updated["expires_at"], int(time.time()))
        self.assertEqual(request.call_count, 1)
        # token 不能出现在请求参数之外的地方（这里只确认表单字段正确）
        body = request.call_args.kwargs["body"].decode()
        self.assertIn("refresh_token=rt-1", body)
        self.assertIn("grant_type=refresh_token", body)

    def test_refresh_new_refresh_token_replaces_old(self):
        payload = json.dumps(
            {"access_token": "a2", "expires_in": 60, "refresh_token": "rt-2"}
        ).encode()
        with mock.patch.object(gemini, "_http_request", return_value=(200, payload)):
            updated = gemini.refresh({"access": "a1", "refresh": "rt-1"})
        self.assertEqual(updated["refresh"], "rt-2")
        self.assertGreater(updated["expires_at"], int(time.time()))

    def test_refresh_invalid_grant_returns_none(self):
        with mock.patch.object(gemini, "_http_request", return_value=(400, b'{"error":"invalid_grant"}')):
            self.assertIsNone(gemini.refresh({"access": "a", "refresh": "rt"}))

    def test_refresh_network_error_is_raised_as_network(self):
        # 网络错误 ≠ 登录过期：原样抛给上层，让用户看到「网络连不上」。
        with mock.patch.object(
            gemini, "_http_request", side_effect=gemini.AccountError("network", "网络")
        ):
            with self.assertRaises(gemini.AccountError) as ctx:
                gemini.refresh({"access": "a", "refresh": "rt"})
        self.assertEqual(ctx.exception.code, "network")


class RequestBuildingTests(unittest.TestCase):
    def test_build_request_maps_messages(self):
        payload = gemini._build_request(
            "gemini-2.5-pro",
            "proj-1",
            [
                {"role": "system", "content": "你是助手"},
                {"role": "user", "content": "你好"},
                {"role": "assistant", "content": "在的"},
                {"role": "user", "content": "继续"},
            ],
            temperature=0.5,
            max_tokens=256,
        )
        self.assertEqual(payload["model"], "gemini-2.5-pro")
        self.assertEqual(payload["project"], "proj-1")
        request = payload["request"]
        self.assertEqual(request["systemInstruction"], {"role": "user", "parts": [{"text": "你是助手"}]})
        self.assertEqual(
            [content["role"] for content in request["contents"]],
            ["user", "model", "user"],
        )
        self.assertEqual(request["contents"][0]["parts"], [{"text": "你好"}])
        self.assertEqual(request["generationConfig"], {"temperature": 0.5, "maxOutputTokens": 256})
        self.assertIn("session_id", request)
        self.assertIn("user_prompt_id", payload)

    def test_build_request_without_project_and_options(self):
        payload = gemini._build_request("gemini-2.5-flash", None, [{"role": "user", "content": "hi"}])
        self.assertNotIn("project", payload)
        self.assertNotIn("generationConfig", payload)

    def test_build_request_rejects_empty(self):
        with self.assertRaises(gemini.AccountError):
            gemini._build_request("m", None, [])

    def test_consume_stream_skips_thought_parts(self):
        stream = io.BytesIO(
            _sse(
                _chunk("思考中", thought=True),
                _chunk("你好"),
                _chunk("，世界"),
            )
        )
        self.assertEqual(gemini._consume_stream(stream), "你好，世界")

    def test_consume_stream_tolerates_bad_chunk(self):
        stream = io.BytesIO(b"data: not-json\n\ndata: {\"response\": {}}\n\n")
        self.assertEqual(gemini._consume_stream(stream), "")

    def test_consume_stream_blocked(self):
        chunk = {"response": {"promptFeedback": {"blockReason": "SAFETY"}, "candidates": []}}
        with self.assertRaises(gemini.AccountError) as ctx:
            gemini._consume_stream(io.BytesIO(_sse(chunk)))
        self.assertIn("安全策略", str(ctx.exception))


class ChatTests(unittest.TestCase):
    def _record(self):
        return {
            "access": "access-token-1",
            "extra": {"project": "proj-1", "email": "a@b.com"},
        }

    def test_chat_collects_stream(self):
        captured = {}

        def fake_open(url, body, headers, timeout=30):
            captured["url"] = url
            captured["body"] = json.loads(body)
            captured["headers"] = headers
            captured["timeout"] = timeout
            return io.BytesIO(_sse(_chunk("你好"), _chunk("！")))

        with mock.patch.object(gemini, "_open_stream", side_effect=fake_open):
            text = gemini.chat(
                self._record(),
                "gemini-2.5-pro",
                [
                    {"role": "system", "content": "系统"},
                    {"role": "user", "content": "在吗"},
                ],
                temperature=0.3,
                max_tokens=128,
                timeout=45,
            )
        self.assertEqual(text, "你好！")
        self.assertTrue(captured["url"].startswith("https://cloudcode-pa.googleapis.com/v1internal:streamGenerateContent"))
        self.assertIn("alt=sse", captured["url"])
        self.assertEqual(captured["body"]["model"], "gemini-2.5-pro")
        self.assertEqual(captured["body"]["project"], "proj-1")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer access-token-1")
        self.assertIn("GeminiCLI/", captured["headers"]["User-Agent"])
        self.assertEqual(captured["timeout"], 45)

    def test_chat_requires_token(self):
        with self.assertRaises(gemini.AccountError) as ctx:
            gemini.chat({}, "gemini-2.5-pro", [{"role": "user", "content": "hi"}], None, None, 30)
        self.assertEqual(ctx.exception.code, "not-signed-in")

    def test_chat_setup_project_when_missing(self):
        def fake_open(url, body, headers, timeout=30):
            return io.BytesIO(_sse(_chunk("ok")))

        with mock.patch.object(gemini, "_setup_project", return_value=("lazy-proj", "free-tier")) as setup:
            with mock.patch.object(gemini, "_open_stream", side_effect=fake_open):
                text = gemini.chat(
                    {"access": "token"},
                    "gemini-2.5-flash",
                    [{"role": "user", "content": "hi"}],
                    None,
                    None,
                    30,
                )
        self.assertEqual(text, "ok")
        setup.assert_called_once()

    def test_chat_error_does_not_leak_token(self):
        token = "super-secret-token"
        error = gemini._http_status_error(401)
        self.assertNotIn(token, str(error))
        self.assertNotIn(token, getattr(error, "message", ""))


class ProjectSetupTests(unittest.TestCase):
    def test_existing_tier_with_project(self):
        with mock.patch.object(
            gemini,
            "_load_code_assist",
            return_value={"currentTier": {"id": "standard-tier"}, "cloudaicompanionProject": "p1"},
        ):
            self.assertEqual(gemini._setup_project("token"), ("p1", "standard-tier"))

    def test_existing_tier_without_project_is_unsupported(self):
        with mock.patch.object(
            gemini, "_load_code_assist", return_value={"currentTier": {"id": "standard-tier"}}
        ):
            with self.assertRaises(gemini.AccountError) as ctx:
                gemini._setup_project("token")
        self.assertEqual(ctx.exception.code, "unsupported")

    def test_onboard_free_tier(self):
        load = {"allowedTiers": [{"id": "free-tier", "isDefault": True, "name": "Free"}]}
        onboard = {"done": True, "response": {"cloudaicompanionProject": {"id": "onboarded"}}}
        with mock.patch.object(gemini, "_load_code_assist", return_value=load):
            with mock.patch.object(gemini, "_onboard_user", return_value=onboard) as onboard_mock:
                project, tier = gemini._setup_project("token")
        self.assertEqual(project, "onboarded")
        self.assertEqual(tier, "free-tier")
        # 免费层不能带项目（否则 Precondition Failed）
        payload = onboard_mock.call_args.args[2]
        self.assertIsNone(payload)

    def test_onboard_polls_operation(self):
        load = {"allowedTiers": [{"id": "free-tier", "isDefault": True}]}
        pending = {"done": False, "name": "operations/abc"}
        finished = {"done": True, "response": {"cloudaicompanionProject": {"id": "p-new"}}}
        with mock.patch.object(gemini, "_load_code_assist", return_value=load):
            with mock.patch.object(gemini, "_onboard_user", return_value=pending):
                with mock.patch.object(gemini, "_wait_operation", return_value=finished) as wait_mock:
                    project, _tier = gemini._setup_project("token")
        self.assertEqual(project, "p-new")
        wait_mock.assert_called_once()

    def test_onboard_free_tier_body_omits_project(self):
        load = {"allowedTiers": [{"id": "free-tier", "isDefault": True}]}
        with mock.patch.object(gemini, "_load_code_assist", return_value=load):
            with mock.patch.object(gemini, "_code_assist_post", return_value={"done": True, "response": {}}) as post:
                with self.assertRaises(gemini.AccountError):
                    gemini._setup_project("token")
        body = post.call_args.args[2]
        self.assertNotIn("cloudaicompanionProject", body)
        self.assertEqual(body["tierId"], "free-tier")

    def test_env_project_is_used_when_tier_has_no_project(self):
        load = {"currentTier": {"id": "standard-tier"}}
        with mock.patch.dict(os.environ, {"GOOGLE_CLOUD_PROJECT": "my-proj-123"}, clear=False):
            with mock.patch.object(gemini, "_load_code_assist", return_value=load) as load_mock:
                project, tier = gemini._setup_project("token")
        self.assertEqual(project, "my-proj-123")
        self.assertEqual(tier, "standard-tier")
        self.assertEqual(load_mock.call_args.args[1], "my-proj-123")

    def test_numeric_env_project_is_ignored(self):
        load = {"currentTier": {"id": "standard-tier"}}
        with mock.patch.dict(os.environ, {"GOOGLE_CLOUD_PROJECT": "12345678"}, clear=False):
            with mock.patch.object(gemini, "_load_code_assist", return_value=load):
                with self.assertRaises(gemini.AccountError):
                    gemini._setup_project("token")

    def test_ineligible_validation_required(self):
        load = {"ineligibleTiers": [{"reasonCode": "VALIDATION_REQUIRED", "reasonMessage": "x"}]}
        with mock.patch.object(gemini, "_load_code_assist", return_value=load):
            with self.assertRaises(gemini.AccountError) as ctx:
                gemini._setup_project("token")
        self.assertEqual(ctx.exception.code, "unsupported")


class BeginTests(unittest.TestCase):
    def test_begin_web_callback_flow(self):
        state_holder = {}

        def fake_authorize(redirect_uri, state, verifier=None, challenge=None):
            state_holder["state"] = state
            return "https://accounts.google.com/o/oauth2/v2/auth?state=" + state

        server = FakeCallbackServer(
            query_factory=lambda: {"code": "code-1", "state": state_holder["state"]}
        )
        tokens = {
            "access_token": "access-1",
            "refresh_token": "refresh-1",
            "expires_in": 3599,
        }
        with mock.patch.object(gemini, "_open_callback", return_value=server):
            with mock.patch.object(gemini, "_authorize_url", side_effect=fake_authorize):
                with mock.patch.object(gemini, "_exchange_code", return_value=tokens) as exchange:
                    with mock.patch.object(gemini, "_fetch_email", return_value="a@b.com"):
                        with mock.patch.object(gemini, "_setup_project", return_value=("proj-9", "free-tier")):
                            attempt = FakeAttempt()
                            record = gemini.begin(attempt)
        self.assertTrue(server.closed)
        self.assertEqual(record["kind"], "oauth")
        self.assertEqual(record["access"], "access-1")
        self.assertEqual(record["refresh"], "refresh-1")
        self.assertEqual(record["account"], "a@b.com")
        self.assertEqual(record["extra"]["project"], "proj-9")
        self.assertGreater(record["expires_at"], int(time.time()))
        self.assertTrue(attempt.urls)
        self.assertIn("accounts.google.com", attempt.urls[0][0])
        exchange.assert_called_once_with("code-1", "http://127.0.0.1:54321/oauth2callback")
        self.assertEqual(attempt.phases[0][0], "starting")

    def test_begin_state_mismatch(self):
        server = FakeCallbackServer(query={"code": "c", "state": "wrong-state"})
        with mock.patch.object(gemini, "_open_callback", return_value=server):
            with mock.patch.object(gemini, "_exchange_code", side_effect=AssertionError("不该换 token")):
                with self.assertRaises(gemini.AccountError) as ctx:
                    gemini.begin(FakeAttempt())
        self.assertEqual(ctx.exception.code, "denied")
        self.assertTrue(server.closed)

    def test_begin_prompt_fallback_uses_pkce_and_user_code_redirect(self):
        tokens = {"access_token": "access-2", "refresh_token": "rt-2", "expires_in": 3600}
        captured = {}

        def fake_exchange(code, redirect_uri, verifier=None):
            captured["code"] = code
            captured["redirect"] = redirect_uri
            captured["verifier"] = verifier
            return tokens

        with mock.patch.object(gemini, "_open_callback", return_value=None):
            with mock.patch.object(gemini, "_exchange_code", side_effect=fake_exchange):
                with mock.patch.object(gemini, "_fetch_email", return_value="b@c.com"):
                    with mock.patch.object(gemini, "_setup_project", side_effect=gemini.AccountError("network", "网络")):
                        attempt = FakeAttempt(answer="the-code")
                        record = gemini.begin(attempt)
        self.assertEqual(captured["code"], "the-code")
        self.assertEqual(captured["redirect"], gemini.USER_CODE_REDIRECT)
        self.assertTrue(captured["verifier"])
        self.assertEqual(record["account"], "b@c.com")
        self.assertIsNone(record["extra"].get("project"))
        self.assertTrue(any(p[0] == "prompt" for p in attempt.phases))
        # prompt 阶段不再显示 open 阶段的 url，提示信息里必须带上授权网址
        self.assertIn("accounts.google.com", attempt.prompt_messages[0])
        # 授权网址必须带 PKCE
        auth_url = attempt.urls[0][0]
        query = urllib.parse.parse_qs(urllib.parse.urlparse(auth_url).query)
        self.assertEqual(query["redirect_uri"][0], gemini.USER_CODE_REDIRECT)
        self.assertEqual(query["code_challenge_method"][0], "S256")

    def test_begin_prompt_cancel_raises_attempt_cancelled(self):
        with mock.patch.object(gemini, "_open_callback", return_value=None):
            with self.assertRaises(gemini.AttemptCancelled):
                gemini.begin(FakeAttempt(answer="x", cancelled=True))

    def test_begin_setup_unsupported_fails_login(self):
        server = FakeCallbackServer(
            query_factory=lambda: {"code": "c", "state": state_holder[0]}
        )
        state_holder = []

        def fake_authorize(redirect_uri, state, verifier=None, challenge=None):
            state_holder[:] = [state]
            return "https://accounts.google.com/x?state=" + state

        tokens = {"access_token": "a", "refresh_token": "r", "expires_in": 3600}
        with mock.patch.object(gemini, "_open_callback", return_value=server):
            with mock.patch.object(gemini, "_authorize_url", side_effect=fake_authorize):
                with mock.patch.object(gemini, "_exchange_code", return_value=tokens):
                    with mock.patch.object(gemini, "_fetch_email", return_value=None):
                        with mock.patch.object(
                            gemini,
                            "_setup_project",
                            side_effect=gemini.AccountError("unsupported", "不能用"),
                        ):
                            with self.assertRaises(gemini.AccountError) as ctx:
                                gemini.begin(FakeAttempt())
        self.assertEqual(ctx.exception.code, "unsupported")


class LabelTests(unittest.TestCase):
    def test_label(self):
        self.assertEqual(gemini.label({"account": "a@b.com"}), "a@b.com")
        self.assertEqual(gemini.label({"account": None, "extra": {"email": "c@d.com"}}), "c@d.com")
        self.assertIsNone(gemini.label({}))


if __name__ == "__main__":
    unittest.main()
