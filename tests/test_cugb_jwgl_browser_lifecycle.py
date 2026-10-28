import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

PLUGIN_PARENT = Path(__file__).resolve().parents[1] / "data" / "plugins"
if str(PLUGIN_PARENT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_PARENT))

from astrbot_plugin_cugb_jwgl.core.jwgl_core import AuthenticationRequired, JwglError
from astrbot_plugin_cugb_jwgl.core.mportal_client import MportalClient
from astrbot_plugin_cugb_jwgl.core.portal import PortalClient
from astrbot_plugin_cugb_jwgl.services.session_service import JwglSessionService

PORTAL_MODULE = "astrbot_plugin_cugb_jwgl.core.portal"
SESSION_MODULE = "astrbot_plugin_cugb_jwgl.services.session_service"
ACADEMIC_URL = "https://elib.cugb.edu.cn/academic/index_frame.jsp"


def _page(url):
    return Mock(
        url=url,
        is_closed=Mock(return_value=False),
        goto=AsyncMock(),
        close=AsyncMock(),
        wait_for_load_state=AsyncMock(),
    )


@pytest.fixture
def session(tmp_path):
    service = JwglSessionService(
        {
            "portal_url": "https://elib.cugb.edu.cn/portal/",
            "session_cookie_file": str(tmp_path / "cookies.json"),
            "browser_profile_dir": str(tmp_path / "profile"),
        }
    )
    service._browser.main_page = _page(service.settings.portal_url)
    service._ensure_browser = AsyncMock()
    return service


@pytest.mark.asyncio
async def test_repeated_queries_reuse_one_academic_page(session):
    work_page = _page(ACADEMIC_URL)
    with (
        patch(f"{SESSION_MODULE}.PortalClient") as portal,
        patch(f"{SESSION_MODULE}.is_login_page", AsyncMock(return_value=False)),
    ):
        portal.return_value.open = AsyncMock()
        portal.return_value.open_jwgl_from_direct_train = AsyncMock(
            return_value=work_page
        )
        first = await session._open_client()
        for _ in range(5):
            assert await session._open_client() is first
        portal.return_value.open_jwgl_from_direct_train.assert_awaited_once()
    assert work_page.goto.await_count == 5
    work_page.close.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["closed", "cas", "legacy"])
async def test_lost_academic_page_is_replaced_without_retaining_it(session, reason):
    old_page, new_page = _page(ACADEMIC_URL), _page(ACADEMIC_URL)
    with (
        patch(f"{SESSION_MODULE}.PortalClient") as portal,
        patch(
            f"{SESSION_MODULE}.is_login_page", AsyncMock(return_value=False)
        ) as login,
    ):
        portal.return_value.open = AsyncMock()
        portal.return_value.open_jwgl_from_direct_train = AsyncMock(
            side_effect=[old_page, new_page]
        )
        first = await session._open_client()
        if reason == "closed":
            old_page.is_closed.return_value = True
        elif reason == "cas":
            login.side_effect = lambda page: page is old_page
        else:
            old_page.url = (
                "https://elib.cugb.edu.cn/academic/common/security/affairLogin"
            )
        replacement = await session._open_client()
    assert replacement is not first
    assert replacement.page is new_page
    assert old_page.close.await_count == (0 if reason == "closed" else 1)


