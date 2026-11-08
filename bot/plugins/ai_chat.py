"""AI 聊天插件。

能力：
- 私聊直接聊；群里只有 @ 机器人才回（可配置）
- 回复时 @ 提问的人，默认不引用原消息
- 用户发图片 → 调视觉模型识别；发文件 → 抽取文本一起喂给模型
- 用户引用（回复）另一条消息时，会去读被引用消息里的图片/文件
- 联网搜索：命中触发词（或 search.always）时先搜再答
- 多轮上下文，持久化到 bot/data/context.json（管理台可看/清空）
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import urllib.error
import urllib.request

import files as files_mod
import media
import providers
import search
from dispatcher import PluginBase
from utils import Cooldown, mentioned, strip_at, text_of, text_seg

_MD_RE = re.compile(r"\*\*|__|`{1,3}|^#{1,6}\s*", re.M)


class Plugin(PluginBase):
    name = "ai_chat"
    priority = 300

    def __init__(self, ctx):
        super().__init__(ctx)
        self.cd = Cooldown(0)
        self.history: dict[str, dict] = {}  # key -> {"ts": float, "messages": [...]}
        self._warned_no_key = False
        self._awake: dict[str, float] = {}   # 群号 -> 最后一次被唤醒的时间
        self.ctx_file = self.ctx.data_dir / "context.json"
        self._loaded_mtime = -1.0
        self._load_if_changed()
        self.store = files_mod.FileStore(self.ctx.data_dir)

    # ---------------- 上下文持久化（管理台可读 / 清空） ----------------
    def _load_if_changed(self):
        try:
            mtime = self.ctx_file.stat().st_mtime
        except FileNotFoundError:
            mtime = 0.0
        if mtime == self._loaded_mtime:
            return
        self._loaded_mtime = mtime
        try:
            data = json.loads(self.ctx_file.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        history = {}
        for key, val in (data or {}).items():
            if isinstance(val, dict) and isinstance(val.get("messages"), list):
                history[key] = {"ts": float(val.get("updated_at") or 0), "messages": val["messages"]}
        self.history = history

    def _save(self):
        self.ctx_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            key: {"updated_at": val["ts"], "messages": val["messages"]}
            for key, val in self.history.items()
        }
        try:
            self.ctx_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            self._loaded_mtime = self.ctx_file.stat().st_mtime
        except Exception as e:
            self.ctx.log.warning("保存上下文失败: %s", e)

    # ---------------- 多套接口（profiles） ----------------
    PROFILE_FIELDS = (
        "provider",
        "base_url",
        "api_key",
        "auth",
        "model",
        "temperature",
        "max_tokens",
    )

    def effective_cfg(self) -> dict:
        """顶层配置 + 当前激活 profile 的覆盖。"""
        return self._profile_settings()

    def _profile_settings(self) -> dict:
        """当前生效的接口设置。

        auth 为空串表示用 API Key；非空是账号 id（chatgpt/claude/gemini/deepseek）。
        账号模式下 model 只看当前 profile，空串表示用该账号的默认模型。
        """
        cfg = dict(self.ctx.plugin_cfg("ai_chat"))
        profiles = [p for p in (cfg.get("profiles") or []) if isinstance(p, dict)]
        active = str(cfg.get("active_profile") or "")
        prof = next((p for p in profiles if str(p.get("id")) == active), None)
        if prof is None and profiles:
            prof = profiles[0]
        if prof:
            for field in self.PROFILE_FIELDS:
                if field == "auth":
                    continue
                v = prof.get(field)
                if v not in (None, ""):
                    cfg[field] = v
            # 认证方式按 profile 显式值：空串也是有效值（改用 API Key）
            if "auth" in prof:
                cfg["auth"] = str(prof.get("auth") or "").strip()
            # 账号模式：模型只认当前 profile；空就用账号默认模型
            if cfg.get("auth"):
                cfg["model"] = str(prof.get("model") or "").strip()
        cfg["auth"] = str(cfg.get("auth") or "").strip()
        return cfg

    # ---------------- 触发判断 ----------------
    def _extract_prompt(self, event) -> str | None:
        """None = 不该由 AI 处理；"" = 触发了但没文字（只发了图片/文件）。"""
        cfg = self.ctx.plugin_cfg("ai_chat")
        trigger = cfg.get("trigger") or {}
        message_type = event.get("message_type")
        message = event.get("message")
        text = text_of(message)

        if message_type == "private":
            if not trigger.get("private", True):
                return None
            return text

        group_id = event.get("group_id")
        whitelist = cfg.get("group_whitelist") or []
        blacklist = cfg.get("group_blacklist") or []
        if whitelist and str(group_id) not in {str(g) for g in whitelist}:
            return None
        if blacklist and str(group_id) in {str(g) for g in blacklist}:
            return None

        # 唤醒后的一段时间内，群里普通消息也继续对话（"叫一次然后接着聊"）
        follow_ttl = float(trigger.get("group_followup_seconds", 0) or 0)
        if follow_ttl > 0:
            awake_key = (str(group_id), str(event.get("user_id")))
            last = self._awake.get(awake_key, 0.0)
            if last and (time.time() - last) <= follow_ttl and text.strip():
                self._awake[awake_key] = time.time()
                return text.strip()

        prefixes = [str(p) for p in (trigger.get("group_prefix") or [])]
        def mark_awake():
            ttl = float(trigger.get("group_followup_seconds", 0) or 0)
            if ttl > 0:
                self._awake[(str(group_id), str(event.get("user_id")))] = time.time()

        if trigger.get("group_at", True) and mentioned(message, self.ctx.self_id):
            mark_awake()
            stripped = text_of(strip_at(message, self.ctx.self_id))
            for prefix in prefixes:
                if stripped.startswith(prefix):
                    stripped = stripped[len(prefix):]
            return stripped.strip()

        # 叫名字也算触发（"大肥鱼呢" / "肥鱼在吗"），不用非得 @
        aliases = [str(x) for x in (trigger.get("group_name_aliases") or []) if str(x)]
        if not aliases:
            aliases = [self.ctx.bot_name]
        for alias in aliases:
            if alias and alias in text:
                mark_awake()
                cleaned = text.replace(alias, "", 1).strip(" ，,。.！!？?~～")
                for prefix in prefixes:
                    if cleaned.startswith(prefix):
                        cleaned = cleaned[len(prefix):].strip()
                return cleaned or "叫本鱼干嘛？"
        for prefix in prefixes:
            if text.startswith(prefix):
                return text[len(prefix):].strip()
        return None

    # ---------------- 历史 ----------------
    def _history(self, key: str) -> list:
        self._load_if_changed()
        cfg = self.ctx.plugin_cfg("ai_chat")
        ttl = float(cfg.get("history_ttl_minutes", 30)) * 60
        item = self.history.get(key)
        if item and (time.time() - item["ts"]) > ttl:
            self.history.pop(key, None)
            item = None
        return item["messages"] if item else []

    def _remember(self, key: str, role: str, content: str):
        cfg = self.ctx.plugin_cfg("ai_chat")
        max_msgs = int(cfg.get("max_history", 8)) * 2
        messages = self._history(key)
        messages.append({"role": role, "content": content})
        del messages[:-max_msgs]
        self.history[key] = {"ts": time.time(), "messages": messages}
        self._save()

    # ---------------- 附件（图片 / 文件 / 引用的消息） ----------------
    @staticmethod
    def _segs(message) -> list:
        return message if isinstance(message, list) else []

    async def _collect_attachments(self, event) -> list[dict]:
        """收集本条消息 + 被引用消息里的图片/文件。"""
        attachments = []
        segs = self._segs(event.get("message"))
        for seg in segs:
            t = seg.get("type")
            if t in ("image", "file"):
                attachments.append({"type": t, "data": seg.get("data") or {}, "quoted": False})

        # 引用了另一条消息：把那条消息里的图片/文件也读出来
        for seg in segs:
            if seg.get("type") != "reply":
                continue
            mid = (seg.get("data") or {}).get("id")
            if not mid:
                continue
            try:
                quoted = await self.ctx.api.call("get_msg", message_id=int(mid))
            except Exception as e:
                self.ctx.log.info("读取被引用消息失败 id=%s: %s", mid, e)
                continue
            for qseg in self._segs((quoted or {}).get("message")):
                t = qseg.get("type")
                if t in ("image", "file"):
                    attachments.append({"type": t, "data": qseg.get("data") or {}, "quoted": True})
        return attachments

    async def _attachment_context(self, event, attachments: list[dict], cfg: dict) -> str:
        att = cfg.get("attachments") or {}
        if not attachments or not att.get("enabled", True):
            return ""
        max_images = int(att.get("max_images", 3))
        max_bytes = int(att.get("max_file_bytes", 15_000_000))
        max_chars = int(att.get("max_file_chars", 4000))
        parts: list[str] = []
        images: list[bytes] = []

        for item in attachments:
            data = item["data"]
            url = str(data.get("url") or data.get("file") or "")
            quoted = "（引用的消息里的）" if item.get("quoted") else ""
            if item["type"] == "image":
                if len(images) >= max_images:
                    continue
                try:
                    raw, _ctype = await asyncio.to_thread(media.fetch, url, max_bytes)
                    images.append(raw)
                except Exception as e:
                    parts.append(f"（{quoted}图片下载失败：{e}）")
                continue

            name = str(data.get("file") or data.get("name") or "文件")
            try:
                raw, ctype = await asyncio.to_thread(media.fetch, url, max_bytes)
            except Exception as e:
                parts.append(f"（{quoted}文件 {name} 下载失败：{e}）")
                continue
            try:
                info = await asyncio.to_thread(media.extract_text, raw, name, ctype, max_chars)
            except Exception as e:
                parts.append(f"（{quoted}文件 {name} 解析失败：{e}）")
                continue
            if info.get("kind") == "image":
                if len(images) < max_images:
                    images.append(raw)
            elif info.get("text"):
                note = "（已截断）" if info.get("truncated") else ""
                parts.append(f"【{quoted}文件 {name}{note}】\n{info['text']}")
            elif info.get("error"):
                parts.append(f"（{quoted}文件 {name} 读取失败：{info['error']}）")

        if images:
            vcfg = cfg.get("vision") or {}
            if vcfg.get("enabled", True):
                try:
                    desc = await asyncio.to_thread(
                        providers.describe_images,
                        provider=str(vcfg.get("provider") or cfg.get("provider") or "openai"),
                        base_url=str(vcfg.get("base_url") or cfg.get("base_url", "")),
                        api_key=str(vcfg.get("api_key") or cfg.get("api_key", "")),
                        model=str(vcfg.get("model") or cfg.get("model", "")),
                        images=images,
                        prompt=str(vcfg.get("prompt") or "请用中文客观描述这张图片，重点提取里面的文字信息。"),
                        timeout=float(vcfg.get("timeout_seconds", 60)),
                    )
                    parts.append(f"【图片内容识别】{desc}")
                except providers.ProviderError as e:
                    parts.append(f"（图片识别失败：{e}）")
                    self.ctx.log.warning("视觉识别失败: %s", e)
                except Exception as e:
                    parts.append(f"（图片识别异常：{e}）")
                    self.ctx.log.warning("视觉识别异常: %s", e)
        return "\n\n".join(parts)

    # ---------------- 联网搜索 ----------------
    @staticmethod
    def _contains_trigger(text: str, cfg: dict) -> bool:
        s = cfg.get("search") or {}
        if not s.get("enabled"):
            return False
        if s.get("always"):
            return True
        text = (text or "").strip()
        if not text:
            return False
        for kw in s.get("trigger_keywords") or []:
            if kw and str(kw) in text:
                return True
        # 显式指令：搜：xxx / 搜索xxx
        return bool(re.match(r"^(请?帮我)?(搜索|搜一下|查一下|搜：|搜:)", text))

    async def _search_context(self, text: str, cfg: dict) -> str:
        s = cfg.get("search") or {}
        try:
            result = await asyncio.to_thread(
                search.search,
                text,
                int(s.get("max_results", 5)),
                str(s.get("provider", "auto")),
            )
        except Exception as e:
            self.ctx.log.warning("联网搜索异常: %s", e)
            return ""
        if not result.get("ok"):
            self.ctx.log.info("联网搜索无结果: %s", result.get("error"))
            return ""
        formatted = search.format_results(result, 1800)
        if formatted:
            self.ctx.log.info("联网搜索命中 %d 条：%s", len(result.get("results") or []), text[:30])
        return formatted

    # ---------------- 调模型（openai / claude / gemini 由 providers 适配） ----------------
    def _sync_chat(self, cfg: dict, messages: list) -> str:
        auth = str(cfg.get("auth") or "").strip()
        model = str(cfg.get("model") or "").strip()
        if not model and not auth:
            model = "deepseek-chat"
        return providers.chat(
            provider=str(cfg.get("provider") or "openai"),
            base_url=str(cfg.get("base_url", "")),
            api_key=str(cfg.get("api_key", "")),
            model=model,
            messages=messages,
            temperature=float(cfg.get("temperature", 0.7)),
            max_tokens=int(cfg.get("max_tokens", 600)),
            timeout=float(cfg.get("timeout_seconds", 60)),
            auth=auth,
        )

    @staticmethod
    def _wants_file(prompt: str, blocks: list, cfg: dict) -> bool:
        fcfg = cfg.get("files") or {}
        if not fcfg.get("enabled", True) or not blocks:
            return False
        if re.search(r"文件|发我|导出|保存|下载|附件|打包|给我发", prompt or ""):
            return True
        # HTML / SVG 在聊天里没法看效果，下载成文件才能用浏览器打开
        if any((b.get("lang") or "") in ("html", "htm", "svg", "xml") for b in blocks):
            return True
        min_lines = int(fcfg.get("min_lines", 15))
        min_chars = int(fcfg.get("min_chars", 400))
        return any(b["lines"] >= min_lines or b["chars"] >= min_chars for b in blocks)

    async def _export_code_files(self, event, prompt: str, answer: str, cfg: dict):
        """把回答里的代码块存成本地文件并发到会话。返回 (新正文, 发送成功的文件名列表)。"""
        fcfg = cfg.get("files") or {}
        blocks = files_mod.extract_code_blocks(answer)
        if not self._wants_file(prompt, blocks, cfg):
            return answer, []
        max_files = int(fcfg.get("max_files", 3))
        sent, body = [], answer
        for i, block in enumerate(blocks[:max_files], start=1):
            name = files_mod.make_filename(block["lang"], "代码", i if len(blocks) > 1 else 0)
            try:
                path = self.store.save_text(block["code"], name)
            except Exception as e:
                self.ctx.log.warning("保存代码文件失败: %s", e)
                continue
            ok = await files_mod.send_file(self.ctx, event, path, name)
            if not ok:
                # 发送失败：保留原始代码，别把内容吞掉
                self.ctx.log.warning("代码文件发送失败，保留正文原文: %s", name)
                continue
            sent.append(name)
            fence = block.get("raw") or ("```" + (block["lang"] or "") + "\n" + block["code"] + "```")
            body = body.replace(fence, "", 1)
        if sent:
            note = str(fcfg.get("note") or "📎 完整代码已保存成文件发出：{names}").replace("{names}", "、".join(sent))
            body = (body.strip() + "\n\n" + note).strip() if body.strip() else note
            self.store.cleanup(80)
        return body, sent

    @staticmethod
    def _clean(text: str, limit: int = 500) -> str:
        text = _MD_RE.sub("", text or "").strip()
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text[:limit] + ("…" if len(text) > limit else "")

    async def _quoted_message(self, event):
        """返回被引用的消息对象（没有则 None）。"""
        for seg in self._segs(event.get("message")):
            if seg.get("type") != "reply":
                continue
            mid = (seg.get("data") or {}).get("id")
            if not mid:
                continue
            try:
                return await self.ctx.api.call("get_msg", message_id=int(mid))
            except Exception:
                return None
        return None

    async def _is_quote_to_bot(self, event) -> bool:
        quoted = await self._quoted_message(event)
        if not quoted:
            return False
        sender = quoted.get("sender") or {}
        return str(sender.get("user_id")) == str(self.ctx.self_id)

    async def _quoted_text_block(self, event) -> str:
        """引用的纯文字消息也要让模型看到（之前只处理了图片/文件）。"""
        quoted = await self._quoted_message(event)
        if not quoted:
            return ""
        sender = quoted.get("sender") or {}
        who = sender.get("card") or sender.get("nickname") or str(sender.get("user_id", ""))
        body = text_of(quoted.get("message")) or ""
        if not body:
            return ""
        return f"【被引用的消息｜{who}】{body[:500]}"

    def _reply_message(self, event, answer: str, cfg: dict) -> list:
        """群聊：@ 提问的人（默认不引用原消息）；私聊：直接文本。"""
        message = [text_seg(answer)]
        if event.get("message_type") == "group":
            head = []
            if cfg.get("reply_quote", False) and event.get("message_id"):
                head.append({"type": "reply", "data": {"id": str(event["message_id"])}})
            if cfg.get("at_sender", True):
                head.append({"type": "at", "data": {"qq": str(event.get("user_id"))}})
                head.append(text_seg(" "))
            message = head + message
        return message

    async def on_event(self, event) -> bool:
        cfg = self.effective_cfg()
        if not cfg.get("enabled"):
            return False
        if event.get("post_type") != "message":
            return False

        prompt = self._extract_prompt(event)
        if prompt is None:
            # 引用机器人的消息也算在跟它说话
            trigger = cfg.get("trigger") or {}
            if event.get("message_type") == "group" and trigger.get("group_quote", True):
                if await self._is_quote_to_bot(event):
                    ttl = float(trigger.get("group_followup_seconds", 0) or 0)
                    if ttl > 0:
                        self._awake[(str(event.get("group_id")), str(event.get("user_id")))] = time.time()
                    prompt = text_of(event.get("message")) or "你引用的这条是本鱼说的"
            if prompt is None:
                return False

        attachments = await self._collect_attachments(event)
        if not prompt and not attachments:
            return False
        if not prompt:
            prompt = "看看我发的东西" if attachments else ""

        user_id = event.get("user_id")
        group_id = event.get("group_id") or 0
        key = f"{'group' + str(group_id) if group_id else 'private'}:{user_id}"

        if not self.cd.allow(key, cfg.get("cooldown_seconds", 3)):
            await self.ctx.reply(event, "别急，我还在想上一个问题～")
            return True

        auth = str(cfg.get("auth") or "").strip()
        api_key = str(cfg.get("api_key", "")).strip()
        if not auth and not api_key:
            if not self._warned_no_key:
                self.ctx.log.warning("ai_chat 未配置 api_key/账号，跳过 AI 回复（编辑 bot/config.json）")
                self._warned_no_key = True
            if event.get("message_type") == "private":
                await self.ctx.reply(event, "[AI 未配置] 请先在控制台 AI 页填 API Key 或连接 AI 账号")
                return True
            return False

        system = self.ctx.format_text(cfg.get("system_prompt", ""), event, group_id=group_id)
        messages = [{"role": "system", "content": system}]
        messages += self._history(key)

        # 附件（图片视觉识别 / 文件文本抽取）
        extra_blocks = []
        quoted_block = await self._quoted_text_block(event)
        if quoted_block:
            extra_blocks.append(quoted_block)
        if attachments:
            att_ctx = await self._attachment_context(event, attachments, cfg)
            if att_ctx:
                extra_blocks.append("【用户发来的图片/文件里的内容】\n" + att_ctx)

        # 联网搜索
        if self._contains_trigger(prompt, cfg):
            search_ctx = await self._search_context(prompt, cfg)
            if search_ctx:
                extra_blocks.append("【联网实时搜索结果】\n" + search_ctx)

        if extra_blocks:
            messages.append(
                {
                    "role": "system",
                    "content": "\n\n".join(extra_blocks)
                    + "\n\n请结合以上资料回答；如果用了搜索结果，可以附上来源链接。",
                }
            )
        messages.append({"role": "user", "content": prompt})

        try:
            answer = await asyncio.to_thread(self._sync_chat, cfg, messages)
        except providers.ProviderError as e:
            self.ctx.log.error("AI 接口报错: %s", e)
            await self.ctx.reply(event, f"AI 接口报错：{e}")
            return True
        except Exception as e:
            self.ctx.log.error("AI 调用失败: %s", e)
            await self.ctx.reply(event, "AI 服务暂时不可用，稍后再试。")
            return True

        # 模型偶发返回空内容（内容过滤/截断）：重试一次，仍空就明确告诉用户
        if not (answer or "").strip():
            self.ctx.log.warning("模型返回空内容，重试一次。prompt=%s", (prompt or "")[:60])
            try:
                retry_messages = messages + [{"role": "system", "content": "请直接用文字回答，不要返回空内容。"}]
                answer = await asyncio.to_thread(self._sync_chat, cfg, retry_messages)
            except Exception as e:
                self.ctx.log.warning("空回复重试失败: %s", e)
                answer = ""
        if not (answer or "").strip():
            await self.ctx.reply(event, "这次模型返回了空内容（多半是触发了内容过滤），换个说法再试试？")
            return True

        answer, sent_files = await self._export_code_files(event, prompt, answer, cfg)
        if sent_files:
            self.ctx.log.info("代码已发文件: %s", ", ".join(sent_files))
        answer = self._clean(answer, int(cfg.get("max_output_chars", 500)))
        if not answer:
            answer = "（这次没想出来，换个问法试试）"
        self._remember(key, "user", prompt)
        self._remember(key, "assistant", answer)

        await self.ctx.reply(event, self._reply_message(event, answer, cfg))
        self.ctx.log.info("AI 回复 %s: %s", key, answer[:40])
        return True
