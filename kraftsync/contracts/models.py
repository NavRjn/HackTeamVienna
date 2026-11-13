"""Shared contracts for KraftSync.

Every module talks to every other module ONLY through these models.
Change this file only via a small PR that the whole team has seen.
Additive changes (new optional field) are fine; renames/removals are not.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Reference entities (static-ish, loaded at plan time)
# ---------------------------------------------------------------------------

class Role(str, Enum):
    site_manager = "site_manager"      # Bauleiter (contractor side)
    foreman = "foreman"                # Polier / trade foreman
    worker = "worker"
    inspector = "inspector"            # e.g. ÖBA / client's site supervision
    client_rep = "client_rep"
    supplier = "supplier"


class Person(BaseModel):
    id: str
    name: str
    role: Role
    company: str | None = None
    trade: str | None = None           # electrical, plumbing, drywall, ...
    phone: str | None = None           # E.164, used for WhatsApp matching
    telegram_handle: str | None = None
    telegram_chat_id: int | None = None
    email: str | None = None
    language: str = "de"


class LocationKind(str, Enum):
    building = "building"
    floor = "floor"
    unit = "unit"                      # apartment ("Top")
    room = "room"
    zone = "zone"


class Location(BaseModel):
    id: str
    name: str
    kind: LocationKind
    parent_id: str | None = None


# ---------------------------------------------------------------------------
# The dependency graph: Tasks are NODES, Dependencies are EDGES.
# Events are NOT nodes - they live in the append-only log (GraphEvent).
# ---------------------------------------------------------------------------

class TaskKind(str, Enum):
    work = "work"
    inspection = "inspection"          # hold point: successors must not start before it is done
    delivery = "delivery"
    milestone = "milestone"            # zero duration


class TaskStatus(str, Enum):
    not_started = "not_started"
    in_progress = "in_progress"
    blocked = "blocked"
    done = "done"
    cancelled = "cancelled"


class Task(BaseModel):
    id: str
    name: str
    kind: TaskKind = TaskKind.work
    trade: str | None = None
    location_id: str
    responsible_person_id: str
    duration_days: float = Field(ge=0)
    planned_start: date
    planned_finish: date
    # live state (mutated only through GraphEvents)
    status: TaskStatus = TaskStatus.not_started
    actual_start: date | None = None
    actual_finish: date | None = None
    forecast_finish: date | None = None
    percent_complete: int = Field(default=0, ge=0, le=100)
    # optional links
    hold_point: bool = False           # True for inspections that gate closing-up work
    boq_ref: str | None = None         # bill-of-quantities / billing position
    drawing_refs: list[str] = Field(default_factory=list)
    notes: str | None = None


class DependencyType(str, Enum):
    FS = "FS"   # finish-to-start (default)
    SS = "SS"   # start-to-start
    FF = "FF"   # finish-to-finish
    SF = "SF"   # start-to-finish (rare)


class Dependency(BaseModel):
    id: str
    predecessor_id: str
    successor_id: str
    type: DependencyType = DependencyType.FS
    lag_days: float = 0                # calendar days; negative = lead
    reason: str | None = None          # "same crew", "screed drying", ...


class ProjectPlan(BaseModel):
    """Everything ingestion produces and the graph loads."""
    project_id: str
    name: str
    data_date: date                    # "today" for the schedule
    calendar: str = "calendar_days"    # working calendars are a stretch goal
    persons: list[Person]
    locations: list[Location]
    tasks: list[Task]
    dependencies: list[Dependency]


# ---------------------------------------------------------------------------
# Live layer: messages in, structured updates out
# ---------------------------------------------------------------------------

class Channel(str, Enum):
    telegram = "telegram"
    whatsapp = "whatsapp"
    email = "email"
    manual = "manual"                  # dashboard / test injection


class Attachment(BaseModel):
    kind: str                          # image | document | audio | video
    path: str                          # local path after download
    mime: str | None = None


class InboundMessage(BaseModel):
    """Produced by integrations. Channel-agnostic."""
    id: str                            # globally unique, e.g. "tg:123:456"
    channel: Channel
    received_at: datetime
    sender_ref: str                    # raw phone / handle / email
    sender_person_id: str | None = None  # resolved by integrations via persons list
    chat_ref: str | None = None        # group / thread id
    text: str = ""
    attachments: list[Attachment] = Field(default_factory=list)
    reply_to_id: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class UpdateKind(str, Enum):
    started = "started"
    progress = "progress"
    completed = "completed"
    delayed = "delayed"
    blocked = "blocked"
    unblocked = "unblocked"
    modified = "modified"              # scope change ("client wants 2 more sockets")
    question = "question"
    irrelevant = "irrelevant"


class TaskUpdate(BaseModel):
    """Engine output: an *interpretation* of a message. Never applied blindly."""
    message_id: str
    task_id: str | None                # None = could not map to a node
    kind: UpdateKind
    confidence: float = Field(ge=0, le=1)
    candidate_task_ids: list[str] = Field(default_factory=list)
    reported_on: date
    new_forecast_finish: date | None = None
    percent_complete: int | None = Field(default=None, ge=0, le=100)
    blocker: str | None = None
    change_description: str | None = None
    rationale: str                     # short, human-readable why
    model: str                         # which model/prompt version produced this


# ---------------------------------------------------------------------------
# Append-only log, discrepancies, actions
# ---------------------------------------------------------------------------

class EventKind(str, Enum):
    plan_loaded = "plan_loaded"
    message_received = "message_received"
    update_interpreted = "update_interpreted"
    task_changed = "task_changed"
    discrepancy_raised = "discrepancy_raised"
    discrepancy_resolved = "discrepancy_resolved"
    action_sent = "action_sent"
    manual_override = "manual_override"


class GraphEvent(BaseModel):
    """Every state change. Never updated or deleted. State = replay(events)."""
    id: int | None = None              # assigned by the store
    ts: datetime
    kind: EventKind
    actor: str                         # person id | "engine" | "system" | "policy"
    task_id: str | None = None
    source_message_id: str | None = None
    before: dict[str, Any] = Field(default_factory=dict)
    after: dict[str, Any] = Field(default_factory=dict)
    payload: dict[str, Any] = Field(default_factory=dict)


class DiscrepancyType(str, Enum):
    delay = "delay"                         # forecast finish > planned finish
    out_of_sequence = "out_of_sequence"     # started/finished before a predecessor is done
    hold_point_violation = "hold_point_violation"  # closed up before inspection passed
    blocked = "blocked"
    scope_change = "scope_change"
    unknown_task = "unknown_task"           # message about work not in the graph
    conflicting_reports = "conflicting_reports"
    silent_overdue = "silent_overdue"       # due, no report received


class Severity(str, Enum):
    info = "info"
    warning = "warning"
    critical = "critical"


class Impact(BaseModel):
    delay_days: float = 0
    affected_task_ids: list[str] = Field(default_factory=list)   # downstream, in topo order
    critical_path_affected: bool = False
    project_finish_before: date | None = None
    project_finish_after: date | None = None


class DiscrepancyStatus(str, Enum):
    open = "open"
    acknowledged = "acknowledged"
    resolved = "resolved"


class Discrepancy(BaseModel):
    id: str
    type: DiscrepancyType
    severity: Severity
    task_id: str | None
    detected_at: datetime
    description: str
    impact: Impact | None = None
    source_event_id: int | None = None
    status: DiscrepancyStatus = DiscrepancyStatus.open


class ActionKind(str, Enum):
    notify = "notify"          # FYI: your start moves
    ask = "ask"                # clarification needed
    escalate = "escalate"      # site manager must decide


class ActionStatus(str, Enum):
    drafted = "drafted"
    sent = "sent"
    failed = "failed"


class Action(BaseModel):
    """Produced by the resolution policy, delivered by integrations."""
    id: str
    discrepancy_id: str | None
    kind: ActionKind
    to_person_id: str
    channel: Channel
    text: str
    created_at: datetime
    status: ActionStatus = ActionStatus.drafted
    reply_expected: bool = False
