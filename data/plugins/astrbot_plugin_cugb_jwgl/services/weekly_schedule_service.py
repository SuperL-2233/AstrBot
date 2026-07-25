from __future__ import annotations

import json
from typing import Any

from ..core.jwgl_core import utc_iso
from ..core.weekly_schedule import (
    WEEKDAY_LABELS,
    WeeklyTimetable,
    build_weekly_timetable,
    compare_weeks,
    section_label,
)
from ..repositories.snapshot_repo import SnapshotRepository

_META_KEY = "weekly_timetable"
_TERM_INFO_KEY = "mportal_term_info"
_VALIDATION_KEY = "weekly_validation_report"


def _term_key(period: str) -> str:
    return f"weekly_timetable:{period}"


def _term_hash_key(period: str) -> str:
    return f"mportal_weeks_hash:{period}"


class WeeklyScheduleService:
    """Generates, persists and serves the structured per-week timetable."""

    def __init__(self, repo: SnapshotRepository) -> None:
        self.repo = repo

    def snapshot_records(self, period: str) -> list[dict[str, str]]:
        """Return course snapshot records whose keys belong to one period."""
        prefix = f"{period}|"
        return [
            record
            for key, record in self.repo.items_with_key("courses")
            if key.startswith(prefix)
        ]

    def current_records(self, strict: bool = False) -> tuple[str, list[dict[str, str]]]:
        """Return the current period label and its course snapshot records.

        Args:
            strict: When True, do not fall back to all stored records if no
                key matches the current period (used by cross-validation to
                avoid mixing terms).

        Returns:
            Tuple of (period label, course records).
        """
        period = self.repo.get_meta("current_period", "")
        rows = self.repo.items_with_key("courses")
        if not period:
            return period, [record for _, record in rows]
        # _course_key 以原始学期标签（含空格）作前缀，如 "2026 秋|课程号|..."
        matched = self.snapshot_records(period)
        if strict:
            return period, matched
        return period, matched or [record for _, record in rows]

    def _persist(self, payload: dict[str, Any]) -> None:
        self.repo.set_meta(_META_KEY, json.dumps(payload, ensure_ascii=False))

    def regenerate(self) -> WeeklyTimetable:
        """Fallback path: rebuild the weekly timetable from jwgl free text.

        Returns:
            The freshly built WeeklyTimetable.
        """
        period, records = self.current_records()
        timetable = build_weekly_timetable(records)
        payload = {
            "period": period,
            "generated_at": utc_iso(),
            "source": "jwgl_freetext",
            "max_week": timetable.max_week,
            "unparsed": timetable.unparsed,
            "weeks": {
                str(week): entries for week, entries in sorted(timetable.weeks.items())
            },
        }
        self._persist(payload)
        return timetable

    def regenerate_mportal(
        self, period: str, week_data: dict[int, dict[str, Any]]
    ) -> dict[str, Any]:
        """Primary path: build the weekly timetable from mportal week scans.

        The payload is stored under the term-specific key, and also under
        the default key when the period is the school's current period.

        Args:
            period: Period label like "2026 秋".
            week_data: Mapping of week number to the normalized get-datatmp
                payload ({"weekdays": [...], "occurrences": [...]}).

        Returns:
            The persisted payload, including week_dates and per-occurrence
            ISO dates.
        """
        weeks: dict[str, list[dict[str, Any]]] = {}
        week_dates: dict[str, list[str]] = {}
        for week in sorted(week_data):
            data = week_data[week]
            if data.get("weekdays"):
                week_dates[str(week)] = list(data["weekdays"])
            occurrences = []
            for item in data.get("occurrences", []):
                occurrences.append(
                    {
                        "course_name": item.get("course_name", "?"),
                        "course_code": item.get("course_code", ""),
                        "teacher": item.get("teacher", ""),
                        "weekday": item.get("weekday"),
                        "weekday_label": WEEKDAY_LABELS.get(
                            item.get("weekday") or 0, "时间待定"
                        ),
                        "sections": item.get("sections", []),
                        "section_label": section_label(item.get("sections", [])),
                        "location": item.get("location", ""),
                        "date": item.get("date", ""),
                    }
                )
            occurrences.sort(
                key=lambda entry: (
                    entry["weekday"] or 8,
                    entry["sections"][0] if entry["sections"] else 99,
                    entry["course_name"],
                )
            )
            if occurrences:
                weeks[str(week)] = occurrences
        payload = {
            "period": period,
            "generated_at": utc_iso(),
            "source": "mportal",
            "max_week": max(week_data, default=0),
            "unparsed": [],
            "weeks": weeks,
            "week_dates": week_dates,
        }
        encoded = json.dumps(payload, ensure_ascii=False)
        self.repo.set_meta(_term_key(period), encoded)
        if period == self.repo.get_meta("current_period", ""):
            self.repo.set_meta(_META_KEY, encoded)
        return payload

    def mportal_hash(self, period: str) -> str:
        """Return the stored mportal scan hash for one period."""
        return self.repo.get_meta(_term_hash_key(period), "")

    def set_mportal_hash(self, period: str, digest: str) -> None:
        """Persist the mportal scan hash for one period."""
        self.repo.set_meta(_term_hash_key(period), digest)

    def get_timetable(self, period: str = "") -> dict[str, Any]:
        """Load a persisted weekly timetable, generating the default when missing.

        Args:
            period: When given, load the term-specific payload (empty dict
                when the term was never scanned). Otherwise load the default
                (current period) payload, lazily generating it from the jwgl
                free text when absent.

        Returns:
            The timetable payload dict (may be empty for unknown terms).
        """
        if period:
            raw = self.repo.get_meta(_term_key(period), "")
            if raw:
                try:
                    stored = json.loads(raw)
                except ValueError:
                    stored = None
                if isinstance(stored, dict) and "weeks" in stored:
                    return stored
            return {}
        raw = self.repo.get_meta(_META_KEY, "")
        if raw:
            try:
                stored = json.loads(raw)
            except ValueError:
                stored = None
            if isinstance(stored, dict) and "weeks" in stored:
                return stored
        self.regenerate()
        return json.loads(self.repo.get_meta(_META_KEY, "{}"))

    def get_week(self, week: int) -> tuple[str, list[dict[str, Any]]]:
        """Return the period label and one week's occurrences from storage.

        Args:
            week: Teaching week number.

        Returns:
            Tuple of (period label, occurrence list for that week).
        """
        stored = self.get_timetable()
        return stored.get("period", ""), stored.get("weeks", {}).get(str(week), [])

    def save_term_info(self, term_info: dict[str, Any]) -> None:
        """Cache the mportal get-index termInfo payload."""
        self.repo.set_meta(
            _TERM_INFO_KEY,
            json.dumps(
                {"fetched_at": utc_iso(), "data": term_info}, ensure_ascii=False
            ),
        )

    def get_term_info(self) -> dict[str, Any] | None:
        """Return the cached mportal termInfo payload, or None."""
        raw = self.repo.get_meta(_TERM_INFO_KEY, "")
        if not raw:
            return None
        try:
            cached = json.loads(raw)
        except ValueError:
            return None
        data = cached.get("data") if isinstance(cached, dict) else None
        return data if isinstance(data, dict) else None

    def term_start(self, year: str, term: int) -> str:
        """Look up one term's startday in the cached mportal termInfo.

        Returns:
            ISO date string of the term's first day, or "" when unknown.
        """
        term_info = self.get_term_info() or {}
        for entry in term_info.get("termInfo", []):
            if str(entry.get("year")) == year and int(entry.get("term", -1)) == term:
                return str(entry.get("startday", ""))
        return ""

    def validate(self, period: str = "") -> dict[str, Any]:
        """Cross-validate the stored timetable against the jwgl free text.

        Args:
            period: Term to validate; defaults to the school's current
                period. The term's mportal payload is compared against the
                snapshot records stored under the same term label.

        Returns:
            The compare_weeks report plus period and checked_at timestamps;
            also persisted to the snapshot database when validating the
            current period. When the term's stored timetable is not
            mportal-sourced, the report is marked skipped.
        """
        current = self.repo.get_meta("current_period", "")
        period = period or current
        stored = self.get_timetable(period)
        if stored.get("source") != "mportal":
            report = {
                "period": period,
                "checked_at": utc_iso(),
                "skipped": True,
                "reason": "该学期周课表不是 mportal 数据源，未执行交叉验证",
            }
            if period == current:
                self.repo.set_meta(
                    _VALIDATION_KEY, json.dumps(report, ensure_ascii=False)
                )
            return report
        reference = {int(week): items for week, items in stored["weeks"].items()}
        candidate = build_weekly_timetable(self.snapshot_records(period)).weeks
        report = compare_weeks(reference, candidate)
        report["period"] = period
        report["checked_at"] = utc_iso()
        report["skipped"] = False
        if period == current:
            self.repo.set_meta(_VALIDATION_KEY, json.dumps(report, ensure_ascii=False))
        return report

    def compare(
        self,
        reference: dict[int, list[dict[str, Any]]],
        candidate: dict[int, list[dict[str, Any]]],
        period: str,
    ) -> dict[str, Any]:
        """Compare two weekly timetables and stamp the report with metadata.

        Args:
            reference: Authoritative weeks (mportal structured data).
            candidate: Weeks to check (parsed from the jwgl free text).
            period: Period label for the report.

        Returns:
            The compare_weeks report with period and checked_at fields.
        """
        report = compare_weeks(reference, candidate)
        report["period"] = period
        report["checked_at"] = utc_iso()
        report["skipped"] = False
        return report

    def get_validation_report(self) -> dict[str, Any] | None:
        """Return the last persisted cross-validation report, or None."""
        raw = self.repo.get_meta(_VALIDATION_KEY, "")
        if not raw:
            return None
        try:
            report = json.loads(raw)
        except ValueError:
            return None
        return report if isinstance(report, dict) else None
