"""文生图模块：OpenAI 兼容接口 + Gemini 原生接口（纯标准库，PIL 仅用于压缩）。

对外只暴露一个函数::

    generate(provider="openai", base_url=..., api_key=..., model=...,
             prompt=..., size="1024x1024", timeout=120.0, n=1) -> list[bytes]

支持的 provider（名字大小写不敏感）：

1. ``openai`` 兼容（OpenAI / DALL·E / gpt-image-1 / one-api 等中转站）::

       POST {base}/v1/images/generations
       Authorization: Bearer <key>
       {"model": ..., "prompt": ..., "n": ..., "size": ...,
        "response_format": "b64_json"}

   - base_url 已以 ``/v1`` 结尾时不重复补 ``/v1``；
   - 返回 400 且错误提示不认 ``response_format`` 时，去掉该字段重试一次；
   - 优先解析 ``data[i].b64_json``；没有 b64 时下载 ``data[i].url``（≤20MB）。

2. ``gemini``（Google Generative Language API）::

       POST {base}/v1beta/models/{model}:generateContent?key=<key>
       {"contents": [{"role": "user", "parts": [{"text": ...}]}],
        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]}}

   - base_url 已含 ``/v1beta`` 时只补 ``/models/...``；
   - 解析 ``candidates[0].content.parts`` 里的 ``inlineData`` / ``inline_data``，
     收集所有图片；没有图片时把模型文字回复截断后带进错误信息。

失败（HTTP 4xx/5xx、超时、网络、JSON 解析、无图片等）一律抛
:class:`ImageGenError`，消息简短并含 HTTP 状态码，且不会泄露 api_key。

3. 账号模式（``generate_with_account``）：用已连接的 AI 账号额度画图，
   目前只支持 ChatGPT 账号（走 Codex 图片接口，见 ``accounts_chatgpt.generate_images``）；
   其它账号会抛 :class:`ImageGenError` 并提示填图片 API Key。

返回前会在 PIL 可用时做一次轻量压缩：最长边超过 1600 的图片缩到 1600
（保持比例）并转 JPEG(quality=88)，以减小 QQ 上传体积；压缩失败或未超限
则原样返回。
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import math
import re
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

__all__ = [
    "ImageGenError",
    "generate",
    "generate_with_account",
    "generate_for_ai",
    "resolve_image_mode",
    "account_image_model",
    "DEFAULT_IMAGE_BASE_URL",
    "DEFAULT_IMAGE_MODEL",
    "ACCOUNT_IMAGE_MODEL",
    "ERROR_IMAGE_CONFIG",
    "ERROR_IMAGE_ACCOUNT",
    "looks_like_avatar_request",
    "avatar_prompt",
    "avatar_reference_path",
    "generate_with_reference",
]

USER_AGENT = "qqbot-imagegen/1.0"

_MAX_DOWNLOAD = 20 * 1024 * 1024  # 从 url 下载图片的上限：20MB
_MAX_IMAGE_SIDE = 1600  # 压缩后最长边
_JPEG_QUALITY = 88
_MAX_N = 10
_MAX_TIMEOUT = 600.0
_DEFAULT_TIMEOUT = 120.0
_ERROR_LIMIT = 300  # 错误信息里保留的响应体长度

_PROVIDER_ALIASES = {
    "openai": "openai",
    "openai-compatible": "openai",
    "openai_compatible": "openai",
    "oai": "openai",
    "dalle": "openai",
    "dall-e": "openai",
    "dall_e": "openai",
    "gemini": "gemini",
    "google": "gemini",
    "google-gemini": "gemini",
    "google_gemini": "gemini",
    "googleai": "gemini",
    "google_ai": "gemini",
}


class ImageGenError(RuntimeError):
    """文生图失败（配置错误、网络/超时、HTTP 错误、返回格式异常、无图片）。"""


# 画图配置（与主对话 ai_chat 解耦）
DEFAULT_IMAGE_BASE_URL = "https://api.openai.com/v1"
DEFAULT_IMAGE_MODEL = "gpt-image-1"
ACCOUNT_IMAGE_MODEL = "gpt-image-2"
ERROR_IMAGE_CONFIG = "画图需要：在画图设置里连接 ChatGPT 账号，或单独填图片 API Key"
ERROR_IMAGE_ACCOUNT = "画图只支持 ChatGPT 账号；其他账号请填图片 API Key"


def _truthy(value) -> bool:
    if value is True:
        return True
    if value is False or value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def account_image_model(image_cfg: dict | None) -> str:
    """账号画图的模型：只接受 gpt-image-2 系（Codex 图片后端），其它值回退默认。

    配置里遗留的 gpt-image-1 / dall-e 等 API 模型不能用于账号通道，否则会 404。
    """
    model = str((image_cfg or {}).get("model") or "").strip().lower()
    # 账号通道只认 gpt-image-N（可带 -suffix），例如 gpt-image-2、gpt-image-3；
    # 配置里遗留的 gpt-image-1 / dall-e / 手输跑偏的名字（含点号等）一律回退 gpt-image-2，
    # 否则会拿无效 model 调 Codex 图片接口得到 404。
    if not re.fullmatch(r"gpt-image-[0-9]+(?:-[a-z0-9]+)*", model):
        return ACCOUNT_IMAGE_MODEL
    if model == "gpt-image-1":
        return ACCOUNT_IMAGE_MODEL
    return model


def resolve_image_mode(ai_cfg: dict | None, image_cfg: dict | None) -> dict:
    """画图分发决策（插件与 AI 工具共用）。

    返回：
    - {"mode": "account", "auth": "chatgpt", "model": "gpt-image-2"}
    - {"mode": "api", "api_key", "base_url", "provider", "model"}
    - {"mode": "none", "error": 中文}
    优先级：image_gen.auth=chatgpt → image_gen.api_key → image_gen.follow_ai
    （要求 ai_chat.auth 为空且有 api_key）→ 未配置错误。
    """
    ai_cfg = ai_cfg if isinstance(ai_cfg, dict) else {}
    image_cfg = image_cfg if isinstance(image_cfg, dict) else {}
    auth = str(image_cfg.get("auth") or "").strip().lower()
    if auth:
        if auth == "chatgpt":
            return {
                "mode": "account",
                "auth": "chatgpt",
                "model": account_image_model(image_cfg),
            }
        return {"mode": "none", "error": ERROR_IMAGE_ACCOUNT}

    image_key = str(image_cfg.get("api_key") or "").strip()
    if image_key:
        return {
            "mode": "api",
            "api_key": image_key,
            "provider": str(image_cfg.get("provider") or "openai"),
            "base_url": str(image_cfg.get("base_url") or "").strip()
            or DEFAULT_IMAGE_BASE_URL,
            "model": str(image_cfg.get("model") or "").strip()
            or DEFAULT_IMAGE_MODEL,
        }

    if _truthy(image_cfg.get("follow_ai")):
        ai_auth = str(ai_cfg.get("auth") or "").strip()
        ai_key = str(ai_cfg.get("api_key") or "").strip()
        if not ai_auth and ai_key:
            return {
                "mode": "api",
                "api_key": ai_key,
                "provider": str(image_cfg.get("provider") or ai_cfg.get("provider") or "openai"),
                "base_url": str(image_cfg.get("base_url") or ai_cfg.get("base_url") or "").strip()
                or DEFAULT_IMAGE_BASE_URL,
                "model": str(image_cfg.get("model") or "").strip()
                or DEFAULT_IMAGE_MODEL,
            }

    return {"mode": "none", "error": ERROR_IMAGE_CONFIG}


# ---------------------------------------------------------------------------
# 小工具：文本截断 / 脱敏 / 图片识别 / base64 解码 / 下载 / 压缩
# ---------------------------------------------------------------------------
def _short(text: object, limit: int = _ERROR_LIMIT) -> str:
    """压成单行并截断，避免错误信息过长。"""
    return re.sub(r"\s+", " ", str(text)).strip()[:limit]


def _redact(text: object, api_key: str) -> str:
    """从错误信息里抹掉 api_key（包括 URL 里的 key=... 形式）。"""
    result = str(text)
    key = str(api_key or "")
    if key:
        result = result.replace(key, "***")
    result = re.sub(
        r"(?i)((?:key|api_key|api-key|token|authorization)\s*[=:]\s*)[^\s&,;\"']+",
        r"\1***",
        result,
    )
    result = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+", r"\1***", result)
    return result


def _safe_error(exc: Exception, api_key: str) -> str:
    """把任意异常转成不含 key 的简短错误描述。"""
    message = str(exc) or exc.__class__.__name__
    return _short(_redact(f"{exc.__class__.__name__}: {message}", api_key), _ERROR_LIMIT)


def _looks_like_image(data: bytes) -> bool:
    """按文件头判断是不是常见位图（PNG/JPEG/GIF/WebP/BMP）。"""
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


def _decode_b64_image(value: object) -> bytes:
    """解码 b64_json / inlineData.data，兼容带 data: 前缀和换行的写法。"""
    text = str(value or "").strip()
    if text.startswith("data:"):
        comma = text.find(",")
        if comma >= 0:
            text = text[comma + 1 :]
    text = re.sub(r"\s+", "", text)
    if not text:
        raise ImageGenError("接口返回的图片 base64 为空")
    text += "=" * (-len(text) % 4)  # 补齐 padding
    try:
        data = base64.b64decode(text, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise ImageGenError("接口返回的图片 base64 解码失败") from exc
    if not data:
        raise ImageGenError("接口返回的图片 base64 解码为空")
    if not _looks_like_image(data):
        raise ImageGenError("接口返回的数据不是有效图片")
    return data


def _download_image(url: str, timeout: float, api_key: str) -> bytes:
    """下载图片 URL，最多 20MB；失败抛 ImageGenError。"""
    if not url.lower().startswith(("http://", "https://")):
        raise ImageGenError("图片 URL 不是 http(s) 地址")
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "image/*,*/*;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            length = response.headers.get("Content-Length")
            if length and str(length).strip().isdigit():
                if int(length) > _MAX_DOWNLOAD:
                    raise ImageGenError("图片超过 20MB 上限，已放弃下载")
            data = response.read(_MAX_DOWNLOAD + 1)
    except ImageGenError:
        raise
    except urllib.error.HTTPError as exc:
        raise ImageGenError(f"下载图片 HTTP {exc.code}") from exc
    except Exception as exc:
        raise ImageGenError(f"下载图片失败: {_safe_error(exc, api_key)}") from exc
    if len(data) > _MAX_DOWNLOAD:
        raise ImageGenError("图片超过 20MB 上限，已放弃下载")
    if not data:
        raise ImageGenError("下载到的图片为空")
    if not _looks_like_image(data):
        raise ImageGenError("下载内容不是有效图片")
    return data


def _compress_image(data: bytes) -> bytes:
    """PIL 可用时压缩：最长边 >1600 缩到 1600 并转 JPEG(88)；失败原样返回。"""
    try:
        from PIL import Image  # 延迟导入：没装 PIL 也不影响模块导入

        image = Image.open(io.BytesIO(data))
        image.load()
        if max(image.size) <= _MAX_IMAGE_SIDE:
            return data
        resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
        image = image.convert("RGB")
        image.thumbnail((_MAX_IMAGE_SIDE, _MAX_IMAGE_SIDE), resampling)
        out = io.BytesIO()
        image.save(out, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
        compressed = out.getvalue()
        return compressed or data
    except Exception:
        return data


# ---------------------------------------------------------------------------
# URL 拼接
# ---------------------------------------------------------------------------
def _require_base(base_url: object) -> str:
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        raise ImageGenError("base_url 为空")
    if not base.lower().startswith(("http://", "https://")):
        raise ImageGenError("base_url 必须是 http:// 或 https:// 地址")
    return base


def _openai_endpoint(base: str) -> str:
    """{base}/v1/images/generations；base 已含 /v1 或完整端点时不重复拼接。"""
    if base.endswith("/images/generations"):
        return base
    if base.endswith("/v1"):
        return base + "/images/generations"
    return base + "/v1/images/generations"


def _gemini_endpoint(base: str, model: str) -> str:
    """{base}/v1beta/models/{model}:generateContent；base 已含 /v1beta 时只补 /models。"""
    name = str(model or "").strip()
    if name.startswith("models/"):
        name = name[len("models/") :]
    name = urllib.parse.quote(name, safe="._-")
    prefix = base if "/v1beta" in base else base + "/v1beta"
    return f"{prefix}/models/{name}:generateContent"


# ---------------------------------------------------------------------------
# HTTP / JSON
# ---------------------------------------------------------------------------
def _post(
    url: str,
    payload: dict,
    headers: dict[str, str],
    timeout: float,
    api_key: str,
) -> tuple[int, bytes]:
    """POST JSON，返回 (HTTP 状态码, 响应体)；网络/超时错误转 ImageGenError。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(getattr(response, "status", 200)), response.read()
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
        except Exception:
            raw = b""
        return int(exc.code), raw
    except Exception as exc:
        raise ImageGenError(f"请求图片接口失败: {_safe_error(exc, api_key)}") from exc


