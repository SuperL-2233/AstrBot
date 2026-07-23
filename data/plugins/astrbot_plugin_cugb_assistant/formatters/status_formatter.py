from __future__ import annotations


def format_status(status: dict) -> str:
    course = status["course"]
    classroom = status["classroom"]
    reminder = status["reminder"]
    lines = [
        "校园助手状态",
        f"课程表：{'已导入' if course['imported'] else '未导入'}，共 {course['count']} 条",
        f"空教室：provider={classroom['provider']}，最近更新={classroom['last_update_time'] or '无'}",
        f"提醒：课前提醒={'开启' if reminder['class_reminder_enabled'] else '关闭'}",
    ]
    return "\n".join(lines)
