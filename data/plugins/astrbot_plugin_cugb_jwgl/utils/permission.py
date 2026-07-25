from __future__ import annotations

from astrbot.api.event import AstrMessageEvent


def ensure_bind_access(event: AstrMessageEvent, config: dict) -> str | None:
    owner_id = str(config.get("owner_qq_id", "")).strip()
    if owner_id and event.get_sender_id() != owner_id:
        return "仅允许配置的所有者绑定教务监控推送。"
    return None
