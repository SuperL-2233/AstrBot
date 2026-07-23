from __future__ import annotations

from copy import deepcopy
from typing import Any

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": False,
    "phone": "",
    "password": "",
    "database": "chaoxing_deadlines.sqlite",
    "timeout_seconds": 20,
    "refresh_interval_minutes": 120,
    "cache_max_age_hours": 24,
    "reminder_enabled": True,
    "reminder_offsets_hours": [168, 120, 72, 24, 5, 2, 1],
    "reminder_scan_interval_minutes": 5,
}


def load_plugin_config(config: dict[str, Any] | None) -> dict[str, Any]:
    merged = deepcopy(DEFAULT_CONFIG)
    if config:
        merged.update(dict(config))
    return merged
