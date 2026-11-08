"""python_run 插件离线测试（用真实 sys.executable 跑简单代码，不联网）。"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent.parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import media  # noqa: E402
from plugins import python_run  # noqa: E402

SELF_ID = 999


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


class FakeApi:
    def __init__(self):
        self.member = {"role": "member"}
        self.quoted = None
        self.fail_member = False
        self.fail_get_msg = False
        self.calls: list = []

    async def call(self, action, **params):
        self.calls.append((action, params))
        if action == "get_group_member_info":
            if self.fail_member:
                raise RuntimeError("member lookup failed")
            return dict(self.member)
        if action == "get_msg":
            if self.fail_get_msg:
                raise RuntimeError("get_msg failed")
            return dict(self.quoted or {})
        return {}


class FakeCtx:
    def __init__(self, root, admins=(), cfg=None, self_id=SELF_ID):
        self.data_dir = Path(root)
        self.log = FakeLog()
        self.self_id = self_id
        self.api = FakeApi()
        self._admins = [str(item) for item in admins]
        self._cfg = {"python_run": dict(cfg or {})}
        self.replies: list[str] = []

    def plugin_cfg(self, name: str) -> dict:
        return self._cfg.get(name) or {}

    def is_admin(self, user_id) -> bool:
        return str(user_id) in self._admins

    async def reply(self, event, message, at: bool = False):
        self.replies.append(python_run._message_text(message))
        return {"message_id": len(self.replies)}

    def format_text(self, template, event, **extra):
        return str(template or "")


class PluginTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.files = self.root / "files"
        self.files.mkdir(parents=True, exist_ok=True)
        self.ctx = FakeCtx(self.root, admins=[10001])
        self.plugin = python_run.Plugin(self.ctx)
        self._media_calls: list = []

    # ---------------- 工具 ----------------
    def set_cfg(self, **cfg):
        self.ctx._cfg["python_run"].update(cfg)

    def patch_media(self, raw: bytes = b"", error: Exception | None = None):
        calls = self._media_calls

        def fake_fetch(url, max_bytes=None, timeout=None, **kwargs):
            calls.append((url, max_bytes, timeout))
            if error is not None:
                raise error
            return raw, "text/plain"

        original = media.fetch
        media.fetch = fake_fetch
        self.addCleanup(setattr, media, "fetch", original)

    @staticmethod
    def event(
        text: str = "",
        *,
        private: bool = False,
        mention: bool = False,
        reply_id=None,
        user_id: int = 10001,
        group_id: int = 12345,
        extra_segments=(),
    ):
        segs = []
        if mention:
            segs.append({"type": "at", "data": {"qq": str(SELF_ID)}})
        if reply_id is not None:
            segs.append({"type": "reply", "data": {"id": str(reply_id)}})
        segs.extend(extra_segments)
        if text:
            segs.append({"type": "text", "data": {"text": text}})
        event = {
            "post_type": "message",
            "message_type": "private" if private else "group",
            "user_id": user_id,
            "message": segs,
        }
        if not private:
            event["group_id"] = group_id
        return event

    async def run_plugin(self, event) -> bool:
        return await self.plugin.on_event(event)

    def last_reply(self) -> str:
        self.assertTrueReplies()
        return self.ctx.replies[-1]

    def assertTrueReplies(self):
        if not self.ctx.replies:
            self.fail("插件没有回复任何消息")

    def write_file(self, name: str, code: str) -> Path:
        path = self.files / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(code, encoding="utf-8")
        return path


class CommandTests(PluginTestCase):
    async def test_py_private_runs_code(self):
        handled = await self.run_plugin(
            self.event('/py print("你好，世界")', private=True)
        )
        self.assertTrue(handled)
        reply = self.last_reply()
        self.assertIn(python_run.RESULT_TITLE, reply)
        self.assertIn("你好，世界", reply)
        self.assertIn("退出码：0", reply)

    async def test_py_multiline_code(self):
        code = "/py\na = 1\nb = 2\nprint(a + b)"
        handled = await self.run_plugin(self.event(code, private=True))
        self.assertTrue(handled)
        self.assertIn("3", self.last_reply())

    async def test_py_inline_code_block(self):
        handled = await self.run_plugin(
            self.event('/py\n```python\nprint("来自代码块")\n```', private=True)
        )
        self.assertTrue(handled)
        self.assertIn("来自代码块", self.last_reply())

    async def test_run_saved_file(self):
        self.write_file("hello.py", "print('file ok')")
        handled = await self.run_plugin(self.event("/run hello.py", private=True))
        self.assertTrue(handled)
        reply = self.last_reply()
        self.assertIn("file ok", reply)
        self.assertIn("退出码：0", reply)

    async def test_run_partial_filename_match(self):
        self.write_file("hello_world.py", "print('partial ok')")
        await self.run_plugin(self.event("/run hello", private=True))
        self.assertIn("partial ok", self.last_reply())

    async def test_run_subdirectory_file(self):
        self.write_file("sub/inner.py", "print('sub ok')")
        await self.run_plugin(self.event("/run sub/inner.py", private=True))
        self.assertIn("sub ok", self.last_reply())

    async def test_run_out_of_bounds_rejected(self):
        outside = self.root / "outside.py"
        outside.write_text("print('should not run')", encoding="utf-8")
        handled = await self.run_plugin(self.event("/run ../outside.py", private=True))
        self.assertTrue(handled)
        reply = self.last_reply()
        self.assertIn("data/files", reply)
        self.assertNotIn("should not run", reply)

    async def test_run_absolute_outside_rejected(self):
        handled = await self.run_plugin(self.event("/run /etc/passwd", private=True))
        self.assertTrue(handled)
        self.assertIn("data/files", self.last_reply())

    async def test_run_missing_file(self):
        handled = await self.run_plugin(self.event("/run nope.py", private=True))
        self.assertTrue(handled)
        self.assertIn("没找到", self.last_reply())

    async def test_run_without_args_is_usage(self):
        handled = await self.run_plugin(self.event("/run", private=True))
        self.assertTrue(handled)
        self.assertIn("/py", self.last_reply())

    async def test_py_without_code_is_usage(self):
        handled = await self.run_plugin(self.event("/py", private=True))
        self.assertTrue(handled)
        self.assertIn("/run", self.last_reply())

    async def test_py_command_with_quoted_code_block(self):
        self.ctx.api.quoted = {
            "message": [
                {
                    "type": "text",
                    "data": {"text": "看这个\n```python\nprint('quoted for py')\n```"},
                }
            ]
        }
        handled = await self.run_plugin(self.event("/py", private=True, reply_id=5))
        self.assertTrue(handled)
        self.assertIn("quoted for py", self.last_reply())


class PermissionTests(PluginTestCase):
    async def test_private_non_admin_allowed(self):
        handled = await self.run_plugin(
            self.event('/py print("private ok")', private=True, user_id=20002)
        )
        self.assertTrue(handled)
        self.assertIn("private ok", self.last_reply())

    async def test_group_non_admin_denied(self):
        self.ctx.api.member = {"role": "member"}
        handled = await self.run_plugin(
            self.event('/py print("nope")', mention=True, user_id=20002)
        )
        self.assertTrue(handled)
        self.assertEqual(self.last_reply(), python_run.PERMISSION_TEXT)
        self.assertNotIn("nope", "".join(self.ctx.replies))

    async def test_group_owner_allowed(self):
        self.ctx.api.member = {"role": "owner"}
        handled = await self.run_plugin(
            self.event('/py print("owner ok")', mention=True, user_id=20002)
        )
        self.assertTrue(handled)
        self.assertIn("owner ok", self.last_reply())

    async def test_group_admin_role_allowed(self):
        self.ctx.api.member = {"role": "admin"}
        await self.run_plugin(
            self.event('/py print("admin ok")', mention=True, user_id=20002)
        )
        self.assertIn("admin ok", self.last_reply())

    async def test_bot_admin_allowed_without_api_lookup(self):
        await self.run_plugin(
            self.event('/py print("bot admin ok")', mention=True, user_id=10001)
        )
        self.assertIn("bot admin ok", self.last_reply())
        self.assertNotIn(
            "get_group_member_info", [call[0] for call in self.ctx.api.calls]
        )

    async def test_group_admin_command_without_mention_allowed(self):
        await self.run_plugin(
            self.event('/py print("no at ok")', user_id=10001)
        )
        self.assertIn("no at ok", self.last_reply())

    async def test_group_non_admin_command_without_mention_denied(self):
        self.ctx.api.member = {"role": "member"}
        handled = await self.run_plugin(
            self.event('/py print("nope")', user_id=20002)
        )
        self.assertTrue(handled)
        self.assertEqual(self.last_reply(), python_run.PERMISSION_TEXT)

    async def test_member_lookup_failure_denied(self):
        self.ctx.api.fail_member = True
        handled = await self.run_plugin(
            self.event('/py print("denied")', mention=True, user_id=20002)
        )
        self.assertTrue(handled)
        self.assertEqual(self.last_reply(), python_run.PERMISSION_TEXT)


class QuotedMessageTests(PluginTestCase):
    async def test_quoted_code_block_natural_language(self):
        self.ctx.api.quoted = {
            "message": [
                {
                    "type": "text",
                    "data": {"text": "代码：\n```python\nprint('from quote')\n```"},
                }
            ]
        }
        handled = await self.run_plugin(self.event("运行", private=True, reply_id=7))
        self.assertTrue(handled)
        self.assertIn("from quote", self.last_reply())

    async def test_quoted_py_file_downloaded_and_run(self):
        self.patch_media(b"print('downloaded ok')")
        self.ctx.api.quoted = {
            "message": [
                {
                    "type": "file",
                    "data": {
                        "file": "remote.py",
                        "url": "https://example.com/remote.py",
                        "file_size": "20",
                    },
                }
            ]
        }
        handled = await self.run_plugin(self.event("/run", private=True, reply_id=9))
        self.assertTrue(handled)
        self.assertIn("downloaded ok", self.last_reply())
        self.assertTrue((self.files / "remote.py").exists())
        self.assertEqual(self._media_calls[0][0], "https://example.com/remote.py")
        self.assertEqual(self._media_calls[0][1], python_run.MAX_DOWNLOAD_BYTES)

    async def test_quoted_py_file_too_large_rejected(self):
        self.patch_media(b"print('x')")
        self.ctx.api.quoted = {
            "message": [
                {
                    "type": "file",
                    "data": {
                        "file": "big.py",
                        "url": "https://example.com/big.py",
                        "file_size": str(300 * 1024),
                    },
                }
            ]
        }
        handled = await self.run_plugin(self.event("运行", private=True, reply_id=1))
        self.assertTrue(handled)
        self.assertIn("太大", self.last_reply())
        self.assertEqual(self._media_calls, [])

    async def test_quoted_py_file_without_url(self):
        self.ctx.api.quoted = {
            "message": [{"type": "file", "data": {"file": "nourl.py"}}]
        }
        await self.run_plugin(self.event("运行", private=True, reply_id=1))
        self.assertIn("下载地址", self.last_reply())

    async def test_quoted_file_not_utf8(self):
        self.patch_media(b"\xff\xfe\x00bad")
        self.ctx.api.quoted = {
            "message": [
                {
                    "type": "file",
                    "data": {
                        "file": "gbk.py",
                        "url": "https://example.com/gbk.py",
                        "file_size": "10",
                    },
                }
            ]
        }
        await self.run_plugin(self.event("运行", private=True, reply_id=1))
        self.assertIn("UTF-8", self.last_reply())

    async def test_download_failure_friendly(self):
        self.patch_media(error=media.MediaError("boom"))
        self.ctx.api.quoted = {
            "message": [
                {
                    "type": "file",
                    "data": {
                        "file": "x.py",
                        "url": "https://example.com/x.py",
                        "file_size": "10",
                    },
                }
            ]
        }
        await self.run_plugin(self.event("运行", private=True, reply_id=1))
        self.assertIn("下载", self.last_reply())

    async def test_group_quoted_code_without_mention_still_permission_checked(self):
        self.ctx.api.quoted = {
            "message": [{"type": "text", "data": {"text": "```python\nprint(1)\n```"}}]
        }
        self.ctx.api.member = {"role": "member"}
        handled = await self.run_plugin(
            self.event("运行", reply_id=3, user_id=20002)
        )
        self.assertTrue(handled)
        self.assertEqual(self.last_reply(), python_run.PERMISSION_TEXT)

    async def test_group_quoted_code_owner_executes_without_mention(self):
        self.ctx.api.quoted = {
            "message": [
                {"type": "text", "data": {"text": "```python\nprint('noadmin ok')\n```"}}
            ]
        }
        self.ctx.api.member = {"role": "owner"}
        handled = await self.run_plugin(
            self.event("运行", reply_id=3, user_id=20002)
        )
        self.assertTrue(handled)
        self.assertIn("noadmin ok", self.last_reply())

    async def test_non_python_code_block_ignored(self):
        self.ctx.api.quoted = {
            "message": [{"type": "text", "data": {"text": "```bash\necho hi\n```"}}]
        }
        handled = await self.run_plugin(self.event("运行", private=True, reply_id=1))
        self.assertFalse(handled)

    async def test_get_msg_failure_falls_through(self):
        self.ctx.api.fail_get_msg = True
        handled = await self.run_plugin(
            self.event("运行", private=True, reply_id=1)
        )
        self.assertFalse(handled)


class OutputTests(PluginTestCase):
    async def test_stderr_and_nonzero_exit(self):
        code = '/py\nimport sys\nsys.stderr.write("boom\\n")\nsys.exit(3)'
        await self.run_plugin(self.event(code, private=True))
        reply = self.last_reply()
        self.assertIn("退出码：3", reply)
        self.assertIn("错误输出", reply)
        self.assertIn("boom", reply)

    async def test_timeout(self):
        self.set_cfg(timeout_seconds=0.3)
        started = time.monotonic()
        handled = await self.run_plugin(
            self.event('/py\nimport time\ntime.sleep(5)\nprint("never")', private=True)
        )
        elapsed = time.monotonic() - started
        self.assertTrue(handled)
        reply = self.last_reply()
        self.assertIn("超时", reply)
        self.assertNotIn("never", reply)
        self.assertLess(elapsed, 5.0)

    async def test_long_output_truncated(self):
        await self.run_plugin(self.event('/py print("x" * 5000)', private=True))
        reply = self.last_reply()
        self.assertLessEqual(len(reply), python_run.MAX_REPLY_CHARS)
        self.assertIn("截断", reply)

    async def test_empty_output(self):
        await self.run_plugin(self.event("/py pass", private=True))
        self.assertIn("没有输出", self.last_reply())

    async def test_stderr_and_stdout_both_shown(self):
        code = '/py\nprint("out-line")\nimport sys\nsys.stderr.write("err-line\\n")'
        await self.run_plugin(self.event(code, private=True))
        reply = self.last_reply()
        self.assertIn("out-line", reply)
        self.assertIn("err-line", reply)


class TriggerTests(PluginTestCase):
    async def test_usage_hint_group_mentioned(self):
        handled = await self.run_plugin(
            self.event("你不是可以在本地运行 py 吗", mention=True, user_id=20002)
        )
        self.assertTrue(handled)
        reply = self.last_reply()
        self.assertIn("用法", reply)
        self.assertIn("/py", reply)

    async def test_usage_hint_private(self):
        handled = await self.run_plugin(
            self.event("你能运行 python 代码吗", private=True)
        )
        self.assertTrue(handled)
        self.assertIn("用法", self.last_reply())

    async def test_group_unaddressed_not_hijacked(self):
        handled = await self.run_plugin(
            self.event("我觉得运行 python 很慢", user_id=20002)
        )
        self.assertFalse(handled)
        self.assertEqual(self.ctx.replies, [])

    async def test_chitchat_not_hijacked(self):
        handled = await self.run_plugin(self.event("今天跑了五公里", private=True))
        self.assertFalse(handled)

    async def test_natural_language_filename(self):
        self.write_file("hello.py", "print('natural ok')")
        handled = await self.run_plugin(
            self.event("帮我运行 hello.py", private=True)
        )
        self.assertTrue(handled)
        self.assertIn("natural ok", self.last_reply())

    async def test_group_natural_language_without_mention_ignored(self):
        self.write_file("hello.py", "print('nope')")
        handled = await self.run_plugin(
            self.event("帮我运行 hello.py", user_id=20002)
        )
        self.assertFalse(handled)

    async def test_non_message_event(self):
        handled = await self.run_plugin({"post_type": "meta_event"})
        self.assertFalse(handled)

    async def test_disabled_config(self):
        self.set_cfg(enabled=False)
        handled = await self.run_plugin(self.event('/py print("x")', private=True))
        self.assertFalse(handled)


class TryTriggerTests(PluginTestCase):
    async def test_try_runs_recent_file(self):
        self.write_file("recent.py", "print('recent ok')")
        handled = await self.run_plugin(self.event("你再试试", private=True))
        self.assertTrue(handled)
        self.assertEqual(len(self.ctx.replies), 2)
        self.assertIn("运行最近的代码文件：recent.py", self.ctx.replies[0])
        self.assertIn("recent ok", self.ctx.replies[1])

    async def test_try_runs_newest_file(self):
        self.write_file("older.py", "print('older ran')")
        newest = self.write_file("newest.py", "print('newest ran')")
        import os

        now = time.time()
        os.utime(self.files / "older.py", (now - 120, now - 120))
        os.utime(newest, (now, now))
        handled = await self.run_plugin(self.event("跑一下", private=True))
        self.assertTrue(handled)
        self.assertIn("newest.py", self.ctx.replies[0])
        self.assertIn("newest ran", self.ctx.replies[-1])

    async def test_try_without_recent_file_does_not_run(self):
        old_file = self.write_file("old.py", "print('old ran')")
        old_time = time.time() - 3600
        import os

        os.utime(old_file, (old_time, old_time))
        handled = await self.run_plugin(self.event("再试试", private=True))
        self.assertFalse(handled)
        self.assertEqual(self.ctx.replies, [])

    async def test_try_without_any_file_does_not_run(self):
        handled = await self.run_plugin(self.event("试试看", private=True))
        self.assertFalse(handled)
        self.assertEqual(self.ctx.replies, [])

    async def test_try_group_without_mention_ignored(self):
        self.write_file("recent.py", "print('nope')")
        handled = await self.run_plugin(self.event("再试试", user_id=20002))
        self.assertFalse(handled)

    async def test_try_group_owner_runs_recent(self):
        self.write_file("recent.py", "print('group try ok')")
        self.ctx.api.member = {"role": "owner"}
        handled = await self.run_plugin(
            self.event("你再试试", mention=True, user_id=20002)
        )
        self.assertTrue(handled)
        self.assertIn("运行最近的代码文件", self.ctx.replies[0])
        self.assertIn("group try ok", self.ctx.replies[-1])

    async def test_try_group_non_admin_denied(self):
        self.write_file("recent.py", "print('nope')")
        self.ctx.api.member = {"role": "member"}
        handled = await self.run_plugin(
            self.event("你再试试", mention=True, user_id=20002)
        )
        self.assertTrue(handled)
        self.assertEqual(self.last_reply(), python_run.PERMISSION_TEXT)
        self.assertEqual(len(self.ctx.replies), 1)

    async def test_all_try_words_trigger(self):
        for word in (
            "再试试",
            "试试",
            "试一下",
            "试下",
            "试一次",
            "跑一下",
            "跑下",
            "运行一下",
            "执行一下",
        ):
            with self.subTest(word=word):
                self.write_file("recent.py", "print('ok')")
                self.ctx.replies.clear()
                handled = await self.run_plugin(self.event(word, private=True))
                self.assertTrue(handled, word)
                self.assertIn("运行最近的代码文件", self.ctx.replies[0], word)


class QuotedFilenameTests(PluginTestCase):
    SAVED_NAME = "代码_1108_120000.py"

    def _quoted_text(self, text: str):
        self.ctx.api.quoted = {"message": [{"type": "text", "data": {"text": text}}]}

    async def test_quoted_text_filename_runs(self):
        self.write_file(self.SAVED_NAME, "print('quoted filename ok')")
        self._quoted_text(f"📎 完整代码已保存成文件发出：{self.SAVED_NAME}")
        handled = await self.run_plugin(self.event("运行", private=True, reply_id=1))
        self.assertTrue(handled)
        self.assertIn("quoted filename ok", self.last_reply())

    async def test_quoted_text_filename_with_try_trigger(self):
        self.write_file(self.SAVED_NAME, "print('try quoted ok')")
        self._quoted_text(f"📎 完整代码已保存成文件发出：{self.SAVED_NAME}")
        handled = await self.run_plugin(self.event("你再试试", private=True, reply_id=1))
        self.assertTrue(handled)
        self.assertIn("try quoted ok", self.last_reply())

    async def test_run_command_with_quoted_filename(self):
        self.write_file(self.SAVED_NAME, "print('run quoted ok')")
        self._quoted_text(f"文件：{self.SAVED_NAME}")
        handled = await self.run_plugin(self.event("/run", private=True, reply_id=1))
        self.assertTrue(handled)
        self.assertIn("run quoted ok", self.last_reply())

    async def test_group_quoted_filename_without_mention_executes(self):
        self.write_file(self.SAVED_NAME, "print('group quoted ok')")
        self._quoted_text(f"文件：{self.SAVED_NAME}")
        self.ctx.api.member = {"role": "owner"}
        handled = await self.run_plugin(
            self.event("运行", reply_id=1, user_id=20002)
        )
        self.assertTrue(handled)
        self.assertIn("group quoted ok", self.last_reply())

    async def test_group_quoted_filename_non_admin_denied(self):
        self.write_file(self.SAVED_NAME, "print('nope')")
        self._quoted_text(f"文件：{self.SAVED_NAME}")
        self.ctx.api.member = {"role": "member"}
        handled = await self.run_plugin(
            self.event("运行", reply_id=1, user_id=20002)
        )
        self.assertTrue(handled)
        self.assertEqual(self.last_reply(), python_run.PERMISSION_TEXT)

    async def test_quoted_filename_not_existing_ignored(self):
        self._quoted_text("看这个 not_exists.py 吧")
        handled = await self.run_plugin(self.event("运行", private=True, reply_id=1))
        self.assertFalse(handled)

    async def test_init_logs_loaded(self):
        self.assertTrue(
            any("python_run 已加载" in item for item in self.ctx.log.records)
        )
        self.assertTrue(
            any("/py" in item and "/run" in item for item in self.ctx.log.records)
        )


class AiChatPromptHintTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ctx = FakeCtx(self._tmp.name)

    def test_hint_when_python_run_enabled(self):
        from plugins import ai_chat

        hint = ai_chat.local_python_hint(self.ctx)
        self.assertIn("本地 Python", hint)
        self.assertIn("/py", hint)
        self.assertIn("没有工具", hint)

    def test_hint_empty_when_disabled(self):
        from plugins import ai_chat

        self.ctx._cfg["python_run"] = {"enabled": False}
        self.assertEqual(ai_chat.local_python_hint(self.ctx), "")


class AiChatPromptIntegrationTests(PluginTestCase):
    async def test_system_prompt_contains_local_python_hint(self):
        from plugins import ai_chat

        self.ctx._cfg["ai_chat"] = {
            "enabled": True,
            "provider": "openai",
            "base_url": "",
            "api_key": "key",
            "model": "m",
            "temperature": 0.7,
            "max_tokens": 100,
            "timeout_seconds": 10,
            "system_prompt": "你是助手",
            "trigger": {"private": True},
        }
        plugin = ai_chat.Plugin(self.ctx)
        captured: dict = {}
        original = ai_chat.providers.chat

        def fake_chat(**kwargs):
            captured.update(kwargs)
            return "好的"

        ai_chat.providers.chat = fake_chat
        self.addCleanup(setattr, ai_chat.providers, "chat", original)
        handled = await plugin.on_event(self.event("再试试", private=True))
        self.assertTrue(handled)
        system = captured["messages"][0]
        self.assertEqual(system["role"], "system")
        self.assertIn("你是助手", system["content"])
        self.assertIn("本地 Python", system["content"])

    async def test_system_prompt_without_hint_when_disabled(self):
        from plugins import ai_chat

        self.ctx._cfg["python_run"] = {"enabled": False}
        self.ctx._cfg["ai_chat"] = {
            "enabled": True,
            "provider": "openai",
            "base_url": "",
            "api_key": "key",
            "model": "m",
            "temperature": 0.7,
            "max_tokens": 100,
            "timeout_seconds": 10,
            "system_prompt": "你是助手",
            "trigger": {"private": True},
        }
        plugin = ai_chat.Plugin(self.ctx)
        captured: dict = {}
        original = ai_chat.providers.chat

        def fake_chat(**kwargs):
            captured.update(kwargs)
            return "好的"

        ai_chat.providers.chat = fake_chat
        self.addCleanup(setattr, ai_chat.providers, "chat", original)
        handled = await plugin.on_event(self.event("你好", private=True))
        self.assertTrue(handled)
        self.assertEqual(captured["messages"][0]["content"], "你是助手")


class HelperTests(unittest.TestCase):
    def test_python_blocks_filtering(self):
        text = (
            "```bash\necho hi\n```\n"
            "```python\nprint(1)\n```\n"
            "```py\nprint(2)\n```\n"
        )
        blocks = python_run._python_blocks(text)
        self.assertEqual(len(blocks), 2)
        self.assertIn("print(1)", blocks[0]["code"])
        self.assertIn("print(2)", blocks[1]["code"])

    def test_plain_blocks_only_when_allowed(self):
        text = "```\nprint(3)\n```"
        self.assertEqual(python_run._python_blocks(text), [])
        self.assertEqual(len(python_run._python_blocks(text, allow_plain=True)), 1)

    def test_truncate_keeps_suffix_within_limit(self):
        text, cut = python_run._truncate("x" * 100, 40)
        self.assertTrue(cut)
        self.assertLessEqual(len(text), 40)
        self.assertIn("截断", text)

    def test_safe_download_name(self):
        self.assertEqual(python_run._safe_download_name("a/b.py"), "b.py")
        self.assertEqual(python_run._safe_download_name("evil"), "evil.py")
        self.assertTrue(python_run._safe_download_name("x.txt").endswith(".py"))

    def test_within(self):
        base = Path("/tmp/data/files")
        self.assertTrue(python_run._is_within(base / "a.py", base))
        self.assertFalse(python_run._is_within(base / ".." / "secret.py", base))

    def test_plugin_priority_and_registration(self):
        self.assertEqual(python_run.Plugin.priority, 15)
        import plugins  # noqa: F401  （包内 import python_run 后不再报错）
        self.assertTrue(hasattr(plugins, "python_run"))
        main_source = (BOT_DIR / "main.py").read_text(encoding="utf-8")
        self.assertIn("python_run", main_source)

    def test_timeout_clamped(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        plugin = python_run.Plugin.__new__(python_run.Plugin)
        ctx = FakeCtx(tmp.name)
        plugin.ctx = ctx
        ctx._cfg["python_run"]["timeout_seconds"] = "bad"
        self.assertEqual(plugin._timeout(), python_run.DEFAULT_TIMEOUT)
        ctx._cfg["python_run"]["timeout_seconds"] = 0
        self.assertEqual(plugin._timeout(), python_run.MIN_TIMEOUT)
        ctx._cfg["python_run"]["timeout_seconds"] = 999
        self.assertEqual(plugin._timeout(), python_run.MAX_TIMEOUT)


if __name__ == "__main__":
    unittest.main()
