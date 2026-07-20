from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path


class ClassroomRepository:
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
                CREATE TABLE IF NOT EXISTS classrooms (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    building TEXT NOT NULL,
                    room TEXT NOT NULL,
                    start_time TEXT NOT NULL,
                    end_time TEXT NOT NULL,
                    capacity INTEGER,
                    source_update_time TEXT,
                    source TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS classroom_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cache_date TEXT NOT NULL,
                    building TEXT NOT NULL,
                    room TEXT NOT NULL,
                    capacity INTEGER,
                    occupied_sections TEXT,
                    source_update_time TEXT NOT NULL,
                    source TEXT NOT NULL,
                    raw_data TEXT NOT NULL,
                    UNIQUE(cache_date, building, room)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS classroom_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def replace_all(self, classrooms: list[dict]) -> int:
        with self._connect() as conn:
            conn.execute("DELETE FROM classrooms")
            conn.executemany(
                """
                INSERT INTO classrooms (
                    building, room, start_time, end_time,
                    capacity, source_update_time, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        item["building"],
                        item["room"],
                        item["start_time"],
                        item["end_time"],
                        item.get("capacity"),
                        item.get("source_update_time"),
                        item.get("source", "cache"),
                    )
                    for item in classrooms
                ],
            )
            conn.commit()
        return len(classrooms)

    def query(self, building: str = "", start_time: str = "", end_time: str = "") -> list[dict]:
        sql = "SELECT * FROM classrooms WHERE 1=1"
        params: list[str] = []
        if building:
            sql += " AND building = ?"
            params.append(building)
        if start_time:
            sql += " AND start_time <= ?"
            params.append(start_time)
        if end_time:
            sql += " AND end_time >= ?"
            params.append(end_time)
        sql += " ORDER BY building, room"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def last_update_time(self) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT source_update_time FROM classroom_cache
                ORDER BY source_update_time DESC
                LIMIT 1
                """
            ).fetchone()
            if not row:
                row = conn.execute(
                    "SELECT source_update_time FROM classrooms ORDER BY source_update_time DESC LIMIT 1"
                ).fetchone()
        return row["source_update_time"] if row and row["source_update_time"] else None

    def count(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM classroom_cache").fetchone()
        return int(row["count"])

    def replace_cache(self, cache_date: str, classrooms: list[dict], source_update_time: str) -> int:
        with self._connect() as conn:
            conn.execute("DELETE FROM classroom_cache WHERE cache_date = ?", (cache_date,))
            conn.executemany(
                """
                INSERT OR REPLACE INTO classroom_cache (
                    cache_date, building, room, capacity, occupied_sections,
                    source_update_time, source, raw_data
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        cache_date,
                        item["building"],
                        item["room"],
                        item.get("capacity"),
                        ",".join(item.get("occupied_sections", [])),
                        source_update_time,
                        item.get("source", "mportal"),
                        json.dumps(item.get("raw_data", item), ensure_ascii=False),
                    )
                    for item in classrooms
                ],
            )
            conn.commit()
        return len(classrooms)

    def query_cache(self, cache_date: str, building: str = "") -> list[dict]:
        sql = "SELECT * FROM classroom_cache WHERE cache_date = ?"
        params = [cache_date]
        if building:
            sql += " AND building = ?"
            params.append(building)
        sql += " ORDER BY building, room"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_cache_item(row) for row in rows]

    def has_cache(self, cache_date: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM classroom_cache WHERE cache_date = ?",
                (cache_date,),
            ).fetchone()
        return int(row["count"]) > 0

    def is_cache_expired(self, expire_minutes: int) -> bool:
        last_update = self.last_update_time()
        if not last_update:
            return True
        try:
            updated_at = datetime.strptime(last_update, "%Y-%m-%d %H:%M")
        except ValueError:
            return True
        return datetime.now() - updated_at > timedelta(minutes=expire_minutes)

    def set_metadata(self, key: str, value: dict, updated_at: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO classroom_metadata (key, value, updated_at)
                VALUES (?, ?, ?)
                """,
                (key, json.dumps(value, ensure_ascii=False), updated_at),
            )
            conn.commit()

    def get_metadata(self, key: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM classroom_metadata WHERE key = ?",
                (key,),
            ).fetchone()
        if not row:
            return None
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _row_to_cache_item(row: sqlite3.Row) -> dict:
        raw_data = {}
        try:
            raw_data = json.loads(row["raw_data"])
        except json.JSONDecodeError:
            raw_data = {}
        occupied_sections = [
            value.strip()
            for value in str(row["occupied_sections"] or "").split(",")
            if value.strip()
        ]
        return {
            "building": row["building"],
            "room": row["room"],
            "capacity": row["capacity"],
            "occupied_sections": occupied_sections,
            "source_update_time": row["source_update_time"],
            "source": row["source"],
            "raw_data": raw_data,
        }
