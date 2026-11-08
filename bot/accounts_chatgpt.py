"""ChatGPT（OpenAI Codex）账号 OAuth 登录与聊天。

按 pi-ai 的 auth/oauth/openai-codex.js、api/openai-codex-responses.js 移植：
- 设备码登录：POST auth.openai.com/api/accounts/deviceauth/usercode，轮询 token
- 浏览器登录：https://auth.openai.com/oauth/authorize，回调 127.0.0.1:1455/auth/callback
  （设备码不可用 → 浏览器回调；回调端口被占用 → 粘贴网址）
- 聊天：chatgpt.com/backend-api/codex/responses（SSE），chatgpt-account-id 从 access JWT 取

只依赖 Python 标准库。
"""
from __future__ import annotations

import base64
import binascii
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import account_core

ID = "chatgpt"
NAME = "ChatGPT"

# 模型 id 来自 pi-ai 的 openai-codex 模型目录（providers/data/openai-codex.json）。
MODELS = [
    "gpt-6.1-sol",
    "gpt-6.1-terra",
    "gpt-6-sol",
    "gpt-6-astra",
    "gpt-6-luna",
    "gpt-5.6-sol",
    "gpt-5.6-luna",
    "gpt-5.6-terra",
]
DEFAULT_MODEL = MODELS[0]

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTH_BASE_URL = "https://auth.openai.com"
AUTHORIZE_URL = f"{AUTH_BASE_URL}/oauth/authorize"
TOKEN_URL = f"{AUTH_BASE_URL}/oauth/token"
CALLBACK_HOST = "127.0.0.1"
CALLBACK_PORT = 1455
CALLBACK_PATH = "/auth/callback"
REDIRECT_URI = f"http://localhost:{CALLBACK_PORT}{CALLBACK_PATH}"

DEVICE_USER_CODE_URL = f"{AUTH_BASE_URL}/api/accounts/deviceauth/usercode"
DEVICE_TOKEN_URL = f"{AUTH_BASE_URL}/api/accounts/deviceauth/token"
DEVICE_VERIFICATION_URI = f"{AUTH_BASE_URL}/codex/device"
DEVICE_REDIRECT_URI = f"{AUTH_BASE_URL}/deviceauth/callback"

SCOPE = "openid profile email offline_access"
JWT_CLAIM_PATH = "https://api.openai.com/auth"

API_URL = "https://chatgpt.com/backend-api/codex/responses"
ORIGINATOR = "pi"

TOKEN_TIMEOUT = 30.0
CHAT_TIMEOUT = 90.0
EXPIRES_MARGIN_SECONDS = 60

# 画图（ChatGPT OAuth 账号额度，Codex 图片接口）
IMAGES_URL = "https://chatgpt.com/backend-api/codex/images/generations"
IMAGE_MODEL = "gpt-image-2"
IMAGE_TIMEOUT = 120.0
IMAGE_MAX_BYTES = 20 * 1024 * 1024
IMAGE_ERROR_MAP = {
    401: ("expired", None),
    403: ("expired", None),
    429: ("generic", "画图请求太频繁或额度用完了，稍后再试"),
}
DEVICE_DEFAULT_INTERVAL = 5.0
DEVICE_MIN_INTERVAL = 1.0


class _DeviceUnavailable(Exception):
    """设备码流程不可用（接口不存在/被拒），改走浏览器登录。"""


# ---------------------------------------------------------------- 记录工具


