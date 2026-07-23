from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urljoin

from .models import ContentLink, Notice, NoticeDetail
from .parser import NoticeParseError


class _CmsListParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.notices: list[Notice] = []
        self._in_item = False
        self._title_href: str | None = None
        self._title_parts: list[str] = []
        self._date_parts: list[str] = []
        self._capture: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = set((values.get("class") or "").split())
        if tag == "li" and "list_cont_li" in classes:
            self._in_item = True
            self._title_href = None
            self._title_parts = []
            self._date_parts = []
        elif self._in_item and tag == "a" and "p1" in classes and values.get("href"):
            self._title_href = values["href"]
            self._capture = "title"
        elif self._in_item and tag == "p":
            self._capture = "date"

    def handle_endtag(self, tag: str) -> None:
        if tag in {"a", "p"}:
            self._capture = None
        if tag == "li" and self._in_item:
            title = " ".join("".join(self._title_parts).split())
            published_date = " ".join("".join(self._date_parts).split())
            if self._title_href and title and published_date:
                self.notices.append(
                    Notice(
                        title=title,
                        published_date=published_date,
                        url=urljoin(self.base_url, self._title_href),
                    )
                )
            self._in_item = False
            self._capture = None

    def handle_data(self, data: str) -> None:
        if self._capture == "title":
            self._title_parts.append(data)
        elif self._capture == "date":
            self._date_parts.append(data)


def parse_cms_notices(html: str, base_url: str) -> list[Notice]:
    parser = _CmsListParser(base_url)
    parser.feed(html)
    parser.close()
    if not parser.notices:
        raise NoticeParseError("No CMS notices found; the upstream layout may have changed")
    return parser.notices


class _CmsDetailParser(HTMLParser):
    _block_tags = {"p", "div", "li", "tr", "section", "article", "h1", "h2", "h3"}

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.title_parts: list[str] = []
        self.description_parts: list[str] = []
        self.content_parts: list[str] = []
        self.links: list[ContentLink] = []
        self.images: list[str] = []
        self._container_depth = 0
        self._capture: str | None = None
        self._body_started = False
        self._ignored_tag: str | None = None
        self._link_href: str | None = None
        self._link_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = set((values.get("class") or "").split())
        if tag == "div" and "detail_cont" in classes:
            self._container_depth = 1
            return
        if not self._container_depth:
            return
        if tag == "div":
            self._container_depth += 1
        if tag in {"script", "style"}:
            self._ignored_tag = tag
            return
        if self._ignored_tag:
            return
        if tag == "p" and "tit" in classes:
            self._capture = "title"
            return
        if tag == "p" and "des" in classes:
            self._capture = "description"
            return
        if not self._body_started:
            return
        if tag == "br":
            self.content_parts.append("\n")
        elif tag == "a" and values.get("href"):
            self._link_href = urljoin(self.base_url, values["href"])
            self._link_parts = []
        elif tag == "img" and values.get("src"):
            self.images.append(urljoin(self.base_url, values["src"]))

    def handle_endtag(self, tag: str) -> None:
        if not self._container_depth:
            return
        if self._ignored_tag:
            if tag == self._ignored_tag:
                self._ignored_tag = None
            return
        if tag == "p" and self._capture == "title":
            self._capture = None
        elif tag == "p" and self._capture == "description":
            self._capture = None
            self._body_started = True
        elif self._body_started:
            if tag == "a" and self._link_href:
                text = " ".join("".join(self._link_parts).split())
                self.links.append(ContentLink(text=text, url=self._link_href))
                self._link_href = None
                self._link_parts = []
            if tag in self._block_tags:
                self.content_parts.append("\n")
        if tag == "div":
            self._container_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._container_depth or self._ignored_tag:
            return
        if self._capture == "title":
            self.title_parts.append(data)
        elif self._capture == "description":
            self.description_parts.append(data)
        elif self._body_started:
            self.content_parts.append(data)
            if self._link_href:
                self._link_parts.append(data)


def parse_cms_detail(
    html: str,
    base_url: str,
    *,
    notice_id: str,
    fallback_date: str,
) -> NoticeDetail:
    parser = _CmsDetailParser(base_url)
    parser.feed(html)
    parser.close()
    title = " ".join("".join(parser.title_parts).split())
    lines = [" ".join(line.split()) for line in "".join(parser.content_parts).splitlines()]
    content = "\n".join(line for line in lines if line)
    if not title or not content:
        raise NoticeParseError("CMS detail content was not found; the layout may have changed")

    description = " ".join("".join(parser.description_parts).split())
    date_match = re.search(r"(\d{4}-\d{2}-\d{2})", description)
    author_match = re.search(r"发布[：:]\s*(.*?)\s+点击", description)
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
