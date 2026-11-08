#!/usr/bin/env bash
# 查看机器人状态：进程、OneBot 连接、最近日志
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIDFILE="$ROOT/run/bot.pid"
WS_URL="$(python3 -c "
import json,sys
try:
    d=json.load(open('$ROOT/bot/config.json'))
    print(d.get('onebot',{}).get('ws_url','ws://127.0.0.1:3001'))
except Exception: print('ws://127.0.0.1:3001')
")"
HOSTPORT="${WS_URL#ws://}"; HOSTPORT="${HOSTPORT%%/*}"
HOST="${HOSTPORT%%:*}"; PORT="${HOSTPORT##*:}"

ok=0
if [ -f "$PIDFILE" ] && [ -r "/proc/$(cat "$PIDFILE")/cmdline" ] && tr '\0' ' ' < "/proc/$(cat "$PIDFILE")/cmdline" | grep -q "main.py -c"; then
  echo "🤖 机器人：运行中（PID $(cat "$PIDFILE")）"
else
  echo "🤖 机器人：未运行"
  ok=1
fi

if python3 - "$HOST" "$PORT" <<'EOF'
import socket, sys
s = socket.socket(); s.settimeout(1.5)
sys.exit(0 if s.connect_ex((sys.argv[1], int(sys.argv[2]))) == 0 else 1)
EOF
then
  echo "🔌 OneBot 端口 $PORT：可连接"
else
  echo "🔌 OneBot 端口 $PORT：连不上（NapCat 没起来？看 protocol/status.sh）"
  ok=1
fi

if python3 - <<'EOF'
import socket, sys
s = socket.socket(); s.settimeout(1)
sys.exit(0 if s.connect_ex(("127.0.0.1", 8080)) == 0 else 1)
EOF
then
  echo "📱 管理台 8080：可访问 → http://127.0.0.1:8080/"
else
  echo "📱 管理台 8080：未启动（bash scripts/admin-start.sh）"
fi

echo "---- 最近日志 ----"
tail -n 15 "$ROOT/logs/bot.out" 2>/dev/null || echo "（还没有日志）"
exit $ok
