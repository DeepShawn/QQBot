"""ChatGPT 账号画图 + image_gen 插件账号模式/触发词测试（全离线 mock）。"""
from __future__ import annotations

import asyncio
import base64
import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent.parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import account_core  # noqa: E402
import accounts_chatgpt  # noqa: E402
import imagegen  # noqa: E402
from plugins import image_gen as image_gen_plugin  # noqa: E402

# 1x1 透明 PNG
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
TINY_PNG_B64 = base64.b64encode(TINY_PNG).decode("ascii")
ACCESS = "SECRET-ACCESS-TOKEN-123456"
ACCOUNT_ID = "acct-123"


def _record() -> dict:
    return {
        "kind": "oauth",
        "access": ACCESS,
        "refresh": "refresh",
        "expires_at": 9999999999,
        "account": "u@example.com",
        "extra": {"chatgpt_account_id": ACCOUNT_ID},
    }


class _FakeHTTPResponse:
    def __init__(self, raw: bytes):
        self._raw = raw
        self.headers = {"Content-Length": str(len(raw))}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size: int = -1):
        if size is None or size < 0:
            return self._raw
        return self._raw[:size]


class ChatGPTCurrentImageTests(unittest.TestCase):
    def setUp(self):
        self._patch(account_core, "mark_expired", lambda pid: None)

    def _patch(self, obj, name, value):
        old = getattr(obj, name)
        setattr(obj, name, value)
        self.addCleanup(setattr, obj, name, old)

    def test_b64_response_and_request_shape(self):
        calls: dict = {}

        def fake_http_json(url, payload=None, **kwargs):
            calls.update(url=url, payload=payload, kwargs=kwargs)
            return {"data": [{"b64_json": TINY_PNG_B64}]}

        self._patch(account_core, "http_json", fake_http_json)
        images = accounts_chatgpt.generate_images(_record(), "一只猫")
        self.assertEqual(images, [TINY_PNG])
        self.assertEqual(calls["url"], accounts_chatgpt.IMAGES_URL)
        payload = calls["payload"]
        self.assertEqual(payload["model"], "gpt-image-2")
        self.assertEqual(payload["prompt"], "一只猫")
        self.assertEqual(payload["size"], "1024x1024")
        self.assertEqual(payload["n"], 1)
        headers = calls["kwargs"]["headers"]
        self.assertEqual(headers["Authorization"], f"Bearer {ACCESS}")
        self.assertEqual(headers["ChatGPT-Account-ID"], ACCOUNT_ID)
        self.assertEqual(headers["accept"], "application/json")
        self.assertIn("Mozilla", headers["User-Agent"])
        self.assertEqual(calls["kwargs"]["label"], "ChatGPT 画图")
        error_map = calls["kwargs"]["error_map"]
        self.assertEqual(error_map[401][0], "expired")
        self.assertEqual(error_map[403][0], "expired")
        self.assertIn("额度", error_map[429][1])

    def test_model_and_size_passthrough(self):
        captured: dict = {}

        def fake_http_json(url, payload=None, **kwargs):
            captured.update(payload or {})
            return {"data": [{"b64_json": TINY_PNG_B64}]}

        self._patch(account_core, "http_json", fake_http_json)
        accounts_chatgpt.generate_images(
            _record(), "猫", size="512x512", model="gpt-image-3"
        )
        self.assertEqual(captured["model"], "gpt-image-3")
        self.assertEqual(captured["size"], "512x512")

    def test_url_response_downloads_with_browser_ua(self):
        captured: dict = {}

        def fake_http_json(url, payload=None, **kwargs):
            return {"data": [{"url": "https://example.com/img.png"}]}

        def fake_urlopen(request, timeout=None):
            captured["headers"] = {
                key.lower(): str(value) for key, value in request.header_items()
            }
            return _FakeHTTPResponse(TINY_PNG)

        self._patch(account_core, "http_json", fake_http_json)
        self._patch(accounts_chatgpt.urllib.request, "urlopen", fake_urlopen)
        images = accounts_chatgpt.generate_images(_record(), "猫")
        self.assertEqual(images, [TINY_PNG])
        self.assertIn("Mozilla", captured["headers"].get("user-agent", ""))

    def test_401_marks_expired_without_fallback(self):
        marked: list = []
        fallback_called: list = []

        def fake_http_json(url, payload=None, **kwargs):
            raise account_core.AccountError("expired")

        def fake_post_sse(*args, **kwargs):
            fallback_called.append(True)
            raise AssertionError("401 不应该再走 SSE 兜底")

        self._patch(account_core, "http_json", fake_http_json)
        self._patch(account_core, "post_sse", fake_post_sse)
        self._patch(account_core, "mark_expired", lambda pid: marked.append(pid))
        with self.assertRaises(account_core.AccountError) as ctx:
            accounts_chatgpt.generate_images(_record(), "猫")
        self.assertEqual(ctx.exception.code, "expired")
        self.assertEqual(marked, ["chatgpt"])
        self.assertEqual(fallback_called, [])

    def test_429_message_surfaced_when_fallback_fails(self):
        def fake_http_json(url, payload=None, **kwargs):
            code, message = kwargs["error_map"][429]
            raise account_core.AccountError(code, message)

        def fake_post_sse(*args, **kwargs):
            raise account_core.AccountError("network")

        self._patch(account_core, "http_json", fake_http_json)
        self._patch(account_core, "post_sse", fake_post_sse)
        with self.assertRaises(account_core.AccountError) as ctx:
            accounts_chatgpt.generate_images(_record(), "猫")
        self.assertIn("额度", str(ctx.exception))
        self.assertNotIn(ACCESS, str(ctx.exception))

    def test_no_images_raises_friendly_error(self):
        self._patch(account_core, "http_json", lambda *a, **k: {"data": []})
        self._patch(account_core, "post_sse", lambda *a, **k: [])
        with self.assertRaises(account_core.AccountError) as ctx:
            accounts_chatgpt.generate_images(_record(), "猫")
        self.assertIn("没有返回图片", str(ctx.exception))
        self.assertNotIn(ACCESS, str(ctx.exception))

    def test_sse_fallback_returns_image(self):
        captured: dict = {}

        def fake_http_json(*args, **kwargs):
            raise account_core.AccountError("generic", "ChatGPT 画图返回错误（HTTP 500）")

        def fake_post_sse(url, payload, **kwargs):
            captured["url"] = url
            captured["payload"] = payload
            captured["headers"] = kwargs.get("headers") or {}
            return [
                {"type": "response.created"},
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "image_generation_call",
                        "result": TINY_PNG_B64,
                    },
                },
            ]

        self._patch(account_core, "http_json", fake_http_json)
        self._patch(account_core, "post_sse", fake_post_sse)
        images = accounts_chatgpt.generate_images(_record(), "一只狗", size="1024x1024")
        self.assertEqual(images, [TINY_PNG])
        self.assertEqual(captured["url"], accounts_chatgpt.API_URL)
        payload = captured["payload"]
        self.assertEqual(payload["tools"][0]["type"], "image_generation")
        self.assertEqual(payload["tools"][0]["output_format"], "png")
        self.assertEqual(payload["tools"][0]["size"], "1024x1024")
        headers = captured["headers"]
        self.assertEqual(headers["ChatGPT-Account-ID"], ACCOUNT_ID)
        self.assertEqual(headers["accept"], "text/event-stream")

    def test_missing_access_raises_expired(self):
        with self.assertRaises(account_core.AccountError) as ctx:
            accounts_chatgpt.generate_images({"access": ""}, "猫")
        self.assertEqual(ctx.exception.code, "expired")

    def test_empty_prompt_raises(self):
        with self.assertRaises(account_core.AccountError):
            accounts_chatgpt.generate_images(_record(), "   ")

    def test_network_error_not_leaking_token(self):
        def fake_http_json(*args, **kwargs):
            raise account_core.AccountError("network")

        def fake_post_sse(*args, **kwargs):
            raise account_core.AccountError("generic", "ChatGPT 画图返回错误（HTTP 500）")

        self._patch(account_core, "http_json", fake_http_json)
        self._patch(account_core, "post_sse", fake_post_sse)
        with self.assertRaises(account_core.AccountError) as ctx:
            accounts_chatgpt.generate_images(_record(), "猫")
        self.assertNotIn(ACCESS, str(ctx.exception))


