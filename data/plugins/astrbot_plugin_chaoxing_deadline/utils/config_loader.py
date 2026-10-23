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
    "sync_lease_minutes": 30,
    "antispider_cooldown_hours": 12,
    "timezone": "Asia/Shanghai",
    "reminder_enabled": True,
    "reminder_offsets_hours": [72, 48, 24, 12, 1],
    "reminder_scan_interval_minutes": 5,
}


def load_plugin_config(config: dict[str, Any] | None) -> dict[str, Any]:
    """Merge saved settings and migrate the previous default reminder stages.

    Args:
        config: Saved AstrBot plugin settings, if any.

    Returns:
        Effective settings with custom reminder stages preserved.
    """
    merged = deepcopy(DEFAULT_CONFIG)
    if config:
        merged.update(dict(config))
    if merged["reminder_offsets_hours"] == [168, 120, 72, 24, 5, 2, 1]:
        merged["reminder_offsets_hours"] = list(
            DEFAULT_CONFIG["reminder_offsets_hours"]
        )
    return merged
