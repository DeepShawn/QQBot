"""多模型接口适配层：OpenAI 兼容 / Anthropic Claude / Google Gemini。

只依赖 Python 标准库；PIL（Pillow）是可选增强——装了就把图片缩到最长边
≤1280 并转成 JPEG(quality 82)，没装就把原图原样上传。

对外接口（供 ai_chat 调用）::

    ProviderError
    normalize_provider(name) -> str
    chat(*, provider, base_url, api_key, model, messages,
         temperature=0.7, max_tokens=600, timeout=60.0, auth="") -> str
    chat_with_tools(*, provider, base_url, api_key, model, messages, tools,
                    temperature=0.7, max_tokens=2000, timeout=60.0) -> dict
    describe_images(*, provider, base_url, api_key, model, images,
                    prompt="...", timeout=60.0, max_tokens=700,
                    auth="") -> str

统一约定：
- messages 用 OpenAI 格式：role 只能是 system / user / assistant，content 是 str。
  本模块负责转换成各家协议：Claude 的 system 单独抽出来，Gemini 的 assistant 映射成 model。
- 所有失败都抛 ProviderError，错误信息短小（HTTP 错误只截 300 字节 body），
  不打印、不记录 api_key（Gemini 的 key 在 URL 里，错误信息已脱敏）。
"""
from __future__ import annotations

import base64
import io
import json
import urllib.error
import urllib.parse
import urllib.request

__all__ = [
    "ProviderError",
    "normalize_provider",
    "chat",
    "chat_with_tools",
    "describe_images",
]

# ---------------------------------------------------------------- 常量

DEFAULT_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "claude": "https://api.anthropic.com",
    "gemini": "https://generativelanguage.googleapis.com",
}

SUPPORTED_PROVIDERS = ("openai", "claude", "gemini")

ANTHROPIC_VERSION = "2023-06-01"
MAX_IMAGE_SIDE = 1280
JPEG_QUALITY = 82
MAX_IMAGES = 4
IMAGE_TEMPERATURE = 0.2
ERROR_BODY_LIMIT = 300  # HTTPError 时最多读取的 body 字节数

_ALIASES = {
    "openai": "openai",
    "chatgpt": "openai",
    "gpt": "openai",
    "中转": "openai",
    "自定义": "openai",
    "claude": "claude",
    "anthropic": "claude",
    "gemini": "gemini",
    "google": "gemini",
    # Grok / xAI 的 API 是 OpenAI 兼容（https://api.x.ai/v1）
    "grok": "openai",
    "xai": "openai",
    "x.ai": "openai",
    "x-ai": "openai",
    "groq": "openai",
}

try:  # 可选依赖：有就用，没有也能跑
    from PIL import Image  # type: ignore

    _PIL_AVAILABLE = True
except Exception:  # pragma: no cover - 取决于运行环境
    Image = None  # type: ignore[assignment]
    _PIL_AVAILABLE = False


class ProviderError(RuntimeError):
    """模型接口调用失败（配置错误 / HTTP 错误 / 网络错误 / 返回格式异常）。"""


# ---------------------------------------------------------------- 工具函数


def normalize_provider(name: str) -> str:
    """把用户写的 provider 名字归一化。

    openai / chatgpt / gpt / 中转 / 自定义 → "openai"
    claude / anthropic                    → "claude"
    gemini / google                       → "gemini"
    空字符串（或 None）                   → "openai"
    其它                                   → 去掉空白后的小写原名
    """
    key = ("" if name is None else str(name)).strip().lower()
    if not key:
        return "openai"
    return _ALIASES.get(key, key)


