#!/usr/bin/env python3
"""imagegen.py 自测：本地起 HTTP 假服务，全程不调用真实 API、不花钱。

覆盖：
- 接口契约：ImageGenError 继承 RuntimeError；generate 的签名/关键字参数/默认值
- openai：b64_json、url 下载、400 不认 response_format 时自动重试
- gemini：inlineData / inline_data 解析、多图收集、无图片报错
- 错误：401 / 500 / 无图片 / 非 JSON / 坏 base64 / 非图片 / 下载失败 / 超时 / 网络不通
- base_url 拼接：末尾 /、已含 /v1、已含 /v1beta
- 压缩：>1600px 缩到 1600 转 JPEG；小图原样返回
- 多图与 n 上限；错误信息不泄露 api_key

运行：python3 tools/test_imagegen.py
"""

from __future__ import annotations

import base64
import contextlib
import inspect
import io
import json
import socket
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOT_DIR = ROOT / "bot"
sys.dont_write_bytecode = True  # 不在 bot/ 下生成 __pycache__
sys.path.insert(0, str(BOT_DIR))

import imagegen  # noqa: E402

try:
    from PIL import Image
except Exception:  # pragma: no cover - 本机已装 PIL，1x1 PNG 仅作兜底
    Image = None

PASSED = 0
FAILED: list[str] = []
REQUESTS: list[dict] = []

# 1x1 透明 PNG，PIL 缺失时兜底（正常情况下不会用到）
FALLBACK_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def make_png(width: int, height: int, color: tuple[int, int, int]) -> bytes:
    """现场生成真 PNG（PIL 缺失时退回 1x1 PNG）。"""
    if Image is None:
        return FALLBACK_PNG
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


SMALL_PNG = make_png(64, 48, (30, 120, 200))
SMALL_PNG2 = make_png(48, 48, (200, 80, 40))
LARGE_PNG = make_png(2000, 1200, (90, 160, 210))


# ---------------------------------------------------------------------------
# 断言小工具
# ---------------------------------------------------------------------------
def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED
    if ok:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED.append(name + (f" -> {detail}" if detail else ""))
        print(f"  [FAIL] {name}" + (f" -> {detail}" if detail else ""))


def check_error(name: str, fn, contains=(), excludes=()) -> None:
    """断言 fn() 抛出 ImageGenError，且消息包含/不包含指定片段。"""
    try:
        fn()
    except imagegen.ImageGenError as exc:
        message = str(exc)
        ok = all(token in message for token in contains) and all(
            token not in message for token in excludes
        )
        check(name, ok, message[:200])
    except Exception as exc:  # noqa: BLE001 - 抛错类型不对也要报 FAIL
        check(name, False, f"抛出了 {type(exc).__name__}: {exc}")
    else:
        check(name, False, "没有抛出 ImageGenError")


def is_valid_image(data: bytes) -> bool:
    if Image is not None:
        try:
            Image.open(io.BytesIO(data)).verify()
            return True
        except Exception:
            return False
    return data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\xff\xd8\xff")


def reset_requests() -> None:
    REQUESTS.clear()


def posts(contains: str = "") -> list[dict]:
    return [r for r in REQUESTS if r["method"] == "POST" and contains in r["path"]]


def gets(contains: str = "") -> list[dict]:
    return [r for r in REQUESTS if r["method"] == "GET" and contains in r["path"]]


def path_of(request: dict) -> str:
    return urllib.parse.urlsplit(request.get("path") or "").path


def query_of(request: dict) -> dict[str, list[str]]:
    return urllib.parse.parse_qs(urllib.parse.urlsplit(request.get("path") or "").query)


def body_of(request: dict) -> dict:
    value = request.get("body")
    return value if isinstance(value, dict) else {}


def header_of(request: dict, name: str) -> str:
    for key, value in (request.get("headers") or {}).items():
        if key.lower() == name.lower():
            return value
    return ""


