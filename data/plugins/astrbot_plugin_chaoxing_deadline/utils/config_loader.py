from __future__ import annotations

from copy import deepcopy
from typing import Any

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": False,
    "phone": "",
    "password": "",
    "owner_qq_id": "",
    "database": "chaoxing_deadlines.sqlite",
    "timeout_seconds": 20,
    "refresh_interval_minutes": 120,
    "reminder_enabled": True,
    "remind_before_hours": 24,
}


def load_plugin_config(config: dict[str, Any] | None) -> dict[str, Any]:
    merged = deepcopy(DEFAULT_CONFIG)
    if config:
        merged.update(dict(config))
    return merged