def _parse_json(raw: bytes, label: str, status: int) -> dict:
    try:
        obj = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ImageGenError(f"{label} 返回不是合法 JSON（HTTP {status}）") from exc
    if not isinstance(obj, dict):
        raise ImageGenError(f"{label} 返回格式异常（HTTP {status}）")
    return obj


def _http_error_text(label: str, status: int, raw: bytes, api_key: str) -> str:
    """构造简短错误信息：含 HTTP 状态码 + 响应里的 error.message，且已脱敏。"""
    try:
        decoded = raw.decode("utf-8", "replace")
    except Exception:
        decoded = ""
    message = ""
    try:
        obj = json.loads(decoded)
        if isinstance(obj, dict):
            error = obj.get("error")
            if isinstance(error, dict):
                message = str(error.get("message") or error.get("code") or "")
            elif isinstance(error, str):
                message = error
            if not message:
                message = str(obj.get("message") or obj.get("msg") or "")
    except Exception:
        pass
    detail = _short(_redact(message, api_key), _ERROR_LIMIT)
    if not detail:
        detail = _short(_redact(decoded, api_key), _ERROR_LIMIT)
    return f"{label} HTTP {status}" + (f": {detail}" if detail else "")


# ---------------------------------------------------------------------------
# OpenAI 兼容 provider
# ---------------------------------------------------------------------------
def _generate_openai(
    base: str,
    api_key: str,
    model: str,
    prompt: str,
    size: str,
    timeout: float,
    limit: int,
) -> list[bytes]:
    endpoint = _openai_endpoint(base)
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
        "Authorization": f"Bearer {api_key}",
    }
    payload = {
        "model": model,
        "prompt": prompt,
        "n": limit,
        "size": size,
        "response_format": "b64_json",
    }
    status, raw = _post(endpoint, payload, headers, timeout, api_key)

    # 部分模型（如 gpt-image-1）不认 response_format：去掉后重试一次。
    if status == 400 and "response_format" in raw.decode("utf-8", "replace").lower():
        payload.pop("response_format", None)
        status, raw = _post(endpoint, payload, headers, timeout, api_key)

    if not 200 <= status < 300:
        raise ImageGenError(_http_error_text("OpenAI 图片接口", status, raw, api_key))

    obj = _parse_json(raw, "OpenAI 图片接口", status)
    items = obj.get("data")
    if not isinstance(items, list) or not items:
        raise ImageGenError("OpenAI 图片接口未返回图片（data 为空）")

    images: list[bytes] = []
    for item in items:
        if len(images) >= limit:
            break
        if not isinstance(item, dict):
            continue
        encoded = item.get("b64_json") or item.get("b64Json") or item.get("b64")
        if isinstance(encoded, str) and encoded.strip():
            images.append(_decode_b64_image(encoded))
            continue
        link = item.get("url")
        if isinstance(link, str) and link.strip():
            images.append(_download_image(link.strip(), timeout, api_key))
    if not images:
        raise ImageGenError("OpenAI 图片接口未返回图片（没有 b64_json 或 url）")
    return images[:limit]


