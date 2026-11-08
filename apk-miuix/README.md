# 机器人控制台（独立 App）

Jetpack Compose + [MIUIX](https://github.com/miuix-kotlin-multiplatform/miuix) 写的安卓 App。
它不只是控制台：**整套 QQ 机器人（Linux QQ + NapCat + Python 机器人 + 管理 API）都跑在这个 App 里**，
装好后不依赖 Termux、DSH 或电脑。

## 工作原理

```
App（前台服务 ProotService）
 └─ proot（assets 里的 aarch64 二进制，App 私有目录里 exec）
     └─ Ubuntu rootfs（首次启动从 rootfs.tar.gz 解压到 filesDir/rootfs）
         ├─ Xvfb + Linux QQ + NapCat  → OneBot v11 ws://127.0.0.1:3001
         ├─ bot/main.py               → 机器人
         └─ bot/admin/server.py       → 管理 API http://127.0.0.1:8080
App 界面（MIUIX 原生）通过 8080 读写状态和配置
```

- `targetSdk 28`：为了能从 App 私有目录执行 proot（和 Termux 一样），所以只能侧载，不能上架 Play。
- proot 参数、环境变量（`PROOT_LOADER` / `PROOT_TMP_DIR` / `PROOT_L2S_DIR`）与 DSH 在同一台手机上验证过的配置一致。
- 前台服务 + `PARTIAL_WAKE_LOCK` + Wi-Fi 锁，熄屏不断线；开机 / App 更新后自动恢复。
- 闪退日志：`/sdcard/Download/DSH-Go/qqbot-crash.log`；运行日志：`qqbot-app.log`。

## 页面

| 页 | 内容 |
|---|---|
| 概览 | 运行状态、开启 / 关闭 / 重启、登录入口、自动守护 |
| AI | AI 账号（ChatGPT / Claude / Gemini / DeepSeek 登录）、多套接口切换、认证方式、协议 / 地址 / 模型 / Key、人格、触发方式、能力开关、记忆 |
| 群管 | 欢迎、违禁词（含正则）、防刷屏、豁免开关与名单、管理指令 |
| 更多 | 关键词回复、画图、对话记忆、日志、网页控制台、后台运行（后台常驻 / 电池优化 / 自启动）、检查更新 / 自动检查更新、更新机器人代码、重装运行环境 |

## 构建

```bash
cd apk-miuix
# 签名：在 ~/.gradle/gradle.properties 或命令行提供
#   QQBOT_KS_FILE / QQBOT_KS_PASS / QQBOT_KS_ALIAS
./gradlew :app:assembleDebug    # 或 gradle 8.13+
```

需要 JDK 17、Android SDK platform 37、AGP 8.13。在 arm64 Linux（如手机 proot）上构建时，
Google 的 aapt2 只有 x86_64 版，要用 `android.aapt2FromMavenOverride` 指向 arm64 版 aapt2。

## 运行环境包（rootfs.tar.gz）

**不在仓库里**（约 900MB，且会包含你的 QQ 登录态和 API Key）。在已经跑通的 Ubuntu 环境里打包：

```bash
cd / && tar czf /sdcard/Download/DSH-Go/rootfs-pkg/rootfs.tar.gz --hard-dereference \
  --exclude=root/.l2s --exclude=root/.cache --exclude=__pycache__ \
  --exclude=usr/share/doc --exclude=usr/share/man --exclude=usr/share/locale \
  bin sbin lib usr etc opt/QQ opt/python opt/napcat root/napcat root/workspace/qqbot root/.config/QQ var/lib/dpkg
```

App 首次打开会在上面这个默认位置找安装包，也可以手动选择文件。装完建议删掉手机上的安装包。

## 第三方组件

- assets 里的 `proot` / `loader` / `libtalloc.so.2` / `libandroid-shmem.so` 来自 [Termux 软件源](https://packages.termux.dev)（proot 为 GPL-2.0）。
