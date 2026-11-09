"""T46 生图意图稳定触发：确定性兜底 + 强制画图流程 + image_gen 内置触发词。"""
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
import providers  # noqa: E402
from plugins import ai_chat  # noqa: E402
from plugins import image_gen as image_gen_plugin  # noqa: E402

TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
SELF_ID = 999
SELF_ID = 999


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


class FakeApi:
    async def call(self, action, **params):
        return {}


class FakeCtx:
    def __init__(self, root, cfg=None):
        self.data_dir = Path(root)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log = FakeLog()
        self.self_id = SELF_ID
        self.bot_name = "测试机器人"
        self.api = FakeApi()
        self._cfg = cfg or {}
        self.replies: list[str] = []
        self.sent: list[dict] = []

    @property
    def cfg(self) -> dict:
        return self._cfg

    def plugin_cfg(self, name: str) -> dict:
        return (self._cfg.get("plugins") or {}).get(name) or {}

    def is_admin(self, user_id) -> bool:
        return False

    def format_text(self, template, event, **extra):
        return str(template or "")

    async def reply(self, event, message, at: bool = False):
        if isinstance(message, list):
            text = "".join(
                str((seg.get("data") or {}).get("text", ""))
                for seg in message
                if isinstance(seg, dict) and seg.get("type") == "text"
            )
        else:
            text = str(message or "")
        self.replies.append(text)
        return {"message_id": len(self.replies)}

    async def send_private(self, user_id, message):
        self.sent.append({"type": "private", "user_id": user_id, "message": message})

    async def send_group(self, group_id, message):
        self.sent.append({"type": "group", "group_id": group_id, "message": message})


def _ai_cfg(**overrides) -> dict:
    cfg = {
        "enabled": True,
        "provider": "openai",
        "base_url": "",
        "api_key": "key",
        "auth": "",
        "model": "m",
        "temperature": 0.7,
        "max_tokens": 500,
        "timeout_seconds": 10,
        "system_prompt": "你是助手",
        "history_ttl_minutes": 30,
        "trigger": {
            "private": True,
            "group_at": True,
            "group_quote": True,
            "group_followup_seconds": 0,
            "group_prefix": [],
            "group_name_aliases": [],
        },
    }
    cfg.update(overrides)
    return cfg


def _image_cfg(**overrides) -> dict:
    cfg = {
        "enabled": True,
        "provider": "openai",
        "base_url": "",
        "api_key": "",
        "auth": "chatgpt",
        "follow_ai": False,
        "model": "gpt-image-1",
        "size": "1024x1024",
        "timeout_seconds": 30,
        "triggers": [],
        "push_groups": [],
        "caption": "",
        "require_at": True,
        "at_sender": True,
        "cooldown_seconds": 0,
    }
    cfg.update(overrides)
    return cfg


def _make_ctx(root, ai_cfg=None, image_cfg=None, image_enabled=True) -> FakeCtx:
    plugins = {
        "ai_chat": ai_cfg if ai_cfg is not None else _ai_cfg(),
        "image_gen": _image_cfg(enabled=image_enabled, **(image_cfg or {})),
        "python_run": {"enabled": False},
        "dev_tools": {"enabled": False},
        "skills": {"enabled": False},
    }
    return FakeCtx(root, {"plugins": plugins})


def _event(text: str = "画张风景图", *, message_type="private", mention=False) -> dict:
    segs = []
    if mention:
        segs.append({"type": "at", "data": {"qq": str(SELF_ID)}})
    segs.append({"type": "text", "data": {"text": text}})
    event = {
        "post_type": "message",
        "message_type": message_type,
        "user_id": 20002,
        "message": segs,
    }
    if message_type == "group":
        event["group_id"] = 12345
    return event


