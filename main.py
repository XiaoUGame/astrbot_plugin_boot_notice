import asyncio
import signal
import threading
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register

BOOT_DELAY_SECONDS = 2.0
READY_POLL_INTERVAL_SECONDS = 0.5
_MISSING = object()


@register("astrbot_plugin_boot_notice", "Deepseek,XiaoU", "启动/关闭通知", "1.3.0")
class BootNotice(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        cfg = config or {}

        self.notify_on_boot = cfg.get("notify_on_boot", False)
        self.notify_on_first_message = cfg.get("notify_on_first_message", True)
        self.notify_on_shutdown = cfg.get("notify_on_shutdown", True)
        self.platform_ready_timeout = self._parse_timeout(
            cfg.get("platform_ready_timeout", 60),
        )
        self.boot_retry_count = self._parse_int(cfg.get("boot_retry_count", 3), 3, 1)
        self.boot_retry_interval = self._parse_timeout(
            cfg.get("boot_retry_interval", 5),
            default=5.0,
        )

        # 过滤无效 UMO
        self.sessions = [s for s in (cfg.get("sessions") or []) if s]
        if not self.sessions:
            logger.error(
                "[boot_notice] 无有效 UMO，插件将不发送任何通知。请在配置中填入目标会话的 UMO。"
            )

        self.startup_msg = cfg.get("startup_msg") or "✅ AstrBot 已启动"
        self.shutdown_msg = cfg.get("shutdown_msg") or "🛑 AstrBot 正在关闭"

        self._pending_sessions: set[str] = set()
        self._lock = asyncio.Lock()
        self._signal_installed = False
        self._shutdown_sent = False
        self._boot_task: asyncio.Task | None = None

    @staticmethod
    def _parse_timeout(value, default: float = 60.0) -> float:
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _parse_int(value, default: int, minimum: int = 0) -> int:
        try:
            return max(minimum, int(value))
        except (TypeError, ValueError):
            return default

    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        if not self.sessions:
            return

        # 信号监听与平台就绪无关，尽早挂上，避免等待期间退出收不到通知
        if self.notify_on_shutdown:
            self._install_signal_handlers()

        if not self.notify_on_boot and self.notify_on_first_message:
            # 纯补发模式：立即进入待补发，避免平台就绪到轮询之间漏掉首条消息
            self._pending_sessions.update(self.sessions)
            logger.info("[boot_notice] 已进入待补发状态")
            return

        if self.notify_on_boot:
            # 后台等待平台就绪后再推送，避免阻塞 AstrBot 启动流程
            self._boot_task = asyncio.create_task(self._run_boot_notice())

    async def _run_boot_notice(self) -> None:
        try:
            await asyncio.sleep(BOOT_DELAY_SECONDS)
            await self._wait_for_platforms_ready()
            if self._shutdown_sent:
                logger.info("[boot_notice] 进程已在启动通知发出前退出，跳过发送")
                return

            attempts = max(1, self.boot_retry_count)
            remaining = list(self.sessions)
            for attempt in range(1, attempts + 1):
                remaining = await self._send_many(
                    remaining,
                    self.startup_msg,
                    force_proactive=True,
                )
                if not remaining:
                    return
                if attempt < attempts:
                    logger.warning(
                        f"[boot_notice] 启动通知第 {attempt}/{attempts} 次发送失败，"
                        f"{self.boot_retry_interval:g} 秒后重试: {', '.join(remaining)}",
                    )
                    await asyncio.sleep(self.boot_retry_interval)

            if self.notify_on_first_message:
                self._pending_sessions.update(remaining)
                logger.warning(
                    f"[boot_notice] 主动推送 {attempts} 次均失败，"
                    f"转为等待目标会话来消息后补发: {', '.join(remaining)}",
                )
            else:
                logger.warning(
                    f"[boot_notice] 启动通知发送失败（已尝试 {attempts} 次）: {', '.join(remaining)}",
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error(f"[boot_notice] 启动通知流程异常: {e}")

    async def _send_many(
        self,
        sessions: list[str],
        text: str,
        *,
        force_proactive: bool,
    ) -> list[str]:
        """向多个会话发送，返回发送失败的会话列表。"""
        failed: list[str] = []
        for session in sessions:
            if not await self._send_one(session, text, force_proactive=force_proactive):
                failed.append(session)
        return failed

    async def _send_one(
        self, session: str, text: str, *, force_proactive: bool
    ) -> bool:
        inst = None
        session_id = None
        previous_msg_id = None
        track_msg_id = False
        restore = None
        try:
            parts = session.split(":", 2)
            if len(parts) == 3:
                inst = self._get_platform_inst(parts[0])
                if (
                    inst is not None
                    and parts[1] == "GroupMessage"
                    and self._is_qq_official(inst)
                ):
                    # 与适配器 send_by_session 内部一致：UMO 里的会话 ID 会做一次 rsplit
                    session_id = parts[2].rsplit("_", 1)[-1]
                    msg_ids = getattr(inst, "_session_last_message_id", None)
                    if isinstance(msg_ids, dict):
                        previous_msg_id = msg_ids.get(session_id)
                        track_msg_id = True
                    if force_proactive:
                        restore = self._force_qq_group_scene(inst, session_id)
            sent = await self.context.send_message(
                session, MessageChain().message(text)
            )
            ok = sent is not False
        except Exception as e:  # noqa: BLE001
            logger.error(f"[boot_notice] 发送失败 {session}: {e}")
            ok = False
        finally:
            if restore is not None:
                restore()

        if ok and track_msg_id:
            msg_ids = getattr(inst, "_session_last_message_id", None)
            current = msg_ids.get(session_id) if isinstance(msg_ids, dict) else None
            # 适配器成功发送后会缓存返回的 msg_id；没变化说明消息被静默跳过或请求超时
            if not current or current == previous_msg_id:
                ok = False
                logger.debug(
                    f"[boot_notice] QQ 官方会话 {session} 未返回消息 ID，视为未发送"
                )
        return ok

    def _force_qq_group_scene(self, inst, session_id: str):
        """临时把 QQ 群会话标记为 group，让 AstrBot 走主动推送而不是跳过。

        AstrBot 的 qq_official 适配器要求本次运行内该会话有 scene/msg_id 才肯
        主动推送；重启后缓存为空，因此会在发送前直接 return。这里只在发送瞬间
        补状态，发送结束后立即还原，不影响后续正常收发。
        """
        scenes = getattr(inst, "_session_scene", None)
        if not isinstance(scenes, dict):
            return None
        msg_ids = getattr(inst, "_session_last_message_id", None)
        if isinstance(msg_ids, dict) and msg_ids.get(session_id):
            return None  # 已有上下文，保持被动回复
        if scenes.get(session_id) == "group":
            return None  # 已是群场景，无需处理

        previous_scene = scenes.get(session_id, _MISSING)
        scenes[session_id] = "group"

        previous_allow = getattr(inst, "_allow_group_proactive_send", _MISSING)
        try:
            inst._allow_group_proactive_send = True
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[boot_notice] 无法临时开启主动推送: {e}")

        def restore() -> None:
            if previous_scene is _MISSING:
                scenes.pop(session_id, None)
            else:
                scenes[session_id] = previous_scene
            try:
                if previous_allow is _MISSING:
                    delattr(inst, "_allow_group_proactive_send")
                else:
                    inst._allow_group_proactive_send = previous_allow
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[boot_notice] 还原主动推送状态失败: {e}")

        return restore

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_message(self, event: AstrMessageEvent):
        if event.unified_msg_origin not in self.sessions:
            return
        async with self._lock:
            if not self._pending_sessions:
                return
            pending = list(self._pending_sessions)
            # 先摘除再发送，避免并发消息导致重复补发
            self._pending_sessions.clear()
        failed = await self._send_many(pending, self.startup_msg, force_proactive=True)
        if failed:
            async with self._lock:
                self._pending_sessions.update(failed)
            logger.warning(f"[boot_notice] 补发未成功，继续等待: {', '.join(failed)}")

    # ---------- 平台就绪检测 ----------

    def _target_platform_ids(self) -> list[str]:
        ids: list[str] = []
        for session in self.sessions:
            platform_id = session.split(":", 1)[0].strip()
            if platform_id and platform_id not in ids:
                ids.append(platform_id)
        return ids

    def _platform_configs(self) -> list | None:
        manager = getattr(self.context, "platform_manager", None)
        configs = getattr(manager, "platforms_config", None)
        return configs if isinstance(configs, list) else None

    def _get_platform_inst(self, platform_id: str):
        getter = getattr(self.context, "get_platform_inst", None)
        if not callable(getter):
            return None
        try:
            return getter(platform_id)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[boot_notice] 获取平台实例 {platform_id} 失败: {e}")
            return None

    @staticmethod
    def _is_qq_official(inst) -> bool:
        try:
            return str(getattr(inst.meta(), "name", "")) == "qq_official"
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _qq_official_websocket_state(inst) -> bool | None:
        """QQ 官方适配器是否已完成 WebSocket 握手。

        返回 True/False 表示已/未就绪，返回 None 表示当前版本无法判断。
        botpy 在收到网关 READY 后才会给 websocket 对象设置 user，
        因此不能只看连接对象是否存在（登录后即会创建）。
        """
        client = getattr(inst, "client", None)
        sockets = getattr(client, "_active_websockets", None)
        if sockets is None:
            return None
        for ws in list(sockets):
            if getattr(ws, "user", None):
                return True
            session = getattr(ws, "_session", None)
            if isinstance(session, dict) and session.get("session_id"):
                return True
        return False

    @staticmethod
    def _platform_status_name(inst) -> str:
        if inst is None:
            return ""
        status = getattr(inst, "status", None)
        return str(getattr(status, "name", None) or status or "").upper()

    def _is_platform_ready(self, platform_id: str) -> bool:
        inst = self._get_platform_inst(platform_id)
        if inst is None:
            return False
        if self._is_qq_official(inst):
            connected = self._qq_official_websocket_state(inst)
            if connected is not None:
                return connected
        # 其他平台没有统一的“已连接”信号，退化为 AstrBot 的运行状态判断
        status_name = self._platform_status_name(inst)
        return not status_name or status_name == "RUNNING"

    async def _wait_for_platforms_ready(self) -> None:
        timeout = self.platform_ready_timeout
        platform_ids = self._target_platform_ids()
        if timeout <= 0 or not platform_ids:
            return
        if not callable(getattr(self.context, "get_platform_inst", None)):
            logger.debug(
                "[boot_notice] 当前 AstrBot 版本不支持查询平台实例，跳过就绪等待"
            )
            return

        configs = self._platform_configs()
        pending: list[str] = []
        for platform_id in platform_ids:
            if configs is not None:
                config = next(
                    (
                        c
                        for c in configs
                        if isinstance(c, dict) and str(c.get("id", "")) == platform_id
                    ),
                    None,
                )
                if config is None:
                    logger.warning(
                        f"[boot_notice] 平台配置中不存在 {platform_id}，跳过该平台的等待"
                    )
                    continue
                if not config.get("enable", True):
                    logger.warning(
                        f"[boot_notice] 平台 {platform_id} 未启用，跳过该平台的等待"
                    )
                    continue
            if not self._is_platform_ready(platform_id):
                pending.append(platform_id)

        if not pending:
            return

        start = time.monotonic()
        waited = list(pending)
        logger.info(
            f"[boot_notice] 等待平台就绪: {', '.join(waited)}（最多 {timeout:g} 秒）"
        )
        while pending:
            elapsed = time.monotonic() - start
            if elapsed >= timeout:
                logger.warning(
                    f"[boot_notice] 等待平台就绪超时（{timeout:g} 秒），继续发送: {', '.join(pending)}",
                )
                return
            await asyncio.sleep(min(READY_POLL_INTERVAL_SECONDS, timeout - elapsed))
            still_pending: list[str] = []
            for platform_id in pending:
                if self._is_platform_ready(platform_id):
                    continue
                if (
                    self._platform_status_name(self._get_platform_inst(platform_id))
                    == "ERROR"
                ):
                    logger.error(f"[boot_notice] 平台 {platform_id} 启动失败，停止等待")
                    continue
                still_pending.append(platform_id)
            pending = still_pending

        logger.info(
            f"[boot_notice] 平台已就绪: {', '.join(waited)}"
            f"（{time.monotonic() - start:.1f} 秒）",
        )

    # ---------- 进程退出信号 ----------

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
                signal.signal(
                    sig, self._make_signal_handler(signal.getsignal(sig), loop)
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[boot_notice] 挂载信号 {sig} 失败: {e}")
        self._signal_installed = True
        logger.info("[boot_notice] 已挂载进程退出信号监听")

    def _make_signal_handler(self, old_handler, loop):
        def handler(signum, frame):
            # 投递发送任务
            if not self._shutdown_sent:
                self._shutdown_sent = True
                loop.call_soon_threadsafe(
                    lambda: asyncio.ensure_future(
                        self._send_many(
                            self.sessions,
                            self.shutdown_msg,
                            force_proactive=True,
                        ),
                    ),
                )

            # 延迟 2 秒放行原退出逻辑
            def chain():
                try:
                    if callable(old_handler):
                        old_handler(signum, frame)
                    else:
                        signal.default_int_handler(signum, frame)
                except Exception as e:  # noqa: BLE001
                    logger.debug(f"[boot_notice] 执行原信号处理器异常: {e}")

            threading.Timer(2.0, chain).start()

        return handler

    async def terminate(self):
        """插件被停用/卸载时的兜底触发点"""
        if self._boot_task is not None and not self._boot_task.done():
            self._boot_task.cancel()
            try:
                await self._boot_task
            except asyncio.CancelledError:
                pass
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[boot_notice] 取消启动通知任务异常: {e}")

        if self.notify_on_shutdown and self.sessions and not self._shutdown_sent:
            self._shutdown_sent = True
            try:
                await asyncio.wait_for(
                    self._send_many(
                        self.sessions,
                        self.shutdown_msg,
                        force_proactive=True,
                    ),
                    timeout=5.0,
                )
            except TimeoutError:
                logger.warning("[boot_notice] 关闭通知发送超时，后续目标可能未收到消息")
            except Exception as e:  # noqa: BLE001
                logger.error(f"[boot_notice] 关闭通知发送失败: {e}")
