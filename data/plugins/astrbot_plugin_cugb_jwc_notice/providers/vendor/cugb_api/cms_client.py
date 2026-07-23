from __future__ import annotations

from urllib.parse import urljoin

from .client import NoticeClient
from .cms_parser import parse_cms_detail, parse_cms_notices
from .models import Notice, NoticeDetail


class CmsNoticeClient(NoticeClient):
    def __init__(self, *args, detail_path_prefix: str = "/c", **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.detail_path_prefix = "/" + detail_path_prefix.strip("/")

    def fetch_page(self, page: int) -> list[Notice]:
        if page < 1:
            raise ValueError("page must be positive")
        page_url = self.source_url if page == 1 else urljoin(
            self.source_url, f"index_{page}.shtml"
        )
        html, final_url = self._download(page_url)
        return parse_cms_notices(html, final_url)

    def fetch_detail(self, published_date: str, notice_id: str) -> NoticeDetail:
        detail_url = urljoin(
            self.source_url,
            f"{self.detail_path_prefix}/{published_date}/{notice_id}.shtml",
        )
        html, final_url = self._download(detail_url)
        return parse_cms_detail(
            html,
            final_url,
            notice_id=notice_id,
            fallback_date=published_date,
        )
