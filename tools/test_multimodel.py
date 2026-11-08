#!/usr/bin/env python3
"""多模型接口 + 文生图 集成测试（不花 API 钱，全部本地假服务）。

覆盖：
- ai_chat.provider = openai / claude / gemini 三种协议都能通过真机器人进程跑通
- 运行时改 config.json（provider 切换）能热加载生效
- image_gen 插件：OpenAI Images / Gemini 两条路径都能出图并作为 image 消息发出去

运行：python3 tools/test_multimodel.py
"""
from __future__ import annotations

import asyncio
import base64
import io
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
USER = 20000
ACTIONS: list = []
CHECKS: list = []
REQUESTS: list = []


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def png_bytes() -> bytes:
    from PIL import Image

    im = Image.new("RGB", (32, 32), (30, 160, 200))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


class Stub(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) or b"{}"
        try:
            payload = json.loads(body)
        except Exception:
            payload = {}
        REQUESTS.append({"path": self.path, "body": payload, "headers": dict(self.headers)})
        path = self.path.split("?")[0]

        # ---- 聊天接口 ----
        if path.endswith("/chat/completions"):
            return self._json({"choices": [{"index": 0, "message": {"role": "assistant", "content": "OPENAI-OK"}}]})
        if path.endswith("/v1/messages"):
            return self._json({"content": [{"type": "text", "text": "CLAUDE-OK"}]})
        if path.endswith(":generateContent") and "gemini-img" not in path:
            return self._json({"candidates": [{"content": {"parts": [{"text": "GEMINI-OK"}]}}]})

        # ---- 图像生成接口 ----
        if path.endswith("/images/generations"):
            return self._json({"data": [{"b64_json": base64.b64encode(png_bytes()).decode()}]})
        if path.endswith(":generateContent") and "gemini-img" in path:
            return self._json(
                {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(png_bytes()).decode()}}
                                ]
                            }
                        }
                    ]
                }
            )
        return self._json({"error": "not found"}, 404)

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


def image_segments(action):
    msg = (action.get("params") or {}).get("message")
    return [s for s in (msg or []) if isinstance(s, dict) and s.get("type") == "image"]


def check(name, ok, detail=""):
    CHECKS.append((name, ok, detail))
    print(("✅ " if ok else "❌ ") + name + (f"  ↳ {detail}" if detail and not ok else ""))


def write_cfg(path: Path, port: int, *, provider="openai", model="m1", image_provider="openai", image_model="gpt-image-1"):
    cfg = {
        "onebot": {"ws_url": f"ws://127.0.0.1:{OB_PORT}"},
        "bot": {"name": "测试bot", "log_level": "INFO"},
        "plugins": {
            "keyword": {"enabled": False},
            "group_admin": {"enabled": False},
            "image_gen": {
                "enabled": True,
                "provider": image_provider,
                "base_url": f"http://127.0.0.1:{port}",
                "api_key": "img-key",
                "model": image_model,
                "size": "1024x1024",
                "triggers": ["画一张", "img:"],
                "push_groups": [],
                "caption": "🎨 画好了",
            },
            "ai_chat": {
                "enabled": True,
                "provider": provider,
                "base_url": f"http://127.0.0.1:{port}/v1" if provider == "openai" else f"http://127.0.0.1:{port}",
                "api_key": "test-key",
                "model": model,
                "cooldown_seconds": 0,
                "system_prompt": "测试",
                "search": {"enabled": False},
                "attachments": {"enabled": False},
            },
        },
    }
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


OB_PORT = 0


