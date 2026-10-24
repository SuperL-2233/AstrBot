import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

PLUGIN_PARENT = Path(__file__).resolve().parents[1] / "data" / "plugins"
if str(PLUGIN_PARENT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_PARENT))

from astrbot_plugin_file_transfer.main import FileTransferPlugin
from astrbot_plugin_file_transfer.providers.transfer_provider import (
    TransferProvider,
    TransferProviderError,
)

from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_tool_exec import call_local_llm_tool
from astrbot.core.provider.register import llm_tools
from astrbot.core.star.filter.command import CommandFilter, GreedyStr


class Event:
    def __init__(self, sender="owner", private=True):
        self.sender = sender
        self.private = private

    def get_sender_id(self):
        return self.sender

    def is_private_chat(self):
        return self.private

    def plain_result(self, text):
        return text


@pytest.fixture
def plugin():
    result = FileTransferPlugin(
        SimpleNamespace(),
        {
            "enabled": True,
            "owner_qq_id": "owner",
            "admin_token": "test-only-" + "x" * 40,
        },
    )
    result.provider.request = AsyncMock(
        return_value={
            "id": "a" * 32,
            "quota_bytes": 2_000_000_000,
            "retention_seconds": 1800,
            "redeem_deadline": 2_000_000_000,
            "public_url": "https://example.com/transfer/",
            "upload_key": "U-test-only-key",
        }
    )
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sender,private,configured",
    [("other", True, True), ("owner", False, True), ("owner", True, False)],
)
async def test_only_explicit_owner_in_private_chat_can_create_grants(
    plugin, sender, private, configured
):
    if not configured:
        plugin.config["owner_qq_id"] = ""
    response = [text async for text in plugin.authorize(Event(sender, private), "")]
    assert response and "U-test-only-key" not in response[0]
    plugin.provider.request.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args,quota,retention",
    [
        ("", 2_000_000_000, 1800),
        ("5G 60", 5_000_000_000, 3600),
        ("0.5G 15", 500_000_000, 900),
        ("10GB 30", 10_000_000_000, 1800),
        ("50M", 50_000_000, 1800),
    ],
)
async def test_authorization_arguments_and_defaults(plugin, args, quota, retention):
    response = [text async for text in plugin.authorize(Event(), args)]
    plugin.provider.request.assert_awaited_once_with(
        "POST", "/admin/grants", {"quota_bytes": quota, "retention_seconds": retention}
    )
    assert "U-test-only-key" in response[0]
    assert "兑换后 30 分钟" in response[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [
        "11G",
        "0G",
        "-1G",
        "2G 0",
        "2G -1",
        "2G nope",
        "2G 30 extra",
        "0.1B",
        "2G 999999999999999999999",
    ],
)
async def test_invalid_authorization_does_not_contact_service(plugin, args):
    response = [text async for text in plugin.authorize(Event(), args)]
    assert response
    plugin.provider.request.assert_not_called()


@pytest.mark.asyncio
async def test_list_and_revoke_are_owner_only_and_validate_complete_id(plugin):
    for handler, args in [
        (plugin.list_command, ()),
        (plugin.revoke, ("a" * 32,)),
        (plugin.status_command, ()),
    ]:
        response = [text async for text in handler(Event("other"), *args)]
        assert "仅允许" in response[0]
    plugin.provider.request.assert_not_called()
    assert [text async for text in plugin.revoke(Event(), "../other")]
    plugin.provider.request.assert_not_called()
    response = [text async for text in plugin.revoke(Event(), "a" * 32)]
    plugin.provider.request.assert_awaited_once_with(
        "DELETE", "/admin/batches/" + "a" * 32, {}
    )
    assert "已撤销" in response[0]