class ImageGenAccountDispatchTests(unittest.TestCase):
    def _patch(self, obj, name, value):
        old = getattr(obj, name)
        setattr(obj, name, value)
        self.addCleanup(setattr, obj, name, old)

    def _chatgpt_module(self):
        """拿「imagegen 里 import 会拿到」的那个 accounts_chatgpt 模块对象。

        其它测试可能把 sys.modules 里的 accounts_chatgpt 换成 stub/重新 import，
        所以这里以 sys.modules 当前对象为准，避免 patch 到旧对象上。
        """
        module = importlib.import_module("accounts_chatgpt")
        previous = sys.modules.get("accounts_chatgpt")
        sys.modules["accounts_chatgpt"] = module
        if previous is None:
            self.addCleanup(sys.modules.pop, "accounts_chatgpt", None)
        else:
            self.addCleanup(sys.modules.__setitem__, "accounts_chatgpt", previous)
        return module

    def test_chatgpt_routes_to_account_images(self):
        captured: dict = {}

        def fake_generate(record, prompt, size="1024x1024", model=None, timeout=120):
            captured.update(
                record=record, prompt=prompt, size=size, model=model, timeout=timeout
            )
            return [TINY_PNG]

        self._patch(self._chatgpt_module(), "generate_images", fake_generate)
        self._patch(
            account_core,
            "ensure_access",
            lambda provider: {"access": ACCESS, "extra": {"chatgpt_account_id": ACCOUNT_ID}},
        )
        images = imagegen.generate_with_account("chatgpt", "一只猫")
        self.assertEqual(images, [TINY_PNG])
        self.assertEqual(captured["prompt"], "一只猫")
        self.assertEqual(captured["size"], "1024x1024")

    def test_other_account_rejected_with_friendly_message(self):
        with self.assertRaises(imagegen.ImageGenError) as ctx:
            imagegen.generate_with_account("claude", "猫")
        self.assertIn("只支持 ChatGPT", str(ctx.exception))
        self.assertIn("API Key", str(ctx.exception))

    def test_empty_auth_rejected(self):
        with self.assertRaises(imagegen.ImageGenError):
            imagegen.generate_with_account("", "猫")

    def test_not_signed_in(self):
        def fake_ensure(provider):
            raise account_core.AccountError("not-signed-in")

        self._patch(account_core, "ensure_access", fake_ensure)
        with self.assertRaises(imagegen.ImageGenError) as ctx:
            imagegen.generate_with_account("chatgpt", "猫")
        self.assertIn("还没有连接", str(ctx.exception))

    def test_account_error_converted(self):
        def fake_generate(*args, **kwargs):
            raise account_core.AccountError("expired")

        self._patch(self._chatgpt_module(), "generate_images", fake_generate)
        self._patch(account_core, "ensure_access", lambda provider: _record())
        with self.assertRaises(imagegen.ImageGenError) as ctx:
            imagegen.generate_with_account("chatgpt", "猫")
        self.assertEqual(str(ctx.exception), account_core.ERRORS["expired"])

    def test_provided_record_skips_ensure_access(self):
        calls: list = []
        self._patch(
            account_core,
            "ensure_access",
            lambda provider: calls.append(provider) or _record(),
        )
        self._patch(
            self._chatgpt_module(), "generate_images", lambda *a, **k: [TINY_PNG]
        )
        imagegen.generate_with_account("chatgpt", "猫", record=_record())
        self.assertEqual(calls, [])


