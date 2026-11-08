# -*- coding: utf-8 -*-
"""accounts_deepseek 单元测试（无网络：所有 HTTP / 回调服务都被替换）。"""

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

import accounts_deepseek as deepseek  # noqa: E402


def envelope(biz_data):
    return json.dumps({"code": 0, "data": {"biz_code": 0, "biz_data": biz_data}}).encode()


def sse(*events):
    """events: (event_name, data_dict) 或 data_dict。"""
    parts = []
    for event in events:
        if isinstance(event, tuple):
            name, data = event
            parts.append("event: %s\n" % name)
        else:
            data = event
        parts.append("data: " + json.dumps(data, ensure_ascii=False) + "\n\n")
    return "".join(parts).encode("utf-8")


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
            raise deepseek.AttemptCancelled()


class FakeCallbackServer:
    def __init__(self, redirect_uri="http://127.0.0.1:54321/oauth/callback", query=None, query_factory=None):
        self.redirect_uri = redirect_uri
        self.query = query or {}
        self.query_factory = query_factory
        self.closed = False

    def wait(self, timeout, attempt):
        attempt.check_cancelled()
        return self.query_factory() if self.query_factory else dict(self.query)

    def close(self):
        self.closed = True


class ProtocolTests(unittest.TestCase):
    def test_unwrap_valid(self):
        self.assertEqual(deepseek._unwrap(json.loads(envelope({"token": "t"}))), {"token": "t"})

    def test_unwrap_rejection_codes(self):
        with self.assertRaises(deepseek.AccountError) as ctx:
            deepseek._unwrap({"code": 40003})
        self.assertEqual(ctx.exception.code, "expired")
        with self.assertRaises(deepseek.AccountError):
            deepseek._unwrap({"code": 1})
        with self.assertRaises(deepseek.AccountError):
            deepseek._unwrap({"code": 0, "data": {"biz_code": 2, "biz_data": {}}})

    def test_browser_url_validation(self):
        good = "https://platform.deepseek.com/dsh/authorize?x=1"
        self.assertEqual(deepseek._browser_url(good, "/dsh/authorize"), good)
        for bad in (
            "http://platform.deepseek.com/dsh/authorize",
            "https://platform.deepseek.com/dsh/authorized",
            "https://platform.deepseek.com/dsh/authorize#frag",
            "https://user:pass@platform.deepseek.com/dsh/authorize",
            "",
            None,
        ):
            with self.assertRaises(deepseek.AccountError):
                deepseek._browser_url(bad, "/dsh/authorize")

    def test_token_validation(self):
        self.assertEqual(deepseek._token_from_result({"token": "tok_123"}), "tok_123")
        with self.assertRaises(deepseek.AccountError):
            deepseek._token_from_result({})
        with self.assertRaises(deepseek.AccountError):
            deepseek._token_from_result({"token": "tok\n123"})

    def test_pkce(self):
        import base64
        import hashlib

        verifier, challenge = deepseek._pkce_pair()
        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        self.assertEqual(challenge, expected.rstrip(b"=").decode())

    def test_code_from_prompt(self):
        self.assertEqual(deepseek._code_from_prompt("bare-code"), ("bare-code", ""))
        url = "http://127.0.0.1:8086/oauth/callback?code=abc&state=st"
        self.assertEqual(deepseek._code_from_prompt(url), ("abc", "st"))
        with self.assertRaises(deepseek.AccountError):
            deepseek._code_from_prompt("")

    def test_inference_error_mapping(self):
        self.assertEqual(deepseek._inference_error(401).code, "expired")
        self.assertEqual(deepseek._inference_error(403).code, "expired")
        self.assertEqual(deepseek._inference_error(429).code, "generic")
        self.assertEqual(deepseek._inference_error(500).code, "generic")


