"""文生图插件：私聊发「画一张…」、群里 @机器人 发「画一张…」，生成图片并发到会话。

- provider: openai 兼容（OpenAI/DALL·E/gpt-image-1/中转站）或 gemini
- 未单独配置 base_url/api_key 时，自动跟随 plugins.ai_chat 的配置
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
        for trigger in cfg.get("triggers") or []:
            trigger = str(trigger)
            if trigger and text.startswith(trigger):
                prompt = text[len(trigger):].lstrip("：: ，,。.").strip()
                return prompt or None
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

    def _generate_sync(self, cfg: dict, ai_cfg: dict, prompt: str) -> list[bytes]:
        return imagegen.generate(
            provider=str(cfg.get("provider") or "openai"),
            base_url=str(cfg.get("base_url") or ai_cfg.get("base_url") or ""),
            api_key=str(cfg.get("api_key") or ai_cfg.get("api_key") or ""),
            model=str(cfg.get("model") or "gpt-image-1"),
            prompt=prompt,
            size=str(cfg.get("size") or "1024x1024"),
            timeout=float(cfg.get("timeout_seconds", 120)),
        )

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
        if not str(cfg.get("api_key") or ai_cfg.get("api_key") or "").strip():
            await self.ctx.reply(event, "[画图未配置] 先在控制台「画图」页填 API Key（或复用 AI 的 Key）。")
            return True

        self.ctx.log.info("文生图请求 %s: %s", user_id, prompt[:60])
        try:
            images = await asyncio.to_thread(self._generate_sync, cfg, ai_cfg, prompt)
        except imagegen.ImageGenError as e:
            self.ctx.log.warning("文生图失败: %s", e)
            await self.ctx.reply(event, f"画图失败：{e}")
            return True
        except Exception as e:
            self.ctx.log.exception("文生图异常")
            await self.ctx.reply(event, f"画图异常：{e}")
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