async def main() -> int:
    global OB_PORT
    stub = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    port = stub.server_address[1]

    OB_PORT = free_port()
    mock = MockOneBot()
    tmp = Path(tempfile.mkdtemp(prefix="qqbot-mm-"))
    cfg_path = tmp / "config.json"
    write_cfg(cfg_path, port)

    import websockets

    server = await websockets.serve(mock.handler, "127.0.0.1", OB_PORT, max_size=2**24)
    bot = subprocess.Popen(
        [sys.executable, str(BOT_DIR / "main.py"), "-c", str(cfg_path)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env={**__import__("os").environ, "QQBOT_DATA_DIR": str(tmp / "data")},
    )
    out = b""
    try:
        await asyncio.wait_for(mock.connected.wait(), timeout=25)
        await asyncio.sleep(1.5)

        async def send(text, wait=3.0):
            before = len(ACTIONS)
            await mock.ws.send(json.dumps(msg_event(text, mid=int(time.time() * 1000) % 100000)))
            await asyncio.sleep(wait)
            return [a for a in ACTIONS[before:] if a.get("action") == "send_private_msg"]

        # 1) OpenAI 兼容
        replies = await send("你好")
        check("OpenAI 兼容接口", any("OPENAI-OK" in all_text(a) for a in replies), f"{[all_text(a) for a in replies]}")

        # 2) Claude（热加载切 provider）
        write_cfg(cfg_path, port, provider="claude", model="claude-x")
        await asyncio.sleep(3.0)
        replies = await send("你好")
        check("Claude 原生接口", any("CLAUDE-OK" in all_text(a) for a in replies), f"{[all_text(a) for a in replies]}")

        # 3) Gemini（热加载）
        write_cfg(cfg_path, port, provider="gemini", model="gemini-x")
        await asyncio.sleep(3.0)
        replies = await send("你好")
        check("Gemini 原生接口", any("GEMINI-OK" in all_text(a) for a in replies), f"{[all_text(a) for a in replies]}")

        # 4) 文生图 OpenAI Images
        write_cfg(cfg_path, port, image_provider="openai", image_model="gpt-image-1")
        await asyncio.sleep(3.0)
        replies = await send("画一张 蓝色的小鲸鱼", wait=6.0)
        segs = [s for a in replies for s in image_segments(a)]
        ok_img = False
        for s in segs:
            f = (s.get("data") or {}).get("file", "")
            if f.startswith("base64://"):
                raw = base64.b64decode(f[len("base64://"):])
                ok_img = raw[:3] == b"\xff\xd8\xff" or raw[:8] == b"\x89PNG\r\n\x1a\n"
        check("文生图（OpenAI Images）发图到会话", ok_img, f"image segments={len(segs)}")

        # 5) 文生图 Gemini
        write_cfg(cfg_path, port, image_provider="gemini", image_model="gemini-img-x")
        await asyncio.sleep(3.0)
        replies = await send("画一张 橙色小猫", wait=6.0)
        segs = [s for a in replies for s in image_segments(a)]
        ok_img2 = False
        for s in segs:
            f = (s.get("data") or {}).get("file", "")
            if f.startswith("base64://"):
                raw = base64.b64decode(f[len("base64://"):])
                ok_img2 = raw[:3] == b"\xff\xd8\xff" or raw[:8] == b"\x89PNG\r\n\x1a\n"
        check("文生图（Gemini）发图到会话", ok_img2, f"image segments={len(segs)}")

        # 6) 接口路径检查：claude 走 /v1/messages，gemini 走 :generateContent
        paths = [r["path"] for r in REQUESTS]
        check("Claude 请求路径正确", any(p.endswith("/v1/messages") for p in paths), f"paths={paths[:8]}")
        check("Gemini 请求路径正确", any(":generateContent" in p and "gemini-x" in p for p in paths), f"paths={paths[:8]}")
    finally:
        bot.terminate()
        try:
            bot.wait(timeout=5)
        except subprocess.TimeoutExpired:
            bot.kill()
        out = bot.stdout.read() if bot.stdout else b""
        server.close()
        await server.wait_closed()
        stub.shutdown()

    failed = [c for c in CHECKS if not c[1]]
    print("\n通过 %d / %d" % (len(CHECKS) - len(failed), len(CHECKS)))
    if failed:
        print(out.decode("utf-8", "replace")[-3000:])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
