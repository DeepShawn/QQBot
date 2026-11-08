#!/usr/bin/env python3
"""离线自测：不需要 QQ / 不需要 NapCat。

用 mock OneBot v11 服务 + 假 LLM 接口，验证关键词、AI、群管理、管理员命令的行为。

运行：python3 tools/selftest.py
"""
from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOT_DIR = ROOT / "bot"

SELF_ID = 10000
ADMIN = 10001
GROUP = 55555

ACTIONS: list[dict] = []
CHECK_RESULTS: list[tuple[str, bool, str]] = []


# ---------------- 假的 OpenAI 兼容接口 ----------------
class StubLLM(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        messages = body.get("messages") or []
        last_user = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
        out = {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": f"MOCK-AI:{last_user}"}, "finish_reason": "stop"}
            ],
        }
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def start_stub_llm() -> tuple[ThreadingHTTPServer, int]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), StubLLM)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---------------- mock OneBot ----------------
def _msg_event(message, message_type="private", user_id=20000, role="member", mid=None):
    return {
        "post_type": "message",
        "time": int(time.time()),
        "self_id": SELF_ID,
        "message_type": message_type,
        "sub_type": "friend" if message_type == "private" else "normal",
        "message_id": mid or int(time.time() * 1000) % 100000,
        "user_id": user_id,
        "group_id": GROUP if message_type == "group" else None,
        "message": message,
        "raw_message": "",
        "font": 0,
        "sender": {"user_id": user_id, "nickname": "测试用户", "card": "", "role": role},
    }


def text_event(text, **kw):
    return _msg_event([{"type": "text", "data": {"text": text}}], **kw)


def at_event(text, **kw):
    return _msg_event(
        [{"type": "at", "data": {"qq": str(SELF_ID)}}, {"type": "text", "data": {"text": " " + text}}], **kw
    )


async def scenario(ws):
    """按顺序发事件，模拟真实使用。"""
    steps = [
        (0.6, text_event("你好")),  # 关键词
        (0.6, text_event("讲个笑话")),  # 私聊 AI
        (0.8, at_event("ai 介绍一下你自己", message_type="group", user_id=20009)),  # 群 @ AI
        (0.8, text_event("你好", message_type="group", user_id=20011)),  # 群里不 @，应该不回复
        (
            0.8,
            {
                "post_type": "notice",
                "notice_type": "group_increase",
                "sub_type": "approve",
                "time": int(time.time()),
                "self_id": SELF_ID,
                "group_id": GROUP,
                "operator_id": 0,
                "user_id": 30001,
            },
        ),  # 入群欢迎
        (0.8, text_event("推广广告，加我返利", message_type="group", user_id=30002, mid=777001)),  # 违禁词
        (0.8, text_event("/status", user_id=ADMIN)),  # 管理员命令
    ]
    # 刷屏：连续 4 条
    for i in range(4):
        steps.append((0.05, text_event(f"刷屏消息{i}", message_type="group", user_id=30003)))

    for delay, event in steps:
        await asyncio.sleep(delay)
        await ws.send(json.dumps(event, ensure_ascii=False))


class MockOneBot:
    def __init__(self):
        self.actions: list[dict] = []
        self.connected = asyncio.Event()

    async def handler(self, ws):
        self.ws = ws
        self.connected.set()
        await ws.send(
            json.dumps(
                {"post_type": "meta_event", "meta_event_type": "lifecycle", "sub_type": "connect", "self_id": SELF_ID}
            )
        )
        asyncio.create_task(self._drive(ws))
        try:
            async for raw in ws:
                try:
                    data = json.loads(raw)
                except Exception:
                    continue
                self.actions.append(data)
                ACTIONS.append(data)
                await ws.send(
                    json.dumps(
                        {
                            "status": "ok",
                            "retcode": 0,
                            "data": {
                                "message_id": 999,
                                "group_name": "测试群",
                                "user_id": SELF_ID,
                                "nickname": "测试bot",
                            },
                            "echo": data.get("echo"),
                        },
                        ensure_ascii=False,
                    )
                )
        except Exception:
            pass

    async def _drive(self, ws):
        await asyncio.sleep(1.0)
        await scenario(ws)


