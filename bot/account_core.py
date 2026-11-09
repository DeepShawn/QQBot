"""AI 账号核心：账号存储、OAuth 登录状态机、回调服务器、错误与脱敏。

只依赖 Python 标准库。接口约定见 docs/accounts-and-update.md §3 / §4。

对外主要 API（管理台 T4 只按这些调用）::

    AccountError / ERRORS / PROVIDER_IDS / PROVIDER_NAMES
    list_view()
    begin(provider) / poll(attempt_id) / answer(attempt_id, value) / cancel(attempt_id)
    signout(provider) / ensure_access(provider) / dispose()

辅助工具（给 accounts_<provider>.py 用）::

    Attempt / AttemptCancelled / CallbackServer
    generate_pkce() / random_state() / decode_jwt()
    parse_oauth_input() / verify_oauth_state() / require_https_url()
    http_json() / redact()
    load_provider() / get_record() / mark_expired()

安全约定：
- 账号文件 0600，原子写（临时文件 + os.replace）。
- 日志/错误/接口返回里不出现 token、完整授权 URL；一律过 redact()。
"""
from __future__ import annotations

import base64
import hashlib
import importlib
import json
import logging
import math
import os
import re
import secrets
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger("account_core")

# ---------------------------------------------------------------- 常量

PROVIDER_IDS = ("chatgpt", "claude", "gemini", "deepseek")
PROVIDER_NAMES = {
    "chatgpt": "ChatGPT",
    "claude": "Claude",
    "gemini": "Gemini",
    "deepseek": "DeepSeek",
}

ERRORS = {
    "not-signed-in": "还没有连接这个账号",
    "expired": "登录已过期，重新连接一次",
    "network": "网络连不上，检查网络后再试",
    "denied": "授权被拒绝了，再试一次",
    "timeout": "登录超时了，再试一次",
    "busy": "上一次登录还没结束，稍后再试",
    "unsupported": "这个版本还不支持这种账号",
    "generic": "授权没有完成，再试一次",
}

BOT_DIR = Path(__file__).resolve().parent
DATA_DIR = BOT_DIR / "data"
ACCOUNTS_FILE = DATA_DIR / "accounts.json"
ACCOUNTS_VERSION = 1
ACCOUNTS_MODE = 0o600

ATTEMPT_TIMEOUT_SECONDS = 300.0      # 单次登录 5 分钟
ATTEMPT_KEEP_SECONDS = 600.0         # 结束后保留 10 分钟供迟到轮询
REFRESH_SKEW_SECONDS = 60            # 过期前 60 秒就刷新
HTTP_TIMEOUT_SECONDS = 30.0

# 账号相关的 HTTP 请求默认用 Android Chrome 的 UA：不少平台的 WAF 会对非浏览器 UA
# 直接返回 429（用户实测 DeepSeek 登录首次即被拦）。调用方传入 headers 时以传入值为准。
# 注意：Claude / Gemini 按各自参考实现要求单独设置 UA，不用这个默认值。
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36"
)

_TERMINAL_PHASES = ("done", "failed", "cancelled")

CALLBACK_OK_HTML = (
    "<!doctype html><html><head><meta charset=\"utf-8\">"
    "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
    "<title>登录完成</title></head><body style=\"font-family:sans-serif;"
    "text-align:center;padding:40px;color:#222\"><h2>登录完成</h2>"
    "<p>登录完成，可以关掉这个页面了。</p></body></html>"
)
CALLBACK_FAIL_HTML = (
    "<!doctype html><html><head><meta charset=\"utf-8\">"
    "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
    "<title>登录没有完成</title></head><body style=\"font-family:sans-serif;"
    "text-align:center;padding:40px;color:#222\"><h2>登录没有完成</h2>"
    "<p>回到控制台再试一次。</p></body></html>"
)
CALLBACK_DENIED_HTML = (
    "<!doctype html><html><head><meta charset=\"utf-8\">"
    "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
    "<title>授权被拒绝</title></head><body style=\"font-family:sans-serif;"
    "text-align:center;padding:40px;color:#222\"><h2>授权被拒绝了</h2>"
    "<p>可以关掉这个页面，回到控制台重试。</p></body></html>"
)

__all__ = [
    "PROVIDER_IDS",
    "PROVIDER_NAMES",
    "ERRORS",
    "BROWSER_USER_AGENT",
    "AccountError",
    "Attempt",
    "AttemptCancelled",
    "CallbackServer",
    "list_view",
    "begin",
    "poll",
    "answer",
    "cancel",
    "signout",
    "ensure_access",
    "dispose",
    "generate_pkce",
    "random_state",
    "decode_jwt",
    "parse_oauth_input",
    "verify_oauth_state",
    "require_https_url",
    "http_json",
    "post_sse",
    "redact",
    "load_provider",
    "get_record",
    "mark_expired",
    "refresh_cooldown_remaining",
]


# ---------------------------------------------------------------- 错误

