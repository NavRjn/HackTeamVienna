"""In-memory project state + append-only events.jsonl (placeholder until the graph package lands).

State = plan.json + replay(events). Events are only ever appended (reset truncates the demo file).
"""

import csv
import json
import logging
import re
import threading
import unicodedata
from datetime import date, datetime, timedelta
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

log = logging.getLogger("kraftsync.state")

DATE_WINDOW_DAYS = 3       # a task is "on this date" if it is active within +-3 days of today
MAX_TASKS = 15             # cap per list (`tasks`, `related_tasks`)


def _fold(text: str) -> str:
    """Case-, accent- and whitespace-insensitive form, used to match location names in message text."""
    folded = unicodedata.normalize("NFKD", text).casefold()
    return " ".join("".join(c for c in folded if not unicodedata.combining(c)).split())


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"\w+", _fold(text)))


def _active_around(t: Task, today: date) -> bool:
    """Is this task relevant on `today`? In progress/blocked, overlapping the +-3 day window, overdue and still
    open, or finished within the window (people report on what they just finished)."""
    lo, hi = today - timedelta(days=DATE_WINDOW_DAYS), today + timedelta(days=DATE_WINDOW_DAYS)
    if t.status == TaskStatus.cancelled:
        return False
    if t.status == TaskStatus.done:
        return t.actual_finish is not None and lo <= t.actual_finish <= hi
    if t.status in (TaskStatus.in_progress, TaskStatus.blocked):
        return True
    last = max(t.planned_finish, t.forecast_finish or t.planned_finish)
    return last < lo or (t.planned_start <= hi and last >= lo)


class ProjectState:
    def __init__(self, plan_path: Path, events_path: Path):
        self.plan_path = plan_path
        self.events_path = events_path
        self.lock = threading.RLock()
        self._init_from_plan()
        self._replay()

    def _telegram_ids_from_csv(self) -> dict[str, int]:
        """Telegram ids typed into persons.csv (next to the plan) count too: person id -> id. Blank/invalid rows are skipped."""
        path = self.plan_path.with_name("persons.csv")
        if not path.exists():
            return {}
        ids: dict[str, int] = {}
        with path.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                raw = (row.get("telegram_chat_id") or "").strip().strip('"')
                if not raw:
                    continue
                try:
                    ids[row["id"]] = int(raw)
                except ValueError:
                    log.warning("persons.csv: ignoring non-numeric telegram_chat_id for %s", row.get("id"))
        return ids

    def _init_from_plan(self) -> None:
        self.plan = ProjectPlan.model_validate_json(self.plan_path.read_text(encoding="utf-8"))
        csv_ids = self._telegram_ids_from_csv()
        self.plan.persons = [p.model_copy(update={"telegram_chat_id": csv_ids[p.id]}) if p.id in csv_ids else p
                             for p in self.plan.persons]
        self.persons: dict[str, Person] = {p.id: p for p in self.plan.persons}
        self.deps: list[Dependency] = list(self.plan.dependencies)
        self.tasks: dict[str, Task] = {t.id: t for t in self.plan.tasks}
        self.events: list[GraphEvent] = []
        self.discrepancies: list[Discrepancy] = []
        self._next_id = 1
        with_id = [p.id for p in self.plan.persons if p.telegram_chat_id is not None]
        without = [p.id for p in self.plan.persons if p.telegram_chat_id is None]
        log.info("plan loaded: %s | %d persons, %d with telegram id (%d from persons.csv)%s",
                 self.plan_path.name, len(self.plan.persons), len(with_id), len(csv_ids),
                 f" | WITHOUT id (unknown to /context): {', '.join(without)}" if without else "")

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

    def resolve_sender(self, sender_id: int) -> Person | None:
        """Identify a sender by Telegram user id (`Person.telegram_chat_id` in the plan). Unknown id -> None."""
        return next((p for p in self.persons.values()
                     if p.telegram_chat_id is not None and p.telegram_chat_id == sender_id), None)

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

    def context_for(self, person_id: str | None, today: date, text: str = "") -> tuple[list[Task], list[Task]]:
        """Deterministic shortlists for the LLM, kept apart on purpose.

        tasks:         the sender's own tasks (assigned to them or to their trade) that are active around `today`.
        related_tasks: everyone else's tasks that may be what the message is about: tasks at locations named in
                       the text, direct predecessors/successors of the sender's tasks, other tasks active around
                       `today`. Lower authority: the prompt should prefer `tasks`.
        Unknown sender -> both empty.
        """
        person = self.persons.get(person_id) if person_id else None
        if person is None:
            return [], []

        def own(t: Task) -> bool:
            return t.responsible_person_id == person.id or bool(person.trade and t.trade == person.trade)

        def by_date(t: Task) -> tuple:
            return (t.status == TaskStatus.done, t.planned_start)

        mine = sorted((t for t in self.tasks.values() if own(t) and _active_around(t, today)), key=by_date)
        mine_ids = {t.id for t in mine}
        neighbours = {d.successor_id for d in self.deps if d.predecessor_id in mine_ids} | \
                     {d.predecessor_id for d in self.deps if d.successor_id in mine_ids}
        mentioned = self._locations_mentioned(text) if text else set()

        # own tasks outside the +-3 day window stay hidden, unless the message names their location
        related = [t for t in self.tasks.values()
                   if t.id not in mine_ids and t.status != TaskStatus.cancelled
                   and (t.location_id in mentioned
                        or (not own(t) and (t.id in neighbours or _active_around(t, today))))]
        related.sort(key=lambda t: (t.location_id not in mentioned, t.id not in neighbours, *by_date(t)))
        return mine[:MAX_TASKS], related[:MAX_TASKS]

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