class PlatformHeaderTests(unittest.TestCase):
    """platform.deepseek.com 的 WAF 会拦非浏览器 UA（429 Request Blocked）。"""

    def test_client_headers_look_like_browser(self):
        headers = deepseek._client_headers()
        self.assertEqual(headers["user-agent"], deepseek.BROWSER_USER_AGENT)
        self.assertIn("Chrome/", headers["user-agent"])
        self.assertIn("Mobile Safari", headers["user-agent"])
        self.assertEqual(headers["accept"], "application/json, text/plain, */*")
        self.assertEqual(headers["accept-language"], "zh-CN,zh;q=0.9")
        # 参考实现的 x-client-* 身份头保留
        self.assertEqual(headers["x-client-platform"], "web")
        self.assertEqual(headers["x-client-version"], deepseek.CLIENT_VERSION)
        self.assertIn("x-client-timezone-offset", headers)

    def test_platform_post_sends_browser_headers_origin_and_referer(self):
        captured = {}

        def fake_http(url, method="GET", headers=None, body=None, timeout=30):
            captured["url"] = url
            captured["method"] = method
            captured["headers"] = dict(headers or {})
            captured["body"] = body
            return 200, envelope(
                {"authorize_url": "https://platform.deepseek.com/dsh/authorize?x=1", "authorize_id": "a", "expires_in": 600}
            )

        with mock.patch.object(deepseek, "_http_request", side_effect=fake_http):
            result = deepseek._platform_post("auth_init", {"state": "s"})
        self.assertEqual(result["authorize_id"], "a")
        self.assertTrue(captured["url"].endswith("/auth-api/v0/dsh/auth_init"))
        headers = captured["headers"]
        self.assertEqual(headers["origin"], deepseek.PLATFORM_ORIGIN)
        self.assertEqual(headers["referer"], deepseek.PLATFORM_ORIGIN + "/")
        self.assertEqual(headers["user-agent"], deepseek.BROWSER_USER_AGENT)
        self.assertEqual(headers["accept"], deepseek.BROWSER_ACCEPT)
        self.assertEqual(headers["accept-language"], deepseek.BROWSER_ACCEPT_LANGUAGE)
        self.assertEqual(headers["content-type"], "application/json")

    def test_platform_get_sends_browser_headers_origin_and_referer(self):
        captured = {}

        def fake_http(url, method="GET", headers=None, body=None, timeout=30):
            captured["headers"] = dict(headers or {})
            return 200, envelope({"id": "u1", "email": "a@b.com"})

        with mock.patch.object(deepseek, "_http_request", side_effect=fake_http):
            profile = deepseek._fetch_profile("tok-1")
        self.assertEqual(profile["email"], "a@b.com")
        headers = captured["headers"]
        self.assertEqual(headers[deepseek.TOKEN_HEADER], "tok-1")
        self.assertEqual(headers["origin"], deepseek.PLATFORM_ORIGIN)
        self.assertEqual(headers["referer"], deepseek.PLATFORM_ORIGIN + "/")
        self.assertEqual(headers["user-agent"], deepseek.BROWSER_USER_AGENT)

    def test_inference_headers_unchanged(self):
        # api.deepseek.com 推理接口没有 WAF 问题，保持参考实现的 deepseek-harness UA。
        headers = deepseek._messages_headers("tok", "u1")
        self.assertTrue(headers["user-agent"].startswith("deepseek-harness/"))
        self.assertNotEqual(headers["user-agent"], deepseek.BROWSER_USER_AGENT)
        self.assertNotIn("origin", headers)
        self.assertNotIn("referer", headers)

    def test_429_messages(self):
        platform_error = deepseek._platform_error(429)
        self.assertEqual(platform_error.code, "generic")
        self.assertIn("风控", str(platform_error))
        inference_error = deepseek._inference_error(429)
        self.assertIn("太频繁", str(inference_error))


