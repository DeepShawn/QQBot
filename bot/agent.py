"""长任务代理：``/task <需求>`` 在后台跑「模型 <-> 工具」循环，实时汇报进度。

工具：
- ``run_shell``：bot/shelltool.py（/bin/bash -lc，超时 killpg）；
- ``run_python``：bot/pytool.py（隔离 Python）；
- ``install_package``：apt-get / pip 安装缺失环境（命令不存在、No module named 时使用）。

模型调用两种方式：
- OpenAI 兼容 + API Key：providers.chat_with_tools 原生 function calling；
- 账号 / 非 OpenAI：providers.chat + JSON 动作协议。
最多 10 步、总时长 10 分钟；过程消息限流约 15 条；权限与 /sh 等工具一致
（私聊直接；群里 bot.admins 或群主/群管理员）。只依赖 Python 标准库。
"""
from __future__ import annotations

import asyncio
import json
import re
import shlex
import sys
import time
from pathlib import Path

import providers
import pytool
import shelltool

MAX_STEPS = 10
MAX_TOTAL_SECONDS = 600.0
MAX_PROGRESS_MESSAGES = 15
SUMMARY_CHARS = 300
MODEL_TEMPERATURE = 0.3
MODEL_MAX_TOKENS = 1500
MODEL_TIMEOUT = 120.0
SHELL_TIMEOUT = 60.0
PYTHON_TIMEOUT = 120.0
INSTALL_TIMEOUT = 300.0
TOOL_RESULT_LIMIT = 4000

_SAFE_PACKAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+:/@=-]{0,120}$")

__all__ = ["run_task", "can_use", "task_key", "TOOL_SPECS", "MAX_STEPS"]


TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": "在手机本机 Ubuntu 环境执行 shell 命令。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的 shell 命令"},
                    "timeout": {
                        "type": "integer",
                        "description": "超时秒数，可选，默认 60，最大 3600",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": "运行一段 Python 代码并返回 stdout/stderr。",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "要运行的 Python 源码"},
                    "timeout": {"type": "integer", "description": "超时秒数，可选"},
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "install_package",
            "description": (
                "安装缺失的运行环境。命令不存在（command not found）时用 apt 安装，"
                "Python 模块不存在（No module named）时用 pip 安装；装完再重试原命令。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "manager": {"type": "string", "enum": ["apt", "pip"]},
                    "name": {
                        "type": "string",
                        "description": "包名，例如 ffmpeg 或 Pillow",
                    },
                },
                "required": ["manager", "name"],
            },
        },
    },
]

_SYSTEM_BASE = (
    "你是 QQ 机器人里的长任务代理，在手机 Ubuntu 运行环境里用工具完成用户需求。\n"
    "规则：\n"
    "1. 每次只做一件小事，做完再根据结果决定下一步；\n"
    "2. 遇到 command not found / No module named 等缺环境错误时，先调用 install_package "
    "安装再重试（ffmpeg 等命令用 apt，Pillow/requests 等 Python 包用 pip），不要直接放弃；\n"
    "3. 任务完成后不要继续调用工具，直接用一段简短中文总结结果。"
)

_JSON_PROTOCOL = (
    "\n你只能输出一个 JSON 对象，不要解释、不要代码块：\n"
    '{"action":"run_shell","command":"shell 命令"}\n'
    '{"action":"run_python","code":"python 代码"}\n'
    '{"action":"install_package","manager":"apt","name":"ffmpeg"}\n'
    '{"action":"final","summary":"任务完成的中文总结"}\n'
)


# ---------------------------------------------------------------- 权限/键


async def can_use(ctx, event) -> bool:
    """私聊直接允许；群里只允许 bot.admins 或群主/群管理员。"""
    if event.get("message_type") != "group":
        return True
    user_id = event.get("user_id")
    try:
        if ctx.is_admin(user_id):
            return True
    except Exception:
        pass
    api = getattr(ctx, "api", None)
    if api is None:
        return False
    try:
        info = await api.call(
            "get_group_member_info",
            group_id=int(event.get("group_id") or 0),
            user_id=int(user_id or 0),
            no_cache=True,
        )
    except Exception:
        return False
    role = str((info or {}).get("role") or "").lower() if isinstance(info, dict) else ""
    return role in ("owner", "admin")


