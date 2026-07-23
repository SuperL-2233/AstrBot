from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


class NoticeRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS notices (
                    notice_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    published_date TEXT NOT NULL,
                    url TEXT NOT NULL,
                    author TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL DEFAULT '',
                    links_json TEXT NOT NULL DEFAULT '[]',
                    images_json TEXT NOT NULL DEFAULT '[]',
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_notices_published
                    ON notices(published_date DESC, notice_id DESC);
                CREATE TABLE IF NOT EXISTS notification_deliveries (
                    notice_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    delivered_at TEXT,
                    FOREIGN KEY(notice_id) REFERENCES notices(notice_id)
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )

    @staticmethod
    def _now() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    def get_setting(self, key: str) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return str(row["value"]) if row else ""

    def set_setting(self, key: str, value: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO settings(key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (key, value),
            )

    def clear_setting(self, key: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM settings WHERE key = ?", (key,))

    def missing_detail_ids(self, rows: list[dict[str, Any]]) -> set[str]:
        ids = [str(row["id"]) for row in rows]
        if not ids:
            return set()
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as connection:
            found = connection.execute(
                f"SELECT notice_id FROM notices WHERE notice_id IN ({placeholders}) "
                "AND content <> ''",
                ids,
            ).fetchall()
        complete = {str(row["notice_id"]) for row in found}
        return set(ids) - complete

    def missing_detail_summaries(self, limit: int) -> list[dict[str, str]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT notice_id AS id, title, published_date, url
                   FROM notices
                   WHERE content = ''
                   ORDER BY published_date DESC, notice_id DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def sync_state(self, *, include_ids: bool = True) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT MIN(published_date) AS earliest_date,
                          MAX(published_date) AS latest_date,
                          SUM(CASE WHEN content = '' THEN 1 ELSE 0 END) AS missing_count
                   FROM notices"""
            ).fetchone()
            ids = (
                {
                    str(item["notice_id"])
                    for item in connection.execute("SELECT notice_id FROM notices")
                }
                if include_ids
                else set()
            )
            coverage_row = connection.execute(
                "SELECT value FROM settings WHERE key = 'coverage_since_date'"
            ).fetchone()
        return {
            "earliest_date": str(row["earliest_date"] or ""),
            "latest_date": str(row["latest_date"] or ""),
            "missing_detail_count": int(row["missing_count"] or 0),
            "notice_ids": ids,
            "coverage_since_date": str(coverage_row["value"]) if coverage_row else "",
        }

    def apply_snapshot(
        self, rows: list[dict[str, Any]], *, coverage_since_date: str = ""
    ) -> dict[str, Any]:
        now = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            initialized = connection.execute(
                "SELECT value FROM settings WHERE key = 'initialized'"
            ).fetchone()
            existing = {
                str(row["notice_id"])
                for row in connection.execute("SELECT notice_id FROM notices")
            }
            latest_existing_row = connection.execute(
                "SELECT MAX(published_date) AS latest_date FROM notices"
            ).fetchone()
            latest_existing_date = str(latest_existing_row["latest_date"] or "")
            new_rows = [row for row in rows if str(row["id"]) not in existing]
            notification_rows = [
                row
                for row in new_rows
                if initialized
                and latest_existing_date
                and str(row["published_date"]) >= latest_existing_date
            ]
            for row in rows:
                detail_loaded = row.get("content") is not None
                connection.execute(
                    """
                    INSERT INTO notices(
                        notice_id, title, published_date, url, author, content,
                        links_json, images_json, first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(notice_id) DO UPDATE SET
                        title = excluded.title,
                        published_date = excluded.published_date,
                        url = excluded.url,
                        author = CASE WHEN ? THEN excluded.author ELSE notices.author END,
                        content = CASE WHEN ? THEN excluded.content ELSE notices.content END,
                        links_json = CASE WHEN ? THEN excluded.links_json ELSE notices.links_json END,
                        images_json = CASE WHEN ? THEN excluded.images_json ELSE notices.images_json END,
                        last_seen_at = excluded.last_seen_at
                    """,
                    (
                        str(row["id"]),
                        str(row["title"]),
                        str(row["published_date"]),
                        str(row["url"]),
                        str(row.get("author") or ""),
                        str(row.get("content") or ""),
                        json.dumps(row.get("links") or [], ensure_ascii=False),
                        json.dumps(row.get("images") or [], ensure_ascii=False),
                        now,
                        now,
                        detail_loaded,
                        detail_loaded,
                        detail_loaded,
                        detail_loaded,
                    ),
                )
            if initialized:
                for row in notification_rows:
                    connection.execute(
                        """INSERT OR IGNORE INTO notification_deliveries(
                               notice_id, state, created_at
                           ) VALUES (?, 'pending', ?)""",
                        (str(row["id"]), now),
                    )
            connection.execute(
                """INSERT INTO settings(key, value) VALUES ('initialized', '1')
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value"""
            )
            connection.execute(
                """INSERT INTO settings(key, value) VALUES ('last_successful_sync', ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (now,),
            )
            if coverage_since_date:
                connection.execute(
                    """INSERT INTO settings(key, value)
                       VALUES ('coverage_since_date', ?)
                       ON CONFLICT(key) DO UPDATE SET value =
                           CASE
                               WHEN settings.value < excluded.value THEN settings.value
                               ELSE excluded.value
                           END""",
                    (coverage_since_date,),
                )
        return {
            "new_rows": new_rows if initialized else [],
            "notification_rows": notification_rows if initialized else [],
            "baseline_count": 0 if initialized else len(rows),
        }

    @staticmethod
    def _decode_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["links"] = json.loads(result.pop("links_json"))
        result["images"] = json.loads(result.pop("images_json"))
        return result

    def pending_notifications(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT n.* FROM notices n
                   JOIN notification_deliveries d ON d.notice_id = n.notice_id
                   WHERE d.state = 'pending'
                   ORDER BY n.published_date, n.notice_id"""
            ).fetchall()
        return [self._decode_row(row) for row in rows]

    def mark_delivered(self, notice_ids: list[str]) -> None:
        if not notice_ids:
            return
        placeholders = ",".join("?" for _ in notice_ids)
        with self._connect() as connection:
            connection.execute(
                f"UPDATE notification_deliveries SET state = 'delivered', "
                f"delivered_at = ? WHERE notice_id IN ({placeholders})",
                [self._now(), *notice_ids],
            )

    @staticmethod
    def _escaped_like(value: str) -> str:
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    def query_notices(
        self,
        *,
        keyword: str = "",
        content_only: bool = False,
        start_date: str = "",
        end_date: str = "",
        limit: int = 10,
        offset: int = 0,
    ) -> dict[str, Any]:
        conditions: list[str] = []
        parameters: list[Any] = []
        if keyword:
            pattern = f"%{self._escaped_like(keyword)}%"
            if content_only:
                conditions.append("content LIKE ? ESCAPE '\\'")
                parameters.append(pattern)
            else:
                conditions.append(
                    "(title LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\')"
                )
                parameters.extend([pattern, pattern])
        if start_date:
            conditions.append("published_date >= ?")
            parameters.append(start_date)
        if end_date:
            conditions.append("published_date <= ?")
            parameters.append(end_date)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self._connect() as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM notices {where}", parameters
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"""SELECT * FROM notices {where}
                    ORDER BY published_date DESC, notice_id DESC
                    LIMIT ? OFFSET ?""",
                [*parameters, limit, offset],
            ).fetchall()
        return {"total_count": total, "rows": [self._decode_row(row) for row in rows]}

    def statistics(self, start_date: str = "", end_date: str = "") -> dict[str, Any]:
        conditions: list[str] = []
        parameters: list[str] = []
        if start_date:
            conditions.append("published_date >= ?")
            parameters.append(start_date)
        if end_date:
            conditions.append("published_date <= ?")
            parameters.append(end_date)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self._connect() as connection:
            summary = connection.execute(
                f"""SELECT COUNT(*) AS total_count,
                            SUM(CASE WHEN content <> '' THEN 1 ELSE 0 END)
                                AS with_content_count,
                            MIN(published_date) AS earliest_date,
                            MAX(published_date) AS latest_date
                     FROM notices {where}""",
                parameters,
            ).fetchone()
            months = connection.execute(
                f"""SELECT SUBSTR(published_date, 1, 7) AS month, COUNT(*) AS count
                     FROM notices {where}
                     GROUP BY month ORDER BY month""",
                parameters,
            ).fetchall()
        total = int(summary["total_count"] or 0)
        with_content = int(summary["with_content_count"] or 0)
        return {
            "total_count": total,
            "with_content_count": with_content,
            "missing_content_count": total - with_content,
            "earliest_date": str(summary["earliest_date"] or ""),
            "latest_date": str(summary["latest_date"] or ""),
            "monthly_counts": [dict(row) for row in months],
        }

    def status(self) -> dict[str, Any]:
        with self._connect() as connection:
            notice_count = int(
                connection.execute("SELECT COUNT(*) FROM notices").fetchone()[0]
            )
            pending_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM notification_deliveries WHERE state = 'pending'"
                ).fetchone()[0]
            )
        sync_state = self.sync_state(include_ids=False)
        return {
            "notice_count": notice_count,
            "pending_notification_count": pending_count,
            "initialized": self.get_setting("initialized") == "1",
            "last_successful_sync": self.get_setting("last_successful_sync"),
            "target_bound": bool(self.get_setting("target_umo")),
            "earliest_cached_date": sync_state["earliest_date"],
            "latest_cached_date": sync_state["latest_date"],
            "coverage_since_date": sync_state["coverage_since_date"],
        }
