# QQ 机器人 · 跑在 Android 手机里

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

一个**完全跑在安卓手机里**的 QQ 机器人：Linux QQ + NapCat 提供协议层，Python 机器人提供功能，
管理台既是网页也能打包成独立的安卓 App（服务内置在 App 里，不依赖 Termux / 电脑 / 服务器）。

- **AI 聊天**：多套接口随时切换（OpenAI 兼容 / Claude / Gemini 原生，支持中转站）
- **AI 账号**：ChatGPT / Claude / Gemini / DeepSeek 可以用账号登录（浏览器授权，不填 API Key，用订阅额度）
- **自动更新**：App 检查 GitHub 发布并一键安装；机器人代码也能一键更新
- **触发方式**：@、叫名字、引用它的消息，或唤醒后连续对话（可关）
- **群管理**：入群欢迎、违禁词（支持正则）、刷屏检测（文字 / 图片），豁免开关与名单
- **管理指令**：引用消息 + @它 说「撤回 / 禁言 / 踢了」
- **能力**：联网搜索、识图、读文档、写代码自动存文件发群、文生图
- **管理台**：概览 / AI / 群管 / 更多四个页面，改配置 2 秒生效

## 快速开始

前置：一台 arm64 安卓手机（Android 8+，建议 10 以上），能装 Linux QQ 的 arm64 版本。

```bash
# 1) 协议层：Xvfb + Linux QQ + NapCat（首次会自动装依赖和 QQ）
git clone https://github.com/<你>/QQBot.git && cd QQBot
bash protocol/start.sh          # 首次会提示扫码登录，登录后 3001 端口就绪

# 2) 机器人
cp bot/config.example.json bot/config.json   # 然后填 API key
pip install websockets pillow pypdf
bash scripts/bot-start.sh

# 3) 管理台（网页）
bash scripts/admin-start.sh     # http://127.0.0.1:8080/
```

一键全起：`bash start-all.sh`。

**安卓 App（可选，服务内置）**：`apk-miuix/` 是 Jetpack Compose + MIUIX 的独立 App，
内置 proot 运行环境，装好后不依赖任何其它 App。见 [apk-miuix/README.md](apk-miuix/README.md)。

## 目录结构

```
QQBot/
├── protocol/            # 协议层：Linux QQ(arm64) + NapCat + Xvfb
│   ├── start.sh         # 启动（Xvfb + QQ + NapCat，含依赖自修复）
│   ├── stop.sh / status.sh / qr.sh
│   ├── setup.sh         # 环境修复（apt 依赖、QQ、NapCat）
│   └── findings.md      # 版本、端口、坑与排查记录
├── bot/                 # 机器人本体（纯 Python，无框架依赖）
│   ├── main.py          # 入口：连 NapCat 的 OneBot v11 WebSocket
│   ├── config.py        # 默认配置 + 热更新
│   ├── plugins/         # keyword / ai_chat / group_admin / image_gen / commands
│   ├── providers.py     # OpenAI 兼容 / Claude / Gemini 三种协议
│   ├── media.py         # 图片下载、文档文本抽取（pdf/docx/pptx/xlsx）
│   ├── search.py        # DuckDuckGo / Bing 联网搜索
│   ├── files.py         # 代码块存文件并发送
│   └── admin/web/       # 管理台单文件页面
├── apk-miuix/           # 独立安卓 App（Compose + MIUIX + 内置 proot）
├── apk/                 # 早期的 WebView 版控制台（已不再使用）
├── scripts/             # 机器人 / 管理台启停
└── tools/               # 自测与调试脚本（selftest / send_file / 各功能测试）
```

## 一、日常使用

```bash
cd ~/workspace/qqbot

./start-all.sh          # 一键：协议层（QQ+NapCat）→ 等 3001 → 机器人
./scripts/bot-status.sh # 看机器人状态
./scripts/bot-stop.sh   # 停机器人
./protocol/stop.sh      # 停协议层
```

> 第一次使用要先登录一次 QQ，见第五节。之后 `./start-all.sh` 就会用快捷登录免扫码。

查看日志：

```bash
tail -f logs/bot.out              # 机器人控制台输出
tail -f bot/logs/bot.log         # 机器人结构化日志
tail -f protocol/logs/*.log       # NapCat / QQ
```

