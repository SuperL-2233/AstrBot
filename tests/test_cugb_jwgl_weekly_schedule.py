import asyncio
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

PLUGIN_PARENT = Path(__file__).resolve().parents[1] / "data" / "plugins"
if str(PLUGIN_PARENT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_PARENT))

from astrbot_plugin_cugb_jwgl.core.weekly_schedule import (
    build_weekly_timetable,
    parse_schedule_text,
    section_label,
    structure_course_record,
)
from astrbot_plugin_cugb_jwgl.formatters.jwgl_formatter import (
    format_status,
    format_week_courses,
    format_weekly_overview,
)
from astrbot_plugin_cugb_jwgl.repositories.snapshot_repo import SnapshotRepository
from astrbot_plugin_cugb_jwgl.services.poll_service import PollService
from astrbot_plugin_cugb_jwgl.services.reminder_service import ClassReminderService
from astrbot_plugin_cugb_jwgl.services.weekly_schedule_service import (
    WeeklyScheduleService,
)
from astrbot_plugin_cugb_jwgl.utils.config_loader import load_plugin_config

TZ = ZoneInfo("Asia/Shanghai")


def _record(name: str, schedule: str) -> dict[str, str]:
    return {
        "课程名称": name,
        "课程号": "TEST001",
        "任课教师": "测试教师",
        "上课时间、地点": schedule,
    }


def test_parse_range_with_single_week_override():
    entries = parse_schedule_text(
        "10-17周 星期四 第1-2节 综合楼504 第17周 星期二 第6-7节 综合楼504"
    )
    assert len(entries) == 2
    assert entries[0].weeks == list(range(10, 18))
    assert entries[0].weekday == 4
    assert entries[0].sections == [1, 2]
    assert entries[0].location == "综合楼504"
    assert entries[1].weeks == [17]
    assert entries[1].weekday == 2
    assert entries[1].sections == [6, 7]


def test_parse_odd_weeks_and_trailing_week_only_fragment():
    entries = parse_schedule_text(
        "1-9周单 星期二 第8-9节 综合楼703 "
        "10-17周 星期一 第8-9节 综合楼403 "
        "10-17周 星期四 第6-7节 综合楼403 14-15周"
    )
    assert len(entries) == 4
    assert entries[0].weeks == [1, 3, 5, 7, 9]
    assert entries[0].weekday == 2
    assert entries[1].weeks == list(range(10, 18))
    assert entries[1].weekday == 1
    trailing = entries[3]
    assert trailing.weeks == [14, 15]
    assert trailing.weekday is None
    assert trailing.sections == []
    assert trailing.location == ""


def test_parse_multi_period_section():
    entries = parse_schedule_text("第14周 星期二 第6-9节 综合楼401")
    assert entries[0].weeks == [14]
    assert entries[0].sections == [6, 7, 8, 9]


def test_parse_empty_text_returns_no_entries():
    assert parse_schedule_text("") == []
    assert parse_schedule_text("时间地点待定") == []


def test_section_label():
    assert section_label([]) == ""
    assert section_label([3]) == "第3节"
    assert section_label([6, 7, 8, 9]) == "第6-9节"


def test_build_weekly_timetable_expands_per_week():
    records = [
        _record(
            "高等数学A(1)",
            "1-17周 星期一 第3-4节 综合楼402 1-17周 星期三 第1-2节 综合楼402",
        ),
        _record("大学计算机", "1-9周 星期二 第3-4节 综合楼202"),
        _record("形势与政策(1)", "第14周 星期二 第6-9节 综合楼401"),
    ]
    timetable = build_weekly_timetable(records)
    assert timetable.max_week == 17
    assert timetable.unparsed == []
    for week in range(1, 18):
        names = [item["course_name"] for item in timetable.weeks[week]]
        assert "高等数学A(1)" in names
    week1 = timetable.weeks[1]
    assert [item["weekday"] for item in week1] == [1, 2, 3]
    week14_names = [item["course_name"] for item in timetable.weeks[14]]
    assert "形势与政策(1)" in week14_names
    week10_names = [item["course_name"] for item in timetable.weeks[10]]
    assert "大学计算机" not in week10_names


def test_build_weekly_timetable_tracks_unparsed_courses():
    timetable = build_weekly_timetable([_record("神秘课程", "另行通知")])
    assert timetable.unparsed == ["神秘课程"]
    assert timetable.max_week == 0


def test_format_week_courses():
    timetable = build_weekly_timetable(
        [_record("大学英语(1)", "1-17周 星期一 第1-2节 教5楼515")]
    )
    text = format_week_courses(1, timetable.weeks[1], "2025 秋")
    assert "第1周课表（2025 秋）" in text
    assert "星期一 第1-2节 大学英语(1)（测试教师） @ 教5楼515" in text
    empty = format_week_courses(2, [], "2025 秋")
    assert "本周没有课程" in empty


