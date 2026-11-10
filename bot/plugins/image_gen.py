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
import providers
from dispatcher import PluginBase
from utils import at_seg, mentioned, strip_at, text_of, text_seg

# 角色化画图说明：绝不承认是 AI/机器人，不出现机械词
CAPTION_AI_SYSTEM = (
    "你是一个有名字、有性格的角色（像群友一样说话），绝不承认自己是 AI、机器人、模型或程序。"
    "请用你的角色口吻，针对刚完成的画写 1-2 句中文说明：结合用户需求，"
    "描述画面主体/光线/氛围，口语化、有细节、可以带一点小得意；"
    "禁止出现「画好了」「AI」「机器人」「模型」「生成」这些词，只输出这句话。"
)
CAPTION_BANNED = ("画好了", "AI", "机器人", "模型", "生成")
CAPTION_FALLBACKS = (
    "喏，收好——这幅我盯着调了半天光和颜色，喜欢就夸我两句嘛～",
    "哼哼，本鱼出手，风都乖乖钻进画里了；你看看还顺眼吗？",
    "给你，这就是刚刚在我脑子里浮现的画面，氛围感拉满，不许说不好看～",
)

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

    @staticmethod
    def _effective_ai_cfg(ai_cfg: dict) -> dict:
        """合并 ai_chat 当前 profile（auth/model/provider/base_url...），供说明生成用。"""
        cfg = dict(ai_cfg) if isinstance(ai_cfg, dict) else {}
        profiles = [p for p in (cfg.get("profiles") or []) if isinstance(p, dict)]
        active = str(cfg.get("active_profile") or "")
        profile = next((p for p in profiles if str(p.get("id")) == active), None)
        if profile is None and profiles:
            profile = profiles[0]
        if profile:
            for field in ("provider", "base_url", "api_key", "model", "temperature", "max_tokens"):
                value = profile.get(field)
                if value not in (None, ""):
                    cfg[field] = value
            if "auth" in profile:
                cfg["auth"] = str(profile.get("auth") or "").strip()
            if cfg.get("auth"):
                cfg["model"] = str(profile.get("model") or "").strip()
        cfg["auth"] = str(cfg.get("auth") or "").strip()
        return cfg

    @staticmethod
    def _caption_fallback(prompt: str) -> str:
        key = str(prompt or "").encode("utf-8") or b"x"
        return CAPTION_FALLBACKS[sum(key) % len(CAPTION_FALLBACKS)]

    def _roleplay_caption_sync(self, ai_cfg: dict, prompt: str, images: list) -> str:
        """用当前 AI 配置生成角色口吻的 1-2 句画面说明；失败返回角色化兜底。"""
        ai_cfg = ai_cfg if isinstance(ai_cfg, dict) else {}
        auth = str(ai_cfg.get("auth") or "").strip()
        api_key = str(ai_cfg.get("api_key") or "").strip()
        if not auth and not api_key:
            return self._caption_fallback(prompt)
        provider = str(ai_cfg.get("provider") or "openai")
        base_url = str(ai_cfg.get("base_url") or "")
        vision = ai_cfg.get("vision") if isinstance(ai_cfg.get("vision"), dict) else {}
        model = str(ai_cfg.get("model") or "").strip()
        if not model and not auth:
            model = "deepseek-chat"
        description = ""
        if images and vision.get("enabled", True) is not False:
            try:
                description = providers.describe_images(
                    provider=provider,
                    base_url=base_url,
                    api_key=api_key,
                    model=str(vision.get("model") or model),
                    images=list(images[:1]),
                    prompt="请客观描述这张图的画面主体、颜色、光线和氛围，1-2 句话。",
                    timeout=float(vision.get("timeout_seconds") or 30),
                    auth=auth,
                )
            except Exception:
                description = ""
        user = f"用户想要的画面：{str(prompt or '').strip()}"
        if description:
            user += f"\n画面实际内容参考：{str(description).strip()[:500]}"
        user += "\n请用你的角色口吻写 1-2 句画面说明。"
        try:
            reply = providers.chat(
                provider=provider,
                base_url=base_url,
                api_key=api_key,
                model=model,
                messages=[
                    {"role": "system", "content": CAPTION_AI_SYSTEM},
                    {"role": "user", "content": user},
                ],
                temperature=0.8,
                max_tokens=160,
                timeout=45,
                auth=auth,
            )
        except providers.ProviderError:
            return self._caption_fallback(prompt)
        text = str(reply or "").replace("```", "").strip()
        text = text.strip("\"'“”「」").strip()
        text = " ".join(text.split())
        if len(text) < 4:
            return self._caption_fallback(prompt)
        lowered = text.lower()
        if any(word.lower() in lowered for word in CAPTION_BANNED):
            return self._caption_fallback(prompt)
        return text[:200]

    async def _roleplay_caption(self, ai_cfg: dict, prompt: str, images: list) -> str:
        try:
            return await asyncio.to_thread(
                self._roleplay_caption_sync, ai_cfg, prompt, images
            )
        except Exception as e:
            self.ctx.log.info("角色化画图说明生成失败，用兜底: %s", e)
            return self._caption_fallback(prompt)

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
        ai_cfg = self._effective_ai_cfg(self.ctx.plugin_cfg("ai_chat"))
        mode = self._mode(cfg, ai_cfg)
        if mode[0] == "none":
            await self.ctx.reply(event, str(mode[1]))
            return True

        # 机器人形象请求：快捷触发词路径也要前置形象提示词 + 优先用参考图
        avatar_request = imagegen.looks_like_avatar_request(prompt)
        avatar_prompt = imagegen.avatar_prompt(ai_cfg)
        if avatar_request and avatar_prompt and avatar_prompt not in prompt:
            prompt = f"{avatar_prompt}，{prompt}"
            self.ctx.log.info("形象请求：已前置 avatar.prompt")
        reference_path = (
            imagegen.avatar_reference_path(ai_cfg) if avatar_request else None
        )
        images = None
        if reference_path is not None:
            size = str(cfg.get("size") or "1024x1024").strip() or "1024x1024"
            try:
                timeout = float(cfg.get("timeout_seconds") or 120)
            except (TypeError, ValueError):
                timeout = 120.0
            try:
                images = await asyncio.to_thread(
                    imagegen.generate_with_reference,
                    ai_cfg,
                    cfg,
                    prompt,
                    reference_path,
                    size,
                    timeout,
                )
                if images:
                    self.ctx.log.info("形象请求：使用参考图生成")
            except Exception as e:
                self.ctx.log.warning("参考图生成失败，回退纯文生图: %s", e)
                images = None

        if not images:
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
        if cfg.get("caption_ai", True) is not False:
            caption = await self._roleplay_caption(ai_cfg, prompt, images)
        else:
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
