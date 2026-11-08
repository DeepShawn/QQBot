"""定时发送：每隔 interval_minutes（默认 301 分钟 = 5 小时 01 分）发一条问候。

流程：
1. 用主对话模型（plugins.ai_chat 配置）生成「问候语 + 生图提示词」；
2. 用画图配置（plugins.image_gen）调 imagegen.generate_for_ai 生成一张图；
3. 把文字 + 图片合成一条 QQ 消息发到目标（auto / private / group）。

状态保存在 ``<data_dir>/scheduled.json``（{"last_sent": ts}，原子写），避免重复发送。
`tick()` 由 main.py 每 30 秒调用一次；`/报时` 插件用 `run_once()` 立即执行。
只依赖 Python 标准库 + 项目内模块。
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import tempfile
import time
from pathlib import Path

import imagegen
import providers
from utils import text_seg

STATE_FILE_NAME = "scheduled.json"
DEFAULT_INTERVAL_MINUTES = 301
MIN_INTERVAL_MINUTES = 5
MAX_INTERVAL_MINUTES = 1440
DEFAULT_IMAGE_SIZE = "1024x1024"
MODEL_TEMPERATURE = 0.8
MODEL_MAX_TOKENS = 400
MODEL_TIMEOUT = 60.0
IMAGE_TIMEOUT = 120.0
MAX_IMAGE_SIDE = 1600
JPEG_QUALITY = 88

__all__ = [
    "tick",
    "run_once",
    "greeting_label",
    "default_greeting",
    "default_image_prompt",
]


# ---------------------------------------------------------------- 时间/文案


def greeting_label(hour: int) -> str:
    """按本地小时返回问候标签。"""
    try:
        value = int(hour)
    except (TypeError, ValueError):
        value = 12
    value %= 24
    if 5 <= value < 9:
        return "早上好"
    if 9 <= value < 11:
        return "上午好"
    if 11 <= value < 13:
        return "中午好"
    if 13 <= value < 18:
        return "下午好"
    if 18 <= value < 23:
        return "晚上好"
    return "夜深了"


_DEFAULT_GREETINGS = {
    "早上好": "早上好呀，新的一天开始啦，今天也要开开心心～",
    "上午好": "上午好，愿你今天精神满满、诸事顺利～",
    "中午好": "中午好，记得好好吃午饭、休息一下～",
    "下午好": "下午好，喝口水放松一下，继续加油～",
    "晚上好": "晚上好，忙了一天辛苦啦，记得早点休息～",
    "夜深了": "夜深了，早点睡吧，晚安～",
}


def default_greeting(label: str) -> str:
    return _DEFAULT_GREETINGS.get(str(label), "你好呀，愿你今天一切顺利～")


def default_image_prompt(label: str) -> str:
    """按问候标签给画图模型的默认提示词。"""
    label = str(label)
    if label in ("早上好", "上午好"):
        return "一张治愈的清晨风景插画，柔和晨光，温暖色调，横构图"
    if label in ("中午好", "下午好"):
        return "一张治愈的午后风景插画，柔和阳光，温暖色调，横构图"
    if label == "晚上好":
        return "一张治愈的傍晚风景插画，柔和灯光，暖色调，横构图"
    return "一张治愈的夜晚风景插画，柔和月光，安静氛围，横构图"


# ---------------------------------------------------------------- 状态文件


def state_path(ctx) -> Path:
    return Path(ctx.data_dir) / STATE_FILE_NAME


def _load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"last_sent": 0}
    if not isinstance(data, dict):
        return {"last_sent": 0}
    try:
        last_sent = float(data.get("last_sent") or 0)
    except (TypeError, ValueError):
        last_sent = 0.0
    return {"last_sent": max(0.0, last_sent)}


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"last_sent": float(state.get("last_sent") or 0)}, ensure_ascii=False)
    fd, tmp_name = tempfile.mkstemp(prefix=".scheduled-", suffix=".tmp", dir=str(path.parent))
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


# ---------------------------------------------------------------- 目标解析


def _recent_private_from_context(ctx) -> int | None:
    path = Path(ctx.data_dir) / "context.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    best_id = None
    best_ts = -1.0
    for key, value in data.items():
        if not isinstance(key, str) or not key.startswith("private:"):
            continue
        tail = key[len("private:"):].strip()
        if not tail.isdigit():
            continue
        ts = 0.0
        if isinstance(value, dict):
            try:
                ts = float(value.get("updated_at") or 0)
            except (TypeError, ValueError):
                ts = 0.0
        if ts >= best_ts:
            best_ts = ts
            best_id = int(tail)
    return best_id


def _admin_id(ctx) -> int | None:
    try:
        admins = (ctx.cfg.get("bot") or {}).get("admins") or []
    except Exception:
        admins = []
    for item in admins:
        try:
            value = int(item)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def resolve_target(ctx, cfg: dict) -> dict | None:
    """返回 {"type": "private"|"group", "id": int}；没有可用目标返回 None。"""
    target_type = str(cfg.get("target_type") or "auto").strip().lower()
    if target_type in ("private", "group"):
        try:
            target_id = int(cfg.get("target_id") or 0)
        except (TypeError, ValueError):
            target_id = 0
        if target_id <= 0:
            return None
        return {"type": target_type, "id": target_id}
    # auto：最近的私聊 > bot.admins[0]
    recent = _recent_private_from_context(ctx)
    if recent:
        return {"type": "private", "id": recent}
    admin = _admin_id(ctx)
    if admin:
        return {"type": "private", "id": admin}
    return None


# ---------------------------------------------------------------- 模型生成


def _parse_json_object(text) -> dict | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    decoder = json.JSONDecoder()
    index = raw.find("{")
    while index >= 0:
        try:
            value, _end = decoder.raw_decode(raw[index:])
        except Exception:
            value = None
        if isinstance(value, dict):
            return value
        index = raw.find("{", index + 1)
    return None


def _ai_model(ai_cfg: dict) -> str:
    model = str(ai_cfg.get("model") or "").strip()
    auth = str(ai_cfg.get("auth") or "").strip()
    if not model and not auth:
        model = "deepseek-chat"
    return model


def _sync_generate_greeting(ai_cfg: dict, now_ts: float) -> tuple[str, str]:
    """调用主对话模型生成 (greeting, image_prompt)；失败抛 ProviderError。"""
    local = time.localtime(now_ts)
    label = greeting_label(local.tm_hour)
    now_text = time.strftime("%Y-%m-%d %H:%M", local)
    system = (
        f"现在是 {now_text}（{label}）。请写一条自然简短的中文问候（包含合适的问候语，"
        "再加一句生活分享或提醒），并为画图模型写一句具体的生图提示词"
        "（主体、风格、光线、画幅）。只输出严格 JSON："
        '{"greeting":"问候语","image_prompt":"生图提示词"}'
    )
    payload = providers.chat(
        provider=str(ai_cfg.get("provider") or "openai"),
        base_url=str(ai_cfg.get("base_url") or ""),
        api_key=str(ai_cfg.get("api_key") or ""),
        model=_ai_model(ai_cfg),
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": "请生成这条定时问候。"},
        ],
        temperature=MODEL_TEMPERATURE,
        max_tokens=MODEL_MAX_TOKENS,
        timeout=MODEL_TIMEOUT,
        auth=str(ai_cfg.get("auth") or "").strip(),
    )
    text = str(payload or "").strip()
    data = _parse_json_object(text)
    greeting = ""
    image_prompt = ""
    if isinstance(data, dict):
        greeting = str(data.get("greeting") or "").strip()
        image_prompt = str(data.get("image_prompt") or "").strip()
    return greeting, image_prompt


# ---------------------------------------------------------------- 图片


def image_segment(data: bytes) -> dict:
    """图片 -> base64 image 消息段（PIL 可用时压成 JPEG）。"""
    payload = bytes(data or b"")
    try:
        from PIL import Image

        image = Image.open(io.BytesIO(payload))
        image = image.convert("RGB")
        if max(image.size) > MAX_IMAGE_SIDE:
            image.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
        out = io.BytesIO()
        image.save(out, format="JPEG", quality=JPEG_QUALITY)
        payload = out.getvalue()
    except Exception:
        pass
    return {
        "type": "image",
        "data": {"file": "base64://" + base64.b64encode(payload).decode("ascii")},
    }


def _try_generate_image(ai_cfg: dict, image_cfg: dict, prompt: str, size: str) -> bytes | None:
    try:
        decision = imagegen.resolve_image_mode(ai_cfg, image_cfg)
    except Exception:
        return None
    if decision.get("mode") not in ("account", "api"):
        return None
    try:
        images = imagegen.generate_for_ai(
            ai_cfg, image_cfg, prompt, size=size, timeout=IMAGE_TIMEOUT
        )
    except imagegen.ImageGenError:
        return None
    except Exception:
        return None
    if not images:
        return None
    return bytes(images[0])


# ---------------------------------------------------------------- 主流程


async def _run(ctx, now=None, force: bool = False) -> dict:
    cfg = ctx.plugin_cfg("scheduled") or {}
    if cfg.get("enabled") is False:
        return {"sent": False, "reason": "disabled"}
    now_ts = float(now if now is not None else time.time())
    path = state_path(ctx)
    state = _load_state(path)
    interval = _interval_seconds(cfg)
    last_sent = float(state.get("last_sent") or 0)
    if not force and last_sent and now_ts < last_sent + interval:
        return {"sent": False, "reason": "not-due"}

    target = resolve_target(ctx, cfg)
    if target is None:
        try:
            ctx.log.info("定时发送跳过：没有可用的目标（auto 无最近私聊/管理员，或 target_id 未配置）")
        except Exception:
            pass
        return {"sent": False, "reason": "no-target"}

    ai_cfg = ctx.plugin_cfg("ai_chat") or {}
    image_cfg = ctx.plugin_cfg("image_gen") or {}
    label = greeting_label(time.localtime(now_ts).tm_hour)
    greeting = ""
    image_prompt = ""
    try:
        greeting, image_prompt = await asyncio.to_thread(
            _sync_generate_greeting, ai_cfg, now_ts
        )
    except Exception as e:
        try:
            ctx.log.warning("定时问候模型调用失败，用默认文案: %s", e)
        except Exception:
            pass
    if not greeting:
        greeting = default_greeting(label)
    if not image_prompt:
        image_prompt = default_image_prompt(label)

    image_bytes = None
    if cfg.get("use_image") is not False:
        size = str(cfg.get("image_size") or DEFAULT_IMAGE_SIZE).strip() or DEFAULT_IMAGE_SIZE
        image_bytes = await asyncio.to_thread(
            _try_generate_image, ai_cfg, image_cfg, image_prompt, size
        )

    message = [text_seg(greeting)]
    if image_bytes:
        message.append(image_segment(image_bytes))
    sent = False
    try:
        if target["type"] == "group":
            await ctx.send_group(target["id"], message)
        else:
            await ctx.send_private(target["id"], message)
        sent = True
    except Exception as e:
        try:
            ctx.log.warning("定时问候发送失败 target=%s:%s: %s", target["type"], target["id"], e)
        except Exception:
            pass

    if sent:
        _save_state(path, {"last_sent": now_ts})
        try:
            ctx.log.info(
                "定时问候已发送 target=%s:%s image=%d",
                target["type"],
                target["id"],
                1 if image_bytes else 0,
            )
        except Exception:
            pass
    else:
        # 发送失败也记录时间：避免每 30 秒重试刷屏（下个周期再试）
        try:
            _save_state(path, {"last_sent": now_ts})
        except Exception:
            pass
    return {
        "sent": sent,
        "reason": "ok" if sent else "send-failed",
        "target": target,
        "greeting": greeting,
        "image": image_bytes is not None,
    }


async def tick(ctx, now=None, force: bool = False) -> bool:
    """main.py 每 30 秒调用；到点则发一条，返回是否发送成功。"""
    result = await _run(ctx, now=now, force=force)
    return bool(result.get("sent"))


async def run_once(ctx, now=None) -> dict:
    """/报时 等场景：忽略间隔立即执行一次，返回执行结果 dict。"""
    return await _run(ctx, now=now, force=True)
