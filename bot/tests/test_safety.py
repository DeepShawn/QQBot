"""防封（low_risk）模式离线测试：配置夹取、限频、白名单、内容过滤、Context 发送集成。"""
from __future__ import annotations

import asyncio
import copy
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

BOT_DIR = Path(__file__).resolve().parent.parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import config  # noqa: E402
import safety  # noqa: E402
from context import Context  # noqa: E402
from utils import text_seg  # noqa: E402


# ---------------------------------------------------------------------------
# 配置夹取
# ---------------------------------------------------------------------------

class NormalizeTests(unittest.TestCase):
    def test_defaults_when_empty(self):
        conf = safety.normalize({})
        self.assertEqual(conf["low_risk"], True)
        self.assertEqual(conf["reply_delay_min"], 1.0)
        self.assertEqual(conf["reply_delay_max"], 3.0)
        self.assertEqual(conf["max_per_minute"], 20)
        self.assertEqual(conf["max_per_hour"], 300)
        self.assertEqual(conf["block_links_group"], True)
        self.assertEqual(conf["banned_words"], [])
        self.assertEqual(conf["group_whitelist"], [])

    def test_delay_clamped_and_swapped(self):
        self.assertEqual(safety.normalize({"reply_delay_min": -5, "reply_delay_max": -1})["reply_delay_min"], 0.0)
        conf = safety.normalize({"reply_delay_min": 100, "reply_delay_max": 200})
        self.assertEqual(conf["reply_delay_min"], 30.0)
        self.assertEqual(conf["reply_delay_max"], 30.0)
        conf = safety.normalize({"reply_delay_min": 5, "reply_delay_max": 2})
        self.assertEqual((conf["reply_delay_min"], conf["reply_delay_max"]), (2.0, 5.0))

    def test_invalid_numbers_fall_back(self):
        conf = safety.normalize(
            {"reply_delay_min": "x", "reply_delay_max": None, "max_per_minute": {}, "max_per_hour": []}
        )
        self.assertEqual(conf["reply_delay_min"], 1.0)
        self.assertEqual(conf["reply_delay_max"], 3.0)
        self.assertEqual(conf["max_per_minute"], 20)
        self.assertEqual(conf["max_per_hour"], 300)

    def test_rate_clamped(self):
        self.assertEqual(safety.normalize({"max_per_minute": 0})["max_per_minute"], 1)
        self.assertEqual(safety.normalize({"max_per_minute": -9})["max_per_minute"], 1)
        self.assertEqual(safety.normalize({"max_per_minute": 99999})["max_per_minute"], 600)
        self.assertEqual(safety.normalize({"max_per_hour": 0})["max_per_hour"], 1)
        self.assertEqual(safety.normalize({"max_per_hour": 10**9})["max_per_hour"], 10000)

    def test_bool_strings(self):
        self.assertFalse(safety.normalize({"low_risk": "false"})["low_risk"])
        self.assertTrue(safety.normalize({"low_risk": "true"})["low_risk"])
        self.assertFalse(safety.normalize({"block_links_group": "0"})["block_links_group"])
        self.assertTrue(safety.normalize({"block_links_group": "yes"})["block_links_group"])

    def test_word_lists(self):
        conf = safety.normalize({"banned_words": ["加群", " 加群 ", "", None, 123]})
        self.assertEqual(conf["banned_words"], ["加群", "123"])
        conf = safety.normalize({"group_whitelist": [123, "456", " 456 "]})
        self.assertEqual(conf["group_whitelist"], ["123", "456"])
        self.assertEqual(safety.normalize({"banned_words": "加群"})["banned_words"], [])

    def test_cfg_from_context_like_object(self):
        ctx = types.SimpleNamespace(cfg={"safety": {"low_risk": False, "max_per_minute": 5}})
        conf = safety.cfg(ctx)
        self.assertFalse(conf["low_risk"])
        self.assertEqual(conf["max_per_minute"], 5)

    def test_cfg_from_plain_dict(self):
        self.assertFalse(safety.cfg({"safety": {"low_risk": False}})["low_risk"])
        self.assertFalse(safety.cfg({"low_risk": False})["low_risk"])
        self.assertTrue(safety.cfg({})["low_risk"])


