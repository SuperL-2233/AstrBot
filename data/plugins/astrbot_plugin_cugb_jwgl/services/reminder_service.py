from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from astrbot.api import logger

from ..core.jwgl_core import safe_exception_name
from ..core.mportal_client import period_to_year_term
from ..formatters.jwgl_formatter import format_class_reminder
from ..repositories.snapshot_repo import SnapshotRepository
from .weekly_schedule_service import WeeklyScheduleService

PushFunc = Callable[[str], Awaitable[bool]]

_META_KEY = "class_reminder_sent"


class ClassReminderService:
    """Pushes a reminder to the bound user shortly before each class starts.

    Class datetimes are derived from the persisted weekly timetable, the
    configured semester start date (Monday of week 1) and the configured
    per-section start times. Sent reminders are deduplicated in the
    snapshot database so restarts never re-push the same class.
    """

    def __init__(
        self,
        config: dict[str, Any],
        weekly: WeeklyScheduleService,
        repo: SnapshotRepository,
        push: PushFunc,
    ) -> None:
        self.config = config
        self.weekly = weekly
        self.repo = repo
        self.push = push
        self._task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return bool(self._task and not self._task.done())

    def start(self) -> None:
        if not self.running:
            self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def _semester_start(self) -> date | None:
        """Resolve the semester start date.

        Priority: manual config override, then the mportal termInfo cache
        matching the stored timetable's period. None when both are unknown.
        """
        raw = str(self.config.get("semester_start_date", "")).strip()
        if raw:
            try:
                return date.fromisoformat(raw)
            except ValueError:
                pass
        stored = self.weekly.get_timetable()
        mapped = period_to_year_term(stored.get("period", ""))
        if mapped is not None:
            startday = self.weekly.term_start(*mapped)
            if startday:
                try:
                    return date.fromisoformat(startday)
                except ValueError:
                    pass
        return None

    def semester_start_source(self) -> str:
        """Describe where the semester start date comes from, for status display."""
        if str(self.config.get("semester_start_date", "")).strip():
            return "manual"
        if self._semester_start() is not None:
            return "mportal"
        return "none"

    def _teaching_week(self, now: datetime, stored: dict[str, Any]) -> int | None:
        """Map a date to its teaching week number.

        Prefers exact week_dates from the mportal-sourced timetable; falls
        back to computing from the semester start date.
        """
        today = now.date().isoformat()
        for week, dates in (stored.get("week_dates") or {}).items():
            if today in dates:
                return int(week)
        semester_start = self._semester_start()
        if semester_start is None:
            return None
        week = (now.date() - semester_start).days // 7 + 1
        return week if week >= 1 else None

    def _load_sent(self) -> set[str]:
        raw = self.repo.get_meta(_META_KEY, "")
        try:
            data = json.loads(raw) if raw else []
        except ValueError:
            return set()
        return set(data) if isinstance(data, list) else set()

    def _save_sent(self, sent: set[str], today: str) -> None:
        pruned = sorted(key for key in sent if key.startswith(today))
        self.repo.set_meta(_META_KEY, json.dumps(pruned, ensure_ascii=False))

    async def check_once(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """Push reminders for today's classes that entered the lead window.

        Args:
            now: Optional timezone-aware current time, injectable for tests.

        Returns:
            Occurrences that triggered a reminder during this check. Empty
            when reminders are disabled, the semester start date is not
            configured, or no class is due within the lead window.
        """
        if not self.config.get("class_reminder_enabled", True):
            return []
        tz = ZoneInfo(str(self.config.get("timezone", "Asia/Shanghai")))
        now = now or datetime.now(tz)
        stored = self.weekly.get_timetable()
        week = self._teaching_week(now, stored)
        if week is None:
            return []
        lead_seconds = (
            max(1, int(self.config.get("class_reminder_lead_minutes", 30))) * 60
        )
        section_times = self.config.get("section_start_times", {})
        sent = self._load_sent()
        pushed: list[dict[str, Any]] = []
        for occurrence in stored.get("weeks", {}).get(str(week), []):
            if occurrence.get("weekday") != now.isoweekday():
                continue
            sections = occurrence.get("sections") or []
            if not sections:
                continue
            raw_time = section_times.get(str(sections[0]))
            if not raw_time:
                continue
            try:
                hour, minute = (int(part) for part in str(raw_time).split(":"))
            except ValueError:
                continue
            class_start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            delta = (class_start - now).total_seconds()
            if not 0 <= delta <= lead_seconds:
                continue
            key = (
                f"{now.date().isoformat()}|{occurrence.get('course_code', '')}"
                f"|{occurrence.get('course_name', '')}|{sections[0]}"
            )
            if key in sent:
                continue
            minutes = max(1, round(delta / 60))
            message = format_class_reminder(
                occurrence, class_start.strftime("%H:%M"), minutes
            )
            if await self.push(message):
                sent.add(key)
                pushed.append(occurrence)
        if pushed:
            self._save_sent(sent, now.date().isoformat())
        return pushed

    async def _run_loop(self) -> None:
        interval = max(10, int(self.config.get("class_reminder_check_seconds", 60)))
        while True:
            try:
                pushed = await self.check_once()
                if pushed:
                    logger.info("已推送 %d 条上课提醒", len(pushed))
            except Exception as error:
                logger.warning("上课提醒检查异常：%s", safe_exception_name(error))
            await asyncio.sleep(interval)
