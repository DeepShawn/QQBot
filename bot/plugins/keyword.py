"""关键词 / 正则自动回复。"""
from __future__ import annotations

import re

from dispatcher import PluginBase
from utils import Cooldown, mentioned, text_of


class Plugin(PluginBase):
    name = "keyword"
    priority = 200

    def __init__(self, ctx):
        super().__init__(ctx)
        self.cd = Cooldown(0)

    @staticmethod
    def _scope_ok(scope: str, message_type: str) -> bool:
        return scope in (None, "", "all", message_type)

    @staticmethod
    def _norm(s: str) -> str:
        """去掉首尾空白和标点、统一小写，「你好！」「Hello~」也能精确命中。"""
        return re.sub(r"^[\s\W_]+|[\s\W_]+$", "", s or "").lower()

    @staticmethod
    def _contains(word: str, text: str) -> bool:
        # 英文/数字关键词按「整词」匹配：hi 不会命中 this / which，hello 不会命中 helloworld
        if re.fullmatch(r"[A-Za-z0-9_]+", word):
            return re.search(r"(?<![A-Za-z0-9_])" + re.escape(word) + r"(?![A-Za-z0-9_])", text, re.I) is not None
        return word in text

    def _match(self, rule: dict, text: str, max_len: int) -> bool:
        match = (rule.get("match") or "contains").lower()
        if match == "regex":
            pattern = rule.get("regex") or ""
            try:
                return bool(pattern) and re.search(pattern, text) is not None
            except re.error:
                return False
        words = [str(w) for w in (rule.get("keywords") or []) if str(w)]
        if match == "exact":
            t = self._norm(text)
            return any(t == self._norm(w) for w in words)
        # contains：只对短消息生效。长消息是在提问/提需求，交给 AI，
        # 否则「写个 hello world 的 html」会被「打招呼」规则截走。
        limit = int(rule.get("max_len", max_len) or 0)
        if limit and len(text) > limit:
            return False
        return any(self._contains(w, text) for w in words)

    async def on_event(self, event) -> bool:
        cfg = self.ctx.plugin_cfg("keyword")
        if not cfg.get("enabled"):
            return False
        if event.get("post_type") != "message":
            return False

        text = text_of(event.get("message"))
        if not text:
            return False

        message_type = event.get("message_type")
        # 群里只在 @ 机器人 时才回复（可在 config.json 用 keyword.group_require_at 关掉）
        if message_type == "group" and cfg.get("group_require_at", True):
            if not mentioned(event.get("message"), self.ctx.self_id):
                return False

        max_len = int(cfg.get("contains_max_len", 12) or 0)
        for rule in cfg.get("rules") or []:
            if not isinstance(rule, dict):
                continue
            if not self._scope_ok(rule.get("scope"), message_type):
                continue
            if not self._match(rule, text, max_len):
                continue
            key = f"{message_type}:{event.get('group_id', 0)}:{rule.get('name') or rule.get('regex') or ''}"
            if not self.cd.allow(key, cfg.get("cooldown_seconds", 2)):
                return True
            reply = self.ctx.format_text(rule.get("reply", ""), event)
            self.ctx.log.info("关键词命中[%s] -> %s", rule.get("name"), text[:30])
            # 群里 @ 提问的人（不要引用对话）；私聊不加 @
            at = rule.get("at_sender", cfg.get("group_at_sender", True))
            await self.ctx.reply(event, reply, at=bool(at))
            return True
        return False
