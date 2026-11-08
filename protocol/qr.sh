#!/usr/bin/env bash
# 把最新的登录二维码导出到手机相册/下载目录，并打印 WebUI 链接
# 用法：
#   bash qr.sh            # 导出当前二维码
#   bash qr.sh --refresh  # 重启 NapCat 生成一张全新二维码再导出（扫码前推荐）
set -u
P=/root/workspace/qqbot/protocol
N=/opt/napcat
QR="$N/napcat/cache/qrcode.png"
OUT1="/sdcard/Download/DSH-Go/QQ登录二维码.png"
OUT2="/sdcard/DCIM/QQBot/napcat-qr.png"

if [ "${1:-}" = "--refresh" ]; then
  echo "[qr] 重启 NapCat 以生成新二维码…"
  bash "$P/stop.sh" >/dev/null 2>&1
  bash "$P/start.sh" >/dev/null 2>&1
  # 等二维码文件刷新
  for _ in $(seq 1 30); do
    [ -f "$QR" ] && [ "$(find "$QR" -newermt '-40 seconds' 2>/dev/null)" ] && break
    sleep 1
  done
fi

if [ ! -f "$QR" ]; then
  echo "[qr] 还没有二维码文件，先启动协议层：bash $P/start.sh" >&2
  exit 1
fi

TOKEN=$(python3 -c "import json;print(json.load(open('$N/napcat/config/webui.json'))['token'])" 2>/dev/null || echo "")
mkdir -p "/sdcard/Download/DSH-Go" "/sdcard/DCIM/QQBot"
cp "$QR" "$OUT1" 2>/dev/null && echo "[qr] 已导出：$OUT1"
cp "$QR" "$OUT2" 2>/dev/null && echo "[qr] 已导出：$OUT2（会出现在相册）"
python3 - "$QR" "$OUT1" <<'PY'
import sys
try:
    from PIL import Image
    src, dst = sys.argv[1], sys.argv[2]
    im = Image.open(src).convert("L")
    w, h = im.size
    scale = max(1, 600 // max(w, h))
    big = im.resize((w * scale, h * scale), Image.NEAREST)
    canvas = Image.new("L", (big.width + 80, big.height + 80), 255)
    canvas.paste(big, (40, 40))
    out = dst.replace(".png", "-大图.png")
    canvas.save(out)
    print(f"[qr] 放大版（好扫）：{out}")
except Exception as e:
    print("[qr] 放大失败（不影响扫码）：", e)
PY
echo "[qr] WebUI 登录页：http://127.0.0.1:6099/webui?token=${TOKEN}"
echo "[qr] 二维码约 1-2 分钟过期；过期就再跑一次：bash $P/qr.sh --refresh"
echo "[qr] 登录方式：① QQ 扫一扫 → 右下角相册 → 选上面的图片；② WebUI 里用密码登录"
