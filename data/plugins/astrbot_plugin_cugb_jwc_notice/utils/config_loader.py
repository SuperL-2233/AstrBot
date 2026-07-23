from __future__ import annotations

from copy import deepcopy
from typing import Any

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "database": "jwc_notices.sqlite",
    "source_url": "https://jwc.cugb.edu.cn/xszq/",
    "teaching_updates_url": "https://jwc.cugb.edu.cn/jxdt/",
    "ai_college_news_url": "https://sai.cugb.edu.cn/xyxw/",
    "ai_college_notices_url": "https://sai.cugb.edu.cn/xygg/",
    "student_affairs_news_url": "https://bm.cugb.edu.cn/xgb/jjxg/",
    "student_affairs_notices_url": "https://bm.cugb.edu.cn/xgb/tzgg/",
    "refresh_interval_minutes": 10,
    "sync_since_date": "2025-09-01",
    "max_list_pages": 200,
    "max_search_pages_per_year": 100,
    "timeout_seconds": 10,
    "request_retries": 2,
    "target_umo": "",
    "owner_qq_id": "",
    "search_limit": 10,
    "detail_retry_limit": 20,
}


def load_plugin_config(config: dict[str, Any] | None) -> dict[str, Any]:
    merged = deepcopy(DEFAULT_CONFIG)
    if config:
        merged.update(dict(config))
    return merged