def _account_id(token: str) -> str:
    payload = account_core.decode_jwt(token) or {}
    auth = payload.get(JWT_CLAIM_PATH)
    if isinstance(auth, dict):
        value = auth.get("chatgpt_account_id")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _email(token: str) -> str:
    payload = account_core.decode_jwt(token) or {}
    for key in ("email", "preferred_username"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    auth = payload.get(JWT_CLAIM_PATH)
    if isinstance(auth, dict):
        for key in ("email", "preferred_username"):
            value = auth.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _record_from_token(data: dict, old_refresh: str = "") -> dict:
    access = str(data.get("access_token") or "").strip()
    if not access:
        raise account_core.AccountError("generic", "登录服务没有返回有效结果，再试一次")
    if not isinstance(data.get("expires_in"), (int, float)):
        raise account_core.AccountError("generic", "登录服务没有返回有效结果，再试一次")
    refresh = str(data.get("refresh_token") or "").strip() or str(old_refresh or "")
    expires_at = int(time.time()) + int(float(data.get("expires_in") or 0)) - EXPIRES_MARGIN_SECONDS
    account_id = _account_id(access)
    return {
        "kind": "oauth",
        "access": access,
        "refresh": refresh,
        "expires_at": max(expires_at, int(time.time()) + 60),
        "account": _email(access),
        "extra": {"chatgpt_account_id": account_id} if account_id else {},
    }


def _parse_interval(value) -> float:
    try:
        number = float(str(value).strip()) if isinstance(value, str) else float(value)
    except (TypeError, ValueError):
        number = DEVICE_DEFAULT_INTERVAL
    if number != number or number < 0:  # NaN / 负数
        number = DEVICE_DEFAULT_INTERVAL
    return max(DEVICE_MIN_INTERVAL, number)


def _sleep(seconds: float, attempt) -> None:
    deadline = time.time() + max(0.0, float(seconds))
    while True:
        attempt.check_cancelled()
        left = deadline - time.time()
        if left <= 0:
            return
        time.sleep(min(0.2, left))


# ---------------------------------------------------------------- 设备码流程


def _device_request() -> dict:
    try:
        data = account_core.http_json(
            DEVICE_USER_CODE_URL,
            {"client_id": CLIENT_ID},
            timeout=TOKEN_TIMEOUT,
            label="ChatGPT",
            error_map={
                403: ("generic", None),
                404: ("generic", None),
            },
        )
    except account_core.AccountError as e:
        raise _DeviceUnavailable(e.message) from e
    device_id = str(data.get("device_auth_id") or "").strip()
    user_code = str(data.get("user_code") or "").strip()
    if not device_id or not user_code:
        raise _DeviceUnavailable("设备码接口没有返回有效结果")
    return {
        "device_auth_id": device_id,
        "user_code": user_code,
        "interval": _parse_interval(data.get("interval")),
    }


def _device_error_code(body: str) -> str:
    try:
        parsed = json.loads(body)
    except Exception:
        return ""
    if not isinstance(parsed, dict):
        return ""
    error = parsed.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        return code if isinstance(code, str) else ""
    if isinstance(error, str):
        return error
    return ""


def _device_poll_once(device_id: str, user_code: str):
    """返回 {"code","verifier"} / None（继续轮询）/ "slow_down"。"""
    payload = json.dumps(
        {"device_auth_id": device_id, "user_code": user_code}, ensure_ascii=False
    ).encode("utf-8")
    request = urllib.request.Request(
        DEVICE_TOKEN_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": account_core.BROWSER_USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TOKEN_TIMEOUT) as response:
            raw = response.read()
    except urllib.error.HTTPError as e:
        if e.code in (403, 404):
            return None
        body = ""
        try:
            body = e.read(400).decode("utf-8", "replace")
        except Exception:
            body = ""
        code = _device_error_code(body)
        if code in ("deviceauth_authorization_pending", "authorization_pending"):
            return None
        if code == "slow_down" or e.code == 429:
            return "slow_down"
        raise account_core.AccountError(
            "generic", "ChatGPT 登录没有完成，再试一次"
        ) from e
    except TimeoutError as e:
        raise account_core.AccountError("timeout") from e
    except urllib.error.URLError as e:
        raise account_core.AccountError("network") from e
    except OSError as e:
        raise account_core.AccountError("network") from e
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except Exception as e:
        raise account_core.AccountError(
            "generic", "ChatGPT 返回的内容看不懂，再试一次"
        ) from e
    if isinstance(data, dict):
        code = str(data.get("authorization_code") or "").strip()
        verifier = str(data.get("code_verifier") or "").strip()
        if code and verifier:
            return {"code": code, "verifier": verifier}
    return None


def _begin_device(attempt) -> dict:
    device = _device_request()
    attempt.notify_device(
        DEVICE_VERIFICATION_URI,
        device["user_code"],
        "在打开的网页里输入下面的代码",
    )
    interval = device["interval"]
    while True:
        attempt.check_cancelled()
        if time.time() >= attempt.deadline:
            raise account_core.AccountError("timeout")
        result = _device_poll_once(device["device_auth_id"], device["user_code"])
        if isinstance(result, dict):
            attempt.set_phase("starting", "正在完成登录…")
            data = account_core.http_json(
                TOKEN_URL,
                {
                    "grant_type": "authorization_code",
                    "client_id": CLIENT_ID,
                    "code": result["code"],
                    "code_verifier": result["verifier"],
                    "redirect_uri": DEVICE_REDIRECT_URI,
                },
                form=True,
                timeout=TOKEN_TIMEOUT,
                label="ChatGPT",
                secrets_=(result["code"], result["verifier"]),
            )
            return _record_from_token(data)
        if result == "slow_down":
            interval = min(interval + 5.0, 60.0)
        left = attempt.remaining()
        if left <= 0:
            raise account_core.AccountError("timeout")
        _sleep(min(interval, left), attempt)


# ---------------------------------------------------------------- 浏览器流程


def _begin_browser(attempt) -> dict:
    verifier, challenge = account_core.generate_pkce()
    state = account_core.random_state()

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
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": redirect_uri,
            "scope": SCOPE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
            "originator": ORIGINATOR,
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
        else:
            attempt.notify_url(auth_url, "在浏览器里完成登录，然后回到这里粘贴地址")
            pasted = attempt.prompt(
                REDIRECT_URI,
                "把浏览器地址栏里的完整网址粘贴到这里（或授权码）",
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
                "code_verifier": verifier,
                "redirect_uri": redirect_uri,
            },
            form=True,
            timeout=TOKEN_TIMEOUT,
            label="ChatGPT",
            secrets_=(code, verifier),
        )
        return _record_from_token(data)
    finally:
        if server is not None:
            server.close()


def begin(attempt) -> dict:
    """优先设备码（无需回调端口）；不可用时走浏览器登录，端口被占再降级粘贴。"""
    try:
        return _begin_device(attempt)
    except _DeviceUnavailable:
        return _begin_browser(attempt)


# ---------------------------------------------------------------- 画图（账号额度）


def _image_headers(access: str, account_id: str, accept: str = "application/json") -> dict:
    return {
        "Authorization": f"Bearer {access}",
        "ChatGPT-Account-ID": account_id,
        "accept": accept,
        "content-type": "application/json",
        "originator": ORIGINATOR,
        "User-Agent": account_core.BROWSER_USER_AGENT,
    }


def _looks_like_image(data: bytes) -> bool:
    if not data or len(data) < 12:
        return False
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return True
    if data.startswith(b"\xff\xd8\xff"):
        return True
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return True
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True
    if data.startswith(b"BM"):
        return True
    return False


def _decode_image_b64(value) -> bytes:
    """解码图片 base64（兼容 data: 前缀/换行）；失败抛 AccountError。

    b64_json 按接口约定就是图片数据，这里只做 base64 解码；图片头校验放在
    url 下载路径（_download_generated_image）里。
    """
    text = str(value or "").strip()
    if text.startswith("data:"):
        comma = text.find(",")
        if comma >= 0:
            text = text[comma + 1:]
    text = re.sub(r"\s+", "", text)
    if not text:
        raise account_core.AccountError("generic", "ChatGPT 画图返回的图片数据是空的")
    text += "=" * (-len(text) % 4)
    try:
        raw = base64.b64decode(text, validate=False)
    except (binascii.Error, ValueError) as e:
        raise account_core.AccountError(
            "generic", "ChatGPT 画图返回的图片数据看不懂"
        ) from e
    if not raw:
        raise account_core.AccountError("generic", "ChatGPT 画图返回的图片数据是空的")
    return raw


def _download_generated_image(url: str, timeout: float) -> bytes:
    """下载接口返回的图片 URL（浏览器 UA，上限 20MB，校验图片头）。"""
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": account_core.BROWSER_USER_AGENT,
            "Accept": "image/*,*/*;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=float(timeout)) as response:
            length = response.headers.get("Content-Length")
            if length and str(length).strip().isdigit():
                if int(length) > IMAGE_MAX_BYTES:
                    raise account_core.AccountError("generic", "生成的图片太大了（超过 20MB）")
            raw = response.read(IMAGE_MAX_BYTES + 1)
    except account_core.AccountError:
        raise
    except urllib.error.HTTPError as e:
        raise account_core.AccountError(
            "generic", f"下载生成的图片失败（HTTP {int(e.code)}）"
        ) from e
    except TimeoutError as e:
        raise account_core.AccountError("timeout") from e
    except urllib.error.URLError as e:
        raise account_core.AccountError("network") from e
    except OSError as e:
        raise account_core.AccountError("network") from e
    if len(raw) > IMAGE_MAX_BYTES:
        raise account_core.AccountError("generic", "生成的图片太大了（超过 20MB）")
    if not raw:
        raise account_core.AccountError("generic", "下载到的图片是空的")
    if not _looks_like_image(raw):
        raise account_core.AccountError("generic", "下载到的内容不是有效图片")
    return raw


def _images_from_payload(data: dict, timeout: float) -> list[bytes]:
    """从 images/generations 返回里取图片：data[].b64_json 或 data[].url。"""
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    images: list[bytes] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        b64 = item.get("b64_json") or item.get("b64")
        if isinstance(b64, str) and b64.strip():
            try:
                images.append(_decode_image_b64(b64))
            except account_core.AccountError:
                continue
            continue
        url = item.get("url")
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            try:
                images.append(_download_generated_image(url, timeout))
            except account_core.AccountError:
                continue
    return images


def _generate_images_via_responses(
    prompt: str, size: str, timeout: float, access: str, account_id: str
) -> list[bytes]:
    """兜底路径：responses + image_generation 工具，SSE 的 item.result 是 base64。"""
    payload = {
        "model": DEFAULT_MODEL,
        "store": False,
        "stream": True,
        "instructions": "Generate the image the user asks for.",
        "input": [
            {"role": "user", "content": [{"type": "input_text", "text": prompt}]}
        ],
        "tools": [
            {"type": "image_generation", "output_format": "png", "size": size}
        ],
        "tool_choice": "auto",
    }
    events = account_core.post_sse(
        API_URL,
        payload,
        headers=_image_headers(access, account_id, accept="text/event-stream"),
        timeout=float(timeout),
        label="ChatGPT 画图",
        error_map=IMAGE_ERROR_MAP,
        secrets_=(access,),
    )
    images: list[bytes] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get("type") != "response.output_item.done":
            continue
        item = event.get("item")
        if not isinstance(item, dict) or item.get("type") != "image_generation_call":
            continue
        result = item.get("result")
        if isinstance(result, str) and result.strip():
            try:
                images.append(_decode_image_b64(result))
            except account_core.AccountError:
                continue
    return images


def generate_images(
    record: dict,
    prompt: str,
    size: str = "1024x1024",
    model: str | None = None,
    timeout: float = IMAGE_TIMEOUT,
) -> list[bytes]:
    """用 ChatGPT 账号额度画图，返回图片字节列表；失败抛 AccountError（中文）。

    主路径：POST /backend-api/codex/images/generations（model 默认 gpt-image-2）。
    兜底：/backend-api/codex/responses + image_generation 工具（SSE）。
    """
    record = record if isinstance(record, dict) else {}
    access = str(record.get("access") or "").strip()
    if not access:
        raise account_core.AccountError("expired")
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
    account_id = str(extra.get("chatgpt_account_id") or "").strip() or _account_id(access)
    if not account_id:
        raise account_core.AccountError("generic", "账号信息不完整，重新连接一次")
    prompt_text = str(prompt or "").strip()
    if not prompt_text:
        raise account_core.AccountError("generic", "画图描述是空的")
    model_name = str(model or "").strip() or IMAGE_MODEL
    size_text = str(size or "").strip() or "1024x1024"
    try:
        limit = float(timeout or IMAGE_TIMEOUT)
    except (TypeError, ValueError):
        limit = IMAGE_TIMEOUT
    limit = max(5.0, min(600.0, limit))

    primary_error: account_core.AccountError | None = None
    try:
        data = account_core.http_json(
            IMAGES_URL,
            {
                "model": model_name,
                "prompt": prompt_text,
                "size": size_text,
                "n": 1,
            },
            headers=_image_headers(access, account_id),
            timeout=limit,
            label="ChatGPT 画图",
            error_map=IMAGE_ERROR_MAP,
            secrets_=(access,),
        )
        images = _images_from_payload(data, limit)
        if images:
            return images
        primary_error = account_core.AccountError(
            "generic", "ChatGPT 画图没有返回图片，稍后再试"
        )
    except account_core.AccountError as e:
        if e.code == "expired":
            try:
                account_core.mark_expired(ID)
            except Exception:
                pass
            raise
        primary_error = e

    # 兜底：responses + image_generation 工具
    try:
        fallback = _generate_images_via_responses(
            prompt_text, size_text, limit, access, account_id
        )
    except account_core.AccountError as e:
        if e.code == "expired":
            try:
                account_core.mark_expired(ID)
            except Exception:
                pass
            raise
        if primary_error is not None:
            raise primary_error from e
        raise
    if fallback:
        return fallback
    if primary_error is not None:
        raise primary_error
    raise account_core.AccountError("generic", "ChatGPT 画图没有返回图片，稍后再试")


# ---------------------------------------------------------------- 刷新 / 聊天


def refresh(record: dict) -> dict | None:
    refresh_token = str(record.get("refresh") or "").strip()
    if not refresh_token:
        return None
    try:
        data = account_core.http_json(
            TOKEN_URL,
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": CLIENT_ID,
            },
            form=True,
            timeout=TOKEN_TIMEOUT,
            label="ChatGPT",
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


def _convert_messages(messages: list) -> tuple[str, list[dict]]:
    system_parts: list[str] = []
    items: list[dict] = []
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        content = str(message.get("content") or "")
        if role == "system":
            if content.strip():
                system_parts.append(content)
        elif role == "user":
            items.append(
                {"role": "user", "content": [{"type": "input_text", "text": content}]}
            )
        elif role == "assistant":
            items.append(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": content, "annotations": []}
                    ],
                    "status": "completed",
                }
            )
    return "\n\n".join(system_parts), items


