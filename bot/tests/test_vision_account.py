"""账号模式识图（describe_images）离线测试：ChatGPT / Claude / Gemini + providers 分发。

不联网：所有 HTTP / SSE 都被 monkeypatch。
"""
from __future__ import annotations

import base64
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

BOT_DIR = Path(__file__).resolve().parent.parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import account_core  # noqa: E402
import providers  # noqa: E402
import accounts_chatgpt  # noqa: E402
import accounts_claude  # noqa: E402
import accounts_gemini  # noqa: E402

TOKEN = "secret-access-token"
JPEG = b"\xff\xd8\xff\xe0" + b"jpeg-body"
PNG = b"\x89PNG\r\n\x1a\n" + b"png-body"


def _sse(*chunks) -> bytes:
    return "".join(
        "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n" for chunk in chunks
    ).encode("utf-8")


def _gemini_text_chunk(text: str) -> dict:
    return {"response": {"candidates": [{"content": {"parts": [{"text": text}]}}]}}


# ---------------------------------------------------------------------------
# providers.describe_images(auth=...) 分发
# ---------------------------------------------------------------------------

class _FakeAccountModule:
    ID = "chatgpt"
    NAME = "ChatGPT"
    MODELS = ["gpt-test"]

    def __init__(self, result="一只黑猫", error=None):
        self.result = result
        self.error = error
        self.calls = []

    def describe_images(self, record, model, images, prompt, timeout, max_tokens):
        self.calls.append(
            {
                "record": record,
                "model": model,
                "images": images,
                "prompt": prompt,
                "timeout": timeout,
                "max_tokens": max_tokens,
            }
        )
        if self.error is not None:
            raise self.error
        return self.result


class ProviderDispatchTests(unittest.TestCase):
    def _call(self, **overrides):
        kwargs = {
            "provider": "openai",
            "base_url": "https://ignored.example/v1",
            "api_key": "ignored-key",
            "model": "",
            "images": [JPEG],
            "prompt": "描述一下",
            "timeout": 30,
            "max_tokens": 500,
            "auth": "chatgpt",
        }
        kwargs.update(overrides)
        return providers.describe_images(**kwargs)

    def test_auth_dispatches_to_account_module(self):
        module = _FakeAccountModule("一只黑猫")
        with mock.patch.object(account_core, "load_provider", return_value=module) as load:
            with mock.patch.object(account_core, "ensure_access", return_value={"access": TOKEN}) as ensure:
                text = self._call(auth="  ChatGPT ")
        self.assertEqual(text, "一只黑猫")
        load.assert_called_once_with("chatgpt")
        ensure.assert_called_once_with("chatgpt")
        call = module.calls[0]
        self.assertEqual(call["record"], {"access": TOKEN})
        self.assertEqual(call["model"], "")  # 空 model 交给账号模块用默认值
        self.assertEqual(call["images"], [JPEG])
        self.assertEqual(call["prompt"], "描述一下")
        self.assertEqual(call["timeout"], 30.0)
        self.assertEqual(call["max_tokens"], 500)

    def test_auth_ignores_provider_base_url_api_key(self):
        module = _FakeAccountModule("ok")
        with mock.patch.object(account_core, "load_provider", return_value=module):
            with mock.patch.object(account_core, "ensure_access", return_value={"access": TOKEN}):
                # provider 不合法也不影响账号模式
                self.assertEqual(self._call(provider="不存在的接口", auth="chatgpt"), "ok")

    def test_unsupported_module_without_describe_images(self):
        class NoVision:
            ID = "deepseek"

        with mock.patch.object(account_core, "load_provider", return_value=NoVision()):
            with self.assertRaises(providers.ProviderError) as ctx:
                self._call(auth="deepseek")
        self.assertIn("API Key", str(ctx.exception))

    def test_unknown_account_module_maps_to_unsupported(self):
        with mock.patch.object(
            account_core,
            "load_provider",
            side_effect=account_core.AccountError("unsupported"),
        ):
            with self.assertRaises(providers.ProviderError) as ctx:
                self._call(auth="nope")
        self.assertIn("API Key", str(ctx.exception))

    def test_not_signed_in_account_maps_to_provider_error(self):
        module = _FakeAccountModule("x")
        with mock.patch.object(account_core, "load_provider", return_value=module):
            with mock.patch.object(
                account_core,
                "ensure_access",
                side_effect=account_core.AccountError("not-signed-in"),
            ):
                with self.assertRaises(providers.ProviderError) as ctx:
                    self._call()
        self.assertEqual(str(ctx.exception), account_core.ERRORS["not-signed-in"])

    def test_account_error_from_module_is_wrapped(self):
        module = _FakeAccountModule(
            error=account_core.AccountError("expired", "登录已过期，重新连接一次")
        )
        with mock.patch.object(account_core, "load_provider", return_value=module):
            with mock.patch.object(account_core, "ensure_access", return_value={"access": TOKEN}):
                with self.assertRaises(providers.ProviderError) as ctx:
                    self._call()
        self.assertEqual(str(ctx.exception), "登录已过期，重新连接一次")
        self.assertNotIn(TOKEN, str(ctx.exception))

    def test_empty_images_short_circuits(self):
        with mock.patch.object(account_core, "load_provider") as load:
            self.assertEqual(self._call(images=[]), "")
        load.assert_not_called()

    def test_invalid_images_rejected_before_auth(self):
        with self.assertRaises(providers.ProviderError):
            self._call(images=["not-bytes"])

    def test_api_key_path_unchanged_without_auth(self):
        captured = {}

        def fake_post(url, payload, headers, **kwargs):
            captured["url"] = url
            captured["payload"] = payload
            captured["headers"] = headers
            return {"choices": [{"message": {"content": "API Key 描述"}}]}

        with mock.patch.object(providers, "_post_json", side_effect=fake_post):
            text = providers.describe_images(
                provider="openai",
                base_url="https://api.example.com/v1",
                api_key="sk-test",
                model="gpt-4o",
                images=[JPEG],
                prompt="描述",
                timeout=20,
                max_tokens=300,
            )
        self.assertEqual(text, "API Key 描述")
        self.assertTrue(captured["url"].endswith("/chat/completions"))
        self.assertEqual(captured["payload"]["model"], "gpt-4o")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer sk-test")
        self.assertNotIn("auth", captured["payload"])

    def test_api_key_path_still_requires_model(self):
        with self.assertRaises(providers.ProviderError) as ctx:
            providers.describe_images(
                provider="openai",
                base_url="",
                api_key="sk-test",
                model="",
                images=[JPEG],
            )
        self.assertIn("model", str(ctx.exception))


