#!/usr/bin/env python3
"""群内触发 & 群管理测试：名字唤醒、引用触发、引用文本、管理指令、违禁词、图片刷屏。"""
from __future__ import annotations
import asyncio, json, os, socket, subprocess, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SELF, USER, ADMIN, GROUP = 10001, 10002, 10002, 100100
ACTIONS, CHECKS, LLM_REQS = [], [], []

def free():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

class StubLLM(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        LLM_REQS.append(body)
        out = {"choices": [{"index": 0, "message": {"role": "assistant", "content": "MOCK-OK"}}]}
        data = json.dumps(out).encode()
        self.send_response(200); self.send_header("Content-Type","application/json")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def log_message(self, *a): pass

def gmsg(message, user=USER, role="owner", mid=1):
    return {"post_type":"message","message_type":"group","sub_type":"normal","time":int(time.time()),
            "self_id":SELF,"message_id":mid,"user_id":user,"group_id":GROUP,
            "message":message,"sender":{"user_id":user,"nickname":"测试用户","card":"","role":role}}

def text(t): return [{"type":"text","data":{"text":t}}]
def at(): return {"type":"at","data":{"qq":str(SELF)}}
def reply(mid): return {"type":"reply","data":{"id":str(mid)}}
def image(): return {"type":"image","data":{"file":"a.png","url":"http://x/a.png"}}

class Mock:
    def __init__(self): self.ws=None; self.connected=asyncio.Event()
    async def handler(self, ws):
        self.ws=ws; self.connected.set()
        await ws.send(json.dumps({"post_type":"meta_event","meta_event_type":"lifecycle","sub_type":"connect","self_id":SELF}))
        try:
            async for raw in ws:
                d=json.loads(raw); ACTIONS.append(d)
                data={"message_id":1}
                if d.get("action")=="get_msg":
                    mid=d["params"]["message_id"]
                    # 引用：如果引用 777 就是机器人自己发的
                    sender = {"user_id": SELF, "nickname":"大肥鱼"} if int(mid)==777 else {"user_id": 999, "nickname":"何知松"}
                    data={"message_id":mid,"sender":sender,"message":text("被引用的原话内容ABC")}
                await ws.send(json.dumps({"status":"ok","retcode":0,"data":data,"echo":d.get("echo")}, ensure_ascii=False))
        except Exception: pass

def check(n, ok, d=""):
    CHECKS.append((n, ok, d)); print(("✅ " if ok else "❌ ")+n+(f"  ↳ {d}" if d and not ok else ""))

def sent(act):
    return [a for a in ACTIONS if a.get("action")==act]

async def main():
    llm=ThreadingHTTPServer(("127.0.0.1",0),StubLLM); threading.Thread(target=llm.serve_forever,daemon=True).start()
    ob=free(); mock=Mock(); tmp=Path(tempfile.mkdtemp(prefix="qqbot-grp-"))
    cfg={"onebot":{"ws_url":f"ws://127.0.0.1:{ob}"},
         "bot":{"name":"大肥鱼","admins":[],"log_level":"INFO"},
         "plugins":{"keyword":{"enabled":False},"image_gen":{"enabled":False},
                    "ai_chat":{"enabled":True,"base_url":f"http://127.0.0.1:{llm.server_address[1]}/v1","api_key":"k",
                               "model":"m","cooldown_seconds":0,"max_tokens":100,
                               "trigger":{"private":True,"group_at":True,"group_name_aliases":["大肥鱼","肥鱼"],"group_quote":True,"group_followup_seconds":120,"group_prefix":[]},
                               "search":{"enabled":False},"attachments":{"enabled":False},"files":{"enabled":False}},
                    "group_admin":{"enabled":True,"admin_commands":True,"admin_mute_seconds":60,
                                   "welcome":{"enabled":False},"badwords":{"enabled":True,"words":["广告"],"ignore_admins":False,"action":"recall","mute_seconds":60,"warn_text":"{at} 违规"}
                                   ,"flood":{"enabled":True,"max_messages":8,"max_images":5,"window_seconds":10,"action":"mute","mute_seconds":60,"ignore_admins":False,"warn_text":"{at} 刷屏"}}}}
    cp=tmp/"config.json"; cp.write_text(json.dumps(cfg,ensure_ascii=False),encoding="utf-8")
    import websockets
    srv=await websockets.serve(mock.handler,"127.0.0.1",ob,max_size=2**24)
    bot=subprocess.Popen([sys.executable,str(ROOT/"bot/main.py"),"-c",str(cp)],stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT,env={**os.environ,"QQBOT_DATA_DIR":str(tmp/"data")})
    out=b""
    try:
        await asyncio.wait_for(mock.connected.wait(),20); await asyncio.sleep(1)
        async def send(ev, wait=2.0):
            before=len(ACTIONS); await mock.ws.send(json.dumps(ev,ensure_ascii=False)); await asyncio.sleep(wait)
            return ACTIONS[before:]
        # 1) 叫名字触发（无 @）
        events=await send(gmsg(text("大肥鱼呢"), mid=11))
        check("叫名字也能触发", any(a["action"]=="send_group_msg" for a in events), str(events)[:200])
        # 2) 引用机器人消息触发（无 @）
        events=await send(gmsg([reply(777), ]+text("你说的不对"), mid=12))
        check("引用机器人消息触发", any(a["action"]=="send_group_msg" for a in events), str(events)[:200])
        # 3) 引用文本进模型上下文
        saw = any("被引用的原话内容ABC" in json.dumps(r, ensure_ascii=False) for r in LLM_REQS)
        check("被引用文字进了模型上下文", saw)
        # 4) 管理指令：引用消息 + @机器人 说撤回
        events=await send(gmsg([at(), reply(999), ]+text("撤回"), mid=13))
        check("管理指令可撤回", any(a["action"]=="delete_msg" for a in events), str(events)[:300])
        # 5) 违禁词撤回
        events=await send(gmsg(text("这是一条广告，快来加群"), user=2153654564, role="member", mid=14))
        check("违禁词撤回", any(a["action"]=="delete_msg" for a in events), str(events)[:300])
        # 5.5) 叫一次之后，普通消息也继续对话
        events=await send(gmsg(text("那你说说今天天气"), mid=15), wait=2.5)
        check("唤醒后普通消息继续对话", any(a["action"]=="send_group_msg" for a in events), str(events)[:200])
        # 5.55) 把唤醒窗口设为 0 → @一次只答一次：唤醒后普通消息不应回
        st0=json.load(open(str(cp),encoding="utf-8"))
        st0["plugins"]["ai_chat"]["trigger"]["group_followup_seconds"]=0
        cp.write_text(json.dumps(st0,ensure_ascii=False),encoding="utf-8")
        await asyncio.sleep(3)
        events=await send(gmsg(text("大肥鱼你还在吗"), mid=19), wait=2.5)
        first_ok=any(a["action"]=="send_group_msg" for a in events)
        events2=await send(gmsg(text("那我再说一句"), mid=20), wait=2.5)
        second_reply=any(a["action"]=="send_group_msg" for a in events2)
        check("窗口关掉后：叫一次只回一次", first_ok and not second_reply, f"first={first_ok} second={second_reply}")

        # 5.6) 群主(owner)发的违禁词也要撤回（不豁免管理员）
        events=await send(gmsg(text("这是广告"), user=USER, role="owner", mid=16))
        check("群主违禁词也撤回", any(a["action"]=="delete_msg" for a in events), str(events)[:300])
        # 5.7) 选择性豁免：打开 ignore_admins 后群主不被处理
        st=json.load(open(str(cp),encoding="utf-8"))
        st["plugins"]["group_admin"]["badwords"]["ignore_admins"]=True
        cp.write_text(json.dumps(st,ensure_ascii=False),encoding="utf-8")
        await asyncio.sleep(3)   # 等热加载
        events=await send(gmsg(text("这条是广告"), user=USER, role="owner", mid=17))
        check("开启豁免后群主不被处理", not any(a["action"]=="delete_msg" for a in events), str(events)[:200])
        # 5.8) ignore_users 指定的人豁免（普通成员）
        st["plugins"]["group_admin"]["badwords"]["ignore_admins"]=False
        st["plugins"]["group_admin"]["badwords"]["ignore_users"]=[2153654565]
        cp.write_text(json.dumps(st,ensure_ascii=False),encoding="utf-8")
        await asyncio.sleep(3)
        events=await send(gmsg(text("又是广告"), user=2153654565, role="member", mid=18))
        check("豁免名单内的人不被处理", not any(a["action"]=="delete_msg" for a in events), str(events)[:200])
        # 6) 图片刷屏
        got_ban=False
        for i in range(7):
            ev=await send(gmsg([image()], user=2153654563, role="member", mid=100+i), wait=0.15)
            if any(a["action"]=="set_group_ban" for a in ev): got_ban=True; break
        check("图片刷屏会被禁言", got_ban)
    finally:
        bot.terminate()
        try: bot.wait(timeout=5)
        except subprocess.TimeoutExpired: bot.kill()
        out=bot.stdout.read() if bot.stdout else b""
        srv.close(); await srv.wait_closed(); llm.shutdown()
    failed=[c for c in CHECKS if not c[1]]
    print("\n通过 %d / %d" % (len(CHECKS)-len(failed), len(CHECKS)))
    if failed:
        print(out.decode("utf-8","replace")[-2000:])
        for a in ACTIONS: print(json.dumps(a,ensure_ascii=False)[:200])
        return 1
    return 0

sys.exit(asyncio.run(main()))
