import asyncio
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

PLUGIN_PARENT = Path(__file__).resolve().parents[1] / "data" / "plugins"
if str(PLUGIN_PARENT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_PARENT))

from astrbot_plugin_cugb_jwgl.core.jwgl_core import AuthenticationRequired
from astrbot_plugin_cugb_jwgl.main import CugbJwglPlugin
from astrbot_plugin_cugb_jwgl.repositories.snapshot_repo import SnapshotRepository
from astrbot_plugin_cugb_jwgl.services.poll_service import PollService
from astrbot_plugin_cugb_jwgl.services.query_service import AcademicQueryService
from astrbot_plugin_cugb_jwgl.services.session_service import JwglSessionService
from astrbot_plugin_cugb_jwgl.services.weekly_schedule_service import (
    WeeklyScheduleService,
)
from astrbot_plugin_cugb_jwgl.utils.config_loader import load_plugin_config

COURSE = {
    "课程号": "TEST001",
    "课程名称": "示例课程",
    "任课教师": "测试教师",
    "上课时间、地点": "第1周 星期一 第3-4节 综合楼101",
}


@pytest.fixture
def plugin(tmp_path):
    instance = CugbJwglPlugin.__new__(CugbJwglPlugin)
    instance.config = load_plugin_config({})
    instance.repo = SnapshotRepository(tmp_path / "jwgl.sqlite")
    instance.repo.set_meta("current_period", "2026 秋")
    instance.session_service = JwglSessionService(instance.config)
    instance.session_service.query_live = AsyncMock(
        side_effect=AssertionError("A cached query must not use the browser")
    )
    instance.session_service.fetch_mportal_timetable = AsyncMock(
        side_effect=AssertionError("Query validation must not scan mportal")
    )
    instance.weekly_service = WeeklyScheduleService(instance.repo)
    instance.query_service = AcademicQueryService(
        instance.session_service, instance.repo, instance.weekly_service
    )
    instance.poll_service = PollService(
        instance.config,
        instance.session_service,
        instance.repo,
        AsyncMock(),
        instance.weekly_service,
    )
    yield instance
    instance.repo.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("year,term", [("", ""), ("2026", "秋")])
async def test_course_tool_uses_snapshot_and_cached_validation_during_poll(
    plugin, year, term
):
    plugin.repo.diff_and_update("courses", [COURSE], lambda _: "2026 秋|TEST001")
    plugin.repo.diff_and_update(
        "courses", [{**COURSE, "课程名称": "其他学期课程"}], lambda _: "2025 秋|TEST001"
    )
    plugin.weekly_service.regenerate_mportal(
        "2026 秋",
        {
            1: {
                "weekdays": [f"2026-09-{day}" for day in range(14, 21)],
                "occurrences": [
                    {
                        "course_name": "示例课程",
                        "weekday": 1,
                        "sections": [3, 4],
                        "location": "综合楼101",
                    }
                ],
            }
        },
    )
    async with plugin.session_service._lock:
        result = json.loads(
            await asyncio.wait_for(plugin.tool_get_courses(None, year, term), timeout=1)
        )
    assert result["source"] == "snapshot"
    assert result["period"] == "2026 秋"
    assert [row["课程名称"] for row in result["records"]] == ["示例课程"]
    assert result["records"][0]["schedule"][0]["sections"] == [3, 4]
    assert result["validation"]["consistent"] is True
    plugin.session_service.query_live.assert_not_awaited()
    plugin.session_service.fetch_mportal_timetable.assert_not_awaited()


@pytest.mark.asyncio
async def test_score_tool_defaults_to_latest_cached_period(plugin):
    plugin.repo.set_meta("last_scores_period", "2026 春")
    score = {"学年": "2026", "学期": "春", "课程名": "示例课程", "成绩": "90"}
    plugin.repo.diff_and_update("scores", [score], lambda _: "2026|春|TEST001")
    plugin.repo.diff_and_update(
        "scores", [{**score, "学年": "2025"}], lambda _: "2025|春|TEST001"
    )
    result = json.loads(await plugin.tool_get_scores(None))
    assert result["period"] == "2026 春"
    assert result["records"] == [score]
    assert result["source"] == "snapshot"
    plugin.session_service.query_live.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["courses", "scores", "exams"])
async def test_successfully_cached_empty_results_do_not_query_online(plugin, kind):
    plugin.repo.diff_and_update(kind, [], lambda _: "unused")
    if kind == "courses":
        plugin.repo.set_meta("snapshot_period:courses:2026 秋", "1")
    result = await plugin.query_service.query(kind)
    assert result["source"] == "snapshot"
    assert result["records"] == []
    plugin.session_service.query_live.assert_not_awaited()


@pytest.mark.asyncio
async def test_poll_records_empty_course_period_as_a_cache_hit(plugin):
    await plugin.poll_service._diff_and_store(
        {
            "current_period": "2026 秋",
            "courses": {"2026 秋": []},
            "scores": {},
            "exams": [],
        },
        scores_backfill=False,
        courses_backfill=False,
    )
    result = json.loads(await plugin.tool_get_courses(None))
    assert result["source"] == "snapshot"
    assert result["records"] == []
    plugin.session_service.query_live.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_current_week_query_uses_freetext_cache_without_mportal(plugin):
    plugin.repo.diff_and_update("courses", [COURSE], lambda _: "2026 秋|TEST001")
    plugin.poll_service.config["mportal_enabled"] = False
    result = json.loads(await plugin.tool_get_week_courses(None, "1", "2026", "秋"))
    assert result["period"] == "2026 秋"
    assert result["source"] == "jwgl_freetext"
    assert result["courses"][0]["course_name"] == "示例课程"
    plugin.session_service.fetch_mportal_timetable.assert_not_awaited()


