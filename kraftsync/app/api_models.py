"""HTTP envelope models for the local service. Built from `kraftsync.contracts`; contracts stay untouched."""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from kraftsync.contracts import (
    Action,
    Channel,
    Dependency,
    Discrepancy,
    GraphEvent,
    Person,
    Task,
    TaskUpdate,
)


class ContextRequest(BaseModel):
    sender_id: int                                            # Telegram user id (message.from.id); must be in the plan
    received_at: datetime | None = None                       # message time; sets "today" unless DEMO_TODAY is set
    text: str = Field(default="", max_length=4000)            # used to add tasks at locations named in the message


class ContextResponse(BaseModel):
    """What n8n puts into the LLM prompt. The LLM may only pick task ids from `tasks` or `related_tasks`."""
    person: Person | None
    tasks: list[Task]            # the sender's own tasks that are active around `today` (+-3 days); prefer these
    related_tasks: list[Task]    # other people's tasks that may be meant: named locations, neighbours, active now
    today: date


class UpdateRequest(BaseModel):
    """One chat message with one or more claims about tasks. Each claim needs its own unique `message_id`
    (e.g. "tg:123:456#1", "tg:123:456#2"): it is the idempotency key."""
    sender_id: int               # Telegram user id (message.from.id)
    text: str = Field(default="", max_length=4000)
    received_at: datetime
    channel: Channel = Channel.telegram
    chat_ref: str | None = None
    updates: list[TaskUpdate] = Field(min_length=1, max_length=10)


class Recipient(BaseModel):
    """Convenience so n8n can send an Action without a second lookup."""
    person_id: str
    name: str
    telegram_chat_id: int | None = None


Status = Literal["applied", "needs_clarification", "ignored", "duplicate"]
DocumentCategory = Literal["bim_models", "drawings_plans", "schedules", "financial_documents"]


class DocumentView(BaseModel):
    id: str
    original_filename: str
    category: DocumentCategory
    file_size: int
    uploaded_at: datetime
    revision_label: str | None = None
    location_ref: str | None = None
    task_ref: str | None = None


class ClaimResult(BaseModel):
    message_id: str
    status: Status
    note: str | None = None
    events: list[GraphEvent] = Field(default_factory=list)
    discrepancies: list[Discrepancy] = Field(default_factory=list)
    actions: list[Action] = Field(default_factory=list)


class UpdateResponse(BaseModel):
    """Aggregate over all claims. `results` has the per-claim detail. n8n only needs `actions` + `recipients`."""
    status: Status               # applied if any claim applied, else needs_clarification, else ignored, else duplicate
    note: str | None = None
    results: list[ClaimResult]
    events: list[GraphEvent] = Field(default_factory=list)
    discrepancies: list[Discrepancy] = Field(default_factory=list)
    actions: list[Action] = Field(default_factory=list)
    recipients: dict[str, Recipient] = Field(default_factory=dict)


class ActionOut(Action):
    """An outbox entry: the Action (with its current `status`) plus who/where to send it."""
    recipient: Recipient
    detail: str | None = None    # what the sender reported (e.g. an error), if anything


class ActionResultRequest(BaseModel):
    """The notifier reports what happened to one action. Sent twice with the same status is fine."""
    action_id: str = Field(min_length=1, max_length=300)
    status: Literal["sent", "failed"]
    detail: str | None = Field(default=None, max_length=300)


class ConflictHook(BaseModel):
    """Body we POST to the n8n webhook when there is something to tell people."""
    message_id: str
    discrepancies: list[Discrepancy]
    actions: list[Action]
    recipients: dict[str, Recipient]


class TaskView(Task):
    cpm_start: date | None = None      # computed forecast (dependency ripple)
    cpm_finish: date | None = None


class StateResponse(BaseModel):
    project_id: str
    name: str
    today: date
    project_finish_planned: date | None
    project_finish_forecast: date | None
    tasks: list[TaskView]
    dependencies: list[Dependency]
    open_discrepancies: list[Discrepancy]
