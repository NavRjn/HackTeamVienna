"""Append-only event store: THE event log of the service. Events are never updated or deleted.

`EventStore()` keeps events in memory only (tests, playground). `EventStore("data/live/events.jsonl")`
also appends every event as one JSON line, and reads the file back on start (restart-safe).
Everything that must survive a restart (graph changes, discrepancies, the notification outbox)
is an event in this one log.
"""

from __future__ import annotations

import threading
from pathlib import Path

from kraftsync.contracts import GraphEvent


class EventStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = None if path in (None, ":memory:") else Path(path)
        self._lock = threading.Lock()
        self._events: list[GraphEvent] = []
        self._next_id = 1
        if self.path is not None and self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    ev = GraphEvent.model_validate_json(line)
                    self._events.append(ev)
                    self._next_id = max(self._next_id, (ev.id or 0) + 1)

    def append(self, event: GraphEvent) -> GraphEvent:
        with self._lock:
            ev = event.model_copy(update={"id": self._next_id})
            if self.path is not None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(ev.model_dump_json() + "\n")
            self._next_id += 1
            self._events.append(ev)
        return ev

    def since(self, after_id: int = 0) -> list[GraphEvent]:
        with self._lock:
            return [e for e in self._events if (e.id or 0) > after_id]

    def all(self) -> list[GraphEvent]:
        return self.since(0)

    def clear(self) -> None:
        """Demo reset only (POST /reset). Never call this in the live pipeline."""
        with self._lock:
            self._events.clear()
            self._next_id = 1
            if self.path is not None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text("", encoding="utf-8")

    def close(self) -> None:
        """Nothing to release; kept so callers can treat the store like a resource."""
