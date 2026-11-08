#!/usr/bin/env python3
"""本地假后端：给 bot/admin/web/index.html 联调用（只用标准库）。

用法:
    python3 mock_server.py                     # 监听 127.0.0.1:8091，读 bot/config.json 当假数据（只读）
    MOCK_ISOLATED=1 python3 mock_server.py     # 不读真实 config，用内置假数据（测试可重复）
    python3 mock_server.py 8080                # 换端口

接口:
    GET  /api/state /api/context /api/logs /api/presets
    POST /api/config /api/context/clear /api/bot/action /api/logs/clean

action 支持 bot_start/bot_stop/bot_restart、protocol_*、all_*（含 all_off）
以及旧的 start/stop/restart。profiles 的 api_key 掩码会原样返回，保存时用旧值补回，
和真实 server.py 行为一致。数据全是假的，**不会写真实 config.json**。
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(HERE, "index.html")
REAL_CONFIG = os.path.join(HERE, "..", "..", "config.json")  # bot/config.json（只读）
ISOLATED = os.environ.get("MOCK_ISOLATED") == "1"  # 1=不读真实 config，用内置假数据（测试可重复）

LOCK = threading.Lock()
START_TS = time.time()

# ---- 假状态 ----
BOT = {
    "running": True,
    "pid": 11433,
    "connected": True,
    "self_id": "10001",
    "uptime_seconds": 812,
    "started_at": START_TS - 812,
}
PROTOCOL = {
    "qq_running": True,
    "ws_open": True,
    "login_uin": "10001",
    "webui_url": "http://127.0.0.1:6099/webui?token=<WebUI token>",
    "ws_url": "ws://127.0.0.1:3001",
}

# ---- 假配置（内部保存“真” api_key，输出时掩码） ----
FALLBACK_CONFIG = {
    "onebot": {"ws_url": "ws://127.0.0.1:3001", "access_token": "change-me-token",
               "reconnect_seconds": 5, "request_timeout": 20},
    "bot": {"name": "小助手", "admins": [], "log_level": "INFO", "self_id": 0},
    "plugins": {
        "keyword": {
            "enabled": True, "group_require_at": True, "cooldown_seconds": 2,
            "rules": [
                {"name": "打招呼", "keywords": ["你好", "在吗", "hi", "hello"],
                 "match": "contains", "scope": "all",
                 "reply": "你好呀 {nickname}，我是{bot_name}，有什么可以帮你？"},
                {"name": "帮助", "keywords": ["/help", "#帮助", "帮助"],
                 "match": "exact", "scope": "all",
                 "reply": "可用功能：\n1) 关键词问答\n2) @我 聊天（AI）\n3) 群管理（欢迎/违禁词）"},
                {"name": "群规", "keywords": ["群规", "规则"],
                 "match": "exact", "scope": "group",
                 "reply": "本群群规：友善交流、禁止广告、禁止刷屏。"},
            ],
        },
        "ai_chat": {
            "enabled": True,
            "provider": "openai",
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "sk-demo-1234567890abcd",  # 真值只在后端内存里
            "model": "deepseek-chat",
            "temperature": 0.7,
            "max_tokens": 600,
            "timeout_seconds": 60,
            "system_prompt": "你是 QQ 群里的机器人 {bot_name}，说话简短、口语化，像群友一样。",
            "trigger": {"private": True, "group_at": True, "group_prefix": [],
                         "group_name_aliases": ["小助手"], "group_quote": True,
                         "group_followup_seconds": 0},
            "max_history": 8,
            "history_ttl_minutes": 30,
            "cooldown_seconds": 3,
            "reply_quote": True,
            "vision": {
                "enabled": True,
                "provider": "",
                "base_url": "",
                "api_key": "",
                "model": "",
                "prompt": "请用中文客观描述这张图片，重点提取里面的文字信息。",
            },
            "search": {
                "enabled": True,
                "provider": "auto",
                "max_results": 5,
                "always": False,
                "trigger_keywords": ["搜索", "搜一下", "最新", "新闻", "天气"],
            },
            "attachments": {
                "enabled": True,
                "max_images": 3,
                "max_file_bytes": 15000000,
                "max_file_chars": 4000,
            },
        },
        "image_gen": {
            "enabled": True,
            "provider": "openai",
            "base_url": "",
            "api_key": "",
            "model": "gpt-image-1",
            "size": "1024x1024",
            "triggers": ["画一张", "画个", "生成图片", "生图"],
            "push_groups": [],
            "caption": "🎨 画好了",
        },
        "group_admin": {
            "enabled": True,
            "welcome": {"enabled": True, "groups": [],
                        "template": "欢迎 {at} 加入本群～ 有问题 @ 我"},
            "badwords": {"enabled": True, "groups": [],
                         "words": ["加群", "广告", "代刷", "赌博", "兼职日结"],
                         "regex_words": [], "action": "recall+mute", "mute_seconds": 300,
                         "ignore_admins": False, "ignore_users": [],
                         "warn_text": "{at} 发言包含违禁词，已处理。"},
            "flood": {"enabled": True, "groups": [],
                      "max_messages": 6, "max_images": 5, "window_seconds": 10,
                      "action": "mute", "mute_seconds": 120,
                      "ignore_admins": False, "ignore_users": [],
                      "warn_text": "{at} 刷屏了，禁言 {mute_seconds} 秒。"},
        },
    },
}


def load_config() -> dict:
    """优先读 bot/config.json（只读），失败则用内置假配置，再补一个假 key。"""
    cfg = copy.deepcopy(FALLBACK_CONFIG)
    if not ISOLATED:
        try:
            with open(REAL_CONFIG, "r", encoding="utf-8") as f:
                real = json.load(f)
            if isinstance(real, dict):
                cfg = deep_merge(cfg, real)
        except Exception:
            pass
    ai = cfg.setdefault("plugins", {}).setdefault("ai_chat", {})
    if not ai.get("api_key"):
        ai["api_key"] = "sk-demo-1234567890abcd"
    # 多套接口：保证至少有 2 套，方便测试切换
    profiles = ai.get("profiles")
    if not isinstance(profiles, list) or not profiles:
        profiles = [{
            "id": "default", "name": "默认接口",
            "provider": ai.get("provider", "openai"),
            "base_url": ai.get("base_url", ""),
            "api_key": ai.get("api_key", ""),
            "model": ai.get("model", ""),
            "temperature": ai.get("temperature", 0.7),
            "max_tokens": ai.get("max_tokens", 4096),
        }]
        ai["profiles"] = profiles
    if len(profiles) < 2:
        profiles.append({
            "id": "relay", "name": "中转站", "provider": "openai",
            "base_url": "https://relay.example.com/v1",
            "api_key": "sk-demo-relay-1234", "model": "gpt-4o-mini",
            "temperature": 0.7, "max_tokens": 4096,
        })
    if not ai.get("active_profile"):
        ai["active_profile"] = profiles[0].get("id", "default")
    return cfg


PRESETS = [
    {"id": "deepseek", "name": "DeepSeek 官方", "provider": "openai",
     "base_url": "https://api.deepseek.com/v1",
     "models": ["deepseek-chat", "deepseek-reasoner"], "note": "国内直连，OpenAI 兼容"},
    {"id": "openai", "name": "OpenAI / ChatGPT", "provider": "openai",
     "base_url": "https://api.openai.com/v1",
     "models": ["gpt-4o", "gpt-4o-mini", "gpt-4.1"], "note": "需要海外网络或中转"},
    {"id": "grok", "name": "xAI Grok", "provider": "openai",
     "base_url": "https://api.x.ai/v1",
     "models": ["grok-4", "grok-3", "grok-2-latest"], "note": "OpenAI 兼容"},
    {"id": "gemini", "name": "Google Gemini（原生）", "provider": "gemini",
     "base_url": "https://generativelanguage.googleapis.com",
     "models": ["gemini-2.5-flash", "gemini-2.5-pro"], "note": "原生 generateContent 协议"},
    {"id": "gemini-openai", "name": "Gemini（OpenAI 兼容）", "provider": "openai",
     "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
     "models": ["gemini-2.5-flash", "gemini-2.5-pro"], "note": "换 base_url 即可"},
    {"id": "claude", "name": "Anthropic Claude", "provider": "claude",
     "base_url": "https://api.anthropic.com",
     "models": ["claude-sonnet-4-5", "claude-haiku-4-5"], "note": "原生 Messages 协议"},
    {"id": "relay", "name": "中转站 / 自定义", "provider": "openai",
     "base_url": "", "models": [], "note": "填中转站的 /v1 地址"},
]


def deep_merge(base: dict, patch: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


CONFIG = load_config()

SESSIONS = [
    {
        "key": "group_12345:67890",
        "updated_at": time.time() - 95,
        "messages": [
            {"role": "user", "content": "你好呀"},
            {"role": "assistant", "content": "你好～我是小助手，有什么可以帮你的吗？"},
            {"role": "user", "content": "帮我写个 8 点起床的闹钟"},
            {"role": "assistant", "content": "好的，已记下：每天 8:00 提醒起床。需要我顺便加个天气播报吗？"},
        ],
    },
    {
        "key": "group_12345:24680",
        "updated_at": time.time() - 600,
        "messages": [
            {"role": "user", "content": "群规是什么？"},
            {"role": "assistant", "content": "本群群规：友善交流、禁止广告、禁止刷屏。"},
        ],
    },
    {
        "key": "private_11223344",
        "updated_at": time.time() - 3600,
        "messages": [
            {"role": "user", "content": "在吗"},
            {"role": "assistant", "content": "在的，有什么需要？"},
        ],
    },
]

_LOG_BASE = [
    "INFO OneBot：ws://127.0.0.1:3001",
    "INFO 已连接 OneBot: ws://127.0.0.1:3001",
    "INFO 收到生命周期事件: connect",
    "INFO 加载插件 keyword ai_chat group_admin commands",
    "INFO 关键词命中[打招呼] -> 你好",
    "INFO 收到群消息 group=12345 user=67890",
    "INFO ai_chat 回复群 12345（模型 deepseek-chat，history=2）",
    "WARNING 群 12345 触发防刷屏，禁言 120 秒",
    "INFO 新成员 24680 加入群 12345，已发送欢迎语",
    "ERROR ai_chat 请求失败: timeout after 60s",
    "INFO 重新连接 OneBot（第 1 次）",
    "INFO 已连接 OneBot: ws://127.0.0.1:3001",
]


def fake_logs(n: int) -> list:
    n = max(1, min(n, 1000))
    now = time.time()
    lines = []
    for i in range(n):
        ts = time.strftime("%m-%d %H:%M:%S", time.localtime(now - (n - i) * 7))
        lines.append(f"{ts} {_LOG_BASE[i % len(_LOG_BASE)]}")
    if ACTION_LOGS:
        lines.extend(ACTION_LOGS[-20:])
    return lines[-n:]


ACTION_LOGS: list = []


def restore_profile_keys(old: dict, new: dict) -> None:
    """前端提交 profiles 时未改动的 api_key 是掩码（已被剥掉）→ 用旧配置补回（同真实后端）。"""
    try:
        old_ai = old["plugins"]["ai_chat"]
        new_ai = new["plugins"]["ai_chat"]
    except Exception:
        return
    old_map = {str(p.get("id")): p for p in (old_ai.get("profiles") or []) if isinstance(p, dict)}
    for prof in (new_ai.get("profiles") or []):
        if not isinstance(prof, dict):
            continue
        key = str(prof.get("api_key") or "")
        if (not key.strip()) or ("****" in key):
            prev = old_map.get(str(prof.get("id")))
            if prev and prev.get("api_key"):
                prof["api_key"] = prev["api_key"]


def mask_key(key: str) -> str:
    key = str(key or "")
    if not key:
        return ""
    if len(key) <= 8:
        return "sk-****"
    return key[:3] + "****" + key[-4:]


def mask_keys(obj):
    """递归把任何名为 api_key / *api_key 的字段替换成掩码（模拟真实后端）。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k == "api_key" or k.endswith("api_key"):
                if isinstance(v, str):
                    out[k] = mask_key(v)
                else:
                    out[k] = mask_keys(v)
            else:
                out[k] = mask_keys(v)
        return out
    if isinstance(obj, list):
        return [mask_keys(x) for x in obj]
    return obj


