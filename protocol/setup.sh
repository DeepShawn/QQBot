#!/usr/bin/env bash
# 一键安装 / 修复运行环境（幂等，可重复执行）。
# 手机 App 更新可能会重置 /opt 和 /usr，但 /root 会保留，所以：
#   - NapCat 装在 /root/napcat（持久），/opt/napcat 只是软链接
#   - QQ 安装包、NapCat 安装包缓存在 protocol/downloads/，不用重新下载
#   - QQ 登录态在 /root/.config/QQ（持久），修复后可快捷登录
set -uo pipefail
P="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DL="$P/downloads"
NAPCAT_HOME=/root/napcat
QQ_DEB="$(ls "$DL"/linuxqq_*_arm64.deb 2>/dev/null | head -1)"
export DEBIAN_FRONTEND=noninteractive

log() { echo "[setup $(date +%H:%M:%S)] $*"; }
need_apt=0

# ---------- 1. 系统依赖 ----------
for b in curl jq unzip xvfb-run Xvfb xauth g++ pgrep python3; do
  command -v "$b" >/dev/null 2>&1 || need_apt=1
done
[ -x /opt/QQ/qq ] || need_apt=1

if [ "$need_apt" = 1 ]; then
  log "安装系统依赖（apt）…"
  apt-get update -qq || log "apt-get update 有报错，继续尝试安装"
  apt-get install -y -qq curl jq zip unzip xvfb xauth procps g++ screen python3 python3-pip \
      libnss3 libgbm1 libgssapi-krb5-2 >/dev/null || { log "依赖安装失败"; exit 1; }
  apt-get install -y -qq libasound2t64 >/dev/null 2>&1 || apt-get install -y -qq libasound2 >/dev/null 2>&1 || true
else
  log "系统依赖已齐全"
fi

# ---------- 2. Linux QQ ----------
if [ ! -x /opt/QQ/qq ]; then
  if [ -z "$QQ_DEB" ]; then
    log "下载 QQ 安装包…"
    mkdir -p "$DL"
    curl -fL --retry 3 -o "$DL/linuxqq_3.2.34-53644_arm64.deb" \
      "https://qqdl.gtimg.cn/qqfile/QQNT/9.9.36/beta/9ee04bef/linuxqq_3.2.34-53644_arm64.deb" || { log "QQ 下载失败"; exit 1; }
    QQ_DEB="$DL/linuxqq_3.2.34-53644_arm64.deb"
  fi
  log "安装 Linux QQ（$(basename "$QQ_DEB")）…"
  apt-get install -y -qq --allow-downgrades "$QQ_DEB" >/dev/null || { log "QQ 安装失败"; exit 1; }
else
  log "Linux QQ 已安装"
fi
missing=$(ldd /opt/QQ/qq 2>/dev/null | grep "not found" || true)
[ -n "$missing" ] && log "警告：QQ 缺少动态库：$missing"

# ---------- 3. NapCat（持久化在 /root/napcat） ----------
# 迁移：旧版装在 /opt/napcat（真实目录）时搬到 /root/napcat
if [ -d /opt/napcat ] && [ ! -L /opt/napcat ] && [ ! -e "$NAPCAT_HOME" ]; then
  log "迁移 /opt/napcat → $NAPCAT_HOME"
  mv /opt/napcat "$NAPCAT_HOME"
fi
mkdir -p "$NAPCAT_HOME"
if [ ! -f "$NAPCAT_HOME/napcat/napcat.mjs" ] || [ ! -f "$NAPCAT_HOME/libnapcat_launcher.so" ] || [ ! -f "$NAPCAT_HOME/launcher.sh" ]; then
  log "安装 NapCat 到 $NAPCAT_HOME …"
  cp "$P/napcat-linux-install.sh" "$NAPCAT_HOME/install.sh"
  [ -f "$DL/NapCat.Shell.zip" ] && cp "$DL/NapCat.Shell.zip" "$NAPCAT_HOME/NapCat.Shell.zip"
  args=(--skip-deps --github-proxy 0)
  [ -f "$DL/launcher.cpp" ] && args+=(--launcher-source "$DL/launcher.cpp")
  ( cd "$NAPCAT_HOME" && bash ./install.sh "${args[@]}" ) || { log "NapCat 安装失败"; exit 1; }
  [ -f "$NAPCAT_HOME/launcher.cpp" ] && cp "$NAPCAT_HOME/launcher.cpp" "$DL/launcher.cpp"
else
  log "NapCat 已安装（$NAPCAT_HOME）"
fi
# launcher.so 依赖系统 libstdc++ 版本，重置后重新编译一次更稳
if ! LD_PRELOAD="$NAPCAT_HOME/libnapcat_launcher.so" /bin/true 2>/dev/null; then
  log "重新编译 libnapcat_launcher.so"
  ( cd "$NAPCAT_HOME" && g++ -shared -fPIC launcher.cpp -o libnapcat_launcher.so -ldl ) || log "编译失败"
fi
ln -sfn "$NAPCAT_HOME" /opt/napcat

# OneBot 模板（首次登录时据此生成 onebot11_<QQ号>.json）
mkdir -p "$NAPCAT_HOME/napcat/config"
[ -f "$NAPCAT_HOME/napcat/config/onebot11.json" ] || cp "$P/onebot11.template.json" "$NAPCAT_HOME/napcat/config/onebot11.json"
# 已知账号但账号配置丢了：从模板补回，保证 3001 WS 配置正确
uin_file="$P/.last_uin"
if [ -s "$uin_file" ]; then
  uin="$(cat "$uin_file")"
  [ -f "$NAPCAT_HOME/napcat/config/onebot11_${uin}.json" ] || cp "$P/onebot11.template.json" "$NAPCAT_HOME/napcat/config/onebot11_${uin}.json"
fi

# ---------- 4. Python 依赖 ----------
if ! python3 -c "import websockets, PIL, pypdf" >/dev/null 2>&1; then
  log "安装 Python 依赖（websockets pillow pypdf）…"
  PIP_EXTRA=""
  python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" 2>/dev/null && PIP_EXTRA="--break-system-packages"
  pip3 install -q --root-user-action=ignore $PIP_EXTRA websockets pillow pypdf || { log "pip 安装失败"; exit 1; }
else
  log "Python 依赖已齐全"
fi

log "✅ 环境就绪"
