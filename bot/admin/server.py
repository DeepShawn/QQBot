#!/usr/bin/env python3
"""QQ 机器人管理服务（手机 APK / 浏览器用）。

监听 127.0.0.1:8080，纯标准库，无第三方依赖。
- 读/改 bot/config.json（机器人 2 秒内热加载）
- 查看/清空 AI 对话上下文（bot/data/context.json）
- 启动/停止/重启机器人
- 看日志、看状态
前端页面：bot/admin/web/index.html
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent.parent     # qqbot/
BOT = ROOT / "bot"
CONFIG_PATH = BOT / "config.json"
DATA_DIR = BOT / "data"
WEB_DIR = BOT / "admin" / "web"
RUN_DIR = ROOT / "run"
LOG_DIR = ROOT / "logs"
HOST, PORT = "127.0.0.1", int(__import__("os").environ.get("QQBOT_ADMIN_PORT") or 8080)

NAPCAT_CONFIG = Path("/opt/napcat/napcat/config")


# ---------------------------------------------------------------- 工具
def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def deep_merge(base, patch):
    if not isinstance(base, dict) or not isinstance(patch, dict):
        return patch
    out = dict(base)
    for k, v in patch.items():
        out[k] = deep_merge(out.get(k), v) if isinstance(v, dict) else v
    return out


def port_open(port: int) -> bool:
    s = socket.socket()
    s.settimeout(0.4)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def pid_alive(pid, must_contain: str = "") -> bool:
    try:
        os.kill(int(pid), 0)
    except Exception:
        return False
    if not must_contain:
        return True
    try:
        cmd = Path(f"/proc/{int(pid)}/cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace")
        return must_contain in cmd
    except Exception:
        return False


def read_pid(name: str):
    try:
        return int((RUN_DIR / f"{name}.pid").read_text().strip())
    except Exception:
        return None


def proc_running(pattern: str) -> bool:
    """在 /proc 里按 cmdline 找进程（proot 下 ps 不可靠）。"""
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                cmd = Path(f"/proc/{entry}/cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace")
            except Exception:
                continue
            if pattern in cmd:
                return True
    except Exception:
        pass
    return False


def tail_lines(path: Path, n: int = 300):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            block = min(size, max(8192, n * 200))
            f.seek(size - block)
            data = f.read().decode("utf-8", "replace")
        return data.splitlines()[-n:]
    except Exception:
        return []


def mask_keys(obj):
    """递归把任何名为 api_key 的字段替换成掩码。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k == "api_key" and isinstance(v, str):
                out[k] = mask_key(v)
            elif k.endswith("api_key") and isinstance(v, str):
                out[k] = mask_key(v)
            else:
                out[k] = mask_keys(v)
        return out
    if isinstance(obj, list):
        return [mask_keys(x) for x in obj]
    return obj


def strip_masked_keys(obj):
    """保存配置时：api_key 为空或仍是掩码 → 删除，避免覆盖真实 key。"""
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


def mask_key(key: str) -> str:
    key = str(key or "")
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return key[:3] + "****" + key[-4:]


def public_config(cfg: dict) -> dict:
    return mask_keys(cfg)


def load_config() -> dict:
    return read_json(CONFIG_PATH, {}) or {}


# ---------------------------------------------------------------- 状态
def login_uin():
    try:
        for p in sorted(NAPCAT_CONFIG.glob("onebot11_*.json")):
            uin = p.stem.split("_", 1)[1]
            if uin.isdigit():
                return uin
    except Exception:
        pass
    return ""


def webui_info():
    d = read_json(NAPCAT_CONFIG / "webui.json", {}) or {}
    token = d.get("token", "")
    return f"http://127.0.0.1:6099/webui?token={token}", token


def _disk_info() -> dict:
    try:
        st = os.statvfs("/root")
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        return {"total_gb": round(total / 2**30, 1), "free_gb": round(free / 2**30, 1)}
    except Exception:
        return {}


