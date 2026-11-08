# -*- coding: utf-8 -*-
"""Gemini 账号：Google 登录 + Gemini Code Assist 额度聊天。

移植自 gemini-cli 的 ``code_assist/{oauth2,setup,server,converter}.js``
（按协议重新实现，不 import 参考实现）。只依赖标准库。

- OAuth：Google「已安装应用」客户端 + 本机回环回调 ``127.0.0.1:8085/oauth2callback``；
  端口被占（或无回调服务）时降级为用户码模式：Google 官方回调页
  ``https://codeassist.google.com/authcode`` 会显示授权码，让用户粘贴。
- 聊天：``loadCodeAssist`` →（需要时）``onboardUser`` → ``streamGenerateContent``，
  端点为 ``https://cloudcode-pa.googleapis.com/v1internal``。

安全：access/refresh token 不写日志、不进异常文本；错误只给中文短句。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from account_core import AccountError, AttemptCancelled, CallbackServer

ID = "gemini"
NAME = "Gemini"
MODELS = [
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-3-pro-preview",
    "gemini-3-flash-preview",
]
DEFAULT_MODEL = MODELS[0]

# 参考实现里的 Google OAuth 客户端（已安装应用，client_secret 本就不算机密）。
OAUTH_CLIENT_ID = (
    "681255809395-oo8ft2oprdrnp9e3aqf6av3hmdib135j.apps.googleusercontent.com"
)
OAUTH_CLIENT_SECRET = "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"
OAUTH_SCOPES = [
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "openid",
]
AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"

CODE_ASSIST_ENDPOINT = "https://cloudcode-pa.googleapis.com"
CODE_ASSIST_API_VERSION = "v1internal"
# gemini-cli 的版本号，只用于 User-Agent 自报身份。
CODE_ASSIST_CLIENT_VERSION = "0.67.0"

CALLBACK_PORT = 8085
CALLBACK_PATH = "/oauth2callback"
USER_CODE_REDIRECT = "https://codeassist.google.com/authcode"

HTTP_TIMEOUT = 30
LOGIN_TIMEOUT = 240
ONBOARD_TIMEOUT = 60
TOKEN_SKEW = 60


# ---------------------------------------------------------------------------
# 基础工具（测试里会替换这些函数，避免联网）
# ---------------------------------------------------------------------------

def _now() -> int:
    return int(time.time())


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _pkce_pair() -> tuple[str, str]:
    """生成 PKCE S256 的 (verifier, challenge)。"""
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def _http_request(
    url: str,
    method: str = "GET",
    headers: dict | None = None,
    body: bytes | None = None,
    timeout: int = HTTP_TIMEOUT,
) -> tuple[int, bytes]:
    """发一次普通 HTTP 请求，返回 (status, body)。

    网络层失败统一转成 AccountError，绝不把底层异常文本直接抛给用户。
    """
    request = urllib.request.Request(url, data=body, headers=dict(headers or {}), method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
        except Exception:  # noqa: BLE001 - 读不到响应体就算了
            raw = b""
        return int(exc.code), raw
    except AccountError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
            raise AccountError("timeout", "网络超时了，稍后再试") from None
        raise AccountError("network", "网络连不上，检查网络后再试") from None


def _open_stream(
    url: str,
    body: bytes,
    headers: dict,
    timeout: int = HTTP_TIMEOUT,
):
    """POST 并返回可逐行读的响应对象（SSE）。失败转 AccountError。"""
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raise _http_status_error(int(exc.code)) from None
    except AccountError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
            raise AccountError("timeout", "网络超时了，稍后再试") from None
        raise AccountError("network", "网络连不上，检查网络后再试") from None


def _decode_json(raw: bytes) -> dict:
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError):
        raise AccountError("generic", "服务返回的数据看不懂，稍后再试") from None
    if not isinstance(data, dict):
        raise AccountError("generic", "服务返回的数据看不懂，稍后再试")
    return data


def _http_status_error(status: int) -> AccountError:
    """把 Google / Code Assist 的 HTTP 状态码翻译成中文错误。"""
    if status in (401,):
        return AccountError("expired", "登录已过期，重新连接一次")
    if status in (403,):
        return AccountError("denied", "这个 Google 账号没有访问权限，换一个账号试试")
    if status in (408, 504):
        return AccountError("timeout", "网络超时了，稍后再试")
    if status == 429:
        return AccountError("generic", "请求太频繁了，稍后再试")
    if 500 <= status < 600:
        return AccountError("generic", "Google 服务暂时出错了，稍后再试")
    if status == 400:
        return AccountError("generic", "请求没有被 Google 接受，重新登录一次")
    return AccountError("generic", "登录没有完成，再试一次")


def _auth_headers(access: str) -> dict:
    return {"Authorization": "Bearer " + access}


def _close_quietly(obj) -> None:
    if obj is None:
        return
    close = getattr(obj, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception:  # noqa: BLE001 - 收尾失败不影响结果
        pass


# ---------------------------------------------------------------------------
# Google OAuth
# ---------------------------------------------------------------------------

def _form_body(fields: dict) -> bytes:
    return urllib.parse.urlencode(fields).encode("utf-8")


def _authorize_url(
    redirect_uri: str,
    state: str,
    verifier: str | None = None,
    challenge: str | None = None,
) -> str:
    """拼 Google 授权页地址（网页流程不带 PKCE，用户码流程带 S256 PKCE）。"""
    params = {
        "client_id": OAUTH_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(OAUTH_SCOPES),
        "access_type": "offline",
        # 强制每次登录都显示同意页，保证能拿到 refresh_token。
        "prompt": "consent",
        "state": state,
    }
    if verifier and challenge:
        params["code_challenge"] = challenge
        params["code_challenge_method"] = "S256"
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)


def _exchange_code(code: str, redirect_uri: str, verifier: str | None = None) -> dict:
    """用授权码换 token。"""
    fields = {
        "client_id": OAUTH_CLIENT_ID,
        "client_secret": OAUTH_CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }
    if verifier:
        fields["code_verifier"] = verifier
    status, raw = _http_request(
        TOKEN_URL,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        body=_form_body(fields),
    )
    if status != 200:
        if status == 400:
            raise AccountError("denied", "授权码无效或已经过期，重新登录一次")
        raise _http_status_error(status)
    data = _decode_json(raw)
    if not isinstance(data.get("access_token"), str) or not data["access_token"]:
        raise AccountError("generic", "登录没有拿到凭证，再试一次")
    return data


def _fetch_email(access: str, timeout: int = HTTP_TIMEOUT) -> str | None:
    """取账号邮箱；失败不算登录失败（label 只是展示用）。"""
    try:
        status, raw = _http_request(
            USERINFO_URL, headers=_auth_headers(access), timeout=timeout
        )
    except AccountError:
        return None
    if status != 200:
        return None
    try:
        data = _decode_json(raw)
    except AccountError:
        return None
    email = data.get("email")
    return email if isinstance(email, str) and email.strip() else None


def refresh(record: dict) -> dict | None:
    """用 refresh_token 换新的 access token；失败返回 None（由核心提示重新登录）。"""
    record = record or {}
    refresh_token = record.get("refresh") or ""
    if not refresh_token:
        return None
    fields = {
        "client_id": OAUTH_CLIENT_ID,
        "client_secret": OAUTH_CLIENT_SECRET,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    try:
        status, raw = _http_request(
            TOKEN_URL,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=_form_body(fields),
        )
    except AccountError as exc:
        # 网络问题不是「登录过期」，让上层原样报网络错误。
        if exc.code in ("network", "timeout"):
            raise
        return None
    if status != 200:
        return None
    try:
        data = _decode_json(raw)
    except AccountError:
        return None
    access = data.get("access_token")
    if not isinstance(access, str) or not access:
        return None
    expires_in = data.get("expires_in")
    try:
        ttl = int(expires_in)
    except (TypeError, ValueError):
        ttl = 3600
    if ttl <= 0:
        ttl = 3600
    now = _now()
    updated = dict(record)
    updated["access"] = access
    updated["expires_at"] = now + max(ttl - TOKEN_SKEW, 60)
    new_refresh = data.get("refresh_token")
    if isinstance(new_refresh, str) and new_refresh:
        updated["refresh"] = new_refresh
    updated["updated_at"] = now
    return updated


def _code_from_query(query) -> tuple[str, str]:
    if not isinstance(query, dict):
        raise AccountError("generic", "登录回调没有带回参数，再试一次")
    if query.get("error"):
        raise AccountError("denied", "授权被拒绝了，再试一次")
    code = query.get("code")
    if not isinstance(code, str) or not code.strip():
        raise AccountError("denied", "授权没有完成，再试一次")
    state = query.get("state")
    return code.strip(), state if isinstance(state, str) else ""


def _code_from_prompt(value: str | None) -> str:
    """用户码模式下，用户可能粘授权码，也可能粘一整条网址。"""
    text = (value or "").strip()
    if not text:
        raise AccountError("denied", "没有收到授权码，再试一次")
    if "code=" in text:
        try:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(text).query)
        except ValueError:
            query = {}
        candidates = query.get("code") or []
        if candidates and candidates[0].strip():
            return candidates[0].strip()
    return text


def _redirect_uri_from_server(server) -> str:
    """从回调服务拿回环 redirect_uri，并统一成 127.0.0.1。"""
    uri = getattr(server, "redirect_uri", None)
    parsed = urllib.parse.urlparse(uri or "")
    if (
        parsed.scheme == "http"
        and parsed.hostname in ("127.0.0.1", "localhost")
        and parsed.port
        and parsed.path == CALLBACK_PATH
    ):
        return "http://127.0.0.1:%d%s" % (parsed.port, CALLBACK_PATH)
    raise AccountError("generic", "本地回调服务没有准备好，再试一次")


def _open_callback():
    """优先固定 8085 端口；被占用时换随机端口；都不行返回 None（降级用户码）。"""
    for port in (CALLBACK_PORT, 0):
        try:
            return CallbackServer(port=port, path=CALLBACK_PATH)
        except AccountError:
            continue
    return None


def _wait_callback(server, attempt) -> dict:
    query = server.wait(LOGIN_TIMEOUT, attempt)
    if not isinstance(query, dict):
        raise AccountError("timeout", "登录超时了，再试一次")
    return query


def begin(attempt) -> dict:
    """阻塞完成一次 Google 登录，返回要保存的账号记录。"""
    attempt.check_cancelled()
    attempt.set_phase("starting", "正在准备登录…")
    state = secrets.token_hex(32)
    server = None
    try:
        server = _open_callback()
        if server is not None:
            redirect_uri = _redirect_uri_from_server(server)
            auth_url = _authorize_url(redirect_uri, state)
            attempt.notify_url(auth_url, "在浏览器里完成登录")
            query = _wait_callback(server, attempt)
            code, got_state = _code_from_query(query)
            if got_state != state:
                raise AccountError("denied", "登录状态不匹配，重新登录一次")
            attempt.check_cancelled()
            tokens = _exchange_code(code, redirect_uri)
        else:
            verifier, challenge = _pkce_pair()
            redirect_uri = USER_CODE_REDIRECT
            auth_url = _authorize_url(redirect_uri, state, verifier, challenge)
            attempt.notify_url(auth_url, "在浏览器里打开下面的网址完成登录")
            # prompt 阶段界面不再显示 open 阶段的 url，把网址一起放进提示里。
            answer = attempt.prompt(
                "把网页上显示的授权码粘贴到这里",
                message="打开下面的网址完成授权，再把网页上显示的授权码粘贴到这里："
                + auth_url,
            )
            code = _code_from_prompt(answer)
            attempt.check_cancelled()
            tokens = _exchange_code(code, redirect_uri, verifier)
    finally:
        _close_quietly(server)

    access = tokens.get("access_token")
    if not isinstance(access, str) or not access:
        raise AccountError("generic", "登录没有拿到凭证，再试一次")
    refresh_token = tokens.get("refresh_token")
    refresh_token = refresh_token if isinstance(refresh_token, str) else ""
    expires_in = tokens.get("expires_in")
    try:
        ttl = int(expires_in)
    except (TypeError, ValueError):
        ttl = 3600
    if ttl <= 0:
        ttl = 3600

    attempt.check_cancelled()
    email = _fetch_email(access)

    # 登录后顺带把 Code Assist 项目准备好，聊天时就不用每次都探测。
    project = None
    tier = None
    try:
        project, tier = _setup_project(access)
    except AccountError as exc:
        # 网络问题不阻塞登录：聊天时会再试一次。
        if exc.code not in ("network", "timeout"):
            raise

    now = _now()
    extra = {
        "email": email,
        "project": project,
        "tier": tier,
        "oauth_client": "gemini-cli",
    }
    return {
        "kind": "oauth",
        "access": access,
        "refresh": refresh_token,
        "expires_at": now + max(ttl - TOKEN_SKEW, 60),
        "account": email,
        "extra": {key: value for key, value in extra.items() if value is not None},
        "updated_at": now,
    }


# ---------------------------------------------------------------------------
# Gemini Code Assist：项目 + 聊天
# ---------------------------------------------------------------------------

def _client_metadata(project: str | None = None) -> dict:
    metadata = {
        "ideType": "IDE_UNSPECIFIED",
        "platform": "PLATFORM_UNSPECIFIED",
        "pluginType": "GEMINI",
    }
    if project:
        metadata["duetProject"] = project
    return metadata


def _code_assist_post(method: str, access: str, payload: dict, timeout: int = HTTP_TIMEOUT) -> dict:
    url = "%s/%s:%s" % (CODE_ASSIST_ENDPOINT, CODE_ASSIST_API_VERSION, method)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = _auth_headers(access)
    headers["Content-Type"] = "application/json"
    status, raw = _http_request(url, method="POST", headers=headers, body=body, timeout=timeout)
    if status != 200:
        if status == 403:
            # 企业 VPC-SC 用户的 loadCodeAssist 会 403，参考实现按标准层继续。
            try:
                error = _decode_json(raw).get("error") or {}
            except AccountError:
                error = {}
            details = error.get("details") if isinstance(error, dict) else None
            if isinstance(details, list):
                for detail in details:
                    if isinstance(detail, dict) and detail.get("reason") == "SECURITY_POLICY_VIOLATED":
                        return {"currentTier": {"id": "standard-tier"}}
        raise _http_status_error(status)
    return _decode_json(raw)


def _code_assist_get(url: str, access: str, timeout: int = HTTP_TIMEOUT) -> dict:
    headers = _auth_headers(access)
    headers["Content-Type"] = "application/json"
    status, raw = _http_request(url, headers=headers, timeout=timeout)
    if status != 200:
        raise _http_status_error(status)
    return _decode_json(raw)


def _load_code_assist(access: str, project: str | None, timeout: int = HTTP_TIMEOUT) -> dict:
    payload = {"metadata": _client_metadata(project)}
    if project:
        payload["cloudaicompanionProject"] = project
    return _code_assist_post("loadCodeAssist", access, payload, timeout)


def _pick_default_tier(load_response: dict) -> dict:
    allowed = load_response.get("allowedTiers") or []
    if isinstance(allowed, list):
        for tier in allowed:
            if isinstance(tier, dict) and tier.get("isDefault"):
                return tier
    return {"id": "legacy-tier", "name": ""}


def _onboard_user(access: str, tier: dict, project: str | None, timeout: int = HTTP_TIMEOUT) -> dict:
    tier_id = tier.get("id")
    payload: dict = {"metadata": _client_metadata(None)}
    if tier_id:
        payload["tierId"] = tier_id
    if tier_id == "free-tier":
        # 免费层用 Google 托管项目，带项目字段会 Precondition Failed，直接不传。
        pass
    elif project:
        payload["cloudaicompanionProject"] = project
        payload["metadata"] = _client_metadata(project)
    return _code_assist_post("onboardUser", access, payload, timeout)


def _wait_operation(access: str, name: str, timeout: int = ONBOARD_TIMEOUT) -> dict:
    url = "%s/%s/%s" % (CODE_ASSIST_ENDPOINT, CODE_ASSIST_API_VERSION, name)
    deadline = time.time() + timeout
    result = _code_assist_get(url, access)
    while not result.get("done") and result.get("name"):
        if time.time() > deadline:
            raise AccountError("generic", "开通 Gemini Code Assist 超时了，稍后再试")
        time.sleep(5)
        result = _code_assist_get(url, access)
    return result


def _project_from_onboard(result: dict) -> str | None:
    response = result.get("response")
    if not isinstance(response, dict):
        return None
    project = response.get("cloudaicompanionProject")
    if isinstance(project, dict):
        project_id = project.get("id")
        if isinstance(project_id, str) and project_id:
            return project_id
    if isinstance(project, str) and project:
        return project
    return None


def _env_project() -> str | None:
    """企业/付费账号可以用环境变量指定 GCP 项目（参考实现同样支持）。"""
    for key in ("GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_PROJECT_ID"):
        value = os.environ.get(key)
        if isinstance(value, str) and value.strip() and not value.strip().isdigit():
            return value.strip()
    return None


def _setup_project(access: str, project_hint: str | None = None) -> tuple[str | None, str | None]:
    """确认账号的 Code Assist 项目；需要时开通免费层。返回 (project, tier_id)。"""
    project_hint = project_hint or _env_project()
    load_response = _load_code_assist(access, project_hint)
    current_tier = load_response.get("currentTier")
    if isinstance(current_tier, dict) and current_tier:
        tier_id = current_tier.get("id") or "standard-tier"
        project = load_response.get("cloudaicompanionProject") or project_hint
        if not project:
            raise AccountError(
                "unsupported",
                "这个 Google 账号没有可用的 Gemini Code Assist 项目，换一个账号试试",
            )
        return str(project), str(tier_id)
    if project_hint:
        # 显式项目（企业/付费）无需开通。
        return project_hint, "standard-tier"

    ineligible = load_response.get("ineligibleTiers")
    if isinstance(ineligible, list) and ineligible:
        for tier in ineligible:
            if not isinstance(tier, dict):
                continue
            if tier.get("reasonCode") == "VALIDATION_REQUIRED":
                raise AccountError(
                    "unsupported",
                    "这个 Google 账号需要额外验证，暂时不能使用 Gemini Code Assist",
                )
        raise AccountError(
            "unsupported",
            "这个 Google 账号不在 Gemini Code Assist 的可用范围内，换一个账号试试",
        )

    tier = _pick_default_tier(load_response)
    result = _onboard_user(access, tier, project_hint)
    if not result.get("done") and result.get("name"):
        result = _wait_operation(access, str(result["name"]))
    project = _project_from_onboard(result) or project_hint
    if not project:
        raise AccountError(
            "unsupported",
            "开通 Gemini Code Assist 没有拿到项目，稍后再试或换一个账号",
        )
    tier_id = tier.get("id")
    return project, (str(tier_id) if tier_id else None)


def _record_project(record: dict) -> str | None:
    extra = record.get("extra")
    if not isinstance(extra, dict):
        return None
    project = extra.get("project")
    return project if isinstance(project, str) and project else None


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks = []
        for part in content:
            if isinstance(part, str):
                chunks.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                chunks.append(part["text"])
        return "".join(chunks)
    return ""


def _system_and_contents(messages) -> tuple[str, list]:
    """OpenAI 格式消息 → (system 文本, Gemini contents)。"""
    system_parts: list[str] = []
    contents: list[dict] = []
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        role = message.get("role") or "user"
        text = _content_text(message.get("content"))
        if not text:
            continue
        if role == "system":
            system_parts.append(text)
            continue
        wire_role = "model" if role == "assistant" else "user"
        if contents and contents[-1]["role"] == wire_role:
            contents[-1]["parts"].append({"text": text})
        else:
            contents.append({"role": wire_role, "parts": [{"text": text}]})
    return "\n\n".join(system_parts), contents


def _build_request(
    model: str,
    project: str | None,
    messages,
    temperature=None,
    max_tokens=None,
) -> dict:
    system, contents = _system_and_contents(messages)
    if not contents:
        raise AccountError("generic", "没有可以发送的消息")
    generation_config: dict = {}
    if temperature is not None:
        try:
            generation_config["temperature"] = float(temperature)
        except (TypeError, ValueError):
            pass
    if max_tokens:
        try:
            generation_config["maxOutputTokens"] = int(max_tokens)
        except (TypeError, ValueError):
            pass
    request: dict = {"contents": contents, "session_id": str(uuid.uuid4())}
    if system:
        request["systemInstruction"] = {"role": "user", "parts": [{"text": system}]}
    if generation_config:
        request["generationConfig"] = generation_config
    payload: dict = {
        "model": model,
        "user_prompt_id": str(uuid.uuid4()),
        "request": request,
    }
    if project:
        payload["project"] = project
    return payload


def _client_user_agent(model: str) -> str:
    arch = platform.machine().lower()
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(
        arch, arch or "unknown"
    )
    os_name = platform.system().lower() or "linux"
    return "GeminiCLI/%s/%s (%s; %s; terminal)" % (
        CODE_ASSIST_CLIENT_VERSION,
        model,
        os_name,
        arch,
    )


def _iter_sse(response):
    """逐条解析 SSE 里的 JSON 数据块（容忍坏块，参考实现同样跳过）。"""
    data_lines: list[str] = []

    def flush():
        if not data_lines:
            return None
        chunk = "\n".join(data_lines)
        data_lines.clear()
        try:
            return json.loads(chunk)
        except ValueError:
            return None

    while True:
        try:
            raw = response.readline()
        except (OSError, ValueError):
            break
        if not raw:
            break
        if isinstance(raw, bytes):
            line = raw.decode("utf-8", "replace")
        else:
            line = str(raw)
        line = line.rstrip("\r\n")
        if line.startswith("data:"):
            data_lines.append(line[5:].strip())
        elif line == "":
            item = flush()
            if item is not None:
                yield item
    item = flush()
    if item is not None:
        yield item


def _consume_stream(response) -> str:
    """收集 streamGenerateContent 的所有文本片段。"""
    chunks: list[str] = []
    block_reason = None
    for item in _iter_sse(response):
        if not isinstance(item, dict):
            continue
        payload = item.get("response")
        if not isinstance(payload, dict):
            payload = item if "candidates" in item else None
        if payload is None:
            continue
        feedback = payload.get("promptFeedback")
        if isinstance(feedback, dict) and feedback.get("blockReason"):
            block_reason = feedback.get("blockReason")
        for candidate in payload.get("candidates") or []:
            if not isinstance(candidate, dict):
                continue
            content = candidate.get("content")
            if not isinstance(content, dict):
                continue
            for part in content.get("parts") or []:
                if not isinstance(part, dict) or part.get("thought"):
                    continue
                piece = part.get("text")
                if isinstance(piece, str) and piece:
                    chunks.append(piece)
    text = "".join(chunks).strip()
    if not text and block_reason:
        raise AccountError("generic", "Gemini 拒绝了这段对话内容（触发了安全策略）")
    return text


def _image_mime(data: bytes) -> str:
    """按文件头识别图片 mime（识别不了按 JPEG）。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def describe_images(
    record,
    model,
    images,
    prompt="请描述这些图片",
    timeout=60,
    max_tokens=700,
) -> str:
    """用账号额度识图：contents parts 里放文本 + inlineData 图片块。"""
    record = record or {}
    access = record.get("access")
    if not isinstance(access, str) or not access:
        raise AccountError("not-signed-in", "还没有连接这个账号")
    prepared: list[bytes] = []
    for raw in list(images or [])[:4]:
        if not isinstance(raw, (bytes, bytearray)):
            raise AccountError("generic", "图片格式不对，再试一次")
        data = bytes(raw)
        if data:
            prepared.append(data)
    if not prepared:
        raise AccountError("generic", "没有图片可以识别")

    project = _record_project(record)
    if not project:
        project, _tier = _setup_project(access)
    model_name = str(model or "").strip() or DEFAULT_MODEL

    parts: list[dict] = [{"text": str(prompt or "").strip() or "请描述这些图片"}]
    for data in prepared:
        parts.append(
            {
                "inlineData": {
                    "mimeType": _image_mime(data),
                    "data": base64.b64encode(data).decode("ascii"),
                }
            }
        )
    request: dict = {
        "contents": [{"role": "user", "parts": parts}],
        "session_id": str(uuid.uuid4()),
    }
    generation_config: dict = {"temperature": 0.2}
    try:
        limit = int(max_tokens) if max_tokens else 0
    except (TypeError, ValueError):
        limit = 0
    if limit > 0:
        generation_config["maxOutputTokens"] = limit
    request["generationConfig"] = generation_config

    payload: dict = {
        "model": model_name,
        "user_prompt_id": str(uuid.uuid4()),
        "request": request,
    }
    if project:
        payload["project"] = project
    url = "%s/%s:streamGenerateContent?alt=sse" % (
        CODE_ASSIST_ENDPOINT,
        CODE_ASSIST_API_VERSION,
    )
    headers = {
        "Authorization": "Bearer " + access,
        "Content-Type": "application/json",
        "User-Agent": _client_user_agent(model_name),
    }
    request_timeout = int(timeout) if timeout else 120
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    response = _open_stream(url, body, headers, timeout=request_timeout)
    try:
        text = _consume_stream(response)
    finally:
        _close_quietly(response)
    if not text:
        raise AccountError("generic", "Gemini 没有返回图片描述，再试一次")
    return text


