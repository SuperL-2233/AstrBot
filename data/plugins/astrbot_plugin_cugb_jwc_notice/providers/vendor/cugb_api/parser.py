from __future__ import annotations

from html.parser import HTMLParser
import re
from urllib.parse import urljoin

from .models import ContentLink, Notice, NoticeDetail


class NoticeParseError(ValueError):
    """Raised when the expected notice list cannot be found."""


class _NoticeListParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.notices: list[Notice] = []
        self._container_depth = 0
        self._anchor_href: str | None = None
        self._title_parts: list[str] = []
        self._date_parts: list[str] = []
        self._capture: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "div" and values.get("id") == "list_detail_box":
            self._container_depth = 1
            return
        if not self._container_depth:
            return
        if tag == "div":
            self._container_depth += 1
        if tag == "a" and values.get("href"):
            self._anchor_href = values["href"]
            self._title_parts = []
            self._date_parts = []
        classes = set((values.get("class") or "").split())
        if self._anchor_href and "list_con_main" in classes:
            self._capture = "title"
        elif self._anchor_href and "list_con_time" in classes:
            self._capture = "date"

    def handle_endtag(self, tag: str) -> None:
        if not self._container_depth:
            return
        if tag == "a" and self._anchor_href:
            title = " ".join("".join(self._title_parts).split())
            published_date = " ".join("".join(self._date_parts).split())
            if title and published_date:
                self.notices.append(
                    Notice(
                        title=title,
                        published_date=published_date,
                        url=urljoin(self.base_url, self._anchor_href),
                    )
                )
            self._anchor_href = None
            self._capture = None
        elif tag == "div":
            self._capture = None
            self._container_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._capture == "title":
            self._title_parts.append(data)
        elif self._capture == "date":
            self._date_parts.append(data)


def parse_notices(html: str, base_url: str) -> list[Notice]:
    parser = _NoticeListParser(base_url)
    parser.feed(html)
    parser.close()
    if not parser.notices:
        raise NoticeParseError(
            "No notices found in #list_detail_box; the upstream layout may have changed"
        )
    return parser.notices


class _NoticeDetailParser(HTMLParser):
    _block_tags = {"p", "div", "li", "tr", "section", "article", "h1", "h2", "h3"}

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.title_parts: list[str] = []
        self.info_parts: list[str] = []
        self.content_parts: list[str] = []
        self.links: list[ContentLink] = []
        self.images: list[str] = []
        self._title_depth = 0
        self._info_depth = 0
        self._content_depth = 0
        self._ignored_tag: str | None = None
        self._link_href: str | None = None
        self._link_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = set((values.get("class") or "").split())
        if tag == "div" and "detail_title" in classes:
            self._title_depth = 1
            return
        if tag == "div" and "detail_info" in classes:
            self._info_depth = 1
            return
        if tag == "div" and "detail_content_box" in classes:
            self._content_depth = 1
            return

        if tag == "div":
            if self._title_depth:
                self._title_depth += 1
            if self._info_depth:
                self._info_depth += 1
            if self._content_depth:
                self._content_depth += 1

        if self._content_depth:
            if tag in {"script", "style"}:
                self._ignored_tag = tag
            elif self._ignored_tag:
                return
            elif tag == "br":
                self.content_parts.append("\n")
            elif tag == "a" and values.get("href"):
                self._link_href = urljoin(self.base_url, values["href"])
                self._link_parts = []
            elif tag == "img" and values.get("src"):
                self.images.append(urljoin(self.base_url, values["src"]))

    def handle_endtag(self, tag: str) -> None:
        if self._ignored_tag:
            if tag == self._ignored_tag:
                self._ignored_tag = None
            return
        if self._content_depth and tag == "a" and self._link_href:
            text = " ".join("".join(self._link_parts).split())
            self.links.append(ContentLink(text=text, url=self._link_href))
            self._link_href = None
            self._link_parts = []
        if self._content_depth and tag in self._block_tags:
            self.content_parts.append("\n")
        if tag == "div":
            if self._title_depth:
                self._title_depth -= 1
            if self._info_depth:
                self._info_depth -= 1
            if self._content_depth:
                self._content_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._ignored_tag:
            return
        if self._title_depth:
            self.title_parts.append(data)
        if self._info_depth:
            self.info_parts.append(data)
        if self._content_depth:
            self.content_parts.append(data)
            if self._link_href:
                self._link_parts.append(data)


def parse_notice_detail(
    html: str,
    base_url: str,
    *,
    notice_id: str,
    fallback_date: str,
) -> NoticeDetail:
    parser = _NoticeDetailParser(base_url)
    parser.feed(html)
    parser.close()
    title = " ".join("".join(parser.title_parts).split())
    content_lines = [" ".join(line.split()) for line in "".join(parser.content_parts).splitlines()]
    content = "\n".join(line for line in content_lines if line)
    if not title or not content:
        raise NoticeParseError("Notice detail content was not found; the layout may have changed")

    info = " ".join("".join(parser.info_parts).split())
    author_match = re.search(r"作者[：:]\s*(.*?)\s+发布时间", info)
    date_match = re.search(r"发布时间[：:]\s*(\d{4}-\d{2}-\d{2})", info)
    return NoticeDetail(
        notice_id=notice_id,
        title=title,
        published_date=date_match.group(1) if date_match else fallback_date,
        author=author_match.group(1) if author_match else "",
        url=base_url,
        content=content,
        links=tuple(parser.links),
        images=tuple(dict.fromkeys(parser.images)),
    )
