from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import urljoin, urlsplit

from astrbot.api import logger

from .jwgl_core import (
    TERM_ORDER,
    AuthenticationRequired,
    JwglError,
    JwglSettings,
    is_login_page,
    parse_courses,
    parse_exams,
    parse_scores,
    reject_legacy_affair_login,
    sanitize_url,
)

TERM_SEQUENCE = ("春", "夏", "秋")

BACKFILL_START = ("2020", "春")


def next_period(year: str, term: str) -> tuple[str, str]:
    if term == "秋":
        return str(int(year) + 1), "春"
    return year, TERM_SEQUENCE[TERM_SEQUENCE.index(term) + 1]


def prev_period(year: str, term: str) -> tuple[str, str]:
    if term == "春":
        return str(int(year) - 1), "秋"
    return year, TERM_SEQUENCE[TERM_SEQUENCE.index(term) - 1]


def periods_between(
    start: tuple[str, str], end: tuple[str, str]
) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    current = start
    while True:
        result.append(current)
        if current == end:
            return result
        current = next_period(*current)
        if len(result) > 120:
            raise JwglError(f"学期区间异常：{start} 到 {end}")


def period_label(year: str, term: str) -> str:
    return f"{year} {term}"


class PortalClient:
    def __init__(self, page: Any, settings: JwglSettings) -> None:
        self.page = page
        self.settings = settings

    async def open(self) -> None:
        logger.info("Opening the information portal")
        await self.page.goto(
            self.settings.portal_url,
            wait_until="domcontentloaded",
            timeout=self.settings.navigation_timeout_ms,
        )

    async def open_jwgl_from_direct_train(self) -> Any:
        if await is_login_page(self.page):
            raise AuthenticationRequired("信息门户要求 CAS 登录")

        logger.info("Finding 系统直通车 → 教务管理 in the portal")
        deadline = asyncio.get_running_loop().time() + 30
        target: Any | None = None
        while asyncio.get_running_loop().time() < deadline:
            matches = self.page.get_by_text("教务管理", exact=True)
            for index in range(await matches.count()):
                candidate = matches.nth(index)
                if await candidate.is_visible():
                    target = candidate
                    break
            if target is not None:
                break
            await asyncio.sleep(0.25)
        if target is None:
            raise JwglError("门户未渲染 系统直通车 → 教务管理 入口")

        existing_pages = set(self.page.context.pages)
        await target.scroll_into_view_if_needed()
        await target.click()

        deadline = asyncio.get_running_loop().time() + 30
        while asyncio.get_running_loop().time() < deadline:
            pages = [
                *[
                    page
                    for page in self.page.context.pages
                    if page not in existing_pages
                ],
                self.page,
            ]
            for candidate in pages:
                candidate_path = urlsplit(candidate.url).path.lower()
                if "/academic/" not in candidate_path:
                    continue
                try:
                    await candidate.wait_for_load_state(
                        "domcontentloaded", timeout=5_000
                    )
                except Exception:
                    pass
                await reject_legacy_affair_login(candidate)
                if "/academic/login/cugb/loginids6valid.jsp" in candidate_path:
                    await asyncio.sleep(0.25)
                    continue
                logger.info("Portal SSO opened the JWGL system")
                return candidate
            await asyncio.sleep(0.25)
        raise JwglError("点击门户的教务管理入口后没有打开 /academic/ 页面")


