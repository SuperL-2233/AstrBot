import json
import sys
from pathlib import Path

import pytest


PLUGIN_PARENT = Path(__file__).resolve().parents[1] / "data" / "plugins"
if str(PLUGIN_PARENT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_PARENT))

from astrbot_plugin_cugb_assistant.importers.import_timetable import TimetableImporter
from astrbot_plugin_cugb_assistant.providers.agent_webhook_provider import AgentWebhookProvider
from astrbot_plugin_cugb_assistant.providers.classroom_mportal_provider import (
    ClassroomMportalProvider,
)
from astrbot_plugin_cugb_assistant.repositories.classroom_repo import ClassroomRepository
from astrbot_plugin_cugb_assistant.repositories.course_repo import CourseRepository
from astrbot_plugin_cugb_assistant.repositories.notification_repo import NotificationRepository
from astrbot_plugin_cugb_assistant.services.agent_notification_service import (
    AgentNotificationService,
    NotificationRequestError,
)
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


@pytest.mark.asyncio
async def test_agent_notification_delivers_once_and_persists_target(tmp_path):
    config = load_plugin_config(
        {
            "agent_notification": {
                "enabled": True,
                "secret": "test-secret",
                "database": str(tmp_path / "notifications.sqlite"),
            }
        }
    )
    repo = NotificationRepository(tmp_path / "notifications.sqlite")
    service = AgentNotificationService(config, repo)
    service.bind_target("aiocqhttp:FriendMessage:123456")
    sent = []

    async def send_message(target, message):
        sent.append((target, message))
        return True

    payload = {
        "source": "codex",
        "event": "agent-turn-complete",
        "event_id": "codex:thread-1:turn-1",
        "session_id": "thread-1",
        "project": "/work/example",
        "message": "实现与测试已完成。",
    }
    first = await service.deliver(payload, send_message)
    second = await service.deliver(payload, send_message)

    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert len(sent) == 1
    assert sent[0][0] == "aiocqhttp:FriendMessage:123456"
    assert "[Codex] 一轮对话已结束" in sent[0][1]
    assert "项目：example" in sent[0][1]
    assert NotificationRepository(tmp_path / "notifications.sqlite").get_target_umo() == sent[0][0]


@pytest.mark.asyncio
async def test_agent_notification_releases_failed_delivery_for_retry(tmp_path):
    config = load_plugin_config(None)
    repo = NotificationRepository(tmp_path / "notifications.sqlite")
    service = AgentNotificationService(config, repo)
    service.bind_target("aiocqhttp:FriendMessage:123456")
    payload = {
        "source": "opencode",
        "event": "session.idle",
        "event_id": "opencode:session-1:message-1",
        "session_id": "session-1",
    }

    async def failed_send(_target, _message):
        return False

    with pytest.raises(NotificationRequestError) as first_error:
        await service.deliver(payload, failed_send)
    with pytest.raises(NotificationRequestError) as retry_error:
        await service.deliver(payload, failed_send)

    assert first_error.value.error_code == "NOTIFICATION_DELIVERY_FAILED"
    assert retry_error.value.error_code == "NOTIFICATION_DELIVERY_FAILED"


@pytest.mark.asyncio
async def test_agent_notification_rejects_mismatched_source_event(tmp_path):
    service = AgentNotificationService(
        load_plugin_config(None),
        NotificationRepository(tmp_path / "notifications.sqlite"),
    )

    with pytest.raises(NotificationRequestError) as error:
        await service.deliver(
            {
                "source": "codex",
                "event": "session.idle",
                "event_id": "bad-event",
                "session_id": "thread-1",
            },
            lambda _target, _message: None,
        )

    assert error.value.error_code == "INVALID_EVENT"


@pytest.mark.asyncio
async def test_agent_webhook_requires_bearer_token():
    received = []

    async def callback(payload):
        received.append(payload)
        return {"success": True}

    class Request:
        def __init__(self, authorization):
            self.headers = {"Authorization": authorization}

        async def json(self):
            return {"event_id": "event-1"}

    provider = AgentWebhookProvider(
        {
            "agent_notification": {
                "enabled": True,
                "secret": "test-secret",
            }
        },
        callback,
    )
    unauthorized = await provider._handle(Request("Bearer wrong"))
    authorized = await provider._handle(Request("Bearer test-secret"))

    assert unauthorized.status == 401
    assert authorized.status == 200
    assert json.loads(authorized.body) == {"success": True}
    assert received == [{"event_id": "event-1"}]
