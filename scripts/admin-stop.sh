#!/usr/bin/env bash
# 停止管理台
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIDFILE="$ROOT/run/admin.pid"

if [ -f "$PIDFILE" ]; then
  PID="$(cat "$PIDFILE")"
  if kill -0 "$PID" 2>/dev/null; then
    kill -TERM "$PID" 2>/dev/null || true
    for _ in $(seq 1 20); do
      kill -0 "$PID" 2>/dev/null || break
      sleep 0.2
    done
    kill -0 "$PID" 2>/dev/null && kill -9 "$PID" 2>/dev/null || true
    echo "管理台已停止，PID $PID"
  else
    echo "PID $PID 不在运行"
  fi
  rm -f "$PIDFILE"
else
  echo "没有 pid 文件，尝试按进程名清理"
  pkill -f "admin/server.py" 2>/dev/null && echo "已清理" || echo "没找到运行中的管理台"
fi