# ---------------------------------------------------------------------------
# Gemini provider
# ---------------------------------------------------------------------------
def _generate_gemini(
    base: str,
    api_key: str,
    model: str,
    prompt: str,
    timeout: float,
    limit: int,
) -> list[bytes]:
    query = urllib.parse.urlencode({"key": api_key})
    url = _gemini_endpoint(base, model) + "?" + query
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
    }
    status, raw = _post(url, payload, headers, timeout, api_key)

    # 个别中转站/旧模型不认 responseModalities：去掉 generationConfig 重试一次。
    unsupported = re.search(
        r"(?i)response_?modalities|generation_?config",
        raw.decode("utf-8", "replace"),
    )
    if status == 400 and unsupported:
        payload.pop("generationConfig", None)
        status, raw = _post(url, payload, headers, timeout, api_key)

    if not 200 <= status < 300:
        raise ImageGenError(_http_error_text("Gemini 图片接口", status, raw, api_key))

    obj = _parse_json(raw, "Gemini 图片接口", status)
    candidates = obj.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        feedback = obj.get("promptFeedback") or obj.get("prompt_feedback")
        reason = ""
        if isinstance(feedback, dict):
            reason = feedback.get("blockReason") or feedback.get("block_reason") or ""
        raise ImageGenError(
            "Gemini 未返回 candidates"
            + (f"（{_short(_redact(reason, api_key), 100)}）" if reason else "")
        )

    first = candidates[0] if isinstance(candidates[0], dict) else {}
    content = first.get("content")
    parts = content.get("parts") if isinstance(content, dict) else None
    if not isinstance(parts, list) or not parts:
        raise ImageGenError("Gemini 返回中没有 candidates[0].content.parts")

    images: list[bytes] = []
    texts: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        inline = part.get("inlineData") or part.get("inline_data")
        if isinstance(inline, dict):
            encoded = inline.get("data")
            if isinstance(encoded, str) and encoded.strip():
                images.append(_decode_b64_image(encoded))
                if len(images) >= limit:
                    break
                continue
        text = part.get("text")
        if isinstance(text, str) and text.strip():
            texts.append(text.strip())

    if not images:
        detail = _short(_redact(" ".join(texts), api_key), _ERROR_LIMIT)
        if not detail:
            finish = first.get("finishReason") or first.get("finish_reason") or ""
            detail = f"finishReason={finish}" if finish else "响应里没有 inlineData 图片"
        raise ImageGenError(f"Gemini 未返回图片：{detail}")
    return images[:limit]


