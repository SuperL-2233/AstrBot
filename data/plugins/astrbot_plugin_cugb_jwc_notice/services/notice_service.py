from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any

from ..providers.notice_provider import NoticeProvider
from ..repositories.notice_repo import NoticeRepository

SendMessage = Callable[[str, str], Awaitable[bool]]


class NoticeService:
    def __init__(
        self,
        config: dict[str, Any],
        provider: NoticeProvider,
        repository: NoticeRepository,
        send_message: SendMessage,
    ) -> None:
        self.config = config
        self.provider = provider
        self.repository = repository
        self.send_message = send_message
        self._refresh_lock = asyncio.Lock()

    def get_target_umo(self) -> str:
        configured = str(self.config.get("target_umo", "")).strip()
        return configured or self.repository.get_setting("target_umo")

    def bind_target(self, target_umo: str) -> None:
        self.repository.set_setting("target_umo", target_umo.strip())

    def clear_target(self) -> None:
        self.repository.clear_setting("target_umo")

    async def refresh(self, earliest_date: str = "") -> dict[str, Any]:
        effective_date = (
            earliest_date.strip()
            or str(self.config.get("sync_since_date", "2025-09-01")).strip()
        )
        try:
            date.fromisoformat(effective_date)
        except ValueError:
            return self._result(
                False,
                "最早同步日期必须使用 YYYY-MM-DD 格式。",
                error_code="INVALID_EARLIEST_DATE",
            )
        if self._refresh_lock.locked():
            return self._result(
                False,
                "已有教务处同步正在进行。",
                error_code="SYNC_IN_PROGRESS",
            )
        async with self._refresh_lock:
            sync_state = self.repository.sync_state()
            needs_backfill = (
                not sync_state["coverage_since_date"]
                or sync_state["coverage_since_date"] > effective_date
                or sync_state["missing_detail_count"] > 0
            )
            stop_at_ids = set() if needs_backfill else sync_state["notice_ids"]
            try:
                summaries, pages_fetched = await self.provider.fetch_list(
                    effective_date, stop_at_ids
                )
            except Exception as exc:
                return self._result(
                    False,
                    f"教务处通知列表同步失败：{exc}",
                    error_code="NOTICE_SOURCE_UNAVAILABLE",
                )

            missing_ids = self.repository.missing_detail_ids(summaries)
            detail_errors: list[str] = []
            records: list[dict[str, Any]] = []
            for summary in summaries:
                record: dict[str, Any] = dict(summary)
                if str(summary["id"]) in missing_ids:
                    try:
                        detail = await self.provider.fetch_detail(
                            str(summary["published_date"]), str(summary["id"])
                        )
                        record.update(detail)
                        record["id"] = str(summary["id"])
                    except Exception as exc:
                        record["content"] = None
                        detail_errors.append(f"{summary['id']}: {exc}")
                else:
                    record["content"] = None
                records.append(record)

            snapshot = self.repository.apply_snapshot(
                records,
                coverage_since_date=effective_date if needs_backfill else "",
            )
            new_rows = snapshot["new_rows"]
            notification_rows = snapshot["notification_rows"]
            delivery = await self.deliver_pending()
            message = (
                f"教务处同步完成：从 {effective_date} 起读取 {pages_fetched} 页、"
                f"{len(records)} 条，新增入库 {len(new_rows)} 条，"
                f"新增通知 {len(notification_rows)} 条。"
            )
            if detail_errors:
                message += f" {len(detail_errors)} 条正文暂未缓存。"
            if delivery["sent_count"]:
                message += f" 已向 QQ 推送 {delivery['sent_count']} 条。"
            elif delivery["pending_count"]:
                message += f" 尚有 {delivery['pending_count']} 条等待推送：{delivery['message']}"
            return self._result(
                True,
                message,
                data={
                    "fetched_count": len(records),
                    "pages_fetched": pages_fetched,
                    "earliest_date": effective_date,
                    "new_count": len(new_rows),
                    "new_notices": new_rows,
                    "notification_count": len(notification_rows),
                    "notifications": notification_rows,
                    "baseline_count": snapshot["baseline_count"],
                    "detail_error_count": len(detail_errors),
                    "detail_errors": detail_errors,
                    **delivery,
                },
            )

    async def deliver_pending(self) -> dict[str, Any]:
        pending = self.repository.pending_notifications()
        if not pending:
            return {
                "sent_count": 0,
                "pending_count": 0,
                "message": "没有待推送通知。",
            }
        target = self.get_target_umo()
        if not target:
            return {
                "sent_count": 0,
                "pending_count": len(pending),
                "message": "尚未绑定 QQ 私聊目标。",
            }
        text = self._format_push(pending)
        try:
            sent = await self.send_message(target, text)
        except Exception as exc:
            return {
                "sent_count": 0,
                "pending_count": len(pending),
                "message": f"QQ 发送失败：{exc}",
            }
        if not sent:
            return {
                "sent_count": 0,
                "pending_count": len(pending),
                "message": "AstrBot 未找到目标会话的平台适配器。",
            }
        self.repository.mark_delivered([str(row["notice_id"]) for row in pending])
        return {
            "sent_count": len(pending),
            "pending_count": 0,
            "message": "QQ 推送成功。",
        }

    def search(self, keyword: str) -> dict[str, Any]:
        keyword = keyword.strip()
        if not keyword:
            return self._result(
                False,
                "请提供要检索的正文关键词。",
                error_code="KEYWORD_REQUIRED",
            )
        limit = min(50, max(1, int(self.config.get("search_limit", 10))))
        rows = self.repository.search_content(keyword, limit)
        return self._result(
            True,
            f"在本地正文缓存中找到 {len(rows)} 条匹配通知。",
            data=rows,
        )

    def status(self) -> dict[str, Any]:
        status = self.repository.status()
        status.update(
            {
                "enabled": bool(self.config.get("enabled", True)),
                "target_bound": bool(self.get_target_umo()),
                "refresh_interval_minutes": max(
                    1, int(self.config.get("refresh_interval_minutes", 10))
                ),
                "source_url": self.config.get("source_url"),
                "sync_since_date": self.config.get("sync_since_date"),
                "sync_in_progress": self._refresh_lock.locked(),
            }
        )
        return status

    @staticmethod
    def _format_push(rows: list[dict[str, Any]]) -> str:
        lines = [f"教务处有 {len(rows)} 条新通知"]
        for row in rows:
            lines.append(f"\n{row['published_date']}｜{row['title']}\n{row['url']}")
        return "\n".join(lines)

    @staticmethod
    def _result(
        success: bool,
        message: str,
        *,
        data: Any = None,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        return {
            "success": success,
            "data": data,
            "message": message,
            "error_code": error_code,
            "source_status": {"provider": "CUGBJwcAPI", "read_only": True},
        }
