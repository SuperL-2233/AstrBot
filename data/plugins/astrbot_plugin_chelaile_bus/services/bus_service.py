from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import httpx

from ..providers.chelaile_provider import ChelaileProvider, ChelaileUpstreamError
from ..utils.result import make_result

UPSTREAM_ERRORS = (httpx.HTTPError, ChelaileUpstreamError, ValueError, KeyError)


class BusService:
    """Orchestrates the search-line -> match-station -> realtime-bus query flow."""

    def __init__(
        self, config: dict[str, Any], provider: ChelaileProvider | None
    ) -> None:
        self.config = config
        self.provider = provider

    async def query_arrivals(
        self, line: str, station: str, city_id: str = ""
    ) -> dict[str, Any]:
        """Query realtime arrivals of a bus line at a given station.

        Args:
            line: Bus line number, e.g. "368".
            station: Exact station name, e.g. "苏州桥南".
            city_id: Optional chelaile city ID overriding the configured default.

        Returns:
            A result dict from make_result. On success data contains line,
            station, city_id and directions; each direction holds a caption
            and up to max_buses approaching buses with distance_meters,
            station_left, travel_time_seconds and expected_arrival_time.
        """
        bus_cfg = self.config["bus"]
        if not bus_cfg.get("enabled", True) or self.provider is None:
            return make_result(
                False, message="公交查询功能未启用。", error_code="BUS_DISABLED"
            )
        line = (line or "").strip()
        station = (station or "").strip()
        if not line or not station:
            return make_result(
                False,
                message="请提供公交线路号和站点名称。",
                error_code="BUS_INVALID_ARGUMENT",
            )
        city = (city_id or "").strip() or str(bus_cfg.get("city_id", "027"))
        max_buses = max(1, int(bus_cfg.get("max_buses", 2)))

        try:
            lines = await self.provider.search_lines(line, city)
        except UPSTREAM_ERRORS as exc:
            return make_result(
                False,
                message=f"公交线路查询失败：{exc}",
                error_code="BUS_UPSTREAM_ERROR",
            )
        if not lines:
            return make_result(
                False,
                message=f"未找到公交线路「{line}」。",
                error_code="BUS_LINE_NOT_FOUND",
            )

        directions = []
        station_choices = []
        errors = []
        for line_info in lines:
            try:
                stations = await self.provider.get_line_stations(
                    line_info["line_id"], city
                )
            except UPSTREAM_ERRORS as exc:
                errors.append(exc)
                continue
            station_choices.append(
                {
                    "caption": line_info["caption"],
                    "stations": [s["stop_name"] for s in stations],
                }
            )
            matched = next((s for s in stations if s["stop_name"] == station), None)
            if matched is None:
                continue
            try:
                buses = await self.provider.get_realtime_buses(
                    line_info["line_id"],
                    matched["station_id"],
                    str(matched["stop_number"]),
                    matched["lat"],
                    matched["lng"],
                    city,
                )
            except UPSTREAM_ERRORS as exc:
                errors.append(exc)
                continue
            directions.append(
                {
                    "caption": line_info["caption"],
                    "buses": self._normalize_buses(
                        buses, matched["stop_number"], max_buses
                    ),
                }
            )

        if not directions:
            if station_choices and not errors:
                return make_result(
                    False,
                    data={"directions": station_choices},
                    message=(
                        f"线路「{line}」上没有找到站点「{station}」，"
                        "请检查站点名称是否与列表完全一致。"
                    ),
                    error_code="BUS_STATION_NOT_FOUND",
                )
            if errors:
                return make_result(
                    False,
                    message=f"公交实时数据查询失败：{errors[0]}",
                    error_code="BUS_UPSTREAM_ERROR",
                )
            return make_result(
                False,
                data={"directions": station_choices},
                message=f"线路「{line}」上没有找到站点「{station}」。",
                error_code="BUS_STATION_NOT_FOUND",
            )

        return make_result(
            True,
            data={
                "line": line,
                "station": station,
                "city_id": city,
                "directions": directions,
            },
            message="查询成功。",
        )

    @staticmethod
    def _normalize_buses(
        buses: list[dict[str, Any]], target_order: Any, max_buses: int
    ) -> list[dict[str, Any]]:
        approaching = [
            b
            for b in buses
            if isinstance(b.get("distanceToWaitStn"), (int, float))
            and b["distanceToWaitStn"] >= 0
        ]
        approaching.sort(key=lambda b: b.get("order", 0), reverse=True)
        normalized = []
        for index, bus in enumerate(approaching[:max_buses]):
            travels = (bus.get("travels") or [{}])[0]
            travel_time = travels.get("travelTime")
            normalized.append(
                {
                    "label": "最近一辆车" if index == 0 else f"第{index + 1}辆车",
                    "distance_meters": bus["distanceToWaitStn"],
                    "station_left": int(target_order) - int(bus.get("order", 0)),
                    "travel_time_seconds": travel_time,
                    "expected_arrival_time": _expected_arrival_text(
                        travel_time, travels.get("arrivalTime")
                    ),
                }
            )
        return normalized


def _expected_arrival_text(travel_time: Any, arrival_time: Any) -> str:
    """Derive an HH:MM arrival text from upstream time fields.

    Args:
        travel_time: Seconds until arrival reported by upstream, if present.
        arrival_time: Upstream arrival timestamp fallback, in seconds or
            milliseconds since the epoch.

    Returns:
        An HH:MM string, or an empty string when no time is available.
    """
    if isinstance(travel_time, (int, float)):
        arrive_at = datetime.now() + timedelta(seconds=travel_time)
        return arrive_at.strftime("%H:%M")
    if isinstance(arrival_time, (int, float)) and arrival_time > 0:
        timestamp = arrival_time / 1000 if arrival_time > 1e12 else arrival_time
        return datetime.fromtimestamp(timestamp).strftime("%H:%M")
    return ""
