from __future__ import annotations

from typing import Any

_MAX_ROWS = 20


def _clip(lines: list[str], total: int) -> str:
    text = "\n".join(lines[:_MAX_ROWS])
    if total > _MAX_ROWS:
        text += f"\n……等共 {total} 条"
    return text


def format_course(record: dict[str, str]) -> str:
    teacher = record.get("任课教师", "")
    suffix = f"（{teacher}）" if teacher else ""
    return f"{record.get('课程名称', '?')}{suffix}"


def format_score(record: dict[str, str]) -> str:
    name = record.get("课程名") or record.get("课程名称", "?")
    score = record.get("总评", "?")
    credit = record.get("学分", "")
    credit_text = f" {credit}学分" if credit else ""
    year = record.get("学年", "")
    term = record.get("学期", "")
    period_text = f"【{year}{term}】" if year or term else ""
    return f"{period_text}{name}：{score}{credit_text}"


def format_exam(record: dict[str, str]) -> str:
    name = record.get("课程名称", "?")
    time = record.get("考试时间", "?")
    place = record.get("考试地点") or record.get("考场", "")
    place_text = f" @ {place}" if place else ""
    return f"{name}：{time}{place_text}"


def format_week_courses(
    week: int, entries: list[dict[str, Any]], period: str = ""
) -> str:
    """Render one week's timetable as a plain-text message."""
    header = f"第{week}周课表（{period}）" if period else f"第{week}周课表"
    if not entries:
        return f"{header}\n本周没有课程。"
    lines: list[str] = []
    for entry in entries:
        teacher = entry.get("teacher", "")
        teacher_text = f"（{teacher}）" if teacher else ""
        location = entry.get("location", "")
        location_text = f" @ {location}" if location else ""
        time_text = " ".join(
            part
            for part in [entry.get("weekday_label", ""), entry.get("section_label", "")]
            if part
        )
        lines.append(
            f"{time_text} {entry.get('course_name', '?')}{teacher_text}{location_text}"
        )
    return f"{header}\n{_clip(lines, len(entries))}"


def format_weekly_overview(
    weeks: dict[int, list[dict[str, Any]]], period: str = ""
) -> str:
    """Summarize per-week course counts and hint how to query one week."""
    header = f"课表周次总览（{period}）" if period else "课表周次总览"
    if not weeks:
        return f"{header}\n暂无可解析的周次课表数据。"
    lines = [f"第{week}周：{len(weeks[week])} 节次" for week in sorted(weeks)]
    lines.append("发送 /教务周课表 <周次> 查看该周课表")
    return f"{header}\n{_clip(lines, len(lines))}"


def format_class_reminder(entry: dict[str, Any], start_time: str, minutes: int) -> str:
    """Render a pre-class reminder message.

    Args:
        entry: One weekly-timetable occurrence with course fields.
        start_time: Class start clock time as HH:MM.
        minutes: Minutes remaining until the class starts.

    Returns:
        The reminder text including the course, time and location.
    """
    teacher = entry.get("teacher", "")
    teacher_text = f"（{teacher}）" if teacher else ""
    lines = [
        f"上课提醒：约 {minutes} 分钟后开始",
        f"{entry.get('course_name', '?')}{teacher_text}",
    ]
    time_parts = [start_time, entry.get("section_label", "")]
    lines.append("时间：" + " ".join(part for part in time_parts if part))
    location = entry.get("location", "")
    if location:
        lines.append(f"地点：{location}")
    return "\n".join(lines)


def format_validation_report(report: dict[str, Any], week: int = 0) -> str:
    """Render a cross-validation report between mportal and jwgl free text.

    Args:
        report: The compare_weeks report (plus period/checked_at fields).
        week: Optional week number to restrict the output to.
    """
    period = report.get("period", "")
    header = f"课表交叉验证（{period}）" if period else "课表交叉验证"
    if report.get("skipped"):
        return f"{header}\n{report.get('reason', '本次未执行。')}"
    mismatch_weeks = report.get("mismatch_weeks", [])
    if week:
        mismatch_weeks = [item for item in mismatch_weeks if item == week]
    if not mismatch_weeks:
        scope = f"第{week}周" if week else "全部周"
        return (
            f"{header}\n{scope}与 mportal 结构化课表一致"
            f"（匹配 {report.get('matched', 0)} 节次）。"
        )
    lines = [
        header,
        f"共 {len(mismatch_weeks)} 周不一致（匹配 {report.get('matched', 0)}"
        f"/{report.get('total_reference', 0)} 节次）：",
    ]
    for number in mismatch_weeks:
        detail = report.get("weeks", {}).get(number, {})
        # 周次键在 JSON 持久化后可能是字符串
        if not detail:
            detail = report.get("weeks", {}).get(str(number), {})
        lines.append(f"【第{number}周】")
        for label in detail.get("only_in_reference", []):
            lines.append(f"  教务文本缺少：{label}")
        for label in detail.get("only_in_candidate", []):
            lines.append(f"  mportal 缺少：{label}")
    return _clip(lines, len(lines))


