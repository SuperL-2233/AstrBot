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
from astrbot_plugin_file_transfer.formatters.transfer_formatter import (
    format_files,
    format_space,
)
from astrbot_plugin_file_transfer.providers.transfer_provider import (
    TransferProvider,
    TransferProviderError,
)
from astrbot_plugin_capability_registry.services.registry_service import (
    PluginRegistryService,
)

from astrbot.core.provider.register import llm_tools
from astrbot.core.star.filter.command import CommandFilter, GreedyStr
from astrbot.core.star.star_handler import star_handlers_registry


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
    plugin.provider.request.side_effect = TransferProviderError("无法连接暂传服务")
    assert [text async for text in plugin.authorize(Event(), "")] == [
        "无法连接暂传服务"
    ]
    assert plugin.get_public_capabilities()["llm_tools"] == []
    assert "admin_token" not in plugin.get_public_status()


def test_plugin_registers_no_language_model_tools(plugin):
    assert plugin.get_public_capabilities()["llm_tools"] == []
    assert llm_tools.get_func("create_file_transfer_grant") is None
    assert not any(
        tool.handler_module_path == FileTransferPlugin.__module__
        for tool in llm_tools.func_list
    )


@pytest.mark.asyncio
async def test_capability_registry_reports_only_zanchuan_commands(plugin):
    metadata = SimpleNamespace(
        name="astrbot_plugin_file_transfer",
        display_name="暂传",
        root_dir_name="astrbot_plugin_file_transfer",
        version="0.3.0",
        author="test",
        activated=True,
        reserved=False,
        star_cls=plugin,
    )
    registry = PluginRegistryService(SimpleNamespace(get_all_stars=lambda: [metadata]))
    result = await registry.query("暂传")
    assert result["success"] is True
    capabilities = result["data"][0]["capabilities"]
    assert capabilities["llm_tools"] == []
    assert len(capabilities["commands"]) == 8
    assert "/暂传空间" in capabilities["commands"]
    assert all(command.startswith("/暂传") for command in capabilities["commands"])
    plugin.provider.request.assert_not_called()


@pytest.mark.asyncio
async def test_help_only_advertises_explicit_zanchuan_commands(plugin):
    reply = [text async for text in plugin.help_command(Event())][0]
    assert "/暂传授权 5G 60" in reply
    assert "/暂传空间 [页码]" in reply and "10 GiB" in reply
    assert "快传" not in reply
    assert "帮我" not in reply
    assert "create_file_transfer_grant" not in reply
    plugin.provider.request.assert_not_called()


def test_greedy_command_parameters_preserve_defaults_and_reject_extra_text():
    command = CommandFilter("暂传授权")
    assert command.validate_and_convert_params([], {"args": GreedyStr}) == {"args": ""}
    assert command.validate_and_convert_params(["5G", "60"], {"args": GreedyStr}) == {
        "args": "5G 60"
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["暂传", "快传"])
@pytest.mark.parametrize(
    "suffix,args,params",
    [
        ("帮助", "", {}),
        ("授权", "5G 60", {"args": "5G 60"}),
        ("列表", "", {}),
        ("文件", "2", {"args": "2"}),
        ("删除", "a" * 32, {"args": "a" * 32}),
        ("撤销", "b" * 32, {"args": "b" * 32}),
        ("状态", "", {}),
        ("空间", "2", {"args": "2"}),
    ],
)
async def test_only_zanchuan_commands_route_and_keep_owner_checks(
    plugin, prefix, suffix, args, params
):
    parsed = {}
    event = SimpleNamespace(
        is_at_or_wake_command=True,
        get_message_str=lambda: f"{prefix}{suffix} {args}",
        set_extra=parsed.__setitem__,
    )
    matches = [
        handler
        for handler in star_handlers_registry.get_handlers_by_module_name(
            FileTransferPlugin.__module__
        )
        for command in handler.event_filters
        if isinstance(command, CommandFilter) and command.filter(event, {})
    ]
    if prefix == "快传":
        assert matches == []
        assert parsed == {}
        plugin.provider.request.assert_not_called()
        return
    assert len(matches) == 1
    assert parsed["parsed_params"] == params
    assert f"/暂传{suffix}" in plugin.get_public_capabilities()["commands"]
    response = [
        text async for text in matches[0].handler(plugin, Event("other"), **params)
    ]
    assert "仅允许" in response[0]
    plugin.provider.request.assert_not_called()


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler,args",
    [("files_command", ""), ("delete_command", "a" * 32), ("space_command", "")],
)
@pytest.mark.parametrize(
    "sender,private,configured,enabled",
    [
        ("other", True, True, True),
        ("owner", False, True, True),
        ("owner", True, False, True),
        ("owner", True, True, False),
    ],
)
async def test_file_management_requires_enabled_owner_private_access(
    plugin, handler, args, sender, private, configured, enabled
):
    plugin.config["owner_qq_id"] = "owner" if configured else ""
    plugin.config["enabled"] = enabled
    response = [
        text async for text in getattr(plugin, handler)(Event(sender, private), args)
    ]
    assert response
    plugin.provider.request.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("args,page", [("", 1), ("2", 2), (" 3 ", 3)])