def _guess_mime(data: bytes) -> str:
    """按文件头识别图片 mime（PNG / JPEG / GIF / WEBP，识别不了按 JPEG）。"""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _prepare_image(data: bytes) -> tuple[bytes, str]:
    """压缩图片并返回 (bytes, mime)。PIL 不可用或解码失败时原样返回。"""
    raw = bytes(data or b"")
    if not raw:
        raise ProviderError("图片数据为空")
    if not _PIL_AVAILABLE or Image is None:
        return raw, _guess_mime(raw)
    try:
        im = Image.open(io.BytesIO(raw))
        im.load()
        if getattr(im, "is_animated", False):
            im.seek(0)
        # 透明图铺白底，避免转 JPEG 后透明区域变黑
        has_alpha = im.mode in ("RGBA", "LA") or (
            im.mode == "P" and "transparency" in getattr(im, "info", {})
        )
        if has_alpha:
            rgba = im.convert("RGBA")
            background = Image.new("RGB", rgba.size, (255, 255, 255))
            background.paste(rgba, mask=rgba.split()[-1])
            im = background
        else:
            im = im.convert("RGB")
        if max(im.size) > MAX_IMAGE_SIDE:
            im.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
        out = io.BytesIO()
        im.save(out, format="JPEG", quality=JPEG_QUALITY)
        return out.getvalue(), "image/jpeg"
    except Exception:
        # 损坏 / 不支持的格式：不阻断，交给厂商接口去判断
        return raw, _guess_mime(raw)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _redact(text: str, api_key: str) -> str:
    """确保错误信息里不会出现 api_key。"""
    text = str(text)
    if api_key:
        for secret in (api_key, urllib.parse.quote(api_key, safe="")):
            if secret:
                text = text.replace(secret, "***")
    return text


def _parse_json_body(raw: bytes, *, label: str, api_key: str = "") -> dict:
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except Exception as e:
        snippet = _redact(repr(raw[:200]), api_key)
        raise ProviderError(f"{label} 接口返回不是合法 JSON: {snippet}") from e
    if not isinstance(data, dict):
        raise ProviderError(f"{label} 接口返回格式异常: 顶层不是 JSON 对象")
    return data


def _post_json(
    url: str,
    payload: dict,
    headers: dict,
    *,
    timeout: float,
    label: str,
    api_key: str = "",
) -> dict:
    """POST JSON，返回解析后的 dict；所有失败转成 ProviderError。"""
    try:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    except Exception as e:  # payload 里有不可序列化的东西
        raise ProviderError(f"{label} 请求体序列化失败: {e}") from e

    request_headers = {"Content-Type": "application/json"}
    request_headers.update(headers)
    req = urllib.request.Request(url, data=body, headers=request_headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=float(timeout)) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        snippet = ""
        try:
            snippet = e.read(ERROR_BODY_LIMIT).decode("utf-8", "replace").strip()
        except Exception:
            snippet = ""
        message = f"{label} 接口 HTTP {e.code}"
        if snippet:
            message += f": {snippet}"
        raise ProviderError(_redact(message, api_key)) from e
    except Exception as e:  # 超时、连接失败、DNS 错误等
        reason = getattr(e, "reason", None) or e
        raise ProviderError(
            _redact(f"{label} 接口网络错误/超时: {reason}", api_key)
        ) from e

    return _parse_json_body(raw, label=label, api_key=api_key)


