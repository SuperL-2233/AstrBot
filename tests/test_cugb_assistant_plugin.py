from copy import deepcopy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


PLUGIN_PARENT = Path(__file__).resolve().parents[1] / "data" / "plugins"
if str(PLUGIN_PARENT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_PARENT))

from astrbot_plugin_cugb_assistant.formatters.classroom_formatter import (
    format_classroom_tool_result,
)
from astrbot_plugin_cugb_assistant.main import CugbAssistantPlugin
from astrbot_plugin_cugb_assistant.providers.classroom_mportal_provider import (
    ClassroomMportalProvider,
)
from astrbot_plugin_cugb_assistant.repositories.classroom_repo import (
    ClassroomRepository,
)
from astrbot_plugin_cugb_assistant.services.classroom_service import ClassroomService
from astrbot_plugin_cugb_assistant.services.distance_service import DistanceService
from astrbot_plugin_cugb_assistant.utils.config_loader import load_plugin_config
from astrbot_plugin_cugb_assistant.utils.result import make_result


@pytest.mark.asyncio
async def test_classroom_service_disabled_returns_clear_message(tmp_path):
    repo = ClassroomRepository(tmp_path / "classrooms.sqlite")
    provider = ClassroomMportalProvider(repo, load_plugin_config(None))
    service = ClassroomService(
        load_plugin_config({"classroom": {"provider": "disabled"}}), provider
    )

    result = await service.get_empty_classrooms_now()

    assert result["success"] is False
    assert result["error_code"] == "CLASSROOM_SOURCE_NOT_CONFIGURED"


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
                "zyxx": {
                    "1": {"name": ""},
                    "2": {"name": ""},
                    "3": {"name": "线性代数"},
                },
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,args",
    [
        ("tool_get_empty_classrooms_now", ("教三",)),
        (
            "tool_get_empty_classrooms_by_time",
            ("教三", "2026-10-24 14:00", "2026-10-24 16:00"),
        ),
    ],
)
async def test_classroom_tools_preserve_all_rooms_and_shared_metadata(method, args):
    rows = [
        {
            "building": "教三",
            "room": str(number),
            "capacity": 80,
            "start_time": "2026-10-24 14:00",
            "end_time": "2026-10-24 16:00",
            "source_update_time": "2026-10-24 12:00",
            "source": "cache",
        }
        for number in range(101, 151)
    ]
    result = make_result(
        True, rows, source_status={"provider": "mportal", "count": 100}
    )
    original = deepcopy(result)
    plugin = object.__new__(CugbAssistantPlugin)
    plugin.classroom_service = SimpleNamespace(
        get_empty_classrooms_now=AsyncMock(return_value=result),
        get_empty_classrooms_by_time=AsyncMock(return_value=result),
    )

    output = await getattr(plugin, method)(None, *args)
    payload = json.loads(output)

    assert len(payload["data"]) == 50
    assert payload["query"] == {
        "start_time": rows[0]["start_time"],
        "end_time": rows[0]["end_time"],
        "source_update_time": rows[0]["source_update_time"],
        "source": "cache",
    }
    assert payload["data"][0] == {"building": "教三", "room": "101", "capacity": 80}
    assert payload["data"][-1]["room"] == "150"
    assert payload["source_status"] == result["source_status"]
    assert result == original


def test_classroom_tool_keeps_different_row_update_times():
    rows = [
        {"building": "教三", "room": "101", "source_update_time": "2026-10-24 12:00"},
        {"building": "教三", "room": "102", "source_update_time": "2026-10-24 13:00"},
    ]

    payload = json.loads(format_classroom_tool_result(make_result(True, rows)))

    assert payload["data"] == rows
    assert "query" not in payload


@pytest.mark.asyncio
@pytest.mark.parametrize("limit,returned", [(None, 5), (0, 12), (8, 8), (20, 12)])
async def test_recommendation_tool_limits_results_after_ranking(limit, returned):
    plugin = object.__new__(CugbAssistantPlugin)
    rows = [
        {"building": building, "room": str(number)}
        for building in ("教三", "教一")
        for number in range(101, 107)
    ]
    plugin.classroom_service = SimpleNamespace(
        get_empty_classrooms_now=AsyncMock(return_value=make_result(True, rows))
    )
    plugin.distance_service = DistanceService()
    kwargs = {} if limit is None else {"limit": limit}

    payload = json.loads(
        await plugin.tool_recommend_study_room(
            None, next_course_location="教一201", **kwargs
        )
    )

    assert payload["success"] is True
    assert len(payload["data"]) == returned
    assert [row["building"] for row in payload["data"][:5]] == ["教一"] * 5
    assert all(row["reason"] for row in payload["data"])
    assert all("priority" not in row for row in payload["data"])
    if limit != 0:
        assert payload["total_count"] == 12
        assert payload["returned_count"] == returned
        assert payload["has_more"] is (returned < 12)


@pytest.mark.asyncio
async def test_recommendation_tool_preserves_disabled_source_failure():
    plugin = object.__new__(CugbAssistantPlugin)
    failure = make_result(
        False,
        [],
        "source unavailable",
        "CLASSROOM_SOURCE_NOT_CONFIGURED",
        {"enabled": False},
    )
    plugin.classroom_service = SimpleNamespace(
        get_empty_classrooms_now=AsyncMock(return_value=failure)
    )

    payload = json.loads(await plugin.tool_recommend_study_room(None))

    assert payload == failure


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [-1, False, 1.5])
async def test_recommendation_tool_rejects_invalid_limit_before_query(limit):
    plugin = object.__new__(CugbAssistantPlugin)
    plugin.classroom_service = SimpleNamespace(get_empty_classrooms_now=AsyncMock())

    payload = json.loads(await plugin.tool_recommend_study_room(None, limit=limit))

    assert payload["success"] is False
    assert payload["error_code"] == "INVALID_LIMIT"
    plugin.classroom_service.get_empty_classrooms_now.assert_not_awaited()
