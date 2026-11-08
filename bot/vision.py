"""视觉识别：调用 OpenAI 兼容的多模态接口（image_url / base64 data URI）。

兼容 DeepSeek、通义千问 VL、智谱 GLM-4V、OpenAI、one-api 中转等。
"""
from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.request


class VisionError(RuntimeError):
    pass


def _guess_mime(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _downscale(data: bytes, max_side: int = 1280) -> bytes:
    """尽量缩小图片（PIL 可用时），减小上传体积和 token。"""
    if max_side <= 0:
        return data
    try:
        import io

        from PIL import Image

        im = Image.open(io.BytesIO(data))
        im = im.convert("RGB")
        if max(im.size) <= max_side:
            return data
        im.thumbnail((max_side, max_side))
        out = io.BytesIO()
        im.save(out, format="JPEG", quality=82)
        return out.getvalue()
    except Exception:
        return data


def describe_images(
    images: list[bytes],
    *,
    base_url: str,
    api_key: str,
    model: str,
    prompt: str = "请用中文客观描述这张图片，重点提取其中的文字信息。",
    timeout: float = 60.0,
    max_tokens: int = 700,
    max_side: int = 1280,
    max_chars: int = 800,
) -> str:
    """把 1~N 张图交给视觉模型，返回文字描述。失败抛 VisionError。"""
    if not images:
        return ""
    if not base_url or not api_key or not model:
        raise VisionError("视觉模型未配置（vision.base_url / api_key / model 为空）")

    content = [{"type": "text", "text": prompt}]
    for data in images[:4]:
        small = _downscale(data, max_side)
        mime = _guess_mime(small)
        b64 = base64.b64encode(small).decode("ascii")
        content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": int(max_tokens),
        "temperature": 0.2,
        "stream": False,
    }
    req = urllib.request.Request(
        str(base_url).rstrip("/") + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        raise VisionError(f"视觉接口 HTTP {e.code}: {body}") from e
    except Exception as e:
        raise VisionError(f"视觉接口调用失败: {e}") from e

    try:
        text = str(data["choices"][0]["message"]["content"]).strip()
    except Exception as e:
        raise VisionError(f"视觉接口返回格式异常: {str(data)[:200]}") from e
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[:max_chars] + ("…" if len(text) > max_chars else "")
