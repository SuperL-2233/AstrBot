from __future__ import annotations

from astrbot.api.event import AstrMessageEvent


def ensure_private_chat(event: AstrMessageEvent) -> str | None:
    if not event.is_private_chat():
        return "请在私聊中绑定或解绑超星提醒。"
    return None