class GroupWhitelistTests(unittest.TestCase):
    def test_empty_whitelist_allows_all(self):
        self.assertTrue(safety.group_allowed(12345, {"group_whitelist": []}))

    def test_whitelist_str_compare(self):
        conf = {"group_whitelist": [123, "456"]}
        self.assertTrue(safety.group_allowed(123, conf))
        self.assertTrue(safety.group_allowed("123", conf))
        self.assertTrue(safety.group_allowed(456, conf))
        self.assertFalse(safety.group_allowed(789, conf))


# ---------------------------------------------------------------------------
# 发送限频
# ---------------------------------------------------------------------------

class FakeClock:
    def __init__(self, now: float = 1000.0):
        self.now = now
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class WaitSlotTests(unittest.TestCase):
    def setUp(self):
        safety.reset_limiter()
        self.clock = FakeClock()
        self.addCleanup(safety.reset_limiter)
        patcher = mock.patch.object(safety, "time", types.SimpleNamespace(time=self.clock.time))
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(safety, "_sleep", self.clock.sleep)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, coro):
        return asyncio.run(coro)

    def test_free_slot_is_recorded(self):
        conf = safety.normalize({"max_per_minute": 2, "max_per_hour": 10})
        self.assertTrue(self._run(safety.wait_slot(conf)))
        self.assertEqual(len(safety._send_times), 1)
        self.assertEqual(self.clock.sleeps, [])

    def test_minute_limit_short_wait_sleeps_then_succeeds(self):
        conf = safety.normalize({"max_per_minute": 1, "max_per_hour": 600})
        self.assertTrue(self._run(safety.wait_slot(conf)))  # t=1000
        self.clock.now = 1055.0  # 距上次 55 秒，再等 5 秒即可
        self.assertTrue(self._run(safety.wait_slot(conf)))
        self.assertEqual(len(self.clock.sleeps), 1)
        self.assertAlmostEqual(self.clock.sleeps[0], 5.0, places=6)
        self.assertEqual(len(safety._send_times), 2)

    def test_minute_limit_too_long_returns_false(self):
        conf = safety.normalize({"max_per_minute": 1, "max_per_hour": 600})
        self.assertTrue(self._run(safety.wait_slot(conf)))
        self.clock.now = 1001.0  # 要等 59 秒 > 20 秒上限
        self.assertFalse(self._run(safety.wait_slot(conf)))
        self.assertEqual(self.clock.sleeps, [])  # 不会白等 20 秒
        self.assertEqual(len(safety._send_times), 1)

    def test_hour_limit_short_wait_sleeps_then_succeeds(self):
        conf = safety.normalize({"max_per_minute": 600, "max_per_hour": 1})
        self.assertTrue(self._run(safety.wait_slot(conf)))  # t=1000，小时窗口到 4600 释放
        self.clock.now = 4590.0  # 还差 10 秒
        self.assertTrue(self._run(safety.wait_slot(conf)))
        self.assertEqual(len(self.clock.sleeps), 1)
        self.assertAlmostEqual(self.clock.sleeps[0], 10.0, places=6)

    def test_hour_limit_too_long_returns_false(self):
        conf = safety.normalize({"max_per_minute": 600, "max_per_hour": 1})
        self.assertTrue(self._run(safety.wait_slot(conf)))
        self.clock.now = 1002.0
        self.assertFalse(self._run(safety.wait_slot(conf)))
        self.assertEqual(self.clock.sleeps, [])

    def test_both_limits_uses_larger_wait(self):
        conf = safety.normalize({"max_per_minute": 1, "max_per_hour": 1})
        self.assertTrue(self._run(safety.wait_slot(conf)))  # t=1000
        self.clock.now = 1055.0  # 分钟窗口只要等 5 秒，但小时窗口要等 3545 秒
        self.assertFalse(self._run(safety.wait_slot(conf)))
        self.assertEqual(self.clock.sleeps, [])

    def test_old_records_are_pruned(self):
        conf = safety.normalize({"max_per_minute": 600, "max_per_hour": 1})
        self.assertTrue(self._run(safety.wait_slot(conf)))  # t=1000
        self.clock.now = 5000.0  # 超过 1 小时，旧记录应被清掉
        self.assertTrue(self._run(safety.wait_slot(conf)))
        self.assertEqual(len(safety._send_times), 1)

    def test_reset_limiter(self):
        conf = safety.normalize({"max_per_minute": 1, "max_per_hour": 600})
        self.assertTrue(self._run(safety.wait_slot(conf)))
        safety.reset_limiter()
        self.assertEqual(len(safety._send_times), 0)
        self.assertTrue(self._run(safety.wait_slot(conf)))

    def test_many_sends_respect_minute_quota(self):
        conf = safety.normalize({"max_per_minute": 3, "max_per_hour": 600})
        results = []
        for _ in range(3):
            results.append(self._run(safety.wait_slot(conf)))
        self.assertEqual(results, [True, True, True])
        # 第 4 条：最早记录 +60 秒后才释放，要等 60 秒 → 直接 False
        self.assertFalse(self._run(safety.wait_slot(conf)))


