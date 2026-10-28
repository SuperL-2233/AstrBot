from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import quote, urlsplit

from astrbot.api import logger

from .jwgl_core import AuthenticationRequired, JwglError, is_login_page

DEFAULT_MPORTAL_BASE_URL = "https://mportal.cugb.edu.cn"

_TIMETABLE_PAGE_PATH = "/timetable/wap/default/index"


def _login_entry_url(base_url: str) -> str:
    """Build the mportal login entry URL with a concrete redirect target.

    The school's own pattern always passes the target page's absolute URL
    (an empty redirect makes the SSO callback resolve back onto itself,
    looping into net::ERR_TOO_MANY_REDIRECTS once authenticated).
    """
    base = base_url.rstrip("/")
    target = quote(f"{base}{_TIMETABLE_PAGE_PATH}", safe="")
    return f"{base}/uc/wap/login?redirect={target}"


async def probe_needs_login(page: Any, base_url: str, timeout_ms: int) -> bool:
    """Report whether mportal API access currently requires (re-)login.

    Uses the context's request API against get-index instead of navigating
    the shared page (every navigation is a hijack risk for other flows).
    The URL alone is not enough: an expired app session does not redirect
    but answers e=10013 JSON ("用户信息已失效"), so the body decides too.

    Args:
        page: Playwright page (only its request context is used).
        base_url: Mportal base URL.
        timeout_ms: Request timeout in milliseconds.

    Returns:
        True when the CAS SSO or the mportal app session is missing/expired;
        also True on network or malformed-response errors (fail-safe).
    """
    try:
        response = await page.context.request.get(
            f"{base_url.rstrip('/')}/timetable/wap/default/get-index",
            headers={"X-Requested-With": "XMLHttpRequest"},
            timeout=timeout_ms,
        )
        try:
            final = urlsplit(response.url)
            expected_host = urlsplit(base_url).hostname or ""
            if (final.hostname or "") != expected_host or "login" in final.path.lower():
                return True
            body = await response.json()
            return not (isinstance(body, dict) and body.get("e") == 0)
        finally:
            await response.dispose()
    except Exception:
        return True


async def open_login_form(page: Any, base_url: str, timeout_ms: int) -> bool:
    """Open the mportal login entry and report whether a CAS form showed up.

    With a valid CAS ticket the flow bounces straight back to the timetable
    page, establishing the mportal app session without any user interaction;
    otherwise it settles on the CAS login form.

    Args:
        page: Playwright page to navigate.
        base_url: Mportal base URL.
        timeout_ms: Overall navigation/settle budget in milliseconds.

    Returns:
        True when the flow settled on the CAS login form (ready to drive);
        False when SSO bounced back to mportal (session established).
    """
    expected_host = urlsplit(base_url).hostname or ""
    try:
        await page.goto(
            _login_entry_url(base_url),
            wait_until="domcontentloaded",
            timeout=timeout_ms,
        )
    except Exception as error:
        # 跳转链异常（如重定向循环）时仍依据当前页面状态判定
        logger.warning("打开 mportal 登录入口时导航异常：%s", type(error).__name__)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(5.0, timeout_ms / 1000)
    login_since: float | None = None
    while loop.time() < deadline:
        if (urlsplit(page.url).hostname or "") == expected_host:
            return False
        if await is_login_page(page):
            login_since = login_since or loop.time()
            if loop.time() - login_since > 4:
                return True
        else:
            login_since = None
        await asyncio.sleep(0.5)
    return True


TERM_CODE_TO_LABEL = {1: "秋", 2: "春", 3: "夏"}
TERM_LABEL_TO_CODE = {label: code for code, label in TERM_CODE_TO_LABEL.items()}

# Sanity window for the month a term's startday should fall into.
_TERM_START_MONTHS = {1: {8, 9, 10}, 2: {1, 2, 3}, 3: {5, 6, 7}}


def period_to_year_term(period: str) -> tuple[str, int] | None:
    """Map a jwgl period label like "2026 夏" to the mportal (year, term code).

    Returns:
        Tuple of (year string, term code) or None when the label is unknown.
    """
    parts = period.split()
    if len(parts) == 2 and parts[0].isdigit() and parts[1] in TERM_LABEL_TO_CODE:
        return parts[0], TERM_LABEL_TO_CODE[parts[1]]
    return None


def term_countweek(term_info: dict[str, Any], year: str, term: int) -> int | None:
    """Find the week count of one term inside a get-index termInfo payload.

    Args:
        term_info: The "d" object of the get-index response.
        year: Four-digit year string.
        term: Mportal term code (1=秋, 2=春, 3=夏).

    Returns:
        countweek as int, or None when the term is absent or inconsistent.

    Raises:
        JwglError: When the term's startday month contradicts the term code.
    """
    for entry in term_info.get("termInfo", []):
        if str(entry.get("year")) != year or int(entry.get("term", -1)) != term:
            continue
        startday = str(entry.get("startday", ""))
        try:
            month = int(startday.split("-")[1])
        except (IndexError, ValueError):
            month = 0
        if month and month not in _TERM_START_MONTHS[term]:
            raise JwglError(
                f"mportal 学期信息异常：{year} term={term} 的 startday 为 {startday}"
            )
        try:
            return int(entry.get("countweek"))
        except (TypeError, ValueError):
            return None
    return None


