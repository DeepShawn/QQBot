"""官方 QQ 机器人通道（bot/official.py）离线测试：token/gateway/事件/被动回复/错误。"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent.parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import official  # noqa: E402
from utils import at_seg, mentioned, strip_at, text_of  # noqa: E402

SELF_ID = 123456
GROUP_D = {
    "id": "MSG-G1",
    "group_openid": "GROUP-OPENID-1",
    "content": "帮我看看这个",
    "timestamp": "1700000000",
    "author": {"member_openid": "MEMBER-OPENID-1", "id": "MEMBER-OPENID-1"},
}
C2C_D = {
    "id": "MSG-C1",
    "content": "私聊你好",
    "timestamp": "1700000001",
    "author": {"user_openid": "USER-OPENID-1"},
    "attachments": [
        {"content_type": "image/png", "url": "https://example.com/a.png"},
        {"content_type": "file/pdf", "url": "https://example.com/a.pdf"},
    ],
}


def _patch(test: unittest.TestCase, obj, name, value):
    old = getattr(obj, name)
    setattr(obj, name, value)
    test.addCleanup(setattr, obj, name, old)


class FakeLog:
    def __init__(self):
        self.records: list[str] = []

    def _record(self, msg, args):
        try:
            self.records.append(str(msg) % args if args else str(msg))
        except Exception:
            self.records.append(str(msg))

    def info(self, msg, *args, **kwargs):
        self._record(msg, args)

    def warning(self, msg, *args, **kwargs):
        self._record(msg, args)

    def error(self, msg, *args, **kwargs):
        self._record(msg, args)

    def exception(self, msg, *args, **kwargs):
        self._record(msg, args)


class OfficialBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.events: list[dict] = []

    def make_client(self, on_event=None, sandbox=False) -> official.OfficialClient:
        async def recorder(event):
            self.events.append(event)

        client = official.OfficialClient(
            "app-id-123",
            "app-secret-XYZ",
            sandbox=sandbox,
            logger=FakeLog(),
            on_event=on_event if on_event is not None else recorder,
            request_timeout=5,
            data_dir=self.root,
        )
        client.self_id = SELF_ID
        return client

    async def drain(self):
        for _ in range(3):
            await asyncio.sleep(0)


# ---------------------------------------------------------------- token / headers


class TokenTests(OfficialBase):
    async def test_token_fetch_cache_and_refresh(self):
        clock = [1000.0]
        client = self.make_client()
        client._now = lambda: clock[0]
        fake = _FakeHTTP(
            [
                {"access_token": "TOKEN-1", "expires_in": 7200},
                {"access_token": "TOKEN-2", "expires_in": 7200},
            ]
        )
        _patch(self, official, "_http_json", fake)
        self.assertEqual(await client._get_token(), "TOKEN-1")
        first = fake.calls[0]
        self.assertEqual(first["url"], official.TOKEN_URL)
        self.assertEqual(first["body"], {"appId": "app-id-123", "clientSecret": "app-secret-XYZ"})
        # 缓存：再取一次不重复请求
        self.assertEqual(await client._get_token(), "TOKEN-1")
        self.assertEqual(len(fake.calls), 1)
        # 超过提前刷新窗口 -> 重新获取
        clock[0] += 7200 - official.TOKEN_REFRESH_MARGIN + 1
        self.assertEqual(await client._get_token(), "TOKEN-2")
        self.assertEqual(len(fake.calls), 2)

    async def test_token_error_hides_secret(self):
        client = self.make_client()

        def boom(*args, **kwargs):
            raise official._HttpError(401)

        _patch(self, official, "_http_json", boom)
        with self.assertRaises(official.OfficialError) as ctx:
            await client._get_token()
        text = str(ctx.exception)
        self.assertNotIn("app-secret-XYZ", text)
        self.assertNotIn("TOKEN", text)
        self.assertIn("401", text)

    async def test_authorization_header_qqbot(self):
        client = self.make_client()
        client._token = "TOKEN-X"
        client._token_expires = client._now() + 7200
        fake = _FakeHTTP([{"ok": True}])
        _patch(self, official, "_http_json", fake)
        await client._request("GET", "/users/@me")
        headers = fake.calls[0]["headers"]
        self.assertEqual(headers["Authorization"], "QQBot TOKEN-X")
        self.assertNotIn("Bearer", headers["Authorization"])

    async def test_gateway_and_self_id(self):
        client = self.make_client(sandbox=True)
        calls: list = []

        async def fake_request(method, path, body=None, **kwargs):
            calls.append((method, path))
            if path == "/gateway/bot":
                return {"url": "wss://gateway.example/ws"}
            if path == "/users/@me":
                return {"id": "987654"}
            return {}

        client._request = fake_request
        self.assertEqual(await client._get_gateway(), "wss://gateway.example/ws")
        await client._fetch_self_id()
        self.assertEqual(client.self_id, 987654)
        self.assertEqual(client.api_base, official.SANDBOX_API_BASE)
        self.assertEqual(client.url, "官方 QQ 机器人（WebSocket）")

    async def test_gateway_missing_url(self):
        client = self.make_client()

        async def fake_request(method, path, body=None, **kwargs):
            return {}

        client._request = fake_request
        with self.assertRaises(official.OfficialError):
            await client._get_gateway()


# ---------------------------------------------------------------- 事件翻译


class EventTranslateTests(OfficialBase):
    async def test_group_event_translation_with_synthetic_at(self):
        client = self.make_client()
        await client._handle_payload(
            {"op": 0, "t": "GROUP_AT_MESSAGE_CREATE", "s": 7, "d": GROUP_D}
        )
        await self.drain()
        self.assertEqual(len(self.events), 1)
        event = self.events[0]
        self.assertEqual(event["post_type"], "message")
        self.assertEqual(event["message_type"], "group")
        self.assertIsInstance(event["group_id"], int)
        self.assertGreater(event["group_id"], 0)
        self.assertIsInstance(event["user_id"], int)
        self.assertEqual(event["message_id"], "MSG-G1")
        self.assertEqual(event["raw_message"], "帮我看看这个")
        self.assertEqual(event["time"], 1700000000)
        self.assertEqual(event["self_id"], SELF_ID)
        # 合成 at 段在最前面，插件 mentioned()/strip_at() 能正常工作
        self.assertEqual(event["message"][0], at_seg(SELF_ID))
        self.assertTrue(mentioned(event["message"], SELF_ID))
        self.assertEqual(
            text_of(strip_at(event["message"], SELF_ID)), "帮我看看这个"
        )
        self.assertEqual(event["_official"]["kind"], "group")
        self.assertEqual(event["_official"]["group_openid"], "GROUP-OPENID-1")
        self.assertEqual(event["_official"]["member_openid"], "MEMBER-OPENID-1")
        self.assertEqual(event["_official"]["msg_id"], "MSG-G1")
        self.assertEqual(client._last_s, 7)
        # 被动回复上下文：群窗口 5 分钟
        ctx = client._context("group", "GROUP-OPENID-1")
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx["msg_id"], "MSG-G1")

    async def test_c2c_event_translation_with_image(self):
        client = self.make_client()
        await client._handle_payload(
            {"op": 0, "t": "C2C_MESSAGE_CREATE", "s": 1, "d": C2C_D}
        )
        await self.drain()
        event = self.events[0]
        self.assertEqual(event["message_type"], "private")
        self.assertEqual(event["user_id"], client._ids.get("user", "USER-OPENID-1"))
        self.assertEqual(event["message"][0]["type"], "text")
        self.assertEqual(len(event["message"]), 3)  # 文本 + 图片 + 文件
        image = event["message"][1]
        self.assertEqual(image["type"], "image")
        self.assertEqual(image["data"]["file"], "https://example.com/a.png")
        self.assertEqual(event["_official"]["kind"], "c2c")
        self.assertEqual(event["_official"]["user_openid"], "USER-OPENID-1")
        ctx = client._context("c2c", "USER-OPENID-1")
        self.assertAlmostEqual(
            ctx["expires"] - client._now(), official.C2C_CONTEXT_SECONDS, delta=5
        )

    async def test_ids_stable_and_persisted(self):
        client = self.make_client()
        event1 = client._translate_event("GROUP_AT_MESSAGE_CREATE", GROUP_D)
        same = client._translate_event("GROUP_AT_MESSAGE_CREATE", GROUP_D)
        self.assertEqual(event1["group_id"], same["group_id"])
        self.assertEqual(event1["user_id"], same["user_id"])
        # 新客户端（同一 data 目录）从 official_ids.json 恢复
        client2 = self.make_client()
        event2 = client2._translate_event("GROUP_AT_MESSAGE_CREATE", GROUP_D)
        self.assertEqual(event1["group_id"], event2["group_id"])
        self.assertEqual(event1["user_id"], event2["user_id"])
        self.assertEqual(client2._ids.reverse("group", event2["group_id"]), "GROUP-OPENID-1")

    async def test_id_collision_linear_probe(self):
        _patch(self, official.zlib, "crc32", lambda data: 777)
        client = self.make_client()
        event_a = client._translate_event(
            "GROUP_AT_MESSAGE_CREATE", dict(GROUP_D, group_openid="A")
        )
        event_b = client._translate_event(
            "GROUP_AT_MESSAGE_CREATE", dict(GROUP_D, group_openid="B")
        )
        self.assertNotEqual(event_a["group_id"], event_b["group_id"])
        self.assertEqual(event_a["group_id"], 777)

    async def test_other_events_ignored(self):
        client = self.make_client()
        self.assertIsNone(client._translate_event("SOMETHING_ELSE", {}))
        await client._handle_payload({"op": 0, "t": "SOMETHING_ELSE", "d": {}})
        await self.drain()
        self.assertEqual(self.events, [])

    async def test_ready_sets_self_id(self):
        client = self.make_client()
        client.self_id = 0
        await client._handle_payload({"op": 0, "t": "READY", "d": {"user": {"id": "555"}}})
        self.assertEqual(client.self_id, 555)

    async def test_op_heartbeat_and_reconnect(self):
        class FakeWS:
            def __init__(self):
                self.sent: list = []

            async def send(self, raw):
                self.sent.append(json.loads(raw))

        client = self.make_client()
        ws = FakeWS()
        client._ws = ws
        client._last_s = 42
        await client._handle_payload({"op": 10, "d": {"heartbeat_interval": 30}})
        self.assertIsNotNone(client._heartbeat_task)
        client._cancel_heartbeat()
        await client._handle_payload({"op": 11})
        with self.assertRaises(official._Reconnect):
            await client._handle_payload({"op": 7})
        with self.assertRaises(official._Reconnect):
            await client._handle_payload({"op": 9})


# ---------------------------------------------------------------- 被动回复


class SendTests(OfficialBase):
    def make_sender(self, kind="group", clock=None):
        client = self.make_client()
        if clock is not None:
            client._now = lambda: clock[0]
        if kind == "group":
            event = client._translate_event("GROUP_AT_MESSAGE_CREATE", GROUP_D)
            target_id = event["group_id"]
            action = "send_group_msg"
            openid = "GROUP-OPENID-1"
        else:
            event = client._translate_event("C2C_MESSAGE_CREATE", C2C_D)
            target_id = event["user_id"]
            action = "send_private_msg"
            openid = "USER-OPENID-1"
        self.requests: list = []

        async def fake_request(method, path, body=None, **kwargs):
            self.requests.append({"method": method, "path": path, "body": body})
            if path.endswith("/files"):
                return {"file_info": "FILE-INFO-1"}
            return {"id": "SENT-ID-1"}

        client._request = fake_request
        return client, action, target_id, openid, event

    async def test_send_text_and_seq_increment(self):
        client, action, target_id, openid, event = self.make_sender()
        result = await client.call(
            action, **{("group_id" if action == "send_group_msg" else "user_id"): target_id},
            message=[{"type": "text", "data": {"text": "你好呀"}}],
        )
        self.assertEqual(result["message_id"], "SENT-ID-1")
        body = self.requests[0]["body"]
        self.assertEqual(self.requests[0]["path"], f"/v2/groups/{openid}/messages")
        self.assertEqual(body["content"], "你好呀")
        self.assertEqual(body["msg_type"], 0)
        self.assertEqual(body["msg_id"], event["message_id"])
        self.assertEqual(body["msg_seq"], 1)
        # 第二次递增
        await client.call(
            action, **{("group_id" if action == "send_group_msg" else "user_id"): target_id},
            message="再来一条",
        )
        self.assertEqual(self.requests[1]["body"]["msg_seq"], 2)

    async def test_send_image_upload_then_media(self):
        client, action, target_id, openid, event = self.make_sender()
        await client.call(
            action,
            **{("group_id" if action == "send_group_msg" else "user_id"): target_id},
            message=[
                {"type": "text", "data": {"text": "看图"}},
                {"type": "image", "data": {"file": "base64://aGVsbG8="}},
            ],
        )
        self.assertEqual(len(self.requests), 3)
        upload = self.requests[1]
        self.assertEqual(upload["path"], f"/v2/groups/{openid}/files")
        self.assertEqual(upload["body"]["file_type"], 1)
        self.assertFalse(upload["body"]["srv_send_msg"])
        self.assertEqual(upload["body"]["file_data"], "aGVsbG8=")
        media = self.requests[2]
        self.assertEqual(media["body"]["msg_type"], 7)
        self.assertEqual(media["body"]["media"]["file_info"], "FILE-INFO-1")
        self.assertEqual(media["body"]["msg_seq"], 2)

    async def test_send_image_by_url(self):
        client, action, target_id, openid, event = self.make_sender()
        await client.call(
            action,
            **{("group_id" if action == "send_group_msg" else "user_id"): target_id},
            message=[{"type": "image", "data": {"url": "https://example.com/i.png"}}],
        )
        self.assertEqual(
            self.requests[0]["body"]["url"], "https://example.com/i.png"
        )

    async def test_no_context_passive_reply_error(self):
        client = self.make_client()
        with self.assertRaises(official.OfficialError) as ctx:
            await client.call("send_group_msg", group_id=999999, message="hi")
        self.assertIn("只能被动回复", str(ctx.exception))

    async def test_group_window_expired(self):
        clock = [1000.0]
        client, action, target_id, openid, event = self.make_sender(clock=clock)
        clock[0] += official.GROUP_CONTEXT_SECONDS + 1
        with self.assertRaises(official.OfficialError):
            await client.call("send_group_msg", group_id=target_id, message="hi")
        self.assertEqual(self.requests, [])

    async def test_c2c_window_longer(self):
        clock = [1000.0]
        client, action, target_id, openid, event = self.make_sender("c2c", clock=clock)
        clock[0] += official.GROUP_CONTEXT_SECONDS + 1  # 群已过期、C2C 仍有效
        await client.call("send_private_msg", user_id=target_id, message="hi")
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0]["path"], f"/v2/users/{openid}/messages")

    async def test_group_file_rejected(self):
        client, action, target_id, openid, event = self.make_sender()
        with self.assertRaises(official.OfficialError) as ctx:
            await client.call(
                "send_group_msg",
                group_id=target_id,
                message=[{"type": "file", "data": {"file": "base64://aGk="}}],
            )
        self.assertIn("不支持发文件", str(ctx.exception))
        self.assertEqual(self.requests, [])

    async def test_c2c_file_upload(self):
        client, action, target_id, openid, event = self.make_sender("c2c")
        await client.call(
            "send_private_msg",
            user_id=target_id,
            message=[{"type": "file", "data": {"file": "base64://aGk="}}],
        )
        upload = self.requests[0]
        self.assertEqual(upload["path"], f"/v2/users/{openid}/files")
        self.assertEqual(upload["body"]["file_type"], 4)
        self.assertEqual(self.requests[1]["body"]["msg_type"], 7)

    async def test_msg_seq_limit(self):
        client, action, target_id, openid, event = self.make_sender()
        for _ in range(official.MAX_MSG_SEQ):
            await client.call("send_group_msg", group_id=target_id, message="hi")
        with self.assertRaises(official.OfficialError) as ctx:
            await client.call("send_group_msg", group_id=target_id, message="hi")
        self.assertIn("5", str(ctx.exception))
        self.assertEqual(len(self.requests), official.MAX_MSG_SEQ)

    async def test_member_info_uses_event_role(self):
        client = self.make_client()
        info = await client.call("get_group_member_info", group_id=1, user_id=2)
        self.assertEqual(info["role"], "member")
        client._member_roles[(1, 2)] = "owner"
        info = await client.call("get_group_member_info", group_id=1, user_id=2)
        self.assertEqual(info["role"], "owner")

    async def test_unsupported_actions(self):
        client = self.make_client()
        for action in (
            "delete_msg",
            "set_group_ban",
            "set_group_kick",
            "get_msg",
            "send_like",
            "upload_file",
        ):
            with self.subTest(action=action):
                with self.assertRaises(official.OfficialError) as ctx:
                    await client.call(action, group_id=1)
                self.assertIn("不支持", str(ctx.exception))

    async def test_request_http_error_hides_token(self):
        client = self.make_client()
        client._token = "SECRET-TOKEN"
        client._token_expires = client._now() + 7200

        def boom(*args, **kwargs):
            raise official._HttpError(500)

        _patch(self, official, "_http_json", boom)
        with self.assertRaises(official.OfficialError) as ctx:
            await client._request("GET", "/x")
        text = str(ctx.exception)
        self.assertIn("500", text)
        self.assertNotIn("SECRET-TOKEN", text)
        self.assertNotIn("app-secret-XYZ", text)

    async def test_request_network_error(self):
        client = self.make_client()
        client._token = "SECRET-TOKEN"
        client._token_expires = client._now() + 7200

        def boom(*args, **kwargs):
            raise official._NetworkError("net")

        _patch(self, official, "_http_json", boom)
        with self.assertRaises(official.OfficialError) as ctx:
            await client._request("GET", "/x")
        self.assertNotIn("SECRET-TOKEN", str(ctx.exception))

    async def test_429_retry_once(self):
        client = self.make_client()
        client._token = "TOKEN"
        client._token_expires = client._now() + 7200
        responses = [official._HttpError(429), {"ok": True}]
        calls: list = []

        def fake(*args, **kwargs):
            calls.append(1)
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        _patch(self, official, "_http_json", fake)
        _patch(self, official, "RETRY_429_SLEEP", 0.0)
        data = await client._request("GET", "/x")
        self.assertEqual(data, {"ok": True})
        self.assertEqual(len(calls), 2)


# ---------------------------------------------------------------- main 选择


class _FakeHTTP:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list = []

    def __call__(self, method, url, body=None, headers=None, timeout=20.0):
        self.calls.append(
            {"method": method, "url": url, "body": body, "headers": headers}
        )
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeOfficialClient:
    init_args: dict | None = None

    def __init__(self, **kwargs):
        FakeOfficialClient.init_args = kwargs
        self.reconnect_seconds = 5.0
        self.url = "官方 QQ 机器人（WebSocket）"

    async def close(self):
        pass


class FakeOneBotClient:
    init_args: dict | None = None

    def __init__(self, **kwargs):
        FakeOneBotClient.init_args = kwargs
        self.reconnect_seconds = 1.0
        self.url = kwargs.get("url", "")


class FakeConfig:
    def __init__(self, data: dict):
        self.data = data

    def get(self, *keys, default=None):
        node = self.data
        for key in keys:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node


class MainSelectionTests(unittest.TestCase):
    def setUp(self):
        import main as main_module

        self.main = main_module
        self._old_official = main_module.OfficialClient
        self._old_onebot = main_module.OneBotClient
        main_module.OfficialClient = FakeOfficialClient
        main_module.OneBotClient = FakeOneBotClient
        self.addCleanup(setattr, main_module, "OfficialClient", self._old_official)
        self.addCleanup(setattr, main_module, "OneBotClient", self._old_onebot)
        FakeOfficialClient.init_args = None
        FakeOneBotClient.init_args = None

    def build(self, data):
        return self.main.build_client(
            FakeConfig(data), FakeLog(), lambda event: None, "/tmp/data"
        )

    def test_official_selected_when_enabled(self):
        api, channel = self.build(
            {
                "official": {
                    "enabled": True,
                    "app_id": "id-1",
                    "app_secret": "secret-1",
                    "sandbox": True,
                    "request_timeout": 15,
                    "reconnect_seconds": 3,
                },
                "onebot": {"ws_url": "ws://x"},
            }
        )
        self.assertEqual(channel, "official")
        self.assertIsInstance(api, FakeOfficialClient)
        args = FakeOfficialClient.init_args
        self.assertEqual(args["app_id"], "id-1")
        self.assertEqual(args["app_secret"], "secret-1")
        self.assertTrue(args["sandbox"])
        self.assertEqual(args["data_dir"], "/tmp/data")
        self.assertEqual(api.reconnect_seconds, 3.0)

    def test_fallback_to_onebot_when_disabled(self):
        api, channel = self.build(
            {
                "official": {"enabled": False, "app_id": "id", "app_secret": "s"},
                "onebot": {"ws_url": "ws://127.0.0.1:3001", "access_token": "t"},
            }
        )
        self.assertEqual(channel, "onebot")
        self.assertIsInstance(api, FakeOneBotClient)
        self.assertEqual(FakeOneBotClient.init_args["url"], "ws://127.0.0.1:3001")

    def test_fallback_when_secret_missing(self):
        api, channel = self.build(
            {
                "official": {"enabled": True, "app_id": "id", "app_secret": ""},
                "onebot": {"ws_url": "ws://x"},
            }
        )
        self.assertEqual(channel, "onebot")

    def test_fallback_when_official_import_failed(self):
        self.main.OfficialClient = None
        api, channel = self.build(
            {
                "official": {"enabled": True, "app_id": "id", "app_secret": "s"},
                "onebot": {"ws_url": "ws://x"},
            }
        )
        self.assertEqual(channel, "onebot")

    def test_config_defaults(self):
        import config

        cfg = config.DEFAULTS["official"]
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["app_id"], "")
        self.assertEqual(cfg["app_secret"], "")
        self.assertFalse(cfg["sandbox"])


if __name__ == "__main__":
    unittest.main()
