"""Service-side state: the thin layer between the HTTP API and the graph.

Everything about the project itself (tasks, schedule, applying updates, detecting conflicts, the event log,
who-may-be-meant shortlists) lives in `kraftsync.graph.ProjectGraph`: that is the single point of control.
This module only adds what the service needs around it:
  * the plan with Telegram ids typed into persons.csv,
  * seeding the demo history once, on a fresh log,
  * the notification outbox (events in the same log, so it survives restarts),
  * the derived snapshot file.

Two kinds of files:
  * baseline (static, committed): data/demo/plan.json (+ persons.csv, history.jsonl). Never written by the service.
  * live (dynamic, gitignored, written by the service): <live dir>/events.jsonl (append-only truth),
    graph.json (derived snapshot) and the call log.
State = baseline + replay(live events). Reset empties the live events and re-seeds the history (demo only).
"""

import csv
import logging
import os
import threading
from pathlib import Path

from kraftsync.contracts import Action, ActionStatus, EventKind, GraphEvent, Person, ProjectPlan
from kraftsync.graph import EventStore, ProjectGraph, feed, read_reports

log = logging.getLogger("kraftsync.state")


class ProjectState:
    def __init__(self, plan_path: Path, events_path: Path, snapshot_path: Path | None = None,
                 history_path: Path | None = None, confidence_min: float | None = None):
        self.plan_path = plan_path
        self.events_path = events_path
        self.snapshot_path = snapshot_path
        self.history_path = history_path if history_path is not None else plan_path.with_name("history.jsonl")
        self.confidence_min = confidence_min
        self.lock = threading.RLock()
        self.store = EventStore(events_path)
        self._build()

    # -- load ------------------------------------------------------------------------------

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

    def _build(self) -> None:
        plan = ProjectPlan.model_validate_json(self.plan_path.read_text(encoding="utf-8"))
        csv_ids = self._telegram_ids_from_csv()
        plan.persons = [p.model_copy(update={"telegram_chat_id": csv_ids[p.id]}) if p.id in csv_ids else p
                        for p in plan.persons]
        fresh = not self.store.all()
        self.graph = ProjectGraph.from_plan(plan, self.store, today=plan.data_date,
                                            confidence_min=self.confidence_min)
        if fresh:   # demo story: baseline + what happened until "today", through the same apply() as everything else
            feed(self.graph, read_reports(self.history_path))
        self.actions: dict[str, Action] = {}        # the outbox: drafted -> sent | failed (rebuilt from events)
        self.action_detail: dict[str, str | None] = {}
        for ev in self.store.all():
            if ev.kind == EventKind.action_drafted:
                a = Action.model_validate(ev.payload["action"])
                self.actions[a.id] = a
            elif ev.kind == EventKind.action_sent and ev.payload.get("action_id") in self.actions:
                aid = ev.payload["action_id"]
                self.actions[aid] = self.actions[aid].model_copy(update={"status": ActionStatus(ev.payload["status"])})
                self.action_detail[aid] = ev.payload.get("detail")
        with_id = [p.id for p in plan.persons if p.telegram_chat_id is not None]
        without = [p.id for p in plan.persons if p.telegram_chat_id is None]
        log.info("plan loaded: %s | %d persons, %d with telegram id (%d from persons.csv)%s",
                 self.plan_path.name, len(plan.persons), len(with_id), len(csv_ids),
                 f" | WITHOUT id (unknown to /context): {', '.join(without)}" if without else "")

    def reset(self) -> None:
        with self.lock:
            self.store.clear()
            self._build()

    # -- lookups ---------------------------------------------------------------------------

    @property
    def persons(self) -> dict[str, Person]:
        return self.graph.persons

    @property
    def events(self) -> list[GraphEvent]:
        return self.store.all()

    def resolve_sender(self, sender_id: int) -> Person | None:
        """Identify a sender by Telegram user id (`Person.telegram_chat_id` in the plan). Unknown id -> None."""
        return next((p for p in self.persons.values()
                     if p.telegram_chat_id is not None and p.telegram_chat_id == sender_id), None)

    def has_open_clarification(self, person_id: str) -> bool:
        """Did we ask this person which task they meant and not acknowledged an answer since?"""
        open_ask = False
        for e in self.store.all():
            if e.kind != EventKind.action_drafted or e.payload["action"]["to_person_id"] != person_id:
                continue
            aid = e.payload["action"]["id"]
            if f":clarify:{person_id}" in aid:
                open_ask = True
            elif f":ack:{person_id}" in aid:
                open_ask = False
        return open_ask

    # -- outbox (events in the one log) ------------------------------------------------------

    def record_action(self, a: Action, source_message_id: str | None = None) -> GraphEvent:
        """Put a drafted Action into the outbox (an event, so it survives restarts)."""
        self.actions[a.id] = a
        return self.graph.record(EventKind.action_drafted, "policy", source_message_id=source_message_id,
                                 payload={"action": a.model_dump(mode="json")})

    def record_action_result(self, action_id: str, status: ActionStatus, detail: str | None) -> Action:
        """The sender (n8n) reports delivery. Idempotent; `sent` is final. KeyError = unknown id, ValueError = conflict."""
        current = self.actions[action_id]
        if current.status == status:
            return current
        if current.status == ActionStatus.sent:
            raise ValueError("action is already sent")
        self.graph.record(EventKind.action_sent, "n8n",
                          payload={"action_id": action_id, "status": status.value, "detail": detail})
        self.actions[action_id] = current.model_copy(update={"status": status})
        self.action_detail[action_id] = detail
        return self.actions[action_id]

    def write_snapshot(self, text: str) -> None:
        """Atomically rewrite the derived live graph file (the UI or a teammate can read it)."""
        if self.snapshot_path is None:
            return
        self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.snapshot_path.with_suffix(".json.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self.snapshot_path)
