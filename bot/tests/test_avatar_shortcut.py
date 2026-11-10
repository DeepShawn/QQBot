"""T59 形象请求在 image_gen 快捷触发词路径也生效（共用 imagegen 实现）。"""
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

import imagegen  # noqa: E402
from plugins import image_gen as image_gen_plugin  # noqa: E402

TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


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
        self.self_id = 999
        self._cfg = cfg
        self.replies: list[str] = []
        self.sent: list[dict] = []

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


def _event(text="画张你自己的照片"):
    return {
        "post_type": "message",
        "message_type": "private",
        "user_id": 20002,
        "message": [{"type": "text", "data": {"text": text}}],
    }


AVATAR_PROMPT = "蓝白配色、鲸鱼娘、圆脸、戴耳机"


class SharedAvatarTests(unittest.TestCase):
    def test_looks_like_avatar_request(self):
        for text in (
            "你自己的照片",
            "画张你自己的照片",
            "画你的照片",
            "机器人形象",
            "你的头像",
            "画个头像",
            "自画像",
        ):
            with self.subTest(text=text):
                self.assertTrue(imagegen.looks_like_avatar_request(text))
        for text in ("画一张风景图", "山间日落", "帮我写代码"):
            with self.subTest(text=text):
                self.assertFalse(imagegen.looks_like_avatar_request(text))

    def test_avatar_prompt_and_reference_path(self):
        cfg = {"avatar": {"prompt": AVATAR_PROMPT, "reference_image": ""}}
        self.assertEqual(imagegen.avatar_prompt(cfg), AVATAR_PROMPT)
        self.assertIsNone(imagegen.avatar_reference_path(cfg))
        cfg["avatar"]["reference_image"] = "data/avatar/reference.png"
        self.assertIsNone(imagegen.avatar_reference_path(cfg, base_dir="/nonexistent"))


class ShortcutAvatarFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.ref = self.root / "reference.png"
        self.ref.write_bytes(TINY_PNG)
        self.ai_cfg = {
            "provider": "openai",
            "base_url": "",
            "api_key": "ai-key",
            "auth": "",
            "model": "m",
            "avatar": {
                "prompt": AVATAR_PROMPT,
                "reference_image": str(self.ref),
            },
            "vision": {"enabled": False},
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
            "triggers": ["画张", "画一张"],
            "caption": "🎨 画好了",
            "caption_ai": False,
            "at_sender": True,
            "cooldown_seconds": 0,
        }
        self.ctx = FakeCtx(
            self.root,
            {"plugins": {"ai_chat": self.ai_cfg, "image_gen": self.image_cfg}},
        )
        self.normal_calls: list = []
        self.reference_calls: list = []
        self.reference_error: Exception | None = None
        self.account_calls: list = []
        self._orig_generate = imagegen.generate
        self._orig_reference = imagegen.generate_with_reference
        self._orig_account = image_gen_plugin.Plugin._generate_account_sync
        self.addCleanup(setattr, imagegen, "generate", self._orig_generate)
        self.addCleanup(setattr, imagegen, "generate_with_reference", self._orig_reference)
        self.addCleanup(
            setattr, image_gen_plugin.Plugin, "_generate_account_sync", self._orig_account
        )

        def fake_generate(*args, **kwargs):
            self.normal_calls.append((args, kwargs))
            return [TINY_PNG]

        def fake_reference(*args, **kwargs):
            self.reference_calls.append((args, kwargs))
            if self.reference_error is not None:
                raise self.reference_error
            return [TINY_PNG]

        test_self = self

        def fake_account(plugin_self, auth, model, cfg, prompt):
            test_self.account_calls.append(
                {"auth": auth, "model": model, "prompt": prompt}
            )
            return [TINY_PNG]

        imagegen.generate = fake_generate
        imagegen.generate_with_reference = fake_reference
        image_gen_plugin.Plugin._generate_account_sync = fake_account

    async def run_event(self, text="画张你自己的照片"):
        return await image_gen_plugin.Plugin(self.ctx).on_event(_event(text))

    def generated_prompt(self):
        if self.reference_calls:
            return self.reference_calls[0][0][2]
        if self.normal_calls:
            return self.normal_calls[0][1].get("prompt", "")
        return ""

    def sent_text(self):
        message = self.ctx.sent[-1]["message"]
        return "".join(
            str((seg.get("data") or {}).get("text") or "")
            for seg in message
            if seg.get("type") == "text"
        )

    async def test_shortcut_avatar_uses_reference_and_prompt(self):
        self.image_cfg["caption_ai"] = False
        self.assertTrue(await self.run_event())
        self.assertEqual(len(self.reference_calls), 1)
        prompt = self.reference_calls[0][0][2]
        self.assertTrue(prompt.startswith(AVATAR_PROMPT))
        self.assertIn("你自己的照片", prompt)
        self.assertEqual(self.normal_calls, [])
        self.assertTrue(any(seg.get("type") == "image" for seg in self.ctx.sent[-1]["message"]))
        self.assertIn("参考图", "\n".join(self.ctx.log.records))

    async def test_reference_failure_falls_back_with_avatar_prompt(self):
        self.reference_error = imagegen.ImageGenError("参考图接口 HTTP 500")
        await self.run_event()
        self.assertEqual(len(self.normal_calls), 1)
        prompt = self.normal_calls[0][1]["prompt"]
        self.assertTrue(prompt.startswith(AVATAR_PROMPT))
        self.assertIn("回退纯文生图", "\n".join(self.ctx.log.records))

    async def test_normal_drawing_unchanged(self):
        await self.run_event("画张山间日落")
        self.assertEqual(self.reference_calls, [])
        self.assertEqual(len(self.normal_calls), 1)
        self.assertEqual(self.normal_calls[0][1]["prompt"], "山间日落")

    async def test_account_mode_falls_back_to_account_generation(self):
        self.image_cfg["auth"] = "chatgpt"
        self.image_cfg["api_key"] = ""
        self.reference_error = imagegen.ImageGenError("账号额度不支持参考图")
        await self.run_event()
        self.assertEqual(len(self.account_calls), 1)
        self.assertIn(AVATAR_PROMPT, self.account_calls[0]["prompt"])
        self.assertIn("回退纯文生图", "\n".join(self.ctx.log.records))


class ReferenceEditsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.ref = self.root / "reference.png"
        self.ref.write_bytes(TINY_PNG)
        self.ai_cfg = {"avatar": {"prompt": AVATAR_PROMPT}}
        self.image_cfg = {
            "provider": "openai",
            "base_url": "https://api.openai.com/v1",
            "api_key": "img-key",
            "auth": "",
            "model": "gpt-image-1",
        }

    def test_multipart_edit_request(self):
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

        _patch(self, imagegen.urllib.request, "urlopen", fake_urlopen)
        images = imagegen.generate_with_reference(
            self.ai_cfg, self.image_cfg, "鲸鱼娘立绘", self.ref, "1024x1024", 10
        )
        self.assertEqual(images, [TINY_PNG])
        request = captured[0]
        self.assertTrue(request.full_url.endswith("/images/edits"))
        self.assertIn("/v1/images/edits", request.full_url)
        self.assertEqual(request.get_header("Authorization"), "Bearer img-key")
        self.assertIn(b'name="prompt"', request.data)
        self.assertIn(b'name="image"', request.data)
        self.assertIn("鲸鱼娘立绘".encode("utf-8"), request.data)

    def test_account_mode_reference_unsupported(self):
        image_cfg = dict(self.image_cfg, api_key="", auth="chatgpt")
        with self.assertRaises(imagegen.ImageGenError) as ctx:
            imagegen.generate_with_reference(
                self.ai_cfg, image_cfg, "鲸鱼娘", self.ref, "1024x1024", 10
            )
        self.assertIn("不支持参考图", str(ctx.exception))

    def test_http_error_hides_token(self):
        class Boom(Exception):
            pass

        def fake_urlopen(request, timeout=None):
            raise official_http_error()

        def official_http_error():
            import urllib.error

            return urllib.error.HTTPError(
                "https://api.openai.com/v1/images/edits",
                500,
                "error",
                {},
                None,
            )

        _patch(self, imagegen.urllib.request, "urlopen", fake_urlopen)
        with self.assertRaises(imagegen.ImageGenError) as ctx:
            imagegen.generate_with_reference(
                self.ai_cfg, self.image_cfg, "鲸鱼娘", self.ref, "1024x1024", 10
            )
        self.assertNotIn("img-key", str(ctx.exception))
        self.assertIn("500", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