_FORMATTERS = {
    "courses": format_course,
    "scores": format_score,
    "exams": format_exam,
}

_KIND_LABELS = {
    "courses": "课表",
    "scores": "成绩",
    "exams": "考试安排",
}


def format_records(kind: str, records: list[dict[str, str]], period: str = "") -> str:
    label = _KIND_LABELS.get(kind, kind)
    if not records:
        return f"暂无{label}数据。"
    header = f"{label}（{period}）" if period else label
    lines = [_FORMATTERS[kind](record) for record in records]
    return f"{header}\n{_clip(lines, len(records))}"


def format_changes(
    changes: dict[str, tuple[list[dict[str, str]], list[dict[str, str]]]],
    scores_period: str = "",
) -> str:
    sections: list[str] = []
    for kind, (new_records, changed_records) in changes.items():
        label = _KIND_LABELS.get(kind, kind)
        formatter = _FORMATTERS[kind]
        if new_records:
            lines = [formatter(record) for record in new_records]
            sections.append(f"【新增{label}】\n{_clip(lines, len(new_records))}")
        if changed_records:
            lines = [formatter(record) for record in changed_records]
            sections.append(f"【{label}更新】\n{_clip(lines, len(changed_records))}")
    period_note = f"\n（成绩学期：{scores_period}）" if scores_period else ""
    return "教务系统有新数据：" + period_note + "\n\n" + "\n\n".join(sections)


def format_status(status: dict[str, Any]) -> str:
    state_labels = {
        "unknown": "未知（尚未登录）",
        "active": "登录态有效",
        "expired": "登录态已失效",
    }
    lines = [
        "CUGB 教务监控状态",
        f"登录态：{state_labels.get(status.get('state', 'unknown'), status.get('state'))}",
        f"轮询循环：{'运行中' if status.get('poll_loop_running') else '未运行'}",
        f"推送绑定：{'已绑定' if status.get('target_bound') else '未绑定'}",
    ]
    if status.get("login_pending"):
        stage = status.get("login_stage")
        stage_label = "mportal 结构化课表" if stage == "mportal" else "教务门户"
        lines.append(f"登录流程：等待{stage_label}短信验证码提交")
    last_poll = status.get("last_poll")
    if last_poll:
        lines.append(
            f"最近轮询：{last_poll.get('started_at', '?')} {last_poll.get('status', '?')}"
        )
        detail = last_poll.get("detail") or ""
        if detail:
            lines.append(f"轮询详情：{detail}")
    else:
        lines.append("最近轮询：无")
    period = status.get("last_scores_period", "")
    if period:
        lines.append(f"成绩学期：{period}")
    current = status.get("current_period", "")
    if current:
        lines.append(f"当前学期：{current}")
    source_labels = {
        "mportal": "mportal 结构化课表",
        "jwgl_freetext": "教务文本解析（mportal 不可用）",
    }
    weekly_source = status.get("weekly_source", "")
    if weekly_source:
        lines.append(f"周课表数据源：{source_labels.get(weekly_source, weekly_source)}")
    validation = status.get("weekly_validation") or {}
    if validation:
        if validation.get("skipped"):
            lines.append("课表校验：未执行（当前不是 mportal 数据源）")
        else:
            mismatch = len(validation.get("mismatch_weeks", []))
            outcome = "全部一致" if mismatch == 0 else f"{mismatch} 周失配"
            lines.append(f"课表校验：{outcome}（{validation.get('checked_at', '?')}）")
    reminder = "运行中" if status.get("class_reminder_running") else "未运行"
    start_source = status.get("semester_start_source", "none")
    if start_source == "manual":
        reminder += "（开学日期：手动配置）"
    elif start_source == "mportal":
        reminder += "（开学日期：mportal 自动）"
    elif start_source == "timetable":
        reminder += "（日期：周课表自动）"
    else:
        reminder += "（开学日期未知，提醒不生效）"
    lines.append(f"上课提醒：{reminder}")
    if "class_reminder_lead_minutes" in status:
        lines.append(
            f"提醒时间：每门课开始前 {status['class_reminder_lead_minutes']} 分钟"
        )
    lines.append(
        "已存数据：课表 {courses} 条 / 成绩 {scores} 条 / 考试 {exams} 条".format(
            courses=status.get("courses_count", 0),
            scores=status.get("scores_count", 0),
            exams=status.get("exams_count", 0),
        )
    )
    return "\n".join(lines)