# ---------------------------------------------------------------------------
# 内容过滤
# ---------------------------------------------------------------------------

class SanitizeTests(unittest.TestCase):
    def _conf(self, **kwargs):
        raw = {"block_links_group": True, "banned_words": ["加群", "VIP"]}
        raw.update(kwargs)
        return raw

    def test_group_hides_links_private_keeps(self):
        text = "看这个 https://example.com/a?b=1 好不好"
        group = safety.sanitize_message(text, is_group=True, cfg=self._conf())
        self.assertNotIn("https://", group)
        self.assertIn("（链接已隐藏）", group)
        private = safety.sanitize_message(text, is_group=False, cfg=self._conf())
        self.assertEqual(private, text)

    def test_block_links_can_be_disabled(self):
        text = "看 https://example.com"
        result = safety.sanitize_message(
            text, is_group=True, cfg=self._conf(block_links_group=False)
        )
        self.assertEqual(result, text)

    def test_banned_words_case_insensitive(self):
        result = safety.sanitize_message("快来加群，vip 有优惠", is_group=False, cfg=self._conf())
        self.assertEqual(result, "快来***，*** 有优惠")

    def test_no_change_returns_same_object(self):
        text = "普通消息"
        self.assertIs(safety.sanitize_message(text, is_group=True, cfg=self._conf()), text)
        segments = [{"type": "text", "data": {"text": "普通消息"}}]
        self.assertIs(
            safety.sanitize_message(segments, is_group=True, cfg=self._conf()), segments
        )

    def test_segment_list_only_touches_text_and_keeps_original(self):
        segments = [
            {"type": "text", "data": {"text": "加群 https://x.com"}},
            {"type": "image", "data": {"file": "abc.png"}},
        ]
        original = copy.deepcopy(segments)
        result = safety.sanitize_message(segments, is_group=True, cfg=self._conf())
        self.assertEqual(segments, original)  # 原 list / dict 不被改
        self.assertEqual(result[1], {"type": "image", "data": {"file": "abc.png"}})
        self.assertEqual(result[0]["data"]["text"], "*** （链接已隐藏）")
        self.assertIsNot(result[0], segments[0])

    def test_private_segments_keep_links(self):
        segments = [{"type": "text", "data": {"text": "https://x.com 加群"}}]
        result = safety.sanitize_message(segments, is_group=False, cfg=self._conf())
        self.assertEqual(result[0]["data"]["text"], "https://x.com ***")

    def test_unsupported_message_type_passthrough(self):
        self.assertIsNone(safety.sanitize_message(None, is_group=True, cfg=self._conf()))
        payload = {"a": 1}
        self.assertIs(safety.sanitize_message(payload, is_group=True, cfg=self._conf()), payload)

    def test_reply_delay_range(self):
        conf = safety.normalize({"reply_delay_min": 1.0, "reply_delay_max": 3.0})
        for _ in range(20):
            self.assertGreaterEqual(safety.reply_delay(conf), 1.0)
            self.assertLessEqual(safety.reply_delay(conf), 3.0)
        self.assertEqual(safety.reply_delay(safety.normalize({"reply_delay_min": 0, "reply_delay_max": 0})), 0.0)


# ---------------------------------------------------------------------------
# Context 发送集成
# ---------------------------------------------------------------------------

