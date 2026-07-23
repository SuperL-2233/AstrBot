from datetime import datetime, timedelta
from pathlib import Path
import sys

import pytest


PLUGIN_PARENT = Path(__file__).resolve().parents[1] / "data" / "plugins"
if str(PLUGIN_PARENT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_PARENT))

from astrbot_plugin_chaoxing_deadline.providers import (
    chaoxing_provider as provider_module,
)
from astrbot_plugin_chaoxing_deadline.providers.chaoxing_provider import (
    ChaoxingProvider,
)
from astrbot_plugin_chaoxing_deadline.repositories.task_repository import TaskRepository
from astrbot_plugin_chaoxing_deadline.services.reminder_service import ReminderService
from astrbot_plugin_chaoxing_deadline.services.task_service import TaskService


def task(deadline: str, task_type: str = "作业") -> dict:
    return {
        "task_key": f"key-{task_type}",
        "course_name": "测试课程",
        "course_id": "10",
        "class_id": "20",
        "task_type": task_type,
        "title": f"测试{task_type}",
        "status": "pending",
        "start_time": "",
        "deadline": deadline,
        "source_update_time": "2026-07-23 10:00:00",
        "url": "https://example.test/task",
        "note": "",
        "source": ChaoxingProvider.SOURCE,
    }


def test_repository_keeps_missing_deadlines_queryable_but_not_remindable(tmp_path):
    repository = TaskRepository(tmp_path / "tasks.sqlite")
    repository.replace_tasks([task("", "视频")])

    assert repository.query_tasks("视频")[0]["deadline"] == ""
    assert repository.due_reminders(24) == []


def test_task_service_validates_type_and_deadline_window(tmp_path):
    repository = TaskRepository(tmp_path / "tasks.sqlite")
    config = {"enabled": False}
    provider = ChaoxingProvider(config, repository)
    service = TaskService(config, provider, repository)

    assert service.get_tasks("考试")["error_code"] == "INVALID_TASK_TYPE"
    assert service.get_deadlines(366)["error_code"] == "INVALID_WITHIN_DAYS"


@pytest.mark.asyncio
async def test_reminder_delivery_is_deduplicated(tmp_path):
    repository = TaskRepository(tmp_path / "tasks.sqlite")
    deadline = (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    repository.replace_tasks([task(deadline)])
    repository.set_target_umo("aiocqhttp:FriendMessage:123")
    service = ReminderService(
        {"reminder_enabled": True, "remind_before_hours": 24}, repository
    )
    messages: list[str] = []

    async def send(_target: str, text: str) -> bool:
        messages.append(text)
        return True

    first = await service.deliver_due(send)
    second = await service.deliver_due(send)

    assert first["sent"] == 1
    assert second["sent"] == 0
    assert len(messages) == 1
    assert "测试作业" in messages[0]


@pytest.mark.asyncio
async def test_failed_reminder_can_be_retried(tmp_path):
    repository = TaskRepository(tmp_path / "tasks.sqlite")
    deadline = (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    repository.replace_tasks([task(deadline)])
    repository.set_target_umo("aiocqhttp:FriendMessage:123")
    service = ReminderService(
        {"reminder_enabled": True, "remind_before_hours": 24}, repository
    )

    async def fail(_target: str, _text: str) -> bool:
        raise RuntimeError("adapter unavailable")

    async def succeed(_target: str, _text: str) -> bool:
        return True

    with pytest.raises(RuntimeError, match="adapter unavailable"):
        await service.deliver_due(fail)
    result = await service.deliver_due(succeed)

    assert result["sent"] == 1


def test_online_provider_normalizes_all_supported_types(tmp_path, monkeypatch):
    repository = TaskRepository(tmp_path / "tasks.sqlite")
    config = {"timeout_seconds": 20, "enabled": True, "phone": "p", "password": "s"}
    provider = ChaoxingProvider(config, repository)
    api = provider_module.chaoxing_api
    monkeypatch.setattr(api, "create_session", lambda: object())
    monkeypatch.setattr(api, "login", lambda *_args: "uid")
    monkeypatch.setattr(
        api,
        "get_courses",
        lambda *_args: [
            {"name": "测试课程", "courseId": "10", "classId": "20", "url": "u"}
        ],
    )
    monkeypatch.setattr(
        api,
        "get_course_context",
        lambda *_args: ({"courseId": "10", "classId": "20", "url": "u"}, "page"),
    )
    monkeypatch.setattr(
        api,
        "extract_video_rows",
        lambda *_args: (
            [
                {
                    "type": "视频",
                    "name": "第一章",
                    "item_id": "v1",
                    "status": "",
                    "deadline": "",
                    "url": "v",
                }
            ],
            {},
        ),
    )
    monkeypatch.setattr(
        api,
        "extract_course_schedule",
        lambda *_args: {"deadline": "2026-08-01 12:00:00", "deadline_raw": "raw"},
    )
    monkeypatch.setattr(
        api,
        "extract_work_rows",
        lambda *_args, **_kwargs: (
            [
                {
                    "type": "作业",
                    "name": "作业一",
                    "item_id": "h1",
                    "status": "未交",
                    "deadline": "2026-07-30 12:00:00",
                    "url": "h",
                }
            ],
            "",
        ),
    )
    monkeypatch.setattr(
        api,
        "extract_discussion_rows",
        lambda *_args: (
            [
                {
                    "type": "讨论",
                    "name": "主题一",
                    "item_id": "d1",
                    "status": "",
                    "deadline": "",
                    "url": "d",
                }
            ],
            "",
        ),
    )

    result = provider._refresh_sync("p", "s")

    assert result["task_count"] == 3
    assert {row["task_type"] for row in repository.query_tasks()} == {
        "作业",
        "视频",
        "讨论",
    }
    video = repository.query_tasks("视频")[0]
    assert video["deadline"] == "2026-08-01 12:00:00"
    assert video["source"] == "SuperL-2233/chaoxing-api@554e01a"


def test_upstream_parser_does_not_invent_a_missing_deadline():
    api = provider_module.chaoxing_api

    assert api.extract_start_and_deadline("普通视频章节，没有开放或截止日期") == (
        "",
        "",
        "",
        "",
    )
