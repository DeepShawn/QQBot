#!/usr/bin/env python3
"""把本地文件发到 QQ 群 / 私聊（走 NapCat 的 OneBot 11 接口）。

用法：
  python3 tools/send_file.py 文件路径 --group 123456789
  python3 tools/send_file.py 文件路径 --private 987654321
  python3 tools/send_file.py 文件路径 --group 123456789 --name 改名.py

注意：文件会真的发到 QQ 里，确认目标群/号再发。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import websockets

WS_URL = os.environ.get("QBOT_WS_URL", "ws://127.0.0.1:3001?access_token=change-me-token")


async def call(ws, action: str, **params):
    echo = action
    await ws.send(json.dumps({"action": action, "params": params, "echo": echo}, ensure_ascii=False))
    while True:
        data = json.loads(await asyncio.wait_for(ws.recv(), timeout=120))
        if data.get("echo") == echo:
            return data


async def main() -> int:
    ap = argparse.ArgumentParser(description="发送本地文件到 QQ")
    ap.add_argument("file", help="本地文件路径")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--group", type=int, help="群号")
    g.add_argument("--private", type=int, help="私聊 QQ 号")
    ap.add_argument("--name", default="", help="发送时的文件名（默认用原文件名）")
    ap.add_argument("--url", default=WS_URL, help="OneBot WS 地址")
    args = ap.parse_args()

    path = Path(args.file).expanduser().resolve()
    if not path.is_file():
        print(f"❌ 文件不存在：{path}")
        return 1
    name = args.name or path.name

    async with websockets.connect(args.url, max_size=2 ** 24) as ws:
        if args.group:
            resp = await call(ws, "upload_group_file", group_id=args.group, file=str(path), name=name)
        else:
            resp = await call(ws, "upload_private_file", user_id=args.private, file=str(path), name=name)

    ok = resp.get("status") == "ok" and resp.get("retcode", 0) == 0
    if ok:
        target = f"群 {args.group}" if args.group else f"QQ {args.private}"
        print(f"✅ 已发送 {name}（{path.stat().st_size} 字节）→ {target}")
        return 0
    print(f"❌ 发送失败：{json.dumps(resp, ensure_ascii=False)[:400]}")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