def _as_float(value, name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as e:
        raise ProviderError(f"{name} 必须是数字") from e


def _timeout_value(value) -> float:
    seconds = _as_float(value, "timeout")
    if seconds <= 0:
        raise ProviderError("timeout 必须大于 0")
    return seconds


def _as_int(value, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as e:
        raise ProviderError(f"{name} 必须是整数") from e


def _check_provider(provider: str) -> str:
    name = normalize_provider(provider)
    if name not in SUPPORTED_PROVIDERS:
        raise ProviderError(f"不支持的 provider: {name}")
    return name


def _resolve_base_url(provider: str, base_url: str) -> str:
    base = str(base_url or "").strip().rstrip("/")
    return base or DEFAULT_BASE_URLS[provider]


def _clean_api_key(api_key: str) -> str:
    return str(api_key or "").strip()


def _require_model(model: str) -> str:
    name = str(model or "").strip()
    if not name:
        raise ProviderError("未指定 model")
    return name


def _validate_messages(messages: list) -> list[dict]:
    """校验并复制 messages，role 只允许 system/user/assistant。"""
    if not isinstance(messages, (list, tuple)):
        raise ProviderError("messages 必须是列表")
    cleaned: list[dict] = []
    for item in messages:
        if not isinstance(item, dict):
            raise ProviderError("messages 里的每条消息必须是 dict")
        role = item.get("role")
        content = item.get("content")
        if role not in ("system", "user", "assistant"):
            raise ProviderError(f"不支持的消息 role: {role}")
        if not isinstance(content, str):
            raise ProviderError("消息 content 必须是字符串")
        cleaned.append({"role": role, "content": content})
    if not cleaned:
        raise ProviderError("messages 不能为空")
    return cleaned


# ---------------------------------------------------------------- 响应解析


def _openai_text(data: dict) -> str:
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise ProviderError(
            "OpenAI 兼容接口返回格式异常: 缺少 choices[0].message.content"
        ) from e
    if isinstance(content, list):  # 少数中转会把 content 返回成分段数组
        content = "".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict)
        )
    return str(content or "").strip()


def _claude_text(data: dict) -> str:
    blocks = data.get("content")
    if not isinstance(blocks, list):
        raise ProviderError("Claude 接口返回格式异常: 缺少 content")
    return "".join(
        str(block.get("text") or "")
        for block in blocks
        if isinstance(block, dict)
    ).strip()


def _gemini_text(data: dict) -> str:
    candidates = data.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ProviderError("Gemini 接口返回格式异常: 没有 candidates")
    first = candidates[0]
    content = first.get("content") if isinstance(first, dict) else None
    parts = content.get("parts") if isinstance(content, dict) else None
    if not isinstance(parts, list):
        raise ProviderError("Gemini 接口返回格式异常: 缺少 candidates[0].content.parts")
    return "".join(
        str(part.get("text") or "")
        for part in parts
        if isinstance(part, dict)
    ).strip()


# ---------------------------------------------------------------- 各厂商 URL / 请求体


def _openai_url(base: str) -> str:
    return base + "/chat/completions"


def _claude_url(base: str) -> str:
    if base.endswith("/messages"):
        return base
    if base.endswith("/v1"):
        return base + "/messages"
    return base + "/v1/messages"


def _gemini_url(base: str, model: str, api_key: str) -> str:
    model_path = urllib.parse.quote(model, safe="-._~")
    if base.endswith("/v1beta"):
        path = f"/models/{model_path}:generateContent"
    else:
        path = f"/v1beta/models/{model_path}:generateContent"
    return base + path + "?key=" + urllib.parse.quote(api_key, safe="")


def _openai_headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}"}


