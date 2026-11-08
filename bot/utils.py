"""通用小工具：消息段解析、占位符格式化、冷却器。"""
from __future__ import annotations

import re
import time

_CQ_RE = re.compile(r"\[CQ:[^\]]*\]")


def now() -> float:
    return time.time()


def segments(message) -> list:
    """把 OneBot 的 message 字段统一成 segment 列表。"""
    if isinstance(message, list):
        return message
    if isinstance(message, str):
        # 字符串形式（CQ 码）：只保留纯文本
        return [{"type": "text", "data": {"text": _CQ_RE.sub("", message)}}]
    return []


def text_of(message) -> str:
    """取出消息里的纯文本部分（去掉 @、图片等）。"""
    parts = []
    for seg in segments(message):
        if seg.get("type") == "text":
            parts.append(str(seg.get("data", {}).get("text", "")))
    return "".join(parts).strip()


def at_seg(qq) -> dict:
    return {"type": "at", "data": {"qq": str(qq)}}


def text_seg(text: str) -> dict:
    return {"type": "text", "data": {"text": str(text)}}


def mentioned(message, qq) -> bool:
    """消息里是否 @ 了指定 QQ。"""
    for seg in segments(message):
        if seg.get("type") == "at" and str(seg.get("data", {}).get("qq", "")) == str(qq):
            return True
    return False


def strip_at(message, qq) -> list:
    """去掉 @ 指定 QQ 的 segment。"""
    return [
        seg
        for seg in segments(message)
        if not (seg.get("type") == "at" and str(seg.get("data", {}).get("qq", "")) == str(qq))
    ]


class _SafeDict(dict):
    def __missing__(self, key):  # 未提供的占位符原样保留，不抛异常
        return "{" + key + "}"


def fmt(template: str, **kwargs) -> str:
    """安全格式化：{nickname} 这种占位符，缺的字段原样留着。"""
    try:
        return template.format_map(_SafeDict(**kwargs))
    except Exception:
        return template


class Cooldown:
    """按 key 记录上次触发时间，用于防刷屏 / 频率限制。"""

    def __init__(self, seconds: float = 0):
        self.seconds = float(seconds)
        self._last: dict = {}

    def allow(self, key, seconds: float | None = None) -> bool:
        s = self.seconds if seconds is None else float(seconds)
        if s <= 0:
            return True
        t = time.monotonic()
        if t - self._last.get(key, 0.0) < s:
            return False
        self._last[key] = t
        return True

    def reset(self, key=None):
        if key is None:
            self._last.clear()
        else:
            self._last.pop(key, None)


def group_key(event) -> str:
    return f"group_{event.get('group_id')}"


def user_key(event) -> str:
    return f"user_{event.get('user_id')}"
