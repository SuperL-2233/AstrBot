import json
import re
from decimal import Decimal

from astrbot.api import llm_tool, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.star.filter.command import GreedyStr

from .formatters.transfer_formatter import format_batches, format_files, format_grant
from .providers.transfer_provider import TransferProvider, TransferProviderError


@star.register(
    "astrbot_plugin_file_transfer", "macroladlezilla", "私人文件快传", "0.2.0"
)
class FileTransferPlugin(star.Star):
    """Owner-only QQ administration for an independent transfer service."""

    def __init__(self, context: star.Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self.config = {
            "enabled": False,
            "owner_qq_id": "",
            "admin_url": "http://127.0.0.1:8788",
            "admin_token": "",
        }
        if config:
            self.config.update(dict(config))
        self.provider = TransferProvider(
            str(self.config["admin_url"]), str(self.config["admin_token"])
        )

    async def terminate(self) -> None:
        """Close management connections when AstrBot unloads this plugin."""
        await self.provider.close()

    def _access_error(self, event: AstrMessageEvent) -> str | None:
        """Require the explicitly configured owner in a private QQ session.

        Args:
            event: Command event authenticated by AstrBot's platform adapter.

        Returns:
            A denial message, or None if management is authorized.
        """
        owner = str(self.config.get("owner_qq_id", "")).strip()
        if not owner:
            return "快传管理已关闭，请先配置 owner_qq_id。"
        if not event.is_private_chat() or str(event.get_sender_id()) != owner:
            return "仅允许配置的所有者在私聊中管理文件快传。"
        if not self.config.get("enabled"):
            return "快传插件尚未启用，请先部署独立文件服务并完成配置。"
        return None

    def get_public_capabilities(self) -> dict:
        """Describe management capabilities without exposing credentials.

        Returns:
            The capability registry's public plugin contract.
        """
        return {
            "summary": "所有者私聊管理独立文件快传授权。",
            "features": ["上传授权签发", "批次查询与撤销", "存储文件查询与删除"],
            "commands": [
                "/快传帮助",
                "/快传授权",
                "/快传列表",
                "/快传文件",
                "/快传删除",
                "/快传撤销",
                "/快传状态",
            ],
            "llm_tools": ["create_file_transfer_grant"],
        }

    def get_public_status(self) -> dict:
        """Expose configuration readiness and the last real connection result.

        Returns:
            Non-sensitive readiness fields; no simulated online status.
        """
        return {
            "enabled": bool(self.config.get("enabled")),
            "owner_configured": bool(str(self.config.get("owner_qq_id", "")).strip()),
            "admin_token_configured": len(
                str(self.config.get("admin_token", "")).strip()
            )
            >= 32,
            "last_check_success": self.provider.last_check_success,
        }

    @filter.command("快传帮助")
    async def help_command(self, event: AstrMessageEvent):
        """Show command syntax to the configured owner.

        Args:
            event: Incoming command event.

        Yields:
            An owner-private help or permission response.
        """
        error = self._access_error(event)
        if error:
            yield event.plain_result(error)
            return
        yield event.plain_result(
            "私人文件快传\n/快传授权 [总量] [保留分钟]\n例如：/快传授权 5G 60\n也可直接说：帮我开一个 5G、保留 60 分钟的快传授权。\n默认整批 2GB，最多 10GB，文件保留 30 分钟。\n通行密钥 1 小时内兑换，兑换后可上传 30 分钟。\n/快传列表（批次）\n/快传文件 [页码]（每页 10 个）\n/快传删除 <完整文件ID>（仅删除该文件）\n/快传撤销 <完整批次ID>（清理整批）\n/快传状态"
        )

    @filter.command("快传授权")
    async def authorize(self, event: AstrMessageEvent, args: GreedyStr):
        """Issue a grant through the same authorization path used by the tool.

        Args:
            event: Owner-private command event.
            args: Optional size and retention minutes, captured as trailing text.

        Yields:
            A new upload grant or a bounded validation/service error.
        """
        result = await self._create_grant(event, str(args).split())
        yield event.plain_result(result["message"])

    @llm_tool("create_file_transfer_grant")
    async def tool_create_grant(
        self,
        event: AstrMessageEvent,
        total_size: str = "2G",
        retention_minutes: int = 30,
    ) -> str:
        """Create one 快传 upload grant per owner request in owner private chat. Each
        call creates a new key; call once. Relay entrance/key exactly. Redemption
        1h, upload window 30m; retention starts after finalization.

        Args:
            total_size(string): Batch quota: 2G (default), 500M or 5GB. Decimal
                units; max 10GB.
            retention_minutes(number): Positive whole retention minutes; default 30.

        Returns:
            JSON with success and the authorization or error message.
        """
        result = await self._create_grant(
            event, [str(total_size), str(retention_minutes)]
        )
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))

    async def _create_grant(self, event: AstrMessageEvent, parts: list[str]) -> dict:
        """Authorize and validate one grant request without retrying issuance.

        Args:
            event: AstrBot-authenticated sender and chat context, never model input.
            parts: Separate size and retention arguments, preserving tool boundaries.

        Returns:
            A success flag and the formatted grant or a bounded failure message.
        """
        error = self._access_error(event)
        if error:
            return {"success": False, "message": error}
        size = re.fullmatch(
            r"(\d+(?:\.\d+)?)(B|KB|MB|GB|K|M|G)?", parts[0].upper() if parts else "2G"
        )
        minutes = parts[1] if len(parts) > 1 else "30"
        if len(parts) > 2 or not size or not minutes.isascii() or not minutes.isdigit():
            return {
                "success": False,
                "message": "用法：/快传授权 [总量，例如 2G] [保留分钟，例如 30]",
            }
        units = {
            "B": 1,
            "K": 1000,
            "KB": 1000,
            "M": 1_000_000,
            "MB": 1_000_000,
            "G": 1_000_000_000,
            "GB": 1_000_000_000,
        }
        quota = Decimal(size[1]) * units[size[2] or "G"]
        retention = int(minutes) * 60
        if (
            quota != int(quota)
            or not 1 <= quota <= 10_000_000_000
            or not 1 <= retention <= 2**31 - 1
        ):
            return {
                "success": False,
                "message": "整批额度必须大于 0 且不超过 10GB，保留分钟必须为正整数。",
            }
        try:
            result = await self.provider.request(
                "POST",
                "/admin/grants",
                {"quota_bytes": int(quota), "retention_seconds": retention},
            )
            return {"success": True, "message": format_grant(result)}
        except TransferProviderError as exc:
            return {"success": False, "message": str(exc)}

    @filter.command("快传列表")
    async def list_command(self, event: AstrMessageEvent):
        """List recent batches for the configured owner.

        Args:
            event: Owner-private command event.

        Yields:
            A batch list or a permission/service error.
        """
        error = self._access_error(event)
        if error:
            yield event.plain_result(error)
            return
        try:
            yield event.plain_result(
                format_batches(await self.provider.request("GET", "/admin/batches"))
            )
        except TransferProviderError as exc:
            yield event.plain_result(str(exc))

    @filter.command("快传文件")
    async def files_command(self, event: AstrMessageEvent, args: GreedyStr):
        """List one page of stored files across all managed batches.

        Args:
            event: Owner-private command event.
            args: Optional one-based page number.

        Yields:
            File identities, states, deadlines, or a bounded error response.
        """
        error = self._access_error(event)
        if error:
            yield event.plain_result(error)
            return
        page_text = str(args).strip() or "1"
        if (
            not re.fullmatch(r"[0-9]{1,10}", page_text)
            or not 1 <= int(page_text) <= 2**31 - 1
        ):
            yield event.plain_result("用法：/快传文件 [正整数页码]，默认第 1 页。")
            return
        try:
            result = await self.provider.request(
                "GET", f"/admin/files?page={int(page_text)}"
            )
            yield event.plain_result(format_files(result))
        except TransferProviderError as exc:
            yield event.plain_result(str(exc))

    @filter.command("快传删除")
    async def delete_command(self, event: AstrMessageEvent, args: GreedyStr):
        """Delete exactly one file selected by its complete internal identifier.

        Args:
            event: Owner-private command event.
            args: Full file identifier from the file listing.

        Yields:
            Confirmed deletion details or a validation/service error.
        """
        error = self._access_error(event)
        if error:
            yield event.plain_result(error)
            return
        file_id = str(args).strip()
        if not re.fullmatch(r"[a-f0-9]{32}", file_id):
            yield event.plain_result("用法：/快传删除 <快传文件中的完整文件ID>")
            return
        try:
            result = await self.provider.request(
                "DELETE", f"/admin/files/{file_id}", {}
            )
            file = result.get("file")
            if (
                result.get("ok") is not True
                or not isinstance(file, dict)
                or file.get("id") != file_id
            ):
                raise TransferProviderError(
                    "快传服务未确认文件删除，请刷新文件列表检查。"
                )
            yield event.plain_result(
                f"已删除文件：{file.get('name', file_id)}\n文件 ID：{file_id}"
            )
        except TransferProviderError as exc:
            yield event.plain_result(str(exc))

    @filter.command("快传撤销")
    async def revoke(self, event: AstrMessageEvent, args: GreedyStr):
        """Revoke an entire batch and stop its active transfers.

        Args:
            event: Owner-private command event.
            args: Full opaque batch identifier.

        Yields:
            Revocation confirmation or a validation/service error.
        """
        error = self._access_error(event)
        if error:
            yield event.plain_result(error)
            return
        batch_id = str(args).strip()
        if not re.fullmatch(r"[a-f0-9]{32}", batch_id):
            yield event.plain_result("用法：/快传撤销 <快传列表中的完整批次ID>")
            return
        try:
            await self.provider.request("DELETE", f"/admin/batches/{batch_id}", {})
            yield event.plain_result(f"已撤销并清理快传批次：{batch_id}")
        except TransferProviderError as exc:
            yield event.plain_result(str(exc))

    @filter.command("快传状态")
    async def status_command(self, event: AstrMessageEvent):
        """Perform a real connectivity check against the private listener.

        Args:
            event: Owner-private command event.

        Yields:
            Current service connectivity or a permission/service error.
        """
        error = self._access_error(event)
        if error:
            yield event.plain_result(error)
            return
        try:
            result = await self.provider.request("GET", "/admin/health")
            yield event.plain_result(
                "快传服务连接正常。"
                if result.get("ok") is True
                else "快传服务尚未就绪。"
            )
        except TransferProviderError as exc:
            yield event.plain_result(str(exc))
