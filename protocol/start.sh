#!/bin/bash
# NapCat 启动脚本（幂等）：Xvfb + QQ(NT arm64) + NapCat Shell
# 用法: bash start.sh
set -u

P=/root/workspace/qqbot/protocol
N=/opt/napcat
LOG="$P/logs/napcat.log"
mkdir -p "$P/logs"

panel_up() {
  python3 - <<'PY'
import sys, urllib.request
try:
    urllib.request.urlopen("http://127.0.0.1:6099/", timeout=2); sys.exit(0)
except urllib.error.HTTPError:
    sys.exit(0)   # 有 HTTP 响应就算起来了
except Exception:
    sys.exit(1)
PY
}

# 环境被重置（QQ/NapCat/依赖丢失）时自动修复
if [ ! -x /opt/QQ/qq ] || [ ! -f /opt/napcat/launcher.sh ] || ! command -v xvfb-run >/dev/null 2>&1; then
  echo "[start] 检测到运行环境缺失，先执行修复：bash $P/setup.sh"
  bash "$P/setup.sh" || { echo "[start] 环境修复失败" >&2; exit 1; }
fi

port_open() {
  python3 - "$1" <<'PY'
import socket, sys
s = socket.socket(); s.settimeout(1)
try:
    s.connect(("127.0.0.1", int(sys.argv[1]))); sys.exit(0)
except Exception:
    sys.exit(1)
finally:
    s.close()
PY
}

if panel_up; then
  echo "[start] NapCat 已在运行（WebUI 6099 可访问），不重复启动。"
  if ! port_open 3001; then
    echo "[start] 需要登录（或正在登录中）：OneBot WS 3001 尚未打开。"
    WEBUI_TOKEN=$(python3 -c "import json;print(json.load(open('$N/napcat/config/webui.json'))['token'])" 2>/dev/null || true)
    echo "[start] 登录入口: http://127.0.0.1:6099/webui?token=${WEBUI_TOKEN}"
  fi
  echo "[start] 查看状态: bash $P/status.sh"
  exit 0
fi

# 有残留 QQ 进程但端口没起来：先清干净
if pgrep -f "/opt/QQ/qq" >/dev/null 2>&1; then
  echo "[start] 检测到残留 QQ 进程，先清理..."
  bash "$P/stop.sh" >/dev/null 2>&1
fi

# 清理 xvfb-run / X 锁残留（proot 里非正常退出常见）
rm -rf /tmp/xvfb-run.* 2>/dev/null
rm -f /tmp/.X*-lock 2>/dev/null

if [ ! -f "$P/onebot11.template.json" ]; then
  echo "[start] 缺少配置模板 $P/onebot11.template.json" >&2
  exit 1
fi

# 首次登录时 NapCat 会用 config/onebot11.json 作为模板生成 onebot11_<QQ号>.json
mkdir -p "$N/napcat/config"
if [ ! -f "$N/napcat/config/onebot11.json" ]; then
  cp "$P/onebot11.template.json" "$N/napcat/config/onebot11.json"
fi

# 已经登录过（存在 onebot11_<uin>.json）就用 -q 快速登录，省去重新扫码
UIN=$(ls "$N"/napcat/config/onebot11_*.json 2>/dev/null \
      | sed -nE 's/.*onebot11_([0-9]+)\.json$/\1/p' | head -1 || true)

# ---- 日志限流：QQ 在 proot 里会狂刷 GPU/dbus 错误，无限追加会撑爆磁盘 ----
mkdir -p "$P/logs"
rotate_log() {  # $1=文件 $2=上限MB
  [ -f "$1" ] || return 0
  local mb=$(( $(stat -c%s "$1" 2>/dev/null || echo 0) / 1048576 ))
  if [ "$mb" -ge "${2:-20}" ]; then
    tail -c 2000000 "$1" > "$1.tmp" 2>/dev/null || : > "$1.tmp"
    mv -f "$1.tmp" "$1"
  fi
}
rotate_log "$LOG" 20
ERRLOG="$P/logs/qq-stderr.log"
[ -f "$ERRLOG" ] && mv -f "$ERRLOG" "$ERRLOG.old" 2>/dev/null || true
# QQ 崩溃转储也可能很大
find /root/.config/QQ -maxdepth 3 -name "*.dmp" -size +50M -delete 2>/dev/null || true