def build_state() -> dict:
    pid = read_pid("bot")
    runtime = read_json(DATA_DIR / "runtime.json", {}) or {}
    running = bool(pid and pid_alive(pid, "main.py -c"))
    connected = bool(running and runtime.get("connected"))
    started_at = runtime.get("started_at") or 0
    uin = login_uin()
    webui_url, _ = webui_info()
    cfg = load_config()
    ai = (cfg.get("plugins") or {}).get("ai_chat") or {}
    vision = (ai.get("vision") or {})
    image_gen = (cfg.get("plugins") or {}).get("image_gen") or {}
    return {
        "bot": {
            "running": running,
            "pid": pid if running else None,
            "connected": connected,
            "self_id": runtime.get("self_id") or uin or "",
            "uptime_seconds": int(time.time() - started_at) if running and started_at else 0,
        },
        "protocol": {
            "qq_running": proc_running("/opt/QQ/qq"),
            "ws_open": port_open(3001),
            "login_uin": uin,
            "webui_url": webui_url,
            "ws_url": "ws://127.0.0.1:3001",
            "need_login": bool((proc_running("/opt/QQ/qq") and not port_open(3001)) or (RUN_DIR / "force_need_login").exists()),
            "qr_path": "/root/napcat/napcat/cache/qrcode.png"
            if Path("/root/napcat/napcat/cache/qrcode.png").exists()
            else "",
        },
        "control": {
            "auto_restart_bot": bool(((cfg.get("control") or {}).get("auto_restart_bot", True))),
            "auto_restart_protocol": bool(((cfg.get("control") or {}).get("auto_restart_protocol", True))),
            "desired": read_json(RUN_DIR / "desired.json", {}) or {},
        },
        "config": public_config(cfg),
        "api_key_set": bool(str(ai.get("api_key", "")).strip()),
        "vision_key_set": bool(str(vision.get("api_key", "")).strip()),
        "image_key_set": bool(str(image_gen.get("api_key", "")).strip()),
        "disk": _disk_info(),
        "server_time": time.time(),
    }


# ---------------------------------------------------------------- API 动作
def _clamp_int(obj: dict, key: str, lo: int, hi: int, default: int):
    try:
        v = int(obj.get(key))
    except Exception:
        obj[key] = default
        return
    obj[key] = max(lo, min(hi, v))


def _clamp_float(obj: dict, key: str, lo: float, hi: float, default: float):
    try:
        v = float(obj.get(key))
    except Exception:
        obj[key] = default
        return
    obj[key] = max(lo, min(hi, v))


def _restore_profile_keys(old: dict, new: dict):
    """前端提交 profiles 数组时，未修改的 api_key 是掩码（已被剥掉）→ 用旧配置补回。"""
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


def sanitize_config(cfg: dict) -> dict:
    """防止误填导致烧钱/报错：参数超范围就夹回安全区间。"""
    plugins = cfg.setdefault("plugins", {})
    ai = plugins.setdefault("ai_chat", {})
    _clamp_int(ai, "max_tokens", 64, 8192, 4096)
    _clamp_float(ai, "temperature", 0.0, 2.0, 0.7)
    _clamp_int(ai, "max_history", 1, 100, 8)
    _clamp_int(ai, "history_ttl_minutes", 1, 10080, 30)
    _clamp_int(ai, "cooldown_seconds", 0, 600, 3)
    _clamp_int(ai, "max_output_chars", 50, 4000, 800)
    vis = ai.setdefault("vision", {})
    _clamp_int(vis, "timeout_seconds", 10, 180, 60)
    files = ai.setdefault("files", {})
    _clamp_int(files, "min_lines", 1, 500, 15)
    _clamp_int(files, "min_chars", 50, 20000, 400)
    _clamp_int(files, "max_files", 1, 5, 3)
    for prof in (ai.get("profiles") or []):
        if not isinstance(prof, dict):
            continue
        _clamp_float(prof, "temperature", 0.0, 2.0, 0.7)
        _clamp_int(prof, "max_tokens", 64, 8192, 4096)
        if not str(prof.get("id") or "").strip():
            prof["id"] = "profile"
    ig = plugins.setdefault("image_gen", {})
    _clamp_int(ig, "timeout_seconds", 10, 300, 120)
    return cfg


ONEIBOT_LAST_ERROR = ""