class JwglClient:
    MODULES = {"courses": "2000", "scores": "2020", "exams": "2030"}

    def __init__(self, page: Any, settings: JwglSettings) -> None:
        self.page = page
        self.settings = settings
        self.jwgl_base_url = self._derive_jwgl_base(page.url)

    @staticmethod
    def _derive_jwgl_base(url: str) -> str:
        parsed = urlsplit(url)
        marker = parsed.path.lower().find("/academic/")
        if parsed.scheme != "https" or marker < 0:
            raise JwglError("门户 SSO 结果不是合法的教务 /academic/ 地址")
        if parsed.hostname not in {"elib.cugb.edu.cn", "jwglxt.cugb.edu.cn"}:
            raise JwglError("门户 SSO 结果使用了意外的教务主机")
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path[:marker]}"

    async def _module_link(self, module_id: str) -> str:
        shell_url = f"{self.jwgl_base_url}/academic/index_frame.jsp"
        logger.info("Initializing the JWGL shell")
        await self.page.goto(
            shell_url,
            wait_until="domcontentloaded",
            timeout=self.settings.navigation_timeout_ms,
        )
        if await is_login_page(self.page):
            raise AuthenticationRequired("教务外壳跳转到了 CAS")
        await reject_legacy_affair_login(self.page)

        deadline = asyncio.get_running_loop().time() + 20
        while asyncio.get_running_loop().time() < deadline:
            menu_frame = next(
                (frame for frame in self.page.frames if frame.name == "menuFrame"),
                None,
            )
            if menu_frame is not None:
                anchor = menu_frame.locator(
                    f'a[href*="accessModule.do"][href*="moduleId={module_id}"]'
                ).first
                if await anchor.count():
                    href = await anchor.get_attribute("href")
                    if href:
                        target = urljoin(menu_frame.url, href)
                        parsed = urlsplit(target)
                        if (
                            parsed.scheme == "https"
                            and parsed.hostname == "elib.cugb.edu.cn"
                            and "accessModule.do" in parsed.path
                        ):
                            return target
                        raise JwglError("教务菜单返回了意外的模块地址")
            await asyncio.sleep(0.25)
        raise JwglError("教务 menuFrame 或其模块链接未加载")

    async def open_module(self, name: str) -> None:
        await self._open_module(name)

    async def _open_module(self, name: str) -> None:
        logger.info("Opening JWGL module %s", name)
        module_url = await self._module_link(self.MODULES[name])
        await self.page.goto(
            module_url,
            wait_until="domcontentloaded",
            timeout=self.settings.navigation_timeout_ms,
        )
        if await is_login_page(self.page):
            raise AuthenticationRequired("教务模块导航跳转到了 CAS")
        await reject_legacy_affair_login(self.page)

    async def _option_labels(self, name: str) -> list[str]:
        select = self.page.locator(f'select[name="{name}"]').first
        try:
            if await select.count() == 0:
                return []
            labels = await select.locator("option").all_inner_texts()
        except Exception:
            return []
        return [re.sub(r"\s+", "", text) for text in labels if text.strip()]

    async def _selected_label(self, name: str) -> str:
        select = self.page.locator(f'select[name="{name}"]').first
        try:
            if await select.count() == 0:
                return ""
            value = await select.input_value()
            options = select.locator("option")
            for index in range(await options.count()):
                option = options.nth(index)
                if (await option.get_attribute("value")) == value:
                    return re.sub(r"\s+", "", await option.inner_text())
        except Exception:
            pass
        return ""

    async def read_current_period(self) -> tuple[str, str]:
        """学校页面对 year/term 下拉框的默认选中项，即学校口径的当前学期。"""
        year_label = await self._selected_label("year")
        term_label = await self._selected_label("term")
        year_match = re.search(r"\d{4}", year_label)
        term_char = next((char for char in TERM_SEQUENCE if char in term_label), "")
        if not year_match or not term_char:
            raise JwglError(
                f"无法从页面读取当前学期（year={year_label!r} term={term_label!r}）"
            )
        return year_match.group(0), term_char

    async def _select_option_label(self, name: str, label: str) -> None:
        select = self.page.locator(f'select[name="{name}"]').first
        try:
            await select.select_option(label=label)
        except Exception as error:
            raise JwglError(f"页面上没有 {name}={label} 这个选项") from error

    async def _submit_query(self) -> None:
        submit = self.page.locator('input[type="submit"]').first
        if await submit.count() == 0:
            raise JwglError("查询提交按钮未找到")
        await submit.click()
        try:
            await self.page.wait_for_load_state(
                "domcontentloaded", timeout=self.settings.navigation_timeout_ms
            )
        except Exception:
            logger.warning("Query navigation did not fully settle; parsing current DOM")
        if await is_login_page(self.page):
            raise AuthenticationRequired("查询提交跳转到了 CAS")
        await reject_legacy_affair_login(self.page)

    async def query_courses_current(self) -> list[dict[str, str]]:
        await self._open_module("courses")
        return parse_courses(await self.page.content())

    async def _query_courses_for(
        self, year_label: str, term_label: str
    ) -> list[dict[str, str]]:
        await self._select_option_label("year", year_label)
        await self._select_option_label("term", term_label)
        await self._submit_query()
        return parse_courses(await self.page.content())

    async def _query_courses_safely(
        self, year_label: str, term_label: str
    ) -> list[dict[str, str]]:
        """没有课程的学期查询后页面不含结果表格，解析抛错即视为空。"""
        try:
            return await self._query_courses_for(year_label, term_label)
        except JwglError:
            return []

    async def query_courses_period(
        self, year_label: str, term_label: str
    ) -> list[dict[str, str]]:
        await self._open_module("courses")
        return await self._query_courses_safely(year_label, term_label)

    async def query_courses_periods(
        self, periods: list[tuple[str, str]]
    ) -> dict[str, list[dict[str, str]]]:
        """模块需已打开；在页内逐个学期查询。"""
        result: dict[str, list[dict[str, str]]] = {}
        for year_label, term_label in periods:
            records = await self._query_courses_safely(year_label, term_label)
            result[period_label(year_label, term_label)] = records
        return result

    async def query_exams(self) -> list[dict[str, str]]:
        await self._open_module("exams")
        return parse_exams(await self.page.content())

    _TERM_START_MONTH = {"春": 2, "夏": 7, "秋": 9}

    @classmethod
    def _period_candidates(
        cls, years: list[str], terms: list[str]
    ) -> list[tuple[str, str]]:
        from datetime import datetime

        now = datetime.now()
        parsed_years: list[tuple[int, str]] = []
        for label in years:
            match = re.search(r"\d{4}", label)
            if match:
                parsed_years.append((int(match.group(0)), label))
        parsed_terms: list[tuple[int, str, str]] = []
        for label in terms:
            for char, order in TERM_ORDER.items():
                if char in label:
                    parsed_terms.append((order, label, char))
                    break
        combos: list[tuple[int, int, str, str]] = []
        for year_num, year_label in parsed_years:
            if year_num > now.year:
                continue
            for term_order, term_label, term_char in parsed_terms:
                if year_num == now.year:
                    start = cls._TERM_START_MONTH.get(term_char, 1)
                    # 学期开始前一个月起允许探测（空结果按无成绩处理）
                    if now.month < start - 1:
                        continue
                combos.append((year_num, term_order, year_label, term_label))
        combos.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [(year_label, term_label) for _, _, year_label, term_label in combos]

    async def _query_scores_for(
        self, year_label: str | None, term_label: str | None
    ) -> list[dict[str, str]]:
        if year_label is not None:
            await self._select_option_label("year", year_label)
        if term_label is not None:
            await self._select_option_label("term", term_label)
        await self._submit_query()
        return parse_scores(await self.page.content())

    async def _query_scores_safely(
        self, year_label: str | None, term_label: str | None
    ) -> list[dict[str, str]]:
        """没有成绩的学期查询后页面不含结果表格，解析抛错即视为空。"""
        try:
            return await self._query_scores_for(year_label, term_label)
        except JwglError:
            return []

    async def query_scores_period(
        self, year_label: str, term_label: str
    ) -> list[dict[str, str]]:
        await self._open_module("scores")
        return await self._query_scores_safely(year_label, term_label)

    async def query_scores_periods(
        self, periods: list[tuple[str, str]]
    ) -> dict[str, list[dict[str, str]]]:
        """模块需已打开；在页内逐个学期查询。"""
        result: dict[str, list[dict[str, str]]] = {}
        for year_label, term_label in periods:
            records = await self._query_scores_safely(year_label, term_label)
            result[period_label(year_label, term_label)] = records
        return result

    async def find_latest_scores(
        self, max_terms: int = 6
    ) -> tuple[list[dict[str, str]], dict[str, str] | None]:
        """查询最晚有成绩的那个学期；寒暑假默认页没有成绩时自动向前回溯。"""
        await self._open_module("scores")
        years = await self._option_labels("year")
        terms = await self._option_labels("term")
        candidates = self._period_candidates(years, terms)
        if not candidates:
            records = await self._query_scores_safely(None, None)
            return records, None
        for year_label, term_label in candidates[: max(1, max_terms)]:
            records = await self._query_scores_safely(year_label, term_label)
            if records:
                return records, {"year": year_label, "term": term_label}
        return [], None

    def describe_url(self) -> str:
        return sanitize_url(self.page.url) or ""