def _write_history(root: Path, messages: list[dict]) -> None:
    payload = {
        "private:20002": {"updated_at": 9_999_999_999, "messages": messages}
    }
    (root / "context.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


class IntentDetectionTests(unittest.TestCase):
    def test_true_cases(self):
        for text in (
            "画张风景图",
            "帮我画一张猫",
            "来一张日落",
            "生成一张图片",
            "生成图：海边",
            "绘制一只小狗",
            "来个壁纸",
            "使用画图功能",
            "用画图功能画只猫",
            "画一下这个场景",
            "画只猫",
            "画一幅山水",
            "画一个太阳",
            "帮我画张插画",
            "做张海报",
            "换个壁纸",
            "画个头像",
            "来张风景图",
            "做个 logo",
            "来个表情包",
            "draw a cat",
            "illustration of a cat",
            "image of a sunset",
            "画图",
        ):
            with self.subTest(text=text):
                self.assertTrue(ai_chat.Plugin._looks_like_image_request(text))

    def test_false_cases(self):
        for text in (
            "你好呀",
            "今天天气怎么样",
            "讲个笑话",
            "帮我写个 Python 脚本",
            "总结一下这段话",
            "继续",  # 没有历史时不算生图
            "",
        ):
            with self.subTest(text=text):
                self.assertFalse(ai_chat.Plugin._looks_like_image_request(text))

    def test_prompt_hardening(self):
        hint = ai_chat.LOCAL_IMAGE_TOOL_HINT
        self.assertIn("必须调用", hint)
        self.assertIn("绝对不要用 ASCII/emoji/代码画图", hint)
        self.assertIn("SVG", hint)
        spec = ai_chat.IMAGE_TOOL_SPEC["function"]["description"]
        self.assertIn("必须直接生成图片", spec)
        self.assertIn("ASCII", spec)
        route = ai_chat.ROUTE_SYSTEM_HEAD
        self.assertIn("必须选择 generate_image", route)
        self.assertIn("ASCII", route)
        self.assertIn("SVG", route)
        writer = ai_chat.IMAGE_PROMPT_FROM_HISTORY_SYSTEM
        self.assertIn("生图提示词", writer)
        self.assertIn("不要照抄", writer)


class ForcedImageFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.ctx = _make_ctx(
            self.root, ai_cfg=_ai_cfg(auth="chatgpt", api_key="")
        )
        self.chat_calls: list = []
        self.chat_script: list = []
        self.image_calls: list = []
        self._orig_chat = ai_chat.providers.chat
        self._orig_generate = imagegen.generate_for_ai
        self.addCleanup(setattr, ai_chat.providers, "chat", self._orig_chat)
        self.addCleanup(setattr, imagegen, "generate_for_ai", self._orig_generate)

    def install_chat(self, script=None):
        self.chat_script = list(script or [])

        def fake_chat(**kwargs):
            self.chat_calls.append(kwargs)
            if not self.chat_script:
                return "普通回答"
            item = self.chat_script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        ai_chat.providers.chat = fake_chat

    def install_image(self, result=None, error=None):
        def fake_generate(*args, **kwargs):
            self.image_calls.append((args, kwargs))
            if error is not None:
                raise error
            return list(result or [TINY_PNG])

        imagegen.generate_for_ai = fake_generate

    async def run_event(self, event=None) -> bool:
        return await ai_chat.Plugin(self.ctx).on_event(event or _event())

    def last_reply(self) -> str:
        return self.ctx.replies[-1]

    async def test_forced_image_flow_with_history(self):
        history = [
            {"role": "user", "content": "历史1"},
            {"role": "assistant", "content": "历史2"},
            {"role": "user", "content": "历史3"},
            {"role": "assistant", "content": "历史4"},
            {"role": "user", "content": "历史5"},
            {"role": "assistant", "content": "历史6"},
            {"role": "user", "content": "历史7"},
            {"role": "assistant", "content": "历史8"},
        ]
        _write_history(self.root, history)
        self.install_chat(
            ["一只橘猫坐在窗台上，水彩风格，暖色阳光，竖构图", "画好啦，橘猫在窗台上晒太阳～"]
        )
        self.install_image()
        self.assertTrue(await self.run_event(_event("画张橘猫的图")))
        # 只调两次模型：写提示词 + 最终描述（没走路由/没被 ASCII 忽悠）
        self.assertEqual(len(self.chat_calls), 2)
        writer_messages = self.chat_calls[0]["messages"]
        self.assertEqual(writer_messages[0]["role"], "system")
        visible = [m for m in writer_messages[1:] if m.get("role") in ("user", "assistant")]
        self.assertEqual(len(visible), 7)  # 最近 6 条历史 + 本次需求
        self.assertEqual(visible[0]["content"], "历史3")
        self.assertEqual(visible[-1]["role"], "user")
        self.assertIn("画张橘猫的图", visible[-1]["content"])
        # 画图用写好的提示词
        self.assertEqual(len(self.image_calls), 1)
        image_prompt = self.image_calls[0][0][2]
        self.assertEqual(image_prompt, "一只橘猫坐在窗台上，水彩风格，暖色阳光，竖构图")
        # 图片只发一次，且是 image 段
        self.assertEqual(len(self.ctx.sent), 1)
        message = self.ctx.sent[0]["message"]
        self.assertTrue(any(seg.get("type") == "image" for seg in message))
        # 最终描述来自主模型
        self.assertEqual(self.last_reply(), "画好啦，橘猫在窗台上晒太阳～")
        # 最终描述指令里禁止 ASCII/emoji 冒充图片
        final_system = "\n".join(
            str(m.get("content") or "")
            for m in self.chat_calls[1]["messages"]
            if m.get("role") == "system"
        )
        self.assertIn("绝对不要用 ASCII", final_system)
        self.assertIn("不要重复提示词", final_system)

    async def test_context_continue_triggers_forced_image(self):
        _write_history(
            self.root,
            [
                {"role": "user", "content": "画张风景图"},
                {"role": "assistant", "content": "好的"},
            ],
        )
        self.install_chat(["一张黄昏山谷风景插画，柔和光线，横构图", "画好啦～"])
        self.install_image()
        await self.run_event(_event("继续"))
        self.assertEqual(len(self.image_calls), 1)
        self.assertEqual(
            self.image_calls[0][0][2], "一张黄昏山谷风景插画，柔和光线，横构图"
        )
        self.assertEqual(len(self.chat_calls), 2)

    async def test_writer_failure_continue_uses_last_image_request_from_history(self):
        _write_history(
            self.root,
            [
                {"role": "user", "content": "画张风景图"},
                {"role": "assistant", "content": "好的"},
            ],
        )
        self.install_chat([providers.ProviderError("down"), "画好了～"])
        self.install_image()
        await self.run_event(_event("继续"))
        self.assertEqual(len(self.image_calls), 1)
        self.assertEqual(self.image_calls[0][0][2], "画张风景图")

    async def test_image_not_configured_falls_back_to_text(self):
        self.ctx = _make_ctx(
            self.root,
            ai_cfg=_ai_cfg(auth="chatgpt", api_key=""),
            image_enabled=False,
        )
        self.install_chat(["我暂时不能画图"])
        self.install_image()
        await self.run_event(_event("画张风景图"))
        self.assertEqual(self.image_calls, [])
        self.assertEqual(self.last_reply(), "我暂时不能画图")

    async def test_non_image_request_keeps_route(self):
        self.install_chat(['{"tool":"none"}', "普通回答"])
        self.install_image()
        await self.run_event(_event("讲个笑话"))
        self.assertEqual(len(self.chat_calls), 2)
        self.assertEqual(self.image_calls, [])
        self.assertEqual(self.last_reply(), "普通回答")

    async def test_writer_failure_falls_back_to_user_text(self):
        self.install_chat([providers.ProviderError("down"), "画好了～"])
        self.install_image()
        await self.run_event(_event("画张风景图"))
        self.assertEqual(len(self.image_calls), 1)
        self.assertEqual(self.image_calls[0][0][2], "画张风景图")
        self.assertEqual(self.last_reply(), "画好了～")

    async def test_empty_final_answer_falls_back(self):
        self.install_chat(["一只猫，水彩风格，柔和光线", ""])
        self.install_image()
        await self.run_event(_event("来一张猫的图"))
        self.assertEqual(self.last_reply(), ai_chat.IMAGE_PROMPT_FALLBACK_REPLY)

    async def test_image_generation_failure_falls_back_to_chat(self):
        self.install_chat(["一只猫，水彩风格，柔和光线", "抱歉，画图失败了"])
        self.install_image(error=imagegen.ImageGenError("画图失败"))
        await self.run_event(_event("画张猫"))
        self.assertEqual(self.ctx.sent, [])
        self.assertEqual(self.last_reply(), "抱歉，画图失败了")

    async def test_native_mode_not_forced(self):
        native_calls: list = []

        def fake_native(**kwargs):
            native_calls.append(kwargs)
            return {"content": "这是 ASCII 画", "tool_calls": []}

        _patch(self, ai_chat.providers, "chat_with_tools", fake_native)
        self.ctx = _make_ctx(self.root, ai_cfg=_ai_cfg(auth="", api_key="key"))
        self.install_chat([])
        self.install_image()
        await self.run_event(_event("画张风景图"))
        self.assertEqual(len(native_calls), 1)
        self.assertEqual(self.image_calls, [])


class BuiltinTriggerTests(unittest.TestCase):
    def make_plugin(self, triggers, root=None) -> tuple[image_gen_plugin.Plugin, FakeCtx]:
        if root is None:
            tmp = tempfile.TemporaryDirectory()
            self.addCleanup(tmp.cleanup)
            root = Path(tmp.name)
        ctx = _make_ctx(root, image_cfg={"triggers": list(triggers)})
        return image_gen_plugin.Plugin(ctx), ctx

    def prompt_of(self, plugin, text, *, message_type="private", mention=False):
        return plugin._prompt_of(_event(text, message_type=message_type, mention=mention), plugin.ctx.plugin_cfg("image_gen"))

    def test_builtin_triggers_extract_prompt(self):
        plugin, _ctx = self.make_plugin([])
        cases = {
            "画张风景图": "风景图",
            "来一张日落": "日落",
            "生成一张猫": "猫",
            "生成一张图片": "一张风景插画",
            "绘制一只小狗": "一只小狗",
            "生成一张图片": "一张风景插画",
            "画个苹果": "苹果",
            "画一个太阳": "太阳",
            "帮我画只猫": "猫",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(self.prompt_of(plugin, text), expected)

    def test_builtin_pure_trigger_uses_default_prompt(self):
        plugin, _ctx = self.make_plugin([])
        self.assertEqual(self.prompt_of(plugin, "使用画图功能"), "一张风景插画")
        self.assertEqual(self.prompt_of(plugin, "画一张"), "一张风景插画")

    def test_builtin_group_requires_at(self):
        plugin, _ctx = self.make_plugin([])
        self.assertIsNone(self.prompt_of(plugin, "画张风景图", message_type="group"))
        self.assertEqual(
            self.prompt_of(plugin, "画张风景图", message_type="group", mention=True),
            "风景图",
        )

    def test_configured_pure_trigger_keeps_old_behavior(self):
        plugin, _ctx = self.make_plugin(["生图"])
        self.assertIsNone(self.prompt_of(plugin, "生图"))
        self.assertEqual(self.prompt_of(plugin, "生图 一只猫"), "一只猫")

    def test_unrelated_text_not_triggered(self):
        plugin, _ctx = self.make_plugin([])
        self.assertIsNone(self.prompt_of(plugin, "今天天气不错"))
        # 壁纸/头像只作为 AI 生图意图识别，不单独触发 image_gen 插件（避免误伤）
        self.assertIsNone(self.prompt_of(plugin, "换个壁纸"))
        self.assertIsNone(self.prompt_of(plugin, "头像好看吗"))


class BuiltinTriggerEventTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.ctx = _make_ctx(
            self.root, image_cfg={"auth": "", "api_key": "img-key"}
        )
        self.image_calls: list = []
        self._orig_generate = imagegen.generate
        self.addCleanup(setattr, imagegen, "generate", self._orig_generate)

        def fake_generate(*args, **kwargs):
            self.image_calls.append((args, kwargs))
            return [TINY_PNG]

        imagegen.generate = fake_generate

    async def test_event_generates_image(self):
        plugin = image_gen_plugin.Plugin(self.ctx)
        self.assertTrue(await plugin.on_event(_event("画张风景图")))
        self.assertEqual(len(self.image_calls), 1)
        self.assertEqual(self.image_calls[0][1]["prompt"], "风景图")
        self.assertEqual(len(self.ctx.sent), 1)
        message = self.ctx.sent[0]["message"]
        self.assertTrue(any(seg.get("type") == "image" for seg in message))


if __name__ == "__main__":
    unittest.main()