def strip_masked_keys(obj):
    """保存时：api_key 为空或含 **** → 删除，避免覆盖真实 key。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k.endswith("api_key") and isinstance(v, str):
                if (not v.strip()) or "****" in v:
                    continue
                out[k] = v
            else:
                out[k] = strip_masked_keys(v)
        return out
    if isinstance(obj, list):
        return [strip_masked_keys(x) for x in obj]
    return obj


def masked_config() -> dict:
    return mask_keys(CONFIG)


def state_payload() -> dict:
    if BOT["running"]:
        BOT["uptime_seconds"] = int(time.time() - BOT["started_at"])
    bot = {k: BOT[k] for k in ("running", "pid", "connected", "self_id", "uptime_seconds")}
    if not BOT["running"]:
        bot["pid"] = None
        bot["uptime_seconds"] = 0
        bot["connected"] = False
    plugins = CONFIG.get("plugins", {})
    ai = plugins.get("ai_chat", {}) or {}
    vision = ai.get("vision", {}) or {}
    image_gen = plugins.get("image_gen", {}) or {}
    ctrl = CONFIG.get("control") or {}
    return {
        "bot": bot,
        "protocol": dict(PROTOCOL),
        "config": masked_config(),
        "api_key_set": bool(ai.get("api_key")),
        "vision_key_set": bool(vision.get("api_key")),
        "image_key_set": bool(image_gen.get("api_key")),
        "control": {
            "auto_restart_bot": bool(ctrl.get("auto_restart_bot", True)),
            "auto_restart_protocol": bool(ctrl.get("auto_restart_protocol", True)),
            "desired": {
                "bot": "running" if BOT["running"] else "stopped",
                "protocol": "running" if PROTOCOL["qq_running"] else "stopped",
            },
        },
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "QQBotMock/1.0"
    protocol_version = "HTTP/1.1"

    # ---------- 输出 ----------
    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, msg, code=400):
        self._send_json({"ok": False, "error": msg}, code)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return None

    # ---------- 路由 ----------
    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html", "/admin/", "/admin/index.html"):
            return self._serve_index()
        if path == "/api/state":
            return self._send_json(state_payload())
        if path == "/api/presets":
            return self._send_json({"ok": True, "providers": copy.deepcopy(PRESETS)})
        if path == "/api/context":
            with LOCK:
                return self._send_json({"sessions": copy.deepcopy(SESSIONS),
                                        "total": len(SESSIONS)})
        if path == "/api/logs":
            qs = parse_qs(urlparse(self.path).query)
            try:
                n = int((qs.get("lines") or ["300"])[0])
            except ValueError:
                n = 300
            return self._send_json({"lines": fake_logs(n)})
        if path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path.startswith("/api/"):
            return self._error("unknown api: " + path, 404)
        self._error("not found", 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/config":
            return self._handle_config()
        if path == "/api/context/clear":
            with LOCK:
                SESSIONS.clear()
            return self._send_json({"ok": True})
        if path == "/api/bot/action":
            return self._handle_action()
        if path == "/api/logs/clean":
            return self._send_json({"ok": True, "freed_mb": 0})
        self._error("not found: " + path, 404)

    def do_PATCH(self):
        if urlparse(self.path).path == "/api/config":
            return self._handle_config()
        self._error("not found", 404)

    # ---------- 处理函数 ----------
    def _serve_index(self):
        try:
            with open(INDEX, "rb") as f:
                body = f.read()
        except OSError as e:
            return self._error("index.html 读不到: %s" % e, 500)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _handle_config(self):
        patch = self._read_json()
        if patch is None:
            return self._error("请求体不是合法 JSON")
        if not isinstance(patch, dict):
            return self._error("patch 必须是对象")
        with LOCK:
            patch = strip_masked_keys(copy.deepcopy(patch))
            global CONFIG
            old = CONFIG
            CONFIG = deep_merge(old, patch)
            restore_profile_keys(old, CONFIG)
            cfg = masked_config()
        return self._send_json({"ok": True, "config": cfg})

    def _handle_action(self):
        body = self._read_json()
        if body is None:
            return self._error("请求体不是合法 JSON")
        action = str((body or {}).get("action") or "")

        def bot_on():
            if not BOT["running"]:
                BOT["pid"] = random.randint(10000, 30000)
                BOT["started_at"] = time.time()
            BOT["running"] = True
            BOT["connected"] = True

        def bot_off():
            BOT["running"] = False
            BOT["connected"] = False
            BOT["pid"] = None

        def proto_on():
            PROTOCOL["qq_running"] = True
            PROTOCOL["ws_open"] = True

        def proto_off():
            PROTOCOL["qq_running"] = False
            PROTOCOL["ws_open"] = False

        with LOCK:
            if action in ("start", "bot_start", "restart", "bot_restart"):
                bot_on()
                _log_action("机器人已启动/重启 (pid=%s)" % BOT["pid"])
            elif action in ("stop", "bot_stop"):
                bot_off()
                _log_action("机器人已停止")
            elif action == "protocol_start":
                proto_on()
                _log_action("协议层已启动（mock）")
            elif action == "protocol_stop":
                proto_off()
                _log_action("协议层已停止（mock）")
            elif action == "protocol_restart":
                proto_on()
                _log_action("协议层已重启（mock）")
            elif action == "all_start":
                # 与真实后端一致：协议层在线就只起机器人
                if not PROTOCOL["qq_running"]:
                    proto_on()
                bot_on()
                _log_action("全部服务已启动（mock）")
            elif action == "all_stop":
                # 与真实后端一致：只停机器人，协议层保持在线
                bot_off()
                _log_action("机器人已停止，协议层保持在线（mock）")
            elif action == "all_off":
                bot_off()
                proto_off()
                _log_action("全部服务已彻底关闭（mock）")
            elif action == "all_restart":
                proto_on()
                bot_on()
                _log_action("全部服务已重启（mock）")
            else:
                return self._error("未知 action: %r" % action, 400)
            state = state_payload()
        return self._send_json({"ok": True, "action": action,
                               "output": "mock: " + action + " done", "state": state})

    # 安静一点
    def log_message(self, fmt, *args):
        pass


def _log_action(msg: str):
    ts = time.strftime("%m-%d %H:%M:%S", time.localtime())
    ACTION_LOGS.append("INFO " + ts + " " + msg)


def main():
    ap = argparse.ArgumentParser(description="QQ 机器人管理台假后端")
    ap.add_argument("port", nargs="?", type=int, default=8091, help="端口，默认 8091")
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print("mock server: http://%s:%d/  (Ctrl+C 退出)%s" % (args.host, args.port, " [isolated]" if ISOLATED else ""))
    print("index: %s" % INDEX)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