class BeginTests(unittest.TestCase):
    def _router(self, calls, exchange_user=True):
        def route(url, method="GET", headers=None, body=None, timeout=30):
            calls.setdefault("requests", []).append(
                {"url": url, "method": method, "headers": dict(headers or {}), "body": body}
            )
            if url.endswith("/auth-api/v0/dsh/auth_init"):
                calls["init"] = json.loads(body)
                return (
                    200,
                    envelope(
                        {
                            "authorize_url": "https://platform.deepseek.com/dsh/authorize?state=x",
                            "authorize_id": "authorize-1",
                            "expires_in": 600,
                        }
                    ),
                )
            if url.endswith("/auth-api/v0/dsh/auth_exchange"):
                calls["exchange"] = json.loads(body)
                return (
                    200,
                    envelope(
                        {
                            "token": "tok-ok",
                            "authorized_url": "https://platform.deepseek.com/dsh/authorized",
                            "user": {
                                "id": "u-1",
                                "email": "a@b.com",
                                "id_profile": {"name": "小明"},
                            }
                            if exchange_user
                            else None,
                        }
                    ),
                )
            if url.endswith("/auth-api/v0/users/current"):
                return 200, envelope({"id": "u-1", "email": "fallback@b.com", "id_profile": {}})
            if url.endswith("/models"):
                return 200, json.dumps(
                    {"data": [{"id": "deepseek-flash"}, {"id": "deepseek-v4-pro"}, {"id": "deepseek-flash"}]}
                ).encode()
            raise AssertionError("意外的请求：" + url)

        return route

    def test_begin_web_callback_flow(self):
        calls = {}
        server = FakeCallbackServer(
            query_factory=lambda: {
                "code": "code-1",
                "state": calls["init"]["state"],
            }
        )
        with mock.patch.object(deepseek, "_open_callback", return_value=server):
            with mock.patch.object(deepseek, "_http_request", side_effect=self._router(calls)):
                attempt = FakeAttempt()
                record = deepseek.begin(attempt)

        self.assertTrue(server.closed)
        self.assertEqual(record["kind"], "oauth")
        self.assertEqual(record["access"], "tok-ok")
        self.assertEqual(record["refresh"], "")
        self.assertEqual(record["account"], "a@b.com")
        self.assertEqual(record["extra"]["origin"], deepseek.PLATFORM_ORIGIN)
        self.assertEqual(record["extra"]["email"], "a@b.com")
        self.assertEqual(record["extra"]["name"], "小明")
        self.assertEqual(record["extra"]["models"], ["deepseek-flash", "deepseek-v4-pro"])
        # 平台 grant 没有过期时间：按 account_core 约定记 0（长期有效）
        self.assertEqual(record["expires_at"], 0)
        self.assertTrue(attempt.urls)
        self.assertIn("/dsh/authorize", attempt.urls[0][0])

        init = calls["init"]
        self.assertEqual(init["code_challenge_method"], "S256")
        self.assertEqual(init["redirect_uri"], "http://127.0.0.1:54321/oauth/callback")
        self.assertEqual(init["locale"], "zh_CN")
        self.assertEqual(init["login_source"], "web")
        self.assertTrue(init["code_challenge"])
        self.assertTrue(init["state"])

        exchange = calls["exchange"]
        self.assertEqual(exchange["code"], "code-1")
        self.assertEqual(exchange["redirect_uri"], init["redirect_uri"])
        self.assertTrue(exchange["device_id"])
        self.assertTrue(exchange["device_model"])
        self.assertIn("os_version", exchange)
        # code_verifier 的 sha256 必须等于 auth_init 里的 code_challenge
        import base64
        import hashlib

        expected = base64.urlsafe_b64encode(
            hashlib.sha256(exchange["code_verifier"].encode()).digest()
        ).rstrip(b"=").decode()
        self.assertEqual(expected, init["code_challenge"])

        # 平台请求必须带 x-client-* 身份头，且装成浏览器（WAF 只放行浏览器请求）
        init_request = calls["requests"][0]
        headers = init_request["headers"]
        self.assertEqual(headers["x-client-platform"], "web")
        self.assertEqual(headers["x-client-version"], deepseek.CLIENT_VERSION)
        self.assertIn("x-client-timezone-offset", headers)
        self.assertEqual(headers["content-type"], "application/json")
        self.assertEqual(headers["user-agent"], deepseek.BROWSER_USER_AGENT)
        self.assertEqual(headers["accept"], deepseek.BROWSER_ACCEPT)
        self.assertEqual(headers["accept-language"], deepseek.BROWSER_ACCEPT_LANGUAGE)
        self.assertEqual(headers["origin"], deepseek.PLATFORM_ORIGIN)
        self.assertEqual(headers["referer"], deepseek.PLATFORM_ORIGIN + "/")

    def test_begin_state_mismatch(self):
        calls = {}
        server = FakeCallbackServer(query={"code": "code-1", "state": "wrong"})
        with mock.patch.object(deepseek, "_open_callback", return_value=server):
            with mock.patch.object(deepseek, "_http_request", side_effect=self._router(calls)):
                with self.assertRaises(deepseek.AccountError) as ctx:
                    deepseek.begin(FakeAttempt())
        self.assertEqual(ctx.exception.code, "denied")
        self.assertNotIn("exchange", calls)
        self.assertTrue(server.closed)

    def test_begin_prompt_fallback_pastes_url(self):
        calls = {}
        attempt = FakeAttempt(
            answer=lambda: "http://127.0.0.1:8086/oauth/callback?code=code-2&state="
            + calls["init"]["state"]
        )
        with mock.patch.object(deepseek, "_open_callback", return_value=None):
            with mock.patch.object(deepseek, "_http_request", side_effect=self._router(calls)):
                record = deepseek.begin(attempt)
        self.assertEqual(calls["init"]["redirect_uri"], deepseek.FALLBACK_REDIRECT)
        self.assertEqual(calls["exchange"]["code"], "code-2")
        self.assertEqual(record["access"], "tok-ok")
        self.assertTrue(any(phase[0] == "prompt" for phase in attempt.phases))
        # prompt 阶段界面不显示 open 阶段的 url，提示信息里必须带上授权网址
        self.assertIn("/dsh/authorize", attempt.prompt_messages[0])

    def test_begin_prompt_cancel(self):
        with mock.patch.object(deepseek, "_open_callback", return_value=None):
            with self.assertRaises(deepseek.AttemptCancelled):
                deepseek.begin(FakeAttempt(answer="x", cancelled=True))

    def test_begin_fetches_profile_when_missing_from_exchange(self):
        calls = {}
        server = FakeCallbackServer(
            query_factory=lambda: {"code": "c", "state": calls["init"]["state"]}
        )
        with mock.patch.object(deepseek, "_open_callback", return_value=server):
            with mock.patch.object(
                deepseek, "_http_request", side_effect=self._router(calls, exchange_user=False)
            ):
                record = deepseek.begin(FakeAttempt())
        self.assertEqual(record["access"], "tok-ok")
        self.assertEqual(record["account"], "fallback@b.com")

    def test_begin_rejects_bad_authorize_url(self):
        def route(url, method="GET", headers=None, body=None, timeout=30):
            return (
                200,
                envelope(
                    {
                        "authorize_url": "https://evil.example/dsh/authorize",
                        "authorize_id": "a",
                        "expires_in": 600,
                    }
                ),
            )

        with mock.patch.object(
            deepseek, "_open_callback", return_value=FakeCallbackServer(query={"code": "c", "state": "s"})
        ):
            with mock.patch.object(deepseek, "_http_request", side_effect=route):
                with self.assertRaises(deepseek.AccountError):
                    deepseek.begin(FakeAttempt())


