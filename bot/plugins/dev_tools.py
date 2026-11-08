"""开发者工具插件：``/sh <命令>`` 在手机 Ubuntu 环境执行 shell（私聊直接可用）。

群里只允许 bot.admins 或群主/群管理员（与 /py 等命令相同的权限门）。
执行逻辑在 ``bot/shelltool.py``：/bin/bash -lc、cwd 默认仓库根、超时 killpg、
输出截断；配置 ``plugins.dev_tools.git_token`` 会以 GITHUB_TOKEN/GH_TOKEN 注入
子进程环境（不写日志），``repo_path`` 可作为默认 cwd。
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import shelltool
from dispatcher import PluginBase
from utils import mentioned, strip_at, text_of

RESULT_TITLE = "🛠 Shell 运行结果"
PERMISSION_TEXT = "运行 Shell 只有管理员能用"
USAGE_TEXT = "🛠 用法：/sh <命令>，例如 /sh ls -la 或 /sh git status"
DEFAULT_TIMEOUT = shelltool.DEFAULT_TIMEOUT
MAX_OUTPUT_CHARS = 1200
MAX_REPLY_CHARS = 3000

_CMD_RE = re.compile(r"^/sh\b\s*(.*)$", re.I | re.S)


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    text = str(text or "")
    if len(text) <= limit:
        return text, False
    suffix = "\n…（内容过长，已截断）"
    keep = max(0, limit - len(suffix))
    return text[:keep] + suffix, True


class Plugin(PluginBase):
    name = "dev_tools"
    priority = 15  # 与 python_run 同优先级，排在 ai_chat 之前

    def __init__(self, ctx):
        super().__init__(ctx)
        self.data_dir = Path(ctx.data_dir)
        try:
            self.ctx.log.info("dev_tools 已加载（/sh）")
        except Exception:
            pass

    # ---------------- 配置 ----------------
    def _cfg(self) -> dict:
        try:
            return self.ctx.plugin_cfg("dev_tools") or {}
        except Exception:
            return {}

    def _timeout(self) -> float:
        try:
            value = float(self._cfg().get("timeout_seconds", DEFAULT_TIMEOUT))
        except (TypeError, ValueError):
            value = DEFAULT_TIMEOUT
        return max(shelltool.MIN_TIMEOUT, min(shelltool.MAX_TIMEOUT, value))

    def _cwd(self):
        repo_path = str(self._cfg().get("repo_path") or "").strip()
        return repo_path or None  # None -> shelltool 用仓库根

    def _env_extra(self):
        token = str(self._cfg().get("git_token") or "").strip()
        if not token:
            return None
        return {"GITHUB_TOKEN": token, "GH_TOKEN": token}

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
            self.ctx.log.info("dev_tools 查询群成员失败: %s", e)
            return False
        role = str((info or {}).get("role") or "").lower() if isinstance(info, dict) else ""
        return role in ("owner", "admin")

    # ---------------- 回复 ----------------
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

    # ---------------- 主入口 ----------------
    async def on_event(self, event) -> bool:
        if event.get("post_type") != "message":
            return False
        if self._cfg().get("enabled") is False:
            return False
        message_type = event.get("message_type")
        if message_type not in ("group", "private"):
            return False
        self_id = getattr(self.ctx, "self_id", 0)
        text = (
            text_of(strip_at(event.get("message"), self_id)).strip()
            if self_id
            else text_of(event.get("message"))
        )
        match = _CMD_RE.match(text)
        if not match:
            return False
        command = (match.group(1) or "").strip()
        if not command:
            await self.ctx.reply(event, USAGE_TEXT)
            return True
        if not await self._allowed(event):
            await self.ctx.reply(event, PERMISSION_TEXT)
            return True
        # 命令内容不写日志（可能带密钥），只记一条执行信息
        self.ctx.log.info("dev_tools 执行 /sh 命令（长度 %d）", len(command))
        result = await asyncio.to_thread(
            shelltool.run_shell,
            command,
            self._cwd(),
            self._timeout(),
            self._env_extra(),
        )
        await self.ctx.reply(event, self._format_result(result))
        return True
