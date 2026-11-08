#!/usr/bin/env python3
"""GET /api/models 的单元测试。

全部用本地假 HTTP / 假 account_core，不访问真实第三方接口、不消耗用户 key。
"""
from __future__ import annotations

import json
import socket
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent.parent
BOT_DIR = REPO / "bot"
ADMIN_DIR = BOT_DIR / "admin"
for _p in (str(ADMIN_DIR), str(BOT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import server  # noqa: E402


class _FakeHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        state = self.server.fake_state  # type: ignore[attr-defined]
        state["requests"].append({
            "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
        })
        body = state["body"]
        raw = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.send_response(state["status"])
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except Exception:
            pass


class FakeAPI:
    """本地假模型接口。"""

    def __init__(self, status: int = 200, body=None):
        self.state = {"status": status, "body": {} if body is None else body,
                      "requests": []}
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeHandler)
        self.srv.daemon_threads = True
        self.srv.fake_state = self.state  # type: ignore[attr-defined]
        self.thread = threading.Thread(
            target=self.srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        return "http://127.0.0.1:%d" % self.srv.server_address[1]

    @property
    def requests(self):
        return self.state["requests"]

    def close(self):
        try:
            self.srv.shutdown()
        finally:
            self.srv.server_close()


def make_config(profiles, active="default"):
    return {"plugins": {"ai_chat": {"active_profile": active, "profiles": profiles}}}


def profile(pid="default", provider="openai", base_url="", api_key="sk-test",
            model="", auth=""):
    return {"id": pid, "name": pid, "provider": provider, "base_url": base_url,
            "api_key": api_key, "model": model, "auth": auth}


def closed_port_url() -> str:
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    finally:
        sock.close()
    return "http://127.0.0.1:%d" % port


class _FakeAccountModule:
    MODELS = ["claude-a", "claude-b"]
    DEFAULT_MODEL = "claude-a"


class _FakeDeepSeekModule:
    MODELS = ["deepseek-flash"]
    DEFAULT_MODEL = "deepseek-flash"


class _FakeAccountCore:
    PROVIDER_IDS = ("chatgpt", "claude", "gemini", "deepseek")

    def __init__(self, records=None, modules=None, load_error=False):
        self.records = records or {}
        self.modules = modules or {}
        self.load_error = load_error
        self.calls = []

    def load_provider(self, provider):
        self.calls.append(("load_provider", provider))
        if self.load_error:
            raise RuntimeError("no module")
        return self.modules.get(provider, _FakeAccountModule())

    def get_record(self, provider):
        self.calls.append(("get_record", provider))
        return self.records.get(provider)


class ModelsAPITest(unittest.TestCase):
    def setUp(self):
        self.cfg = None
        self.patch_cfg = mock.patch.object(server, "load_config", new=lambda: self.cfg)
        self.patch_cfg.start()

    def tearDown(self):
        self.patch_cfg.stop()

    # ---------- 接口模式：openai 兼容 ----------
    def test_openai_domain_only_uses_v1_models(self):
        fake = FakeAPI(200, {"data": [{"id": "gpt-4o"}, {"id": "gpt-4o-mini"}, {"id": "gpt-4o"}]})
        try:
            self.cfg = make_config([profile(base_url=fake.base)])
            res = server.api_models("")
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["source"], "api")
            self.assertEqual(res["models"], ["gpt-4o", "gpt-4o-mini"])
            self.assertEqual(res["default"], "gpt-4o")
            self.assertEqual(len(fake.requests), 1)
            self.assertEqual(fake.requests[0]["path"], "/v1/models")
            self.assertEqual(fake.requests[0]["headers"].get("authorization"), "Bearer sk-test")
            self.assertNotIn("sk-test", json.dumps(res))
        finally:
            fake.close()

    def test_openai_base_v1_not_duplicated(self):
        fake = FakeAPI(200, {"data": [{"id": "deepseek-chat"}]})
        try:
            self.cfg = make_config([profile(base_url=fake.base + "/v1")])
            res = server.api_models("default")
            self.assertTrue(res["ok"], res)
            self.assertEqual(fake.requests[0]["path"], "/v1/models")
            self.assertEqual(res["models"], ["deepseek-chat"])
        finally:
            fake.close()

    def test_openai_base_v1beta_openai(self):
        fake = FakeAPI(200, {"data": [{"id": "gemini-2.5-flash"}]})
        try:
            self.cfg = make_config([profile(base_url=fake.base + "/v1beta/openai/")])
            res = server.api_models("default")
            self.assertTrue(res["ok"], res)
            self.assertEqual(fake.requests[0]["path"], "/v1beta/openai/models")
        finally:
            fake.close()

    def test_openai_keeps_current_model_as_default(self):
        fake = FakeAPI(200, {"data": [{"id": "a"}, {"id": "b"}]})
        try:
            self.cfg = make_config([profile(base_url=fake.base, model="b")])
            res = server.api_models("default")
            self.assertEqual(res["default"], "b")
        finally:
            fake.close()

    # ---------- 接口模式：claude ----------
    def test_claude_headers_and_parse(self):
        fake = FakeAPI(200, {"data": [{"id": "claude-sonnet-4-5"},
                                      {"id": "claude-opus-4-1"}]})
        try:
            self.cfg = make_config([profile(provider="claude", base_url=fake.base)])
            res = server.api_models("default")
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["models"], ["claude-sonnet-4-5", "claude-opus-4-1"])
            req = fake.requests[0]
            self.assertEqual(req["path"], "/v1/models")
            self.assertEqual(req["headers"].get("x-api-key"), "sk-test")
            self.assertEqual(req["headers"].get("anthropic-version"), "2023-06-01")
        finally:
            fake.close()

    def test_claude_base_v1_not_duplicated(self):
        fake = FakeAPI(200, {"data": [{"id": "claude-x"}]})
        try:
            self.cfg = make_config([profile(provider="claude", base_url=fake.base + "/v1")])
            res = server.api_models("default")
            self.assertTrue(res["ok"], res)
            self.assertEqual(fake.requests[0]["path"], "/v1/models")
        finally:
            fake.close()

    # ---------- 接口模式：gemini ----------
    def test_gemini_filters_generate_content_and_strips_prefix(self):
        fake = FakeAPI(200, {"models": [
            {"name": "models/gemini-2.5-flash",
             "supportedGenerationMethods": ["generateContent", "countTokens"]},
            {"name": "models/embedding-001",
             "supportedGenerationMethods": ["embedContent"]},
            {"name": "models/gemini-2.5-pro"},
        ]})
        try:
            self.cfg = make_config([profile(provider="gemini", base_url=fake.base)])
            res = server.api_models("default")
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["models"], ["gemini-2.5-flash", "gemini-2.5-pro"])
            path = fake.requests[0]["path"]
            self.assertTrue(path.startswith("/v1beta/models?"), path)
            self.assertIn("key=sk-test", path)
            self.assertNotIn("sk-test", json.dumps(res))
        finally:
            fake.close()

    def test_gemini_base_v1beta_not_duplicated(self):
        fake = FakeAPI(200, {"models": [{"name": "models/gemini-2.5-pro"}]})
        try:
            self.cfg = make_config([profile(provider="gemini", base_url=fake.base + "/v1beta")])
            res = server.api_models("default")
            self.assertTrue(res["ok"], res)
            self.assertTrue(fake.requests[0]["path"].startswith("/v1beta/models?"),
                            fake.requests[0]["path"])
        finally:
            fake.close()

    # ---------- 错误映射 ----------
    def _fetch_error(self, status, expected, body=None, provider="openai"):
        fake = FakeAPI(status, body)
        try:
            self.cfg = make_config([profile(provider=provider, base_url=fake.base)])
            res = server.api_models("default")
            self.assertFalse(res["ok"])
            self.assertEqual(res["error"], expected)
            return res
        finally:
            fake.close()

    def test_error_401(self):
        res = self._fetch_error(401, "密钥不对或没有权限")
        self.assertNotIn("sk-test", json.dumps(res))

    def test_error_403(self):
        self._fetch_error(403, "密钥不对或没有权限")

    def test_error_404(self):
        self._fetch_error(404, "接口地址不对")

    def test_error_other_http_status(self):
        self._fetch_error(500, "获取模型失败（HTTP 500）")

    def test_error_bad_json(self):
        self._fetch_error(200, "接口返回的模型列表看不懂", body=b"<html>nope</html>")

    def test_error_empty_models(self):
        self._fetch_error(200, "接口没有返回可用模型，检查接口设置", body={"data": []})

    def test_error_network(self):
        self.cfg = make_config([profile(base_url=closed_port_url())])
        res = server.api_models("default")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "网络连不上")

    # ---------- profile 选择 / 参数校验 ----------
    def test_profile_not_found(self):
        self.cfg = make_config([profile(pid="default", base_url="http://127.0.0.1:1")])
        res = server.api_models("missing-profile")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "找不到这套接口")

    def test_active_profile_is_used_when_empty(self):
        fake1 = FakeAPI(200, {"data": [{"id": "from-a"}]})
        fake2 = FakeAPI(200, {"data": [{"id": "from-b"}]})
        try:
            self.cfg = make_config([
                profile(pid="a", base_url=fake1.base),
                profile(pid="b", base_url=fake2.base),
            ], active="b")
            res = server.api_models("")
            self.assertEqual(res["models"], ["from-b"])
            self.assertEqual(len(fake1.requests), 0)
        finally:
            fake1.close()
            fake2.close()

    def test_stale_active_profile_falls_back_to_first(self):
        fake = FakeAPI(200, {"data": [{"id": "fallback-model"}]})
        try:
            self.cfg = make_config([profile(pid="a", base_url=fake.base)], active="deleted-id")
            res = server.api_models("")
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["models"], ["fallback-model"])
        finally:
            fake.close()

    def test_explicit_profile_id(self):
        fake = FakeAPI(200, {"data": [{"id": "x"}]})
        try:
            self.cfg = make_config([profile(pid="a", base_url="http://127.0.0.1:1"),
                                    profile(pid="b", base_url=fake.base)])
            res = server.api_models("b")
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["models"], ["x"])
        finally:
            fake.close()

    def test_legacy_config_without_profiles(self):
        fake = FakeAPI(200, {"data": [{"id": "legacy-model"}]})
        try:
            self.cfg = {"plugins": {"ai_chat": {
                "provider": "openai", "base_url": fake.base,
                "api_key": "sk-test", "model": "legacy-model", "auth": "",
            }}}
            res = server.api_models("")
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["models"], ["legacy-model"])
            self.assertEqual(res["default"], "legacy-model")
            self.assertEqual(fake.requests[0]["path"], "/v1/models")
        finally:
            fake.close()

    def test_missing_base_url(self):
        self.cfg = make_config([profile(base_url="")])
        res = server.api_models("default")
        self.assertEqual(res["error"], "先填接口地址")

    def test_bad_scheme(self):
        self.cfg = make_config([profile(base_url="api.example.com")])
        res = server.api_models("default")
        self.assertEqual(res["error"], "接口地址要以 http(s):// 开头")

    def test_missing_api_key_makes_no_request(self):
        fake = FakeAPI(200, {"data": [{"id": "x"}]})
        try:
            self.cfg = make_config([profile(base_url=fake.base, api_key="")])
            res = server.api_models("default")
            self.assertFalse(res["ok"])
            self.assertEqual(res["error"], "先填 API Key")
            self.assertEqual(len(fake.requests), 0)
        finally:
            fake.close()

    # ---------- 账号模式 ----------
    def _use_account_core(self, core):
        patcher = mock.patch.object(server, "account_core", core)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_account_mode_returns_module_models(self):
        core = _FakeAccountCore(records={"claude": {"account": "a@b.com", "extra": {}}},
                                modules={"claude": _FakeAccountModule()})
        self._use_account_core(core)
        self.cfg = make_config([profile(provider="claude", auth="claude", base_url="http://127.0.0.1:1")])
        res = server.api_models("default")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["source"], "account")
        self.assertEqual(res["models"], ["claude-a", "claude-b"])
        self.assertEqual(res["default"], "claude-a")
        self.assertEqual(core.calls[0], ("load_provider", "claude"))

    def test_account_mode_deepseek_prefers_record_models(self):
        record = {"access": "SECRET-TOKEN", "extra": {"models": ["deepseek-v4-pro", "deepseek-flash"]}}
        core = _FakeAccountCore(records={"deepseek": record},
                                modules={"deepseek": _FakeDeepSeekModule()})
        self._use_account_core(core)
        self.cfg = make_config([profile(provider="deepseek", auth="deepseek")])
        res = server.api_models("default")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["models"], ["deepseek-v4-pro", "deepseek-flash"])
        self.assertEqual(res["default"], "deepseek-flash")
        self.assertNotIn("SECRET-TOKEN", json.dumps(res))

    def test_account_mode_falls_back_to_static_models(self):
        core = _FakeAccountCore(records={"deepseek": {"extra": {}}},
                                modules={"deepseek": _FakeDeepSeekModule()})
        self._use_account_core(core)
        self.cfg = make_config([profile(provider="deepseek", auth="deepseek")])
        res = server.api_models("default")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["models"], ["deepseek-flash"])

    def test_account_not_connected(self):
        core = _FakeAccountCore(records={})
        self._use_account_core(core)
        self.cfg = make_config([profile(provider="claude", auth="claude")])
        res = server.api_models("default")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "先连接这个账号")

    def test_account_module_missing(self):
        core = _FakeAccountCore(load_error=True)
        self._use_account_core(core)
        self.cfg = make_config([profile(provider="gemini", auth="gemini")])
        res = server.api_models("default")
        self.assertEqual(res["error"], "先连接这个账号")

    def test_account_core_unavailable(self):
        self._use_account_core(None)
        self.cfg = make_config([profile(provider="chatgpt", auth="chatgpt")])
        res = server.api_models("default")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "先连接这个账号")

    # ---------- 配置缺失 ----------
    def test_config_without_ai_chat_uses_empty_legacy_profile(self):
        # 没有 ai_chat 配置时按老配置兜底（空接口），提示先填地址
        self.cfg = {}
        res = server.api_models("")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "先填接口地址")

    def test_empty_profiles_list_uses_legacy_profile(self):
        self.cfg = {"plugins": {"ai_chat": {"active_profile": "default",
                                            "profiles": [], "base_url": ""}}}
        res = server.api_models("")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "先填接口地址")


if __name__ == "__main__":
    unittest.main()
