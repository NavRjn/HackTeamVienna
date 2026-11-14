"""Feed already-interpreted reports into a ProjectGraph (demo history, gold labels, manual updates).

This is NOT the engine: no text is interpreted here. Each JSONL line already carries its
interpretation, either flat or under "gold" (data/demo/messages.jsonl):

    {"id": "hist:01", "date": "2026-11-02", "sender": "P-DRY", "text": "...",
     "task_id": "T5-FRAME", "kind": "started"}

Optional: new_forecast_finish, percent_complete, blocker, change_description.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from kraftsync.contracts import Discrepancy, TaskUpdate, UpdateKind
from kraftsync.graph.project_graph import ProjectGraph

EXTRA_FIELDS = ("new_forecast_finish", "percent_complete", "blocker", "change_description")


@dataclass
class Report:
    id: str
    text: str
    sender: str
    day: date | None
    task_id: str | None
    kind: UpdateKind
    extra: dict = field(default_factory=dict)
    expected: list[str] | None = None        # gold "expected_discrepancies", if present

    def to_update(self, day: date, confidence: float = 0.95) -> TaskUpdate:
        return TaskUpdate(message_id=self.id, task_id=self.task_id, kind=self.kind, confidence=confidence,
                          reported_on=day, rationale=self.text[:80] or "feed", model="feed", **self.extra)


def read_reports(path: str | Path) -> list[Report]:
    """Missing file -> []. Raises ValueError with file:line on a bad line."""
    path = Path(path)
    if not path.exists():
        return []
    out = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            g = row.get("gold", row)
            day = row.get("date") or str(row.get("received_at", ""))[:10] or None
            out.append(Report(
                id=row.get("id") or f"{path.stem}:{n}",
                text=row.get("text", ""),
                sender=row.get("sender") or g.get("sender_person_id") or row.get("sender_ref") or "unknown",
                day=date.fromisoformat(day) if day else None,
                task_id=g.get("task_id"),
                kind=UpdateKind(g.get("kind", "irrelevant")),
                extra={k: g[k] for k in EXTRA_FIELDS if g.get(k) is not None},
                expected=g.get("expected_discrepancies"),
            ))
        except (ValueError, KeyError) as e:
            raise ValueError(f"{path}:{n}: {e}") from e
    return out


def feed(pg: ProjectGraph, reports: list[Report]) -> list[tuple[Report, list[Discrepancy]]]:
    """Apply reports in order. `today` follows the report dates and never moves backwards."""
    out = []
    for r in reports:
        day = r.day or pg.today
        pg.today = max(pg.today, day)
        out.append((r, pg.detect(pg.apply(r.to_update(day), actor=r.sender))))
    return out