class AccountError(Exception):
    """code 是稳定错误码（见 ERRORS），message 是给用户看的中文短句。"""

    def __init__(self, code: str, message: str | None = None):
        self.code = code if code in ERRORS else "generic"
        self.message = str(message or ERRORS[self.code])
        super().__init__(self.message)

    def __str__(self) -> str:  # 保证 str(e) 只给用户文案
        return self.message


class AttemptCancelled(Exception):
    """登录被取消（用户点取消 / dispose / 账号退出）。内部异常，不暴露给界面。"""

    def __init__(self, message: str = "登录已取消"):
        self.message = message
        super().__init__(message)


# ---------------------------------------------------------------- 脱敏

_SECRET_PATTERNS = (
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 ***"),
    (
        re.compile(
            r"(?i)\b(access_token|refresh_token|id_token|authorization_code|"
            r"code_verifier|code|state)=([^&\s\"'<>]+)"
        ),
        r"\1=***",
    ),
    (re.compile(r"sk-[A-Za-z0-9_-]{8,}"), "***"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{3,}"), "***"),
)


def redact(text, secrets_: tuple | list = ()) -> str:
    """把 token / 授权码 / JWT 从文本里抹掉，用于日志与错误文案。"""
    out = str("" if text is None else text)
    for secret in secrets_ or ():
        value = str(secret or "")
        if len(value) >= 6:
            out = out.replace(value, "***")
    for pattern, repl in _SECRET_PATTERNS:
        out = pattern.sub(repl, out)
    return out


def _host_of(url: str) -> str:
    try:
        return urllib.parse.urlsplit(str(url)).netloc or "?"
    except Exception:
        return "?"


# ---------------------------------------------------------------- 账号存储

_store_lock = threading.RLock()


def _empty_store() -> dict:
    return {"version": ACCOUNTS_VERSION, "accounts": {}}


def _clean_store(raw) -> dict:
    accounts: dict[str, dict] = {}
    if isinstance(raw, dict) and isinstance(raw.get("accounts"), dict):
        for pid, record in raw["accounts"].items():
            name = str(pid or "").strip().lower()
            if name in PROVIDER_IDS and isinstance(record, dict):
                accounts[name] = dict(record)
    return {"version": ACCOUNTS_VERSION, "accounts": accounts}


def _read_store() -> dict:
    path = Path(ACCOUNTS_FILE)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _empty_store()
    except Exception:
        log.warning("账号文件读取失败，按空账号处理")
        return _empty_store()
    return _clean_store(raw)


def _write_store(store: dict) -> None:
    """临时文件 + os.replace 原子写，权限 0600。"""
    path = Path(ACCOUNTS_FILE)
    # bot/data 本来就存在（.gitignore 已忽略）；这里只管账号文件权限，不动目录
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(store, ensure_ascii=False, indent=2)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".accounts-", suffix=".tmp", dir=str(path.parent)
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, ACCOUNTS_MODE)
        os.replace(tmp, path)
        try:
            os.chmod(path, ACCOUNTS_MODE)
        except OSError:
            pass
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _normalize_record(record: dict, base: dict | None = None) -> dict:
    """统一记录字段，补齐 kind / refresh / expires_at / extra / updated_at。"""
    source = dict(base or {})
    for key, value in (record or {}).items():
        if value is not None:
            source[key] = value

    base_extra = dict(base.get("extra")) if isinstance((base or {}).get("extra"), dict) else {}
    new_extra = record.get("extra") if isinstance(record, dict) else None
    if isinstance(new_extra, dict):
        base_extra.update(new_extra)

    out = {
        "kind": "oauth",
        "access": str(source.get("access") or ""),
        "refresh": str(source.get("refresh") or ""),
        "expires_at": 0,
        "account": str(source.get("account") or ""),
        "extra": base_extra,
        "updated_at": int(time.time()),
    }
    try:
        out["expires_at"] = int(source.get("expires_at") or 0)
    except (TypeError, ValueError):
        out["expires_at"] = 0
    if not out["account"] and isinstance(base, dict):
        out["account"] = str(base.get("account") or "")
    # 保留 provider 自己写的其它字段（不覆盖上面规范字段）
    for key, value in source.items():
        if key not in out and value is not None:
            out[key] = value
    return out


def _provider_id(provider) -> str:
    pid = str(provider or "").strip().lower()
    if pid not in PROVIDER_IDS:
        raise AccountError("unsupported")
    return pid


def get_record(provider) -> dict | None:
    """读某个账号的本地记录（没有返回 None）。"""
    pid = _provider_id(provider)
    with _store_lock:
        record = _read_store()["accounts"].get(pid)
    return dict(record) if isinstance(record, dict) else None


def _get_record(pid: str) -> dict | None:
    with _store_lock:
        record = _read_store()["accounts"].get(pid)
    return dict(record) if isinstance(record, dict) else None