def chat(record, model, messages, temperature, max_tokens, timeout) -> str:
    record = record or {}
    access = record.get("access")
    if not isinstance(access, str) or not access:
        raise AccountError("not-signed-in", "还没有连接这个账号")
    project = _record_project(record)
    if not project:
        # 登录时可能因为网络没准备好项目，这里补一次。
        project, _tier = _setup_project(access)
    request_timeout = int(timeout) if timeout else 120
    payload = _build_request(model or DEFAULT_MODEL, project, messages, temperature, max_tokens)
    url = "%s/%s:streamGenerateContent?alt=sse" % (CODE_ASSIST_ENDPOINT, CODE_ASSIST_API_VERSION)
    headers = {
        "Authorization": "Bearer " + access,
        "Content-Type": "application/json",
        "User-Agent": _client_user_agent(model or DEFAULT_MODEL),
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    response = _open_stream(url, body, headers, timeout=request_timeout)
    try:
        text = _consume_stream(response)
    finally:
        _close_quietly(response)
    if not text:
        raise AccountError("generic", "Gemini 没有返回内容，再试一次")
    return text


def label(record) -> str | None:
    record = record or {}
    account = record.get("account")
    if isinstance(account, str) and account.strip():
        return account.strip()
    extra = record.get("extra")
    if isinstance(extra, dict):
        email = extra.get("email")
        if isinstance(email, str) and email.strip():
            return email.strip()
    return None
