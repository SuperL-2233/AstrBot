from __future__ import annotations

import asyncio
import json
import re
from contextlib import suppress
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from astrbot.api import llm_tool, logger, star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter

from .core.jwgl_core import AuthenticationRequired, JwglError, safe_exception_name
from .core.mportal_client import TERM_CODE_TO_LABEL, period_to_year_term
from .core.weekly_schedule import structure_course_record
from .formatters.jwgl_formatter import (
    format_records,
    format_status,
    format_validation_report,
    format_week_courses,
    format_weekly_overview,
)
from .repositories.snapshot_repo import SnapshotRepository
from .services.consent_service import ConsentService
from .services.poll_service import PollService
from .services.query_service import AcademicQueryService
from .services.reminder_service import ClassReminderService
from .services.session_service import JwglSessionService
from .services.weekly_schedule_service import WeeklyScheduleService
from .utils.config_loader import load_plugin_config
from .utils.paths import resolve_data_path
from .utils.permission import ensure_bind_access


@star.register(
    "astrbot_plugin_cugb_jwgl",
    "macroladlezilla",
    "CUGB 教务系统监控",
    "0.1.0",
)
class CugbJwglPlugin(star.Star):
    def __init__(self, context: star.Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self.config = load_plugin_config(config)
        self.repo = SnapshotRepository(resolve_data_path(self.config["database"]))
        self.session_service = JwglSessionService(self.config)
        self._startup_task: asyncio.Task | None = None
        self.weekly_service = WeeklyScheduleService(self.repo)
        self.query_service = AcademicQueryService(
            self.session_service, self.repo, self.weekly_service
        )
        self.poll_service = PollService(
            self.config,
            self.session_service,
            self.repo,
            self._push,
            self.weekly_service,
        )
        self.reminder_service = ClassReminderService(
            self.config, self.weekly_service, self.repo, self._push
        )
        self.consent_service = ConsentService(
            self.config,
            self.session_service,
            self.poll_service,
            self._push,
            self._target_umo,
        )

    async def initialize(self) -> None:
        if self.config.get("enabled", True):
            self.poll_service.start()
            self._startup_task = asyncio.create_task(self._startup_probe())
            logger.info("CUGB 教务监控轮询循环已启动")
        else:
            logger.info("CUGB 教务监控已禁用")
        if self.config.get("morning_login_enabled", True):
            try:
                await self.context.cron_manager.add_basic_job(
                    name="cugb_jwgl_morning_login",
                    cron_expression=str(
                        self.config.get("morning_login_cron", "0 9 * * *")
                    ),
                    handler=self.consent_service.morning_check,
                    timezone=str(self.config.get("timezone", "Asia/Shanghai")),
                    persistent=False,
                )
                logger.info("CUGB 教务早晨登录检查已注册")
            except Exception as error:
                logger.warning("注册早晨登录检查失败：%s", safe_exception_name(error))
        if self.config.get("class_reminder_enabled", True):
            self.reminder_service.start()
            logger.info("CUGB 上课提醒循环已启动")

    async def terminate(self) -> None:
        if self._startup_task:
            self._startup_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._startup_task
        await self.poll_service.stop()
        await self.reminder_service.stop()
        await self.session_service.stop()
        self.repo.close()

    async def _startup_probe(self) -> None:
        await asyncio.sleep(20)
        if self.session_service.state == "active":
            return
        if not self.session_service.portal_configured:
            return
        try:
            await self.poll_service.poll_now(trigger="startup", notify_expiry=False)
        except Exception as error:
            logger.warning("启动探测异常：%s", safe_exception_name(error))

    def _target_umo(self) -> str:
        configured = str(self.config.get("target_umo", "")).strip()
        if configured:
            return configured
        return self.repo.get_meta("target_umo", "")

    async def _push(self, text: str) -> bool:
        umo = self._target_umo()
        if not umo:
            return False
        return await self.context.send_message(umo, MessageChain().message(text))

    def get_public_capabilities(self) -> dict:
        return {
            "summary": "每日定时登录 CUGB 教务系统，监控课表、成绩与考试安排变化并向绑定 QQ 私聊推送，命令与 LLM 查询优先读取本地缓存，支持主动刷新。",
            "features": [
                "每天早上按 cron 检查登录态，失效时 QQ 征询后自动发送短信验证码登录",
                "登录后每 15±2 分钟轮询，兼具 WebVPN 与会话续期",
                "成绩自动定位最晚有成绩的学期，寒暑假默认页无成绩时自动回溯",
                "课表、成绩、考试快照 diff，新增或变化行 QQ 推送",
                "每轮轮询扫描 mportal 全周结构化课表（CAS 直连，无需 WebVPN），变动时重建按周课表并与教务文本交叉验证",
                "按周课表支持指定年份与学期（mportal 数据），未缓存的学期实时拉取并缓存",
                "课表、成绩、考试查询优先读取本地快照，不等待后台浏览器；支持直接查今天、明天、本周、下周课表",
                "开学日期自动来自 mportal 学期信息，可手动覆盖",
                "每节课开始前按配置提前向绑定 QQ 推送上课提醒",
            ],
            "commands": [
                "/教务帮助",
                "/教务状态",
                "/教务登录",
                "/教务验证码",
                "/教务轮询",
                "/教务成绩",
                "/教务课表",
                "/教务周课表",
                "/教务课表校验",
                "/教务考试",
                "/教务绑定",
                "/教务解绑",
            ],
            "llm_tools": [
                "jwgl_get_status",
                "jwgl_get_scores",
                "jwgl_get_courses",
                "jwgl_get_week_courses",
                "jwgl_get_exams",
                "jwgl_request_login",
                "jwgl_submit_sms_code",
                "jwgl_poll_now",
            ],
        }

    def get_public_status(self) -> dict:
        stored = self.weekly_service.get_timetable()
        return {
            "state": self.session_service.state,
            "login_pending": self.session_service.login_pending,
            "login_stage": self.session_service.login_stage,
            "poll_loop_running": self.poll_service.running,
            "target_bound": bool(self._target_umo()),
            "last_poll": self.repo.last_poll(),
            "current_period": self.repo.get_meta("current_period", ""),
            "last_scores_period": self.repo.get_meta("last_scores_period", ""),
            "courses_count": self.repo.count("courses"),
            "scores_count": self.repo.count("scores"),
            "exams_count": self.repo.count("exams"),
            "class_reminder_running": self.reminder_service.running,
            "class_reminder_lead_minutes": self.config["class_reminder_lead_minutes"],
            "semester_start_source": self.reminder_service.semester_start_source(),
            "weekly_source": stored.get("source", ""),
            "weekly_validation": self.weekly_service.get_validation_report() or {},
        }

    def _latest_scores_snapshot(self) -> list[dict[str, str]]:
        period = self.repo.get_meta("last_scores_period", "")
        records = self.repo.items("scores")
        if not period:
            return records
        year, _, term = period.partition(" ")
        matched = [
            record
            for record in records
            if record.get("学年", "") == year and record.get("学期", "") == term
        ]
        return matched or records

    def _current_courses_snapshot(self) -> list[dict[str, str]]:
        return self.weekly_service.current_records()[1]

    async def _snapshot_or_live(
        self, kind: str, year: str = "", term: str = ""
    ) -> dict:
        """Query academic records and format course schedules for tools.

        Args:
            kind: Academic record type.
            year: Optional year paired with term.
            term: Optional Chinese term label.

        Returns:
            The query result with structured course schedule fields.
        """
        max_terms = int(self.config.get("scores_probe_max_terms", 6))
        result = await self.query_service.query(kind, year, term, max_terms)
        if kind == "courses":
            result["records"] = [
                structure_course_record(record) for record in result["records"]
            ]
        return result

    @staticmethod
    def _parse_period_arg(raw: str) -> tuple[str, str]:
        """把 "2026秋" / "2026年秋" 形式的学期参数解析为 (year, term)，失败返回空串。"""
        match = re.fullmatch(r"(\d{4})年?\s*([春夏秋])", raw.strip())
        if not match:
            return "", ""
        return match.group(1), match.group(2)

    async def _week_courses_payload(
        self, year: str, term: str, *, on_date: date | None = None
    ) -> dict[str, Any]:
        """Resolve a cached timetable or fetch an explicitly requested missing term.

        Args:
            year: Optional year paired with term.
            term: Optional Chinese term label.
            on_date: Date used to choose a cached term when no term is specified.

        Returns:
            The timetable payload or an explicit error message.
        """
        year, term = year.strip(), term.strip()
        if not year and not term:
            on_date = on_date or datetime.now(ZoneInfo(self.config["timezone"])).date()
            return {"payload": self.weekly_service.get_timetable(on_date=on_date)}
        if not year or not term:
            return {"error": "year 和 term 必须同时提供（如 year=2026、term=秋）。"}
        try:
            payload = await self.poll_service.ensure_term_timetable(year, term)
        except AuthenticationRequired:
            return {"error": "mportal 登录态已失效，请先完成登录流程再查询该学期课表。"}
        except JwglError as error:
            return {"error": str(error)}
        except Exception as error:
            logger.warning("按学期拉取周课表失败：%s", safe_exception_name(error))
            return {
                "error": f"拉取 {year} {term} 课表失败（{safe_exception_name(error)}）。"
            }
        return {"payload": payload}

    @filter.command("教务帮助")
    async def help_command(self, event: AstrMessageEvent):
        yield event.plain_result(
            "CUGB 教务系统监控\n"
            "/教务状态\n"
            "/教务登录\n"
            "/教务验证码 <6位数字>\n"
            "/教务轮询\n"
            "/教务成绩\n"
            "/教务课表\n"
            "/教务周课表 [周次] [学期]\n"
            "/教务课表校验 [周次]\n"
            "/教务考试\n"
            "/教务绑定\n"
            "/教务解绑\n"
            "每天早上检查登录态；登录时学校会向你的手机发短信验证码，"
            "把验证码发给我即可完成登录。登录后每 15±2 分钟自动轮询并推送新数据。"
        )

    @filter.command("教务状态")
    async def status_command(self, event: AstrMessageEvent):
        yield event.plain_result(format_status(self.get_public_status()))

    @filter.command("教务登录")
    async def login_command(self, event: AstrMessageEvent):
        # 命令是用户的显式动作：等待中时重发验证码（冷却感知）比干等更符合意图
        message = await self.consent_service.request_login(resend=True)
        yield event.plain_result(message)

    @filter.command("教务验证码")
    async def sms_code_command(self, event: AstrMessageEvent, code: str = ""):
        result = await self.consent_service.submit_sms_code(code)
        yield event.plain_result(result["message"])

    @filter.command("教务轮询")
    async def poll_command(self, event: AstrMessageEvent):
        yield event.plain_result("开始执行一轮教务数据检查，请稍候……")
        result = await self.poll_service.poll_now(trigger="manual")
        yield event.plain_result(result["message"])

    @filter.command("教务成绩")
    async def scores_command(self, event: AstrMessageEvent):
        period = self.repo.get_meta("last_scores_period", "")
        yield event.plain_result(
            format_records("scores", self._latest_scores_snapshot(), period)
        )

    @filter.command("教务课表")
    async def courses_command(self, event: AstrMessageEvent):
        period = self.repo.get_meta("current_period", "")
        yield event.plain_result(
            format_records("courses", self._current_courses_snapshot(), period)
        )

    @filter.command("教务周课表")
    async def week_courses_command(
        self, event: AstrMessageEvent, week: str = "", period: str = ""
    ):
        week = week.strip()
        period = period.strip()
        year, term = self._parse_period_arg(period) if period else ("", "")
        if period and not year:
            yield event.plain_result(
                "学期格式应为 2026秋 这样的形式，例如 /教务周课表 3 2026秋"
            )
            return
        if week and not week.isdigit():
            # 允许 /教务周课表 2026秋（只看该学期总览）
            alt_year, alt_term = self._parse_period_arg(week)
            if alt_term and not period:
                year, term, week = alt_year, alt_term, ""
            else:
                yield event.plain_result(
                    "用法：/教务周课表 [周次] [学期]，例如 /教务周课表 3 2026秋"
                )
                return
        if year:
            yield event.plain_result(f"正在获取 {year} {term} 的周课表，请稍候……")
        result = await self._week_courses_payload(year, term)
        if "error" in result:
            yield event.plain_result(result["error"])
            return
        stored = result["payload"]
        label = stored.get("period", "")
        weeks = {
            int(number): entries for number, entries in stored.get("weeks", {}).items()
        }
        if not week:
            yield event.plain_result(format_weekly_overview(weeks, label))
            return
        yield event.plain_result(
            format_week_courses(int(week), weeks.get(int(week), []), label)
        )

    @filter.command("教务课表校验")
    async def validate_courses_command(self, event: AstrMessageEvent, week: str = ""):
        yield event.plain_result("正在从 mportal 拉取全学期课表并交叉验证，请稍候……")
        result = await self.poll_service.validate_now()
        if not result["success"]:
            yield event.plain_result(result["message"])
            return
        week = week.strip()
        week_number = int(week) if week.isdigit() else 0
        yield event.plain_result(
            format_validation_report(result["report"], week_number)
        )

    @filter.command("教务考试")
    async def exams_command(self, event: AstrMessageEvent):
        yield event.plain_result(format_records("exams", self.repo.items("exams")))

    @filter.command("教务绑定")
    async def bind_command(self, event: AstrMessageEvent):
        access_error = ensure_bind_access(event, self.config)
        if access_error:
            yield event.plain_result(access_error)
            return
        self.repo.set_meta("target_umo", event.unified_msg_origin)
        yield event.plain_result("已将教务监控推送绑定到当前会话。")

    @filter.command("教务解绑")
    async def unbind_command(self, event: AstrMessageEvent):
        access_error = ensure_bind_access(event, self.config)
        if access_error:
            yield event.plain_result(access_error)
            return
        self.repo.set_meta("target_umo", "")
        yield event.plain_result("已解除教务监控推送绑定。")

    @llm_tool("jwgl_get_status")
    async def tool_get_status(self, _context) -> str:
        """获取 CUGB 教务监控的运行状态：登录态、轮询循环、推送绑定、最近轮询结果与各模块数据量。
        login_pending 为 true 表示正在等待用户提交短信验证码，
        login_stage 进一步区分等待的是教务门户（"portal"）还是 mportal 课表（"mportal"）的验证码。"""
        return json.dumps(self.get_public_status(), ensure_ascii=False, default=str)

    @llm_tool("jwgl_get_scores")
    async def tool_get_scores(self, _context, year: str = "", term: str = "") -> str:
        """Query personal scores, using local snapshots before online requests.

        Omit year and term for the latest cached score period; no status call is
        needed first. Supply both to query another period. Background polls keep
        snapshots updated; use jwgl_poll_now only when the user requests a refresh.
        An uncached period without scores may fall back to the latest scores,
        with the actual period and a note in the response.

        Args:
            year(string): Optional natural year, such as "2026"; pair with term.
            term(string): Optional Chinese term label: 春, 夏 or 秋.

        Returns:
            JSON containing records, their period and snapshot/live source.
        """
        return json.dumps(
            await self._snapshot_or_live("scores", year, term), ensure_ascii=False
        )

    @llm_tool("jwgl_get_courses")
    async def tool_get_courses(self, _context, year: str = "", term: str = "") -> str:
        """Query the semester course list from local snapshots first.

        Omit year and term for the current period, including 本学期/这个学期;
        call this tool directly without a preceding status query. Supply both for
        another period. For 今天/明天/本周/下周 use jwgl_get_week_courses instead.
        The schedule fields contain parsed weeks, weekdays, sections and rooms.
        Validation uses cached mportal data only. Use jwgl_poll_now only when the
        user explicitly requests fresh data.

        Args:
            year(string): Optional natural year, such as "2026"; pair with term.
            term(string): Optional Chinese term label: 春, 夏 or 秋.

        Returns:
            JSON records with their actual period, source and cached validation.
        """
        result = await self._snapshot_or_live("courses", year, term)
        mapped = period_to_year_term(result.get("period", ""))
        if mapped and result.get("records"):
            validation = await self.poll_service.validate_query_term(
                mapped[0], TERM_CODE_TO_LABEL[mapped[1]], result["records"]
            )
            if validation is not None:
                result["validation"] = validation
        return json.dumps(result, ensure_ascii=False)

    @llm_tool("jwgl_get_week_courses")
    async def tool_get_week_courses(
        self, _context, week: str = "", year: str = "", term: str = ""
    ) -> str:
        """Query a cached weekly timetable including course dates and rooms.

        For 本周/这周 pass week="current"; for 下周 pass week="next".
        For 今天/明天 pass week="today"/"tomorrow" to return only that day's
        courses. Dates and teaching weeks are resolved locally without a status
        or semester-start query.
        Omit year and term for the current cached period; supply both for another
        period. Only an uncached explicit period needs a live full-term scan.

        Args:
            week(string): "current", "next", "today", "tomorrow", a teaching week
                number such as "3", or empty for an overview of all weeks.
            year(string): Optional natural year, such as "2026"; pair with term.
            term(string): Optional Chinese term label: 春, 夏 or 秋.

        Returns:
            JSON for one week or a compact overview, with its actual period.
        """
        today = datetime.now(ZoneInfo(self.config["timezone"])).date()
        target = today + timedelta(
            days={"next": 7, "tomorrow": 1}.get(week.strip().lower(), 0)
        )
        result = await self._week_courses_payload(year, term, on_date=target)
        if "error" in result:
            return json.dumps(
                {"success": False, "message": result["error"]}, ensure_ascii=False
            )
        return json.dumps(
            self.weekly_service.query_week_courses(
                result["payload"],
                week,
                today=today,
                semester_start_date=self.config.get("semester_start_date", ""),
            ),
            ensure_ascii=False,
        )

    @llm_tool("jwgl_get_exams")
    async def tool_get_exams(self, _context) -> str:
        """Query exam arrangements from local snapshots before online requests.

        Returns:
            JSON records with a snapshot/live source label.
        """
        return json.dumps(await self._snapshot_or_live("exams"), ensure_ascii=False)

    @llm_tool("jwgl_request_login")
    async def tool_request_login(self, _context, resend: bool = False) -> str:
        """发起教务系统登录。重要：验证码由学校统一认证平台通过短信发送到用户手机，
        机器人不发送也不转发验证码；用户收到手机短信后会在当前对话回复 6 位数字，
        届时必须调用 jwgl_submit_sms_code 工具把验证码提交给学校完成登录。
        若返回提示"登录流程已在进行中"，说明此前已发起过登录且正在等待验证码，
        此时不要重复发起，直接让用户回复手机收到的最新验证码即可；
        只有当用户明确说没收到短信、要求重发时，才把 resend 填为 true 重新发送
        （学校限制 60 秒重发间隔，工具会自动等待冷却结束后发出）。
        返回结果会说明流程是否真正发起成功，请把返回内容如实转告用户。

        Args:
            resend(boolean): 已在等待验证码但用户明确说没收到短信时填 true 重发
        """
        message = await self.consent_service.request_login(resend=resend)
        return json.dumps({"message": message}, ensure_ascii=False)

    @llm_tool("jwgl_submit_sms_code")
    async def tool_submit_sms_code(self, _context, code: str) -> str:
        """把用户回复的学校短信验证码提交给统一认证平台，完成教务系统登录。
        仅当 jwgl_get_status 中 login_pending 为 true（已发起登录、等待验证码）时调用；
        login_stage 为 "portal" 表示在等教务门户的验证码，为 "mportal" 表示门户已通过、
        在等 mportal 结构化课表的第二次验证码，两者都用本工具提交。
        返回 success 为 false 且提示页面已失效时，应调用 jwgl_request_login 重新发起登录。

        Args:
            code(string): 用户手机短信收到的 6 位数字验证码
        """
        result = await self.consent_service.submit_sms_code(code)
        return json.dumps(result, ensure_ascii=False)

    @llm_tool("jwgl_poll_now")
    async def tool_poll_now(self, _context) -> str:
        """Refresh academic snapshots online when the user explicitly requests it.

        This scans courses, scores, exams and whole-term timetables and may take
        time. For ordinary queries use the cache-first jwgl_get_* tools directly.
        New data is pushed to the bound user after the scan.

        Returns:
            JSON with the poll outcome.
        """
        result = await self.poll_service.poll_now(trigger="llm-tool")
        return json.dumps(result, ensure_ascii=False)
