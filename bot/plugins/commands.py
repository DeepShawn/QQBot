"""管理员命令：/help /status /ping /reload /model /task（仅 bot.admins 里的 QQ 可用）。

- ``/model``：查看当前账号套餐可用模型并快速切换（也支持切换多套接口 profile）。
- ``/task <需求>``：后台长任务代理（实时汇报进度、缺环境自动安装），见 bot/agent.py。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

import agent
import official
import quota_refresh
from dispatcher import PluginBase
from utils import mentioned, strip_at, text_of


class Plugin(PluginBase):
    name = "commands"
    priority = 10

    def __init__(self, ctx):
        super().__init__(ctx)
        self.CONFIG_PATH = Path(getattr(ctx.config, "path", Path(__file__).resolve().parent.parent / "config.json"))
        self._tasks: dict[str, asyncio.Task] = {}
        self._commands = {
            "/help": self.cmd_help,
            "/status": self.cmd_status,
            "/ping": self.cmd_ping,
            "/reload": self.cmd_reload,
            "/model": self.cmd_model,
            "/task": self.cmd_task,
            "/刷新限额": self.cmd_refresh_quota,
            "/我叫": self.cmd_set_name,
        }

    # ---------- 读写原始 config.json（原子替换，避免和热加载打架） ----------
    def _load_raw(self) -> dict:
        try:
            return json.loads(self.CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_raw(self, data: dict):
        tmp = self.CONFIG_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.CONFIG_PATH)

    def _reload(self):
        try:
            if hasattr(self.ctx.config, "reload_if_changed"):
                self.ctx.config.reload_if_changed()
        except Exception:
            pass

    def _extract(self, event):
        if event.get("post_type") != "message":
            return None
        text = text_of(event.get("message")).strip()
        if event.get("message_type") == "group":
            if not mentioned(event.get("message"), self.ctx.self_id):
                return None
            text = text_of(strip_at(event.get("message"), self.ctx.self_id)).strip()
        return text

    _NAME_RE = re.compile(
        r"^(?:我叫|叫我|我的名字是|我的名字叫|以后叫我|你可以叫我)\s*[「“\"]?([^\s，,。！!？?」”\"、]{1,20})"
    )

    def _auto_bind_name(self, event, text: str) -> None:
        """官方通道：用户自然地说「我叫X / 叫我X」时静默绑定昵称，不吞消息（AI 照常回复）。"""
        meta = event.get("_official") or {}
        if not isinstance(meta, dict) or not meta:
            return
        openid = str(meta.get("member_openid") or meta.get("user_openid") or "")
        if not openid:
            return
        m = self._NAME_RE.match(text.strip())
        if not m:
            return
        name = m.group(1)
        for tail in ("你记住", "记住", "你记好", "哦", "啊", "呀", "吧"):
            if name.endswith(tail) and len(name) > len(tail):
                name = name[: -len(tail)]
        name = name.strip()
        # 「我叫你吃饭」这类是动词「叫」，不是报名字
        if not name or name[0] in "你他她它们大家" :
            return
        try:
            official.set_nickname(self.ctx.data_dir, openid, name)
            event.setdefault("sender", {})["nickname"] = name
            self.ctx.log.info("官方通道自动绑定昵称 openid=%s… name=%s", openid[:6], name)
        except Exception as e:
            self.ctx.log.warning("自动绑定昵称失败: %s", e)

    async def on_event(self, event) -> bool:
        text = self._extract(event)
        if text and not text.startswith("/"):
            self._auto_bind_name(event, text)
        if not text or not text.startswith("/"):
            return False
        cmd = text.split()[0].lower()
        handler = self._commands.get(cmd)
        if handler is None:
            return False
        if cmd == "/task":
            # /task 与其它工具同权限：私聊直接；群里 bot.admins 或群主/群管理员
            if not await agent.can_use(self.ctx, event):
                await self.ctx.reply(event, "这条命令只有管理员能用。")
                return True
        elif cmd not in ("/我叫", "/help", "/帮助") and not self.ctx.is_admin(event.get("user_id")):
            # /我叫 是任何成员都能用的名字绑定，不做管理员门
            await self.ctx.reply(event, "这条命令只有管理员能用。")
            return True
        await handler(event, text.split()[1:])
        return True

    # ---------- /model ----------
    @staticmethod
    def _account_models(auth: str) -> tuple[list[str], str]:
        """懒加载 accounts_<auth> 取套餐模型列表 -> (models, 错误文案)。"""
        auth = str(auth or "").strip().lower()
        if not auth:
            return [], ""
        try:
            import importlib

            module = importlib.import_module(f"accounts_{auth}")
        except Exception:
            return [], f"账号模块 accounts_{auth} 不可用"
        try:
            models = [
                str(item)
                for item in (getattr(module, "MODELS", None) or [])
                if str(item or "").strip()
            ]
        except Exception:
            models = []
        if not models:
            return [], f"这个账号（{auth}）没有可用模型列表"
        return models, ""

    @staticmethod
    def _current_profile(profiles: list[dict], ai: dict) -> dict | None:
        active = str(ai.get("active_profile") or "")
        chosen = next((p for p in profiles if str(p.get("id")) == active), None)
        if chosen is None and profiles:
            chosen = profiles[0]
        return chosen

    @staticmethod
    def _ai_auth(ai: dict, profile: dict | None) -> str:
        if profile is not None and "auth" in profile:
            return str(profile.get("auth") or "").strip()
        return str(ai.get("auth") or "").strip()

    @staticmethod
    def _ai_model(ai: dict, profile: dict | None) -> str:
        if profile is not None and str(profile.get("model") or "").strip():
            return str(profile.get("model")).strip()
        return str(ai.get("model") or "").strip()

    @staticmethod
    def _interface_name(ai: dict, profile: dict | None) -> str:
        if profile is not None:
            return str(profile.get("name") or profile.get("id") or "当前接口")
        return str(ai.get("provider") or "默认接口")

    async def cmd_model(self, event, args):
        """查看 / 切换模型：账号套餐模型 或 多套接口。用法：/model、/model 2、/model 模型名"""
        raw = self._load_raw()
        ai = raw.setdefault("plugins", {}).setdefault("ai_chat", {})
        profiles = [p for p in (ai.get("profiles") or []) if isinstance(p, dict)]
        profile = self._current_profile(profiles, ai)
        auth = self._ai_auth(ai, profile)
        model = self._ai_model(ai, profile)
        models, model_error = self._account_models(auth) if auth else ([], "")

        if not args:
            lines = []
            lines.append(f"当前接口：{self._interface_name(ai, profile)}")
            if auth:
                lines.append(f"认证方式：账号（{auth}）")
                lines.append(f"当前模型：{model or '账号默认'}")
                if models:
                    lines.append("")
                    lines.append("可用模型：")
                    for index, name in enumerate(models, 1):
                        mark = "✅" if name == model else "　"
                        lines.append(f"{mark} {index}. {name}")
                    lines.append("")
                    lines.append("切换模型：/model 序号 或 /model 模型名")
                elif model_error:
                    lines.append(f"（{model_error}）")
            else:
                lines.append(f"当前模型：{model or '未设置'}")
            if profiles:
                lines.append("")
                lines.append("接口列表：")
                active_id = str(profile.get("id")) if profile is not None else ""
                for index, item in enumerate(profiles, 1):
                    mark = "✅" if str(item.get("id")) == active_id else "　"
                    key = "已填key" if str(item.get("api_key") or "").strip() else "无key"
                    lines.append(
                        f"{mark} {index}. {item.get('name') or item.get('id')} · "
                        f"{item.get('model') or '-'} · {key}"
                    )
                lines.append("")
                lines.append("切换接口：/model 接口名称（账号模式下数字优先切模型）")
            elif not auth:
                lines.append("还没有配置多套接口，可以在控制台 App 的「AI」页添加。")
            await self.ctx.reply(event, "\n".join(lines))
            return

        target = str(args[0] or "").strip()

        # 账号套餐模型优先：序号或模型名
        if auth and models:
            chosen_model = None
            if target.isdigit() and 1 <= int(target) <= len(models):
                chosen_model = models[int(target) - 1]
            else:
                lowered = target.lower()
                for name in models:
                    if lowered == name.lower():
                        chosen_model = name
                        break
            if chosen_model is not None:
                if profile is not None:
                    profile["model"] = chosen_model
                else:
                    ai["model"] = chosen_model
                self._save_raw(raw)
                self._reload()
                await self.ctx.reply(event, f"✅ 已切换模型：{chosen_model}")
                return

        # 不是套餐模型 -> 保留原有的「切换接口 profile」逻辑
        if not profiles:
            message = f"没找到模型「{target}」"
            if auth:
                message += f"（当前账号 {auth} 的模型列表可用 /model 查看）"
            else:
                message += "，也没有配置多套接口"
            await self.ctx.reply(event, message)
            return
        chosen_profile = None
        if target.isdigit() and 1 <= int(target) <= len(profiles):
            chosen_profile = profiles[int(target) - 1]
        else:
            lowered = target.lower()
            for item in profiles:
                if lowered in (
                    str(item.get("id")).lower(),
                    str(item.get("name")).lower(),
                ):
                    chosen_profile = item
                    break
        if chosen_profile is None:
            await self.ctx.reply(event, f"没找到接口或模型「{target}」，发 /model 看列表。")
            return
        ai["active_profile"] = chosen_profile.get("id")
        self._save_raw(raw)
        self._reload()
        await self.ctx.reply(
            event,
            f"✅ 已切换到「{chosen_profile.get('name')}」（{chosen_profile.get('model')}）\n"
            f"接口地址：{chosen_profile.get('base_url') or '跟随默认'}",
        )

    # ---------- /task ----------
    async def cmd_task(self, event, args):
        """长任务：/task <需求>；/task stop 停止当前会话任务。"""
        text = " ".join(str(item) for item in (args or [])).strip()
        key = agent.task_key(event)
        running = self._tasks.get(key)
        if running is not None and running.done():
            self._tasks.pop(key, None)
            running = None
        if text.lower() in ("stop", "停止", "取消"):
            if running is not None and not running.done():
                running.cancel()
                await self.ctx.reply(event, "🛑 已停止当前任务。")
            else:
                await self.ctx.reply(event, "没有正在运行的任务。")
            return
        if not text:
            await self.ctx.reply(
                event, "用法：/task <需求>（发 /task stop 停止当前任务）"
            )
            return
        if running is not None and not running.done():
            await self.ctx.reply(
                event, "这个会话已有任务在跑，发 /task stop 停止后再试。"
            )
            return
        await self.ctx.reply(event, "🚀 任务已开始…（发 /task stop 可停止）")
        task = asyncio.create_task(agent.run_task(self.ctx, event, text))
        self._tasks[key] = task

        def _cleanup(done_task, task_key=key):
            if self._tasks.get(task_key) is done_task:
                self._tasks.pop(task_key, None)

        task.add_done_callback(_cleanup)

    # ---------- /我叫 ----------
    async def cmd_set_name(self, event, args):
        """官方通道里绑定/修改自己的昵称（任何成员可用）。"""
        official_meta = event.get("_official") or {}
        kind = (
            str(official_meta.get("kind") or "")
            if isinstance(official_meta, dict)
            else ""
        )
        openid = ""
        if kind == "group":
            openid = str(official_meta.get("member_openid") or "")
        elif kind == "c2c":
            openid = str(official_meta.get("user_openid") or "")
        if not openid:
            await self.ctx.reply(
                event, "这个命令只在官方通道可用（OneBot 模式用平台昵称）。"
            )
            return
        name = " ".join(str(item) for item in (args or [])).strip()
        if not name:
            current = official.get_nickname(self.ctx.data_dir, openid)
            if current:
                await self.ctx.reply(
                    event,
                    f"你现在的名字是「{current}」，改名字：/我叫 <新名字>",
                )
            else:
                await self.ctx.reply(event, "还没有绑定名字。用法：/我叫 <名字>（1-20 个字符）")
            return
        if len(name) > 20:
            await self.ctx.reply(event, "名字太长了，最多 20 个字符。")
            return
        if any(ch in name for ch in "\r\n\t"):
            await self.ctx.reply(event, "名字里不能有换行/制表符。")
            return
        try:
            saved = official.set_nickname(self.ctx.data_dir, openid, name)
        except Exception as e:
            self.ctx.log.warning("保存昵称失败: %s", e)
            await self.ctx.reply(event, "保存名字失败了，稍后再试。")
            return
        await self.ctx.reply(event, f"✅ 好的，以后叫你「{saved}」")

    # ---------- /刷新限额 ----------
    async def cmd_refresh_quota(self, event, args):
        """立即刷新 Claude/ChatGPT 的 5 小时限额（内部发你好，不往 QQ 发消息）。"""
        try:
            result = await quota_refresh.tick(self.ctx, force=True)
        except Exception as e:
            self.ctx.log.warning("手动刷新限额失败: %s", e)
            await self.ctx.reply(event, f"刷新限额失败：{e}")
            return
        skipped = result.get("skipped") or ""
        if skipped == "disabled":
            await self.ctx.reply(event, "限额刷新已在配置里关闭（plugins.quota_refresh.enabled）。")
            return
        if skipped == "no-accounts":
            await self.ctx.reply(event, "没有已登录的 Claude / ChatGPT 账号，先到 App 的「AI 与人格」里连接账号。")
            return
        lines = ["⏱ 限额刷新："]
        refreshed = result.get("refreshed") or []
        for pid in refreshed:
            lines.append(f"✅ {pid}：已刷新")
        for item in result.get("failed") or []:
            if isinstance(item, dict):
                lines.append(f"⚠️ {item.get('provider')}：{item.get('error') or '失败'}")
            else:
                lines.append(f"⚠️ {item}：失败")
        if not refreshed and not (result.get("failed") or []):
            lines.append("没有可刷新的账号")
        lines.append("（只在内部发「你好」，不会往 QQ 发消息）")
        await self.ctx.reply(event, "\n".join(lines))

    # ---------- 其它 ----------
    async def cmd_help(self, event, args):
        await self.ctx.reply(
            event,
            "管理员命令：\n"
            "/status 运行状态\n"
            "/model 查看/切换模型（账号套餐模型列表，或切换接口：/model 2）\n"
            "/task <需求> 长任务（实时汇报进度，缺环境自动安装）\n"
            "/task stop 停止当前长任务\n"
            "/刷新限额 立即刷新 Claude/ChatGPT 的 5 小时限额\n"
            "/我叫 <名字> 绑定官方通道里怎么称呼你\n"
            "/ping 延迟测试\n"
            "/reload 重载 config.json\n"
            "/help 本说明",
        )

    async def cmd_ping(self, event, args=None):
        await self.ctx.reply(event, "pong")

    async def cmd_status(self, event, args=None):
        started = self.ctx.started_at or time.time()
        uptime = int(time.time() - started)
        plugins = self.ctx.cfg.get("plugins") or {}
        enabled = [k for k, v in plugins.items() if isinstance(v, dict) and v.get("enabled")]
        connected = bool(getattr(self.ctx.api, "connected", None) and self.ctx.api.connected.is_set())
        lines = [
            f"机器人：{self.ctx.bot_name}",
            f"QQ：{self.ctx.self_id or '未知'}",
            f"OneBot：{'已连接' if connected else '未连接'}",
            f"插件：{', '.join(enabled) or '无'}",
            f"已运行：{uptime // 3600}h{(uptime % 3600) // 60}m",
        ]
        await self.ctx.reply(event, "\n".join(lines))

    async def cmd_reload(self, event, args=None):
        try:
            self.ctx.config.load()
            await self.ctx.reply(event, "config.json 已重载。")
        except Exception as e:
            await self.ctx.reply(event, f"重载失败：{e}")
