#!/usr/bin/env python3
"""空回复重试测试：模型第一次返回空 → 自动重试一次 → 有内容就正常回复。"""
from __future__ import annotations
import asyncio, json, socket, subprocess, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOT_DIR = ROOT / "bot"
SELF_ID, USER = 10000, 20000
ACTIONS, CHECKS = [], []

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

class StubLLM(BaseHTTPRequestHandler):
    count = 0
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0); self.rfile.read(n)
        StubLLM.count += 1
        content = "" if StubLLM.count == 1 else "RETRY-OK 这是第二次的回复"
        out = {"choices": [{"index": 0, "message": {"role": "assistant", "content": content}}]}
        data = json.dumps(out).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def log_message(self, *a): pass

def msg_event(text, mid=1):
    return {"post_type":"message","message_type":"private","sub_type":"friend","time":int(time.time()),
            "self_id":SELF_ID,"message_id":mid,"user_id":USER,
            "message":[{"type":"text","data":{"text":text}}],"raw_message":text,
            "sender":{"user_id":USER,"nickname":"测试","role":"member"}}

class Mock:
    def __init__(self): self.ws=None; self.connected=asyncio.Event()
    async def handler(self, ws):
        self.ws=ws; self.connected.set()
        await ws.send(json.dumps({"post_type":"meta_event","meta_event_type":"lifecycle","sub_type":"connect","self_id":SELF_ID}))
        try:
            async for raw in ws:
                d=json.loads(raw); ACTIONS.append(d)
                await ws.send(json.dumps({"status":"ok","retcode":0,"data":{"message_id":1},"echo":d.get("echo")},ensure_ascii=False))
        except Exception: pass

def check(n, ok, d=""):
    CHECKS.append((n, ok, d)); print(("✅ " if ok else "❌ ")+n+(f"  ↳ {d}" if d and not ok else ""))

async def main():
    llm=ThreadingHTTPServer(("127.0.0.1",0),StubLLM); threading.Thread(target=llm.serve_forever,daemon=True).start()
    ob=free_port(); mock=Mock(); tmp=Path(tempfile.mkdtemp(prefix="qqbot-retry-"))
    cfg={"onebot":{"ws_url":f"ws://127.0.0.1:{ob}"},"bot":{"name":"t","log_level":"INFO"},
         "plugins":{"keyword":{"enabled":False},"group_admin":{"enabled":False},"image_gen":{"enabled":False},
                    "ai_chat":{"enabled":True,"base_url":f"http://127.0.0.1:{llm.server_address[1]}/v1",
                               "api_key":"t","model":"m","cooldown_seconds":0,"max_tokens":1600,
                               "search":{"enabled":False},"attachments":{"enabled":False},"files":{"enabled":False}}}}
    cp=tmp/"config.json"; cp.write_text(json.dumps(cfg,ensure_ascii=False),encoding="utf-8")
    import websockets
    server=await websockets.serve(mock.handler,"127.0.0.1",ob,max_size=2**24)
    bot=subprocess.Popen([sys.executable,str(BOT_DIR/"main.py"),"-c",str(cp)],stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT,env={**__import__("os").environ,"QQBOT_DATA_DIR":str(tmp/"data")})
    try:
        await asyncio.wait_for(mock.connected.wait(),timeout=20); await asyncio.sleep(1)
        await mock.ws.send(json.dumps(msg_event("触发空回复",mid=1))); await asyncio.sleep(4)
        replies=[a for a in ACTIONS if a.get("action")=="send_private_msg"]
        text="".join(s.get("data",{}).get("text","") for a in replies
                     for s in (a.get("params",{}).get("message") or []) if s.get("type")=="text")
        check("空回复后自动重试成功", "RETRY-OK" in text, text[:120])
        check("确实调用了两次模型", StubLLM.count == 2, f"count={StubLLM.count}")
    finally:
        bot.terminate(); bot.wait(timeout=5); server.close(); await server.wait_closed(); llm.shutdown()
    failed=[c for c in CHECKS if not c[1]]
    print("\n通过 %d / %d" % (len(CHECKS)-len(failed), len(CHECKS)))
    return 1 if failed else 0

sys.exit(asyncio.run(main()))
