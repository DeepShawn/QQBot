"""账号模式聊天分发 account_chat 的离线测试。"""
from __future__ import annotations

import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent.parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import account_chat  # noqa: E402
import account_core  # noqa: E402
import providers  # noqa: E402


class AccountChatTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_accounts_file = account_core.ACCOUNTS_FILE
        account_core.ACCOUNTS_FILE = Path(self._tmp.name) / "accounts.json"
        account_core._module_cache.clear()
        self.addCleanup(self._restore)

    def _restore(self):
        account_core._module_cache.clear()
        account_core.ACCOUNTS_FILE = self._old_accounts_file
        module = sys.modules.get("accounts_claude")
        if module is not None and getattr(module, "__stub__", False):
            sys.modules.pop("accounts_claude", None)
        if "accounts_gemini" in sys.modules and sys.modules["accounts_gemini"] is None:
            sys.modules.pop("accounts_gemini", None)

    def _install_stub(self, chat_fn):
        module = types.ModuleType("accounts_claude")
        module.__stub__ = True
        module.ID = "claude"
        module.NAME = "Claude"
        module.MODELS = ["model-a", "model-b"]
        module.DEFAULT_MODEL = "model-a"
        module.begin = lambda attempt: {}
        module.refresh = lambda record: None
        module.chat = chat_fn
        module.label = lambda record: None
        sys.modules["accounts_claude"] = module
        return module

    def _signed_in(self):
        account_core._save_record(
            "claude",
            {
                "access": "access-1",
                "refresh": "refresh-1",
                "expires_at": int(time.time()) + 3600,
                "account": "a@b.com",
            },
        )

    def test_dispatch_uses_default_model(self):
        captured: dict = {}

        def chat_fn(record, model, messages, temperature, max_tokens, timeout):
            captured.update(
                record=record, model=model, messages=messages,
                temperature=temperature, max_tokens=max_tokens, timeout=timeout,
            )
            return "账号回复"

        self._install_stub(chat_fn)
        self._signed_in()
        text = account_chat.chat(
            "claude", "", [{"role": "user", "content": "在吗"}], 0.7, 700, 30
        )
        self.assertEqual(text, "账号回复")
        self.assertEqual(captured["model"], "model-a")
        self.assertEqual(captured["record"]["access"], "access-1")
        self.assertEqual(captured["max_tokens"], 700)

    def test_dispatch_keeps_explicit_model(self):
        captured: dict = {}

        def chat_fn(record, model, *args):
            captured["model"] = model
            return "ok"

        self._install_stub(chat_fn)
        self._signed_in()
        account_chat.chat("claude", "model-b", [{"role": "user", "content": "hi"}], 0.7, 100, 10)
        self.assertEqual(captured["model"], "model-b")

    def test_not_signed_in(self):
        self._install_stub(lambda *a, **k: "never")
        with self.assertRaises(providers.ProviderError) as ctx:
            account_chat.chat("claude", "", [{"role": "user", "content": "hi"}], 0.7, 100, 10)
        self.assertEqual(str(ctx.exception), account_core.ERRORS["not-signed-in"])

    def test_account_error_becomes_provider_error(self):
        def chat_fn(*args, **kwargs):
            raise account_core.AccountError("expired")

        self._install_stub(chat_fn)
        self._signed_in()
        with self.assertRaises(providers.ProviderError) as ctx:
            account_chat.chat("claude", "", [{"role": "user", "content": "hi"}], 0.7, 100, 10)
        self.assertEqual(str(ctx.exception), account_core.ERRORS["expired"])

    def test_unexpected_error_is_generic(self):
        def chat_fn(*args, **kwargs):
            raise RuntimeError("secret-token-abcdef123456")

        self._install_stub(chat_fn)
        self._signed_in()
        with self.assertRaises(providers.ProviderError) as ctx:
            account_chat.chat("claude", "", [{"role": "user", "content": "hi"}], 0.7, 100, 10)
        text = str(ctx.exception)
        self.assertNotIn("secret-token", text)
        self.assertIn("不可用", text)

    def test_unsupported_provider(self):
        with self.assertRaises(providers.ProviderError):
            account_chat.chat("nope", "", [{"role": "user", "content": "hi"}], 0.7, 100, 10)

    def test_missing_module(self):
        sys.modules["accounts_gemini"] = None
        account_core._module_cache.pop("gemini", None)
        with self.assertRaises(providers.ProviderError) as ctx:
            account_chat.chat("gemini", "", [{"role": "user", "content": "hi"}], 0.7, 100, 10)
        self.assertEqual(str(ctx.exception), account_core.ERRORS["unsupported"])


class ProvidersAuthRoutingTests(unittest.TestCase):
    def setUp(self):
        self._old_account_chat = sys.modules.pop("account_chat", None)
        self.addCleanup(self._restore)

    def _restore(self):
        if self._old_account_chat is not None:
            sys.modules["account_chat"] = self._old_account_chat
        else:
            sys.modules.pop("account_chat", None)

    def test_auth_routes_to_account_chat(self):
        captured: dict = {}
        stub = types.ModuleType("account_chat")

        def fake_chat(provider, model, messages, temperature, max_tokens, timeout):
            captured.update(
                provider=provider, model=model, messages=messages,
                temperature=temperature, max_tokens=max_tokens, timeout=timeout,
            )
            return "账号模式回复"

        stub.chat = fake_chat
        sys.modules["account_chat"] = stub
        text = providers.chat(
            provider="openai",
            base_url="https://api.example.com/v1",
            api_key="should-be-ignored",
            model="",
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.5,
            max_tokens=321,
            timeout=12,
            auth="claude",
        )
        self.assertEqual(text, "账号模式回复")
        self.assertEqual(captured["provider"], "claude")
        self.assertEqual(captured["model"], "")
        self.assertEqual(captured["max_tokens"], 321)
        self.assertEqual(captured["timeout"], 12.0)

    def test_auth_ignores_provider_validation(self):
        stub = types.ModuleType("account_chat")
        stub.chat = lambda *a, **k: "ok"
        sys.modules["account_chat"] = stub
        # provider 写错也没关系，账号模式只看 auth
        self.assertEqual(
            providers.chat(
                provider="whatever",
                base_url="",
                api_key="",
                model="",
                messages=[{"role": "user", "content": "hi"}],
                auth="chatgpt",
            ),
            "ok",
        )

    def test_auth_validates_messages(self):
        stub = types.ModuleType("account_chat")
        stub.chat = lambda *a, **k: "ok"
        sys.modules["account_chat"] = stub
        with self.assertRaises(providers.ProviderError):
            providers.chat(
                provider="openai",
                base_url="",
                api_key="",
                model="",
                messages=[{"role": "tool", "content": "x"}],
                auth="claude",
            )


if __name__ == "__main__":
    unittest.main()
