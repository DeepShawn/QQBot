"""T29 画图与主对话解耦：imagegen 分发 / image_gen 插件 / ai_chat 工具可用性。"""
from __future__ import annotations

import importlib
import sys
import tempfile
import unittest
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent.parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import account_core  # noqa: E402
import accounts_chatgpt  # noqa: E402
import config  # noqa: E402
import imagegen  # noqa: E402
from plugins import ai_chat  # noqa: E402
from plugins import image_gen as image_gen_plugin  # noqa: E402

TINY_PNG = b"\x89PNG\r\n\x1a\n" + b"png-body-of-sorts"


def _patch(test: unittest.TestCase, obj, name, value):
    old = getattr(obj, name)
    setattr(obj, name, value)
    test.addCleanup(setattr, obj, name, old)


def _chatgpt_module(test: unittest.TestCase):
    """拿 imagegen 里 import 会拿到的那份 accounts_chatgpt（其它测试可能换过 sys.modules）。"""
    module = importlib.import_module("accounts_chatgpt")
    previous = sys.modules.get("accounts_chatgpt")
    sys.modules["accounts_chatgpt"] = module
    if previous is None:
        test.addCleanup(sys.modules.pop, "accounts_chatgpt", None)
    else:
        test.addCleanup(sys.modules.__setitem__, "accounts_chatgpt", previous)
    return module


# ---------------------------------------------------------------- resolve_image_mode


class ResolveModeTests(unittest.TestCase):
    def test_account_beats_api_key(self):
        decision = imagegen.resolve_image_mode(
            {"auth": "claude", "api_key": "", "base_url": "https://deepseek"},
            {"auth": "chatgpt", "api_key": "img-key", "model": "gpt-image-3"},
        )
        self.assertEqual(decision["mode"], "account")
        self.assertEqual(decision["auth"], "chatgpt")
        self.assertEqual(decision["model"], "gpt-image-3")

    def test_account_default_model(self):
        decision = imagegen.resolve_image_mode(
            {}, {"auth": "chatgpt", "model": "dall-e-3"}
        )
        self.assertEqual(decision["model"], imagegen.ACCOUNT_IMAGE_MODEL)

    def test_other_account_rejected(self):
        decision = imagegen.resolve_image_mode({}, {"auth": "claude"})
        self.assertEqual(decision["mode"], "none")
        self.assertIn("只支持 ChatGPT", decision["error"])

    def test_api_key_with_default_openai_base(self):
        decision = imagegen.resolve_image_mode(
            {"auth": "claude", "api_key": "ai-key", "base_url": "https://deepseek/"},
            {"api_key": "img-key"},
        )
        self.assertEqual(decision["mode"], "api")
        self.assertEqual(decision["api_key"], "img-key")
        self.assertEqual(decision["base_url"], "https://api.openai.com/v1")
        self.assertEqual(decision["provider"], "openai")
        self.assertEqual(decision["model"], "gpt-image-1")

    def test_api_key_explicit_settings(self):
        decision = imagegen.resolve_image_mode(
            {},
            {
                "api_key": "k",
                "provider": "openai",
                "base_url": "https://relay.example/v1",
                "model": "gpt-image-9",
            },
        )
        self.assertEqual(decision["base_url"], "https://relay.example/v1")
        self.assertEqual(decision["model"], "gpt-image-9")

    def test_follow_ai_disabled_does_not_inherit(self):
        decision = imagegen.resolve_image_mode(
            {"api_key": "ai-key", "base_url": "https://deepseek"},
            {"api_key": "", "follow_ai": False},
        )
        self.assertEqual(decision["mode"], "none")
        self.assertIn("图片 API Key", decision["error"])

    def test_follow_ai_true_uses_ai_key(self):
        decision = imagegen.resolve_image_mode(
            {"auth": "", "api_key": "ai-key", "base_url": "https://deepseek"},
            {"api_key": "", "follow_ai": True},
        )
        self.assertEqual(decision["mode"], "api")
        self.assertEqual(decision["api_key"], "ai-key")
        self.assertEqual(decision["base_url"], "https://deepseek")

    def test_follow_ai_requires_ai_auth_empty(self):
        decision = imagegen.resolve_image_mode(
            {"auth": "claude", "api_key": "ai-key"},
            {"api_key": "", "follow_ai": True},
        )
        self.assertEqual(decision["mode"], "none")

    def test_nothing_configured(self):
        decision = imagegen.resolve_image_mode({}, {})
        self.assertEqual(decision["mode"], "none")
        self.assertIn("ChatGPT 账号", decision["error"])


