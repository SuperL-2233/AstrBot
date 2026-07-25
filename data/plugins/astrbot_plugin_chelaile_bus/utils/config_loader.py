from __future__ import annotations

from copy import deepcopy
from typing import Any

DEFAULT_CONFIG: dict[str, Any] = {
    "permission": {
        "allowed_private_users": [],
        "allowed_groups": [],
    },
    "bus": {
        "enabled": True,
        "city_id": "027",
        "max_buses": 2,
        "request_timeout_seconds": 15,
    },
    "llm": {
        "enable_tools": True,
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
    """Merge user config over plugin defaults.

    Args:
        config: Raw config passed by AstrBot, may be None.

    Returns:
        A deep-copied config dict with all default keys present.
    """
    merged = deepcopy(DEFAULT_CONFIG)
    if config:
        _merge(merged, dict(config))
    return merged
