from datetime import date, timedelta
from pathlib import Path
import sys

import pytest


PLUGIN_PARENT = Path(__file__).resolve().parents[1] / "data" / "plugins"
if str(PLUGIN_PARENT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_PARENT))

from astrbot_plugin_baidu_weather.formatters.weather_formatter import (
    format_weather_report,
)
from astrbot_plugin_baidu_weather.providers.baidu_weather_provider import (
    BaiduWeatherProvider,
)
from astrbot_plugin_baidu_weather.repositories.settings_repo import (
    SettingsRepository,
)
from astrbot_plugin_baidu_weather.services.state_service import StateService
from astrbot_plugin_baidu_weather.services.weather_service import WeatherService
from astrbot_plugin_baidu_weather.utils.config_loader import load_plugin_config
from astrbot_plugin_baidu_weather.utils.result import make_result

FAKE_TODAY = date(2026, 7, 25)

PLACE_WUDAOKE = {"name": "五道口", "lng": "116.33", "lat": "39.99", "district": ""}
PLACE_HANGZHOU = {"name": "杭州", "lng": "120.15", "lat": "30.28", "district": ""}


class _StubProvider:
    """Test double returning deterministic forecasts per place."""

    def __init__(self):
        self.fetch_calls: list[str] = []

    async def resolve_place(self, address: str) -> dict:
        return make_result(
            True,
            data={"name": address, "lng": "116.30", "lat": "39.98", "district": ""},
        )

    async def fetch_weather(self, place: dict) -> dict:
        self.fetch_calls.append(place["name"])
        forecasts = [
            {
                "date": (FAKE_TODAY + timedelta(days=offset)).isoformat(),
                "week": "周五",
                "high": 33,
                "low": 24,
                "text_day": f"{place['name']}晴",
                "text_night": "多云",
                "wd_day": "北风",
                "wd_night": "南风",
                "wc_day": "<3级",
                "wc_night": "微风",
            }
            for offset in range(7)
        ]
        return make_result(
            True,
            data={
                "now": {
                    "temp": 30,
                    "feels_like": 32,
                    "rh": 55,
                    "wind_dir": "北风",
                    "wind_class": "<3级",
                    "text": "晴",
                    "uptime": "2026-07-25 08:00",
                },
                "forecasts": forecasts,
                "alerts": [],
                "hours": [
                    {
                        "time": "2026-07-25 14:00",
                        "text": "晴",
                        "temp": 32,
                        "wind_dir": "北风",
                        "wind_class": "<3级",
                        "rh": 50,
                        "prec_1h": 0.0,
                    }
                ],
            },
        )


def _make_state(tmp_path) -> StateService:
    return StateService(
        load_plugin_config(None), SettingsRepository(tmp_path / "weather.sqlite")
    )


def _patch_today(monkeypatch):
    monkeypatch.setattr(
        "astrbot_plugin_baidu_weather.services.weather_service.today_local",
        lambda _tz: FAKE_TODAY,
    )
    monkeypatch.setattr(
        "astrbot_plugin_baidu_weather.services.state_service.today_local",
        lambda _tz: FAKE_TODAY,
    )


def test_settings_repo_round_trip(tmp_path):
    repo = SettingsRepository(tmp_path / "weather.sqlite")
    assert repo.get_setting("target_umo") == ""
    repo.set_setting("target_umo", "aiocqhttp:FriendMessage:123")
    assert repo.get_setting("target_umo") == "aiocqhttp:FriendMessage:123"
    repo.clear_setting("target_umo")
    assert repo.get_setting("target_umo") == ""


def test_state_service_effective_location_window(tmp_path):
    state = _make_state(tmp_path)
    state.set_resident_location(PLACE_WUDAOKE)
    state.set_trip(PLACE_HANGZHOU, date(2026, 7, 26), date(2026, 7, 30))

    assert state.effective_location(date(2026, 7, 25))["name"] == "五道口"
    assert state.effective_location(date(2026, 7, 26))["name"] == "杭州"
    assert state.effective_location(date(2026, 7, 30))["name"] == "杭州"
    assert state.effective_location(date(2026, 7, 31))["name"] == "五道口"


def test_state_service_expired_trip_dropped(tmp_path):
    state = _make_state(tmp_path)
    state.set_trip(PLACE_HANGZHOU, date(2020, 1, 1), date(2020, 1, 2))

    assert state.get_trip() is None
    assert state.effective_location(date(2020, 1, 1)) is None


def test_state_service_push_time_override(tmp_path):
    state = _make_state(tmp_path)
    assert state.get_resident_push_time() == "07:50"
    state.set_resident_push_time("08:00")
    state.set_push_time_override("06:30", date(2026, 7, 26), date(2026, 7, 30))

    assert state.effective_push_time(date(2026, 7, 25)) == "08:00"
    assert state.effective_push_time(date(2026, 7, 28)) == "06:30"
    assert state.effective_push_time(date(2026, 8, 1)) == "08:00"