async def test_file_command_requests_exact_page_and_reports_no_files(
    plugin, args, page
):
    plugin.provider.request.return_value = {
        "files": [],
        "page": page,
        "page_size": 10,
        "total_count": 0,
        "total_pages": 0,
        "total_bytes": 0,
        "uploaded_bytes": 0,
    }
    assert [text async for text in plugin.files_command(Event(), args)] == [
        "当前没有暂传文件。"
    ]
    plugin.provider.request.assert_awaited_once_with("GET", f"/admin/files?page={page}")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args", ["0", "-1", "1.5", "1 2", "page=2", "2147483648", "９", "9" * 20]
)
async def test_invalid_file_page_never_calls_service(plugin, args):
    response = [text async for text in plugin.files_command(Event(), args)]
    assert "用法" in response[0]
    plugin.provider.request.assert_not_called()


@pytest.mark.asyncio
async def test_delete_command_reports_confirmed_file_identity(plugin):
    plugin.provider.request.return_value = {
        "ok": True,
        "file": {"id": "a" * 32, "batch_id": "b" * 32, "name": "example.txt"},
    }
    response = [text async for text in plugin.delete_command(Event(), "a" * 32)]
    assert "已删除文件：example.txt" in response[0]
    assert "a" * 32 in response[0]
    plugin.provider.request.assert_awaited_once_with(
        "DELETE", "/admin/files/" + "a" * 32, {}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    ["", "example.txt", "../metadata.sqlite", "a" * 16, "a" * 32 + " other", "A" * 32],
)
async def test_delete_command_requires_full_file_id(plugin, args):
    response = [text async for text in plugin.delete_command(Event(), args)]
    assert "完整文件ID" in response[0]
    plugin.provider.request.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler,args",
    [("files_command", ""), ("delete_command", "a" * 32), ("space_command", "")],
)
async def test_file_management_reports_real_errors_without_retry(plugin, handler, args):
    plugin.provider.request.side_effect = TransferProviderError(
        "文件不存在或已被清理。"
    )
    assert [text async for text in getattr(plugin, handler)(Event(), args)] == [
        "文件不存在或已被清理。"
    ]
    assert plugin.provider.request.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        {"ok": False},
        {"ok": True},
        {"ok": True, "file": {"id": "b" * 32}},
        {"file": {"id": "a" * 32}},
    ],
)
async def test_delete_command_does_not_claim_unconfirmed_success(plugin, response):
    plugin.provider.request.return_value = response
    reply = [text async for text in plugin.delete_command(Event(), "a" * 32)]
    assert "未确认文件删除" in reply[0]
    assert "已删除文件" not in reply[0]


