"""文生图插件：私聊发「画一张…」、群里 @机器人 发「画一张…」，生成图片并发到会话。

- provider: openai 兼容（OpenAI/DALL·E/gpt-image-1/中转站）或 gemini
- 触发词可以是句中的任意位置；prompt 取触发词之后的文字（没有则去掉触发词后的全文）
- 画图配置与主对话解耦：image_gen.auth=chatgpt（画图专用账号）→ image_gen.api_key
  → image_gen.follow_ai=true 时复用 AI 的 API Key（要求 ai_chat.auth 为空）
  → 未配置中文提示；API Key 模式的 base_url 空时用 OpenAI 官方，绝不继承 AI 的 base_url
- push_groups: 生成后额外推送到这些群
"""
from __future__ import annotations

import asyncio
import base64
import io
import time

import imagegen
from dispatcher import PluginBase
from utils import at_seg, mentioned, strip_at, text_of, text_seg

# 内置触发词兜底：用户常见的「画张/来一张/使用画图功能」等，不依赖配置
BUILTIN_TRIGGERS = (
    "使用画图功能",
    "用画图功能",
    "生成一张图片",
    "生成一张图",
    "生成一张",
    "生成图片",
    "生成图",
    "来一张",
    "来张",
    "画一张",
    "画张",
    "画一个",
    "绘制",
    "画个",
    "画只",
    "画一幅",
    "画一下",
    "帮我画",
    "用画图",
    "使用画图",
)