def test_provider_parse_geocode_payloads():
    ok_payload = {
        "status": 0,
        "result": {"location": {"lng": 116.3, "lat": 39.9}, "level": "兴趣点"},
    }
    location, error = BaiduWeatherProvider._parse_geocode(ok_payload)
    assert location == {"lng": 116.3, "lat": 39.9}
    assert error == ""

    location, error = BaiduWeatherProvider._parse_geocode(
        {"status": 2, "message": "Parameter Invalid"}
    )
    assert location is None
    assert "Parameter Invalid" in error


def test_provider_parse_weather_payloads():
    ok_payload = {
        "status": 0,
        "result": {
            "now": {
                "temp": 30,
                "feels_like": 999999,
                "rh": 55,
                "wind_dir": "北风",
                "wind_class": "<3级",
                "text": "晴",
                "uptime": "2026-07-25 08:00",
            },
            "forecasts": [
                {
                    "date": "2026-07-25",
                    "week": "周五",
                    "high": 33,
                    "low": 24,
                    "text_day": "晴",
                    "text_night": "多云",
                    "wd_day": "北风",
                    "wd_night": "南风",
                    "wc_day": "<3级",
                    "wc_night": "微风",
                }
            ],
            "alerts": [{"type": "高温", "level": "黄色", "title": "高温黄色预警"}],
            "forecast_hours": [
                {
                    "data_time": "2026-07-25 14:00",
                    "text": "晴",
                    "temp_fc": 32,
                    "wind_dir": "北风",
                    "wind_class": "<3级",
                    "rh": 50,
                    "prec_1h": 999999,
                }
            ],
        },
    }
    data, error = BaiduWeatherProvider._parse_weather(ok_payload)
    assert error == ""
    assert data["now"]["temp"] == 30
    assert data["now"]["feels_like"] is None
    assert data["forecasts"][0]["text_day"] == "晴"
    assert data["alerts"][0]["title"] == "高温黄色预警"
    assert data["hours"][0]["time"] == "2026-07-25 14:00"
    assert data["hours"][0]["temp"] == 32
    assert data["hours"][0]["prec_1h"] is None

    data, error = BaiduWeatherProvider._parse_weather(
        {"status": 401, "message": "AK 不合法"}
    )
    assert data is None
    assert "AK 不合法" in error


@pytest.mark.asyncio
async def test_provider_missing_ak_returns_disabled_result():
    provider = BaiduWeatherProvider(load_plugin_config(None))

    result = await provider.resolve_place("五道口")

    assert result["success"] is False
    assert result["error_code"] == "BAIDU_AK_NOT_CONFIGURED"


@pytest.mark.asyncio
async def test_resolve_place_surfaces_baidu_api_error(monkeypatch):
    provider = BaiduWeatherProvider(load_plugin_config({"baidu": {"ak": "x" * 32}}))

    async def fake_get_json(url, params):
        return {"status": 210, "message": "APP IP校验失败"}

    monkeypatch.setattr(provider, "_get_json", fake_get_json)

    result = await provider.resolve_place("北京")

    assert result["success"] is False
    assert result["error_code"] == "BAIDU_GEOCODE_API_ERROR"
    assert "APP IP校验失败" in result["message"]


@pytest.mark.asyncio
async def test_resolve_place_district_fallback(monkeypatch):
    provider = BaiduWeatherProvider(load_plugin_config({"baidu": {"ak": "x" * 32}}))

    async def fake_get_json(url, params):
        if "geocoding" in url:
            return {"status": 0, "result": {}}
        return {"status": 0, "result": {"now": {}, "forecasts": [], "alerts": []}}

    monkeypatch.setattr(provider, "_get_json", fake_get_json)

    result = await provider.resolve_place("海淀区")

    assert result["success"] is True
    assert result["data"]["district"] == "海淀区"


@pytest.mark.asyncio
async def test_weather_service_splices_trip_boundary(tmp_path, monkeypatch):
    _patch_today(monkeypatch)
    state = _make_state(tmp_path)
    state.set_resident_location(PLACE_WUDAOKE)
    state.set_trip(PLACE_HANGZHOU, date(2026, 7, 26), date(2026, 7, 30))
    provider = _StubProvider()
    service = WeatherService(load_plugin_config(None), provider, state)

    result = await service.query_weather("", 7)

    assert result["success"] is True
    rows = result["data"]["days"]
    assert len(rows) == 7
    assert rows[0]["location"] == "五道口"
    assert [row["location"] for row in rows[1:6]] == ["杭州"] * 5
    assert rows[6]["location"] == "五道口"
    assert provider.fetch_calls == ["五道口", "杭州"]
    assert result["data"]["now"]["temp"] == 30
    assert result["data"]["hours"][0]["temp"] == 32