class GenerateForAiTests(unittest.TestCase):
    def test_account_mode_passes_model(self):
        captured: dict = {}

        def fake_generate(record, prompt, size="1024x1024", model=None, timeout=120):
            captured.update(prompt=prompt, model=model, size=size)
            return [TINY_PNG]

        _patch(self, imagegen, "generate", lambda **k: self.fail("不应走 API Key"))
        _patch(
            self,
            account_core,
            "ensure_access",
            lambda provider: {"access": "a", "extra": {"chatgpt_account_id": "id"}},
        )
        _patch(self, _chatgpt_module(self), "generate_images", fake_generate)
        images = imagegen.generate_for_ai(
            {"auth": "claude"},
            {"auth": "chatgpt", "model": "gpt-image-3", "api_key": "ignored"},
            "猫",
            size="512x512",
        )
        self.assertEqual(images, [TINY_PNG])
        self.assertEqual(captured["model"], "gpt-image-3")
        self.assertEqual(captured["size"], "512x512")

    def test_api_mode_does_not_inherit_ai_base(self):
        captured: dict = {}

        def fake_generate(**kwargs):
            captured.update(kwargs)
            return [TINY_PNG]

        _patch(self, imagegen, "generate", fake_generate)
        imagegen.generate_for_ai(
            {"auth": "claude", "api_key": "ai-key", "base_url": "https://deepseek"},
            {"api_key": "img-key", "base_url": ""},
            "猫",
        )
        self.assertEqual(captured["api_key"], "img-key")
        self.assertEqual(captured["base_url"], imagegen.DEFAULT_IMAGE_BASE_URL)

    def test_follow_ai_disabled_raises(self):
        calls: list = []
        _patch(
            self, imagegen, "generate", lambda **k: calls.append(k) or [TINY_PNG]
        )
        with self.assertRaises(imagegen.ImageGenError) as ctx:
            imagegen.generate_for_ai(
                {"api_key": "ai-key", "base_url": "https://deepseek"},
                {"api_key": "", "follow_ai": False},
                "猫",
            )
        self.assertIn("图片 API Key", str(ctx.exception))
        self.assertEqual(calls, [])


# ---------------------------------------------------------------- 插件


class FakeLog:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass

    def exception(self, *args, **kwargs):
        pass


class FakeCtx:
    def __init__(self, root, plugin_cfg: dict):
        self.data_dir = Path(root)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log = FakeLog()
        self.self_id = 999
        self._cfg = plugin_cfg
        self.replies: list[str] = []
        self.sent: list[dict] = []

    @property
    def cfg(self) -> dict:
        return {"plugins": self._cfg}

    def plugin_cfg(self, name: str) -> dict:
        return self._cfg.get(name) or {}

    def is_admin(self, user_id) -> bool:
        return False

    def format_text(self, template, event, **extra):
        return str(template or "")

    async def reply(self, event, message, at: bool = False):
        self.replies.append(str(message))
        return {"message_id": len(self.replies)}

    async def send_private(self, user_id, message):
        self.sent.append({"target": "private", "message": message})

    async def send_group(self, group_id, message):
        self.sent.append({"target": "group", "message": message})


def _image_cfg(**overrides) -> dict:
    cfg = {
        "enabled": True,
        "provider": "openai",
        "base_url": "",
        "api_key": "",
        "auth": "",
        "follow_ai": False,
        "model": "gpt-image-1",
        "size": "1024x1024",
        "timeout_seconds": 30,
        "triggers": ["画一张"],
        "push_groups": [],
        "caption": "",
        "require_at": True,
        "at_sender": True,
        "cooldown_seconds": 0,
    }
    cfg.update(overrides)
    return cfg


