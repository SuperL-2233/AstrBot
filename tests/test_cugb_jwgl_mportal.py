import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

PLUGIN_PARENT = Path(__file__).resolve().parents[1] / "data" / "plugins"
if str(PLUGIN_PARENT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_PARENT))

from astrbot_plugin_cugb_jwgl.core.jwgl_core import JwglError
from astrbot_plugin_cugb_jwgl.core.mportal_client import (
    parse_week_payload,
    period_to_year_term,
    term_countweek,
)
from astrbot_plugin_cugb_jwgl.core.weekly_schedule import (
    compare_weeks,
    structure_course_record,
    weeks_from_structured_records,
)
from astrbot_plugin_cugb_jwgl.formatters.jwgl_formatter import (
    format_validation_report,
)
from astrbot_plugin_cugb_jwgl.repositories.snapshot_repo import SnapshotRepository
from astrbot_plugin_cugb_jwgl.services.poll_service import PollService
from astrbot_plugin_cugb_jwgl.services.reminder_service import ClassReminderService
from astrbot_plugin_cugb_jwgl.services.weekly_schedule_service import (
    WeeklyScheduleService,
)

TZ = ZoneInfo("Asia/Shanghai")

TERM_INFO = {
    "params": {
        "year": "2026",
        "term": 1,
        "startday": "2026-09-14",
        "countweek": "17",
        "week": 1,
    },
    "termInfo": [
        {"year": "2026", "term": 1, "startday": "2026-09-14", "countweek": "17"},
        {"year": "2026", "term": 3, "startday": "2026-07-06", "countweek": "10"},
    ],
}

WEEKDAYS = [f"2026-09-{day}" for day in range(14, 21)]


def _empty_day() -> dict:
    return {f"c_{index}": [] for index in range(1, 15)}


def _cell(name: str, location: str = "综合楼101", lessons: list[int] | None = None):
    lessons = lessons or [3, 4]
    return {
        "course_id": "XX000001",
        "course_name": name,
        "location": location,
        "weekday": "1",
        "lessons": "".join(f"{item:02d}" for item in lessons),
        "teacher": "张三",
        "week": "1,2,3",
        "khfs": "",
        "jsgh": "0000000000",
        "cseq": "1",
        "lessArr": lessons,
        "order": lessons[0],
        "totalLength": len(lessons),
    }


def _week_body(*cells: dict) -> dict:
    classes = [_empty_day() for _ in range(7)]
    for cell in cells:
        classes[0]["c_3"] = [cell]
    return {
        "e": 0,
        "m": "操作成功",
        "d": {"classes": classes, "weekdays": list(WEEKDAYS)},
    }


def _jwgl_record(name: str, schedule: str) -> dict[str, str]:
    return {
        "课程名称": name,
        "课程号": "XX000001",
        "任课教师": "张三",
        "上课时间、地点": schedule,
    }


def test_period_to_year_term():
    assert period_to_year_term("2026 秋") == ("2026", 1)
    assert period_to_year_term("2026 春") == ("2026", 2)
    assert period_to_year_term("2026 夏") == ("2026", 3)
    assert period_to_year_term("") is None
    assert period_to_year_term("2026-2027-1") is None


def test_term_countweek():
    assert term_countweek(TERM_INFO, "2026", 1) == 17
    assert term_countweek(TERM_INFO, "2026", 3) == 10
    assert term_countweek(TERM_INFO, "2025", 1) is None
    bad = {"termInfo": [{"year": "2026", "term": 1, "startday": "2026-02-16"}]}
    with pytest.raises(JwglError):
        term_countweek(bad, "2026", 1)


def test_parse_week_payload_normalizes_cells():
    parsed = parse_week_payload(_week_body(_cell("示例课程")))
    assert parsed["weekdays"] == WEEKDAYS
    assert len(parsed["occurrences"]) == 1
    occurrence = parsed["occurrences"][0]
    assert occurrence["course_name"] == "示例课程"
    assert occurrence["weekday"] == 1
    assert occurrence["sections"] == [3, 4]
    assert occurrence["date"] == "2026-09-14"
    assert occurrence["location"] == "综合楼101"


