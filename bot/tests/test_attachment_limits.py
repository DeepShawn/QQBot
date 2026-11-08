"""账号套餐模式取消附件大小限制 + media.fetch 支持「不限」的离线测试。

不联网：media.fetch 的 urlopen / ai_chat 的 media.fetch / providers.describe_images
都被 monkeypatch。
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BOT_DIR = Path(__file__).resolve().parent.parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import media  # noqa: E402
import providers  # noqa: E402
from plugins import ai_chat  # noqa: E402

JPEG = b"\xff\xd8\xff" + b"jpeg-body"


class _FakeLog:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass

    def exception(self, *args, **kwargs):
        pass


class FakeCtx:
    def __init__(self, cfg: dict, root: str):
        self._cfg = cfg
        self.data_dir = Path(root)
        self.log = _FakeLog()
        self.self_id = 10000
        self.bot_name = "测试机器人"

    def plugin_cfg(self, name: str) -> dict:
        return self._cfg

    def format_text(self, text, event, group_id=None) -> str:
        return text


# ---------------------------------------------------------------------------
# ai_chat：账号模式 / API Key 模式的附件大小上限
# ---------------------------------------------------------------------------

class AttachmentLimitTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _cfg(self, auth="", **att):
        base = {
            "enabled": True,
            "max_file_bytes": 15_000_000,
            "max_file_chars": 100_000,
        }
        base.update(att)
        return {"auth": auth, "api_key": "sk-test", "attachments": base}

    @staticmethod
    def _file_attachments():
        return [
            {
                "type": "file",
                "data": {"url": "https://example.com/a.txt", "file": "a.txt"},
                "quoted": False,
            }
        ]

    def _run_file(self, cfg):
        plugin = ai_chat.Plugin(FakeCtx(cfg, self._tmp.name))
        calls = []

        def fake_fetch(url, max_bytes, timeout=20.0, allowed_hosts=None):
            calls.append(max_bytes)
            return b"hello world", "text/plain"

        def fake_extract(data, name, ctype, max_chars):
            return {"kind": "text", "text": "文件内容", "chars": 4}

        with mock.patch.object(ai_chat.media, "fetch", side_effect=fake_fetch):
            with mock.patch.object(ai_chat.media, "extract_text", side_effect=fake_extract):
                context = asyncio.run(
                    plugin._attachment_context({}, self._file_attachments(), cfg)
                )
        return calls, context

    def test_account_mode_is_unlimited(self):
        calls, context = self._run_file(self._cfg(auth="chatgpt"))
        self.assertEqual(calls, [0])  # 0 = 不限，不会再被 15MB 卡住
        self.assertIn("文件内容", context)

    def test_api_key_mode_keeps_default_limit(self):
        calls, _ = self._run_file(self._cfg(auth=""))
        self.assertEqual(calls, [15_000_000])

    def test_api_key_mode_custom_limit(self):
        calls, _ = self._run_file(self._cfg(auth="", max_file_bytes=1_000_000))
        self.assertEqual(calls, [1_000_000])

    def test_account_mode_custom_limit_still_ignored(self):
        calls, _ = self._run_file(self._cfg(auth="claude", max_file_bytes=1_000_000))
        self.assertEqual(calls, [0])

    def test_ignore_size_limit_true_overrides_api_key_mode(self):
        calls, _ = self._run_file(self._cfg(auth="", ignore_size_limit=True))
        self.assertEqual(calls, [0])

    def test_ignore_size_limit_false_overrides_account_mode(self):
        calls, _ = self._run_file(self._cfg(auth="chatgpt", ignore_size_limit=False))
        self.assertEqual(calls, [15_000_000])

    def test_ignore_size_limit_string_values(self):
        calls, _ = self._run_file(self._cfg(auth="", ignore_size_limit="true"))
        self.assertEqual(calls, [0])
        calls, _ = self._run_file(self._cfg(auth="chatgpt", ignore_size_limit="false"))
        self.assertEqual(calls, [15_000_000])

    def test_blank_auth_is_api_key_mode(self):
        calls, _ = self._run_file(self._cfg(auth="   "))
        self.assertEqual(calls, [15_000_000])

    def test_invalid_max_file_bytes_falls_back_to_default(self):
        calls, _ = self._run_file(self._cfg(auth="", max_file_bytes="不是数字"))
        self.assertEqual(calls, [15_000_000])

    def test_disabled_attachments_skip_download(self):
        calls, context = self._run_file(self._cfg(auth="chatgpt", enabled=False))
        self.assertEqual(calls, [])
        self.assertEqual(context, "")

    def _run_image(self, cfg):
        plugin = ai_chat.Plugin(FakeCtx(cfg, self._tmp.name))
        fetch_calls = []
        describe_calls = []

        def fake_fetch(url, max_bytes, timeout=20.0, allowed_hosts=None):
            fetch_calls.append(max_bytes)
            return JPEG, "image/jpeg"

        def fake_describe(**kwargs):
            describe_calls.append(kwargs)
            return "图里是一只猫"

        attachments = [
            {"type": "image", "data": {"url": "https://example.com/i.jpg"}, "quoted": False}
        ]
        with mock.patch.object(ai_chat.media, "fetch", side_effect=fake_fetch):
            with mock.patch.object(
                ai_chat.providers, "describe_images", side_effect=fake_describe
            ):
                context = asyncio.run(plugin._attachment_context({}, attachments, cfg))
        return fetch_calls, describe_calls, context

    def test_image_account_mode_unlimited_and_uses_account(self):
        fetch_calls, describe_calls, context = self._run_image(self._cfg(auth="chatgpt"))
        self.assertEqual(fetch_calls, [0])
        self.assertEqual(describe_calls[0]["auth"], "chatgpt")
        self.assertIn("图里是一只猫", context)

    def test_image_api_key_mode_keeps_limit(self):
        fetch_calls, describe_calls, _ = self._run_image(self._cfg(auth=""))
        self.assertEqual(fetch_calls, [15_000_000])
        self.assertEqual(describe_calls[0]["auth"], "")

    def test_attachment_max_bytes_helper(self):
        plugin = ai_chat.Plugin(FakeCtx(self._cfg(), self._tmp.name))
        att = {"max_file_bytes": 2_000_000}
        self.assertEqual(plugin._attachment_max_bytes(att, {"auth": "gemini"}), 0)
        self.assertEqual(plugin._attachment_max_bytes(att, {"auth": ""}), 2_000_000)
        self.assertEqual(
            plugin._attachment_max_bytes({**att, "ignore_size_limit": True}, {"auth": ""}), 0
        )
        self.assertEqual(
            plugin._attachment_max_bytes({**att, "ignore_size_limit": False}, {"auth": "claude"}),
            2_000_000,
        )


# ---------------------------------------------------------------------------
# media.fetch：max_bytes <= 0 表示不限
# ---------------------------------------------------------------------------

class _FakeResponse:
    status = 200

    def __init__(self, chunks, headers):
        self._chunks = list(chunks)
        self.headers = headers

    def read(self, size=-1):
        if not self._chunks:
            return b""
        return self._chunks.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class _FakeOpener:
    def __init__(self, response):
        self._response = response

    def open(self, request, timeout=None):
        return self._response


class MediaFetchLimitTests(unittest.TestCase):
    def _fetch(self, chunks, headers, **kwargs):
        response = _FakeResponse(chunks, headers)
        with mock.patch.object(
            media.urllib.request, "build_opener", side_effect=lambda *a, **k: _FakeOpener(response)
        ):
            return media.fetch("https://example.com/file.bin", **kwargs)

    def test_zero_max_bytes_ignores_content_length(self):
        data, ctype = self._fetch(
            [b"a" * 100, b"b" * 100],
            {"Content-Type": "application/octet-stream", "Content-Length": "999999999"},
            max_bytes=0,
        )
        self.assertEqual(len(data), 200)
        self.assertEqual(ctype, "application/octet-stream")

    def test_negative_max_bytes_is_unlimited(self):
        data, _ = self._fetch([b"x" * 1000], {"Content-Length": "500000000"}, max_bytes=-1)
        self.assertEqual(len(data), 1000)

    def test_zero_max_bytes_reads_all_chunks(self):
        chunks = [b"x" * (64 * 1024)] * 4
        data, _ = self._fetch(chunks, {}, max_bytes=0)
        self.assertEqual(len(data), 4 * 64 * 1024)

    def test_positive_limit_still_rejects_content_length(self):
        with self.assertRaises(media.MediaError) as ctx:
            self._fetch([b"x"], {"Content-Length": "1000"}, max_bytes=10)
        self.assertIn("大小上限", str(ctx.exception))

    def test_positive_limit_still_rejects_cumulative_size(self):
        with self.assertRaises(media.MediaError) as ctx:
            self._fetch([b"x" * 8, b"y" * 8], {}, max_bytes=10)
        self.assertIn("大小上限", str(ctx.exception))

    def test_default_limit_unchanged(self):
        self.assertEqual(media.DEFAULT_MAX_BYTES, 20_000_000)
        with self.assertRaises(media.MediaError):
            self._fetch(
                [b"x"],
                {"Content-Length": str(media.DEFAULT_MAX_BYTES + 1)},
            )

    def test_exact_limit_is_allowed(self):
        data, _ = self._fetch([b"x" * 10], {"Content-Length": "10"}, max_bytes=10)
        self.assertEqual(len(data), 10)

    def test_zero_max_bytes_still_validates_timeout(self):
        with self.assertRaises(media.MediaError):
            self._fetch([b"x"], {}, max_bytes=0, timeout=0)


if __name__ == "__main__":
    unittest.main()