@pytest.mark.asyncio
async def test_navigation_error_keeps_page_available_for_retry(session):
    work_page = _page(ACADEMIC_URL)
    with (
        patch(f"{SESSION_MODULE}.PortalClient") as portal,
        patch(f"{SESSION_MODULE}.is_login_page", AsyncMock(return_value=False)),
    ):
        portal.return_value.open = AsyncMock()
        portal.return_value.open_jwgl_from_direct_train = AsyncMock(
            return_value=work_page
        )
        first = await session._open_client()
        work_page.goto.side_effect = [TimeoutError("temporary network failure"), None]
        with pytest.raises(TimeoutError):
            await session._open_client()
        assert await session._open_client() is first
        portal.return_value.open_jwgl_from_direct_train.assert_awaited_once()
    work_page.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_browser_rebuild_stops_old_driver_and_drops_cached_page(session):
    old_browser = SimpleNamespace(
        context=None, closed=asyncio.Event(), stop=AsyncMock()
    )
    session._browser = old_browser
    session._jwgl_client = object()
    session.cookie_store.restore = AsyncMock(return_value=0)
    with patch(f"{SESSION_MODULE}.BrowserManager") as manager:
        new_browser = manager.return_value
        new_browser.start = AsyncMock(return_value=new_browser.context)
        await JwglSessionService._ensure_browser(session)
        old_browser.stop.assert_awaited_once()
        new_browser.start.assert_awaited_once()
        assert session._browser is new_browser
        assert session._jwgl_client is None


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_sso_closes_unused_popups_even_when_click_fails(session, fails):
    main_page = session._browser.main_page
    main_page.context.pages = [main_page]
    work_page, extra_page = _page(ACADEMIC_URL), _page("about:blank")
    target = Mock(
        is_visible=AsyncMock(return_value=True),
        scroll_into_view_if_needed=AsyncMock(),
    )
    main_page.get_by_text.return_value.count = AsyncMock(return_value=1)
    main_page.get_by_text.return_value.nth.return_value = target

    async def click():
        main_page.context.pages.extend([work_page, extra_page])
        if fails:
            raise RuntimeError("click failed after opening a popup")

    target.click = AsyncMock(side_effect=click)
    portal = PortalClient(main_page, session.settings)
    with patch(f"{PORTAL_MODULE}.is_login_page", AsyncMock(return_value=False)):
        if fails:
            with pytest.raises(RuntimeError, match="click failed"):
                await portal.open_jwgl_from_direct_train()
            work_page.close.assert_awaited_once()
        else:
            assert await portal.open_jwgl_from_direct_train() is work_page
            work_page.close.assert_not_awaited()
    extra_page.close.assert_awaited_once()
    main_page.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_rejected_sso_page_is_closed(session):
    main_page = session._browser.main_page
    invalid_page = _page("https://unexpected.invalid/academic/index_frame.jsp")
    with (
        patch(f"{SESSION_MODULE}.PortalClient") as portal,
        patch(f"{SESSION_MODULE}.is_login_page", AsyncMock(return_value=False)),
    ):
        portal.return_value.open = AsyncMock()
        portal.return_value.open_jwgl_from_direct_train = AsyncMock(
            return_value=invalid_page
        )
        with pytest.raises(JwglError, match="意外的教务主机"):
            await session._open_client()
    invalid_page.close.assert_awaited_once()
    main_page.close.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["term", "week"])
@pytest.mark.parametrize("outcome", ["success", "expired", "malformed", "redirect"])
async def test_mportal_disposes_responses_on_every_exit(operation, outcome):
    body = {"e": 0, "d": {"termInfo": [], "weekdays": [], "classes": []}}
    response = SimpleNamespace(
        url="https://mportal.cugb.edu.cn/timetable/wap/default/get-index",
        json=AsyncMock(return_value=body),
        dispose=AsyncMock(),
    )
    error = None
    if outcome == "expired":
        response.json.return_value = {"e": 10013, "m": "expired"}
        error = JwglError
    elif outcome == "malformed":
        response.json.side_effect = ValueError("invalid JSON")
        error = ValueError
    elif outcome == "redirect":
        response.url = "https://cas.cugb.edu.cn/authserver/login"
        error = AuthenticationRequired
    request = SimpleNamespace(
        get=AsyncMock(return_value=response), post=AsyncMock(return_value=response)
    )
    page = SimpleNamespace(context=SimpleNamespace(request=request))
    client = MportalClient(page, "https://mportal.cugb.edu.cn", 5000)
    call = (
        client.fetch_term_info()
        if operation == "term"
        else client.fetch_week("2026", 1, 1)
    )
    if error:
        with pytest.raises(error):
            await call
    else:
        assert await call == (
            body["d"] if operation == "term" else {"weekdays": [], "occurrences": []}
        )
    response.dispose.assert_awaited_once()