def task_key(event) -> str:
    if event.get("message_type") == "group":
        return f"group:{event.get('group_id')}"
    return f"private:{event.get('user_id')}"


# ---------------------------------------------------------------- 小工具


def _short(text, limit: int = SUMMARY_CHARS) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    return value[:limit] + ("…" if len(value) > limit else "")


def _parse_action(text) -> dict | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    decoder = json.JSONDecoder()
    index = raw.find("{")
    while index >= 0:
        try:
            value, _end = decoder.raw_decode(raw[index:])
        except Exception:
            value = None
        if isinstance(value, dict) and value.get("action"):
            return value
        index = raw.find("{", index + 1)
    return None


def _model_kwargs(ai_cfg: dict) -> dict:
    auth = str(ai_cfg.get("auth") or "").strip()
    model = str(ai_cfg.get("model") or "").strip()
    if not model and not auth:
        model = "deepseek-chat"
    return {
        "provider": str(ai_cfg.get("provider") or "openai"),
        "base_url": str(ai_cfg.get("base_url") or ""),
        "api_key": str(ai_cfg.get("api_key") or ""),
        "model": model,
        "auth": auth,
    }


def _native_supported(ai_cfg: dict) -> bool:
    if str(ai_cfg.get("auth") or "").strip():
        return False
    provider = providers.normalize_provider(str(ai_cfg.get("provider") or "openai"))
    return provider == "openai"


def _result_summary(result: dict) -> str:
    if not isinstance(result, dict):
        return _short(result)
    if result.get("error"):
        return "失败：" + str(result["error"])
    if result.get("timed_out"):
        return "超时（已终止）"
    out = str(result.get("stdout") or "").strip()
    err = str(result.get("stderr") or "").strip()
    code = result.get("exit_code")
    base = out or err or "完成"
    if code not in (0, None):
        return f"退出码 {code}：" + base
    return base


def _env_hint(result: dict) -> str:
    text = ""
    if isinstance(result, dict):
        text = f"{result.get('stderr') or ''}\n{result.get('error') or ''}".lower()
    if "command not found" in text or "no module named" in text:
        return "\n提示：可能缺少依赖，可调用 install_package 安装后重试。"
    return ""


# ---------------------------------------------------------------- 工具执行


def _clamp(number, default: float, low: float, high: float) -> float:
    try:
        value = float(number)
    except (TypeError, ValueError):
        value = default
    if value != value:
        value = default
    return max(low, min(high, value))


def _install_package(manager, name) -> dict:
    manager_text = str(manager or "").strip().lower()
    package = str(name or "").strip()
    if manager_text not in ("apt", "pip"):
        return {"error": "manager 只能是 apt 或 pip"}
    if not package or not _SAFE_PACKAGE_RE.match(package) or package.startswith("-"):
        return {"error": "包名不合法"}
    if manager_text == "apt":
        command = f"apt-get update && apt-get install -y {package}"
    else:
        command = (
            f"{shlex.quote(sys.executable)} -m pip install --no-input "
            f"{shlex.quote(package)}"
        )
    result = shelltool.run_shell(command, timeout=INSTALL_TIMEOUT)
    if isinstance(result, dict):
        result["manager"] = manager_text
        result["name"] = package
    return result


def _execute_action(action: dict, data_dir=None) -> dict:
    if not isinstance(action, dict):
        return {"error": "动作格式不对"}
    act = str(action.get("action") or "").strip().lower()
    if act == "run_shell":
        command = str(action.get("command") or "").strip()
        if not command:
            return {"error": "缺少 command 参数"}
        timeout = _clamp(action.get("timeout"), SHELL_TIMEOUT, 1.0, 3600.0)
        return shelltool.run_shell(command, timeout=timeout)
    if act == "run_python":
        code = str(action.get("code") or "").strip()
        if not code:
            return {"error": "缺少 code 参数"}
        timeout = _clamp(action.get("timeout"), PYTHON_TIMEOUT, 1.0, 600.0)
        return pytool.execute_python(
            code=code, timeout=timeout, data_dir=data_dir
        )
    if act == "install_package":
        return _install_package(action.get("manager"), action.get("name"))
    return {"error": f"未知动作：{act}"}


