from __future__ import annotations

from ..utils.result import make_result


class ClassroomService:
    def __init__(self, config: dict, provider) -> None:
        self.config = config
        self.provider = provider

    async def get_empty_classrooms_now(self, building: str = "") -> dict:
        classroom_cfg = self.config["classroom"]
        if not classroom_cfg.get("enabled", True) or classroom_cfg.get("provider") == "disabled":
            return make_result(
                False,
                data=[],
                message="空教室数据源尚未配置，当前无法查询真实空教室。",
                error_code="CLASSROOM_SOURCE_NOT_CONFIGURED",
                source_status={"enabled": False, "provider": "disabled", "last_update_time": None},
            )
        if self.provider is None:
            return self._unsupported_provider_result()
        return await self.provider.get_current_empty_classrooms(building=building)

    async def get_empty_classrooms_by_time(
        self,
        building: str = "",
        start_time: str = "",
        end_time: str = "",
    ) -> dict:
        classroom_cfg = self.config["classroom"]
        if not classroom_cfg.get("enabled", True) or classroom_cfg.get("provider") == "disabled":
            return make_result(
                False,
                data=[],
                message="空教室数据源尚未配置，当前无法查询真实空教室。",
                error_code="CLASSROOM_SOURCE_NOT_CONFIGURED",
                source_status={"enabled": False, "provider": "disabled", "last_update_time": None},
            )
        if self.provider is None:
            return self._unsupported_provider_result()
        return await self.provider.get_empty_classrooms_by_time(
            building=building,
            start_time=start_time,
            end_time=end_time,
        )

    async def refresh(self) -> dict:
        classroom_cfg = self.config["classroom"]
        if not classroom_cfg.get("enabled", True) or classroom_cfg.get("provider") == "disabled":
            return make_result(
                False,
                data=None,
                message="空教室数据源尚未配置，当前无法刷新。",
                error_code="CLASSROOM_SOURCE_NOT_CONFIGURED",
                source_status={"enabled": False, "provider": "disabled", "last_update_time": None},
            )
        if self.provider is None:
            return self._unsupported_provider_result()
        return await self.provider.refresh()

    async def get_status(self) -> dict:
        classroom_cfg = self.config["classroom"]
        if not classroom_cfg.get("enabled", True) or classroom_cfg.get("provider") == "disabled":
            return {
                "enabled": False,
                "provider": "disabled",
                "last_update_time": None,
                "count": 0,
            }
        if self.provider is None:
            return {
                "enabled": False,
                "provider": str(classroom_cfg.get("provider") or "unknown"),
                "last_update_time": None,
                "count": 0,
                "message": "空教室 provider 配置不受支持。",
            }
        return await self.provider.get_status()

    def _unsupported_provider_result(self) -> dict:
        provider_name = str(self.config["classroom"].get("provider") or "unknown")
        return make_result(
            False,
            data=[],
            message=f"空教室 provider“{provider_name}”未实现，请改用 mportal、cache 或 disabled。",
            error_code="CLASSROOM_PROVIDER_UNSUPPORTED",
            source_status={
                "enabled": False,
                "provider": provider_name,
                "last_update_time": None,
                "count": 0,
            },
        )
