"""shelltool / skills / dev_tools(/sh) 离线测试。"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent.parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import shelltool  # noqa: E402
import skills  # noqa: E402
from plugins import dev_tools  # noqa: E402

SELF_ID = 999


# ---------------------------------------------------------------- shelltool


class ShellToolTests(unittest.TestCase):
    def test_normal_command(self):
        result = shelltool.run_shell('printf "hello\\n"')
        self.assertNotIn("error", result)
        self.assertEqual(result["exit_code"], 0)
        self.assertFalse(result["timed_out"])
        self.assertIn("hello", result["stdout"])
        self.assertGreaterEqual(result["duration"], 0)

    def test_exit_code_and_stderr(self):
        result = shelltool.run_shell('echo oops >&2; exit 3')
        self.assertEqual(result["exit_code"], 3)
        self.assertIn("oops", result["stderr"])

    def test_timeout_kills_process(self):
        started = time.monotonic()
        result = shelltool.run_shell("sleep 10", timeout=1)
        elapsed = time.monotonic() - started
        self.assertTrue(result["timed_out"])
        self.assertLess(elapsed, 6)

    def test_output_truncated(self):
        result = shelltool.run_shell("head -c 300000 /dev/zero | tr '\\0' 'x'")
        self.assertLessEqual(
            len(result["stdout"].encode("utf-8")),
            shelltool.MAX_OUTPUT_BYTES + 64,
        )
        self.assertIn("截断", result["stdout"])

    def test_empty_command(self):
        result = shelltool.run_shell("   ")
        self.assertIn("error", result)

    def test_bad_cwd(self):
        result = shelltool.run_shell("pwd", cwd="/definitely/not/here")
        self.assertIn("error", result)
        self.assertIn("目录", result["error"])

    def test_default_cwd_is_repo_root(self):
        result = shelltool.run_shell("pwd")
        self.assertIn("qqbot", result["stdout"])

    def test_custom_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = shelltool.run_shell("pwd", cwd=tmp)
            self.assertEqual(result["stdout"].strip(), str(Path(tmp).resolve()))

    def test_env_extra_injected(self):
        result = shelltool.run_shell(
            'printf "%s" "$MY_TOKEN"', env_extra={"MY_TOKEN": "abc-123"}
        )
        self.assertEqual(result["stdout"], "abc-123")

    def test_timeout_clamped(self):
        self.assertEqual(shelltool._clamp_timeout(-5), shelltool.MIN_TIMEOUT)
        self.assertEqual(shelltool._clamp_timeout(99999), shelltool.MAX_TIMEOUT)
        self.assertEqual(shelltool._clamp_timeout("bad"), shelltool.DEFAULT_TIMEOUT)


# ---------------------------------------------------------------- skills


class SkillsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name) / "data"
        self.skills_dir = self.data_dir / "skills"
        self.skills_dir.mkdir(parents=True, exist_ok=True)

    def write(self, name: str, content: str) -> Path:
        path = self.skills_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_list_skills(self):
        self.write("deploy.md", "# 部署流程\n\n先构建再上传。")
        self.write("review.md", "代码审查\n关注安全。")
        self.write("ignore.txt", "不是 md")
        self.write("sub/nested.md", "子目录不算")
        items = skills.list_skills(self.data_dir)
        self.assertEqual([item["name"] for item in items], ["deploy", "review"])
        self.assertEqual(items[0]["description"], "部署流程")
        self.assertEqual(items[1]["description"], "代码审查")
        self.assertNotIn("content", items[0])

    def test_list_skills_missing_dir(self):
        self.assertEqual(skills.list_skills(Path(self._tmp.name) / "nope"), [])

    def test_read_skill(self):
        self.write("deploy.md", "# 部署流程\n\n先构建再上传。")
        content = skills.read_skill("deploy", self.data_dir)
        self.assertIn("先构建再上传", content)
        # 带 .md 也可以
        self.assertEqual(skills.read_skill("deploy.md", self.data_dir), content)

    def test_read_skill_rejects(self):
        self.write("deploy.md", "内容")
        self.assertIsNone(skills.read_skill("../deploy", self.data_dir))
        self.assertIsNone(skills.read_skill("..", self.data_dir))
        self.assertIsNone(skills.read_skill("/etc/passwd", self.data_dir))
        self.assertIsNone(skills.read_skill("deploy.txt", self.data_dir))
        self.assertIsNone(skills.read_skill("", self.data_dir))
        self.assertIsNone(skills.read_skill("missing", self.data_dir))

    def test_read_skill_rejects_symlink_outside(self):
        outside = Path(self._tmp.name) / "secret.md"
        outside.write_text("secret", encoding="utf-8")
        link = self.skills_dir / "evil.md"
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlink 不可用")
        self.assertIsNone(skills.read_skill("evil", self.data_dir))

    def test_too_large_skill_skipped(self):
        self.write("big.md", "x" * (skills.MAX_SKILL_BYTES + 10))
        self.assertEqual(skills.list_skills(self.data_dir), [])
        self.assertIsNone(skills.read_skill("big", self.data_dir))

    def test_skills_index_text(self):
        self.write("a.md", "# 甲\n内容")
        self.write("b.md", "第二行不是描述\n")
        text = skills.skills_index_text(self.data_dir)
        self.assertIn("- a — 甲", text)
        self.assertIn("- b — 第二行不是描述", text)


# ---------------------------------------------------------------- dev_tools 插件


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
        self.calls: list = []

    async def call(self, action, **params):
        self.calls.append((action, params))
        if action == "get_group_member_info":
            return dict(self.member)
        return {}


class FakeCtx:
    def __init__(self, root, cfg=None, admins=()):
        self.data_dir = Path(root)
        self.log = FakeLog()
        self.self_id = SELF_ID
        self.api = FakeApi()
        self._cfg = {"dev_tools": dict(cfg or {})}
        self._admins = [str(item) for item in admins]
        self.replies: list[str] = []

    def plugin_cfg(self, name: str) -> dict:
        return self._cfg.get(name) or {}

    def is_admin(self, user_id) -> bool:
        return str(user_id) in self._admins

    async def reply(self, event, message, at: bool = False):
        if isinstance(message, list):
            text = "".join(
                str((seg.get("data") or {}).get("text", ""))
                for seg in message
                if isinstance(seg, dict) and seg.get("type") == "text"
            )
        else:
            text = str(message or "")
        self.replies.append(text)
        return {"message_id": len(self.replies)}


def _event(text: str, *, private: bool = True, mention: bool = False, user_id: int = 20002) -> dict:
    segs = []
    if mention:
        segs.append({"type": "at", "data": {"qq": str(SELF_ID)}})
    if text:
        segs.append({"type": "text", "data": {"text": text}})
    event = {
        "post_type": "message",
        "message_type": "private" if private else "group",
        "user_id": user_id,
        "message": segs,
    }
    if not private:
        event["group_id"] = 12345
    return event


class DevToolsPluginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.ctx = FakeCtx(self.root, admins=[10001])
        self.plugin = dev_tools.Plugin(self.ctx)

    async def run_event(self, event) -> bool:
        return await self.plugin.on_event(event)

    def last_reply(self) -> str:
        self.assertTrue(self.ctx.replies, "插件没有回复")
        return self.ctx.replies[-1]

    async def test_private_sh(self):
        handled = await self.run_event(_event("/sh echo hi", private=True))
        self.assertTrue(handled)
        reply = self.last_reply()
        self.assertIn(dev_tools.RESULT_TITLE, reply)
        self.assertIn("退出码：0", reply)
        self.assertIn("hi", reply)

    async def test_sh_without_command(self):
        handled = await self.run_event(_event("/sh", private=True))
        self.assertTrue(handled)
        self.assertIn("用法", self.last_reply())

    async def test_non_command_ignored(self):
        self.assertFalse(await self.run_event(_event("你好", private=True)))

    async def test_disabled(self):
        self.ctx._cfg["dev_tools"]["enabled"] = False
        self.assertFalse(await self.run_event(_event("/sh echo hi", private=True)))

    async def test_group_member_denied(self):
        self.ctx.api.member = {"role": "member"}
        handled = await self.run_event(
            _event("/sh echo should-not-run", private=False, mention=True)
        )
        self.assertTrue(handled)
        self.assertEqual(self.last_reply(), dev_tools.PERMISSION_TEXT)
        self.assertNotIn("should-not-run", "".join(self.ctx.replies))

    async def test_group_owner_allowed(self):
        self.ctx.api.member = {"role": "owner"}
        await self.run_event(
            _event("/sh echo owner-ok", private=False, mention=True)
        )
        self.assertIn("owner-ok", self.last_reply())

    async def test_bot_admin_allowed_without_api_lookup(self):
        await self.run_event(
            _event("/sh echo admin-ok", private=False, mention=True, user_id=10001)
        )
        self.assertIn("admin-ok", self.last_reply())
        self.assertNotIn(
            "get_group_member_info", [call[0] for call in self.ctx.api.calls]
        )

    async def test_timeout_reply(self):
        self.ctx._cfg["dev_tools"]["timeout_seconds"] = 1
        await self.run_event(_event("/sh sleep 10", private=True))
        self.assertIn("超时", self.last_reply())

    async def test_reply_truncated(self):
        await self.run_event(
            _event(
                '/sh python3 -c "print(\'x\' * 5000)"',
                private=True,
            )
        )
        reply = self.last_reply()
        self.assertLessEqual(len(reply), dev_tools.MAX_REPLY_CHARS)
        self.assertIn("截断", reply)

    async def test_repo_path_used_as_cwd(self):
        repo = self.root / "repo"
        repo.mkdir()
        (repo / "marker.txt").write_text("marker-ok", encoding="utf-8")
        self.ctx._cfg["dev_tools"]["repo_path"] = str(repo)
        await self.run_event(_event("/sh cat marker.txt", private=True))
        self.assertIn("marker-ok", self.last_reply())

    async def test_bad_repo_path(self):
        self.ctx._cfg["dev_tools"]["repo_path"] = "/definitely/not/here"
        await self.run_event(_event("/sh echo hi", private=True))
        self.assertIn("目录", self.last_reply())

    async def test_git_token_in_env_not_logged(self):
        self.ctx._cfg["dev_tools"]["git_token"] = "SECRET-TOKEN-XYZ"
        await self.run_event(_event('/sh printf "%s" "$GITHUB_TOKEN"', private=True))
        self.assertIn("SECRET-TOKEN-XYZ", self.last_reply())
        self.assertNotIn(
            "SECRET-TOKEN-XYZ", "\n".join(self.ctx.log.records)
        )

    async def test_default_repo_root(self):
        await self.run_event(_event("/sh pwd", private=True))
        self.assertIn("qqbot", self.last_reply())


if __name__ == "__main__":
    unittest.main()