def onebot_call(action: str, timeout: float = 15.0, **params) -> dict:
    """同步调用一次 OneBot 接口（给读群消息用），失败返回 {}。"""
    import asyncio
    import json as _json

    try:
        import websockets
    except Exception:
        return {}

    cfg = load_config().get("onebot") or {}
    url = str(cfg.get("ws_url") or "ws://127.0.0.1:3001")
    token = str(cfg.get("access_token") or "")
    if token:
        url += ("&" if "?" in url else "?") + "access_token=" + token

    async def _run() -> dict:
        async with websockets.connect(url, open_timeout=5, close_timeout=3) as ws:
            await ws.send(_json.dumps({"action": action, "params": params, "echo": "x"},
                                      ensure_ascii=False))
            while True:
                data = _json.loads(await asyncio.wait_for(ws.recv(), timeout))
                if data.get("echo") == "x":
                    return data

    global ONEIBOT_LAST_ERROR
    try:
        ONEIBOT_LAST_ERROR = ""
        return asyncio.run(_run()) or {}
    except Exception as e:
        ONEIBOT_LAST_ERROR = f"{type(e).__name__}: {e}"
        return {}


def _msg_text(message) -> str:
    """把消息段拼成可读文本。"""
    if isinstance(message, str):
        return message
    parts = []
    for seg in message or []:
        if not isinstance(seg, dict):
            continue
        t = seg.get("type")
        d = seg.get("data") or {}
        if t == "text":
            parts.append(str(d.get("text") or ""))
        elif t == "at":
            parts.append("@" + str(d.get("qq") or ""))
        elif t == "image":
            parts.append("[图片]")
        elif t == "face":
            parts.append("[表情]")
        elif t == "file":
            parts.append("[文件] " + str(d.get("name") or ""))
        elif t == "reply":
            parts.append("[引用]")
        elif t == "record":
            parts.append("[语音]")
        elif t == "video":
            parts.append("[视频]")
        elif t == "json":
            parts.append("[卡片]")
    return "".join(parts).strip()


def api_groups() -> dict:
    res = onebot_call("get_group_list")
    groups = []
    for g in res.get("data") or []:
        if not isinstance(g, dict):
            continue
        groups.append({
            "id": g.get("group_id"),
            "name": g.get("group_name") or str(g.get("group_id")),
            "member_count": g.get("member_count") or 0,
        })
    groups.sort(key=lambda x: str(x.get("name")))
    return {"ok": True, "groups": groups, "error": ONEIBOT_LAST_ERROR}


def api_messages(group_id: int, count: int = 50, before_seq: int = 0) -> dict:
    count = max(1, min(int(count), 300))
    params = {"group_id": group_id, "count": count}
    if before_seq:
        params["message_seq"] = before_seq
    res = onebot_call("get_group_msg_history", **params)
    msgs = []
    for m in (res.get("data") or {}).get("messages") or []:
        if not isinstance(m, dict):
            continue
        sender = m.get("sender") or {}
        msgs.append({
            "id": m.get("message_id"),
            "seq": m.get("message_seq") or m.get("real_seq") or 0,
            "time": m.get("time") or 0,
            "user_id": sender.get("user_id") or m.get("user_id"),
            "name": sender.get("card") or sender.get("nickname") or str(sender.get("user_id") or ""),
            "text": _msg_text(m.get("message")),
            "group_name": m.get("group_name") or "",
        })
    return {"ok": bool(msgs) or res.get("status") == "ok", "group_id": group_id, "messages": msgs}


FILES_DIR = DATA_DIR / "files"


def api_files() -> dict:
    items = []
    try:
        for f in sorted(FILES_DIR.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)[:200]:
            if not f.is_file():
                continue
            st = f.stat()
            items.append({
                "name": f.name,
                "size": st.st_size,
                "mtime": st.st_mtime,
            })
    except Exception:
        pass
    return {"ok": True, "dir": str(FILES_DIR), "files": items}


