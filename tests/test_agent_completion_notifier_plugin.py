import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from aiohttp import ClientSession

PLUGIN_PARENT = Path(__file__).resolve().parents[1] / "data" / "plugins"
if str(PLUGIN_PARENT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_PARENT))

from astrbot_plugin_agent_completion_notifier import main as notifier_main
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


@pytest.fixture
def generic_service(tmp_path):
    service = NotificationService(
        load_plugin_config({"allowed_sources": []}),
        NotificationRepository(tmp_path / "notifications.sqlite"),
    )
    service.bind_target("aiocqhttp:FriendMessage:123456")
    return service


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {"title": "备份完成", "message": "文件已上传。\n耗时 30 秒。"},
            "备份完成\n\n文件已上传。\n耗时 30 秒。",
        ),
        (
            {"message": "**部署完成**\nhttps://example.com/app?a=1&b=2\n✅"},
            "**部署完成**\nhttps://example.com/app?a=1&b=2\n✅",
        ),
        (
            {
                "title": "  ",
                "message": "  保留 [查看结果](https://example.com/result)  ",
                "source": "external-service",
            },
            "保留 [查看结果](https://example.com/result)",
        ),
    ],
)
async def test_generic_notification_uses_custom_text(
    generic_service, payload, expected
):
    send = AsyncMock(return_value=True)

    result = await generic_service.deliver(payload, send, generic=True)

    assert result["success"] is True
    assert result["duplicate"] is False
    send.assert_awaited_once_with("aiocqhttp:FriendMessage:123456", expected)


@pytest.mark.asyncio
async def test_generic_notification_without_id_sends_each_time(generic_service):
    send = AsyncMock(return_value=True)
    payload = {"message": "定时任务执行完成。"}

    first = await generic_service.deliver(payload, send, generic=True)
    second = await generic_service.deliver(payload, send, generic=True)

    assert first["duplicate"] is False
    assert second["duplicate"] is False
    assert send.await_count == 2
    with generic_service.repository._connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM notification_deliveries"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.asyncio
async def test_generic_notification_idempotency_survives_reload(generic_service):
    send = AsyncMock(return_value=True)
    payload = {
        "title": "部署成功",
        "message": "服务已更新。",
        "source": "deployment",
        "event_id": "deployment:release-1",
    }

    first = await generic_service.deliver(payload, send, generic=True)
    reloaded = NotificationService(
        generic_service.config,
        NotificationRepository(generic_service.repository.database_path),
    )
    duplicate = await reloaded.deliver(payload, send, generic=True)

    assert first["duplicate"] is False
    assert duplicate["duplicate"] is True
    send.assert_awaited_once_with(
        "aiocqhttp:FriendMessage:123456", "部署成功\n\n服务已更新。"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [False, RuntimeError("send failed"), asyncio.CancelledError()]
)
async def test_generic_notification_failed_send_can_retry(generic_service, failure):
    send = AsyncMock(side_effect=[failure, True])
    payload = {"message": "测试通知", "event_id": "retry:event-1"}
    expected_error = (
        asyncio.CancelledError
        if isinstance(failure, asyncio.CancelledError)
        else NotificationRequestError
    )

    with pytest.raises(expected_error) as error:
        await generic_service.deliver(payload, send, generic=True)
    if isinstance(error.value, NotificationRequestError):
        assert error.value.http_status == 502

    retry = await generic_service.deliver(payload, send, generic=True)
    duplicate = await generic_service.deliver(payload, send, generic=True)

    assert retry["duplicate"] is False
    assert duplicate["duplicate"] is True
    assert send.await_count == 2