# ---------------------------------------------------------------- 进度


class _Progress:
    def __init__(self, ctx, event):
        self.ctx = ctx
        self.event = event
        self.count = 0

    async def say(self, text: str, final: bool = False) -> None:
        if not final and self.count >= MAX_PROGRESS_MESSAGES:
            return
        if not final:
            self.count += 1
        try:
            await self.ctx.reply(self.event, text)
        except Exception:
            pass


def _action_label(action: dict) -> str:
    act = str(action.get("action") or "").strip().lower()
    if act == "run_shell":
        return "执行命令：" + _short(action.get("command"), 80)
    if act == "run_python":
        return "运行 Python 代码：" + _short(
            str(action.get("code") or "").replace("\n", " "), 60
        )
    if act == "install_package":
        return f"安装 {action.get('manager')} 包：{action.get('name')}"
    return f"执行动作：{act}"


async def _before_step(progress: _Progress, step_no: int, action: dict) -> None:
    await progress.say(f"🛠 第{step_no}步：{_action_label(action)}")
    if str(action.get("action") or "").strip().lower() == "install_package":
        await progress.say(f"📦 正在安装 {action.get('name')}…")


async def _after_step(progress: _Progress, step_no: int, result: dict) -> None:
    await progress.say(f"✅ 第{step_no}步完成：{_short(_result_summary(result))}")


async def _finish_ok(progress: _Progress, summary: str) -> dict:
    text = _short(summary or "任务完成")
    await progress.say(f"🎉 任务完成：{text}", final=True)
    return {"ok": True, "summary": text}


async def _finish_fail(progress: _Progress, reason: str) -> dict:
    text = _short(reason or "未知原因")
    await progress.say(f"⚠️ 任务未完成：{text}", final=True)
    return {"ok": False, "reason": text}


# ---------------------------------------------------------------- 主流程


def _tool_content(result: dict) -> str:
    text = json.dumps(result, ensure_ascii=False)
    if len(text) > TOOL_RESULT_LIMIT:
        text = text[:TOOL_RESULT_LIMIT] + "…（结果过长截断）"
    return text + _env_hint(result)


async def _run_native(
    ctx, event, ai_cfg: dict, prompt: str, progress: _Progress
) -> dict:
    kwargs = _model_kwargs(ai_cfg)
    started = time.monotonic()
    history = [
        {"role": "system", "content": _SYSTEM_BASE},
        {"role": "user", "content": str(prompt)},
    ]
    step_no = 0
    for _round in range(MAX_STEPS):
        if time.monotonic() - started > MAX_TOTAL_SECONDS:
            return await _finish_fail(progress, "超过总时长上限")
        try:
            result = await asyncio.to_thread(
                providers.chat_with_tools,
                **kwargs,
                messages=history,
                tools=TOOL_SPECS,
                temperature=MODEL_TEMPERATURE,
                max_tokens=MODEL_MAX_TOKENS,
                timeout=MODEL_TIMEOUT,
            )
        except providers.ProviderError as e:
            return await _finish_fail(progress, f"模型调用失败：{e}")
        content = str(result.get("content") or "").strip()
        calls = [c for c in (result.get("tool_calls") or []) if isinstance(c, dict)]
        if not calls:
            return await _finish_ok(progress, content or "任务完成")
        assistant_calls = []
        for index, call in enumerate(calls):
            assistant_calls.append(
                {
                    "id": str(call.get("id") or f"call_{_round}_{index}"),
                    "type": "function",
                    "function": {
                        "name": str(call.get("name") or ""),
                        "arguments": str(call.get("arguments") or "{}"),
                    },
                }
            )
        history.append(
            {
                "role": "assistant",
                "content": content or None,
                "tool_calls": assistant_calls,
            }
        )
        for index, call in enumerate(calls):
            if step_no >= MAX_STEPS:
                return await _finish_fail(progress, f"达到 {MAX_STEPS} 步上限")
            step_no += 1
            try:
                arguments = json.loads(str(call.get("arguments") or "{}"))
            except Exception:
                arguments = {}
            if not isinstance(arguments, dict):
                arguments = {}
            action = {"action": str(call.get("name") or ""), **arguments}
            await _before_step(progress, step_no, action)
            output = await asyncio.to_thread(
                _execute_action, action, getattr(ctx, "data_dir", None)
            )
            await _after_step(progress, step_no, output)
            history.append(
                {
                    "role": "tool",
                    "tool_call_id": assistant_calls[index]["id"],
                    "content": _tool_content(output),
                }
            )
    return await _finish_fail(progress, f"达到 {MAX_STEPS} 步上限")


