from __future__ import annotations

from copy import deepcopy
from typing import Any

from .constants import DEFAULT_TIMEZONE

DEFAULT_CONFIG: dict[str, Any] = {
    "user": {
        "qq_id": "",
        "timezone": DEFAULT_TIMEZONE,
    },
    "permission": {
        "allowed_private_users": [],
        "allowed_groups": [],
        "only_owner_can_query_personal_schedule": True,
    },
    "course": {
        "timetable_file": "raw_timetable.xlsx",
        "database": "courses.sqlite",
    },
    "classroom": {
        "enabled": True,
        "provider": "mportal",
        "database": "classrooms.sqlite",
        "mportal_base_url": "https://mportal.cugb.edu.cn",
        "mportal_campus_id": "1",
        "mportal_page_size": 50,
        "mportal_max_pages": 10,
        "mportal_timeout_seconds": 10,
        "mportal_refresh_buildings": [],
        "update_interval_minutes": 120,
        "cache_expire_minutes": 120,
    },
    "reminder": {
        "enable_class_reminder": True,
        "before_class_minutes": 20,
        "enable_homework_reminder": True,
    },
    "chaoxing": {
        "enable": False,
        "provider": "disabled",
        "database": "homework.sqlite",
        "update_interval_minutes": 120,
    },
    "llm": {
        "enable_tools": True,
    },
    "agent_notification": {
        "enabled": False,
        "host": "127.0.0.1",
        "port": 8766,
        "path": "/api/hooks/agent-completed",
        "secret": "",
        "target_umo": "",
        "database": "agent_notifications.sqlite",
        "allowed_sources": ["codex", "opencode"],
        "max_body_bytes": 16384,
        "max_message_chars": 1500,
    },
}


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = _merge(base[key], value)
        else:
            base[key] = value
    return base


def load_plugin_config(config: dict[str, Any] | None) -> dict[str, Any]:
    merged = deepcopy(DEFAULT_CONFIG)
    if config:
        _merge(merged, dict(config))
    return merged
