from __future__ import annotations

import inspect
from typing import Any


class PluginRegistryService:
    """Discover loaded plugins and consume the optional public-info contract."""

    def __init__(self, context) -> None:
        self.context = context

    @staticmethod
    async def _call_optional(instance: object, method_name: str) -> tuple[bool, Any]:
        method = getattr(instance, method_name, None)
        if not callable(method):
            return False, None
        result = method()
        if inspect.isawaitable(result):
            result = await result
        return True, result

    @staticmethod
    def _matches(metadata, query: str) -> bool:
        if not query:
            return True
        candidates = (
            metadata.name,
            metadata.display_name,
            metadata.root_dir_name,
        )
        normalized = query.casefold()
        return any(normalized in str(value).casefold() for value in candidates if value)

    async def query(self, plugin_name: str = "") -> dict:
        plugins = []
        for metadata in self.context.get_all_stars():
            if not self._matches(metadata, plugin_name.strip()):
                continue
            instance = metadata.star_cls if metadata.activated else None
            capabilities_supported = False
            status_supported = False
            capabilities = None
            status = None
            errors: list[str] = []
            if instance is not None:
                try:
                    capabilities_supported, capabilities = await self._call_optional(
                        instance, "get_public_capabilities"
                    )
                except Exception as exc:
                    errors.append(f"能力读取失败：{exc}")
                try:
                    status_supported, status = await self._call_optional(
                        instance, "get_public_status"
                    )
                except Exception as exc:
                    errors.append(f"状态读取失败：{exc}")
            if not capabilities_supported:
                capabilities = {
                    "summary": metadata.desc
                    or metadata.short_desc
                    or "未提供功能说明。",
                    "features": [],
                    "commands": [],
                    "llm_tools": [],
                }
            plugins.append(
                {
                    "name": metadata.name,
                    "display_name": metadata.display_name or metadata.name,
                    "version": metadata.version,
                    "author": metadata.author,
                    "activated": bool(metadata.activated),
                    "reserved": bool(metadata.reserved),
                    "capabilities_supported": capabilities_supported,
                    "capabilities": capabilities,
                    "status_supported": status_supported,
                    "status": status,
                    "errors": errors,
                }
            )
        if plugin_name.strip() and not plugins:
            return {
                "success": False,
                "data": [],
                "message": f"没有找到名称包含“{plugin_name.strip()}”的插件。",
                "error_code": "PLUGIN_NOT_FOUND",
            }
        return {
            "success": True,
            "data": plugins,
            "message": (
                f"已返回 {len(plugins)} 个已注册插件；详细状态取决于插件是否实现公开状态协议。"
            ),
            "error_code": None,
        }


def format_registry(result: dict) -> str:
    if not result["success"]:
        return result["message"]
    lines = [result["message"]]
    for plugin in result["data"]:
        state = "已启用" if plugin["activated"] else "未启用"
        detail = "详细状态可用" if plugin["status_supported"] else "未提供详细状态"
        summary = plugin["capabilities"].get("summary", "未提供功能说明。")
        lines.append(
            f"- {plugin['display_name']} ({plugin['name']} {plugin['version']})："
            f"{state}，{detail}\n  {summary}"
        )
    return "\n".join(lines)
