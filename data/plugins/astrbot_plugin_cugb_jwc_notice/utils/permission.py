from __future__ import annotations

from astrbot.api.event import AstrMessageEvent


def ensure_bind_access(event: AstrMessageEvent, config: dict) -> str | None:
    owner_id = str(config.get("owner_qq_id", "")).strip()
    if not event.is_private_chat():
        return "请在 QQ 私聊中绑定或解绑教务通知推送。"
    if owner_id and event.get_sender_id() != owner_id:
        return "仅允许配置的 QQ 所有者绑定教务通知推送。"
    return None
