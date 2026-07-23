from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any

from ..providers.notice_provider import (
    SOURCES,
    NoticeProvider,
    resolve_source,
    source_label,
)
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
        self._delivery_lock = asyncio.Lock()

    def get_target_umo(self) -> str:
        configured = str(self.config.get("target_umo", "")).strip()
        return configured or self.repository.get_setting("target_umo")

    def bind_target(self, target_umo: str) -> None:
        self.repository.set_setting("target_umo", target_umo.strip())

    def clear_target(self) -> None:
        self.repository.clear_setting("target_umo")

    def _resolve_sources(self, source: str) -> list[str] | dict[str, Any]:
        try:
            resolved = resolve_source(source)
        except ValueError:
            return self._result(
                False,
                "未知来源。可用来源："
                + "、".join(f"{key}（{spec.label}）" for key, spec in SOURCES.items()),
                error_code="INVALID_SOURCE",
            )
        return [resolved] if resolved else list(SOURCES)

    async def refresh(
        self, source: str = "", earliest_date: str = ""
    ) -> dict[str, Any]:
        sources = self._resolve_sources(source)
        if isinstance(sources, dict):
            return sources
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
                "已有新闻公告同步正在进行。",
                error_code="SYNC_IN_PROGRESS",
            )
        async with self._refresh_lock:
            per_source: dict[str, Any] = {}
            failed: list[str] = []
            for key in sources:
                outcome = await self._refresh_source(key, effective_date)
                per_source[key] = {
                    "success": outcome["success"],
                    "message": outcome["message"],
                    "error_code": outcome["error_code"],
                    **(outcome["data"] or {}),
                }
                if not outcome["success"]:
                    failed.append(key)
            delivery = await self.deliver_pending()
            message_parts = [per_source[key]["message"] for key in sources]
            if delivery["sent_count"]:
                message_parts.append(f"已向 QQ 推送 {delivery['sent_count']} 条。")
            elif delivery["pending_count"]:
                message_parts.append(
                    f"尚有 {delivery['pending_count']} 条等待推送：{delivery['message']}"
                )
            total_stats = self.repository.statistics()
            data: dict[str, Any] = {
                "requested_earliest_date": effective_date,
                "sources": per_source,
                "failed_sources": failed,
                "scanned_notice_count": sum(
                    item.get("scanned_notice_count", 0) for item in per_source.values()
                ),
                "new_count": sum(
                    item.get("new_count", 0) for item in per_source.values()
                ),
                "notification_count": sum(
                    item.get("notification_count", 0) for item in per_source.values()
                ),
                "database_total_count": total_stats["total_count"],
                **delivery,
            }
            error_code = ""
            if failed:
                error_code = (
                    per_source[failed[0]]["error_code"]
                    if len(sources) == 1
                    else "PARTIAL_SYNC_FAILURE"
                )
            return self._result(
                not failed,
                "；".join(message_parts),
                data=data,
                error_code=error_code or None,
            )

    async def _refresh_source(self, source: str, effective_date: str) -> dict[str, Any]:
        label = source_label(source)
        sync_state = self.repository.sync_state(source)
        needs_backfill = (
            not sync_state["coverage_since_date"]
            or sync_state["coverage_since_date"] > effective_date
        )
        stop_at_ids = set() if needs_backfill else sync_state["notice_ids"]
        try:
            summaries, pages_fetched = await self.provider.fetch_list(
                source, effective_date, stop_at_ids
            )
        except Exception as exc:
            return self._result(
                False,
                f"{label} 列表同步失败：{exc}",
                error_code="NOTICE_SOURCE_UNAVAILABLE",
                data={
                    "source": source,
                    "source_label": label,
                    "scanned_notice_count": 0,
                    "new_count": 0,
                    "notification_count": 0,
                },
            )

        retry_limit = min(100, max(1, int(self.config.get("detail_retry_limit", 20))))
        retry_summaries = self.repository.missing_detail_summaries(source, retry_limit)
        summaries_by_id = {str(row["id"]): row for row in summaries}
        for row in retry_summaries:
            summaries_by_id.setdefault(str(row["id"]), row)
        detail_candidates = list(summaries_by_id.values())
        missing_ids = self.repository.missing_detail_ids(source, detail_candidates)
        detail_errors: list[str] = []
        records: list[dict[str, Any]] = []
        for summary in detail_candidates:
            record: dict[str, Any] = dict(summary)
            if str(summary["id"]) in missing_ids:
                try:
                    detail = await self.provider.fetch_detail(
                        source, str(summary["published_date"]), str(summary["id"])
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
            source,
            records,
            coverage_since_date=effective_date if needs_backfill else "",
        )
        new_rows = snapshot["new_rows"]
        notification_rows = snapshot["notification_rows"]
        database_stats = self.repository.statistics(source)
        scan_label = "历史范围扫描" if needs_backfill else "增量检查"
        message = (
            f"{label}：{scan_label} {pages_fetched} 页、{len(summaries)} 条；"
            f"库共 {database_stats['total_count']} 条；新增 {len(new_rows)} 条"
        )
        if notification_rows:
            message += f"，待推送 {len(notification_rows)} 条"
        if detail_errors:
            message += f"；{len(detail_errors)} 条正文暂未缓存"
        return self._result(
            True,
            message,
            data={
                "source": source,
                "source_label": label,
                "scan_mode": "historical_backfill" if needs_backfill else "incremental",
                "scanned_notice_count": len(summaries),
                "pages_fetched": pages_fetched,
                "database_total_count": database_stats["total_count"],
                "database_with_content_count": database_stats["with_content_count"],
                "database_missing_content_count": database_stats[
                    "missing_content_count"
                ],
                "database_earliest_date": database_stats["earliest_date"],
                "database_latest_date": database_stats["latest_date"],
                "new_count": len(new_rows),
                "new_notices": [self._summary(row) for row in new_rows],
                "notification_count": len(notification_rows),
                "notifications": [self._summary(row) for row in notification_rows],
                "baseline_count": snapshot["baseline_count"],
                "detail_retry_count": len(retry_summaries),
                "detail_error_count": len(detail_errors),
                "detail_errors": detail_errors[:20],
            },
        )

    async def deliver_pending(self) -> dict[str, Any]:
        async with self._delivery_lock:
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
            sent_count = 0
            for index in range(0, len(pending), 10):
                batch = pending[index : index + 10]
                try:
                    sent = await self.send_message(target, self._format_push(batch))
                except Exception as exc:
                    return {
                        "sent_count": sent_count,
                        "pending_count": len(pending) - sent_count,
                        "message": f"QQ 发送失败：{exc}",
                    }
                if not sent:
                    return {
                        "sent_count": sent_count,
                        "pending_count": len(pending) - sent_count,
                        "message": "AstrBot 未找到目标会话的平台适配器。",
                    }
                self.repository.mark_delivered(
                    [(str(row["source"]), str(row["notice_id"])) for row in batch]
                )
                sent_count += len(batch)
            return {
                "sent_count": sent_count,
                "pending_count": 0,
                "message": "QQ 推送成功。",
            }

    def search(
        self,
        keyword: str,
        source: str = "",
        start_date: str = "",
        end_date: str = "",
        limit: int = 10,
        offset: int = 0,
    ) -> dict[str, Any]:
        keyword = keyword.strip()
        if not keyword:
            return self._result(
                False,
                "请提供要检索的正文关键词。",
                error_code="KEYWORD_REQUIRED",
            )
        sources = self._resolve_sources(source)
        if isinstance(sources, dict):
            return sources
        validation = self._validate_query(start_date, end_date, limit, offset)
        if validation:
            return validation
        resolved = resolve_source(source)
        result = self.repository.query_notices(
            source=resolved,
            keyword=keyword,
            content_only=True,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
            offset=offset,
        )
        items = [self._search_item(row, keyword) for row in result["rows"]]
        total = result["total_count"]
        return self._result(
            True,
            f"正文共有 {total} 条匹配，本页返回 {len(items)} 条。",
            data={
                "query": {
                    "keyword": keyword,
                    "scope": "content",
                    "source": resolved,
                    "source_label": source_label(resolved) if resolved else "全部来源",
                    "start_date": start_date,
                    "end_date": end_date,
                },
                "total_count": total,
                "returned_count": len(items),
                "limit": limit,
                "offset": offset,
                "has_more": offset + len(items) < total,
                "items": items,
            },
        )

    def list_notices(
        self,
        source: str = "",
        start_date: str = "",
        end_date: str = "",
        limit: int = 10,
        offset: int = 0,
    ) -> dict[str, Any]:
        sources = self._resolve_sources(source)
        if isinstance(sources, dict):
            return sources
        validation = self._validate_query(start_date, end_date, limit, offset)
        if validation:
            return validation
        resolved = resolve_source(source)
        result = self.repository.query_notices(
            source=resolved,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
            offset=offset,
        )
        items = [self._summary(row) for row in result["rows"]]
        total = result["total_count"]
        return self._result(
            True,
            f"指定范围共有 {total} 条记录，本页返回 {len(items)} 条。",
            data={
                "query": {
                    "source": resolved,
                    "source_label": source_label(resolved) if resolved else "全部来源",
                    "start_date": start_date,
                    "end_date": end_date,
                },
                "total_count": total,
                "returned_count": len(items),
                "limit": limit,
                "offset": offset,
                "has_more": offset + len(items) < total,
                "items": items,
            },
        )

    def statistics(
        self, source: str = "", start_date: str = "", end_date: str = ""
    ) -> dict[str, Any]:
        sources = self._resolve_sources(source)
        if isinstance(sources, dict):
            return sources
        validation = self._validate_query(start_date, end_date, 1, 0)
        if validation:
            return validation
        resolved = resolve_source(source)
        data = self.repository.statistics(resolved, start_date, end_date)
        data.update(
            {
                "requested_source": resolved,
                "requested_source_label": (
                    source_label(resolved) if resolved else "全部来源"
                ),
                "requested_start_date": start_date,
                "requested_end_date": end_date,
                "coverage_since_date": self._coverage_map(resolved),
                "last_successful_sync": self.repository.get_setting(
                    "last_successful_sync"
                ),
            }
        )
        return self._result(
            True,
            f"本地新闻公告库共有 {data['total_count']} 条记录，"
            f"日期范围 {data['earliest_date'] or '无'} 至 {data['latest_date'] or '无'}。",
            data=data,
        )

    def _coverage_map(self, resolved: str) -> dict[str, str]:
        keys = [resolved] if resolved else list(SOURCES)
        return {
            key: self.repository.get_setting(f"coverage_since_date:{key}")
            for key in keys
        }

    def status(self) -> dict[str, Any]:
        status = self.repository.status()
        status.update(
            {
                "enabled": bool(self.config.get("enabled", True)),
                "target_bound": bool(self.get_target_umo()),
                "refresh_interval_minutes": max(
                    1, int(self.config.get("refresh_interval_minutes", 10))
                ),
                "sync_since_date": self.config.get("sync_since_date"),
                "sync_in_progress": self._refresh_lock.locked(),
                "source_labels": {key: spec.label for key, spec in SOURCES.items()},
            }
        )
        return status

    def _validate_query(
        self, start_date: str, end_date: str, limit: int, offset: int
    ) -> dict[str, Any] | None:
        for label, value in (("开始日期", start_date), ("结束日期", end_date)):
            if not value:
                continue
            try:
                date.fromisoformat(value)
            except ValueError:
                return self._result(
                    False,
                    f"{label}必须使用 YYYY-MM-DD 格式。",
                    error_code="INVALID_QUERY_DATE",
                )
        if start_date and end_date and start_date > end_date:
            return self._result(
                False,
                "开始日期不能晚于结束日期。",
                error_code="INVALID_DATE_RANGE",
            )
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 50
        ):
            return self._result(
                False,
                "limit 必须在 1 到 50 之间。",
                error_code="INVALID_LIMIT",
            )
        if (
            isinstance(offset, bool)
            or not isinstance(offset, int)
            or not 0 <= offset <= 10000
        ):
            return self._result(
                False,
                "offset 必须在 0 到 10000 之间。",
                error_code="INVALID_OFFSET",
            )
        return None

    @staticmethod
    def _summary(row: dict[str, Any]) -> dict[str, Any]:
        source = str(row.get("source") or "")
        return {
            "source": source,
            "source_label": source_label(source) if source else "",
            "notice_id": str(row.get("notice_id") or row.get("id") or ""),
            "title": str(row.get("title") or ""),
            "published_date": str(row.get("published_date") or ""),
            "author": str(row.get("author") or ""),
            "url": str(row.get("url") or ""),
            "links": list(row.get("links") or []),
        }

    @classmethod
    def _search_item(cls, row: dict[str, Any], keyword: str) -> dict[str, Any]:
        item = cls._summary(row)
        content = " ".join(str(row.get("content") or "").split())
        position = content.casefold().find(keyword.casefold())
        start = max(0, position - 100) if position >= 0 else 0
        end = min(len(content), position + len(keyword) + 160) if position >= 0 else 260
        excerpt = content[start:end]
        if start:
            excerpt = "…" + excerpt
        if end < len(content):
            excerpt += "…"
        item["matched_excerpt"] = excerpt
        return item

    @staticmethod
    def _format_push(rows: list[dict[str, Any]]) -> str:
        lines = [f"CUGB 新闻公告有 {len(rows)} 条更新"]
        for row in rows:
            lines.append(
                f"\n【{source_label(str(row['source']))}】"
                f"{row['published_date']}｜{row['title']}\n{row['url']}"
            )
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
            "source_status": {"provider": "CUGBAPI", "read_only": True},
        }
