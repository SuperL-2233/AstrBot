from __future__ import annotations


def format_homework_list(result: dict, title: str = "作业") -> str:
    if not result["success"]:
        return result["message"]
    rows = result["data"]
    if not rows:
        return "当前没有未完成作业。"
    lines = [title]
    for index, item in enumerate(rows, start=1):
        lines.append(f"{index}. {item['course_name']} - {item['title']}，截止 {item['deadline']}")
    return "\n".join(lines)
