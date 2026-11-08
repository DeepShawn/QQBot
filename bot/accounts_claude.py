"""Claude（Anthropic）账号 OAuth 登录与聊天。

按 pi-ai 的 auth/oauth/anthropic.js 移植（只参考源码，不 import）：
- 浏览器授权：https://claude.ai/oauth/authorize
- 换取/刷新 token：https://platform.claude.com/v1/oauth/token
- 回调：127.0.0.1:53692/callback（端口被占用时降级为粘贴网址）
- 聊天：api.anthropic.com/v1/messages，用 Bearer + Claude Code 身份头

只依赖 Python 标准库。
"""
from __future__ import annotations

import time
import urllib.parse

import account_core

ID = "claude"
NAME = "Claude"

# 模型 id 来自 pi-ai 的 anthropic 模型目录（providers/data/anthropic.json）。
MODELS = [
    "claude-sonnet-5-5",
    "claude-opus-5-5",
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-opus-4-8",
    "claude-sonnet-4-6",
    "claude-haiku-5-5",
]
DEFAULT_MODEL = MODELS[0]

CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CALLBACK_HOST = "127.0.0.1"
CALLBACK_PORT = 53692
CALLBACK_PATH = "/callback"
REDIRECT_URI = f"http://localhost:{CALLBACK_PORT}{CALLBACK_PATH}"
SCOPES = (
    "org:create_api_key user:profile user:inference "
    "user:sessions:claude_code user:mcp_servers user:file_upload"
)

API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
CLAUDE_CODE_BETA = "claude-code-20250219,oauth-2025-04-20"
# OAuth 走 Claude Code 订阅通道，必须带这张身份牌；pi-ai 里 user-agent 是 claude-cli/<version>
CLAUDE_CODE_SYSTEM = "You are Claude Code, Anthropic's official CLI for Claude."
USER_AGENT = "claude-cli/2.1.280"

TOKEN_TIMEOUT = 30.0
CHAT_TIMEOUT = 60.0
# 与 pi-ai 一致：提前 5 分钟视为过期（这里再交给 account_core 提前 60 秒刷新）
EXPIRES_MARGIN_SECONDS = 300


def _record_from_token(data: dict, old_refresh: str = "") -> dict:
    access = str(data.get("access_token") or "").strip()
    if not access:
        raise account_core.AccountError("generic", "登录服务没有返回有效结果，再试一次")
    refresh = str(data.get("refresh_token") or "").strip() or str(old_refresh or "")
    try:
        expires_in = int(data.get("expires_in") or 0)
    except (TypeError, ValueError):
        expires_in = 0
    if expires_in <= 0:
        expires_in = 3600
    return {
        "kind": "oauth",
        "access": access,
        "refresh": refresh,
        "expires_at": int(time.time()) + expires_in - EXPIRES_MARGIN_SECONDS,
        "account": "",
        "extra": {},
    }


