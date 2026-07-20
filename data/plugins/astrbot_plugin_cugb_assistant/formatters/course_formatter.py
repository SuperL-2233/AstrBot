from __future__ import annotations

from ..utils.time_utils import format_time, parse_datetime


def format_course(course: dict) -> str:
    start = format_time(parse_datetime(course["start_time"]))
    end = format_time(parse_datetime(course["end_time"]))
    suffix = f" | {course['teacher']}" if course.get("teacher") else ""
    return f"{start}-{end} {course['course_name']} @ {course['location']}{suffix}"


def format_course_list(result: dict, title: str) -> str:
    if not result["success"]:
        return result["message"]
    lines = [title]
    lines.extend(f"{index}. {format_course(course)}" for index, course in enumerate(result["data"], start=1))
    return "\n".join(lines)


def format_single_course(result: dict, fallback_title: str) -> str:
    if not result["success"]:
        return result["message"]
    return f"{fallback_title}\n{format_course(result['data'])}"
