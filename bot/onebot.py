"""OneBot v11 正向 WebSocket 客户端（NapCat / Lagrange 都兼容）。"""
from __future__ import annotations

import asyncio
import json
import logging

import websockets


class OneBotError(RuntimeError):
    pass


class OneBotClient:
    kind = "onebot"

    def __init__(
        self,
        url: str,
        token: str = "",
        logger: logging.Logger | None = None,
        on_event=None,
        request_timeout: float = 20.0,
    ):
        self.url = url
        self.token = token or ""
        self.log = logger or logging.getLogger("onebot")
        self.on_event = on_event
        self.request_timeout = request_timeout
        self.reconnect_seconds = 1.0
        self._ws = None
        self._pending: dict[str, asyncio.Future] = {}
        self._seq = 0
        self.connected = asyncio.Event()
        self.self_id = 0

    # ---------- 连接 ----------
    async def run(self):
        backoff = max(0.5, float(self.reconnect_seconds))
        while True:
            try:
                await self._connect_once()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.log.warning("连接 %s 失败: %s", self.url, e)
            finally:
                self.connected.clear()
                self._ws = None
                self._fail_pending()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _connect_once(self):
        headers = {}
        url = self.url
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
            # 部分实现（含 NapCat）也认 URL 参数，双保险
            if "access_token=" not in url:
                url += ("&" if "?" in url else "?") + "access_token=" + str(self.token)
        try:
            conn = await websockets.connect(
                url, additional_headers=headers, max_size=2 ** 24, ping_interval=30, ping_timeout=20
            )
        except TypeError:  # 老版本 websockets
            conn = await websockets.connect(
                url, extra_headers=headers, max_size=2 ** 24, ping_interval=30, ping_timeout=20
            )
        self._ws = conn
        self.connected.set()
        self.log.info("已连接 OneBot: %s", self.url)
        async for raw in conn:
            try:
                data = json.loads(raw)
            except Exception:
                continue
            if "echo" in data:  # 是我们发出的 API 调用的返回
                fut = self._pending.pop(str(data["echo"]), None)
                if fut and not fut.done():
                    fut.set_result(data)
                continue
            if self.on_event is not None:
                asyncio.create_task(self._safe_event(data))

    async def _safe_event(self, data):
        try:
            await self.on_event(data)
        except Exception:
            self.log.exception("事件处理异常: %s", data.get("post_type"))

    def _fail_pending(self):
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(OneBotError("连接已断开"))
        self._pending.clear()

    async def close(self):
        if self._ws is not None:
            await self._ws.close()

    # ---------- API ----------
    async def call(self, action: str, **params):
        ws = self._ws
        if ws is None:
            raise OneBotError("尚未连接 OneBot")
        self._seq += 1
        echo = str(self._seq)
        fut = asyncio.get_running_loop().create_future()
        self._pending[echo] = fut
        try:
            await ws.send(json.dumps({"action": action, "params": params, "echo": echo}, ensure_ascii=False))
            resp = await asyncio.wait_for(fut, timeout=self.request_timeout)
        finally:
            self._pending.pop(echo, None)
        if resp.get("status") == "failed" or resp.get("retcode", 0) != 0:
            raise OneBotError(f"{action} 失败: {resp}")
        return resp.get("data")

    async def wait_ready(self, timeout: float = 30.0):
        await asyncio.wait_for(self.connected.wait(), timeout=timeout)
