from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any

from aiohttp import web

from ..services.notification_service import NotificationRequestError


class WebhookProvider:
    """Small authenticated HTTP ingress owned by the plugin lifecycle."""

    def __init__(
        self,
        config: dict[str, Any],
        callback: Callable[[Any], Awaitable[dict[str, Any]]],
        notify_callback: Callable[[Any], Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        self.config = config
        self.callback = callback
        self.notify_callback = notify_callback
        self._runner: web.AppRunner | None = None
        self.started = False
        self.disabled_reason = ""

    async def start(self) -> None:
        if not self.config.get("enabled", False):
            self.disabled_reason = "配置未启用"
            return
        if not str(self.config.get("secret", "")).strip():
            self.disabled_reason = "未配置鉴权密钥"
            return

        app = web.Application(
            client_max_size=max(1024, int(self.config.get("max_body_bytes", 16384)))
        )
        app.router.add_post(str(self.config["path"]), self._handle)
        if self.notify_callback is not None:
            notify_path = str(self.config.get("notify_path", "/api/hooks/notify"))
            if notify_path == str(self.config["path"]):
                raise ValueError("notify_path must differ from the agent webhook path")
            app.router.add_post(
                notify_path, partial(self._handle, callback=self.notify_callback)
            )
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(
            self._runner, str(self.config["host"]), int(self.config["port"])
        )
        try:
            await site.start()
        except Exception:
            await self._runner.cleanup()
            self._runner = None
            raise
        self.started = True
        self.disabled_reason = ""

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
        self._runner = None
        self.started = False

    async def _handle(
        self,
        request: web.Request,
        *,
        callback: Callable[[Any], Awaitable[dict[str, Any]]] | None = None,
    ) -> web.Response:
        """Authenticate, parse, and dispatch a webhook using the shared HTTP policy.

        Args:
            request: Incoming agent or generic notification request.
            callback: Route callback; defaults to the legacy agent callback.

        Returns:
            JSON delivery result or an HTTP error response.
        """
        expected = f"Bearer {str(self.config['secret']).strip()}"
        supplied = request.headers.get("Authorization", "")
        if not hmac.compare_digest(supplied.encode(), expected.encode()):
            return web.json_response(
                {
                    "success": False,
                    "error_code": "UNAUTHORIZED",
                    "message": "鉴权失败。",
                },
                status=401,
            )
        try:
            payload = await request.json()
        except web.HTTPRequestEntityTooLarge:
            return web.json_response(
                {
                    "success": False,
                    "error_code": "REQUEST_TOO_LARGE",
                    "message": "请求体超过大小限制。",
                },
                status=413,
            )
        except Exception:
            return web.json_response(
                {
                    "success": False,
                    "error_code": "INVALID_JSON",
                    "message": "请求体不是有效 JSON。",
                },
                status=400,
            )
        try:
            result = await (callback if callback is not None else self.callback)(
                payload
            )
        except NotificationRequestError as exc:
            return web.json_response(
                {
                    "success": False,
                    "error_code": exc.error_code,
                    "message": exc.message,
                },
                status=exc.http_status,
            )
        return web.json_response(result)
