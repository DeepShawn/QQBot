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
import base64
import io
import json
import re
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import files as files_mod
import imagegen
import media
import providers
import pytool
import search
import shelltool
import skills as skills_mod
from dispatcher import PluginBase
from utils import Cooldown, mentioned, strip_at, text_of, text_seg

_MD_RE = re.compile(r"\*\*|__|`{1,3}|^#{1,6}\s*", re.M)

# 本地 Python 能力提示（追加到 system prompt）
LOCAL_PYTHON_TOOL_HINT = (
    "【本地 Python】你有一个 run_python 工具：用户让你运行代码、计算、执行本地脚本时，"
    "自己调用它（code 传 Python 源码，file 传 data/files 下的 .py 文件名），"
    "不要把「运行」推回给用户，也不要说自己没有工具。"
)
LOCAL_PYTHON_NO_TOOL_HINT = (
    "【本地 Python】机器人在手机本机可以运行 Python，但你当前不能直接调用工具；"
    "用户要运行代码时，可以请有权限的管理员发 /py <代码> 或 /run <文件名>，"
    "不要说自己完全没有运行能力。"
)

# 画图能力提示（追加到 system prompt）
LOCAL_IMAGE_TOOL_HINT = (
    "【画图工具】用户要画图/生成图片/来一张图时，必须调用 generate_image 工具；"
    "绝对不要用 ASCII/emoji/代码画图，禁止用 SVG 或代码块冒充图片，"
    "也不要把画图推回给用户。"
)

IMAGE_TOOL_NAME = "generate_image"
IMAGE_SIZES = ("1024x1024", "1024x1792", "1792x1024")
IMAGE_TOOL_SPEC = {
    "type": "function",
    "function": {
        "name": IMAGE_TOOL_NAME,
        "description": (
            "用户要画图/生成图片/来一张图时调用；禁止用 ASCII、emoji、SVG 或代码块冒充图片，"
            "必须直接生成图片发出去。prompt 必须由你自己根据用户需求写一段高质量生图提示词"
            "（主体、风格、构图、光线、画幅等），不要只复述用户原话。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "你自己写好的高质量生图提示词（不是照抄用户原话）",
                },
                "size": {
                    "type": "string",
                    "enum": list(IMAGE_SIZES),
                    "description": "图片尺寸，可选，默认 1024x1024",
                },
            },
            "required": ["prompt"],
        },
    },
}

SHELL_TOOL_NAME = "run_shell"
SHELL_TOOL_SPEC = {
    "type": "function",
    "function": {
        "name": SHELL_TOOL_NAME,
        "description": (
            "在手机本机的 Ubuntu/Linux 环境执行 shell 命令（可以 apt、git、跑长任务等）。"
            "命令会真实执行，请只做用户要求的事；不要用它读取或输出密钥。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的完整 shell 命令"},
                "timeout": {
                    "type": "integer",
                    "description": "超时秒数，可选，默认 60，范围 1–3600",
                },
            },
            "required": ["command"],
        },
    },
}

SKILL_TOOL_NAME = "read_skill"
SKILL_TOOL_SPEC = {
    "type": "function",
    "function": {
        "name": SKILL_TOOL_NAME,
        "description": (
            "读取技能目录里某个技能的完整 Markdown 说明。"
            "先看 system 提示里的技能列表，需要细节时再调用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "技能名（文件名去掉 .md）"}
            },
            "required": ["name"],
        },
    },
}

LOCAL_SHELL_TOOL_HINT = (
    "【开发者工具】你有一个 run_shell 工具：用户让你执行 shell 命令、apt/git 操作、"
    "查看或修改本机环境、跑长任务时可以自己调用，不要把命令推回给用户。"
)

PYTHON_TOOL_NAME = "run_python"
PYTHON_TOOL_SPEC = {
    "type": "function",
    "function": {
        "name": PYTHON_TOOL_NAME,
        "description": (
            "在手机本机运行 Python 代码或 data/files 下的 .py 文件并返回 stdout/stderr。"
            "用户要求运行代码、计算、执行脚本时使用。code 和 file 二选一。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "要运行的完整 Python 源码"},
                "file": {
                    "type": "string",
                    "description": "data/files 下已有的 .py 文件名，如 代码_0101_120000.py",
                },
            },
        },
    },
}
MAX_TOOL_ROUNDS = 3
ROUTE_MAX_HISTORY = 6
ROUTE_MAX_TOKENS = 300

# 确定性的生图意图兜底（模型路由不可靠时直接走画图流程）
IMAGE_INTENT_RE = re.compile(
    r"(画张|画一张|画一个|画一幅|来一张|来张|生成[^，。！？\s]{0,6}图|绘制|"
    r"插画|海报|壁纸|头像|风景图|logo|表情包|draw|illustration|image\s+of|"
    r"使用画图|用画图|画图|帮我画|画个|画只|画一下)",
    re.I,
)
# 用户只说「继续/再来」时，结合最近历史判断是不是接着画图
CONTEXT_IMAGE_RE = re.compile(r"(继续|再来|再画|接着画|再生成|再来一张)")

# 机器人形象类请求：只有这类才拼 avatar.prompt / 使用参考图
AVATAR_REQUEST_RE = re.compile(
    r"(机器人(的)?(形象|头像|立绘)|你的(形象|头像|立绘)|"
    r"画(个|一个|一张)?你(自己|的形象|的头像)?|自画像|"
    r"头像|立绘|(bot|机器人)\s*(形象|头像|立绘)|avatar)",
    re.I,
)
AVATAR_BASE_DIR = Path(__file__).resolve().parents[2]


