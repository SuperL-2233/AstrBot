"""Selected modules from SuperL-2233/CUGBJwcAPI v1.1.0."""

from .client import NoticeClient
from .models import ContentLink, Notice, NoticeDetail

__all__ = ["ContentLink", "Notice", "NoticeClient", "NoticeDetail"]