def _ai_cfg(**overrides) -> dict:
    cfg = {
        "provider": "openai",
        "base_url": "",
        "api_key": "",
        "auth": "",
        "model": "m",
    }
    cfg.update(overrides)
    return cfg


def _make_ctx(root, image_cfg=None, ai_cfg=None) -> FakeCtx:
    return FakeCtx(
        root,
        {
            "image_gen": image_cfg if image_cfg is not None else _image_cfg(),
            "ai_chat": ai_cfg if ai_cfg is not None else _ai_cfg(),
        },
    )


def _event(text: str = "画一张猫") -> dict:
    return {
        "post_type": "message",
        "message_type": "private",
        "user_id": 20002,
        "message": [{"type": "text", "data": {"text": text}}],
    }


class ConfigDefaultTests(unittest.TestCase):
    def test_config_defaults(self):
        ai_image = config.DEFAULTS["plugins"]["image_gen"]
        self.assertIn("auth", ai_image)
        self.assertEqual(ai_image["auth"], "")
        self.assertIn("follow_ai", ai_image)
        self.assertFalse(ai_image["follow_ai"])


class ImageGenPluginModeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.api_calls: list = []
        self.account_calls: list = []
        self._orig_generate = imagegen.generate
        self._orig_account = imagegen.generate_with_account
        self.addCleanup(setattr, imagegen, "generate", self._orig_generate)
        self.addCleanup(
            setattr, imagegen, "generate_with_account", self._orig_account
        )

    def install_api(self):
        def fake_generate(**kwargs):
            self.api_calls.append(kwargs)
            return [TINY_PNG]

        imagegen.generate = fake_generate

    def install_account(self):
        def fake_account(auth, prompt, **kwargs):
            self.account_calls.append({"auth": auth, "prompt": prompt, **kwargs})
            return [TINY_PNG]

        imagegen.generate_with_account = fake_account

    async def run_plugin(self, ctx) -> bool:
        return await image_gen_plugin.Plugin(ctx).on_event(_event())

    async def test_mode_account(self):
        ctx = _make_ctx(self.root, image_cfg=_image_cfg(auth="chatgpt"))
        mode = image_gen_plugin.Plugin._mode(
            ctx.plugin_cfg("image_gen"), ctx.plugin_cfg("ai_chat")
        )
        self.assertEqual(mode[0], "account")
        self.assertEqual(mode[1], "chatgpt")

    async def test_mode_api_default_base(self):
        ctx = _make_ctx(self.root, image_cfg=_image_cfg(api_key="img-key"))
        mode = image_gen_plugin.Plugin._mode(
            ctx.plugin_cfg("image_gen"), ctx.plugin_cfg("ai_chat")
        )
        self.assertEqual(mode[0], "api")
        self.assertEqual(mode[2], imagegen.DEFAULT_IMAGE_BASE_URL)
        self.assertEqual(mode[4], "gpt-image-1")

    async def test_mode_none_error(self):
        ctx = _make_ctx(self.root)
        mode = image_gen_plugin.Plugin._mode(
            ctx.plugin_cfg("image_gen"), ctx.plugin_cfg("ai_chat")
        )
        self.assertEqual(mode[0], "none")
        self.assertIn("图片 API Key", mode[1])

    async def test_account_plugin_uses_image_auth_even_if_main_is_claude(self):
        ctx = _make_ctx(
            self.root,
            image_cfg=_image_cfg(auth="chatgpt", model=""),
            ai_cfg=_ai_cfg(auth="claude", api_key=""),
        )
        self.install_account()
        self.install_api()
        await self.run_plugin(ctx)
        self.assertEqual(len(self.account_calls), 1)
        self.assertEqual(self.account_calls[0]["auth"], "chatgpt")
        self.assertEqual(self.account_calls[0]["model"], "gpt-image-2")
        self.assertEqual(self.api_calls, [])

    async def test_account_plugin_maps_gpt_image_1_to_2(self):
        # 配置里遗留的 API 默认 gpt-image-1 在 Codex 账号通道不可用，应回退 gpt-image-2
        ctx = _make_ctx(
            self.root, image_cfg=_image_cfg(auth="chatgpt", model="gpt-image-1")
        )
        self.install_account()
        await self.run_plugin(ctx)
        self.assertEqual(self.account_calls[0]["model"], "gpt-image-2")

    async def test_account_model_passthrough(self):
        ctx = _make_ctx(
            self.root, image_cfg=_image_cfg(auth="chatgpt", model="gpt-image-9")
        )
        self.install_account()
        await self.run_plugin(ctx)
        self.assertEqual(self.account_calls[0]["model"], "gpt-image-9")

    async def test_api_plugin_uses_openai_base_not_ai_base(self):
        ctx = _make_ctx(
            self.root,
            image_cfg=_image_cfg(api_key="img-key", base_url=""),
            ai_cfg=_ai_cfg(auth="claude", api_key="ai-key", base_url="https://deepseek"),
        )
        self.install_api()
        await self.run_plugin(ctx)
        self.assertEqual(len(self.api_calls), 1)
        self.assertEqual(
            self.api_calls[0]["base_url"], imagegen.DEFAULT_IMAGE_BASE_URL
        )
        self.assertEqual(self.api_calls[0]["api_key"], "img-key")

    async def test_follow_ai_false_no_inherit(self):
        ctx = _make_ctx(
            self.root,
            image_cfg=_image_cfg(api_key="", follow_ai=False),
            ai_cfg=_ai_cfg(auth="", api_key="ai-key", base_url="https://deepseek"),
        )
        self.install_api()
        await self.run_plugin(ctx)
        self.assertEqual(self.api_calls, [])
        self.assertIn("画图需要", ctx.replies[-1])

    async def test_follow_ai_true_reuses_ai_key(self):
        ctx = _make_ctx(
            self.root,
            image_cfg=_image_cfg(api_key="", follow_ai=True),
            ai_cfg=_ai_cfg(auth="", api_key="ai-key", base_url="https://deepseek"),
        )
        self.install_api()
        await self.run_plugin(ctx)
        self.assertEqual(len(self.api_calls), 1)
        self.assertEqual(self.api_calls[0]["api_key"], "ai-key")
        self.assertEqual(self.api_calls[0]["base_url"], "https://deepseek")

    async def test_other_account_auth_replies_error(self):
        ctx = _make_ctx(
            self.root,
            image_cfg=_image_cfg(auth="claude"),
            ai_cfg=_ai_cfg(auth="claude", api_key=""),
        )
        self.install_api()
        await self.run_plugin(ctx)
        self.assertEqual(self.api_calls, [])
        self.assertIn("只支持 ChatGPT", ctx.replies[-1])


