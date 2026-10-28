import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

import pytest

PLUGIN_PARENT = Path(__file__).resolve().parents[1] / "data" / "plugins"
if str(PLUGIN_PARENT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_PARENT))

from astrbot_plugin_cugb_jwgl.core.jwgl_core import AuthenticationRequired
from astrbot_plugin_cugb_jwgl.main import CugbJwglPlugin
from astrbot_plugin_cugb_jwgl.repositories.snapshot_repo import SnapshotRepository
from astrbot_plugin_cugb_jwgl.services.poll_service import PollService
from astrbot_plugin_cugb_jwgl.services.weekly_schedule_service import (
    WeeklyScheduleService,
)
from astrbot_plugin_cugb_jwgl.utils.config_loader import load_plugin_config


def _poll(repo, push):
    session = SimpleNamespace(
        state="active",
        login_pending=False,
        poll_once=AsyncMock(side_effect=AuthenticationRequired("CAS login required")),
    )
    return PollService({}, session, repo, push, WeeklyScheduleService(repo))


@pytest.mark.asyncio
async def test_expiry_notice_is_sent_once_and_survives_database_reopen(tmp_path):
    database = tmp_path / "jwgl.sqlite"
    push = AsyncMock(return_value=True)
    repo = SnapshotRepository(database)
    poll = _poll(repo, push)
    for _ in range(3):
        result = await poll.poll_now()
        assert result["success"] is False
    assert poll.session.state == "expired"
    assert repo.get_meta("auth_expiry_notified") == "1"
    push.assert_awaited_once()
    assert "明早" not in push.call_args.args[0]
    repo.close()

    repo = SnapshotRepository(database)
    try:
        restarted = _poll(repo, push)
        await restarted.poll_now(trigger="startup")
        push.assert_awaited_once()
        restarted.reset_expiry_notification()
        await restarted.poll_now()
        await restarted.poll_now()
        assert push.await_count == 2
    finally:
        repo.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, RuntimeError("adapter unavailable")])
async def test_failed_expiry_delivery_remains_retryable(tmp_path, failure):
    repo = SnapshotRepository(tmp_path / "jwgl.sqlite")
    push = AsyncMock(side_effect=[failure, True])
    poll = _poll(repo, push)
    try:
        await poll.poll_now()
        assert repo.get_meta("auth_expiry_notified") != "1"
        await poll.poll_now()
        await poll.poll_now()
        assert push.await_count == 2
        assert repo.get_meta("auth_expiry_notified") == "1"
    finally:
        repo.close()


@pytest.mark.asyncio
async def test_network_failure_does_not_claim_authentication_expired(tmp_path):
    repo = SnapshotRepository(tmp_path / "jwgl.sqlite")
    push = AsyncMock(return_value=True)
    poll = _poll(repo, push)
    poll.session.poll_once.side_effect = TimeoutError("network failure")
    try:
        result = await poll.poll_now()
        assert result["success"] is False
        assert poll.session.state == "active"
        push.assert_not_awaited()
        assert repo.get_meta("auth_expiry_notified") != "1"
    finally:
        repo.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("delivered", [False, True])
async def test_expired_auto_loop_only_retries_undelivered_notices(tmp_path, delivered):
    repo = SnapshotRepository(tmp_path / "jwgl.sqlite")
    poll = _poll(repo, AsyncMock(return_value=True))
    poll.session.state = "expired"
    poll._expiry_notified = delivered
    poll.poll_now = AsyncMock()
    try:
        with patch(
            "astrbot_plugin_cugb_jwgl.services.poll_service.asyncio.sleep",
            AsyncMock(side_effect=[None, asyncio.CancelledError]),
        ):
            with pytest.raises(asyncio.CancelledError):
                await poll._run_loop()
        assert poll.poll_now.await_count == (0 if delivered else 1)
    finally:
        repo.close()


