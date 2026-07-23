from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path


class TaskRepository:
    """Persist normalized tasks, a reminder target, and delivery idempotency."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_key TEXT PRIMARY KEY,
                    course_name TEXT NOT NULL,
                    course_id TEXT NOT NULL,
                    class_id TEXT NOT NULL,
                    task_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    start_time TEXT,
                    deadline TEXT,
                    source_update_time TEXT NOT NULL,
                    url TEXT,
                    note TEXT,
                    source TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS reminder_deliveries (
                    reminder_key TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    delivered_at TEXT
                );
                """
            )

    def replace_tasks(self, tasks: list[dict]) -> int:
        with self._connect() as connection:
            connection.execute("DELETE FROM tasks")
            connection.executemany(
                """
                INSERT INTO tasks (
                    task_key, course_name, course_id, class_id, task_type,
                    title, status, start_time, deadline, source_update_time,
                    url, note, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        item["task_key"],
                        item["course_name"],
                        item["course_id"],
                        item["class_id"],
                        item["task_type"],
                        item["title"],
                        item["status"],
                        item.get("start_time"),
                        item.get("deadline"),
                        item["source_update_time"],
                        item.get("url"),
                        item.get("note"),
                        item["source"],
                    )
                    for item in tasks
                ],
            )
            last_sync_time = (
                tasks[0]["source_update_time"]
                if tasks
                else datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            )
            connection.execute(
                """
                INSERT INTO settings(key, value) VALUES('last_sync_time', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (last_sync_time,),
            )
        return len(tasks)

    def query_tasks(
        self, task_type: str = "", keyword: str = "", limit: int = 50
    ) -> list[dict]:
        clauses = ["status = 'pending'"]
        params: list[str | int] = []
        if task_type:
            clauses.append("task_type = ?")
            params.append(task_type)
        if keyword:
            clauses.append("(course_name LIKE ? OR title LIKE ?)")
            params.extend((f"%{keyword}%", f"%{keyword}%"))
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM tasks WHERE "
                + " AND ".join(clauses)
                + " ORDER BY CASE WHEN deadline IS NULL OR deadline = '' THEN 1 ELSE 0 END, deadline, course_name LIMIT ?",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def query_deadlines(
        self, within_days: int, now: datetime | None = None
    ) -> list[dict]:
        current = now or datetime.now()
        end = current + timedelta(days=max(0, within_days))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM tasks
                WHERE status = 'pending' AND deadline > ? AND deadline <= ?
                ORDER BY deadline
                """,
                (
                    current.strftime("%Y-%m-%d %H:%M:%S"),
                    end.strftime("%Y-%m-%d %H:%M:%S"),
                ),
            ).fetchall()
        return [dict(row) for row in rows]

    def due_reminders(
        self, before_hours: int, now: datetime | None = None
    ) -> list[dict]:
        current = now or datetime.now()
        end = current + timedelta(hours=max(1, before_hours))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM tasks
                WHERE status = 'pending' AND deadline > ? AND deadline <= ?
                ORDER BY deadline
                """,
                (
                    current.strftime("%Y-%m-%d %H:%M:%S"),
                    end.strftime("%Y-%m-%d %H:%M:%S"),
                ),
            ).fetchall()
        return [dict(row) for row in rows]

    def status(self) -> dict:
        with self._connect() as connection:
            counts = {
                row["task_type"]: int(row["count"])
                for row in connection.execute(
                    "SELECT task_type, COUNT(*) AS count FROM tasks GROUP BY task_type"
                ).fetchall()
            }
            row = connection.execute(
                "SELECT value FROM settings WHERE key = 'last_sync_time'"
            ).fetchone()
        return {
            "count": sum(counts.values()),
            "counts": counts,
            "last_update_time": row["value"] if row else None,
        }

    def get_target_umo(self) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM settings WHERE key = 'target_umo'"
            ).fetchone()
        return str(row["value"]) if row else ""

    def set_target_umo(self, target_umo: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO settings(key, value) VALUES('target_umo', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (target_umo,),
            )

    def clear_target_umo(self) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM settings WHERE key = 'target_umo'")

    def claim_reminder(self, reminder_key: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO reminder_deliveries(reminder_key, status) VALUES (?, 'pending')",
                (reminder_key,),
            )
        return cursor.rowcount == 1

    def mark_delivered(self, reminder_key: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE reminder_deliveries SET status = 'delivered', delivered_at = CURRENT_TIMESTAMP
                WHERE reminder_key = ?
                """,
                (reminder_key,),
            )

    def release_reminder(self, reminder_key: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM reminder_deliveries WHERE reminder_key = ? AND status = 'pending'",
                (reminder_key,),
            )
