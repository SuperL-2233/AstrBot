from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..repositories.notification_repo import NotificationRepository


@dataclass(frozen=True)
class NotificationRequestError(Exception):
    message: str
    error_code: str
    http_status: int = 400

    def __str__(self) -> str:
        return self.message


class AgentNotificationService:
    EVENT_BY_SOURCE = {
        "codex": "agent-turn-complete",
        "opencode": "session.idle",
    }

    def __init__(self, config: dict[str, Any], repository: NotificationRepository) -> None:
        self.config = config["agent_notification"]
        self.repository = repository

    def get_target_umo(self) -> str:
        configured = str(self.config.get("target_umo", "")).strip()
        return configured or self.repository.get_target_umo()

    def bind_target(self, target_umo: str) -> None:
        self.repository.set_target_umo(target_umo.strip())

    def clear_target(self) -> None:
        self.repository.clear_target_umo()

    def get_status(self) -> dict[str, Any]:
        enabled = bool(self.config.get("enabled", False))
        has_secret = bool(str(self.config.get("secret", "")).strip())
        target_bound = bool(self.get_target_umo())
        return {
            "enabled": enabled,
            "ready": enabled and has_secret and target_bound,
            "has_secret": has_secret,
            "target_bound": target_bound,
            "listen": f"{self.config.get('host')}:{self.config.get('port')}",
            "path": self.config.get("path"),
        }

    async def deliver(
        self,
        payload: Any,
        send_message: Callable[[str, str], Awaitable[bool]],
    ) -> dict[str, Any]:
        data = self._validate(payload)
        target_umo = self.get_target_umo()
        if not target_umo:
            raise NotificationRequestError(
                "尚未绑定通知目标，请先在 QQ 私聊中执行 /开发通知绑定。",
                "NOTIFICATION_TARGET_NOT_BOUND",
                503,
            )

        if not self.repository.claim_event(data["event_id"], data["source"]):
            return {"success": True, "duplicate": True, "message": "事件已处理。"}

        try:
            sent = await send_message(target_umo, self._format_message(data))
            if not sent:
                raise RuntimeError("AstrBot 未找到与目标会话匹配的平台适配器。")
        except Exception as exc:
            self.repository.release_event(data["event_id"])
            raise NotificationRequestError(
                f"QQ 消息发送失败：{exc}",
                "NOTIFICATION_DELIVERY_FAILED",
                502,
            ) from exc

        self.repository.mark_delivered(data["event_id"])
        return {"success": True, "duplicate": False, "message": "通知已推送。"}

    def _validate(self, payload: Any) -> dict[str, str]:
        if not isinstance(payload, dict):
            raise NotificationRequestError("请求体必须是 JSON 对象。", "INVALID_JSON_BODY")

        source = self._field(payload, "source", 32).lower()
        allowed_sources = {str(item).lower() for item in self.config.get("allowed_sources", [])}
        if source not in self.EVENT_BY_SOURCE or source not in allowed_sources:
            raise NotificationRequestError("不支持的通知来源。", "UNSUPPORTED_SOURCE")

        event = self._field(payload, "event", 64)
        if event != self.EVENT_BY_SOURCE[source]:
            raise NotificationRequestError("来源与结束事件不匹配。", "INVALID_EVENT")

        return {
            "source": source,
            "event": event,
            "event_id": self._field(payload, "event_id", 200),
            "session_id": self._field(payload, "session_id", 200),
            "project": self._optional_field(payload, "project", 200),
            "message": self._optional_field(payload, "message", 12000),
        }

    @staticmethod
    def _field(payload: dict[str, Any], name: str, max_length: int) -> str:
        raw_value = payload.get(name)
        if not isinstance(raw_value, str):
            raise NotificationRequestError(f"字段必须是字符串：{name}。", "INVALID_FIELD_TYPE")
        value = raw_value.strip()
        if not value:
            raise NotificationRequestError(f"缺少字段：{name}。", "MISSING_FIELD")
        if len(value) > max_length:
            raise NotificationRequestError(f"字段过长：{name}。", "FIELD_TOO_LONG")
        return value

    @staticmethod
    def _optional_field(payload: dict[str, Any], name: str, max_length: int) -> str:
        raw_value = payload.get(name)
        if raw_value is None:
            return ""
        if not isinstance(raw_value, str):
            raise NotificationRequestError(f"字段必须是字符串：{name}。", "INVALID_FIELD_TYPE")
        value = raw_value.strip()
        if len(value) > max_length:
            raise NotificationRequestError(f"字段过长：{name}。", "FIELD_TOO_LONG")
        return value

    def _format_message(self, data: dict[str, str]) -> str:
        source_name = "Codex" if data["source"] == "codex" else "OpenCode"
        lines = [f"[{source_name}] 一轮对话已结束"]
        if data["project"]:
            lines.append(f"项目：{Path(data['project']).name}")
        lines.append(f"会话：{data['session_id']}")
        if data["message"]:
            lines.extend(["", data["message"]])
        text = "\n".join(lines)
        limit = max(100, int(self.config.get("max_message_chars", 1500)))
        if len(text) > limit:
            text = text[: limit - 12].rstrip() + "\n…（已截断）"
        return text
