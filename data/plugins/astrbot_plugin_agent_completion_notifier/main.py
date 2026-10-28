from __future__ import annotations

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter

from .providers.webhook_provider import WebhookProvider
from .repositories.notification_repo import NotificationRepository
from .services.notification_service import NotificationService
from .utils.config_loader import load_plugin_config
from .utils.paths import resolve_data_path
from .utils.permission_utils import ensure_owner_private_access


@star.register(
    "astrbot_plugin_agent_completion_notifier",
    "macroladlezilla",
    "开发工具结束通知",
    "0.1.1",
)
class AgentCompletionNotifierPlugin(star.Star):
    def __init__(self, context: star.Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self.config = load_plugin_config(config)
        repository = NotificationRepository(resolve_data_path(self.config["database"]))
        self.notification_service = NotificationService(self.config, repository)
        self.webhook_provider = WebhookProvider(self.config, self._deliver_notification)

    async def initialize(self) -> None:
        try:
            await self.webhook_provider.start()
            if self.webhook_provider.started:
                logger.info(
                    "开发工具通知接收器已监听 "
                    f"{self.config['host']}:{self.config['port']}{self.config['path']}"
                )
            else:
                logger.info(
                    f"开发工具通知接收器未启动: {self.webhook_provider.disabled_reason}"
                )
        except Exception as exc:
            self.webhook_provider.disabled_reason = "启动失败"
            logger.error(f"开发工具通知接收器启动失败: {exc}")

    async def terminate(self) -> None:
        await self.webhook_provider.stop()

    async def _deliver_notification(self, payload: object) -> dict:
        async def send(target_umo: str, text: str) -> bool:
            return await self.context.send_message(
                target_umo, MessageChain().message(text)
            )

        return await self.notification_service.deliver(payload, send)

    def get_public_capabilities(self) -> dict:
        """Describe notifier features using the project plugin contract."""
        return {
            "summary": "接收配置中允许的 agent 完成事件并推送到绑定私聊。",
            "features": ["开发工具完成事件接收", "QQ 私聊主动通知"],
            "commands": ["/开发通知状态", "/开发通知绑定", "/开发通知解绑"],
            "llm_tools": [],
        }

    def get_public_status(self) -> dict:
        """Return notifier state for the capability registry."""
        status = self.notification_service.get_status()
        status["listener_running"] = self.webhook_provider.started
        return status

    @filter.command("开发通知帮助")
    async def help_command(self, event: AstrMessageEvent):
        """显示开发工具通知插件帮助。"""
        yield event.plain_result(
            "开发工具结束通知\n/开发通知状态\n/开发通知绑定\n/开发通知解绑"
        )

    @filter.command("开发通知状态")
    async def status_command(self, event: AstrMessageEvent):
        """显示接收器和目标绑定状态。"""
        status = self.notification_service.get_status()
        yield event.plain_result(
            "开发工具通知状态\n"
            f"配置：{'已启用' if status['enabled'] else '未启用'}\n"
            f"监听：{'运行中' if self.webhook_provider.started else '未运行'}\n"
            f"目标：{'已绑定' if status['target_bound'] else '未绑定'}\n"
            f"就绪：{'是' if status['ready'] and self.webhook_provider.started else '否'}"
        )

    @filter.command("开发通知绑定")
    async def bind_notification(self, event: AstrMessageEvent):
        """将结束通知绑定到当前 QQ 私聊会话。"""
        access_error = ensure_owner_private_access(event, self.config)
        if access_error:
            yield event.plain_result(access_error)
            return
        self.notification_service.bind_target(event.unified_msg_origin)
        yield event.plain_result("已将开发工具对话结束通知绑定到当前私聊。")

    @filter.command("开发通知解绑")
    async def unbind_notification(self, event: AstrMessageEvent):
        """清除持久化的通知目标。"""
        access_error = ensure_owner_private_access(event, self.config)
        if access_error:
            yield event.plain_result(access_error)
            return
        self.notification_service.clear_target()
        yield event.plain_result("已解除开发工具通知绑定。")