# ---------------------------------------------------------------- 插件测试


class FakeLog:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass

    def exception(self, *args, **kwargs):
        pass


class FakeCtx:
    def __init__(self, root, cfg: dict):
        self.data_dir = Path(root)
        self.log = FakeLog()
        self.self_id = 999
        self._cfg = cfg
        self.replies: list[str] = []
        self.sent: list[dict] = []

    @property
    def cfg(self) -> dict:
        return self._cfg

    def plugin_cfg(self, name: str) -> dict:
        return (self._cfg.get("plugins") or {}).get(name) or {}

    def format_text(self, template, event, **extra):
        return str(template or "")

    async def reply(self, event, message, at: bool = False):
        self.replies.append(str(message))
        return {"message_id": len(self.replies)}

    async def send_private(self, user_id, message):
        self.sent.append({"type": "private", "user_id": user_id, "message": message})

    async def send_group(self, group_id, message):
        self.sent.append({"type": "group", "group_id": group_id, "message": message})


def _make_cfg(image_gen_cfg=None, ai_cfg=None) -> dict:
    return {
        "plugins": {
            "image_gen": dict(
                {
                    "enabled": True,
                    "provider": "openai",
                    "base_url": "",
                    "api_key": "",
                    "model": "gpt-image-1",
                    "size": "1024x1024",
                    "timeout_seconds": 30,
                    "triggers": ["画一张", "画个", "画只", "生图"],
                    "push_groups": [],
                    "caption": "",
                    "require_at": True,
                    "at_sender": True,
                    "cooldown_seconds": 0,
                },
                **(image_gen_cfg or {}),
            ),
            "ai_chat": dict(
                {
                    "provider": "openai",
                    "base_url": "https://api.deepseek.com/v1",
                    "api_key": "",
                    "auth": "",
                    "model": "deepseek-chat",
                },
                **(ai_cfg or {}),
            ),
        }
    }