def test_format_weekly_overview():
    timetable = build_weekly_timetable(
        [_record("体育(1)(系列课程)", "1-15周 星期一 第6-7节 操场110")]
    )
    text = format_weekly_overview(timetable.weeks, "2025 秋")
    assert "第1周：1 节次" in text
    assert "/教务周课表" in text
    assert "暂无可解析" in format_weekly_overview({})


def test_structure_course_record_replaces_free_text():
    structured = structure_course_record(
        _record("高等数学A(1)", "1-17周 星期一 第3-4节 综合楼402")
    )
    assert "上课时间、地点" not in structured
    assert structured["课程名称"] == "高等数学A(1)"
    assert structured["schedule"] == [
        {
            "weeks": list(range(1, 18)),
            "weekday": 1,
            "weekday_label": "星期一",
            "sections": [3, 4],
            "section_label": "第3-4节",
            "location": "综合楼402",
        }
    ]


def _seed_repo(path) -> SnapshotRepository:
    repo = SnapshotRepository(path)
    repo.set_meta("current_period", "2025 秋")
    repo.diff_and_update(
        "courses",
        [_record("高等数学A(1)", "1-17周 星期一 第3-4节 综合楼402")],
        lambda record: f"2025 秋|{record['课程号']}||{record['课程名称']}",
    )
    return repo


def test_weekly_service_generates_lazily_and_caches(tmp_path):
    repo = _seed_repo(tmp_path / "jwgl.sqlite")
    service = WeeklyScheduleService(repo)
    stored = service.get_timetable()
    assert stored["period"] == "2025 秋"
    assert stored["max_week"] == 17
    assert stored["weeks"]["3"][0]["course_name"] == "高等数学A(1)"
    assert service.get_timetable()["generated_at"] == stored["generated_at"]
    period, entries = service.get_week(3)
    assert period == "2025 秋"
    assert entries[0]["weekday_label"] == "星期一"
    repo.close()


@pytest.mark.asyncio
async def test_poll_regenerates_weekly_timetable_only_on_course_change(tmp_path):
    repo = _seed_repo(tmp_path / "jwgl.sqlite")
    weekly = WeeklyScheduleService(repo)
    regenerate_calls = 0
    original_regenerate = weekly.regenerate

    def counting_regenerate():
        nonlocal regenerate_calls
        regenerate_calls += 1
        return original_regenerate()

    weekly.regenerate = counting_regenerate
    poll = PollService({}, None, repo, lambda _text: None, weekly)
    result = {
        "scores": {},
        "courses": {
            "2025 秋": [
                _record("高等数学A(1)", "1-17周 星期一 第3-4节 综合楼402"),
                _record("大学计算机", "1-9周 星期二 第3-4节 综合楼202"),
            ]
        },
        "exams": [],
        "current_period": "2025 秋",
    }
    await poll._diff_and_store(result, False, False)
    assert regenerate_calls == 1
    first = weekly.get_timetable()
    assert first["max_week"] == 17
    week2 = first["weeks"]["2"]
    assert [item["course_name"] for item in week2] == ["高等数学A(1)", "大学计算机"]

    await poll._diff_and_store(result, False, False)
    assert regenerate_calls == 1

    changed = {
        **result,
        "courses": {
            "2025 秋": [
                _record("高等数学A(1)", "1-17周 星期一 第3-4节 综合楼402"),
                _record("大学计算机", "1-9周 星期三 第5-6节 科研楼B101-A"),
            ]
        },
    }
    await poll._diff_and_store(changed, False, False)
    assert regenerate_calls == 2
    regenerated = weekly.get_timetable()
    assert [
        (item["course_name"], item["weekday"]) for item in regenerated["weeks"]["2"]
    ] == [("高等数学A(1)", 1), ("大学计算机", 3)]
    moved = next(
        item
        for item in regenerated["weeks"]["3"]
        if item["course_name"] == "大学计算机"
    )
    assert moved["weekday"] == 3
    assert moved["section_label"] == "第5-6节"
    repo.close()


def _reminder_config(**overrides):
    config = {
        "class_reminder_enabled": True,
        "class_reminder_lead_minutes": 15,
        "class_reminder_check_seconds": 60,
        "semester_start_date": "2025-09-01",
        "section_start_times": {"3": "09:50", "4": "10:40"},
        "timezone": "Asia/Shanghai",
    }
    config.update(overrides)
    return config


