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
from plugins import ai_chat, commands, dev_tools, group_admin, image_gen, keyword, python_run  # noqa: E402


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
    for module in (commands, group_admin, keyword, dev_tools, python_run, image_gen, ai_chat):
        dispatcher.register(module.Plugin(ctx))

    ob = config.get("onebot", default={}) or {}
    api = OneBotClient(
        url=ob.get("ws_url", "ws://127.0.0.1:3001"),
        token=ob.get("access_token", ""),
        logger=logger,
        on_event=dispatcher.dispatch,
        request_timeout=float(ob.get("request_timeout", 20)),
    )
    api.reconnect_seconds = float(ob.get("reconnect_seconds", 5))
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
    logger.info("OneBot：%s", api.url)

    async def heartbeat():
        """给管理台（admin/server.py）写运行状态。"""
        state_file = ctx.data_dir / "runtime.json"
        payload = {"pid": os.getpid(), "started_at": ctx.started_at, "updated_at": time.time()}
        while True:
            payload.update(
                {
                    "connected": api.connected.is_set(),
                    "self_id": ctx.self_id or "",
                    "updated_at": time.time(),
                }
            )
            try:
                state_file.write_text(json.dumps(payload), encoding="utf-8")
            except Exception:
                pass
            await asyncio.sleep(3)

    tasks = [
        asyncio.create_task(api.run(), name="onebot"),
        asyncio.create_task(config_watcher(), name="config"),
        asyncio.create_task(heartbeat(), name="heartbeat"),
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
