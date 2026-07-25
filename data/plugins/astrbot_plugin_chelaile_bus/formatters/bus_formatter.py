from __future__ import annotations

from typing import Any


def _format_distance(meters: Any) -> str:
    if not isinstance(meters, (int, float)):
        return "未知距离"
    if meters < 1000:
        return f"{int(meters)} 米"
    return f"{meters / 1000:.1f} 公里"


def _format_travel_time(seconds: Any) -> str:
    if not isinstance(seconds, (int, float)) or seconds < 0:
        return ""
    if seconds < 60:
        return "即将到站"
    return f"约 {round(seconds / 60)} 分钟后到站"


def format_bus_arrivals(result: dict[str, Any]) -> str:
    """Render a query_arrivals result dict as chat text.

    Args:
        result: The result contract returned by BusService.query_arrivals.

    Returns:
        A plain text message for chat platforms.
    """
    if not result["success"]:
        text = result["message"]
        choices = (result.get("data") or {}).get("directions") or []
        if choices and result.get("error_code") == "BUS_STATION_NOT_FOUND":
            hints = []
            for choice in choices:
                hints.append(f"{choice['caption']}：{'、'.join(choice['stations'])}")
            text = f"{text}\n可选站点：\n" + "\n".join(hints)
        return text

    data = result["data"]
    lines = [f"{data['station']} 站实时到站"]
    for direction in data["directions"]:
        lines.append(direction["caption"])
        buses = direction["buses"]
        if not buses:
            lines.append("  暂无车辆到站信息")
            continue
        for bus in buses:
            parts = [
                f"距站 {_format_distance(bus['distance_meters'])}",
                f"还有 {bus['station_left']} 站",
            ]
            travel_text = _format_travel_time(bus.get("travel_time_seconds"))
            if travel_text:
                parts.append(travel_text)
            if bus.get("expected_arrival_time"):
                parts.append(f"预计 {bus['expected_arrival_time']} 到达")
            lines.append(f"  {bus['label']}：" + "，".join(parts))
    return "\n".join(lines)