# ---------------------------------------------------------------------------
# 参数归一化
# ---------------------------------------------------------------------------
def _normalize_provider(provider: object) -> str:
    name = str(provider or "").strip().lower()
    if not name:
        return "openai"
    resolved = _PROVIDER_ALIASES.get(name)
    if resolved is None:
        raise ImageGenError(f"不支持的 provider：{_short(name, 40)}（可选 openai / gemini）")
    return resolved


def _clamp_n(n: object) -> int:
    try:
        value = int(n)
    except (TypeError, ValueError):
        value = 1
    return max(1, min(_MAX_N, value))


def _clamp_timeout(timeout: object) -> float:
    try:
        seconds = float(timeout)
    except (TypeError, ValueError):
        seconds = _DEFAULT_TIMEOUT
    if not math.isfinite(seconds) or seconds <= 0:
        seconds = _DEFAULT_TIMEOUT
    return max(0.1, min(_MAX_TIMEOUT, seconds))


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------
def generate(
    *,
    provider: str = "openai",
    base_url: str,
    api_key: str,
    model: str,
    prompt: str,
    size: str = "1024x1024",
    timeout: float = 120.0,
    n: int = 1,
) -> list[bytes]:
    """根据 prompt 生成图片，返回 1~n 张图片的原始字节（PNG/JPEG 均可）。

    Args:
        provider: ``"openai"``（兼容 OpenAI / DALL·E / gpt-image-1 / one-api 中转）
            或 ``"gemini"``；大小写不敏感。
        base_url: 接口根地址，例如 ``https://api.openai.com``、
            ``https://your-relay.example.com/v1``、
            ``https://generativelanguage.googleapis.com``。
        api_key: 接口密钥；只放在 Header 或 Gemini 的 query 里，不会写日志。
        model: 模型名，例如 ``dall-e-3``、``gpt-image-1``、
            ``gemini-2.5-flash-image-preview``。
        prompt: 画面描述；不能为空。
        size: 图片尺寸，OpenAI 兼容接口原样传给服务端；Gemini 忽略该参数。
        timeout: 单次 HTTP 请求超时秒数（默认 120，最大 600）。
        n: 期望张数（1~10，默认 1）；服务端返回不足时返回实际拿到的张数。

    Returns:
        图片原始字节列表，按服务端返回顺序排列，1~n 张；已做可选压缩。

    Raises:
        ImageGenError: 配置错误、网络/超时、HTTP 4xx/5xx、JSON 解析失败、
            返回里没有图片或图片数据非法；消息简短且含 HTTP 状态码（如有）。
    """
    name = _normalize_provider(provider)
    base = _require_base(base_url)
    key = str(api_key or "").strip()
    model_name = str(model or "").strip()
    prompt_text = "" if prompt is None else str(prompt)
    if not prompt_text.strip():
        raise ImageGenError("prompt 为空")
    if not key:
        raise ImageGenError(f"{name} 的 api_key 为空")
    if not model_name:
        raise ImageGenError(f"{name} 的 model 为空")
    limit = _clamp_n(n)
    seconds = _clamp_timeout(timeout)
    size_text = str(size or "").strip() or "1024x1024"

    if name == "openai":
        images = _generate_openai(base, key, model_name, prompt_text, size_text, seconds, limit)
    else:
        images = _generate_gemini(base, key, model_name, prompt_text, seconds, limit)
    if not images:
        raise ImageGenError(f"{name} 未返回图片")
    return [_compress_image(image) for image in images]


