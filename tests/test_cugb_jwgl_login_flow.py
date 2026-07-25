import asyncio
import sys
from pathlib import Path

import pytest

PLUGIN_PARENT = Path(__file__).resolve().parents[1] / "data" / "plugins"
if str(PLUGIN_PARENT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_PARENT))

from astrbot_plugin_cugb_jwgl.core.jwgl_core import JwglError
from astrbot_plugin_cugb_jwgl.core.login import CugbLoginAutomator
from astrbot_plugin_cugb_jwgl.core.mportal_client import probe_needs_login
from astrbot_plugin_cugb_jwgl.repositories.snapshot_repo import SnapshotRepository
from astrbot_plugin_cugb_jwgl.services.consent_service import ConsentService
from astrbot_plugin_cugb_jwgl.services.poll_service import PollService
from astrbot_plugin_cugb_jwgl.services.session_service import JwglSessionService
from astrbot_plugin_cugb_jwgl.services.weekly_schedule_service import (
    WeeklyScheduleService,
)


class _FakeLocator:
    def __init__(self, visible: bool, text: str = "") -> None:
        self._visible = visible
        self._text = text

    @property
    def first(self):
        return self

    async def count(self) -> int:
        return 1 if self._visible else 0

    async def is_visible(self) -> bool:
        return self._visible

    async def inner_text(self) -> str:
        return self._text


class _FakePage:
    """Minimal page double: only the SMS-step visibility is modeled."""

    def __init__(self, sms_step_visible: bool) -> None:
        self._sms_step_visible = sms_step_visible
        self.url = "https://sso.cugb.edu.cn/authserver/login"

    def locator(self, selector: str) -> _FakeLocator:
        visible = selector == "#J_mobile_Captcha" and self._sms_step_visible
        return _FakeLocator(visible)


class _FakeBrowser:
    def __init__(self, page: _FakePage | None) -> None:
        self.main_page = page
        self.closed = asyncio.Event()
        self.context = object() if page is not None else None


def _session(tmp_path) -> JwglSessionService:
    return JwglSessionService(
        {
            "session_cookie_file": str(tmp_path / "cookies.json"),
            "browser_profile_dir": str(tmp_path / "profile"),
        }
    )


@pytest.mark.asyncio
async def test_begin_login_returns_sms_pending_while_waiting(tmp_path):
    session = _session(tmp_path)
    session._set_login_pending("portal")
    session._browser = _FakeBrowser(_LoginPage([("获取验证码", True)]))
    assert await session.begin_login() == "sms_pending"
    assert session.login_pending
    assert session.login_stage == "portal"


@pytest.mark.asyncio
async def test_complete_login_without_pending_rejected(tmp_path):
    session = _session(tmp_path)
    with pytest.raises(JwglError, match="没有等待验证码"):
        await session.complete_login("123456")


@pytest.mark.asyncio
async def test_complete_login_recovers_when_sms_page_gone(tmp_path):
    session = _session(tmp_path)
    session._set_login_pending("portal")
    session._browser = _FakeBrowser(_FakePage(sms_step_visible=False))
    with pytest.raises(JwglError, match="登录页面已失效"):
        await session.complete_login("123456")
    assert not session.login_pending
    assert session.login_stage is None


@pytest.mark.asyncio
async def test_complete_login_recovers_when_browser_closed(tmp_path):
    session = _session(tmp_path)
    session._set_login_pending("mportal")
    session._browser = _FakeBrowser(None)
    with pytest.raises(JwglError, match="浏览器会话已丢失"):
        await session.complete_login("123456")
    assert not session.login_pending


@pytest.mark.asyncio
async def test_page_operations_rejected_while_login_pending(tmp_path):
    session = _session(tmp_path)
    session._set_login_pending("portal")
    with pytest.raises(JwglError, match="登录流程正在进行"):
        await session.poll_once()
    with pytest.raises(JwglError, match="登录流程正在进行"):
        await session.query_live("scores")
    with pytest.raises(JwglError, match="登录流程正在进行"):
        await session.fetch_mportal_timetables([("2026", 1)])


