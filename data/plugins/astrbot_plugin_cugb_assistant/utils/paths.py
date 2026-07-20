from __future__ import annotations

from pathlib import Path

from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .constants import PLUGIN_NAME


def get_plugin_data_dir() -> Path:
    data_dir = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def resolve_data_path(raw_path: str) -> Path:
    candidate = Path(raw_path)
    if candidate.is_absolute():
        candidate.parent.mkdir(parents=True, exist_ok=True)
        return candidate
    path = get_plugin_data_dir() / candidate
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
