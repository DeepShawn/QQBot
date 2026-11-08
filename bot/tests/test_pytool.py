"""pytool 执行工具的离线测试（真实 sys.executable，不联网）。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent.parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import pytool  # noqa: E402


class PyToolTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name) / "data"
        self.files = self.data_dir / "files"
        self.files.mkdir(parents=True, exist_ok=True)

    def write_file(self, name: str, content) -> Path:
        path = self.files / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return path


class ExecuteCodeTests(PyToolTestCase):
    def test_simple_code(self):
        result = pytool.execute_python('print("你好 pytool")', data_dir=self.data_dir)
        self.assertNotIn("error", result)
        self.assertEqual(result["exit_code"], 0)
        self.assertFalse(result["timed_out"])
        self.assertIn("你好 pytool", result["stdout"])
        self.assertEqual(result["stderr"], "")
        self.assertGreaterEqual(result["duration"], 0)

    def test_stderr_and_exit_code(self):
        result = pytool.execute_python(
            'import sys\nsys.stderr.write("boom\\n")\nsys.exit(3)',
            data_dir=self.data_dir,
        )
        self.assertEqual(result["exit_code"], 3)
        self.assertIn("boom", result["stderr"])

    def test_timeout_kills_process(self):
        result = pytool.execute_python(
            "import time\ntime.sleep(10)\nprint('never')",
            timeout=0.3,
            data_dir=self.data_dir,
        )
        self.assertTrue(result["timed_out"])
        self.assertNotIn("never", result["stdout"])

    def test_output_truncated(self):
        result = pytool.execute_python(
            'print("x" * 5000)', data_dir=self.data_dir
        )
        self.assertLessEqual(len(result["stdout"]), pytool.MAX_OUTPUT_CHARS)
        self.assertIn("截断", result["stdout"])

    def test_empty_args(self):
        result = pytool.execute_python(data_dir=self.data_dir)
        self.assertIn("error", result)
        self.assertIn("没有", result["error"])

    def test_code_too_large(self):
        result = pytool.execute_python(
            "x = 1\n" * 60000, data_dir=self.data_dir
        )
        self.assertIn("error", result)
        self.assertIn("太长", result["error"])

    def test_temp_script_removed(self):
        pytool.execute_python("print(1)", data_dir=self.data_dir)
        run_dir = self.data_dir / "run"
        leftovers = list(run_dir.glob("*.py")) if run_dir.is_dir() else []
        self.assertEqual(leftovers, [])


class ExecuteFileTests(PyToolTestCase):
    def test_run_file(self):
        path = self.write_file("hello.py", "print('file ok')")
        result = pytool.execute_python(file=path, data_dir=self.data_dir)
        self.assertNotIn("error", result)
        self.assertIn("file ok", result["stdout"])

    def test_run_file_by_name(self):
        self.write_file("hello.py", "print('by name ok')")
        result = pytool.execute_python(file="hello.py", data_dir=self.data_dir)
        self.assertIn("by name ok", result["stdout"])

    def test_run_file_partial_name(self):
        self.write_file("代码_0101_120000.py", "print('partial ok')")
        result = pytool.execute_python(
            file="代码_0101", data_dir=self.data_dir
        )
        self.assertIn("partial ok", result["stdout"])

    def test_outside_path_rejected(self):
        outside = Path(self._tmp.name) / "outside.py"
        outside.write_text("print('nope')", encoding="utf-8")
        result = pytool.execute_python(file=outside, data_dir=self.data_dir)
        self.assertIn("error", result)
        self.assertIn("data/files", result["error"])
        self.assertNotIn("nope", str(result))

    def test_absolute_etc_rejected(self):
        result = pytool.execute_python(file="/etc/passwd", data_dir=self.data_dir)
        self.assertIn("error", result)

    def test_non_python_file_rejected(self):
        path = self.write_file("note.txt", "hello")
        result = pytool.execute_python(file=path, data_dir=self.data_dir)
        self.assertIn("error", result)
        self.assertIn(".py", result["error"])

    def test_non_utf8_file_rejected(self):
        path = self.write_file("bad.py", b"\xff\xfe\x00bad")
        result = pytool.execute_python(file=path, data_dir=self.data_dir)
        self.assertIn("error", result)
        self.assertIn("UTF-8", result["error"])

    def test_too_large_file_rejected(self):
        path = self.write_file("big.py", "# pad\n" + "x = 1\n" * 40000)
        result = pytool.execute_python(file=path, data_dir=self.data_dir)
        self.assertIn("error", result)
        self.assertIn("太大", result["error"])

    def test_missing_file(self):
        result = pytool.execute_python(file="nope.py", data_dir=self.data_dir)
        self.assertIn("error", result)
        self.assertIn("没找到", result["error"])


class ResolveTests(PyToolTestCase):
    def test_resolve_exact(self):
        path = self.write_file("a.py", "print(1)")
        resolved, error = pytool.resolve_py_file("a.py", self.data_dir)
        self.assertEqual(resolved, path.resolve())
        self.assertEqual(error, "")

    def test_resolve_outside(self):
        outside = Path(self._tmp.name) / "x.py"
        outside.write_text("print(1)", encoding="utf-8")
        resolved, error = pytool.resolve_py_file(str(outside), self.data_dir)
        self.assertIsNone(resolved)
        self.assertIn("data/files", error)

    def test_resolve_parent_traversal(self):
        outside = Path(self._tmp.name) / "x.py"
        outside.write_text("print(1)", encoding="utf-8")
        resolved, error = pytool.resolve_py_file("../x.py", self.data_dir)
        self.assertIsNone(resolved)
        self.assertIn("data/files", error)

    def test_files_root(self):
        self.assertEqual(pytool.files_root(self.data_dir), self.data_dir / "files")


if __name__ == "__main__":
    unittest.main()