@pytest.mark.asyncio
async def test_disabled_and_unavailable_service_never_issue_fake_keys(plugin):
    plugin.config["enabled"] = False
    assert "尚未启用" in [text async for text in plugin.authorize(Event(), "")][0]
    plugin.provider.request.assert_not_called()
    plugin.config["enabled"] = True
    plugin.provider.request.side_effect = TransferProviderError("无法连接快传服务")
    assert [text async for text in plugin.authorize(Event(), "")] == [
        "无法连接快传服务"
    ]
    assert plugin.get_public_capabilities()["llm_tools"] == [
        "create_file_transfer_grant"
    ]
    assert "admin_token" not in plugin.get_public_status()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "settings,quota,retention",
    [
        ({}, 2_000_000_000, 1800),
        ({"total_size": "5G", "retention_minutes": 60}, 5_000_000_000, 3600),
        ({"total_size": "0.5G", "retention_minutes": 15}, 500_000_000, 900),
        ({"total_size": "10GB"}, 10_000_000_000, 1800),
        ({"total_size": "50M"}, 50_000_000, 1800),
    ],
)
async def test_grant_tool_defaults_and_settings(plugin, settings, quota, retention):
    result = json.loads(await plugin.tool_create_grant(Event(), **settings))
    plugin.provider.request.assert_awaited_once_with(
        "POST", "/admin/grants", {"quota_bytes": quota, "retention_seconds": retention}
    )
    assert result["success"] is True
    assert "https://example.com/transfer/" in result["message"]
    assert "U-test-only-key" in result["message"]
    assert plugin.config["admin_token"] not in result["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sender,private,enabled,owner,allowed",
    [
        ("other", True, True, "owner", False),
        ("owner", False, True, "owner", False),
        ("owner", True, True, "", False),
        ("owner", True, False, "owner", False),
        ("owner", True, True, "owner", True),
    ],
)
async def test_grant_tool_checks_framework_event_before_contacting_service(
    plugin, sender, private, enabled, owner, allowed
):
    plugin.config.update(enabled=enabled, owner_qq_id=owner)
    context = ContextWrapper(context=SimpleNamespace(event=Event(sender, private)))
    responses = [
        result
        async for result in call_local_llm_tool(
            context,
            plugin.tool_create_grant,
            "decorator_handler",
            total_size="5G",
            retention_minutes=60,
        )
    ]
    result = json.loads(responses[0])
    assert result["success"] is allowed
    if allowed:
        plugin.provider.request.assert_awaited_once_with(
            "POST",
            "/admin/grants",
            {"quota_bytes": 5_000_000_000, "retention_seconds": 3600},
        )
        assert "U-test-only-key" in result["message"]
    else:
        assert "U-test-only-key" not in result["message"]
        plugin.provider.request.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "settings",
    [
        {"total_size": "11G"},
        {"total_size": "0G"},
        {"total_size": "0.1B"},
        {"total_size": ""},
        {"total_size": " "},
        {"total_size": "5G 60"},
        {"retention_minutes": 0},
        {"retention_minutes": -1},
        {"retention_minutes": 1.5},
        {"retention_minutes": True},
        {"retention_minutes": None},
        {"retention_minutes": 999_999_999_999_999_999_999},
    ],
)
async def test_invalid_grant_tool_settings_never_contact_service(plugin, settings):
    result = json.loads(await plugin.tool_create_grant(Event(), **settings))
    assert result["success"] is False
    assert result["message"]
    plugin.provider.request.assert_not_called()


@pytest.mark.asyncio
async def test_grant_tool_reports_service_failure_without_retry_or_fake_key(plugin):
    plugin.provider.request.side_effect = TransferProviderError("无法连接快传服务")
    result = json.loads(await plugin.tool_create_grant(Event()))
    assert result == {"success": False, "message": "无法连接快传服务"}
    assert plugin.provider.request.await_count == 1


def test_grant_tool_schema_has_settings_but_no_model_supplied_identity():
    tool = llm_tools.get_func("create_file_transfer_grant")
    assert tool is not None
    assert set(tool.parameters["properties"]) == {"total_size", "retention_minutes"}
    assert tool.parameters["properties"]["total_size"]["type"] == "string"
    assert tool.parameters["properties"]["retention_minutes"]["type"] == "number"


def test_greedy_command_parameters_preserve_defaults_and_reject_extra_text():
    command = CommandFilter("快传授权")
    assert command.validate_and_convert_params([], {"args": GreedyStr}) == {"args": ""}
    assert command.validate_and_convert_params(["5G", "60"], {"args": GreedyStr}) == {
        "args": "5G 60"
    }


@pytest.mark.asyncio
async def test_provider_authenticates_private_api_and_closes_session():
    seen = []

    async def issue(request):
        seen.append((request.headers.get("Authorization"), await request.json()))
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_post("/admin/grants", issue)
    async with TestServer(app) as server:
        provider = TransferProvider(str(server.make_url("")).rstrip("/"), "x" * 40)
        assert await provider.request(
            "POST", "/admin/grants", {"quota_bytes": 2_000_000_000}
        ) == {"ok": True}
        assert provider.last_check_success
        await provider.close()
        assert provider.session.closed
    assert seen == [("Bearer " + "x" * 40, {"quota_bytes": 2_000_000_000})]


@pytest.mark.asyncio
async def test_provider_disallows_redirects_and_bounds_response_size():
    called = []

    async def redirect(_request):
        raise web.HTTPFound("/stolen")

    async def stolen(_request):
        called.append(True)
        return web.json_response({"ok": True})

    async def large(_request):
        return web.Response(body=b"x" * (129 * 1024))

    app = web.Application()
    app.router.add_get("/redirect", redirect)
    app.router.add_get("/stolen", stolen)
    app.router.add_get("/large", large)
    async with TestServer(app) as server:
        provider = TransferProvider(str(server.make_url("")).rstrip("/"), "x" * 40)
        try:
            with pytest.raises(TransferProviderError):
                await provider.request("GET", "/redirect")
            with pytest.raises(TransferProviderError, match="过大"):
                await provider.request("GET", "/large")
        finally:
            await provider.close()
    assert not called
