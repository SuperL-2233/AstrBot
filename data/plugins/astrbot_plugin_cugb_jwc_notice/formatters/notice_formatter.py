from __future__ import annotations


def format_search_results(result: dict) -> str:
    return format_notice_page(result, "新闻公告正文搜索")


def format_notice_page(result: dict, title: str = "新闻公告列表") -> str:
    if not result["success"]:
        return result["message"]
    data = result["data"]
    if not data["items"]:
        return result["message"]
    lines = [title, result["message"]]
    for row in data["items"]:
        label = f"【{row['source_label']}】" if row.get("source_label") else ""
        lines.append(f"\n{label}{row['published_date']}｜{row['title']}")
        lines.append(row["url"])
        if row.get("matched_excerpt"):
            lines.append(f"摘要：{row['matched_excerpt']}")
        for link in row.get("links", []):
            label_text = str(link.get("text") or "正文附件")
            url = str(link.get("url") or "")
            if url and url != row["url"]:
                lines.append(f"{label_text}：{url}")
    if data["has_more"]:
        lines.append(
            f"\n还有 {data['total_count'] - data['offset'] - data['returned_count']} 条，"
            f"下一页 offset={data['offset'] + data['returned_count']}。"
        )
    return "\n".join(lines)


def format_statistics(result: dict) -> str:
    if not result["success"]:
        return result["message"]
    data = result["data"]
    lines = [
        f"CUGB 新闻公告统计（{data['requested_source_label']}）",
        f"总记录：{data['total_count']} 条",
        f"已缓存正文：{data['with_content_count']} 条",
        f"缺失正文：{data['missing_content_count']} 条",
        f"日期范围：{data['earliest_date'] or '无'} 至 {data['latest_date'] or '无'}",
    ]
    if data["source_counts"]:
        lines.append(
            "分来源："
            + "，".join(
                f"{key} {count}条" for key, count in data["source_counts"].items()
            )
        )
    coverage = data["coverage_since_date"]
    if coverage:
        lines.append(
            "确认覆盖至："
            + "，".join(
                f"{key} {value or '尚未完成'}" for key, value in coverage.items()
            )
        )
    if data["monthly_counts"]:
        lines.append(
            "按月："
            + "，".join(
                f"{row['month']} {row['count']}条" for row in data["monthly_counts"]
            )
        )
    return "\n".join(lines)


def format_status(status: dict) -> str:
    lines = [
        "CUGB 新闻公告检索状态",
        f"后台同步：{'已启用' if status['enabled'] else '未启用'}",
        f"同步间隔：{status['refresh_interval_minutes']} 分钟",
        f"默认最早日期：{status['sync_since_date']}",
        f"本地记录：{status['notice_count']} 条",
    ]
    labels = status.get("source_labels", {})
    for key, info in status.get("sources", {}).items():
        label = labels.get(key, key)
        lines.append(
            f"- {label}：{info['notice_count']} 条，"
            f"范围 {info['earliest_cached_date'] or '无'} 至 "
            f"{info['latest_cached_date'] or '无'}，"
            f"覆盖至 {info['coverage_since_date'] or '尚未完成'}"
        )
    lines.extend(
        [
            f"待推送：{status['pending_notification_count']} 条",
            f"QQ 目标：{'已绑定' if status['target_bound'] else '未绑定'}",
            f"最近成功同步：{status['last_successful_sync'] or '从未'}",
        ]
    )
    return "\n".join(lines)