def api_file_get(name: str) -> dict:
    name = Path(str(name or "")).name          # 只取文件名，杜绝路径穿越
    path = FILES_DIR / name
    if not name or not path.is_file():
        return {"ok": False, "error": "文件不存在"}
    st = path.stat()
    if st.st_size > (2 << 20):
        return {"ok": False, "error": "文件太大（>2MB），请用文件管理打开"}
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
        binary = False
    except UnicodeDecodeError:
        text = ""
        binary = True
    return {
        "ok": True,
        "name": name,
        "size": st.st_size,
        "mtime": st.st_mtime,
        "binary": binary,
        "text": text,
    }


def api_patch_config(patch: dict) -> dict:
    cfg = load_config()
    patch = strip_masked_keys(patch or {})
    merged = deep_merge(cfg, patch)
    _restore_profile_keys(cfg, merged)
    sanitize_config(merged)
    write_json(CONFIG_PATH, merged)
    return public_config(merged)


def api_context() -> dict:
    raw = read_json(DATA_DIR / "context.json", {}) or {}
    sessions = []
    for key, val in raw.items():
        if not isinstance(val, dict):
            continue
        sessions.append(
            {
                "key": key,
                "updated_at": float(val.get("updated_at") or 0),
                "messages": val.get("messages") or [],
            }
        )
    sessions.sort(key=lambda s: s["updated_at"], reverse=True)
    return {"sessions": sessions, "total": len(sessions)}


def _run(script_path: Path, timeout: int = 120) -> dict:
    proc = subprocess.run(["bash", str(script_path)], capture_output=True, timeout=timeout)
    return {
        "ok": proc.returncode == 0,
        "output": ((proc.stdout or b"") + (proc.stderr or b"")).decode("utf-8", "replace")[-1200:],
    }


def _bot_connected() -> bool:
    rt = read_json(DATA_DIR / "runtime.json", {}) or {}
    return bool(rt.get("connected"))


def _wait_bot_connected(seconds: float = 20.0) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if _bot_connected():
            return True
        time.sleep(1.0)
    return _bot_connected()


def _wait_ws_ready(seconds: float = 60.0, grace_when_panel_up: float = 20.0) -> bool:
    """等 OneBot WS 3001。协议层(6099)已经起来后只给一小段宽限，避免未登录时干等。"""
    start = time.time()
    end = start + seconds
    while time.time() < end:
        if port_open(3001):
            return True
        if port_open(6099) and (time.time() - start) > grace_when_panel_up:
            return False
        time.sleep(1.0)
    return port_open(3001)


def _wait_port(port: int, seconds: float = 120.0) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if port_open(port):
            return True
        time.sleep(1.0)
    return False


def _scripts():
    return {
        "bot": (ROOT / "scripts" / "bot-start.sh", ROOT / "scripts" / "bot-stop.sh"),
        "protocol": (ROOT / "protocol" / "start.sh", ROOT / "protocol" / "stop.sh"),
    }


def _set_desired(bot=None, protocol=None):
    desired = read_json(RUN_DIR / "desired.json", {}) or {}
    if bot is not None:
        desired["bot"] = bot
    if protocol is not None:
        desired["protocol"] = protocol
    desired["updated_at"] = time.time()
    write_json(RUN_DIR / "desired.json", desired)


