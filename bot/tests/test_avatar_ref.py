"""T56/T57 机器人形象：参考图接口 + 形象提示词/参考图生图（离线）。"""
from __future__ import annotations

import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent.parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import admin.server as admin_server  # noqa: E402
import imagegen  # noqa: E402
from plugins import ai_chat  # noqa: E402

TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
SELF_ID = 999


def _patch(test, obj, name, value):
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


class FakeCtx:
    def __init__(self, root, cfg):
        self.data_dir = Path(root)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log = FakeLog()
        self.self_id = SELF_ID
        self.bot_name = "测试机器人"
        self._cfg = cfg
        self.sent: list[dict] = []
        self.replies: list[str] = []

    @property
    def cfg(self):
        return self._cfg

    def plugin_cfg(self, name):
        return (self._cfg.get("plugins") or {}).get(name) or {}

    def is_admin(self, user_id):
        return False

    def format_text(self, template, event, **extra):
        return str(template or "")

    async def reply(self, event, message, at=False):
        self.replies.append(str(message))
        return {"message_id": 1}

    async def send_private(self, user_id, message):
        self.sent.append({"type": "private", "user_id": user_id, "message": message})

    async def send_group(self, group_id, message):
        self.sent.append({"type": "group", "group_id": group_id, "message": message})


def _event():
    return {
        "post_type": "message",
        "message_type": "private",
        "user_id": 20002,
        "message": [{"type": "text", "data": {"text": "画个机器人形象"}}],
    }


class ServerAvatarBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.data_dir = self.root / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.root / "config.json"
        self.config_path.write_text(
            json.dumps({"plugins": {"ai_chat": {"avatar": {"prompt": "P", "reference_image": ""}}}}),
            encoding="utf-8",
        )
        _patch(self, admin_server, "DATA_DIR", self.data_dir)
        _patch(self, admin_server, "CONFIG_PATH", self.config_path)

    def read_config(self):
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def avatar_ref(self):
        return self.read_config()["plugins"]["ai_chat"]["avatar"]["reference_image"]


