from __future__ import annotations


def format_search_results(result: dict) -> str:
    if not result["success"]:
        return result["message"]
    if not result["data"]:
        return result["message"]
    lines = [result["message"]]
    for row in result["data"]:
        lines.append(f"\n{row['published_date']}｜{row['title']}")
        lines.append(row["url"])
        for link in row.get("links", []):
            label = str(link.get("text") or "正文附件")
            url = str(link.get("url") or "")
            if url and url != row["url"]:
                lines.append(f"{label}：{url}")
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