{
  echo ""
  echo "=== napcat start $(date '+%F %T') ==="
} >> "$LOG"

# 日志守护（防止 41GB 日志再次出现）
if ! ( [ -f /root/workspace/qqbot/protocol/logs/.log-guard.pid ] && kill -0 "$(cat /root/workspace/qqbot/protocol/logs/.log-guard.pid)" 2>/dev/null ); then
  setsid nohup bash "$P/log-guard.sh" >/dev/null 2>&1 < /dev/null &
  echo "[start] 日志守护已启动"
fi

cd "$N" || exit 1
# --disable-gpu：Headless Xvfb 下不需要 GPU，能显著减少错误刷屏
QQ_EXTRA="--disable-gpu --disable-dev-shm-usage"
if [ -n "${UIN:-}" ]; then
  echo "[start] 发现已登录账号 $UIN，使用 -q 快速登录"
  setsid nohup bash ./launcher.sh -q "$UIN" $QQ_EXTRA >> "$LOG" 2> "$ERRLOG" < /dev/null &
else
  echo "[start] 首次登录：扫码登录模式"
  setsid nohup bash ./launcher.sh $QQ_EXTRA >> "$LOG" 2> "$ERRLOG" < /dev/null &
fi
echo "[start] launcher 已拉起 (pid=$!)"

# 等待 WebUI 6099
ok=0
for i in $(seq 1 180); do
  if panel_up; then ok=1; echo "[start] WebUI 就绪: http://127.0.0.1:6099 （耗时 ${i}s）"; break; fi
  sleep 1
done
if [ "$ok" != 1 ]; then
  echo "[start] 失败: 180s 内 WebUI 未就绪，日志尾部：" >&2
  tail -40 "$LOG" >&2
  exit 1
fi

WEBUI_TOKEN=$(python3 -c "import json;print(json.load(open('$N/napcat/config/webui.json'))['token'])" 2>/dev/null || true)
echo "[start] WebUI: http://127.0.0.1:6099/webui?token=${WEBUI_TOKEN}"

# 等 OneBot WS 3001（已登录时应自动就绪；一旦发现落到扫码模式就不再干等）
if [ -n "${UIN:-}" ]; then
  qr_mode=0
  for i in $(seq 1 60); do
    if port_open 3001; then
      echo "[start] OneBot v11 正向 WS 已监听: ws://127.0.0.1:3001"
      break
    fi
    # 快速登录失败 → NapCat 转为二维码登录，不必等到超时
    if [ "$qr_mode" = 0 ] && tail -c 20000 "$LOG" 2>/dev/null | grep -q "使用二维码登录方式"; then
      qr_mode=1
      echo "[start] 快速登录未成功，已转为扫码登录模式"
      break
    fi
    sleep 1
  done
  if ! port_open 3001; then
    echo "[start] 需要登录：QQ 当前未登录，所以 OneBot WS 3001 不会自动打开。"
    echo "[start] 登录入口（任选其一，登录成功后 3001 会自动监听、机器人会自动连上）："
    echo "        - 浏览器/App 里打开: http://127.0.0.1:6099/webui?token=${WEBUI_TOKEN} （扫码或密码登录）"
    echo "        - 二维码图片: ${N}/napcat/cache/qrcode.png"
    echo "        登录完成后执行: bash $P/status.sh 确认"
  fi
else
  echo "[start] 尚未登录。OneBot 配置已预置，扫码登录成功后 3001 会自动监听。"
  echo "[start] 扫码入口（二选一）："
  echo "        - WebUI 登录页: http://127.0.0.1:6099/webui?token=${WEBUI_TOKEN} （页面显示二维码，可刷新）"
  echo "        - 二维码图片:   $N/napcat/cache/qrcode.png"
  grep -E "二维码解码URL|二维码已保存|请扫描" "$LOG" | tail -3
fi
