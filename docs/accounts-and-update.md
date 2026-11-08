# AI 账号连接 + 自动更新 · 设计与接口约定

> 本文件是本次改动的唯一接口约定。各模块按这里的 API/文件边界并行实现，不改别人的文件。

## 1. 目标

1. **AI 账号**：控制台 App 和网页控制台的 AI 页新增「AI 账号」，支持用浏览器 OAuth 登录
   ChatGPT、Claude、Gemini、DeepSeek 四种账号，登录后用**账号额度**聊天（不用填 API Key）。
2. **自动更新**：
   - App 自身：检查 GitHub Releases（DeepShawn/QQBot），下载 APK、校验 SHA256、调起安装；支持自动检查开关。
   - 机器人代码：从 GitHub main 分支下载源码包，更新 `bot/`、`scripts/`、`start-all.sh`，保留配置/数据/日志。

## 2. 文件分工（写作用域，禁止越界）

| 负责 | 文件 |
|---|---|
| T1 | `bot/account_core.py`、`bot/accounts_claude.py`、`bot/accounts_chatgpt.py`、`bot/account_chat.py`、`bot/providers.py`、`bot/plugins/ai_chat.py`、`bot/config.py`、`bot/tests/*`（自己那部分） |
| T2 | `bot/accounts_gemini.py`、`bot/accounts_deepseek.py`、`bot/tests/test_accounts_gemini.py`、`bot/tests/test_accounts_deepseek.py` |
| T3 | `apk-miuix/**`（Kotlin/Compose、AndroidManifest、res、build.gradle） |
| T4 | `bot/admin/server.py`、`bot/updater.py`、`bot/admin/web/index.html` |
| Lead | 本文件、`README.md`、`bot/config.example.json`、集成/构建/提交 |

共享改动一律先在本文件更新，再在对应负责人的文件里实现；不要 git commit / push。

## 3. 账号数据

`bot/data/accounts.json`（目录已存在，且 `.gitignore` 已忽略 `bot/data/`），权限 0600：

```json
{
  "version": 1,
  "accounts": {
    "chatgpt": {
      "kind": "oauth",
      "access": "…",
      "refresh": "…",
      "expires_at": 1794000000,
      "account": "user@example.com",
      "extra": {"chatgpt_account_id": "…"},
      "updated_at": 1794000000
    }
  }
}
```

- `expires_at` 秒级 Unix 时间；`refresh` 可为空串。
- 日志、错误信息、接口返回里都**不能出现 token**；用 `redact()` 处理。
- 写文件用临时文件 + `os.replace`，避免并发写坏。

## 4. 账号核心 `bot/account_core.py`（T1）

```python
PROVIDER_IDS = ("chatgpt", "claude", "gemini", "deepseek")
PROVIDER_NAMES = {"chatgpt": "ChatGPT", "claude": "Claude", "gemini": "Gemini", "deepseek": "DeepSeek"}

class AccountError(Exception):
    """code 是稳定错误码（见下），message 是给用户看的中文短句。"""
    def __init__(self, code: str, message: str): ...

ERRORS = {
    "not-signed-in": "还没有连接这个账号",
    "expired": "登录已过期，重新连接一次",
    "network": "网络连不上，检查网络后再试",
    "denied": "授权被拒绝了，再试一次",
    "timeout": "登录超时了，再试一次",
    "busy": "上一次登录还没结束，稍后再试",
    "unsupported": "这个版本还不支持这种账号",
    "generic": "授权没有完成，再试一次",
}
```

### 4.1 Provider 模块约定（`bot/accounts_<id>.py`）

