from __future__ import annotations

from typing import Any

from ..core.jwgl_core import AuthenticationRequired, JwglError
from ..core.mportal_client import TERM_LABEL_TO_CODE
from ..repositories.snapshot_repo import SnapshotRepository
from .session_service import JwglSessionService
from .weekly_schedule_service import WeeklyScheduleService


class AcademicQueryService:
    """Serve academic snapshots without waiting for the shared browser session."""

    def __init__(
        self,
        session: JwglSessionService,
        repo: SnapshotRepository,
        weekly: WeeklyScheduleService,
    ) -> None:
        self.session = session
        self.repo = repo
        self.weekly = weekly

    async def query(
        self,
        kind: str,
        year: str = "",
        term: str = "",
        scores_probe_max_terms: int = 6,
    ) -> dict[str, Any]:
        """Read the requested period from SQLite, querying online only on a miss.

        Args:
            kind: One of courses, scores or exams.
            year: Optional natural calendar year, paired with term.
            term: Optional spring, summer or autumn label in Chinese.
            scores_probe_max_terms: Maximum periods to inspect for live scores.

        Returns:
            Records, their actual period and a snapshot/live source label.
            Unavailable periods return an explicit failure without mixing courses
            from another period. Failed score queries may use the latest cached
            score period, identified in the response.
        """
        year, term = year.strip(), term.strip()
        if kind not in {"courses", "scores", "exams"}:
            return {
                "success": False,
                "records": [],
                "message": "不支持的教务查询类型。",
            }
        if (year or term) and (
            len(year) != 4 or not year.isdigit() or term not in TERM_LABEL_TO_CODE
        ):
            return {
                "success": False,
                "records": [],
                "message": "year 和 term 必须同时提供，例如 year=2026、term=秋。",
            }

        period = f"{year} {term}" if year else ""
        if not period and kind != "exams":
            period = self.repo.get_meta(
                "last_scores_period" if kind == "scores" else "current_period", ""
            )
        if kind == "courses":
            records = self.weekly.snapshot_records(period) if period else []
            cached = bool(records) or bool(
                period
                and self.repo.get_meta(f"snapshot_period:courses:{period}") == "1"
            )
        elif kind == "scores":
            score_year, _, score_term = period.partition(" ")
            records = [
                record
                for key, record in self.repo.items_with_key("scores")
                if not period
                or key.startswith((f"{score_year}|{score_term}|", f"{period}|"))
                or (
                    record.get("学年") == score_year
                    and record.get("学期") == score_term
                )
            ]
            cached = bool(records) or (
                not year
                and self.repo.get_meta("baseline:scores") == "1"
                and not self.repo.count("scores")
            )
        else:
            records = self.repo.items("exams")
            cached = bool(records) or self.repo.get_meta("baseline:exams") == "1"
        if cached:
            return {"records": records, "period": period, "source": "snapshot"}

        # A cache hit returns above, before attempting to acquire the browser lock.
        live_year, live_term = year, term
        if kind == "courses" and period:
            live_year, _, live_term = period.partition(" ")
        try:
            result = await self.session.query_live(
                kind, live_year, live_term, scores_probe_max_terms
            )
            return {
                **result,
                "period": result.get("period") or period,
                "source": "live",
            }
        except (AuthenticationRequired, JwglError) as error:
            if isinstance(error, AuthenticationRequired):
                self.session.state = "expired"
            note = (
                "登录态已失效"
                if isinstance(error, AuthenticationRequired)
                else f"实时查询失败（{error}）"
            )
            if kind == "scores":
                latest = self.repo.get_meta("last_scores_period", "")
                if latest and latest != period:
                    score_year, _, score_term = latest.partition(" ")
                    records = [
                        record
                        for key, record in self.repo.items_with_key("scores")
                        if key.startswith((f"{score_year}|{score_term}|", f"{latest}|"))
                        or (
                            record.get("学年") == score_year
                            and record.get("学期") == score_term
                        )
                    ]
                    if records:
                        return {
                            "records": records,
                            "period": latest,
                            "source": "snapshot",
                            "note": f"{note}，所查学期无缓存，返回 {latest} 的成绩快照。",
                        }
            return {
                "success": False,
                "records": [],
                "period": period,
                "source": "unavailable",
                "note": f"{note}，所查数据尚无本地缓存。",
            }
