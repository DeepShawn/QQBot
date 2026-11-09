#!/usr/bin/env python3
"""QQ 机器人主程序：连接 NapCat 的 OneBot v11 WebSocket，加载插件处理消息。

用法：python3 main.py [-c 配置路径]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from config import Config  # noqa: E402
from context import Context  # noqa: E402
from dispatcher import Dispatcher  # noqa: E402
from onebot import OneBotClient  # noqa: E402
import scheduled  # noqa: E402
import quota_refresh  # noqa: E402

try:  # 账号续期用；导入失败也不影响主流程
    import account_core  # noqa: E402
except Exception:  # pragma: no cover
    account_core = None  # type: ignore[assignment]

try:  # 官方通道可选：导入失败不影响老的 OneBot 通道
    from official import OfficialClient  # noqa: E402
except Exception:  # pragma: no cover - 运行环境缺少依赖时
    OfficialClient = None  # type: ignore[assignment]

from plugins import (  # noqa: E402
    ai_chat,
    commands,
    dev_tools,
    group_admin,
    image_gen,
    keyword,
    python_run,
    scheduled as scheduled_plugin,
)


def setup_logging(level: str, log_file: Path) -> logging.Logger:
    logger = logging.getLogger("qqbot")
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    logger.handlers.clear()
    logger.propagate = False
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%m-%d %H:%M:%S")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    fh = RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    return logger


# 5 小时限额刷新：启动后先等 90s，之后每 60 秒检查一次
QUOTA_REFRESH_FIRST_DELAY = 90.0
QUOTA_REFRESH_INTERVAL = 60.0

# 账号主动续期：启动后先等 60s，之后每 20 分钟检查一次；30 分钟内过期就提前刷新
ACCOUNT_KEEPER_FIRST_DELAY = 60.0
ACCOUNT_KEEPER_INTERVAL = 20 * 60.0
ACCOUNT_KEEPER_REFRESH_WINDOW = 30 * 60.0


async def keeper_tick(now=None, logger=None):
    """检查一遍账号：将要在 30 分钟内过期的就主动 ensure_access 续期。

    返回 [{"provider":..., "ok": bool, "error"?: str}]，异常只记录日志不抛出。
    """
    log = logger or logging.getLogger("qqbot")
    results: list[dict] = []
    if account_core is None:
        return results
    now_ts = float(now if now is not None else time.time())
    try:
        accounts = account_core.list_view()
    except Exception as e:
        log.warning("读取账号列表失败，跳过本轮续期: %s", e)
        return results
    for item in accounts or []:
        if not isinstance(item, dict) or not item.get("signedIn"):
            continue
        provider = str(item.get("id") or "").strip()
        if provider not in account_core.PROVIDER_IDS:
            continue
        try:
            record = account_core.get_record(provider)
        except Exception:
            record = None
        if not isinstance(record, dict):
            continue
        try:
            expires_at = float(record.get("expires_at") or 0)
        except (TypeError, ValueError):
            expires_at = 0.0
        if expires_at and (expires_at - now_ts) >= ACCOUNT_KEEPER_REFRESH_WINDOW:
            continue
        try:
            account_core.ensure_access(provider)
            results.append({"provider": provider, "ok": True})
            log.info("账号主动续期成功 provider=%s", provider)
        except Exception as e:
            message = getattr(e, "message", None) or str(e)
            log.info("账号主动续期失败 provider=%s: %s", provider, message)
            results.append({"provider": provider, "ok": False, "error": message})
    return results


async def account_keeper(logger):
    """后台账号续期循环（main 里每 20 分钟跑一次）。"""
    await asyncio.sleep(ACCOUNT_KEEPER_FIRST_DELAY)
    while True:
        try:
            await keeper_tick(logger=logger)
        except Exception as e:
            logger.warning("账号主动续期异常: %s", e)
        await asyncio.sleep(ACCOUNT_KEEPER_INTERVAL)


def build_client(config, logger, on_event, data_dir):
    """按 config 选择连接通道 -> (client, "official"|"onebot")。"""
    official_cfg = config.get("official", default={}) or {}
    app_id = str(official_cfg.get("app_id") or "").strip()
    app_secret = str(official_cfg.get("app_secret") or "").strip()
    if (
        OfficialClient is not None
        and official_cfg.get("enabled")
        and app_id
        and app_secret
    ):
        api = OfficialClient(
            app_id=app_id,
            app_secret=app_secret,
            sandbox=bool(official_cfg.get("sandbox", False)),
            logger=logger,
            on_event=on_event,
            request_timeout=float(official_cfg.get("request_timeout", 20)),
            data_dir=data_dir,
        )
        api.reconnect_seconds = float(official_cfg.get("reconnect_seconds", 5))
        return api, "official"
    ob = config.get("onebot", default={}) or {}
    api = OneBotClient(
        url=ob.get("ws_url", "ws://127.0.0.1:3001"),
        token=ob.get("access_token", ""),
        logger=logger,
        on_event=on_event,
        request_timeout=float(ob.get("request_timeout", 20)),
    )
    api.reconnect_seconds = float(ob.get("reconnect_seconds", 5))
    return api, "onebot"


async def async_main(config_path: Path) -> int:
    config = Config(config_path)
    if not config_path.exists():
        config.save_default()
        print(f"已生成默认配置：{config_path}")
    config.load()

    log_dir = BASE / "logs"
    log_dir.mkdir(exist_ok=True)
    logger = setup_logging(config.get("bot", "log_level", default="INFO"), log_dir / "bot.log")

    data_dir = Path(os.environ.get("QQBOT_DATA_DIR") or (BASE / "data"))
    ctx = Context(config, logger, data_dir, log_dir)
    ctx.started_at = time.time()

    dispatcher = Dispatcher(ctx)
    for module in (
        commands,
        group_admin,
        keyword,
        dev_tools,
        python_run,
        image_gen,
        ai_chat,
        scheduled_plugin,
    ):
        dispatcher.register(module.Plugin(ctx))

    api, channel = build_client(config, logger, dispatcher.dispatch, data_dir)
    ctx.api = api

    async def config_watcher():
        while True:
            await asyncio.sleep(2)
            try:
                if config.reload_if_changed():
                    logger.info("检测到 config.json 变更，已热加载")
            except Exception as e:
                logger.error("配置热加载失败，继续使用旧配置：%s", e)

    logger.info("================ 机器人启动 ================")
    logger.info("配置：%s", config_path)
    logger.info(
        "通道：%s（%s）",
        api.url,
        "官方 QQ 机器人" if channel == "official" else "OneBot/NapCat",
    )

    async def heartbeat():
        """给管理台（admin/server.py）写运行状态。"""
        state_file = ctx.data_dir / "runtime.json"
        payload = {"pid": os.getpid(), "started_at": ctx.started_at, "updated_at": time.time()}
        while True:
            payload.update(
                {
                    "connected": api.connected.is_set(),
                    "channel": channel,
                    "self_id": ctx.self_id or "",
                    "updated_at": time.time(),
                }
            )
            try:
                state_file.write_text(json.dumps(payload), encoding="utf-8")
            except Exception:
                pass
            await asyncio.sleep(3)

    async def quota_refresh_loop():
        """限额刷新：启动等 90s，之后每 60 秒检查一次是否到 5h01m。"""
        await asyncio.sleep(QUOTA_REFRESH_FIRST_DELAY)
        while True:
            try:
                await quota_refresh.tick(ctx)
            except Exception as e:
                logger.warning("限额刷新 tick 异常: %s", e)
            await asyncio.sleep(QUOTA_REFRESH_INTERVAL)

    async def scheduled_loop():
        """定时发送：每 30 秒检查一次是否到点（状态文件防重复）。"""
        while True:
            try:
                await scheduled.tick(ctx)
            except Exception as e:
                logger.warning("定时发送 tick 异常: %s", e)
            await asyncio.sleep(30)

    tasks = [
        asyncio.create_task(api.run(), name="onebot"),
        asyncio.create_task(config_watcher(), name="config"),
        asyncio.create_task(heartbeat(), name="heartbeat"),
        asyncio.create_task(scheduled_loop(), name="scheduled"),
        asyncio.create_task(account_keeper(logger), name="account-keeper"),
        asyncio.create_task(quota_refresh_loop(), name="quota-refresh"),
    ]
    stop = asyncio.Event()

    def _stop(*_):
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except (NotImplementedError, RuntimeError):
            pass

    try:
        done = await asyncio.wait(
            [asyncio.create_task(stop.wait(), name="stop")] + tasks, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await api.close()
        logger.info("机器人已退出")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="QQ 机器人")
    parser.add_argument("-c", "--config", default=str(BASE / "config.json"))
    args = parser.parse_args()
    try:
        return asyncio.run(async_main(Path(args.config)))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
