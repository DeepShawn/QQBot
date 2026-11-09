"""机器人上下文：配置、日志、数据目录、发消息的便捷方法。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import safety
from utils import at_seg, fmt, text_seg


class Context:
    def __init__(self, config, logger, data_dir, logs_dir):
        self.config = config          # Config 对象（带热更新）
        self.log = logger
        self.data_dir = Path(data_dir)
        self.logs_dir = Path(logs_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.api = None               # OneBotClient，main 里注入
        self.self_id = 0
        self.started_at = None

    # ---------- 配置快捷访问 ----------
    @property
    def cfg(self) -> dict:
        return self.config.data

    @property
    def bot_name(self) -> str:
        return self.cfg.get("bot", {}).get("name", "小助手")

    def plugin_cfg(self, name: str) -> dict:
        return (self.cfg.get("plugins") or {}).get(name) or {}

    def is_admin(self, user_id) -> bool:
        admins = self.cfg.get("bot", {}).get("admins") or []
        return str(user_id) in {str(a) for a in admins}

    # ---------- 发消息 ----------
    async def _low_risk_message(self, message, *, is_group: bool, group_id=None):
        """防封模式处理：群白名单 → 全局限频 → 随机延迟 → 内容过滤。

        返回处理后的 message；返回 None 表示这条消息应丢弃（白名单/限频）。
        非 low_risk 时直接原样返回，不做任何处理。
        """
        data = self.cfg if isinstance(self.cfg, dict) else {}
        raw = data.get("safety") or {}
        if not safety.is_low_risk(raw):
            return message
        conf = safety.normalize(raw)
        if is_group and not safety.group_allowed(group_id, conf):
            self.log.info("防封模式：群 %s 不在白名单，已跳过发送", group_id)
            return None
        if not await safety.wait_slot(conf):
            self.log.warning("防封模式：发送频率超出上限，丢弃一条消息")
            return None
        delay = safety.reply_delay(conf)
        if delay > 0:
            await asyncio.sleep(delay)
        return safety.sanitize_message(message, is_group=is_group, cfg=conf)

    async def send_private(self, user_id, message):
        prepared = await self._low_risk_message(message, is_group=False)
        if prepared is None:
            return None
        return await self.api.call("send_private_msg", user_id=int(user_id), message=prepared)

    async def send_group(self, group_id, message):
        prepared = await self._low_risk_message(message, is_group=True, group_id=group_id)
        if prepared is None:
            return None
        return await self.api.call("send_group_msg", group_id=int(group_id), message=prepared)

    def _with_at(self, message, at_qq):
        msg = message if isinstance(message, list) else [text_seg(message)]
        if at_qq:
            return [at_seg(at_qq), text_seg(" ")] + msg
        return msg

    async def reply(self, event, message, at: bool = False):
        """回复到事件来源（群或私聊）。at=True 时在群里 @ 发言人。"""
        if not isinstance(message, list):
            message = [text_seg(str(message))]
        if event.get("message_type") == "group":
            group_id = event.get("group_id")
            at_qq = event.get("user_id") if at else None
            return await self.send_group(group_id, self._with_at(message, at_qq))
        user_id = event.get("user_id")
        return await self.send_private(user_id, message)

    async def recall(self, message_id):
        try:
            await self.api.call("delete_msg", message_id=message_id)
            return True
        except Exception as e:
            self.log.warning("撤回失败 message_id=%s: %s", message_id, e)
            return False

    async def mute(self, group_id, user_id, seconds: int):
        try:
            await self.api.call("set_group_ban", group_id=int(group_id), user_id=int(user_id), duration=int(seconds))
            return True
        except Exception as e:
            self.log.warning("禁言失败 %s/%s: %s", group_id, user_id, e)
            return False

    async def kick(self, group_id, user_id, reject: bool = False):
        try:
            await self.api.call("set_group_kick", group_id=int(group_id), user_id=int(user_id), reject_add_request=reject)
            return True
        except Exception as e:
            self.log.warning("踢人失败 %s/%s: %s", group_id, user_id, e)
            return False

    async def group_name(self, group_id) -> str:
        try:
            info = await self.api.call("get_group_info", group_id=int(group_id))
            return (info or {}).get("group_name", str(group_id))
        except Exception:
            return str(group_id)

    # ---------- 事件里的常用字段 ----------
    @staticmethod
    def nickname(event) -> str:
        sender = event.get("sender") or {}
        return sender.get("card") or sender.get("nickname") or str(event.get("user_id", ""))

    def format_text(self, template: str, event, **extra) -> str:
        kwargs = {
            "bot_name": self.bot_name,
            "nickname": self.nickname(event),
            "user_id": event.get("user_id", ""),
            "group_id": event.get("group_id", ""),
        }
        kwargs.update(extra)
        return fmt(template, **kwargs)

    # ---------- 数据持久化 ----------
    def load_json(self, name: str, default):
        p = self.data_dir / name
        if not p.exists():
            return default
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return default

    def save_json(self, name: str, data):
        p = self.data_dir / name
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
