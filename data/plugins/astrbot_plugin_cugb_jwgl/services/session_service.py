from __future__ import annotations

import asyncio
from typing import Any

from astrbot.api import logger

from ..core.browser import BrowserManager
from ..core.jwgl_core import (
    AuthenticationRequired,
    JwglError,
    JwglSettings,
    SessionCookieStore,
    is_login_page,
    safe_exception_name,
)
from ..core.login import CugbLoginAutomator
from ..core.mportal_client import (
    DEFAULT_MPORTAL_BASE_URL,
    TERM_CODE_TO_LABEL,
    MportalClient,
    open_login_form,
    probe_needs_login,
    term_countweek,
)
from ..core.portal import (
    BACKFILL_START,
    JwglClient,
    PortalClient,
    next_period,
    period_label,
    periods_between,
    prev_period,
)
from ..utils.paths import resolve_data_path


class JwglSessionService:
    """持有唯一的浏览器会话，串行化一切教务页面操作。"""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.settings = JwglSettings(
            username=str(config.get("username", "")).strip() or None,
            password=str(config.get("password", "")) or None,
            portal_url=str(config.get("portal_url", "")).strip().rstrip("/") + "/",
            session_cookie_path=resolve_data_path(
                str(config.get("session_cookie_file", "webvpn_session_cookies.json"))
            ),
            profile_dir=resolve_data_path(
                str(config.get("browser_profile_dir", "browser-profile"))
            ),
            headless=bool(config.get("headless", True)),
            browser_channel=str(config.get("browser_channel", "")).strip(),
            navigation_timeout_ms=int(
                float(config.get("navigation_timeout_seconds", 30)) * 1000
            ),
        )
        self.cookie_store = SessionCookieStore(self.settings.session_cookie_path)
        self._browser = BrowserManager(self.settings)
        self._jwgl_client: JwglClient | None = None
        self._lock = asyncio.Lock()
        self._login_pending = False
        self._login_pending_since: float | None = None
        self._login_stage: str | None = None  # "portal" | "mportal"
        self._login_pending_timeout = int(config.get("sms_timeout_seconds", 300))
        self.state = "unknown"  # unknown | active | expired

    @property
    def portal_configured(self) -> bool:
        return self.settings.portal_url != "/"

    @property
    def login_pending(self) -> bool:
        return self._login_pending

    @property
    def login_stage(self) -> str | None:
        """Which verification step is waiting for an SMS code ("portal"/"mportal")."""
        return self._login_stage if self._login_pending else None

    def _set_login_pending(self, stage: str) -> None:
        self._login_pending = True
        self._login_stage = stage
        self._login_pending_since = asyncio.get_running_loop().time()

    def _clear_login_pending(self) -> None:
        self._login_pending = False
        self._login_stage = None
        self._login_pending_since = None

    def _ensure_not_logging_in(self) -> None:
        """Reject page operations that would hijack the CAS login page.

        The login flow parks the single shared main page on the CAS SMS form
        and releases the lock while waiting for the user's code. Any other
        operation acquiring the lock in between must not navigate that page.
        """
        if self._login_pending:
            raise JwglError(
                "登录流程正在进行（等待短信验证码），为避免打断登录，本次操作已跳过"
            )

    async def stop(self) -> None:
        async with self._lock:
            await self._browser.stop()
            self._jwgl_client = None

    async def _ensure_browser(self) -> Any:
        if self._browser.context is None or self._browser.closed.is_set():
            await self._browser.stop()
            self._jwgl_client = None
            self._browser = BrowserManager(self.settings)
            context = await self._browser.start()
            restored = await self.cookie_store.restore(context)
            if restored:
                logger.info("Restored %d WebVPN session cookie(s)", restored)
        elif self._browser.main_page is None or self._browser.main_page.is_closed():
            self._browser.main_page = await self._browser.context.new_page()
        return self._browser.context

    async def _open_client(self) -> JwglClient:
        await self._ensure_browser()
        page = self._browser.main_page
        if page is None:
            raise JwglError("浏览器没有可用的门户页面")
        portal = PortalClient(page, self.settings)
        await portal.open()
        if await is_login_page(page):
            self.state = "expired"
            raise AuthenticationRequired("信息门户要求 CAS 登录")
        client = self._jwgl_client
        if client is not None and not client.page.is_closed():
            await client.page.goto(
                f"{client.jwgl_base_url}/academic/index_frame.jsp",
                wait_until="domcontentloaded",
                timeout=self.settings.navigation_timeout_ms,
            )
            if (
                not await is_login_page(client.page)
                and "/academic/common/security/affairlogin"
                not in client.page.url.lower()
            ):
                self.state = "active"
                return client
            # Rebind an expired academic session through the still-valid portal.
            self._jwgl_client = None
            if client.page is page:
                await portal.open()
            else:
                await client.page.close()
        try:
            jwgl_page = await portal.open_jwgl_from_direct_train()
            try:
                self._jwgl_client = JwglClient(jwgl_page, self.settings)
            except Exception:
                if jwgl_page is not page:
                    await jwgl_page.close()
                raise
        except AuthenticationRequired:
            self.state = "expired"
            raise
        self.state = "active"
        return self._jwgl_client

    async def _save_cookies(self) -> None:
        if self._browser.context is None:
            return
        try:
            saved = await self.cookie_store.save(self._browser.context)
            if saved:
                logger.info("Saved %d WebVPN session cookie(s)", saved)
        except Exception as error:
            logger.warning("WebVPN cookie 保存失败：%s", safe_exception_name(error))

    async def poll_once(
        self, scores_backfill: bool = False, courses_backfill: bool = False
    ) -> dict[str, Any]:
        """按计划查询：成绩（当前+上一学期，或回填全部）+ 课表（当前+下一学期，或回填全部）+ 考试。"""
        async with self._lock:
            self._ensure_not_logging_in()
            client = await self._open_client()
            await client.open_module("scores")
            current = await client.read_current_period()
            if scores_backfill:
                score_periods = periods_between(BACKFILL_START, current)
            else:
                score_periods = [prev_period(*current), current]
            scores_map = await client.query_scores_periods(score_periods)

            if courses_backfill:
                course_periods = periods_between(BACKFILL_START, next_period(*current))
            else:
                course_periods = [current, next_period(*current)]
            await client.open_module("courses")
            courses_map = await client.query_courses_periods(course_periods)

            exams = await client.query_exams()
            await self._save_cookies()
            return {
                "current_period": period_label(*current),
                "scores": scores_map,
                "courses": courses_map,
                "exams": exams,
            }

    async def query_live(
        self,
        kind: str,
        year: str = "",
        term: str = "",
        scores_probe_max_terms: int = 6,
    ) -> dict[str, Any]:
        """实时查询单个模块（供 LLM 工具 / 指令使用）；year/term 为空时取默认口径。"""
        year = year.strip()
        term = term.strip()
        async with self._lock:
            self._ensure_not_logging_in()
            client = await self._open_client()
            if kind == "courses":
                if year and term:
                    records = await client.query_courses_period(year, term)
                    period = period_label(year, term)
                else:
                    records = await client.query_courses_current()
                    period = ""
                await self._save_cookies()
                return {"records": records, "period": period}
            if kind == "scores":
                if year and term:
                    records = await client.query_scores_period(year, term)
                    if records:
                        await self._save_cookies()
                        return {
                            "records": records,
                            "period": period_label(year, term),
                        }
                    note = (
                        f"{period_label(year, term)} 暂无成绩，已返回最晚有成绩的学期"
                    )
                else:
                    note = ""
                records, period = await client.find_latest_scores(
                    scores_probe_max_terms
                )
                await self._save_cookies()
                period_text = f"{period['year']} {period['term']}" if period else ""
                result: dict[str, Any] = {"records": records, "period": period_text}
                if note:
                    result["note"] = note
                return result
            if kind == "exams":
                records = await client.query_exams()
                await self._save_cookies()
                return {"records": records, "period": ""}
            raise JwglError(f"未知的模块类型：{kind}")

    def _mportal_base_url(self) -> str:
        return str(self.config.get("mportal_base_url", DEFAULT_MPORTAL_BASE_URL))

    def _mportal_enabled(self) -> bool:
        return bool(self.config.get("mportal_enabled", True))

    async def _drive_login_form(self, page: Any, stage: str) -> str:
        """在当前 CAS 登录页上执行账号密码步骤并请求短信验证码。"""
        automator = CugbLoginAutomator(self.settings)
        if await automator.needs_account_step(page):
            await automator.submit_account_password(page)
        if not await automator.needs_sms_step(page):
            raise JwglError("CAS 登录页既没有账号密码步骤也没有短信验证步骤")
        await automator.request_sms_code(page)
        self._set_login_pending(stage)
        return "sms_sent"

    async def fetch_mportal_timetables(
        self, terms: list[tuple[str, int]]
    ) -> dict[str, Any]:
        """Scan every teaching week of multiple terms in one mportal session.

        Args:
            terms: List of (year, term code) pairs; duplicates are removed
                while preserving order.

        Returns:
            Dict with "term_info" (get-index d body) and "scans" mapping a
            period label like "2026 秋" to {"weeks": ..., "countweek": ...}.
            Terms absent from the mportal term info are skipped.

        Raises:
            AuthenticationRequired: When CAS SSO is not established.
            JwglError: When no requested term exists in mportal term info.
        """
        async with self._lock:
            self._ensure_not_logging_in()
            await self._ensure_browser()
            page = self._browser.main_page
            if page is None:
                raise JwglError("浏览器没有可用的页面")
            base_url = str(
                self.config.get("mportal_base_url", DEFAULT_MPORTAL_BASE_URL)
            )
            client = MportalClient(page, base_url, self.settings.navigation_timeout_ms)
            await client.ensure_session()
            term_info = await client.fetch_term_info()
            scans: dict[str, dict[str, Any]] = {}
            seen: set[tuple[str, int]] = set()
            for year, term in terms:
                if (year, term) in seen:
                    continue
                seen.add((year, term))
                countweek = term_countweek(term_info, year, term)
                if countweek is None:
                    continue
                label = period_label(year, TERM_CODE_TO_LABEL[term])
                logger.info("mportal 扫描 %s（共 %d 周）", label, countweek)
                weeks: dict[int, dict[str, Any]] = {}
                for week in range(1, countweek + 1):
                    weeks[week] = await client.fetch_week(year, term, week)
                scans[label] = {"weeks": weeks, "countweek": countweek}
            if not scans:
                raise JwglError("mportal 学期信息中没有所请求的学期")
            self.state = "active"
            await self._save_cookies()
            return {"term_info": term_info, "scans": scans}

    async def fetch_mportal_timetable(self, year: str, term: int) -> dict[str, Any]:
        """Scan every teaching week of one term from mportal (CAS SSO, no WebVPN).

        Args:
            year: Four-digit year string, e.g. "2026".
            term: Mportal term code (1=秋, 2=春, 3=夏).

        Returns:
            Dict with "term_info", "countweek" and "weeks" for the term.
        """
        label = period_label(year, TERM_CODE_TO_LABEL[term])
        result = await self.fetch_mportal_timetables([(year, term)])
        scan = result["scans"][label]
        return {
            "term_info": result["term_info"],
            "countweek": scan["countweek"],
            "weeks": scan["weeks"],
        }

    async def begin_login(self, resend: bool = False) -> str:
        """发起登录：必要时提交账号密码并请求学校短信验证码。

        门户会话有效但 mportal 直连缺少 CAS 会话时，会在 mportal 的 CAS
        登录页上继续登录（同一套账号密码与短信流程）。

        Args:
            resend: 已有等待中的流程且用户明确说没收到短信时为 True，
                会在学校重发冷却结束后重新发送验证码；为 False 时直接
                报告已有流程在等验证码，不重复发短信（避免旧码失效）。

        返回 "reuse"（两个会话都有效）、"sms_sent"（验证码已发出）、
        "sms_resent"（已重新发送）或 "sms_pending"（正在等待验证码）。
        """
        async with self._lock:
            if self._login_pending:
                since = self._login_pending_since or 0.0
                stale = (
                    asyncio.get_running_loop().time() - since
                    > self._login_pending_timeout
                )
                if not stale:
                    page = self._browser.main_page
                    automator = CugbLoginAutomator(self.settings)
                    if (
                        page is not None
                        and not self._browser.closed.is_set()
                        and await automator.needs_sms_step(page)
                    ):
                        if not resend:
                            return "sms_pending"
                        await automator.request_sms_code(page)
                        self._set_login_pending(self._login_stage or "portal")
                        return "sms_resent"
                # 流程已过期或验证页已丢失：清除旧状态，重新走完整登录
                self._clear_login_pending()
            await self._ensure_browser()
            page = self._browser.main_page
            if page is None:
                raise JwglError("浏览器没有可用的门户页面")
            portal = PortalClient(page, self.settings)
            await portal.open()
            if not await is_login_page(page):
                self.state = "active"
                if self._mportal_enabled() and await probe_needs_login(
                    page, self._mportal_base_url(), self.settings.navigation_timeout_ms
                ):
                    logger.info("门户会话有效，mportal 需要补一次 CAS 登录")
                    if not await open_login_form(
                        page,
                        self._mportal_base_url(),
                        self.settings.navigation_timeout_ms,
                    ):
                        # 探测误差：SSO 直接弹回了 mportal，会话其实已建立
                        await self._save_cookies()
                        return "reuse"
                    return await self._drive_login_form(page, "mportal")
                await self._save_cookies()
                return "reuse"

            return await self._drive_login_form(page, "portal")

    async def complete_login(self, code: str) -> str:
        """提交用户回复的短信验证码并完成登录。验证码错误时可再次调用重试。

        返回 "done"（门户与 mportal 都已就绪）或 "mportal_sms_pending"
        （门户已登录，mportal 的 CAS 登录还需再提交一次验证码）。
        """
        async with self._lock:
            if not self._login_pending:
                raise JwglError("当前没有等待验证码的登录流程")
            stage = self._login_stage or "portal"
            page = self._browser.main_page
            if page is None or self._browser.closed.is_set():
                self._clear_login_pending()
                raise JwglError("浏览器会话已丢失，请重新发起登录")
            automator = CugbLoginAutomator(self.settings)
            if not await automator.needs_sms_step(page):
                # 等待期间页面被导航走（或浏览器重启），旧验证码流程无法继续
                self._clear_login_pending()
                raise JwglError("登录页面已失效，请重新发起登录")
            await automator.submit_sms_code(page, code)

            if stage == "mportal":
                if await probe_needs_login(
                    page, self._mportal_base_url(), self.settings.navigation_timeout_ms
                ):
                    # CAS 可能已通过但应用会话尚未绑定：经登录入口 SSO 回弹建立一次
                    if await open_login_form(
                        page,
                        self._mportal_base_url(),
                        self.settings.navigation_timeout_ms,
                    ):
                        self._clear_login_pending()
                        raise JwglError("mportal 统一认证未能完成，请重新发起登录")
                    if await probe_needs_login(
                        page,
                        self._mportal_base_url(),
                        self.settings.navigation_timeout_ms,
                    ):
                        self._clear_login_pending()
                        raise JwglError("mportal 会话未能建立，请重新发起登录")
                self.state = "active"
                await self._save_cookies()
                self._clear_login_pending()
                return "done"

            if await is_login_page(page):
                raise JwglError("CAS 登录未能完成")
            portal = PortalClient(page, self.settings)
            await portal.open()
            if await is_login_page(page):
                raise JwglError("登录后门户仍停留在 CAS 登录页")
            self.state = "active"
            await self._save_cookies()
            if self._mportal_enabled() and await probe_needs_login(
                page, self._mportal_base_url(), self.settings.navigation_timeout_ms
            ):
                logger.info("门户登录完成，mportal 还需一次 CAS 短信验证")
                if not await open_login_form(
                    page, self._mportal_base_url(), self.settings.navigation_timeout_ms
                ):
                    # mportal 已随门户 SSO 打通，无需第二次验证
                    self._clear_login_pending()
                    return "done"
                await self._drive_login_form(page, "mportal")
                return "mportal_sms_pending"
            self._clear_login_pending()
            return "done"

    async def abort_login(self) -> None:
        async with self._lock:
            self._clear_login_pending()
