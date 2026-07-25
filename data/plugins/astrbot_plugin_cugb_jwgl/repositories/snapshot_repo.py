from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..core.jwgl_core import ensure_private_file, utc_iso


class SnapshotRepository:
    """课表/成绩/考试的快照库：diff 检测变化，记录轮询历史与绑定信息。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS snapshots (
                kind TEXT NOT NULL,
                item_key TEXT NOT NULL,
                payload TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (kind, item_key)
            );
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS poll_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                status TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT ''
            );
            """
        )
        self.connection.commit()
        ensure_private_file(path)

    def close(self) -> None:
        self.connection.close()

    def get_meta(self, key: str, default: str = "") -> str:
        row = self.connection.execute(
            "SELECT value FROM meta WHERE key=?", (key,)
        ).fetchone()
        return str(row["value"]) if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.connection.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.connection.commit()

    def count(self, kind: str) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) AS n FROM snapshots WHERE kind=?", (kind,)
        ).fetchone()
        return int(row["n"])

    def items(self, kind: str) -> list[dict[str, str]]:
        rows = self.connection.execute(
            "SELECT payload FROM snapshots WHERE kind=? ORDER BY item_key", (kind,)
        ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def items_with_key(self, kind: str) -> list[tuple[str, dict[str, str]]]:
        rows = self.connection.execute(
            "SELECT item_key, payload FROM snapshots WHERE kind=? ORDER BY item_key",
            (kind,),
        ).fetchall()
        return [(row["item_key"], json.loads(row["payload"])) for row in rows]

    def clear_kind(self, kind: str) -> None:
        self.connection.execute("DELETE FROM snapshots WHERE kind=?", (kind,))
        self.connection.execute("DELETE FROM meta WHERE key=?", (f"baseline:{kind}",))
        self.connection.commit()

    def diff_and_update(
        self,
        kind: str,
        records: list[dict[str, str]],
        key_of: Callable[[dict[str, str]], str],
    ) -> tuple[bool, list[dict[str, str]], list[dict[str, str]]]:
        """返回 (是否首次建基线, 新增记录, 变化记录) 并落库最新快照。"""
        now = utc_iso()
        baseline_key = f"baseline:{kind}"
        is_baseline = self.get_meta(baseline_key) != "1"
        if is_baseline:
            self.set_meta(baseline_key, "1")
        new_records: list[dict[str, str]] = []
        changed_records: list[dict[str, str]] = []
        for record in records:
            key = key_of(record)
            payload = json.dumps(record, ensure_ascii=False, sort_keys=True)
            row = self.connection.execute(
                "SELECT payload FROM snapshots WHERE kind=? AND item_key=?",
                (kind, key),
            ).fetchone()
            if row is None:
                self.connection.execute(
                    "INSERT INTO snapshots(kind, item_key, payload, first_seen_at, updated_at)"
                    " VALUES(?, ?, ?, ?, ?)",
                    (kind, key, payload, now, now),
                )
                if not is_baseline:
                    new_records.append(record)
            elif row["payload"] != payload:
                self.connection.execute(
                    "UPDATE snapshots SET payload=?, updated_at=? WHERE kind=? AND item_key=?",
                    (payload, now, kind, key),
                )
                changed_records.append(record)
        self.connection.commit()
        return is_baseline, new_records, changed_records

    def record_poll(self, status: str, detail: str = "") -> None:
        self.connection.execute(
            "INSERT INTO poll_log(started_at, status, detail) VALUES(?, ?, ?)",
            (utc_iso(), status, detail),
        )
        self.connection.commit()

    def last_poll(self) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT started_at, status, detail FROM poll_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None
