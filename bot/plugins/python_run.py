"""在 QQ 聊天里真的运行 Python。

用法（私聊直接可用；群里要先 @ 机器人，且只有 bot.admins / 群主 / 群管理员能用）：
- ``/py <代码>``：运行这段 Python（可多行），也可以 ``/py`` + 引用一条含
  ```python 代码块的消息。
- ``/run <文件名>``：运行 bot 保存过的 .py（data/files/ 下，精确或包含匹配；
  也允许带路径，但真实路径必须在 data/files 内）。
- ``/run`` + 引用一条带 .py 文件的消息：下载后运行；引用含 ```python 代码块
  的消息也可以。
- 自然语言：消息里有「运行/执行/跑/run」，并且引用了 .py 文件 / python 代码块，
  或提到 data/files 下存在的 .py 文件名，就直接执行；只提到 python/代码 时回复
  用法提示（不再交给 AI 瞎答）。
- 追问式触发：「再试试 / 试试 / 试一下 / 跑一下 / 运行一下」这类消息在没有明确
  目标时，自动运行 data/files 里最近 30 分钟内改过的 .py，并先回一句文件名。
- 引用消息的纯文本里出现 xxx.py 文件名（比如机器人之前发的「已保存成文件：代码_x.py」）
  也会解析出来并运行；群聊里这种引用不需要 @ 机器人。

执行方式：``sys.executable -I -u`` 跑临时脚本，30 秒超时，输出截断；只使用标准库。
安全：只运行用户明确给出/引用的 Python 代码，不拼接 shell；临时脚本结束即删；
路径做 data/files 越界校验；群里执行前查群成员 role（owner/admin）或 bot.admins。
"""
from __future__ import annotations

import asyncio
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import files as files_mod
import media
from dispatcher import PluginBase
from utils import mentioned, segments, strip_at, text_of

RESULT_TITLE = "🐍 Python 运行结果"
USAGE_TEXT = (
    "🐍 Python 用法：\n"
    "· /py <代码> 直接运行，例如 /py print(\"你好\")\n"
    "· /run 文件名.py 运行 bot 保存过的脚本（data/files/ 下）\n"
    "· 引用带 .py 文件或 ```python 代码块的消息，再发「运行」\n"
    "群里只有机器人管理员和群主/群管理员能用。"
)
PERMISSION_TEXT = "运行 Python 只有管理员能用"

DEFAULT_TIMEOUT = 30.0
MIN_TIMEOUT = 0.2
MAX_TIMEOUT = 120.0
MAX_DOWNLOAD_BYTES = 200 * 1024
MAX_CODE_BYTES = 200 * 1024
MAX_OUTPUT_CHARS = 2000
MAX_REPLY_CHARS = 3000
DOWNLOAD_TIMEOUT = 20.0
KILL_WAIT_SECONDS = 5.0
FILES_DIR_NAME = "files"
RUN_DIR_NAME = "run"

_PY_LANGS = ("python", "python3", "py", "py3")
_CMD_PY_RE = re.compile(r"^/(?:py|python)\b\s*(.*)$", re.I | re.S)
_CMD_RUN_RE = re.compile(r"^/run\b\s*(.*)$", re.I | re.S)
_NL_RUN_RE = re.compile(r"运行|执行|跑|(?<![A-Za-z])run(?![A-Za-z])", re.I)
_TRY_RE = re.compile(
    r"再试|试试|试一下|试下|试一次|跑一下|跑下|运行一下|执行一下", re.I
)
_PY_WORD_RE = re.compile(r"python|(?<![A-Za-z])py(?![A-Za-z])|\.py|代码", re.I)
# 文本里 xxx.py 文件名的粗略抓取（再和 data/files 里的真实文件名对照）
_PY_NAME_RE = re.compile(r"[\w./\\-]+\.py", re.UNICODE)
RECENT_WINDOW_SECONDS = 30 * 60


# ---------------------------------------------------------------- 小工具


def _is_python_lang(lang: str) -> bool:
    first = (str(lang or "").strip().lower().split() or [""])[0]
    return first in _PY_LANGS or first.startswith("python")