class Plugin(PluginBase):
    name = "image_gen"
    priority = 280  # 比 ai_chat(300) 先，命中就自己处理

    def __init__(self, ctx):
        super().__init__(ctx)
        self.images_dir = self.ctx.data_dir / "images"
        self.images_dir.mkdir(parents=True, exist_ok=True)
        self._ratio = float((self.ctx.cfg.get("plugins", {}).get("image_gen") or {}).get("cooldown_seconds", 0) or 0)

    # ---------------- 触发判断 ----------------
    def _prompt_of(self, event, cfg: dict) -> str | None:
        message_type = event.get("message_type")
        if message_type not in ("private", "group"):
            return None
        message = event.get("message")

        if message_type == "group":
            if cfg.get("require_at", True) and not mentioned(message, self.ctx.self_id):
                return None
            text = text_of(strip_at(message, self.ctx.self_id))
        else:
            text = text_of(message)

        text = (text or "").strip()
        if not text:
            return None
        triggers: list[str] = []
        for item in list(cfg.get("triggers") or []) + list(BUILTIN_TRIGGERS):
            trigger = str(item or "").strip()
            if trigger and trigger not in triggers:
                triggers.append(trigger)
        for trigger in triggers:
            index = text.find(trigger)
            if index < 0:
                continue
            after = text[index + len(trigger):].lstrip("：: ，,。.！!？?~～").strip()
            if after:
                return after
            # 触发词后面没内容：退回去掉触发词后的全文（去空白）
            rest = (text[:index] + text[index + len(trigger):]).strip()
            rest = rest.strip("：: ，,。.！!？?~～").strip()
            if rest:
                return rest
            if trigger in BUILTIN_TRIGGERS:
                # 纯触发词（如「使用画图功能」）：用默认风景插画也要出图
                return "一张风景插画"
            return None
        return None

    # ---------------- 生成并发送 ----------------
    @staticmethod
    def _to_image_segment(data: bytes) -> dict:
        """尽量压成 JPEG + base64，减小 QQ 上传体积。"""
        payload = data
        try:
            from PIL import Image

            im = Image.open(io.BytesIO(data))
            im = im.convert("RGB")
            if max(im.size) > 1600:
                im.thumbnail((1600, 1600))
            out = io.BytesIO()
            im.save(out, format="JPEG", quality=88)
            payload = out.getvalue()
        except Exception:
            pass
        return {"type": "image", "data": {"file": "base64://" + base64.b64encode(payload).decode("ascii")}}

    def _generate_sync(
        self, cfg: dict, prompt: str, api_key: str, base_url: str, provider: str, model: str
    ) -> list[bytes]:
        return imagegen.generate(
            provider=provider,
            base_url=base_url,
            api_key=api_key,
            model=model,
            prompt=prompt,
            size=str(cfg.get("size") or "1024x1024"),
            timeout=float(cfg.get("timeout_seconds", 120)),
        )

    def _generate_account_sync(
        self, auth: str, model: str, cfg: dict, prompt: str
    ) -> list[bytes]:
        """账号额度画图（目前只有 ChatGPT）：忽略 provider/base_url。"""
        return imagegen.generate_with_account(
            auth,
            prompt,
            size=str(cfg.get("size") or "1024x1024"),
            timeout=float(cfg.get("timeout_seconds", 120)),
            model=model,
        )

    @staticmethod
    def _mode(cfg: dict, ai_cfg: dict) -> tuple:
        """画图分发模式。

        - ("account", auth)
        - ("api", api_key, base_url, provider, model)
        - ("none", 中文错误)
        """
        decision = imagegen.resolve_image_mode(ai_cfg, cfg)
        mode = decision.get("mode")
        if mode == "account":
            return ("account", str(decision.get("auth") or "chatgpt"))
        if mode == "api":
            return (
                "api",
                str(decision.get("api_key") or ""),
                str(decision.get("base_url") or imagegen.DEFAULT_IMAGE_BASE_URL),
                str(decision.get("provider") or "openai"),
                str(decision.get("model") or imagegen.DEFAULT_IMAGE_MODEL),
            )
        return ("none", str(decision.get("error") or imagegen.ERROR_IMAGE_CONFIG))

    # 兼容旧名字
    _resolve_mode = _mode

    async def _send_images(self, event, caption: str, segments: list[dict], cfg: dict):
        message = [text_seg(caption)] if caption else []
        message += segments
        if event.get("message_type") == "group":
            await self.ctx.send_group(event.get("group_id"), message)
        else:
            await self.ctx.send_private(event.get("user_id"), message)

        # 额外推送到指定群（可选）
        for gid in cfg.get("push_groups") or []:
            try:
                if str(gid) == str(event.get("group_id")):
                    continue  # 触发群已经发过了
                await self.ctx.send_group(int(gid), list(message))
                self.ctx.log.info("图片已推送到群 %s", gid)
            except Exception as e:
                self.ctx.log.warning("推送到群 %s 失败: %s", gid, e)

    async def on_event(self, event) -> bool:
        cfg = self.ctx.plugin_cfg("image_gen")
        if not cfg.get("enabled"):
            return False
        if event.get("post_type") != "message":
            return False

        prompt = self._prompt_of(event, cfg)
        if prompt is None:
            return False

        user_id = event.get("user_id")
        ai_cfg = self.ctx.plugin_cfg("ai_chat")
        mode = self._mode(cfg, ai_cfg)
        if mode[0] == "none":
            await self.ctx.reply(event, str(mode[1]))
            return True

        self.ctx.log.info("文生图请求 %s mode=%s: %s", user_id, mode[0], prompt[:60])
        try:
            if mode[0] == "account":
                account_model = imagegen.account_image_model(cfg)
                images = await asyncio.to_thread(
                    self._generate_account_sync, mode[1], account_model, cfg, prompt
                )
            else:
                _kind, api_key, base_url, provider, model = mode
                images = await asyncio.to_thread(
                    self._generate_sync, cfg, prompt, api_key, base_url, provider, model
                )
        except imagegen.ImageGenError as e:
            self.ctx.log.warning("文生图失败: %s", e)
            await self.ctx.reply(event, f"画图失败：{e}")
            return True
        except Exception as e:
            self.ctx.log.exception("文生图异常")
            await self.ctx.reply(event, "画图出了点问题，稍后再试。")
            return True

        if not images:
            await self.ctx.reply(event, "画图失败：接口没有返回图片。")
            return True

        # 落盘（方便排查 / 管理台以后展示）
        saved = []
        for idx, data in enumerate(images[:2]):
            try:
                path = self.images_dir / f"{int(time.time())}_{idx}.img"
                path.write_bytes(data)
                saved.append(str(path))
            except Exception:
                pass

        segments = [self._to_image_segment(data) for data in images[:2]]
        caption = str(cfg.get("caption") or "")
        if event.get("message_type") == "group" and cfg.get("at_sender", True):
            segments = [at_seg(user_id), text_seg(" ")] + segments
        try:
            await self._send_images(event, caption, segments, cfg)
        except Exception as e:
            self.ctx.log.warning("发送生成的图片失败: %s", e)
            await self.ctx.reply(event, f"图片生成好了但发送失败：{e}")
            return True
        self.ctx.log.info("文生图完成 %d 张，落盘: %s", len(segments), saved[:1])
        return True
