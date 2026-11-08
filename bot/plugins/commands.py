"""管理员命令：/help /status /ping /reload（仅 bot.admins 里的 QQ 可用）。"""
from __future__ import annotations

import json
import time
from pathlib import Path

from dispatcher import PluginBase
from utils import mentioned, strip_at, text_of


class Plugin(PluginBase):
    name = "commands"
    priority = 10

    def __init__(self, ctx):
        super().__init__(ctx)
        self.CONFIG_PATH = Path(getattr(ctx.config, "path", Path(__file__).resolve().parent.parent / "config.json"))
        self._commands = {
            "/help": self.cmd_help,
            "/status": self.cmd_status,
            "/ping": self.cmd_ping,
            "/reload": self.cmd_reload,
            "/model": self.cmd_model,
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

    def _extract(self, event):
        if event.get("post_type") != "message":
            return None
        text = text_of(event.get("message")).strip()
        if event.get("message_type") == "group":
            if not mentioned(event.get("message"), self.ctx.self_id):
                return None
            text = text_of(strip_at(event.get("message"), self.ctx.self_id)).strip()
        return text

    async def on_event(self, event) -> bool:
        text = self._extract(event)
        if not text or not text.startswith("/"):
            return False
        cmd = text.split()[0].lower()
        handler = self._commands.get(cmd)
        if handler is None:
            return False
        if not self.ctx.is_admin(event.get("user_id")):
            await self.ctx.reply(event, "这条命令只有管理员能用。")
            return True
        await handler(event, text.split()[1:])
        return True

    async def cmd_model(self, event, args):
        """查看 / 切换多套 AI 接口。用法：/model、/model 2、/model 名称"""
        raw = self._load_raw()
        ai = raw.setdefault("plugins", {}).setdefault("ai_chat", {})
        profiles = [p for p in (ai.get("profiles") or []) if isinstance(p, dict)]
        if not profiles:
            await self.ctx.reply(event, "还没有配置多套接口，可以在控制台 App 的「AI」页添加。")
            return
        active = str(ai.get("active_profile") or "")
        if not args:
            lines = [f"当前接口：{next((p.get('name') for p in profiles if str(p.get('id')) == active), active or '未设置')}", ""]
            for i, p in enumerate(profiles, 1):
                mark = "✅" if str(p.get("id")) == active else "　"
                key = "已填key" if str(p.get("api_key") or "").strip() else "无key"
                lines.append(f"{mark} {i}. {p.get('name') or p.get('id')} · {p.get('model') or '-'} · {key}")
            lines.append("")
            lines.append("切换：/model 序号（或 /model 名称）")
            await self.ctx.reply(event, "\n".join(lines))
            return

        target = args[0].strip()
        chosen = None
        if target.isdigit() and 1 <= int(target) <= len(profiles):
            chosen = profiles[int(target) - 1]
        else:
            t = target.lower()
            for p in profiles:
                if t in (str(p.get("id")).lower(), str(p.get("name")).lower()):
                    chosen = p
                    break
        if chosen is None:
            await self.ctx.reply(event, f"没找到接口「{target}」，发 /model 看列表。")
            return
        ai["active_profile"] = chosen.get("id")
        self._save_raw(raw)
        self.ctx.config.reload_if_changed() if hasattr(self.ctx.config, "reload_if_changed") else None
        await self.ctx.reply(
            event,
            f"✅ 已切换到「{chosen.get('name')}」（{chosen.get('model')}）\n"
            f"接口地址：{chosen.get('base_url') or '跟随默认'}",
        )

    async def cmd_help(self, event, args):
        await self.ctx.reply(
            event,
            "管理员命令：\n"
            "/status 运行状态\n"
            "/model 查看或切换 AI 接口（/model 2）\n"
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
