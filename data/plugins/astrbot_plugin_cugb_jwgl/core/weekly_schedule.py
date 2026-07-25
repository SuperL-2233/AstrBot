from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_WEEK_SPEC_RE = re.compile(r"第?(\d+)(?:-(\d+))?周(单|双)?")
_WEEKDAY_RE = re.compile(r"星期([一二三四五六日天])")
_SECTION_RE = re.compile(r"第(\d+)(?:-(\d+))?节")

_WEEKDAY_MAP = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7}
WEEKDAY_LABELS = {
    1: "星期一",
    2: "星期二",
    3: "星期三",
    4: "星期四",
    5: "星期五",
    6: "星期六",
    7: "星期日",
}


@dataclass(frozen=True, slots=True)
class ScheduleEntry:
    """One parsed segment of the free-text schedule field.

    Attributes:
        weeks: Sorted teaching week numbers this segment applies to.
        weekday: ISO weekday (1=Monday .. 7=Sunday), None when the segment
            only carries week information without a weekday.
        sections: Sorted class period numbers (节次), empty when unknown.
        location: Classroom or building text, empty when unknown.
    """

    weeks: list[int]
    weekday: int | None
    sections: list[int]
    location: str


@dataclass(slots=True)
class WeeklyTimetable:
    """Course occurrences grouped by teaching week.

    Attributes:
        weeks: Mapping of week number to that week's occurrence list,
            each sorted by weekday then first section.
        max_week: Highest week number present, 0 when empty.
        unparsed: Course names whose schedule text could not be parsed at all.
    """

    weeks: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    max_week: int = 0
    unparsed: list[str] = field(default_factory=list)


def _expand_weeks(start: int, end: int, parity: str | None) -> list[int]:
    weeks = list(range(start, end + 1))
    if parity == "单":
        weeks = [week for week in weeks if week % 2 == 1]
    elif parity == "双":
        weeks = [week for week in weeks if week % 2 == 0]
    return weeks


def parse_schedule_text(text: str) -> list[ScheduleEntry]:
    """Parse the jwgl "上课时间、地点" free-text field into schedule segments.

    Supported forms include "1-17周 星期一 第3-4节 综合楼402",
    "1-9周单 星期二 第8-9节 综合楼703" (odd weeks), "第14周 星期二 第6-9节"
    (single week, multi-period), and trailing week-only fragments such as
    "14-15周" which yield an entry with unknown weekday/sections/location.

    Args:
        text: Raw schedule cell text, segments separated by whitespace.

    Returns:
        List of parsed segments in order of appearance; empty when the text
        contains no recognizable week specification.
    """
    matches = list(_WEEK_SPEC_RE.finditer(text))
    entries: list[ScheduleEntry] = []
    for index, match in enumerate(matches):
        body_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.end() : body_end]
        start = int(match.group(1))
        end = int(match.group(2) or match.group(1))
        weekday_match = _WEEKDAY_RE.search(body)
        sections: list[int] = []
        for section_match in _SECTION_RE.finditer(body):
            first = int(section_match.group(1))
            last = int(section_match.group(2) or section_match.group(1))
            sections.extend(range(first, last + 1))
        location = _WEEKDAY_RE.sub("", body)
        location = _SECTION_RE.sub("", location)
        location = re.sub(r"\s+", "", location)
        entries.append(
            ScheduleEntry(
                weeks=_expand_weeks(start, end, match.group(3)),
                weekday=_WEEKDAY_MAP[weekday_match.group(1)] if weekday_match else None,
                sections=sorted(set(sections)),
                location=location,
            )
        )
    return entries


def section_label(sections: list[int]) -> str:
    """Format a sorted section list like [6, 7, 8, 9] as "第6-9节"."""
    if not sections:
        return ""
    if len(sections) == 1:
        return f"第{sections[0]}节"
    return f"第{sections[0]}-{sections[-1]}节"


def occurrence_label(occurrence: dict[str, Any]) -> str:
    """Render one occurrence compactly for validation reports."""
    parts = [occurrence.get("weekday_label", ""), occurrence.get("section_label", "")]
    if not any(parts):
        weekday = occurrence.get("weekday")
        parts = [WEEKDAY_LABELS.get(weekday or 0, "时间待定")]
        label = section_label(occurrence.get("sections") or [])
        if label:
            parts.append(label)
    head = " ".join(part for part in parts if part)
    name = occurrence.get("course_name", "?")
    location = occurrence.get("location", "")
    location_text = f" @ {location}" if location else ""
    return f"{head} {name}{location_text}".strip()


def _occurrence_key(occurrence: dict[str, Any]) -> tuple:
    sections = occurrence.get("sections") or []
    return (
        occurrence.get("weekday") or 0,
        sections[0] if sections else 0,
        re.sub(r"\s+", "", str(occurrence.get("course_name", ""))),
        re.sub(r"\s+", "", str(occurrence.get("location", ""))),
    )


def _sort_week_occurrences(occurrences: list[dict[str, Any]]) -> None:
    occurrences.sort(
        key=lambda item: (
            item["weekday"] or 8,
            item["sections"][0] if item["sections"] else 99,
            item["course_name"],
        )
    )


