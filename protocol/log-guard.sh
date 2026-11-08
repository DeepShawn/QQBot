#!/usr/bin/env bash
# 日志守护：QQ 在 proot 里会持续刷 GPU/dbus 错误，超限就截断，避免撑爆磁盘。
# 由 start.sh 后台拉起，stop.sh 结束。
set -uo pipefail
P="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIDFILE="$P/logs/.log-guard.pid"
echo $$ > "$PIDFILE"

limit_mb() {  # $1=文件 $2=上限MB $3=保留字节
  [ -f "$1" ] || return 0
  local size mb
  size=$(stat -c%s "$1" 2>/dev/null || echo 0)
  mb=$(( size / 1048576 ))
  if [ "$mb" -ge "${2:-20}" ]; then
    tail -c "${3:-1000000}" "$1" > "$1.tmp" 2>/dev/null || : > "$1.tmp"
    mv -f "$1.tmp" "$1"
  fi
}

while :; do
  limit_mb "$P/logs/napcat.log" 20 2000000
  limit_mb "$P/logs/qq-stderr.log" 20 1000000
  limit_mb "$P/logs/setup.log" 5 1000000
  limit_mb "/root/workspace/qqbot/logs/bot.out" 20 2000000
  limit_mb "/root/workspace/qqbot/logs/admin.out" 10 1000000
  # 画图产物只留最近 100 张
  ls -1t /root/workspace/qqbot/bot/data/images/* 2>/dev/null | tail -n +101 | xargs -r rm -f
  find /root/.config/QQ -maxdepth 3 -name "*.dmp" -size +50M -delete 2>/dev/null || true
  sleep 120
done