def _event(text: str, *, private: bool = True, mention: bool = False) -> dict:
    segs = []
    if mention:
        segs.append({"type": "at", "data": {"qq": "999"}})
    if text:
        segs.append({"type": "text", "data": {"text": text}})
    event = {
        "post_type": "message",
        "message_type": "private" if private else "group",
        "user_id": 20002,
        "message": segs,
    }
    if not private:
        event["group_id"] = 12345
    return event


class ImageGenPluginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ctx = FakeCtx(self._tmp.name, _make_cfg())
        self.plugin = image_gen_plugin.Plugin(self.ctx)
        self.generate_calls: list = []
        self.account_calls: list = []
        self._orig_generate = imagegen.generate
        self._orig_account = imagegen.generate_with_account
        self.addCleanup(setattr, imagegen, "generate", self._orig_generate)
        self.addCleanup(
            setattr, imagegen, "generate_with_account", self._orig_account
        )

    def install_api(self, images=None):
        def fake_generate(**kwargs):
            self.generate_calls.append(kwargs)
            return list(images if images is not None else [TINY_PNG])

        imagegen.generate = fake_generate

    def install_account(self, images=None, error=None):
        def fake_account(auth, prompt, **kwargs):
            self.account_calls.append((auth, prompt, kwargs))
            if error is not None:
                raise error
            return list(images if images is not None else [TINY_PNG])

        imagegen.generate_with_account = fake_account

    async def run_event(self, event) -> bool:
        return await self.plugin.on_event(event)

    # ---------------- 触发词 ----------------
    def use_api_key(self):
        self.ctx._cfg["plugins"]["image_gen"]["api_key"] = "test-key"

    async def test_trigger_in_middle_of_sentence(self):
        self.use_api_key()
        self.install_api()
        handled = await self.run_event(_event("帮我画一张猫"))
        self.assertTrue(handled)
        self.assertEqual(self.generate_calls[0]["prompt"], "猫")

    async def test_trigger_at_start(self):
        self.use_api_key()
        self.install_api()
        await self.run_event(_event("画一张猫"))
        self.assertEqual(self.generate_calls[0]["prompt"], "猫")

    async def test_trigger_at_end_falls_back_to_full_text(self):
        self.use_api_key()
        self.install_api()
        await self.run_event(_event("帮我画一张"))
        self.assertEqual(self.generate_calls[0]["prompt"], "帮我")

    async def test_trigger_only_no_prompt_ignored(self):
        self.use_api_key()
        self.install_api()
        handled = await self.run_event(_event("画一张"))
        self.assertFalse(handled)

    async def test_group_requires_at(self):
        self.use_api_key()
        self.install_api()
        handled = await self.run_event(_event("画一张猫", private=False, mention=False))
        self.assertFalse(handled)
        handled = await self.run_event(_event("画一张猫", private=False, mention=True))
        self.assertTrue(handled)

    # ---------------- 配置优先级 ----------------
    async def test_image_gen_api_key_priority_over_account(self):
        self.ctx._cfg = _make_cfg(
            image_gen_cfg={"api_key": "img-key"},
            ai_cfg={"auth": "chatgpt", "api_key": "ai-key"},
        )
        self.install_api()
        self.install_account()
        await self.run_event(_event("画一张猫"))
        self.assertEqual(len(self.generate_calls), 1)
        self.assertEqual(self.generate_calls[0]["api_key"], "img-key")
        # base_url 为空时用 OpenAI 官方，不继承 AI 的 base_url
        self.assertEqual(
            self.generate_calls[0]["base_url"], imagegen.DEFAULT_IMAGE_BASE_URL
        )
        self.assertEqual(self.account_calls, [])

    async def test_reuses_ai_key_when_follow_ai(self):
        self.ctx._cfg = _make_cfg(
            image_gen_cfg={"follow_ai": True},
            ai_cfg={"auth": "", "api_key": "ai-key"},
        )
        self.install_api()
        await self.run_event(_event("画一张猫"))
        self.assertEqual(self.generate_calls[0]["api_key"], "ai-key")

    async def test_account_mode_uses_chatgpt(self):
        self.ctx._cfg = _make_cfg(
            image_gen_cfg={"auth": "chatgpt"},
            ai_cfg={"auth": "claude", "api_key": ""},
        )
        self.install_account()
        self.install_api()
        handled = await self.run_event(_event("画一张猫"))
        self.assertTrue(handled)
        self.assertEqual(len(self.account_calls), 1)
        auth, prompt, kwargs = self.account_calls[0]
        self.assertEqual(auth, "chatgpt")
        self.assertEqual(prompt, "猫")
        self.assertEqual(kwargs.get("size"), "1024x1024")
        self.assertEqual(self.generate_calls, [])
        self.assertTrue(self.ctx.sent)
        message = self.ctx.sent[0]["message"]
        self.assertTrue(any(seg.get("type") == "image" for seg in message))

    async def test_other_account_replies_hint(self):
        self.ctx._cfg = _make_cfg(
            image_gen_cfg={"auth": "claude"},
            ai_cfg={"auth": "claude", "api_key": ""},
        )
        self.install_account()
        handled = await self.run_event(_event("画一张猫"))
        self.assertTrue(handled)
        self.assertIn("只支持 ChatGPT", self.ctx.replies[-1])
        self.assertEqual(self.ctx.sent, [])
        self.assertEqual(self.account_calls, [])

    async def test_account_error_replies_chinese(self):
        self.ctx._cfg = _make_cfg(
            image_gen_cfg={"auth": "chatgpt"},
            ai_cfg={"auth": "chatgpt", "api_key": ""},
        )
        self.install_account(
            error=imagegen.ImageGenError("登录已过期，重新连接一次")
        )
        await self.run_event(_event("画一张猫"))
        self.assertIn("画图失败", self.ctx.replies[-1])
        self.assertIn("重新连接", self.ctx.replies[-1])

    async def test_nothing_configured_replies_hint(self):
        self.install_api()
        self.install_account()
        handled = await self.run_event(_event("画一张猫"))
        self.assertTrue(handled)
        reply = self.ctx.replies[-1]
        self.assertIn("画图需要", reply)
        self.assertIn("ChatGPT 账号", reply)
        self.assertIn("图片 API Key", reply)
        self.assertEqual(self.generate_calls, [])
        self.assertEqual(self.account_calls, [])


if __name__ == "__main__":
    unittest.main()