```python
ID = "chatgpt"            # 必须等于 provider id
NAME = "ChatGPT"          # 界面显示名
MODELS = ["…"]            # 模型 id 列表（字符串）
DEFAULT_MODEL = MODELS[0]

def begin(attempt) -> dict:
    """在当前线程里阻塞完成一次 OAuth，返回要保存的 record（见第 3 节）。

    用 attempt 报告进度：
      attempt.set_phase("starting", "正在准备登录…")
      attempt.notify_url(url, "在浏览器里完成登录")          # phase=open
      attempt.notify_device(url, "ABCD-1234", "在打开的网页里输入下面的代码")  # phase=device
      value = attempt.prompt("粘贴地址栏里的完整网址")       # phase=prompt，阻塞到用户提交/取消
      attempt.check_cancelled()                            # 取消时抛 AttemptCancelled
    失败抛 AccountError；成功返回 record。
    """

def refresh(record) -> dict | None:
    """用 refresh token 换新 access；成功返回新 record，失败返回 None。"""

def chat(record, model, messages, temperature, max_tokens, timeout) -> str:
    """messages 是 OpenAI 格式 [{role,content}]（content 为 str）。返回纯文本回复。"""

def label(record) -> str | None:
    """账号邮箱/套餐名，用于界面展示；拿不到返回 None。"""
```

### 4.2 核心 API

```python
def list_view() -> list[dict]
# [{"id":"chatgpt","name":"ChatGPT","signedIn":True,"account":"a@b.com",
#   "models":["…"],"default_model":"…","error":None}]
# 模块缺失时仍返回一项，signedIn=False，error="unsupported"

def begin(provider: str) -> dict      # {"attempt": <attempt view>}
def poll(attempt_id: str) -> dict     # {"attempt": <attempt view>}
def answer(attempt_id: str, value: str) -> dict
def cancel(attempt_id: str) -> dict
def signout(provider: str) -> dict    # 删除记录
def ensure_access(provider: str) -> dict   # 读记录；过期先 refresh(record) 再保存；无记录抛 AccountError
def dispose()                          # 管理台退出时取消所有在途登录
```

- `begin` 立刻返回，内部起 daemon 线程跑 `provider.begin`；同一 provider 同时只跑一个，
  再次 `begin` 先取消旧的。
- attempt 视图（`poll` 返回）：
  - `{"id","provider","phase","message"}`
  - phase ∈ `starting|open|device|prompt|done|failed|cancelled`
  - open：加 `url`；device：加 `url` + `userCode`；prompt：加 `placeholder`
  - done：加 `account`；failed：加 `error`
- attempt 5 分钟没结果自动取消（failed + timeout 文案）；结束后保留 10 分钟供迟到轮询读取。
- 回调服务器：`CallbackServer(port=0, path="/callback")`，绑定 `127.0.0.1`，起线程收一次请求后返回
  `query dict`；提供 `redirect_uri`；支持 `wait(timeout, attempt)` 与 `close()`。
  端口被占时抛 `AccountError("generic", …)`，由 provider 改为 prompt 模式（让用户粘贴完整网址）。
- 安全：callback 页只返回「登录完成，可以关掉这个页面了」；只接受一次回调；URL 只允许 https（回调除外）。

### 4.3 聊天分发 `bot/account_chat.py`（T1）

```python
def chat(provider, model, messages, temperature, max_tokens, timeout) -> str
```
内部 `ensure_access(provider)`，动态 `import accounts_<provider>`，调用其 `chat`；把 `AccountError`
与网络异常转成 `providers.ProviderError`（延迟导入避免环）。

### 4.4 `bot/providers.py` 与 `bot/plugins/ai_chat.py`（T1）

- `chat(..., auth="")`：`auth` 是账号 id（chatgpt/claude/gemini/deepseek）时走 `account_chat.chat`，
  忽略 `base_url`/`api_key`；否则保持现有逻辑。`describe_images` 不变。
- `ai_chat.py` 的 `PROFILE_FIELDS` 加 `"auth"`；`_profile_settings()` 返回 auth；调用处传 `auth=`。
- `config.py` 的 ai_chat 默认加 `"auth": ""`；profiles 默认仍为空。
- 账号模式下的模型：profile 的 `model` 非空就用它，空就用该账号 `DEFAULT_MODEL`。

## 5. Admin HTTP API（T4）

沿用现有 `server.py` 风格（`self._json` / `self._error`），全部到 127.0.0.1:8080。
`server.py` 需要 `sys.path` 加入 `bot/` 后 `import account_core`、`import updater`。
**账号登录在管理台进程里跑**（管理台常驻，机器人进程可能被关）。

