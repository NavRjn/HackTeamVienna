"""In-memory project state + append-only events.jsonl (placeholder until the graph package lands).

State = plan.json + replay(events). Events are only ever appended (reset truncates the demo file).
"""

import json
import re
import threading
import unicodedata
from datetime import date, datetime
from pathlib import Path
from typing import Any

from kraftsync.contracts import (
    Dependency,
    Discrepancy,
    EventKind,
    GraphEvent,
    Person,
    ProjectPlan,
    Task,
    TaskStatus,
    TaskUpdate,
    UpdateKind,
)

NEAR_DAYS = 7
MAX_CANDIDATES = 15


def _norm_name(name: str) -> str:
    """Case-, accent- and whitespace-insensitive: 'Marko  PETROVIC' == 'Marko Petrović'."""
    folded = unicodedata.normalize("NFKD", name).casefold()
    return " ".join("".join(c for c in folded if not unicodedata.combining(c)).split())


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"\w+", _norm_name(text)))


class ProjectState:
    def __init__(self, plan_path: Path, events_path: Path):
        self.plan_path = plan_path
        self.events_path = events_path
        self.lock = threading.RLock()
        self._init_from_plan()
        self._replay()

    def _init_from_plan(self) -> None:
        self.plan = ProjectPlan.model_validate_json(self.plan_path.read_text(encoding="utf-8"))
        self.persons: dict[str, Person] = {p.id: p for p in self.plan.persons}
        self.deps: list[Dependency] = list(self.plan.dependencies)
        self.tasks: dict[str, Task] = {t.id: t for t in self.plan.tasks}
        self.events: list[GraphEvent] = []
        self.discrepancies: list[Discrepancy] = []
        self._next_id = 1

    def _replay(self) -> None:
        if not self.events_path.exists():
            return
        for line in self.events_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            ev = GraphEvent.model_validate_json(line)
            self.events.append(ev)
            self._next_id = max(self._next_id, (ev.id or 0) + 1)
            if ev.kind == EventKind.task_changed and ev.task_id in self.tasks:
                merged = {**self.tasks[ev.task_id].model_dump(mode="json"), **ev.after}
                self.tasks[ev.task_id] = Task.model_validate(merged)
            elif ev.kind == EventKind.discrepancy_raised:
                self.discrepancies.append(Discrepancy.model_validate(ev.payload["discrepancy"]))

    def reset(self) -> None:
        with self.lock:
            self.events_path.parent.mkdir(parents=True, exist_ok=True)
            self.events_path.write_text("", encoding="utf-8")
            self._init_from_plan()

    # -- lookups ---------------------------------------------------------------------------

    def resolve_sender(self, full_name: str) -> Person | None:
        """Identify a sender by full name only (synthetic names in the demo; ids/phones are not used)."""
        wanted = _norm_name(full_name)
        return next((p for p in self.persons.values() if _norm_name(p.name) == wanted), None)

    def has_message(self, message_id: str) -> bool:
        return any(e.kind == EventKind.update_interpreted and e.source_message_id == message_id for e in self.events)

    def _locations_mentioned(self, text: str) -> set[str]:
        """Location ids whose name words all appear in the text (plus all their descendants)."""
        words = _tokens(text)
        hit = {loc.id for loc in self.plan.locations if _tokens(loc.name) <= words}
        grew = True
        while grew:   # add children of mentioned locations
            kids = {loc.id for loc in self.plan.locations if loc.parent_id in hit} - hit
            grew = bool(kids)
            hit |= kids
        return hit

    def candidates_for(self, person_id: str | None, today: date, text: str = "") -> list[Task]:
        """Deterministic shortlist for the LLM: the sender's own open tasks first, then tasks at locations
        named in the message, then anything near today."""
        person = self.persons.get(person_id) if person_id else None
        mentioned = self._locations_mentioned(text) if text else set()

        def own_open(t: Task) -> bool:
            return bool(person) and t.status != TaskStatus.done and (
                t.responsible_person_id == person.id or bool(person.trade and t.trade == person.trade))

        def near(t: Task) -> bool:
            dates = [t.planned_start, t.planned_finish, t.actual_start, t.actual_finish]
            return any(d is not None and abs((d - today).days) <= NEAR_DAYS for d in dates)

        def named(t: Task) -> bool:
            return t.location_id in mentioned

        pool = [t for t in self.tasks.values()
                if t.status != TaskStatus.cancelled and (own_open(t) or named(t) or near(t))]
        pool.sort(key=lambda t: (not own_open(t), not named(t), t.status == TaskStatus.done, t.planned_start))
        return pool[:MAX_CANDIDATES]

    # -- writes (append-only) ---------------------------------------------------------------

    def log(self, kind: EventKind, actor: str, now: datetime, *, task_id: str | None = None,
            source_message_id: str | None = None, before: dict | None = None,
            after: dict | None = None, payload: dict | None = None) -> GraphEvent:
        ev = GraphEvent(
            id=self._next_id, ts=now, kind=kind, actor=actor, task_id=task_id,
            source_message_id=source_message_id, before=before or {}, after=after or {}, payload=payload or {},
        )
        self._next_id += 1
        self.events.append(ev)
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        with self.events_path.open("a", encoding="utf-8") as f:
            f.write(ev.model_dump_json() + "\n")
        return ev

    def apply(self, u: TaskUpdate, actor: str, now: datetime) -> GraphEvent | None:
        """Apply an interpreted update to a task. Returns the task_changed event, or None if nothing changed."""
        t = self.tasks[u.task_id]
        ch: dict[str, Any] = {}
        if u.kind == UpdateKind.started:
            ch.update(status=TaskStatus.in_progress, actual_start=t.actual_start or u.reported_on)
        elif u.kind == UpdateKind.completed:
            ch.update(status=TaskStatus.done, actual_start=t.actual_start or u.reported_on,
                      actual_finish=u.reported_on, percent_complete=100)
        elif u.kind == UpdateKind.blocked:
            ch.update(status=TaskStatus.blocked)
        elif u.kind == UpdateKind.unblocked and t.status == TaskStatus.blocked:
            ch.update(status=TaskStatus.in_progress if t.actual_start else TaskStatus.not_started)
        if u.new_forecast_finish and u.kind != UpdateKind.completed:
            ch["forecast_finish"] = u.new_forecast_finish
        if u.percent_complete is not None and u.kind != UpdateKind.completed:
            ch["percent_complete"] = u.percent_complete
        ch = {k: v for k, v in ch.items() if getattr(t, k) != v}
        if not ch:
            return None
        new = t.model_copy(update=ch)
        self.tasks[t.id] = new
        dump_b, dump_a = t.model_dump(mode="json"), new.model_dump(mode="json")
        keys = [k for k in ch]
        return self.log(EventKind.task_changed, actor, now, task_id=t.id, source_message_id=u.message_id,
                        before={k: dump_b[k] for k in keys}, after={k: dump_a[k] for k in keys})

    def record_discrepancy(self, d: Discrepancy, now: datetime) -> GraphEvent:
        self.discrepancies.append(d)
        return self.log(EventKind.discrepancy_raised, "engine", now, task_id=d.task_id,
                        payload={"discrepancy": json.loads(d.model_dump_json())})