## 二、配置（bot/config.json）

改完保存，**不用重启**，2 秒内自动生效。

### 1) 接自己的 AI

```json
"ai_chat": {
  "enabled": true,
  "base_url": "https://api.deepseek.com/v1",
  "api_key": "sk-你的key",
  "model": "deepseek-chat"
}
```

- `base_url` 任何 OpenAI 兼容接口都行（DeepSeek、通义千问、Kimi、自建 one-api、本地 vLLM…），填到 `/v1` 为止。
- 不填 `api_key` 时 AI 功能自动跳过，其它功能照常。

**什么时候回 AI**：私聊直接说话；群里**只有 @机器人** 才回（不吃 `ai ` 前缀）。私聊不想要回复时把 `trigger.private` 改成 `false`。

**回复样式**：群里回复会 @ 提问的人，默认**不引用**原消息（`at_sender: true` / `reply_quote: false`）。

**图片 / 文件**：用户发图片或文件、或者引用一条带图/带文件的消息，机器人会下载后处理：
- 图片 → 视觉模型识别（默认复用 `base_url`/`api_key`/`model`；想单独配视觉模型就填 `plugins.ai_chat.vision`，可用通义 qwen-vl、GLM-4V 等）
- 文件 → txt/md/csv/json/docx/pptx/xlsx/pdf 抽正文（超长截断到 `attachments.max_file_chars`）
- 开关与上限：`plugins.ai_chat.attachments`（`enabled`/`max_images`/`max_file_bytes`/`max_file_chars`）

**写代码 / 发文件**：`plugins.ai_chat.files`
- 回答里出现代码块时，满足以下任一条件就保存成文件（按语言自动选 .py/.js/.ts/.java/.go/.sql/.sh/.html…）并上传到当前会话：
  - 代码 ≥ `min_lines`（默认 15 行）或 ≥ `min_chars`（默认 400 字）
  - 你的消息里包含「文件 / 发我 / 导出 / 保存 / 下载 / 附件 / 打包 / 给我发」
- 发送成功时正文里的长代码块会替换成「（代码已保存为文件 xxx）」，不再刷屏；**发送失败会保留原文**，不会吞内容
- `max_files` 默认 3；`note` 是附在回复末尾的提示语
- 手动把本地文件发到 QQ：`python3 tools/send_file.py 路径 --group 群号` 或 `--private QQ号`

**空回复**：模型偶尔会返回空内容（常是内容过滤/截断），会自动重试一次；仍空则回复「这次模型返回了空内容（多半是触发了内容过滤），换个说法再试试？」，不会假装“没想出来”。

**群管理**（`plugins.group_admin`）：
- 入群欢迎、违禁词（支持 `words` 子串 + `regex_words` 正则）、刷屏（文字 `max_messages`、图片 `max_images`）
- **管理员豁免是可选开关**：`badwords.ignore_admins` / `flood.ignore_admins`（默认 `false`，即群主/管理员违规也处理）；
  还能用 `ignore_users` 指定豁免的 QQ 号（数组）。两个规则各自独立配置。
- **管理指令**：群里引用一条消息，@机器人（或叫名字）说「撤回 / 禁言 / 踢了」，机器人会处理那条消息；
  不引用时默认处理**最近一条群友消息**。只有群主/管理员或 `bot.admins` 里的人能用。
- 权限说明：只有机器人**在该群是管理员**时才能撤回别人的消息、禁言；普通成员群里这些操作会失败并提示。
  检查：App 概览页或 `get_group_member_info` 里机器人的 role。

**联网搜索**：`plugins.ai_chat.search`
- `trigger_keywords`（默认含 搜索/查一下/最新/今天/新闻/天气/股价…）：消息里出现就触发
- `always: true` 每条都搜；`provider`: `auto`（DuckDuckGo→Bing 兜底）/`duckduckgo`/`bing`
- 也可以直接发「搜：问题」强制搜索；搜索结果会以「参考资料 + 链接」注入模型上下文

### 2) 关键词回复

