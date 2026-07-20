from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path


class CourseRepository:
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
                CREATE TABLE IF NOT EXISTS courses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    course_name TEXT NOT NULL,
                    start_time TEXT NOT NULL,
                    end_time TEXT NOT NULL,
                    location TEXT NOT NULL,
                    teacher TEXT,
                    weeks TEXT,
                    weekday INTEGER,
                    source TEXT NOT NULL,
                    raw_data TEXT
                )
                """
            )
            conn.commit()

    def replace_all(self, courses: list[dict]) -> int:
        with self._connect() as conn:
            conn.execute("DELETE FROM courses")
            conn.executemany(
                """
                INSERT INTO courses (
                    course_name, start_time, end_time, location,
                    teacher, weeks, weekday, source, raw_data
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        course["course_name"],
                        course["start_time"],
                        course["end_time"],
                        course["location"],
                        course.get("teacher"),
                        course.get("weeks"),
                        course.get("weekday"),
                        course.get("source", "local_timetable"),
                        course.get("raw_data"),
                    )
                    for course in courses
                ],
            )
            conn.commit()
        return len(courses)

    def get_courses_by_date(self, target_date: str) -> list[dict]:
        start = f"{target_date} 00:00"
        end = f"{target_date} 23:59"
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM courses
                WHERE start_time BETWEEN ? AND ?
                ORDER BY start_time
                """,
                (start, end),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_next_course(self, now_value: datetime) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM courses
                WHERE start_time > ?
                ORDER BY start_time
                LIMIT 1
                """,
                (now_value.strftime("%Y-%m-%d %H:%M"),),
            ).fetchone()
        return dict(row) if row else None

    def get_current_course(self, now_value: datetime) -> dict | None:
        current = now_value.strftime("%Y-%m-%d %H:%M")
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM courses
                WHERE start_time <= ? AND end_time >= ?
                ORDER BY start_time
                LIMIT 1
                """,
                (current, current),
            ).fetchone()
        return dict(row) if row else None

    def search_courses(self, keyword: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM courses
                WHERE course_name LIKE ?
                ORDER BY start_time
                """,
                (f"%{keyword}%",),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_course_dates(self, keyword: str) -> list[dict]:
        return self.search_courses(keyword)

    def count_courses(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM courses").fetchone()
        return int(row["count"])
