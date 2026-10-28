from __future__ import annotations

from copy import deepcopy
from typing import Any

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "database": "jwgl_monitor.sqlite",
    "portal_url": "",
    "username": "",
    "password": "",
    "headless": True,
    "browser_channel": "",
    "browser_profile_dir": "browser-profile",
    "session_cookie_file": "webvpn_session_cookies.json",
    "navigation_timeout_seconds": 30,
    "poll_interval_minutes": 15,
    "poll_jitter_minutes": 2,
    "scores_probe_max_terms": 6,
    "timezone": "Asia/Shanghai",
    "sms_timeout_seconds": 300,
    "target_umo": "",
    "owner_qq_id": "",
    "mportal_enabled": True,
    "mportal_base_url": "https://mportal.cugb.edu.cn",
    "class_reminder_enabled": True,
    "class_reminder_lead_minutes": 15,
    "class_reminder_check_seconds": 60,
    "semester_start_date": "",
    "section_start_times": {
        "1": "08:00",
        "2": "08:50",
        "3": "09:50",
        "4": "10:40",
        "5": "11:30",
        "6": "13:30",
        "7": "14:20",
        "8": "15:20",
        "9": "16:10",
        "10": "17:10",
        "11": "18:00",
        "12": "19:00",
        "13": "19:50",
        "14": "20:40",
    },
}


def load_plugin_config(config: dict[str, Any] | None) -> dict[str, Any]:
    merged = deepcopy(DEFAULT_CONFIG)
    if config:
        merged.update(dict(config))
    return merged
