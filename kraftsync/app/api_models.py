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
    sender_ref: str = Field(min_length=1, max_length=200)   # phone / telegram id or handle / email


class ContextResponse(BaseModel):
    """What n8n puts into the LLM prompt. The LLM may only pick task ids from `candidates`."""
    person: Person | None
    candidates: list[Task]
    today: date


class UpdateRequest(BaseModel):
    sender_ref: str = Field(min_length=1, max_length=200)
    text: str = Field(default="", max_length=4000)
    received_at: datetime
    channel: Channel = Channel.telegram
    chat_ref: str | None = None
    update: TaskUpdate


class Recipient(BaseModel):
    """Convenience so n8n can send an Action without a second lookup."""
    person_id: str
    name: str
    telegram_chat_id: int | None = None


class UpdateResponse(BaseModel):
    status: Literal["applied", "needs_clarification", "ignored", "duplicate"]
    note: str | None = None
    events: list[GraphEvent] = Field(default_factory=list)
    discrepancies: list[Discrepancy] = Field(default_factory=list)
    actions: list[Action] = Field(default_factory=list)
    recipients: dict[str, Recipient] = Field(default_factory=dict)


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