@pytest.mark.asyncio
async def test_generic_notification_requires_bound_target(generic_service):
    generic_service.clear_target()
    send = AsyncMock(return_value=True)

    with pytest.raises(NotificationRequestError) as error:
        await generic_service.deliver({"message": "通知正文"}, send, generic=True)

    assert error.value.error_code == "NOTIFICATION_TARGET_NOT_BOUND"
    assert error.value.http_status == 503
    send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "error_code"),
    [
        pytest.param([], "INVALID_JSON_BODY", id="not-object"),
        pytest.param({}, "INVALID_FIELD_TYPE", id="missing-message"),
        pytest.param({"message": False}, "INVALID_FIELD_TYPE", id="message-type"),
        pytest.param({"message": "  "}, "MISSING_FIELD", id="blank-message"),
        pytest.param({"message": "x" * 12001}, "FIELD_TOO_LONG", id="message-length"),
        pytest.param(
            {"message": "通知", "title": 123}, "INVALID_FIELD_TYPE", id="title-type"
        ),
        pytest.param(
            {"message": "通知", "title": "x" * 201},
            "FIELD_TOO_LONG",
            id="title-length",
        ),
        pytest.param(
            {"message": "通知", "source": []}, "INVALID_FIELD_TYPE", id="source-type"
        ),
        pytest.param(
            {"message": "通知", "source": "x" * 33},
            "FIELD_TOO_LONG",
            id="source-length",
        ),
        pytest.param(
            {"message": "通知", "event_id": 123},
            "INVALID_FIELD_TYPE",
            id="event-id-type",
        ),
        pytest.param(
            {"message": "通知", "event_id": "x" * 201},
            "FIELD_TOO_LONG",
            id="event-id-length",
        ),
    ],
)
async def test_generic_notification_validates_fields(
    generic_service, payload, error_code
):
    send = AsyncMock(return_value=True)

    with pytest.raises(NotificationRequestError) as error:
        await generic_service.deliver(payload, send, generic=True)

    assert error.value.error_code == error_code
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_generic_notification_respects_message_limit(generic_service):
    generic_service.config["max_message_chars"] = 100
    send = AsyncMock(return_value=True)

    await generic_service.deliver(
        {"title": "标题", "message": "正文" * 100}, send, generic=True
    )

    text = send.await_args.args[1]
    assert text.startswith("标题\n\n正文")
    assert text.endswith("…（已截断）")
    assert len(text) <= 100


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent_path", "notify_path"),
    [
        ("/api/hooks/agent-completed", None),
        ("/custom/agent", "/custom/notification"),
    ],
)
async def test_plugin_serves_agent_and_generic_webhooks(
    tmp_path, monkeypatch, agent_path, notify_path
):
    monkeypatch.setattr(
        notifier_main,
        "resolve_data_path",
        lambda _name: tmp_path / "notifications.sqlite",
    )
    sent = []

    class Context:
        async def send_message(self, target, chain):
            sent.append((target, chain.get_plain_text()))
            return True

    config = {
        "enabled": True,
        "host": "127.0.0.1",
        "port": 0,
        "path": agent_path,
        "secret": "test-secret",
        "max_body_bytes": 1024,
    }
    if notify_path is not None:
        config["notify_path"] = notify_path
    plugin = notifier_main.AgentCompletionNotifierPlugin(Context(), config)
    plugin.notification_service.bind_target("aiocqhttp:FriendMessage:123456")
    await plugin.initialize()

    try:
        assert plugin.webhook_provider.started is True
        port = plugin.webhook_provider._runner.addresses[0][1]
        generic_path = notify_path or "/api/hooks/notify"
        assert plugin.get_public_status()["notify_path"] == generic_path
        headers = {"Authorization": "Bearer test-secret"}
        async with ClientSession(base_url=f"http://127.0.0.1:{port}") as client:
            for path in (agent_path, generic_path):
                async with client.post(path, json={"message": "未鉴权"}) as response:
                    assert response.status == 401

            payload = {
                "title": "自定义标题",
                "message": "自定义正文\nhttps://example.com/result",
                "source": "external-service",
                "event_id": "external-service:notice-1",
            }
            async with client.post(
                generic_path, json=payload, headers=headers
            ) as response:
                assert response.status == 200
                assert (await response.json())["duplicate"] is False
            async with client.post(
                generic_path, json=payload, headers=headers
            ) as response:
                assert (await response.json())["duplicate"] is True

            async with client.post(
                agent_path,
                json={
                    "source": "codex",
                    "event": "agent-turn-complete",
                    "event_id": "codex:legacy-session:turn-1",
                    "session_id": "legacy-session",
                    "message": "兼容旧回调。",
                },
                headers=headers,
            ) as response:
                assert response.status == 200

            async with client.post(
                agent_path, json=payload, headers=headers
            ) as response:
                assert response.status == 400
                assert (await response.json())["error_code"] == "UNSUPPORTED_SOURCE"

            for path in (agent_path, generic_path):
                async with client.post(path, data="{", headers=headers) as response:
                    assert response.status == 400
                    assert (await response.json())["error_code"] == "INVALID_JSON"
                async with client.post(
                    path, json={"message": "x" * 2048}, headers=headers
                ) as response:
                    assert response.status == 413
                    assert (await response.json())["error_code"] == "REQUEST_TOO_LARGE"

        assert sent == [
            (
                "aiocqhttp:FriendMessage:123456",
                "自定义标题\n\n自定义正文\nhttps://example.com/result",
            ),
            (
                "aiocqhttp:FriendMessage:123456",
                "服务器-Codex 会话结束\n会话：legacy-session\n\n兼容旧回调。",
            ),
        ]
    finally:
        await plugin.terminate()

    assert plugin.webhook_provider.started is False
    assert plugin.webhook_provider._runner is None


@pytest.mark.asyncio
async def test_webhook_rejects_conflicting_paths():
    callback = AsyncMock(return_value={"success": True})
    provider = WebhookProvider(
        load_plugin_config(
            {
                "enabled": True,
                "secret": "test-secret",
                "notify_path": "/same",
                "path": "/same",
            }
        ),
        callback,
        notify_callback=callback,
    )

    with pytest.raises(ValueError, match="notify_path must differ"):
        await provider.start()

    assert provider.started is False
    assert provider._runner is None
