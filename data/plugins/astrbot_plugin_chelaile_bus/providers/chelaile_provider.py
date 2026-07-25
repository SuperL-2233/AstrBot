from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

BASE_URL = "https://web.chelaile.net.cn/api/bus/"
SIGN_SALT = "qwihrnbtmj"
AES_KEY = b"FF32AE65FBFD19414EAAFF6291A54B42"

DEFAULT_PARAMS: dict[str, str] = {
    "s": "h5",
    "wxs": "wx_app",
    "sign": "1",
    "h5RealData": "1",
    "v": "3.11.28",
    "src": "weixinapp_cx",
    "ctm_mp": "mp_wx",
    "vc": "2",
    "favoriteGray": "1",
    "gpstype": "wgs",
    "geo_type": "wgs",
    "scene": "1256",
}

REQUEST_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 "
        "MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI "
        "MiniProgramEnv/Windows WindowsWechat/WMPF WindowsWechat(0x63090a13) "
        "UnifiedPCWindowsWechat(0xf254160a) XWEB/18055"
    ),
    "xweb_xhr": "1",
    "Content-Type": "text",
    "Accept": "*/*",
    "Sec-Fetch-Site": "cross-site",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
    "Referer": "https://servicewechat.com/wx71d589ea01ce3321/814/page-frame.html",
    "Accept-Language": "zh-CN,zh;q=0.9",
}


class ChelaileUpstreamError(RuntimeError):
    """Raised when the chelaile upstream returns an error or malformed payload."""


def _crypto_sign(params: dict[str, Any]) -> str:
    """Compute the chelaile request signature.

    Args:
        params: All request params (defaults merged with endpoint params),
            in insertion order. The signature itself must not be included.

    Returns:
        The MD5 hex digest used as the cryptoSign param.
    """
    sign_str = "&".join(f'"{k}"="{v}"' for k, v in params.items()) + SIGN_SALT
    return hashlib.md5(sign_str.encode("utf-8")).hexdigest()


def _extract_json(raw: str) -> dict[str, Any]:
    """Extract the first balanced JSON object from an upstream response body.

    Args:
        raw: Raw response text, possibly wrapped in non-JSON junk.

    Returns:
        The parsed JSON object.

    Raises:
        ChelaileUpstreamError: If no balanced JSON object is found.
    """
    start = raw.find("{")
    if start < 0:
        raise ChelaileUpstreamError(f"upstream response is not JSON: {raw[:100]!r}")
    depth = 0
    for i in range(start, len(raw)):
        if raw[i] == "{":
            depth += 1
        elif raw[i] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(raw[start : i + 1])
    raise ChelaileUpstreamError("upstream response JSON is truncated")


def _decrypt_result(ciphertext: str) -> dict[str, Any]:
    """Decrypt an AES-128-ECB encrypted upstream payload.

    Args:
        ciphertext: Base64 encoded ciphertext from the encryptResult field.

    Returns:
        The decrypted and parsed JSON object.
    """
    decryptor = Cipher(algorithms.AES(AES_KEY), modes.ECB()).decryptor()
    plain = decryptor.update(base64.b64decode(ciphertext)) + decryptor.finalize()
    pad = plain[-1]
    return json.loads(plain[:-pad].decode("utf-8"))


