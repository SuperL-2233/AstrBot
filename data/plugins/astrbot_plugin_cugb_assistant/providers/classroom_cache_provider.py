from __future__ import annotations

from datetime import datetime

from ..repositories.classroom_repo import ClassroomRepository
from ..utils.result import make_result


class ClassroomCacheProvider:
    def __init__(self, repo: ClassroomRepository) -> None:
        self.repo = repo

    async def refresh(self) -> dict:
        return make_result(
            False,
            data=None,
            message="当前仅启用缓存 provider，尚未接入真实刷新逻辑。",
            error_code="CLASSROOM_REFRESH_NOT_IMPLEMENTED",
            source_status=await self.get_status(),
        )

    async def get_current_empty_classrooms(self, building: str = "") -> dict:
        now_value = datetime.now()
        rows = self._format_cached_rows(
            self.repo.query_cache(now_value.date().isoformat(), building=building),
            now_value.strftime("%Y-%m-%d %H:%M"),
            now_value.strftime("%Y-%m-%d %H:%M"),
        )
        if not rows:
            return make_result(
                False,
                data=[],
                message="空教室缓存为空，当前无法查询空教室。",
                error_code="CLASSROOM_CACHE_EMPTY",
                source_status=await self.get_status(),
            )
        return make_result(
            True,
            data=rows,
            message="已返回缓存中的空教室数据。",
            source_status=await self.get_status(),
        )

    async def get_empty_classrooms_by_time(
        self,
        building: str = "",
        start_time: str = "",
        end_time: str = "",
    ) -> dict:
        cache_date = start_time[:10] if start_time else datetime.now().date().isoformat()
        rows = self._format_cached_rows(
            self.repo.query_cache(cache_date, building=building),
            start_time,
            end_time,
        )
        if not rows:
            return make_result(
                False,
                data=[],
                message="空教室缓存为空，当前无法查询指定时段空教室。",
                error_code="CLASSROOM_CACHE_EMPTY",
                source_status=await self.get_status(),
            )
        return make_result(
            True,
            data=rows,
            message="已返回缓存中的空教室数据。",
            source_status=await self.get_status(),
        )

    async def get_status(self) -> dict:
        return {
            "enabled": True,
            "provider": "cache",
            "last_update_time": self.repo.last_update_time(),
            "count": self.repo.count(),
        }

    @staticmethod
    def _format_cached_rows(rows: list[dict], start_time: str, end_time: str) -> list[dict]:
        return [
            {
                "building": item["building"],
                "room": item["room"],
                "start_time": start_time,
                "end_time": end_time,
                "capacity": item.get("capacity"),
                "source_update_time": item.get("source_update_time"),
                "source": "cache",
            }
            for item in rows
        ]