def _multipart_body(fields: dict, files: list) -> tuple[bytes, str]:
    """构造 multipart/form-data；files=[(name, filename, content_type, bytes)]。"""
    boundary = "----qqbot" + uuid.uuid4().hex
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")
        )
    for name, filename, content_type, content in files:
        chunks.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode("utf-8")
        )
        chunks.append(bytes(content))
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), boundary
IMAGE_PROMPT_FROM_HISTORY_SYSTEM = (
    "你是生图提示词作者。请结合对话上下文，把用户这次的画图需求写成一句具体的中文生图提示词，"
    "包含主体、风格、构图、光线、画幅等细节；只输出提示词本身，"
    "不要解释、不要加引号或代码块、不要照抄用户原话。"
)

IMAGE_PROMPT_REWRITE_SYSTEM = (
    "你是生图提示词改写助手。根据用户需求写一句具体的中文生图提示词，"
    "包含主体、风格、构图、光线、画幅等细节；只输出提示词本身，"
    "不要解释、不要加引号或代码块、不要照抄用户原话。"
)
IMAGE_PROMPT_FALLBACK_REPLY = "画好了，图在下面～"

ROUTE_SYSTEM_HEAD = (
    "你是工具路由器。先分析用户需求，再从可用工具里选择 0 或 1 个；"
    "不要回答用户问题，不要解释。\n"
    "如果用户要画图/生成图片/来一张图/壁纸，必须选择 generate_image；"
    "禁止用 ASCII、emoji、SVG 或代码块冒充图片。\n"
    "如果选择 generate_image，prompt 字段必须是你自己根据需求写好的具体生图提示词"
    "（包含主体、风格、构图、光线、画幅等），不要照抄用户原话。\n"
    "可用工具：\n"
)
ROUTE_SYSTEM_FORMAT = (
    "\n输出要求：只输出一个严格 JSON 对象（不要代码块、不要多余文字）：\n"
)
ROUTE_SYSTEM_NONE = '· 不需要工具：{"tool":"none"}\n'


