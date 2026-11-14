"""PDF -> draft plan. The LLM is faked (no network, no key); everything after it is real:
building the plan, validation, and grounding against the PDF's own text (pypdf)."""

import copy
import json
import os
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from kraftsync.contracts import ProjectPlan
from kraftsync.graph import EventStore, ProjectGraph
from kraftsync.ingestion import IngestionError, errors, load_plan, validate
from kraftsync.ingestion.__main__ import main as cli
from kraftsync.ingestion.pdf import DRAFT_SCHEMA, ClaudeDrafter, draft_plan_from_pdf

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "data" / "demo"
PDF = DEMO / "sources" / "bauzeitplan.pdf"


@pytest.fixture(scope="module")
def plan() -> ProjectPlan:
    return ProjectPlan.model_validate_json((DEMO / "plan.json").read_text(encoding="utf-8"))


def perfect_draft(plan: ProjectPlan) -> dict:
    """What a perfect reader of bauzeitplan.pdf would return (the PDF is generated from plan.json)."""
    preds: dict[str, list] = {}
    for d in plan.dependencies:
        preds.setdefault(d.successor_id, []).append(
            {"id": d.predecessor_id, "type": d.type.value, "lag_days": d.lag_days, "reason": d.reason or ""})
    return {
        "project_name": plan.name,
        "tasks": [{"id": t.id, "name": t.name, "kind": t.kind.value, "trade": t.trade or "",
                   "location_id": t.location_id, "responsible_person_id": t.responsible_person_id,
                   "planned_start": t.planned_start.isoformat(), "planned_finish": t.planned_finish.isoformat(),
                   "predecessors": preds.get(t.id, []), "hold_point": t.hold_point, "boq_ref": t.boq_ref or ""}
                  for t in plan.tasks],
        "persons": [{"id": p.id, "name": p.name, "role": p.role.value, "company": p.company or "",
                     "trade": p.trade or "", "phone": p.phone or "", "language": p.language} for p in plan.persons],
        "locations": [{"id": loc.id, "name": loc.name, "kind": loc.kind.value, "parent_id": loc.parent_id or ""}
                      for loc in plan.locations],
    }


class FakeDrafter:
    def __init__(self, draft: dict):
        self.result, self.calls = draft, 0

    def draft(self, pdf: bytes, filename: str) -> dict:
        self.calls += 1
        assert pdf.startswith(b"%PDF") and filename == PDF.name
        return copy.deepcopy(self.result)


def _deps(p: ProjectPlan) -> set:
    return {(d.predecessor_id, d.successor_id, d.type.value, d.lag_days, d.reason) for d in p.dependencies}


# ---------------------------------------------------------------- happy path

def test_perfect_draft_is_grounded_valid_and_matches_the_plan(plan):
    draft, problems = draft_plan_from_pdf(PDF, FakeDrafter(perfect_draft(plan)), project_id=plan.project_id)
    assert problems == [], problems                      # every id, name and date found in the PDF text
    assert validate(draft) == []
    assert (draft.name, draft.data_date) == (plan.name, plan.data_date)
    assert sorted(draft.tasks, key=lambda t: t.id) == sorted(plan.tasks, key=lambda t: t.id)
    assert _deps(draft) == _deps(plan)                   # including the reasons from the dependency page
    assert draft.locations == plan.locations
    keep = ("id", "name", "role", "company", "trade", "phone", "language")
    assert [p.model_dump(include=set(keep)) for p in draft.persons] == \
           [p.model_dump(include=set(keep)) for p in plan.persons]
    pg = ProjectGraph.from_plan(draft, EventStore(), today=draft.data_date)
    for t in plan.tasks:
        assert pg.schedule()[t.id] == (t.planned_start, t.planned_finish), t.id


# ---------------------------------------------------------------- grounding catches what the LLM got wrong

def test_invented_task_and_person_are_errors(plan):
    d = perfect_draft(plan)
    d["tasks"].append({**d["tasks"][0], "id": "T7-MAGIC", "predecessors": []})
    d["persons"].append({**d["persons"][0], "id": "P-GHOST", "name": "Max Mustermann"})
    _, problems = draft_plan_from_pdf(PDF, FakeDrafter(d))
    msgs = [str(p) for p in errors(problems)]
    assert any("'T7-MAGIC' does not appear in the PDF" in m for m in msgs)
    assert any("'P-GHOST'" in m for m in msgs)


def test_wrong_date_and_name_are_warnings(plan):
    d = perfect_draft(plan)
    t = next(x for x in d["tasks"] if x["id"] == "T5-SCREED")
    t["planned_start"] = t["planned_finish"] = "2026-11-19"
    t["name"] = "Fließestrich mit Fußbodenheizung"
    _, problems = draft_plan_from_pdf(PDF, FakeDrafter(d))
    assert not errors(problems)
    text = "\n".join(map(str, problems))
    assert "T5-SCREED: planned start 2026-11-19 not found on its row" in text and "T5-SCREED: name" in text


