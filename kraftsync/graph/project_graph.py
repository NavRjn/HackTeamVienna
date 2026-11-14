"""ProjectGraph: the live dependency graph. 100% deterministic, no LLM, no network.

    nodes = Task ids (the Task objects live in self.tasks)
    edges = Dependency (type, lag, reason)

State changes ONLY through apply(), which appends GraphEvents to the store.
State = plan + replay(task_changed events).

Efficiency: the structure is compiled once (cpm.Net: topological order + adjacency lists) and the
CPM schedule is cached until the state or `today` changes, so one apply()+detect() costs one CPM pass.
"""

from __future__ import annotations

import os
import re
import unicodedata
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import networkx as nx

from kraftsync.contracts import (
    Discrepancy,
    DiscrepancyType,
    EventKind,
    GraphEvent,
    Impact,
    ProjectPlan,
    Severity,
    Task,
    TaskStatus,
    TaskUpdate,
    UpdateKind,
)
from kraftsync.graph.cpm import Net, Slot, schedule
from kraftsync.graph.store import EventStore

TZ = ZoneInfo(os.getenv("TZ") or "Europe/Vienna")
LIVE_FIELDS = ("status", "actual_start", "actual_finish", "forecast_finish", "percent_complete")
DATE_FIELDS = {"actual_start", "actual_finish", "forecast_finish"}
NO_STATE_CHANGE = {UpdateKind.question, UpdateKind.irrelevant, UpdateKind.modified}
OVERDUE_QUIET_DAYS = 1      # no report today or yesterday (~24 h) -> silent_overdue
OVERDUE_WARN_DAYS = 2       # overdue for 2+ days (~48 h) -> warning instead of info
DEFAULT_CONFIDENCE_MIN = 0.7  # only a default for scripts/tests; the service passes Settings.confidence_min
DATE_WINDOW_DAYS = 3        # a task is "on this date" if it is active within +-3 days of today
MAX_TASKS = 15              # cap per list in context_for()


def _now() -> datetime:
    return datetime.now(TZ)


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


def _live(t: Task) -> dict:
    return {f: (getattr(t, f).value if f == "status" else getattr(t, f)) for f in LIVE_FIELDS}


def _jsonable(d: dict) -> dict:
    return {k: (v.isoformat() if isinstance(v, date) else v) for k, v in d.items()}


def _with(t: Task, changes: dict[str, Any]) -> Task:
    """Copy of t with live fields changed. Accepts python or JSON values (replay). Cheaper than re-validating."""
    typed: dict[str, Any] = {}
    for k, v in changes.items():
        if k == "status":
            v = TaskStatus(v)
        elif k in DATE_FIELDS and isinstance(v, str):
            v = date.fromisoformat(v)
        typed[k] = v
    return t.model_copy(update=typed)


