"""官方 QQ 机器人通道（q.qq.com）：WebSocket 收事件 + 被动回复。

与 OneBotClient 接口兼容（main.py 按 config.official.enabled 切换）：
- ``async run()``：连网关、心跳、断线重连；
- ``async call(action, **params)``：只支持被动回复 send_group_msg / send_private_msg；
- ``async close()``；``connected`` / ``self_id`` / ``url`` / ``reconnect_seconds``。

官方通道限制：
- 只能被动回复（对方先发消息，群 5 分钟内、C2C 60 分钟内）；
- 群聊只有 @ 机器人的消息（GROUP_AT_MESSAGE_CREATE），C2C 私聊（C2C_MESSAGE_CREATE）；
- 没有群成员/禁言/踢人/撤回/消息历史接口，call 其它动作一律抛 OfficialError。

只依赖 Python 标准库 + websockets。
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path

import websockets

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
API_BASE = "https://api.sgroup.qq.com"
SANDBOX_API_BASE = "https://sandbox.api.sgroup.qq.com"
INTENTS = 1 << 25  # 群聊/C2C 消息相关
TOKEN_REFRESH_MARGIN = 300.0
GROUP_CONTEXT_SECONDS = 300.0
C2C_CONTEXT_SECONDS = 3600.0
MAX_MSG_SEQ = 5
RETRY_429_SLEEP = 1.0
MAX_WS_SIZE = 2 ** 24
STATE_FILE_NAME = "official_ids.json"
NAMES_FILE_NAME = "official_names.json"

__all__ = [
    "OfficialError",
    "OfficialClient",
    "load_names",
    "get_nickname",
    "set_nickname",
    "names_path",
]

_names_lock = threading.RLock()


def names_path(data_dir=None) -> Path:
    """official_names.json：openid -> 用户自己设置的昵称。"""
    base = (
        Path(data_dir)
        if data_dir is not None
        else (Path(__file__).resolve().parent / "data")
    )
    return base / NAMES_FILE_NAME


def load_names(data_dir=None) -> dict[str, str]:
    path = names_path(data_dir)
    with _names_lock:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    items = data.get("names") if isinstance(data, dict) else None
    if not isinstance(items, dict):
        return {}
    result: dict[str, str] = {}
    for key, value in items.items():
        openid = str(key or "").strip()
        name = str(value or "").strip()
        if openid and name:
            result[openid] = name
    return result


def get_nickname(data_dir, openid) -> str:
    openid = str(openid or "").strip()
    if not openid:
        return ""
    return load_names(data_dir).get(openid, "")


def set_nickname(data_dir, openid, name) -> str:
    openid = str(openid or "").strip()
    name = str(name or "").strip()
    if not openid or not name:
        raise ValueError("openid 或名字为空")
    path = names_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _names_lock:
        names = load_names(data_dir)
        names[openid] = name[:24]
        body = json.dumps(
            {"version": 1, "names": names}, ensure_ascii=False
        )
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(body, encoding="utf-8")
        tmp.replace(path)
    return names[openid]


class OfficialError(RuntimeError):
    """官方通道不可用/不支持/网络失败（消息中文，不含 token/secret）。"""


class _HttpError(Exception):
    def __init__(self, status: int):
        self.status = int(status)
        super().__init__(f"HTTP {self.status}")


class _NetworkError(Exception):
    pass


class _Reconnect(Exception):
    """op7/op9：需要重连并重新 identify。"""


def _http_json(method: str, url: str, body=None, headers=None, timeout: float = 20.0) -> dict:
    """同步 HTTP JSON（放 asyncio.to_thread 里跑）；错误抛 _HttpError/_NetworkError。"""
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers=dict(headers or {}), method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=float(timeout)) as response:
            raw = response.read()
    except urllib.error.HTTPError as e:
        raise _HttpError(int(e.code)) from e
    except urllib.error.URLError as e:
        raise _NetworkError("网络错误") from e
    except OSError as e:
        raise _NetworkError("网络错误") from e
    try:
        parsed = json.loads(raw.decode("utf-8", "replace"))
    except Exception as e:
        raise OfficialError("官方接口返回的内容看不懂") from e
    if not isinstance(parsed, dict):
        raise OfficialError("官方接口返回格式异常")
    return parsed


async def _connect_ws(url: str):
    return await websockets.connect(url, max_size=MAX_WS_SIZE, ping_interval=None)


# ---------------------------------------------------------------- openid -> 整数


class _IdMap:
    """openid -> 稳定正整数（crc32 + 线性探测），持久化到 official_ids.json。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._map: dict[str, int] = {}
        self._reverse: dict[int, str] = {}
        self._load()

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return
        items = data.get("ids") if isinstance(data, dict) else None
        if not isinstance(items, dict):
            return
        for key, value in items.items():
            try:
                number = int(value)
            except (TypeError, ValueError):
                continue
            if number > 0:
                self._map[str(key)] = number
                self._reverse[number] = str(key)

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            body = json.dumps({"version": 1, "ids": self._map}, ensure_ascii=False)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(body, encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass

    def get(self, kind: str, openid: str) -> int:
        key = f"{str(kind)}:{str(openid)}"
        with self._lock:
            existing = self._map.get(key)
            if existing:
                return existing
            raw = zlib.crc32(str(openid).encode("utf-8")) & 0x7FFFFFFF
            value = raw or 1
            while value in self._reverse:
                value = (value + 1) & 0x7FFFFFFF
                if value == 0:
                    value = 1
            self._map[key] = value
            self._reverse[value] = key
            self._save()
            return value

    def reverse(self, kind: str, value: int) -> str | None:
        key = self._reverse.get(int(value))
        if not key:
            return None
        prefix = f"{str(kind)}:"
        if not key.startswith(prefix):
            return None
        return key[len(prefix):]


def _normalize_segments(message) -> list[dict]:
    if isinstance(message, str):
        return [{"type": "text", "data": {"text": message}}]
    if isinstance(message, list):
        return [seg for seg in message if isinstance(seg, dict)]
    if isinstance(message, dict):
        return [message]
    return []


def _image_upload_body(segment: dict, file_type: int) -> dict:
    data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
    body = {"file_type": int(file_type), "srv_send_msg": False}
    file_value = str(data.get("file") or "").strip()
    url_value = str(data.get("url") or "").strip()
    if file_value.startswith("base64://"):
        body["file_data"] = file_value[len("base64://"):]
    elif file_value.startswith(("http://", "https://")):
        body["url"] = file_value
    elif url_value.startswith(("http://", "https://")):
        body["url"] = url_value
    elif url_value.startswith("base64://"):
        body["file_data"] = url_value[len("base64://"):]
    else:
        raise OfficialError("图片需要 base64 或 http(s) 地址")
    return body


# ---------------------------------------------------------------- 客户端


class OfficialClient:
    """官方 QQ 机器人 WebSocket 客户端（接口与 OneBotClient 兼容）。"""

    kind = "official"

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        sandbox: bool = False,
        logger: logging.Logger | None = None,
        on_event=None,
        request_timeout: float = 20.0,
        data_dir=None,
    ):
        self.app_id = str(app_id or "").strip()
        self.app_secret = str(app_secret or "").strip()
        self.sandbox = bool(sandbox)
        self.log = logger or logging.getLogger("official")
        self.on_event = on_event
        self.request_timeout = float(request_timeout or 20.0)
        self.url = "官方 QQ 机器人（WebSocket）"
        self.reconnect_seconds = 5.0
        self.connected = asyncio.Event()
        self.self_id = 0
        self._bot_raw_ids: set[str] = set()
        self._ws = None
        self._token = ""
        self._token_expires = 0.0
        self._token_lock = asyncio.Lock()
        self._now = time.time
        self._contexts: dict[str, dict] = {}
        self._contexts_lock = threading.RLock()
        self._last_s = 0
        self._heartbeat_task: asyncio.Task | None = None
        data_path = (
            Path(data_dir) if data_dir is not None else (Path(__file__).resolve().parent / "data")
        )
        self._data_dir = Path(data_path)
        self._ids = _IdMap(self._data_dir / STATE_FILE_NAME)

    # ---------- 属性 ----------
    @property
    def api_base(self) -> str:
        return SANDBOX_API_BASE if self.sandbox else API_BASE

    # ---------- token / gateway ----------
    async def _get_token(self) -> str:
        async with self._token_lock:
            now = self._now()
            if self._token and now < self._token_expires - TOKEN_REFRESH_MARGIN:
                return self._token
            if not self.app_id or not self.app_secret:
                raise OfficialError("官方机器人未配置 AppID 或 AppSecret")
            try:
                data = await asyncio.to_thread(
                    _http_json,
                    "POST",
                    TOKEN_URL,
                    {"appId": self.app_id, "clientSecret": self.app_secret},
                    {"Content-Type": "application/json", "Accept": "application/json"},
                    self.request_timeout,
                )
            except _HttpError as e:
                raise OfficialError(f"获取官方 access_token 失败（HTTP {e.status}）") from None
            except OfficialError:
                raise
            except Exception:
                raise OfficialError(
                    "获取官方 access_token 失败，检查网络/AppID/AppSecret"
                ) from None
            token = str(data.get("access_token") or "").strip()
            if not token:
                raise OfficialError("官方 access_token 返回为空")
            try:
                expires_in = int(float(data.get("expires_in") or 0))
            except (TypeError, ValueError):
                expires_in = 0
            if expires_in <= 0:
                expires_in = 7200
            self._token = token
            self._token_expires = now + expires_in
            return token

    async def _request(
        self, method: str, path: str, body=None, *, retry_429: bool = True
    ) -> dict:
        token = await self._get_token()
        url = self.api_base.rstrip("/") + path
        headers = {
            "Authorization": f"QQBot {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            data = await asyncio.to_thread(
                _http_json, method, url, body, headers, self.request_timeout
            )
        except _HttpError as e:
            if e.status == 429 and retry_429:
                await asyncio.sleep(RETRY_429_SLEEP)
                return await self._request(
                    method, path, body, retry_429=False
                )
            raise OfficialError(f"官方接口请求失败（HTTP {e.status}）") from None
        except OfficialError:
            raise
        except Exception:
            raise OfficialError("官方接口网络错误，稍后再试") from None
        return data

    async def _get_gateway(self) -> str:
        data = await self._request("GET", "/gateway/bot")
        url = str(data.get("url") or "").strip()
        if not url:
            raise OfficialError("获取官方网关地址失败")
        return url

    async def _fetch_self_id(self) -> None:
        try:
            data = await self._request("GET", "/users/@me")
        except OfficialError:
            return
        raw = data.get("id")
        if raw is None and isinstance(data.get("user"), dict):
            raw = data["user"].get("id")
        if raw is None:
            return
        self._bot_raw_ids.add(str(raw))
        try:
            self.self_id = int(raw)
        except (TypeError, ValueError):
            self.self_id = self._ids.get("bot", str(raw))

    # ---------- 连接 / 心跳 ----------
    async def run(self):
        backoff = max(0.5, float(self.reconnect_seconds))
        while True:
            try:
                await self._connect_once()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.log.warning("官方通道连接失败: %s", _safe_message(e))
            finally:
                self.connected.clear()
                self._ws = None
                self._cancel_heartbeat()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _connect_once(self) -> None:
        token = await self._get_token()
        ws_url = await self._get_gateway()
        conn = await _connect_ws(ws_url)
        self._ws = conn
        self._last_s = 0
        try:
            identify = {
                "op": 2,
                "d": {
                    "token": f"QQBot {token}",
                    "intents": INTENTS,
                    "shard": [0, 1],
                    "properties": {
                        "$os": "linux",
                        "$browser": "qqbot",
                        "$device": "phone",
                    },
                },
            }
            await conn.send(json.dumps(identify, ensure_ascii=False))
            try:
                await self._fetch_self_id()
            except Exception:
                pass
            self.connected.set()
            self.log.info("已连接官方 QQ 机器人（WebSocket）")
            try:
                async for raw in conn:
                    try:
                        payload = json.loads(raw)
                    except Exception:
                        continue
                    if isinstance(payload, dict):
                        await self._handle_payload(payload)
            except _Reconnect:
                pass
            code = getattr(conn, "close_code", None)
            if code is not None:
                self.log.warning("官方通道连接已关闭 code=%s reason=%s", code, getattr(conn, "close_reason", ""))
        finally:
            self.connected.clear()
            self._ws = None
            self._cancel_heartbeat()
            try:
                await conn.close()
            except Exception:
                pass

    def _start_heartbeat(self, interval_ms) -> None:
        self._cancel_heartbeat()
        try:
            interval = max(1.0, float(interval_ms) / 1000.0)
        except (TypeError, ValueError):
            interval = 30.0

        async def _loop():
            self._acked = True
            while True:
                await asyncio.sleep(interval)
                ws = self._ws
                if ws is None:
                    return
                if not getattr(self, "_acked", True):
                    # 上一次心跳没有收到 ACK：连接已经是半死状态（能连着但收不到事件），强制重连
                    self.log.warning("官方通道心跳无应答，重新连接")
                    try:
                        await ws.close()
                    except Exception:
                        pass
                    return
                self._acked = False
                try:
                    await ws.send(
                        json.dumps({"op": 1, "d": self._last_s}, ensure_ascii=False)
                    )
                except Exception:
                    return

        self._heartbeat_task = asyncio.create_task(_loop())

    def _cancel_heartbeat(self) -> None:
        task = self._heartbeat_task
        self._heartbeat_task = None
        if task is not None and not task.done():
            try:
                task.cancel()
            except Exception:
                pass

    async def _handle_payload(self, payload: dict) -> None:
        op = payload.get("op")
        seq = payload.get("s")
        if isinstance(seq, int):
            self._last_s = seq
        data = payload.get("d") if isinstance(payload.get("d"), dict) else {}
        if op == 10:  # hello
            self._start_heartbeat(data.get("heartbeat_interval"))
            return
        if op == 11:  # heartbeat ack
            self._acked = True
            return
        if op in (7, 9):  # 重连 / invalid session
            self.log.warning("官方通道要求重连 op=%s", op)
            raise _Reconnect()
        if op != 0:  # 其它 op 忽略
            return
        event_type = str(payload.get("t") or "")
        self.log.info("官方事件 t=%s s=%s keys=%s", event_type, seq, sorted(data.keys()) if isinstance(data, dict) else None)
        if event_type == "READY":
            user = data.get("user") if isinstance(data.get("user"), dict) else {}
            raw = user.get("id")
            if raw is not None:
                self._bot_raw_ids.add(str(raw))
                try:
                    self.self_id = int(raw)
                except (TypeError, ValueError):
                    self.self_id = self._ids.get("bot", str(raw))
            return
        self._handle_dispatch(event_type, data)

    def _handle_dispatch(self, event_type: str, data: dict) -> dict | None:
        event = self._translate_event(event_type, data)
        if event is not None and self.on_event is not None:
            try:
                asyncio.create_task(self._dispatch_event(event))
            except RuntimeError:
                pass
        return event

    async def _dispatch_event(self, event: dict) -> None:
        try:
            result = self.on_event(event)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            self.log.exception("官方事件处理异常")

    def _bot_mentioned(self, data: dict, content: str) -> tuple[bool, set]:
        """全量群消息里判断是否 @ 了本机器人；返回 (是否@, 正文里机器人的标记 id 集合)。"""
        bot_ids = {str(x) for x in self._bot_raw_ids if str(x)}
        if self.self_id:
            bot_ids.add(str(self.self_id))
        hit = False
        mark_ids = set(bot_ids)
        mentions = data.get("mentions")
        summary = []
        if isinstance(mentions, list):
            for item in mentions:
                if not isinstance(item, dict):
                    continue
                ids = {
                    str(item.get(k))
                    for k in ("id", "member_openid", "user_openid", "union_openid")
                    if item.get(k)
                }
                is_bot = bool(item.get("is_you")) or bool(item.get("bot")) or bool(ids & bot_ids)
                summary.append({"keys": sorted(item.keys()), "is_you": item.get("is_you"), "bot": item.get("bot")})
                if is_bot:
                    hit = True
                    mark_ids |= ids
        if not hit:
            for bid in bot_ids:
                if f"<@!{bid}>" in content or f"<@{bid}>" in content:
                    hit = True
                    break
        self.log.info("官方群消息 @机器人=%s mentions=%s", hit, summary)
        return hit, mark_ids

    @staticmethod
    def _attachment_images(data: dict) -> list[dict]:
        """attachments 里的图片 -> image segment（data.file/url 都是图片地址）。"""
        segments: list[dict] = []
        attachments = data.get("attachments")
        if not isinstance(attachments, list):
            return segments
        for item in attachments:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if not url.startswith(("http://", "https://")):
                continue
            mime = str(item.get("content_type") or "").lower()
            if mime and not mime.startswith("image"):
                continue
            segments.append({"type": "image", "data": {"file": url, "url": url}})
        return segments

    # ---------- 事件翻译 ----------
    def _translate_event(self, event_type: str, data: dict) -> dict | None:
        if not isinstance(data, dict):
            return None
        author = data.get("author") if isinstance(data.get("author"), dict) else {}
        msg_id = str(data.get("id") or "")
        content = str(data.get("content") or "")
        try:
            timestamp = int(float(data.get("timestamp") or time.time()))
        except (TypeError, ValueError):
            timestamp = int(time.time())
        if event_type in ("GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"):
            is_at_event = event_type == "GROUP_AT_MESSAGE_CREATE"
            group_openid = str(data.get("group_openid") or "").strip()
            member_openid = str(
                author.get("member_openid") or author.get("id") or ""
            ).strip()
            if not group_openid or not member_openid:
                return None
            group_id = self._ids.get("group", group_openid)
            user_id = self._ids.get("user", member_openid)
            self._remember_context(
                "group", group_openid, msg_id, GROUP_CONTEXT_SECONDS
            )
            # GROUP_MESSAGE_CREATE 可能是"全量群消息"事件：只有真的 @ 了机器人才算 @，
            # 否则按普通群消息翻译（插件里 group_require_at 会拦住，不会乱回复）
            if not is_at_event:
                mentioned_bot, bot_mark_ids = self._bot_mentioned(data, content)
                if not mentioned_bot:
                    return {
                        "post_type": "message",
                        "message_type": "group",
                        "self_id": self.self_id,
                        "user_id": user_id,
                        "group_id": group_id,
                        "message_id": msg_id,
                        "message": [{"type": "text", "data": {"text": content}}]
                        + self._attachment_images(data),
                        "raw_message": content,
                        "time": timestamp,
                        "sender": {
                            "user_id": user_id,
                            "nickname": get_nickname(self._data_dir, member_openid)
                            or "群友",
                        },
                        "_official": {
                            "kind": "group",
                            "group_openid": group_openid,
                            "member_openid": member_openid,
                            "msg_id": msg_id,
                        },
                    }
                # 全量群消息里 @ 是写在正文里的（<@id> / <@!id>），去掉机器人的标记再交给插件
                def _strip(m):
                    return " " if m.group(1) in bot_mark_ids else m.group(0)
                content = re.sub(r"<@!?([^>\s]+)>", _strip, content)
                content = " ".join(content.split())
            return {
                "post_type": "message",
                "message_type": "group",
                "self_id": self.self_id,
                "user_id": user_id,
                "group_id": group_id,
                "message_id": msg_id,
                "message": [
                    # 官方群事件本身就是 @ 机器人触发的；补一个合成 at 段，
                    # 让插件里的 mentioned()/strip_at() 按 OneBot 习惯正常工作
                    {"type": "at", "data": {"qq": str(self.self_id)}},
                    {"type": "text", "data": {"text": content}},
                ]
                + self._attachment_images(data),
                "raw_message": content,
                "time": timestamp,
                "sender": {
                    "user_id": user_id,
                    "nickname": get_nickname(self._data_dir, member_openid)
                    or "群友",
                },
                "_official": {
                    "kind": "group",
                    "group_openid": group_openid,
                    "member_openid": member_openid,
                    "msg_id": msg_id,
                },
            }
        if event_type == "C2C_MESSAGE_CREATE":
            user_openid = str(
                author.get("user_openid") or data.get("user_openid") or ""
            ).strip()
            if not user_openid:
                return None
            user_id = self._ids.get("user", user_openid)
            segments = [{"type": "text", "data": {"text": content}}]
            segments += self._attachment_images(data)
            self._remember_context("c2c", user_openid, msg_id, C2C_CONTEXT_SECONDS)
            return {
                "post_type": "message",
                "message_type": "private",
                "self_id": self.self_id,
                "user_id": user_id,
                "message_id": msg_id,
                "message": segments,
                "raw_message": content,
                "time": timestamp,
                "sender": {
                    "user_id": user_id,
                    "nickname": get_nickname(self._data_dir, user_openid) or "朋友",
                },
                "_official": {
                    "kind": "c2c",
                    "user_openid": user_openid,
                    "msg_id": msg_id,
                },
            }
        return None

    # ---------- 被动回复上下文 ----------
    def _remember_context(
        self, kind: str, openid: str, msg_id: str, window_seconds: float
    ) -> None:
        key = f"{kind}:{openid}"
        with self._contexts_lock:
            self._contexts[key] = {
                "kind": kind,
                "openid": openid,
                "msg_id": msg_id,
                "expires": self._now() + float(window_seconds),
                "msg_seq": 0,
            }

    def _context(self, kind: str, openid: str) -> dict | None:
        key = f"{kind}:{openid}"
        with self._contexts_lock:
            ctx = self._contexts.get(key)
            if ctx is None:
                return None
            if self._now() > float(ctx.get("expires") or 0):
                self._contexts.pop(key, None)
                return None
            return ctx

    def _next_seq(self, ctx: dict) -> int:
        current = int(ctx.get("msg_seq") or 0)
        if current >= MAX_MSG_SEQ:
            raise OfficialError(f"同一条消息最多回复 {MAX_MSG_SEQ} 条（官方限制）")
        current += 1
        ctx["msg_seq"] = current
        return current

    def _messages_path(self, kind: str, openid: str) -> str:
        quoted = urllib.parse.quote(openid, safe="")
        if kind == "group":
            return f"/v2/groups/{quoted}/messages"
        return f"/v2/users/{quoted}/messages"

    def _files_path(self, kind: str, openid: str) -> str:
        quoted = urllib.parse.quote(openid, safe="")
        if kind == "group":
            return f"/v2/groups/{quoted}/files"
        return f"/v2/users/{quoted}/files"

    async def _send_text(self, kind: str, openid: str, ctx: dict, text: str) -> str:
        seq = self._next_seq(ctx)
        body = {
            "content": str(text or ""),
            "msg_type": 0,
            "msg_id": ctx["msg_id"],
            "msg_seq": seq,
        }
        data = await self._request("POST", self._messages_path(kind, openid), body)
        return str(data.get("id") or "")

    async def _upload_file(
        self, kind: str, openid: str, segment: dict, file_type: int
    ) -> str:
        body = _image_upload_body(segment, file_type)
        data = await self._request("POST", self._files_path(kind, openid), body)
        file_info = data.get("file_info")
        if not file_info and isinstance(data.get("data"), dict):
            file_info = data["data"].get("file_info")
        if not file_info:
            raise OfficialError("上传文件失败，没有拿到 file_info")
        return str(file_info)

    async def _send_media(self, kind: str, openid: str, ctx: dict, file_info: str) -> str:
        seq = self._next_seq(ctx)
        body = {
            "content": "",
            "msg_type": 7,
            "media": {"file_info": file_info},
            "msg_id": ctx["msg_id"],
            "msg_seq": seq,
        }
        data = await self._request("POST", self._messages_path(kind, openid), body)
        return str(data.get("id") or "")

    async def _send_passive(self, action: str, params: dict) -> dict:
        kind = "group" if action == "send_group_msg" else "c2c"
        raw_target = params.get("group_id") if kind == "group" else params.get("user_id")
        try:
            target = int(raw_target)
        except (TypeError, ValueError):
            raise OfficialError("发送目标不正确") from None
        # id 映射：群用 group，C2C 与群成员共用 user 命名空间
        map_kind = "group" if kind == "group" else "user"
        openid = self._ids.reverse(map_kind, target)
        if not openid:
            raise OfficialError(
                "官方机器人只能被动回复：需要对方在窗口内先发消息"
            )
        ctx = self._context(kind, openid)
        if ctx is None:
            raise OfficialError(
                "官方机器人只能被动回复：需要对方在窗口内先发消息"
            )
        message = params.get("message")
        if message is None:
            message = params.get("msg")
        segments = _normalize_segments(message)
        if not segments:
            raise OfficialError("回复内容为空")
        if kind == "group" and any(seg.get("type") == "file" for seg in segments):
            raise OfficialError("官方通道群聊不支持发文件")
        mentions = ""
        if kind == "group":
            for seg in segments:
                if seg.get("type") != "at":
                    continue
                qq = str((seg.get("data") or {}).get("qq") or "").strip()
                if not qq or qq == str(self.self_id):
                    continue
                try:
                    user_openid = self._ids.reverse("user", int(qq))
                except (TypeError, ValueError):
                    user_openid = None
                if user_openid:
                    mentions += f"<@{user_openid}> "
        texts = mentions + "".join(
            str((seg.get("data") or {}).get("text") or "")
            for seg in segments
            if seg.get("type") == "text"
        ).strip()
        images = [seg for seg in segments if seg.get("type") == "image"]
        files = [seg for seg in segments if seg.get("type") == "file"]
        if not texts and not images and not files:
            raise OfficialError("回复内容为空")
        last_id = ""
        if texts:
            last_id = await self._send_text(kind, openid, ctx, texts)
        for segment in images:
            file_info = await self._upload_file(kind, openid, segment, 1)
            last_id = await self._send_media(kind, openid, ctx, file_info)
        for segment in files:
            file_info = await self._upload_file(kind, openid, segment, 4)
            last_id = await self._send_media(kind, openid, ctx, file_info)
        return {"message_id": last_id}

    # ---------- 对外接口 ----------
    async def call(self, action: str, **params):
        if action in ("send_group_msg", "send_private_msg"):
            return await self._send_passive(action, params)
        raise OfficialError(f"官方通道不支持 {action}")

    async def wait_ready(self, timeout: float = 30.0):
        await asyncio.wait_for(self.connected.wait(), timeout=timeout)

    async def close(self):
        self._cancel_heartbeat()
        ws = self._ws
        self._ws = None
        self.connected.clear()
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass


def _safe_message(error) -> str:
    """异常文本不包含 token/secret（只保留通用描述）。"""
    text = str(error or "")
    if "token" in text.lower() or "secret" in text.lower():
        return "官方通道连接异常"
    return text