def test_parse_week_payload_rejects_failure():
    with pytest.raises(JwglError):
        parse_week_payload({"e": 1, "m": "未登录"})
    with pytest.raises(JwglError):
        parse_week_payload({"e": 0, "d": None})


def test_regenerate_mportal_payload_has_dates(tmp_path):
    repo = SnapshotRepository(tmp_path / "jwgl.sqlite")
    repo.set_meta("current_period", "2026 秋")
    service = WeeklyScheduleService(repo)
    weeks = {
        1: parse_week_payload(_week_body(_cell("示例课程"))),
        2: {"weekdays": [], "occurrences": []},
    }
    payload = service.regenerate_mportal("2026 秋", weeks)
    assert payload["source"] == "mportal"
    assert payload["max_week"] == 2
    assert payload["week_dates"]["1"][0] == "2026-09-14"
    stored = service.get_timetable()
    occurrence = stored["weeks"]["1"][0]
    assert occurrence["date"] == "2026-09-14"
    assert occurrence["section_label"] == "第3-4节"
    repo.close()


def test_compare_weeks_reports_differences():
    reference = {
        1: [
            {
                "course_name": "示例课程",
                "weekday": 1,
                "weekday_label": "星期一",
                "sections": [3, 4],
                "section_label": "第3-4节",
                "location": "综合楼101",
            }
        ]
    }
    same = compare_weeks(
        reference,
        [dict(item) for item in reference[1]] and {1: [dict(reference[1][0])]},
    )
    assert same["mismatch_weeks"] == []
    assert same["matched"] == 1
    moved = {1: [dict(reference[1][0], location="科研楼B101")]}
    report = compare_weeks(reference, moved)
    assert report["mismatch_weeks"] == [1]
    assert report["weeks"][1]["only_in_reference"]
    assert report["weeks"][1]["only_in_candidate"]


def _seed_snapshot(repo: SnapshotRepository, schedule: str) -> None:
    repo.set_meta("current_period", "2026 秋")
    repo.diff_and_update(
        "courses",
        [_jwgl_record("示例课程", schedule)],
        lambda record: f"2026 秋|{record['课程号']}||{record['课程名称']}",
    )


def test_validate_matches_and_reports_mismatch(tmp_path):
    repo = SnapshotRepository(tmp_path / "jwgl.sqlite")
    _seed_snapshot(repo, "第1周 星期一 第3-4节 综合楼101")
    service = WeeklyScheduleService(repo)
    service.regenerate_mportal(
        "2026 秋", {1: parse_week_payload(_week_body(_cell("示例课程")))}
    )
    report = service.validate()
    assert report["skipped"] is False
    assert report["mismatch_weeks"] == []
    assert report["matched"] == 1

    repo.diff_and_update(
        "courses",
        [_jwgl_record("示例课程", "第1周 星期一 第3-4节 科研楼B101")],
        lambda record: f"2026 秋|{record['课程号']}||{record['课程名称']}",
    )
    report = service.validate()
    assert report["mismatch_weeks"] == [1]
    persisted = service.get_validation_report()
    assert persisted["mismatch_weeks"] == [1]
    text = format_validation_report(report)
    assert "教务文本缺少" in text
    assert "mportal 缺少" in text
    repo.close()


def test_validate_skips_non_mportal_source(tmp_path):
    repo = SnapshotRepository(tmp_path / "jwgl.sqlite")
    _seed_snapshot(repo, "第1周 星期一 第3-4节 综合楼101")
    service = WeeklyScheduleService(repo)
    service.regenerate()
    report = service.validate()
    assert report["skipped"] is True
    repo.close()


def _mportal_result(schedule_location: str = "综合楼101") -> dict:
    return {
        "term_info": TERM_INFO,
        "scans": {
            "2026 秋": {
                "countweek": 17,
                "weeks": {
                    1: parse_week_payload(
                        _week_body(_cell("示例课程", schedule_location))
                    )
                },
            }
        },
        "backfill": False,
        "validate_labels": ["2026 秋", "2027 春"],
    }


