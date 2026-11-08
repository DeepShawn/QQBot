#!/bin/bash
# 停止 NapCat + QQ + Xvfb（proot 环境，无 systemd）
set -u

P=/root/workspace/qqbot/protocol

echo "[stop] 停止 NapCat/QQ/Xvfb ..."

# 先 SIGTERM，让 QQ/NapCat 自己落盘退出
pkill -TERM -f "/opt/QQ/qq"                       2>/dev/null || true
# Electron 子进程 cmdline 是 /proc/self/exe ... --user-data-dir=/root/.config/QQ，单独匹配
pkill -TERM -f "user-data-dir=/root/.config/QQ"   2>/dev/null || true
pkill -TERM -f "xvfb-run"                         2>/dev/null || true

for _ in $(seq 1 15); do
  pgrep -f "/opt/QQ/qq" >/dev/null 2>&1 || break
  sleep 1
done

# 残留的强杀
pkill -KILL -f "/opt/QQ/qq"                       2>/dev/null || true
pkill -KILL -f "user-data-dir=/root/.config/QQ"   2>/dev/null || true
pkill -KILL -f "xvfb-run"                         2>/dev/null || true
pkill -KILL -f "Xvfb :"                           2>/dev/null || true

rm -rf /tmp/xvfb-run.* 2>/dev/null
rm -f  /tmp/.X*-lock   2>/dev/null

GUARD="$P/logs/.log-guard.pid"
if [ -f "$GUARD" ]; then
  kill -TERM "$(cat "$GUARD")" 2>/dev/null || true
  rm -f "$GUARD"
  echo "[stop] 日志守护已结束"
fi
echo "[stop] 完成。"
