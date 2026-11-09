"""每 5 小时 01 分给已登录的 Claude / ChatGPT 账号发一句「你好」，刷新用量窗口。

- 只走账号额度（providers.chat(auth=账号id)），不往 QQ 发任何消息；
- 状态保存在 ``<data_dir>/quota_refresh.json``（{"last_sent": ts}，原子写）；
- 只处理 plugins.quota_refresh.providers 里受支持且已登录的账号；
- 任一账号成功才更新 last_sent；全部失败下一轮重试，失败日志限频避免刷屏；
- 不打印 token / 账号敏感信息。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path

import account_core
import providers

STATE_FILE_NAME = "quota_refresh.json"
DEFAULT_INTERVAL_MINUTES = 301
MIN_INTERVAL_MINUTES = 60
MAX_INTERVAL_MINUTES = 1440
DEFAULT_PROVIDERS = ("claude", "chatgpt")
TARGET_PROVIDERS = ("claude", "chatgpt")  # 只刷新这两个账号的 5 小时窗口
DEFAULT_MESSAGE = "你好"
MAX_TOKENS = 16
CHAT_TIMEOUT = 45.0
FAILURE_LOG_SECONDS = 600.0
SKIP_LOG_SECONDS = 3600.0

__all__ = ["tick", "DEFAULT_PROVIDERS", "STATE_FILE_NAME"]

_log_lock = threading.Lock()
_last_log: dict[str, float] = {}


def _log_limited(log, key: str, message: str, *, seconds: float, level: str = "warning") -> None:
    now = time.time()
    with _log_lock:
        last = float(_last_log.get(key) or 0.0)
        if now - last < seconds:
            return
        _last_log[key] = now
    try:
        getattr(log, level)(message)
    except Exception:
        pass


def state_path(ctx) -> Path:
    return Path(ctx.data_dir) / STATE_FILE_NAME


def _load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"last_sent": 0.0}
    if not isinstance(data, dict):
        return {"last_sent": 0.0}
    try:
        last_sent = float(data.get("last_sent") or 0)
    except (TypeError, ValueError):
        last_sent = 0.0
    return {"last_sent": max(0.0, last_sent)}


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(
        {"last_sent": float(state.get("last_sent") or 0)}, ensure_ascii=False
    )
    fd, tmp_name = tempfile.mkstemp(
        prefix=".quota-refresh-", suffix=".tmp", dir=str(path.parent)
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _interval_seconds(cfg: dict) -> float:
    try:
        minutes = int(cfg.get("interval_minutes", DEFAULT_INTERVAL_MINUTES))
    except (TypeError, ValueError):
        minutes = DEFAULT_INTERVAL_MINUTES
    minutes = max(MIN_INTERVAL_MINUTES, min(MAX_INTERVAL_MINUTES, minutes))
    return minutes * 60.0


def _providers(cfg: dict) -> list[str]:
    raw = cfg.get("providers")
    if not isinstance(raw, list) or not raw:
        raw = list(DEFAULT_PROVIDERS)
    result: list[str] = []
    for item in raw:
        pid = str(item or "").strip().lower()
        if pid and pid not in result:
            result.append(pid)
    return result


def _chat_sync(pid: str, message: str) -> str:
    return providers.chat(
        provider="openai",
        base_url="",
        api_key="",
        model="",
        messages=[{"role": "user", "content": str(message)}],
        max_tokens=MAX_TOKENS,
        timeout=CHAT_TIMEOUT,
        auth=pid,
    )


async def tick(ctx, now=None, force: bool = False) -> dict:
    """检查一次；到点则给已登录账号发「你好」刷新限额。

    返回 {"skipped": "", "refreshed": [id...], "failed": [{provider,error}...], "updated": bool}
    skipped 为 "disabled" / "not-due" / "no-accounts"（未到间隔/关闭/没有已登录账号）。
    """
    cfg = ctx.plugin_cfg("quota_refresh") or {}
    if cfg.get("enabled") is False:
        return {
            "skipped": "disabled",
            "refreshed": [],
            "failed": [],
            "updated": False,
        }
    now_ts = float(now if now is not None else time.time())
    path = state_path(ctx)
    state = _load_state(path)
    interval = _interval_seconds(cfg)
    last_sent = float(state.get("last_sent") or 0)
    if not force and last_sent and now_ts < last_sent + interval:
        return {
            "skipped": "not-due",
            "refreshed": [],
            "failed": [],
            "updated": False,
        }

    message = str(cfg.get("message") or DEFAULT_MESSAGE).strip() or DEFAULT_MESSAGE
    candidates: list[str] = []
    for pid in _providers(cfg):
        if pid not in TARGET_PROVIDERS:
            _log_limited(
                ctx.log,
                f"target:{pid}",
                f"限额刷新只支持 claude/chatgpt，跳过：{pid}",
                seconds=SKIP_LOG_SECONDS,
                level="info",
            )
            continue
        if pid not in account_core.PROVIDER_IDS:
            _log_limited(
                ctx.log,
                f"unsupported:{pid}",
                f"限额刷新跳过不支持的账号：{pid}",
                seconds=SKIP_LOG_SECONDS,
                level="info",
            )
            continue
        try:
            record = account_core.get_record(pid)
        except Exception:
            record = None
        if not isinstance(record, dict):
            _log_limited(
                ctx.log,
                f"unsigned:{pid}",
                f"限额刷新跳过未登录账号：{pid}",
                seconds=SKIP_LOG_SECONDS,
                level="info",
            )
            continue
        candidates.append(pid)
    if not candidates:
        return {
            "skipped": "no-accounts",
            "refreshed": [],
            "failed": [],
            "updated": False,
        }

    refreshed: list[str] = []
    failed: list[dict] = []
    details: list[dict] = []
    for pid in candidates:
        try:
            account_core.ensure_access(pid)
        except Exception as e:
            message_text = getattr(e, "message", None) or str(e)
            failed.append({"provider": pid, "error": message_text})
            details.append({"provider": pid, "ok": False, "error": message_text})
            _log_limited(
                ctx.log,
                f"access:{pid}",
                f"限额刷新：{pid} 获取登录态失败（{message_text}），跳过",
                seconds=FAILURE_LOG_SECONDS,
            )
            continue
        started = time.monotonic()
        try:
            reply = await asyncio.to_thread(_chat_sync, pid, message)
        except providers.ProviderError as e:
            failed.append({"provider": pid, "error": str(e)})
            details.append({"provider": pid, "ok": False, "error": str(e)})
            _log_limited(
                ctx.log,
                f"chat:{pid}",
                f"限额刷新失败：{pid}（{e}），稍后重试",
                seconds=FAILURE_LOG_SECONDS,
            )
            continue
        except Exception as e:
            failed.append({"provider": pid, "error": str(e)})
            details.append({"provider": pid, "ok": False, "error": str(e)})
            _log_limited(
                ctx.log,
                f"chat:{pid}",
                f"限额刷新异常：{pid}（{e}）",
                seconds=FAILURE_LOG_SECONDS,
            )
            continue
        elapsed = time.monotonic() - started
        refreshed.append(pid)
        details.append(
            {
                "provider": pid,
                "ok": True,
                "elapsed": round(elapsed, 2),
                "reply_len": len(str(reply or "")),
            }
        )
        try:
            ctx.log.info("限额刷新成功 provider=%s 耗时=%.1fs", pid, elapsed)
        except Exception:
            pass

    updated = bool(refreshed)
    if updated:
        try:
            _save_state(path, {"last_sent": now_ts})
        except Exception as e:
            _log_limited(
                ctx.log,
                "state",
                f"限额刷新状态保存失败：{e}",
                seconds=FAILURE_LOG_SECONDS,
            )
    elif candidates:
        _log_limited(
            ctx.log,
            "all-failed",
            "限额刷新本轮全部失败，下一轮会重试",
            seconds=FAILURE_LOG_SECONDS,
        )
    return {
        "skipped": "",
        "refreshed": refreshed,
        "failed": failed,
        "updated": updated,
        "details": details,
    }