@pytest.mark.asyncio
async def test_poll_rebuilds_and_validates_only_on_mportal_change(tmp_path):
    repo = SnapshotRepository(tmp_path / "jwgl.sqlite")
    _seed_snapshot(repo, "第1周 星期一 第3-4节 综合楼101")
    weekly = WeeklyScheduleService(repo)
    pushed: list[str] = []

    async def push(text: str) -> bool:
        pushed.append(text)
        return True

    poll = PollService({"mportal_enabled": True}, None, repo, push, weekly)
    result = {
        "scores": {},
        "courses": {
            "2026 秋": [_jwgl_record("示例课程", "第1周 星期一 第3-4节 综合楼101")]
        },
        "exams": [],
        "current_period": "2026 秋",
        "mportal": _mportal_result(),
    }
    await poll._diff_and_store(result, False, False)
    assert weekly.get_timetable()["source"] == "mportal"
    assert weekly.get_term_info()["termInfo"]
    assert pushed == []  # 与教务文本一致时不推送

    first_generated_at = weekly.get_timetable()["generated_at"]
    await poll._diff_and_store(result, False, False)
    assert weekly.get_timetable()["generated_at"] == first_generated_at

    changed = {**result, "mportal": _mportal_result("科研楼B101")}
    await poll._diff_and_store(changed, False, False)
    assert weekly.get_timetable()["weeks"]["1"][0]["location"] == "科研楼B101"
    assert pushed and "课表交叉验证" in pushed[0]
    repo.close()


@pytest.mark.asyncio
async def test_poll_revalidates_when_only_jwgl_text_changes(tmp_path):
    repo = SnapshotRepository(tmp_path / "jwgl.sqlite")
    _seed_snapshot(repo, "第1周 星期一 第3-4节 综合楼101")
    weekly = WeeklyScheduleService(repo)
    pushed: list[str] = []

    async def push(text: str) -> bool:
        pushed.append(text)
        return True

    poll = PollService({"mportal_enabled": True}, None, repo, push, weekly)
    result = {
        "scores": {},
        "courses": {
            "2026 秋": [_jwgl_record("示例课程", "第1周 星期一 第3-4节 综合楼101")]
        },
        "exams": [],
        "current_period": "2026 秋",
        "mportal": _mportal_result(),
    }
    await poll._diff_and_store(result, False, False)
    assert pushed == []
    generated_at = weekly.get_timetable()["generated_at"]

    # mportal 扫描不变，但教务文本里上课地点变了：不重建，只重新校验并推送失配。
    changed = {
        **result,
        "courses": {
            "2026 秋": [_jwgl_record("示例课程", "第1周 星期一 第3-4节 科研楼B101")]
        },
    }
    await poll._diff_and_store(changed, False, False)
    assert weekly.get_timetable()["generated_at"] == generated_at
    assert weekly.get_timetable()["weeks"]["1"][0]["location"] == "综合楼101"
    assert pushed and "课表交叉验证" in pushed[0]
    report = weekly.get_validation_report()
    assert report["mismatch_weeks"] == [1]
    repo.close()


def _reminder_config(**overrides):
    config = {
        "class_reminder_enabled": True,
        "class_reminder_lead_minutes": 30,
        "class_reminder_check_seconds": 60,
        "semester_start_date": "",
        "section_start_times": {"3": "09:50", "4": "10:40"},
        "timezone": "Asia/Shanghai",
    }
    config.update(overrides)
    return config