def begin(attempt) -> dict:
    """跑一次 Claude 浏览器 OAuth；成功返回账号记录。"""
    verifier, challenge = account_core.generate_pkce()
    state = verifier  # pi-ai 的 Claude 流程用 verifier 兼作 state

    server = None
    try:
        try:
            server = account_core.CallbackServer(
                CALLBACK_PORT, CALLBACK_PATH, host=CALLBACK_HOST
            )
        except account_core.AccountError:
            server = None
        redirect_uri = server.redirect_uri if server is not None else REDIRECT_URI

        params = {
            "code": "true",
            "client_id": CLIENT_ID,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": SCOPES,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
        auth_url = AUTHORIZE_URL + "?" + urllib.parse.urlencode(
            params, quote_via=urllib.parse.quote
        )

        attempt.set_phase("starting", "正在准备登录…")
        if server is not None:
            attempt.notify_url(auth_url, "在浏览器里完成登录")
            query = server.wait(attempt.remaining(), attempt)
            if query.get("error"):
                raise account_core.AccountError("denied")
            account_core.verify_oauth_state(query.get("state"), state)
            code = str(query.get("code") or "").strip()
            if not code:
                raise account_core.AccountError("generic", "没有拿到授权码，再试一次")
        else:
            attempt.notify_url(
                auth_url, "在浏览器里完成登录，然后回到这里粘贴地址"
            )
            pasted = attempt.prompt(
                REDIRECT_URI,
                "把浏览器地址栏里的完整网址粘贴到这里（或 code#state）",
            )
            parsed = account_core.parse_oauth_input(pasted)
            account_core.verify_oauth_state(parsed.get("state"), state)
            code = str(parsed.get("code") or "").strip()
            if not code:
                raise account_core.AccountError("generic", "没有拿到授权码，再试一次")

        attempt.set_phase("starting", "正在完成登录…")
        data = account_core.http_json(
            TOKEN_URL,
            {
                "grant_type": "authorization_code",
                "client_id": CLIENT_ID,
                "code": code,
                "state": state,
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
            },
            timeout=TOKEN_TIMEOUT,
            label="登录服务",
            secrets_=(code, verifier),
        )
        record = _record_from_token(data)
        return record
    finally:
        if server is not None:
            server.close()


def refresh(record: dict) -> dict | None:
    """用 refresh token 换新 access；失败返回 None（网络错误抛 AccountError）。"""
    refresh_token = str(record.get("refresh") or "").strip()
    if not refresh_token:
        return None
    try:
        data = account_core.http_json(
            TOKEN_URL,
            {
                "grant_type": "refresh_token",
                "client_id": CLIENT_ID,
                "refresh_token": refresh_token,
            },
            timeout=TOKEN_TIMEOUT,
            label="登录服务",
            secrets_=(refresh_token,),
        )
    except account_core.AccountError as e:
        if e.code in ("network", "timeout"):
            raise
        return None
    try:
        return _record_from_token(data, old_refresh=refresh_token)
    except account_core.AccountError:
        return None


def _headers(access: str) -> dict:
    return {
        "Authorization": f"Bearer {access}",
        "anthropic-version": ANTHROPIC_VERSION,
        "anthropic-beta": CLAUDE_CODE_BETA,
        "accept": "application/json",
        "anthropic-dangerous-direct-browser-access": "true",
        "content-type": "application/json",
        "user-agent": USER_AGENT,
        "x-app": "cli",
    }


def chat(
    record: dict,
    model: str,
    messages: list,
    temperature: float,
    max_tokens: int,
    timeout: float,
) -> str:
    access = str(record.get("access") or "").strip()
    if not access:
        raise account_core.AccountError("expired")
    model_name = str(model or "").strip() or DEFAULT_MODEL

    system_parts: list[str] = []
    conversation: list[dict] = []
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        content = str(message.get("content") or "")
        if role == "system":
            if content.strip():
                system_parts.append(content)
            continue
        if role not in ("user", "assistant"):
            continue
        conversation.append(
            {"role": role, "content": [{"type": "text", "text": content}]}
        )
    if not conversation:
        raise account_core.AccountError("generic", "没有可发送的消息")

    try:
        max_out = max(1, int(max_tokens or 600))
    except (TypeError, ValueError):
        max_out = 600
    _ = temperature  # 订阅通道新模型不接受 temperature，统一不发

    payload = {
        "model": model_name,
        "max_tokens": max_out,
        "messages": conversation,
        "system": [{"type": "text", "text": CLAUDE_CODE_SYSTEM}],
    }
    if system_parts:
        payload["system"].append({"type": "text", "text": "\n\n".join(system_parts)})
    error_map = {
        401: ("expired", None),
        403: ("expired", None),
        429: ("generic", "请求太频繁或额度用完了，稍后再试"),
    }
    try:
        data = account_core.http_json(
            API_URL,
            payload,
            headers=_headers(access),
            timeout=float(timeout or CHAT_TIMEOUT),
            label="Claude",
            error_map=error_map,
            allow_error_body=True,
            secrets_=(access,),
        )
    except account_core.AccountError as e:
        if e.code == "expired":
            account_core.mark_expired(ID)
        raise

    blocks = data.get("content")
    if not isinstance(blocks, list):
        raise account_core.AccountError("generic", "Claude 返回的格式看不懂，再试一次")
    text = "".join(
        str(block.get("text") or "")
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "text"
    ).strip()
    return text


def label(record: dict) -> str | None:
    """Claude 的 OAuth 凭证里没有邮箱，只能显示账号已连接。"""
    value = str(record.get("account") or "").strip()
    return value or None
