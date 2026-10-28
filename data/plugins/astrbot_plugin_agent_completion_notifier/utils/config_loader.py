from __future__ import annotations

from copy import deepcopy
from typing import Any

DEFAULT_CONFIG: dict[str, Any] = {
    "owner_qq_id": "",
    "enabled": False,
    "host": "127.0.0.1",
    "port": 8766,
    "path": "/api/hooks/agent-completed",
    "notify_path": "/api/hooks/notify",
    "secret": "",
    "target_umo": "",
    "database": "agent_notifications.sqlite",
    "allowed_sources": ["codex", "opencode"],
    "max_body_bytes": 16384,
    "max_message_chars": 1500,
}


def load_plugin_config(config: dict[str, Any] | None) -> dict[str, Any]:
    merged = deepcopy(DEFAULT_CONFIG)
    if config:
        merged.update(dict(config))
    return merged
