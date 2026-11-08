"""定时发送 bot/scheduled.py + /报时 插件离线测试。"""
from __future__ import annotations

import base64
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent.parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import config  # noqa: E402
import imagegen  # noqa: E402
import providers  # noqa: E402
import scheduled as scheduled_core  # noqa: E402
from plugins import scheduled as scheduled_plugin  # noqa: E402

TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
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
    def __init__(self):
        self.member = {"role": "member"}
        self.calls: list = []

    async def call(self, action, **params):
        self.calls.append((action, params))
        if action == "get_group_member_info":
            return dict(self.member)
        return {}


class FakeCtx:
    def __init__(
        self,
        root,
        scheduled_cfg=None,
        ai_cfg=None,
        image_cfg=None,
        admins=(),
    ):
        self.data_dir = Path(root)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log = FakeLog()
        self.self_id = SELF_ID
        self.api = FakeApi()
        self._cfg = {
            "bot": {"admins": list(admins)},
            "plugins": {
                "scheduled": dict(
                    scheduled_cfg
                    if scheduled_cfg is not None
                    else {"enabled": True, "use_image": False}
                ),
                "ai_chat": dict(ai_cfg or {"provider": "openai", "api_key": "key"}),
                "image_gen": dict(image_cfg or {"enabled": False}),
            },
        }
        self.replies: list[str] = []
        self.sent: list[dict] = []

    @property
    def cfg(self) -> dict:
        return self._cfg

    def plugin_cfg(self, name: str) -> dict:
        return (self._cfg.get("plugins") or {}).get(name) or {}

    def is_admin(self, user_id) -> bool:
        return str(user_id) in {str(a) for a in (self._cfg["bot"]["admins"] or [])}

    def format_text(self, template, event, **extra):
        return str(template or "")

    async def reply(self, event, message, at: bool = False):
        self.replies.append(str(message))
        return {"message_id": len(self.replies)}

    async def send_private(self, user_id, message):
        self.sent.append({"target": "private", "id": int(user_id), "message": message})

    async def send_group(self, group_id, message):
        self.sent.append({"target": "group", "id": int(group_id), "message": message})


def _event(text: str, *, private: bool = True, mention: bool = False, user_id: int = 20002):
    segs = []
    if mention:
        segs.append({"type": "at", "data": {"qq": str(SELF_ID)}})
    if text:
        segs.append({"type": "text", "data": {"text": text}})
    event = {
        "post_type": "message",
        "message_type": "private" if private else "group",
        "user_id": user_id,
        "message": segs,
    }
    if not private:
        event["group_id"] = 12345
    return event


def _ts(hour: int, minute: int = 1) -> float:
    return time.mktime((2026, 1, 1, hour, minute, 0, 0, 0, -1))


class GreetingTests(unittest.TestCase):
    def test_labels(self):
        cases = {
            0: "夜深了",
            4: "夜深了",
            5: "早上好",
            8: "早上好",
            9: "上午好",
            10: "上午好",
            11: "中午好",
            12: "中午好",
            13: "下午好",
            17: "下午好",
            18: "晚上好",
            22: "晚上好",
            23: "夜深了",
        }
        for hour, label in cases.items():
            with self.subTest(hour=hour):
                self.assertEqual(scheduled_core.greeting_label(hour), label)

    def test_defaults(self):
        self.assertIn("早上好", scheduled_core.default_greeting("早上好"))
        self.assertIn("治愈", scheduled_core.default_image_prompt("早上好"))
        self.assertIn("治愈", scheduled_core.default_image_prompt("晚上好"))


class ScheduledBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.ctx = FakeCtx(self.root)
        self.chat_calls: list = []
        self.chat_result = '{"greeting":"早上好呀，今天也要开心～","image_prompt":"一只橘猫坐在窗台，晨光" }'
        self.chat_error: Exception | None = None
        self.image_calls: list = []
        self.image_result = [TINY_PNG]
        self.image_error: Exception | None = None
        self._orig_chat = scheduled_core.providers.chat
        self._orig_generate = scheduled_core.imagegen.generate_for_ai
        self.addCleanup(
            setattr, scheduled_core.providers, "chat", self._orig_chat
        )
        self.addCleanup(
            setattr, scheduled_core.imagegen, "generate_for_ai", self._orig_generate
        )
        self.install_chat()
        self.install_image()

    def install_chat(self):
        def fake_chat(**kwargs):
            self.chat_calls.append(kwargs)
            if self.chat_error is not None:
                raise self.chat_error
            return self.chat_result

        scheduled_core.providers.chat = fake_chat

    def install_image(self):
        def fake_generate(ai_cfg, image_cfg, prompt, size="1024x1024", timeout=120):
            self.image_calls.append(
                {
                    "ai_cfg": ai_cfg,
                    "image_cfg": image_cfg,
                    "prompt": prompt,
                    "size": size,
                }
            )
            if self.image_error is not None:
                raise self.image_error
            return list(self.image_result)

        scheduled_core.imagegen.generate_for_ai = fake_generate

    def image_config(self) -> dict:
        return {
            "enabled": True,
            "auth": "chatgpt",
            "api_key": "",
            "follow_ai": False,
            "size": "1024x1024",
            "timeout_seconds": 30,
        }

    def write_state(self, last_sent: float) -> None:
        path = scheduled_core.state_path(self.ctx)
        path.write_text(json.dumps({"last_sent": last_sent}), encoding="utf-8")

    def read_state(self) -> dict:
        path = scheduled_core.state_path(self.ctx)
        return json.loads(path.read_text(encoding="utf-8"))

    def write_context(self, data: dict) -> None:
        (self.root / "context.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )


class TickTests(ScheduledBase):
    async def test_disabled_no_send(self):
        self.ctx = FakeCtx(
            self.root, scheduled_cfg={"enabled": False, "use_image": False}
        )
        self.assertFalse(await scheduled_core.tick(self.ctx, now=_ts(9)))
        self.assertEqual(self.ctx.sent, [])

    async def test_not_due(self):
        now = _ts(9)
        self.write_state(now - 60)
        self.assertFalse(await scheduled_core.tick(self.ctx, now=now))
        self.assertEqual(self.ctx.sent, [])

    async def test_interval_clamped_minimum(self):
        now = _ts(9)
        self.ctx = FakeCtx(
            self.root,
            scheduled_cfg={
                "enabled": True,
                "use_image": False,
                "interval_minutes": 1,
            },
        )
        self.write_state(now - 120)  # 120s < 5min 最小值
        self.assertFalse(await scheduled_core.tick(self.ctx, now=now))
        self.assertEqual(self.ctx.sent, [])

    async def test_due_sends_once_and_saves_state(self):
        now = _ts(9)
        self.ctx = FakeCtx(
            self.root,
            scheduled_cfg={"enabled": True, "use_image": False},
            admins=[777],
        )
        self.write_state(now - 301 * 60)
        self.assertTrue(await scheduled_core.tick(self.ctx, now=now))
        self.assertEqual(len(self.ctx.sent), 1)
        self.assertEqual(self.ctx.sent[0]["target"], "private")
        self.assertEqual(self.ctx.sent[0]["id"], 777)
        state = self.read_state()
        self.assertAlmostEqual(state["last_sent"], now, places=1)
        # 同一时间再 tick 不会重复发
        self.assertFalse(await scheduled_core.tick(self.ctx, now=now))
        self.assertEqual(len(self.ctx.sent), 1)

    async def test_no_target_skips(self):
        now = _ts(9)
        self.ctx = FakeCtx(self.root, scheduled_cfg={"enabled": True, "use_image": False})
        self.assertFalse(await scheduled_core.tick(self.ctx, now=now))
        self.assertEqual(self.ctx.sent, [])

    async def test_auto_target_recent_private(self):
        now = _ts(9)
        self.write_context(
            {
                "private:111": {"updated_at": 100},
                "private:222": {"updated_at": 200},
                "group9:5": {"updated_at": 999},
            }
        )
        await scheduled_core.tick(self.ctx, now=now)
        self.assertEqual(len(self.ctx.sent), 1)
        self.assertEqual(self.ctx.sent[0]["target"], "private")
        self.assertEqual(self.ctx.sent[0]["id"], 222)

    async def test_auto_target_falls_back_to_admin(self):
        now = _ts(9)
        self.ctx = FakeCtx(
            self.root,
            scheduled_cfg={"enabled": True, "use_image": False},
            admins=[333],
        )
        await scheduled_core.tick(self.ctx, now=now)
        self.assertEqual(self.ctx.sent[0]["target"], "private")
        self.assertEqual(self.ctx.sent[0]["id"], 333)

    async def test_private_target(self):
        now = _ts(9)
        self.ctx = FakeCtx(
            self.root,
            scheduled_cfg={
                "enabled": True,
                "use_image": False,
                "target_type": "private",
                "target_id": 444,
            },
        )
        await scheduled_core.tick(self.ctx, now=now)
        self.assertEqual(self.ctx.sent[0]["target"], "private")
        self.assertEqual(self.ctx.sent[0]["id"], 444)

    async def test_group_target(self):
        now = _ts(9)
        self.ctx = FakeCtx(
            self.root,
            scheduled_cfg={
                "enabled": True,
                "use_image": False,
                "target_type": "group",
                "target_id": 555,
            },
        )
        await scheduled_core.tick(self.ctx, now=now)
        self.assertEqual(self.ctx.sent[0]["target"], "group")
        self.assertEqual(self.ctx.sent[0]["id"], 555)

    async def test_force_ignores_interval(self):
        now = _ts(9)
        self.ctx = FakeCtx(
            self.root,
            scheduled_cfg={"enabled": True, "use_image": False},
            admins=[777],
        )
        self.write_state(now - 10)
        result = await scheduled_core.run_once(self.ctx, now=now)
        self.assertTrue(result["sent"])
        self.assertEqual(len(self.ctx.sent), 1)


class ContentTests(ScheduledBase):
    def make_ctx_with_image(self):
        self.ctx = FakeCtx(
            self.root,
            scheduled_cfg={
                "enabled": True,
                "target_type": "private",
                "target_id": 444,
                "use_image": True,
                "image_size": "1024x1024",
            },
            image_cfg=self.image_config(),
        )

    async def test_normal_model_json_and_image(self):
        self.make_ctx_with_image()
        now = _ts(9)
        await scheduled_core.tick(self.ctx, now=now)
        self.assertEqual(len(self.ctx.sent), 1)
        message = self.ctx.sent[0]["message"]
        self.assertEqual(len(message), 2)
        self.assertEqual(message[0]["type"], "text")
        self.assertIn("早上好", message[0]["data"]["text"])
        self.assertEqual(message[1]["type"], "image")
        self.assertTrue(message[1]["data"]["file"].startswith("base64://"))
        self.assertEqual(len(self.chat_calls), 1)
        self.assertEqual(self.chat_calls[0]["temperature"], 0.8)
        self.assertEqual(self.chat_calls[0]["max_tokens"], 400)
        self.assertEqual(len(self.image_calls), 1)
        self.assertEqual(self.image_calls[0]["prompt"], "一只橘猫坐在窗台，晨光")

    async def test_fenced_json(self):
        self.make_ctx_with_image()
        self.chat_result = (
            "好的：\n```json\n"
            '{"greeting":"上午好，记得喝水～","image_prompt":"清晨森林，柔和光线"}\n'
            "```"
        )
        await scheduled_core.tick(self.ctx, now=_ts(9))
        message = self.ctx.sent[0]["message"]
        self.assertIn("记得喝水", message[0]["data"]["text"])
        self.assertEqual(self.image_calls[0]["prompt"], "清晨森林，柔和光线")

    async def test_bad_json_falls_back_to_defaults(self):
        self.make_ctx_with_image()
        self.chat_result = "这不是 JSON"
        await scheduled_core.tick(self.ctx, now=_ts(9))
        message = self.ctx.sent[0]["message"]
        self.assertIn("上午好", message[0]["data"]["text"])
        self.assertIn("治愈", self.image_calls[0]["prompt"])

    async def test_model_failure_still_sends_default(self):
        self.make_ctx_with_image()
        self.chat_error = providers.ProviderError("model down")
        await scheduled_core.tick(self.ctx, now=_ts(9))
        self.assertEqual(len(self.ctx.sent), 1)
        self.assertIn("上午好", self.ctx.sent[0]["message"][0]["data"]["text"])
        self.assertIn("治愈", self.image_calls[0]["prompt"])

    async def test_image_failure_sends_text_only(self):
        self.make_ctx_with_image()
        self.image_error = imagegen.ImageGenError("画图失败")
        await scheduled_core.tick(self.ctx, now=_ts(9))
        self.assertEqual(len(self.ctx.sent), 1)
        message = self.ctx.sent[0]["message"]
        self.assertEqual(len(message), 1)
        self.assertEqual(message[0]["type"], "text")

    async def test_use_image_false_skips_image(self):
        now = _ts(9)
        self.ctx = FakeCtx(
            self.root,
            scheduled_cfg={
                "enabled": True,
                "use_image": False,
                "target_type": "private",
                "target_id": 444,
            },
        )
        await scheduled_core.tick(self.ctx, now=now)
        self.assertEqual(len(self.ctx.sent), 1)
        self.assertEqual(len(self.ctx.sent[0]["message"]), 1)
        self.assertEqual(self.image_calls, [])

    async def test_image_config_none_sends_text_only(self):
        self.ctx = FakeCtx(
            self.root,
            scheduled_cfg={
                "enabled": True,
                "target_type": "private",
                "target_id": 444,
                "use_image": True,
            },
            image_cfg={"enabled": True, "auth": "", "api_key": "", "follow_ai": False},
        )
        await scheduled_core.tick(self.ctx, now=_ts(9))
        self.assertEqual(len(self.ctx.sent[0]["message"]), 1)
        self.assertEqual(self.image_calls, [])

    async def test_logs_no_token(self):
        self.make_ctx_with_image()
        self.ctx._cfg["plugins"]["ai_chat"] = {
            "provider": "openai",
            "api_key": "SECRET-AI-KEY",
            "model": "m",
        }
        self.ctx._cfg["plugins"]["image_gen"]["api_key"] = "SECRET-IMG-KEY"
        await scheduled_core.tick(self.ctx, now=_ts(9))
        logs = "\n".join(self.ctx.log.records)
        self.assertNotIn("SECRET-AI-KEY", logs)
        self.assertNotIn("SECRET-IMG-KEY", logs)


class PluginTests(ScheduledBase):
    def plugin(self, admins=()):
        self.ctx = FakeCtx(self.root, admins=admins)
        return scheduled_plugin.Plugin(self.ctx)

    def install_run_once(self, result: dict):
        calls: list = []

        async def fake_run_once(ctx, now=None):
            calls.append(ctx)
            return dict(result)

        _patch(self, scheduled_core, "run_once", fake_run_once)
        return calls

    async def test_private_baoshi(self):
        plugin = self.plugin()
        calls = self.install_run_once(
            {
                "sent": True,
                "target": {"type": "private", "id": 444},
                "image": True,
            }
        )
        handled = await plugin.on_event(_event("/报时", private=True))
        self.assertTrue(handled)
        self.assertEqual(len(calls), 1)
        self.assertIn("已发送", self.ctx.replies[-1])
        self.assertIn("含图片", self.ctx.replies[-1])

    async def test_group_member_denied(self):
        plugin = self.plugin()
        calls = self.install_run_once({"sent": True})
        self.ctx.api.member = {"role": "member"}
        handled = await plugin.on_event(_event("/报时", private=False, mention=True))
        self.assertTrue(handled)
        self.assertEqual(self.ctx.replies[-1], scheduled_plugin.PERMISSION_TEXT)
        self.assertEqual(calls, [])

    async def test_group_owner_allowed(self):
        plugin = self.plugin()
        calls = self.install_run_once({"sent": True, "target": {"type": "group", "id": 1}})
        self.ctx.api.member = {"role": "owner"}
        await plugin.on_event(_event("/报时", private=False, mention=True))
        self.assertEqual(len(calls), 1)
        self.assertIn("已发送", self.ctx.replies[-1])

    async def test_baoshi_no_target_reply(self):
        plugin = self.plugin()
        self.install_run_once({"sent": False, "reason": "no-target"})
        await plugin.on_event(_event("/报时", private=True))
        self.assertIn("没有可用", self.ctx.replies[-1])

    async def test_non_command_ignored(self):
        plugin = self.plugin()
        self.assertFalse(await plugin.on_event(_event("你好", private=True)))


class IntegrationTests(unittest.TestCase):
    def test_config_defaults(self):
        cfg = config.DEFAULTS["plugins"]["scheduled"]
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["interval_minutes"], 301)
        self.assertEqual(cfg["target_type"], "auto")
        self.assertTrue(cfg["use_image"])
        self.assertEqual(cfg["image_size"], "1024x1024")

    def test_main_wires_scheduled(self):
        source = (BOT_DIR / "main.py").read_text(encoding="utf-8")
        self.assertIn("scheduled.tick", source)
        self.assertIn("scheduled_loop", source)
        self.assertIn("scheduled_plugin", source)

    def test_plugins_init_imports_scheduled(self):
        source = (BOT_DIR / "plugins" / "__init__.py").read_text(encoding="utf-8")
        self.assertIn("scheduled", source)


if __name__ == "__main__":
    unittest.main()
