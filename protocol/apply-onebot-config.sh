#!/bin/bash
# 登录成功后执行：把 OneBot v11 配置写入当前账号的 config/onebot11_<QQ号>.json
#   正向 WS: ws://127.0.0.1:3001  token: $QBOT_WS_TOKEN（默认 change-me-token）
#   httpServers 关闭
# 只改 network 字段，保留其它配置；改完自动重启 NapCat（已登录账号会 -q 快速登录）。
# 用法: bash apply-onebot-config.sh [--no-restart]
set -u

P=/root/workspace/qqbot/protocol
N=/opt/napcat
CFG="$N/napcat/config"

if ! ls "$CFG"/onebot11_[0-9]*.json >/dev/null 2>&1; then
  echo "[apply] 还没登录：找不到 $CFG/onebot11_<QQ号>.json"
  echo "[apply] 请先扫码登录（WebUI 或 qrcode.png），登录成功后再运行本脚本。"
  exit 2
fi

python3 - "$CFG" <<'PY'
import glob, json, os, sys

cfgdir = sys.argv[1]
files = sorted(glob.glob(os.path.join(cfgdir, "onebot11_[0-9]*.json")))
token = os.environ.get("QBOT_WS_TOKEN", "change-me-token")

ws = {
    "name": "ws",
    "enable": True,
    "host": "127.0.0.1",
    "port": 3001,
    "messagePostFormat": "array",
    "reportSelfMessage": False,
    "token": token,
    "enableForcePushEvent": True,
    "debug": False,
    "heartInterval": 30000,
}

for f in files:
    try:
        data = json.load(open(f))
    except Exception as e:
        print(f"[apply] 读取 {f} 失败: {e}")
        continue
    net = data.setdefault("network", {})
    net["httpServers"] = []
    net["httpSseServers"] = []
    net["httpClients"] = []
    # 保留/替换名为 ws 的项，其它 websocketServers 条目保留
    others = [s for s in net.get("websocketServers", []) if s.get("name") != "ws"]
    net["websocketServers"] = [ws] + others
    net.setdefault("websocketClients", [])
    net.setdefault("plugins", [])
    data["network"] = net
    open(f, "w").write(json.dumps(data, indent=2) + "\n")
    print(f"[apply] 已写入: {f}")
    print(f"[apply]   → ws://127.0.0.1:3001  token={token}")
    print(f"[apply]   → httpServers 已关闭")
PY

# 同步更新配置模板，保证以后重新登录/新增账号也是这套配置
if [ -f "$P/onebot11.template.json" ]; then
  cp "$P/onebot11.template.json" "$CFG/onebot11.json"
  echo "[apply] 已同步模板 $CFG/onebot11.json"
fi

if [ "${1:-}" = "--no-restart" ]; then
  echo "[apply] 已跳过重启（--no-restart）。需要生效请手动: bash $P/stop.sh && bash $P/start.sh"
  exit 0
fi

echo "[apply] 重启 NapCat 让配置生效 ..."
bash "$P/stop.sh"
bash "$P/start.sh"

echo "[apply] 校验 WS 握手 ..."
bash "$P/verify-ws.sh" || true