class RefreshTests(unittest.TestCase):
    def _record(self):
        return {
            "kind": "oauth",
            "access": "tok",
            "refresh": "",
            "expires_at": 1,
            "account": "old@b.com",
            "extra": {"origin": deepseek.PLATFORM_ORIGIN, "email": "old@b.com"},
        }

    def test_refresh_extends_when_platform_accepts(self):
        with mock.patch.object(
            deepseek, "_fetch_profile", return_value={"email": "new@b.com", "user_id": "u1"}
        ):
            updated = deepseek.refresh(self._record())
        self.assertIsNotNone(updated)
        self.assertEqual(updated["expires_at"], 1)
        self.assertEqual(updated["account"], "new@b.com")
        self.assertEqual(updated["extra"]["email"], "new@b.com")
        self.assertEqual(updated["access"], "tok")

    def test_refresh_returns_none_when_rejected(self):
        with mock.patch.object(
            deepseek, "_fetch_profile", side_effect=deepseek.AccountError("expired", "过期")
        ):
            self.assertIsNone(deepseek.refresh(self._record()))

    def test_refresh_extends_when_network_down(self):
        with mock.patch.object(
            deepseek, "_fetch_profile", side_effect=deepseek.AccountError("network", "网络")
        ):
            updated = deepseek.refresh(self._record())
        self.assertIsNotNone(updated)
        self.assertEqual(updated["expires_at"], 1)

    def test_refresh_without_token(self):
        self.assertIsNone(deepseek.refresh({"access": ""}))

    def test_platform_get_maps_401(self):
        with mock.patch.object(deepseek, "_http_request", return_value=(401, b"{}")):
            with self.assertRaises(deepseek.AccountError) as ctx:
                deepseek._platform_get("/auth-api/v0/users/current", "tok")
        self.assertEqual(ctx.exception.code, "expired")


