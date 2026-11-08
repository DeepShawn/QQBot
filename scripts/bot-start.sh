#!/usr/bin/env bash
# 启动机器人（后台常驻，日志写 logs/bot.out）
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p "$ROOT/run" "$ROOT/logs"
PIDFILE="$ROOT/run/bot.pid"

# PID 必须真的是机器人进程（防止 PID 被别的进程复用造成「假运行」）
is_bot_pid() {
  local pid="$1"
  [ -n "$pid" ] && [ -r "/proc/$pid/cmdline" ] && tr '\0' ' ' < "/proc/$pid/cmdline" | grep -q "main.py -c"
}

if [ -f "$PIDFILE" ] && is_bot_pid "$(cat "$PIDFILE")"; then
  echo "机器人已在运行，PID $(cat "$PIDFILE")"
  exit 0
fi
rm -f "$PIDFILE"

# Python 依赖丢失（环境被重置）时自动补装
if ! python3 -c "import websockets" >/dev/null 2>&1; then
  echo "缺少 Python 依赖，正在安装 websockets pillow pypdf …"
  pip3 install -q --root-user-action=ignore websockets pillow pypdf || { echo "pip 安装失败"; exit 1; }
fi

# bot.out 太大就截断，避免无限增长
if [ -f "$ROOT/logs/bot.out" ]; then
  sz=$(( $(stat -c%s "$ROOT/logs/bot.out" 2>/dev/null || echo 0) / 1048576 ))
  if [ "$sz" -ge 20 ]; then
    tail -c 2000000 "$ROOT/logs/bot.out" > "$ROOT/logs/bot.out.tmp" 2>/dev/null || : > "$ROOT/logs/bot.out.tmp"
    mv -f "$ROOT/logs/bot.out.tmp" "$ROOT/logs/bot.out"
  fi
fi

cd "$ROOT/bot"
setsid bash -c 'echo $$ > "'"$PIDFILE"'"; exec python3 main.py -c "'"$ROOT"'/bot/config.json"' \
  >> "$ROOT/logs/bot.out" 2>&1 &

for _ in $(seq 1 20); do
  [ -s "$PIDFILE" ] && break
  sleep 0.3
done
sleep 2

if [ -f "$PIDFILE" ] && is_bot_pid "$(cat "$PIDFILE")"; then
  echo "机器人已启动，PID $(cat "$PIDFILE")，日志：$ROOT/logs/bot.out"
else
  echo "启动失败，看日志：$ROOT/logs/bot.out"
  tail -20 "$ROOT/logs/bot.out" || true
  rm -f "$PIDFILE"
  exit 1
fi
