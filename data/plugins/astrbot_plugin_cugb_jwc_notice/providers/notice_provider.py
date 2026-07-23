from __future__ import annotations

import asyncio
import gzip
import json
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from html import unescape
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from .vendor.cugb_api.client import NoticeClient, ResourceNotFound
from .vendor.cugb_api.cms_client import CmsNoticeClient


@dataclass(frozen=True, slots=True)
class SourceSpec:
    key: str
    label: str
    config_key: str
    default_url: str
    kind: str
    detail_path_prefix: str = "/c"
    search_catalog_id: str = ""


SOURCES: dict[str, SourceSpec] = {
    spec.key: spec
    for spec in (
        SourceSpec(
            key="jwc_student",
            label="教务处·学生专区",
            config_key="source_url",
            default_url="https://jwc.cugb.edu.cn/xszq/",
            kind="jwc",
            search_catalog_id="17068",
        ),
        SourceSpec(
            key="jwc_teaching",
            label="教务处·教学动态",
            config_key="teaching_updates_url",
            default_url="https://jwc.cugb.edu.cn/jxdt/",
            kind="jwc",
        ),
        SourceSpec(
            key="ai_news",
            label="人工智能学院·新闻动态",
            config_key="ai_college_news_url",
            default_url="https://sai.cugb.edu.cn/xyxw/",
            kind="cms",
        ),
        SourceSpec(
            key="ai_notice",
            label="人工智能学院·公告",
            config_key="ai_college_notices_url",
            default_url="https://sai.cugb.edu.cn/xygg/",
            kind="cms",
        ),
        SourceSpec(
            key="xgb_news",
            label="学工处·新闻动态",
            config_key="student_affairs_news_url",
            default_url="https://bm.cugb.edu.cn/xgb/jjxg/",
            kind="cms",
            detail_path_prefix="/xgb/c",
        ),
        SourceSpec(
            key="xgb_notice",
            label="学工处·通知公告",
            config_key="student_affairs_notices_url",
            default_url="https://bm.cugb.edu.cn/xgb/tzgg/",
            kind="cms",
            detail_path_prefix="/xgb/c",
        ),
    )
}

_SOURCE_ALIASES: dict[str, str] = {
    "jwc": "jwc_student",
    "jwc_student": "jwc_student",
    "xszq": "jwc_student",
    "教务": "jwc_student",
    "教务处": "jwc_student",
    "教务通知": "jwc_student",
    "教务公告": "jwc_student",
    "学生专区": "jwc_student",
    "jwc_teaching": "jwc_teaching",
    "jxdt": "jwc_teaching",
    "教学动态": "jwc_teaching",
    "ai_news": "ai_news",
    "ai-news": "ai_news",
    "ainews": "ai_news",
    "ai新闻": "ai_news",
    "智能新闻": "ai_news",
    "人工智能新闻": "ai_news",
    "ai_notice": "ai_notice",
    "ai_notices": "ai_notice",
    "ai-notice": "ai_notice",
    "ai公告": "ai_notice",
    "智能公告": "ai_notice",
    "人工智能公告": "ai_notice",
    "xgb_news": "xgb_news",
    "xgb-news": "xgb_news",
    "学工新闻": "xgb_news",
    "学工动态": "xgb_news",
    "xgb_notice": "xgb_notice",
    "xgb_notices": "xgb_notice",
    "xgb-notice": "xgb_notice",
    "学工公告": "xgb_notice",
    "学工通知": "xgb_notice",
}


def resolve_source(value: str) -> str:
    """Map a user or LLM supplied source token to a registry key.

    Args:
        value: Source key or alias; empty means every source.

    Returns:
        The canonical source key, or an empty string for all sources.

    Raises:
        ValueError: If the token is not a known key or alias.
    """
    token = value.strip()
    if not token:
        return ""
    key = _SOURCE_ALIASES.get(token.casefold()) or _SOURCE_ALIASES.get(token)
    if key:
        return key
    if token in SOURCES:
        return token
    raise ValueError(f"未知来源：{token}")


def source_label(key: str) -> str:
    return SOURCES[key].label if key in SOURCES else "全部来源"