def weeks_from_structured_records(
    records: list[dict[str, Any]],
) -> dict[int, list[dict[str, Any]]]:
    """Expand structured course records back into per-week occurrences.

    Inverse of structure_course_record: each record's "schedule" segments
    carry weeks/weekday/sections/location, which are expanded into the same
    occurrence shape used by the weekly timetable for comparison.

    Args:
        records: Course records with a parsed "schedule" list.

    Returns:
        Mapping of week number to sorted occurrence list.
    """
    weeks: dict[int, list[dict[str, Any]]] = {}
    for record in records:
        for segment in record.get("schedule", []):
            sections = segment.get("sections") or []
            occurrence = {
                "course_name": record.get("课程名称") or record.get("course_name", "?"),
                "course_code": record.get("课程号") or record.get("course_code", ""),
                "teacher": record.get("任课教师") or record.get("teacher", ""),
                "weekday": segment.get("weekday"),
                "weekday_label": segment.get(
                    "weekday_label",
                    WEEKDAY_LABELS.get(segment.get("weekday") or 0, "时间待定"),
                ),
                "sections": sections,
                "section_label": segment.get("section_label")
                or section_label(sections),
                "location": segment.get("location", ""),
            }
            for week in segment.get("weeks", []):
                weeks.setdefault(int(week), []).append(occurrence)
    for occurrences in weeks.values():
        _sort_week_occurrences(occurrences)
    return weeks


def compare_weeks(
    reference: dict[int, list[dict[str, Any]]],
    candidate: dict[int, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Cross-validate two weekly timetables week by week.

    Args:
        reference: Authoritative weeks (mportal structured data).
        candidate: Weeks to check (parsed from the jwgl free text).

    Returns:
        Report dict with matched count, total reference count, and per-week
        "only_in_reference" / "only_in_candidate" occurrence labels for weeks
        that differ. "mismatch_weeks" lists the differing week numbers.
    """
    report: dict[str, Any] = {
        "matched": 0,
        "total_reference": 0,
        "mismatch_weeks": [],
        "weeks": {},
    }
    for week in sorted(set(reference) | set(candidate)):
        ref_occurrences = reference.get(week, [])
        candidate_keys = {_occurrence_key(item) for item in candidate.get(week, [])}
        reference_keys = {_occurrence_key(item) for item in ref_occurrences}
        report["total_reference"] += len(ref_occurrences)
        matched = len(reference_keys & candidate_keys)
        report["matched"] += matched
        only_in_reference = [
            occurrence_label(item)
            for item in ref_occurrences
            if _occurrence_key(item) not in candidate_keys
        ]
        only_in_candidate = [
            occurrence_label(item)
            for item in candidate.get(week, [])
            if _occurrence_key(item) not in reference_keys
        ]
        if only_in_reference or only_in_candidate:
            report["mismatch_weeks"].append(week)
            report["weeks"][week] = {
                "only_in_reference": only_in_reference,
                "only_in_candidate": only_in_candidate,
            }
    return report


def structure_course_record(record: dict[str, str]) -> dict[str, Any]:
    """Replace the raw schedule free text of a course record with parsed segments.

    Args:
        record: Raw course record containing the 上课时间、地点 field.

    Returns:
        Copy of the record without the 上课时间、地点 free text, plus a
        "schedule" list whose items carry weeks, weekday, weekday_label,
        sections, section_label and location.
    """
    structured: dict[str, Any] = {
        key: value for key, value in record.items() if key != "上课时间、地点"
    }
    structured["schedule"] = [
        {
            "weeks": entry.weeks,
            "weekday": entry.weekday,
            "weekday_label": WEEKDAY_LABELS.get(entry.weekday or 0, "时间待定"),
            "sections": entry.sections,
            "section_label": section_label(entry.sections),
            "location": entry.location,
        }
        for entry in parse_schedule_text(record.get("上课时间、地点", ""))
    ]
    return structured


def build_weekly_timetable(records: list[dict[str, str]]) -> WeeklyTimetable:
    """Expand jwgl course snapshot records into one timetable per week.

    Args:
        records: Course records as stored by the snapshot repository; each
            should carry 课程名称/课程号/任课教师 and the 上课时间、地点 field.

    Returns:
        WeeklyTimetable with occurrences keyed by week number. Each
        occurrence dict has course_name, course_code, teacher, weekday
        (None when unknown), weekday_label, sections, section_label and
        location keys.
    """
    timetable = WeeklyTimetable()
    for record in records:
        text = record.get("上课时间、地点", "")
        entries = parse_schedule_text(text)
        course_name = record.get("课程名称", "?")
        if not entries:
            timetable.unparsed.append(course_name)
            continue
        for entry in entries:
            occurrence = {
                "course_name": course_name,
                "course_code": record.get("课程号", ""),
                "teacher": record.get("任课教师", ""),
                "weekday": entry.weekday,
                "weekday_label": WEEKDAY_LABELS.get(entry.weekday or 0, "时间待定"),
                "sections": entry.sections,
                "section_label": section_label(entry.sections),
                "location": entry.location,
            }
            for week in entry.weeks:
                timetable.weeks.setdefault(week, []).append(occurrence)
                timetable.max_week = max(timetable.max_week, week)
    for occurrences in timetable.weeks.values():
        occurrences.sort(
            key=lambda item: (
                item["weekday"] or 8,
                item["sections"][0] if item["sections"] else 99,
                item["course_name"],
            )
        )
    return timetable
