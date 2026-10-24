from __future__ import annotations

from datetime import datetime


def format_size(value: int) -> str:
    """Render a decimal byte count consistently with the public website.

    Args:
        value: Byte count.

    Returns:
        A compact size with a decimal unit.
    """
    for threshold, unit in ((1_000_000_000, "GB"), (1_000_000, "MB"), (1000, "KB")):
        if value >= threshold:
            return f"{value / threshold:g}{unit}"
    return f"{value}B"


def format_grant(result: dict) -> str:
    """Format the only response that contains a newly issued upload key.

    Args:
        result: Successful grant creation response.

    Returns:
        Owner-private authorization details suitable for forwarding.
    """
    deadline = (
        datetime.fromtimestamp(result["redeem_deadline"])
        .astimezone()
        .strftime("%Y-%m-%d %H:%M:%S %Z")
    )
    return (
        "文件快传授权已生成\n"
        f"入口：{result['public_url']}\n"
        f"通行密钥：{result['upload_key']}\n"
        f"兑换截止：{deadline}\n"
        "上传窗口：兑换后 30 分钟\n"
        f"整批额度：{format_size(result['quota_bytes'])}\n"
        f"文件保留：提交完成后 {result['retention_seconds'] / 60:g} 分钟\n"
        f"批次 ID：{result['id']}\n"
        "通行密钥只能兑换一次；到期会停止传输并清理文件。"
    )


def format_batches(result: dict) -> str:
    """Render a bounded owner-facing list without any file access keys.

    Args:
        result: Batch list response from the private service.

    Returns:
        Recent batch identifiers, states, quotas, and deadlines.
    """
    batches = result.get("batches", [])
    if not batches:
        return "当前没有快传批次。"
    states = {
        "pending": "待兑换",
        "uploading": "上传中",
        "ready": "可下载",
        "expired": "待清理",
    }
    lines = ["最近的快传批次（最多 50 条）"]
    for batch in batches:
        deadline = (
            batch.get("expires_at")
            or batch.get("upload_deadline")
            or batch["redeem_deadline"]
        )
        clock = (
            datetime.fromtimestamp(deadline).astimezone().strftime("%m-%d %H:%M:%S %Z")
        )
        lines.append(
            f"{batch['id']}\n{states.get(batch['state'], batch['state'])} · {format_size(batch['quota_bytes'])} · 截止 {clock}"
        )
    return "\n\n".join(lines)
