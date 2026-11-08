#!/usr/bin/env python3
"""bot/updater.py 的单元测试：不联网、不碰真实仓库。"""
from __future__ import annotations

import io
import json
import sys
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path, PurePosixPath
from unittest import mock

BOT_DIR = Path(__file__).resolve().parent.parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import updater  # noqa: E402


def make_tar(path: Path, files=None, symlinks=None, dirs=None) -> Path:
    """造一个 tar.gz。files: {name: 内容}；symlinks: {name: 目标}。"""
    with tarfile.open(path, "w:gz") as tf:
        for name in dirs or []:
            info = tarfile.TarInfo(name)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            tf.addfile(info)
        for name, data in (files or {}).items():
            raw = data.encode("utf-8") if isinstance(data, str) else data
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            info.mode = 0o644
            tf.addfile(info, io.BytesIO(raw))
        for name, target in (symlinks or {}).items():
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            info.mode = 0o777
            tf.addfile(info)
    return path


class ExtractSafetyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.dest = self.root / "out"

    def tearDown(self):
        self.tmp.cleanup()

    def test_extract_normal_archive(self):
        tar = make_tar(self.root / "a.tar.gz", files={
            "QQBot-main/bot/main.py": "print('hi')\n",
            "QQBot-main/bot/admin/server.py": "# server\n",
            "QQBot-main/start-all.sh": "#!/bin/bash\n",
        })
        updater.safe_extract(tar, self.dest)
        self.assertEqual((self.dest / "QQBot-main" / "bot" / "main.py").read_text(), "print('hi')\n")
        self.assertEqual(updater._find_source_root(self.dest).name, "QQBot-main")

    def test_reject_parent_traversal(self):
        tar = make_tar(self.root / "evil.tar.gz", files={"QQBot-main/../../evil.txt": "x"})
        with self.assertRaises(updater.UpdaterError):
            updater.safe_extract(tar, self.dest)
        self.assertFalse((self.root / "evil.txt").exists())

    def test_reject_absolute_path(self):
        tar = make_tar(self.root / "abs.tar.gz", files={"/tmp/evil.txt": "x"})
        with self.assertRaises(updater.UpdaterError):
            updater.safe_extract(tar, self.dest)

    def test_reject_escaping_symlink(self):
        tar = make_tar(self.root / "sym.tar.gz",
                       files={"QQBot-main/bot/main.py": "x"},
                       symlinks={"QQBot-main/escape": "../../etc/passwd"})
        with self.assertRaises(updater.UpdaterError):
            updater.safe_extract(tar, self.dest)

    def test_reject_absolute_symlink(self):
        tar = make_tar(self.root / "sym2.tar.gz",
                       symlinks={"QQBot-main/etc": "/etc/passwd"})
        with self.assertRaises(updater.UpdaterError):
            updater.safe_extract(tar, self.dest)

    def test_allow_internal_symlink(self):
        tar = make_tar(self.root / "ok.tar.gz",
                       files={"QQBot-main/bot/main.py": "x"},
                       symlinks={"QQBot-main/bot/link.py": "main.py"})
        updater.safe_extract(tar, self.dest)
        self.assertTrue((self.dest / "QQBot-main" / "bot" / "link.py").is_symlink())

    def test_empty_or_bad_archive(self):
        bad = self.root / "bad.tar.gz"
        bad.write_bytes(b"not a tar")
        with self.assertRaises(updater.UpdaterError):
            updater.safe_extract(bad, self.dest)

    def test_find_source_root_multiple_entries(self):
        d = self.root / "multi"
        (d / "a").mkdir(parents=True)
        (d / "b.txt").write_text("x")
        self.assertEqual(updater._find_source_root(d), d)


class IgnoreRulesTest(unittest.TestCase):
    def test_user_paths_are_ignored(self):
        for rel in ("config.json", "data/accounts.json", "logs/bot.log",
                    "__pycache__/x.pyc", "plugins/__pycache__/y.pyc",
                    "utils.pyc", ".update-meta.json", ".update-tmp/x"):
            self.assertTrue(updater._ignore_bot_path(PurePosixPath(rel)), rel)

    def test_program_paths_are_kept(self):
        for rel in ("main.py", "admin/server.py", "admin/web/index.html",
                    "plugins/ai_chat.py", "config.example.json"):
            self.assertFalse(updater._ignore_bot_path(PurePosixPath(rel)), rel)


class SyncTreeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.src = base / "src"
        self.root = base / "repo"
        self.bot = self.root / "bot"
        self.patches = [
            mock.patch.object(updater, "ROOT", self.root),
            mock.patch.object(updater, "BOT_DIR", self.bot),
        ]
        for p in self.patches:
            p.start()
        # 源码包内容
        (self.src / "bot" / "plugins").mkdir(parents=True)
        (self.src / "bot" / "main.py").write_text("new main\n")
        (self.src / "bot" / "plugins" / "ai_chat.py").write_text("new plugin\n")
        (self.src / "bot" / "config.json").write_text('{"should": "not copy"}')
        (self.src / "bot" / "data").mkdir()
        (self.src / "bot" / "data" / "accounts.json").write_text('{"x": 1}')
        (self.src / "bot" / "admin").mkdir()
        (self.src / "bot" / "admin" / "server.py").write_text("# new server\n")
        (self.src / "scripts").mkdir()
        (self.src / "scripts" / "bot-start.sh").write_text("#!/bin/bash\n")
        (self.src / "start-all.sh").write_text("#!/bin/bash\necho new\n")
        # 现有仓库：用户数据要保留
        self.bot.mkdir(parents=True)
        (self.bot / "config.json").write_text('{"user": "keep"}')
        (self.bot / "data").mkdir()
        (self.bot / "data" / "accounts.json").write_text('{"keep": true}')
        (self.bot / "logs").mkdir()
        (self.bot / "logs" / "bot.log").write_text("log")
        (self.bot / "main.py").write_text("old main\n")
        (self.bot / ".update-meta.json").write_text('{"commit": "old"}')
        (self.root / "scripts").mkdir()
        (self.root / "scripts" / "bot-start.sh").write_text("old")
        (self.root / "start-all.sh").write_text("old")

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_sync_updates_code_but_keeps_user_data(self):
        counts = updater._sync_tree(self.src)
        self.assertEqual(counts["bot"], 3)          # main.py, plugins/ai_chat.py, admin/server.py
        self.assertEqual((self.bot / "main.py").read_text(), "new main\n")
        self.assertEqual((self.bot / "plugins" / "ai_chat.py").read_text(), "new plugin\n")
        # 用户数据没被覆盖
        self.assertEqual(json.loads((self.bot / "config.json").read_text()), {"user": "keep"})
        self.assertEqual(json.loads((self.bot / "data" / "accounts.json").read_text()), {"keep": True})
        self.assertFalse((self.bot / "logs" / "bot.log").read_text() != "log")
        self.assertEqual(json.loads((self.bot / ".update-meta.json").read_text()), {"commit": "old"})
        # scripts / start-all.sh 更新了
        self.assertEqual((self.root / "scripts" / "bot-start.sh").read_text(), "#!/bin/bash\n")
        self.assertIn("echo new", (self.root / "start-all.sh").read_text())

    def test_sync_without_bot_dir_fails(self):
        (self.src / "bot").rename(self.src / "botx")
        with self.assertRaises(updater.UpdaterError):
            updater._sync_tree(self.src)


class CheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.meta = Path(self.tmp.name) / ".update-meta.json"
        self.patch = mock.patch.object(updater, "META_PATH", self.meta)
        self.patch.start()
        self.latest = {"commit": "0123456789abcdef" * 2 + "0123", "title": "修复登录", "url": "u"}

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_behind_when_no_meta(self):
        with mock.patch.object(updater, "_fetch_latest_commit", return_value=self.latest):
            data = updater.check()
        self.assertTrue(data["behind"])
        self.assertEqual(data["current"]["commit"], "")
        self.assertEqual(data["latest"]["commit"], self.latest["commit"][:7])
        self.assertEqual(data["latest"]["title"], "修复登录")

    def test_not_behind_when_commit_matches(self):
        self.meta.write_text(json.dumps({"commit": self.latest["commit"]}))
        with mock.patch.object(updater, "_fetch_latest_commit", return_value=self.latest):
            data = updater.check()
        self.assertFalse(data["behind"])

    def test_behind_when_installed_older(self):
        self.meta.write_text(json.dumps({"commit": "deadbeefdeadbeef"}))
        with mock.patch.object(updater, "_fetch_latest_commit", return_value=self.latest):
            data = updater.check()
        self.assertTrue(data["behind"])
        self.assertEqual(data["current"]["commit"], "deadbee")

    def test_rate_limit_message(self):
        err = updater.urllib.error.HTTPError("u", 403, "Forbidden", None, io.BytesIO(b""))
        with mock.patch.object(updater.urllib.request, "urlopen", side_effect=err):
            with self.assertRaises(updater.UpdaterError) as ctx:
                updater._http_json("https://api.github.com/x")
        self.assertIn("频繁", str(ctx.exception))

    def test_network_error_message(self):
        with mock.patch.object(updater.urllib.request, "urlopen", side_effect=OSError("boom")):
            with self.assertRaises(updater.UpdaterError) as ctx:
                updater._http_json("https://api.github.com/x")
        msg = str(ctx.exception)
        self.assertTrue(any(ch in msg for ch in "网络连不上"), msg)
        self.assertNotIn("boom", msg)


class UpdateWorkerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.meta = self.root / "meta.json"
        self.synced = {}

        def fake_download(url, dest):
            make_tar(dest, files={"QQBot-main/bot/main.py": "new\n"})
            return 128

        def fake_sync(src_root):
            self.synced["src"] = str(src_root)
            return {"bot": 1}

        self.patches = [
            mock.patch.object(updater, "META_PATH", self.meta),
            mock.patch.object(updater, "_fetch_latest_commit",
                              return_value={"commit": "abcdef1234567890", "title": "t", "url": "u"}),
            mock.patch.object(updater, "_download", side_effect=fake_download),
            mock.patch.object(updater, "_sync_tree", side_effect=fake_sync),
        ]
        for p in self.patches:
            p.start()
        updater._worker = None
        updater._set_state(phase="idle", message="", error=None, restart_required=False)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        updater._worker = None
        self.tmp.cleanup()

    def test_worker_success_state_machine(self):
        updater._update_worker()
        st = updater.status()
        self.assertEqual(st["phase"], "done", st)
        self.assertTrue(st["restart_required"])
        self.assertIsNone(st["error"])
        self.assertIn("重启服务", st["message"])
        meta = json.loads(self.meta.read_text())
        self.assertEqual(meta["commit"], "abcdef1234567890")
        self.assertIn("src", self.synced)

    def test_worker_failure_is_chinese(self):
        with mock.patch.object(updater, "_fetch_latest_commit",
                               side_effect=updater.UpdaterError("网络连不上，检查网络后再试")):
            updater._update_worker()
        st = updater.status()
        self.assertEqual(st["phase"], "failed")
        self.assertEqual(st["error"], "网络连不上，检查网络后再试")
        self.assertFalse(st["restart_required"])

    def test_worker_unexpected_error_is_generic(self):
        with mock.patch.object(updater, "_fetch_latest_commit", side_effect=RuntimeError("Traceback...")):
            updater._update_worker()
        st = updater.status()
        self.assertEqual(st["phase"], "failed")
        self.assertEqual(st["error"], "更新失败，请稍后再试")

    def test_update_is_single_flight(self):
        gate = threading.Event()
        started = threading.Event()
        calls = []

        def slow_worker():
            calls.append(1)
            started.set()
            gate.wait(5)

        with mock.patch.object(updater, "_update_worker", side_effect=slow_worker):
            self.assertTrue(updater.update())
            started.wait(2)
            self.assertFalse(updater.update())      # 第二次拒绝
            gate.set()
            updater._worker.join(3)
            self.assertFalse(updater._worker.is_alive())
            self.assertTrue(updater.update())       # 结束后可以再启动
            updater._worker.join(3)
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