```json
"keyword": {
  "enabled": true,
  "rules": [
    {
      "name": "签到",
      "keywords": ["签到", "打卡"],
      "match": "contains",
      "scope": "group",
      "reply": "{nickname} 签到成功～"
    },
    {
      "name": "编号",
      "match": "regex",
      "regex": "^#\\d+$",
      "reply": "收到编号"
    }
  ]
}
```

- 群里默认**只有 @ 机器人** 才会触发（`"group_require_at": true`），普通聊天不会插话；私聊照常。
- `match`: `contains`（包含）/ `exact`（完全相等）/ `regex`（正则）
- `scope`: `all` / `group` / `private`
- `reply` 里可用占位符：`{nickname}` `{user_id}` `{group_id}` `{bot_name}`
- `cooldown_seconds`: 同一条规则最低触发间隔（防刷）

### 3) 群管理

```json
"group_admin": {
  "welcome": {
    "enabled": true,
    "groups": [],
    "template": "欢迎 {at} 加入本群～"
  },
  "badwords": {
    "enabled": true,
    "words": ["广告", "加群", "代刷"],
    "action": "recall+mute",
    "mute_seconds": 300
  },
  "flood": {
    "enabled": true,
    "max_messages": 6,
    "window_seconds": 10,
    "mute_seconds": 120
  }
}
```

- `groups: []` 表示所有群生效；填群号则只在这些群生效。
- `action` 可组合：`recall`（撤回）/ `mute`（禁言）/ `kick`（踢人），例如 `recall+mute`。
- 群主和管理员默认不受违禁词/刷屏限制（`ignore_admins`）。

### 4) 管理员

```json
"bot": { "admins": [你的QQ号] }
```

私聊发 `/status` `/ping` `/reload` `/help`；在群里需要 @机器人 再发命令。

## 三、机器人能做什么（当前实现）

| 功能 | 说明 |
|---|---|
| 关键词回复 | 私聊直接命中；群里必须 @ 机器人 才回 |
| AI 聊天 | 私聊直接聊；群里只有 @ 才回；回复会 @ 提问的人（不引用原消息）；带上下文记忆 |
| 图片识别 | 发图 / 引用带图的消息 → 视觉模型识别后一起回答（复用 AI 的 base_url/key/model，实测可用） |
| 文件读取 | 发 txt/docx/pptx/xlsx/pdf → 抽取正文喂给模型（PDF 需 pypdf） |
| 联网搜索 | 消息含「最新/今天/新闻/查一下/搜索…」时先联网搜再回答，结果带来源链接；可显式写「搜：xxx」 |
| 写代码发文件 | 回复里的代码块超过 15 行/400 字，或你说「发文件/发我/导出/保存」，会存成本地文件并发到群里/私聊；也能把本地任意文件发出去 |
| 入群欢迎 | 新成员进群自动 @ 欢迎 |
| 违禁词 | 撤回 + 禁言 + 警告 |
| 防刷屏 | 时间窗内发太多条 → 禁言 |
| 管理员命令 | /status /ping /reload /help |
| 断线重连 | NapCat 重启后机器人自动连回去 |
| 配置热更新 | 改 config.json 不用重启 |

## 四、加自己的功能

在 `bot/plugins/` 下新建文件，继承 `PluginBase`，实现 `async def on_event(self, event) -> bool`，返回 `True` 表示事件已被处理。然后在 `bot/plugins/__init__.py` 和 `bot/main.py` 里注册即可。

```python
from dispatcher import PluginBase

class Plugin(PluginBase):
    name = "my_plugin"
    priority = 400

    async def on_event(self, event):
        if event.get("post_type") != "message":
            return False
        if "写诗" in (event.get("raw_message") or ""):
            await self.ctx.reply(event, "床前明月光…")
            return True
        return False
```

## 五、首次登录 QQ（只做一次）

**为什么不能一上来就「快捷登录」**：NapCat 的 `-q 快速登录` 复用的是**这台设备上已保存的登录凭证**（存在 `/root/.config/QQ`）。这台手机是第一次装 Linux QQ，没有凭证，所以必须先成功登录一次；登录成功后再启动，`protocol/start.sh` 会自动识别账号并用 `-q <QQ号>` 快捷登录，不用再扫码。

二选一：

**方式 A：浏览器密码登录（最快，推荐）**