@pytest.mark.asyncio
async def test_reminder_uses_cached_term_for_today_when_last_poll_term_is_old(tmp_path):
    repo = SnapshotRepository(tmp_path / "jwgl.sqlite")
    repo.set_meta("current_period", "2026 夏")
    weekly = WeeklyScheduleService(repo)
    weekly.save_term_info(TERM_INFO)
    weekly.regenerate_mportal(
        "2026 夏", {1: {"weekdays": ["2026-07-06"], "occurrences": []}}
    )
    weekly.regenerate_mportal(
        "2026 秋", {1: parse_week_payload(_week_body(_cell("示例课程")))}
    )
    sent: list[str] = []

    async def push(text: str) -> bool:
        sent.append(text)
        return True

    service = ClassReminderService(
        _reminder_config(class_reminder_lead_minutes=15), weekly, repo, push
    )
    now = datetime(2026, 9, 14, 9, 35, tzinfo=TZ)
    assert weekly.get_timetable()["period"] == "2026 夏"
    assert weekly.get_timetable(on_date=now.date())["period"] == "2026 秋"
    assert len(await service.check_once(now)) == 1
    assert "示例课程" in sent[0] and "15 分钟后" in sent[0]
    assert await service.check_once(now) == []
    assert repo.get_meta("current_period") == "2026 夏"
    repo.close()


@pytest.mark.asyncio
async def test_reminder_uses_week_dates_without_manual_start(tmp_path):
    repo = SnapshotRepository(tmp_path / "jwgl.sqlite")
    repo.set_meta("current_period", "2026 秋")
    weekly = WeeklyScheduleService(repo)
    weekly.regenerate_mportal(
        "2026 秋", {1: parse_week_payload(_week_body(_cell("示例课程")))}
    )
    sent: list[str] = []

    async def push(text: str) -> bool:
        sent.append(text)
        return True

    service = ClassReminderService(_reminder_config(), weekly, repo, push)
    assert service.semester_start_source() == "timetable"
    # 2026-09-14 是 mportal 第1周周一；示例课程第3-4节 09:50 开始。
    before = datetime(2026, 9, 14, 9, 19, tzinfo=TZ)
    assert await service.check_once(before) == []
    in_window = datetime(2026, 9, 14, 9, 20, tzinfo=TZ)
    pushed = await service.check_once(in_window)
    assert len(pushed) == 1
    assert "示例课程" in sent[0]
    repo.close()


def test_reminder_semester_start_resolution(tmp_path):
    repo = SnapshotRepository(tmp_path / "jwgl.sqlite")
    repo.set_meta("current_period", "2026 秋")
    weekly = WeeklyScheduleService(repo)
    weekly.regenerate_mportal(
        "2026 秋", {1: parse_week_payload(_week_body(_cell("示例课程")))}
    )
    service = ClassReminderService(_reminder_config(), weekly, repo, lambda _text: None)
    assert service._semester_start() is None
    weekly.save_term_info(TERM_INFO)
    assert service._semester_start().isoformat() == "2026-09-14"
    assert service.semester_start_source() == "mportal"
    manual = ClassReminderService(
        _reminder_config(semester_start_date="2026-09-01"),
        weekly,
        repo,
        lambda _text: None,
    )
    assert manual._semester_start().isoformat() == "2026-09-01"
    assert manual.semester_start_source() == "manual"
    repo.close()


def test_format_validation_report_scopes():
    report = {
        "period": "2026 秋",
        "skipped": False,
        "matched": 10,
        "total_reference": 12,
        "mismatch_weeks": [2, 3],
        "weeks": {
            2: {
                "only_in_reference": ["星期一 第3-4节 甲 @ 综合楼101"],
                "only_in_candidate": [],
            },
            3: {
                "only_in_reference": [],
                "only_in_candidate": ["星期二 第1-2节 乙 @ 科研楼B101"],
            },
        },
    }
    text = format_validation_report(report)
    assert "2 周不一致" in text
    assert "教务文本缺少：星期一 第3-4节 甲 @ 综合楼101" in text
    scoped = format_validation_report(report, week=2)
    assert "第2周" in scoped and "第3周" not in scoped
    consistent = format_validation_report(
        {"period": "2026 秋", "skipped": False, "matched": 5, "mismatch_weeks": []}
    )
    assert "一致" in consistent


