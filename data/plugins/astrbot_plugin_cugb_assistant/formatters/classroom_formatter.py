from __future__ import annotations

import json


def format_classroom_tool_result(result: dict, limit: int = 0) -> str:
    """Serialize classrooms with shared metadata outside individual rows.

    Args:
        result: Complete service result, including any source failure.
        limit: Maximum recommendations to return; zero keeps all rows.

    Returns:
        Compact JSON preserving room order, source status and query metadata.
    """
    payload = {
        key: value for key, value in result.items() if value is not None and value != {}
    }
    if result["success"]:
        rows = [
            {key: value for key, value in room.items() if key != "priority"}
            for room in result["data"]
        ]
        shared = {}
        for key in ("start_time", "end_time", "source_update_time", "source"):
            if (
                rows
                and key in rows[0]
                and all(key in row and row[key] == rows[0][key] for row in rows)
            ):
                shared[key] = rows[0][key]
                for row in rows:
                    del row[key]
        if shared:
            payload["query"] = shared
        payload["data"] = rows[:limit] if limit else rows
        if limit:
            payload["total_count"] = len(rows)
            payload["returned_count"] = len(payload["data"])
            payload["has_more"] = len(rows) > limit
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def format_classroom_list(result: dict, title: str = "空教室") -> str:
    if not result["success"]:
        return result["message"]
    rows = result["data"]
    if not rows:
        return "当前没有可用空教室。"
    lines = [title]
    for index, room in enumerate(rows, start=1):
        lines.append(f"{index}. {room['building']}{room['room']}")
    return "\n".join(lines)


def format_recommendations(rows: list[dict]) -> str:
    if not rows:
        return "当前没有可推荐的自习教室。"
    lines = ["当前推荐："]
    for index, room in enumerate(rows[:5], start=1):
        lines.append(f"{index}. {room['building']}{room['room']} | {room['reason']}")
    return "\n".join(lines)
