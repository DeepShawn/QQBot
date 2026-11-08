"""插件基类 + 事件分发。"""
from __future__ import annotations


class PluginBase:
    name = "plugin"
    priority = 100  # 数字小的先执行

    def __init__(self, ctx):
        self.ctx = ctx

    async def on_event(self, event) -> bool:
        """返回 True 表示已处理，后续插件不再收到这个事件。"""
        return False


class Dispatcher:
    def __init__(self, ctx):
        self.ctx = ctx
        self.log = ctx.log
        self.plugins: list[PluginBase] = []

    def register(self, plugin: PluginBase):
        self.plugins.append(plugin)
        self.plugins.sort(key=lambda p: p.priority)

    async def dispatch(self, event: dict):
        post_type = event.get("post_type")
        self_id = event.get("self_id")
        if self_id and not self.ctx.self_id:
            self.ctx.self_id = self_id

        if post_type == "meta_event":
            if event.get("meta_event_type") == "lifecycle":
                self.log.info("收到生命周期事件: %s", event.get("sub_type"))
            return
        if post_type not in ("message", "notice", "request"):
            return

        # 忽略自己发的消息，避免自触发
        if str(event.get("user_id", "")) == str(self.ctx.self_id or self_id or ""):
            if post_type == "message":
                return

        for plugin in self.plugins:
            try:
                if await plugin.on_event(event):
                    return
            except Exception:
                self.log.exception("插件 %s 处理事件出错", plugin.name)