class _ScriptedSendButton:
    """Send-code button whose label/enabled state evolves per cooldown check."""

    def __init__(self, states: list[tuple[str, bool]]) -> None:
        self._states = states
        self._checks = 0
        self.clicks = 0

    def _state(self) -> tuple[str, bool]:
        return self._states[min(self._checks, len(self._states) - 1)]

    async def count(self) -> int:
        return 1

    async def is_visible(self) -> bool:
        return True

    async def get_attribute(self, name: str) -> str:
        state = self._state()
        self._checks += 1  # 每轮冷却检查推进一次按钮文案
        return state[0]

    async def is_enabled(self) -> bool:
        return self._state()[1]

    async def click(self) -> None:
        self.clicks += 1


class _LoginPage:
    """CAS page double routing selectors to scripted locators."""

    def __init__(
        self,
        button_states: list[tuple[str, bool]],
        error_message: str = "",
        sms_step_visible: bool = True,
    ) -> None:
        self.url = "https://sso.cugb.edu.cn/authserver/login"
        self.send_button = _ScriptedSendButton(button_states)
        self._error_message = error_message
        self._sms_step_visible = sms_step_visible

    def locator(self, selector: str):
        if selector == "#doubletelphoneSendCaptcha":
            return self.send_button
        if selector == "#J_mobile_Captcha":
            return _FakeLocator(self._sms_step_visible)
        if selector == "#J_mobileMsg":
            return _FakeLocator(bool(self._error_message), self._error_message)
        return _FakeLocator(False)


@pytest.mark.asyncio
async def test_request_sms_code_waits_out_cooldown_before_clicking():
    page = _LoginPage(
        [("59秒后重新获取", False), ("58秒后重新获取", False), ("重新获取", True)]
    )
    automator = CugbLoginAutomator(None)
    await automator.request_sms_code(page)
    assert page.send_button.clicks == 1


@pytest.mark.asyncio
async def test_request_sms_code_surfaces_page_error_after_click():
    page = _LoginPage([("获取验证码", True)], error_message="发送过于频繁，请稍后再试")
    automator = CugbLoginAutomator(None)
    with pytest.raises(JwglError, match="发送过于频繁"):
        await automator.request_sms_code(page)


@pytest.mark.asyncio
async def test_begin_login_resend_only_when_requested(tmp_path):
    session = _session(tmp_path)
    session._set_login_pending("portal")
    page = _LoginPage([("获取验证码", True)])
    session._browser = _FakeBrowser(page)

    assert await session.begin_login() == "sms_pending"
    assert page.send_button.clicks == 0

    assert await session.begin_login(resend=True) == "sms_resent"
    assert page.send_button.clicks == 1
    assert session.login_pending
    assert session.login_stage == "portal"


@pytest.mark.asyncio
async def test_begin_login_restarts_when_pending_page_lost(tmp_path):
    session = _session(tmp_path)
    session._set_login_pending("portal")
    session._browser = _FakeBrowser(_LoginPage([("获取验证码", True)], False, False))

    async def _stop_here():
        raise JwglError("已进入完整登录流程")

    session._ensure_browser = _stop_here
    with pytest.raises(JwglError, match="已进入完整登录流程"):
        await session.begin_login()
    assert not session.login_pending


class _ProbeResponse:
    def __init__(self, url: str, body: dict | None = None) -> None:
        self.url = url
        self._body = body

    async def json(self) -> dict:
        if self._body is None:
            raise ValueError("not a JSON response")
        return self._body


class _ProbeRequest:
    def __init__(self, final_url: str | None, body: dict | None = None) -> None:
        self._final_url = final_url
        self._body = body

    async def get(self, *_args, **_kwargs) -> _ProbeResponse:
        if self._final_url is None:
            raise RuntimeError("net::ERR_TOO_MANY_REDIRECTS")
        return _ProbeResponse(self._final_url, self._body)


