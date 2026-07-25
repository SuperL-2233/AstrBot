from __future__ import annotations

from typing import Any

import httpx

from ..utils.result import make_result

GEOCODE_URL = "https://api.map.baidu.com/geocoding/v3/"
WEATHER_URL = "https://api.map.baidu.com/weather/v1/"


def _clean_number(value: Any) -> Any:
    """Normalize Baidu numeric fields; 999999 is Baidu's missing-value sentinel."""
    if value == 999999:
        return None
    return value


class BaiduWeatherProvider:
    """Baidu Maps geocoding and weather API client.

    Place records are dicts: {"name", "lng", "lat", "district"}. Records carry
    either coordinates (preferred) or a district name for the fallback query.
    """

    def __init__(self, config: dict) -> None:
        baidu_cfg = config["baidu"]
        self.ak = str(baidu_cfg.get("ak", "")).strip()
        self.timeout_seconds = int(baidu_cfg.get("timeout_seconds", 10))

    def _ak_missing_result(self) -> dict:
        return make_result(
            False,
            message="尚未配置百度地图开放平台 AK，请在插件配置 baidu.ak 中填写后重试。",
            error_code="BAIDU_AK_NOT_CONFIGURED",
        )

    async def _get_json(self, url: str, params: dict[str, str]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response.json()

    async def resolve_place(self, address: str) -> dict:
        """Resolve a free-form address into a place record.

        Tries geocoding first (handles POIs like campus or street names); on
        failure falls back to the weather API's district-name query so plain
        district names still work.

        Args:
            address: Free-form place text supplied by the user.

        Returns:
            make_result dict whose data is {"name", "lng", "lat", "district"}.
        """
        if not self.ak:
            return self._ak_missing_result()
        address = address.strip()
        if not address:
            return make_result(
                False, message="地点不能为空。", error_code="BAIDU_EMPTY_ADDRESS"
            )
        try:
            payload = await self._get_json(
                GEOCODE_URL, {"address": address, "output": "json", "ak": self.ak}
            )
        except httpx.HTTPError as exc:
            return make_result(
                False,
                message=f"百度地理编码请求失败：{exc}",
                error_code="BAIDU_GEOCODE_REQUEST_FAILED",
            )
        location, error_message = self._parse_geocode(payload)
        if location:
            return make_result(
                True,
                data={
                    "name": address,
                    "lng": str(location["lng"]),
                    "lat": str(location["lat"]),
                    "district": "",
                },
                message=f"已解析地点「{address}」。",
            )
        if payload.get("status") != 0:
            # API-level failure (bad AK, IP whitelist, quota) — surface it verbatim.
            return make_result(
                False,
                message=f"百度地理编码接口返回错误：{error_message}",
                error_code="BAIDU_GEOCODE_API_ERROR",
            )
        # Geocoding succeeded but found no coordinates: district-name fallback.
        probe = await self._fetch_weather_payload({"district": address})
        if probe["success"]:
            return make_result(
                True,
                data={"name": address, "lng": "", "lat": "", "district": address},
                message=f"已按区县名称解析地点「{address}」。",
            )
        return make_result(
            False,
            message=f"无法解析地点「{address}」，请换用更具体的名称（如带上城市名）。",
            error_code="BAIDU_LOCATION_NOT_FOUND",
        )

    async def fetch_weather(self, place: dict) -> dict:
        """Fetch realtime weather, 7-day forecast and alerts for a place record.

        Args:
            place: Place record returned by resolve_place (or loaded from storage).

        Returns:
            make_result dict whose data is {"now", "forecasts", "alerts"}.
        """
        if not self.ak:
            return self._ak_missing_result()
        params: dict[str, str] = {}
        if place.get("lng") and place.get("lat"):
            params["location"] = f"{place['lng']},{place['lat']}"
            params["coordtype"] = "bd09ll"
        elif place.get("district"):
            params["district"] = str(place["district"])
        else:
            return make_result(
                False,
                message="位置记录无效，请重新设置地点。",
                error_code="BAIDU_INVALID_PLACE",
            )
        return await self._fetch_weather_payload(params)

    async def _fetch_weather_payload(self, extra_params: dict[str, str]) -> dict:
        params = {"ak": self.ak, "data_type": "all", "output": "json", **extra_params}
        try:
            payload = await self._get_json(WEATHER_URL, params)
        except httpx.HTTPError as exc:
            return make_result(
                False,
                message=f"百度天气请求失败：{exc}",
                error_code="BAIDU_WEATHER_REQUEST_FAILED",
            )
        data, error_message = self._parse_weather(payload)
        if data is None:
            return make_result(
                False,
                message=f"百度天气接口返回错误：{error_message}",
                error_code="BAIDU_WEATHER_API_ERROR",
            )
        return make_result(True, data=data, message="ok")

    @staticmethod
    def _parse_geocode(payload: dict[str, Any]) -> tuple[dict | None, str]:
        """Split a geocoding response into (location, error_message)."""
        if payload.get("status") == 0:
            location = (payload.get("result") or {}).get("location") or {}
            if location.get("lng") is not None and location.get("lat") is not None:
                return location, ""
            return None, "地理编码结果缺少坐标"
        message = payload.get("message") or payload.get("msg") or "未知错误"
        return None, str(message)

    @staticmethod
    def _parse_weather(payload: dict[str, Any]) -> tuple[dict | None, str]:
        """Split a weather response into (normalized data, error_message)."""
        if payload.get("status") != 0:
            message = payload.get("message") or payload.get("msg") or "未知错误"
            return None, str(message)
        result = payload.get("result") or {}
        now_raw = result.get("now") or {}
        now = {
            "temp": _clean_number(now_raw.get("temp")),
            "feels_like": _clean_number(now_raw.get("feels_like")),
            "rh": _clean_number(now_raw.get("rh")),
            "wind_dir": now_raw.get("wind_dir", ""),
            "wind_class": now_raw.get("wind_class", ""),
            "text": now_raw.get("text", ""),
            "uptime": now_raw.get("uptime", ""),
        }
        forecasts = [
            {
                "date": item.get("date", ""),
                "week": item.get("week", ""),
                "high": _clean_number(item.get("high")),
                "low": _clean_number(item.get("low")),
                "text_day": item.get("text_day", ""),
                "text_night": item.get("text_night", ""),
                "wd_day": item.get("wd_day", ""),
                "wd_night": item.get("wd_night", ""),
                "wc_day": item.get("wc_day", ""),
                "wc_night": item.get("wc_night", ""),
            }
            for item in result.get("forecasts") or []
        ]
        alerts = [
            {
                "type": item.get("type", ""),
                "level": item.get("level", ""),
                "title": item.get("title", ""),
            }
            for item in result.get("alerts") or []
        ]
        hours = [
            {
                "time": item.get("data_time", ""),
                "text": item.get("text", ""),
                "temp": _clean_number(item.get("temp_fc")),
                "wind_dir": item.get("wind_dir", ""),
                "wind_class": item.get("wind_class", ""),
                "rh": _clean_number(item.get("rh")),
                "prec_1h": _clean_number(item.get("prec_1h")),
            }
            for item in result.get("forecast_hours") or []
        ]
        return {
            "now": now,
            "forecasts": forecasts,
            "alerts": alerts,
            "hours": hours,
        }, ""
