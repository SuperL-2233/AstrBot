from __future__ import annotations

import asyncio
import hashlib
import json
import random
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

from astrbot.api import logger

from ..core.jwgl_core import AuthenticationRequired, JwglError, safe_exception_name
from ..core.mportal_client import (
    TERM_CODE_TO_LABEL,
    TERM_LABEL_TO_CODE,
    period_to_year_term,
)
from ..core.portal import BACKFILL_START, next_period, period_label, periods_between
from ..core.weekly_schedule import weeks_from_structured_records
from ..formatters.jwgl_formatter import format_changes, format_validation_report
from ..repositories.snapshot_repo import SnapshotRepository
from .session_service import JwglSessionService
from .weekly_schedule_service import WeeklyScheduleService

PushFunc = Callable[[str], Awaitable[bool]]


def _fallback_key(record: dict[str, str]) -> str:
    raw = json.dumps(record, ensure_ascii=False, sort_keys=True)
    return "hash:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _course_key(period: str) -> Callable[[dict[str, str]], str]:
    def key(record: dict[str, str]) -> str:
        code = record.get("课程号", "").strip()
        seq = (record.get("课序号") or record.get("课程序号", "")).strip()
        name = record.get("课程名称", "").strip()
        body = f"{code}|{seq}|{name}" if code or name else _fallback_key(record)
        return f"{period}|{body}"

    return key


def _exam_key(record: dict[str, str]) -> str:
    key = record.get("课程号", "").strip()
    name = record.get("课程名称", "").strip()
    return f"{key}|{name}" if key or name else _fallback_key(record)


def _score_key(period: str) -> Callable[[dict[str, str]], str]:
    def key(record: dict[str, str]) -> str:
        year = record.get("学年", "").strip()
        term = record.get("学期", "").strip()
        code = record.get("课程号", "").strip()
        seq = record.get("课序号", "").strip()
        name = (record.get("课程名") or record.get("课程名称", "")).strip()
        if year or term:
            return f"{year}|{term}|{code}|{seq}|{name}"
        body = f"{code}|{name}" if code or name else _fallback_key(record)
        return f"{period}|{body}"

    return key


