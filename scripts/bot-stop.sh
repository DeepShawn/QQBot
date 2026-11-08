#!/usr/bin/env bash
# 停止机器人
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIDFILE="$ROOT/run/bot.pid"

is_bot_pid() {
  [ -n "$1" ] && [ -r "/proc/$1/cmdline" ] && tr '\0' ' ' < "/proc/$1/cmdline" | grep -q "main.py -c"
}

if [ -f "$PIDFILE" ]; then
  PID="$(cat "$PIDFILE")"
  if is_bot_pid "$PID"; then
    kill -TERM "$PID" 2>/dev/null || true
    for _ in $(seq 1 20); do
      kill -0 "$PID" 2>/dev/null || break
      sleep 0.3
    done
    kill -0 "$PID" 2>/dev/null && kill -9 "$PID" 2>/dev/null || true
    echo "已停止，PID $PID"
  else
    echo "PID $PID 不在运行"
  fi
  rm -f "$PIDFILE"
else
  echo "没有 pid 文件，尝试按进程名清理"
  pkill -f "main.py -c $ROOT/bot/config.json" 2>/dev/null && echo "已清理" || echo "没找到运行中的机器人"
fi
