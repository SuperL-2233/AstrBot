from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class Notice:
    title: str
    published_date: str
    url: str

    @property
    def notice_id(self) -> str:
        return PurePosixPath(urlsplit(self.url).path).stem

    def to_dict(self) -> dict[str, str]:
        return {**asdict(self), "id": self.notice_id}


@dataclass(frozen=True, slots=True)
class ContentLink:
    text: str
    url: str


@dataclass(frozen=True, slots=True)
class NoticeDetail:
    notice_id: str
    title: str
    published_date: str
    author: str
    url: str
    content: str
    links: tuple[ContentLink, ...] = ()
    images: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            **asdict(self),
            "links": [asdict(link) for link in self.links],
            "images": list(self.images),
        }
