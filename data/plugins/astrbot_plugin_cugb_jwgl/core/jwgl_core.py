from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


class JwglError(RuntimeError):
    """教务系统操作中用户可读的失败。"""


class AuthenticationRequired(JwglError):
    """当前页面回到了 CAS 登录页，登录态已失效。"""


TERM_ORDER = {"春": 1, "夏": 2, "秋": 3}  # 成绩页学年按自然年标注，秋为 9 月


def utc_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def sanitize_url(url: str | None) -> str | None:
    """去掉 query 和 fragment，避免落库 CAS ticket 等临时凭据。"""
    if not url:
        return None
    try:
        parts = urlsplit(url)
        if not parts.scheme or not parts.netloc:
            return "<relative-url>"
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except (TypeError, ValueError):
        return "<invalid-url>"


def safe_exception_name(error: BaseException) -> str:
    return type(error).__name__


def ensure_private_file(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass


@dataclass(frozen=True, slots=True)
class JwglSettings:
    username: str | None
    password: str | None
    portal_url: str
    session_cookie_path: Path
    profile_dir: Path
    headless: bool
    browser_channel: str
    navigation_timeout_ms: int


class SessionCookieStore:
    """在多次 Chromium 启动之间只持久化 WebVPN 进程期 cookie。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    @staticmethod
    def _allowed(cookie: dict[str, Any]) -> bool:
        domain = str(cookie.get("domain") or "").lower().lstrip(".")
        name = str(cookie.get("name") or "")
        return domain == "elib.cugb.edu.cn" and (
            name.startswith("wengine_vpn_ticket") or name == "refresh"
        )

    @staticmethod
    def _serializable(cookie: dict[str, Any]) -> dict[str, Any]:
        result = {
            "name": str(cookie["name"]),
            "value": str(cookie["value"]),
            "domain": str(cookie["domain"]),
            "path": str(cookie.get("path") or "/"),
            "httpOnly": bool(cookie.get("httpOnly")),
            "secure": bool(cookie.get("secure")),
        }
        same_site = cookie.get("sameSite")
        if same_site in {"Strict", "Lax", "None"}:
            result["sameSite"] = same_site
        return result

    async def restore(self, context: Any) -> int:
        if not self.path.is_file():
            return 0
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            raw_cookies = (
                payload.get("cookies", []) if isinstance(payload, dict) else []
            )
            cookies = [
                self._serializable(cookie)
                for cookie in raw_cookies
                if isinstance(cookie, dict)
                and self._allowed(cookie)
                and cookie.get("value")
            ]
            if cookies:
                await context.add_cookies(cookies)
            return len(cookies)
        except Exception as error:
            raise JwglError("保存的 WebVPN 会话 cookie 文件无效或不可读") from error

    async def save(self, context: Any) -> int:
        cookies = [
            self._serializable(cookie)
            for cookie in await context.cookies()
            if self._allowed(cookie) and cookie.get("value")
        ]
        if not cookies:
            return 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(
                {"version": 1, "saved_at": utc_iso(), "cookies": cookies},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)
        ensure_private_file(self.path)
        return len(cookies)


async def _is_visible(page: Any, selector: str) -> bool:
    locator = page.locator(selector).first
    try:
        return await locator.count() > 0 and await locator.is_visible()
    except Exception:
        return False


async def is_login_page(page: Any) -> bool:
    if await _is_visible(page, "#double_username"):
        return True
    if await _is_visible(page, "#J_mobile_Captcha"):
        return True
    if await _is_visible(page, "#username") and await _is_visible(page, "#password"):
        return True
    path = urlsplit(page.url).path.lower()
    return path.endswith("/login") and await page.locator("#fm1").count() > 0


async def reject_legacy_affair_login(page: Any) -> None:
    if "/academic/common/security/affairlogin" in urlsplit(page.url).path.lower():
        raise JwglError("教务系统回退到了已废弃的 affairLogin 页面，门户 SSO 未建立")


async def _text(page: Any, selector: str) -> str:
    locator = page.locator(selector).first
    try:
        if await locator.count() and await locator.is_visible():
            return (await locator.inner_text()).strip()
    except Exception:
        pass
    return ""


@dataclass(slots=True)
class _Table:
    rows: list[list[str]]
    current_row: list[str] | None = None
    current_cell: list[str] | None = None


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[_Table] = []
        self.tables: list[list[list[str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.lower()
        if lowered == "table":
            self.stack.append(_Table([]))
        elif lowered == "tr" and self.stack:
            self.stack[-1].current_row = []
        elif lowered in {"th", "td"} and self.stack:
            self.stack[-1].current_cell = []
        elif lowered == "br":
            for table in self.stack:
                if table.current_cell is not None:
                    table.current_cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered in {"th", "td"} and self.stack:
            table = self.stack[-1]
            if table.current_row is not None and table.current_cell is not None:
                table.current_row.append(_normalize_text("".join(table.current_cell)))
            table.current_cell = None
        elif lowered == "tr" and self.stack:
            table = self.stack[-1]
            if table.current_row is not None and any(table.current_row):
                table.rows.append(table.current_row)
            table.current_row = None
        elif lowered == "table" and self.stack:
            table = self.stack.pop()
            if table.rows:
                self.tables.append(table.rows)

    def handle_data(self, data: str) -> None:
        for table in self.stack:
            if table.current_cell is not None:
                table.current_cell.append(data)


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _normalize_header(value: str) -> str:
    return re.sub(r"\s+", "", value)


def parse_tables(html: str) -> list[list[list[str]]]:
    parser = _TableParser()
    parser.feed(html)
    parser.close()
    return parser.tables


def _unique_headers(headers: Iterable[str]) -> list[str]:
    result: list[str] = []
    counts: dict[str, int] = {}
    for index, raw in enumerate(headers, start=1):
        header = _normalize_header(raw) or f"column_{index}"
        counts[header] = counts.get(header, 0) + 1
        suffix = f"_{counts[header]}" if counts[header] > 1 else ""
        result.append(f"{header}{suffix}")
    return result


def find_table_records(html: str, required_headers: set[str]) -> list[dict[str, str]]:
    best: tuple[int, list[list[str]], int] | None = None
    for table in parse_tables(html):
        for row_index, row in enumerate(table[:5]):
            normalized = {_normalize_header(cell) for cell in row}
            score = len(required_headers & normalized)
            if required_headers <= normalized:
                candidate = (score * 1_000 + len(table), table, row_index)
                if best is None or candidate[0] > best[0]:
                    best = candidate
    if best is None:
        expected = ", ".join(sorted(required_headers))
        raise JwglError(f"未找到包含表头 {expected} 的结果表格")

    _score, table, header_index = best
    headers = _unique_headers(table[header_index])
    records: list[dict[str, str]] = []
    for row in table[header_index + 1 :]:
        values = row[: len(headers)] + [""] * max(0, len(headers) - len(row))
        record = {headers[index]: values[index] for index in range(len(headers))}
        if any(record.values()):
            records.append(record)
    return records


def parse_courses(html: str) -> list[dict[str, str]]:
    return find_table_records(html, {"课程号", "课程名称", "任课教师"})


def parse_scores(html: str) -> list[dict[str, str]]:
    return find_table_records(html, {"课程名", "总评", "学分"})


def parse_exams(html: str) -> list[dict[str, str]]:
    return find_table_records(html, {"课程号", "课程名称", "考试时间"})
