#!/usr/bin/env python3
"""管理台「任务 / 技能」API 的单元测试：不联网、不占 8080、不开长任务。"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent.parent
BOT_DIR = REPO / "bot"
ADMIN_DIR = BOT_DIR / "admin"
for _p in (str(ADMIN_DIR), str(BOT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import server  # noqa: E402


def wait_task(testcase, task_id, timeout=8.0):
    """轮询任务直到不再 running，返回 detail 里的 task。"""
    end = time.time() + timeout
    last = None
    while time.time() < end:
        res = server.api_tasks_detail(task_id)
        if not res.get("ok"):
            testcase.fail(f"读取任务失败：{res}")
        last = res["task"]
        if last["status"] != "running":
            return last
        time.sleep(0.02)
    testcase.fail(f"任务在 {timeout}s 内没有结束：{last}")


class AdminTasksTest(unittest.TestCase):
    def setUp(self):
        with server._tasks_lock:
            server._tasks.clear()
        self.tmp = tempfile.TemporaryDirectory(prefix="skills-test-")
        self.addCleanup(self.tmp.cleanup)
        self.skills_dir = Path(self.tmp.name) / "skills"
        self.patch_skills = mock.patch.object(server, "SKILLS_DIR", self.skills_dir)
        self.patch_skills.start()
        self.addCleanup(self.patch_skills.stop)

    def tearDown(self):
        self._stop_all_tasks()

    def _stop_all_tasks(self):
        with server._tasks_lock:
            running = [task for task in server._tasks if task.get("status") == "running"]
        for task in running:
            proc = task.get("proc")
            if proc is not None:
                server._kill_python_process(proc)
            else:
                with server._tasks_lock:
                    if task.get("status") == "running":
                        task["status"] = "stopped"
        end = time.time() + 5
        while time.time() < end:
            with server._tasks_lock:
                if not any(t.get("status") == "running" for t in server._tasks):
                    break
            time.sleep(0.02)
        with server._tasks_lock:
            server._tasks.clear()

    # ================================================================ 任务

    def test_run_echo_and_detail(self):
        res = server.api_tasks_run({"command": "echo hello", "timeout": 10})
        self.assertTrue(res["ok"], res)
        task = res["task"]
        self.assertEqual(task["status"], "running")
        self.assertTrue(task["id"])
        self.assertIn("echo hello", task["command"])
        detail = wait_task(self, task["id"])
        self.assertEqual(detail["status"], "done", detail)
        self.assertEqual(detail["exit_code"], 0)
        self.assertEqual(detail["stdout"], "hello\n")
        self.assertEqual(detail["stderr"], "")
        self.assertFalse(detail["timed_out"])
        self.assertGreaterEqual(detail["duration_ms"], 0)

    def test_nonzero_exit_code(self):
        task = server.api_tasks_run({"command": "exit 3"})["task"]
        detail = wait_task(self, task["id"])
        self.assertEqual(detail["status"], "failed")
        self.assertEqual(detail["exit_code"], 3)

    def test_stderr_captured(self):
        task = server.api_tasks_run({"command": "echo oops 1>&2"})["task"]
        detail = wait_task(self, task["id"])
        self.assertEqual(detail["status"], "done")
        self.assertIn("oops", detail["stderr"])
        self.assertNotIn("oops", detail["stdout"])

    def test_default_cwd_is_repo_root(self):
        task = server.api_tasks_run({"command": "pwd"})["task"]
        detail = wait_task(self, task["id"])
        self.assertEqual(detail["stdout"].strip(), str(REPO.resolve()))
        self.assertEqual(detail["cwd"], str(REPO.resolve()))

    def test_relative_cwd(self):
        task = server.api_tasks_run({"command": "pwd", "cwd": "bot"})["task"]
        detail = wait_task(self, task["id"])
        self.assertEqual(Path(detail["stdout"].strip()), (REPO / "bot").resolve())

    def test_cwd_outside_allowed_rejected(self):
        for bad in ("/etc", "/tmp", str(REPO / "..")):
            res = server.api_tasks_run({"command": "pwd", "cwd": bad})
            self.assertFalse(res["ok"], (bad, res))
            self.assertTrue(any('\u4e00' <= ch <= '\u9fff' for ch in res["error"]), res)

    def test_cwd_missing_rejected(self):
        res = server.api_tasks_run({"command": "pwd", "cwd": "no-such-dir-xyz"})
        self.assertFalse(res["ok"], res)
        self.assertIn("不存在", res["error"])

    def test_missing_command(self):
        for body in ({}, None, {"command": ""}, {"command": "   "}, {"timeout": 5}):
            res = server.api_tasks_run(body)
            self.assertFalse(res["ok"], (body, res))
            self.assertIn("命令", res["error"])

    def test_timeout_kills_process(self):
        task = server.api_tasks_run({"command": "sleep 30", "timeout": 1})["task"]
        detail = wait_task(self, task["id"], timeout=10)
        self.assertEqual(detail["status"], "failed")
        self.assertTrue(detail["timed_out"])
        self.assertNotEqual(detail["exit_code"], 0)
        self.assertLess(detail["duration_ms"], 15000)

    def test_stop_running_task(self):
        task = server.api_tasks_run({"command": "sleep 30"})["task"]
        res = server.api_tasks_stop({"id": task["id"]})
        self.assertTrue(res["ok"], res)
        detail = wait_task(self, task["id"], timeout=10)
        self.assertEqual(detail["status"], "stopped")
        self.assertLess(detail["duration_ms"], 8000)

    def test_stop_unknown_and_missing_id(self):
        res = server.api_tasks_stop({"id": "no-such-task"})
        self.assertFalse(res["ok"], res)
        self.assertIn("找不到", res["error"])
        res = server.api_tasks_stop({})
        self.assertFalse(res["ok"], res)
        self.assertIn("id", res["error"])

    def test_max_three_running(self):
        started = []
        try:
            for _ in range(server.TASKS_MAX_RUNNING):
                res = server.api_tasks_run({"command": "sleep 30"})
                self.assertTrue(res["ok"], res)
                started.append(res["task"]["id"])
            res = server.api_tasks_run({"command": "true"})
            self.assertFalse(res["ok"], res)
            self.assertIn(str(server.TASKS_MAX_RUNNING), res["error"])
        finally:
            for task_id in started:
                server.api_tasks_stop({"id": task_id})
            for task_id in started:
                wait_task(self, task_id)

    def test_list_recent_20_and_order(self):
        with server._tasks_lock:
            server._tasks.extend([{
                "id": "fake%d" % i,
                "command": "true",
                "cwd": str(REPO),
                "status": "done",
                "started_at": float(i),
                "duration_ms": 1,
                "exit_code": 0,
                "timed_out": False,
            } for i in range(25)])
        res = server.api_tasks_list()
        self.assertTrue(res["ok"], res)
        self.assertEqual(len(res["tasks"]), server.TASKS_LIST_LIMIT)
        self.assertEqual(res["tasks"][0]["id"], "fake24")
        self.assertEqual(res["tasks"][-1]["id"], "fake5")

    def test_prune_keeps_50_and_keeps_running(self):
        with server._tasks_lock:
            server._tasks.extend([{
                "id": "old%d" % i,
                "command": "true",
                "cwd": str(REPO),
                "status": "done",
                "started_at": float(i),
                "duration_ms": 1,
                "exit_code": 0,
                "timed_out": False,
            } for i in range(60)])
            server._tasks.append({
                "id": "running-task",
                "command": "sleep 30",
                "cwd": str(REPO),
                "status": "running",
                "started_at": 1e9,
                "duration_ms": 0,
                "exit_code": None,
                "timed_out": False,
            })
            server._prune_tasks_locked()
            ids = [task["id"] for task in server._tasks]
        self.assertEqual(len(ids), server.TASKS_KEEP)
        self.assertIn("running-task", ids)
        self.assertNotIn("old0", ids)

    def test_running_detail_has_partial_output(self):
        task = server.api_tasks_run({"command": "echo first; sleep 1; echo second"})["task"]
        deadline = time.time() + 3
        saw_running = False
        partial = None
        while time.time() < deadline:
            res = server.api_tasks_detail(task["id"])
            task_view = res["task"]
            if task_view["status"] == "running" and "first" in task_view["stdout"]:
                saw_running = True
                partial = task_view["stdout"]
                break
            if task_view["status"] != "running":
                break
            time.sleep(0.02)
        self.assertTrue(saw_running, f"没能读到运行中的输出：{partial}")
        detail = wait_task(self, task["id"])
        self.assertIn("second", detail["stdout"])

    def test_output_truncated_to_200kb(self):
        code = f"{sys.executable} -c \"import sys; sys.stdout.write('x'*300000)\""
        task = server.api_tasks_run({"command": code})["task"]
        detail = wait_task(self, task["id"], timeout=15)
        self.assertEqual(detail["status"], "done", detail)
        self.assertIn("已截断", detail["stdout"])
        self.assertLessEqual(len(detail["stdout"]), 200 * 1024 + 100)

    def test_list_marks_running_status(self):
        task = server.api_tasks_run({"command": "sleep 30"})["task"]
        try:
            res = server.api_tasks_list()
            found = [item for item in res["tasks"] if item["id"] == task["id"]]
            self.assertTrue(found, res)
            self.assertEqual(found[0]["status"], "running")
            self.assertIsNone(found[0]["exit_code"])
        finally:
            server.api_tasks_stop({"id": task["id"]})
            wait_task(self, task["id"])

    # ================================================================ 技能

    def test_skills_empty_list(self):
        res = server.api_skills_list()
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["skills"], [])

    def test_skills_save_list_get_delete(self):
        res = server.api_skills_save({"name": "测试技能_1", "content": "# 我的技能\n正文内容"})
        self.assertTrue(res["ok"], res)
        self.assertTrue((self.skills_dir / "测试技能_1.md").is_file())

        listing = server.api_skills_list()
        self.assertTrue(listing["ok"], listing)
        self.assertEqual(len(listing["skills"]), 1)
        item = listing["skills"][0]
        self.assertEqual(item["name"], "测试技能_1")
        self.assertEqual(item["description"], "我的技能")
        self.assertEqual(item["size"], len("# 我的技能\n正文内容".encode("utf-8")))
        self.assertGreater(item["updated_at"], 0)

        got = server.api_skills_get("测试技能_1")
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["name"], "测试技能_1")
        self.assertEqual(got["content"], "# 我的技能\n正文内容")

        deleted = server.api_skills_delete({"name": "测试技能_1"})
        self.assertTrue(deleted["ok"], deleted)
        self.assertFalse((self.skills_dir / "测试技能_1.md").exists())
        self.assertFalse(server.api_skills_get("测试技能_1")["ok"])

    def test_skills_description_first_line(self):
        server.api_skills_save({"name": "a1", "content": "第一行说明\n第二行"})
        server.api_skills_save({"name": "a2", "content": "\n\n###  带井号的标题\n正文"})
        server.api_skills_save({"name": "a3", "content": ""})
        listing = {item["name"]: item for item in server.api_skills_list()["skills"]}
        self.assertEqual(listing["a1"]["description"], "第一行说明")
        self.assertEqual(listing["a2"]["description"], "带井号的标题")
        self.assertEqual(listing["a3"]["description"], "")

    def test_skills_overwrite(self):
        server.api_skills_save({"name": "same", "content": "old"})
        server.api_skills_save({"name": "same", "content": "new"})
        self.assertEqual(server.api_skills_get("same")["content"], "new")

    def test_skills_md_suffix_accepted(self):
        res = server.api_skills_save({"name": "带后缀.md", "content": "# 标题"})
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["name"], "带后缀")
        self.assertTrue((self.skills_dir / "带后缀.md").is_file())
        self.assertEqual(server.api_skills_get("带后缀.md")["content"], "# 标题")

    def test_skills_name_validation(self):
        bad_names = ("", "   ", "../evil", "a/b", "a\\b", "x" * 61, "有 空格",
                     "a.b", "a$b", "技能" + "x" * 61)
        for name in bad_names:
            res = server.api_skills_save({"name": name, "content": "x"})
            self.assertFalse(res["ok"], (name, res))
            self.assertTrue(any('\u4e00' <= ch <= '\u9fff' for ch in res["error"]), res)
            self.assertFalse(server.api_skills_get(name)["ok"], name)
            self.assertFalse(server.api_skills_delete({"name": name})["ok"], name)

    def test_skills_content_too_large(self):
        res = server.api_skills_save({"name": "big", "content": "x" * (200 * 1024 + 1)})
        self.assertFalse(res["ok"], res)
        self.assertIn("太长", res["error"])
        self.assertFalse((self.skills_dir / "big.md").exists())

    def test_skills_content_must_be_text(self):
        res = server.api_skills_save({"name": "bad", "content": 123})
        self.assertFalse(res["ok"], res)
        self.assertIn("文本", res["error"])

    def test_skills_delete_missing(self):
        res = server.api_skills_delete({"name": "no-such"})
        self.assertFalse(res["ok"], res)
        self.assertIn("找不到", res["error"])

    def test_skills_get_missing(self):
        res = server.api_skills_get("no-such")
        self.assertFalse(res["ok"], res)
        self.assertIn("找不到", res["error"])

    def test_skills_path_traversal(self):
        outside = Path(self.tmp.name) / "outside.md"
        outside.write_text("secret", encoding="utf-8")
        for name in ("..%2Foutside", "..%2f..%2Fetc%2Fpasswd", "%2e%2e/outside"):
            self.assertFalse(server.api_skills_get(name)["ok"], name)
            self.assertFalse(server.api_skills_delete({"name": name})["ok"], name)
        # 目录里不会有任何越界文件被创建
        self.assertFalse((self.skills_dir / "..%2Foutside.md").exists())

    def test_skills_sorted_by_name(self):
        for name in ("bbb", "aaa", "中文技能"):
            self.assertTrue(server.api_skills_save({"name": name, "content": "x"})["ok"])
        names = [item["name"] for item in server.api_skills_list()["skills"]]
        self.assertEqual(names, sorted(names))

    def test_skills_list_skips_invalid_filenames(self):
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        (self.skills_dir / "bad name.md").write_text("# bad", encoding="utf-8")
        (self.skills_dir / "good.md").write_text("# good", encoding="utf-8")
        (self.skills_dir / "not-md.txt").write_text("x", encoding="utf-8")
        names = [item["name"] for item in server.api_skills_list()["skills"]]
        self.assertEqual(names, ["good"])

    def test_skills_no_temp_files_left(self):
        server.api_skills_save({"name": "tmpcheck", "content": "# x"})
        leftovers = list(self.skills_dir.glob("*.tmp")) + list(self.skills_dir.glob(".*.tmp"))
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