def _chatgpt_account_images(
    record,
    prompt: str,
    size: str,
    timeout: float,
    model: str | None = None,
) -> list[bytes]:
    """账号额度画图（ChatGPT）：ensure_access + accounts_chatgpt.generate_images。"""
    import account_core  # 延迟导入：imagegen 平时不依赖账号模块

    if record is None:
        try:
            record = account_core.ensure_access("chatgpt")
        except account_core.AccountError as e:
            raise ImageGenError(e.message) from e
        except Exception:
            raise ImageGenError("读取 ChatGPT 账号失败，重新连接一次") from None
    try:
        import accounts_chatgpt

        images = accounts_chatgpt.generate_images(
            record,
            prompt,
            size=str(size or "").strip() or "1024x1024",
            model=model,
            timeout=float(timeout or 120.0),
        )
    except account_core.AccountError as e:
        raise ImageGenError(e.message) from e
    except ImageGenError:
        raise
    except Exception:
        raise ImageGenError("ChatGPT 画图失败，稍后再试") from None
    if not images:
        raise ImageGenError("ChatGPT 没有返回图片")
    return [_compress_image(image) for image in images]


def generate_with_account(
    auth: str,
    prompt: str,
    *,
    size: str = "1024x1024",
    timeout: float = 120.0,
    record: dict | None = None,
    model: str | None = None,
) -> list[bytes]:
    """用 AI 账号额度画图（目前只支持 ChatGPT 账号）。

    Args:
        auth: 账号 id，目前只支持 ``"chatgpt"``。
        prompt: 画面描述。
        size: 图片尺寸，透传给账号模块。
        timeout: 超时秒数。
        record: 可选，已读取的账号记录（不给就按 auth 调 account_core.ensure_access）。
        model: 可选模型名（默认账号接口的 gpt-image-2）。

    Returns:
        图片原始字节列表（已尝试压缩）。

    Raises:
        ImageGenError: 账号不支持 / 未连接 / 登录过期 / 网络失败 / 无图片，
            消息为中文短句，不含 token。
    """
    provider = str(auth or "").strip().lower()
    if not provider:
        raise ImageGenError("没有指定画图账号")
    if provider != "chatgpt":
        raise ImageGenError(ERROR_IMAGE_ACCOUNT)
    prompt_text = str(prompt or "").strip()
    if not prompt_text:
        raise ImageGenError("prompt 为空")
    return _chatgpt_account_images(record, prompt_text, size, timeout, model)