class ModelListingTests(unittest.TestCase):
    def test_list_models_parses_and_dedupes(self):
        body = json.dumps(
            {"data": [{"id": "deepseek-flash"}, {"id": "deepseek-v4-pro"}, {"id": "deepseek-flash"}, {"id": 1}]}
        ).encode()
        with mock.patch.object(deepseek, "_http_request", return_value=(200, body)) as request:
            models = deepseek._list_models("tok-1")
        self.assertEqual(models, ["deepseek-flash", "deepseek-v4-pro"])
        headers = request.call_args.kwargs["headers"]
        self.assertEqual(headers[deepseek.TOKEN_HEADER], "tok-1")
        # /models 在 api.deepseek.com，不需要平台浏览器头
        self.assertTrue(headers["user-agent"].startswith("deepseek-harness/"))
        self.assertNotIn("origin", headers)
        self.assertNotIn("referer", headers)

    def test_list_models_401(self):
        with mock.patch.object(deepseek, "_http_request", return_value=(401, b"{}")):
            with self.assertRaises(deepseek.AccountError) as ctx:
                deepseek._list_models("tok-1")
        self.assertEqual(ctx.exception.code, "expired")

    def test_list_models_bad_body(self):
        with mock.patch.object(deepseek, "_http_request", return_value=(200, b'{"data": 1}')):
            with self.assertRaises(deepseek.AccountError):
                deepseek._list_models("tok-1")