# ---------------------------------------------------------------------------
# ChatGPT 账号识图
# ---------------------------------------------------------------------------

class ChatGPTVisionTests(unittest.TestCase):
    def _record(self):
        return {"access": TOKEN, "extra": {"chatgpt_account_id": "acc-1"}}

    def _patch_sse(self, events):
        captured = {}

        def fake_post_sse(url, payload, **kwargs):
            captured["url"] = url
            captured["payload"] = payload
            captured["kwargs"] = kwargs
            return events

        return captured, mock.patch.object(account_core, "post_sse", side_effect=fake_post_sse)

    def test_payload_and_text(self):
        events = [{"type": "response.output_text.delta", "delta": "一只橘猫"}]
        captured, patcher = self._patch_sse(events)
        with patcher:
            text = accounts_chatgpt.describe_images(
                self._record(), "", [JPEG, PNG], prompt="看看图", timeout=30, max_tokens=256
            )
        self.assertEqual(text, "一只橘猫")
        self.assertTrue(captured["url"].endswith("/backend-api/codex/responses"))
        payload = captured["payload"]
        self.assertEqual(payload["model"], accounts_chatgpt.DEFAULT_MODEL)
        self.assertEqual(payload["max_output_tokens"], 256)
        self.assertTrue(payload["stream"])
        content = payload["input"][0]["content"]
        self.assertEqual(content[0], {"type": "input_text", "text": "看看图"})
        self.assertEqual(content[1]["type"], "input_image")
        self.assertTrue(content[1]["image_url"].startswith("data:image/jpeg;base64,"))
        self.assertEqual(
            base64.b64decode(content[1]["image_url"].split(",", 1)[1]), JPEG
        )
        self.assertTrue(content[2]["image_url"].startswith("data:image/png;base64,"))
        headers = captured["kwargs"]["headers"]
        self.assertEqual(headers["Authorization"], "Bearer " + TOKEN)
        self.assertEqual(headers["chatgpt-account-id"], "acc-1")
        self.assertEqual(captured["kwargs"]["secrets_"], (TOKEN,))

    def test_max_tokens_zero_omits_limit(self):
        captured, patcher = self._patch_sse([{"type": "response.output_text.delta", "delta": "x"}])
        with patcher:
            accounts_chatgpt.describe_images(self._record(), "", [JPEG], max_tokens=0)
        self.assertNotIn("max_output_tokens", captured["payload"])

    def test_empty_result_raises(self):
        _, patcher = self._patch_sse([])
        with patcher:
            with self.assertRaises(account_core.AccountError) as ctx:
                accounts_chatgpt.describe_images(self._record(), "", [JPEG])
        self.assertIn("没有返回图片描述", str(ctx.exception))

    def test_401_marks_expired_and_hides_token(self):
        with mock.patch.object(
            account_core,
            "post_sse",
            side_effect=account_core.AccountError("expired", "登录已过期，重新连接一次"),
        ):
            with mock.patch.object(account_core, "mark_expired") as mark:
                with self.assertRaises(account_core.AccountError) as ctx:
                    accounts_chatgpt.describe_images(self._record(), "", [JPEG])
        self.assertEqual(ctx.exception.code, "expired")
        mark.assert_called_once_with("chatgpt")
        self.assertNotIn(TOKEN, str(ctx.exception))

    def test_missing_access(self):
        with self.assertRaises(account_core.AccountError):
            accounts_chatgpt.describe_images({}, "", [JPEG])

    def test_bad_images(self):
        with self.assertRaises(account_core.AccountError):
            accounts_chatgpt.describe_images(self._record(), "", ["x"])
        with self.assertRaises(account_core.AccountError):
            accounts_chatgpt.describe_images(self._record(), "", [])


