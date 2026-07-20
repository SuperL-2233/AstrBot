from __future__ import annotations

import sqlite3
from pathlib import Path


class NotificationRepository:
    """Persist the outbound target and webhook delivery idempotency state."""

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
                CREATE TABLE IF NOT EXISTS notification_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS notification_deliveries (
                    event_id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    delivered_at TEXT
                );
                """
            )

    def get_target_umo(self) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM notification_settings WHERE key = 'target_umo'"
            ).fetchone()
        return str(row["value"]) if row else ""

    def set_target_umo(self, target_umo: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO notification_settings(key, value) VALUES('target_umo', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (target_umo,),
            )

    def clear_target_umo(self) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM notification_settings WHERE key = 'target_umo'")

    def claim_event(self, event_id: str, source: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO notification_deliveries(event_id, source, status)
                VALUES(?, ?, 'pending')
                """,
                (event_id, source),
            )
        return cursor.rowcount == 1

    def mark_delivered(self, event_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE notification_deliveries
                SET status = 'delivered', delivered_at = CURRENT_TIMESTAMP
                WHERE event_id = ?
                """,
                (event_id,),
            )

    def release_event(self, event_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM notification_deliveries WHERE event_id = ? AND status = 'pending'",
                (event_id,),
            )