1. 手机浏览器打开 `http://127.0.0.1:6099/webui?token=<WebUI token>`
2. 点「密码登录」→ 填小号 QQ 号 + 密码 → 登录（可能要过一下滑块 / 短信验证）
3. 登录成功后，OneBot 的 3001 端口会自动监听，机器人会在 30 秒内自动连上

**方式 B：扫码（适合用另一台设备扫）**

```bash
bash protocol/qr.sh --refresh     # 重启并生成新二维码，导出到相册 + 下载目录
```

然后用 QQ「扫一扫」→ 右下角「相册」→ 选导出的二维码 → 确认登录。二维码约 1–2 分钟过期，过期就再执行一次命令。

登录后验证：

```bash
bash protocol/status.sh    # 看到「登录账号: xxxxx」和「OneBot WS 3001: OPEN」
bash scripts/bot-status.sh # 看到机器人「OneBot 端口 3001：可连接」
tail -5 logs/bot.out       # 看到「已连接 OneBot: ws://127.0.0.1:3001」
```

> 万一以后掉登录：`bash protocol/start.sh` 会自动尝试快捷登录；失败就再扫码/密码登一次。

## 六、排错

| 现象 | 处理 |
|---|---|
| 机器人日志一直 `连接 ws://127.0.0.1:3001 失败` | 协议层没起来，跑 `./protocol/status.sh` |
| QQ 掉线/要重新扫码 | `./protocol/stop.sh && ./protocol/start.sh`，看 findings.md 里的扫码方式 |
| AI 不回 | config 里 `api_key` 没填 / base_url 写错 / 余额或额度问题，看 `logs/bot.out` / `bot/logs/bot.log` |
| 改了关键词没生效 | 保存后等 2 秒；还没生效看是不是 JSON 写错了（日志会提示热加载失败，旧配置继续用） |
| 手机内存不够 | 停掉协议层：`./protocol/stop.sh`；机器人本体只占几十 MB |

离线自测（不碰真 QQ，验证插件逻辑）：

```bash
python3 tools/selftest.py
```


## 七、手机 App「机器人控制台」

一个 17KB 的小 APK，本质是全屏 WebView 壳，加载手机本机的管理台 `http://127.0.0.1:8080/`
（页面由 `bot/admin/server.py` 提供，改页面不用重装 APK）。

**能做什么**

| 页面 | 功能 |
|---|---|
| 概览 | 机器人运行状态 / PID / QQ 号 / OneBot 连接 / 协议层状态 / 运行时长；启动、停止、重启 |
| AI | API Key（留空不改）、接口地址、模型、温度、max_tokens、人格提示词、私聊/群 @ 开关、上下文轮数与过期时间 |
| 回复规则 | 关键词/正则规则增删改、群里必须 @ 的开关 |
| 群管理 | 入群欢迎、违禁词、防刷屏的开关和参数 |
| 上下文 | 查看每个会话的 AI 对话记录、一键清空 |
| 日志 | 最近 300 行运行日志 |

**安装**：APK 在 [apk/机器人控制台.apk](apk/机器人控制台.apk)，复制到手机后用文件管理器点开安装（允许「安装未知应用」即可）。

**管理服务**：`./start-all.sh` 会顺带启动；也可以单独：

```bash
bash scripts/admin-start.sh    # 启动（127.0.0.1:8080）
bash scripts/admin-stop.sh     # 停止
curl http://127.0.0.1:8080/api/state   # 看看状态
```

没有 APK 也能用：手机浏览器打开 `http://127.0.0.1:8080/` 就是同一个页面。

**改 App / 重新打包**：源码在 `apk/`（AndroidManifest.xml、MainActivity.java、res/、assets/），
`bash apk/build.sh` 会用手机上的 aapt2 + javac + d8 + apksigner 直接打出签名 APK（不需要 Gradle/SDK Manager）。


## 八、服务控制（App 里开/关）与自愈

### App 能做什么

底部「概览」页的**服务控制**卡片：

| 控件 | 作用 |
|---|---|
| ▶ 开启服务 / ■ 关闭服务 | **只启停机器人**，QQ 协议层保持在线 → 秒开秒关、不用重新扫码；彻底关闭用下面的「协议层→停止」或「彻底关闭」 |

