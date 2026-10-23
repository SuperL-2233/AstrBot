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

                CREATE TABLE IF NOT EXISTS reminder_ignores (
                    task_key TEXT PRIMARY KEY,
                    reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS reminder_schedule (
                    schedule_key TEXT PRIMARY KEY,
                    task_key TEXT NOT NULL,
                    deadline TEXT NOT NULL,
                    offset_hours INTEGER NOT NULL,
                    due_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'scheduled',
                    delivered_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_reminder_schedule_due
                ON reminder_schedule(status, due_at);
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
        self,
        task_type: str = "",
        keyword: str = "",
        task_status: str = "pending",
        limit: int = 50,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list[str | int] = []
        if task_status:
            clauses.append("status = ?")
            params.append(task_status)
        if task_type:
            if task_type == "视频":
                clauses.append("task_type IN ('视频', '限时章节')")
            else:
                clauses.append("task_type = ?")
                params.append(task_type)
        if keyword:
            clauses.append("(course_name LIKE ? OR title LIKE ?)")
            params.extend((f"%{keyword}%", f"%{keyword}%"))
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM tasks"
                + (" WHERE " + " AND ".join(clauses) if clauses else "")
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

    def cache_state(self, max_age_hours: int, now: datetime | None = None) -> dict:
        current = now or datetime.now()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM settings WHERE key = 'last_sync_time'"
            ).fetchone()
        if not row:
            return {"stale": True, "last_sync_time": None, "age_hours": None}
        try:
            last_sync = datetime.strptime(row["value"], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return {
                "stale": True,
                "last_sync_time": row["value"],
                "age_hours": None,
            }
        age_hours = max(0.0, (current - last_sync).total_seconds() / 3600)
        return {
            "stale": age_hours >= max(1, max_age_hours),
            "last_sync_time": row["value"],
            "age_hours": round(age_hours, 2),
        }

    @staticmethod
    def _parse_timestamp(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None

    def sync_guard_state(self, now: datetime | None = None) -> dict:
        current = now or datetime.now()
        with self._connect() as connection:
            values = {
                row["key"]: row["value"]
                for row in connection.execute(
                    """
                    SELECT key, value FROM settings
                    WHERE key IN (
                        'sync_lease_owner', 'sync_lease_until',
                        'sync_backoff_until', 'sync_backoff_reason'
                    )
                    """
                ).fetchall()
            }
        lease_until = self._parse_timestamp(values.get("sync_lease_until"))
        backoff_until = self._parse_timestamp(values.get("sync_backoff_until"))
        return {
            "lease_active": bool(lease_until and lease_until > current),
            "lease_until": values.get("sync_lease_until") or None,
            "backoff_active": bool(backoff_until and backoff_until > current),
            "backoff_until": values.get("sync_backoff_until") or None,
            "backoff_reason": values.get("sync_backoff_reason") or "",
        }

    def claim_sync_lease(
        self,
        owner: str,
        lease_minutes: int = 30,
        now: datetime | None = None,
    ) -> bool:
        current = now or datetime.now()
        lease_until = current + timedelta(minutes=max(1, lease_minutes))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = {
                row["key"]: row["value"]
                for row in connection.execute(
                    """
                    SELECT key, value FROM settings
                    WHERE key IN ('sync_lease_owner', 'sync_lease_until')
                    """
                ).fetchall()
            }
            active_until = self._parse_timestamp(rows.get("sync_lease_until"))
            if (
                active_until
                and active_until > current
                and rows.get("sync_lease_owner") != owner
            ):
                return False
            for key, value in (
                ("sync_lease_owner", owner),
                ("sync_lease_until", lease_until.strftime("%Y-%m-%d %H:%M:%S")),
            ):
                connection.execute(
                    """
                    INSERT INTO settings(key, value) VALUES(?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (key, value),
                )
        return True

    def release_sync_lease(self, owner: str) -> None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM settings WHERE key = 'sync_lease_owner'"
            ).fetchone()
            if row and row["value"] == owner:
                connection.execute(
                    "DELETE FROM settings WHERE key IN ('sync_lease_owner', 'sync_lease_until')"
                )

    def set_sync_backoff(self, until: datetime, reason: str) -> None:
        with self._connect() as connection:
            for key, value in (
                ("sync_backoff_until", until.strftime("%Y-%m-%d %H:%M:%S")),
                ("sync_backoff_reason", reason),
            ):
                connection.execute(
                    """
                    INSERT INTO settings(key, value) VALUES(?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (key, value),
                )

    def clear_sync_backoff(self) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM settings WHERE key IN ('sync_backoff_until', 'sync_backoff_reason')"
            )

    def sync_reminder_schedule(
        self, offsets_hours: list[int], now: datetime | None = None
    ) -> int:
        """Replace obsolete pending stages and retain only one catch-up stage.

        Args:
            offsets_hours: Positive reminder lead times in hours.
            now: Current time in the deadline timezone, without timezone metadata.

        Returns:
            Number of newly inserted stages.
        """
        current = now or datetime.now()
        now_text = current.strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as connection:
            connection.execute(
                f"""
                DELETE FROM reminder_schedule
                WHERE status = 'scheduled' AND (
                  offset_hours NOT IN ({",".join("?" for _ in offsets_hours)})
                  OR NOT EXISTS (
                    SELECT 1 FROM tasks
                    LEFT JOIN reminder_ignores ON reminder_ignores.task_key = tasks.task_key
                    WHERE tasks.task_key = reminder_schedule.task_key
                      AND tasks.status = 'pending'
                      AND tasks.deadline = reminder_schedule.deadline
                      AND tasks.deadline > ?
                      AND reminder_ignores.task_key IS NULL
                  )
                )
                """,
                (*offsets_hours, now_text),
            )
            tasks = connection.execute(
                """
                SELECT tasks.task_key, tasks.deadline
                FROM tasks
                LEFT JOIN reminder_ignores ON reminder_ignores.task_key = tasks.task_key
                WHERE tasks.status = 'pending'
                  AND tasks.deadline IS NOT NULL AND tasks.deadline != ''
                  AND tasks.deadline > ?
                  AND reminder_ignores.task_key IS NULL
                """,
                (now_text,),
            ).fetchall()
            inserted = 0
            for task in tasks:
                try:
                    deadline = datetime.strptime(task["deadline"], "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue
                # A newly discovered task or a restart catches up only the
                # latest threshold, avoiding a burst of obsolete reminders.
                catch_up_offset = next(
                    (
                        offset
                        for offset in sorted(offsets_hours)
                        if deadline - timedelta(hours=offset) <= current
                    ),
                    None,
                )
                for offset_hours in offsets_hours:
                    due_at = deadline - timedelta(hours=offset_hours)
                    schedule_key = (
                        f"{task['task_key']}|{task['deadline']}|{offset_hours}"
                    )
                    if due_at <= current and offset_hours != catch_up_offset:
                        connection.execute(
                            "DELETE FROM reminder_schedule WHERE schedule_key = ? AND status = 'scheduled'",
                            (schedule_key,),
                        )
                        continue
                    cursor = connection.execute(
                        """
                        INSERT OR IGNORE INTO reminder_schedule(
                            schedule_key, task_key, deadline, offset_hours, due_at
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            schedule_key,
                            task["task_key"],
                            task["deadline"],
                            offset_hours,
                            due_at.strftime("%Y-%m-%d %H:%M:%S"),
                        ),
                    )
                    inserted += cursor.rowcount
        return inserted

    def due_scheduled_reminders(self, now: datetime | None = None) -> list[dict]:
        current = (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT reminder_schedule.schedule_key,
                       reminder_schedule.offset_hours,
                       reminder_schedule.due_at,
                       tasks.*
                FROM reminder_schedule
                JOIN tasks ON tasks.task_key = reminder_schedule.task_key
                LEFT JOIN reminder_ignores ON reminder_ignores.task_key = tasks.task_key
                WHERE reminder_schedule.status = 'scheduled'
                  AND reminder_schedule.due_at <= ?
                  AND reminder_schedule.deadline > ?
                  AND reminder_schedule.deadline = tasks.deadline
                  AND tasks.status = 'pending'
                  AND reminder_ignores.task_key IS NULL
                ORDER BY reminder_schedule.due_at, tasks.deadline
                """,
                (current, current),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_schedule_delivered(self, schedule_key: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE reminder_schedule
                SET status = 'delivered', delivered_at = CURRENT_TIMESTAMP
                WHERE schedule_key = ?
                """,
                (schedule_key,),
            )

    def find_trackable_tasks(
        self, keyword: str = "", task_key: str = "", limit: int = 10
    ) -> list[dict]:
        clauses = ["tasks.deadline IS NOT NULL", "tasks.deadline != ''"]
        params: list[str | int] = []
        if task_key:
            clauses.append("tasks.task_key = ?")
            params.append(task_key)
        elif keyword:
            clauses.append("(tasks.course_name LIKE ? OR tasks.title LIKE ?)")
            params.extend((f"%{keyword}%", f"%{keyword}%"))
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT tasks.*,
                       CASE WHEN reminder_ignores.task_key IS NULL THEN 1 ELSE 0 END AS tracked,
                       COALESCE(reminder_ignores.reason, '') AS ignore_reason
                FROM tasks
                LEFT JOIN reminder_ignores ON reminder_ignores.task_key = tasks.task_key
                WHERE """
                + " AND ".join(clauses)
                + " ORDER BY tasks.deadline, tasks.course_name LIMIT ?",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def set_task_ignored(self, task_key: str, ignored: bool, reason: str = "") -> None:
        with self._connect() as connection:
            if ignored:
                connection.execute(
                    """
                    INSERT INTO reminder_ignores(task_key, reason) VALUES(?, ?)
                    ON CONFLICT(task_key) DO UPDATE SET reason = excluded.reason
                    """,
                    (task_key, reason),
                )
                connection.execute(
                    "DELETE FROM reminder_schedule WHERE task_key = ? AND status = 'scheduled'",
                    (task_key,),
                )
            else:
                connection.execute(
                    "DELETE FROM reminder_ignores WHERE task_key = ?", (task_key,)
                )

    def reminder_schedule_status(self) -> dict:
        with self._connect() as connection:
            scheduled = connection.execute(
                "SELECT COUNT(*) AS count, MIN(due_at) AS next_due_at FROM reminder_schedule WHERE status = 'scheduled'"
            ).fetchone()
            delivered = connection.execute(
                "SELECT COUNT(*) AS count FROM reminder_schedule WHERE status = 'delivered'"
            ).fetchone()["count"]
            ignored = connection.execute(
                "SELECT COUNT(*) AS count FROM reminder_ignores"
            ).fetchone()["count"]
        return {
            "scheduled_stages": int(scheduled["count"]),
            "next_due_at": scheduled["next_due_at"],
            "delivered_stages": int(delivered),
            "ignored_tasks": int(ignored),
        }

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
            status_counts = {
                row["status"]: int(row["count"])
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS count FROM tasks GROUP BY status"
                ).fetchall()
            }
        return {
            "count": sum(counts.values()),
            "counts": counts,
            "status_counts": status_counts,
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