async def _run_json(
    ctx, event, ai_cfg: dict, prompt: str, progress: _Progress
) -> dict:
    kwargs = _model_kwargs(ai_cfg)
    started = time.monotonic()
    history = [
        {"role": "system", "content": _SYSTEM_BASE + _JSON_PROTOCOL},
        {"role": "user", "content": str(prompt)},
    ]
    for step_no in range(1, MAX_STEPS + 1):
        if time.monotonic() - started > MAX_TOTAL_SECONDS:
            return await _finish_fail(progress, "超过总时长上限")
        try:
            raw = await asyncio.to_thread(
                providers.chat,
                **kwargs,
                messages=history,
                temperature=MODEL_TEMPERATURE,
                max_tokens=MODEL_MAX_TOKENS,
                timeout=MODEL_TIMEOUT,
            )
        except providers.ProviderError as e:
            return await _finish_fail(progress, f"模型调用失败：{e}")
        action = _parse_action(raw)
        if action is None:
            history.append({"role": "assistant", "content": str(raw or "")})
            history.append(
                {
                    "role": "user",
                    "content": "上一条不是合法 JSON。请只输出 JSON 动作；如果任务已完成，输出 final。",
                }
            )
            continue
        if str(action.get("action") or "").strip().lower() == "final":
            summary = str(action.get("summary") or "").strip() or str(raw or "")
            return await _finish_ok(progress, summary)
        await _before_step(progress, step_no, action)
        output = await asyncio.to_thread(
            _execute_action, action, getattr(ctx, "data_dir", None)
        )
        await _after_step(progress, step_no, output)
        history.append({"role": "assistant", "content": str(raw or "")})
        history.append(
            {
                "role": "user",
                "content": "工具执行结果：" + _tool_content(output),
            }
        )
    return await _finish_fail(progress, f"达到 {MAX_STEPS} 步上限")


async def run_task(ctx, event, prompt: str) -> dict:
    """在后台任务里执行长任务；进度/结果用 ctx.reply 发到当前会话。"""
    try:
        if not await can_use(ctx, event):
            return {"ok": False, "reason": "permission"}
        text = str(prompt or "").strip()
        if not text:
            return {"ok": False, "reason": "empty"}
        ai_cfg = ctx.plugin_cfg("ai_chat") or {}
        auth = str(ai_cfg.get("auth") or "").strip()
        api_key = str(ai_cfg.get("api_key") or "").strip()
        if not auth and not api_key:
            await ctx.reply(event, "⚠️ 任务未完成：AI 接口还没配置（填 API Key 或连接账号）")
            return {"ok": False, "reason": "not-configured"}
        progress = _Progress(ctx, event)
        if _native_supported(ai_cfg):
            return await _run_native(ctx, event, ai_cfg, text, progress)
        return await _run_json(ctx, event, ai_cfg, text, progress)
    except asyncio.CancelledError:
        try:
            await ctx.reply(event, "🛑 任务已停止。")
        except Exception:
            pass
        raise
