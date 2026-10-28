from __future__ import annotations

from functools import partial

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
    "通知接收与推送",
    "0.2.0",
)
class AgentCompletionNotifierPlugin(star.Star):
    def __init__(self, context: star.Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self.config = load_plugin_config(config)
        repository = NotificationRepository(resolve_data_path(self.config["database"]))
        self.notification_service = NotificationService(self.config, repository)
        self.webhook_provider = WebhookProvider(
            self.config,
            self._deliver_notification,
            notify_callback=partial(self._deliver_notification, generic=True),
        )

    async def initialize(self) -> None:
        try:
            await self.webhook_provider.start()
            if self.webhook_provider.started:
                logger.info(
                    f"Notification receiver listening on {self.config['host']}:"
                    f"{self.config['port']} at {self.config['path']} "
                    f"and {self.config['notify_path']}"
                )
            else:
                logger.info(
                    f"Notification receiver disabled: {self.webhook_provider.disabled_reason}"
                )
        except Exception as exc:
            self.webhook_provider.disabled_reason = "启动失败"
            logger.error(f"Notification receiver failed to start: {exc}")

    async def terminate(self) -> None:
        await self.webhook_provider.stop()

    async def _deliver_notification(
        self, payload: object, *, generic: bool = False
    ) -> dict:
        async def send(target_umo: str, text: str) -> bool:
            return await self.context.send_message(
                target_umo, MessageChain().message(text)
            )

        return await self.notification_service.deliver(payload, send, generic=generic)

    def get_public_capabilities(self) -> dict:
        """Describe notifier features using the project plugin contract."""
        return {
            "summary": "接收 agent 完成事件和自定义通知，并推送到绑定私聊。",
            "features": [
                "开发工具完成事件接收",
                "自定义标题和正文通知",
                "QQ 私聊主动通知",
            ],
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
        """Show notification commands and the custom-message endpoint."""
        yield event.plain_result(
            "通知接收与推送\n/开发通知状态\n/开发通知绑定\n/开发通知解绑\n"
            f"通用通知入口：{self.config['notify_path']}\n"
            "提交 JSON 的 message 自定义正文，title 可选。"
        )

    @filter.command("开发通知状态")
    async def status_command(self, event: AstrMessageEvent):
        """显示接收器和目标绑定状态。"""
        status = self.notification_service.get_status()
        yield event.plain_result(
            "通知推送状态\n"
            f"配置：{'已启用' if status['enabled'] else '未启用'}\n"
            f"监听：{'运行中' if self.webhook_provider.started else '未运行'}\n"
            f"目标：{'已绑定' if status['target_bound'] else '未绑定'}\n"
            f"就绪：{'是' if status['ready'] and self.webhook_provider.started else '否'}"
        )

    @filter.command("开发通知绑定")
    async def bind_notification(self, event: AstrMessageEvent):
        """Bind agent and custom notifications to the owner's private chat."""
        access_error = ensure_owner_private_access(event, self.config)
        if access_error:
            yield event.plain_result(access_error)
            return
        self.notification_service.bind_target(event.unified_msg_origin)
        yield event.plain_result("已将 agent 和通用通知绑定到当前私聊。")

    @filter.command("开发通知解绑")
    async def unbind_notification(self, event: AstrMessageEvent):
        """清除持久化的通知目标。"""
        access_error = ensure_owner_private_access(event, self.config)
        if access_error:
            yield event.plain_result(access_error)
            return
        self.notification_service.clear_target()
        yield event.plain_result("已解除通知推送绑定。")