# ---------------------------------------------------------------- ai_chat 可用性


class AIChatImageToolAvailabilityTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _configured(self, image_cfg: dict, ai_cfg: dict) -> bool:
        ctx = FakeCtx(self._tmp.name, {"image_gen": image_cfg})
        plugin = ai_chat.Plugin(ctx)
        return plugin._image_tool_configured(ai_cfg)

    def test_image_account_independent_of_main_auth(self):
        self.assertTrue(
            self._configured(
                _image_cfg(auth="chatgpt"),
                _ai_cfg(auth="claude", api_key=""),
            )
        )

    def test_image_api_key(self):
        self.assertTrue(
            self._configured(_image_cfg(api_key="img-key"), _ai_cfg())
        )

    def test_follow_ai_false_no_tool(self):
        self.assertFalse(
            self._configured(
                _image_cfg(api_key="", follow_ai=False),
                _ai_cfg(auth="claude", api_key="ai-key"),
            )
        )

    def test_follow_ai_true_tool(self):
        self.assertTrue(
            self._configured(
                _image_cfg(api_key="", follow_ai=True),
                _ai_cfg(auth="", api_key="ai-key"),
            )
        )

    def test_other_account_no_tool(self):
        self.assertFalse(
            self._configured(_image_cfg(auth="claude"), _ai_cfg(auth="claude"))
        )

    def test_disabled_no_tool(self):
        self.assertFalse(
            self._configured(
                _image_cfg(auth="chatgpt", enabled=False), _ai_cfg()
            )
        )


if __name__ == "__main__":
    unittest.main()