def _extract_text(events: list[dict]) -> str:
    deltas: list[str] = []
    final_parts: list[str] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        etype = str(event.get("type") or "")
        if etype == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str):
                deltas.append(delta)
        elif etype == "response.output_item.done":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "message":
                content = item.get("content")
                if isinstance(content, list):
                    text = "".join(
                        str(part.get("text") or part.get("refusal") or "")
                        for part in content
                        if isinstance(part, dict)
                    )
                    if text:
                        final_parts.append(text)
        elif etype == "response.failed":
            raise account_core.AccountError("generic", "ChatGPT 没有完成回答，再试一次")
        elif etype == "error":
            raise account_core.AccountError("generic", "ChatGPT 返回错误，再试一次")
    text = "".join(final_parts) if final_parts else "".join(deltas)
    return text.strip()


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
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
    account_id = str(extra.get("chatgpt_account_id") or "").strip() or _account_id(access)
    if not account_id:
        raise account_core.AccountError(
            "generic", "账号信息不完整，重新连接一次"
        )
    model_name = str(model or "").strip() or DEFAULT_MODEL
    instructions, items = _convert_messages(messages)
    if not items:
        raise account_core.AccountError("generic", "没有可发送的消息")

    try:
        max_out = int(max_tokens) if max_tokens else 0
    except (TypeError, ValueError):
        max_out = 0
    payload = {
        "model": model_name,
        "store": False,
        "stream": True,
        "instructions": instructions or "You are a helpful assistant.",
        "input": items,
        "text": {"verbosity": "low"},
        "include": ["reasoning.encrypted_content"],
        "tool_choice": "auto",
        "parallel_tool_calls": True,
    }
    # 账号订阅模式不限制输出：0 表示不传 max_output_tokens，用账号默认
    if max_out > 0:
        payload["max_output_tokens"] = max_out
    # Codex 的推理模型不接受 temperature（传了会 400），这里统一不发
    _ = temperature

    headers = {
        "Authorization": f"Bearer {access}",
        "chatgpt-account-id": account_id,
        "OpenAI-Beta": "responses=experimental",
        "originator": ORIGINATOR,
        "accept": "text/event-stream",
        "content-type": "application/json",
        "User-Agent": account_core.BROWSER_USER_AGENT,
    }
    error_map = {
        401: ("expired", None),
        403: ("expired", None),
        429: ("generic", "请求太频繁或额度用完了，稍后再试"),
    }
    try:
        events = account_core.post_sse(
            API_URL,
            payload,
            headers=headers,
            timeout=float(timeout or CHAT_TIMEOUT),
            label="ChatGPT",
            error_map=error_map,
            secrets_=(access,),
        )
    except account_core.AccountError as e:
        if e.code == "expired":
            account_core.mark_expired(ID)
        raise
    return _extract_text(events)