实测（已登录状态）：关闭服务 0.5 秒、开启服务 5 秒（含等机器人连上 OneBot），期间 WS 3001 始终在线。
| 协议层 启动/停止/重启 | 只动 QQ + NapCat（启动后会自动等 OneBot WS 就绪） |
| 机器人 启动/停止/重启 | 只动机器人进程 |
| 自动守护 两个开关 | 进程挂了自动拉起（手动「关闭服务」后不会被拉起） |
| 需要登录时 | 页面检测到未登录会**自动打开 NapCat 登录页**（5 分钟内只自动开一次；不想被打扰可点「不再自动打开」，之后手动点「去登录」） |

底部 Tab 还有「AI / 回复规则 / 群管理 / 画图 / 上下文 / 日志」，都能在手机上直接改。

对应接口（`127.0.0.1:8080`）：

```bash
curl -X POST http://127.0.0.1:8080/api/bot/action \
  -H 'Content-Type: application/json' \
  -d '{"action":"all_start"}'
# action 可选：all_start|all_stop|all_restart（只动机器人，协议层保持）
#            all_off（彻底关闭：连 QQ 一起停，下次开启要重新登录验证）
#            protocol_start|protocol_stop|protocol_restart
#            bot_start|bot_stop|bot_restart（旧 start/stop/restart 仍兼容=机器人）
```

### 环境被 App 更新重置了怎么办

手机 App 更新可能把 `/opt`、`/usr` 里的东西重置（QQ、NapCat、系统依赖、pip 包都没了），
但 `/root` 会保留（代码、配置、QQ 登录态、安装包缓存）。这时：

- `protocol/start.sh`、`scripts/bot-start.sh`、App 的「开启服务」都会**自动检测并修复**；
- 也可以手动跑：`bash protocol/setup.sh`（幂等，能从 `protocol/downloads/` 缓存装回，不用重新下载）。

NapCat 现在装在 `/root/napcat`（持久），`/opt/napcat` 只是软链接，所以配置不会再丢。

### 日志与磁盘（自动清理）

QQ 在 proot 里会持续刷 GPU/dbus 错误，日志可能暴涨（出现过 41GB）。现在有两层自动清理：

| 对象 | 规则 |
|---|---|
| `logs/bot.out` | 超 20MB 截断保留 2MB（启动时 + 每 10 分钟检查） |
| `logs/admin.out` | 超 10MB 截断保留 1MB |
| `protocol/logs/napcat.log` | 超 20MB 截断保留 2MB |
| `protocol/logs/qq-stderr.log` | 超 20MB 截断保留 1MB |
| `protocol/logs/setup.log` | 超 5MB 截断保留 1MB |
| 旧日志（`*.old` 等） | 超过 3 天删除 |
| 画图产物 `bot/data/images/` | 只保留最近 100 张 |
| QQ 崩溃转储 `*.dmp` | 超 50MB 删除 |

- **管理服务内置清理线程**：启动 20 秒后首跑，之后每 10 分钟一次（不依赖协议层是否在跑）；
- `protocol/log-guard.sh` 在协议层运行期间每 2 分钟再兜底一次（含 `bot.out`/`admin.out`）；
- 机器人自身的 `bot/logs/bot.log` 用 `RotatingFileHandler`（2MB × 3）；
- App「日志」页有 **「清理日志」** 按钮，并显示磁盘剩余；实测 32MB 日志一键清理到 2MB；
- QQ 启动带 `--disable-gpu`，从源头减少错误刷屏。

### 登录态

- 登录成功一次后，`start.sh` 会用 `-q <QQ号>` 快捷登录，**不需要每次扫码**；
- 如果出现「新设备需要扫码验证」，用扫码方式登录（QQ 扫一扫 → 相册选二维码截图）通常能过；
- 已知限制：环境被重置过（设备指纹变化）时，QQ 的「快速登录」可能报 **“登录需要手Q验证”**，这是 QQ 侧风控，服务端无法绕过。
  所以**不要频繁彻底关闭协议层**：用 App 的「关闭服务」（只停机器人）就不会触发这个问题；
- 未登录时 App 概览会显示「协议层未就绪」+「去登录」按钮，WS 3001 不会打开（这是正常的），
  登录成功后 3001 自动监听、机器人自动连上，无需重启任何东西。

