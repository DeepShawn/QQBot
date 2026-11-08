#!/usr/bin/env bash
# 构建「机器人控制台」APK（纯 aapt2 + javac + d8 + apksigner，不需要 Gradle）
set -euo pipefail
cd "$(dirname "$0")"

SDK=/opt/android-sdk
AAPT2="bash $SDK/bin/aapt2"
OUT=out
APKNAME=机器人控制台.apk

rm -rf "$OUT"
mkdir -p "$OUT/res" "$OUT/gen" "$OUT/classes" "$OUT/dex"

echo "[1/6] 编译资源…"
$AAPT2 compile --dir res -o "$OUT/res.zip"

echo "[2/6] 链接资源 + 生成 R.java…"
$AAPT2 link -o "$OUT/base.apk" \
  -I "$SDK/android.jar" \
  --manifest AndroidManifest.xml \
  -R "$OUT/res.zip" \
  --java "$OUT/gen" \
  --min-sdk-version 21 \
  --target-sdk-version 30 \
  --version-code 1 --version-name 1.0.0 \
  --auto-add-overlay

echo "[3/6] 编译 Java…"
find java "$OUT/gen" -name "*.java" > "$OUT/sources.txt"
javac -source 8 -target 8 -bootclasspath "$SDK/android.jar" -d "$OUT/classes" @"$OUT/sources.txt" 2>&1 | grep -v "bootstrap class path" || true
jar cf "$OUT/classes.jar" -C "$OUT/classes" .

echo "[4/6] dex…"
java -cp "$SDK/d8.jar" com.android.tools.r8.D8 --release --min-api 21 \
  --lib "$SDK/android.jar" --output "$OUT/dex" "$OUT/classes.jar"

echo "[5/6] 对齐重打包…"
python3 repack.py "$OUT/base.apk" "$OUT/dex/classes.dex" assets "$OUT/unsigned.apk"

echo "[6/6] 签名…"
if [ ! -f qqbot.keystore ]; then
  keytool -genkeypair -keystore qqbot.keystore -alias qqbot -keyalg RSA -keysize 2048 \
    -validity 10000 -storepass "${QBOT_KS_PASS:-qqbot123}" -keypass "${QBOT_KS_PASS:-qqbot123}" \
    -dname "CN=QQBot Console, OU=DSH, O=DSH, L=NA, ST=NA, C=CN" >/dev/null 2>&1
  echo "  已生成签名用 keystore: qqbot.keystore"
fi
java -jar "$SDK/apksigner.jar" sign \
  --ks qqbot.keystore --ks-pass "pass:${QBOT_KS_PASS:-qqbot123}" --key-pass "pass:${QBOT_KS_PASS:-qqbot123}" \
  --min-sdk-version 21 --out "$APKNAME" "$OUT/unsigned.apk"

java -jar "$SDK/apksigner.jar" verify --min-sdk-version 21 "$APKNAME"

echo
echo "✅ 构建完成: $(pwd)/$APKNAME"
ls -la "$APKNAME"