def generate_for_ai(
    ai_cfg: dict,
    image_cfg: dict,
    prompt: str,
    size: str = "1024x1024",
    timeout: float = 120.0,
) -> list[bytes]:
    """给 AI 工具（generate_image）用的画图入口：按画图配置分发（与主对话解耦）。

    优先级（见 :func:`resolve_image_mode`）：
    - ``image_cfg.auth == "chatgpt"`` -> 账号额度画图（model 默认 gpt-image-2）；
    - ``image_cfg.api_key`` -> 独立 API Key，base_url 空时用 OpenAI 官方；
    - ``image_cfg.follow_ai`` 且主对话 auth 为空、有 api_key -> 复用 AI 的 Key；
    - 否则抛 ImageGenError（中文），**不会**拿主对话的 base_url（如 DeepSeek）去拼图片接口。

    Raises:
        ImageGenError: 没有可用的画图配置、账号没连/过期、网络失败、无图片；
            消息为中文短句，不含 token。
    """
    ai_cfg = ai_cfg if isinstance(ai_cfg, dict) else {}
    image_cfg = image_cfg if isinstance(image_cfg, dict) else {}
    prompt_text = str(prompt or "").strip()
    if not prompt_text:
        raise ImageGenError("prompt 为空")
    size_text = str(size or "").strip() or "1024x1024"
    try:
        seconds = float(timeout or 120.0)
    except (TypeError, ValueError):
        seconds = 120.0

    decision = resolve_image_mode(ai_cfg, image_cfg)
    mode = decision.get("mode")
    if mode == "account":
        model = str(decision.get("model") or "").strip() or ACCOUNT_IMAGE_MODEL
        return _chatgpt_account_images(None, prompt_text, size_text, seconds, model)
    if mode == "api":
        return generate(
            provider=str(decision.get("provider") or "openai"),
            base_url=str(decision.get("base_url") or DEFAULT_IMAGE_BASE_URL),
            api_key=str(decision.get("api_key") or ""),
            model=str(decision.get("model") or DEFAULT_IMAGE_MODEL),
            prompt=prompt_text,
            size=size_text,
            timeout=seconds,
        )
    raise ImageGenError(str(decision.get("error") or ERROR_IMAGE_CONFIG))


