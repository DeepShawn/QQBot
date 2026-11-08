"""Shell 执行工具：给 /sh 插件和 AI run_shell 工具共用。

对外接口::

    run_shell(command, cwd=None, timeout=60, env_extra=None) -> dict
        # 成功：{"stdout","stderr","exit_code","timed_out","duration"}
        # 失败（参数/目录/启动问题）：{"error": "中文短句"}

约定：
- 用 ``/bin/bash -lc <command>`` 执行（登录 shell，能拿到用户 PATH）；
- cwd 默认仓库根目录（bot/..）；不存在时返回中文错误；
- timeout 限制在 1–3600 秒（默认 60）；
- Popen(start_new_session=True) + communicate，超时 killpg 整组；
- stdout/stderr 各截断 200KB；只依赖 Python 标准库；
- 不在日志/异常里输出环境变量（git_token 等通过 env_extra 注入子进程）。
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

DEFAULT_TIMEOUT = 60.0
MIN_TIMEOUT = 1.0
MAX_TIMEOUT = 3600.0
MAX_OUTPUT_BYTES = 200 * 1024
KILL_WAIT_SECONDS = 5.0

__all__ = [
    "DEFAULT_TIMEOUT",
    "MIN_TIMEOUT",
    "MAX_TIMEOUT",
    "MAX_OUTPUT_BYTES",
    "repo_root",
    "run_shell",
]


def repo_root() -> Path:
    """仓库根目录（bot/ 的上一级）。"""
    return Path(__file__).resolve().parent.parent


def _clamp_timeout(timeout) -> float:
    try:
        value = float(timeout)
    except (TypeError, ValueError):
        value = DEFAULT_TIMEOUT
    if value != value:  # NaN
        value = DEFAULT_TIMEOUT
    return max(MIN_TIMEOUT, min(MAX_TIMEOUT, value))


def _decode_limited(raw: bytes) -> str:
    data = bytes(raw or b"")
    over = len(data) > MAX_OUTPUT_BYTES
    if over:
        data = data[:MAX_OUTPUT_BYTES]
    text = data.decode("utf-8", "replace")
    if over:
        text += "\n…（输出过长，已截断）"
    return text


def run_shell(
    command: str,
    cwd=None,
    timeout: float = DEFAULT_TIMEOUT,
    env_extra: dict | None = None,
) -> dict:
    """执行一条 shell 命令，返回结果 dict（不抛异常）。"""
    text = str(command if command is not None else "")
    if not text.strip():
        return {"error": "命令为空。"}
    workdir = Path(cwd) if cwd is not None else repo_root()
    if not workdir.is_dir():
        return {"error": "工作目录不存在，检查配置里的仓库路径。"}
    limit = _clamp_timeout(timeout)
    env = dict(os.environ)
    if isinstance(env_extra, dict):
        for key, value in env_extra.items():
            if value is None:
                continue
            env[str(key)] = str(value)
    env.setdefault("PYTHONIOENCODING", "UTF-8")

    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            ["/bin/bash", "-lc", text],
            cwd=str(workdir),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
    except OSError:
        return {"error": "启动 shell 失败。"}

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
        return {"error": "执行 shell 时出错了。"}

    return {
        "stdout": _decode_limited(out),
        "stderr": _decode_limited(err),
        "exit_code": int(proc.returncode if proc.returncode is not None else -1),
        "timed_out": timed_out,
        "duration": time.monotonic() - started,
    }


def _kill_group(proc) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except Exception:
            pass
