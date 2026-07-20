from pathlib import Path
import sys

import pytest


PLUGIN_PARENT = Path(__file__).resolve().parents[1] / "data" / "plugins"
if str(PLUGIN_PARENT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_PARENT))

from astrbot_plugin_cugb_assistant.importers.import_timetable import TimetableImporter
from astrbot_plugin_cugb_assistant.providers.classroom_mportal_provider import (
    ClassroomMportalProvider,
)
from astrbot_plugin_cugb_assistant.repositories.classroom_repo import ClassroomRepository
from astrbot_plugin_cugb_assistant.repositories.course_repo import CourseRepository
from astrbot_plugin_cugb_assistant.services.chaoxing_service import ChaoxingService
from astrbot_plugin_cugb_assistant.services.classroom_service import ClassroomService
from astrbot_plugin_cugb_assistant.services.course_service import CourseService
from astrbot_plugin_cugb_assistant.utils.config_loader import load_plugin_config


def test_course_service_reports_missing_timetable(tmp_path):
    repo = CourseRepository(tmp_path / "courses.sqlite")
    service = CourseService(repo, TimetableImporter(tmp_path / "missing.xlsx"))

    result = service.get_today_courses()

    assert result["success"] is False
    assert "课程表刷新" in result["message"]


@pytest.mark.asyncio
async def test_classroom_service_disabled_returns_clear_message(tmp_path):
    repo = ClassroomRepository(tmp_path / "classrooms.sqlite")
    provider = ClassroomMportalProvider(repo, load_plugin_config(None))
    service = ClassroomService(load_plugin_config({"classroom": {"provider": "disabled"}}), provider)

    result = await service.get_empty_classrooms_now()

    assert result["success"] is False
    assert result["error_code"] == "CLASSROOM_SOURCE_NOT_CONFIGURED"


@pytest.mark.asyncio
async def test_chaoxing_service_disabled_returns_clear_message():
    service = ChaoxingService(load_plugin_config(None), provider=None)

    result = await service.get_pending_homework()

    assert result["success"] is False
    assert result["error_code"] == "CHAOXING_NOT_ENABLED"


def test_mportal_provider_resolves_sections_and_filters_occupied_rooms():
    sections = {
        "jieci": {"上午": [1, 2, 3]},
        "time": {
            "1": "08:00-08:45",
            "2": "08:55-09:40",
            "3": "10:00-10:45",
        },
    }
    section_ids = ClassroomMportalProvider._resolve_section_ids(
        sections,
        start_time=__import__("datetime").time(8, 30),
        end_time=__import__("datetime").time(9, 10),
    )

    assert section_ids == ["1", "2"]

    rooms = ClassroomMportalProvider._normalize_rooms(
        [
            {
                "jxlm": "综合楼",
                "jsm": "101",
                "jsrl": "80",
                "zyjc": "3",
                "zyxx": {"1": {"name": ""}, "2": {"name": ""}, "3": {"name": "线性代数"}},
            },
            {
                "jxlm": "综合楼",
                "jsm": "102",
                "jsrl": "60",
                "zyjc": "1",
                "zyxx": {"1": {"name": "高等数学"}, "2": {"name": ""}},
            },
        ],
        start_dt=__import__("datetime").datetime(2026, 6, 11, 8, 30),
        end_dt=__import__("datetime").datetime(2026, 6, 11, 9, 10),
        required_sections=section_ids,
    )

    assert len(rooms) == 1
    assert rooms[0]["building"] == "综合楼"
    assert rooms[0]["room"] == "101"
    assert rooms[0]["capacity"] == 80


def test_classroom_repo_saves_and_queries_cache(tmp_path):
    repo = ClassroomRepository(tmp_path / "classrooms.sqlite")
    count = repo.replace_cache(
        "2026-06-11",
        [
            {
                "building": "综合楼",
                "room": "综合楼101",
                "capacity": 340,
                "occupied_sections": ["3"],
                "source": "mportal",
                "raw_data": {"jxlm": "综合楼", "jsm": "综合楼101", "zyjc": "3"},
            }
        ],
        "2026-06-11 12:00",
    )

    rows = repo.query_cache("2026-06-11", "综合楼")

    assert count == 1
    assert rows[0]["room"] == "综合楼101"
    assert rows[0]["occupied_sections"] == ["3"]
    assert repo.last_update_time() == "2026-06-11 12:00"