class FakeLogger:
    def __init__(self):
        self.infos = []
        self.warnings = []

    def info(self, *args, **kwargs):
        self.infos.append(args)

    def warning(self, *args, **kwargs):
        self.warnings.append(args)

    def error(self, *args, **kwargs):
        pass

    def exception(self, *args, **kwargs):
        pass


class FakeApi:
    def __init__(self):
        self.calls = []

    async def call(self, action, **kwargs):
        self.calls.append({"action": action, "kwargs": kwargs})
        return {"message_id": len(self.calls)}


class FakeConfig:
    def __init__(self, data: dict):
        self.data = data


class ContextSendTests(unittest.TestCase):
    def setUp(self):
        safety.reset_limiter()
        self.addCleanup(safety.reset_limiter)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _ctx(self, safety_cfg: dict) -> Context:
        ctx = Context(FakeConfig({"safety": safety_cfg}), FakeLogger(), self._tmp.name, self._tmp.name)
        ctx.api = FakeApi()
        return ctx

    @staticmethod
    def _async_mock(return_value=None):
        async def fake(*args, **kwargs):
            fake.calls.append((args, kwargs))
            return return_value

        fake.calls = []
        return fake

    def test_low_risk_off_keeps_old_behavior(self):
        ctx = self._ctx({"low_risk": False})
        message = "看 https://x.com 加群"
        wait = self._async_mock(True)
        sleep = self._async_mock()
        with mock.patch.object(safety, "wait_slot", wait):
            with mock.patch("context.asyncio.sleep", sleep):
                result = asyncio.run(ctx.send_group(123, message))
        self.assertEqual(result, {"message_id": 1})
        self.assertEqual(ctx.api.calls[0]["kwargs"]["message"], message)  # 不延迟、不过滤
        self.assertEqual(wait.calls, [])
        self.assertEqual(sleep.calls, [])

    def test_group_message_sanitized_in_low_risk(self):
        ctx = self._ctx(
            {"low_risk": True, "reply_delay_min": 0, "reply_delay_max": 0, "banned_words": ["加群"]}
        )
        with mock.patch.object(safety, "wait_slot", self._async_mock(True)):
            asyncio.run(ctx.send_group(123, "看 https://x.com 加群"))
        sent = ctx.api.calls[0]["kwargs"]["message"]
        self.assertNotIn("https://", sent)
        self.assertNotIn("加群", sent)
        self.assertIn("（链接已隐藏）", sent)

    def test_private_message_keeps_links(self):
        ctx = self._ctx(
            {"low_risk": True, "reply_delay_min": 0, "reply_delay_max": 0, "banned_words": ["加群"]}
        )
        with mock.patch.object(safety, "wait_slot", self._async_mock(True)):
            asyncio.run(ctx.send_private(456, "https://x.com 加群"))
        sent = ctx.api.calls[0]["kwargs"]["message"]
        self.assertIn("https://x.com", sent)
        self.assertIn("***", sent)

    def test_random_delay_within_range(self):
        ctx = self._ctx({"low_risk": True, "reply_delay_min": 1.0, "reply_delay_max": 3.0})
        sleep = self._async_mock()
        with mock.patch.object(safety, "wait_slot", self._async_mock(True)):
            with mock.patch("context.asyncio.sleep", sleep):
                asyncio.run(ctx.send_private(456, "hi"))
        self.assertEqual(len(sleep.calls), 1)
        delay = sleep.calls[0][0][0]
        self.assertGreaterEqual(delay, 1.0)
        self.assertLessEqual(delay, 3.0)

    def test_group_whitelist_blocks_unknown_group(self):
        ctx = self._ctx({"low_risk": True, "group_whitelist": ["111"]})
        with mock.patch.object(safety, "wait_slot", self._async_mock(True)):
            result = asyncio.run(ctx.send_group(222, "hi"))
        self.assertIsNone(result)
        self.assertEqual(ctx.api.calls, [])
        self.assertTrue(ctx.log.infos)  # info 日志说明跳过

    def test_group_whitelist_allows_listed_group(self):
        ctx = self._ctx({"low_risk": True, "group_whitelist": ["111"], "reply_delay_min": 0, "reply_delay_max": 0})
        with mock.patch.object(safety, "wait_slot", self._async_mock(True)):
            result = asyncio.run(ctx.send_group(111, "hi"))
        self.assertEqual(result, {"message_id": 1})
        self.assertEqual(len(ctx.api.calls), 1)

    def test_whitelist_does_not_block_private(self):
        ctx = self._ctx({"low_risk": True, "group_whitelist": ["111"], "reply_delay_min": 0, "reply_delay_max": 0})
        with mock.patch.object(safety, "wait_slot", self._async_mock(True)):
            result = asyncio.run(ctx.send_private(999, "hi"))
        self.assertEqual(result, {"message_id": 1})

    def test_wait_slot_false_drops_message(self):
        ctx = self._ctx({"low_risk": True})
        with mock.patch.object(safety, "wait_slot", self._async_mock(False)):
            result = asyncio.run(ctx.send_group(123, "hi"))
        self.assertIsNone(result)
        self.assertEqual(ctx.api.calls, [])
        self.assertTrue(ctx.log.warnings)  # warning 说明丢弃

    def test_send_group_segments_are_sanitized(self):
        ctx = self._ctx(
            {"low_risk": True, "reply_delay_min": 0, "reply_delay_max": 0, "banned_words": ["加群"]}
        )
        segments = [
            text_seg("https://x.com 加群"),
            {"type": "image", "data": {"file": "a.png"}},
        ]
        original = copy.deepcopy(segments)
        with mock.patch.object(safety, "wait_slot", self._async_mock(True)):
            asyncio.run(ctx.send_group(123, segments))
        sent = ctx.api.calls[0]["kwargs"]["message"]
        self.assertEqual(sent[1], {"type": "image", "data": {"file": "a.png"}})
        self.assertIn("（链接已隐藏）", sent[0]["data"]["text"])
        self.assertEqual(segments, original)

    def test_reply_goes_through_safety(self):
        ctx = self._ctx(
            {"low_risk": True, "reply_delay_min": 0, "reply_delay_max": 0, "banned_words": ["加群"]}
        )
        event = {"message_type": "group", "group_id": 111, "user_id": 222, "message_id": 1}
        with mock.patch.object(safety, "wait_slot", self._async_mock(True)):
            asyncio.run(ctx.reply(event, "加群 https://x.com"))
        call = ctx.api.calls[0]
        self.assertEqual(call["action"], "send_group_msg")
        segments = call["kwargs"]["message"]
        self.assertEqual(segments[0]["type"], "text")
        text = "".join(seg["data"]["text"] for seg in segments if seg["type"] == "text")
        self.assertIn("***", text)
        self.assertIn("（链接已隐藏）", text)

    def test_no_safety_config_defaults_to_low_risk(self):
        # 老配置没有 safety 段时，按默认开启防封模式（会延迟/过滤）。
        ctx = self._ctx({})
        sleep = self._async_mock()
        with mock.patch.object(safety, "wait_slot", self._async_mock(True)):
            with mock.patch("context.asyncio.sleep", sleep):
                asyncio.run(ctx.send_private(456, "https://x.com"))
        self.assertEqual(len(sleep.calls), 1)
        self.assertIn("https://x.com", ctx.api.calls[0]["kwargs"]["message"])  # 私聊链接保留


class ConfigDefaultsTests(unittest.TestCase):
    def test_defaults_have_safety(self):
        self.assertIn("safety", config.DEFAULTS)
        conf = config.DEFAULTS["safety"]
        self.assertTrue(conf["low_risk"])
        for key in (
            "reply_delay_min",
            "reply_delay_max",
            "max_per_minute",
            "max_per_hour",
            "block_links_group",
            "banned_words",
            "group_whitelist",
        ):
            self.assertIn(key, conf)

    def test_example_config_has_safety(self):
        path = BOT_DIR / "config.example.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("safety", data)
        self.assertTrue(data["safety"]["low_risk"])
        self.assertEqual(data["safety"]["max_per_minute"], 20)

    def test_config_load_merges_safety_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text('{"bot": {"name": "x"}}', encoding="utf-8")
            cfg = config.Config(path)
            data = cfg.load()
        self.assertEqual(data["safety"]["low_risk"], True)
        self.assertEqual(data["bot"]["name"], "x")


if __name__ == "__main__":
    unittest.main()
