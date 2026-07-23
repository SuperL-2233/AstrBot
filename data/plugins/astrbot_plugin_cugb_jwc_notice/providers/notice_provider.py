from __future__ import annotations

import asyncio
import gzip
import json
import re
import time
from datetime import date, datetime
from html import unescape
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from .vendor.cugb_jwc_api.client import NoticeClient, ResourceNotFound


class NoticeProvider:
    """Async boundary around the vendored synchronous public-page client."""

    CMS_SEARCH_URL = "https://cms.cugb.edu.cn/cms/front/search/result"
    CMS_SITE_ID = "168"
    STUDENT_NOTICE_CATALOG_ID = "17068"

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
        self.request_retries = retries
        self.max_list_pages = min(1000, max(1, int(config.get("max_list_pages", 200))))
        self.max_search_pages_per_year = min(
            1000, max(1, int(config.get("max_search_pages_per_year", 100)))
        )

    async def fetch_list(
        self, earliest_date: str, stop_at_ids: set[str] | None = None
    ) -> tuple[list[dict[str, str]], int]:
        return await asyncio.to_thread(
            self._fetch_list_sync, earliest_date, stop_at_ids or set()
        )

    def _fetch_list_sync(
        self, earliest_date: str, stop_at_ids: set[str]
    ) -> tuple[list[dict[str, str]], int]:
        cutoff = date.fromisoformat(earliest_date)
        rows: list[dict[str, str]] = []
        seen: set[str] = set()
        pages_fetched = 0
        completed_range = False
        for page in range(1, self.max_list_pages + 1):
            try:
                notices = self.client.fetch_page(page)
            except ResourceNotFound:
                if page == 1:
                    raise
                completed_range = True
                break
            pages_fetched += 1
            reached_cutoff = False
            reached_known = False
            for notice in notices:
                published = date.fromisoformat(notice.published_date)
                if published < cutoff:
                    reached_cutoff = True
                    continue
                if notice.notice_id in stop_at_ids:
                    reached_known = True
                if notice.notice_id in seen:
                    continue
                seen.add(notice.notice_id)
                rows.append(notice.to_dict())
            if reached_cutoff or reached_known:
                completed_range = True
                break
        else:
            raise RuntimeError(
                f"在 {self.max_list_pages} 页内未到达同步起始日期 {earliest_date}"
            )
        if not completed_range:
            raise RuntimeError("教务处列表同步未能确认结束位置")

        if (
            not stop_at_ids
            and rows
            and date.fromisoformat(rows[-1]["published_date"]) > cutoff
        ):
            search_rows, search_pages = self._fetch_search_archive(cutoff)
            pages_fetched += search_pages
            for row in search_rows:
                if row["id"] in seen:
                    continue
                seen.add(row["id"])
                rows.append(row)
            rows.sort(key=lambda row: (row["published_date"], row["id"]), reverse=True)
        return rows, pages_fetched

    def _fetch_search_archive(self, cutoff: date) -> tuple[list[dict[str, str]], int]:
        rows: list[dict[str, str]] = []
        pages_fetched = 0
        current_year = datetime.now().astimezone().year
        for year in range(cutoff.year, current_year + 1):
            page_index = 0
            processed = 0
            while page_index < self.max_search_pages_per_year:
                query = urlencode(
                    {
                        "siteID": self.CMS_SITE_ID,
                        "query": str(year),
                        "pageIndex": page_index,
                        "pageSize": 10,
                    }
                )
                payload = self._download_search_json(f"{self.CMS_SEARCH_URL}?{query}")
                if payload.get("status") != 1 or not isinstance(
                    payload.get("data"), dict
                ):
                    raise RuntimeError("教务处历史搜索接口返回了无效响应")
                result = payload["data"]
                items = result.get("data")
                if not isinstance(items, list):
                    raise RuntimeError("教务处历史搜索结果不是列表")
                pages_fetched += 1
                if not items:
                    break
                processed += len(items)
                for item in items:
                    row = self._normalize_search_item(item, cutoff, year)
                    if row:
                        rows.append(row)
                if processed >= int(result.get("total") or 0):
                    break
                page_index += 1
            else:
                raise RuntimeError(
                    f"历史搜索 {year} 年结果超过安全上限 "
                    f"{self.max_search_pages_per_year} 页"
                )
        return rows, pages_fetched

    def _download_search_json(self, url: str) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(self.request_retries + 1):
            try:
                request = Request(
                    url,
                    headers={
                        "User-Agent": "AstrBot-CUGB-JWC-Notice/0.2",
                        "Accept": "application/json",
                        "Accept-Encoding": "gzip",
                        "Origin": "https://jwc.cugb.edu.cn",
                        "Referer": "https://jwc.cugb.edu.cn/search/result.shtml",
                    },
                )
                with urlopen(request, timeout=self.client.timeout_seconds) as response:
                    body = response.read()
                    if response.headers.get("Content-Encoding", "").lower() == "gzip":
                        body = gzip.decompress(body)
                    payload = json.loads(body.decode("utf-8", errors="replace"))
                    if not isinstance(payload, dict):
                        raise ValueError("教务处历史搜索响应不是 JSON 对象")
                    return payload
            except (
                HTTPError,
                URLError,
                TimeoutError,
                OSError,
                UnicodeError,
                json.JSONDecodeError,
                ValueError,
            ) as exc:
                last_error = exc
                if attempt < self.request_retries:
                    time.sleep(2**attempt)
        raise RuntimeError(f"教务处历史搜索请求失败：{last_error}") from last_error

    @classmethod
    def _normalize_search_item(
        cls, item: object, cutoff: date, expected_year: int
    ) -> dict[str, str] | None:
        if not isinstance(item, dict):
            return None
        if str(item.get("catalogID") or "") != cls.STUDENT_NOTICE_CATALOG_ID:
            return None
        published_text = str(item.get("publishDate") or item.get("addTime") or "")
        try:
            published = date.fromisoformat(published_text[:10])
        except ValueError:
            return None
        if published < cutoff or published.year != expected_year:
            return None
        url = str(item.get("jsonLink") or item.get("url") or "").strip()
        notice_id = str(item.get("id") or "").strip()
        if not url or not notice_id:
            return None
        title = unescape(re.sub(r"<[^>]+>", "", str(item.get("title") or "")))
        return {
            "id": notice_id,
            "title": " ".join(title.split()),
            "published_date": published.isoformat(),
            "url": url,
        }

    async def fetch_detail(
        self, published_date: str, notice_id: str
    ) -> dict[str, object]:
        detail = await asyncio.to_thread(
            self.client.fetch_detail, published_date, notice_id
        )
        return detail.to_dict()