# ---------------------------------------------------------------------------
# 机器人形象：请求识别 + 参考图 /images/edits（image_gen 插件与 ai_chat 共用）
# ---------------------------------------------------------------------------
AVATAR_REQUEST_RE = re.compile(
    r"(机器人(的)?(形象|头像|立绘|照片)|你的(形象|头像|立绘|照片)|"
    r"画(张|个|一个|一张|一幅)?你(自己)?(的)?(形象|头像|立绘|照片)?|"
    r"你(自己)?的?(照片|形象|头像|立绘)|自画像|头像|立绘|"
    r"(bot|机器人)\s*(形象|头像|立绘)|avatar)",
    re.I,
)


def looks_like_avatar_request(text) -> bool:
    """是不是在要「机器人自己的形象/头像/立绘/照片」这类请求。"""
    raw = str(text or "").strip()
    return bool(raw) and bool(AVATAR_REQUEST_RE.search(raw))


def avatar_prompt(ai_cfg: dict | None) -> str:
    """plugins.ai_chat.avatar.prompt（外观约束）。"""
    cfg = ai_cfg if isinstance(ai_cfg, dict) else {}
    avatar = cfg.get("avatar") if isinstance(cfg.get("avatar"), dict) else {}
    return str(avatar.get("prompt") or "").strip()


