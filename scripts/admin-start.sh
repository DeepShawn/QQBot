#!/usr/bin/env bash
# 启动管理台（127.0.0.1:8080，给 APK / 浏览器用）
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p "$ROOT/run" "$ROOT/logs"
PIDFILE="$ROOT/run/admin.pid"

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "管理台已在运行，PID $(cat "$PIDFILE") → http://127.0.0.1:8080/"
  exit 0
fi

setsid bash -c 'echo $$ > "'"$PIDFILE"'"; exec python3 "'"$ROOT"'/bot/admin/server.py"' \
  >> "$ROOT/logs/admin.out" 2>&1 &

for _ in $(seq 1 20); do
  [ -s "$PIDFILE" ] && break
  sleep 0.3
done
sleep 1

if python3 - <<'EOF'
import socket, sys
s = socket.socket(); s.settimeout(1)
sys.exit(0 if s.connect_ex(("127.0.0.1", 8080)) == 0 else 1)
EOF
then
  echo "管理台已启动：http://127.0.0.1:8080/ （PID $(cat "$PIDFILE")）"
else
  echo "管理台启动失败，日志：$ROOT/logs/admin.out"
  tail -20 "$ROOT/logs/admin.out" || true
  exit 1
fi
