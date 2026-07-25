from __future__ import annotations

from copy import deepcopy
from typing import Any

DEFAULT_CONFIG: dict[str, Any] = {
    "permission": {
        "allowed_private_users": [],
        "allowed_groups": [],
    },
    "baidu": {
        "ak": "",
        "timeout_seconds": 10,
    },
    "push": {
        "enabled": True,
        "default_time": "07:50",
        "timezone": "Asia/Shanghai",
        "target_umo": "",
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
