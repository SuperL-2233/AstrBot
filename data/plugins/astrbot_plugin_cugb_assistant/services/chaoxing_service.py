from __future__ import annotations

from ..utils.result import make_result


class ChaoxingService:
    def __init__(self, config: dict, provider) -> None:
        self.config = config
        self.provider = provider

    async def get_pending_homework(self, course_name: str = "") -> dict:
        chaoxing_cfg = self.config["chaoxing"]
        if not chaoxing_cfg.get("enable", False) or chaoxing_cfg.get("provider") == "disabled":
            return make_result(
                False,
                data=[],
                message="学习通功能尚未启用，目前无法读取作业信息。",
                error_code="CHAOXING_NOT_ENABLED",
                source_status=await self.get_status(),
            )
        if self.provider is None:
            return await self._unsupported_provider_result()
        return await self.provider.get_pending_homework(course_name=course_name)

    async def get_recent_deadlines(self, within_days: int = 7) -> dict:
        chaoxing_cfg = self.config["chaoxing"]
        if not chaoxing_cfg.get("enable", False) or chaoxing_cfg.get("provider") == "disabled":
            return make_result(
                False,
                data=[],
                message="学习通功能尚未启用，目前无法读取作业信息。",
                error_code="CHAOXING_NOT_ENABLED",
                source_status=await self.get_status(),
            )
        if self.provider is None:
            return await self._unsupported_provider_result()
        return await self.provider.get_recent_deadlines(within_days=within_days)

    async def refresh(self) -> dict:
        chaoxing_cfg = self.config["chaoxing"]
        if not chaoxing_cfg.get("enable", False) or chaoxing_cfg.get("provider") == "disabled":
            return make_result(
                False,
                data=None,
                message="学习通功能尚未启用，目前无法刷新作业缓存。",
                error_code="CHAOXING_NOT_ENABLED",
                source_status=await self.get_status(),
            )
        if self.provider is None:
            return await self._unsupported_provider_result()
        return await self.provider.refresh()

    async def get_status(self) -> dict:
        chaoxing_cfg = self.config["chaoxing"]
        if not chaoxing_cfg.get("enable", False) or chaoxing_cfg.get("provider") == "disabled":
            return {
                "enabled": False,
                "provider": "disabled",
                "last_update_time": None,
                "count": 0,
            }
        if self.provider is None:
            return {
                "enabled": False,
                "provider": str(chaoxing_cfg.get("provider") or "unknown"),
                "last_update_time": None,
                "count": 0,
                "message": "学习通 provider 配置不受支持。",
            }
        return await self.provider.get_status()

    async def _unsupported_provider_result(self) -> dict:
        provider_name = str(self.config["chaoxing"].get("provider") or "unknown")
        return make_result(
            False,
            data=[],
            message=f"学习通 provider“{provider_name}”未实现，请改用 cache 或 disabled。",
            error_code="CHAOXING_PROVIDER_UNSUPPORTED",
            source_status=await self.get_status(),
        )