def _claude_headers(api_key: str) -> dict:
    return {"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION}


# ---------------------------------------------------------------- 对外接口


def chat(
    *,
    provider: str,
    base_url: str,
    api_key: str,
    model: str,
    messages: list,
    temperature: float = 0.7,
    max_tokens: int = 600,
    timeout: float = 60.0,
    auth: str = "",
) -> str:
    """纯文本对话，返回模型回答文本；失败抛 ProviderError。

    provider 会先经 normalize_provider 归一化（如 chatgpt/中转 → openai）。
    auth 是账号 id（chatgpt/claude/gemini/deepseek）时走账号额度，
    忽略 base_url / api_key；model 为空时用该账号的默认模型。
    """
    auth_id = str(auth or "").strip().lower()
    if auth_id:
        # 账号模式：延迟导入避免 providers <-> account_chat 循环依赖
        import account_chat

        msgs = _validate_messages(messages)
        return account_chat.chat(
            auth_id,
            str(model or "").strip(),
            msgs,
            _as_float(temperature, "temperature"),
            # 账号订阅模式不限制输出长度：0 表示交给账号/模型默认（不套用 API Key 的 max_tokens）
            0,
            _timeout_value(timeout),
        )

    name = _check_provider(provider)
    msgs = _validate_messages(messages)
    model_name = _require_model(model)
    key = _clean_api_key(api_key)
    base = _resolve_base_url(name, base_url)
    temp = _as_float(temperature, "temperature")
    max_out = _as_int(max_tokens, "max_tokens")
    timeout_s = _timeout_value(timeout)

    if name == "openai":
        payload = {
            "model": model_name,
            "messages": msgs,
            "temperature": temp,
            "max_tokens": max_out,
            "stream": False,
        }
        data = _post_json(
            _openai_url(base),
            payload,
            _openai_headers(key),
            timeout=timeout_s,
            label="OpenAI 兼容",
            api_key=key,
        )
        return _openai_text(data)

    if name == "claude":
        # Claude 的 system 是单独字段；多条 system 按顺序合并（常见于 ai_chat 追加的资料块）
        system_text = "\n\n".join(
            m["content"] for m in msgs if m["role"] == "system" and m["content"].strip()
        )
        conversation = [m for m in msgs if m["role"] != "system"]
        if not conversation:
            raise ProviderError("Claude 需要至少一条 user/assistant 消息")
        payload = {
            "model": model_name,
            "max_tokens": max_out,
            "temperature": temp,
            "messages": [
                {
                    "role": m["role"],
                    "content": [{"type": "text", "text": m["content"]}],
                }
                for m in conversation
            ],
        }
        if system_text:
            payload["system"] = system_text
        data = _post_json(
            _claude_url(base),
            payload,
            _claude_headers(key),
            timeout=timeout_s,
            label="Claude",
            api_key=key,
        )
        return _claude_text(data)

    # gemini
    contents: list[dict] = []
    for m in msgs:
        if m["role"] == "system":
            continue
        role = "model" if m["role"] == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": m["content"]}]})
    if not contents:
        raise ProviderError("Gemini 需要至少一条 user/assistant 消息")
    system_text = "\n\n".join(
        m["content"] for m in msgs if m["role"] == "system" and m["content"].strip()
    )
    payload = {
        "contents": contents,
        "generationConfig": {
            "temperature": temp,
            "maxOutputTokens": max_out,
        },
    }
    if system_text:
        payload["systemInstruction"] = {"parts": [{"text": system_text}]}
    data = _post_json(
        _gemini_url(base, model_name, key),
        payload,
        {},
        timeout=timeout_s,
        label="Gemini",
        api_key=key,
    )
    return _gemini_text(data)


# ---------------------------------------------------------------- 工具调用（OpenAI 兼容）

PYTHON_TOOL_MESSAGE_ROLES = ("system", "user", "assistant", "tool")


def _normalize_tool_messages(messages: list) -> list[dict]:
    """工具循环用的消息校验：在普通 role 之外允许 assistant.tool_calls 和 tool 结果。"""
    if not isinstance(messages, (list, tuple)):
        raise ProviderError("messages 必须是列表")
    cleaned: list[dict] = []
    for item in messages:
        if not isinstance(item, dict):
            raise ProviderError("messages 里的每条消息必须是 dict")
        role = item.get("role")
        if role not in PYTHON_TOOL_MESSAGE_ROLES:
            raise ProviderError(f"不支持的消息 role: {role}")
        content = item.get("content")
        if content is not None and not isinstance(content, str):
            raise ProviderError("消息 content 必须是字符串或 null")
        message: dict = {"role": role, "content": content}
        if role == "assistant":
            raw_calls = item.get("tool_calls")
            if isinstance(raw_calls, list) and raw_calls:
                calls = []
                for call in raw_calls:
                    if not isinstance(call, dict):
                        continue
                    function = call.get("function") if isinstance(call.get("function"), dict) else {}
                    calls.append(
                        {
                            "id": str(call.get("id") or ""),
                            "type": "function",
                            "function": {
                                "name": str(function.get("name") or ""),
                                "arguments": str(function.get("arguments") or "{}"),
                            },
                        }
                    )
                if calls:
                    message["tool_calls"] = calls
        elif role == "tool":
            message["tool_call_id"] = str(item.get("tool_call_id") or "")
        cleaned.append(message)
    if not cleaned:
        raise ProviderError("messages 不能为空")
    return cleaned


def _parse_tool_completion(data: dict) -> dict:
    """解析 choices[0].message -> {content, tool_calls:[{id,name,arguments}]}。"""
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as e:
        raise ProviderError(
            "OpenAI 兼容接口返回格式异常: 缺少 choices[0].message"
        ) from e
    if not isinstance(message, dict):
        raise ProviderError("OpenAI 兼容接口返回格式异常: message 不是对象")
    content = message.get("content")
    if isinstance(content, list):  # 少数中转会分段返回
        content = "".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict)
        )
    calls: list[dict] = []
    raw_calls = message.get("tool_calls")
    if isinstance(raw_calls, list):
        for call in raw_calls:
            if not isinstance(call, dict):
                continue
            function = call.get("function") if isinstance(call.get("function"), dict) else {}
            calls.append(
                {
                    "id": str(call.get("id") or ""),
                    "name": str(function.get("name") or ""),
                    "arguments": str(function.get("arguments") or ""),
                }
            )
    return {"content": str(content or ""), "tool_calls": calls}


