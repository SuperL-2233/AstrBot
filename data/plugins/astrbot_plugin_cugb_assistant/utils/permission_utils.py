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


def ensure_personal_schedule_access(
    event: AstrMessageEvent, config: dict
) -> str | None:
    access_error = ensure_general_access(event, config)
    if access_error:
        return access_error

    permission_cfg = config["permission"]
    owner_id = str(config["user"].get("qq_id", "")).strip()
    if not permission_cfg.get("only_owner_can_query_personal_schedule", True):
        return None
    if not owner_id:
        return None
    if event.get_sender_id() != owner_id:
        return "当前仅允许课表所属用户查询个人课程表。"
    return None
