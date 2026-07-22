from __future__ import annotations

from astrbot.api.event import AstrMessageEvent


def ensure_owner_private_access(event: AstrMessageEvent, config: dict) -> str | None:
    owner_id = str(config.get("owner_qq_id", "")).strip()
    if not owner_id:
        return "请先在插件配置中设置 owner_qq_id，才能绑定开发通知目标。"
    if not event.is_private_chat() or event.get_sender_id() != owner_id:
        return (
            "仅允许配置的 QQ 所有者在私聊中绑定开发通知目标。\n"
            f"你的发送者 ID：{event.get_sender_id() or '(未知)'}"
            "（QQ 官方机器人下发的是 openid，请把它填入 owner_qq_id 后重载插件再绑定）"
        )
    return None
