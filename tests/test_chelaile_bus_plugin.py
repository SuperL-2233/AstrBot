from pathlib import Path
import base64
import json
import sys

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


PLUGIN_PARENT = Path(__file__).resolve().parents[1] / "data" / "plugins"
if str(PLUGIN_PARENT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_PARENT))

from astrbot_plugin_chelaile_bus.formatters.bus_formatter import format_bus_arrivals
from astrbot_plugin_chelaile_bus.providers.chelaile_provider import (
    AES_KEY,
    ChelaileUpstreamError,
    _crypto_sign,
    _decrypt_result,
    _extract_json,
)
from astrbot_plugin_chelaile_bus.services.bus_service import BusService
from astrbot_plugin_chelaile_bus.utils.config_loader import load_plugin_config


def test_crypto_sign_matches_reference_algorithm():
    params = {"cityId": "027", "key": "368"}

    assert _crypto_sign(params) == "6826c9238eae0aae6d5c6afa8216097a"


def test_extract_json_tolerates_surrounding_junk():
    payload = _extract_json('junk{"jsonr": {"status": "00"}}tail')

    assert payload == {"jsonr": {"status": "00"}}


def test_extract_json_rejects_non_json():
    with pytest.raises(ChelaileUpstreamError):
        _extract_json("not json at all")


def test_decrypt_result_round_trips_aes_ecb_payload():
    raw = json.dumps({"buses": [{"order": 3}]}, ensure_ascii=False).encode("utf-8")
    pad = 16 - len(raw) % 16
    padded = raw + bytes([pad]) * pad
    encryptor = Cipher(algorithms.AES(AES_KEY), modes.ECB()).encryptor()
    ciphertext = base64.b64encode(encryptor.update(padded) + encryptor.finalize())

    assert _decrypt_result(ciphertext.decode("ascii")) == {"buses": [{"order": 3}]}


class StubProvider:
    def __init__(self, lines=None, stations=None, buses=None, error=None):
        self.lines = lines or []
        self.stations = stations or {}
        self.buses = buses or {}
        self.error = error

    async def search_lines(self, number, city_id):
        if self.error:
            raise self.error
        return self.lines

    async def get_line_stations(self, line_id, city_id):
        return self.stations.get(line_id, [])

    async def get_realtime_buses(
        self, line_id, station_id, target_order, lat, lng, city_id
    ):
        return self.buses.get(line_id, [])


def _line(line_id="L1", name="368路"):
    return {
        "line_id": line_id,
        "name": name,
        "start_station": "起点",
        "end_station": "终点",
        "caption": f"{name}(起点-终点)",
    }


def _station(stop_number=10, name="苏州桥南"):
    return {
        "station_id": "S1",
        "stop_number": stop_number,
        "stop_name": name,
        "lat": 39.9,
        "lng": 116.3,
    }


@pytest.mark.asyncio
async def test_query_arrivals_returns_nearest_two_buses_per_direction():
    provider = StubProvider(
        lines=[_line()],
        stations={"L1": [_station()]},
        buses={
            "L1": [
                {
                    "order": 5,
                    "distanceToWaitStn": 3000,
                    "travels": [{"travelTime": 900}],
                },
                {
                    "order": 8,
                    "distanceToWaitStn": 800,
                    "travels": [{"travelTime": 240}],
                },
                {
                    "order": 7,
                    "distanceToWaitStn": 1500,
                    "travels": [{"travelTime": 420}],
                },
                {"order": 9, "distanceToWaitStn": -1},
            ]
        },
    )
    service = BusService(load_plugin_config(None), provider)

    result = await service.query_arrivals("368", "苏州桥南")

    assert result["success"] is True
    buses = result["data"]["directions"][0]["buses"]
    assert len(buses) == 2
    assert buses[0]["label"] == "最近一辆车"
    assert buses[0]["distance_meters"] == 800
    assert buses[0]["station_left"] == 2
    assert buses[0]["travel_time_seconds"] == 240
    assert buses[0]["expected_arrival_time"]
    assert buses[1]["label"] == "第2辆车"
    assert buses[1]["distance_meters"] == 1500
    assert buses[1]["station_left"] == 3


@pytest.mark.asyncio
async def test_query_arrivals_reports_unknown_line():
    service = BusService(load_plugin_config(None), StubProvider())

    result = await service.query_arrivals("999", "苏州桥南")

    assert result["success"] is False
    assert result["error_code"] == "BUS_LINE_NOT_FOUND"


@pytest.mark.asyncio
async def test_query_arrivals_reports_unknown_station_with_choices():
    provider = StubProvider(
        lines=[_line()],
        stations={"L1": [_station(name="人民大学"), _station()]},
    )
    service = BusService(load_plugin_config(None), provider)

    result = await service.query_arrivals("368", "不存在的站")

    assert result["success"] is False
    assert result["error_code"] == "BUS_STATION_NOT_FOUND"
    stations = result["data"]["directions"][0]["stations"]
    assert stations == ["人民大学", "苏州桥南"]


@pytest.mark.asyncio
async def test_query_arrivals_disabled_returns_clear_message():
    config = load_plugin_config({"bus": {"enabled": False}})
    service = BusService(config, None)

    result = await service.query_arrivals("368", "苏州桥南")

    assert result["success"] is False
    assert result["error_code"] == "BUS_DISABLED"


def test_formatter_renders_two_buses():
    result = {
        "success": True,
        "message": "查询成功。",
        "data": {
            "line": "368",
            "station": "苏州桥南",
            "city_id": "027",
            "directions": [
                {
                    "caption": "368路(起点-终点)",
                    "buses": [
                        {
                            "label": "最近一辆车",
                            "distance_meters": 800,
                            "station_left": 2,
                            "travel_time_seconds": 240,
                            "expected_arrival_time": "14:32",
                        },
                        {
                            "label": "第2辆车",
                            "distance_meters": 1500,
                            "station_left": 3,
                            "travel_time_seconds": 660,
                            "expected_arrival_time": "14:39",
                        },
                    ],
                }
            ],
        },
    }

    text = format_bus_arrivals(result)

    assert "苏州桥南 站实时到站" in text
    assert (
        "最近一辆车：距站 800 米，还有 2 站，约 4 分钟后到站，预计 14:32 到达" in text
    )
    assert (
        "第2辆车：距站 1.5 公里，还有 3 站，约 11 分钟后到站，预计 14:39 到达" in text
    )