class ChelaileProvider:
    """Async client for the chelaile realtime bus API."""

    def __init__(self, config: dict[str, Any]) -> None:
        bus_cfg = config["bus"]
        self.timeout_seconds = float(bus_cfg.get("request_timeout_seconds", 15))
        self._client: httpx.AsyncClient | None = None

    async def close(self) -> None:
        """Close the underlying HTTP client if it was created."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request(self, endpoint: str, extra: dict[str, Any]) -> dict[str, Any]:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers=REQUEST_HEADERS, timeout=self.timeout_seconds
            )
        params = dict(DEFAULT_PARAMS)
        params.update(extra)
        params["cryptoSign"] = _crypto_sign(params)
        response = await self._client.get(BASE_URL + endpoint, params=params)
        response.raise_for_status()
        envelope = _extract_json(response.text)
        jsonr = envelope.get("jsonr", {})
        if jsonr.get("status") not in (None, "00", "0000", "0"):
            raise ChelaileUpstreamError(
                f"upstream error: {json.dumps(jsonr, ensure_ascii=False)}"
            )
        data = jsonr.get("data") or {}
        if data.get("encryptResult"):
            return _decrypt_result(data["encryptResult"])
        return data

    async def search_lines(self, number: str, city_id: str) -> list[dict[str, Any]]:
        """Search bus lines by line number, one entry per direction.

        Args:
            number: Line number keyword, e.g. "368".
            city_id: Chelaile city ID.

        Returns:
            A list of dicts with line_id, name, start_station, end_station
            and caption, with subway lines filtered out.
        """
        data = await self._request(
            "query!nSearch.action",
            {
                "cityId": city_id,
                "localCityId": city_id,
                "key": number,
                "supportPhyStn": "true",
            },
        )
        lines = []
        for line in data.get("result", {}).get("lines", []):
            if line.get("subwayV2") == 1:
                continue
            name = line.get("name") or ""
            if number not in name:
                continue
            start_sn = line.get("startSn", "")
            end_sn = line.get("endSn", "")
            lines.append(
                {
                    "line_id": line.get("lineId"),
                    "name": name,
                    "start_station": start_sn,
                    "end_station": end_sn,
                    "caption": f"{name}({start_sn}-{end_sn})",
                }
            )
        return lines

    async def search_stations(
        self, keyword: str, city_id: str
    ) -> dict[str, list[dict[str, Any]]]:
        """Search stations and POIs by a free-text keyword.

        Args:
            keyword: Fuzzy station or place name, e.g. "翠微路口".
            city_id: Chelaile city ID.

        Returns:
            A dict with stations (station_id, name, lat, lng) and pois
            (name, address, tag, lat, lng), with subway entries filtered out.
        """
        data = await self._request(
            "query!nSearch.action",
            {
                "cityId": city_id,
                "localCityId": city_id,
                "key": keyword,
                "supportPhyStn": "true",
            },
        )
        result = data.get("result", {})
        stations = [
            {
                "station_id": s.get("sId"),
                "name": s.get("sn"),
                "lat": s.get("lat"),
                "lng": s.get("lng"),
            }
            for s in result.get("stations", [])
            if s.get("subwayV2") != 1
        ]
        pois = [
            {
                "name": poi.get("sn1") or poi.get("sn"),
                "address": poi.get("sn1Address") or poi.get("address"),
                "tag": poi.get("sn1Tag"),
                "lat": poi.get("lat"),
                "lng": poi.get("lng"),
            }
            for poi in result.get("pois", [])
        ]
        return {"stations": stations, "pois": pois}

    async def get_line_stations(
        self, line_id: str, city_id: str
    ) -> list[dict[str, Any]]:
        """Fetch all stations of a line direction in stop order.

        Args:
            line_id: Chelaile line ID from search_lines.
            city_id: Chelaile city ID.

        Returns:
            A list of dicts with station_id, stop_number, stop_name, lat, lng.
        """
        data = await self._request(
            "line!encryptedLineDetail.action",
            {
                "cityId": city_id,
                "localCityId": city_id,
                "lineId": line_id,
                "lat": "",
                "lng": "",
                "geo_lat": "",
                "geo_lng": "",
            },
        )
        return [
            {
                "station_id": s.get("sId"),
                "stop_number": s.get("order"),
                "stop_name": s.get("sn"),
                "lat": s.get("wgsLat"),
                "lng": s.get("wgsLng"),
            }
            for s in data.get("stations", [])
        ]

    async def get_realtime_buses(
        self,
        line_id: str,
        station_id: str,
        target_order: str,
        lat: Any,
        lng: Any,
        city_id: str,
    ) -> list[dict[str, Any]]:
        """Fetch realtime bus positions relative to a target station.

        Args:
            line_id: Chelaile line ID.
            station_id: Target station ID.
            target_order: Target station order number on the line.
            lat: Station WGS84 latitude.
            lng: Station WGS84 longitude.
            city_id: Chelaile city ID.

        Returns:
            The raw buses list from upstream.
        """
        lat_str = str(lat or "")
        lng_str = str(lng or "")
        target = str(target_order)
        data = await self._request(
            "line!encryptedBusDetail.action",
            {
                "cshow": "busDetail",
                "specail": "0",
                "specialType": "undefined",
                "cityId": city_id,
                "localCityId": city_id,
                "lineId": line_id,
                "targetOrder": target,
                "specialTargetOrder": target,
                "stationId": station_id,
                "lat": lat_str,
                "lng": lng_str,
                "geo_lat": lat_str,
                "geo_lng": lng_str,
                "userId": "",
                "h5Id": "",
                "unionId": "",
                "accountId": "",
                "secret": "",
            },
        )
        return data.get("buses", [])
