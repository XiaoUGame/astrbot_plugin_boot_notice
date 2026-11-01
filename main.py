import asyncio
import signal
import threading
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register

@register("astrbot_plugin_boot_notice", "Deepseek,XiaoU", "启动/关闭通知", "1.2.4")
class BootNotice(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        cfg = config or {}

        self.notify_on_boot = cfg.get("notify_on_boot", False)
        self.notify_on_first_message = cfg.get("notify_on_first_message", True)
        self.notify_on_shutdown = cfg.get("notify_on_shutdown", True)

        # 过滤无效 UMO
        self.sessions = [s for s in (cfg.get("sessions") or []) if s]
        if not self.sessions:
            logger.error("[boot_notice] 无有效 UMO，插件将不发送任何通知。请在配置中填入目标会话的 UMO。")

        self.startup_msg = cfg.get("startup_msg") or "✅ AstrBot 已启动"
        self.shutdown_msg = cfg.get("shutdown_msg") or "🛑 AstrBot 正在关闭"

        self._pending = False
        self._lock = asyncio.Lock()
        self._signal_installed = False
        self._shutdown_sent = False

        if self.notify_on_boot and self.notify_on_first_message:
            logger.warning("[boot_notice] notify_on_boot 与 notify_on_first_message 同时开启，后者不生效。")

    async def _send(self, text: str) -> None:
        for s in self.sessions:
            try:
                await self.context.send_message(s, MessageChain().message(text))
            except Exception as e:
                logger.error(f"[boot_notice] 发送失败 {s}: {e}")

    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        if not self.sessions:
            return
        await asyncio.sleep(2)
        if self.notify_on_boot:
            await self._send(self.startup_msg)
        elif self.notify_on_first_message:
            self._pending = True
            logger.info("[boot_notice] 已进入待补发状态")

        if self.notify_on_shutdown:
            self._install_signal_handlers()

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_message(self, event: AstrMessageEvent):
        if event.unified_msg_origin not in self.sessions:
            return
        async with self._lock:
            if not self._pending:
                return
            self._pending = False
        await self._send(self.startup_msg)

    def _install_signal_handlers(self):
        if self._signal_installed:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("[boot_notice] 无法获取事件循环，跳过信号监听")
            return

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, self._make_signal_handler(signal.getsignal(sig), loop))
            except Exception as e:
                logger.warning(f"[boot_notice] 挂载信号 {sig} 失败: {e}")
        self._signal_installed = True
        logger.info("[boot_notice] 已挂载进程退出信号监听")

    def _make_signal_handler(self, old_handler, loop):
        def handler(signum, frame):
            # 投递发送任务
            if not self._shutdown_sent:
                self._shutdown_sent = True
                loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self._send(self.shutdown_msg)))

            # 延迟 2 秒放行原退出逻辑
            def chain():
                try:
                    if callable(old_handler):
                        old_handler(signum, frame)
                    else:
                        signal.default_int_handler(signum, frame)
                except Exception:
                    pass
            threading.Timer(2.0, chain).start()
        return handler
    async def terminate(self):
        """插件被停用/卸载时的兜底触发点"""
        if self.notify_on_shutdown and self.sessions and not self._shutdown_sent:
            self._shutdown_sent = True
            try:
                await asyncio.wait_for(self._send(self.shutdown_msg), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("[boot_notice] 关闭通知发送超时，后续目标可能未收到消息")
            except Exception as e:
                logger.error(f"[boot_notice] 关闭通知发送失败: {e}")
