from __future__ import annotations

import gzip
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from .models import Notice, NoticeDetail
from .parser import parse_notice_detail, parse_notices


class FetchError(RuntimeError):
    """Raised after all attempts to download the notice page fail."""


class ResourceNotFound(FetchError):
    """Raised when a requested upstream page does not exist."""


class NoticeClient:
    def __init__(
        self,
        source_url: str,
        timeout_seconds: float = 10,
        retries: int = 2,
    ) -> None:
        self.source_url = source_url
        self.timeout_seconds = timeout_seconds
        self.retries = retries

    def fetch(self) -> list[Notice]:
        return self.fetch_page(1)

    def fetch_page(self, page: int) -> list[Notice]:
        if page < 1:
            raise ValueError("page must be positive")
        page_url = self.source_url if page == 1 else urljoin(
            self.source_url, f"index_{page}.shtml"
        )
        html, final_url = self._download(page_url)
        return parse_notices(html, final_url)

    def fetch_detail(self, published_date: str, notice_id: str) -> NoticeDetail:
        detail_url = urljoin(
            self.source_url,
            f"/c/{published_date}/{notice_id}.shtml",
        )
        html, final_url = self._download(detail_url)
        return parse_notice_detail(
            html,
            final_url,
            notice_id=notice_id,
            fallback_date=published_date,
        )

    def _download(self, url: str) -> tuple[str, str]:
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                request = Request(
                    url,
                    headers={
                        "User-Agent": "Notice-API/2.1",
                        "Accept": "text/html,application/xhtml+xml",
                        "Accept-Encoding": "gzip",
                    },
                )
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    body = response.read()
                    if response.headers.get("Content-Encoding", "").lower() == "gzip":
                        body = gzip.decompress(body)
                    charset = response.headers.get_content_charset() or "utf-8"
                    html = body.decode(charset, errors="replace")
                    return html, response.geturl()
            except HTTPError as error:
                if error.code == 404:
                    raise ResourceNotFound(f"Upstream resource not found: {url}") from error
                last_error = error
                if attempt < self.retries:
                    time.sleep(2**attempt)
            except (URLError, TimeoutError, OSError, UnicodeError) as error:
                last_error = error
                if attempt < self.retries:
                    time.sleep(2**attempt)
        raise FetchError(
            f"Failed to fetch {url!r} after {self.retries + 1} attempts"
        ) from last_error