def closed_port() -> int:
    """拿一个刚释放的本机端口，用于制造连接失败。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


# ---------------------------------------------------------------------------
# 本地假服务
# ---------------------------------------------------------------------------
class FakeAPI(BaseHTTPRequestHandler):
    server_version = "FakeImageAPI/1.0"

    def log_message(self, *args) -> None:  # 静音访问日志
        pass

    def _write(self, code: int, data: bytes, ctype: str) -> None:
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # 客户端已超时/断开

    def _json(self, code: int, obj) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._write(code, raw, "application/json; charset=utf-8")

    # ---------------- GET：供 openai url 形式下载图片 ----------------
    def do_GET(self) -> None:  # noqa: N802 - http.server 约定
        parsed = urllib.parse.urlsplit(self.path)
        REQUESTS.append(
            {"method": "GET", "path": self.path, "headers": dict(self.headers), "body": None}
        )
        if parsed.path == "/img.png":
            self._write(200, SMALL_PNG, "image/png")
        elif parsed.path == "/html":
            self._write(200, b"<html>not an image</html>", "text/html; charset=utf-8")
        else:
            self._write(404, b"not found", "text/plain")

    # ---------------- POST：假 openai / gemini ----------------
    def do_POST(self) -> None:  # noqa: N802 - http.server 约定
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = None
        parsed = urllib.parse.urlsplit(self.path)
        REQUESTS.append(
            {"method": "POST", "path": self.path, "headers": dict(self.headers), "body": body}
        )
        ns = parsed.path.strip("/").split("/")[0]
        data = body if isinstance(body, dict) else {}
        if ":generateContent" in parsed.path:
            self._gemini(ns, data)
        else:
            self._openai(ns, data)

    def _openai(self, ns: str, body: dict) -> None:
        if ns in ("b64", "join-a", "join-b", "one"):
            self._json(200, {"created": 1700000000, "data": [{"b64_json": b64(SMALL_PNG)}]})
        elif ns == "url":
            port = self.server.server_address[1]
            self._json(200, {"data": [{"url": f"http://127.0.0.1:{port}/img.png"}]})
        elif ns == "urlbad":
            port = self.server.server_address[1]
            self._json(200, {"data": [{"url": f"http://127.0.0.1:{port}/html"}]})
        elif ns == "retry":
            if "response_format" in body:
                self._json(
                    400,
                    {
                        "error": {
                            "message": "Unknown parameter: 'response_format'.",
                            "type": "invalid_request_error",
                        }
                    },
                )
            else:
                self._json(200, {"data": [{"b64_json": b64(SMALL_PNG)}]})
        elif ns == "auth":
            self._json(401, {"error": {"message": "Incorrect API key provided."}})
        elif ns == "boom":
            self._json(500, {"error": {"message": "The server had an error."}})
        elif ns == "empty":
            self._json(200, {"data": []})
        elif ns == "badjson":
            self._write(200, b"<html>not json</html>", "text/html; charset=utf-8")
        elif ns == "slow":
            time.sleep(1.0)
            self._json(200, {"data": [{"b64_json": b64(SMALL_PNG)}]})
        elif ns == "big":
            self._json(200, {"data": [{"b64_json": b64(LARGE_PNG)}]})
        elif ns == "badb64":
            self._json(200, {"data": [{"b64_json": "@@@@"}]})
        elif ns == "notimage":
            self._json(200, {"data": [{"b64_json": b64(b"hello, not an image")}]})
        elif ns == "multi":
            self._json(
                200,
                {
                    "data": [
                        {"b64_json": b64(SMALL_PNG)},
                        {"b64_json": b64(SMALL_PNG2)},
                        {"b64_json": b64(SMALL_PNG)},
                    ]
                },
            )
        elif ns == "no-url":
            self._json(200, {"data": [{"revised_prompt": "only text"}]})
        else:
            self._json(404, {"error": {"message": f"unknown route: {ns}"}})

    def _gemini(self, ns: str, body: dict) -> None:
        def image_part(data: bytes) -> dict:
            return {"inlineData": {"mimeType": "image/png", "data": b64(data)}}

        if ns in ("gemini", "gemini-v1beta", "gmulti"):
            parts = [{"text": "好的，图在这里"}, image_part(SMALL_PNG)]
            if ns == "gmulti":
                parts.append(
                    {"inline_data": {"mime_type": "image/png", "data": b64(SMALL_PNG2)}}
                )
            self._json(
                200,
                {
                    "candidates": [
                        {
                            "content": {"role": "model", "parts": parts},
                            "finishReason": "STOP",
                        }
                    ]
                },
            )
        elif ns == "gtext":
            self._json(
                200,
                {
                    "candidates": [
                        {
                            "content": {
                                "role": "model",
                                "parts": [{"text": "抱歉，我无法按这个提示词生成图片。"}],
                            },
                            "finishReason": "STOP",
                        }
                    ]
                },
            )
        elif ns == "gstrip":
            if "generationConfig" in body:
                self._json(
                    400,
                    {
                        "error": {
                            "code": 400,
                            "message": "Unknown name \"responseModalities\": Cannot find field.",
                        }
                    },
                )
            else:
                self._json(
                    200,
                    {
                        "candidates": [
                            {"content": {"parts": [image_part(SMALL_PNG)]}, "finishReason": "STOP"}
                        ]
                    },
                )
        elif ns == "g401":
            self._json(
                401,
                {
                    "error": {
                        "code": 400,
                        "message": "API key not valid. Please pass a valid API key.",
                        "status": "INVALID_ARGUMENT",
                    }
                },
            )
        elif ns == "g500":
            self._json(500, {"error": {"code": 500, "message": "Internal error encountered."}})
        else:
            self._json(404, {"error": {"message": f"unknown route: {ns}"}})


class FakeServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address) -> None:  # noqa: D401 - 静音线程异常
        pass


def start_server() -> FakeServer:
    server = FakeServer(("127.0.0.1", 0), FakeAPI)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# ---------------------------------------------------------------------------
# 各测试分组
# ---------------------------------------------------------------------------
def test_contract() -> None:
    print("=== A. 接口契约 ===")
    check(
        "导入的是 bot/imagegen.py",
        Path(imagegen.__file__).resolve() == (BOT_DIR / "imagegen.py").resolve(),
        str(imagegen.__file__),
    )
    check("ImageGenError 继承 RuntimeError", issubclass(imagegen.ImageGenError, RuntimeError))
    signature = inspect.signature(imagegen.generate)
    params = signature.parameters
    expected = ["provider", "base_url", "api_key", "model", "prompt", "size", "timeout", "n"]
    check("generate 参数名与顺序符合契约", list(params) == expected, str(list(params)))
    check(
        "generate 参数全为关键字参数",
        all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values()),
        str({name: str(p.kind) for name, p in params.items()}),
    )
    defaults = {
        "provider": params["provider"].default,
        "size": params["size"].default,
        "timeout": params["timeout"].default,
        "n": params["n"].default,
    }
    check(
        "generate 默认值 provider=openai / size=1024x1024 / timeout=120.0 / n=1",
        defaults == {"provider": "openai", "size": "1024x1024", "timeout": 120.0, "n": 1},
        str(defaults),
    )


def test_openai_b64(base: str) -> None:
    print("=== B. OpenAI 兼容：b64_json ===")
    reset_requests()
    images = imagegen.generate(
        provider="OpenAI",  # 大小写不敏感
        base_url=base + "/b64",
        api_key="sk-test-openai-123",
        model="dall-e-3",
        prompt="一只戴帽子的猫",
        size="512x512",
    )
    check("返回 1 张图片", len(images) == 1, str(len(images)))
    check(
        "图片字节与假服务返回的真 PNG 一致（小图不重编码）",
        bool(images) and images[0] == SMALL_PNG,
        f"{len(images[0]) if images else 0} bytes",
    )
    check("图片可被 PIL 正常打开", bool(images) and is_valid_image(images[0]))
    request_list = posts("/b64/")
    check("请求落到 /b64/v1/images/generations", len(request_list) == 1, str(REQUESTS))
    request = request_list[0] if request_list else {}
    check(
        "Authorization: Bearer <key>",
        header_of(request, "Authorization") == "Bearer sk-test-openai-123",
        header_of(request, "Authorization"),
    )
    body = body_of(request)
    check(
        "body 含 model/prompt/n/size/response_format",
        body.get("model") == "dall-e-3"
        and body.get("prompt") == "一只戴帽子的猫"
        and body.get("n") == 1
        and body.get("size") == "512x512"
        and body.get("response_format") == "b64_json",
        json.dumps(body, ensure_ascii=False)[:220],
    )

    reset_requests()
    images = imagegen.generate(
        base_url=base + "/b64", api_key="sk-default", model="gpt-image-1", prompt="默认 provider"
    )
    check(
        "省略 provider 时默认走 openai",
        len(images) == 1 and len(posts("/b64/")) == 1,
        str(len(images)),
    )


def test_openai_url(base: str) -> None:
    print("=== C. OpenAI 兼容：url 形式下载 ===")
    reset_requests()
    images = imagegen.generate(
        base_url=base + "/url", api_key="sk-url-download", model="dall-e-3", prompt="海边日落"
    )
    check(
        "url 形式能下载到图片且字节一致",
        len(images) == 1 and images[0] == SMALL_PNG,
        f"{len(images)} 张",
    )
    check("确实用 GET 请求了 /img.png", len(gets("/img.png")) == 1, str(REQUESTS))
    check(
        "下载图片的 URL 里不带 api_key",
        all("sk-url-download" not in (r.get("path") or "") for r in REQUESTS),
    )


def test_openai_retry(base: str) -> None:
    print("=== D. OpenAI 兼容：response_format 自动重试 ===")
    reset_requests()
    images = imagegen.generate(
        base_url=base + "/retry", api_key="sk-retry", model="gpt-image-1", prompt="二次尝试"
    )
    request_list = posts("/retry/")
    check("共发出 2 次请求（先 400 再重试）", len(request_list) == 2, str(len(request_list)))
    check(
        "首次请求带 response_format",
        bool(request_list) and "response_format" in body_of(request_list[0]),
        json.dumps(body_of(request_list[0]) if request_list else {}, ensure_ascii=False)[:160],
    )
    check(
        "重试请求去掉了 response_format",
        len(request_list) >= 2 and "response_format" not in body_of(request_list[1]),
        json.dumps(body_of(request_list[1]) if len(request_list) >= 2 else {}, ensure_ascii=False)[:160],
    )
    check("重试后拿到图片", len(images) == 1 and images[0] == SMALL_PNG)


def test_gemini(base: str) -> None:
    print("=== E. Gemini ===")
    reset_requests()
    images = imagegen.generate(
        provider="gemini",
        base_url=base + "/gemini",
        api_key="gm-secret-key-123",
        model="gemini-test",
        prompt="画一只狗",
    )
    check("Gemini 返回 1 张图片", len(images) == 1 and images[0] == SMALL_PNG, str(len(images)))
    request_list = posts("/gemini/")
    check("请求路径为 /v1beta/models/{model}:generateContent", len(request_list) == 1, str(REQUESTS))
    request = request_list[0] if request_list else {}
    check(
        "路径拼写正确",
        path_of(request) == "/gemini/v1beta/models/gemini-test:generateContent",
        path_of(request),
    )
    check(
        "key 放在 query 里",
        query_of(request).get("key") == ["gm-secret-key-123"],
        str(query_of(request)),
    )
    body = body_of(request)
    check(
        "body 含 contents 文本",
        body.get("contents") == [{"role": "user", "parts": [{"text": "画一只狗"}]}],
        json.dumps(body, ensure_ascii=False)[:200],
    )
    check(
        "generationConfig.responseModalities=[TEXT,IMAGE]",
        (body.get("generationConfig") or {}).get("responseModalities") == ["TEXT", "IMAGE"],
        json.dumps(body.get("generationConfig"), ensure_ascii=False),
    )

    reset_requests()
    images = imagegen.generate(
        provider="GeMiNi",
        base_url=base + "/gemini",
        api_key="gm-secret-key-123",
        model="gemini-test",
        prompt="大小写测试",
    )
    check("provider 大小写不敏感", len(images) == 1 and len(posts("/gemini/")) == 1, str(len(images)))

    reset_requests()
    images = imagegen.generate(
        provider="gemini",
        base_url=base + "/gmulti",
        api_key="gm-secret-key-123",
        model="gemini-test",
        prompt="两张图",
        n=2,
    )
    check(
        "收集所有 inlineData / inline_data 图片（2 张）",
        len(images) == 2 and images[0] == SMALL_PNG and images[1] == SMALL_PNG2,
        f"{len(images)} 张",
    )


def test_errors(base: str) -> None:
    print("=== F. 错误处理 ===")
    check_error(
        "OpenAI 401 抛 ImageGenError 且含 401",
        lambda: imagegen.generate(
            base_url=base + "/auth", api_key="sk-bad-key-401", model="dall-e-3", prompt="x"
        ),
        contains=("401",),
        excludes=("sk-bad-key-401",),
    )
    check_error(
        "OpenAI 500 抛 ImageGenError 且含 500",
        lambda: imagegen.generate(
            base_url=base + "/boom", api_key="sk-boom", model="dall-e-3", prompt="x"
        ),
        contains=("500",),
    )
    check_error(
        "OpenAI 空 data 抛 ImageGenError",
        lambda: imagegen.generate(
            base_url=base + "/empty", api_key="sk-empty", model="dall-e-3", prompt="x"
        ),
        contains=("图片",),
    )
    check_error(
        "OpenAI 返回非 JSON 抛 ImageGenError",
        lambda: imagegen.generate(
            base_url=base + "/badjson", api_key="sk-badjson", model="dall-e-3", prompt="x"
        ),
        contains=("JSON",),
    )
    check_error(
        "坏 base64 抛 ImageGenError",
        lambda: imagegen.generate(
            base_url=base + "/badb64", api_key="sk-badb64", model="dall-e-3", prompt="x"
        ),
        contains=("base64",),
    )
    check_error(
        "base64 解出非图片抛 ImageGenError",
        lambda: imagegen.generate(
            base_url=base + "/notimage", api_key="sk-notimage", model="dall-e-3", prompt="x"
        ),
        contains=("图片",),
    )
    check_error(
        "下载到的内容不是图片抛 ImageGenError",
        lambda: imagegen.generate(
            base_url=base + "/urlbad", api_key="sk-urlbad", model="dall-e-3", prompt="x"
        ),
        contains=("图片",),
    )
    check_error(
        "服务端无 url 也无 b64 抛 ImageGenError",
        lambda: imagegen.generate(
            base_url=base + "/no-url", api_key="sk-nourl", model="dall-e-3", prompt="x"
        ),
        contains=("图片",),
    )
    check_error(
        "超时转 ImageGenError 且不泄露 key",
        lambda: imagegen.generate(
            base_url=base + "/slow",
            api_key="sk-slow-secret",
            model="dall-e-3",
            prompt="x",
            timeout=0.25,
        ),
        excludes=("sk-slow-secret",),
    )
    check_error(
        "Gemini 只有文字没有图片抛 ImageGenError（附文字）",
        lambda: imagegen.generate(
            provider="gemini",
            base_url=base + "/gtext",
            api_key="gm-key-gtext",
            model="gemini-test",
            prompt="x",
        ),
        contains=("抱歉",),
        excludes=("gm-key-gtext",),
    )
    check_error(
        "Gemini 401 抛 ImageGenError 且含 401",
        lambda: imagegen.generate(
            provider="gemini",
            base_url=base + "/g401",
            api_key="gm-bad-key-401",
            model="gemini-test",
            prompt="x",
        ),
        contains=("401",),
        excludes=("gm-bad-key-401",),
    )
    check_error(
        "Gemini 500 抛 ImageGenError 且含 500",
        lambda: imagegen.generate(
            provider="gemini",
            base_url=base + "/g500",
            api_key="gm-boom",
            model="gemini-test",
            prompt="x",
        ),
        contains=("500",),
    )
    check_error(
        "未知 provider 抛 ImageGenError",
        lambda: imagegen.generate(
            provider="mistral", base_url=base + "/b64", api_key="k", model="m", prompt="x"
        ),
        contains=("mistral", "openai", "gemini"),
    )
    check_error(
        "空 prompt 抛 ImageGenError",
        lambda: imagegen.generate(
            base_url=base + "/b64", api_key="sk", model="dall-e-3", prompt="   "
        ),
    )
    check_error(
        "空 api_key 抛 ImageGenError",
        lambda: imagegen.generate(base_url=base + "/b64", api_key="", model="dall-e-3", prompt="x"),
    )
    check_error(
        "网络不通转 ImageGenError",
        lambda: imagegen.generate(
            base_url=f"http://127.0.0.1:{closed_port()}",
            api_key="sk-net-secret",
            model="dall-e-3",
            prompt="x",
            timeout=1.0,
        ),
        excludes=("sk-net-secret",),
    )

    # 失败路径绝不能把 key 打印到 stdout/stderr
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
        for call in (
            lambda: imagegen.generate(
                base_url=base + "/auth", api_key="sk-log-secret", model="m", prompt="x"
            ),
            lambda: imagegen.generate(
                provider="gemini",
                base_url=base + "/g401",
                api_key="gm-log-secret",
                model="m",
                prompt="x",
            ),
        ):
            try:
                call()
            except imagegen.ImageGenError:
                pass
    output = captured.getvalue()
    check(
        "失败路径不打印 api_key",
        "sk-log-secret" not in output and "gm-log-secret" not in output,
        output[:200],
    )


def test_compression(base: str) -> None:
    print("=== G. 压缩（PIL）===")
    if Image is None:
        print("  [SKIP] PIL 不可用，跳过压缩断言")
        return
    reset_requests()
    images = imagegen.generate(
        base_url=base + "/big", api_key="sk-big", model="dall-e-3", prompt="一张大图"
    )
    check(
        "超过 1600px 的图被压缩成 JPEG",
        bool(images) and images[0][:2] == b"\xff\xd8",
        images[0][:8].hex() if images else "",
    )
    if images:
        with Image.open(io.BytesIO(images[0])) as image:
            size = image.size
            fmt = image.format
        check("压缩后最长边 1600", fmt == "JPEG" and max(size) == 1600, f"{fmt} {size}")
        check("压缩保持宽高比 2000x1200 → 1600x960", size == (1600, 960), str(size))
    else:
        check("压缩后最长边 1600", False, "没有图片")

    reset_requests()
    images = imagegen.generate(
        base_url=base + "/b64", api_key="sk-small", model="dall-e-3", prompt="一张小图"
    )
    check("未超过 1600px 的图原样返回（不重新编码）", bool(images) and images[0] == SMALL_PNG)


def test_multi_and_n(base: str) -> None:
    print("=== H. 多图与 n ===")
    reset_requests()
    images = imagegen.generate(
        base_url=base + "/multi", api_key="sk-multi", model="dall-e-3", prompt="三张图", n=3
    )
    check("n=3 返回 3 张图片", len(images) == 3, str(len(images)))
    check("3 张图都能正常打开", all(is_valid_image(data) for data in images))
    reset_requests()
    images = imagegen.generate(
        base_url=base + "/multi", api_key="sk-multi", model="dall-e-3", prompt="三张图"
    )
    check("默认 n=1 只取 1 张", len(images) == 1, str(len(images)))


def test_base_url_joining(base: str) -> None:
    print("=== I. base_url 拼接 ===")
    reset_requests()
    imagegen.generate(base_url=base + "/join-a/", api_key="sk", model="m", prompt="x")
    request_list = posts()
    check(
        "openai base 末尾多余 / 被去掉后拼 /v1/images/generations",
        len(request_list) == 1 and path_of(request_list[-1]) == "/join-a/v1/images/generations",
        str([path_of(r) for r in request_list]),
    )

    reset_requests()
    imagegen.generate(base_url=base + "/join-b/v1", api_key="sk", model="m", prompt="x")
    request_list = posts()
    check(
        "openai base 已含 /v1 时不重复拼接",
        len(request_list) == 1 and path_of(request_list[-1]) == "/join-b/v1/images/generations",
        str([path_of(r) for r in request_list]),
    )

    reset_requests()
    imagegen.generate(
        provider="gemini", base_url=base + "/gemini/", api_key="gk", model="gemini-test", prompt="x"
    )
    request_list = posts()
    check(
        "gemini base 末尾 / 被去掉后拼 /v1beta/models/...",
        len(request_list) == 1
        and path_of(request_list[-1])
        == "/gemini/v1beta/models/gemini-test:generateContent",
        str([path_of(r) for r in request_list]),
    )

    reset_requests()
    imagegen.generate(
        provider="gemini",
        base_url=base + "/gemini-v1beta/v1beta",
        api_key="gk",
        model="gemini-test",
        prompt="x",
    )
    request_list = posts()
    check(
        "gemini base 已含 /v1beta 时不重复拼接",
        len(request_list) == 1
        and path_of(request_list[-1])
        == "/gemini-v1beta/v1beta/models/gemini-test:generateContent",
        str([path_of(r) for r in request_list]),
    )


def test_gemini_compat_fallback(base: str) -> None:
    print("=== J. Gemini 兼容回退 ===")
    reset_requests()
    images = imagegen.generate(
        provider="gemini", base_url=base + "/gstrip", api_key="gk", model="gemini-test", prompt="x"
    )
    request_list = posts("/gstrip/")
    check(
        "400 不认 responseModalities 时去掉 generationConfig 重试一次",
        len(request_list) == 2
        and "generationConfig" in body_of(request_list[0])
        and "generationConfig" not in body_of(request_list[1])
        and len(images) == 1,
        f"{len(request_list)} 次请求 / {len(images)} 张图",
    )


def main() -> int:
    print("=== imagegen.py 自测（本地假服务，不调用真实 API）===")
    server = start_server()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        test_contract()
        test_openai_b64(base)
        test_openai_url(base)
        test_openai_retry(base)
        test_gemini(base)
        test_errors(base)
        test_compression(base)
        test_multi_and_n(base)
        test_base_url_joining(base)
        test_gemini_compat_fallback(base)
    finally:
        server.shutdown()
        server.server_close()

    total = PASSED + len(FAILED)
    print(f"\n断言：{PASSED}/{total} 通过")
    if FAILED:
        print("失败项：")
        for item in FAILED:
            print("  - " + item)
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