def api_bot_action(action: str) -> dict:
    """action: bot|protocol|all + _start/_stop/_restart，也兼容旧的 start/stop/restart（=机器人）。"""
    aliases = {"start": "bot_start", "stop": "bot_stop", "restart": "bot_restart"}
    action = aliases.get(action, action)
    scripts = _scripts()
    parts: list[str] = []
    started = {"bot": None, "protocol": None}

    def do(target: str, op: str):
        start, stop = scripts[target]
        if op == "start":
            r = _run(start, timeout=900 if target == "protocol" else 180)
            if target == "protocol":
                out = r.get("output") or ""
                # 未登录时 start.sh 会明确提示，此时不必等 3001
                if "需要登录" in out or "扫码登录" in out:
                    pass
                else:
                    _wait_ws_ready(60, 20)
            started[target] = True
        else:
            r = _run(stop, timeout=60)
            started[target] = False
        parts.append(f"[{target}] {op}: {'ok' if r['ok'] else 'fail'}\n{r['output']}")
        return r["ok"]

    ok = True
    if action == "bot_start":
        ok &= do("bot", "start")
        _wait_bot_connected(20)
        _set_desired(bot="running")
    elif action == "bot_stop":
        ok &= do("bot", "stop")
        _set_desired(bot="stopped")
    elif action == "bot_restart":
        do("bot", "stop")
        ok &= do("bot", "start")
        _wait_bot_connected(20)
        _set_desired(bot="running")
    elif action == "protocol_start":
        ok &= do("protocol", "start")
        _set_desired(protocol="running")
    elif action == "protocol_stop":
        ok &= do("protocol", "stop")
        _set_desired(protocol="stopped")
    elif action == "protocol_restart":
        do("protocol", "stop")
        ok &= do("protocol", "start")
        _set_desired(protocol="running")
    elif action == "all_start":
        # 协议层已在线就只起机器人（秒开、免扫码）；不在线才拉起协议层
        if not port_open(6099):
            ok &= do("protocol", "start")
            _set_desired(protocol="running")
        else:
            parts.append("[protocol] 已在线，跳过启动（OneBot WS 无需重建）")
        ok &= do("bot", "start")
        _wait_bot_connected(20)
        _set_desired(bot="running")
    elif action == "all_stop":
        # 只停机器人（免扫码）；协议层保持在线，WS 3001 一直在
        ok &= do("bot", "stop")
        _set_desired(bot="stopped")
        parts.append("[protocol] 保持在线（这样下次开启不用重新扫码/验证）")
    elif action == "all_off":
        # 彻底关闭：连 QQ 一起停，下次开启需要重新登录验证
        ok &= do("bot", "stop")
        _set_desired(bot="stopped")
        ok &= do("protocol", "stop")
        _set_desired(protocol="stopped")
    elif action == "all_restart":
        ok &= do("bot", "stop")
        if not port_open(6099):
            ok &= do("protocol", "start")
        ok &= do("bot", "start")
        _set_desired(bot="running", protocol="running")
    else:
        raise ValueError(f"未知 action: {action}")

    return {"ok": bool(ok), "action": action, "output": "\n".join(parts)[-1500:]}


# ---------------- 看门狗：掉了自动拉起（尊重手动停止） ----------------
_WATCH_LOCK = {"busy": False, "last": 0.0}


def watchdog_once():
    if _WATCH_LOCK["busy"] or time.time() - _WATCH_LOCK["last"] < 45:
        return
    cfg = load_config()
    ctl = (cfg.get("control") or {})
    desired = read_json(RUN_DIR / "desired.json", {}) or {}
    _WATCH_LOCK["busy"] = True
    try:
        # 协议层
        if ctl.get("auto_restart_protocol", True) and desired.get("protocol") != "stopped":
            if not port_open(3001) and not proc_running("/opt/QQ/qq"):
                _WATCH_LOCK["last"] = time.time()
                _run(ROOT / "protocol" / "start.sh", timeout=900)
                _wait_port(6099, 180)
                return
        # 机器人
        if ctl.get("auto_restart_bot", True) and desired.get("bot") != "stopped":
            pid = read_pid("bot")
            if not (pid and pid_alive(pid, "main.py -c")):
                _WATCH_LOCK["last"] = time.time()
                _run(ROOT / "scripts" / "bot-start.sh", timeout=120)
    finally:
        _WATCH_LOCK["busy"] = False


def watchdog_thread():
    time.sleep(5)
    while True:
        try:
            watchdog_once()
        except Exception as e:
            print(f"[admin] watchdog 异常: {e}", file=sys.stderr)
        time.sleep(15)


