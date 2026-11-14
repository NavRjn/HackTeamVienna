"""PDF schedule -> DRAFT ProjectPlan. Plan time only, never in the live pipeline.

A PDF has no fixed structure, so this is the one ingestion path that uses an LLM - and only for
the step that needs it:

    1. draft      the LLM reads the PDF (native document input) and fills a fixed JSON schema:
                  tasks with predecessors, persons, locations. It is told to copy, never invent.
    2. build      deterministic: JSON -> ProjectPlan with the same contracts as every other source
    3. check      deterministic: validate(plan) + grounding_problems(plan, pdf text):
                  every task id and person id must literally appear in the PDF's own text
                  (pypdf), otherwise the draft is refused. Dates/names not found -> warnings.
    4. review     a person checks the draft and freezes it to plan.json. load_plan() refuses PDFs
                  so a PDF can never become a plan by accident.

The LLM sits behind the `Drafter` protocol; tests use a fake, so they need no network and no key.
"""

from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Protocol

from kraftsync.contracts import (
    Dependency,
    DependencyType,
    Location,
    LocationKind,
    Person,
    ProjectPlan,
    Role,
    Task,
    TaskKind,
)
from kraftsync.ingestion.csv_loader import IngestionError, _build
from kraftsync.ingestion.validate import Problem

DEFAULT_MODEL = "claude-opus-5-5"
MAX_PDF_BYTES = 30 * 1024 * 1024          # request limit is 32 MB including the base64 overhead margin

# ---------------------------------------------------------------------------- the schema the LLM fills


def _obj(props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def _enum(e) -> dict[str, Any]:
    return {"type": "string", "enum": [x.value for x in e]}


S = {"type": "string"}
DRAFT_SCHEMA: dict[str, Any] = _obj({
    "project_name": S,
    "tasks": {"type": "array", "items": _obj({
        "id": S, "name": S, "kind": _enum(TaskKind), "trade": S, "location_id": S,
        "responsible_person_id": S, "planned_start": S, "planned_finish": S,
        "predecessors": {"type": "array", "items": _obj({
            "id": S, "type": _enum(DependencyType), "lag_days": {"type": "number"}, "reason": S})},
        "hold_point": {"type": "boolean"}, "boq_ref": S,
    })},
    "persons": {"type": "array", "items": _obj({
        "id": S, "name": S, "role": _enum(Role), "company": S, "trade": S, "phone": S, "language": S})},
    "locations": {"type": "array", "items": _obj({
        "id": S, "name": S, "kind": _enum(LocationKind), "parent_id": S})},
})

SYSTEM = """You turn a construction schedule document (Bauzeitplan) into JSON for a scheduling tool.

Copy what the document says; never invent. Task ids, person ids, location ids, names and dates must
be exactly as printed. If a value is not in the document, use an empty string (or an empty list).
Do not add tasks, people, locations or dependencies that the document does not show.

Dates are YYYY-MM-DD; a two-digit year like 02.11.26 means 2026-11-02. A milestone has the same
start and finish. Predecessors come from the predecessor column (Vorgänger) and/or a dependency
list: a bare id means finish-to-start with no lag, "ID FS+14" means finish-to-start with 14 days
lag; take the reason (Grund) from the dependency list when there is one. A hold point (Haltepunkt,
HP) is an inspection that later work must wait for."""

PROMPT = "Extract the complete schedule from this document: every task, person, location and dependency."


# ---------------------------------------------------------------------------- drafters

class Drafter(Protocol):
    def draft(self, pdf: bytes, filename: str) -> dict[str, Any]:
        """Return a dict matching DRAFT_SCHEMA."""


@dataclass
class ClaudeDrafter:
    """Claude reads the PDF and fills DRAFT_SCHEMA (structured outputs). Needs credentials
    (ANTHROPIC_API_KEY or an `ant auth login` profile). Model: INGEST_MODEL env or claude-opus-5-5."""
    model: str | None = None
    client: Any = None                       # anthropic.Anthropic(); injectable for tests
    effort: str = "high"                     # extraction accuracy matters more than cost here

    def draft(self, pdf: bytes, filename: str) -> dict[str, Any]:
        import anthropic

        client = self.client or anthropic.Anthropic()
        model = self.model or os.getenv("INGEST_MODEL") or DEFAULT_MODEL
        with client.beta.messages.stream(
            model=model,
            max_tokens=64000,
            system=SYSTEM,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",             # on a policy refusal, the API retries on a fallback model
            output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": DRAFT_SCHEMA}},
            messages=[{"role": "user", "content": [
                {"type": "document", "title": filename,
                 "source": {"type": "base64", "media_type": "application/pdf",
                            "data": base64.standard_b64encode(pdf).decode("ascii")}},
                {"type": "text", "text": PROMPT},
            ]}],
        ) as stream:
            msg = stream.get_final_message()
        if msg.stop_reason == "refusal":
            raise IngestionError(f"{filename}: the model declined to read this document")
        if msg.stop_reason == "max_tokens":
            raise IngestionError(f"{filename}: the schedule is too long for one draft (output limit reached)")
        text = next((b.text for b in msg.content if b.type == "text"), "")
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise IngestionError(f"{filename}: the draft is not valid JSON ({e})") from e


# ---------------------------------------------------------------------------- draft -> plan (deterministic)

def plan_from_draft(d: dict[str, Any], src: str, project_id: str | None = None, name: str | None = None,
                    data_date: date | None = None) -> ProjectPlan:
    def blank(v: Any) -> Any:
        return None if v in ("", None) else v

    persons = [_build(Person, src, i, {k: blank(v) for k, v in p.items() if blank(v) is not None})
               for i, p in enumerate(d.get("persons", []), 1)]
    locations = [_build(Location, src, i, {k: blank(v) for k, v in loc.items() if blank(v) is not None})
                 for i, loc in enumerate(d.get("locations", []), 1)]
    tasks, deps = [], []
    for i, t in enumerate(d.get("tasks", []), 1):
        start, finish = t["planned_start"], t["planned_finish"]
        try:
            days = (date.fromisoformat(finish) - date.fromisoformat(start)).days + 1
        except ValueError as e:
            raise IngestionError(f"{src}: task {t.get('id')!r}: unreadable date ({e})") from e
        milestone = t["kind"] == TaskKind.milestone.value
        tasks.append(_build(Task, src, i, {
            "id": t["id"], "name": t["name"], "kind": t["kind"], "trade": blank(t.get("trade")),
            "location_id": t["location_id"], "responsible_person_id": t["responsible_person_id"],
            "duration_days": 0 if milestone else days, "planned_start": start,
            "planned_finish": start if milestone else finish, "hold_point": bool(t.get("hold_point")),
            "boq_ref": blank(t.get("boq_ref")),
        }))
        for p in t.get("predecessors", []):
            deps.append(Dependency(id=f"D{len(deps) + 1:03d}", predecessor_id=p["id"], successor_id=t["id"],
                                   type=p.get("type") or "FS", lag_days=p.get("lag_days") or 0,
                                   reason=blank(p.get("reason"))))
    if not tasks:
        raise IngestionError(f"{src}: the draft contains no tasks")
    return ProjectPlan(
        project_id=project_id or Path(src).stem, name=name or d.get("project_name") or Path(src).stem,
        data_date=data_date or min(t.planned_start for t in tasks),
        persons=persons, locations=locations, tasks=tasks, dependencies=deps,
    )


# ---------------------------------------------------------------------------- grounding (deterministic)

def pdf_text(pdf: bytes) -> str:
    import io

    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(pdf))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as e:                                  # pypdf raises many types on broken files
        raise IngestionError(f"not a readable PDF ({e.__class__.__name__}: {e})") from e


