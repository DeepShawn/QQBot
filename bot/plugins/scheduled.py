"""``/报时``：立即执行一次定时问候（主对话模型写问候 + 画图模型出图，合成一条消息）。

私聊直接可用；群里只允许 bot.admins 或群主/群管理员。
实际逻辑在 ``bot/scheduled.py``（main.py 每 30 秒 tick 一次）。
"""
from __future__ import annotations

import re

import scheduled as scheduled_core
from dispatcher import PluginBase
from utils import strip_at, text_of

PERMISSION_TEXT = "定时发送只有管理员能用"
_CMD_RE = re.compile(r"^/报时\b\s*(.*)$", re.S)


class Plugin(PluginBase):
    name = "scheduled"
    priority = 25

    def __init__(self, ctx):
        super().__init__(ctx)
        try:
            self.ctx.log.info("scheduled 插件已加载（/报时）")
        except Exception:
            pass

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
            self.ctx.log.info("scheduled 查询群成员失败: %s", e)
            return False
        role = str((info or {}).get("role") or "").lower() if isinstance(info, dict) else ""
        return role in ("owner", "admin")

    @staticmethod
    def _format_result(result: dict) -> str:
        if result.get("sent"):
            target = result.get("target") or {}
            where = "群" if target.get("type") == "group" else "私聊"
            image_note = "（含图片）" if result.get("image") else "（仅有文字）"
            return f"⏰ 已发送定时问候到{where} {target.get('id', '')}{image_note}"
        reason = str(result.get("reason") or "")
        if reason == "disabled":
            return "⏰ 定时发送已在配置里关闭"
        if reason == "no-target":
            return "⏰ 没有可用的发送目标：请先在配置里设置 target_type/target_id，或让机器人有一个私聊/管理员"
        if reason == "not-due":
            return "⏰ 还没到发送时间"
        return "⏰ 定时问候发送失败，稍后再试"

    async def on_event(self, event) -> bool:
        if event.get("post_type") != "message":
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
        if not await self._allowed(event):
            await self.ctx.reply(event, PERMISSION_TEXT)
            return True
        self.ctx.log.info("scheduled /报时 手动触发")
        try:
            result = await scheduled_core.run_once(self.ctx)
        except Exception as e:
            self.ctx.log.warning("scheduled /报时 执行异常: %s", e)
            result = {"sent": False, "reason": "send-failed"}
        await self.ctx.reply(event, self._format_result(result))
        return True
