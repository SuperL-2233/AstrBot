from __future__ import annotations

import asyncio
import json
from urllib.parse import urlsplit

import aiohttp


class TransferProviderError(Exception):
    """Report a bounded, user-facing management API failure."""


class TransferProvider:
    """Call the separate service without handling file bytes in AstrBot.

    Args:
        admin_url: Private service origin, without a public route prefix.
        admin_token: Shared management credential.
    """

    def __init__(self, admin_url: str, admin_token: str) -> None:
        self.admin_url = admin_url.rstrip("/")
        self.admin_token = admin_token
        self.session: aiohttp.ClientSession | None = None
        self.last_check_success = False

    async def close(self) -> None:
        """Close pooled management connections on plugin unload."""
        if self.session is not None:
            await self.session.close()

    async def request(
        self, method: str, path: str, payload: dict | None = None
    ) -> dict:
        """Make a bounded administrative request without retries or secret logs.

        Args:
            method: HTTP method.
            path: Internal, plugin-selected administrative path.
            payload: Optional small JSON request body.

        Returns:
            The service's JSON object.

        Raises:
            TransferProviderError: Configuration, connection, or response is invalid.
        """
        origin = urlsplit(self.admin_url)
        if (
            origin.scheme not in {"http", "https"}
            or not origin.netloc
            or origin.username
            or origin.password
            or origin.path
            or origin.query
            or origin.fragment
            or len(self.admin_token.strip()) < 32
        ):
            raise TransferProviderError(
                "请先配置有效的 admin_url 和至少 32 字符的 admin_token。"
            )
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10, connect=3),
                connector=aiohttp.TCPConnector(limit=2),
                trust_env=False,
            )
        try:
            async with self.session.request(
                method,
                self.admin_url + path,
                json=payload,
                headers={"Authorization": f"Bearer {self.admin_token}"},
                allow_redirects=False,
            ) as response:
                raw = bytearray()
                async for chunk in response.content.iter_chunked(16 * 1024):
                    raw.extend(chunk)
                    if len(raw) > 128 * 1024:
                        raise TransferProviderError("暂传服务返回的数据过大。")
                try:
                    result = json.loads(raw)
                except (ValueError, UnicodeError) as exc:
                    raise TransferProviderError(
                        "暂传服务响应异常，请检查管理地址。"
                    ) from exc
                if not isinstance(result, dict):
                    raise TransferProviderError("暂传服务响应格式无效。")
                if not 200 <= response.status < 300:
                    message = result.get("message")
                    raise TransferProviderError(
                        message[:200]
                        if isinstance(message, str)
                        else "暂传管理请求失败。"
                    )
                self.last_check_success = True
                return result
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            self.last_check_success = False
            raise TransferProviderError(
                "无法连接暂传服务，请检查服务状态和内部网络。"
            ) from exc
        except TransferProviderError:
            self.last_check_success = False
            raise
