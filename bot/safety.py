"""防封（降低风险）模式：发送随机延迟、全局限频、群白名单、链接/敏感词过滤。

只作用于 QQ 消息发送（Context.send_private / send_group）；管理台、CLI 等
内部 API 调用不经过这里，不受影响。

配置在 config.json 顶层的 ``safety``（见 config.DEFAULTS / config.example.json）::

    "safety": {
      "low_risk": true,
      "reply_delay_min": 1.0,
      "reply_delay_max": 3.0,
      "max_per_minute": 20,
      "max_per_hour": 300,
      "block_links_group": true,
      "banned_words": [],
      "group_whitelist": []
    }
"""
from __future__ import annotations

import asyncio
import collections
import random
import re
import time
import weakref

# 默认值（config.DEFAULTS 里有一份同样的，这里用于兜底/夹取）
DEFAULTS = {
    "low_risk": True,
    "reply_delay_min": 1.0,
    "reply_delay_max": 3.0,
    "max_per_minute": 20,
    "max_per_hour": 300,
    "block_links_group": True,
    "banned_words": [],
    "group_whitelist": [],
}

DELAY_MIN_SECONDS = 0.0
DELAY_MAX_SECONDS = 30.0
PER_MINUTE_MIN = 1
PER_MINUTE_MAX = 600
PER_HOUR_MIN = 1
PER_HOUR_MAX = 10000
# 单次调用最多等这么久；等不到名额就返回 False，由调用方丢弃这条消息。
MAX_WAIT_SECONDS = 20.0
MINUTE_WINDOW = 60.0
HOUR_WINDOW = 3600.0

HIDDEN_LINK_TEXT = "（链接已隐藏）"
_LINK_RE = re.compile(r"https?://\S+", re.IGNORECASE)

# 单独存一份，测试里可以替换（避免真 sleep）。
_sleep = asyncio.sleep

# 最近一小时内的发送时间（按时间升序）。分钟窗口从这里面数。
_send_times: "collections.deque[float]" = collections.deque()
# asyncio.Lock 在 Python 3.10+ 是延迟绑定事件循环的；测试/多轮 asyncio.run 时
# 同一把锁跨 loop 复用会报错，所以按当前 loop 懒创建（WeakKeyDictionary）。
_loop_locks: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


# ---------------------------------------------------------------- 配置

def _as_bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off"):
            return False
        return default
    if value is None:
        return default
    return bool(value)