class InferenceTests(unittest.TestCase):
    def test_build_body(self):
        body = deepseek._build_body(
            "deepseek-flash",
            [
                {"role": "system", "content": "系统提示"},
                {"role": "user", "content": "你好"},
                {"role": "assistant", "content": "在"},
                {"role": "user", "content": "继续"},
            ],
            temperature=0.2,
            max_tokens=1024,
        )
        self.assertEqual(body["model"], "deepseek-flash")
        self.assertTrue(body["stream"])
        self.assertEqual(body["max_tokens"], 1024)
        self.assertEqual(body["system"], "系统提示")
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertEqual(body["temperature"], 0.2)
        self.assertEqual([m["role"] for m in body["messages"]], ["user", "assistant", "user"])
        self.assertEqual(body["messages"][0]["content"], [{"type": "text", "text": "你好"}])

    def test_build_body_default_max_tokens(self):
        # 账号订阅模式：不传 max_tokens，交给账号默认（不限制）
        body = deepseek._build_body("deepseek-flash", [{"role": "user", "content": "hi"}])
        self.assertNotIn("max_tokens", body)
        self.assertNotIn("temperature", body)
        self.assertNotIn("system", body)
        # 显式给 0 也一样不传
        body0 = deepseek._build_body("deepseek-flash", [{"role": "user", "content": "hi"}], max_tokens=0)
        self.assertNotIn("max_tokens", body0)
        # 显式大于 0 才传
        body2 = deepseek._build_body("deepseek-flash", [{"role": "user", "content": "hi"}], max_tokens=512)
        self.assertEqual(body2["max_tokens"], 512)

    def test_build_body_rejects_empty(self):
        with self.assertRaises(deepseek.AccountError):
            deepseek._build_body("m", [])

    def test_consume_stream_text_deltas(self):
        stream = io.BytesIO(
            sse(
                ("message_start", {"type": "message_start"}),
                (
                    "content_block_start",
                    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                ),
                (
                    "content_block_delta",
                    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "你好"}},
                ),
                (
                    "content_block_delta",
                    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "世界"}},
                ),
                ("message_stop", {"type": "message_stop"}),
            )
        )
        self.assertEqual(deepseek._consume_stream(stream), "你好世界")

    def test_consume_stream_whole_json(self):
        payload = {
            "content": [
                {"type": "thinking", "thinking": "内部思考"},
                {"type": "text", "text": "答案"},
            ]
        }
        stream = io.BytesIO(json.dumps(payload).encode())
        self.assertEqual(deepseek._consume_stream(stream), "答案")

    def test_consume_stream_error_event(self):
        stream = io.BytesIO(
            sse(
                (
                    "error",
                    {
                        "type": "error",
                        "error": {"type": "insufficient_quota", "message": "quota exceeded"},
                    },
                )
            )
        )
        with self.assertRaises(deepseek.AccountError) as ctx:
            deepseek._consume_stream(stream)
        self.assertIn("额度", str(ctx.exception))

    def test_chat_request_shape(self):
        captured = {}

        def fake_open(url, body, headers, timeout=30):
            captured["url"] = url
            captured["body"] = json.loads(body)
            captured["headers"] = headers
            captured["timeout"] = timeout
            return io.BytesIO(
                sse(
                    (
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {"type": "text_delta", "text": "回复"},
                        },
                    )
                )
            )

        record = {"access": "secret-token", "extra": {"email": "a@b.com"}}
        with mock.patch.object(deepseek, "_open_stream", side_effect=fake_open):
            text = deepseek.chat(
                record,
                "deepseek-flash",
                [{"role": "user", "content": "在吗"}],
                0.7,
                64,
                30,
            )
        self.assertEqual(text, "回复")
        self.assertTrue(captured["url"].endswith("/anthropic/v1/messages"))
        self.assertEqual(captured["headers"][deepseek.TOKEN_HEADER], "secret-token")
        self.assertEqual(captured["headers"]["anthropic-version"], "2023-06-01")
        # 推理请求不能被平台浏览器的头污染
        self.assertTrue(captured["headers"]["user-agent"].startswith("deepseek-harness/"))
        self.assertNotIn("origin", captured["headers"])
        self.assertNotIn("referer", captured["headers"])
        self.assertEqual(captured["body"]["model"], "deepseek-flash")
        self.assertEqual(captured["body"]["max_tokens"], 64)
        # token 不能出现在错误/日志里（这里只确认没进 URL）
        self.assertNotIn("secret-token", captured["url"])

    def test_chat_requires_token(self):
        with self.assertRaises(deepseek.AccountError) as ctx:
            deepseek.chat({}, "deepseek-flash", [{"role": "user", "content": "hi"}], None, None, 30)
        self.assertEqual(ctx.exception.code, "not-signed-in")

    def test_chat_401_maps_to_expired_without_token(self):
        def fake_open(url, body, headers, timeout=30):
            raise deepseek._inference_error(401)

        with mock.patch.object(deepseek, "_open_stream", side_effect=fake_open):
            with self.assertRaises(deepseek.AccountError) as ctx:
                deepseek.chat(
                    {"access": "secret-token"},
                    "deepseek-flash",
                    [{"role": "user", "content": "hi"}],
                    None,
                    None,
                    30,
                )
        self.assertEqual(ctx.exception.code, "expired")
        self.assertNotIn("secret-token", str(ctx.exception))


class LabelTests(unittest.TestCase):
    def test_label(self):
        self.assertEqual(deepseek.label({"extra": {"email": "a@b.com"}}), "a@b.com")
        self.assertEqual(deepseek.label({"extra": {}, "account": "13800000000"}), "13800000000")
        self.assertIsNone(deepseek.label({}))


if __name__ == "__main__":
    unittest.main()