def local_python_hint(ctx, tool_available: bool = False) -> str:
    """python_run 启用时返回本地 Python 提示；工具可用/不可用两种文案。"""
    try:
        cfg = ctx.plugin_cfg("python_run") or {}
    except Exception:
        cfg = {}
    if cfg.get("enabled") is False:
        return ""
    return LOCAL_PYTHON_TOOL_HINT if tool_available else LOCAL_PYTHON_NO_TOOL_HINT


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
        top_cfg = self.ctx.plugin_cfg("ai_chat")
        cfg = dict(top_cfg)
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
        # 人格提示词只认顶层：profile 只覆盖接口字段，不能让 system_prompt 变空
        cfg["system_prompt"] = str(top_cfg.get("system_prompt") or "").strip()
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
        try:
            ttl_minutes = float(cfg.get("history_ttl_minutes", 30))
        except (TypeError, ValueError):
            ttl_minutes = 30.0
        ttl = ttl_minutes * 60
        item = self.history.get(key)
        # ttl <= 0 表示永不过期（用户选择「不遗忘」，0 和负数都算）
        if item and ttl > 0 and (time.time() - item["ts"]) > ttl:
            self.history.pop(key, None)
            item = None
        return item["messages"] if item else []

    def _remember(self, key: str, role: str, content: str):
        cfg = self.ctx.plugin_cfg("ai_chat")
        try:
            max_turns = int(cfg.get("max_history", 8))
        except (TypeError, ValueError):
            max_turns = 8
        messages = self._history(key)
        messages.append({"role": role, "content": content})
        # max_turns <= 0 表示不限制轮数、保留全部历史；注意不能写成 del messages[:-0]
        # （切片 -0 等价于 0，会把历史清空）。
        if max_turns > 0:
            max_msgs = max_turns * 2
            if len(messages) > max_msgs:
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

    @staticmethod
    def _attachment_max_bytes(att: dict, cfg: dict) -> int:
        """附件下载大小上限（0 = 不限）。

        - attachments.ignore_size_limit 显式配置优先（true → 不限，false → 用 max_file_bytes）；
        - 未配置时：账号套餐模式（ai_chat.auth 非空）不限；API Key 模式沿用
          max_file_bytes（默认 15MB）。
        """
        try:
            default_max = int(att.get("max_file_bytes", 15_000_000))
        except (TypeError, ValueError):
            default_max = 15_000_000
        if default_max <= 0:
            default_max = 15_000_000
        override = att.get("ignore_size_limit")
        if isinstance(override, str):
            value = override.strip().lower()
            if value in ("1", "true", "yes", "on"):
                override = True
            elif value in ("0", "false", "no", "off"):
                override = False
            else:
                override = None
        if isinstance(override, bool):
            return 0 if override else default_max
        if str(cfg.get("auth") or "").strip():
            return 0  # 账号套餐模式：机器人自己不再卡附件大小
        return default_max

    async def _attachment_context(self, event, attachments: list[dict], cfg: dict) -> str:
        att = cfg.get("attachments") or {}
        if not attachments or not att.get("enabled", True):
            return ""
        max_images = int(att.get("max_images", 3))
        max_bytes = self._attachment_max_bytes(att, cfg)
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
                # 识图优先用 vision.api_key；没填时跟随 AI 的认证方式：
                # 账号模式（auth 非空）走账号识图，否则沿用 AI 的 API Key。
                vision_key = str(vcfg.get("api_key") or "").strip()
                vision_auth = "" if vision_key else str(cfg.get("auth") or "").strip()
                vision_api_key = vision_key or (
                    "" if vision_auth else str(cfg.get("api_key") or "")
                )
                vision_model = str(vcfg.get("model") or cfg.get("model") or "").strip()
                try:
                    desc = await asyncio.to_thread(
                        providers.describe_images,
                        provider=str(vcfg.get("provider") or cfg.get("provider") or "openai"),
                        base_url=str(vcfg.get("base_url") or cfg.get("base_url", "")),
                        api_key=vision_api_key,
                        model=vision_model,
                        images=images,
                        prompt=str(vcfg.get("prompt") or "请用中文客观描述这张图片，重点提取里面的文字信息。"),
                        timeout=float(vcfg.get("timeout_seconds", 60)),
                        auth=vision_auth,
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

    # ---------------- 本地 Python 工具（由 AI 自主决定是否调用） ----------------
    def _python_run_cfg(self) -> dict:
        try:
            return self.ctx.plugin_cfg("python_run") or {}
        except Exception:
            return {}

    def _python_tool_timeout(self) -> float:
        try:
            return float(
                self._python_run_cfg().get("timeout_seconds", pytool.DEFAULT_TIMEOUT)
            )
        except (TypeError, ValueError):
            return pytool.DEFAULT_TIMEOUT

    async def _can_use_tools(self, event) -> bool:
        """工具（run_python / generate_image）权限：私聊直接可以；

        群里只允许 bot.admins 或群主/群管理员。
        """
        if event.get("message_type") != "group":
            return True
        user_id = event.get("user_id")
        try:
            if self.ctx.is_admin(user_id):
                return True
        except Exception:
            pass
        api = getattr(self.ctx, "api", None)
        if api is None:
            return False
        try:
            info = await api.call(
                "get_group_member_info",
                group_id=int(event.get("group_id") or 0),
                user_id=int(user_id or 0),
                no_cache=True,
            )
        except Exception as e:
            self.ctx.log.info("ai_chat 查询群成员失败: %s", e)
            return False
        role = (
            str((info or {}).get("role") or "").lower()
            if isinstance(info, dict)
            else ""
        )
        return role in ("owner", "admin")

    def _python_tool_base_available(self) -> bool:
        """python_run 插件是否允许 AI 使用 run_python（不看模型是否支持工具调用）。"""
        py_cfg = self._python_run_cfg()
        return py_cfg.get("enabled") is not False and py_cfg.get("ai_tool") is not False

    def _dev_tools_cfg(self) -> dict:
        try:
            return self.ctx.plugin_cfg("dev_tools") or {}
        except Exception:
            return {}

    def _skills_cfg(self) -> dict:
        try:
            return self.ctx.plugin_cfg("skills") or {}
        except Exception:
            return {}

    def _shell_cwd(self):
        repo_path = str(self._dev_tools_cfg().get("repo_path") or "").strip()
        return repo_path or None  # None -> shelltool 用仓库根

    def _shell_env_extra(self):
        token = str(self._dev_tools_cfg().get("git_token") or "").strip()
        if not token:
            return None
        # 只放进子进程 env，不写日志
        return {"GITHUB_TOKEN": token, "GH_TOKEN": token}

    def _shell_timeout(self) -> float:
        try:
            value = float(
                self._dev_tools_cfg().get("timeout_seconds", shelltool.DEFAULT_TIMEOUT)
            )
        except (TypeError, ValueError):
            value = shelltool.DEFAULT_TIMEOUT
        return max(shelltool.MIN_TIMEOUT, min(shelltool.MAX_TIMEOUT, value))

    def _skills_enabled(self) -> bool:
        return self._skills_cfg().get("enabled") is not False

    def _skills_list(self) -> list:
        try:
            return skills_mod.list_skills(self.ctx.data_dir)
        except Exception:
            return []

    async def _shell_tool_available(self, event) -> bool:
        cfg = self._dev_tools_cfg()
        if cfg.get("enabled") is False or cfg.get("ai_tool") is False:
            return False
        return await self._can_use_tools(event)

    def _native_tools_supported(self, cfg: dict) -> bool:
        """当前 profile 是否能走原生 OpenAI function calling。"""
        if str(cfg.get("auth") or "").strip():
            return False
        return providers.normalize_provider(str(cfg.get("provider") or "openai")) == "openai"

    def _image_gen_cfg(self) -> dict:
        try:
            return self.ctx.plugin_cfg("image_gen") or {}
        except Exception:
            return {}

    def _image_tool_configured(self, cfg: dict) -> bool:
        """image_gen 配置上是否能画图（画图配置与主对话 auth 解耦）。"""
        image_cfg = self._image_gen_cfg()
        if image_cfg.get("enabled") is False:
            return False
        try:
            decision = imagegen.resolve_image_mode(cfg, image_cfg)
        except Exception:
            return False
        return decision.get("mode") in ("account", "api")

    async def _image_tool_available(self, event, cfg: dict) -> bool:
        """图片工具可用性：配置 ok + 权限 ok。"""
        if not self._image_tool_configured(cfg):
            return False
        return await self._can_use_tools(event)

    async def _python_tools(self, event, cfg: dict) -> list | None:
        """原生 function calling 下的 run_python 工具（账号/非 openai 由路由兜底）。"""
        if not self._native_tools_supported(cfg):
            return None
        if not self._python_tool_base_available():
            return None
        if not await self._can_use_tools(event):
            return None
        return [PYTHON_TOOL_SPEC]

    def _sync_chat_with_tools(
        self, cfg: dict, messages: list, tools: list | None
    ) -> dict:
        model = str(cfg.get("model") or "").strip() or "deepseek-chat"
        return providers.chat_with_tools(
            provider=str(cfg.get("provider") or "openai"),
            base_url=str(cfg.get("base_url", "")),
            api_key=str(cfg.get("api_key", "")),
            model=model,
            messages=messages,
            tools=tools,
            temperature=float(cfg.get("temperature", 0.7)),
            max_tokens=int(cfg.get("max_tokens", 2000) or 2000),
            timeout=float(cfg.get("timeout_seconds", 60)),
        )

    def _execute_python_tool(self, call: dict) -> str:
        """执行一次 run_python 调用，返回给模型的 JSON 文本。"""
        try:
            arguments = json.loads(str(call.get("arguments") or "{}"))
        except Exception:
            return json.dumps({"error": "工具参数不是合法 JSON"}, ensure_ascii=False)
        if not isinstance(arguments, dict):
            return json.dumps({"error": "工具参数格式不对"}, ensure_ascii=False)
        code = arguments.get("code")
        file = arguments.get("file")
        if not isinstance(code, str) or not code.strip():
            code = None
        if not isinstance(file, str) or not file.strip():
            file = None
        if code is None and file is None:
            return json.dumps({"error": "需要 code 或 file 参数"}, ensure_ascii=False)
        try:
            result = pytool.execute_python(
                code=code,
                file=file,
                timeout=self._python_tool_timeout(),
                data_dir=self.ctx.data_dir,
            )
        except Exception as e:
            self.ctx.log.warning("run_python 工具执行异常: %s", e)
            result = {"error": "运行 Python 失败，稍后再试。"}
        return json.dumps(result, ensure_ascii=False)

    def _execute_shell_tool(self, cfg: dict, call: dict) -> str:
        """执行一次 run_shell 调用，返回给模型的 JSON 文本。"""
        try:
            arguments = json.loads(str(call.get("arguments") or "{}"))
        except Exception:
            return json.dumps({"error": "工具参数不是合法 JSON"}, ensure_ascii=False)
        if not isinstance(arguments, dict):
            return json.dumps({"error": "工具参数格式不对"}, ensure_ascii=False)
        command = str(arguments.get("command") or "").strip()
        if not command:
            return json.dumps({"error": "需要 command 参数"}, ensure_ascii=False)
        timeout = arguments.get("timeout")
        try:
            timeout_value = float(timeout) if timeout is not None else self._shell_timeout()
        except (TypeError, ValueError):
            timeout_value = self._shell_timeout()
        try:
            result = shelltool.run_shell(
                command,
                cwd=self._shell_cwd(),
                timeout=timeout_value,
                env_extra=self._shell_env_extra(),
            )
        except Exception as e:
            self.ctx.log.warning("run_shell 工具执行异常: %s", e)
            result = {"error": "执行 shell 失败，稍后再试。"}
        return json.dumps(result, ensure_ascii=False)

    def _execute_skill_tool(self, call: dict) -> str:
        """执行一次 read_skill 调用，返回技能内容（截断 20KB）。"""
        try:
            arguments = json.loads(str(call.get("arguments") or "{}"))
        except Exception:
            return json.dumps({"error": "工具参数不是合法 JSON"}, ensure_ascii=False)
        if not isinstance(arguments, dict):
            return json.dumps({"error": "工具参数格式不对"}, ensure_ascii=False)
        name = str(arguments.get("name") or "").strip()
        if not name:
            return json.dumps({"error": "需要 name 参数"}, ensure_ascii=False)
        try:
            content = skills_mod.read_skill(name, self.ctx.data_dir)
        except Exception as e:
            self.ctx.log.warning("read_skill 工具执行异常: %s", e)
            content = None
        if content is None:
            return json.dumps(
                {"error": f"没找到技能：{name}"}, ensure_ascii=False
            )
        limit = 20 * 1024
        truncated = len(content) > limit
        return json.dumps(
            {
                "name": name,
                "content": content[:limit],
                "truncated": truncated,
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _image_segment(data: bytes) -> dict:
        """把图片压成 JPEG + base64 消息段（参考 image_gen 插件的做法）。"""
        payload = bytes(data or b"")
        try:
            from PIL import Image

            image = Image.open(io.BytesIO(payload))
            image = image.convert("RGB")
            if max(image.size) > 1600:
                image.thumbnail((1600, 1600))
            out = io.BytesIO()
            image.save(out, format="JPEG", quality=88)
            payload = out.getvalue()
        except Exception:
            pass
        return {
            "type": "image",
            "data": {"file": "base64://" + base64.b64encode(payload).decode("ascii")},
        }

    @staticmethod
    def _looks_like_avatar_request(text) -> bool:
        raw = str(text or "").strip()
        return bool(raw) and bool(AVATAR_REQUEST_RE.search(raw))

    @staticmethod
    def _avatar_cfg(cfg: dict) -> dict:
        value = cfg.get("avatar") if isinstance(cfg, dict) else None
        return value if isinstance(value, dict) else {}

    def _avatar_reference_path(self, cfg: dict) -> Path | None:
        """avatar.reference_image 指向的本地参考图（不存在返回 None）。"""
        reference = str(self._avatar_cfg(cfg).get("reference_image") or "").strip()
        if not reference:
            return None
        path = Path(reference)
        if not path.is_absolute():
            path = AVATAR_BASE_DIR / reference
        try:
            return path if path.is_file() else None
        except OSError:
            return None

    def _generate_avatar_reference_sync(
        self,
        ai_cfg: dict,
        image_cfg: dict,
        prompt: str,
        reference_path: Path,
        size: str,
        timeout: float,
    ) -> list[bytes]:
        """OpenAI 兼容 /images/edits（multipart）按参考图生成；失败抛异常由上层回退。"""
        decision = imagegen.resolve_image_mode(ai_cfg, image_cfg)
        if decision.get("mode") != "api":
            raise RuntimeError("当前画图方式（账号额度）不支持参考图")
        provider = str(decision.get("provider") or "openai").strip().lower()
        if provider != "openai":
            raise RuntimeError("当前画图 provider 不支持参考图")
        api_key = str(decision.get("api_key") or "")
        model = str(decision.get("model") or imagegen.DEFAULT_IMAGE_MODEL)
        base = str(decision.get("base_url") or imagegen.DEFAULT_IMAGE_BASE_URL)
        endpoint = imagegen._openai_endpoint(base).rsplit("/", 1)[0] + "/edits"
        raw = reference_path.read_bytes()
        body, boundary = _multipart_body(
            {"model": model, "prompt": prompt, "size": size},
            [("image", reference_path.name or "reference.png", "image/png", raw)],
        )
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
            "User-Agent": "qqbot-avatar/1.0",
        }
        request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=float(timeout)) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"参考图接口返回 HTTP {e.code}") from None
        except Exception:
            raise RuntimeError("参考图接口网络错误") from None
        items = payload.get("data") if isinstance(payload, dict) else None
        images: list[bytes] = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            encoded = item.get("b64_json") or item.get("b64")
            if isinstance(encoded, str) and encoded.strip():
                try:
                    images.append(base64.b64decode(encoded.split(",", 1)[-1]))
                except Exception:
                    continue
                continue
            link = item.get("url")
            if isinstance(link, str) and link.startswith(("http://", "https://")):
                try:
                    with urllib.request.urlopen(link, timeout=float(timeout)) as r:
                        images.append(r.read())
                except Exception:
                    continue
        if not images:
            raise RuntimeError("参考图接口没有返回图片")
        return images[:2]

    async def _execute_image_tool(
        self, event, cfg: dict, call: dict, user_text: str = ""
    ) -> str:
        """执行一次 generate_image 调用（原生 tools 路径）。"""
        try:
            arguments = json.loads(str(call.get("arguments") or "{}"))
        except Exception:
            return json.dumps({"error": "工具参数不是合法 JSON"}, ensure_ascii=False)
        if not isinstance(arguments, dict):
            return json.dumps({"error": "工具参数格式不对"}, ensure_ascii=False)
        return await self._perform_image_tool(event, cfg, arguments, user_text)

    async def _perform_image_tool(
        self, event, cfg: dict, arguments: dict, user_text: str = ""
    ) -> str:
        """按参数生成图片 -> 直接发到当前会话 -> 返回 JSON 文本。"""
        prompt = str(arguments.get("prompt") or "").strip()
        if not prompt:
            return json.dumps({"error": "需要 prompt 参数"}, ensure_ascii=False)
        image_cfg = self._image_gen_cfg()
        size = str(arguments.get("size") or "").strip()
        if size not in IMAGE_SIZES:
            size = str(image_cfg.get("size") or "").strip()
        if size not in IMAGE_SIZES:
            size = "1024x1024"
        try:
            timeout = float(image_cfg.get("timeout_seconds") or 120)
        except (TypeError, ValueError):
            timeout = 120.0

        # 机器人形象：只有形象/头像/立绘类请求才拼形象提示词、用参考图
        avatar = self._avatar_cfg(cfg)
        avatar_prompt = str(avatar.get("prompt") or "").strip()
        avatar_request = self._looks_like_avatar_request(
            user_text
        ) or self._looks_like_avatar_request(prompt)
        if avatar_prompt and avatar_request and avatar_prompt not in prompt:
            prompt = f"{avatar_prompt}，{prompt}" if prompt else avatar_prompt
        reference_path = (
            self._avatar_reference_path(cfg) if avatar_request else None
        )
        images = None
        if reference_path is not None:
            try:
                images = await asyncio.to_thread(
                    self._generate_avatar_reference_sync,
                    cfg,
                    image_cfg,
                    prompt,
                    reference_path,
                    size,
                    timeout,
                )
                if images:
                    self.ctx.log.info("机器人形象：使用参考图生成 %d 张", len(images))
            except Exception as e:
                self.ctx.log.warning("参考图生图失败，回退纯文生图: %s", e)
                images = None
        if not images:
            try:
                images = await asyncio.to_thread(
                    imagegen.generate_for_ai, cfg, image_cfg, prompt, size, timeout
                )
            except imagegen.ImageGenError as e:
                self.ctx.log.warning("generate_image 工具失败: %s", e)
                return json.dumps({"error": str(e)}, ensure_ascii=False)
            except Exception as e:
                self.ctx.log.warning("generate_image 工具异常: %s", e)
                return json.dumps({"error": "画图失败，稍后再试"}, ensure_ascii=False)
        if not images:
            return json.dumps({"error": "画图接口没有返回图片"}, ensure_ascii=False)
        segments = [self._image_segment(data) for data in images[:2]]
        try:
            if event.get("message_type") == "group":
                await self.ctx.send_group(event.get("group_id"), segments)
            else:
                await self.ctx.send_private(event.get("user_id"), segments)
        except Exception as e:
            self.ctx.log.warning("发送 AI 生成的图片失败: %s", e)
            return json.dumps(
                {"error": "图片生成好了但发送失败"}, ensure_ascii=False
            )
        self.ctx.log.info("AI 画图完成 %d 张: %s", len(segments), prompt[:40])
        return json.dumps(
            {
                "ok": True,
                "count": len(segments),
                "prompt": prompt,
                "note": "图片已发送",
            },
            ensure_ascii=False,
        )

    # ---------------- 账号/非 OpenAI 模式的意图路由 ----------------
    def _sync_route_chat(
        self,
        cfg: dict,
        messages: list,
        temperature: float = 0.0,
        max_tokens: int = ROUTE_MAX_TOKENS,
    ) -> str:
        """轻量模型调用：工具路由默认 temperature=0，改写提示词时用 0.7。"""
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
            temperature=float(temperature),
            max_tokens=int(max_tokens),
            timeout=float(cfg.get("timeout_seconds", 60)),
            auth=auth,
        )

    @staticmethod
    def _parse_route_json(text) -> dict | None:
        """从模型输出里取第一个完整 JSON 对象（容忍 ```json 围栏/前后文字）。"""
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

    @staticmethod
    def _image_prompt_weak(candidate: str, user_text: str) -> bool:
        """路由给的生图 prompt 是否太敷衍：空/过短/基本复述用户原话。"""
        text = str(candidate or "").strip()
        original = str(user_text or "").strip()
        if len(text) < 4:
            return True
        if not original:
            return False
        if text == original:
            return True
        return text in original or original in text

    async def _rewrite_image_prompt(self, cfg: dict, user_text: str) -> str:
        """让主模型把用户需求改写成一句具体生图提示词；失败返回空串。"""
        messages = [
            {"role": "system", "content": IMAGE_PROMPT_REWRITE_SYSTEM},
            {"role": "user", "content": str(user_text or "").strip()},
        ]
        try:
            raw = await asyncio.to_thread(
                self._sync_route_chat, cfg, messages, 0.7, 300
            )
        except providers.ProviderError as e:
            self.ctx.log.info("生图提示词改写失败，退回用户原话: %s", e)
            return ""
        text = str(raw or "").strip()
        # 容忍模型偶尔加代码块/引号
        text = re.sub(r"^```[A-Za-z0-9_-]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
        text = text.strip("\"'“”「」").strip()
        if len(text) < 4:
            return ""
        return text[:400]

    @staticmethod
    def _looks_like_image_request(text, history=None) -> bool:
        """确定性判断：用户这句话是不是在要图片（画张/来一张/壁纸/使用画图功能等）。

        用户只说「继续/再来一张」时，结合最近历史里是否有画图需求来判断。
        """
        raw = str(text or "").strip()
        if not raw:
            return False
        if IMAGE_INTENT_RE.search(raw):
            return True
        if history and CONTEXT_IMAGE_RE.search(raw):
            for item in list(history)[-6:]:
                if (
                    isinstance(item, dict)
                    and item.get("role") == "user"
                    and IMAGE_INTENT_RE.search(str(item.get("content") or ""))
                ):
                    return True
        return False

    async def _write_image_prompt_from_history(
        self, cfg: dict, history: list, user_text: str
    ) -> str:
        """结合最近对话历史，让主模型写出生图提示词；失败返回空串。"""
        messages: list[dict] = [
            {"role": "system", "content": IMAGE_PROMPT_FROM_HISTORY_SYSTEM}
        ]
        for item in (history or [])[-6:]:
            if isinstance(item, dict) and item.get("role") in ("user", "assistant"):
                content = str(item.get("content") or "").strip()
                if content:
                    messages.append({"role": item["role"], "content": content})
        messages.append(
            {
                "role": "user",
                "content": "用户这次的画图需求：" + str(user_text or "").strip(),
            }
        )
        try:
            raw = await asyncio.to_thread(
                self._sync_route_chat, cfg, messages, 0.7, 300
            )
        except providers.ProviderError as e:
            self.ctx.log.info("生图提示词生成失败，退回用户原话: %s", e)
            return ""
        text = str(raw or "").strip()
        text = re.sub(r"^```[A-Za-z0-9_-]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
        text = text.strip("\"'“”「」").strip()
        if len(text) < 4:
            return ""
        return text[:400]

    async def _force_image_chat(
        self,
        event,
        cfg: dict,
        messages: list,
        history: list,
        prompt: str,
    ) -> str | None:
        """生图意图兜底：写提示词 -> generate_image -> 发图 -> 主模型描述。

        走账号/非 OpenAI 路由模式的确定性路径；生成失败返回 None 交给普通聊天。
        """
        image_cfg = self._image_gen_cfg()
        image_prompt = await self._write_image_prompt_from_history(
            cfg, history, prompt
        )
        if not image_prompt:
            image_prompt = str(prompt or "").strip()
            if not IMAGE_INTENT_RE.search(image_prompt):
                # 用户只说「继续」：沿用历史里最近一条画图需求
                for item in reversed(list(history or [])[-6:]):
                    if (
                        isinstance(item, dict)
                        and item.get("role") == "user"
                        and IMAGE_INTENT_RE.search(str(item.get("content") or ""))
                    ):
                        image_prompt = str(item.get("content") or "").strip()
                        break
            if not image_prompt:
                image_prompt = "一张风景插画"
        size = str(image_cfg.get("size") or "").strip()
        if size not in IMAGE_SIZES:
            size = "1024x1024"
        output = await self._perform_image_tool(
            event, cfg, {"prompt": image_prompt, "size": size}, user_text=prompt
        )
        try:
            parsed = json.loads(output)
        except Exception:
            parsed = None
        if not isinstance(parsed, dict) or not parsed.get("ok"):
            self.ctx.log.info("强制生图失败，退回普通聊天: %s", str(output)[:120])
            return None
        instruction = (
            f"【工具执行结果】generate_image：{output}\n"
            "图片已发送。请用简短中文告诉用户画好了，并用一两句描述画面；"
            "不要重复提示词原文，也不要说自己不能画图；"
            "绝对不要用 ASCII/emoji/代码画图，禁止用 SVG 或代码块冒充图片。"
        )
        final_messages = list(messages) + [
            {"role": "system", "content": instruction}
        ]
        try:
            final = await asyncio.to_thread(
                self._sync_chat, cfg, final_messages
            )
        except providers.ProviderError as e:
            self.ctx.log.info("画图后的描述生成失败，用兜底文案: %s", e)
            return IMAGE_PROMPT_FALLBACK_REPLY
        text = str(final or "").strip()
        return text or IMAGE_PROMPT_FALLBACK_REPLY

    async def _route_tool_chat(
        self,
        event,
        cfg: dict,
        messages: list,
        history: list,
        prompt: str,
        image_ok: bool,
        python_ok: bool,
        shell_ok: bool = False,
        skills_ok: bool = False,
    ) -> str | None:
        """账号/非 OpenAI 模式的「意图路由」：一次轻量 chat 判断要不要用工具。

        返回最终文字回复；路由失败/不需要工具返回 None（调用方走普通聊天）。
        """
        tools = []
        examples = []
        if image_ok:
            tools.append("generate_image（用户要画图/生成图片）")
            examples.append(
                '· 需要画图：{"tool":"generate_image",'
                '"prompt":"你自己写的具体生图提示词","size":"1024x1024"}'
            )
        if python_ok:
            tools.append("run_python（用户要运行代码/脚本）")
            examples.append(
                '· 需要运行 Python：{"tool":"run_python","code":"python 源码"} 或 '
                '{"tool":"run_python","file":"data/files 下的 .py 文件名"}'
            )
        if shell_ok:
            tools.append("run_shell（用户要执行 shell 命令/apt/git/长任务）")
            examples.append(
                '· 需要执行命令：{"tool":"run_shell","command":"shell 命令"}'
            )
        if skills_ok:
            tools.append("read_skill（需要某个技能的完整说明）")
            examples.append(
                '· 需要读技能：{"tool":"read_skill","name":"技能名"}'
            )
        if not tools:
            return None
        route_messages = [
            {
                "role": "system",
                "content": (
                    ROUTE_SYSTEM_HEAD
                    + "\n".join(tools)
                    + ROUTE_SYSTEM_FORMAT
                    + "\n".join(examples)
                    + "\n"
                    + ROUTE_SYSTEM_NONE
                ),
            }
        ]
        for item in (history or [])[-ROUTE_MAX_HISTORY:]:
            if isinstance(item, dict) and item.get("role") in ("user", "assistant"):
                route_messages.append(
                    {"role": item["role"], "content": str(item.get("content") or "")}
                )
        route_messages.append({"role": "user", "content": str(prompt or "")})
        try:
            raw = await asyncio.to_thread(self._sync_route_chat, cfg, route_messages)
        except providers.ProviderError as e:
            self.ctx.log.info("工具路由调用失败，按普通聊天处理: %s", e)
            return None
        decision = self._parse_route_json(raw)
        tool = (
            str(decision.get("tool") or "").strip().lower()
            if isinstance(decision, dict)
            else ""
        )
        if tool == "generate_image" and image_ok:
            arguments = {k: v for k, v in decision.items() if k != "tool"}
            route_prompt = str(arguments.get("prompt") or "").strip()
            image_prompt = route_prompt
            if self._image_prompt_weak(route_prompt, prompt):
                # 路由只是复述/太短：让主模型自己写一版具体生图提示词
                rewritten = await self._rewrite_image_prompt(cfg, prompt)
                if rewritten:
                    image_prompt = rewritten
                elif str(prompt or "").strip():
                    image_prompt = str(prompt).strip()
                self.ctx.log.info(
                    "生图提示词已改写: %r -> %r",
                    route_prompt[:30],
                    image_prompt[:40],
                )
            arguments["prompt"] = image_prompt
            self.ctx.log.info("工具路由命中 generate_image")
            output = await self._perform_image_tool(
                event, cfg, arguments, user_text=prompt
            )
            result_label = "generate_image"
        elif tool == "run_python" and python_ok:
            arguments = {k: v for k, v in decision.items() if k != "tool"}
            self.ctx.log.info("工具路由命中 run_python")
            call = {
                "name": "run_python",
                "arguments": json.dumps(arguments, ensure_ascii=False),
            }
            output = await asyncio.to_thread(self._execute_python_tool, call)
            result_label = "run_python"
        elif tool == "run_shell" and shell_ok:
            arguments = {k: v for k, v in decision.items() if k != "tool"}
            self.ctx.log.info("工具路由命中 run_shell")
            call = {
                "name": "run_shell",
                "arguments": json.dumps(arguments, ensure_ascii=False),
            }
            output = await asyncio.to_thread(self._execute_shell_tool, cfg, call)
            result_label = "run_shell"
        elif tool == "read_skill" and skills_ok:
            arguments = {k: v for k, v in decision.items() if k != "tool"}
            self.ctx.log.info("工具路由命中 read_skill")
            call = {
                "name": "read_skill",
                "arguments": json.dumps(arguments, ensure_ascii=False),
            }
            output = await asyncio.to_thread(self._execute_skill_tool, call)
            result_label = "read_skill"
        else:
            if tool and tool != "none":
                self.ctx.log.info("工具路由给出不可用的工具 %s，按普通聊天处理", tool)
            return None
        if result_label == "generate_image":
            instruction = (
                f"【工具执行结果】generate_image：{output}\n"
                "图片已发送。请用简短中文告诉用户画好了，并用一两句描述画面；"
                "不要重复提示词原文，也不要说自己不能画图。"
            )
        else:
            instruction = (
                f"【工具执行结果】{result_label}：{output}\n"
                "请根据以上结果用中文回复用户；不要再重复发送图片，"
                "也不要说自己不能运行工具。"
            )
        final_messages = list(messages) + [
            {"role": "system", "content": instruction}
        ]
        try:
            final = await asyncio.to_thread(self._sync_chat, cfg, final_messages)
        except providers.ProviderError as e:
            if result_label == "generate_image":
                self.ctx.log.info("画图后的最终回复失败，用兜底文案: %s", e)
                return IMAGE_PROMPT_FALLBACK_REPLY
            self.ctx.log.info("工具结果后的最终回复失败，按普通聊天处理: %s", e)
            return None
        text = str(final or "").strip()
        if text:
            return text
        if result_label == "generate_image":
            return IMAGE_PROMPT_FALLBACK_REPLY
        return None

    async def _tool_chat(self, event, cfg: dict, messages: list, tools: list) -> str:
        """最多 3 轮工具循环：tool_calls -> 执行 -> role=tool 结果回给模型。"""
        working = [dict(item) for item in messages]
        user_text = ""
        for item in reversed(messages):
            if isinstance(item, dict) and item.get("role") == "user":
                user_text = str(item.get("content") or "")
                break
        last_content = ""
        had_image_result = False
        for round_index in range(MAX_TOOL_ROUNDS):
            result = await asyncio.to_thread(
                self._sync_chat_with_tools, cfg, working, tools
            )
            content = str(result.get("content") or "").strip()
            calls = [c for c in (result.get("tool_calls") or []) if isinstance(c, dict)]
            if content:
                last_content = content
            if not calls:
                if not content and had_image_result:
                    return IMAGE_PROMPT_FALLBACK_REPLY
                return content
            assistant_calls = []
            for index, call in enumerate(calls):
                call_id = str(call.get("id") or f"call_{round_index}_{index}")
                assistant_calls.append(
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": str(call.get("name") or PYTHON_TOOL_NAME),
                            "arguments": str(call.get("arguments") or "{}"),
                        },
                    }
                )
            working.append(
                {
                    "role": "assistant",
                    "content": content or None,
                    "tool_calls": assistant_calls,
                }
            )
            for index, call in enumerate(calls):
                call_id = assistant_calls[index]["id"]
                name = str(call.get("name") or "").strip()
                if name == IMAGE_TOOL_NAME:
                    output = await self._execute_image_tool(
                        event, cfg, call, user_text
                    )
                    try:
                        parsed = json.loads(output)
                    except Exception:
                        parsed = None
                    if isinstance(parsed, dict) and parsed.get("ok"):
                        had_image_result = True
                elif name == SHELL_TOOL_NAME:
                    output = await asyncio.to_thread(
                        self._execute_shell_tool, cfg, call
                    )
                elif name == SKILL_TOOL_NAME:
                    output = await asyncio.to_thread(self._execute_skill_tool, call)
                else:
                    output = await asyncio.to_thread(self._execute_python_tool, call)
                working.append(
                    {"role": "tool", "tool_call_id": call_id, "content": output}
                )
        # 3 轮之后还没收尾：不再提供工具，让模型直接回答
        final = await asyncio.to_thread(
            self._sync_chat_with_tools, cfg, working, None
        )
        text = str(final.get("content") or "").strip() or last_content
        if not text and had_image_result:
            return IMAGE_PROMPT_FALLBACK_REPLY
        return text

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

        # 原生 function calling 只支持 OpenAI 兼容 + API Key；账号/其它 provider 用意图路由。
        native_supported = self._native_tools_supported(cfg)
        python_ok = self._python_tool_base_available() and await self._can_use_tools(event)
        image_ok = await self._image_tool_available(event, cfg)
        shell_ok = await self._shell_tool_available(event)
        skills_enabled = self._skills_enabled()
        skills_list = self._skills_list() if skills_enabled else []
        # 原生 tools：skills 只要没被禁用就提供 read_skill（目录为空时读会返回没找到）；
        # 意图路由只在技能目录确实有内容时才加入 read_skill，避免无意义的多一轮路由。
        skills_route = skills_enabled and bool(skills_list)
        tools: list = []
        if native_supported:
            if python_ok:
                tools.append(PYTHON_TOOL_SPEC)
            if image_ok:
                tools.append(IMAGE_TOOL_SPEC)
            if shell_ok:
                tools.append(SHELL_TOOL_SPEC)
            if skills_enabled:
                tools.append(SKILL_TOOL_SPEC)

        system = self.ctx.format_text(cfg.get("system_prompt", ""), event, group_id=group_id)
        hints = []
        py_hint = local_python_hint(self.ctx, python_ok)
        if py_hint:
            hints.append(py_hint)
        if image_ok:
            hints.append(LOCAL_IMAGE_TOOL_HINT)
        if shell_ok:
            hints.append(LOCAL_SHELL_TOOL_HINT)
        if skills_list:
            skills_text = "\n".join(
                f"- {item.get('name')} — {item.get('description')}"
                for item in skills_list
            )
            hints.append(
                "【技能】可用技能：\n"
                + skills_text
                + "\n需要某个技能的完整说明时，调用 read_skill 工具。"
            )
        if hints:
            system = (str(system).rstrip() + "\n\n" + "\n\n".join(hints)).strip()
        official_meta = event.get("_official") or {}
        if (
            event.get("message_type") == "group"
            and isinstance(official_meta, dict)
            and official_meta.get("kind") == "group"
        ):
            try:
                self.ctx.log.info(
                    "AI 官方群触发 group_openid=%s addressed=%s system_prompt_len=%d",
                    official_meta.get("group_openid") or "",
                    mentioned(event.get("message"), self.ctx.self_id),
                    len(str(system or "")),
                )
            except Exception:
                pass

        history_messages = self._history(key)
        messages = [{"role": "system", "content": system}] + history_messages

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
        # 官方通道：去掉原始 @ 标记，并告诉模型说话人是谁（绑定过名字就叫名字）
        if isinstance(official_meta, dict) and official_meta:
            prompt = re.sub(r"<@!?[A-Za-z0-9_\-]{6,}>|<qqbot-at-user[^>]*/?>", " ", str(prompt or "")).strip() or prompt
            speaker = str((event.get("sender") or {}).get("nickname") or "").strip()
            if speaker and speaker not in ("群友", "朋友"):
                messages.append({"role": "system", "content": f"当前和你说话的人叫「{speaker}」，称呼对方时用这个名字。"})
        messages.append({"role": "user", "content": prompt})

        try:
            if tools:
                try:
                    answer = await self._tool_chat(event, cfg, messages, tools)
                except providers.ProviderError as e:
                    # 有的 OpenAI 兼容中转/本地模型不接受 tools 参数：退回普通聊天，
                    # 不因此让原本能用的对话失败
                    self.ctx.log.warning("工具调用失败，退回普通聊天: %s", e)
                    answer = await asyncio.to_thread(self._sync_chat, cfg, messages)
            elif (
                not native_supported
                and image_ok
                and self._looks_like_image_request(prompt, history_messages)
            ):
                # 生图意图确定性兜底：路由模型不可靠时直接走画图流程，
                # 避免把「画张风景图」答成 ASCII/emoji 文字画。
                forced = await self._force_image_chat(
                    event, cfg, messages, history_messages, prompt
                )
                if forced is not None:
                    answer = forced
                else:
                    answer = await asyncio.to_thread(self._sync_chat, cfg, messages)
            elif not native_supported and (
                python_ok or image_ok or shell_ok or skills_route
            ):
                # 账号 / 非 OpenAI 模式：用一次轻量「意图路由」让模型自己决定要不要用工具
                routed = await self._route_tool_chat(
                    event,
                    cfg,
                    messages,
                    history_messages,
                    prompt,
                    image_ok,
                    python_ok,
                    shell_ok,
                    skills_route,
                )
                if routed:
                    answer = routed
                else:
                    answer = await asyncio.to_thread(self._sync_chat, cfg, messages)
            else:
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