class PollService:
    """15±2 分钟轮询：给 WebVPN/教务会话续期，并 diff 推送数据变化。"""

    def __init__(
        self,
        config: dict[str, Any],
        session: JwglSessionService,
        repo: SnapshotRepository,
        push: PushFunc,
        weekly: WeeklyScheduleService,
    ) -> None:
        self.config = config
        self.session = session
        self.repo = repo
        self.push = push
        self.weekly = weekly
        self._task: asyncio.Task | None = None
        self._cycle_lock = asyncio.Lock()
        self._expiry_notified = self.repo.get_meta("auth_expiry_notified") == "1"
        self._mportal_auth_notified = self.repo.get_meta("mportal_auth_notified") == "1"

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

    def reset_expiry_notification(self) -> None:
        """Allow one new expiry notification after verified authentication."""
        self._expiry_notified = False
        self.repo.set_meta("auth_expiry_notified", "0")

    async def _run_loop(self) -> None:
        interval = max(1, float(self.config.get("poll_interval_minutes", 15))) * 60
        jitter = max(0, float(self.config.get("poll_jitter_minutes", 2))) * 60
        while True:
            await asyncio.sleep(interval + random.uniform(-jitter, jitter))
            if self.session.state != "active" and not (
                self.session.state == "expired" and not self._expiry_notified
            ):
                continue
            try:
                await self.poll_now(trigger="auto")
            except Exception as error:
                logger.warning("自动轮询异常：%s", safe_exception_name(error))

    async def poll_now(self, trigger: str = "manual") -> dict[str, Any]:
        async with self._cycle_lock:
            if self.session.login_pending:
                return {
                    "success": False,
                    "message": "登录流程进行中，请先完成验证码步骤。",
                }
            scores_backfill = self.repo.get_meta("backfill_done:scores") != "1"
            courses_backfill = self.repo.get_meta("backfill_done:courses") != "1"
            try:
                result = await self.session.poll_once(
                    scores_backfill=scores_backfill,
                    courses_backfill=courses_backfill,
                )
                self.reset_expiry_notification()
                result["mportal"] = await self._scan_mportal(result["current_period"])
            except AuthenticationRequired as error:
                self.session.state = "expired"
                self.repo.record_poll("AUTH_REQUIRED", str(error))
                if not self._expiry_notified:
                    try:
                        delivered = await self.push(
                            "教务系统登录态已失效，轮询已暂停。"
                            "请发送 /教务登录 重新登录；本次失效只提醒一次。"
                        )
                        if delivered and self.session.state == "expired":
                            self._expiry_notified = True
                            self.repo.set_meta("auth_expiry_notified", "1")
                    except Exception:
                        logger.exception("Failed to deliver JWGL session expiry notice")
                return {"success": False, "message": "登录态已失效，需要重新登录。"}
            except Exception as error:
                self.repo.record_poll("ERROR", safe_exception_name(error))
                logger.warning("轮询失败：%s", safe_exception_name(error))
                return {
                    "success": False,
                    "message": f"轮询失败（{safe_exception_name(error)}），本次不算登录态失效。",
                }

            outcome = await self._diff_and_store(
                result, scores_backfill, courses_backfill
            )
            self.repo.record_poll("SUCCESS", f"trigger={trigger}")
            if outcome["backfill"]:
                message = (
                    "已建立教务数据初态："
                    f"成绩 {outcome['scores_total']} 条（{outcome['scores_terms']} 个学期）、"
                    f"课表 {outcome['courses_total']} 条、"
                    f"考试 {outcome['exams_total']} 条。"
                    "之后每轮只查近期学期，新成绩和考试安排会自动推送。"
                )
                await self.push(message)
                return {"success": True, "message": message, "backfill": True}
            score_new, score_changed = outcome["score_changes"]
            exam_new, exam_changed = outcome["exam_changes"]
            if score_new or score_changed or exam_new or exam_changed:
                await self.push(
                    format_changes(
                        {
                            "scores": outcome["score_changes"],
                            "exams": outcome["exam_changes"],
                        },
                        outcome["last_scores_period"],
                    )
                )
                return {
                    "success": True,
                    "message": "本轮有新数据，已推送。",
                    "changed": True,
                }
            return {"success": True, "message": "本轮无新数据。"}

    async def _scan_mportal(self, current_period: str) -> dict[str, Any] | None:
        """Scan mportal for one poll cycle: current + next term, plus backfill.

        On the first successful run all terms from BACKFILL_START up to the
        next term are scanned once, so historical and upcoming weekly
        timetables are built upfront.

        Returns:
            Dict with "term_info", "scans" (period label to week data),
            "backfill" flag and "validate_labels" ([current, next]), or None
            when mportal is disabled/unavailable (logged; the poll itself
            never fails because of mportal).
        """
        if not self.config.get("mportal_enabled", True):
            return None
        mapped = period_to_year_term(current_period)
        if mapped is None:
            logger.warning("无法把当前学期 %r 映射到 mportal 学期代码", current_period)
            return None
        year, term = mapped
        next_year, next_term_label = next_period(year, TERM_CODE_TO_LABEL[term])
        watch_terms = [(year, term), (next_year, TERM_LABEL_TO_CODE[next_term_label])]
        backfill = self.repo.get_meta("backfill_done:mportal") != "1"
        terms = list(watch_terms)
        if backfill:
            terms = [
                (y, TERM_LABEL_TO_CODE[t])
                for y, t in periods_between(
                    BACKFILL_START, (next_year, next_term_label)
                )
            ]
        try:
            result = await self.session.fetch_mportal_timetables(terms)
        except AuthenticationRequired:
            # mportal 的 CAS 会话独立于门户 WebVPN 会话，缺失时只降级，
            # 不影响教务处主流程；提醒用户补一次登录（节流一次）。
            logger.warning("mportal 缺少 CAS 会话，本轮跳过结构化课表扫描")
            if not self._mportal_auth_notified:
                delivered = await self.push(
                    "mportal 结构化课表需要单独登录一次统一认证：请发送 /教务登录，"
                    "按提示再提交一次短信验证码即可，之后会自动保持。"
                )
                if delivered:
                    self._mportal_auth_notified = True
                    self.repo.set_meta("mportal_auth_notified", "1")
            return None
        except Exception as error:
            logger.warning("mportal 全周课表扫描失败：%s", safe_exception_name(error))
            return None
        self._mportal_auth_notified = False
        self.repo.set_meta("mportal_auth_notified", "0")
        return {
            **result,
            "backfill": backfill,
            "validate_labels": [
                period_label(year, TERM_CODE_TO_LABEL[term]),
                period_label(next_year, next_term_label),
            ],
        }

    async def _refresh_term_scan(
        self, label: str, scan: dict[str, Any], validate: bool, courses_changed: bool
    ) -> dict[str, Any] | None:
        """Store one term's mportal scan, rebuilding on change.

        Args:
            label: Period label like "2026 秋".
            scan: {"weeks": ..., "countweek": ...} for the term.
            validate: Whether to cross-validate against the jwgl free text
                (only current/next term; backfilled historical terms are
                stored without validation).
            courses_changed: Whether the jwgl snapshot diff found changes.

        Returns:
            The validation report when one was produced, else None.
        """
        weeks = scan["weeks"]
        digest = hashlib.sha256(
            json.dumps(weeks, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        if digest == self.weekly.mportal_hash(label):
            if not validate or not courses_changed:
                return None
            logger.info("%s mportal 课表未变，但教务文本有改动，重新交叉验证", label)
        else:
            self.weekly.set_mportal_hash(label, digest)
            payload = self.weekly.regenerate_mportal(label, weeks)
            logger.info(
                "%s mportal 课表有改动，已重建按周结构化课表（覆盖 %d 周）",
                label,
                payload["max_week"],
            )
        if not validate:
            return None
        report = self.weekly.validate(label)
        if not report.get("skipped") and report.get("mismatch_weeks"):
            await self.push(format_validation_report(report))
            logger.warning(
                "%s 课表交叉验证失配：%d 周与教务文本解析不一致",
                label,
                len(report["mismatch_weeks"]),
            )
        return report

    async def validate_query_term(
        self, year: str, term: str, structured_records: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        """Cross-validate course records using an already-cached mportal timetable.

        Args:
            year: Four-digit year label.
            term: Term character 春/夏/秋.
            structured_records: Course records with parsed "schedule" lists
                (as returned to the caller; compared against cached mportal).

        Returns:
            A compact report dict, or None when the term is unmappable or
            mportal data is not cached. Query-time validation never uses the
            browser or waits for a background scan.
        """
        if not self.config.get("mportal_enabled", True):
            return None
        code = TERM_LABEL_TO_CODE.get(term)
        if not year.isdigit() or code is None:
            return None
        period = period_label(year, term)
        stored = self.weekly.get_timetable(period)
        if not stored and period == self.repo.get_meta("current_period", ""):
            stored = self.weekly.get_timetable()
        if stored.get("source") != "mportal" or stored.get("period") != period:
            return None
        reference = {int(week): items for week, items in stored["weeks"].items()}
        candidate = weeks_from_structured_records(structured_records)
        report = self.weekly.compare(reference, candidate, period)
        mismatch = report.get("mismatch_weeks", [])
        return {
            "matched": report.get("matched", 0),
            "total_reference": report.get("total_reference", 0),
            "consistent": not mismatch,
            "mismatch_weeks": mismatch,
            "weeks": {str(week): report["weeks"][week] for week in mismatch[:3]},
        }

    async def ensure_term_timetable(self, year: str, term: str) -> dict[str, Any]:
        """Return one term's weekly timetable, live-scanning mportal when missing.

        Poll cycles already scan the current and next term into per-term
        payloads; this serves those cached payloads first and only falls
        back to a live mportal scan for terms that were never scanned,
        caching the result afterwards.

        Args:
            year: Four-digit year label, e.g. "2026".
            term: Term character 春/夏/秋.

        Returns:
            The per-term timetable payload dict.

        Raises:
            JwglError: When the term is invalid, mportal is disabled, the
                term does not exist in mportal, or a login flow is running.
            AuthenticationRequired: When the CAS session has expired.
        """
        code = TERM_LABEL_TO_CODE.get(term)
        if not year.isdigit() or code is None:
            raise JwglError(f"无法识别的学期：{year or '?'} {term or '?'}")
        period = period_label(year, term)
        stored = self.weekly.get_timetable(period)
        if not stored and period == self.repo.get_meta("current_period", ""):
            default = self.weekly.get_timetable()
            if default.get("period") == period:
                stored = default
        if stored:
            return stored
        if not self.config.get("mportal_enabled", True):
            raise JwglError(f"{period} 尚未生成周课表，且 mportal 已停用")
        scan = await self.session.fetch_mportal_timetable(year, code)
        self.weekly.save_term_info(scan["term_info"])
        payload = self.weekly.regenerate_mportal(period, scan["weeks"])
        logger.info("已按查询实时拉取并缓存 %s 的 mportal 周课表", period)
        return payload

    async def validate_now(self) -> dict[str, Any]:
        """Manually cross-validate: live-scan mportal, rebuild if changed.

        Returns:
            Dict with success flag and the validation report or message.
        """
        period = self.repo.get_meta("current_period", "")
        mapped = period_to_year_term(period)
        if mapped is None:
            return {
                "success": False,
                "message": f"无法把当前学期 {period!r} 映射到 mportal 学期代码。",
            }
        year, term = mapped
        try:
            scan = await self.session.fetch_mportal_timetable(year, term)
        except AuthenticationRequired:
            return {"success": False, "message": "登录态已失效，需要先重新登录。"}
        except Exception as error:
            detail = (
                str(error)
                if isinstance(error, JwglError)
                else safe_exception_name(error)
            )
            return {
                "success": False,
                "message": f"mportal 课表扫描失败（{detail}）。",
            }
        self.weekly.save_term_info(scan["term_info"])
        await self._refresh_term_scan(period, scan, validate=True, courses_changed=True)
        report = self.weekly.validate(period)
        return {"success": True, "report": report}

    async def _diff_and_store(
        self,
        result: dict[str, Any],
        scores_backfill: bool,
        courses_backfill: bool,
    ) -> dict[str, Any]:
        score_new: list[dict[str, str]] = []
        score_changed: list[dict[str, str]] = []
        scores_total = 0
        scores_terms = 0
        last_scores_period = self.repo.get_meta("last_scores_period", "")
        for label, records in result["scores"].items():
            _baseline, new_records, changed = self.repo.diff_and_update(
                "scores", records, _score_key(label)
            )
            scores_total += len(records)
            if records:
                scores_terms += 1
                last_scores_period = label  # scores map 按学期先后有序
            if not scores_backfill:
                score_new.extend(new_records)
                score_changed.extend(changed)
        if scores_backfill:
            self.repo.set_meta("backfill_done:scores", "1")
        self.repo.set_meta("last_scores_period", last_scores_period)

        if courses_backfill:
            self.repo.clear_kind("courses")
        courses_total = 0
        courses_changed = courses_backfill
        for label, records in result["courses"].items():
            # 课表变动只入库，不推送
            _baseline, course_new, course_changed = self.repo.diff_and_update(
                "courses", records, _course_key(label)
            )
            # Distinguish a successfully scanned empty term from a cache miss.
            self.repo.set_meta(f"snapshot_period:courses:{label}", "1")
            courses_total += len(records)
            if course_new or course_changed:
                courses_changed = True
        if courses_backfill:
            self.repo.set_meta("backfill_done:courses", "1")

        _baseline, exam_new, exam_changed = self.repo.diff_and_update(
            "exams", result["exams"], _exam_key
        )
        if scores_backfill or courses_backfill:
            exam_new, exam_changed = [], []

        self.repo.set_meta("current_period", result["current_period"])
        mportal = result.get("mportal")
        if mportal is not None:
            self.weekly.save_term_info(mportal["term_info"])
            validate_labels = set(mportal.get("validate_labels", []))
            current_label = result["current_period"]
            for label, scan in mportal["scans"].items():
                await self._refresh_term_scan(
                    label,
                    scan,
                    validate=label in validate_labels,
                    courses_changed=courses_changed and label == current_label,
                )
            if mportal.get("backfill"):
                self.repo.set_meta("backfill_done:mportal", "1")
                logger.info("mportal 历史学期课表回填完成")
        elif courses_changed:
            timetable = self.weekly.regenerate()
            logger.info(
                "课表数据有改动，已按教务文本重新生成周课表（覆盖 %d 周，mportal 不可用）",
                timetable.max_week,
            )
        return {
            "backfill": scores_backfill or courses_backfill,
            "score_changes": (score_new, score_changed),
            "exam_changes": (exam_new, exam_changed),
            "scores_total": scores_total,
            "scores_terms": scores_terms,
            "courses_total": courses_total,
            "exams_total": len(result["exams"]),
            "last_scores_period": last_scores_period,
        }