def _save_record(pid: str, record: dict) -> dict:
    normalized = _normalize_record(record)
    with _store_lock:
        store = _read_store()
        store["accounts"][pid] = normalized
        try:
            _write_store(store)
        except OSError as e:
            log.error("账号文件写入失败 provider=%s: %s", pid, redact(str(e)))
            raise AccountError("generic", "保存登录信息失败，检查存储权限") from e
    return normalized


def _delete_record(pid: str) -> None:
    with _store_lock:
        store = _read_store()
        if store["accounts"].pop(pid, None) is not None:
            try:
                _write_store(store)
            except OSError as e:
                log.error("账号文件写入失败 provider=%s: %s", pid, redact(str(e)))
                raise AccountError("generic", "保存登录信息失败，检查存储权限") from e


def mark_expired(provider) -> None:
    """聊天接口明确返回未授权时，把记录标记为过期，下一次会先刷新。"""
    pid = _provider_id(provider)
    with _store_lock:
        store = _read_store()
        record = store["accounts"].get(pid)
        if not isinstance(record, dict):
            return
        record["expires_at"] = int(time.time()) - 1
        record["updated_at"] = int(time.time())
        store["accounts"][pid] = record
        try:
            _write_store(store)
        except OSError:
            pass


# ---------------------------------------------------------------- 动态加载 provider 模块

_module_cache: dict[str, object] = {}
_module_lock = threading.RLock()


def load_provider(provider):
    """动态 import accounts_<provider>；缺失或不可用抛 AccountError(unsupported)。"""
    pid = _provider_id(provider)
    with _module_lock:
        cached = _module_cache.get(pid)
    if cached is not None:
        return cached
    try:
        module = importlib.import_module(f"accounts_{pid}")
    except ImportError as e:
        log.info("账号模块 accounts_%s 不存在：%s", pid, redact(str(e)))
        raise AccountError("unsupported") from e
    except Exception as e:
        log.warning("账号模块 accounts_%s 加载失败：%s", pid, redact(str(e)))
        raise AccountError("unsupported") from e
    if not callable(getattr(module, "begin", None)):
        raise AccountError("unsupported")
    module_id = str(getattr(module, "ID", pid) or pid)
    if module_id != pid:
        log.warning("账号模块 ID 不匹配：%s != %s", module_id, pid)
    with _module_lock:
        _module_cache[pid] = module
    return module


# ---------------------------------------------------------------- PKCE / 解析工具


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def generate_pkce() -> tuple[str, str]:
    """返回 (code_verifier, code_challenge)，S256。"""
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def random_state() -> str:
    return secrets.token_hex(16)