def test_regenerate_mportal_stores_per_term_and_default(tmp_path):
    repo = SnapshotRepository(tmp_path / "jwgl.sqlite")
    repo.set_meta("current_period", "2026 秋")
    service = WeeklyScheduleService(repo)
    weeks = {1: parse_week_payload(_week_body(_cell("示例课程")))}
    service.regenerate_mportal("2026 秋", weeks)
    service.regenerate_mportal("2027 春", weeks)
    assert service.get_timetable()["period"] == "2026 秋"
    assert service.get_timetable("2027 春")["period"] == "2027 春"
    assert service.get_timetable("2020 春") == {}
    repo.close()


@pytest.mark.asyncio
async def test_poll_backfills_all_scanned_terms_once(tmp_path):
    repo = SnapshotRepository(tmp_path / "jwgl.sqlite")
    _seed_snapshot(repo, "第1周 星期一 第3-4节 综合楼101")
    weekly = WeeklyScheduleService(repo)

    async def push(_text: str) -> bool:
        return True

    poll = PollService({"mportal_enabled": True}, None, repo, push, weekly)
    result = {
        "scores": {},
        "courses": {
            "2026 秋": [_jwgl_record("示例课程", "第1周 星期一 第3-4节 综合楼101")]
        },
        "exams": [],
        "current_period": "2026 秋",
        "mportal": {
            "term_info": TERM_INFO,
            "scans": {
                "2020 春": {
                    "countweek": 18,
                    "weeks": {1: parse_week_payload(_week_body(_cell("示例课程")))},
                },
                "2026 秋": _mportal_result()["scans"]["2026 秋"],
                "2027 春": {
                    "countweek": 18,
                    "weeks": {1: parse_week_payload(_week_body(_cell("示例课程")))},
                },
            },
            "backfill": True,
            "validate_labels": ["2026 秋", "2027 春"],
        },
    }
    await poll._diff_and_store(result, False, False)
    assert repo.get_meta("backfill_done:mportal") == "1"
    assert weekly.get_timetable("2020 春")["source"] == "mportal"
    assert weekly.get_timetable("2027 春")["source"] == "mportal"
    assert weekly.get_timetable()["period"] == "2026 秋"
    repo.close()


def test_weeks_from_structured_records_round_trip():
    raw = {
        "课程名称": "示例课程",
        "课程号": "XX000001",
        "任课教师": "张三",
        "上课时间、地点": "1-3周 星期一 第3-4节 综合楼101",
    }
    structured = structure_course_record(raw)
    weeks = weeks_from_structured_records([structured])
    assert sorted(weeks) == [1, 2, 3]
    occurrence = weeks[1][0]
    assert occurrence["course_name"] == "示例课程"
    assert occurrence["weekday"] == 1
    assert occurrence["sections"] == [3, 4]
    assert occurrence["location"] == "综合楼101"


@pytest.mark.asyncio
async def test_validate_query_term_against_cached_mportal(tmp_path):
    repo = SnapshotRepository(tmp_path / "jwgl.sqlite")
    weekly = WeeklyScheduleService(repo)
    scan = {
        "term_info": TERM_INFO,
        "countweek": 17,
        "weeks": {1: parse_week_payload(_week_body(_cell("示例课程")))},
    }
    weekly.regenerate_mportal("2026 秋", scan["weeks"])
    session = AsyncMock()
    poll = PollService(
        {"mportal_enabled": True},
        session,
        repo,
        lambda _text: None,
        weekly,
    )
    structured = [
        structure_course_record(
            _jwgl_record("示例课程", "第1周 星期一 第3-4节 综合楼101")
        )
    ]
    report = await poll.validate_query_term("2026", "秋", structured)
    assert report["consistent"] is True
    assert report["matched"] == 1

    moved = [
        structure_course_record(
            _jwgl_record("示例课程", "第1周 星期一 第3-4节 科研楼B101")
        )
    ]
    report = await poll.validate_query_term("2026", "秋", moved)
    assert report["consistent"] is False
    assert report["mismatch_weeks"] == [1]
    assert report["weeks"]["1"]["only_in_candidate"]

    assert await poll.validate_query_term("", "", structured) is None
    assert await poll.validate_query_term("2027", "春", structured) is None
    session.fetch_mportal_timetable.assert_not_awaited()
    repo.close()
