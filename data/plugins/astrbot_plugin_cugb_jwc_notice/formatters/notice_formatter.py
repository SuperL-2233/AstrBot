from __future__ import annotations


def format_search_results(result: dict) -> str:
    return format_notice_page(result, "教务通知正文搜索")


def format_notice_page(result: dict, title: str = "教务通知列表") -> str:
    if not result["success"]:
        return result["message"]
    data = result["data"]
    if not data["items"]:
        return result["message"]
    lines = [title, result["message"]]
    for row in data["items"]:
        lines.append(f"\n{row['published_date']}｜{row['title']}")
        lines.append(row["url"])
        if row.get("matched_excerpt"):
            lines.append(f"摘要：{row['matched_excerpt']}")
        for link in row.get("links", []):
            label = str(link.get("text") or "正文附件")
            url = str(link.get("url") or "")
            if url and url != row["url"]:
                lines.append(f"{label}：{url}")
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
        "CUGB 教务通知统计",
        f"总记录：{data['total_count']} 条",
        f"已缓存正文：{data['with_content_count']} 条",
        f"缺失正文：{data['missing_content_count']} 条",
        f"日期范围：{data['earliest_date'] or '无'} 至 {data['latest_date'] or '无'}",
        f"确认覆盖至：{data['coverage_since_date'] or '尚未完成'}",
    ]
    if data["monthly_counts"]:
        lines.append(
            "按月："
            + "，".join(
                f"{row['month']} {row['count']}条" for row in data["monthly_counts"]
            )
        )
    return "\n".join(lines)


def format_status(status: dict) -> str:
    return (
        "CUGB 教务处通知状态\n"
        f"后台同步：{'已启用' if status['enabled'] else '未启用'}\n"
        f"同步间隔：{status['refresh_interval_minutes']} 分钟\n"
        f"默认最早日期：{status['sync_since_date']}\n"
        f"已确认列表覆盖至：{status['coverage_since_date'] or '尚未完成'}\n"
        f"本地通知：{status['notice_count']} 条\n"
        f"本地日期范围：{status['earliest_cached_date'] or '无'}"
        f" 至 {status['latest_cached_date'] or '无'}\n"
        f"待推送：{status['pending_notification_count']} 条\n"
        f"QQ 目标：{'已绑定' if status['target_bound'] else '未绑定'}\n"
        f"最近成功同步：{status['last_successful_sync'] or '从未'}"
    )