# ---------------------------------------------------------------------------
# Claude 账号识图
# ---------------------------------------------------------------------------

class ClaudeVisionTests(unittest.TestCase):
    def test_payload_and_text(self):
        captured = {}

        def fake_http(url, payload, **kwargs):
            captured["url"] = url
            captured["payload"] = payload
            captured["kwargs"] = kwargs
            return {"content": [{"type": "text", "text": "两只狗"}, {"type": "tool_use"}]}

        with mock.patch.object(account_core, "http_json", side_effect=fake_http):
            text = accounts_claude.describe_images(
                {"access": TOKEN}, "", [PNG], prompt="看看", timeout=25, max_tokens=400
            )
        self.assertEqual(text, "两只狗")
        self.assertTrue(captured["url"].endswith("/v1/messages"))
        payload = captured["payload"]
        self.assertEqual(payload["model"], accounts_claude.DEFAULT_MODEL)
        self.assertEqual(payload["max_tokens"], 400)
        blocks = payload["messages"][0]["content"]
        self.assertEqual(blocks[0], {"type": "text", "text": "看看"})
        self.assertEqual(blocks[1]["type"], "image")
        self.assertEqual(blocks[1]["source"]["type"], "base64")
        self.assertEqual(blocks[1]["source"]["media_type"], "image/png")
        self.assertEqual(base64.b64decode(blocks[1]["source"]["data"]), PNG)
        self.assertEqual(payload["system"][0]["text"], accounts_claude.CLAUDE_CODE_SYSTEM)
        headers = captured["kwargs"]["headers"]
        self.assertEqual(headers["Authorization"], "Bearer " + TOKEN)
        self.assertEqual(captured["kwargs"]["secrets_"], (TOKEN,))

    def test_default_max_tokens(self):
        captured = {}

        def fake_http(url, payload, **kwargs):
            captured["payload"] = payload
            return {"content": [{"type": "text", "text": "x"}]}

        with mock.patch.object(account_core, "http_json", side_effect=fake_http):
            accounts_claude.describe_images({"access": TOKEN}, "", [JPEG], max_tokens=0)
        self.assertEqual(captured["payload"]["max_tokens"], 700)

    def test_401_marks_expired(self):
        with mock.patch.object(
            account_core,
            "http_json",
            side_effect=account_core.AccountError("expired", "登录已过期，重新连接一次"),
        ):
            with mock.patch.object(account_core, "mark_expired") as mark:
                with self.assertRaises(account_core.AccountError) as ctx:
                    accounts_claude.describe_images({"access": TOKEN}, "", [JPEG])
        self.assertEqual(ctx.exception.code, "expired")
        mark.assert_called_once_with("claude")
        self.assertNotIn(TOKEN, str(ctx.exception))

    def test_empty_content_raises(self):
        with mock.patch.object(account_core, "http_json", return_value={"content": []}):
            with self.assertRaises(account_core.AccountError):
                accounts_claude.describe_images({"access": TOKEN}, "", [JPEG])

    def test_missing_access(self):
        with self.assertRaises(account_core.AccountError):
            accounts_claude.describe_images({}, "", [JPEG])


