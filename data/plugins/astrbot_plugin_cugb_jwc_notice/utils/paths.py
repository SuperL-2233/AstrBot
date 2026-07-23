from __future__ import annotations

from pathlib import Path

from astrbot.core.utils.astrbot_path import get_astrbot_data_path

PLUGIN_NAME = "astrbot_plugin_cugb_jwc_notice"


def resolve_data_path(raw_path: str) -> Path:
    candidate = Path(raw_path)
    if candidate.is_absolute():
        candidate.parent.mkdir(parents=True, exist_ok=True)
        return candidate
    path = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME / candidate
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