def avatar_reference_path(ai_cfg: dict | None, base_dir=None) -> Path | None:
    """avatar.reference_image 指向的本地参考图；不存在返回 None。"""
    cfg = ai_cfg if isinstance(ai_cfg, dict) else {}
    avatar = cfg.get("avatar") if isinstance(cfg.get("avatar"), dict) else {}
    reference = str(avatar.get("reference_image") or "").strip()
    if not reference:
        return None
    path = Path(reference)
    if not path.is_absolute():
        base = Path(base_dir) if base_dir is not None else Path(__file__).resolve().parent.parent
        path = base / reference
    try:
        return path if path.is_file() else None
    except OSError:
        return None


def _multipart_body(fields: dict, files: list) -> tuple[bytes, str]:
    boundary = "----qqbot" + uuid.uuid4().hex
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")
        )
    for name, filename, content_type, content in files:
        chunks.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode("utf-8")
        )
        chunks.append(bytes(content))
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), boundary


def generate_with_reference(
    ai_cfg: dict,
    image_cfg: dict,
    prompt: str,
    reference_path,
    size: str = "1024x1024",
    timeout: float = 120.0,
) -> list[bytes]:
    """OpenAI 兼容 /images/edits multipart：把参考图作为外观参考生成。

    仅在 API Key 模式 + openai provider 下可用；其它模式抛 ImageGenError，
    调用方负责回退纯文生图。
    """
    decision = resolve_image_mode(ai_cfg, image_cfg)
    if decision.get("mode") != "api":
        raise ImageGenError("当前画图方式（账号额度）不支持参考图")
    provider = _normalize_provider(decision.get("provider") or "openai")
    if provider != "openai":
        raise ImageGenError("当前画图 provider 不支持参考图")
    api_key = str(decision.get("api_key") or "")
    model = str(decision.get("model") or DEFAULT_IMAGE_MODEL)
    base = str(decision.get("base_url") or DEFAULT_IMAGE_BASE_URL)
    endpoint = _openai_endpoint(base).rsplit("/", 1)[0] + "/edits"
    prompt_text = str(prompt or "").strip()
    if not prompt_text:
        raise ImageGenError("prompt 为空")
    path = Path(reference_path)
    try:
        raw = path.read_bytes()
    except Exception as e:
        raise ImageGenError(f"读取参考图失败: {_safe_error(e, api_key)}") from None
    body, boundary = _multipart_body(
        {"model": model, "prompt": prompt_text, "size": str(size or "1024x1024")},
        [("image", path.name or "reference.png", "image/png", raw)],
    )
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=float(timeout)) as response:
            status = int(getattr(response, "status", 200))
            payload = response.read()
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
        except Exception:
            raw = b""
        raise ImageGenError(_http_error_text("参考图接口", int(exc.code), raw, api_key)) from None
    except Exception as exc:
        raise ImageGenError(f"请求参考图接口失败: {_safe_error(exc, api_key)}") from None
    if not 200 <= status < 300:
        raise ImageGenError(f"参考图接口返回 HTTP {status}")
    try:
        obj = json.loads(payload.decode("utf-8", "replace"))
    except Exception:
        raise ImageGenError("参考图接口返回的内容看不懂") from None
    items = obj.get("data") if isinstance(obj, dict) else None
    images: list[bytes] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        encoded = item.get("b64_json") or item.get("b64Json") or item.get("b64")
        if isinstance(encoded, str) and encoded.strip():
            try:
                images.append(_decode_b64_image(encoded))
            except Exception:
                continue
            continue
        link = item.get("url")
        if isinstance(link, str) and link.strip():
            try:
                images.append(_download_image(link.strip(), float(timeout), api_key))
            except Exception:
                continue
    if not images:
        raise ImageGenError("参考图接口没有返回图片")
    return images[:2]
