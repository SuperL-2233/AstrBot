from __future__ import annotations

from datetime import date, datetime, time
from typing import Any

import httpx

from ..repositories.classroom_repo import ClassroomRepository
from ..utils.result import make_result
from ..utils.time_utils import format_datetime, now_local, parse_datetime


class ClassroomMportalProvider:
    def __init__(self, repo: ClassroomRepository, config: dict) -> None:
        self.repo = repo
        self.config = config
        classroom_cfg = config["classroom"]
        self.base_url = classroom_cfg.get("mportal_base_url", "https://mportal.cugb.edu.cn").rstrip("/")
        self.campus_id = str(classroom_cfg.get("mportal_campus_id", "1"))
        self.page_size = int(classroom_cfg.get("mportal_page_size", 50))
        self.max_pages = int(classroom_cfg.get("mportal_max_pages", 10))
        self.timeout_seconds = float(classroom_cfg.get("mportal_timeout_seconds", 10))
        self.cache_expire_minutes = int(classroom_cfg.get("cache_expire_minutes", 120))
        self.refresh_buildings = [
            str(item).strip()
            for item in classroom_cfg.get("mportal_refresh_buildings", [])
            if str(item).strip()
        ]

    async def refresh(self) -> dict:
        target_date = now_local().date()
        try:
            count = await self._refresh_cache(target_date)
        except httpx.HTTPError as exc:
            return make_result(
                False,
                data=None,
                message=f"空教室门户刷新失败：{exc}",
                error_code="CLASSROOM_MPORTAL_REFRESH_FAILED",
                source_status=await self.get_status(),
            )
        return make_result(
            True,
            data={"count": count},
            message="空教室门户数据已刷新。",
            source_status=await self.get_status(),
        )

    async def get_current_empty_classrooms(self, building: str = "") -> dict:
        now_value = now_local().replace(second=0, microsecond=0)
        return await self._query(
            building=building,
            target_date=now_value.date(),
            start_time=now_value.time(),
            end_time=now_value.time(),
        )

    async def get_empty_classrooms_by_time(
        self,
        building: str = "",
        start_time: str = "",
        end_time: str = "",
    ) -> dict:
        start_dt = parse_datetime(start_time) if start_time else now_local()
        end_dt = parse_datetime(end_time) if end_time else start_dt
        return await self._query(
            building=building,
            target_date=start_dt.date(),
            start_time=start_dt.time(),
            end_time=end_dt.time(),
        )

    async def get_status(self) -> dict:
        return {
            "enabled": True,
            "provider": "mportal",
            "last_update_time": self.repo.last_update_time(),
            "count": self.repo.count(),
        }

    async def _query(
        self,
        building: str,
        target_date: date,
        start_time: time,
        end_time: time,
    ) -> dict:
        try:
            metadata = await self._get_cached_metadata(target_date)
            building_name = self._resolve_building_name(building, metadata.get("buildings", []))
            section_ids = self._resolve_section_ids(metadata.get("sections", []), start_time, end_time)
            if self._should_refresh(target_date):
                await self._refresh_cache(target_date, metadata=metadata)
            cached_rooms = self.repo.query_cache(target_date.isoformat(), building=building_name)
            rows = self._normalize_cached_rooms(
                cached_rooms,
                start_dt=datetime.combine(target_date, start_time),
                end_dt=datetime.combine(target_date, end_time),
                required_sections=section_ids,
            )
        except httpx.HTTPError as exc:
            return make_result(
                False,
                data=[],
                message=f"空教室门户请求失败：{exc}",
                error_code="CLASSROOM_MPORTAL_REQUEST_FAILED",
                source_status=await self.get_status(),
            )
        except ValueError as exc:
            return make_result(
                False,
                data=[],
                message=str(exc),
                error_code="CLASSROOM_MPORTAL_QUERY_INVALID",
                source_status=await self.get_status(),
            )

        return make_result(
            True,
            data=rows,
            message="已返回空教室缓存数据。",
            source_status=await self.get_status(),
        )

    def _should_refresh(self, target_date: date) -> bool:
        return (
            not self.repo.has_cache(target_date.isoformat())
            or self.repo.is_cache_expired(self.cache_expire_minutes)
        )

    async def _get_cached_metadata(self, target_date: date) -> dict[str, Any]:
        metadata = self.repo.get_metadata("mportal_index")
        if metadata and not self._should_refresh(target_date):
            return metadata
        metadata = await self._fetch_index()
        self.repo.set_metadata(
            "mportal_index",
            metadata,
            format_datetime(now_local().replace(second=0, microsecond=0)),
        )
        return metadata

    async def _refresh_cache(self, target_date: date, metadata: dict[str, Any] | None = None) -> int:
        metadata = metadata or await self._fetch_index()
        update_time = format_datetime(now_local().replace(second=0, microsecond=0))
        self.repo.set_metadata("mportal_index", metadata, update_time)
        building_names = self._refresh_building_names(metadata.get("buildings", []))
        raw_rooms: list[dict[str, Any]] = []
        for building_name in building_names:
            raw_rooms.extend(
                await self._fetch_search(
                    building=building_name,
                    target_date=target_date.isoformat(),
                    jc="",
                )
            )
        cache_rows = [row for row in (self._to_cache_row(item) for item in raw_rooms) if row["building"] and row["room"]]
        return self.repo.replace_cache(target_date.isoformat(), cache_rows, update_time)

    async def _fetch_index(self) -> dict[str, Any]:
        payload = await self._get_json(
            "/freeclasscugb/wap/default/index",
            params={"xqid": self.campus_id},
        )
        data = payload.get("d", payload)
        return {
            "campuses": data.get("xq") or [],
            "buildings": data.get("jxl") or [],
            "sections": data.get("jieci") or [],
        }

    def _refresh_building_names(self, buildings: list[dict[str, Any]]) -> list[str]:
        configured = [
            self._resolve_building_name(building, buildings)
            for building in self.refresh_buildings
        ]
        if configured:
            return configured
        return [
            str(item.get("jxlm") or item.get("name") or item.get("id") or "").strip()
            for item in buildings
            if str(item.get("jxlm") or item.get("name") or item.get("id") or "").strip()
        ]

    async def _fetch_search(self, building: str, target_date: str, jc: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for page in range(1, self.max_pages + 1):
            payload = await self._post_json(
                "/freeclasscugb/wap/default/search",
                data={
                    "xq": self.campus_id,
                    "lh": building,
                    "date": target_date,
                    "jc": jc,
                    "pagesize": str(self.page_size),
                    "page": str(page),
                },
            )
            data = payload.get("d", payload)
            page_rows = data.get("list") or []
            if not page_rows:
                break
            rows.extend(page_rows)
            if len(page_rows) < self.page_size:
                break
        return rows

    async def _get_json(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        async with httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout_seconds,
            headers=self._headers(),
        ) as client:
            response = await client.get(path, params=params)
            response.raise_for_status()
            return response.json()

    async def _post_json(self, path: str, data: dict[str, str]) -> dict[str, Any]:
        async with httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout_seconds,
            headers=self._headers(),
        ) as client:
            response = await client.post(path, data=data)
            response.raise_for_status()
            return response.json()

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self.base_url}/site/freeClassroomNew/index",
            "User-Agent": "Mozilla/5.0 AstrBot CUGB Assistant",
        }

    @staticmethod
    def _resolve_building_name(building: str, buildings: list[dict[str, Any]]) -> str:
        keyword = building.strip()
        if not keyword:
            return ""
        names = [str(item.get("jxlm") or item.get("name") or item.get("text") or "").strip() for item in buildings]
        if keyword in names:
            return keyword
        aliases = {
            "教五": "教5楼",
            "教5": "教5楼",
            "教学五楼": "教5楼",
            "学十六": "学16楼",
            "学16": "学16楼",
        }
        alias = aliases.get(keyword)
        if alias:
            return alias
        matches = [name for name in names if keyword and keyword in name]
        if len(matches) == 1:
            return matches[0]
        return keyword

    @classmethod
    def _resolve_section_ids(
        cls,
        sections: Any,
        start_time: time,
        end_time: time,
    ) -> list[str]:
        normalized = [
            item
            for item in (cls._normalize_section(item) for item in cls._iter_section_items(sections))
            if item
        ]
        if not normalized:
            return []
        matched = [
            item["id"]
            for item in normalized
            if cls._time_ranges_overlap(start_time, end_time, item["start"], item["end"])
        ]
        return matched

    @staticmethod
    def _iter_section_items(sections: Any) -> list[dict[str, Any]]:
        if isinstance(sections, list):
            return [item for item in sections if isinstance(item, dict)]
        if not isinstance(sections, dict):
            return []

        time_map = sections.get("time")
        if isinstance(time_map, dict):
            return [
                {"jc": section_id, "time": section_time}
                for section_id, section_time in time_map.items()
            ]

        items: list[dict[str, Any]] = []
        for value in sections.values():
            if isinstance(value, dict):
                items.extend(ClassroomMportalProvider._iter_section_items(value))
            elif isinstance(value, list):
                items.extend(item for item in value if isinstance(item, dict))
        return items

    @staticmethod
    def _normalize_section(item: dict[str, Any]) -> dict[str, Any] | None:
        section_id = item.get("jc") or item.get("id") or item.get("value") or item.get("name")
        start_raw = item.get("start") or item.get("start_time") or item.get("kssj")
        end_raw = item.get("end") or item.get("end_time") or item.get("jssj")
        if not start_raw or not end_raw:
            label = str(item.get("time") or item.get("sjd") or item.get("text") or "")
            if "-" in label:
                start_raw, end_raw = [part.strip() for part in label.split("-", 1)]
        if section_id is None or not start_raw or not end_raw:
            return None
        return {
            "id": str(section_id),
            "start": datetime.strptime(str(start_raw)[:5], "%H:%M").time(),
            "end": datetime.strptime(str(end_raw)[:5], "%H:%M").time(),
        }

    @staticmethod
    def _time_ranges_overlap(
        start_a: time,
        end_a: time,
        start_b: time,
        end_b: time,
    ) -> bool:
        return start_a <= end_b and end_a >= start_b

    @classmethod
    def _normalize_rooms(
        cls,
        rooms: list[dict[str, Any]],
        start_dt: datetime,
        end_dt: datetime,
        required_sections: list[str],
    ) -> list[dict[str, Any]]:
        update_time = format_datetime(now_local().replace(second=0, microsecond=0))
        normalized = []
        for item in rooms:
            if not cls._is_free(item, required_sections):
                continue
            normalized.append(
                {
                    "building": str(item.get("jxlm") or "").strip(),
                    "room": str(item.get("jsm") or item.get("jsh") or "").strip(),
                    "start_time": format_datetime(start_dt),
                    "end_time": format_datetime(end_dt),
                    "capacity": cls._parse_capacity(item.get("jsrl")),
                    "source_update_time": update_time,
                    "source": "mportal",
                }
            )
        return [item for item in normalized if item["building"] and item["room"]]

    @classmethod
    def _normalize_cached_rooms(
        cls,
        rooms: list[dict[str, Any]],
        start_dt: datetime,
        end_dt: datetime,
        required_sections: list[str],
    ) -> list[dict[str, Any]]:
        normalized = []
        for item in rooms:
            raw_data = item.get("raw_data") or {}
            if not cls._is_free(raw_data, required_sections):
                continue
            normalized.append(
                {
                    "building": item["building"],
                    "room": item["room"],
                    "start_time": format_datetime(start_dt),
                    "end_time": format_datetime(end_dt),
                    "capacity": item.get("capacity"),
                    "source_update_time": item.get("source_update_time"),
                    "source": "cache",
                }
            )
        return normalized

    @classmethod
    def _to_cache_row(cls, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "building": str(item.get("jxlm") or "").strip(),
            "room": str(item.get("jsm") or item.get("jsh") or "").strip(),
            "capacity": cls._parse_capacity(item.get("jsrl")),
            "occupied_sections": sorted(cls._occupied_sections(item), key=lambda value: int(value) if value.isdigit() else value),
            "source": "mportal",
            "raw_data": item,
        }

    @classmethod
    def _is_free(cls, item: dict[str, Any], required_sections: list[str]) -> bool:
        occupied = cls._occupied_sections(item)
        if required_sections:
            return not set(required_sections).intersection(occupied) and cls._section_details_free(
                item,
                required_sections,
            )
        occupied_details = item.get("zyxx") or []
        if isinstance(occupied_details, list):
            return all(not str(detail.get("name") or "").strip() for detail in occupied_details if isinstance(detail, dict))
        if isinstance(occupied_details, dict):
            details = occupied_details.values()
            if required_sections:
                details = [
                    detail
                    for section_id, detail in occupied_details.items()
                    if str(section_id) in set(required_sections)
                ]
            return all(not str(detail.get("name") or "").strip() for detail in details if isinstance(detail, dict))
        return not occupied

    @staticmethod
    def _section_details_free(item: dict[str, Any], required_sections: list[str]) -> bool:
        occupied_details = item.get("zyxx") or {}
        if isinstance(occupied_details, dict):
            details = [
                detail
                for section_id, detail in occupied_details.items()
                if str(section_id) in set(required_sections)
            ]
            return all(not str(detail.get("name") or "").strip() for detail in details if isinstance(detail, dict))
        if isinstance(occupied_details, list):
            return all(not str(detail.get("name") or "").strip() for detail in occupied_details if isinstance(detail, dict))
        return True

    @staticmethod
    def _occupied_sections(item: dict[str, Any]) -> set[str]:
        raw = item.get("zyjc") or ""
        if isinstance(raw, list):
            return {str(value).strip() for value in raw if str(value).strip()}
        return {value.strip() for value in str(raw).split(",") if value.strip()}

    @staticmethod
    def _parse_capacity(value: Any) -> int | None:
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
