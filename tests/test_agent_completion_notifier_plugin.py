import json
import sys
from pathlib import Path

import pytest


PLUGIN_PARENT = Path(__file__).resolve().parents[1] / "data" / "plugins"
if str(PLUGIN_PARENT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_PARENT))

from astrbot_plugin_agent_completion_notifier.providers.webhook_provider import (
    WebhookProvider,
)
from astrbot_plugin_agent_completion_notifier.repositories.notification_repo import (
    NotificationRepository,
)
from astrbot_plugin_agent_completion_notifier.services.notification_service import (
    NotificationRequestError,
    NotificationService,
)
from astrbot_plugin_agent_completion_notifier.utils.config_loader import (
    load_plugin_config,
)
from astrbot_plugin_agent_completion_notifier.utils.permission_utils import (
    ensure_owner_private_access,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "event", "source_name"),
    [
        ("codex", "agent-turn-complete", "Codex"),
        ("opencode", "session.idle", "OpenCode"),
    ],
)
async def test_notification_delivers_once_and_persists_target(
    tmp_path, source, event, source_name
):
    config = load_plugin_config(
        {
            "enabled": True,
            "secret": "test-secret",
            "database": str(tmp_path / "notifications.sqlite"),
            "allowed_sources": ["codex", "opencode"],
        }
    )
    repo = NotificationRepository(tmp_path / "notifications.sqlite")
    service = NotificationService(config, repo)
    service.bind_target("aiocqhttp:FriendMessage:123456")
    sent = []

    async def send_message(target, message):
        sent.append((target, message))
        return True

    payload = {
        "source": source,
        "event": event,
        "event_id": f"{source}:thread-1:turn-1",
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
    assert sent[0][1] == (
        f"服务器-{source_name} 会话结束\n会话：thread-1\n\n实现与测试已完成。"
    )
    assert (
        NotificationRepository(tmp_path / "notifications.sqlite").get_target_umo()
        == sent[0][0]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["", "工作站"])
async def test_notification_delivers_configured_custom_source(tmp_path, host):
    service = NotificationService(
        load_plugin_config(
            {
                "allowed_sources": ["codex", "opencode", "Custom-Agent"],
                "target_umo": "aiocqhttp:FriendMessage:123456",
            }
        ),
        NotificationRepository(tmp_path / "notifications.sqlite"),
    )
    sent = []

    async def send_message(target, message):
        sent.append((target, message))
        return True

    payload = {
        "source": "CUSTOM-AGENT",
        "event": "turn.finished",
        "event_id": "custom-agent:session-1:turn-1",
        "session_id": "session-1",
        "host": host,
        "message": "本轮已完成。",
    }
    first = await service.deliver(payload, send_message)
    second = await service.deliver(payload, send_message)

    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert sent == [
        (
            "aiocqhttp:FriendMessage:123456",
            f"{host or '服务器-custom-agent'} 会话结束\n会话：session-1\n\n本轮已完成。",
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("config", "source", "event"),
    [
        (None, "custom-agent", "turn.finished"),
        ({"allowed_sources": ["codex", "opencode"]}, "custom-agent", "turn.finished"),
        ({"allowed_sources": ["codex"]}, "opencode", "session.idle"),
        ({"allowed_sources": []}, "codex", "agent-turn-complete"),
    ],
)
async def test_notification_preserves_source_allowlist(tmp_path, config, source, event):
    service = NotificationService(
        load_plugin_config(config),
        NotificationRepository(tmp_path / "notifications.sqlite"),
    )

    with pytest.raises(NotificationRequestError) as error:
        await service.deliver(
            {
                "source": source,
                "event": event,
                "event_id": "blocked-event",
                "session_id": "session-1",
            },
            lambda _target, _message: None,
        )

    assert error.value.error_code == "UNSUPPORTED_SOURCE"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event", "error_code"),
    [
        (None, "INVALID_FIELD_TYPE"),
        (123, "INVALID_FIELD_TYPE"),
        ("", "MISSING_FIELD"),
        ("   ", "MISSING_FIELD"),
        ("x" * 65, "FIELD_TOO_LONG"),
    ],
)
async def test_notification_validates_custom_event(tmp_path, event, error_code):
    service = NotificationService(
        load_plugin_config({"allowed_sources": ["custom-agent"]}),
        NotificationRepository(tmp_path / "notifications.sqlite"),
    )

    with pytest.raises(NotificationRequestError) as error:
        await service.deliver(
            {
                "source": "custom-agent",
                "event": event,
                "event_id": "custom-agent:session-1:turn-1",
                "session_id": "session-1",
            },
            lambda _target, _message: None,
        )

    assert error.value.error_code == error_code


@pytest.mark.asyncio
async def test_notification_releases_failed_delivery_for_retry(tmp_path):
    config = load_plugin_config(None)
    repo = NotificationRepository(tmp_path / "notifications.sqlite")
    service = NotificationService(config, repo)
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
@pytest.mark.parametrize(
    ("source", "event"),
    [("codex", "session.idle"), ("opencode", "agent-turn-complete")],
)
async def test_notification_rejects_mismatched_source_event(tmp_path, source, event):
    service = NotificationService(
        load_plugin_config(None),
        NotificationRepository(tmp_path / "notifications.sqlite"),
    )

    with pytest.raises(NotificationRequestError) as error:
        await service.deliver(
            {
                "source": source,
                "event": event,
                "event_id": "bad-event",
                "session_id": "thread-1",
            },
            lambda _target, _message: None,
        )

    assert error.value.error_code == "INVALID_EVENT"


@pytest.mark.asyncio
async def test_webhook_requires_bearer_token():
    received = []

    async def callback(payload):
        received.append(payload)
        return {"success": True}

    class Request:
        def __init__(self, authorization):
            self.headers = {"Authorization": authorization}

        async def json(self):
            return {"event_id": "event-1"}

    provider = WebhookProvider(
        load_plugin_config({"enabled": True, "secret": "test-secret"}),
        callback,
    )
    unauthorized = await provider._handle(Request("Bearer wrong"))
    authorized = await provider._handle(Request("Bearer test-secret"))

    assert unauthorized.status == 401
    assert authorized.status == 200
    assert json.loads(authorized.body) == {"success": True}
    assert received == [{"event_id": "event-1"}]


@pytest.mark.asyncio
async def test_webhook_stays_disabled_without_secret():
    async def callback(_payload):
        return {"success": True}

    provider = WebhookProvider(load_plugin_config({"enabled": True}), callback)

    await provider.start()

    assert provider.started is False
    assert provider.disabled_reason == "未配置鉴权密钥"


def test_binding_requires_configured_owner_private_chat():
    class Event:
        def __init__(self, sender_id, private):
            self.sender_id = sender_id
            self.private = private

        def get_sender_id(self):
            return self.sender_id

        def is_private_chat(self):
            return self.private

    assert ensure_owner_private_access(Event("123", True), {}) is not None
    assert (
        ensure_owner_private_access(Event("456", True), {"owner_qq_id": "123"})
        is not None
    )
    assert (
        ensure_owner_private_access(Event("123", False), {"owner_qq_id": "123"})
        is not None
    )
    assert (
        ensure_owner_private_access(Event("123", True), {"owner_qq_id": "123"}) is None
    )