class NoticeProvider:
    """Async boundary around the vendored synchronous public-page clients."""

    CMS_SEARCH_URL = "https://cms.cugb.edu.cn/cms/front/search/result"
    CMS_SITE_ID = "168"

    def __init__(self, config: dict[str, Any]) -> None:
        retries = int(config.get("request_retries", 2))
        if not 0 <= retries <= 10:
            raise ValueError("request_retries must be between 0 and 10")
        self.timeout_seconds = max(1, int(config.get("timeout_seconds", 10)))
        self.clients: dict[str, NoticeClient] = {}
        for key, spec in SOURCES.items():
            source_url = str(config.get(spec.config_key) or spec.default_url).strip()
            parsed = urlparse(source_url)
            if parsed.scheme != "https" or not parsed.netloc:
                raise ValueError(f"{spec.config_key} must be an HTTPS URL")
            client_class = NoticeClient if spec.kind == "jwc" else CmsNoticeClient
            kwargs: dict[str, Any] = {}
            if spec.kind == "cms":
                kwargs["detail_path_prefix"] = spec.detail_path_prefix
            self.clients[key] = client_class(
                source_url,
                timeout_seconds=self.timeout_seconds,
                retries=retries,
                **kwargs,
            )
        self.request_retries = retries
        self.max_list_pages = min(1000, max(1, int(config.get("max_list_pages", 200))))
        self.max_search_pages_per_year = min(
            1000, max(1, int(config.get("max_search_pages_per_year", 100)))
        )

    async def fetch_list(
        self, source: str, earliest_date: str, stop_at_ids: set[str] | None = None
    ) -> tuple[list[dict[str, str]], int]:
        return await asyncio.to_thread(
            self._fetch_list_sync, source, earliest_date, stop_at_ids or set()
        )

    def _fetch_list_sync(
        self, source: str, earliest_date: str, stop_at_ids: set[str]
    ) -> tuple[list[dict[str, str]], int]:
        spec = SOURCES[source]
        client = self.clients[source]
        cutoff = date.fromisoformat(earliest_date)
        rows: list[dict[str, str]] = []
        seen: set[str] = set()
        pages_fetched = 0
        completed_range = False
        for page in range(1, self.max_list_pages + 1):
            try:
                notices = client.fetch_page(page)
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
                f"{spec.label} 在 {self.max_list_pages} 页内未到达同步起始日期 {earliest_date}"
            )
        if not completed_range:
            raise RuntimeError(f"{spec.label} 列表同步未能确认结束位置")

        if (
            spec.search_catalog_id
            and not stop_at_ids
            and rows
            and date.fromisoformat(rows[-1]["published_date"]) > cutoff
        ):
            search_rows, search_pages = self._fetch_search_archive(
                spec.search_catalog_id, cutoff
            )
            pages_fetched += search_pages
            for row in search_rows:
                if row["id"] in seen:
                    continue
                seen.add(row["id"])
                rows.append(row)
            rows.sort(key=lambda row: (row["published_date"], row["id"]), reverse=True)
        return rows, pages_fetched

    def _fetch_search_archive(
        self, catalog_id: str, cutoff: date
    ) -> tuple[list[dict[str, str]], int]:
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
                    row = self._normalize_search_item(item, catalog_id, cutoff, year)
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
                        "User-Agent": "AstrBot-CUGB-Notice/0.4",
                        "Accept": "application/json",
                        "Accept-Encoding": "gzip",
                        "Origin": "https://jwc.cugb.edu.cn",
                        "Referer": "https://jwc.cugb.edu.cn/search/result.shtml",
                    },
                )
                with urlopen(request, timeout=self.timeout_seconds) as response:
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
        cls, item: object, catalog_id: str, cutoff: date, expected_year: int
    ) -> dict[str, str] | None:
        if not isinstance(item, dict):
            return None
        if str(item.get("catalogID") or "") != catalog_id:
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
        self, source: str, published_date: str, notice_id: str
    ) -> dict[str, object]:
        detail = await asyncio.to_thread(
            self.clients[source].fetch_detail, published_date, notice_id
        )
        return detail.to_dict()
