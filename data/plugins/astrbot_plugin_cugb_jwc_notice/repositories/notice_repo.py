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

    def apply_snapshot(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
            new_rows = [row for row in rows if str(row["id"]) not in existing]
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
                for row in new_rows:
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
        return new_rows if initialized else []

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

    def search_content(self, keyword: str, limit: int) -> list[dict[str, Any]]:
        escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM notices
                   WHERE content LIKE ? ESCAPE '\\'
                   ORDER BY published_date DESC, notice_id DESC
                   LIMIT ?""",
                (f"%{escaped}%", limit),
            ).fetchall()
        return [self._decode_row(row) for row in rows]

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
        return {
            "notice_count": notice_count,
            "pending_notification_count": pending_count,
            "initialized": self.get_setting("initialized") == "1",
            "last_successful_sync": self.get_setting("last_successful_sync"),
            "target_bound": bool(self.get_setting("target_umo")),
        }
