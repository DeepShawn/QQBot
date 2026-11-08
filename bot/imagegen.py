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
import urllib.error
import urllib.parse
import urllib.request

__all__ = ["ImageGenError", "generate"]

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
