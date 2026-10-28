from __future__ import annotations

import asyncio
from typing import Any

from .jwgl_core import JwglSettings, safe_exception_name, sanitize_url


class BrowserDependencyError(RuntimeError):
    pass


class BrowserManager:
    def __init__(self, settings: JwglSettings) -> None:
        self.settings = settings
        self.playwright: Any | None = None
        self.context: Any | None = None
        self.main_page: Any | None = None
        self.closed = asyncio.Event()

    async def start(self) -> Any:
        self.settings.profile_dir.mkdir(parents=True, exist_ok=True)
        try:
            from playwright.async_api import async_playwright
        except ImportError as error:
            raise BrowserDependencyError(
                "Playwright 未安装，请先 pip install playwright 并执行 "
                "playwright install chromium"
            ) from error

        self.playwright = await async_playwright().start()
        launch_options: dict[str, Any] = {
            "user_data_dir": str(self.settings.profile_dir),
            "headless": self.settings.headless,
        }
        if self.settings.browser_channel:
            launch_options["channel"] = self.settings.browser_channel
        try:
            self.context = await self.playwright.chromium.launch_persistent_context(
                **launch_options
            )
        except Exception as error:
            error_text = str(error).lower()
            await self.playwright.stop()
            self.playwright = None
            if any(
                marker in error_text
                for marker in (
                    "processsingleton",
                    "profile in use",
                    "profile is in use",
                )
            ):
                hint = "浏览器 Profile 已被占用，请先停止其他监控实例。"
            else:
                hint = "请执行 playwright install chromium。"
            raise BrowserDependencyError(
                f"Chromium 启动失败。{hint}（错误类型：{safe_exception_name(error)}）"
            ) from error

        self.context.set_default_timeout(self.settings.navigation_timeout_ms)
        self.context.set_default_navigation_timeout(self.settings.navigation_timeout_ms)
        self.context.on("close", lambda *_args: self.closed.set())
        self.main_page = (
            self.context.pages[0]
            if self.context.pages
            else await self.context.new_page()
        )
        for page in list(self.context.pages):
            if page is not self.main_page:
                await page.close()
        return self.context

    async def stop(self) -> None:
        if self.context is not None:
            try:
                await self.context.close()
            except Exception:
                pass
            self.context = None
        if self.playwright is not None:
            try:
                await self.playwright.stop()
            except Exception:
                pass
            self.playwright = None
        self.main_page = None

    def describe_start_url(self) -> str:
        return sanitize_url(self.settings.portal_url) or ""