def parse_week_payload(body: dict[str, Any]) -> dict[str, Any]:
    """Normalize one get-datatmp response into weekdays plus occurrences.

    Args:
        body: Full JSON body of the get-datatmp response.

    Returns:
        Dict with "weekdays" (7 ISO dates) and "occurrences" (normalized
        course cells carrying course_code, course_name, teacher, location,
        weekday, sections and date).

    Raises:
        JwglError: When the response reports failure or is malformed.
    """
    if not isinstance(body, dict) or body.get("e") != 0:
        message = (
            body.get("m", "未知响应") if isinstance(body, dict) else "非 JSON 响应"
        )
        raise JwglError(f"mportal 周课表接口返回失败：{message}")
    data = body.get("d")
    if not isinstance(data, dict):
        raise JwglError("mportal 周课表接口缺少 d 数据体")
    weekdays = [str(item) for item in data.get("weekdays", [])]
    classes = data.get("classes", [])
    if not isinstance(classes, list):
        raise JwglError("mportal 周课表 classes 结构异常")
    occurrences: list[dict[str, Any]] = []
    for day_index, day_cells in enumerate(classes):
        if not isinstance(day_cells, dict):
            continue
        for cell_items in day_cells.values():
            if not isinstance(cell_items, list):
                continue
            for item in cell_items:
                if not isinstance(item, dict):
                    continue
                sections = [int(x) for x in item.get("lessArr", [])]
                raw_weekday = str(item.get("weekday", "")).strip()
                weekday = int(raw_weekday) if raw_weekday.isdigit() else day_index + 1
                occurrences.append(
                    {
                        "course_code": str(item.get("course_id", "")),
                        "course_name": str(item.get("course_name", "")),
                        "teacher": str(item.get("teacher", "")),
                        "location": str(item.get("location", "")),
                        "weekday": weekday,
                        "sections": sorted(sections),
                        "date": weekdays[day_index]
                        if day_index < len(weekdays)
                        else "",
                    }
                )
    return {"weekdays": weekdays, "occurrences": occurrences}


class MportalClient:
    """Structured timetable API client on mportal.cugb.edu.cn (CAS SSO, no WebVPN)."""

    def __init__(self, page: Any, base_url: str, navigation_timeout_ms: int) -> None:
        self.page = page
        self.base_url = base_url.rstrip("/")
        self.navigation_timeout_ms = navigation_timeout_ms

    async def ensure_session(self) -> None:
        """Establish the mportal session, re-binding through CAS SSO when stale.

        A valid CAS ticket re-establishes the app session silently through
        the login-entry bounce; only a settled CAS login form means the user
        must log in again.

        Raises:
            AuthenticationRequired: When the flow lands on the CAS login
                form, or the session is still invalid after the SSO bounce.
        """
        if not await probe_needs_login(
            self.page, self.base_url, self.navigation_timeout_ms
        ):
            return
        if await open_login_form(self.page, self.base_url, self.navigation_timeout_ms):
            raise AuthenticationRequired("mportal 需要 CAS 统一认证登录")
        if await probe_needs_login(
            self.page, self.base_url, self.navigation_timeout_ms
        ):
            raise AuthenticationRequired("mportal SSO 回弹后会话仍未建立")

    def _check_response_url(self, url: str) -> None:
        if "login" in urlsplit(url).path.lower():
            raise AuthenticationRequired("mportal 接口跳转到了 CAS 登录页")

    async def fetch_term_info(self) -> dict[str, Any]:
        """GET get-index; returns the "d" body with params and termInfo."""
        response = await self.page.context.request.get(
            f"{self.base_url}/timetable/wap/default/get-index",
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        try:
            self._check_response_url(response.url)
            body = await response.json()
            if not isinstance(body, dict) or body.get("e") != 0:
                message = (
                    body.get("m", "未知响应")
                    if isinstance(body, dict)
                    else "非 JSON 响应"
                )
                raise JwglError(f"mportal 学期信息接口返回失败：{message}")
            data = body.get("d")
            if not isinstance(data, dict) or "termInfo" not in data:
                raise JwglError("mportal 学期信息接口缺少 termInfo")
            return data
        finally:
            await response.dispose()

    async def fetch_week(self, year: str, term: int, week: int) -> dict[str, Any]:
        """POST get-datatmp for one teaching week and normalize the payload.

        Args:
            year: Four-digit year string.
            term: Mportal term code (1=秋, 2=春, 3=夏).
            week: Teaching week number, 1-based.

        Returns:
            Normalized dict from parse_week_payload.
        """
        response = await self.page.context.request.post(
            f"{self.base_url}/timetable/wap/default/get-datatmp",
            form={"year": year, "term": str(term), "week": str(week), "type": "2"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        try:
            self._check_response_url(response.url)
            return parse_week_payload(await response.json())
        finally:
            await response.dispose()
