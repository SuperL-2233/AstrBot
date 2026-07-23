from __future__ import annotations


def format_tasks(result: dict, title: str) -> str:
    if not result["success"]:
        return result["message"]
    if not result["data"]:
        return f"{title}\n{result['message']}\n没有符合条件的任务。"
    lines = [title, result["message"]]
    for index, task in enumerate(result["data"], 1):
        deadline = task.get("deadline") or "平台未提供明确截止时间"
        status = "已完成" if task.get("status") == "completed" else "待办"
        lines.append(
            f"{index}. [{task['task_type']}][{status}] "
            f"{task['course_name']} - {task['title']}\n   截止：{deadline}"
        )
    return "\n".join(lines)


def format_status(task_status: dict, reminder_status: dict, loop_running: bool) -> str:
    counts = task_status.get("counts", {})
    cache = task_status.get("cache", {})
    offsets = ", ".join(str(value) for value in reminder_status["offsets_hours"])
    return "\n".join(
        (
            "超星学习通状态",
            f"在线同步：{'已启用' if task_status['enabled'] else '未启用'}",
            f"凭据：{'已配置' if task_status['credentials_configured'] else '未配置'}",
            f"同步任务：{'运行中' if loop_running else '未运行'}",
            f"最近同步：{task_status.get('last_update_time') or '无'}",
            f"缓存状态：{'已过期' if cache.get('stale', True) else '有效'}，年龄 {cache.get('age_hours') if cache.get('age_hours') is not None else '未知'} 小时",
            f"缓存：作业 {counts.get('作业', 0)}，视频 {counts.get('视频', 0)}，讨论 {counts.get('讨论', 0)}",
            f"提醒：{'已启用' if reminder_status['enabled'] else '未启用'}，目标{'已绑定' if reminder_status['target_bound'] else '未绑定'}",
            f"提醒阶段（小时）：{offsets}；每 {reminder_status['scan_interval_minutes']} 分钟扫描",
            f"最近错误：{task_status.get('last_error') or '无'}",
        )
    )
