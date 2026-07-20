from __future__ import annotations


def format_status(status: dict) -> str:
    course = status["course"]
    classroom = status["classroom"]
    chaoxing = status["chaoxing"]
    reminder = status["reminder"]
    agent_notification = status["agent_notification"]
    lines = [
        "校园助手状态",
        f"课程表：{'已导入' if course['imported'] else '未导入'}，共 {course['count']} 条",
        f"空教室：provider={classroom['provider']}，最近更新={classroom['last_update_time'] or '无'}",
        f"学习通：{'已启用' if chaoxing['enabled'] else '未启用'}，provider={chaoxing['provider']}",
        f"提醒：课前提醒={'开启' if reminder['class_reminder_enabled'] else '关闭'}，作业提醒={'开启' if reminder['homework_reminder_enabled'] else '关闭'}",
        f"开发通知：{'已监听' if agent_notification['listener_started'] else '未监听'}，目标={'已绑定' if agent_notification['target_bound'] else '未绑定'}",
    ]
    return "\n".join(lines)