def test_dependency_to_a_task_missing_from_the_draft_is_an_error(plan):
    d = perfect_draft(plan)
    d["tasks"] = [t for t in d["tasks"] if t["id"] != "T6-INSP"]
    _, problems = draft_plan_from_pdf(PDF, FakeDrafter(d))
    assert any("'T6-INSP' is not a task in the draft" in str(p) for p in errors(problems))


# ---------------------------------------------------------------- the boundary: PDFs are never loaded silently

def test_load_plan_refuses_pdfs():
    with pytest.raises(IngestionError, match="PDFs are drafted, not loaded"):
        load_plan(PDF)


def test_bad_files_fail_before_any_llm_call(tmp_path, plan):
    fake = FakeDrafter(perfect_draft(plan))
    (tmp_path / "x.pdf").write_text("hello", encoding="utf-8")
    with pytest.raises(IngestionError, match="not a PDF file"):
        draft_plan_from_pdf(tmp_path / "x.pdf", fake)
    (tmp_path / "broken.pdf").write_bytes(b"%PDF-1.4\n garbage")
    with pytest.raises(IngestionError, match="not a readable PDF"):
        draft_plan_from_pdf(tmp_path / "broken.pdf", fake)
    assert fake.calls == 0


# ---------------------------------------------------------------- CLI

def test_cli_draft_writes_a_reviewed_draft(tmp_path, capsys, plan):
    out = tmp_path / "plan.draft.json"
    rc = cli([str(PDF), "--draft", "--project-id", plan.project_id, "-o", str(out)],
             drafter_factory=lambda: FakeDrafter(perfect_draft(plan)))
    printed = capsys.readouterr().out
    assert rc == 0 and out.exists()
    assert "REVIEW THIS DRAFT" in printed and "T6-BOARD" in printed and "DRAFT - review" in printed


def test_cli_refuses_an_ungrounded_draft(tmp_path, capsys, plan):
    d = perfect_draft(plan)
    d["tasks"][0]["id"] = "X-INVENTED"
    out = tmp_path / "never.json"
    rc = cli([str(PDF), "--draft", "-o", str(out)], drafter_factory=lambda: FakeDrafter(d))
    assert rc == 1 and not out.exists()
    assert "invented" in capsys.readouterr().out


# ---------------------------------------------------------------- the Claude request (fake client)

class FakeClient:
    def __init__(self, stop_reason="end_turn", text="{}"):
        self.kwargs, self.stop_reason, self.text = None, stop_reason, text
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    @contextmanager
    def _stream(self, **kwargs):
        self.kwargs = kwargs
        msg = SimpleNamespace(stop_reason=self.stop_reason, content=[SimpleNamespace(type="text", text=self.text)])
        yield SimpleNamespace(get_final_message=lambda: msg)


def test_claude_drafter_sends_the_pdf_with_a_strict_schema(plan):
    client = FakeClient(text=json.dumps(perfect_draft(plan)))
    draft, problems = draft_plan_from_pdf(PDF, ClaudeDrafter(client=client), project_id=plan.project_id)
    assert not errors(problems) and len(draft.tasks) == len(plan.tasks)
    k = client.kwargs
    assert k["model"] == os.getenv("INGEST_MODEL", "claude-opus-5-5") or k["model"] == "claude-opus-5-5"
    assert k["output_config"]["format"] == {"type": "json_schema", "schema": DRAFT_SCHEMA}
    assert k["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in k["betas"]
    doc, prompt = k["messages"][0]["content"]
    assert doc["type"] == "document" and doc["source"]["media_type"] == "application/pdf"
    assert "\n" not in doc["source"]["data"] and prompt["type"] == "text"
    assert "never invent" in k["system"]


@pytest.mark.parametrize("stop, text, message", [
    ("refusal", "", "declined"), ("max_tokens", "{", "output limit"), ("end_turn", "not json", "not valid JSON")])
def test_claude_drafter_handles_bad_responses(stop, text, message):
    with pytest.raises(IngestionError, match=message):
        ClaudeDrafter(client=FakeClient(stop, text)).draft(PDF.read_bytes(), PDF.name)


def test_draft_schema_is_strict_everywhere():
    def walk(s):
        if s.get("type") == "object":
            assert s["additionalProperties"] is False and set(s["required"]) == set(s["properties"])
            for v in s["properties"].values():
                walk(v)
        if s.get("type") == "array":
            walk(s["items"])
    walk(DRAFT_SCHEMA)


@pytest.mark.skipif(not os.getenv("KRAFTSYNC_LIVE_PDF"), reason="live API call; set KRAFTSYNC_LIVE_PDF=1 to run")
def test_live_claude_reads_the_demo_pdf(plan):                       # pragma: no cover - costs money
    draft, problems = draft_plan_from_pdf(PDF, project_id=plan.project_id)
    assert not errors(problems) and not errors(validate(draft))
    assert {t.id for t in draft.tasks} == {t.id for t in plan.tasks}