```
GET  /api/accounts
→ {"ok":true,"accounts":[{"id","name","signedIn","account","models","default_model","error"}…]}

POST /api/accounts/begin    {"provider":"chatgpt"}
→ {"ok":true,"attempt":{…视图…}}
GET  /api/accounts/poll?attempt=<id>
→ {"ok":true,"attempt":{…视图…}}
POST /api/accounts/answer   {"attempt":"<id>","value":"…"}
→ {"ok":true}
POST /api/accounts/cancel   {"attempt":"<id>"}
→ {"ok":true}
POST /api/accounts/signout  {"provider":"claude"}
→ {"ok":true}
```

失败统一 `{"ok":false,"error":"中文短句"}`（HTTP 200 也可，和现有接口保持一致）。

```
GET  /api/update/bot
→ {"ok":true,"current":{"commit":"abc1234"},"latest":{"commit":"def5678","title":"…","url":"…"},"behind":true}

POST /api/update/bot
→ {"ok":true,"started":true}

GET  /api/update/bot/status
→ {"ok":true,"phase":"idle|checking|downloading|extracting|syncing|done|failed",
   "message":"…","error":null,"restart_required":true}
```

## 6. 机器人代码更新 `bot/updater.py`（T4）

- 仓库 `https://github.com/DeepShawn/QQBot`，分支 `main`。
- `check() -> dict`：GitHub API `/repos/DeepShawn/QQBot/commits/main` 取最新 commit；
  本地 `.update-meta.json`（写在 `bot/` 下，需加入 .gitignore）记已安装 commit；
  没有本地记录时 behind=true（首次更新后写入记录）。
- `update()`（后台线程，带状态机）：
  1. 下载 `https://github.com/DeepShawn/QQBot/archive/refs/heads/main.tar.gz` 到临时目录；
  2. 安全解压（拒绝 `..`、绝对路径、符号链接越界）；
  3. 同步 `bot/`（排除 `config.json`、`data/`、`logs/`、`__pycache__`、`*.pyc`、`.update-*`）、
     `scripts/`、`start-all.sh`；保留用户文件；
  4. 写入 `.update-meta.json`（commit、时间），状态 done，`restart_required=true`。
- 失败要给中文原因（网络、解压、写入权限），不要抛异常到接口外。
- 不自动重启；界面提示「重启服务生效」。

## 7. Android App（T3）

### 7.1 AI 账号（AI 页）

- AI 页顶部加「AI 账号」分组（在「当前接口」之前）：四行 ChatGPT / Claude / Gemini / DeepSeek。
  每行 summary：已连接 → 账号邮箱/名称；未连接 → 「未连接」。
- 点一行打开账号面板（Dialog）：
  - 未连接：「开始登录」→ `POST /api/accounts/begin`；然后每 1.2s `GET /api/accounts/poll?attempt=`：
    - `open`：显示「在浏览器里完成登录」+「打开浏览器」按钮（`Intent.ACTION_VIEW`）；可取消；
    - `device`：显示网址 + 一次性代码 +「打开浏览器」+ 复制代码；
    - `prompt`：输入框（粘贴完整网址/授权码）+ 提交（`/api/accounts/answer`）；
    - `done`：刷新账号列表，提示已连接；
    - `failed`：显示 error，可重试。
  - 已连接：显示账号、模型列表（只读）、「退出登录」（确认后 `POST /api/accounts/signout`）。
- 接口设置里加「认证方式」下拉：`API Key`（auth=""）/ 四个账号。选账号时 API Key 行显示「已改用账号登录」；
  协议/地址/Key 字段保留不删。写 `profiles[i].auth`。
- 用了账号但对应账号未连接时，在该行下方显示提示。

### 7.2 自动更新（更多页 + 启动检查）

- `Update.kt`（新建）：
  - `checkApp(): UpdateInfo?`：`GET https://api.github.com/repos/DeepShawn/QQBot/releases/latest`，
    读 `tag_name`（形如 `v3.3`）；和 `BuildConfig.VERSION_NAME` 比较（去掉 `v`，按数字段比较）；
    资产找 `QQBot-Console-*.apk` 与 `SHA256SUMS-*.txt`；返回版本、更新说明（release body）、下载地址、校验地址。
  - `downloadAndInstall(ctx, info)`：下载到 `ctx.cacheDir`（进度回调），按 SHA256SUMS 校验，
    通过 FileProvider 发 `ACTION_VIEW` + `application/vnd.android.package-archive` 安装；
    无「安装未知应用」权限时跳 `Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES`。
  - 自动检查：SharedPreferences 存开关（默认 true）和上次检查时间；每天最多一次；App 启动时检查，
    有新版弹窗（更新说明 + 立即更新 / 稍后 / 跳过这个版本）。
