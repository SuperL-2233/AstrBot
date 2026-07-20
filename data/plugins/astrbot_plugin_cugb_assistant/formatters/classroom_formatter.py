from __future__ import annotations


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