def chat_with_tools(
    *,
    provider: str,
    base_url: str,
    api_key: str,
    model: str,
    messages: list,
    tools: list | None = None,
    temperature: float = 0.7,
    max_tokens: int = 2000,
    timeout: float = 60.0,
) -> dict:
    """OpenAI 兼容接口的「一轮」工具调用：发 tools、解析 content/tool_calls。

    不做循环（工具循环在 ai_chat）；tools 为空时就是普通对话，只是解析结果统一。
    只支持 OpenAI 兼容接口（provider 归一化后必须是 openai）；其它接口直接报错。
    返回 {"content": str, "tool_calls": [{"id","name","arguments"}]}；失败抛 ProviderError。
    """
    name = _check_provider(provider)
    if name != "openai":
        raise ProviderError("这个接口不支持工具调用")
    msgs = _normalize_tool_messages(messages)
    model_name = _require_model(model)
    key = _clean_api_key(api_key)
    base = _resolve_base_url(name, base_url)
    temp = _as_float(temperature, "temperature")
    max_out = _as_int(max_tokens, "max_tokens")
    timeout_s = _timeout_value(timeout)

    payload = {
        "model": model_name,
        "messages": msgs,
        "temperature": temp,
        "max_tokens": max_out,
        "stream": False,
    }
    if tools:
        if not isinstance(tools, (list, tuple)):
            raise ProviderError("tools 必须是列表")
        payload["tools"] = list(tools)
        payload["tool_choice"] = "auto"
    data = _post_json(
        _openai_url(base),
        payload,
        _openai_headers(key),
        timeout=timeout_s,
        label="OpenAI 兼容",
        api_key=key,
    )
    return _parse_tool_completion(data)


def _describe_images_with_account(
    auth_id: str,
    model: str,
    images: list[bytes],
    prompt: str,
    timeout: float,
    max_tokens: int,
) -> str:
    """账号模式识图：懒加载 accounts_<id>，用账号额度调该账号的多模态接口。"""
    try:
        import account_core
    except Exception as e:  # pragma: no cover - 账号核心缺失时给出可操作提示
        raise ProviderError("账号识图暂时不可用，改填视觉 API Key") from e
    try:
        module = account_core.load_provider(auth_id)
    except account_core.AccountError as e:
        if e.code == "unsupported":
            raise ProviderError("这个账号还不支持识图，改填视觉 API Key") from e
        raise ProviderError(e.message) from e
    describe = getattr(module, "describe_images", None)
    if not callable(describe):
        raise ProviderError("这个账号还不支持识图，改填视觉 API Key")
    try:
        record = account_core.ensure_access(auth_id)
    except account_core.AccountError as e:
        raise ProviderError(e.message) from e
    try:
        text = describe(record, model, images, prompt, timeout, max_tokens)
    except account_core.AccountError as e:
        raise ProviderError(e.message) from e
    return str(text or "")


