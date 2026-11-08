"""群管理：入群欢迎、违禁词（撤回+禁言）、刷屏检测。"""
from __future__ import annotations

import re
import time
from collections import deque

from dispatcher import PluginBase
from utils import at_seg, mentioned, text_of, text_seg


class Plugin(PluginBase):
    name = "group_admin"
    priority = 50

    def __init__(self, ctx):
        super().__init__(ctx)
        self._flood_windows: dict[tuple, deque] = {}
        self._last_msg: dict[str, dict] = {}   # 群号 -> 最近一条群友消息

    # ---------- 小工具 ----------
    @staticmethod
    def _in_groups(cfg_groups, group_id) -> bool:
        groups = cfg_groups or []
        # 空列表 = 对所有群生效
        return not groups or str(group_id) in {str(g) for g in groups}

    @staticmethod
    def _is_exempt(rule_cfg: dict, event) -> bool:
        """选择性豁免：可开关「豁免群主/管理员」，也可指定豁免的 QQ 号。"""
        sender = event.get("sender") or {}
        if rule_cfg.get("ignore_admins") and sender.get("role") in ("admin", "owner"):
            return True
        uid = str(event.get("user_id") or "")
        exempt = {str(x) for x in (rule_cfg.get("ignore_users") or []) if str(x)}
        return bool(uid and uid in exempt)

    def _build(self, template: str, event, **extra):
        """把 {at} 换成真正的 @ 消息段，其它占位符走文本替换。"""
        parts = []
        template = str(template or "")
        for i, chunk in enumerate(template.split("{at}")):
            if i > 0:
                parts.append(at_seg(event.get("user_id")))
            chunk = self.ctx.format_text(chunk, event, **extra)
            if chunk:
                parts.append(text_seg(chunk))
        return parts or [text_seg(" ")]

    # ---------- 入群欢迎 ----------
    async def _welcome(self, event, cfg: dict):
        welcome = cfg.get("welcome") or {}
        if not welcome.get("enabled"):
            return False
        group_id = event.get("group_id")
        if not self._in_groups(welcome.get("groups"), group_id):
            return False
        message = self._build(welcome.get("template", "欢迎 {at} 加入本群！"), event)
        try:
            await self.ctx.send_group(group_id, message)
            self.ctx.log.info("欢迎新成员 %s -> 群 %s", event.get("user_id"), group_id)
        except Exception as e:
            self.ctx.log.warning("发送欢迎语失败: %s", e)
        return True

    # ---------- 违禁词 ----------
    async def _badwords(self, event, cfg: dict) -> bool:
        bw = cfg.get("badwords") or {}
        if not bw.get("enabled"):
            return False
        group_id = event.get("group_id")
        if not self._in_groups(bw.get("groups"), group_id):
            return False
        if self._is_exempt(bw, event):
            return False
        text = text_of(event.get("message"))
        if not text:
            return False
        hit = next((w for w in (bw.get("words") or []) if str(w) and str(w) in text), None)
        if not hit:
            for pat in (bw.get("regex_words") or []):
                try:
                    if pat and re.search(str(pat), text):
                        hit = f"正则:{pat}"
                        break
                except re.error:
                    continue
        if not hit:
            return False

        action = str(bw.get("action", "recall+mute"))
        mute_seconds = int(bw.get("mute_seconds", 300))
        self.ctx.log.info("违禁词命中 %r（群 %s，用户 %s）", hit, group_id, event.get("user_id"))

        if "recall" in action and event.get("message_id"):
            await self.ctx.recall(event.get("message_id"))
        if "mute" in action:
            await self.ctx.mute(group_id, event.get("user_id"), mute_seconds)
        if "kick" in action:
            await self.ctx.kick(group_id, event.get("user_id"))
        warn = bw.get("warn_text")
        if warn:
            await self.ctx.reply(event, self._build(warn, event, mute_seconds=mute_seconds))
        return True

    # ---------- 图片刷屏 ----------
    async def _image_flood(self, event, cfg: dict) -> bool:
        flood = cfg.get("flood") or {}
        if not flood.get("enabled"):
            return False
        max_img = int(flood.get("max_images", 5))
        if max_img <= 0:
            return False
        group_id = event.get("group_id")
        if not self._in_groups(flood.get("groups"), group_id):
            return False
        if self._is_exempt(flood, event):
            return False
        n_img = sum(1 for seg in (event.get("message") or [])
                    if isinstance(seg, dict) and seg.get("type") == "image")
        if n_img == 0:
            return False
        key = (str(group_id), str(event.get("user_id")), "img")
        window = float(flood.get("window_seconds", 10))
        t = time.monotonic()
        q = self._flood_windows.setdefault(key, deque())
        q.append(t)
        while q and t - q[0] > window:
            q.popleft()
        if len(q) <= max_img:
            return False
        q.clear()
        mute_seconds = int(flood.get("mute_seconds", 120))
        self.ctx.log.info("图片刷屏 %s 群 %s，禁言 %ss", event.get("user_id"), group_id, mute_seconds)
        if "mute" in str(flood.get("action", "mute")):
            await self.ctx.mute(group_id, event.get("user_id"), mute_seconds)
        await self.ctx.reply(event, self._build("发图太快了，缓一缓～", event, mute_seconds=mute_seconds))
        return True

    # ---------- 刷屏 ----------
    async def _flood(self, event, cfg: dict) -> bool:
        flood = cfg.get("flood") or {}
        if not flood.get("enabled"):
            return False
        group_id = event.get("group_id")
        if not self._in_groups(flood.get("groups"), group_id):
            return False
        if self._is_exempt(flood, event):
            return False

        key = (str(group_id), str(event.get("user_id")))
        window = float(flood.get("window_seconds", 10))
        max_messages = int(flood.get("max_messages", 6))
        t = time.monotonic()
        q = self._flood_windows.setdefault(key, deque())
        q.append(t)
        while q and t - q[0] > window:
            q.popleft()
        if len(q) <= max_messages:
            return False

        q.clear()
        mute_seconds = int(flood.get("mute_seconds", 120))
        self.ctx.log.info("刷屏检测 %s 群 %s，禁言 %ss", key[1], key[0], mute_seconds)
        if "mute" in str(flood.get("action", "mute")):
            await self.ctx.mute(group_id, event.get("user_id"), mute_seconds)
        warn = flood.get("warn_text") or "{at} 刷屏了，禁言 {mute_seconds} 秒。"
        await self.ctx.reply(event, self._build(warn, event, mute_seconds=mute_seconds))
        return True

    # ---------- 管理指令：@机器人 说「撤回 / 禁言 / 踢了」 ----------
    async def _admin_commands(self, event, cfg: dict) -> bool:
        if not cfg.get("admin_commands", True):
            return False
        group_id = event.get("group_id")
        text = text_of(event.get("message"))
        if not text:
            return False

        # 只有群主/管理员/机器人管理员能下指令
        sender = event.get("sender") or {}
        if sender.get("role") not in ("admin", "owner") and not self.ctx.is_admin(event.get("user_id")):
            return False

        # 必须是在叫机器人（@ 或 叫名字），避免误触
        trigger = (self.ctx.plugin_cfg("ai_chat").get("trigger") or {})
        aliases = [str(x) for x in (trigger.get("group_name_aliases") or []) if str(x)] or [self.ctx.bot_name]
        called = mentioned(event.get("message"), self.ctx.self_id) or any(a and a in text for a in aliases)
        if not called:
            return False

        action = None
        if re.search(r"撤回|删掉|删了|收回|撤了", text):
            action = "recall"
        elif re.search(r"禁言|闭嘴|安静", text):
            action = "mute"
        elif re.search(r"踢了|踢出去|踢人", text):
            action = "kick"
        if not action:
            return False

        # 目标：优先被引用的消息，其次最近一条群友消息
        target = None
        for seg in (event.get("message") or []):
            if isinstance(seg, dict) and seg.get("type") == "reply":
                mid = (seg.get("data") or {}).get("id")
                if mid:
                    try:
                        quoted = await self.ctx.api.call("get_msg", message_id=int(mid))
                        tgt_sender = (quoted or {}).get("sender") or {}
                        target = {"message_id": int(mid), "user_id": tgt_sender.get("user_id"),
                                  "text": text_of((quoted or {}).get("message"))}
                    except Exception as e:
                        self.ctx.log.warning("管理指令读取被引用消息失败: %s", e)
                break
        if target is None:
            target = self._last_msg.get(str(group_id))
        if not target:
            await self.ctx.reply(event, "要处理哪条？引用一下那条消息再叫我。", at=True)
            return True

        uid = target.get("user_id")
        self.ctx.log.info("管理指令 %s 目标=%s", action, target)
        if action == "recall":
            ok = await self.ctx.recall(target.get("message_id"))
            await self.ctx.reply(event, "已撤回。" if ok else "撤回失败（可能不是我管的群、消息太旧或没有权限）。", at=True)
        elif action == "mute":
            secs = int(cfg.get("admin_mute_seconds", 300))
            ok = await self.ctx.mute(group_id, uid, secs) if uid else False
            await self.ctx.reply(event, f"已禁言 {secs} 秒。" if ok else "禁言失败（机器人可能不是管理员）。", at=True)
        else:
            ok = await self.ctx.kick(group_id, uid) if uid else False
            await self.ctx.reply(event, "已踢出。" if ok else "踢人失败（机器人可能不是管理员）。", at=True)
        return True

    # ---------- 入口 ----------
    async def on_event(self, event) -> bool:
        cfg = self.ctx.plugin_cfg("group_admin")
        if not cfg.get("enabled"):
            return False

        post_type = event.get("post_type")
        if post_type == "notice":
            if event.get("notice_type") == "group_increase":
                return await self._welcome(event, cfg)
            return False
        if post_type != "message" or event.get("message_type") != "group":
            return False

        group_id = str(event.get("group_id"))
        # 记录最近一条群友消息（供「撤回/禁言」指令用），机器人自己发的跳过
        if str(event.get("user_id")) != str(self.ctx.self_id):
            self._last_msg[group_id] = {
                "message_id": event.get("message_id"),
                "user_id": event.get("user_id"),
                "text": text_of(event.get("message")),
            }

        if await self._admin_commands(event, cfg):
            return True
        if await self._badwords(event, cfg):
            return True
        if await self._image_flood(event, cfg):
            return True
        return await self._flood(event, cfg)