def _reminder_service(repo, config, sent: list[str]) -> ClassReminderService:
    async def push(text: str) -> bool:
        sent.append(text)
        return True

    return ClassReminderService(config, WeeklyScheduleService(repo), repo, push)


@pytest.mark.asyncio
async def test_class_reminder_pushes_within_lead_window_and_dedupes(tmp_path):
    repo = _seed_repo(tmp_path / "jwgl.sqlite")
    sent: list[str] = []
    service = _reminder_service(repo, _reminder_config(), sent)
    # 2025-09-08 is the Monday of teaching week 2; 高等数学 is 第3-4节 (09:50).
    before_window = datetime(2025, 9, 8, 9, 34, 59, tzinfo=TZ)
    assert await service.check_once(before_window) == []
    in_window = datetime(2025, 9, 8, 9, 35, tzinfo=TZ)
    pushed = await service.check_once(in_window)
    assert len(pushed) == 1
    assert "高等数学A(1)" in sent[0]
    assert "09:50" in sent[0]
    assert "综合楼402" in sent[0]
    assert "15 分钟后" in sent[0]
    assert await service.check_once(in_window) == []
    assert len(sent) == 1
    repo.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("omit_lead_setting", [False, True])
async def test_default_reminder_is_15_minutes_for_each_course(
    tmp_path, omit_lead_setting
):
    repo = _seed_repo(tmp_path / "jwgl.sqlite")
    repo.diff_and_update(
        "courses",
        [_record("大学英语", "1-17周 星期一 第6-7节 教5楼515")],
        lambda record: f"2025 秋|{record['课程号']}||{record['课程名称']}",
    )
    config = load_plugin_config({"semester_start_date": "2025-09-01"})
    if omit_lead_setting:
        config.pop("class_reminder_lead_minutes")
    sent: list[str] = []
    service = _reminder_service(repo, config, sent)
    assert await service.check_once(datetime(2025, 9, 8, 9, 34, 59, tzinfo=TZ)) == []
    assert len(await service.check_once(datetime(2025, 9, 8, 9, 35, tzinfo=TZ))) == 1
    assert "高等数学A(1)" in sent[0] and "15 分钟后" in sent[0]
    # A consecutive class block receives one reminder before its first section.
    assert await service.check_once(datetime(2025, 9, 8, 10, 25, tzinfo=TZ)) == []
    assert await service.check_once(datetime(2025, 9, 8, 13, 14, 59, tzinfo=TZ)) == []
    assert len(await service.check_once(datetime(2025, 9, 8, 13, 15, tzinfo=TZ))) == 1
    assert "大学英语" in sent[1] and "13:30" in sent[1] and "教5楼515" in sent[1]
    assert len(sent) == 2
    repo.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["false", "exception"])
async def test_class_reminder_retries_failed_delivery_without_repeating_successes(
    tmp_path, failure
):
    path = tmp_path / "jwgl.sqlite"
    repo = _seed_repo(path)
    repo.diff_and_update(
        "courses",
        [_record("失败课程", "1-17周 星期一 第3-4节 综合楼202")],
        lambda record: f"2025 秋|{record['课程号']}||{record['课程名称']}",
    )
    sent: list[str] = []

    async def push(text: str) -> bool:
        if "失败课程" in text:
            if failure == "exception":
                raise RuntimeError("Delivery failed")
            return False
        sent.append(text)
        return True

    now = datetime(2025, 9, 8, 9, 35, tzinfo=TZ)
    service = ClassReminderService(
        _reminder_config(), WeeklyScheduleService(repo), repo, push
    )
    pushed = await service.check_once(now)
    assert [entry["course_name"] for entry in pushed] == ["高等数学A(1)"]
    assert len(sent) == 1
    repo.close()

    repo = SnapshotRepository(path)
    resumed = _reminder_service(repo, _reminder_config(), sent)
    retried = await resumed.check_once(datetime(2025, 9, 8, 9, 36, tzinfo=TZ))
    assert [entry["course_name"] for entry in retried] == ["失败课程"]
    assert len(sent) == 2
    assert await resumed.check_once(now) == []
    repo.close()


@pytest.mark.asyncio
async def test_class_reminder_persists_success_before_later_delivery_is_cancelled(tmp_path):
    path = tmp_path / "jwgl.sqlite"
    repo = SnapshotRepository(path)
    repo.set_meta("current_period", "2025 秋")
    repo.diff_and_update(
        "courses",
        [
            _record("First course", "1-17周 星期一 第3-4节 综合楼101"),
            _record("Second course", "1-17周 星期一 第3-4节 综合楼102"),
        ],
        lambda record: f"2025 秋|{record['课程名称']}",
    )
    sent: list[str] = []

    async def push(text: str) -> bool:
        if "Second course" in text:
            raise asyncio.CancelledError
        sent.append(text)
        return True

    now = datetime(2025, 9, 8, 9, 35, tzinfo=TZ)
    service = ClassReminderService(
        _reminder_config(), WeeklyScheduleService(repo), repo, push
    )
    with pytest.raises(asyncio.CancelledError):
        await service.check_once(now)
    assert len(sent) == 1 and "First course" in sent[0]
    repo.close()

    repo = SnapshotRepository(path)
    resumed = _reminder_service(repo, _reminder_config(), sent)
    pushed = await resumed.check_once(now)
    assert [entry["course_name"] for entry in pushed] == ["Second course"]
    assert len(sent) == 2
    repo.close()