@pytest.mark.asyncio
async def test_weather_service_without_location(tmp_path):
    state = _make_state(tmp_path)
    service = WeatherService(load_plugin_config(None), _StubProvider(), state)

    result = await service.query_weather("", 1)

    assert result["success"] is False
    assert result["error_code"] == "WEATHER_LOCATION_NOT_SET"


@pytest.mark.asyncio
async def test_weather_service_explicit_location_ignores_state(tmp_path, monkeypatch):
    _patch_today(monkeypatch)
    state = _make_state(tmp_path)
    provider = _StubProvider()
    service = WeatherService(load_plugin_config(None), provider, state)

    result = await service.query_weather("西安", 2)

    assert result["success"] is True
    assert [row["location"] for row in result["data"]["days"]] == ["西安", "西安"]


def test_weather_service_clamps_days():
    days, note = WeatherService._normalize_days(10)
    assert days == 7
    assert "7 天" in note
    assert WeatherService._normalize_days(0) == (1, "")


@pytest.mark.asyncio
async def test_set_location_window_validation(tmp_path, monkeypatch):
    _patch_today(monkeypatch)
    state = _make_state(tmp_path)
    service = WeatherService(load_plugin_config(None), _StubProvider(), state)

    result = await service.set_location("杭州", "2026-07-30", "2026-07-26")
    assert result["success"] is False
    assert "开始日期" in result["message"]

    result = await service.set_location("杭州", "2026-07-26", "")
    assert result["success"] is False
    assert "同时提供" in result["message"]

    result = await service.set_location("杭州", "2020-01-01", "2020-01-02")
    assert result["success"] is False
    assert "早于今天" in result["message"]


@pytest.mark.asyncio
async def test_set_location_trip_and_resident(tmp_path, monkeypatch):
    _patch_today(monkeypatch)
    state = _make_state(tmp_path)
    service = WeatherService(load_plugin_config(None), _StubProvider(), state)

    resident = await service.set_location("五道口")
    assert resident["success"] is True
    assert state.get_resident_location()["name"] == "五道口"

    trip = await service.set_location("杭州", "2026-07-26", "2026-07-30")
    assert trip["success"] is True
    assert state.get_trip()["name"] == "杭州"


def test_set_push_time_validation_and_storage(tmp_path, monkeypatch):
    _patch_today(monkeypatch)
    state = _make_state(tmp_path)
    service = WeatherService(load_plugin_config(None), _StubProvider(), state)

    assert service.set_push_time("25:00")["success"] is False

    resident = service.set_push_time("8:00")
    assert resident["success"] is True
    assert state.get_resident_push_time() == "08:00"

    override = service.set_push_time("06:30", "2026-07-26", "2026-07-30")
    assert override["success"] is True
    assert state.get_push_time_override()["time"] == "06:30"


def test_format_weather_report_renders_sections():
    data = {
        "days": [
            {
                "date": "2026-07-25",
                "week": "周五",
                "location": "五道口",
                "text_day": "晴",
                "text_night": "多云",
                "high": 33,
                "low": 24,
                "wd_day": "北风",
                "wc_day": "<3级",
                "wd_night": "南风",
                "wc_night": "微风",
            }
        ],
        "now": {
            "temp": 30,
            "feels_like": 32,
            "rh": 55,
            "wind_dir": "北风",
            "wind_class": "<3级",
            "text": "晴",
        },
        "hours": [
            {
                "time": "2026-07-25 14:00",
                "text": "晴",
                "temp": 32,
                "wind_dir": "北风",
                "wind_class": "<3级",
                "rh": 50,
                "prec_1h": 0.0,
            },
            {
                "time": "2026-07-25 15:00",
                "text": "多云",
                "temp": 31,
                "wind_dir": "北风",
                "wind_class": "<3级",
                "rh": 52,
                "prec_1h": 0.0,
            },
        ],
        "alerts": [{"type": "高温", "level": "黄色", "title": "高温黄色预警"}],
    }

    text = format_weather_report(data, greeting="早上好！今日天气：")

    assert "早上好" in text
    assert "7月25日 周五 晴转多云 24~33℃" in text
    assert "白天：晴，北风<3级" in text
    assert "夜间：多云，南风微风" in text
    assert "实时：晴 30℃（体感 32℃，湿度 55%，北风<3级）" in text
    assert "逐小时（每3小时间隔）：14时晴32℃" in text
    assert "气象预警：高温黄色预警" in text