## 九、多套 AI 接口（profiles）与切换

### 结构

`plugins.ai_chat` 里可以保存**多套接口**，字段放在 `profiles` 数组里，`active_profile` 指定当前生效的那套：

```json
"ai_chat": {
  "active_profile": "grok",
  "profiles": [
    {"id":"deepseek","name":"DeepSeek 官方","provider":"openai",
     "base_url":"https://api.deepseek.com/v1","api_key":"sk-…",
     "model":"deepseek-chat","temperature":0.7,"max_tokens":4096},
    {"id":"grok","name":"xAI Grok","provider":"openai",
     "base_url":"https://api.x.ai/v1","api_key":"xai-…",
     "model":"grok-4","temperature":0.7,"max_tokens":4096}
  ]
}
```

生效规则：当前 profile 里的 `provider/base_url/api_key/auth/model/temperature/max_tokens` 覆盖顶层同名字段；
其它设置（人格提示词、触发条件、搜索、识图、发文件）全局生效，不随 profile 变。

`auth` 是**认证方式**：空串 = 用 API Key；填 `chatgpt` / `claude` / `gemini` / `deepseek` 就用下面「AI 账号」里连接的账号额度，此时 `base_url`、`api_key` 不生效，`model` 留空用该账号默认模型。

### 怎么切换

- **群里/私聊**：管理员发 `/model` 看列表，`/model 2` 或 `/model Grok` 切换（立即生效）
- **控制台 App**：AI 页顶部「接口配置」选择 →「设为当前」，也能新增/删除/编辑多套
- **配置文件**：改 `active_profile`，2 秒内热加载

### 内置服务商预设（App 里「+ 从预设添加」）

| 服务商 | provider | base_url | 模型名示例 |
|---|---|---|---|
| DeepSeek | openai | `https://api.deepseek.com/v1` | deepseek-chat / deepseek-reasoner |
| OpenAI / ChatGPT | openai | `https://api.openai.com/v1` | gpt-4o / gpt-4o-mini |
| **xAI Grok** | openai | `https://api.x.ai/v1` | grok-4 / grok-3 / grok-2-latest |
| **Gemini（原生）** | gemini | `https://generativelanguage.googleapis.com` | gemini-2.5-flash / gemini-2.5-pro |
| **Gemini（OpenAI 兼容）** | openai | `https://generativelanguage.googleapis.com/v1beta/openai` | gemini-2.5-flash |
| Anthropic Claude | claude | `https://api.anthropic.com` | claude-sonnet-4-5 等 |
| 中转站 / 自定义 | openai | 中转站给的 `/v1` 地址 | 它支持的模型名 |