class ProjectGraph:
    def __init__(self, plan: ProjectPlan, store: EventStore, today: date | None = None,
                 confidence_min: float | None = None) -> None:
        self.plan = plan
        self.store = store
        self.confidence_min = DEFAULT_CONFIDENCE_MIN if confidence_min is None else confidence_min   # service: Settings
        self.tasks: dict[str, Task] = {t.id: t.model_copy(deep=True) for t in plan.tasks}
        self.persons = {p.id: p for p in plan.persons}
        self.g = nx.DiGraph()
        self.g.add_nodes_from(self.tasks)
        for d in plan.dependencies:
            for end in (d.predecessor_id, d.successor_id):
                if end not in self.tasks:
                    raise ValueError(f"dependency {d.id} refers to unknown task {end}")
            self.g.add_edge(d.predecessor_id, d.successor_id,
                            type=d.type.value, lag=d.lag_days, reason=d.reason, id=d.id)
        if not nx.is_directed_acyclic_graph(self.g):
            raise ValueError(f"plan has a dependency cycle: {nx.find_cycle(self.g)}")
        self.net = Net.compile(self.g)
        self._cache: dict[str, Slot] | None = None
        self._rows: dict[str, dict] = {}              # task id -> model_dump(mode="json"), per changed task
        self._snap: dict | None = None                # last snapshot(), valid while _cache is
        self._last_report: dict[str, date] = {}       # task id -> last day someone reported on it
        self._raised: set[str] = set()                # discrepancy ids already logged (dedupe)
        self.discrepancies: list[Discrepancy] = []    # everything raised so far (the "open" list of GET /state)
        self._today = today or date.today()   # the service sets it per request (Settings.demo_today wins)

    # ------------------------------------------------------------------ build

    @classmethod
    def from_plan(cls, plan: ProjectPlan, store: EventStore, today: date | None = None,
                  confidence_min: float | None = None) -> ProjectGraph:
        """New store -> logs plan_loaded. Existing store -> replays it (restart-safe)."""
        pg = cls(plan, store, today, confidence_min)
        events = store.all()
        if not events:
            pg.record(EventKind.plan_loaded, "system",
                      payload={"project_id": plan.project_id, "tasks": len(plan.tasks)})
        else:
            pg._replay(events)
        return pg

    def _replay(self, events: list[GraphEvent]) -> None:
        for e in events:
            if e.kind in (EventKind.task_changed, EventKind.manual_override) and e.task_id in self.tasks:
                self.tasks[e.task_id] = _with(self.tasks[e.task_id], e.after)
                self._rows.pop(e.task_id, None)
            elif e.kind == EventKind.update_interpreted:
                self._note_report(TaskUpdate.model_validate(e.payload["update"]))
            elif e.kind == EventKind.discrepancy_raised:
                d = Discrepancy.model_validate(e.payload.get("discrepancy", e.payload))
                self._raised.add(d.id)
                self.discrepancies.append(d)
        self._cache = self._snap = None

    def _note_report(self, u: TaskUpdate) -> None:
        if u.task_id in self.tasks:
            prev = self._last_report.get(u.task_id)
            self._last_report[u.task_id] = max(prev, u.reported_on) if prev else u.reported_on

    # ------------------------------------------------------------------ queries

    @property
    def today(self) -> date:
        return self._today

    @today.setter
    def today(self, value: date) -> None:
        if value != self._today:
            self._today = value
            self._cache = self._snap = None

    def _slots(self) -> dict[str, Slot]:
        if self._cache is None:
            self._cache = schedule(self.net, self.tasks, self._today)
        return self._cache

    def schedule(self) -> dict[str, tuple[date, date]]:
        return {n: (s.start, s.finish) for n, s in self._slots().items()}

    def project_finish(self, slots: dict[str, Slot] | None = None) -> date:
        return max(s.finish for s in (slots or self._slots()).values())

    def critical_path(self, slots: dict[str, Slot] | None = None) -> list[str]:
        slots = slots or self._slots()
        return [n for n in self.net.order
                if slots[n].total_float <= 0
                and self.tasks[n].status not in (TaskStatus.done, TaskStatus.cancelled)]

    def reach(self, task_id: str, levels: int) -> dict[str, int]:
        """Tasks within `levels` dependency steps downstream of task_id, with their distance (the task itself = 0)."""
        return nx.single_source_shortest_path_length(self.g, task_id, cutoff=levels)

    def final_task(self, schedule: dict[str, tuple[date, date]] | None = None) -> str:
        """The task that ends the project: the last-finishing task without successors (the handover milestone)."""
        schedule = schedule or self.schedule()
        return max((n for n in self.net.order if not self.net.succs[n]), key=lambda n: schedule[n][1])

    def downstream(self, task_id: str) -> list[str]:
        """Everything that (transitively) waits on task_id, in topological order."""
        return self.net.sort(nx.descendants(self.g, task_id))

    def upstream(self, task_id: str) -> list[str]:
        return self.net.sort(nx.ancestors(self.g, task_id))

    def open_predecessors(self, task_id: str) -> list[str]:
        """Predecessors that should be finished (FS/FF) or started (SS/SF) before `task_id` is under way."""
        out = []
        for pred, typ, _lag in self.net.preds[task_id]:
            ps = self.tasks[pred].status
            if ps == TaskStatus.cancelled:
                continue
            if (typ in ("FS", "FF") and ps != TaskStatus.done) or (typ in ("SS", "SF") and ps == TaskStatus.not_started):
                out.append(pred)
        return out

    def has_message(self, message_id: str) -> bool:
        """Was a claim with this id already interpreted? (idempotency key for retries)"""
        return any(e.kind == EventKind.update_interpreted and e.source_message_id == message_id
                   for e in self.store.all())

    def planned_finish(self) -> date:
        """Handover date of the untouched baseline plan."""
        return max(t.planned_finish for t in self.plan.tasks)

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
        """What the LLM may choose from, for one message.

        tasks:         the sender's own tasks (assigned to them or to their trade) that are active around `today`.
        related_tasks: everyone else's tasks that may be what the message is about: tasks at locations named in
                       the text, direct predecessors/successors of the sender's tasks, other tasks active around
                       `today`. Lower authority: the prompt should prefer `tasks`.
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
        neighbours = {s for n in mine_ids for s, _, _ in self.net.succs[n]} | \
                     {p for n in mine_ids for p, _, _ in self.net.preds[n]}
        mentioned = self._locations_mentioned(text) if text else set()

        # own tasks outside the +-3 day window stay hidden, unless the message names their location
        related = [t for t in self.tasks.values()
                   if t.id not in mine_ids and t.status != TaskStatus.cancelled
                   and (t.location_id in mentioned
                        or (not own(t) and (t.id in neighbours or _active_around(t, today))))]
        related.sort(key=lambda t: (t.location_id not in mentioned, t.id not in neighbours, *by_date(t)))
        return mine[:MAX_TASKS], related[:MAX_TASKS]

    # ------------------------------------------------------------------ change

    def is_confident(self, u: TaskUpdate) -> bool:
        return u.confidence >= self.confidence_min

    def record(self, kind: EventKind, actor: str, *, task_id: str | None = None,
               source_message_id: str | None = None, before: dict | None = None, after: dict | None = None,
               payload: dict | None = None) -> GraphEvent:
        """The ONE place that writes to the event log (graph changes, messages, outbox)."""
        return self.store.append(GraphEvent(
            ts=_now(), kind=kind, actor=actor, task_id=task_id, source_message_id=source_message_id,
            before=before or {}, after=after or {}, payload=payload or {}))

    def log_interpretation(self, update: TaskUpdate, actor: str, applied: bool) -> GraphEvent:
        e = self.record(EventKind.update_interpreted, actor, task_id=update.task_id,
                        source_message_id=update.message_id,
                        payload={"update": update.model_dump(mode="json"), "applied": applied})
        self._note_report(update)
        return e

    def apply(self, update: TaskUpdate, actor: str) -> list[GraphEvent]:
        """Interpretation in, events out. Low-confidence / unmapped updates change nothing."""
        applied = (update.task_id in self.tasks and self.is_confident(update)
                   and update.kind not in NO_STATE_CHANGE)
        events = [self.log_interpretation(update, actor, applied)]
        if not applied:
            return events

        t = self.tasks[update.task_id]
        before = _live(t)
        new = self._next_state(t, update)
        changed = {k: v for k, v in new.items() if before[k] != v}
        if not changed:
            return events

        before_slots = self._slots()
        self.tasks[t.id] = _with(t, changed)
        self._rows.pop(t.id, None)
        self._cache = self._snap = None
        after_slots = self._slots()
        moved = [n for n in self.net.order if after_slots[n].finish != before_slots[n].finish]
        events.append(self.record(
            EventKind.task_changed, actor, task_id=t.id, source_message_id=update.message_id,
            before=_jsonable({k: before[k] for k in changed}), after=_jsonable(changed),
            payload={
                "update_kind": update.kind.value,
                "finish_before": before_slots[t.id].finish.isoformat(),
                "finish_after": after_slots[t.id].finish.isoformat(),
                "project_finish_before": self.project_finish(before_slots).isoformat(),
                "project_finish_after": self.project_finish(after_slots).isoformat(),
                "moved_task_ids": moved,
                "was_critical": before_slots[t.id].total_float <= 0,
            }))
        return events

    def _next_state(self, t: Task, u: TaskUpdate) -> dict:
        s = _live(t)
        day = u.reported_on
        if t.status == TaskStatus.done and u.kind != UpdateKind.completed:
            return s   # a finished task stays finished: later chatter about it never reopens it
        not_started = s["status"] == TaskStatus.not_started.value
        if u.kind == UpdateKind.started or (u.kind == UpdateKind.progress
                                            and (not not_started or u.percent_complete)):
            s["status"] = TaskStatus.in_progress.value
            s["actual_start"] = s["actual_start"] or day
            if u.percent_complete is not None:
                s["percent_complete"] = u.percent_complete
            if u.new_forecast_finish:
                s["forecast_finish"] = u.new_forecast_finish
        elif u.kind == UpdateKind.progress:
            # "Screed is fine for Thursday" about a task that has not started: a confirmation,
            # not a start. Only a reported finish date is taken over.
            if u.new_forecast_finish:
                s["forecast_finish"] = u.new_forecast_finish
        elif u.kind == UpdateKind.completed:
            s.update(status=TaskStatus.done.value, actual_start=s["actual_start"] or day,
                     actual_finish=day, percent_complete=100, forecast_finish=None)
        elif u.kind == UpdateKind.delayed:
            if u.new_forecast_finish:
                s["forecast_finish"] = u.new_forecast_finish
        elif u.kind == UpdateKind.blocked:
            s["status"] = TaskStatus.blocked.value
        elif u.kind == UpdateKind.unblocked:
            s["status"] = (TaskStatus.in_progress if s["actual_start"] else TaskStatus.not_started).value
        return s

    # ------------------------------------------------------------------ detect

    def detect(self, events: list[GraphEvent]) -> list[Discrepancy]:
        """Deterministic rules from docs/04-graph. Logs a discrepancy_raised event for each."""
        found: list[Discrepancy] = []
        for e in events:
            if e.kind == EventKind.update_interpreted:
                found += self._from_interpretation(e)
            elif e.kind == EventKind.task_changed:
                found += self._from_change(e)
        return self._log(found)

    def check_overdue(self) -> list[Discrepancy]:
        """silent_overdue (run on a timer): due before today, not done, nobody reported for ~24 h.
        Raised at most once per task per day."""
        today, found = self.today, []
        for n in self.net.order:
            t = self.tasks[n]
            if t.status in (TaskStatus.done, TaskStatus.cancelled):
                continue
            due = t.forecast_finish or t.planned_finish
            last = self._last_report.get(n)
            if due >= today or (last and (today - last).days < OVERDUE_QUIET_DAYS + 1):
                continue
            did = f"D-overdue-{n}-{today.isoformat()}"
            if did in self._raised:
                continue
            days = (today - due).days
            found.append(Discrepancy(
                id=did, type=DiscrepancyType.silent_overdue,
                severity=Severity.warning if days >= OVERDUE_WARN_DAYS else Severity.info,
                task_id=n, detected_at=_now(),
                description=f"{self._name(n)} was due {due:%d %b} ({days} d ago), status {t.status.value}, "
                            f"and nobody has reported on it since "
                            f"{f'{last:%d %b}' if last else 'the plan was loaded'}.",
                impact=Impact(delay_days=days, affected_task_ids=self.downstream(n),
                              critical_path_affected=self._slots()[n].total_float <= 0)))
        return self._log(found)

    def _log(self, found: list[Discrepancy]) -> list[Discrepancy]:
        for d in found:
            self._raised.add(d.id)
            self.discrepancies.append(d)
            self.record(EventKind.discrepancy_raised, "system", task_id=d.task_id, payload=d.model_dump(mode="json"))
        return found

    def _disc(self, e: GraphEvent, typ: DiscrepancyType, sev: Severity, desc: str,
              impact: Impact | None = None) -> Discrepancy:
        return Discrepancy(id=f"D-{e.id}-{typ.value}", type=typ, severity=sev, task_id=e.task_id,
                           detected_at=_now(), description=desc, impact=impact or Impact(),
                           source_event_id=e.id)

    def _name(self, task_id: str | None) -> str:
        return f"{task_id} ({self.tasks[task_id].name})" if task_id in self.tasks else str(task_id)

    def _from_interpretation(self, e: GraphEvent) -> list[Discrepancy]:
        u = TaskUpdate.model_validate(e.payload["update"])
        if u.task_id is None and u.kind not in (UpdateKind.irrelevant, UpdateKind.question):
            return [self._disc(e, DiscrepancyType.unknown_task, Severity.info,
                               f"Message {u.message_id} reports '{u.kind.value}' but matches no task in the plan.")]
        if u.task_id not in self.tasks or not self.is_confident(u):
            return []  # unknown id or low confidence: the engine asks the sender instead
        if u.kind == UpdateKind.modified:
            return [self._disc(e, DiscrepancyType.scope_change, Severity.warning,
                               f"Scope change on {self._name(u.task_id)}: {u.change_description or u.rationale}",
                               Impact(affected_task_ids=self.downstream(u.task_id)))]
        if u.kind == UpdateKind.blocked and e.payload.get("applied", True):
            crit = u.task_id in self.critical_path()
            return [self._disc(e, DiscrepancyType.blocked, Severity.critical if crit else Severity.warning,
                               f"{self._name(u.task_id)} is blocked: {u.blocker or 'no reason given'}",
                               Impact(affected_task_ids=self.downstream(u.task_id), critical_path_affected=crit))]
        return []

    def _from_change(self, e: GraphEvent) -> list[Discrepancy]:
        out: list[Discrepancy] = []
        t = self.tasks[e.task_id]
        p = e.payload

        # out_of_sequence / hold_point_violation (only when the status itself changed)
        if "status" in e.after and t.status in (TaskStatus.in_progress, TaskStatus.done):
            unfinished = self.open_predecessors(t.id)
            if unfinished:
                impact = Impact(affected_task_ids=self.downstream(t.id))
                names = ", ".join(self._name(x) for x in unfinished)
                out.append(self._disc(e, DiscrepancyType.out_of_sequence, Severity.warning,
                                      f"{self._name(t.id)} is {t.status.value} while predecessor {names} is not done.",
                                      impact))
                holds = [x for x in unfinished if self.tasks[x].hold_point]
                if holds:
                    out.append(self._disc(e, DiscrepancyType.hold_point_violation, Severity.critical,
                                          f"{self._name(t.id)} started before hold point "
                                          f"{', '.join(self._name(x) for x in holds)} was passed: walls may be "
                                          f"closed before inspection.", impact))

        # delay: finish got later than before AND later than the baseline
        fb, fa = date.fromisoformat(p["finish_before"]), date.fromisoformat(p["finish_after"])
        if fa > fb and fa > t.planned_finish:
            pfb, pfa = date.fromisoformat(p["project_finish_before"]), date.fromisoformat(p["project_finish_after"])
            moved = [x for x in p["moved_task_ids"] if x != t.id]
            crit = p["was_critical"] or pfa > pfb
            out.append(self._disc(
                e, DiscrepancyType.delay, Severity.critical if crit else Severity.warning,
                f"{self._name(t.id)} now finishes {fa:%d %b} instead of {fb:%d %b} "
                f"(+{(fa - fb).days} d). {len(moved)} downstream task(s) move"
                + (f"; handover moves {pfb:%d %b} -> {pfa:%d %b}." if pfa > pfb else "."),
                Impact(delay_days=(fa - fb).days, affected_task_ids=moved, critical_path_affected=crit,
                       project_finish_before=pfb, project_finish_after=pfa)))
        return out

    # ------------------------------------------------------------------ export

    def snapshot(self) -> dict:
        """JSON-ready state for the API / UI. Cached until the state or `today` changes;
        treat the result as read-only."""
        if self._snap is not None and self._cache is not None:
            return self._snap
        slots = self._slots()
        crit = set(self.critical_path(slots))
        tasks = []
        for n in self.net.order:
            t, s = self.tasks[n], slots[n]
            if n not in self._rows:
                self._rows[n] = t.model_dump(mode="json")
            row = dict(self._rows[n])
            row.update(forecast_start=s.start.isoformat(), forecast_finish_cpm=s.finish.isoformat(),
                       total_float=s.total_float, critical=n in crit,
                       slip_days=(s.finish - t.planned_finish).days)
            tasks.append(row)
        self._snap = {
            "today": self.today.isoformat(),
            "project_finish": self.project_finish(slots).isoformat(),
            "critical_path": [n for n in self.net.order if n in crit],
            "tasks": tasks,
            "edges": [{"from": a, "to": b, **d} for a, b, d in self.g.edges(data=True)],
        }
        return self._snap

