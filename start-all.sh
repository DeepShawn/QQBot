#!/usr/bin/env bash
# 一键启动：协议层（QQ+NapCat）→ 等 3001 端口就绪 → 机器人
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "① 启动协议层（Linux QQ + NapCat）…"
if [ -x "$ROOT/protocol/start.sh" ]; then
  bash "$ROOT/protocol/start.sh" || echo "⚠️ 协议层启动有报错，继续尝试启动机器人"
else
  echo "⚠️ 没有 protocol/start.sh，先跳过协议层"
fi

echo "② 等 OneBot 端口 3001…"
for i in $(seq 1 60); do
  if python3 - <<'EOF'
import socket, sys
s = socket.socket(); s.settimeout(1)
sys.exit(0 if s.connect_ex(("127.0.0.1", 3001)) == 0 else 1)
EOF
  then
    echo "   ✅ 3001 已就绪"
    break
  fi
  sleep 2
  [ "$i" = "60" ] && echo "   ⚠️ 120 秒还没就绪，先起机器人（它会自动重连）"
done

echo "③ 启动机器人…"
bash "$ROOT/scripts/bot-start.sh"

echo "④ 启动管理台（APK / 浏览器用）…"
bash "$ROOT/scripts/admin-start.sh" || true

echo
echo "完成。状态：$ROOT/scripts/bot-status.sh"
echo "管理台：http://127.0.0.1:8080/"