def _as_float(value, default: float, lo: float, hi: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number or number in (float("inf"), float("-inf")):  # NaN/Inf
        return default
    return max(lo, min(hi, number))


def _as_int(value, default: int, lo: int, hi: int) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default
    return max(lo, min(hi, number))


def _str_list(value) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    items = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in items:
            items.append(text)
    return items


def normalize(raw) -> dict:
    """把用户配置夹到安全区间，返回字段齐全的 safety 配置。"""
    raw = raw if isinstance(raw, dict) else {}
    delay_min = _as_float(
        raw.get("reply_delay_min"), DEFAULTS["reply_delay_min"], DELAY_MIN_SECONDS, DELAY_MAX_SECONDS
    )
    delay_max = _as_float(
        raw.get("reply_delay_max"), DEFAULTS["reply_delay_max"], DELAY_MIN_SECONDS, DELAY_MAX_SECONDS
    )
    if delay_min > delay_max:
        delay_min, delay_max = delay_max, delay_min
    return {
        "low_risk": _as_bool(raw.get("low_risk"), DEFAULTS["low_risk"]),
        "reply_delay_min": delay_min,
        "reply_delay_max": delay_max,
        "max_per_minute": _as_int(
            raw.get("max_per_minute"), DEFAULTS["max_per_minute"], PER_MINUTE_MIN, PER_MINUTE_MAX
        ),
        "max_per_hour": _as_int(
            raw.get("max_per_hour"), DEFAULTS["max_per_hour"], PER_HOUR_MIN, PER_HOUR_MAX
        ),
        "block_links_group": _as_bool(raw.get("block_links_group"), DEFAULTS["block_links_group"]),
        "banned_words": _str_list(raw.get("banned_words")),
        "group_whitelist": _str_list(raw.get("group_whitelist")),
    }


def is_low_risk(raw) -> bool:
    """该配置是否开启了防封模式（缺省按 DEFAULTS 视为开启）。"""
    raw = raw if isinstance(raw, dict) else {}
    return _as_bool(raw.get("low_risk"), DEFAULTS["low_risk"])


def cfg(ctx) -> dict:
    """从 Context（或裸配置 dict）取顶层 safety 配置并夹好参数。"""
    raw = None
    try:
        if isinstance(ctx, dict):
            raw = ctx.get("safety")
            if raw is None and "low_risk" in ctx:
                raw = ctx
        else:
            data = getattr(ctx, "cfg", None)
            if isinstance(data, dict):
                raw = data.get("safety")
            else:
                data = getattr(getattr(ctx, "config", None), "data", None)
                if isinstance(data, dict):
                    raw = data.get("safety")
    except Exception:
        raw = None
    return normalize(raw)


# ---------------------------------------------------------------- 群白名单

def group_allowed(group_id, cfg) -> bool:
    """白名单非空时，只有名单内的群才允许发消息（str 比较）。"""
    conf = cfg or {}
    whitelist = conf.get("group_whitelist") or []
    if not whitelist:
        return True
    return str(group_id) in {str(item) for item in whitelist}


# ---------------------------------------------------------------- 发送限频

def reset_limiter() -> None:
    """清空发送记录（测试 / 热重载后重新开始计数）。"""
    _send_times.clear()


def _lock_for_current_loop() -> asyncio.Lock:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # 没有事件循环时给一把临时锁（正常不会走到）
        return asyncio.Lock()
    lock = _loop_locks.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _loop_locks[loop] = lock
    return lock


def _prune(current: float) -> None:
    while _send_times and current - _send_times[0] >= HOUR_WINDOW:
        _send_times.popleft()


def _wait_needed(current: float, per_minute: int, per_hour: int) -> float:
    """当前已满的话，还要等多少秒才能空出一个名额；0 表示现在就有名额。"""
    waits = []
    if per_minute > 0:
        minute_times = [t for t in _send_times if current - t < MINUTE_WINDOW]
        if len(minute_times) >= per_minute:
            oldest = minute_times[len(minute_times) - per_minute]
            waits.append(oldest + MINUTE_WINDOW - current)
    if per_hour > 0 and len(_send_times) >= per_hour:
        oldest = _send_times[len(_send_times) - per_hour]
        waits.append(oldest + HOUR_WINDOW - current)
    return max(waits) if waits else 0.0


async def wait_slot(cfg, now: float | None = None) -> bool:
    """申请一个发送名额：可用则记录并返回 True。

    超过频率上限时按最早一条记录 sleep 到窗口释放（单次最多等 MAX_WAIT_SECONDS），
    仍然超限则返回 False（调用方丢弃这条消息并 warning，避免无限堆积）。
    """
    conf = normalize(cfg)
    per_minute = conf["max_per_minute"]
    per_hour = conf["max_per_hour"]
    lock = _lock_for_current_loop()
    waited = 0.0
    first_now = now
    while True:
        current = float(first_now) if first_now is not None else time.time()
        first_now = None
        async with lock:
            _prune(current)
            wait = _wait_needed(current, per_minute, per_hour)
            if wait <= 0:
                _send_times.append(current)
                return True
        remaining = MAX_WAIT_SECONDS - waited
        if wait > remaining:
            return False
        await _sleep(wait)
        waited += wait


# ---------------------------------------------------------------- 内容过滤

def _clean_text(text: str, *, is_group: bool, block_links: bool, words: list[str]) -> str:
    result = text
    if is_group and block_links:
        result = _LINK_RE.sub(HIDDEN_LINK_TEXT, result)
    for word in words:
        if word:
            result = re.sub(re.escape(word), "***", result, flags=re.IGNORECASE)
    return result


def sanitize_message(message, *, is_group: bool, cfg):
    """群消息隐藏链接、敏感词替换 ***。只处理 text，文件/图片段原样保留。

    message 可以是 str 或 OneBot 消息段列表；返回新对象（无变化时原样返回），
    不会修改传入的 list / 段 dict。
    """
    conf = cfg or {}
    block_links = bool(conf.get("block_links_group", True))
    words = [str(w) for w in (conf.get("banned_words") or []) if str(w or "").strip()]
    if not message:
        return message

    if isinstance(message, str):
        cleaned = _clean_text(message, is_group=is_group, block_links=block_links, words=words)
        return cleaned if cleaned != message else message

    if isinstance(message, list):
        changed = False
        result = []
        for segment in message:
            if isinstance(segment, dict) and segment.get("type") == "text":
                data = segment.get("data")
                if isinstance(data, dict) and isinstance(data.get("text"), str):
                    cleaned = _clean_text(
                        data["text"], is_group=is_group, block_links=block_links, words=words
                    )
                    if cleaned != data["text"]:
                        new_segment = dict(segment)
                        new_data = dict(data)
                        new_data["text"] = cleaned
                        new_segment["data"] = new_data
                        result.append(new_segment)
                        changed = True
                        continue
            result.append(segment)
        return result if changed else message

    return message


# ---------------------------------------------------------------- 延迟

def reply_delay(conf: dict) -> float:
    """随机回复延迟（秒）；0 表示不延迟。"""
    conf = normalize(conf)
    low = conf["reply_delay_min"]
    high = conf["reply_delay_max"]
    if high <= 0:
        return 0.0
    return random.uniform(low, high)