class AvatarApiTests(ServerAvatarBase):
    def test_post_get_delete_roundtrip(self):
        payload = {"data_base64": base64.b64encode(TINY_PNG).decode("ascii"), "filename": "a.png"}
        result = admin_server.api_avatar_reference_post(payload)
        self.assertTrue(result["ok"])
        self.assertEqual(result["path"], "data/avatar/reference.png")
        self.assertEqual(result["width"], 1)
        self.assertEqual(result["height"], 1)
        self.assertGreater(result["bytes"], 0)
        saved = admin_server.avatar_reference_path()
        self.assertTrue(saved.is_file())
        saved_bytes = saved.read_bytes()
        self.assertTrue(saved_bytes.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(self.avatar_ref(), "data/avatar/reference.png")
        self.assertEqual(admin_server.api_avatar_reference_get(), saved_bytes)
        deleted = admin_server.api_avatar_reference_delete()
        self.assertTrue(deleted["ok"])
        self.assertFalse(saved.exists())
        self.assertIsNone(admin_server.api_avatar_reference_get())
        self.assertEqual(self.avatar_ref(), "")

    def test_post_data_url(self):
        data_url = "data:image/png;base64," + base64.b64encode(TINY_PNG).decode("ascii")
        result = admin_server.api_avatar_reference_post({"data_url": data_url})
        self.assertTrue(result["ok"])
        saved = admin_server.avatar_reference_path().read_bytes()
        self.assertTrue(saved.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_reject_non_image(self):
        with self.assertRaises(ValueError) as ctx:
            admin_server.api_avatar_reference_post(
                {"data_base64": base64.b64encode(b"hello world").decode("ascii")}
            )
        self.assertIn("png/jpeg/webp", str(ctx.exception))

    def test_reject_too_large(self):
        _patch(self, admin_server, "AVATAR_MAX_BYTES", 8)
        with self.assertRaises(ValueError) as ctx:
            admin_server.api_avatar_reference_post(
                {"data_base64": base64.b64encode(TINY_PNG).decode("ascii")}
            )
        self.assertIn("太大", str(ctx.exception))

    def test_reject_empty(self):
        with self.assertRaises(ValueError):
            admin_server.api_avatar_reference_post({})

    def test_get_missing_returns_none(self):
        self.assertIsNone(admin_server.api_avatar_reference_get())

    def test_delete_when_missing(self):
        self.assertTrue(admin_server.api_avatar_reference_delete()["ok"])
        self.assertEqual(self.avatar_ref(), "")

    def test_sanitize_avatar(self):
        cfg = {
            "plugins": {
                "ai_chat": {
                    "avatar": {
                        "prompt": "长" * 3000,
                        "reference_image": "../../etc/passwd",
                    }
                }
            }
        }
        out = admin_server.sanitize_config(cfg)["plugins"]["ai_chat"]["avatar"]
        self.assertEqual(len(out["prompt"]), 2000)
        self.assertEqual(out["reference_image"], "")
        cfg2 = {
            "plugins": {
                "ai_chat": {
                    "avatar": {
                        "prompt": "蓝白配色",
                        "reference_image": "data/avatar/reference.png",
                    }
                }
            }
        }
        out2 = admin_server.sanitize_config(cfg2)["plugins"]["ai_chat"]["avatar"]
        self.assertEqual(out2["prompt"], "蓝白配色")
        self.assertEqual(out2["reference_image"], "data/avatar/reference.png")


class _DummyHandler(admin_server.Handler):
    def __init__(self, path, body=None):
        self.path = path
        self.headers = {"Content-Length": "0"}
        self._body_value = body or {}
        self.calls: list = []

    def _body(self):
        return self._body_value

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        self.calls.append(("send", code, body, ctype))

    def _json(self, data, code=200):
        self.calls.append(("json", code, data))

    def _error(self, msg, code=400):
        self.calls.append(("error", code, msg))


class AvatarEndpointTests(ServerAvatarBase):
    def test_get_endpoint_missing_then_exists(self):
        handler = _DummyHandler("/api/avatar/reference")
        handler.do_GET()
        self.assertEqual(handler.calls[0][0], "error")
        self.assertEqual(handler.calls[0][1], 404)
        admin_server.api_avatar_reference_post(
            {"data_base64": base64.b64encode(TINY_PNG).decode("ascii")}
        )
        handler = _DummyHandler("/api/avatar/reference")
        handler.do_GET()
        self.assertEqual(handler.calls[0][0], "send")
        self.assertEqual(handler.calls[0][1], 200)
        self.assertTrue(handler.calls[0][2].startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(handler.calls[0][3], "image/png")

    def test_post_endpoint(self):
        handler = _DummyHandler(
            "/api/avatar/reference",
            {"data_base64": base64.b64encode(TINY_PNG).decode("ascii")},
        )
        handler.do_POST()
        self.assertEqual(handler.calls[0][0], "json")
        self.assertTrue(handler.calls[0][2]["ok"])

    def test_post_endpoint_bad_input(self):
        handler = _DummyHandler(
            "/api/avatar/reference",
            {"data_base64": base64.b64encode(b"nope").decode("ascii")},
        )
        handler.do_POST()
        self.assertEqual(handler.calls[0][0], "error")
        self.assertEqual(handler.calls[0][1], 400)

    def test_delete_endpoint(self):
        admin_server.api_avatar_reference_post(
            {"data_base64": base64.b64encode(TINY_PNG).decode("ascii")}
        )
        handler = _DummyHandler("/api/avatar/reference")
        handler.do_DELETE()
        self.assertEqual(handler.calls[0][0], "json")
        self.assertTrue(handler.calls[0][2]["ok"])
        self.assertFalse(admin_server.avatar_reference_path().exists())


class AvatarIntentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.avatar_cfg = {
            "prompt": "蓝白配色、鲸鱼娘、圆脸、戴耳机",
            "reference_image": "data/avatar/reference.png",
        }
        self.ai_cfg = {
            "enabled": True,
            "provider": "openai",
            "api_key": "key",
            "auth": "",
            "model": "m",
            "avatar": dict(self.avatar_cfg),
        }
        self.image_cfg = {
            "enabled": True,
            "provider": "openai",
            "base_url": "https://api.openai.com/v1",
            "api_key": "img-key",
            "auth": "",
            "model": "gpt-image-1",
            "size": "1024x1024",
            "timeout_seconds": 10,
        }
        self.ctx = FakeCtx(
            self.root,
            {
                "plugins": {
                    "ai_chat": self.ai_cfg,
                    "image_gen": self.image_cfg,
                    "python_run": {"enabled": False},
                    "dev_tools": {"enabled": False},
                    "skills": {"enabled": False},
                }
            },
        )
        _patch(self, ai_chat, "AVATAR_BASE_DIR", self.root)
        self.plugin = ai_chat.Plugin(self.ctx)
        self.generate_calls: list = []
        self._orig_generate = imagegen.generate_for_ai
        self.addCleanup(setattr, imagegen, "generate_for_ai", self._orig_generate)

        def fake_generate(*args, **kwargs):
            self.generate_calls.append((args, kwargs))
            return [TINY_PNG]

        imagegen.generate_for_ai = fake_generate

    def make_reference(self):
        ref = self.root / "data" / "avatar" / "reference.png"
        ref.parent.mkdir(parents=True, exist_ok=True)
        ref.write_bytes(TINY_PNG)
        return ref

    async def test_avatar_prompt_only_for_avatar_request(self):
        await self.plugin._perform_image_tool(
            _event(),
            self.ai_cfg,
            {"prompt": "可爱的鲸鱼娘站在海边", "size": "1024x1024"},
            user_text="画一个你的机器人形象",
        )
        self.assertEqual(len(self.generate_calls), 1)
        prompt = self.generate_calls[0][0][2]
        self.assertTrue(prompt.startswith("蓝白配色、鲸鱼娘、圆脸、戴耳机"))
        self.assertIn("可爱的鲸鱼娘", prompt)
        self.assertEqual(len(self.ctx.sent), 1)

    async def test_avatar_prompt_ignored_for_normal_drawing(self):
        await self.plugin._perform_image_tool(
            _event(),
            self.ai_cfg,
            {"prompt": "山间晨雾", "size": "1024x1024"},
            user_text="画一张风景图",
        )
        self.assertEqual(self.generate_calls[0][0][2], "山间晨雾")

    async def test_avatar_reference_used_for_avatar_request(self):
        self.make_reference()

        def fake_ref(*args, **kwargs):
            return [TINY_PNG]

        _patch(self, ai_chat.Plugin, "_generate_avatar_reference_sync", fake_ref)
        await self.plugin._perform_image_tool(
            _event(),
            self.ai_cfg,
            {"prompt": "鲸鱼娘立绘", "size": "1024x1024"},
            user_text="画你的形象",
        )
        self.assertEqual(self.generate_calls, [])  # 直接用参考图，没走纯文生图
        self.assertIn("参考图", "\n".join(self.ctx.log.records))
        self.assertEqual(len(self.ctx.sent), 1)

    async def test_reference_failure_falls_back_to_text_to_image(self):
        self.make_reference()

        def fake_ref(*args, **kwargs):
            raise RuntimeError("参考图接口 500")

        _patch(self, ai_chat.Plugin, "_generate_avatar_reference_sync", fake_ref)
        await self.plugin._perform_image_tool(
            _event(),
            self.ai_cfg,
            {"prompt": "鲸鱼娘立绘", "size": "1024x1024"},
            user_text="画你的形象",
        )
        self.assertEqual(len(self.generate_calls), 1)
        self.assertIn("回退纯文生图", "\n".join(self.ctx.log.records))

    async def test_reference_edit_multipart(self):
        ref = self.make_reference()
        captured: list = []

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(
                    {"data": [{"b64_json": base64.b64encode(TINY_PNG).decode("ascii")}]}
                ).encode("utf-8")

        def fake_urlopen(request, timeout=None):
            captured.append(request)
            return FakeResponse()

        _patch(self, ai_chat.urllib.request, "urlopen", fake_urlopen)
        images = self.plugin._generate_avatar_reference_sync(
            self.ai_cfg,
            self.image_cfg,
            "蓝白配色，鲸鱼娘",
            ref,
            "1024x1024",
            10,
        )
        self.assertEqual(images, [TINY_PNG])
        request = captured[0]
        self.assertTrue(request.full_url.endswith("/images/edits"))
        self.assertEqual(request.get_header("Authorization"), "Bearer img-key")
        self.assertIn(b'name="prompt"', request.data)
        self.assertIn(b'name="image"', request.data)
        self.assertIn("蓝白配色".encode("utf-8"), request.data)

    async def test_avatar_reference_path(self):
        self.make_reference()
        path = self.plugin._avatar_reference_path(self.ai_cfg)
        self.assertIsNotNone(path)
        self.assertTrue(str(path).endswith("data/avatar/reference.png"))
        self.ai_cfg["avatar"]["reference_image"] = "data/avatar/missing.png"
        self.assertIsNone(self.plugin._avatar_reference_path(self.ai_cfg))

    def test_avatar_request_detection(self):
        for text in (
            "画个机器人形象",
            "你的头像",
            "画个头像",
            "画你自己",
            "机器人立绘",
            "自画像",
            "avatar",
        ):
            with self.subTest(text=text):
                self.assertTrue(ai_chat.Plugin._looks_like_avatar_request(text))
        for text in ("画一张风景图", "帮我写代码", "随便聊聊"):
            with self.subTest(text=text):
                self.assertFalse(ai_chat.Plugin._looks_like_avatar_request(text))


class ConfigDefaultsTests(unittest.TestCase):
    def test_config_defaults_avatar(self):
        import config

        avatar = config.DEFAULTS["plugins"]["ai_chat"]["avatar"]
        self.assertEqual(avatar["prompt"], "")
        self.assertEqual(avatar["reference_image"], "")


if __name__ == "__main__":
    unittest.main()