# ---------------------------------------------------------------------------
# Gemini 账号识图
# ---------------------------------------------------------------------------

class GeminiVisionTests(unittest.TestCase):
    def test_payload_and_text(self):
        captured = {}

        def fake_open(url, body, headers, timeout=30):
            captured["url"] = url
            captured["body"] = json.loads(body)
            captured["headers"] = headers
            captured["timeout"] = timeout
            return io.BytesIO(_sse(_gemini_text_chunk("一只白兔")))

        record = {"access": TOKEN, "extra": {"project": "proj-1"}}
        with mock.patch.object(accounts_gemini, "_open_stream", side_effect=fake_open):
            text = accounts_gemini.describe_images(
                record, "", [JPEG], prompt="看看", timeout=40, max_tokens=300
            )
        self.assertEqual(text, "一只白兔")
        self.assertIn(":streamGenerateContent", captured["url"])
        self.assertIn("alt=sse", captured["url"])
        payload = captured["body"]
        self.assertEqual(payload["model"], accounts_gemini.DEFAULT_MODEL)
        self.assertEqual(payload["project"], "proj-1")
        parts = payload["request"]["contents"][0]["parts"]
        self.assertEqual(parts[0], {"text": "看看"})
        inline = parts[1]["inlineData"]
        self.assertEqual(inline["mimeType"], "image/jpeg")
        self.assertEqual(base64.b64decode(inline["data"]), JPEG)
        self.assertEqual(payload["request"]["generationConfig"]["maxOutputTokens"], 300)
        self.assertEqual(captured["headers"]["Authorization"], "Bearer " + TOKEN)
        self.assertTrue(captured["headers"]["User-Agent"].startswith("GeminiCLI/"))

    def test_missing_project_is_setup_lazily(self):
        captured = {}

        def fake_open(url, body, headers, timeout=30):
            captured["body"] = json.loads(body)
            return io.BytesIO(_sse(_gemini_text_chunk("ok")))

        with mock.patch.object(accounts_gemini, "_open_stream", side_effect=fake_open):
            with mock.patch.object(accounts_gemini, "_setup_project", return_value=("lazy", None)) as setup:
                accounts_gemini.describe_images({"access": TOKEN}, "", [JPEG])
        setup.assert_called_once_with(TOKEN)
        self.assertEqual(captured["body"]["project"], "lazy")

    def test_401_maps_to_expired(self):
        with mock.patch.object(
            accounts_gemini,
            "_open_stream",
            side_effect=accounts_gemini.AccountError("expired", "登录已过期，重新连接一次"),
        ):
            with self.assertRaises(accounts_gemini.AccountError) as ctx:
                accounts_gemini.describe_images(
                    {"access": TOKEN, "extra": {"project": "p"}}, "", [JPEG]
                )
        self.assertEqual(ctx.exception.code, "expired")
        self.assertNotIn(TOKEN, str(ctx.exception))

    def test_empty_text_raises(self):
        with mock.patch.object(
            accounts_gemini, "_open_stream", side_effect=lambda *a, **k: io.BytesIO(b"")
        ):
            with self.assertRaises(accounts_gemini.AccountError):
                accounts_gemini.describe_images(
                    {"access": TOKEN, "extra": {"project": "p"}}, "", [JPEG]
                )

    def test_missing_access_and_bad_images(self):
        with self.assertRaises(accounts_gemini.AccountError):
            accounts_gemini.describe_images({}, "", [JPEG])
        with self.assertRaises(accounts_gemini.AccountError):
            accounts_gemini.describe_images({"access": TOKEN, "extra": {"project": "p"}}, "", [b""])


class ImageMimeTests(unittest.TestCase):
    def test_mime_detection(self):
        self.assertEqual(accounts_chatgpt._image_mime(PNG), "image/png")
        self.assertEqual(accounts_chatgpt._image_mime(JPEG), "image/jpeg")
        self.assertEqual(accounts_chatgpt._image_mime(b"GIF89a0000"), "image/gif")
        self.assertEqual(
            accounts_chatgpt._image_mime(b"RIFF\x00\x00\x00\x00WEBPdata"), "image/webp"
        )
        self.assertEqual(accounts_chatgpt._image_mime(b"unknown"), "image/jpeg")
        self.assertEqual(accounts_claude._image_mime(PNG), "image/png")
        self.assertEqual(accounts_gemini._image_mime(JPEG), "image/jpeg")


if __name__ == "__main__":
    unittest.main()
