from __future__ import annotations

import sqlite3
from pathlib import Path


class HomeworkRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS homework (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    course_name TEXT NOT NULL,
                    title TEXT NOT NULL,
                    deadline TEXT NOT NULL,
                    status TEXT NOT NULL,
                    source_update_time TEXT,
                    url TEXT,
                    source TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def replace_all(self, homework_items: list[dict]) -> int:
        with self._connect() as conn:
            conn.execute("DELETE FROM homework")
            conn.executemany(
                """
                INSERT INTO homework (
                    course_name, title, deadline, status,
                    source_update_time, url, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        item["course_name"],
                        item["title"],
                        item["deadline"],
                        item["status"],
                        item.get("source_update_time"),
                        item.get("url"),
                        item.get("source", "chaoxing_cache"),
                    )
                    for item in homework_items
                ],
            )
            conn.commit()
        return len(homework_items)

    def query_pending(self, course_name: str = "") -> list[dict]:
        sql = "SELECT * FROM homework WHERE status = 'pending'"
        params: list[str] = []
        if course_name:
            sql += " AND course_name LIKE ?"
            params.append(f"%{course_name}%")
        sql += " ORDER BY deadline"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def query_recent(self, limit: int = 10) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM homework WHERE status = 'pending' ORDER BY deadline LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def last_update_time(self) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT source_update_time FROM homework ORDER BY source_update_time DESC LIMIT 1"
            ).fetchone()
        return row["source_update_time"] if row and row["source_update_time"] else None

    def count(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM homework").fetchone()
        return int(row["count"])