def test_file_formatter_keeps_ids_counts_progress_and_bounded_messages():
    files = [
        {
            "id": f"{number:032x}",
            "batch_id": "b" * 32,
            "name": "文件名" * 80,
            "size": 10_000_000_000,
            "offset": 5_000_000_000,
            "status": "partial",
            "deadline": 2_000_000_000,
        }
        for number in range(10)
    ]
    result = {
        "files": files,
        "page": 1,
        "page_size": 10,
        "total_pages": 2,
        "total_count": 11,
        "total_bytes": 100_000_000_000,
        "uploaded_bytes": 50_000_000_000,
    }
    reply = format_files(result)
    assert "共 11 个" in reply and "下一页：/暂传文件 2" in reply
    assert "部分上传" in reply and "5GB/10GB" in reply
    assert all(file["id"] in reply for file in files)
    assert len(reply) < 2000
    assert "没有文件" in format_files({**result, "page": 3, "files": []})


@pytest.mark.asyncio
@pytest.mark.parametrize("args,page", [("", 1), ("2", 2), (" 3 ", 3)])
async def test_space_command_requests_page_and_shows_empty_global_quota(
    plugin, args, page
):
    plugin.provider.request.return_value = {
        "limit_bytes": 10 * 1024**3,
        "allocated_bytes": 0,
        "available_bytes": 10 * 1024**3,
        "total_count": 0,
        "batches": [],
        "page": page,
        "page_size": 10,
        "total_pages": 0,
    }
    reply = [text async for text in plugin.space_command(Event(), args)][0]
    assert "总上限：10 GiB" in reply and "已分配：0B" in reply
    assert "剩余额度：10 GiB" in reply and "当前没有暂传会话" in reply
    plugin.provider.request.assert_awaited_once_with("GET", f"/admin/space?page={page}")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args", ["0", "-1", "1.5", "1 2", "page=2", "2147483648", "９", "9" * 20]
)
async def test_space_command_rejects_invalid_page_without_service_access(plugin, args):
    reply = [text async for text in plugin.space_command(Event(), args)]
    assert "用法：/暂传空间" in reply[0]
    plugin.provider.request.assert_not_called()


@pytest.mark.asyncio
async def test_grant_reports_global_quota_exhaustion_without_retry_or_fake_key(plugin):
    message = "暂传总空间限额为 10 GiB，剩余额度不足，请撤销旧会话或等待到期清理。"
    plugin.provider.request.side_effect = TransferProviderError(message)
    assert [text async for text in plugin.authorize(Event(), "2G")] == [message]
    plugin.provider.request.assert_awaited_once()


def test_space_formatter_distinguishes_allocated_and_received_bytes_and_paginates():
    batches = [
        {
            "id": f"{number:032x}",
            "state": "expired" if number == 0 else "uploading",
            "quota_bytes": 512 * 1024**2,
            "declared_bytes": 128 * 1024**2,
            "uploaded_bytes": 64 * 1024**2,
            "deadline": 2_000_000_000,
        }
        for number in range(10)
    ]
    result = {
        "limit_bytes": 10 * 1024**3,
        "allocated_bytes": 6 * 1024**3,
        "available_bytes": 4 * 1024**3,
        "total_count": 11,
        "page": 1,
        "page_size": 10,
        "total_pages": 2,
        "batches": batches,
    }
    reply = format_space(result)
    assert "总上限：10 GiB" in reply and "已分配：6 GiB" in reply
    assert "剩余额度：4 GiB" in reply and "共 11 个" in reply
    assert "分配 512 MiB" in reply and "文件预留 128 MiB · 已收 64 MiB" in reply
    assert "待清理（仍占额）" in reply and "下一页：/暂传空间 2" in reply
    assert all(batch["id"] in reply for batch in batches)
    assert len(reply) < 2000
    missing = format_space({**result, "page": 3, "batches": []})
    assert "已分配：6 GiB" in missing and "第 3 页没有会话" in missing