@pytest.mark.asyncio
async def test_verified_poll_allows_one_notice_for_a_later_expiry(tmp_path):
    repo = SnapshotRepository(tmp_path / "jwgl.sqlite")
    push = AsyncMock(return_value=True)
    poll = _poll(repo, push)
    poll._scan_mportal = AsyncMock(return_value=None)
    poll._diff_and_store = AsyncMock(
        return_value={
            "backfill": False,
            "score_changes": ([], []),
            "exam_changes": ([], []),
        }
    )
    try:
        await poll.poll_now()
        poll.session.poll_once.side_effect = None
        poll.session.poll_once.return_value = {"current_period": "2026 秋"}
        poll.session.state = "active"
        assert (await poll.poll_now())["success"] is True
        assert repo.get_meta("auth_expiry_notified") == "0"
        poll.session.poll_once.side_effect = AuthenticationRequired("expired again")
        await poll.poll_now()
        await poll.poll_now()
        assert push.await_count == 2
    finally:
        repo.close()


@pytest.mark.asyncio
async def test_mportal_expiry_notice_is_persisted_separately(tmp_path):
    database = tmp_path / "jwgl.sqlite"
    push = AsyncMock(return_value=True)
    repo = SnapshotRepository(database)
    poll = _poll(repo, push)
    poll.session.fetch_mportal_timetables = AsyncMock(
        side_effect=AuthenticationRequired("mportal login required")
    )
    await poll._scan_mportal("2026 秋")
    await poll._scan_mportal("2026 秋")
    assert repo.get_meta("mportal_auth_notified") == "1"
    assert repo.get_meta("auth_expiry_notified") == ""
    repo.close()

    repo = SnapshotRepository(database)
    try:
        restarted = _poll(repo, push)
        restarted.session.fetch_mportal_timetables = (
            poll.session.fetch_mportal_timetables
        )
        await restarted._scan_mportal("2026 秋")
        push.assert_awaited_once()
        restarted.session.fetch_mportal_timetables.side_effect = None
        restarted.session.fetch_mportal_timetables.return_value = {"scans": {}}
        await restarted._scan_mportal("2026 秋")
        assert repo.get_meta("mportal_auth_notified") == "0"
        restarted.session.fetch_mportal_timetables.side_effect = AuthenticationRequired(
            "expired again"
        )
        await restarted._scan_mportal("2026 秋")
        assert push.await_count == 2
    finally:
        repo.close()


@pytest.mark.asyncio
async def test_initialize_removes_legacy_morning_jobs_without_rescheduling():
    cron = SimpleNamespace(
        list_jobs=AsyncMock(
            return_value=[
                SimpleNamespace(name="cugb_jwgl_morning_login", job_id="legacy-1"),
                SimpleNamespace(name="weather", job_id="keep-weather"),
                SimpleNamespace(name="cugb_jwgl_morning_login", job_id="legacy-2"),
            ]
        ),
        delete_job=AsyncMock(),
        add_basic_job=AsyncMock(),
    )
    plugin = CugbJwglPlugin.__new__(CugbJwglPlugin)
    plugin.context = SimpleNamespace(cron_manager=cron)
    plugin.config = load_plugin_config(
        {
            "enabled": False,
            "class_reminder_enabled": False,
            "morning_login_enabled": True,
            "morning_login_cron": "0 9 * * *",
        }
    )
    await plugin.initialize()
    assert cron.delete_job.await_args_list == [call("legacy-1"), call("legacy-2")]
    cron.add_basic_job.assert_not_awaited()
    assert "morning_login_enabled" not in load_plugin_config({})


@pytest.mark.asyncio
async def test_startup_probe_uses_normal_deduplicated_expiry_notification():
    plugin = CugbJwglPlugin.__new__(CugbJwglPlugin)
    plugin.session_service = SimpleNamespace(state="unknown", portal_configured=True)
    plugin.poll_service = SimpleNamespace(poll_now=AsyncMock())
    with patch("astrbot_plugin_cugb_jwgl.main.asyncio.sleep", AsyncMock()):
        await plugin._startup_probe()
    plugin.poll_service.poll_now.assert_awaited_once_with(trigger="startup")
