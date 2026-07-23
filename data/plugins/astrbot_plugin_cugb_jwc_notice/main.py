from __future__ import annotations

import asyncio
import json
import re
from contextlib import suppress

from astrbot.api import llm_tool, logger, star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.core.star.filter.command import GreedyStr

from .formatters.notice_formatter import (
    format_notice_page,
    format_search_results,
    format_statistics,
    format_status,
)
from .providers.notice_provider import NoticeProvider
from .repositories.notice_repo import NoticeRepository
from .services.notice_service import NoticeService
from .utils.config_loader import load_plugin_config
from .utils.paths import resolve_data_path
from .utils.permission import ensure_bind_access

_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_source_dates(
    args: list[str], *, max_dates: int
) -> tuple[str, str, str] | str:
    source = ""
    dates: list[str] = []
    for arg in args:
        token = arg.strip()
        if not token:
            continue
        if _DATE_PATTERN.match(token):
            dates.append(token)
        elif not source:
            source = token
        else:
            return "参数过多或无法识别。来源名称和日期（YYYY-MM-DD）请检查。"
    if len(dates) > max_dates:
        return f"该命令最多接受 {max_dates} 个日期参数。"
    start = dates[0] if dates else ""
    end = dates[1] if len(dates) > 1 else ""
    return source, start, end