def decode_jwt(token: str) -> dict | None:
    """解析 JWT 的 payload（只解码，不验签）；失败返回 None。"""
    try:
        parts = str(token or "").split(".")
        if len(parts) != 3:
            return None
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        raw = base64.urlsafe_b64decode(payload.encode("ascii"))
        data = json.loads(raw.decode("utf-8", "replace"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _first_param(query: dict, key: str):
    values = query.get(key)
    if isinstance(values, list):
        return values[0] if values else None
    return values


def _loopback_host(hostname: str | None) -> bool:
    host = str(hostname or "").strip().lower().strip("[]")
    return host in ("localhost", "127.0.0.1", "::1")


def parse_oauth_input(value: str) -> dict:
    """解析用户粘贴的授权结果：完整回调 URL / code#state / code=… / 裸 code。

    只接受 https 网址；http 只允许本机回环地址（回调地址）。
    """
    text = str(value or "").strip()
    if not text:
        return {}
    if "://" in text:
        try:
            parts = urllib.parse.urlsplit(text)
        except Exception:
            return {}
        scheme = (parts.scheme or "").lower()
        if scheme == "https" or (scheme == "http" and _loopback_host(parts.hostname)):
            query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
            return {
                "code": _first_param(query, "code"),
                "state": _first_param(query, "state"),
            }
        return {}
    if "#" in text:
        code, state = text.split("#", 1)
        return {"code": code.strip() or None, "state": state.strip() or None}
    if "code=" in text:
        query = urllib.parse.parse_qs(text, keep_blank_values=True)
        return {
            "code": _first_param(query, "code"),
            "state": _first_param(query, "state"),
        }
    return {"code": text}


def verify_oauth_state(state: str | None, expected: str) -> None:
    """校验 OAuth state；不匹配统一给中文错误（不回显任何 state）。"""
    if not expected:
        return
    if str(state or "") != str(expected):
        raise AccountError("generic", "登录校验没有通过，再试一次")


def require_https_url(url: str) -> str:
    """界面要打开的授权地址必须是 https。"""
    try:
        parts = urllib.parse.urlsplit(str(url or ""))
    except Exception as e:
        raise AccountError("generic", "授权地址无效，再试一次") from e
    if parts.scheme != "https" or not parts.netloc:
        raise AccountError("generic", "授权地址无效，再试一次")
    return str(url)


# ---------------------------------------------------------------- 安全 HTTP JSON


def _http_error(
    error: urllib.error.HTTPError,
    *,
    label: str,
    error_map: dict | None,
    secrets_: tuple | list,
    allow_error_body: bool,
) -> AccountError:
    status = int(error.code)
    mapping = (error_map or {}).get(status)
    if mapping:
        code, message = mapping
        return AccountError(code, message)
    if status == 429:
        return AccountError("busy", "登录服务被限流（HTTP 429），稍后自动重试")
    detail = ""
    if allow_error_body:
        try:
            detail = error.read(400).decode("utf-8", "replace").strip()
        except Exception:
            detail = ""
    message = f"{label}返回错误（HTTP {status}）"
    if detail:
        message += "：" + redact(detail, secrets_)[:120]
    return AccountError("generic", message)


def http_json(
    url: str,
    payload: dict | None = None,
    *,
    form: bool = False,
    headers: dict | None = None,
    timeout: float = HTTP_TIMEOUT_SECONDS,
    label: str = "登录服务",
    secrets_: tuple | list = (),
    allow_error_body: bool = False,
    error_map: dict | None = None,
) -> dict:
    """POST/GET JSON，失败统一转成 AccountError（中文短句、已脱敏）。

    error_map: {HTTP 状态码: (AccountError.code, 文案或 None)}，聊天接口用它把
    401/403 映射成 expired、429 映射成额度提示。
    """
    data: bytes | None = None
    request_headers = {"Accept": "application/json", "User-Agent": BROWSER_USER_AGENT}
    if payload is not None:
        if form:
            data = urllib.parse.urlencode(payload).encode("utf-8")
            request_headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
    if headers:
        request_headers.update({str(k): str(v) for k, v in headers.items()})
    request = urllib.request.Request(
        str(url), data=data, headers=request_headers, method="POST" if data is not None else "GET"
    )
    try:
        with urllib.request.urlopen(request, timeout=float(timeout)) as response:
            raw = response.read()
    except urllib.error.HTTPError as e:
        raise _http_error(
            e,
            label=label,
            error_map=error_map,
            secrets_=secrets_,
            allow_error_body=allow_error_body,
        ) from e
    except TimeoutError as e:
        raise AccountError("timeout", ERRORS["timeout"]) from e
    except urllib.error.URLError as e:
        raise AccountError("network", ERRORS["network"]) from e
    except OSError as e:
        raise AccountError("network", ERRORS["network"]) from e
    try:
        parsed = json.loads(raw.decode("utf-8", "replace"))
    except Exception as e:
        raise AccountError("generic", f"{label}返回的内容看不懂，再试一次") from e
    if not isinstance(parsed, dict):
        raise AccountError("generic", f"{label}返回的内容看不懂，再试一次")
    return parsed


def post_sse(
    url: str,
    payload: dict,
    *,
    headers: dict | None = None,
    timeout: float = HTTP_TIMEOUT_SECONDS,
    label: str = "接口",
    error_map: dict | None = None,
    secrets_: tuple | list = (),
) -> list[dict]:
    """POST JSON 并收完一次 text/event-stream，返回解析后的 event 列表（非流式用法）。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request_headers = {
        "Accept": "text/event-stream",
        "Content-Type": "application/json",
        "User-Agent": BROWSER_USER_AGENT,
    }
    if headers:
        request_headers.update({str(k): str(v) for k, v in headers.items()})
    request = urllib.request.Request(
        str(url), data=data, headers=request_headers, method="POST"
    )
    try:
        response = urllib.request.urlopen(request, timeout=float(timeout))
    except urllib.error.HTTPError as e:
        raise _http_error(
            e,
            label=label,
            error_map=error_map,
            secrets_=secrets_,
            allow_error_body=True,
        ) from e
    except TimeoutError as e:
        raise AccountError("timeout", ERRORS["timeout"]) from e
    except urllib.error.URLError as e:
        raise AccountError("network", ERRORS["network"]) from e
    except OSError as e:
        raise AccountError("network", ERRORS["network"]) from e

    events: list[dict] = []
    data_lines: list[str] = []

    def flush() -> None:
        if not data_lines:
            return
        chunk = "\n".join(data_lines).strip()
        data_lines.clear()
        if not chunk or chunk == "[DONE]":
            return
        try:
            event = json.loads(chunk)
        except Exception as e:
            raise AccountError("generic", f"{label}返回的内容看不懂，再试一次") from e
        if isinstance(event, dict):
            events.append(event)

    try:
        for raw_line in response:
            try:
                line = raw_line.decode("utf-8", "replace").rstrip("\r\n")
            except Exception:
                continue
            if line == "":
                flush()
                continue
            if line.startswith(":"):
                continue
            if line.startswith("data:"):
                piece = line[5:].strip()
                if piece:
                    data_lines.append(piece)
        flush()
    except TimeoutError as e:
        raise AccountError("timeout", ERRORS["timeout"]) from e
    except urllib.error.URLError as e:
        raise AccountError("network", ERRORS["network"]) from e
    except OSError as e:
        raise AccountError("network", ERRORS["network"]) from e
    finally:
        try:
            response.close()
        except Exception:
            pass
    return events


# ---------------------------------------------------------------- 登录 Attempt


class Attempt:
    """一次登录的状态机（线程安全，管理台通过 poll 读取视图）。"""

    def __init__(self, provider: str, timeout: float | None = None):
        self.id = secrets.token_hex(8)
        self.provider = str(provider)
        self.created_at = time.time()
        self.deadline = self.created_at + float(
            ATTEMPT_TIMEOUT_SECONDS if timeout is None else timeout
        )
        self.finished_at = 0.0
        self.url = ""
        self.user_code = ""
        self.placeholder = ""
        self.account = ""
        self.error = ""
        self._phase = "starting"
        self._message = "正在准备登录…"
        self._lock = threading.RLock()
        self._answer = ""
        self._answer_event = threading.Event()
        self._cancel_event = threading.Event()
        self._timer: threading.Timer | None = None

    # ---------------- 状态读取
    @property
    def phase(self) -> str:
        with self._lock:
            return self._phase

    @property
    def finished(self) -> bool:
        with self._lock:
            return self._phase in _TERMINAL_PHASES

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.time())

    def view(self) -> dict:
        with self._lock:
            data = {
                "id": self.id,
                "provider": self.provider,
                "phase": self._phase,
                "message": self._message,
            }
            if self._phase == "open":
                data["url"] = self.url
            elif self._phase == "device":
                data["url"] = self.url
                data["userCode"] = self.user_code
            elif self._phase == "prompt":
                data["placeholder"] = self.placeholder
            elif self._phase == "done":
                data["account"] = self.account
            elif self._phase == "failed":
                data["error"] = self.error or self._message
            return data

    # ---------------- 给 provider 用
    def check_cancelled(self) -> None:
        if self._cancel_event.is_set():
            raise AttemptCancelled()

    def set_phase(self, phase: str, message: str | None = None) -> None:
        self.check_cancelled()
        self._set(phase, message)

    def notify_url(self, url: str, message: str = "在浏览器里完成登录") -> None:
        self.check_cancelled()
        self._set("open", message or "在浏览器里完成登录", url=require_https_url(url))

    def notify_device(
        self,
        url: str,
        user_code: str,
        message: str = "在打开的网页里输入下面的代码",
    ) -> None:
        self.check_cancelled()
        self._set(
            "device",
            message or "在打开的网页里输入下面的代码",
            url=require_https_url(url),
            user_code=str(user_code or ""),
        )

    def prompt(self, placeholder: str, message: str = "在下面粘贴授权结果") -> str:
        """切到 prompt 阶段并阻塞等待 answer()；取消抛 AttemptCancelled，超时抛 AccountError。"""
        self.check_cancelled()
        with self._lock:
            self._answer = ""
            self._answer_event.clear()
            self._set("prompt", message or "在下面粘贴授权结果", placeholder=str(placeholder or ""))
        while True:
            if self._answer_event.wait(0.2):
                break
            self.check_cancelled()
            if time.time() >= self.deadline:
                raise AccountError("timeout")
        self.check_cancelled()
        with self._lock:
            value = self._answer
            self._answer = ""
            self._answer_event.clear()
        if value is None:
            raise AttemptCancelled()
        self._set("starting", "正在完成登录…")
        return str(value)

    def submit_answer(self, value: str) -> bool:
        with self._lock:
            if self._phase != "prompt":
                return False
            self._answer = str(value)
            self._answer_event.set()
            return True

    # ---------------- 内部状态切换
    def _set(self, phase: str, message: str | None = None, **fields) -> bool:
        with self._lock:
            if self._phase in _TERMINAL_PHASES and phase not in _TERMINAL_PHASES:
                return False
            self._phase = phase
            if message is not None:
                self._message = str(message)
            for key, value in fields.items():
                setattr(self, key, value)
            return True

    def _finish(self, phase: str, *, account: str | None = None,
                error: str | None = None, message: str | None = None) -> bool:
        with self._lock:
            if self._phase in _TERMINAL_PHASES:
                return False
            self._phase = phase
            if message is not None:
                self._message = str(message)
            if account is not None:
                self.account = str(account)
            if error is not None:
                self.error = str(error)
            self.finished_at = time.time()
            self._answer_event.set()
        self._stop_timer()
        return True

    def finish_done(self, account: str = "") -> bool:
        return self._finish("done", account=account, message="登录完成")

    def finish_failed(self, code: str = "generic", message: str | None = None) -> bool:
        text = str(message or ERRORS.get(code, ERRORS["generic"]))
        return self._finish("failed", error=text, message=text)

    def finish_cancelled(self) -> bool:
        with self._lock:
            if self._phase in _TERMINAL_PHASES:
                return False
            self._cancel_event.set()
            self._phase = "cancelled"
            self._message = "登录已取消"
            self.finished_at = time.time()
            self._answer_event.set()
        self._stop_timer()
        return True

    def expire_now(self) -> bool:
        """5 分钟到点：标记失败并通知 worker 尽快退出。"""
        with self._lock:
            if self._phase in _TERMINAL_PHASES:
                return False
            self._cancel_event.set()
            self._phase = "failed"
            self.error = ERRORS["timeout"]
            self._message = ERRORS["timeout"]
            self.finished_at = time.time()
            self._answer_event.set()
        self._stop_timer()
        log.info("账号登录超时 provider=%s", self.provider)
        return True

    def cancel(self) -> bool:
        return self.finish_cancelled()

    def _start_timer(self) -> None:
        if ATTEMPT_TIMEOUT_SECONDS <= 0:
            return
        timer = threading.Timer(ATTEMPT_TIMEOUT_SECONDS, self.expire_now)
        timer.daemon = True
        with self._lock:
            self._timer = timer
        timer.start()

    def _stop_timer(self) -> None:
        with self._lock:
            timer = self._timer
            self._timer = None
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass


# ---------------------------------------------------------------- 回调服务器


class CallbackServer:
    """绑定 127.0.0.1 的 OAuth 回调服务器：只收一次请求，返回 query dict。"""

    def __init__(
        self,
        port: int = 0,
        path: str = "/callback",
        host: str = "127.0.0.1",
        redirect_host: str = "localhost",
        name: str = "oauth-callback",
    ):
        self._path = path if str(path).startswith("/") else "/" + str(path)
        self._result: dict | None = None
        self._claimed = False
        self._closed = False
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._redirect_host = str(redirect_host or "localhost")
        owner = self

        class _Handler(BaseHTTPRequestHandler):
            server_version = "QQBotOAuth/1.0"
            # 浏览器会预连接空闲 socket；不设超时的话单线程服务会一直卡在它身上，真正的回调进不来
            timeout = 10

            def log_message(self, fmt, *args):  # 不打印访问日志（URL 里有授权码）
                return

            def do_GET(self):
                owner._handle(self)

            def do_POST(self):
                owner._handle(self)

        try:
            self._httpd = ThreadingHTTPServer((str(host), int(port)), _Handler)
        except OSError as e:
            raise AccountError(
                "generic", "本地回调端口被占用，改用粘贴方式"
            ) from e
        self._httpd.daemon_threads = True
        self.port = int(self._httpd.server_address[1])
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name=name, daemon=True
        )
        self._thread.start()

    @property
    def redirect_uri(self) -> str:
        host = self._redirect_host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"http://{host}:{self.port}{self._path}"

    def _respond(self, request: BaseHTTPRequestHandler, status: int, html: str) -> None:
        body = html.encode("utf-8")
        request.send_response(int(status))
        request.send_header("Content-Type", "text/html; charset=utf-8")
        request.send_header("Cache-Control", "no-store")
        request.send_header("Content-Length", str(len(body)))
        request.end_headers()
        try:
            request.wfile.write(body)
        except Exception:
            pass

    def _handle(self, request: BaseHTTPRequestHandler) -> None:
        try:
            parts = urllib.parse.urlsplit(request.path or "/")
            query = {
                key: values[0]
                for key, values in urllib.parse.parse_qs(
                    parts.query, keep_blank_values=True
                ).items()
            }
            if parts.path != self._path:
                self._respond(request, 404, CALLBACK_FAIL_HTML)
                return
            with self._lock:
                if self._claimed or self._closed:
                    self._respond(request, 409, CALLBACK_FAIL_HTML)
                    return
                if not query.get("code") and not query.get("error"):
                    self._respond(request, 400, CALLBACK_FAIL_HTML)
                    return
                self._claimed = True
                self._result = dict(query)
            success = bool(query.get("code")) and not query.get("error")
            self._respond(
                request, 200, CALLBACK_OK_HTML if success else CALLBACK_DENIED_HTML
            )
            self._event.set()
        except Exception:
            try:
                self._respond(request, 500, CALLBACK_FAIL_HTML)
            except Exception:
                pass

    def wait(self, timeout: float | None = None, attempt: Attempt | None = None) -> dict:
        """阻塞到收到回调；取消抛 AttemptCancelled，超时抛 AccountError(timeout)。"""
        limit = ATTEMPT_TIMEOUT_SECONDS if timeout is None else max(0.0, float(timeout))
        deadline = time.time() + limit
        while True:
            if self._event.wait(0.2):
                break
            if attempt is not None:
                attempt.check_cancelled()
            if time.time() >= deadline:
                raise AccountError("timeout")
        with self._lock:
            result = dict(self._result) if isinstance(self._result, dict) else None
            closed = self._closed
        if result is None:
            if closed:
                raise AccountError("generic", "登录没有完成，再试一次")
            raise AccountError("generic")
        return result

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._event.set()
        try:
            self._httpd.shutdown()
        except Exception:
            pass
        try:
            self._httpd.server_close()
        except Exception:
            pass
        try:
            self._thread.join(timeout=1.0)
        except Exception:
            pass


# ---------------------------------------------------------------- 登录注册表

_registry_lock = threading.RLock()
_attempts: dict[str, Attempt] = {}
_active: dict[str, Attempt] = {}


def _purge_finished() -> None:
    now = time.time()
    expired = [
        attempt_id
        for attempt_id, attempt in _attempts.items()
        if attempt.finished and attempt.finished_at and now - attempt.finished_at > ATTEMPT_KEEP_SECONDS
    ]
    for attempt_id in expired:
        _attempts.pop(attempt_id, None)


def _find_attempt(attempt_id) -> Attempt:
    key = str(attempt_id or "").strip()
    with _registry_lock:
        _purge_finished()
        attempt = _attempts.get(key)
    if attempt is None:
        raise AccountError("generic", "这次登录已经结束，重新开始一次")
    return attempt


def _start_attempt(attempt: Attempt) -> None:
    attempt._start_timer()
    thread = threading.Thread(
        target=_run_attempt, args=(attempt,), name=f"login-{attempt.provider}", daemon=True
    )
    thread.start()


def _run_attempt(attempt: Attempt) -> None:
    pid = attempt.provider
    try:
        attempt.set_phase("starting", "正在准备登录…")
        module = load_provider(pid)
        record = module.begin(attempt)
        attempt.check_cancelled()  # begin 返回后、落盘前再确认一次没被取消/超时
        if not isinstance(record, dict):
            raise AccountError("generic", "登录服务没有返回有效结果，再试一次")
        saved = _save_record(pid, record)
        if not saved.get("access"):
            raise AccountError("generic", "登录服务没有返回有效结果，再试一次")
        account = str(saved.get("account") or "").strip()
        if not account:
            label = getattr(module, "label", None)
            if callable(label):
                try:
                    account = str(label(saved) or "").strip()
                except Exception:
                    account = ""
        attempt.finish_done(account)
        log.info("账号登录完成 provider=%s", pid)
    except AttemptCancelled:
        attempt.finish_cancelled()
    except AccountError as e:
        attempt.finish_failed(e.code, e.message)
        log.info("账号登录失败 provider=%s code=%s", pid, e.code)
    except Exception as e:  # 兜底：不把堆栈甩给界面
        attempt.finish_failed("generic", ERRORS["generic"])
        log.warning("账号登录异常 provider=%s: %s", pid, redact(str(e)))
    finally:
        attempt._stop_timer()
        with _registry_lock:
            if _active.get(pid) is attempt:
                _active.pop(pid, None)


# ---------------------------------------------------------------- 对外 API


def list_view() -> list[dict]:
    """账号列表视图（模块缺失也返回一项，signedIn=False + error=unsupported）。"""
    result: list[dict] = []
    for pid in PROVIDER_IDS:
        entry = {
            "id": pid,
            "name": PROVIDER_NAMES.get(pid, pid),
            "signedIn": False,
            "account": None,
            "models": [],
            "default_model": "",
            "error": None,
        }
        module = None
        try:
            module = load_provider(pid)
        except AccountError:
            entry["error"] = "unsupported"
        except Exception:
            entry["error"] = "unsupported"
        if module is not None:
            try:
                name = str(getattr(module, "NAME", "") or "").strip()
                if name:
                    entry["name"] = name
                models = [
                    str(item)
                    for item in (getattr(module, "MODELS", None) or [])
                    if str(item or "").strip()
                ]
                entry["models"] = models
                default = str(getattr(module, "DEFAULT_MODEL", "") or "").strip()
                entry["default_model"] = default or (models[0] if models else "")
            except Exception:
                pass
        if module is None:
            result.append(entry)
            continue
        try:
            record = _get_record(pid)
        except Exception:
            record = None
        if isinstance(record, dict):
            entry["signedIn"] = True
            account = str(record.get("account") or "").strip()
            if not account:
                label = getattr(module, "label", None)
                if callable(label):
                    try:
                        account = str(label(record) or "").strip()
                    except Exception:
                        account = ""
            entry["account"] = account or None
            entry["error"] = None
        result.append(entry)
    return result


def begin(provider: str) -> dict:
    """立刻返回并起后台线程跑 accounts_<provider>.begin；同一 provider 只保留一个。"""
    pid = _provider_id(provider)
    load_provider(pid)  # 模块缺失直接给 unsupported，别起线程
    attempt = Attempt(pid)
    with _registry_lock:
        _purge_finished()
        old = _active.get(pid)
        if old is not None and not old.finished:
            old.cancel()
        _active[pid] = attempt
        _attempts[attempt.id] = attempt
    _start_attempt(attempt)
    return {"attempt": attempt.view()}


def poll(attempt_id: str) -> dict:
    attempt = _find_attempt(attempt_id)
    return {"attempt": attempt.view()}


def answer(attempt_id: str, value: str) -> dict:
    attempt = _find_attempt(attempt_id)
    text = str(value or "").strip()
    if not text:
        raise AccountError("generic", "输入内容不能为空")
    if not attempt.submit_answer(text):
        raise AccountError("generic", "现在不需要输入内容")
    return {"attempt": attempt.view()}


def cancel(attempt_id: str) -> dict:
    attempt = _find_attempt(attempt_id)
    attempt.cancel()
    return {"attempt": attempt.view()}


def signout(provider: str) -> dict:
    pid = _provider_id(provider)
    with _registry_lock:
        old = _active.get(pid)
        if old is not None and not old.finished:
            old.cancel()
        _active.pop(pid, None)
    _delete_record(pid)
    log.info("账号已退出 provider=%s", pid)
    return {"ok": True, "accounts": list_view()}


_refresh_locks_lock = threading.Lock()
_refresh_locks: dict[str, threading.Lock] = {}

# 刷新被限流时的冷却：冷却期内不再发 HTTP，避免越刷越限
REFRESH_COOLDOWN_SECONDS = 600.0
_cooldown_lock = threading.Lock()
_refresh_cooldowns: dict[str, float] = {}


def _cooldown_remaining(pid: str) -> float:
    with _cooldown_lock:
        until = float(_refresh_cooldowns.get(pid) or 0.0)
    return max(0.0, until - time.time())


def _set_refresh_cooldown(pid: str, seconds: float | None = None) -> float:
    window = float(REFRESH_COOLDOWN_SECONDS if seconds is None else seconds)
    with _cooldown_lock:
        _refresh_cooldowns[pid] = time.time() + max(1.0, window)
    return max(1.0, window)


def _clear_refresh_cooldown(pid: str) -> None:
    with _cooldown_lock:
        _refresh_cooldowns.pop(pid, None)


def _busy_message(remaining: float) -> str:
    minutes = max(1, int(math.ceil(max(0.0, remaining) / 60.0)))
    return f"登录服务被限流，约 {minutes} 分钟后自动重试"


def refresh_cooldown_remaining(provider) -> float:
    """当前 provider 的刷新冷却剩余秒数（0 = 不在冷却）。"""
    pid = _provider_id(provider)
    return _cooldown_remaining(pid)


def _refresh_lock(pid: str) -> threading.Lock:
    with _refresh_locks_lock:
        lock = _refresh_locks.get(pid)
        if lock is None:
            lock = threading.Lock()
            _refresh_locks[pid] = lock
        return lock


def _needs_refresh(record: dict) -> bool:
    access = str(record.get("access") or "")
    if not access:
        return True
    try:
        expires_at = int(record.get("expires_at") or 0)
    except (TypeError, ValueError):
        expires_at = 0
    if expires_at <= 0:
        return False  # 没有过期时间，当作长期有效
    return time.time() >= expires_at - REFRESH_SKEW_SECONDS


def ensure_access(provider: str) -> dict:
    """取有效 access：无记录抛 not-signed-in；过期先 refresh(record) 再保存。"""
    pid = _provider_id(provider)
    module = load_provider(pid)
    record = _get_record(pid)
    if not isinstance(record, dict):
        raise AccountError("not-signed-in")
    if not _needs_refresh(record):
        return record
    remaining = _cooldown_remaining(pid)
    if remaining > 0:
        raise AccountError("busy", _busy_message(remaining))
    with _refresh_lock(pid):
        # 等锁期间可能已经被别的线程刷新过
        record = _get_record(pid) or record
        if not _needs_refresh(record):
            return record
        remaining = _cooldown_remaining(pid)
        if remaining > 0:
            raise AccountError("busy", _busy_message(remaining))
        if not str(record.get("refresh") or "").strip():
            raise AccountError("expired")
        refresh_fn = getattr(module, "refresh", None)
        if not callable(refresh_fn):
            raise AccountError("expired")
        try:
            fresh = refresh_fn(record)
        except AccountError as e:
            if e.code == "busy":
                remaining = _set_refresh_cooldown(pid)
                log.info("账号刷新被限流，进入冷却 provider=%s", pid)
                raise AccountError("busy", _busy_message(remaining)) from e
            if e.code in ("network", "timeout"):
                raise
            raise AccountError("expired") from e
        except Exception as e:
            log.info("刷新账号失败 provider=%s: %s", pid, redact(str(e)))
            raise AccountError("expired") from e
        if not isinstance(fresh, dict) or not str(fresh.get("access") or "").strip():
            raise AccountError("expired")
        merged = _normalize_record(fresh, base=record)
        if not merged.get("account"):
            label = getattr(module, "label", None)
            if callable(label):
                try:
                    merged["account"] = str(label(merged) or "")
                except Exception:
                    merged["account"] = ""
        saved = _save_record(pid, merged)
        _clear_refresh_cooldown(pid)
        log.info("账号已刷新 provider=%s", pid)
        return saved


def dispose() -> None:
    """管理台退出时取消所有在途登录并清空注册表。"""
    with _registry_lock:
        attempts = list(_attempts.values())
        _attempts.clear()
        _active.clear()
    for attempt in attempts:
        try:
            attempt.cancel()
        except Exception:
            pass
        attempt._stop_timer()
