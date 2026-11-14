"""Append-only event store (SQLite). Events are never updated or deleted."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from kraftsync.contracts import GraphEvent

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    actor TEXT NOT NULL,
    task_id TEXT,
    source_message_id TEXT,
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    payload_json TEXT NOT NULL
)
"""


class EventStore:
    """`EventStore(":memory:")` for tests, `EventStore("data/kraftsync.db")` for real runs."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute(_SCHEMA)
        self._db.commit()
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def append(self, event: GraphEvent) -> GraphEvent:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO events (ts, kind, actor, task_id, source_message_id, "
                "before_json, after_json, payload_json) VALUES (?,?,?,?,?,?,?,?)",
                (
                    event.ts.isoformat(), event.kind.value, event.actor, event.task_id,
                    event.source_message_id,
                    json.dumps(event.before, default=str), json.dumps(event.after, default=str),
                    json.dumps(event.payload, default=str),
                ),
            )
            self._db.commit()
            new_id = cur.lastrowid
        return event.model_copy(update={"id": new_id})

    def since(self, after_id: int = 0) -> list[GraphEvent]:
        with self._lock:   # one shared connection: reads must not interleave with writes from other threads
            rows = self._db.execute(
                "SELECT id, ts, kind, actor, task_id, source_message_id, before_json, after_json, "
                "payload_json FROM events WHERE id > ? ORDER BY id", (after_id,)
            ).fetchall()
        return [
            GraphEvent(
                id=r[0], ts=r[1], kind=r[2], actor=r[3], task_id=r[4], source_message_id=r[5],
                before=json.loads(r[6]), after=json.loads(r[7]), payload=json.loads(r[8]),
            )
            for r in rows
        ]

    def all(self) -> list[GraphEvent]:
        return self.since(0)

    def clear(self) -> None:
        """Demo reset only (POST /reset). Never call this in the live pipeline."""
        with self._lock:
            self._db.execute("DELETE FROM events")
            self._db.commit()