@star.register(
    "astrbot_plugin_cugb_jwc_notice",
    "macroladlezilla",
    "CUGB 新闻公告检索",
    "0.4.0",
)
class CugbNewsNoticePlugin(star.Star):
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
            logger.info("CUGB 新闻公告同步循环已启动")
        else:
            logger.info("CUGB 新闻公告在线同步已禁用")

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
                logger.warning(f"CUGB 新闻公告同步任务异常：{exc}")
            await asyncio.sleep(interval)

    async def _send_message(self, target_umo: str, text: str) -> bool:
        return await self.context.send_message(target_umo, MessageChain().message(text))

    def get_public_capabilities(self) -> dict:
        return {
            "summary": "同步 CUGB 教务处、人工智能学院和学工处的公开新闻公告，推送新增条目，并检索本地正文缓存。",
            "features": [
                "默认每 10 分钟同步六个公开栏目自 2025-09-01 以来的条目",
                "教务处学生专区、教学动态，人工智能学院新闻动态、公告，学工处新闻动态、通知公告",
                "新增条目 QQ 私聊推送",
                "正文关键词检索与链接返回，可按来源过滤",
                "分来源与整体的数量、日期范围和按月统计",
                "列表分页查询",
            ],
            "commands": [
                "/新闻公告状态",
                "/新闻公告同步",
                "/新闻公告搜索",
                "/新闻公告列表",
                "/新闻公告统计",
                "/新闻公告绑定",
                "/新闻公告解绑",
            ],
            "llm_tools": [
                "refresh_cugb_notices",
                "search_cugb_notice_contents",
                "list_cugb_notices",
                "get_cugb_notice_stats",
            ],
        }

    def get_public_status(self) -> dict:
        status = self.notice_service.status()
        status["refresh_loop_running"] = bool(
            self._refresh_task and not self._refresh_task.done()
        )
        return status

    @filter.command("新闻公告帮助")
    async def help_command(self, event: AstrMessageEvent):
        yield event.plain_result(
            "CUGB 新闻公告检索\n"
            "/新闻公告状态\n"
            "/新闻公告同步 [来源] [最早日期 YYYY-MM-DD]\n"
            "/新闻公告搜索 <正文关键词>\n"
            "/新闻公告列表 [来源] [开始日期] [结束日期]\n"
            "/新闻公告统计 [来源] [开始日期] [结束日期]\n"
            "/新闻公告绑定\n"
            "/新闻公告解绑\n"
            "来源可用：教务、教学动态、AI新闻、AI公告、学工新闻、学工公告；留空为全部。"
        )

    @filter.command("新闻公告状态")
    async def status_command(self, event: AstrMessageEvent):
        yield event.plain_result(format_status(self.get_public_status()))

    @filter.command("新闻公告同步")
    async def refresh_command(
        self,
        event: AstrMessageEvent,
        first: str = "",
        second: str = "",
    ):
        parsed = _parse_source_dates([first, second], max_dates=1)
        if isinstance(parsed, str):
            yield event.plain_result(parsed)
            return
        source, earliest_date, _ = parsed
        result = await self.notice_service.refresh(source, earliest_date)
        yield event.plain_result(result["message"])

    @filter.command("新闻公告搜索")
    async def search_command(self, event: AstrMessageEvent, keyword: GreedyStr = ""):
        result = self.notice_service.search(
            keyword.strip(), limit=int(self.config.get("search_limit", 10))
        )
        yield event.plain_result(format_search_results(result))

    @filter.command("新闻公告列表")
    async def list_command(
        self,
        event: AstrMessageEvent,
        first: str = "",
        second: str = "",
        third: str = "",
    ):
        parsed = _parse_source_dates([first, second, third], max_dates=2)
        if isinstance(parsed, str):
            yield event.plain_result(parsed)
            return
        source, start_date, end_date = parsed
        result = self.notice_service.list_notices(
            source,
            start_date,
            end_date,
            limit=int(self.config.get("search_limit", 10)),
        )
        yield event.plain_result(format_notice_page(result))

    @filter.command("新闻公告统计")
    async def statistics_command(
        self,
        event: AstrMessageEvent,
        first: str = "",
        second: str = "",
        third: str = "",
    ):
        parsed = _parse_source_dates([first, second, third], max_dates=2)
        if isinstance(parsed, str):
            yield event.plain_result(parsed)
            return
        source, start_date, end_date = parsed
        yield event.plain_result(
            format_statistics(
                self.notice_service.statistics(source, start_date, end_date)
            )
        )

    @filter.command("新闻公告绑定")
    async def bind_command(self, event: AstrMessageEvent):
        access_error = ensure_bind_access(event, self.config)
        if access_error:
            yield event.plain_result(access_error)
            return
        self.notice_service.bind_target(event.unified_msg_origin)
        delivery = await self.notice_service.deliver_pending()
        text = "已将 CUGB 新闻公告更新绑定到当前 QQ 私聊。"
        if delivery["sent_count"]:
            text += f" 已补推 {delivery['sent_count']} 条待发送通知。"
        yield event.plain_result(text)

    @filter.command("新闻公告解绑")
    async def unbind_command(self, event: AstrMessageEvent):
        access_error = ensure_bind_access(event, self.config)
        if access_error:
            yield event.plain_result(access_error)
            return
        self.notice_service.clear_target()
        yield event.plain_result("已解除新闻公告 QQ 推送绑定。")

    @llm_tool("refresh_cugb_notices")
    async def tool_refresh_cugb_notices(
        self, _context, source: str = "", earliest_date: str = ""
    ) -> str:
        """立即同步 CUGB 公开新闻公告，返回各来源本次扫描数和数据库总数。

        Args:
            source(string): 可选来源：jwc_student 教务处学生专区、jwc_teaching 教务处教学动态、ai_news 人工智能学院新闻动态、ai_notice 人工智能学院公告、xgb_news 学工处新闻动态、xgb_notice 学工处通知公告；留空同步全部来源
            earliest_date(string): 可选最早发布日期，格式 YYYY-MM-DD；留空使用插件默认值
        """
        return json.dumps(
            await self.notice_service.refresh(source, earliest_date),
            ensure_ascii=False,
        )

    @llm_tool("search_cugb_notice_contents")
    async def tool_search_cugb_notice_contents(
        self,
        _context,
        keyword: str,
        source: str = "",
        start_date: str = "",
        end_date: str = "",
        limit: int = 10,
        offset: int = 0,
    ) -> str:
        """按关键词分页检索新闻公告正文，返回真实总命中数、摘要及链接。

        Args:
            keyword(string): 要在正文中查找的关键词
            source(string): 可选来源过滤：jwc_student、jwc_teaching、ai_news、ai_notice、xgb_news、xgb_notice；留空检索全部来源
            start_date(string): 可选发布日期下限，格式 YYYY-MM-DD
            end_date(string): 可选发布日期上限，格式 YYYY-MM-DD
            limit(number): 本页条数，范围 1 到 50，默认 10
            offset(number): 分页偏移量，默认 0；总数必须读取 total_count
        """
        return json.dumps(
            self.notice_service.search(
                keyword.strip(), source, start_date, end_date, limit, offset
            ),
            ensure_ascii=False,
        )

    @llm_tool("list_cugb_notices")
    async def tool_list_cugb_notices(
        self,
        _context,
        source: str = "",
        start_date: str = "",
        end_date: str = "",
        limit: int = 10,
        offset: int = 0,
    ) -> str:
        """分页列出已缓存的新闻公告；无需伪造关键词来浏览列表。

        Args:
            source(string): 可选来源过滤：jwc_student、jwc_teaching、ai_news、ai_notice、xgb_news、xgb_notice；留空列出全部来源
            start_date(string): 可选发布日期下限，格式 YYYY-MM-DD
            end_date(string): 可选发布日期上限，格式 YYYY-MM-DD
            limit(number): 本页条数，范围 1 到 50，默认 10
            offset(number): 分页偏移量，默认 0；总数必须读取 total_count
        """
        return json.dumps(
            self.notice_service.list_notices(
                source, start_date, end_date, limit, offset
            ),
            ensure_ascii=False,
        )

    @llm_tool("get_cugb_notice_stats")
    async def tool_get_cugb_notice_stats(
        self,
        _context,
        source: str = "",
        start_date: str = "",
        end_date: str = "",
    ) -> str:
        """统计本地新闻公告总数、正文数、日期范围、分来源数量和逐月数量。

        当用户询问“一共有多少条”“覆盖到什么时候”等总量问题时，必须调用本工具，
        不要使用正文关键词搜索结果的返回条数推断总数。

        Args:
            source(string): 可选来源过滤：jwc_student、jwc_teaching、ai_news、ai_notice、xgb_news、xgb_notice；留空统计全部来源
            start_date(string): 可选统计日期下限，格式 YYYY-MM-DD
            end_date(string): 可选统计日期上限，格式 YYYY-MM-DD
        """
        return json.dumps(
            self.notice_service.statistics(source, start_date, end_date),
            ensure_ascii=False,
        )
