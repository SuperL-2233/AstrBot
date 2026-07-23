from __future__ import annotations

from astrbot.api.event import AstrMessageEvent


def ensure_owner(
    event: AstrMessageEvent, config: dict, private_only: bool = False
) -> str | None:
    owner_id = str(config.get("owner_qq_id", "")).strip()
    if not owner_id:
        return "请先在插件配置中设置 owner_qq_id。"
    if event.get_sender_id() != owner_id:
        return "仅允许配置的 QQ 所有者访问个人学习通数据。"
    if private_only and not event.is_private_chat():
        return "请在 QQ 私聊中绑定或解绑超星提醒。"
    return None