@pytest.mark.asyncio
async def test_class_reminder_does_not_send_after_class_starts(tmp_path):
    repo = _seed_repo(tmp_path / "jwgl.sqlite")
    sent: list[str] = []
    service = _reminder_service(repo, _reminder_config(), sent)
    assert await service.check_once(datetime(2025, 9, 8, 9, 50, 1, tzinfo=TZ)) == []
    assert sent == []
    repo.close()


@pytest.mark.asyncio
async def test_class_reminder_normalizes_current_time_to_configured_timezone(tmp_path):
    repo = _seed_repo(tmp_path / "jwgl.sqlite")
    sent: list[str] = []
    service = _reminder_service(repo, _reminder_config(), sent)
    now = datetime(2025, 9, 8, 1, 35, tzinfo=ZoneInfo("UTC"))
    assert len(await service.check_once(now)) == 1
    assert "09:50" in sent[0] and "15 分钟后" in sent[0]
    repo.close()


@pytest.mark.asyncio
async def test_class_reminder_invalid_section_time_does_not_block_other_courses(tmp_path):
    repo = _seed_repo(tmp_path / "jwgl.sqlite")
    repo.diff_and_update(
        "courses",
        [_record("大学英语", "1-17周 星期一 第6-7节 教5楼515")],
        lambda record: f"2025 秋|{record['课程号']}||{record['课程名称']}",
    )
    config = _reminder_config(section_start_times={"3": "25:00", "6": "09:50"})
    sent: list[str] = []
    service = _reminder_service(repo, config, sent)
    pushed = await service.check_once(datetime(2025, 9, 8, 9, 35, tzinfo=TZ))
    assert [entry["course_name"] for entry in pushed] == ["大学英语"]
    repo.close()


def test_class_reminder_status_shows_lead_time_and_timetable_date_source():
    text = format_status(
        {
            "class_reminder_running": True,
            "class_reminder_lead_minutes": 15,
            "semester_start_source": "timetable",
        }
    )
    assert "上课提醒：运行中（日期：周课表自动）" in text
    assert "每门课开始前 15 分钟" in text
    assert "提醒不生效" not in text


@pytest.mark.asyncio
async def test_class_reminder_persists_dedupe_across_service_instances(tmp_path):
    repo = _seed_repo(tmp_path / "jwgl.sqlite")
    sent: list[str] = []
    now = datetime(2025, 9, 8, 9, 40, tzinfo=TZ)
    assert (
        len(await _reminder_service(repo, _reminder_config(), sent).check_once(now))
        == 1
    )
    assert await _reminder_service(repo, _reminder_config(), sent).check_once(now) == []
    assert len(sent) == 1
    repo.close()


@pytest.mark.asyncio
async def test_class_reminder_inactive_without_semester_start_or_disabled(tmp_path):
    repo = _seed_repo(tmp_path / "jwgl.sqlite")
    sent: list[str] = []
    now = datetime(2025, 9, 8, 9, 40, tzinfo=TZ)
    no_start = _reminder_service(repo, _reminder_config(semester_start_date=""), sent)
    assert await no_start.check_once(now) == []
    disabled = _reminder_service(
        repo, _reminder_config(class_reminder_enabled=False), sent
    )
    assert await disabled.check_once(now) == []
    assert sent == []
    repo.close()


@pytest.mark.asyncio
async def test_class_reminder_skips_other_weekdays_and_unknown_sections(tmp_path):
    repo = _seed_repo(tmp_path / "jwgl.sqlite")
    repo.diff_and_update(
        "courses",
        [_record("待定课程", "14-15周")],
        lambda record: f"2025 秋|{record['课程号']}|2|{record['课程名称']}",
    )
    sent: list[str] = []
    service = _reminder_service(repo, _reminder_config(), sent)
    # Tuesday of week 14: 待定课程 has no weekday/sections, nothing should fire.
    tuesday = datetime(2025, 12, 2, 9, 40, tzinfo=TZ)
    assert await service.check_once(tuesday) == []
    assert sent == []
    repo.close()