- 更多页加「更新」分组：
  - 「检查更新」行：显示当前版本；点击检查，有新版弹「立即更新」；
  - 「自动检查更新」开关；
  - 「更新机器人代码」行：`GET /api/update/bot` 显示有无更新；点击 `POST /api/update/bot` 并轮询
    `/api/update/bot/status`，完成后提示「已更新，重启服务生效」，并提供「重启服务」（复用现有重启入口）。
- Manifest：`<uses-permission android:name="android.permission.REQUEST_INSTALL_PACKAGES"/>`；
  `<provider android:name="androidx.core.content.FileProvider" android:authorities="${applicationId}.files" ...>`
  + `res/xml/file_paths.xml`。
- `build.gradle`：versionCode 6，versionName "3.3"。
- 构建：`cd apk-miuix && gradle :app:assembleDebug`（gradle 8.13 在 `/root/build/gradle-8.13/bin/gradle`，
  JDK 17，aapt2 覆盖在 ~/.gradle/gradle.properties）。构建成功并记录产物路径。

## 8. 网页控制台（T4）

- AI 视图加「AI 账号」卡：四行状态 + 「连接」/「退出」；登录弹层按 poll 的 phase 展示
  （open/device 用 `window.open(url)`，prompt 用输入框提交，done 刷新）。
- 概览或日志视图加「机器人代码更新」卡：检查、更新、状态轮询，完成后提示重启生效。
- 保持单文件、无外部依赖；风格跟随现有变量与组件。

## 9. 文案与安全

- 全中文短句，错误像人话；不出现英文异常、堆栈、token。
- 订阅登录有账号风控风险，README 要写清楚「自担风险」。
- 不要在日志里打印 OAuth 的 authorization URL（含 PKCE state）；只打印 host。

## 10. 参考实现（可直接读，不要 import）

- ChatGPT：`/opt/dsh/node_modules/@earendil-works/pi-ai/dist/auth/oauth/openai-codex.js`
  （client_id=`app_EMoamEEZ73f0CkXaXp7hrann`，authorize/token、device code、scope）
- Claude：`/opt/dsh/node_modules/@earendil-works/pi-ai/dist/auth/oauth/anthropic.js`
  （client_id=`9d1c250a-e61b-44d9-88ed-5944d1962f5e`，`https://claude.ai/oauth/authorize`，
  token `https://platform.claude.com/v1/oauth/token`，回调端口 53692）
- 公共：`…/auth/oauth/pkce.js`、`callback-server.js`、`device-code.js`
- 聊天调用：`…/dist/api/anthropic-messages.js`（Claude 的 OAuth 头/刷新）、
  `…/dist/api/openai-codex-responses.js`（ChatGPT 的 responses SSE）、`…/dist/models.generated.js`（模型 id）
- Gemini：`/root/refs/gemini-cli-core/package/dist/src/code_assist/oauth2.js`
  （Google OAuth client id/secret、回调 `http://localhost:8085/oauth2callback`、
  Code Assist `loadCodeAssist`/`onboardUser`/`streamGenerateContent`）
- DeepSeek：`/opt/dsh/node_modules/@deepseek-ai/dsh-deepseek-account-platform/lib/index.js`
  （浏览器授权协议、`x-dsh-auth-token` 等）、
  `/opt/dsh/node_modules/@deepseek-ai/dsh-llm-deepseek-account/lib/index.js`（模型列表/推理调用）

## 11. 验收

- 所有改动的 Python 文件 `python3 -m py_compile` 通过。
- `python3 -m unittest discover -s bot/tests -v` 通过（无网络，用假 HTTP/假记录）。
- `apk-miuix` 构建成功。
- README 更新（Lead）。
