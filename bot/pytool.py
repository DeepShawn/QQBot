"""Python 执行工具：给 /py /run 插件和 AI 工具调用（run_python）共用。

对外接口::

    execute_python(code=None, file=None, timeout=30, data_dir=None) -> dict
        # 成功：{"stdout","stderr","exit_code","timed_out","duration"}
        # 失败：{"error": "中文短句"}
    resolve_py_file(file, data_dir=None) -> (Path | None, 错误文案)
    files_root(data_dir) -> Path

安全与限制（和插件/聊天工具共用同一套）：
- 只运行用户明确给出或被 AI 明确指定的 Python 代码；
- file 只能指向 data/files 内的 .py，≤200KB，UTF-8；
- 临时脚本写到 data/run，结束即删；
- 子进程用 sys.executable -I -u -X utf8，start_new_session，超时 killpg 整组；
- stdout/stderr 各截断 2000 字。
只依赖 Python 标准库。
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

DEFAULT_TIMEOUT = 30.0
MIN_TIMEOUT = 0.2
MAX_TIMEOUT = 120.0
KILL_WAIT_SECONDS = 5.0
MAX_FILE_BYTES = 200 * 1024
MAX_CODE_BYTES = 200 * 1024
MAX_OUTPUT_CHARS = 2000
FILES_DIR_NAME = "files"
RUN_DIR_NAME = "run"

__all__ = [
    "DEFAULT_TIMEOUT",
    "MAX_OUTPUT_CHARS",
    "files_root",
    "execute_python",
    "resolve_py_file",
]


# ---------------------------------------------------------------- 路径/输出工具


def _default_data_dir() -> Path:
    return Path(__file__).resolve().parent / "data"


def files_root(data_dir: Path | str | None = None) -> Path:
    base = Path(data_dir) if data_dir is not None else _default_data_dir()
    return base / FILES_DIR_NAME


def _is_within(path: Path, base: Path) -> bool:
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


def _iter_py_files(root: Path) -> list[Path]:
    try:
        if not root.is_dir():
            return []
        return sorted(
            (p for p in root.rglob("*.py") if p.is_file()),
            key=lambda p: len(p.name),
        )
    except OSError:
        return []


def _truncate_output(text: str) -> str:
    text = str(text or "")
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    suffix = "\n…（输出过长，已截断）"
    return text[: max(0, MAX_OUTPUT_CHARS - len(suffix))] + suffix


def _decode(raw) -> str:
    try:
        return bytes(raw or b"").decode("utf-8", "replace")
    except Exception:
        return ""


# ---------------------------------------------------------------- 文件解析


def resolve_py_file(file, data_dir: Path | str | None = None) -> tuple[Path | None, str]:
    """把 file 参数解析成 data/files 内的 .py 路径 -> (path, 错误文案)。"""
    raw = str(file or "").strip().strip("\"'")
    if raw.startswith("file://"):
        raw = raw[len("file://"):]
    if not raw:
        return None, "没有指定 .py 文件。"

    base = Path(data_dir) if data_dir is not None else _default_data_dir()
    root = files_root(base)
    try:
        root_resolved = root.resolve()
    except OSError:
        root_resolved = root

    def _check(candidate: Path) -> tuple[Path | None, str]:
        try:
            target = candidate.resolve()
        except OSError:
            return None, "这个路径打不开，换一个再试。"
        if not _is_within(target, root_resolved):
            return None, "只能运行 data/files 里的 .py 文件。"
        if not target.is_file():
            return None, "没找到这个 .py 文件。"
        if target.suffix.lower() != ".py":
            return None, "只能运行 .py 文件。"
        return target, ""

    candidate = Path(raw)
    if candidate.is_absolute() or "/" in raw or "\\" in raw or raw.startswith(".."):
        # 允许 "data/files/x.py"、"files/x.py" 这类相对写法，逐个候选试；
        # 带 .. 的输入不做 basename 兜底，避免看起来像越界却被悄悄替换成别的文件
        candidates = [candidate]
        if not candidate.is_absolute():
            candidates = [candidate, base / candidate, root / candidate]
            if ".." not in raw:
                candidates.append(root / Path(raw).name)
        last_error = ""
        for item in candidates:
            path, error = _check(item)
            if path is not None:
                return path, ""
            last_error = error or last_error
        return None, last_error or "只能运行 data/files 里的 .py 文件。"

    # 只给文件名：先精确，再前缀、包含匹配
    wanted = Path(raw).name.lower()
    files = _iter_py_files(root)
    for matcher in (
        lambda p: p.name.lower() == wanted,
        lambda p: p.name.lower() == wanted + ".py",
        lambda p: p.name.lower().startswith(wanted),
        lambda p: wanted in p.name.lower(),
    ):
        for path in files:
            if matcher(path):
                return path, ""
    return None, f"没找到 {raw} 这个 .py 文件。可以发文件后 /run 文件名，或直接 /py 贴代码。"


# ---------------------------------------------------------------- 执行


def _clamp_timeout(timeout) -> float:
    try:
        value = float(timeout)
    except (TypeError, ValueError):
        value = DEFAULT_TIMEOUT
    unknown = value != value  # NaN
    if unknown:
        value = DEFAULT_TIMEOUT
    return max(MIN_TIMEOUT, min(MAX_TIMEOUT, value))


def execute_python(
    code: str | None = None,
    file: Path | str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    data_dir: Path | str | None = None,
) -> dict:
    """运行一段 Python 代码或 data/files 里的 .py，返回结果 dict（不抛异常）。"""
    base = Path(data_dir) if data_dir is not None else _default_data_dir()
    source = ""
    if code is not None:
        if not isinstance(code, str) or not code.strip():
            return {"error": "没有要运行的代码。"}
        source = code
    elif file is not None:
        path, error = resolve_py_file(file, base)
        if path is None:
            return {"error": error or "找不到这个 .py 文件。"}
        try:
            size = path.stat().st_size
        except OSError:
            return {"error": "读取这个 .py 文件失败。"}
        if size > MAX_FILE_BYTES:
            return {"error": "这个 .py 文件太大了（超过 200KB）。"}
        try:
            raw = path.read_bytes()
        except OSError:
            return {"error": "读取这个 .py 文件失败。"}
        try:
            source = raw.decode("utf-8")
        except UnicodeDecodeError:
            return {"error": "这个 .py 文件不是 UTF-8 文本，先转成 UTF-8 再试。"}
    else:
        return {"error": "没有要运行的代码或文件。"}

    if len(source.encode("utf-8", "replace")) > MAX_CODE_BYTES:
        return {"error": "代码太长了（超过 200KB）。"}

    run_dir = base / RUN_DIR_NAME
    script = None
    started = time.monotonic()
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        script = run_dir / f"run_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}.py"
        script.write_text(source, encoding="utf-8")
    except OSError:
        return {"error": "运行失败：写临时脚本失败，检查 data 目录权限。"}

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "UTF-8"
    env["PYTHONUTF8"] = "1"
    env.pop("PYTHONSTARTUP", None)
    limit = _clamp_timeout(timeout)
    try:
        proc = subprocess.Popen(
            # -I: 隔离模式（忽略 PYTHON* 环境变量/用户 site）；-u: 不缓冲；
            # -X utf8: -I 会忽略 PYTHONIOENCODING，用它兜底中文输出
            [sys.executable, "-I", "-u", "-X", "utf8", str(script)],
            cwd=str(run_dir),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
    except OSError:
        _cleanup(script)
        return {"error": "运行失败：启动 Python 失败。"}

    timed_out = False
    try:
        out, err = proc.communicate(timeout=limit)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(proc)
        try:
            out, err = proc.communicate(timeout=KILL_WAIT_SECONDS)
        except Exception:
            out, err = b"", b""
    except Exception:
        _kill_group(proc)
        _cleanup(script)
        return {"error": "运行失败：执行过程中出错了。"}
    finally:
        _cleanup(script)

    return {
        "stdout": _truncate_output(_decode(out)),
        "stderr": _truncate_output(_decode(err)),
        "exit_code": int(proc.returncode if proc.returncode is not None else -1),
        "timed_out": timed_out,
        "duration": time.monotonic() - started,
    }


def _cleanup(script) -> None:
    if script is None:
        return
    try:
        Path(script).unlink(missing_ok=True)
    except OSError:
        pass


def _kill_group(proc) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except Exception:
            pass
