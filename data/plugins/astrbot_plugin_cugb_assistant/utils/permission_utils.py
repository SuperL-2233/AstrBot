from __future__ import annotations

from astrbot.api.event import AstrMessageEvent


def ensure_general_access(event: AstrMessageEvent, config: dict) -> str | None:
    permission_cfg = config["permission"]
    sender_id = event.get_sender_id()
    group_id = event.get_group_id()
    allowed_private = {str(i) for i in permission_cfg.get("allowed_private_users", [])}
    allowed_groups = {str(i) for i in permission_cfg.get("allowed_groups", [])}

    if event.is_private_chat():
        if allowed_private and sender_id not in allowed_private:
            return "当前私聊用户未被授权使用校园助手。"
        return None

    if allowed_groups and group_id not in allowed_groups:
        return "当前群聊未被授权使用校园助手。"
    return None
