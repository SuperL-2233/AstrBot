from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlparse

from .vendor.cugb_jwc_api.client import NoticeClient


class NoticeProvider:
    """Async boundary around the vendored synchronous public-page client."""

    def __init__(self, config: dict[str, Any]) -> None:
        source_url = str(config.get("source_url", "")).strip()
        parsed = urlparse(source_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("source_url must be an HTTPS URL")
        retries = int(config.get("request_retries", 2))
        if not 0 <= retries <= 10:
            raise ValueError("request_retries must be between 0 and 10")
        self.client = NoticeClient(
            source_url,
            timeout_seconds=max(1, int(config.get("timeout_seconds", 10))),
            retries=retries,
        )
        self.list_pages = min(20, max(1, int(config.get("list_pages", 1))))

    async def fetch_list(self) -> list[dict[str, str]]:
        return await asyncio.to_thread(self._fetch_list_sync)

    def _fetch_list_sync(self) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        seen: set[str] = set()
        for page in range(1, self.list_pages + 1):
            for notice in self.client.fetch_page(page):
                if notice.notice_id in seen:
                    continue
                seen.add(notice.notice_id)
                rows.append(notice.to_dict())
        return rows

    async def fetch_detail(
        self, published_date: str, notice_id: str
    ) -> dict[str, object]:
        detail = await asyncio.to_thread(
            self.client.fetch_detail, published_date, notice_id
        )
        return detail.to_dict()
