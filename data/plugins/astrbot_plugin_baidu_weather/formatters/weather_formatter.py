from __future__ import annotations

from datetime import date


def _fmt_date(iso_date: str) -> str:
    """Render an ISO date as '7月25日'; falls back to the raw string."""
    try:
        parsed = date.fromisoformat(iso_date)
    except ValueError:
        return iso_date
    return f"{parsed.month}月{parsed.day}日"


def _fmt_place(place: dict | None) -> str:
    if not place:
        return "未设置"
    return str(place.get("name", "未设置"))


def _temp_range(row: dict) -> str:
    high = row.get("high")
    low = row.get("low")
    if high is None or low is None:
        return "气温未知"
    return f"{low}~{high}℃"


def format_weather_report(data: dict, greeting: str = "") -> str:
    """Render query/push weather data as plain text.

    Args:
        data: WeatherService.query_weather result data.
        greeting: Optional leading line (used by the daily push).

    Returns:
        Multi-line plain text ready for a chat message.
    """
    lines: list[str] = []
    if greeting:
        lines.append(greeting)
    days = data.get("days") or []
    distinct_locations = {row.get("location", "") for row in days}
    for row in days:
        date_label = f"{_fmt_date(row.get('date', ''))} {row.get('week', '')}".strip()
        location_suffix = (
            f"（{row['location']}）"
            if len(distinct_locations) > 1 and row.get("location")
            else ""
        )
        if "missing" in row:
            lines.append(f"{date_label}{location_suffix}：{row['missing']}")
            continue
        text_day = row.get("text_day", "")
        text_night = row.get("text_night", "")
        summary = (
            f"{text_day}转{text_night}"
            if text_day and text_night and text_day != text_night
            else text_day or text_night
        )
        lines.append(f"{date_label}{location_suffix} {summary} {_temp_range(row)}")
        lines.append(
            f"  白天：{text_day}，{row.get('wd_day', '')}{row.get('wc_day', '')}"
        )
        lines.append(
            f"  夜间：{text_night}，{row.get('wd_night', '')}{row.get('wc_night', '')}"
        )
    now = data.get("now")
    if now:
        parts = [part for part in [now.get("text", ""), _fmt_now_temp(now)] if part]
        extras = []
        if now.get("feels_like") is not None:
            extras.append(f"体感 {now['feels_like']}℃")
        if now.get("rh") is not None:
            extras.append(f"湿度 {now['rh']}%")
        wind = f"{now.get('wind_dir', '')}{now.get('wind_class', '')}".strip()
        if wind:
            extras.append(wind)
        detail = " ".join(parts)
        if extras:
            detail = f"{detail}（{'，'.join(extras)}）" if detail else "，".join(extras)
        if detail:
            lines.append(f"实时：{detail}")
    hours = data.get("hours") or []
    if hours:
        compact = []
        for entry in hours[:24:3]:
            hour_digits = entry.get("time", "")[11:13]
            label = (
                f"{int(hour_digits)}时"
                if hour_digits.isdigit()
                else entry.get("time", "")
            )
            temp = entry.get("temp")
            temp_text = f"{temp}℃" if temp is not None else ""
            compact.append(f"{label}{entry.get('text', '')}{temp_text}")
        if compact:
            lines.append("逐小时（每3小时间隔）：" + "，".join(compact))
    alerts = data.get("alerts") or []
    if alerts:
        titles = "；".join(a.get("title", "") for a in alerts if a.get("title"))
        if titles:
            lines.append(f"气象预警：{titles}")
    return "\n".join(lines)


def _fmt_now_temp(now: dict) -> str:
    return f"{now['temp']}℃" if now.get("temp") is not None else ""


def format_location_status(status: dict) -> str:
    """Render the location section of /天气位置."""
    lines = ["当前位置状态"]
    lines.append(f"常驻位置：{_fmt_place(status.get('resident_location'))}")
    trip = status.get("trip")
    if trip:
        window = f"{_fmt_date(trip['start_date'])} 至 {_fmt_date(trip['end_date'])}"
        state = "生效中" if status.get("trip_active_today") else "未开始"
        lines.append(f"行程：{window} 在「{trip.get('name', '')}」（{state}）")
    else:
        lines.append("行程：无")
    effective = status.get("effective_location")
    if effective:
        source = "行程" if status.get("trip_active_today") else "常驻位置"
        lines.append(f"今日生效位置：{_fmt_place(effective)}（{source}）")
    else:
        lines.append("今日生效位置：未设置")
    return "\n".join(lines)


def format_push_time_status(status: dict) -> str:
    """Render the push time section of /天气推送时间."""
    lines = ["推送时间状态"]
    lines.append(f"常驻推送时间：{status.get('resident_push_time', '')}")
    override = status.get("push_time_override")
    if override:
        window = (
            f"{_fmt_date(override['start_date'])} 至 {_fmt_date(override['end_date'])}"
        )
        lines.append(f"限期推送时间：{override.get('time', '')}（{window}）")
    else:
        lines.append("限期推送时间：无")
    lines.append(f"今日生效时间：{status.get('effective_push_time', '')}")
    lines.append(f"推送开关：{'开' if status.get('push_enabled') else '关'}")
    lines.append(f"推送目标：{'已绑定' if status.get('target_umo_set') else '未绑定'}")
    return "\n".join(lines)