def _date_forms(d: date) -> list[str]:
    return [d.isoformat(), d.strftime("%d.%m.%Y"), d.strftime("%d.%m.%y"), f"{d.day}.{d.month}.{d.year}"]


def grounding_problems(plan: ProjectPlan, text: str) -> list[Problem]:
    """Everything the draft claims must be findable in the PDF's own text.
    Ids not in the PDF are errors (the LLM invented them); dates and names are warnings
    (PDF text extraction can split or truncate them)."""
    flat = re.sub(r"\s+", " ", text)
    squashed = re.sub(r"\s+", "", text)

    def found(s: str) -> bool:
        return bool(s) and (s in flat or re.sub(r"\s+", "", s) in squashed)

    lines = [ln.strip() for ln in text.splitlines()]
    out: list[Problem] = []
    for t in plan.tasks:
        if not found(t.id):
            out.append(Problem("error", f"task id {t.id!r} does not appear in the PDF (invented by the draft?)"))
            continue
        if not found(t.name) and not found(t.name[:20]):
            out.append(Problem("warning", f"task {t.id}: name {t.name!r} not found in the PDF text"))
        # A date must be on the task's OWN row (the line that starts with its id), not just anywhere:
        # otherwise another task's date would hide a wrong one. No such row -> any line with the id.
        own = re.compile(rf"^{re.escape(t.id)}(\s|$)")
        rows = [ln for ln in lines if own.match(ln)] or [ln for ln in lines if t.id in ln]
        for label, d in (("start", t.planned_start), ("finish", t.planned_finish)):
            if not any(f in row for row in rows for f in _date_forms(d)):
                out.append(Problem("warning", f"task {t.id}: planned {label} {d} not found on its row in the PDF"))
    for p in plan.persons:
        if not found(p.id) and not found(p.name):
            out.append(Problem("error", f"person {p.id!r} ({p.name}) does not appear in the PDF (invented?)"))
    for d in plan.dependencies:
        for end in (d.predecessor_id, d.successor_id):
            if end not in {t.id for t in plan.tasks}:
                out.append(Problem("error", f"dependency {d.predecessor_id} -> {d.successor_id}: "
                                            f"{end!r} is not a task in the draft"))
    return out


# ---------------------------------------------------------------------------- entry point

def draft_plan_from_pdf(path: str | Path, drafter: Drafter | None = None, project_id: str | None = None,
                        name: str | None = None, data_date: date | None = None) -> tuple[ProjectPlan, list[Problem]]:
    """Returns (draft plan, grounding problems). Run validate() on the plan as well; a person must
    review the result before it is frozen to plan.json."""
    path = Path(path)
    if not path.exists():
        raise IngestionError(f"{path}: file not found")
    pdf = path.read_bytes()
    if not pdf.startswith(b"%PDF"):
        raise IngestionError(f"{path}: not a PDF file")
    if len(pdf) > MAX_PDF_BYTES:
        raise IngestionError(f"{path}: {len(pdf) // 1_000_000} MB is too large for one request (max ~30 MB)")
    text = pdf_text(pdf)                                    # fail early on a broken file, before any API call
    draft = (drafter or ClaudeDrafter()).draft(pdf, path.name)
    plan = plan_from_draft(draft, f"{path.name} (draft)", project_id, name, data_date)
    return plan, grounding_problems(plan, text)
