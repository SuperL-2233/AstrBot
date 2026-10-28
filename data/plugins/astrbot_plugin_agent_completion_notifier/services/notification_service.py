from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..repositories.notification_repo import NotificationRepository


def _strip_markdown(text: str) -> str:
    """Convert common Markdown syntax to plain text for QQ rendering.

    QQ cannot render Markdown, so fenced code blocks, emphasis markers,
    links, images, headings, quotes, and inline code are flattened while
    keeping their textual content.

    Args:
        text: Raw message text that may contain Markdown.

    Returns:
        Plain text with Markdown markers removed.
    """
    # Fenced code blocks: drop fence lines, keep the code content.
    text = re.sub(r"```[^\n]*\n(.*?)```", r"\1", text, flags=re.S)
    text = re.sub(r"```[^\n]*$", "", text)
    # Images keep their alt text; links keep their link text.
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    # Inline code, bold, italic, and strikethrough keep their content.
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m.group(1) or m.group(2), text)
    text = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"\1", text)
    text = re.sub(r"(?<![\w_])_([^_\n]+)_(?![\w_])", r"\1", text)
    text = re.sub(r"~~(.+?)~~", r"\1", text)
    # Headings and blockquotes drop their leading markers.
    text = re.sub(r"^[ \t]{0,3}#{1,6}[ \t]*", "", text, flags=re.M)
    text = re.sub(r"^[ \t]*>[ \t]?", "", text, flags=re.M)
    # Collapse 3+ consecutive blank lines left behind by the removals.
    return re.sub(r"\n{3,}", "\n\n", text).strip()


@dataclass(frozen=True)
class NotificationRequestError(Exception):
    message: str
    error_code: str
    http_status: int = 400

    def __str__(self) -> str:
        return self.message


class NotificationService:
    EVENT_BY_SOURCE = {
        "codex": "agent-turn-complete",
        "opencode": "session.idle",
    }

    def __init__(
        self, config: dict[str, Any], repository: NotificationRepository
    ) -> None:
        self.config = config
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
            raise NotificationRequestError(
                "请求体必须是 JSON 对象。", "INVALID_JSON_BODY"
            )

        source = self._field(payload, "source", 32).lower()
        allowed_sources = {
            str(item).lower() for item in self.config.get("allowed_sources", [])
        }
        if source not in allowed_sources:
            raise NotificationRequestError("不支持的通知来源。", "UNSUPPORTED_SOURCE")

        event = self._field(payload, "event", 64)
        expected_event = self.EVENT_BY_SOURCE.get(source)
        if expected_event is not None and event != expected_event:
            raise NotificationRequestError("来源与结束事件不匹配。", "INVALID_EVENT")

        return {
            "source": source,
            "event": event,
            "event_id": self._field(payload, "event_id", 200),
            "session_id": self._field(payload, "session_id", 200),
            "project": self._optional_field(payload, "project", 200),
            "host": self._optional_field(payload, "host", 64),
            "message": self._optional_field(payload, "message", 12000),
        }

    @staticmethod
    def _field(payload: dict[str, Any], name: str, max_length: int) -> str:
        raw_value = payload.get(name)
        if not isinstance(raw_value, str):
            raise NotificationRequestError(
                f"字段必须是字符串：{name}。", "INVALID_FIELD_TYPE"
            )
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
            raise NotificationRequestError(
                f"字段必须是字符串：{name}。", "INVALID_FIELD_TYPE"
            )
        value = raw_value.strip()
        if len(value) > max_length:
            raise NotificationRequestError(f"字段过长：{name}。", "FIELD_TOO_LONG")
        return value

    def _format_message(self, data: dict[str, str]) -> str:
        source_name = {"codex": "Codex", "opencode": "OpenCode"}.get(
            data["source"], data["source"]
        )
        label = data["host"] or f"服务器-{source_name}"
        lines = [f"{label} 会话结束", f"会话：{data['session_id']}"]
        if data["message"]:
            lines.extend(["", _strip_markdown(data["message"])])
        text = "\n".join(lines)
        limit = max(100, int(self.config.get("max_message_chars", 1500)))
        if len(text) > limit:
            text = text[: limit - 12].rstrip() + "\n…（已截断）"
        return text