def _python_blocks(text: str, allow_plain: bool = False) -> list[dict]:
    """从文本里取 python 代码块（``allow_plain`` 时也接受没写语言的代码块）。"""
    try:
        blocks = files_mod.extract_code_blocks(text or "")
    except Exception:
        return []
    out = []
    for block in blocks:
        lang = str(block.get("lang") or "")
        if _is_python_lang(lang) or (allow_plain and not lang.strip()):
            out.append(block)
    return out


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    text = str(text or "")
    if len(text) <= limit:
        return text, False
    suffix = "\n…（内容过长，已截断）"
    keep = max(0, limit - len(suffix))
    return text[:keep] + suffix, True


def _message_text(message) -> str:
    """把消息段拼成纯文本（测试/日志用）。"""
    if isinstance(message, list):
        return "".join(
            str((seg.get("data") or {}).get("text", ""))
            for seg in message
            if isinstance(seg, dict) and seg.get("type") == "text"
        )
    return str(message or "")


def _safe_download_name(name: str) -> str:
    base = Path(str(name or "")).name
    base = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", base)[:80].strip()
    if not base:
        base = "quoted"
    if not base.lower().endswith(".py"):
        base += ".py"
    return base


class Plugin(PluginBase):
    name = "python_run"
    priority = 15  # commands(10) 之后，group_admin(50) / keyword(200) / ai_chat(300) 之前

    def __init__(self, ctx):
        super().__init__(ctx)
        self.data_dir = Path(ctx.data_dir)
        self.files_dir = self.data_dir / FILES_DIR_NAME
        self.run_dir = self.data_dir / RUN_DIR_NAME
        try:
            self.ctx.log.info("python_run 已加载（/py、/run）")
        except Exception:
            pass

    # ---------------- 配置 ----------------
    def _cfg(self) -> dict:
        try:
            return self.ctx.plugin_cfg("python_run") or {}
        except Exception:
            return {}

    def _timeout(self) -> float:
        try:
            value = float(self._cfg().get("timeout_seconds", DEFAULT_TIMEOUT))
        except (TypeError, ValueError):
            value = DEFAULT_TIMEOUT
        return max(MIN_TIMEOUT, min(MAX_TIMEOUT, value))

    # ---------------- 消息解析 ----------------
    async def _quoted(self, event):
        """取被引用的消息对象（OneBot get_msg 的 data）；拿不到返回 None。"""
        for seg in segments(event.get("message")):
            if not isinstance(seg, dict) or seg.get("type") != "reply":
                continue
            mid = (seg.get("data") or {}).get("id")
            if not mid:
                continue
            api = getattr(self.ctx, "api", None)
            if api is None:
                return None
            try:
                return await api.call("get_msg", message_id=int(mid))
            except Exception as e:
                self.ctx.log.info("python_run 读取引用消息失败: %s", e)
                return None
        return None

    @staticmethod
    def _has_reply(event) -> bool:
        return any(
            isinstance(seg, dict) and seg.get("type") == "reply"
            for seg in segments(event.get("message"))
        )

    @staticmethod
    def _quoted_text(quoted) -> str:
        if not isinstance(quoted, dict):
            return ""
        return text_of(quoted.get("message"))

    def _quoted_file(self, quoted):
        """引用消息里的 .py 文件段 -> {"data","name"}，没有返回 None。"""
        if not isinstance(quoted, dict):
            return None
        for seg in segments(quoted.get("message")):
            if not isinstance(seg, dict) or seg.get("type") != "file":
                continue
            data = seg.get("data") or {}
            name = str(data.get("file") or data.get("name") or "").strip()
            if name.lower().endswith(".py"):
                return {"data": data, "name": name}
        return None

    def _quoted_code(self, quoted) -> str:
        blocks = _python_blocks(self._quoted_text(quoted))
        if not blocks:
            return ""
        return str(blocks[0].get("code") or "").strip("\n")

    def _quoted_action(self, quoted):
        """引用消息 -> ("file_seg", data, name) / ("code", code) / None。"""
        file_info = self._quoted_file(quoted)
        if file_info is not None:
            return ("file_seg", file_info["data"], file_info["name"])
        code = self._quoted_code(quoted)
        if code:
            return ("code", code)
        return None

    @staticmethod
    def _parse_command(text: str) -> tuple[str, str]:
        """返回 (command, arg)；不是命令返回 ("", "")。"""
        if not text.startswith("/"):
            return "", ""
        match = _CMD_PY_RE.match(text)
        if match:
            return "py", (match.group(1) or "").strip()
        match = _CMD_RUN_RE.match(text)
        if match:
            return "run", (match.group(1) or "").strip()
        return "", ""

    # ---------------- 文件查找 ----------------
    def _iter_py_files(self) -> list[Path]:
        try:
            if not self.files_dir.is_dir():
                return []
            return sorted(
                (p for p in self.files_dir.rglob("*.py") if p.is_file()),
                key=lambda p: len(p.name),
            )
        except OSError:
            return []

    def _resolve_run_arg(self, name: str) -> tuple[Path | None, str]:
        """把 /run 后面的参数解析成 data/files 内的 .py 路径 -> (path, 错误文案)。"""
        raw = str(name or "").strip().strip("\"'")
        raw = raw.removeprefix("file://") if raw.startswith("file://") else raw
        if not raw:
            return None, USAGE_TEXT
        try:
            base = self.files_dir.resolve()
        except OSError:
            base = self.files_dir
        candidate = Path(raw)
        path_like = (
            candidate.is_absolute()
            or "/" in raw
            or "\\" in raw
            or raw.startswith("..")
        )
        if path_like:
            target = candidate if candidate.is_absolute() else (base / raw)
            try:
                target = target.resolve()
            except OSError:
                return None, "这个路径打不开，换一个再试。"
            if not _is_within(target, base):
                return None, "只能运行 data/files 里的 .py 文件。"
            if not target.is_file():
                return None, "没找到这个 .py 文件。"
            if target.suffix.lower() != ".py":
                return None, "只能运行 .py 文件。"
            return target, ""

        wanted = Path(raw).name.lower()
        candidates = self._iter_py_files()
        exact = [p for p in candidates if p.name.lower() == wanted]
        if not exact and not wanted.endswith(".py"):
            exact = [p for p in candidates if p.name.lower() == wanted + ".py"]
        if exact:
            return exact[0], ""
        partial = [p for p in candidates if p.name.lower().startswith(wanted)]
        if partial:
            return partial[0], ""
        contained = [p for p in candidates if wanted in p.name.lower()]
        if contained:
            return contained[0], ""
        return None, f"没找到 {raw} 这个 .py 文件。可以发文件后 /run 文件名，或直接 /py 贴代码。"

    def _recent_py_file(self) -> Path | None:
        """data/files 下最近 30 分钟内修改过的 .py（取最新一个）。"""
        now = time.time()
        best: tuple[float, Path] | None = None
        for path in self._iter_py_files():
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if now - mtime > RECENT_WINDOW_SECONDS:
                continue
            if best is None or mtime > best[0]:
                best = (mtime, path)
        return best[1] if best is not None else None

    def _quoted_filename(self, quoted) -> Path | None:
        """引用消息的纯文本里出现 xxx.py 文件名 -> data/files 里解析出路径。"""
        text = self._quoted_text(quoted)
        if not text:
            return None
        # 1) 先和 data/files 里的真实文件名对照（机器人自己发的保存提示最常见）
        best: Path | None = None
        for path in self._iter_py_files():
            if path.name and path.name in text:
                if best is None or len(path.name) > len(best.name):
                    best = path
        if best is not None:
            return best
        # 2) 再从文本里抓 xxx.py 片段，交给 /run 的查找逻辑
        for match in _PY_NAME_RE.finditer(text):
            raw = match.group(0).strip("./\\").strip()
            if not raw:
                continue
            path, _error = self._resolve_run_arg(raw)
            if path is not None:
                return path
        return None

    def _find_file_in_text(self, text: str) -> Path | None:
        """自然语言里提到 data/files 下已有的 .py 文件名 -> 路径。"""
        best: tuple[int, Path] | None = None
        for path in self._iter_py_files():
            if path.name and path.name in text:
                size = len(path.name)
                if best is None or size > best[0]:
                    best = (size, path)
        return best[1] if best else None

    # ---------------- 权限 ----------------
    async def _allowed(self, event) -> bool:
        if event.get("message_type") != "group":
            return True
        user_id = event.get("user_id")
        try:
            if self.ctx.is_admin(user_id):
                return True
        except Exception:
            pass
        api = getattr(self.ctx, "api", None)
        if api is None:
            return False
        try:
            info = await api.call(
                "get_group_member_info",
                group_id=int(event.get("group_id") or 0),
                user_id=int(user_id or 0),
                no_cache=True,
            )
        except Exception as e:
            self.ctx.log.info("python_run 查询群成员失败: %s", e)
            return False
        role = str((info or {}).get("role") or "").lower() if isinstance(info, dict) else ""
        return role in ("owner", "admin")

    # ---------------- 执行 ----------------
    def _execute(self, code: str) -> dict:
        """同步执行代码（放 asyncio.to_thread 里跑），返回结果 dict。"""
        started = time.monotonic()
        script = None
        try:
            self.run_dir.mkdir(parents=True, exist_ok=True)
            script = self.run_dir / f"run_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}.py"
            script.write_text(code, encoding="utf-8")
        except OSError as e:
            self.ctx.log.warning("python_run 写临时脚本失败: %s", e)
            return {"error": "运行失败：写临时脚本失败，检查 data 目录权限。"}
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "UTF-8"
        env["PYTHONUTF8"] = "1"
        env.pop("PYTHONSTARTUP", None)
        timeout = self._timeout()
        try:
            proc = subprocess.Popen(
                # -I: 隔离模式（忽略 PYTHON* 环境变量/用户 site）；-u: 不缓冲；
                # -X utf8: -I 会忽略 PYTHONIOENCODING，用它兜底中文输出
                [sys.executable, "-I", "-u", "-X", "utf8", str(script)],
                cwd=str(self.run_dir),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                start_new_session=True,
            )
        except OSError as e:
            self.ctx.log.warning("python_run 启动 Python 失败: %s", e)
            self._cleanup_script(script)
            return {"error": "运行失败：启动 Python 失败。"}
        timed_out = False
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._kill_group(proc)
            try:
                out, err = proc.communicate(timeout=KILL_WAIT_SECONDS)
            except Exception:
                out, err = b"", b""
        except Exception as e:
            self.ctx.log.warning("python_run 执行异常: %s", e)
            self._kill_group(proc)
            self._cleanup_script(script)
            return {"error": "运行失败：执行过程中出错了。"}
        finally:
            self._cleanup_script(script)
        return {
            "stdout": self._decode(out),
            "stderr": self._decode(err),
            "exit_code": int(proc.returncode if proc.returncode is not None else -1),
            "timed_out": timed_out,
            "duration": time.monotonic() - started,
        }

    @staticmethod
    def _decode(raw) -> str:
        try:
            return bytes(raw or b"").decode("utf-8", "replace")
        except Exception:
            return ""

    @staticmethod
    def _cleanup_script(script) -> None:
        if script is None:
            return
        try:
            Path(script).unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def _kill_group(proc) -> None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except Exception:
                pass

    # ---------------- 结果回复 ----------------
    def _format_result(self, result: dict) -> str:
        error = result.get("error")
        if error:
            return _truncate(f"{RESULT_TITLE}\n{error}", MAX_REPLY_CHARS)[0]
        if result.get("timed_out"):
            status = f"⏱ 超时（{self._timeout():g} 秒，已终止）"
        else:
            status = f"退出码：{result.get('exit_code', -1)}"
        header = f"{RESULT_TITLE}\n{status} · 耗时 {float(result.get('duration') or 0):.2f}s"
        stdout, out_cut = _truncate(result.get("stdout") or "", MAX_OUTPUT_CHARS)
        stderr, err_cut = _truncate(result.get("stderr") or "", MAX_OUTPUT_CHARS)
        parts = [header]
        if stdout.strip():
            parts.append("输出：\n```\n" + stdout.strip() + "\n```")
        elif not stderr.strip():
            parts.append("（没有输出）")
        if stderr.strip():
            parts.append("错误输出：\n```\n" + stderr.strip() + "\n```")
        text = "\n\n".join(parts)
        if out_cut or err_cut:
            text += "\n\n（输出较长，已截断）"
        return _truncate(text, MAX_REPLY_CHARS)[0]

    async def _reply(self, event, text: str):
        await self.ctx.reply(event, text)

    async def _reply_result(self, event, result: dict):
        await self._reply(event, self._format_result(result))

    # ---------------- 各种执行入口 ----------------
    async def _run_code(self, event, code: str) -> None:
        if len(code.encode("utf-8", "replace")) > MAX_CODE_BYTES:
            await self._reply(event, f"{RESULT_TITLE}\n代码太长了（超过 200KB）。")
            return
        result = await asyncio.to_thread(self._execute, code)
        await self._reply_result(event, result)

    async def _run_file(self, event, path: Path) -> None:
        try:
            path = Path(path).resolve()
        except OSError:
            await self._reply(event, f"{RESULT_TITLE}\n这个路径打不开，换一个再试。")
            return
        # 二次兜底：模糊匹配/符号链接也可能指到 data/files 外面
        if not _is_within(path, self.files_dir):
            await self._reply(event, f"{RESULT_TITLE}\n只能运行 data/files 里的 .py 文件。")
            return
        try:
            raw = path.read_bytes()
        except OSError as e:
            self.ctx.log.warning("python_run 读取文件失败: %s", e)
            await self._reply(event, f"{RESULT_TITLE}\n读取这个 .py 文件失败。")
            return
        if len(raw) > MAX_CODE_BYTES:
            await self._reply(event, f"{RESULT_TITLE}\n这个 .py 文件太大了（超过 200KB）。")
            return
        try:
            code = raw.decode("utf-8")
        except UnicodeDecodeError:
            await self._reply(event, f"{RESULT_TITLE}\n这个 .py 文件不是 UTF-8 文本，先转成 UTF-8 再试。")
            return
        await self._run_code(event, code)

    async def _run_file_segment(self, event, data: dict, name: str) -> None:
        data = data if isinstance(data, dict) else {}
        try:
            declared = int(str(data.get("file_size") or 0))
        except (TypeError, ValueError):
            declared = 0
        if declared > MAX_DOWNLOAD_BYTES:
            await self._reply(event, f"{RESULT_TITLE}\n这个 .py 文件太大了（超过 200KB）。")
            return
        url = str(data.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            fallback = str(data.get("file") or "").strip()
            url = fallback if fallback.startswith(("http://", "https://")) else ""
        if not url:
            await self._reply(event, f"{RESULT_TITLE}\n拿不到这个 .py 文件的下载地址，重新发一次文件试试。")
            return
        try:
            raw, _ctype = await asyncio.to_thread(
                media.fetch, url, MAX_DOWNLOAD_BYTES, DOWNLOAD_TIMEOUT
            )
        except Exception as e:
            self.ctx.log.info("python_run 下载 .py 文件失败: %s", e)
            await self._reply(event, f"{RESULT_TITLE}\n下载这个 .py 文件失败，重新发一次试试。")
            return
        raw = bytes(raw or b"")
        if len(raw) > MAX_DOWNLOAD_BYTES:
            await self._reply(event, f"{RESULT_TITLE}\n这个 .py 文件太大了（超过 200KB）。")
            return
        try:
            code = raw.decode("utf-8")
        except UnicodeDecodeError:
            await self._reply(event, f"{RESULT_TITLE}\n这个 .py 文件不是 UTF-8 文本，先转成 UTF-8 再试。")
            return
        target_name = _safe_download_name(name)
        try:
            path = files_mod.FileStore(self.data_dir).save_text(code, target_name)
        except OSError as e:
            self.ctx.log.warning("python_run 保存 .py 文件失败: %s", e)
            await self._reply(event, f"{RESULT_TITLE}\n保存这个 .py 文件失败，检查 data 目录权限。")
            return
        self.ctx.log.info("python_run 已下载并保存 .py：%s", target_name)
        await self._run_file(event, path)

    async def _handle_action(self, event, action) -> None:
        """执行 action：("code", code) / ("file", Path) / ("file_seg", data, name) / ("error", msg)。"""
        kind = action[0]
        if kind == "error":
            await self._reply(event, f"{RESULT_TITLE}\n{action[1]}")
            return
        if not await self._allowed(event):
            await self._reply(event, PERMISSION_TEXT)
            return
        if kind == "recent":
            path = action[1]
            await self._reply(event, f"运行最近的代码文件：{Path(path).name}")
            await self._run_file(event, path)
            return
        if kind == "code":
            await self._run_code(event, str(action[1]))
        elif kind == "file":
            await self._run_file(event, action[1])
        elif kind == "file_seg":
            await self._run_file_segment(event, action[1], action[2])

    # ---------------- 主入口 ----------------
    async def on_event(self, event) -> bool:
        if event.get("post_type") != "message":
            return False
        cfg = self._cfg()
        if cfg.get("enabled") is False:
            return False
        message_type = event.get("message_type")
        if message_type not in ("group", "private"):
            return False
        is_group = message_type == "group"
        self_id = getattr(self.ctx, "self_id", 0)
        addressed = (not is_group) or bool(self_id and mentioned(event.get("message"), self_id))
        has_reply = self._has_reply(event)
        text = (
            text_of(strip_at(event.get("message"), self_id)).strip()
            if self_id
            else text_of(event.get("message"))
        )
        command, arg = self._parse_command(text)

        # 群里：明确的 /py、/run 命令可以直接用（执行前仍查权限）；自然语言必须 @ 机器人，
        # 或者引用了一条带 python 代码/文件的消息（回复「运行」的工作流）。
        if is_group and not addressed and not has_reply and not command:
            return False
        quoted = await self._quoted(event) if has_reply else None
        quoted_action = self._quoted_action(quoted)
        quoted_path = (
            self._quoted_filename(quoted)
            if quoted is not None and quoted_action is None
            else None
        )
        # 群聊：引用消息里能解析到 .py 文件名（或代码/文件）也算可处理，不强制 @
        if (
            is_group
            and not addressed
            and quoted_action is None
            and quoted_path is None
            and not command
        ):
            return False

        if command == "py":
            code = ""
            if arg:
                code = self._code_from_arg(arg)
            if not code:
                code = self._quoted_code(quoted)
            if code:
                await self._handle_action(event, ("code", code))
                return True
            if quoted_action is not None and quoted_action[0] == "file_seg":
                await self._handle_action(event, quoted_action)
                return True
            if quoted_path is not None:
                await self._handle_action(event, ("file", quoted_path))
                return True
            await self._reply(event, USAGE_TEXT)
            return True

        if command == "run":
            if arg:
                path, error = self._resolve_run_arg(arg)
                await self._handle_action(
                    event, ("file", path) if path is not None else ("error", error)
                )
                return True
            if quoted_action is not None:
                await self._handle_action(event, quoted_action)
                return True
            if quoted_path is not None:
                await self._handle_action(event, ("file", quoted_path))
                return True
            await self._reply(event, USAGE_TEXT)
            return True

        # 自然语言：「运行/执行/跑/run」或「再试试/跑一下」这类追问
        if not (_NL_RUN_RE.search(text) or _TRY_RE.search(text)):
            return False
        if quoted_action is not None:
            await self._handle_action(event, quoted_action)
            return True
        if quoted_path is not None:
            await self._handle_action(event, ("file", quoted_path))
            return True
        path = self._find_file_in_text(text)
        if path is not None:
            await self._handle_action(event, ("file", path))
            return True
        # 没有明确目标时，「再试试/跑一下」自动跑最近 30 分钟内的 .py
        if _TRY_RE.search(text):
            recent = self._recent_py_file()
            if recent is not None:
                await self._handle_action(event, ("recent", recent))
                return True
        # 只提到 python/py/代码：给用法提示，不再让 AI 瞎答
        if _PY_WORD_RE.search(text):
            await self._reply(event, USAGE_TEXT)
            return True
        return False

    @staticmethod
    def _code_from_arg(arg: str) -> str:
        text = str(arg or "").strip()
        if not text:
            return ""
        blocks = _python_blocks(text, allow_plain=True)
        if blocks:
            return str(blocks[0].get("code") or "").strip("\n")
        return text


def _is_within(path: Path, base: Path) -> bool:
    """path 是否在 base 内（含等于）；用 resolved 路径判断，防 ../ 越界。"""
    try:
        path = Path(path).resolve()
        base = Path(base).resolve()
    except OSError:
        return False
    if path == base:
        return True
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False
