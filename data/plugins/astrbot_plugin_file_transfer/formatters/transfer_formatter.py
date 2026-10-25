from __future__ import annotations

from datetime import datetime


def format_size(value: int, *, binary: bool = False) -> str:
    """Render decimal file sizes or binary space allocations.

    Args:
        value: Byte count.
        binary: Use binary units for the global quota and session allocations.

    Returns:
        A compact size using decimal or binary units.
    """
    units = (
        ((1024**3, "GiB"), (1024**2, "MiB"), (1024, "KiB"))
        if binary
        else ((1_000_000_000, "GB"), (1_000_000, "MB"), (1000, "KB"))
    )
    for threshold, unit in units:
        if value >= threshold:
            if binary:
                size = f"{value / threshold:.3f}".rstrip("0").rstrip(".")
                return f"{size} {unit}"
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
        "暂传授权已生成\n"
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
        return "当前没有暂传批次。"
    states = {
        "pending": "待兑换",
        "uploading": "上传中",
        "ready": "可下载",
        "expired": "待清理",
    }
    lines = ["最近的暂传批次（最多 50 条）"]
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


def format_space(result: dict) -> str:
    """Render exact global totals and a bounded page of session allocations.

    Args:
        result: Private space response with global quota and per-session usage.

    Returns:
        Owner-visible allocated space, remaining quota, and full batch IDs.
    """
    lines = [
        "暂传空间",
        f"总上限：{format_size(result['limit_bytes'], binary=True)}",
        f"已分配：{format_size(result['allocated_bytes'], binary=True)}",
        f"剩余额度：{format_size(result['available_bytes'], binary=True)}",
        f"会话：共 {result['total_count']} 个",
        "签发即占额，整批清理成功后释放。",
    ]
    if not result["total_count"]:
        return "\n".join([*lines, "当前没有暂传会话。"])
    page, pages = result["page"], result["total_pages"]
    if not result["batches"]:
        return "\n".join(
            [
                *lines,
                f"第 {page} 页没有会话，共 {pages} 页。使用 /暂传空间 1 返回首页。",
            ]
        )
    states = {
        "pending": "待兑换",
        "uploading": "上传中",
        "ready": "可下载",
        "expired": "待清理（仍占额）",
    }
    lines.append(f"第 {page}/{pages} 页")
    for batch in result["batches"]:
        clock = (
            datetime.fromtimestamp(batch["deadline"])
            .astimezone()
            .strftime("%m-%d %H:%M:%S")
        )
        lines.append(
            f"会话 {batch['id']}\n"
            f"{states.get(batch['state'], batch['state'])} · 分配 {format_size(batch['quota_bytes'], binary=True)}\n"
            f"文件预留 {format_size(batch['declared_bytes'], binary=True)} · 已收 {format_size(batch['uploaded_bytes'], binary=True)}\n"
            f"截止：{clock}"
        )
    if page < pages:
        lines.append(f"下一页：/暂传空间 {page + 1}")
    return "\n\n".join([*lines, "撤销会话：/暂传撤销 <完整批次ID>"])


def format_files(result: dict) -> str:
    """Render a compact file page with exact deletion identifiers.

    Args:
        result: Private file listing with exact counts and bounded pagination.

    Returns:
        A QQ-friendly listing of files, received sizes, states, and deadlines.
    """
    if not result["total_count"]:
        return "当前没有暂传文件。"
    page = result["page"]
    pages = result["total_pages"]
    if not result["files"]:
        return f"第 {page} 页没有文件，共 {pages} 页。使用 /暂传文件 1 返回首页。"
    states = {
        "pending": "待上传",
        "partial": "部分上传",
        "uploading": "上传中",
        "uploaded": "待提交",
        "ready": "可下载",
        "expired": "已到期",
        "deleting": "待清理",
    }
    lines = [
        f"暂传文件 第 {page}/{pages} 页，共 {result['total_count']} 个\n声明总量 {format_size(result['total_bytes'])}，已接收 {format_size(result['uploaded_bytes'])}"
    ]
    for number, file in enumerate(
        result["files"], start=(page - 1) * result["page_size"] + 1
    ):
        name = file["name"][:40] + ("…" if len(file["name"]) > 40 else "")
        clock = (
            datetime.fromtimestamp(file["deadline"])
            .astimezone()
            .strftime("%m-%d %H:%M:%S")
        )
        state = states.get(file["status"], file["status"])
        lines.append(
            f"{number}. {name}\n文件ID：{file['id']}\n批次：{file['batch_id']}\n{state} · 已收 {format_size(file['offset'])}/{format_size(file['size'])}\n截止：{clock}"
        )
    if page < pages:
        lines.append(f"下一页：/暂传文件 {page + 1}")
    lines.append("删除：/暂传删除 <文件ID>")
    return "\n\n".join(lines)
