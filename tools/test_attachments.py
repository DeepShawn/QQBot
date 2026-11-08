#!/usr/bin/env python3
"""附件能力集成测试（不需要真 QQ / 不需要真模型）。

覆盖：
1. 用户发图片 → 视觉模型识别 → 描述进入上下文 → 回复带上识别结果
2. 用户发文档（docx）→ 抽取文本 → 回复带上文件内容
3. 用户引用一条带图片的消息 → 也能读到图片并识别

用本地 stub：假 LLM（同时充当视觉接口）、本地文件 HTTP 服务、mock OneBot。
运行：python3 tools/test_attachments.py
"""
from __future__ import annotations

import asyncio
import io
import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOT_DIR = ROOT / "bot"

SELF_ID = 10000
USER = 20000
GROUP = 55555
ACTIONS: list = []
CHECKS: list = []


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ---------------- 测试素材 ----------------
def make_png() -> bytes:
    from PIL import Image

    im = Image.new("RGB", (64, 64), (0, 200, 220))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def make_docx(text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="xml" ContentType="application/xml"/></Types>',
        )
        z.writestr(
            "word/document.xml",
            '<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>",
        )
    return buf.getvalue()


class FileServer(BaseHTTPRequestHandler):
    routes: dict = {}

    def do_GET(self):
        data = self.routes.get(self.path)
        if data is None:
            self.send_response(404)
            self.end_headers()
            return
        payload, ctype = data
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):
        pass


