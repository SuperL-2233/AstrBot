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
from astrbot_plugin_chaoxing_deadline.main import ChaoxingDeadlinePlugin
from astrbot_plugin_chaoxing_deadline.providers.chaoxing_provider import (
    ChaoxingProvider,
)
from astrbot_plugin_chaoxing_deadline.repositories.task_repository import TaskRepository
from astrbot_plugin_chaoxing_deadline.services.reminder_service import ReminderService
from astrbot_plugin_chaoxing_deadline.services.task_service import TaskService
from astrbot_plugin_chaoxing_deadline.utils.config_loader import load_plugin_config
from astrbot_plugin_chaoxing_deadline.utils.permission import ensure_private_chat


def task(
    deadline: str,
    task_type: str = "作业",
    status: str = "pending",
    suffix: str = "",
) -> dict:
    return {
        "task_key": f"key-{task_type}-{status}-{suffix}",
        "course_name": "测试课程",
        "course_id": "10",
        "class_id": "20",
        "task_type": task_type,
        "title": f"测试{task_type}{suffix}",
        "status": status,
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
    assert service.get_tasks(task_status="未知")["error_code"] == "INVALID_TASK_STATUS"
    assert service.get_deadlines(366)["error_code"] == "INVALID_WITHIN_DAYS"


def test_task_queries_support_pending_completed_all_and_type_filters(tmp_path):
    repository = TaskRepository(tmp_path / "tasks.sqlite")
    repository.replace_tasks(
        [
            task("", "作业", "pending"),
            task("", "作业", "completed"),
            task("", "视频", "completed"),
            task("", "讨论", "pending"),
        ]
    )
    service = TaskService(
        {"enabled": False}, ChaoxingProvider({}, repository), repository
    )

    assert len(service.get_tasks(task_status="待办")["data"]) == 2
    assert len(service.get_tasks(task_status="已完成")["data"]) == 2
    assert len(service.get_tasks(task_status="全部")["data"]) == 4
    completed_homeworks = service.get_tasks("作业", task_status="已完成")["data"]
    assert len(completed_homeworks) == 1
    assert completed_homeworks[0]["status"] == "completed"


def test_configuration_does_not_require_owner_qq_id():
    assert "owner_qq_id" not in load_plugin_config(None)


def test_only_reminder_binding_requires_private_chat():
    class Event:
        @staticmethod
        def is_private_chat() -> bool:
            return False

    assert "私聊" in ensure_private_chat(Event())


@pytest.mark.asyncio
async def test_reminder_delivery_is_deduplicated(tmp_path):
    repository = TaskRepository(tmp_path / "tasks.sqlite")
    base = datetime.now().replace(microsecond=0)
    deadline = (base + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
    repository.replace_tasks([task(deadline)])
    repository.set_target_umo("aiocqhttp:FriendMessage:123")
    service = ReminderService(
        {"reminder_enabled": True, "reminder_offsets_hours": [5]}, repository
    )
    service.sync_schedule(base)
    messages: list[str] = []

    async def send(_target: str, text: str) -> bool:
        messages.append(text)
        return True

    due_time = base + timedelta(hours=3, minutes=1)
    first = await service.deliver_due(send, due_time)
    second = await service.deliver_due(send, due_time)

    assert first["sent"] == 1
    assert second["sent"] == 0
    assert len(messages) == 1
    assert "测试作业" in messages[0]


@pytest.mark.asyncio
async def test_failed_reminder_can_be_retried(tmp_path):
    repository = TaskRepository(tmp_path / "tasks.sqlite")
    base = datetime.now().replace(microsecond=0)
    deadline = (base + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
    repository.replace_tasks([task(deadline)])
    repository.set_target_umo("aiocqhttp:FriendMessage:123")
    service = ReminderService(
        {"reminder_enabled": True, "reminder_offsets_hours": [5]}, repository
    )
    service.sync_schedule(base)
    due_time = base + timedelta(hours=3, minutes=1)

    async def fail(_target: str, _text: str) -> bool:
        raise RuntimeError("adapter unavailable")

    async def succeed(_target: str, _text: str) -> bool:
        return True

    with pytest.raises(RuntimeError, match="adapter unavailable"):
        await service.deliver_due(fail, due_time)
    result = await service.deliver_due(succeed, due_time)

    assert result["sent"] == 1


def test_schedule_covers_every_pending_dated_type_and_respects_tracking(tmp_path):
    repository = TaskRepository(tmp_path / "tasks.sqlite")
    base = datetime.now().replace(microsecond=0)
    deadline = (base + timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
    pending = [
        task(deadline, "作业"),
        task(deadline, "视频"),
        task(deadline, "限时章节"),
        task(deadline, "讨论"),
    ]
    repository.replace_tasks(
        pending
        + [
            task(deadline, "作业", "completed", "完成"),
            task("", "视频", "pending", "无截止"),
        ]
    )
    service = ReminderService({"reminder_enabled": True}, repository)

    assert service.sync_schedule(base) == 28
    assert service.status()["scheduled_stages"] == 28

    ignored = service.set_tracking(tracked=False, keyword="测试视频", reason="选做")
    assert ignored["success"] is True
    assert service.status()["scheduled_stages"] == 21
    assert service.status()["ignored_tasks"] == 1

    repository.replace_tasks(pending)
    service.sync_schedule(base)
    assert service.status()["scheduled_stages"] == 21

    restored = service.set_tracking(tracked=True, task_key=ignored["data"]["task_key"])
    assert restored["success"] is True
    assert service.status()["scheduled_stages"] == 28


def test_refresh_removes_future_stages_for_completed_task(tmp_path):
    repository = TaskRepository(tmp_path / "tasks.sqlite")
    base = datetime.now().replace(microsecond=0)
    deadline = (base + timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
    pending = task(deadline, "讨论")
    repository.replace_tasks([pending])
    service = ReminderService({"reminder_enabled": True}, repository)
    service.sync_schedule(base)
    assert service.status()["scheduled_stages"] == 7

    completed = dict(pending, status="completed")
    repository.replace_tasks([completed])
    service.sync_schedule(base)
    assert service.status()["scheduled_stages"] == 0


@pytest.mark.asyncio
async def test_stale_query_refreshes_cloud_once_and_rebuilds_schedule(tmp_path):
    repository = TaskRepository(tmp_path / "tasks.sqlite")
    stale = task("")
    stale["source_update_time"] = "2020-01-01 00:00:00"
    repository.replace_tasks([stale])
    calls = 0
    callbacks = 0
    reminder = ReminderService({"reminder_enabled": True}, repository)

    class Provider:
        async def refresh(self) -> dict:
            nonlocal calls
            calls += 1
            fresh = task(
                (datetime.now() + timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
            )
            fresh["source_update_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            repository.replace_tasks([fresh])
            return {"success": True, "data": {}, "message": "ok", "error_code": None}

        @staticmethod
        def status() -> dict:
            return {}

    def on_refreshed() -> None:
        nonlocal callbacks
        callbacks += 1
        reminder.sync_schedule()

    service = TaskService(
        {"enabled": True, "cache_max_age_hours": 24},
        Provider(),
        repository,
        on_refreshed=on_refreshed,
    )

    first = await service.query_tasks()
    second = await service.query_tasks()

    assert first["cache_refresh"]["refreshed"] is True
    assert second["cache_refresh"]["attempted"] is False
    assert calls == 1
    assert callbacks == 1
    assert reminder.status()["scheduled_stages"] == 7


@pytest.mark.asyncio
async def test_stale_refresh_failure_preserves_and_returns_cached_tasks(tmp_path):
    repository = TaskRepository(tmp_path / "tasks.sqlite")
    stale = task("", suffix="旧缓存")
    stale["source_update_time"] = "2020-01-01 00:00:00"
    repository.replace_tasks([stale])

    class Provider:
        @staticmethod
        async def refresh() -> dict:
            return {
                "success": False,
                "data": None,
                "message": "网络不可用",
                "error_code": "CHAOXING_REFRESH_FAILED",
            }

        @staticmethod
        def status() -> dict:
            return {}

    service = TaskService(
        {"enabled": True, "cache_max_age_hours": 24}, Provider(), repository
    )
    result = await service.query_tasks()

    assert result["success"] is True
    assert result["data"][0]["title"] == "测试作业旧缓存"
    assert result["cache_refresh"]["attempted"] is True
    assert result["cache_refresh"]["refreshed"] is False
    assert "网络不可用" in result["message"]


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
                },
                {
                    "type": "作业",
                    "name": "作业二",
                    "item_id": "h2",
                    "status": "已提交",
                    "deadline": "2026-07-29 12:00:00",
                    "url": "h2",
                },
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

    assert result["task_count"] == 4
    assert {row["task_type"] for row in repository.query_tasks()} == {
        "作业",
        "视频",
        "讨论",
    }
    video = repository.query_tasks("视频")[0]
    assert video["deadline"] == "2026-08-01 12:00:00"
    assert video["source"] == "SuperL-2233/chaoxing-api@554e01a"
    completed = repository.query_tasks("作业", task_status="completed")
    assert len(completed) == 1
    assert completed[0]["title"] == "作业二"


def test_upstream_parser_does_not_invent_a_missing_deadline():
    api = provider_module.chaoxing_api

    assert api.extract_start_and_deadline("普通视频章节，没有开放或截止日期") == (
        "",
        "",
        "",
        "",
    )


def test_expired_label_is_not_treated_as_completed():
    assert not any(marker in "已截止" for marker in ChaoxingProvider.COMPLETED_MARKERS)


@pytest.mark.asyncio
async def test_category_tools_query_completed_records():
    class TaskStatus:
        @staticmethod
        async def query_tasks(task_type: str, keyword: str, task_status: str) -> dict:
            return {
                "success": True,
                "data": {
                    "type": task_type,
                    "status": task_status,
                    "keyword": keyword,
                },
            }

    plugin = object.__new__(ChaoxingDeadlinePlugin)
    plugin.task_service = TaskStatus()
    payload = __import__("json").loads(
        await plugin.tool_get_chaoxing_homeworks(None, "已完成", "线性代数")
    )

    assert payload["data"] == {
        "type": "作业",
        "status": "已完成",
        "keyword": "线性代数",
    }
