import sys
from pathlib import Path

import pytest

PLUGIN_PARENT = Path(__file__).resolve().parents[1] / "data" / "plugins"
if str(PLUGIN_PARENT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_PARENT))

from astrbot_plugin_capability_registry.services.registry_service import (  # noqa: E402
    PluginRegistryService,
)


class Metadata:
    def __init__(
        self,
        name: str,
        instance: object | None,
        *,
        activated: bool = True,
    ) -> None:
        self.name = name
        self.display_name = name
        self.root_dir_name = name
        self.version = "1.0.0"
        self.author = "test"
        self.activated = activated
        self.reserved = False
        self.star_cls = instance
        self.desc = f"{name} description"
        self.short_desc = None


@pytest.mark.asyncio
async def test_registry_reports_every_plugin_and_optional_contract():
    class DetailedPlugin:
        @staticmethod
        def get_public_capabilities() -> dict:
            return {
                "summary": "详细能力",
                "features": ["查询"],
                "commands": [],
                "llm_tools": [],
            }

        @staticmethod
        async def get_public_status() -> dict:
            return {"ready": True}

    class Context:
        @staticmethod
        def get_all_stars() -> list[Metadata]:
            return [
                Metadata("detailed", DetailedPlugin()),
                Metadata("metadata_only", object()),
                Metadata("disabled", None, activated=False),
            ]

    result = await PluginRegistryService(Context()).query()

    assert result["success"] is True
    assert len(result["data"]) == 3
    detailed = result["data"][0]
    assert detailed["capabilities_supported"] is True
    assert detailed["status_supported"] is True
    assert detailed["status"] == {"ready": True}
    metadata_only = result["data"][1]
    assert metadata_only["capabilities_supported"] is False
    assert metadata_only["status_supported"] is False
    assert "description" in metadata_only["capabilities"]["summary"]


@pytest.mark.asyncio
async def test_registry_filters_plugins_and_reports_missing_name():
    class Context:
        @staticmethod
        def get_all_stars() -> list[Metadata]:
            return [Metadata("astrbot_plugin_chaoxing", object())]

    service = PluginRegistryService(Context())

    assert len((await service.query("chaoxing"))["data"]) == 1
    missing = await service.query("missing")
    assert missing["success"] is False
    assert missing["error_code"] == "PLUGIN_NOT_FOUND"
