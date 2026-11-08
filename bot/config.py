"""配置加载 + 热更新（改 config.json 不用重启）。"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

DEFAULTS = {
    "onebot": {
        "ws_url": "ws://127.0.0.1:3001",
        "access_token": "",
        "reconnect_seconds": 5,
        "request_timeout": 20,
    },
    "bot": {
        "name": "小助手",
        "admins": [],
        "log_level": "INFO",
        "self_id": 0,
    },
    "plugins": {
        "keyword": {
            "enabled": True,
            "group_require_at": True,
            "group_at_sender": True,
            "contains_max_len": 12,
            "cooldown_seconds": 2,
            "rules": [
                {
                    "name": "打招呼",
                    "keywords": ["你好", "在吗", "hi", "hello"],
                    "match": "contains",
                    "scope": "all",
                    "reply": "你好呀 {nickname}，我是{bot_name}，有什么可以帮你？",
                },
                {
                    "name": "帮助",
                    "keywords": ["/help", "#帮助", "帮助"],
                    "match": "exact",
                    "scope": "all",
                    "reply": "可用功能：\n1) 关键词问答\n2) @我 聊天（AI）\n3) 群管理（欢迎/违禁词）\n发 /help 看这条。",
                },
                {
                    "name": "群规",
                    "keywords": ["群规", "规则"],
                    "match": "exact",
                    "scope": "group",
                    "reply": "本群群规：友善交流、禁止广告、禁止刷屏。违规会被撤回/禁言。",
                },
            ],
        },
        "ai_chat": {
            "enabled": True,
            # 多套接口：profiles 里每套可单独配置，active_profile 指定当前用哪套
            "profiles": [],
            "active_profile": "",
            "provider": "openai",
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "",
            # 认证方式：空串 = 用上面的 API Key；也可以填账号 id（chatgpt/claude/gemini/deepseek）
            "auth": "",
            "model": "deepseek-chat",
            "temperature": 0.7,
            "max_tokens": 1600,
            "timeout_seconds": 60,
            "system_prompt": (
                "你是 QQ 群里的机器人 {bot_name}，说话简短、口语化，像群友一样。"
                "回答控制在 120 字以内，不要用 Markdown 标题，不要暴露自己是 AI 模型。"
            ),
            "trigger": {
                "private": True,
                "group_at": True,
                "group_name_aliases": [],
                "group_quote": True,
                "group_followup_seconds": 120,
                "group_prefix": [],
            },
            "max_history": 8,
            "history_ttl_minutes": 30,
            "cooldown_seconds": 3,
            "group_whitelist": [],
            "group_blacklist": [],
            "at_sender": True,
            "reply_quote": False,
            "files": {
                "enabled": True,
                "min_lines": 15,
                "min_chars": 400,
                "max_files": 3,
                "note": "📎 完整代码已保存成文件发出：{names}",
            },
            "attachments": {
                "enabled": True,
                "max_images": 3,
                "max_file_bytes": 15000000,
                "max_file_chars": 4000,
            },
            "search": {
                "enabled": True,
                "provider": "auto",
                "max_results": 5,
                "always": False,
                "trigger_keywords": [
                    "搜索", "搜一下", "帮我搜", "帮我查", "查一下", "查查",
                    "最新", "最近", "今天", "新闻", "天气", "股价", "多少钱", "是谁", "什么时候",
                ],
            },
            "vision": {
                "enabled": True,
                "provider": "",
                "base_url": "",
                "api_key": "",
                "model": "",
                "timeout_seconds": 60,
                "prompt": "请用中文客观描述这张图片，重点提取里面的文字信息。",
            },
        },
        "dev_tools": {
            "enabled": True,
            "ai_tool": True,
            "timeout_seconds": 60,
            "repo_path": "",
            "git_token": "",
        },
        "skills": {
            "enabled": True,
        },
        "image_gen": {
            "enabled": True,
            "provider": "openai",
            "base_url": "",
            "api_key": "",
            "model": "gpt-image-1",
            "size": "1024x1024",
            "triggers": ["画一张", "画个", "画只", "画一幅", "生成图片", "生图", "img:"],
            "push_groups": [],
            "caption": "🎨 画好了",
        },
        "group_admin": {
            "enabled": True,
            "admin_commands": True,
            "admin_mute_seconds": 300,
            "welcome": {
                "enabled": True,
                "groups": [],
                "template": "欢迎 {at} 加入本群～ 有问题 @ 我",
            },
            "badwords": {
                "enabled": True,
                "groups": [],
                "words": ["加群", "广告", "代刷", "赌博", "兼职日结"],
                "regex_words": [],
                "action": "recall+mute",
                "mute_seconds": 300,
                "ignore_admins": False,
                "ignore_users": [],
                "warn_text": "{at} 发言包含违禁词，已处理。",
            },
            "flood": {
                "enabled": True,
                "groups": [],
                "max_messages": 8,
                "max_images": 5,
                "window_seconds": 10,
                "action": "mute",
                "mute_seconds": 120,
                "ignore_admins": False,
                "ignore_users": [],
                "warn_text": "{at} 刷屏了，禁言 {mute_seconds} 秒。",
            },
            "recall_on_ban": True,
        },
    },
}


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data: dict = {}
        # 有的运行环境（proot）里 stat/statx 可能返回 ENOSYS，热加载因此改用内容摘要比较
        self._digest: str = ""

    def _read_text(self) -> str | None:
        try:
            return self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError:
            return None

    @staticmethod
    def _digest_of(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()

    def load(self) -> dict:
        raw: dict = {}
        text = self._read_text()
        if text is not None:
            try:
                raw = json.loads(text)
            except Exception as e:  # 配置写错时保留旧配置
                raise ValueError(f"config.json 解析失败: {e}") from e
        self.data = deep_merge(DEFAULTS, raw)
        self._digest = self._digest_of(text or "")
        return self.data

    def save_default(self) -> None:
        text = json.dumps(DEFAULTS, ensure_ascii=False, indent=2)
        self.path.write_text(text, encoding="utf-8")
        self._digest = self._digest_of(text)

    def get(self, *keys, default=None):
        node = self.data
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node

    def reload_if_changed(self) -> bool:
        """内容变了才重载（不依赖 stat mtime，避免 proot 下 ENOSYS 报错）。"""
        text = self._read_text()
        if text is None:
            return False
        digest = self._digest_of(text)
        if digest == self._digest:
            return False
        try:
            raw = json.loads(text)
        except Exception:
            # 文件正在被写入/写坏了：记住这次内容，等下次变化再试，避免每 2 秒刷错误
            self._digest = digest
            return False
        self.data = deep_merge(DEFAULTS, raw)
        self._digest = digest
        return True