# ---------------- 假 LLM（兼视觉接口） ----------------
class StubLLM(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        messages = body.get("messages") or []
        raw = json.dumps(messages, ensure_ascii=False)
        has_image_input = "image_url" in raw
        if has_image_input:
            content = "VISION:图片里有一条鲸鱼和几个字"
        else:
            last_user = ""
            for m in reversed(messages):
                if m.get("role") == "user":
                    c = m.get("content")
                    last_user = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
                    break
            flags = ""
            if "【图片内容识别】" in raw:
                flags += "|SEE_IMAGE"
            if "【文件 " in raw:
                flags += "|SEE_FILE"
            if "青花瓷" in raw:
                flags += "|SAW_SECRET"
            content = "MOCK-AI:" + last_user + flags
        out = {"choices": [{"index": 0, "message": {"role": "assistant", "content": content}}]}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


# ---------------- mock OneBot ----------------
def seg_image(url):
    return {"type": "image", "data": {"file": "a.png", "url": url, "sub_type": 0}}


def seg_file(url, name="测试文档.docx"):
    return {"type": "file", "data": {"file": name, "url": url, "size": 100}}


def seg_reply(mid):
    return {"type": "reply", "data": {"id": str(mid)}}


def msg_event(message, user_id=USER, mid=1):
    return {
        "post_type": "message",
        "message_type": "private",
        "sub_type": "friend",
        "time": int(time.time()),
        "self_id": SELF_ID,
        "message_id": mid,
        "user_id": user_id,
        "message": message,
        "raw_message": "",
        "font": 0,
        "sender": {"user_id": user_id, "nickname": "测试用户", "card": "", "role": "member"},
    }


class MockOneBot:
    def __init__(self):
        self.actions = []
        self.connected = asyncio.Event()
        self.img_url = ""
        self.file_url = ""

    async def handler(self, ws):
        self.ws = ws
        self.connected.set()
        await ws.send(json.dumps({"post_type": "meta_event", "meta_event_type": "lifecycle", "sub_type": "connect", "self_id": SELF_ID}))
        asyncio.create_task(self._drive())
        try:
            async for raw in ws:
                data = json.loads(raw)
                self.actions.append(data)
                ACTIONS.append(data)
                resp_data = {"message_id": 999}
                if data.get("action") == "get_msg":
                    # 模拟「被引用的消息」：一条带图片的消息
                    resp_data = {
                        "message_id": data["params"]["message_id"],
                        "message": [seg_image(self.img_url)],
                        "sender": {"user_id": 30001, "nickname": "别人"},
                    }
                await ws.send(json.dumps({"status": "ok", "retcode": 0, "data": resp_data, "echo": data.get("echo")}, ensure_ascii=False))
        except Exception:
            pass

    async def _drive(self):
        await asyncio.sleep(1.2)
        # 1) 只发图片（无文字）
        await self.ws.send(json.dumps(msg_event([seg_image(self.img_url)], mid=101)))
        await asyncio.sleep(3.0)
        # 2) 发一个 docx 文件
        await self.ws.send(json.dumps(msg_event([{"type": "text", "data": {"text": "帮我看看这个文档"}}, seg_file(self.file_url)], mid=102)))
        await asyncio.sleep(3.5)
        # 3) 引用一条带图片的消息
        await self.ws.send(json.dumps(msg_event([seg_reply(555), {"type": "text", "data": {"text": "这张图讲了啥"}}], mid=103)))
        await asyncio.sleep(3.0)


def all_text(action) -> str:
    msg = (action.get("params") or {}).get("message")
    if isinstance(msg, str):
        return msg
    return "".join(s.get("data", {}).get("text", "") for s in (msg or []) if s.get("type") == "text")


def check(name, ok, detail=""):
    CHECKS.append((name, ok, detail))
    print(("✅ " if ok else "❌ ") + name + (f"  ↳ {detail}" if detail and not ok else ""))


async def main() -> int:
    llm = ThreadingHTTPServer(("127.0.0.1", 0), StubLLM)
    threading.Thread(target=llm.serve_forever, daemon=True).start()
    llm_port = llm.server_address[1]

    FileServer.routes = {
        "/img.png": (make_png(), "image/png"),
        "/doc.docx": (make_docx("这是隐形的测试文档内容，里面写着秘密口令：青花瓷"), "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    }
    files = ThreadingHTTPServer(("127.0.0.1", 0), FileServer)
    threading.Thread(target=files.serve_forever, daemon=True).start()
    file_port = files.server_address[1]

    ob_port = free_port()
    mock_obj = MockOneBot()
    mock_obj.img_url = f"http://127.0.0.1:{file_port}/img.png"
    mock_obj.file_url = f"http://127.0.0.1:{file_port}/doc.docx"

    tmp = Path(tempfile.mkdtemp(prefix="qqbot-att-"))
    cfg = {
        "onebot": {"ws_url": f"ws://127.0.0.1:{ob_port}"},
        "bot": {"name": "测试bot", "admins": [], "log_level": "INFO"},
        "plugins": {
            "keyword": {"enabled": False},
            "group_admin": {"enabled": False},
            "ai_chat": {
                "enabled": True,
                "base_url": f"http://127.0.0.1:{llm_port}/v1",
                "api_key": "test-key",
                "model": "mock-model",
                "cooldown_seconds": 0,
                "system_prompt": "你是测试机器人",
                "search": {"enabled": False},
                "attachments": {"enabled": True, "max_images": 3, "max_file_chars": 4000},
                "vision": {"enabled": True, "base_url": "", "api_key": "", "model": ""},
            },
        },
    }
    cfg_path = tmp / "config.json"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    import websockets

    server = await websockets.serve(mock_obj.handler, "127.0.0.1", ob_port, max_size=2**22)
    bot = subprocess.Popen(
        [sys.executable, str(BOT_DIR / "main.py"), "-c", str(cfg_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env={**__import__("os").environ, "QQBOT_DATA_DIR": str(tmp / "data")},
    )
    out = b""
    try:
        await asyncio.wait_for(mock_obj.connected.wait(), timeout=20)
        await asyncio.sleep(12)

        replies = [a for a in ACTIONS if a.get("action") == "send_private_msg"]
        texts = [all_text(a) for a in replies]

        check("发图片能识别（视觉）", any("SEE_IMAGE" in t for t in texts), f"replies={texts}")
        check(
            "发文档能读取内容",
            any("SEE_FILE" in t for t in texts),
            f"replies={texts}",
        )
        # 文档内容进了上下文：模型应看到 secret
        check("文档正文被送进模型", any("SAW_SECRET" in t for t in texts), f"replies={texts}")
        check("引用消息里的图片也能识别", sum("SEE_IMAGE" in t for t in texts) >= 2, f"SEE_IMAGE 次数={sum('SEE_IMAGE' in t for t in texts)}")
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
        files.shutdown()

    failed = [c for c in CHECKS if not c[1]]
    print("\n通过 %d / %d" % (len(CHECKS) - len(failed), len(CHECKS)))
    if failed:
        print("\n机器人日志尾部：")
        print(out.decode("utf-8", "replace")[-3000:])
        for a in ACTIONS:
            print(json.dumps(a, ensure_ascii=False)[:200])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