class _ProbeContext:
    def __init__(self, final_url: str | None, body: dict | None = None) -> None:
        self.request = _ProbeRequest(final_url, body)


class _ProbePage:
    def __init__(self, final_url: str | None, body: dict | None = None) -> None:
        self.context = _ProbeContext(final_url, body)


@pytest.mark.asyncio
async def test_probe_needs_login_uses_api_not_page_navigation():
    base = "https://mportal.cugb.edu.cn"
    ok = _ProbePage(
        "https://mportal.cugb.edu.cn/timetable/wap/default/get-index", {"e": 0}
    )
    assert await probe_needs_login(ok, base, 30_000) is False
    cas = _ProbePage("https://sso.cugb.edu.cn/authserver/login?service=x")
    assert await probe_needs_login(cas, base, 30_000) is True
    redirected = _ProbePage("https://mportal.cugb.edu.cn/uc/wap/login?redirect=")
    assert await probe_needs_login(redirected, base, 30_000) is True
    broken = _ProbePage(None)
    assert await probe_needs_login(broken, base, 30_000) is True


@pytest.mark.asyncio
async def test_probe_needs_login_detects_expired_app_session_body():
    base = "https://mportal.cugb.edu.cn"
    expired = _ProbePage(
        "https://mportal.cugb.edu.cn/timetable/wap/default/get-index",
        {"e": 10013, "m": "用户信息已失效,请重新进入页面"},
    )
    assert await probe_needs_login(expired, base, 30_000) is True
    not_json = _ProbePage("https://mportal.cugb.edu.cn/timetable/wap/default/get-index")
    assert await probe_needs_login(not_json, base, 30_000) is True


class _StubSession:
    """Consent-facing session double with scriptable login outcomes."""

    portal_configured = True

    def __init__(
        self,
        begin_stage: str = "sms_sent",
        login_stage: str | None = None,
        complete_error: Exception | None = None,
    ) -> None:
        self._begin_stage = begin_stage
        self.login_stage = login_stage
        self._complete_error = complete_error
        self.login_pending = begin_stage == "sms_pending" or complete_error is not None
        self.resend_requests: list[bool] = []

    async def begin_login(self, resend: bool = False) -> str:
        self.resend_requests.append(resend)
        return self._begin_stage

    async def complete_login(self, code: str) -> str:
        if self._complete_error is not None:
            raise self._complete_error
        return "done"


class _StubPoll:
    def __init__(self) -> None:
        self.started = False

    def start(self) -> None:
        self.started = True

    async def poll_now(self, trigger: str = "manual") -> dict:
        return {"success": True, "message": "ok"}


def _consent(session, poll=None) -> ConsentService:
    async def push(_text: str) -> bool:
        return True

    return ConsentService({}, session, poll or _StubPoll(), push, lambda: "umo")


@pytest.mark.asyncio
async def test_request_login_reports_pending_portal_stage():
    consent = _consent(_StubSession(begin_stage="sms_pending", login_stage="portal"))
    message = await consent.request_login()
    assert "登录流程已在进行中" in message
    assert "无需重复发起登录" in message


@pytest.mark.asyncio
async def test_request_login_reports_pending_mportal_stage():
    consent = _consent(_StubSession(begin_stage="sms_pending", login_stage="mportal"))
    message = await consent.request_login()
    assert "登录流程已在进行中" in message
    assert "mportal" in message


@pytest.mark.asyncio
async def test_request_login_resend_message_and_passthrough():
    session = _StubSession(begin_stage="sms_resent", login_stage="portal")
    consent = _consent(session)
    message = await consent.request_login(resend=True)
    assert session.resend_requests == [True]
    assert "已重新发送短信验证码" in message


