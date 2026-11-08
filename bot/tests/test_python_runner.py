#!/usr/bin/env python3
"""POST /api/python/run 的单元测试。

只在本机用当前解释器跑 Python，不访问网络、不动 8080 上的运行实例。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
BOT_DIR = REPO / "bot"
ADMIN_DIR = BOT_DIR / "admin"
for _p in (str(ADMIN_DIR), str(BOT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import server  # noqa: E402

DATA_DIR = REPO / "bot" / "data"
TMP_DIR = Path(tempfile.gettempdir())


class PythonRunnerTest(unittest.TestCase):
    def setUp(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.created = []

    def tearDown(self):
        for path in self.created:
            try:
                Path(path).unlink()
            except Exception:
                pass

    def make_file(self, content: str, directory: Path = DATA_DIR, suffix: str = ".py") -> Path:
        fd, name = tempfile.mkstemp(prefix=".test-py-", suffix=suffix, dir=str(directory))
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        path = Path(name)
        self.created.append(path)
        return path

    # ---------- 正常运行 ----------
    def test_run_print(self):
        res = server.api_python_run({"code": "print('hello world')", "timeout": 10})
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["stdout"], "hello world\n")
        self.assertEqual(res["stderr"], "")
        self.assertEqual(res["exit_code"], 0)
        self.assertFalse(res["timed_out"])
        self.assertGreaterEqual(res["duration_ms"], 0)

    def test_run_prints_chinese(self):
        # 手机环境 locale 可能是 C，运行器要保证中文输出不报编码错误
        res = server.api_python_run({"code": "print('你好，世界')", "timeout": 10})
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["stdout"], "你好，世界\n")
        self.assertEqual(res["stderr"], "")

    def test_exit_code(self):
        res = server.api_python_run({"code": "import sys\nsys.exit(3)", "timeout": 10})
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["exit_code"], 3)
        self.assertFalse(res["timed_out"])

    def test_stderr(self):
        res = server.api_python_run(
            {"code": "import sys\nprint('oops', file=sys.stderr)", "timeout": 10}
        )
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["exit_code"], 0)
        self.assertIn("oops", res["stderr"])
        self.assertNotIn("oops", res["stdout"])

    def test_syntax_error_is_normal_run_result(self):
        res = server.api_python_run({"code": "def broken(:\n    pass", "timeout": 10})
        self.assertTrue(res["ok"], res)
        self.assertNotEqual(res["exit_code"], 0)
        self.assertIn("SyntaxError", res["stderr"])

    def test_timeout_kills_process(self):
        res = server.api_python_run(
            {"code": "import time\ntime.sleep(30)", "timeout": 1}
        )
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["timed_out"])
        self.assertNotEqual(res["exit_code"], 0)
        self.assertLess(res["duration_ms"], 15000)
        self.assertIn("超时", res.get("message", ""))

    def test_output_truncated_to_200kb(self):
        code = "import sys\nprint('x' * 300000)\nprint('y' * 300000, file=sys.stderr)"
        res = server.api_python_run({"code": code, "timeout": 30})
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["exit_code"], 0)
        self.assertIn("已截断", res["stdout"])
        self.assertIn("已截断", res["stderr"])
        self.assertLessEqual(len(res["stdout"]), 200 * 1024 + 100)
        self.assertLessEqual(len(res["stderr"]), 200 * 1024 + 100)

    # ---------- 缺参 ----------
    def test_missing_code(self):
        for body in ({}, None, {"code": ""}, {"code": "   "}, {"timeout": 10}):
            res = server.api_python_run(body)
            self.assertFalse(res["ok"], res)
            self.assertIn("代码", res["error"])

    def test_timeout_clamped(self):
        self.assertEqual(server._python_timeout(0), 1)
        self.assertEqual(server._python_timeout(-5), 1)
        self.assertEqual(server._python_timeout(1), 1)
        self.assertEqual(server._python_timeout(999), 300)
        self.assertEqual(server._python_timeout("abc"), 30)
        self.assertEqual(server._python_timeout(None), 30)
        self.assertEqual(server._python_timeout(12.9), 12)

    def test_invalid_timeout_still_runs(self):
        res = server.api_python_run({"code": "print('ok')", "timeout": "不是数字"})
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["stdout"], "ok\n")

    # ---------- path 模式 ----------
    def test_path_mode_runs_file(self):
        path = self.make_file("print('from file')\n")
        res = server.api_python_run({"path": str(path), "timeout": 10})
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["stdout"], "from file\n")
        self.assertEqual(res["exit_code"], 0)

    def test_relative_path_is_resolved_from_repo_root(self):
        path = self.make_file("print('relative ok')\n")
        rel = path.relative_to(REPO)
        res = server.api_python_run({"path": str(rel), "timeout": 10})
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["stdout"], "relative ok\n")

    def test_code_wins_over_path(self):
        bad_path = Path("/etc/passwd")
        res = server.api_python_run(
            {"code": "print('code wins')", "path": str(bad_path), "timeout": 10}
        )
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["stdout"], "code wins\n")

    def test_path_outside_allowed_dirs_rejected(self):
        outside = self.make_file("print('outside')\n", directory=TMP_DIR)
        res = server.api_python_run({"path": str(outside), "timeout": 10})
        self.assertFalse(res["ok"], res)
        self.assertIn("不在允许", res["error"])

    def test_path_traversal_rejected(self):
        outside = self.make_file("print('outside')\n", directory=TMP_DIR)
        sneaky = str(DATA_DIR / ".." / ".." / ".." / ".." / ".." / "tmp" / outside.name)
        res = server.api_python_run({"path": sneaky, "timeout": 10})
        self.assertFalse(res["ok"], res)
        self.assertIn("不在允许", res["error"])

    @unittest.skipUnless(hasattr(os, "symlink"), "环境不支持符号链接")
    def test_symlink_escape_rejected(self):
        outside = self.make_file("print('outside')\n", directory=TMP_DIR)
        link = DATA_DIR / (".test-link-%d.py" % os.getpid())
        try:
            os.symlink(str(outside), str(link))
        except OSError:
            self.skipTest("创建符号链接失败")
        self.created.append(link)
        res = server.api_python_run({"path": str(link), "timeout": 10})
        self.assertFalse(res["ok"], res)
        self.assertIn("不在允许", res["error"])

    def test_path_missing_rejected(self):
        res = server.api_python_run({"path": str(DATA_DIR / "no-such-file.py")})
        self.assertFalse(res["ok"], res)
        self.assertIn("找不到", res["error"])

    def test_path_must_be_py_file(self):
        path = self.make_file("not python\n", suffix=".txt")
        res = server.api_python_run({"path": str(path)})
        self.assertFalse(res["ok"], res)
        self.assertIn(".py", res["error"])

    def test_path_directory_rejected(self):
        res = server.api_python_run({"path": str(DATA_DIR)})
        self.assertFalse(res["ok"], res)

    # ---------- 其它 ----------
    def test_temp_file_removed_after_run(self):
        before = set(TMP_DIR.glob("qqbot-py-*.py"))
        res = server.api_python_run({"code": "print('cleanup')", "timeout": 10})
        self.assertTrue(res["ok"], res)
        after = set(TMP_DIR.glob("qqbot-py-*.py"))
        self.assertEqual(after - before, set())

    def test_cwd_is_repo_root(self):
        res = server.api_python_run({"code": "import os; print(os.getcwd())", "timeout": 10})
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["stdout"].strip(), str(REPO.resolve()))

    def test_no_shell_injection(self):
        # 代码里的 shell 元字符应原样当 Python 字符串，而不是被 shell 执行
        res = server.api_python_run(
            {"code": "print('a && b; echo pwned')", "timeout": 10}
        )
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["stdout"], "a && b; echo pwned\n")
        self.assertFalse((REPO / "pwned").exists())


if __name__ == "__main__":
    unittest.main()
