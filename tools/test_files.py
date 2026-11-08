#!/usr/bin/env python3
"""代码文件导出测试：AI 回答里的代码块 → 存成本地文件 → 通过 OneBot 发到会话。

运行：python3 tools/test_files.py
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
USER = 20000
ACTIONS: list = []
CHECKS: list = []

CODE = '''```python
def fib(n):
    a, b = 0, 1
    for _ in range(n):
        yield a
        a, b = b, a + b

print(list(fib(10)))
```'''


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class StubLLM(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        content = "这是给你的脚本：\n\n" + CODE + "\n\n运行后输出前 10 个斐波那契数。"
        out = {"choices": [{"index": 0, "message": {"role": "assistant", "content": content}}]}
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
        "post_type": "message", "message_type": "private", "sub_type": "friend",
        "time": int(time.time()), "self_id": SELF_ID, "message_id": mid,
        "user_id": USER, "message": [{"type": "text", "data": {"text": text}}],
        "raw_message": text, "sender": {"user_id": USER, "nickname": "测试用户", "role": "member"},
    }


class MockOneBot:
    def __init__(self):
        self.ws = None
        self.connected = asyncio.Event()

    async def handler(self, ws):
        self.ws = ws
        self.connected.set()
        await ws.send(json.dumps({"post_type": "meta_event", "meta_event_type": "lifecycle",
                                  "sub_type": "connect", "self_id": SELF_ID}))
        try:
            async for raw in ws:
                data = json.loads(raw)
                ACTIONS.append(data)
                await ws.send(json.dumps({"status": "ok", "retcode": 0, "data": {"message_id": 1},
                                          "echo": data.get("echo")}, ensure_ascii=False))
        except Exception:
            pass


def check(name, ok, detail=""):
    CHECKS.append((name, ok, detail))
    print(("✅ " if ok else "❌ ") + name + (f"  ↳ {detail}" if detail and not ok else ""))


async def main() -> int:
    llm = ThreadingHTTPServer(("127.0.0.1", 0), StubLLM)
    threading.Thread(target=llm.serve_forever, daemon=True).start()
    llm_port = llm.server_address[1]

    ob_port = free_port()
    mock = MockOneBot()
    tmp = Path(tempfile.mkdtemp(prefix="qqbot-files-"))
    cfg = {
        "onebot": {"ws_url": f"ws://127.0.0.1:{ob_port}"},
        "bot": {"name": "测试bot", "log_level": "INFO"},
        "plugins": {
            "keyword": {"enabled": False}, "group_admin": {"enabled": False},
            "image_gen": {"enabled": False},
            "ai_chat": {
                "enabled": True,
                "base_url": f"http://127.0.0.1:{llm_port}/v1",
                "api_key": "test", "model": "m", "cooldown_seconds": 0,
                "search": {"enabled": False}, "attachments": {"enabled": False},
                "files": {"enabled": True, "min_lines": 3, "min_chars": 50, "max_files": 3,
                          "note": "📎 完整代码已保存成文件发出：{names}"},
            },
        },
    }
    cfg_path = tmp / "config.json"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    import websockets

    server = await websockets.serve(mock.handler, "127.0.0.1", ob_port, max_size=2 ** 24)
    bot = subprocess.Popen(
        [sys.executable, str(BOT_DIR / "main.py"), "-c", str(cfg_path)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env={**__import__("os").environ, "QQBOT_DATA_DIR": str(tmp / "data")},
    )
    out = b""
    try:
        await asyncio.wait_for(mock.connected.wait(), timeout=20)
        await asyncio.sleep(1.0)
        await mock.ws.send(json.dumps(msg_event("帮我写个 python 脚本", mid=11)))
        await asyncio.sleep(5)

        uploads = [a for a in ACTIONS if a.get("action") == "upload_private_file"]
        replies = [a for a in ACTIONS if a.get("action") == "send_private_msg"]
        text = ""
        for r in replies:
            for seg in (r.get("params", {}).get("message") or []):
                if seg.get("type") == "text":
                    text += seg["data"].get("text", "")

        check("成功上传文件到私聊", bool(uploads), f"actions={[a.get('action') for a in ACTIONS]}")
        if uploads:
            fpath = Path(uploads[0]["params"]["file"])
            check("文件确实写在本地", fpath.is_file(), str(fpath))
            if fpath.is_file():
                content = fpath.read_text(encoding="utf-8", errors="replace")
                check("文件内容包含完整代码", "def fib(n)" in content and "print(list(fib(10)))" in content, content[:120])
            check("文件名是 .py", str(uploads[0]["params"].get("name", "")).endswith(".py"),
                  str(uploads[0]["params"].get("name")))
        check("文字回复提示文件已发送", "文件" in text, text[:200])
        check("回复里不再堆长代码", "def fib(n)" not in text, text[:200])
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
        for a in ACTIONS:
            print(json.dumps(a, ensure_ascii=False)[:250])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
