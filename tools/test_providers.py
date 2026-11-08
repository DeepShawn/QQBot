#!/usr/bin/env python3
"""providers.py 自测：本地起 3 个假 HTTP 服务（openai / claude / gemini），全部用断言。

覆盖：
- normalize_provider 各种别名
- 三家 chat：路径 / 鉴权头 / 模型 / 温度 / max_tokens / messages 转换 / 返回文本
- 三家 describe_images：图片块构造（base64 可解码、mime 与字节一致）、返回文本
- 图片压缩：PIL 存在时最长边 ≤1280 且转 JPEG；PIL 不存在时原样上传
- 401 / 500 / 超时 / 连不上 → ProviderError，错误信息含状态码、body 截断 300 字节
- 未知 provider / 非法 role / 空 messages
- 调用过程不打印 api_key，错误信息不泄漏 api_key

运行：
    python3 tools/test_providers.py            # 默认只跑本地假服务（offline）
    python3 tools/test_providers.py --online   # 额外尝试真实接口（需环境变量，默认跳过）
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import io
import json
import os
import socket
import struct
import sys
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent.parent
BOT_DIR = ROOT / "bot"
sys.dont_write_bytecode = True  # 不在 bot/ 下生成 __pycache__
sys.path.insert(0, str(BOT_DIR))

import providers  # noqa: E402

PASS = 0
FAIL = 0
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  \u2713 {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  \u2717 {name}" + (f"  [{detail}]" if detail else ""))


def header(title: str) -> None:
    print(f"\n== {title} ==")


def expect_error(fn, **kwargs):
    """调用 fn，返回异常对象（ProviderError 或其它），没抛异常返回 None。"""
    try:
        fn(**kwargs)
    except providers.ProviderError as e:
        return e
    except Exception as e:  # noqa: BLE001 - 断言里要区分异常类型
        return e
    return None


def _get(node, *keys, default=None):
    """安全地按 dict key / list index 取值。"""
    for key in keys:
        if isinstance(node, dict):
            node = node.get(key)
        elif isinstance(node, list) and isinstance(key, int) and -len(node) <= key < len(node):
            node = node[key]
        else:
            return default
        if node is None:
            return default
    return node


def decode_b64(text: str) -> bytes:
    try:
        return base64.b64decode(str(text or ""))
    except Exception:
        return b""


def decode_data_uri(uri: str) -> tuple[str, bytes]:
    if not isinstance(uri, str) or not uri.startswith("data:"):
        return "", b""
    head, _, payload = uri.partition(",")
    mime = head[len("data:"):].split(";")[0]
    return mime, decode_b64(payload)


def magic_mime(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "unknown"


# ---------------------------------------------------------------- 假 HTTP 服务

OPENAI_TEXT = "这是 OpenAI 兼容接口的回答"
OPENAI_IMG_TEXT = "图里写着四个字：你好世界"
CLAUDE_TEXT = "这是 Claude 的回答，且是多段。"
CLAUDE_IMG_TEXT = "Claude 看到图片了：你好世界"
GEMINI_TEXT = "这是 Gemini 的回答。"
GEMINI_IMG_TEXT = "Gemini 看到图片了：你好世界"


class MockState:
    def __init__(self):
        self.requests: list[dict] = []
        self.error_status: int | None = None
        self.error_body: bytes = b""
        self.delay: float = 0.0
        self.lock = threading.Lock()

    def clear(self):
        with self.lock:
            self.requests.clear()
            self.error_status = None
            self.error_body = b""
            self.delay = 0.0

    def add(self, req: dict):
        with self.lock:
            self.requests.append(req)

    def last(self) -> dict:
        return self.requests[-1]

    def count(self) -> int:
        return len(self.requests)


def _fake_answer(provider: str, body: dict) -> dict:
    body = body if isinstance(body, dict) else {}
    if provider == "openai":
        has_image = False
        for msg in body.get("messages") or []:
            content = msg.get("content") if isinstance(msg, dict) else None
            if isinstance(content, list) and any(
                isinstance(part, dict) and part.get("type") == "image_url" for part in content
            ):
                has_image = True
        text = OPENAI_IMG_TEXT if has_image else OPENAI_TEXT
        return {"choices": [{"index": 0, "message": {"role": "assistant", "content": text}}]}

    if provider == "claude":
        blocks = _get(body, "messages", 0, "content") or []
        has_image = any(isinstance(b, dict) and b.get("type") == "image" for b in blocks)
        texts = [CLAUDE_IMG_TEXT] if has_image else ["这是 Claude 的回答", "，且是多段。"]
        return {
            "id": "msg_mock",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": t} for t in texts],
            "stop_reason": "end_turn",
        }

    # gemini
    parts = _get(body, "contents", 0, "parts") or []
    has_image = any(isinstance(p, dict) and "inline_data" in p for p in parts)
    texts = [GEMINI_IMG_TEXT] if has_image else ["这是 Gemini 的回答", "。"]
    return {
        "candidates": [
            {
                "content": {"role": "model", "parts": [{"text": t} for t in texts]},
                "finishReason": "STOP",
            }
        ]
    }


def _make_handler(provider: str, state: MockState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # 静默，测试输出干净
            return

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            raw = self.rfile.read(length) if length > 0 else b""
            split = urlsplit(self.path)
            try:
                body = json.loads(raw.decode("utf-8")) if raw else None
            except Exception:
                body = None
            state.add(
                {
                    "method": self.command,
                    "path": split.path,
                    "query": parse_qs(split.query),
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": body,
                    "raw": raw,
                }
            )
            if state.delay:
                time.sleep(state.delay)
            if state.error_status:
                self._send(state.error_status, state.error_body or b'{"error":"mock"}')
                return
            answer = _fake_answer(provider, body)
            self._send(200, json.dumps(answer, ensure_ascii=False).encode("utf-8"))

        def _send(self, status: int, payload: bytes):
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except Exception:  # 客户端超时/断开后写回失败是正常的
                pass

    return Handler


def start_mock(provider: str):
    state = MockState()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(provider, state))
    thread = threading.Thread(
        target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    return srv, state, base


def stop_mock(srv) -> None:
    srv.shutdown()
    srv.server_close()


def free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


# ---------------------------------------------------------------- 测试图片


def make_png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """纯标准库现场生成一张 RGB PNG。"""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    row = b"\x00" + bytes(rgb) * width
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(row * height))
        + chunk(b"IEND", b"")
    )


# ---------------------------------------------------------------- 各段测试


def test_normalize() -> None:
    header("normalize_provider 别名")
    cases = [
        ("openai", "openai"),
        ("OpenAI", "openai"),
        ("chatgpt", "openai"),
        ("GPT", "openai"),
        ("中转", "openai"),
        ("自定义", "openai"),
        ("", "openai"),
        ("   ", "openai"),
        (None, "openai"),
        ("claude", "claude"),
        ("Anthropic", "claude"),
        ("anthropic", "claude"),
        ("gemini", "gemini"),
        ("Google", "gemini"),
        ("DeepSeek", "deepseek"),
        ("  ONEAPI ", "oneapi"),
    ]
    for raw, expected in cases:
        got = providers.normalize_provider(raw)
        check(f"normalize({raw!r}) -> {expected!r}", got == expected, repr(got))


def test_openai_chat(state: MockState, base: str) -> None:
    header("OpenAI 兼容 chat")
    state.clear()
    messages = [
        {"role": "system", "content": "你是测试助手"},
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好呀"},
        {"role": "user", "content": "讲个笑话"},
    ]
    answer = providers.chat(
        provider="ChatGPT",  # 别名
        base_url=base + "/v1/",  # 结尾斜杠也要能处理
        api_key="sk-openai-secret",
        model="gpt-test",
        messages=messages,
        temperature=0.3,
        max_tokens=123,
        timeout=5,
    )
    check("openai chat 返回文本", answer == OPENAI_TEXT, repr(answer))
    check("openai 只发一次请求", state.count() == 1, str(state.count()))
    req = state.last()
    check("openai 路径 /v1/chat/completions", req["path"] == "/v1/chat/completions", req["path"])
    check(
        "openai Authorization 头",
        req["headers"].get("authorization") == "Bearer sk-openai-secret",
        repr(req["headers"].get("authorization")),
    )
    check(
        "openai Content-Type 是 json",
        str(req["headers"].get("content-type", "")).startswith("application/json"),
        repr(req["headers"].get("content-type")),
    )
    body = req["body"] or {}
    check("openai body.model", body.get("model") == "gpt-test", repr(body.get("model")))
    check(
        "openai body.temperature",
        isinstance(body.get("temperature"), (int, float))
        and abs(float(body["temperature"]) - 0.3) < 1e-9,
        repr(body.get("temperature")),
    )
    check("openai body.max_tokens", body.get("max_tokens") == 123, repr(body.get("max_tokens")))
    check("openai body.stream=False", body.get("stream") is False, repr(body.get("stream")))
    check("openai messages 原样透传", body.get("messages") == messages, repr(body.get("messages"))[:120])


def test_claude_chat(state: MockState, base: str) -> None:
    header("Claude chat")
    state.clear()
    messages = [
        {"role": "system", "content": "你是测试助手"},
        {"role": "user", "content": "在吗"},
        {"role": "assistant", "content": "在的"},
        {"role": "user", "content": "帮我写句话"},
    ]
    answer = providers.chat(
        provider="claude",
        base_url=base,
        api_key="sk-claude-secret",
        model="claude-test",
        messages=messages,
        temperature=0.2,
        max_tokens=321,
        timeout=5,
    )
    check("claude chat 多段 text 拼接", answer == CLAUDE_TEXT, repr(answer))
    req = state.last()
    check("claude 路径 /v1/messages", req["path"] == "/v1/messages", req["path"])
    check(
        "claude x-api-key 头",
        req["headers"].get("x-api-key") == "sk-claude-secret",
        repr(req["headers"].get("x-api-key")),
    )
    check(
        "claude anthropic-version 头",
        req["headers"].get("anthropic-version") == "2023-06-01",
        repr(req["headers"].get("anthropic-version")),
    )
    body = req["body"] or {}
    check("claude body.model", body.get("model") == "claude-test", repr(body.get("model")))
    check("claude body.max_tokens", body.get("max_tokens") == 321, repr(body.get("max_tokens")))
    check(
        "claude body.temperature",
        isinstance(body.get("temperature"), (int, float))
        and abs(float(body["temperature"]) - 0.2) < 1e-9,
        repr(body.get("temperature")),
    )
    check("claude system 单独抽出", body.get("system") == "你是测试助手", repr(body.get("system")))
    expected = [
        {"role": "user", "content": [{"type": "text", "text": "在吗"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "在的"}]},
        {"role": "user", "content": [{"type": "text", "text": "帮我写句话"}]},
    ]
    check("claude messages 按顺序转成 content 块", body.get("messages") == expected, repr(body.get("messages"))[:160])
    check(
        "claude messages 里没有 system role",
        all(m.get("role") in ("user", "assistant") for m in (body.get("messages") or [])),
    )

    # base_url 已以 /v1 结尾时不能拼成 /v1/v1/messages
    state.clear()
    providers.chat(
        provider="anthropic",
        base_url=base + "/v1/",
        api_key="sk-claude-secret",
        model="claude-test",
        messages=[{"role": "user", "content": "hi"}],
        timeout=5,
    )
    check("claude base 含 /v1 不重复", state.last()["path"] == "/v1/messages", state.last()["path"])

    # 多条 system（ai_chat 会追加资料块）合并进 system 字段
    state.clear()
    providers.chat(
        provider="claude",
        base_url=base,
        api_key="sk-claude-secret",
        model="claude-test",
        messages=[
            {"role": "system", "content": "人设A"},
            {"role": "system", "content": "资料B"},
            {"role": "user", "content": "问题"},
        ],
        timeout=5,
    )
    body = state.last()["body"] or {}
    check("claude 多条 system 合并", body.get("system") == "人设A\n\n资料B", repr(body.get("system")))
    check(
        "claude 合并后只有一条 user 消息",
        len(body.get("messages") or []) == 1,
        str(len(body.get("messages") or [])),
    )


def test_gemini_chat(state: MockState, base: str) -> None:
    header("Gemini chat")
    state.clear()
    messages = [
        {"role": "system", "content": "你是测试助手"},
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好呀"},
        {"role": "user", "content": "再见"},
    ]
    answer = providers.chat(
        provider="Google",
        base_url=base,
        api_key="sk-gemini-secret",
        model="gemini-test",
        messages=messages,
        temperature=0.5,
        max_tokens=222,
        timeout=5,
    )
    check("gemini chat 多段 text 拼接", answer == GEMINI_TEXT, repr(answer))
    req = state.last()
    check(
        "gemini 路径 /v1beta/models/gemini-test:generateContent",
        req["path"] == "/v1beta/models/gemini-test:generateContent",
        req["path"],
    )
    check(
        "gemini key 在 query",
        req["query"].get("key") == ["sk-gemini-secret"],
        repr(req["query"].get("key")),
    )
    check("gemini 不带 Authorization 头", "authorization" not in req["headers"], repr(req["headers"].keys()))
    body = req["body"] or {}
    contents = body.get("contents") or []
    check(
        "gemini contents 角色转换 user/model",
        [c.get("role") for c in contents] == ["user", "model", "user"],
        repr([c.get("role") for c in contents]),
    )
    check(
        "gemini contents 文本",
        [
            _get(c, "parts", 0, "text")
            for c in contents
        ]
        == ["你好", "你好呀", "再见"],
        repr([_get(c, "parts", 0, "text") for c in contents]),
    )
    check(
        "gemini systemInstruction",
        _get(body, "systemInstruction", "parts", 0, "text") == "你是测试助手",
        repr(_get(body, "systemInstruction", "parts", 0, "text")),
    )
    config = body.get("generationConfig") or {}
    check(
        "gemini generationConfig.temperature",
        isinstance(config.get("temperature"), (int, float))
        and abs(float(config["temperature"]) - 0.5) < 1e-9,
        repr(config.get("temperature")),
    )
    check(
        "gemini generationConfig.maxOutputTokens",
        config.get("maxOutputTokens") == 222,
        repr(config.get("maxOutputTokens")),
    )

    # base_url 已含 /v1beta 时不能拼成 /v1beta/v1beta
    state.clear()
    providers.chat(
        provider="gemini",
        base_url=base + "/v1beta/",
        api_key="sk-gemini-secret",
        model="gemini-test",
        messages=[{"role": "user", "content": "hi"}],
        timeout=5,
    )
    check(
        "gemini base 含 /v1beta 不重复",
        state.last()["path"] == "/v1beta/models/gemini-test:generateContent",
        state.last()["path"],
    )
    body = state.last()["body"] or {}
    check("gemini 无 system 时不发 systemInstruction", "systemInstruction" not in body, repr(body.keys()))


def test_openai_images(state: MockState, base: str) -> None:
    header("OpenAI 兼容 describe_images")
    state.clear()
    images = [make_png(8, 6, (200, 30, 30)), make_png(10, 4, (20, 30, 200))]
    text = providers.describe_images(
        provider="openai",
        base_url=base + "/v1",
        api_key="sk-openai-secret",
        model="gpt-vision-test",
        images=images,
        prompt="描述一下",
        timeout=5,
        max_tokens=321,
    )
    check("openai 图像返回文本", text == OPENAI_IMG_TEXT, repr(text))
    req = state.last()
    check("openai 图像路径", req["path"] == "/v1/chat/completions", req["path"])
    body = req["body"] or {}
    content = _get(body, "messages", 0, "content") or []
    check("openai 图像 messages[0].role=user", _get(body, "messages", 0, "role") == "user")
    check("openai 图像第一块是 text prompt", _get(content, 0, "type") == "text" and _get(content, 0, "text") == "描述一下", repr(_get(content, 0)))
    image_parts = [p for p in content if isinstance(p, dict) and p.get("type") == "image_url"]
    check("openai 图片块数量", len(image_parts) == 2, str(len(image_parts)))
    for idx, part in enumerate(image_parts):
        mime, data = decode_data_uri(_get(part, "image_url", "url") or "")
        check(f"openai 图片{idx + 1} base64 可解码", bool(data), repr(_get(part, "image_url", "url"))[:40])
        check(
            f"openai 图片{idx + 1} mime 与字节一致",
            bool(data) and mime == magic_mime(data),
            f"mime={mime} magic={magic_mime(data)}",
        )
    check("openai 图像 body.model", body.get("model") == "gpt-vision-test", repr(body.get("model")))
    check("openai 图像 max_tokens", body.get("max_tokens") == 321, repr(body.get("max_tokens")))
    check(
        "openai 图像 temperature=0.2",
        isinstance(body.get("temperature"), (int, float))
        and abs(float(body["temperature"]) - 0.2) < 1e-9,
        repr(body.get("temperature")),
    )

    # 超过 4 张只取前 4 张
    state.clear()
    providers.describe_images(
        provider="openai",
        base_url=base + "/v1",
        api_key="sk-openai-secret",
        model="gpt-vision-test",
        images=[make_png(2, 2, (1, 2, 3))] * 5,
        timeout=5,
    )
    content = _get(state.last()["body"], "messages", 0, "content") or []
    check(
        "describe_images 最多 4 张图",
        len([p for p in content if isinstance(p, dict) and p.get("type") == "image_url"]) == 4,
        str(len(content)),
    )


def test_claude_images(state: MockState, base: str) -> None:
    header("Claude describe_images")
    state.clear()
    text = providers.describe_images(
        provider="claude",
        base_url=base,
        api_key="sk-claude-secret",
        model="claude-test",
        images=[make_png(8, 6, (200, 30, 30)), make_png(10, 4, (20, 30, 200))],
        prompt="描述一下",
        timeout=5,
        max_tokens=321,
    )
    check("claude 图像返回文本", text == CLAUDE_IMG_TEXT, repr(text))
    req = state.last()
    body = req["body"] or {}
    check("claude 图像路径", req["path"] == "/v1/messages", req["path"])
    blocks = _get(body, "messages", 0, "content") or []
    check("claude 图像第一块是 text prompt", _get(blocks, 0, "type") == "text" and _get(blocks, 0, "text") == "描述一下")
    image_blocks = [b for b in blocks if isinstance(b, dict) and b.get("type") == "image"]
    check("claude 图片块数量", len(image_blocks) == 2, str(len(image_blocks)))
    for idx, block in enumerate(image_blocks):
        source = _get(block, "source") or {}
        data = decode_b64(source.get("data"))
        check(f"claude 图片{idx + 1} source.type=base64", source.get("type") == "base64", repr(source.get("type")))
        check(f"claude 图片{idx + 1} base64 可解码", bool(data), repr(source.get("data"))[:40])
        check(
            f"claude 图片{idx + 1} media_type 与字节一致",
            bool(data) and source.get("media_type") == magic_mime(data),
            f"media_type={source.get('media_type')} magic={magic_mime(data)}",
        )
    check("claude 图像 max_tokens", body.get("max_tokens") == 321, repr(body.get("max_tokens")))
    check("claude 图像 model", body.get("model") == "claude-test", repr(body.get("model")))
    check("claude 图像不带 system", "system" not in body, repr(body.keys()))


def test_gemini_images(state: MockState, base: str) -> None:
    header("Gemini describe_images")
    state.clear()
    text = providers.describe_images(
        provider="gemini",
        base_url=base,
        api_key="sk-gemini-secret",
        model="gemini-test",
        images=[make_png(8, 6, (200, 30, 30)), make_png(10, 4, (20, 30, 200))],
        prompt="描述一下",
        timeout=5,
        max_tokens=321,
    )
    check("gemini 图像返回文本", text == GEMINI_IMG_TEXT, repr(text))
    req = state.last()
    check(
        "gemini 图像路径",
        req["path"] == "/v1beta/models/gemini-test:generateContent",
        req["path"],
    )
    check("gemini 图像 key", req["query"].get("key") == ["sk-gemini-secret"])
    body = req["body"] or {}
    parts = _get(body, "contents", 0, "parts") or []
    check("gemini 图像第一块是 text prompt", _get(parts, 0, "text") == "描述一下", repr(_get(parts, 0)))
    inline_parts = [p for p in parts if isinstance(p, dict) and "inline_data" in p]
    check("gemini 图片块数量", len(inline_parts) == 2, str(len(inline_parts)))
    for idx, part in enumerate(inline_parts):
        inline = part.get("inline_data") or {}
        data = decode_b64(inline.get("data"))
        check(f"gemini 图片{idx + 1} base64 可解码", bool(data), repr(inline.get("data"))[:40])
        check(
            f"gemini 图片{idx + 1} mime_type 与字节一致",
            bool(data) and inline.get("mime_type") == magic_mime(data),
            f"mime_type={inline.get('mime_type')} magic={magic_mime(data)}",
        )
    config = body.get("generationConfig") or {}
    check("gemini 图像 maxOutputTokens", config.get("maxOutputTokens") == 321, repr(config.get("maxOutputTokens")))


def test_image_compression() -> None:
    header("图片识别 / 压缩")
    png = make_png(8, 6, (200, 30, 30))
    samples = [
        ("PNG", png, "image/png"),
        ("JPEG", b"\xff\xd8\xff\xe0" + b"\x00" * 8, "image/jpeg"),
        ("GIF", b"GIF89a" + b"\x00" * 8, "image/gif"),
        ("WEBP", b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 4, "image/webp"),
        ("未知格式", b"not-an-image", "image/jpeg"),
    ]
    for name, data, expected in samples:
        got = providers._guess_mime(data)
        check(f"_guess_mime {name}", got == expected, repr(got))

    # 模拟没装 PIL：原样上传
    old_flag = providers._PIL_AVAILABLE
    providers._PIL_AVAILABLE = False
    try:
        data, mime = providers._prepare_image(png)
        check("无 PIL 时图片原样返回", data == png and mime == "image/png", f"{len(data)} {mime}")
    finally:
        providers._PIL_AVAILABLE = old_flag

    if providers._PIL_AVAILABLE:
        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (1800, 1200), (10, 120, 200)).save(buf, format="PNG")
        big = buf.getvalue()
        small, mime = providers._prepare_image(big)
        try:
            im = Image.open(io.BytesIO(small))
            size = im.size
            fmt = im.format
        except Exception as e:  # noqa: BLE001
            size, fmt = (0, 0), f"打开失败: {e}"
        check("有 PIL 时最长边 ≤1280", max(size) <= 1280, str(size))
        check("有 PIL 时转成 JPEG", fmt == "JPEG" and mime == "image/jpeg", f"fmt={fmt} mime={mime}")

        # 透明 PNG 转 JPEG 不应崩
        rgba_buf = io.BytesIO()
        Image.new("RGBA", (64, 48), (255, 0, 0, 0)).save(rgba_buf, format="PNG")
        data, mime = providers._prepare_image(rgba_buf.getvalue())
        check("透明 PNG 可转 JPEG", mime == "image/jpeg" and magic_mime(data) == "image/jpeg", mime)
    else:
        print("  - SKIP: 没装 PIL，跳过压缩相关断言")


def test_errors(states: dict[str, MockState], bases: dict[str, str], png: bytes) -> None:
    header("错误处理：HTTP 401 / 500")
    for provider in ("openai", "claude", "gemini"):
        state = states[provider]
        base = bases[provider]
        for status in (401, 500):
            state.clear()
            state.error_status = status
            state.error_body = b'{"error":{"message":"invalid key"}}'
            err = expect_error(
                providers.chat,
                provider=provider,
                base_url=base + "/v1" if provider == "openai" else base,
                api_key="sk-error-secret",
                model="test-model",
                messages=[{"role": "user", "content": "hi"}],
                timeout=5,
            )
            label = f"{provider} chat"
            check(f"{label} HTTP {status} 抛 ProviderError", isinstance(err, providers.ProviderError), repr(err))
            message = str(err) if err else ""
            check(f"{label} HTTP {status} 错误信息含状态码", str(status) in message, message[:120])
            check(f"{label} HTTP {status} 错误信息含 body 片段", "invalid key" in message, message[:120])

        for status in (401, 500):
            state.clear()
            state.error_status = status
            state.error_body = b'{"error":"bad image request"}'
            err = expect_error(
                providers.describe_images,
                provider=provider,
                base_url=base + "/v1" if provider == "openai" else base,
                api_key="sk-error-secret",
                model="test-model",
                images=[png],
                timeout=5,
            )
            label = f"{provider} describe_images"
            check(f"{label} HTTP {status} 抛 ProviderError", isinstance(err, providers.ProviderError), repr(err))
            message = str(err) if err else ""
            check(f"{label} HTTP {status} 错误信息含状态码", str(status) in message, message[:120])
            check(f"{label} HTTP {status} 错误信息含 body 片段", "bad image request" in message, message[:120])

    header("错误处理：body 截断 / 网络 / 超时 / 参数")
    state = states["openai"]
    state.clear()
    state.error_status = 500
    state.error_body = b'{"error":"' + b"X" * 1000 + b'"}'
    err = expect_error(
        providers.chat,
        provider="openai",
        base_url=bases["openai"] + "/v1",
        api_key="sk-error-secret",
        model="test-model",
        messages=[{"role": "user", "content": "hi"}],
        timeout=5,
    )
    message = str(err) if err else ""
    check("HTTP 错误 body 只读 300 字节", "X" * 301 not in message, f"len={len(message)}")
    check("HTTP 错误仍含状态码 500", "500" in message, message[:80])

    # 连不上：连接被拒
    dead_port = free_port()
    err = expect_error(
        providers.chat,
        provider="gemini",
        base_url=f"http://127.0.0.1:{dead_port}",
        api_key="sk-gemini-secret",
        model="test-model",
        messages=[{"role": "user", "content": "hi"}],
        timeout=2,
    )
    check("网络错误抛 ProviderError", isinstance(err, providers.ProviderError), repr(err))
    check("网络错误信息不含 api_key", "sk-gemini-secret" not in (str(err) if err else ""), str(err)[:120])
    check(
        "网络错误信息提到网络/超时",
        any(word in (str(err) if err else "") for word in ("网络", "超时")),
        str(err)[:120],
    )

    # 超时
    state.clear()
    state.delay = 1.0
    started = time.monotonic()
    err = expect_error(
        providers.chat,
        provider="openai",
        base_url=bases["openai"] + "/v1",
        api_key="sk-timeout-secret",
        model="test-model",
        messages=[{"role": "user", "content": "hi"}],
        timeout=0.25,
    )
    elapsed = time.monotonic() - started
    check("超时抛 ProviderError", isinstance(err, providers.ProviderError), repr(err))
    check("超时信息提到超时/网络", any(word in (str(err) if err else "") for word in ("超时", "网络")), str(err)[:120])
    check("超时不会等满 60 秒", elapsed < 0.9, f"{elapsed:.2f}s")
    state.clear()

    # 未知 provider
    err = expect_error(
        providers.chat,
        provider="foobar",
        base_url="http://127.0.0.1:1",
        api_key="k",
        model="m",
        messages=[{"role": "user", "content": "hi"}],
    )
    check(
        "未知 provider chat 抛 ProviderError",
        isinstance(err, providers.ProviderError) and "不支持的 provider: foobar" in str(err),
        repr(err),
    )
    err = expect_error(
        providers.describe_images,
        provider="unknown-vendor",
        base_url="http://127.0.0.1:1",
        api_key="k",
        model="m",
        images=[png],
    )
    check(
        "未知 provider describe_images 抛 ProviderError",
        isinstance(err, providers.ProviderError) and "不支持的 provider: unknown-vendor" in str(err),
        repr(err),
    )

    # 非法 messages
    base_kwargs = dict(
        provider="openai",
        base_url=bases["openai"] + "/v1",
        api_key="k",
        model="m",
    )
    err = expect_error(
        providers.chat,
        messages=[{"role": "tool", "content": "x"}],
        **base_kwargs,
    )
    check(
        "非法 role 抛 ProviderError",
        isinstance(err, providers.ProviderError) and "role" in str(err),
        repr(err),
    )
    err = expect_error(providers.chat, messages=[], **base_kwargs)
    check(
        "空 messages 抛 ProviderError",
        isinstance(err, providers.ProviderError) and "messages" in str(err),
        repr(err),
    )
    err = expect_error(
        providers.chat,
        messages=[{"role": "user", "content": 123}],  # type: ignore[list-item]
        **base_kwargs,
    )
    check(
        "content 不是 str 抛 ProviderError",
        isinstance(err, providers.ProviderError) and "content" in str(err),
        repr(err),
    )

    # 不打印 / 不泄漏 api_key
    state.clear()
    state.error_status = 500
    state.error_body = b'{"error":"boom"}'
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
        leak_err = expect_error(
            providers.chat,
            provider="openai",
            base_url=bases["openai"] + "/v1",
            api_key="sk-should-not-print",
            model="m",
            messages=[{"role": "user", "content": "hi"}],
            timeout=5,
        )
    check("调用过程不打印 api_key", "sk-should-not-print" not in captured.getvalue(), captured.getvalue()[:120])
    check(
        "错误信息不泄漏 api_key",
        "sk-should-not-print" not in (str(leak_err) if leak_err else ""),
        str(leak_err)[:120],
    )
    state.clear()


def run_offline(servers: dict, states: dict, bases: dict) -> None:
    test_normalize()
    test_openai_chat(states["openai"], bases["openai"])
    test_claude_chat(states["claude"], bases["claude"])
    test_gemini_chat(states["gemini"], bases["gemini"])
    test_openai_images(states["openai"], bases["openai"])
    test_claude_images(states["claude"], bases["claude"])
    test_gemini_images(states["gemini"], bases["gemini"])
    test_image_compression()
    test_errors(states, bases, make_png(6, 6, (1, 2, 3)))


def run_online() -> None:
    header("在线真实接口（可选，默认跳过）")
    key = os.environ.get("PROVIDERS_ONLINE_API_KEY", "").strip()
    if not key:
        print("  - SKIP: 未设置 PROVIDERS_ONLINE_API_KEY，不调用真实接口（避免花钱）")
        return
    base = os.environ.get("PROVIDERS_ONLINE_BASE_URL", "https://api.openai.com/v1")
    model = os.environ.get("PROVIDERS_ONLINE_MODEL", "gpt-4o-mini")
    try:
        answer = providers.chat(
            provider="openai",
            base_url=base,
            api_key=key,
            model=model,
            messages=[{"role": "user", "content": "只回复两个字：你好"}],
            max_tokens=20,
            timeout=30,
        )
        check("在线 openai chat 有返回", bool(answer.strip()), repr(answer)[:80])
    except providers.ProviderError as e:
        check("在线 openai chat 有返回", False, str(e)[:160])


def main() -> int:
    parser = argparse.ArgumentParser(description="providers.py 自测（默认本地假服务）")
    parser.add_argument("--offline", action="store_true", help="只跑本地假服务测试（默认行为）")
    parser.add_argument("--online", action="store_true", help="额外尝试真实接口（需 PROVIDERS_ONLINE_API_KEY）")
    args = parser.parse_args()
    _ = args.offline  # 默认就是 offline，保留参数方便显式指定

    print("providers.py 自测：本地假 HTTP 服务（openai / claude / gemini）")
    servers: dict = {}
    states: dict[str, MockState] = {}
    bases: dict[str, str] = {}
    try:
        for provider in ("openai", "claude", "gemini"):
            srv, state, base = start_mock(provider)
            servers[provider] = srv
            states[provider] = state
            bases[provider] = base
            print(f"  假 {provider} 服务: {base}")
        run_offline(servers, states, bases)
        if args.online:
            run_online()
    finally:
        for srv in servers.values():
            stop_mock(srv)

    print()
    if FAIL == 0:
        print(f"PASS: {PASS} passed, 0 failed")
        return 0
    print(f"FAIL: {PASS} passed, {FAIL} failed")
    for name in FAILED:
        print(f"  - {name}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
