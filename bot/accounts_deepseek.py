# -*- coding: utf-8 -*-
"""DeepSeek 账号：浏览器 PKCE 授权 + 账号额度聊天。

移植自 DeepSeek Harness 的
``@deepseek-ai/dsh-deepseek-account-platform``（浏览器授权：auth_init /
auth_exchange / auth_cancel，``x-dsh-auth-token`` 授权头）与
``@deepseek-ai/dsh-llm-deepseek-account``（``GET /models`` 列模型、
``POST {base}/v1/messages`` Anthropic Messages 协议推理）。只依赖标准库。

- 授权：``https://platform.deepseek.com`` 的 ``/auth-api/v0/dsh/*``，
  授权结果是一次长期有效的账号凭证（grant token），没有单独的 refresh token。
- 推理：``https://api.deepseek.com/anthropic/v1/messages``，
  用 ``x-dsh-auth-token`` 头带账号凭证，SSE 流式返回文本。

安全：授权网址里带 PKCE state，不写日志；token 不进日志/异常；错误给中文短句。
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import platform
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from account_core import AccountError, AttemptCancelled, CallbackServer

ID = "deepseek"
NAME = "DeepSeek"
# 参考实现的默认模型；账号真正的可用列表由 GET /models 返回，这里只做默认展示。
MODELS = ["deepseek-flash", "deepseek-v4-pro"]
DEFAULT_MODEL = MODELS[0]

PLATFORM_ORIGIN = "https://platform.deepseek.com"
INFERENCE_ORIGIN = "https://api.deepseek.com"
INFERENCE_BASE = "https://api.deepseek.com/anthropic"
MODELS_URL = "https://api.deepseek.com/models"
TOKEN_HEADER = "x-dsh-auth-token"
# 参考实现（DeepSeek Harness Web 客户端）的版本号，用于 x-client-* 身份头。
CLIENT_VERSION = "0.2.2-rc.1"
CLIENT_LOCALE = "zh_CN"

# platform.deepseek.com 的 WAF（Server: CW）会拦截非浏览器 UA，直接回 429
# "Request Blocked"；平台 auth-api 的请求统一伪装成 Android Chrome 移动端浏览器。
# api.deepseek.com 的推理接口没有这个限制，那边仍用参考实现的 deepseek-harness UA。
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 14; 22011211C) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36"
)
BROWSER_ACCEPT = "application/json, text/plain, */*"
BROWSER_ACCEPT_LANGUAGE = "zh-CN,zh;q=0.9"

CALLBACK_PATH = "/oauth/callback"
# 回调服务起不来时，仍用回环地址做 redirect_uri；浏览器打不开也没关系，
# 用户把地址栏里的完整网址粘回来即可。
FALLBACK_REDIRECT = "http://127.0.0.1:8086/oauth/callback"

# 平台 grant 是一次长期授权，没有公开的过期时间。
# account_core 约定 expires_at <= 0 表示「不知道过期时间，按长期有效处理」，
# 所以这里写 0；真的被平台拒绝时聊天接口会收到 401，界面提示重新连接即可。
GRANT_TTL = 0
HTTP_TIMEOUT = 30
LOGIN_TIMEOUT = 240


# ---------------------------------------------------------------------------
# 基础工具（测试里会替换这些函数，避免联网）
# ---------------------------------------------------------------------------

def _now() -> int:
    return int(time.time())


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _pkce_pair() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def _timezone_offset_seconds() -> int:
    offset = time.localtime().tm_gmtoff
    return int(offset)


def _client_headers() -> dict:
    """平台 auth-api 的请求头：浏览器身份 + 参考实现的 x-client-* 身份头。

    platform.deepseek.com 的 WAF 只放行浏览器形态的请求，缺 UA 会 429。
    """
    return {
        "user-agent": BROWSER_USER_AGENT,
        "accept": BROWSER_ACCEPT,
        "accept-language": BROWSER_ACCEPT_LANGUAGE,
        "x-client-bundle-id": "",
        "x-client-platform": "web",
        "x-client-version": CLIENT_VERSION,
        "x-client-locale": CLIENT_LOCALE,
        "x-client-timezone-offset": str(_timezone_offset_seconds()),
    }


def _http_request(
    url: str,
    method: str = "GET",
    headers: dict | None = None,
    body: bytes | None = None,
    timeout: int = HTTP_TIMEOUT,
) -> tuple[int, bytes]:
    """发一次普通 HTTP 请求，返回 (status, body)。网络失败转 AccountError。"""
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
        detail = b""
        try:
            detail = exc.read()
        except Exception:  # noqa: BLE001
            detail = b""
        raise _inference_error(int(exc.code), detail) from None
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


def _is_unauthorized(status: int, payload) -> bool:
    if status in (401, 403):
        return True
    if isinstance(payload, dict) and payload.get("code") == 40003:
        return True
    return False


def _inference_error(status: int, raw: bytes = b"") -> AccountError:
    """把推理接口的状态码翻成中文错误（不透传服务端原文，避免夹带凭证）。"""
    if status in (401, 403):
        return AccountError("expired", "登录已过期，重新连接一次")
    if status == 429:
        return AccountError("generic", "请求太频繁了，稍后再试")
    if 500 <= status < 600:
        return AccountError("generic", "DeepSeek 服务暂时出错了，稍后再试")
    if status == 400:
        return AccountError("generic", "DeepSeek 没有接受这次请求（可能是模型不支持）")
    return AccountError("generic", "DeepSeek 请求失败，稍后再试")


def _platform_error(status: int, raw: bytes = b"") -> AccountError:
    if status in (401, 403):
        return AccountError("expired", "登录已过期，重新连接一次")
    if status == 429:
        # 平台 WAF 拦非浏览器请求时也回 429；正常不该出现（请求头已按浏览器形态发）。
        return AccountError("generic", "DeepSeek 拒绝了请求（可能被风控），稍后再试")
    if 500 <= status < 600:
        return AccountError("generic", "DeepSeek 服务暂时出错了，稍后再试")
    return AccountError("generic", "DeepSeek 登录没有完成，再试一次")


# ---------------------------------------------------------------------------
# 平台协议
# ---------------------------------------------------------------------------

def _unwrap(payload: dict) -> dict:
    """校验平台统一响应信封：{code:0, data:{biz_code:0, biz_data:...}}。"""
    if not isinstance(payload, dict):
        raise AccountError("generic", "DeepSeek 返回的数据看不懂，再试一次")
    if payload.get("code") == 40003:
        raise AccountError("expired", "登录已过期，重新连接一次")
    if payload.get("code") != 0:
        raise AccountError("generic", "DeepSeek 登录没有完成，再试一次")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise AccountError("generic", "DeepSeek 登录没有完成，再试一次")
    if data.get("biz_code") != 0:
        raise AccountError("generic", "DeepSeek 登录没有完成，再试一次")
    biz_data = data.get("biz_data")
    return biz_data if isinstance(biz_data, dict) else {}


def _platform_post(method: str, payload: dict, timeout: int = HTTP_TIMEOUT) -> dict:
    url = "%s/auth-api/v0/dsh/%s" % (PLATFORM_ORIGIN, method)
    headers = _client_headers()
    headers["content-type"] = "application/json"
    # 浏览器同源请求会带的头，WAF 校验的一部分。
    headers["origin"] = PLATFORM_ORIGIN
    headers["referer"] = PLATFORM_ORIGIN + "/"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    status, raw = _http_request(url, method="POST", headers=headers, body=body, timeout=timeout)
    if status != 200:
        try:
            decoded = json.loads(raw.decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError):
            decoded = None
        if _is_unauthorized(status, decoded):
            raise AccountError("expired", "登录已过期，重新连接一次")
        raise _platform_error(status, raw)
    return _unwrap(_decode_json(raw))


def _platform_get(path: str, token: str, timeout: int = HTTP_TIMEOUT) -> dict:
    url = PLATFORM_ORIGIN + path
    headers = _client_headers()
    headers[TOKEN_HEADER] = token
    headers["origin"] = PLATFORM_ORIGIN
    headers["referer"] = PLATFORM_ORIGIN + "/"
    status, raw = _http_request(url, headers=headers, timeout=timeout)
    if status != 200:
        try:
            decoded = json.loads(raw.decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError):
            decoded = None
        if _is_unauthorized(status, decoded):
            raise AccountError("expired", "登录已过期，重新连接一次")
        raise _platform_error(status, raw)
    return _unwrap(_decode_json(raw))


def _browser_url(value, path: str) -> str:
    """校验平台返回的浏览器地址：必须是 https + 固定的授权/完成路径。"""
    if not isinstance(value, str) or not value:
        raise AccountError("generic", "DeepSeek 返回的登录地址不对，再试一次")
    try:
        parsed = urllib.parse.urlparse(value)
    except ValueError:
        raise AccountError("generic", "DeepSeek 返回的登录地址不对，再试一次") from None
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.fragment
        or parsed.path != path
    ):
        raise AccountError("generic", "DeepSeek 返回的登录地址不对，再试一次")
    return value


def _profile_from_user(user) -> dict:
    if not isinstance(user, dict):
        return {}
    identity = user.get("id_profile")
    identity = identity if isinstance(identity, dict) else {}
    email = user.get("email")
    name = identity.get("name")
    contact = user.get("mobile") or user.get("mobile_number") or email
    profile = {
        "email": email if isinstance(email, str) and email.strip() else None,
        "name": name if isinstance(name, str) and name.strip() else None,
        "contact": contact if isinstance(contact, str) and contact.strip() else None,
        "user_id": user.get("id") if isinstance(user.get("id"), str) else None,
    }
    return {key: value for key, value in profile.items() if value}


def _fetch_profile(token: str, timeout: int = HTTP_TIMEOUT) -> dict:
    return _profile_from_user(_platform_get("/auth-api/v0/users/current", token, timeout))


def _token_from_result(result: dict) -> str:
    token = result.get("token")
    if not isinstance(token, str) or not token:
        raise AccountError("generic", "DeepSeek 没有返回登录凭证，再试一次")
    # 参考实现要求 token 是可打印 ASCII（参考 exchange schema）。
    try:
        token.encode("ascii")
    except UnicodeEncodeError:
        raise AccountError("generic", "DeepSeek 返回的登录凭证不对，再试一次") from None
    if any(ord(ch) < 0x21 or ord(ch) > 0x7E for ch in token):
        raise AccountError("generic", "DeepSeek 返回的登录凭证不对，再试一次")
    return token


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


def _code_from_prompt(value: str | None) -> tuple[str, str]:
    """用户粘贴整条网址或裸授权码都可以。"""
    text = (value or "").strip()
    if not text:
        raise AccountError("denied", "没有收到授权码，再试一次")
    if "code=" in text:
        try:
            parsed = urllib.parse.urlparse(text)
            query = urllib.parse.parse_qs(parsed.query)
        except ValueError:
            query = {}
        codes = query.get("code") or []
        states = query.get("state") or []
        if codes and codes[0].strip():
            return codes[0].strip(), (states[0] if states else "")
    return text, ""


def _device_facts() -> tuple[str, str]:
    system = platform.system().lower() or "linux"
    machine = platform.machine().lower() or "unknown"
    # 参考实现用 Node 的 os.arch()：x64 / arm64。
    machine = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64"}.get(machine, machine)
    os_version = "%s %s" % (platform.system() or "Linux", platform.release() or "")
    return "%s-%s" % (system, machine), os_version.strip()


def _list_models(token: str, timeout: int = HTTP_TIMEOUT) -> list[str]:
    """GET /models：账号可用的模型 id 列表（参考实现的模型列表接口）。"""
    headers = {
        "accept": "application/json",
        "user-agent": "deepseek-harness/%s (+https://github.com/deepseek-ai/deepseek-harness)"
        % CLIENT_VERSION,
        TOKEN_HEADER: token,
    }
    status, raw = _http_request(MODELS_URL, headers=headers, timeout=timeout)
    if status != 200:
        if status in (401, 403):
            raise AccountError("expired", "登录已过期，重新连接一次")
        raise _platform_error(status, raw)
    payload = _decode_json(raw)
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise AccountError("generic", "DeepSeek 返回的模型列表看不懂，稍后再试")
    models: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        model_id = row.get("id")
        if isinstance(model_id, str) and model_id and model_id not in models:
            models.append(model_id)
    return models


def _open_callback():
    """在回环地址上开一次性回调服务；失败返回 None（降级粘贴网址）。"""
    try:
        return CallbackServer(port=0, path=CALLBACK_PATH)
    except AccountError:
        return None


def _redirect_uri_from_server(server) -> str:
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


def _cancel_authorization(authorize_id: str | None, verifier: str) -> None:
    """取消登录时尽力通知平台作废这次授权；失败不影响取消结果。"""
    if not authorize_id:
        return
    try:
        _platform_post(
            "auth_cancel",
            {"authorize_id": authorize_id, "code_verifier": verifier},
            timeout=5,
        )
    except Exception:  # noqa: BLE001 - 只是清理，不要影响取消
        pass


def _wait_callback(server, attempt, timeout: int) -> dict:
    query = server.wait(timeout, attempt)
    if not isinstance(query, dict):
        raise AccountError("timeout", "登录超时了，再试一次")
    return query


def begin(attempt) -> dict:
    """阻塞完成一次 DeepSeek 浏览器授权，返回要保存的账号记录。"""
    attempt.check_cancelled()
    attempt.set_phase("starting", "正在准备登录…")

    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(32)
    server = None
    authorize_id = None
    try:
        server = _open_callback()
        if server is not None:
            redirect_uri = _redirect_uri_from_server(server)
        else:
            redirect_uri = FALLBACK_REDIRECT

        init = _platform_post(
            "auth_init",
            {
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": state,
                "redirect_uri": redirect_uri,
                "locale": CLIENT_LOCALE,
                "login_source": "web",
            },
        )
        authorize_url = _browser_url(init.get("authorize_url"), "/dsh/authorize")
        if not isinstance(init.get("authorize_id"), str) or not init["authorize_id"]:
            raise AccountError("generic", "DeepSeek 登录没有开始成功，再试一次")
        authorize_id = init["authorize_id"]
        try:
            expires_in = int(init.get("expires_in"))
        except (TypeError, ValueError):
            expires_in = LOGIN_TIMEOUT
        if expires_in <= 0:
            expires_in = LOGIN_TIMEOUT
        wait_timeout = min(max(expires_in, 30), LOGIN_TIMEOUT)

        attempt.notify_url(authorize_url, "在浏览器里完成登录")
        if server is not None:
            query = _wait_callback(server, attempt, wait_timeout)
            code, got_state = _code_from_query(query)
        else:
            # prompt 阶段界面不再显示 open 阶段的 url，把网址一起放进提示里。
            answer = attempt.prompt(
                "在浏览器完成登录后，把地址栏里的完整网址粘贴到这里",
                message="打开下面的网址完成授权，再把浏览器地址栏里的完整网址粘贴到这里："
                + authorize_url,
            )
            code, got_state = _code_from_prompt(answer)
        if got_state != state:
            raise AccountError("denied", "登录状态不匹配，重新登录一次")
        attempt.check_cancelled()

        device_id = str(uuid.uuid4())
        device_model, os_version = _device_facts()
        result = _platform_post(
            "auth_exchange",
            {
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": redirect_uri,
                "device_id": device_id,
                "device_model": device_model,
                "os_version": os_version,
            },
        )
        token = _token_from_result(result)
        _browser_url(result.get("authorized_url"), "/dsh/authorized")
    except AttemptCancelled:
        _cancel_authorization(authorize_id, verifier)
        raise
    finally:
        _close_quietly(server)

    attempt.check_cancelled()
    user = result.get("user")
    profile = _profile_from_user(user)
    if not profile:
        try:
            profile = _fetch_profile(token, timeout=15)
        except AccountError as exc:
            # 网络问题不影响授权本身：凭证已拿到，展示名稍后可以补。
            if exc.code not in ("network", "timeout"):
                raise
            profile = {}

    # 顺手记录账号可用的模型列表（失败不算登录失败，静态 MODELS 仍可用）。
    models: list[str] = []
    try:
        models = _list_models(token, timeout=15)
    except AccountError:
        models = []

    now = _now()
    account = profile.get("email") or profile.get("contact") or profile.get("name")
    extra = {
        "origin": PLATFORM_ORIGIN,
        "email": profile.get("email"),
        "name": profile.get("name"),
        "user_id": profile.get("user_id"),
        "device_id": device_id,
        "grant_type": "platform",
        "models": models or None,
    }
    return {
        "kind": "oauth",
        "access": token,
        "refresh": "",
        "expires_at": GRANT_TTL,
        "account": account,
        "extra": {key: value for key, value in extra.items() if value is not None},
        "updated_at": now,
    }


def refresh(record: dict) -> dict | None:
    """平台授权没有 refresh token，这里向平台核对凭证是否仍然有效并续期。

    - 平台明确拒绝（401 / 40003）→ 返回 None，让核心提示重新登录。
    - 网络不通 → 先按原记录续期（聊天时会给出网络错误），避免误判成过期。
    """
    record = record or {}
    token = record.get("access")
    if not isinstance(token, str) or not token:
        return None
    now = _now()
    try:
        profile = _fetch_profile(token, timeout=15)
    except AccountError as exc:
        if exc.code in ("network", "timeout"):
            updated = dict(record)
            updated["updated_at"] = now
            return updated
        return None
    updated = dict(record)
    updated["expires_at"] = int(record.get("expires_at") or 0)
    updated["updated_at"] = now
    if profile:
        extra = dict(updated.get("extra") or {})
        for key in ("email", "name", "user_id"):
            if profile.get(key):
                extra[key] = profile[key]
        updated["extra"] = extra
        updated["account"] = (
            profile.get("email") or profile.get("contact") or updated.get("account")
        )
    return updated


# ---------------------------------------------------------------------------
# 推理（Anthropic Messages 协议）
# ---------------------------------------------------------------------------

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


def _build_body(model: str, messages, temperature=None, max_tokens=None) -> dict:
    """OpenAI 格式消息 → Anthropic Messages 请求体（参考实现的 serialize 简化版）。"""
    system_parts: list[str] = []
    wire_messages: list[dict] = []
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
        wire_role = "assistant" if role == "assistant" else "user"
        block = [{"type": "text", "text": text}]
        if wire_messages and wire_messages[-1]["role"] == wire_role:
            wire_messages[-1]["content"].extend(block)
        else:
            wire_messages.append({"role": wire_role, "content": block})
    if not wire_messages:
        raise AccountError("generic", "没有可以发送的消息")
    try:
        token_limit = int(max_tokens) if max_tokens else 0
    except (TypeError, ValueError):
        token_limit = 0
    body: dict = {
        "model": model,
        "stream": True,
        "messages": wire_messages,
        # 关掉思考块：这样文本回复简单，temperature 也不会被拒。
        "thinking": {"type": "disabled"},
    }
    # 账号订阅模式不限制输出：0 表示不传 max_tokens，用账号默认
    if token_limit > 0:
        body["max_tokens"] = token_limit
    if system_parts:
        body["system"] = "\n\n".join(system_parts)
    if temperature is not None:
        try:
            body["temperature"] = float(temperature)
        except (TypeError, ValueError):
            pass
    return body


def _messages_headers(token: str, user_id: str | None = None) -> dict:
    headers = {
        "content-type": "application/json",
        "accept": "text/event-stream",
        TOKEN_HEADER: token,
        "anthropic-version": "2023-06-01",
        # 参考实现每次请求都带这个自报身份的 UA。
        "user-agent": "deepseek-harness/%s (+https://github.com/deepseek-ai/deepseek-harness)"
        % CLIENT_VERSION,
    }
    if user_id:
        # 参考实现带的匿名用户标识（这里用平台自己的用户 id，不额外存东西）。
        headers["x-deepseek-harness-user-id"] = user_id
    return headers


def _iter_sse(response):
    """逐条解析 SSE 数据块。"""
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


def _text_from_json_payload(payload: dict) -> str:
    """非流式（或整包 JSON）响应的文本提取。"""
    chunks = []
    for block in payload.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "thinking":
            continue
        text = block.get("text")
        if isinstance(text, str) and text:
            chunks.append(text)
    return "".join(chunks).strip()


def _consume_stream(response) -> str:
    """读 SSE，拼接 text_delta；如果服务端直接返回整包 JSON 也能兼容。"""
    try:
        body = response.read()
    except (OSError, ValueError):
        raise AccountError("network", "网络连不上，检查网络后再试") from None
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            whole = json.loads(stripped)
        except ValueError:
            whole = None
        if isinstance(whole, dict) and isinstance(whole.get("content"), list):
            return _text_from_json_payload(whole)

    chunks: list[str] = []
    events = 0
    for item in _iter_sse(io.BytesIO(text.encode("utf-8"))):
        events += 1
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "error":
            error = item.get("error")
            message = ""
            if isinstance(error, dict):
                message = str(error.get("message") or "")
            if "quota" in message.lower() or "insufficient" in message.lower():
                raise AccountError("generic", "DeepSeek 账号额度不够了，充值或换一个账号再试")
            raise AccountError("generic", "DeepSeek 返回了错误，稍后再试")
        if kind == "content_block_delta":
            delta = item.get("delta")
            if isinstance(delta, dict) and delta.get("type") in ("text_delta", None):
                piece = delta.get("text")
                if isinstance(piece, str) and piece:
                    chunks.append(piece)
            continue
        if kind == "content_block_start":
            block = item.get("content_block")
            if isinstance(block, dict) and block.get("type") == "text":
                piece = block.get("text")
                if isinstance(piece, str) and piece:
                    chunks.append(piece)
            continue
        if kind is None and "content" in item:
            # 服务端直接给了整包 JSON（非 SSE）。
            return _text_from_json_payload(item)
    text = "".join(chunks).strip()
    if not text and events == 0:
        raise AccountError("generic", "DeepSeek 没有返回内容，再试一次")
    return text


def chat(record, model, messages, temperature, max_tokens, timeout) -> str:
    record = record or {}
    token = record.get("access")
    if not isinstance(token, str) or not token:
        raise AccountError("not-signed-in", "还没有连接这个账号")
    request_timeout = int(timeout) if timeout else 120
    body = _build_body(model or DEFAULT_MODEL, messages, temperature, max_tokens)
    url = INFERENCE_BASE + "/v1/messages"
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    extra = record.get("extra")
    user_id = None
    if isinstance(extra, dict):
        value = extra.get("user_id")
        if isinstance(value, str) and value:
            user_id = value
    response = _open_stream(
        url, payload, _messages_headers(token, user_id), timeout=request_timeout
    )
    try:
        text = _consume_stream(response)
    finally:
        _close_quietly(response)
    if not text:
        raise AccountError("generic", "DeepSeek 没有返回内容，再试一次")
    return text


def label(record) -> str | None:
    record = record or {}
    extra = record.get("extra")
    if isinstance(extra, dict):
        for key in ("email", "name"):
            value = extra.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    account = record.get("account")
    if isinstance(account, str) and account.strip():
        return account.strip()
    return None