# ---------------------------------------------------------------- 服务商预设
PROVIDER_PRESETS = [
    {
        "id": "deepseek", "name": "DeepSeek 官方", "provider": "openai",
        "base_url": "https://api.deepseek.com/v1",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "note": "国内直连，OpenAI 兼容",
    },
    {
        "id": "openai", "name": "OpenAI / ChatGPT", "provider": "openai",
        "base_url": "https://api.openai.com/v1",
        "models": ["gpt-4o", "gpt-4o-mini", "gpt-4.1"],
        "note": "需要海外网络或中转",
    },
    {
        "id": "grok", "name": "xAI Grok", "provider": "openai",
        "base_url": "https://api.x.ai/v1",
        "models": ["grok-4", "grok-3", "grok-2-latest"],
        "note": "OpenAI 兼容；模型名以 xAI Models 页为准，可手动改",
    },
    {
        "id": "gemini", "name": "Google Gemini（原生）", "provider": "gemini",
        "base_url": "https://generativelanguage.googleapis.com",
        "models": ["gemini-2.5-flash", "gemini-2.5-pro"],
        "note": "原生 generateContent 协议，支持识图",
    },
    {
        "id": "gemini-openai", "name": "Gemini（OpenAI 兼容）", "provider": "openai",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "models": ["gemini-2.5-flash", "gemini-2.5-pro"],
        "note": "用 OpenAI 协议调 Gemini，换 base_url 即可",
    },
    {
        "id": "claude", "name": "Anthropic Claude", "provider": "claude",
        "base_url": "https://api.anthropic.com",
        "models": ["claude-sonnet-4-5", "claude-opus-4-1", "claude-3-5-haiku-latest"],
        "note": "原生 Messages 协议；中转站请用「OpenAI 兼容」",
    },
    {
        "id": "relay", "name": "中转站 / 自定义", "provider": "openai",
        "base_url": "",
        "models": [],
        "note": "填中转站给出的 /v1 地址和它支持的模型名",
    },
]


# ---------------------------------------------------------------- 日志/磁盘自动清理
CLEAN_TARGETS = [
    # (路径, 上限MB, 截断后保留MB)
    ("logs/bot.out", 20, 2),
    ("logs/admin.out", 10, 1),
    ("protocol/logs/napcat.log", 20, 2),
    ("protocol/logs/qq-stderr.log", 20, 1),
    ("protocol/logs/setup.log", 5, 1),
]
KEEP_DAYS = 3          # *.old / 旧日志保留天数
KEEP_IMAGES = 100      # 画图产物最多保留张数


def _truncate(path: Path, limit_mb: int, keep_mb: int) -> int:
    try:
        size = path.stat().st_size
    except Exception:
        return 0
    if size < limit_mb * 1024 * 1024:
        return 0
    keep = keep_mb * 1024 * 1024
    try:
        with open(path, "rb") as f:
            f.seek(max(0, size - keep))
            tail = f.read()
        path.write_bytes(tail)
        return size - len(tail)
    except Exception:
        try:
            path.write_bytes(b"")
            return size
        except Exception:
            return 0


def cleanup_logs_once() -> dict:
    freed = 0
    for rel, limit_mb, keep_mb in CLEAN_TARGETS:
        freed += _truncate(ROOT / rel, limit_mb, keep_mb)

    # 旧日志（.old / .1 之类）按天数删
    cutoff = time.time() - KEEP_DAYS * 86400
    for pat in ("protocol/logs/*.old", "protocol/logs/*.log.1", "logs/*.old", "bot/logs/*.log.[0-9]"):
        for f in ROOT.glob(pat):
            try:
                if f.stat().st_mtime < cutoff:
                    freed += f.stat().st_size
                    f.unlink()
            except Exception:
                pass

    # 画图产物：按时间倒序保留最近 KEEP_IMAGES 张
    img_dir = BOT / "data" / "images"
    try:
        files = sorted(img_dir.glob("*"), key=lambda x: x.stat().st_mtime, reverse=True)
        for f in files[KEEP_IMAGES:]:
            freed += f.stat().st_size
            f.unlink()
    except Exception:
        pass

    # QQ 崩溃转储
    try:
        for f in Path("/root/.config/QQ").rglob("*.dmp"):
            if f.stat().st_size > 50 * 1024 * 1024:
                freed += f.stat().st_size
                f.unlink()
    except Exception:
        pass
    return {"freed_bytes": freed, "freed_mb": round(freed / 1048576, 2), "time": time.time()}


