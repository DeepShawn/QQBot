#!/usr/bin/env python3
"""多套接口测试：/model 列表、切换、切换后 AI 请求真的用新接口的模型。"""
from __future__ import annotations
import asyncio, json, os, socket, subprocess, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SELF, ADMIN = 10001, 20000
ACTIONS, CHECKS, LLM_REQS = [], [], []

def free():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

class StubLLM(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        LLM_REQS.append({"path": self.path, "model": body.get("model")})
        out = {"choices": [{"index": 0, "message": {"role": "assistant", "content": "来自模型 " + str(body.get("model"))}}]}
        data = json.dumps(out).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def log_message(self, *a): pass

def msg(text, mid=1):
    return {"post_type": "message", "message_type": "private", "sub_type": "friend",
            "time": int(time.time()), "self_id": SELF, "message_id": mid, "user_id": ADMIN,
            "message": [{"type": "text", "data": {"text": text}}], "raw_message": text,
            "sender": {"user_id": ADMIN, "nickname": "管理员", "role": "member"}}

class Mock:
    def __init__(self): self.ws = None; self.connected = asyncio.Event()
    async def handler(self, ws):
        self.ws = ws; self.connected.set()
        await ws.send(json.dumps({"post_type": "meta_event", "meta_event_type": "lifecycle", "sub_type": "connect", "self_id": SELF}))
        try:
            async for raw in ws:
                d = json.loads(raw); ACTIONS.append(d)
                await ws.send(json.dumps({"status": "ok", "retcode": 0, "data": {"message_id": 1}, "echo": d.get("echo")}, ensure_ascii=False))
        except Exception: pass

def check(n, ok, d=""):
    CHECKS.append((n, ok, d)); print(("✅ " if ok else "❌ ") + n + (f"  ↳ {d}" if d and not ok else ""))

def last_text():
    t = ""
    for a in ACTIONS:
        if a.get("action") == "send_private_msg":
            for s in (a.get("params", {}).get("message") or []):
                if s.get("type") == "text": t += s["data"].get("text", "")
    return t

async def main():
    llm = ThreadingHTTPServer(("127.0.0.1", 0), StubLLM)
    threading.Thread(target=llm.serve_forever, daemon=True).start()
    port = llm.server_address[1]
    ob = free(); mock = Mock(); tmp = Path(tempfile.mkdtemp(prefix="qqbot-prof-"))
    cfg_path = tmp / "config.json"
    cfg = {"onebot": {"ws_url": f"ws://127.0.0.1:{ob}"}, "bot": {"name": "t", "admins": [ADMIN], "log_level": "INFO"},
           "plugins": {"keyword": {"enabled": False}, "group_admin": {"enabled": False}, "image_gen": {"enabled": False},
                       "ai_chat": {"enabled": True, "base_url": f"http://127.0.0.1:{port}/v1", "api_key": "k1",
                                   "cooldown_seconds": 0, "max_tokens": 100,
                                   "search": {"enabled": False}, "attachments": {"enabled": False}, "files": {"enabled": False},
                                   "active_profile": "p1",
                                   "profiles": [
                                       {"id": "p1", "name": "A接口", "provider": "openai", "base_url": f"http://127.0.0.1:{port}/v1", "api_key": "ka", "model": "model-A"},
                                       {"id": "p2", "name": "B接口", "provider": "openai", "base_url": f"http://127.0.0.1:{port}/v1", "api_key": "kb", "model": "model-B"},
                                   ]}}}
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    import websockets
    srv = await websockets.serve(mock.handler, "127.0.0.1", ob, max_size=2**24)
    bot = subprocess.Popen([sys.executable, str(ROOT / "bot/main.py"), "-c", str(cfg_path)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           env={**os.environ, "QQBOT_DATA_DIR": str(tmp / "data")})
    try:
        await asyncio.wait_for(mock.connected.wait(), 20); await asyncio.sleep(1.2)
        await mock.ws.send(json.dumps(msg("/model", 1))); await asyncio.sleep(1.5)
        check("/model 列出接口", "A接口" in last_text() and "B接口" in last_text(), last_text()[:200])

        LLM_REQS.clear()
        await mock.ws.send(json.dumps(msg("你好呀", 2))); await asyncio.sleep(3)
        check("默认用 A 接口的模型", LLM_REQS and LLM_REQS[-1]["model"] == "model-A", str(LLM_REQS[-1:]))

        await mock.ws.send(json.dumps(msg("/model 2", 3))); await asyncio.sleep(1.5)
        check("切换命令回复", "已切换" in last_text(), last_text()[:200])
        saved = json.loads(cfg_path.read_text(encoding="utf-8"))["plugins"]["ai_chat"]["active_profile"]
        check("active_profile 已写回配置", saved == "p2", saved)

        LLM_REQS.clear()
        await mock.ws.send(json.dumps(msg("再聊一句", 4))); await asyncio.sleep(3)
        check("切换后 AI 用 B 接口模型", LLM_REQS and LLM_REQS[-1]["model"] == "model-B", str(LLM_REQS[-1:]))
        check("配置文件其它 profile 的 key 未被破坏",
              json.loads(cfg_path.read_text(encoding="utf-8"))["plugins"]["ai_chat"]["profiles"][0]["api_key"] == "ka")
    finally:
        bot.terminate()
        try: bot.wait(timeout=5)
        except subprocess.TimeoutExpired: bot.kill()
        srv.close(); await srv.wait_closed(); llm.shutdown()
    failed = [c for c in CHECKS if not c[1]]
    print("\n通过 %d / %d" % (len(CHECKS) - len(failed), len(CHECKS)))
    return 1 if failed else 0

sys.exit(asyncio.run(main()))
