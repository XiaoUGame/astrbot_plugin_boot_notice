import asyncio

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register


@register("astrbot_plugin_boot_notice", "Deepseek,XiaoU", "启动/关闭通知", "1.2.1")
class BootNotice(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        cfg = config or {}

        self.enable = cfg.get("enable", True)
        self.notify_on_boot = cfg.get("notify_on_boot", False)
        self.notify_on_first_message = cfg.get("notify_on_first_message", True)
        self.notify_on_shutdown = cfg.get("notify_on_shutdown", True)

        raw_sessions = cfg.get("sessions") or []
        self.sessions = [s for s in raw_sessions if s and s != "TYPE HERE"]
        if not self.sessions:
            logger.error(
                "[boot_notice] sessions 未配置或没有有效 UMO，插件将不发送任何通知。"
                "请在插件配置的 sessions 中填入目标会话的 UMO，格式 平台ID:消息类型:会话ID。"
            )

        self.startup_msg = cfg.get("startup_msg") or "✅ AstrBot 已启动"
        self.shutdown_msg = cfg.get("shutdown_msg") or "🛑 AstrBot 正在关闭"

        self._pending = False
        self._lock = asyncio.Lock()

        # 显式处理 notify_on_boot / notify_on_first_message 的互斥
        if self.notify_on_boot and self.notify_on_first_message:
            logger.warning(
                "[boot_notice] notify_on_boot 与 notify_on_first_message 同时为 true，"
                "notify_on_boot 优先，notify_on_first_message 本次运行不会生效。"
            )

    async def _send(self, text: str) -> None:
        if not self.sessions:
            return
        for s in self.sessions:
            try:
                await self.context.send_message(s, MessageChain().message(text))
            except Exception as e:
                logger.error(f"[boot_notice] 发送失败 {s}: {e}")

    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        if not self.enable or not self.sessions:
            return
        await asyncio.sleep(2)
        if self.notify_on_boot:
            await self._send(self.startup_msg)
            self._pending = False
        elif self.notify_on_first_message:
            self._pending = True
            logger.info("[boot_notice] 已进入待补发状态，等待任意目标会话出现新消息")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_message(self, event: AstrMessageEvent):
        if event.unified_msg_origin not in self.sessions:
            return
        # 用锁保护 _pending 的读取与清除，避免并发消息导致重复补发
        async with self._lock:
            if not self._pending:
                return
            self._pending = False
        await self._send(self.startup_msg)

    async def terminate(self):
        """插件被停用/卸载时调用。关闭通知在此触发，不再拦截系统信号。"""
        if not self.enable:
            return
        if self.notify_on_shutdown and self.sessions:
            try:
                await asyncio.wait_for(self._send(self.shutdown_msg), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning(
                    "[boot_notice] 关闭通知发送超时。已发出的消息不会回滚，"
                    "超时点之后的目标会话可能未收到关闭消息。"
                )
            except Exception as e:
                logger.error(f"[boot_notice] 关闭通知发送失败: {e}")
