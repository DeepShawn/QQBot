"""账号模式聊天分发：把 providers.chat 的调用转给 accounts_<provider>.chat。

避免循环导入：providers 只在 auth 非空时延迟 import 本模块；这里也在需要时才
import providers（拿 ProviderError）。
"""
from __future__ import annotations

import account_core


def chat(
    provider: str,
    model: str,
    messages: list,
    temperature: float,
    max_tokens: int,
    timeout: float,
) -> str:
    """用账号额度聊天；AccountError / 网络异常统一转成 providers.ProviderError。"""
    from providers import ProviderError  # 延迟导入，避免 providers <-> account_chat 循环

    pid = str(provider or "").strip().lower()
    if pid not in account_core.PROVIDER_IDS:
        raise ProviderError("不支持的账号类型")
    model_name = str(model or "").strip()

    try:
        module = account_core.load_provider(pid)
        record = account_core.ensure_access(pid)
        if not model_name:
            models = [str(item) for item in (getattr(module, "MODELS", None) or []) if str(item)]
            model_name = str(getattr(module, "DEFAULT_MODEL", "") or "").strip()
            if not model_name and models:
                model_name = models[0]
        chat_fn = getattr(module, "chat", None)
        if not callable(chat_fn):
            raise account_core.AccountError("unsupported")
        return chat_fn(
            record,
            model_name,
            messages,
            temperature,
            max_tokens,
            timeout,
        )
    except account_core.AccountError as e:
        raise ProviderError(e.message) from e
    except ProviderError:
        raise
    except Exception as e:  # 兜底：给用户一句人话，不带堆栈/token
        raise ProviderError("账号接口暂时不可用，稍后再试") from e
