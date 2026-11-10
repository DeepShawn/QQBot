"""T63 每个预设独立参考图：存取/接口/生效/防穿越（离线）。"""
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
TINY_B64 = base64.b64encode(TINY_PNG).decode("ascii")


def _patch(test, obj, name, value):
    old = getattr(obj, name)
    setattr(obj, name, value)
    test.addCleanup(setattr, obj, name, old)


def _presets():
    return [dict(item) for item in admin_server._default_presets()]


class ServerBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.data_dir = self.root / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.root / "config.json"
        self.config_path.write_text(
            json.dumps(
                {
                    "plugins": {
                        "ai_chat": {
                            "avatar": {
                                "prompt": "顶层形象",
                                "reference_image": "",
                            },
                            "active_preset": "custom",
                            "presets": _presets(),
                        }
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        _patch(self, admin_server, "DATA_DIR", self.data_dir)
        _patch(self, admin_server, "CONFIG_PATH", self.config_path)

    def read_config(self):
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def preset(self, preset_id):
        ai = self.read_config()["plugins"]["ai_chat"]
        return next(
            (item for item in ai["presets"] if item["id"] == preset_id), None
        )

    def avatar_ref(self):
        return self.read_config()["plugins"]["ai_chat"]["avatar"]["reference_image"]


class PresetUploadTests(ServerBase):
    def test_post_preset_saves_file_and_config(self):
        result = admin_server.api_avatar_reference_post(
            {"data_base64": TINY_B64}, preset_id="catgirl"
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["preset"], "catgirl")
        self.assertEqual(result["path"], "data/avatar/catgirl.png")
        path = self.data_dir / "avatar" / "catgirl.png"
        self.assertTrue(path.is_file())
        self.assertTrue(path.read_bytes().startswith(b"\x89PNG"))
        preset = self.preset("catgirl")
        self.assertEqual(preset["reference_image"], "data/avatar/catgirl.png")
        # 其它预设和全局不受影响
        self.assertEqual(self.preset("whale")["reference_image"], "")
        self.assertEqual(self.avatar_ref(), "")

    def test_post_body_preset_and_query_preset(self):
        admin_server.api_avatar_reference_post(
            {"data_base64": TINY_B64, "preset": "whale"}
        )
        self.assertEqual(
            self.preset("whale")["reference_image"], "data/avatar/whale.png"
        )
        self.assertTrue((self.data_dir / "avatar" / "whale.png").is_file())
        admin_server.api_avatar_reference_post(
            {"data_base64": TINY_B64}, preset_id="catgirl"
        )
        self.assertTrue((self.data_dir / "avatar" / "catgirl.png").is_file())

    def test_post_global_still_works(self):
        admin_server.api_avatar_reference_post({"data_base64": TINY_B64})
        self.assertEqual(self.avatar_ref(), "data/avatar/reference.png")
        self.assertTrue(
            (self.data_dir / "avatar" / "reference.png").is_file()
        )
        self.assertEqual(self.preset("catgirl")["reference_image"], "")

    def test_builtin_preset_upload_when_presets_empty(self):
        cfg = self.read_config()
        cfg["plugins"]["ai_chat"]["presets"] = []
        self.config_path.write_text(json.dumps(cfg), encoding="utf-8")
        admin_server.api_avatar_reference_post(
            {"data_base64": TINY_B64}, preset_id="whale"
        )
        preset = self.preset("whale")
        self.assertIsNotNone(preset)
        self.assertEqual(preset["reference_image"], "data/avatar/whale.png")
        self.assertEqual(preset["name"], "大肥鱼")

    def test_get_preset(self):
        admin_server.api_avatar_reference_post(
            {"data_base64": TINY_B64}, preset_id="catgirl"
        )
        data = admin_server.api_avatar_reference_get("catgirl")
        self.assertTrue(data.startswith(b"\x89PNG"))
        self.assertIsNone(admin_server.api_avatar_reference_get("whale"))
        self.assertIsNone(admin_server.api_avatar_reference_get("custom"))

    def test_delete_preset_only_clears_that_preset(self):
        admin_server.api_avatar_reference_post(
            {"data_base64": TINY_B64}, preset_id="catgirl"
        )
        admin_server.api_avatar_reference_post({"data_base64": TINY_B64})
        result = admin_server.api_avatar_reference_delete("catgirl")
        self.assertTrue(result["ok"])
        self.assertFalse((self.data_dir / "avatar" / "catgirl.png").exists())
        self.assertEqual(self.preset("catgirl")["reference_image"], "")
        # 全局保留
        self.assertEqual(self.avatar_ref(), "data/avatar/reference.png")
        self.assertTrue((self.data_dir / "avatar" / "reference.png").is_file())

    def test_invalid_preset_id_rejected(self):
        for bad in ("../x", "Cat Girl", "a/b", "..", "x" * 40):
            with self.subTest(preset=bad):
                with self.assertRaises(ValueError):
                    admin_server.api_avatar_reference_post(
                        {"data_base64": TINY_B64}, preset_id=bad
                    )
                with self.assertRaises(ValueError):
                    admin_server.api_avatar_reference_delete(bad)
                with self.assertRaises(ValueError):
                    admin_server.api_avatar_reference_get(bad)


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


class PresetEndpointTests(ServerBase):
    def test_get_with_preset_query(self):
        admin_server.api_avatar_reference_post(
            {"data_base64": TINY_B64}, preset_id="catgirl"
        )
        handler = _DummyHandler("/api/avatar/reference?preset=catgirl")
        handler.do_GET()
        self.assertEqual(handler.calls[0][0], "send")
        self.assertEqual(handler.calls[0][1], 200)
        self.assertTrue(handler.calls[0][2].startswith(b"\x89PNG"))
        missing = _DummyHandler("/api/avatar/reference?preset=whale")
        missing.do_GET()
        self.assertEqual(missing.calls[0][1], 404)

    def test_post_with_preset_query(self):
        handler = _DummyHandler(
            "/api/avatar/reference?preset=catgirl",
            {"data_base64": TINY_B64},
        )
        handler.do_POST()
        self.assertEqual(handler.calls[0][0], "json")
        self.assertTrue(handler.calls[0][2]["ok"])
        self.assertEqual(handler.calls[0][2]["preset"], "catgirl")
        self.assertTrue((self.data_dir / "avatar" / "catgirl.png").is_file())

    def test_delete_with_preset_query(self):
        admin_server.api_avatar_reference_post(
            {"data_base64": TINY_B64}, preset_id="catgirl"
        )
        handler = _DummyHandler("/api/avatar/reference?preset=catgirl")
        handler.do_DELETE()
        self.assertEqual(handler.calls[0][0], "json")
        self.assertTrue(handler.calls[0][2]["ok"])
        self.assertFalse((self.data_dir / "avatar" / "catgirl.png").exists())

    def test_invalid_preset_returns_400(self):
        handler = _DummyHandler(
            "/api/avatar/reference", {"data_base64": TINY_B64, "preset": "../x"}
        )
        handler.do_POST()
        self.assertEqual(handler.calls[0][0], "error")
        self.assertEqual(handler.calls[0][1], 400)


class SanitizePresetAvatarTests(unittest.TestCase):
    def test_preset_reference_paths_validated(self):
        cfg = {
            "plugins": {
                "ai_chat": {
                    "presets": [
                        {"id": "whale", "reference_image": "data/avatar/whale.png"},
                        {"id": "catgirl", "reference_image": "data/avatar/whale.png"},
                        {"id": "sister", "reference_image": "../../etc/passwd"},
                    ],
                    "avatar": {
                        "prompt": "",
                        "reference_image": "../../etc/passwd",
                    },
                }
            }
        }
        ai = admin_server.sanitize_config(cfg)["plugins"]["ai_chat"]
        by_id = {item["id"]: item for item in ai["presets"]}
        self.assertEqual(by_id["whale"]["reference_image"], "data/avatar/whale.png")
        self.assertEqual(by_id["catgirl"]["reference_image"], "")
        self.assertEqual(by_id["sister"]["reference_image"], "")
        self.assertEqual(ai["avatar"]["reference_image"], "")

    def test_default_presets_get_empty_reference(self):
        cfg = {"plugins": {"ai_chat": {}}}
        ai = admin_server.sanitize_config(cfg)["plugins"]["ai_chat"]
        for item in ai["presets"]:
            self.assertEqual(item["reference_image"], "")


class ReferencePathPriorityTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "data" / "avatar").mkdir(parents=True, exist_ok=True)
        (self.root / "data" / "avatar" / "reference.png").write_bytes(TINY_PNG)
        (self.root / "data" / "avatar" / "catgirl.png").write_bytes(TINY_PNG)

    def cfg(self, active="catgirl", preset_ref="data/avatar/catgirl.png"):
        return {
            "active_preset": active,
            "presets": [
                {
                    "id": "catgirl",
                    "reference_image": preset_ref,
                }
            ],
            "avatar": {"reference_image": "data/avatar/reference.png"},
        }

    def test_preset_priority(self):
        path = imagegen.avatar_reference_path(self.cfg(), base_dir=self.root)
        self.assertEqual(path, self.root / "data" / "avatar" / "catgirl.png")

    def test_missing_preset_file_falls_back_to_global(self):
        path = imagegen.avatar_reference_path(
            self.cfg(preset_ref="data/avatar/missing.png"), base_dir=self.root
        )
        self.assertEqual(path, self.root / "data" / "avatar" / "reference.png")

    def test_empty_preset_ref_falls_back_to_global(self):
        path = imagegen.avatar_reference_path(
            self.cfg(preset_ref=""), base_dir=self.root
        )
        self.assertEqual(path, self.root / "data" / "avatar" / "reference.png")

    def test_custom_and_unknown_use_global(self):
        for active in ("custom", "unknown"):
            with self.subTest(active=active):
                path = imagegen.avatar_reference_path(
                    self.cfg(active=active), base_dir=self.root
                )
                self.assertEqual(path, self.root / "data" / "avatar" / "reference.png")

    def test_explicit_preset_id(self):
        path = imagegen.avatar_reference_path(
            self.cfg(active="custom"), base_dir=self.root, preset_id="catgirl"
        )
        self.assertEqual(path, self.root / "data" / "avatar" / "catgirl.png")

    def test_traversal_reference_rejected(self):
        path = imagegen.avatar_reference_path(
            self.cfg(preset_ref="../../etc/passwd"), base_dir=self.root
        )
        # 预设路径非法 -> 回退全局
        self.assertEqual(path, self.root / "data" / "avatar" / "reference.png")


class AiChatUsesPresetReferenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_ai_chat_wrapper_uses_active_preset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data" / "avatar").mkdir(parents=True)
            (root / "data" / "avatar" / "catgirl.png").write_bytes(TINY_PNG)
            (root / "data" / "avatar" / "reference.png").write_bytes(TINY_PNG)
            _patch(self, ai_chat, "AVATAR_BASE_DIR", root)

            class Ctx:
                def __init__(self):
                    self.data_dir = root
                    self.log = None
                    self._cfg = {
                        "plugins": {
                            "ai_chat": {
                                "active_preset": "catgirl",
                                "presets": [
                                    {
                                        "id": "catgirl",
                                        "reference_image": "data/avatar/catgirl.png",
                                    }
                                ],
                                "avatar": {
                                    "reference_image": "data/avatar/reference.png"
                                },
                            }
                        }
                    }

                def plugin_cfg(self, name):
                    return self._cfg["plugins"].get(name) or {}

            plugin = ai_chat.Plugin(Ctx())
            cfg = plugin.ctx.plugin_cfg("ai_chat")
            path = plugin._avatar_reference_path(cfg)
            self.assertEqual(path, root / "data" / "avatar" / "catgirl.png")


if __name__ == "__main__":
    unittest.main()