@pytest.mark.asyncio
async def test_submit_sms_code_includes_unexpected_error_detail():
    consent = _consent(
        _StubSession(complete_error=RuntimeError("Execution context was destroyed"))
    )
    result = await consent.submit_sms_code("123456")
    assert result["success"] is False
    assert "RuntimeError" in result["message"]
    assert "Execution context was destroyed" in result["message"]


@pytest.mark.asyncio
async def test_submit_sms_code_validates_input_and_pending():
    consent = _consent(_StubSession(begin_stage="sms_sent"))
    consent.session.login_pending = False
    result = await consent.submit_sms_code("12345")
    assert result["success"] is False
    assert "6 位数字" in result["message"]
    result = await consent.submit_sms_code("123456")
    assert result["success"] is False
    assert "没有等待验证码" in result["message"]


TERM_INFO = {
    "params": {"year": "2027", "term": 2, "startday": "2027-02-22", "countweek": "17"},
    "termInfo": [
        {"year": "2027", "term": 2, "startday": "2027-02-22", "countweek": "17"}
    ],
}

_WEEK_SCAN = {
    "weekdays": [f"2027-02-{day}" for day in range(22, 29)],
    "occurrences": [
        {
            "course_code": "XX000001",
            "course_name": "示例课程",
            "teacher": "张三",
            "location": "综合楼101",
            "weekday": 1,
            "sections": [3, 4],
            "date": "2027-02-22",
        }
    ],
}


class _MportalSession:
    def __init__(self) -> None:
        self.fetched: list[tuple[str, int]] = []

    async def fetch_mportal_timetable(self, year: str, term: int) -> dict:
        self.fetched.append((year, term))
        return {
            "term_info": TERM_INFO,
            "countweek": 17,
            "weeks": {1: dict(_WEEK_SCAN)},
        }


def _weekly(repo: SnapshotRepository) -> WeeklyScheduleService:
    service = WeeklyScheduleService(repo)
    service.regenerate_mportal("2027 春", {1: dict(_WEEK_SCAN)})
    return service


@pytest.mark.asyncio
async def test_ensure_term_timetable_serves_cache_without_fetch(tmp_path):
    repo = SnapshotRepository(tmp_path / "jwgl.sqlite")
    weekly = _weekly(repo)
    session = _MportalSession()
    poll = PollService({"mportal_enabled": True}, session, repo, None, weekly)
    payload = await poll.ensure_term_timetable("2027", "春")
    assert payload["period"] == "2027 春"
    assert payload["source"] == "mportal"
    assert session.fetched == []
    repo.close()


@pytest.mark.asyncio
async def test_ensure_term_timetable_fetches_and_caches_missing_term(tmp_path):
    repo = SnapshotRepository(tmp_path / "jwgl.sqlite")
    weekly = WeeklyScheduleService(repo)
    session = _MportalSession()
    poll = PollService({"mportal_enabled": True}, session, repo, None, weekly)
    payload = await poll.ensure_term_timetable("2027", "春")
    assert session.fetched == [("2027", 2)]
    assert payload["weeks"]["1"][0]["course_name"] == "示例课程"
    cached = weekly.get_timetable("2027 春")
    assert cached["period"] == "2027 春"
    assert weekly.get_term_info()["termInfo"]
    repo.close()


@pytest.mark.asyncio
async def test_ensure_term_timetable_rejects_invalid_or_disabled(tmp_path):
    repo = SnapshotRepository(tmp_path / "jwgl.sqlite")
    weekly = WeeklyScheduleService(repo)
    session = _MportalSession()
    poll = PollService({"mportal_enabled": True}, session, repo, None, weekly)
    with pytest.raises(JwglError, match="无法识别的学期"):
        await poll.ensure_term_timetable("2027", "冬")
    disabled = PollService({"mportal_enabled": False}, session, repo, None, weekly)
    with pytest.raises(JwglError, match="尚未生成周课表"):
        await disabled.ensure_term_timetable("2027", "春")
    repo.close()