要点：
- **Grok**：xAI 接口是 OpenAI 兼容的，`provider` 填 `openai`（写 `grok`/`xai` 会自动归一化），
  base_url 用 `https://api.x.ai/v1`，key 用 xAI 控制台的 `xai-…`；模型名以 [xAI Models 文档](https://docs.x.ai/developers/models) 为准，可在 App 里随时改。
- **Gemini**：两种接法 —— 原生（`provider: gemini`，`:generateContent`，支持识图）或 OpenAI 兼容（`provider: openai`，base 用 `…/v1beta/openai`）。
- **中转站**：多数是 OpenAI 兼容，选「中转站 / 自定义」填 `/v1` 地址；Claude 原生协议的中转把 `provider` 选 `claude`。
- 每套的 Key 在控制台都是掩码显示，只改其它字段不会丢 Key（后端自动保留）。

## 十、AI 账号（订阅登录，不用 API Key）

控制台 App 的「AI」页和网页控制台都能连四种账号：**ChatGPT、Claude、Gemini、DeepSeek**。
登录在浏览器里完成，机器人用账号额度对话，不占用 API Key。

### 怎么连

1. App 打开 **AI** 页 → 顶部 **AI 账号** → 点要连的服务。
2. 点 **开始登录**，按提示在浏览器里完成授权（有的服务会显示一次性代码，复制到网页里）。
3. 回到 App，状态变成「已连接」就成功了。随时可以**退出登录**。

网页控制台（`http://127.0.0.1:8080/`）的 AI 页也有同样的「AI 账号」卡片。

### 怎么用

在 **接口设置 → 认证方式** 里选对应账号；也可以直接改 `bot/config.json` 的 profile：

```json
{"id":"claude-acc","name":"Claude 账号","provider":"claude","auth":"claude","model":""}
```

- `auth` 为空 = 用 API Key；非空 = 用 `chatgpt`/`claude`/`gemini`/`deepseek` 账号。
- 账号模式下 `base_url`/`api_key` 不生效；`model` 留空就用该账号的默认模型。
- 识图 / 画图仍然走 API Key 配置。

App 的「接口设置」里：
- **认证方式（API Key / 账号）** 排在最前面，选账号时行下会显示这个账号连没连上。
- **模型** 点开可以「获取模型列表」直接选，不用手输；列表拿不到或要自定义时，点「手动输入」。
- 网页控制台的模型行旁也有「获取模型」按钮。

### 注意

- 这是用**订阅账号额度**，不是官方 API；高频或多人使用可能触发上游风控，**风险自担**，建议用小号。
- 登录令牌只保存在手机 `bot/data/accounts.json`（权限 600），不会上传到任何地方。
- Gemini 需要账号地区支持 Gemini Code Assist；DeepSeek 走 `platform.deepseek.com` 授权。
- 浏览器和机器人要在同一台设备上（回调地址是 `127.0.0.1`）；用另一台电脑打开网页控制台时，选「粘贴网址 / 验证码」的方式完成登录。

## 十一、自动更新

### App 自身

- 更多页 → **更新** → **检查更新**：和 GitHub Releases 比较版本，有新版提示「立即更新」。
- 下载后校验 SHA256，通过后调起系统安装；没有「安装未知应用」权限时会先跳到系统设置。
- **自动检查更新**默认开启，每天最多一次；跳过的版本不再提示。

### 机器人代码

- 更多页 → **更新** → **更新机器人代码**：从 GitHub `main` 拉最新代码，更新 `bot/`、`scripts/`、`start-all.sh`，
  保留 `config.json`、`data/`、`logs/`。
- 更新完成后要**重启服务**才生效（页面会提示，并提供重启入口）。
- 更新记录写在 `bot/.update-meta.json`；首次更新之后「已是最新」的判断才准确。

### 命令行

```bash
curl -s http://127.0.0.1:8080/api/update/bot          # 看有没有更新
curl -s -X POST http://127.0.0.1:8080/api/update/bot  # 开始更新
curl -s http://127.0.0.1:8080/api/update/bot/status   # 看进度
```

## 十二、注意

- 自建 QQ 机器人属于**第三方协议**，腾讯官方不支持，**有被风控/封号的风险**，务必用不常用的小号。
- 机器人账号不要频繁加群、发广告、改设备，容易触发风控。
- 手机跑协议层耗电、发热比平时高，在意的话可以把 `protocol` 换成电脑/服务器部署，机器人本体的配置完全通用。

## App 里的「记录」页

- **群内消息**：选群 + 选条数（20/50/100/200/300），点「读取消息」查看最近消息（时间、昵称、内容，图片/文件/引用都有标记）
- **机器人写的文件**：AI 写代码存下来的文件列表，点开看源码（HTML/SVG/Python 等），单文件上限 2MB
- **AI 上下文**：每个群/私聊的记忆，点开逐条看完整对话，可一键清空

对应接口（管理台 8080）：`/api/groups`、`/api/messages?group=&count=`、`/api/files`、`/api/files/get?name=`。

## 改代码不用重打运行环境

独立 App 启动时会检查手机存储里的 `Download/DSH-Go/qqbot-src/`：
如果存在，就把里面的 `bot/`、`scripts/`、`*.sh` 同步进 App 内的运行环境再启动。

```bash
# 在开发机/手机 DSH 里更新代码后
rsync -a --exclude data --exclude logs bot/ /sdcard/Download/DSH-Go/qqbot-src/bot/
cp scripts/*.sh start-all.sh /sdcard/Download/DSH-Go/qqbot-src/   # 按需
```
然后在 App 里「彻底关闭」→「开启服务」即可生效（不用重装 900MB 的 rootfs）。

## 许可证

[MIT](LICENSE) © 2026 DeepShawn