def label(record: dict) -> str | None:
    account = str(record.get("account") or "").strip()
    if account:
        return account
    email = _email(str(record.get("access") or ""))
    return email or None


# ---------------------------------------------------------------- 识图（账号额度）


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
    record: dict,
    model: str,
    images: list,
    prompt: str = "请描述这些图片",
    timeout: float = 60,
    max_tokens: int = 700,
) -> str:
    """用账号额度识图：Codex responses 的 input_image（data URL）+ input_text。"""
    access = str(record.get("access") or "").strip()
    if not access:
        raise account_core.AccountError("expired")
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
    account_id = str(extra.get("chatgpt_account_id") or "").strip() or _account_id(access)
    if not account_id:
        raise account_core.AccountError("generic", "账号信息不完整，重新连接一次")
    model_name = str(model or "").strip() or DEFAULT_MODEL

    prepared: list[bytes] = []
    for raw in list(images or [])[:4]:
        if not isinstance(raw, (bytes, bytearray)):
            raise account_core.AccountError("generic", "图片格式不对，再试一次")
        data = bytes(raw)
        if data:
            prepared.append(data)
    if not prepared:
        raise account_core.AccountError("generic", "没有图片可以识别")

    content: list[dict] = [{"type": "input_text", "text": str(prompt or "").strip() or "请描述这些图片"}]
    for data in prepared:
        mime = _image_mime(data)
        b64 = base64.b64encode(data).decode("ascii")
        content.append({"type": "input_image", "image_url": f"data:{mime};base64,{b64}"})

    try:
        max_out = int(max_tokens) if max_tokens else 0
    except (TypeError, ValueError):
        max_out = 0
    payload = {
        "model": model_name,
        "store": False,
        "stream": True,
        "instructions": "You are a helpful assistant that describes images.",
        "input": [{"role": "user", "content": content}],
        "text": {"verbosity": "low"},
        "include": ["reasoning.encrypted_content"],
        "tool_choice": "auto",
        "parallel_tool_calls": True,
    }
    if max_out > 0:
        payload["max_output_tokens"] = max_out

    headers = {
        "Authorization": f"Bearer {access}",
        "chatgpt-account-id": account_id,
        "OpenAI-Beta": "responses=experimental",
        "originator": ORIGINATOR,
        "accept": "text/event-stream",
        "content-type": "application/json",
        "User-Agent": account_core.BROWSER_USER_AGENT,
    }
    error_map = {
        401: ("expired", None),
        403: ("expired", None),
        429: ("generic", "请求太频繁或额度用完了，稍后再试"),
    }
    try:
        events = account_core.post_sse(
            API_URL,
            payload,
            headers=headers,
            timeout=float(timeout or CHAT_TIMEOUT),
            label="ChatGPT",
            error_map=error_map,
            secrets_=(access,),
        )
    except account_core.AccountError as e:
        if e.code == "expired":
            account_core.mark_expired(ID)
        raise
    text = _extract_text(events)
    if not text:
        raise account_core.AccountError("generic", "ChatGPT 没有返回图片描述，再试一次")
    return text
