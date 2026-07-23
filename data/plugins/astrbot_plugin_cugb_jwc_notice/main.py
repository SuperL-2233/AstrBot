from __future__ import annotations

import asyncio
import json
from contextlib import suppress

from astrbot.api import llm_tool, logger, star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.core.star.filter.command import GreedyStr

from .formatters.notice_formatter import format_search_results, format_status
from .providers.notice_provider import NoticeProvider
from .repositories.notice_repo import NoticeRepository
from .services.notice_service import NoticeService
from .utils.config_loader import load_plugin_config
from .utils.paths import resolve_data_path
from .utils.permission import ensure_bind_access


@star.register(
    "astrbot_plugin_cugb_jwc_notice",
    "macroladlezilla",
    "CUGB 教务处通知",
    "0.2.0",
)
class CugbJwcNoticePlugin(star.Star):
    def __init__(self, context: star.Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self.config = load_plugin_config(config)
        repository = NoticeRepository(resolve_data_path(self.config["database"]))
        provider = NoticeProvider(self.config)
        self.notice_service = NoticeService(
            self.config, provider, repository, self._send_message
        )
        self._refresh_task: asyncio.Task | None = None

    async def initialize(self) -> None:
        if self.config.get("enabled", True):
            self._refresh_task = asyncio.create_task(self._run_refresh_loop())
            logger.info("CUGB 教务处通知同步循环已启动")
        else:
            logger.info("CUGB 教务处通知在线同步已禁用")

    async def terminate(self) -> None:
        if self._refresh_task:
            self._refresh_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._refresh_task

    async def _run_refresh_loop(self) -> None:
        interval = max(1, int(self.config.get("refresh_interval_minutes", 10))) * 60
        while True:
            try:
                result = await self.notice_service.refresh()
                if result["success"]:
                    logger.info(result["message"])
                else:
                    logger.warning(result["message"])
            except Exception as exc:
                logger.warning(f"CUGB 教务处通知同步任务异常：{exc}")
            await asyncio.sleep(interval)

    async def _send_message(self, target_umo: str, text: str) -> bool:
        return await self.context.send_message(target_umo, MessageChain().message(text))

    def get_public_capabilities(self) -> dict:
        return {
            "summary": "同步教务处公开通知，推送新增条目，并检索本地正文缓存。",
            "features": [
                "默认每 10 分钟同步 2025-09-01 以来的教务处通知",
                "新增通知 QQ 私聊推送",
                "通知正文关键词检索与链接返回",
            ],
            "commands": [
                "/教务通知状态",
                "/教务通知同步",
                "/教务通知搜索",
                "/教务通知绑定",
                "/教务通知解绑",
            ],
            "llm_tools": ["refresh_jwc_notices", "search_jwc_notice_contents"],
        }

    def get_public_status(self) -> dict:
        status = self.notice_service.status()
        status["refresh_loop_running"] = bool(
            self._refresh_task and not self._refresh_task.done()
        )
        return status

    @filter.command("教务通知帮助")
    async def help_command(self, event: AstrMessageEvent):
        yield event.plain_result(
            "CUGB 教务处通知\n"
            "/教务通知状态\n"
            "/教务通知同步 [最早日期 YYYY-MM-DD]\n"
            "/教务通知搜索 <正文关键词>\n"
            "/教务通知绑定\n"
            "/教务通知解绑"
        )

    @filter.command("教务通知状态")
    async def status_command(self, event: AstrMessageEvent):
        yield event.plain_result(format_status(self.get_public_status()))

    @filter.command("教务通知同步")
    async def refresh_command(self, event: AstrMessageEvent, earliest_date: str = ""):
        result = await self.notice_service.refresh(earliest_date)
        yield event.plain_result(result["message"])

    @filter.command("教务通知搜索")
    async def search_command(self, event: AstrMessageEvent, keyword: GreedyStr = ""):
        result = self.notice_service.search(keyword.strip())
        yield event.plain_result(format_search_results(result))

    @filter.command("教务通知绑定")
    async def bind_command(self, event: AstrMessageEvent):
        access_error = ensure_bind_access(event, self.config)
        if access_error:
            yield event.plain_result(access_error)
            return
        self.notice_service.bind_target(event.unified_msg_origin)
        delivery = await self.notice_service.deliver_pending()
        text = "已将教务处新增通知绑定到当前 QQ 私聊。"
        if delivery["sent_count"]:
            text += f" 已补推 {delivery['sent_count']} 条待发送通知。"
        yield event.plain_result(text)

    @filter.command("教务通知解绑")
    async def unbind_command(self, event: AstrMessageEvent):
        access_error = ensure_bind_access(event, self.config)
        if access_error:
            yield event.plain_result(access_error)
            return
        self.notice_service.clear_target()
        yield event.plain_result("已解除教务处通知 QQ 推送绑定。")

    @llm_tool("refresh_jwc_notices")
    async def tool_refresh_jwc_notices(self, _context, earliest_date: str = "") -> str:
        """立即同步 CUGB 教务处通知；默认追溯到 2025-09-01。

        Args:
            earliest_date(string): 可选最早发布日期，格式 YYYY-MM-DD；留空使用插件默认值
        """
        return json.dumps(
            await self.notice_service.refresh(earliest_date), ensure_ascii=False
        )

    @llm_tool("search_jwc_notice_contents")
    async def tool_search_jwc_notice_contents(self, _context, keyword: str) -> str:
        """按关键词检索已同步的 CUGB 教务处通知正文并返回原文及附件链接。

        Args:
            keyword(string): 要在通知正文中查找的关键词
        """
        return json.dumps(
            self.notice_service.search(keyword.strip()), ensure_ascii=False
        )
