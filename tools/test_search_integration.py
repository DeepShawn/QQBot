#!/usr/bin/env python3
"""联网搜索集成测试：命中触发词 → 搜索 → 结果进入模型上下文。

用本地 stub LLM，搜索走真实网络（DDG/Bing）。网络不可用时标记 SKIP 退出 0。
运行：python3 tools/test_search_integration.py
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
sys.path.insert(0, str(BOT_DIR))

SELF_ID = 10000
USER = 20000
ACTIONS: list = []
CHECKS: list = []


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class StubLLM(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        raw = json.dumps(body.get("messages") or [], ensure_ascii=False)
        flags = ""
        if "【联网实时搜索结果】" in raw:
            flags += "|SAW_SEARCH"
        if "http" in raw and "搜索结果" in raw:
            flags += "|HAS_URL"
        out = {"choices": [{"index": 0, "message": {"role": "assistant", "content": "MOCK-AI" + flags}}]}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def msg_event(text, mid=1):
    return {
        "post_type": "message",
        "message_type": "private",
        "sub_type": "friend",
        "time": int(time.time()),
        "self_id": SELF_ID,
        "message_id": mid,
        "user_id": USER,
        "message": [{"type": "text", "data": {"text": text}}],
        "raw_message": text,
        "sender": {"user_id": USER, "nickname": "测试用户", "role": "member"},
    }


class MockOneBot:
    def __init__(self):
        self.ws = None
        self.connected = asyncio.Event()

    async def handler(self, ws):
        self.ws = ws
        self.connected.set()
        await ws.send(json.dumps({"post_type": "meta_event", "meta_event_type": "lifecycle", "sub_type": "connect", "self_id": SELF_ID}))
        try:
            async for raw in ws:
                data = json.loads(raw)
                ACTIONS.append(data)
                await ws.send(json.dumps({"status": "ok", "retcode": 0, "data": {"message_id": 999}, "echo": data.get("echo")}, ensure_ascii=False))
        except Exception:
            pass


def all_text(action) -> str:
    msg = (action.get("params") or {}).get("message")
    if isinstance(msg, str):
        return msg
    return "".join(s.get("data", {}).get("text", "") for s in (msg or []) if s.get("type") == "text")


def check(name, ok, detail=""):
    CHECKS.append((name, ok, detail))
    print(("✅ " if ok else "❌ ") + name + (f"  ↳ {detail}" if detail and not ok else ""))


async def main() -> int:
    import search as search_mod

    probe = search_mod.search("DeepSeek 最新模型", max_results=2, provider="auto")
    if not probe.get("ok"):
        print("SKIP: 当前网络搜索不可用 →", probe.get("error"))
        return 0

    llm = ThreadingHTTPServer(("127.0.0.1", 0), StubLLM)
    threading.Thread(target=llm.serve_forever, daemon=True).start()
    llm_port = llm.server_address[1]

    ob_port = free_port()
    mock = MockOneBot()
    tmp = Path(tempfile.mkdtemp(prefix="qqbot-search-"))
    cfg = {
        "onebot": {"ws_url": f"ws://127.0.0.1:{ob_port}"},
        "bot": {"name": "测试bot", "log_level": "INFO"},
        "plugins": {
            "keyword": {"enabled": False},
            "group_admin": {"enabled": False},
            "ai_chat": {
                "enabled": True,
                "base_url": f"http://127.0.0.1:{llm_port}/v1",
                "api_key": "test-key",
                "model": "mock-model",
                "cooldown_seconds": 0,
                "search": {"enabled": True, "provider": "auto", "max_results": 3, "always": False,
                           "trigger_keywords": ["搜索", "查一下", "最新"]},
            },
        },
    }
    cfg_path = tmp / "config.json"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    import websockets

    server = await websockets.serve(mock.handler, "127.0.0.1", ob_port, max_size=2**22)
    bot = subprocess.Popen(
        [sys.executable, str(BOT_DIR / "main.py"), "-c", str(cfg_path)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env={**__import__("os").environ, "QQBOT_DATA_DIR": str(tmp / "data")},
    )
    out = b""
    try:
        await asyncio.wait_for(mock.connected.wait(), timeout=20)
        await asyncio.sleep(1.0)
        # 命中触发词「最新」
        await mock.ws.send(json.dumps(msg_event("帮我查一下最新的 DeepSeek 新闻", mid=201)))
        await asyncio.sleep(14)
        replies = [all_text(a) for a in ACTIONS if a.get("action") == "send_private_msg"]
        check("搜索命中后模型收到搜索结果", any("SAW_SEARCH" in t for t in replies), f"replies={replies}")
        check("搜索结果带链接", any("HAS_URL" in t for t in replies), f"replies={replies}")

        # 普通闲聊不应该触发搜索
        before = len(ACTIONS)
        await mock.ws.send(json.dumps(msg_event("讲个笑话", mid=202)))
        await asyncio.sleep(8)
        new_replies = [all_text(a) for a in ACTIONS[before:] if a.get("action") == "send_private_msg"]
        check("普通消息不触发搜索", not any("SAW_SEARCH" in t for t in new_replies), f"replies={new_replies}")
    finally:
        bot.terminate()
        try:
            bot.wait(timeout=5)
        except subprocess.TimeoutExpired:
            bot.kill()
        out = bot.stdout.read() if bot.stdout else b""
        server.close()
        await server.wait_closed()
        llm.shutdown()

    failed = [c for c in CHECKS if not c[1]]
    print("\n通过 %d / %d" % (len(CHECKS) - len(failed), len(CHECKS)))
    if failed:
        print(out.decode("utf-8", "replace")[-2500:])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
