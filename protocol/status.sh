#!/bin/bash
# 查看 NapCat/QQ/Xvfb 运行状态（proot 环境专用：不依赖 ps aux）
set -u

P=/root/workspace/qqbot/protocol
N=/opt/napcat
LOG="$P/logs/napcat.log"

echo "===== NapCat 状态 $(date '+%F %T') ====="

# ---------- 进程 ----------
main_n=$(pgrep -f "^/opt/QQ/qq --no-sandbox$" 2>/dev/null | wc -l)
all_n=$(pgrep -f "/opt/QQ/qq" 2>/dev/null | wc -l)
util_n=$(pgrep -f "user-data-dir=/root/.config/QQ" 2>/dev/null | wc -l)
if [ "$all_n" -gt 0 ] || [ "$util_n" -gt 0 ]; then
  echo "QQ 进程   : 运行中 (主进程 ${main_n}, QQ 相关进程 ${all_n}, Electron 子进程 ${util_n})"
else
  echo "QQ 进程   : 未运行"
fi
if pgrep -f "xvfb-run" >/dev/null 2>&1; then
  echo "Xvfb      : $(pgrep -af 'Xvfb :' 2>/dev/null | head -1)"
else
  echo "Xvfb      : 未运行"
fi

# ---------- 端口 / 内存 / 配置 ----------
python3 - "$P" "$N" <<'PY'
import json, os, socket, sys, time

P, N = sys.argv[1], sys.argv[2]

def port_open(p):
    s = socket.socket(); s.settimeout(1)
    try:
        s.connect(("127.0.0.1", p)); return True
    except Exception:
        return False
    finally:
        s.close()

def read_json(path):
    try:
        return json.load(open(path))
    except Exception:
        return None

for name, p in (("WebUI 6099", 6099), ("OneBot WS 3001", 3001), ("反向WS 3002(备用)", 3002)):
    print(f"{name:<18}: {'OPEN' if port_open(p) else 'closed'}")

# 内存：QQ 主进程 + Electron 子进程（proot 下 cmdline 是 /proc/self/exe，靠 user-data-dir 识别）
tot = 0
for pid in os.listdir("/proc"):
    if not pid.isdigit():
        continue
    try:
        cmd = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode("utf8", "replace")
        if "/opt/QQ/qq" not in cmd and "user-data-dir=/root/.config/QQ" not in cmd:
            continue
        rss = 0
        for line in open(f"/proc/{pid}/status"):
            if line.startswith("VmRSS"):
                rss = int(line.split()[1]); break
        tot += rss
    except Exception:
        pass
print(f"QQ 总内存占用     : {tot // 1024} MB (RSS 合计)")

webui = read_json(os.path.join(N, "napcat/config/webui.json")) or {}
tok = webui.get("token", "")
print(f"WebUI 地址        : http://127.0.0.1:{webui.get('port', 6099)}/webui?token={tok}")

# 登录状态 / OneBot 配置
cfgdir = os.path.join(N, "napcat/config")
uins = []
try:
    for f in os.listdir(cfgdir):
        if f.startswith("onebot11_") and f.endswith(".json"):
            u = f[len("onebot11_"):-len(".json")]
            if u.isdigit():
                uins.append(u)
except Exception:
    pass
if uins:
    print(f"登录账号          : {', '.join(uins)} (config/onebot11_<uin>.json 已生成)")
    ob = read_json(os.path.join(cfgdir, f"onebot11_{uins[0]}.json")) or {}
    net = ob.get("network", {})
    for srv in net.get("websocketServers", []):
        print(f"OneBot WS 配置    : ws://{srv.get('host')}:{srv.get('port')} enable={srv.get('enable')} token={srv.get('token')}")
    print(f"httpServers 数量  : {len(net.get('httpServers', []))} (应为 0)")
else:
    print("登录账号          : 未登录（还没有 config/onebot11_<uin>.json）")
    tpl = read_json(os.path.join(P, "onebot11.template.json")) or {}
    for srv in tpl.get("network", {}).get("websocketServers", []):
        print(f"登录后将生效的WS  : ws://{srv.get('host')}:{srv.get('port')} token={srv.get('token')}")

qr = os.path.join(N, "napcat/cache/qrcode.png")
if os.path.exists(qr):
    age = int(time.time() - os.path.getmtime(qr))
    print(f"二维码图片        : {qr} (生成于 {age}s 前)")
else:
    print("二维码图片        : 不存在（已登录或尚未生成）")
PY

# ---------- 最新扫码信息 ----------
if [ -f "$LOG" ]; then
  echo "----- 最新扫码/登录日志 -----"
  grep -E "二维码解码URL|二维码已保存|请扫描|登录成功|已经登录|OneBot|WebUi Token" "$LOG" | tail -5
  echo "----- 日志尾部 -----"
  tail -8 "$LOG"
else
  echo "日志不存在: $LOG"
fi