@pytest.mark.asyncio
async def test_uncached_course_period_is_queried_live(plugin):
    plugin.session_service.query_live = AsyncMock(
        return_value={"records": [COURSE], "period": "2027 春"}
    )
    result = json.loads(await plugin.tool_get_courses(None, "2027", "春"))
    assert result["source"] == "live"
    assert result["period"] == "2027 春"
    assert result["records"][0]["schedule"]
    plugin.session_service.query_live.assert_awaited_once_with(
        "courses", "2027", "春", 6
    )
    plugin.session_service.fetch_mportal_timetable.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_uncached_course_query_never_returns_another_period(plugin):
    plugin.repo.diff_and_update("courses", [COURSE], lambda _: "2026 秋|TEST001")
    plugin.session_service.query_live = AsyncMock(side_effect=AuthenticationRequired())
    result = json.loads(await plugin.tool_get_courses(None, "2027", "春"))
    assert result["success"] is False
    assert result["source"] == "unavailable"
    assert result["period"] == "2027 春"
    assert result["records"] == []


@pytest.mark.asyncio
async def test_failed_score_query_labels_the_actual_fallback_period(plugin):
    plugin.repo.set_meta("last_scores_period", "2026 春")
    score = {"学年": "2026", "学期": "春", "课程名": "示例课程", "成绩": "90"}
    plugin.repo.diff_and_update("scores", [score], lambda _: "2026|春|TEST001")
    plugin.session_service.query_live = AsyncMock(side_effect=AuthenticationRequired())
    result = json.loads(await plugin.tool_get_scores(None, "2027", "春"))
    assert result["source"] == "snapshot"
    assert result["period"] == "2026 春"
    assert result["records"] == [score]
    assert "所查学期无缓存" in result["note"]


@pytest.mark.asyncio
@pytest.mark.parametrize("year,term", [("2026", ""), ("", "秋"), ("2026", "冬")])
async def test_incomplete_or_invalid_period_does_not_trigger_network(
    plugin, year, term
):
    result = json.loads(await plugin.tool_get_courses(None, year, term))
    assert result["success"] is False
    plugin.session_service.query_live.assert_not_awaited()


@pytest.mark.parametrize(
    "week,expected_week,expected_count",
    [
        ("current", 1, 2),
        ("next", 2, 1),
        ("today", 1, 1),
        ("tomorrow", 1, 1),
        ("2", 2, 1),
    ],
)
def test_relative_week_and_day_queries_resolve_locally(
    plugin, week, expected_week, expected_count
):
    stored = {
        "period": "2026 秋",
        "source": "mportal",
        "max_week": 2,
        "week_dates": {
            "1": ["2026-09-14", "2026-09-15"],
            "2": ["2026-09-21"],
        },
        "weeks": {
            "1": [
                {"course_name": "周一课程", "weekday": 1},
                {"course_name": "周二课程", "weekday": 2},
            ],
            "2": [{"course_name": "下周课程", "weekday": 1}],
        },
    }
    result = plugin.weekly_service.query_week_courses(
        stored, week, today=date(2026, 9, 14)
    )
    assert result["week"] == expected_week
    assert len(result["courses"]) == expected_count
    if week == "tomorrow":
        assert result["date"] == "2026-09-15"
        assert result["courses"][0]["course_name"] == "周二课程"


def test_week_query_uses_cached_term_start_without_exact_dates(plugin):
    plugin.repo.diff_and_update("courses", [COURSE], lambda _: "2026 秋|TEST001")
    plugin.weekly_service.save_term_info(
        {"termInfo": [{"year": "2026", "term": 1, "startday": "2026-09-14"}]}
    )
    stored = plugin.weekly_service.get_timetable()
    result = plugin.weekly_service.query_week_courses(
        stored, "current", today=date(2026, 9, 14)
    )
    assert result["week"] == 1
    assert result["courses"][0]["course_name"] == "示例课程"
    unknown = plugin.weekly_service.query_week_courses(
        stored, "current", today=date(2026, 7, 14)
    )
    assert unknown["success"] is False


@pytest.mark.asyncio
async def test_tomorrow_tool_selects_cached_new_term_across_sunday_boundary(plugin):
    now = datetime(2026, 9, 20, 23, 59, tzinfo=ZoneInfo(plugin.config["timezone"]))
    today = now.date()
    tomorrow = today + timedelta(days=1)
    plugin.weekly_service.save_term_info({"termInfo": [{"year": "2027", "term": 2}]})
    plugin.weekly_service.regenerate_mportal(
        "2027 春",
        {
            1: {
                "weekdays": [tomorrow.isoformat()],
                "occurrences": [
                    {"course_name": "新学期课程", "weekday": tomorrow.isoweekday()}
                ],
            }
        },
    )
    with patch("astrbot_plugin_cugb_jwgl.main.datetime") as clock:
        clock.now.return_value = now
        result = json.loads(await plugin.tool_get_week_courses(None, "tomorrow"))
    assert result["period"] == "2027 春"
    assert result["date"] == tomorrow.isoformat()
    assert result["courses"][0]["course_name"] == "新学期课程"
    plugin.session_service.query_live.assert_not_awaited()
    plugin.session_service.fetch_mportal_timetable.assert_not_awaited()
