from __future__ import annotations

from datetime import date

from ..providers.baidu_weather_provider import BaiduWeatherProvider
from ..utils.result import make_result
from ..utils.time_utils import date_range, parse_date, parse_hhmm, today_local
from .state_service import StateService

MAX_FORECAST_DAYS = 7
WEEK_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _place_key(place: dict) -> tuple:
    return (
        place.get("name", ""),
        place.get("lng", ""),
        place.get("lat", ""),
        place.get("district", ""),
    )


class WeatherService:
    """Business behavior: weather queries with per-day location splicing,
    plus location and push-time mutation orchestration."""

    def __init__(
        self,
        config: dict,
        provider: BaiduWeatherProvider,
        state: StateService,
    ) -> None:
        self.config = config
        self.provider = provider
        self.state = state

    async def query_weather(self, location: str = "", days: int = 1) -> dict:
        """Query weather for the coming days.

        Resolves the effective location for each queried day (an active trip
        overrides the resident location on its dates), fetches each distinct
        place once, and splices the forecasts so every day's row matches where
        the user actually is that day.

        Args:
            location: Explicit place text; empty means the stored location.
            days: Number of days starting from today, clamped to 1..7.

        Returns:
            make_result dict; data is {"start_date", "days", "now", "alerts"}.
        """
        days, note = self._normalize_days(days)
        today = today_local(self.state.tz_name)
        queried_dates = date_range(today, days)

        if location.strip():
            resolved = await self.provider.resolve_place(location)
            if not resolved["success"]:
                return resolved
            day_places = [(day, resolved["data"]) for day in queried_dates]
        else:
            day_places = [
                (day, self.state.effective_location(day)) for day in queried_dates
            ]
            if all(place is None for _, place in day_places):
                return make_result(
                    False,
                    message="尚未设置位置。请告诉我「我现在在××」，或使用 /天气位置 设置。",
                    error_code="WEATHER_LOCATION_NOT_SET",
                )

        weather_by_place: dict[tuple, dict] = {}
        for _, place in day_places:
            if place is None:
                continue
            key = _place_key(place)
            if key not in weather_by_place:
                weather_by_place[key] = await self.provider.fetch_weather(place)

        rows = []
        first_error = ""
        for day, place in day_places:
            if place is None:
                rows.append(self._missing_row(day, "", "该日期没有已设置的位置"))
                continue
            weather = weather_by_place[_place_key(place)]
            if not weather["success"]:
                first_error = first_error or weather["message"]
                rows.append(self._missing_row(day, place["name"], weather["message"]))
                continue
            forecast = next(
                (
                    item
                    for item in weather["data"]["forecasts"]
                    if item["date"] == day.isoformat()
                ),
                None,
            )
            if forecast is None:
                rows.append(
                    self._missing_row(day, place["name"], "该日期超出可预报范围")
                )
            else:
                rows.append({**forecast, "location": place["name"]})

        if all("missing" in row for row in rows) and first_error:
            return make_result(
                False, message=first_error, error_code="BAIDU_WEATHER_API_ERROR"
            )

        now = None
        alerts: list[dict] = []
        hours: list[dict] = []
        first_place = day_places[0][1]
        if first_place is not None:
            today_weather = weather_by_place[_place_key(first_place)]
            if today_weather["success"]:
                now = today_weather["data"]["now"]
                alerts = today_weather["data"]["alerts"]
                hours = today_weather["data"]["hours"]

        return make_result(
            True,
            data={
                "start_date": today.isoformat(),
                "days": rows,
                "now": now,
                "alerts": alerts,
                "hours": hours,
            },
            message=note or "ok",
        )

    async def set_location(
        self, address: str, start_date: str = "", end_date: str = ""
    ) -> dict:
        """Set the resident location or a date-bounded trip.

        Args:
            address: Free-form place text, geocoded before storing.
            start_date: Trip start (YYYY-MM-DD); both dates empty sets resident.
            end_date: Trip end (YYYY-MM-DD).

        Returns:
            make_result dict describing what was stored.
        """
        address = address.strip()
        if not address:
            return make_result(False, message="地点不能为空。")
        start_date = start_date.strip()
        end_date = end_date.strip()
        if not start_date and not end_date:
            resolved = await self.provider.resolve_place(address)
            if not resolved["success"]:
                return resolved
            self.state.set_resident_location(resolved["data"])
            return make_result(
                True,
                data={"mode": "resident", "place": resolved["data"]},
                message=f"已将常驻位置设为「{address}」。",
            )
        window = self._validate_window(start_date, end_date)
        if isinstance(window, str):
            return make_result(False, message=window)
        start, end = window
        resolved = await self.provider.resolve_place(address)
        if not resolved["success"]:
            return resolved
        self.state.set_trip(resolved["data"], start, end)
        return make_result(
            True,
            data={
                "mode": "trip",
                "place": resolved["data"],
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
            },
            message=(
                f"已设置行程：{start.isoformat()} 至 {end.isoformat()} 在「{address}」。"
                "期间天气查询与推送将使用该位置，行程结束后自动回退到常驻位置。"
            ),
        )

    def set_push_time(
        self, time_text: str, start_date: str = "", end_date: str = ""
    ) -> dict:
        """Set the resident push time or a date-bounded override.

        Args:
            time_text: Push time as HH:MM.
            start_date: Override start (YYYY-MM-DD); both empty sets resident.
            end_date: Override end (YYYY-MM-DD).

        Returns:
            make_result dict describing what was stored.
        """
        hhmm = parse_hhmm(time_text)
        if hhmm is None:
            return make_result(False, message="时间格式应为 HH:MM，如 07:50。")
        start_date = start_date.strip()
        end_date = end_date.strip()
        if not start_date and not end_date:
            self.state.set_resident_push_time(hhmm)
            return make_result(
                True,
                data={"mode": "resident", "time": hhmm},
                message=f"已将每日天气推送时间设为 {hhmm}。",
            )
        window = self._validate_window(start_date, end_date)
        if isinstance(window, str):
            return make_result(False, message=window)
        start, end = window
        self.state.set_push_time_override(hhmm, start, end)
        return make_result(
            True,
            data={
                "mode": "override",
                "time": hhmm,
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
            },
            message=(
                f"已设置限期推送时间：{start.isoformat()} 至 {end.isoformat()} "
                f"每天 {hhmm} 推送，结束后自动回退到常驻推送时间。"
            ),
        )

    def _validate_window(
        self, start_date: str, end_date: str
    ) -> tuple[date, date] | str:
        """Validate a date window; returns (start, end) or an error message."""
        if not start_date or not end_date:
            return "需要同时提供开始日期和结束日期（YYYY-MM-DD）。"
        start = parse_date(start_date)
        end = parse_date(end_date)
        if start is None or end is None:
            return "日期格式应为 YYYY-MM-DD，如 2026-07-26。"
        if start > end:
            return "开始日期不能晚于结束日期。"
        if end < today_local(self.state.tz_name):
            return "结束日期早于今天，无需设置。"
        return start, end

    @staticmethod
    def _missing_row(day: date, location: str, reason: str) -> dict:
        return {
            "date": day.isoformat(),
            "week": WEEK_CN[day.weekday()],
            "location": location,
            "missing": reason,
        }

    @staticmethod
    def _normalize_days(days: int) -> tuple[int, str]:
        try:
            value = int(days)
        except (TypeError, ValueError):
            return 1, ""
        if value < 1:
            return 1, ""
        if value > MAX_FORECAST_DAYS:
            return MAX_FORECAST_DAYS, (
                f"百度天气接口最多提供未来 {MAX_FORECAST_DAYS} 天预报，已按 "
                f"{MAX_FORECAST_DAYS} 天返回。"
            )
        return value, ""