def describe_images(
    *,
    provider: str,
    base_url: str,
    api_key: str,
    model: str,
    images: list[bytes],
    prompt: str = "请用中文描述这张图片，重点提取文字。",
    timeout: float = 60.0,
    max_tokens: int = 700,
    auth: str = "",
) -> str:
    """把 1~4 张图交给对应厂商的多模态接口，返回文字描述；失败抛 ProviderError。

    auth 非空时走账号额度（accounts_<auth>.describe_images），忽略
    provider / base_url / api_key；auth 为空保持原来的 API Key 逻辑。
    """
    if images is None or not isinstance(images, (list, tuple)):
        raise ProviderError("images 必须是 bytes 列表")
    if not images:
        return ""

    prepared_raw: list[bytes] = []
    for raw in list(images)[:MAX_IMAGES]:
        if not isinstance(raw, (bytes, bytearray)):
            raise ProviderError("images 里每张图必须是 bytes")
        prepared_raw.append(bytes(raw))

    auth_id = str(auth or "").strip().lower()
    if auth_id:
        max_out = _as_int(max_tokens, "max_tokens")
        timeout_s = _timeout_value(timeout)
        text_prompt = str(prompt or "").strip() or "请描述这些图片"
        return _describe_images_with_account(
            auth_id,
            str(model or "").strip(),
            prepared_raw,
            text_prompt,
            timeout_s,
            max_out,
        )

    name = _check_provider(provider)
    model_name = _require_model(model)
    key = _clean_api_key(api_key)
    base = _resolve_base_url(name, base_url)
    max_out = _as_int(max_tokens, "max_tokens")
    timeout_s = _timeout_value(timeout)
    text_prompt = str(prompt or "")

    prepared: list[tuple[bytes, str]] = []
    for raw in prepared_raw:
        prepared.append(_prepare_image(raw))

    if name == "openai":
        content: list[dict] = [{"type": "text", "text": text_prompt}]
        for data, mime in prepared:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{_b64(data)}"},
                }
            )
        payload = {
            "model": model_name,
            "messages": [{"role": "user", "content": content}],
            "temperature": IMAGE_TEMPERATURE,
            "max_tokens": max_out,
            "stream": False,
        }
        data = _post_json(
            _openai_url(base),
            payload,
            _openai_headers(key),
            timeout=timeout_s,
            label="OpenAI 兼容",
            api_key=key,
        )
        return _openai_text(data)

    if name == "claude":
        blocks: list[dict] = [{"type": "text", "text": text_prompt}]
        for data, mime in prepared:
            blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": mime,
                        "data": _b64(data),
                    },
                }
            )
        payload = {
            "model": model_name,
            "max_tokens": max_out,
            "temperature": IMAGE_TEMPERATURE,
            "messages": [{"role": "user", "content": blocks}],
        }
        data = _post_json(
            _claude_url(base),
            payload,
            _claude_headers(key),
            timeout=timeout_s,
            label="Claude",
            api_key=key,
        )
        return _claude_text(data)

    # gemini
    parts: list[dict] = [{"text": text_prompt}]
    for data, mime in prepared:
        parts.append({"inline_data": {"mime_type": mime, "data": _b64(data)}})
    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "temperature": IMAGE_TEMPERATURE,
            "maxOutputTokens": max_out,
        },
    }
    data = _post_json(
        _gemini_url(base, model_name, key),
        payload,
        {},
        timeout=timeout_s,
        label="Gemini",
        api_key=key,
    )
    return _gemini_text(data)