def cleanup_thread():
    time.sleep(20)
    while True:
        try:
            r = cleanup_logs_once()
            if r["freed_bytes"] > 5 * 1048576:
                print(f"[admin] 自动清理释放 {r['freed_mb']}MB", file=sys.stderr)
        except Exception as e:
            print(f"[admin] 清理异常: {e}", file=sys.stderr)
        time.sleep(600)   # 每 10 分钟


# ---------------------------------------------------------------- HTTP
MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
    ".ico": "image/x-icon",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "QQBotAdmin/1.0"

    def log_message(self, fmt, *args):  # 安静点
        pass

    # ---- 基础 ----
    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, data, code: int = 200):
        self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def _error(self, msg: str, code: int = 400):
        self._json({"ok": False, "error": msg}, code)

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    # ---- 路由 ----
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        try:
            if path == "/api/presets":
                return self._json({"ok": True, "providers": PROVIDER_PRESETS})
            if path == "/api/state":
                return self._json(build_state())
            if path == "/api/context":
                return self._json(api_context())
            if path == "/api/groups":
                return self._json(api_groups())
            if path == "/api/messages":
                gid = int((qs.get("group") or ["0"])[0])
                cnt = int((qs.get("count") or ["50"])[0])
                seq = int((qs.get("before_seq") or ["0"])[0])
                if not gid:
                    return self._error("缺少 group 参数")
                return self._json(api_messages(gid, cnt, seq))
            if path == "/api/debug/onebot":
                res = onebot_call("get_group_list")
                return self._json({
                    "ok": bool(res),
                    "error": ONEIBOT_LAST_ERROR,
                    "raw_status": res.get("status"),
                    "groups": len(res.get("data") or []),
                })
            if path == "/api/files":
                return self._json(api_files())
            if path == "/api/files/get":
                return self._json(api_file_get((qs.get("name") or [""])[0]))
            if path == "/api/logs":
                lines = int((qs.get("lines") or ["300"])[0])
                source = (qs.get("source") or ["bot"])[0]
                if source == "protocol":
                    target = ROOT / "protocol" / "logs" / "napcat.log"
                elif source == "adminfile":
                    target = ROOT / "logs" / "admin.out"
                elif source == "botfile":
                    target = BOT / "logs" / "bot.log"
                else:
                    target = LOG_DIR / "bot.out"
                return self._json({"lines": tail_lines(target, max(1, min(lines, 2000))), "source": source})
            if path in ("/", "/index.html"):
                return self._static(WEB_DIR / "index.html")
            return self._static(WEB_DIR / path.lstrip("/"))
        except Exception as e:
            return self._error(f"内部错误: {e}", 500)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self._body()
            if path == "/api/config":
                cfg = api_patch_config(body)
                return self._json({"ok": True, "config": cfg})
            if path == "/api/logs/clean":
                return self._json({"ok": True, **cleanup_logs_once()})
            if path == "/api/context/clear":
                write_json(DATA_DIR / "context.json", {})
                return self._json({"ok": True})
            if path == "/api/bot/action":
                action = str(body.get("action", "")).strip()
                result = api_bot_action(action)
                result["state"] = build_state()
                return self._json(result)
            return self._error("not found", 404)
        except subprocess.TimeoutExpired:
            return self._error("操作超时（脚本仍可能在后台继续执行，稍后刷新看状态）", 500)
        except Exception as e:
            return self._error(f"内部错误: {e}", 500)

    def _static(self, path: Path):
        try:
            path = path.resolve()
            if not str(path).startswith(str(WEB_DIR.resolve())):
                return self._error("forbidden", 403)
            if not path.is_file():
                return self._error("not found", 404)
            data = path.read_bytes()
            return self._send(200, data, MIME.get(path.suffix, "application/octet-stream"))
        except Exception as e:
            return self._error(f"读取文件失败: {e}", 500)


def main():
    import threading

    threading.Thread(target=watchdog_thread, daemon=True, name="watchdog").start()
    threading.Thread(target=cleanup_thread, daemon=True, name="cleanup").start()
    if not (WEB_DIR / "index.html").exists():
        print(f"[admin] 警告: 前端页面不存在 {WEB_DIR / 'index.html'}", file=sys.stderr)
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    print(f"[admin] 管理台已启动: http://{HOST}:{PORT}/")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