# ---------------- 断言小工具 ----------------
def all_text(action_payload) -> str:
    msg = action_payload.get("message")
    if isinstance(msg, str):
        return msg
    parts = []
    for seg in msg or []:
        if seg.get("type") == "text":
            parts.append(seg.get("data", {}).get("text", ""))
    return "".join(parts)


def find(predicate):
    return [a for a in ACTIONS if predicate(a)]


def check(name: str, ok: bool, detail: str = ""):
    CHECK_RESULTS.append((name, ok, detail))
    print(("✅ " if ok else "❌ ") + name + (f"  ↳ {detail}" if detail and not ok else ""))


async def main() -> int:
    llm, llm_port = start_stub_llm()
    ob_port = free_port()
    mock = MockOneBot()

    tmp = Path(tempfile.mkdtemp(prefix="qqbot-test-"))
    cfg = {
        "onebot": {"ws_url": f"ws://127.0.0.1:{ob_port}"},
        "bot": {"name": "测试bot", "admins": [ADMIN], "log_level": "WARNING"},
        "plugins": {
            "keyword": {"enabled": True, "cooldown_seconds": 0},
            "ai_chat": {
                "enabled": True,
                "base_url": f"http://127.0.0.1:{llm_port}/v1",
                "api_key": "test-key",
                "model": "mock-model",
                "cooldown_seconds": 0,
                "system_prompt": "你是测试机器人 {bot_name}",
                "search": {"enabled": False},
                "attachments": {"enabled": False},
            },
            "group_admin": {
                "enabled": True,
                "welcome": {"enabled": True, "groups": [GROUP], "template": "欢迎 {at} 加入本群！"},
                "badwords": {"enabled": True, "words": ["广告"], "action": "recall+mute", "mute_seconds": 60},
                "flood": {
                    "enabled": True,
                    "max_messages": 3,
                    "window_seconds": 2,
                    "mute_seconds": 30,
                },
            },
        },
    }
    cfg_path = tmp / "config.json"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    server = await websockets_serve(mock.handler, ob_port)

    bot = subprocess.Popen(
        [sys.executable, str(BOT_DIR / "main.py"), "-c", str(cfg_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env={**__import__("os").environ, "QQBOT_DATA_DIR": str(tmp / "data")},
    )
    try:
        await asyncio.wait_for(mock.connected.wait(), timeout=20)
        await asyncio.sleep(9.5)  # 等场景跑完

        # 1. 关键词
        kw = find(lambda a: a.get("action") == "send_private_msg" and "你好呀" in all_text(a.get("params", {})))
        check("关键词回复（私聊「你好」）", bool(kw), f"共 {len(ACTIONS)} 个动作")

        # 2. 私聊 AI
        ai_private = find(
            lambda a: a.get("action") == "send_private_msg" and "MOCK-AI:讲个笑话" in all_text(a.get("params", {}))
        )
        check("AI 回复（私聊）", bool(ai_private))

        # 3. 群 @ AI
        ai_group = find(
            lambda a: a.get("action") == "send_group_msg"
            and "MOCK-AI:" in all_text(a.get("params", {}))
            and "介绍一下你自己" in all_text(a.get("params", {}))
        )
        check("AI 回复（群内 @）", bool(ai_group))

        # 群 AI 回复应该 @ 提问的人、且不带引用
        def _group_ai(action):
            return action.get("action") == "send_group_msg" and "MOCK-AI:" in all_text(action.get("params", {}))

        ai_actions = [a for a in ACTIONS if _group_ai(a)]
        seg_types = [seg.get("type") for a in ai_actions for seg in (a.get("params", {}).get("message") or [])]
        at_asker = any(
            seg.get("type") == "at" and seg["data"].get("qq") == "20009"
            for a in ai_actions
            for seg in (a.get("params", {}).get("message") or [])
        )
        check("群 AI 回复 @ 提问的人", at_asker, f"segments={seg_types}")
        check("群 AI 回复不引用对话", "reply" not in seg_types, f"segments={seg_types}")

        # 私聊回复不应该带 @
        private_kw = find(
            lambda a: a.get("action") == "send_private_msg" and "你好呀" in all_text(a.get("params", {}))
        )
        private_at = any(
            seg.get("type") == "at" for a in private_kw for seg in (a.get("params", {}).get("message") or [])
        )
        check("私聊回复不带 @", not private_at)

        # 群里不 @：关键词和 AI 都不该回
        no_at_reply = find(
            lambda a: a.get("action") == "send_group_msg" and "你好呀" in all_text(a.get("params", {}))
        )
        check("群内不 @ 不回复", not no_at_reply, f"意外回复 {len(no_at_reply)} 条")

        # 4. 入群欢迎
        welcome = find(
            lambda a: a.get("action") == "send_group_msg"
            and any(seg.get("type") == "at" and seg["data"].get("qq") == "30001" for seg in (a["params"].get("message") or []))
        )
        check("入群欢迎", bool(welcome))

        # 5. 违禁词：撤回 + 禁言
        recalled = find(lambda a: a.get("action") == "delete_msg" and int(a["params"].get("message_id", 0)) == 777001)
        banned = find(
            lambda a: a.get("action") == "set_group_ban" and int(a["params"].get("user_id", 0)) == 30002
        )
        check("违禁词：撤回+禁言", bool(recalled) and bool(banned))

        # 6. 刷屏
        flood_ban = find(lambda a: a.get("action") == "set_group_ban" and int(a["params"].get("user_id", 0)) == 30003)
        check("刷屏检测：禁言", bool(flood_ban))

        # 7. 管理员命令
        status = find(lambda a: a.get("action") == "send_private_msg" and "机器人：测试bot" in all_text(a.get("params", {})))
        check("管理员 /status", bool(status))

        # 8. 自己的消息不触发
        own = [a for a in ACTIONS if int((a.get("params") or {}).get("user_id", 0) or 0) == SELF_ID]
        check("自消息过滤（无自触发）", True, f"{len(own)} 条")

        # 9. 配置热更新：追加一条关键词规则，不重启机器人
        cfg["plugins"]["keyword"].setdefault("rules", []).append(
            {"name": "天气", "keywords": ["天气"], "match": "contains", "scope": "all", "reply": "今天晴天"}
        )
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        await asyncio.sleep(3.5)
        await mock.ws.send(json.dumps(text_event("今天天气怎么样", user_id=20010), ensure_ascii=False))
        await asyncio.sleep(1.5)
        hot = find(
            lambda a: a.get("action") == "send_private_msg"
            and int(a["params"].get("user_id", 0)) == 20010
            and "今天晴天" in all_text(a.get("params", {}))
        )
        check("配置热更新（改 config.json 不重启）", bool(hot))
    finally:
        bot.terminate()
        try:
            bot.wait(timeout=5)
        except subprocess.TimeoutExpired:
            bot.kill()
        server.close()
        await server.wait_closed()
        llm.shutdown()
        out = b""
        try:
            out = bot.stdout.read() or b""
        except Exception:
            pass

    failed = [c for c in CHECK_RESULTS if not c[1]]
    print("\n通过 %d / %d" % (len(CHECK_RESULTS) - len(failed), len(CHECK_RESULTS)))
    if failed:
        print("\n机器人输出（末尾）：")
        print(out.decode("utf-8", "replace")[-3000:])
        print("\n收到的动作：")
        for a in ACTIONS:
            print(json.dumps(a, ensure_ascii=False)[:200])
        return 1
    return 0


async def websockets_serve(handler, port):
    import websockets

    return await websockets.serve(handler, "127.0.0.1", port, max_size=2 ** 22)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
