"""data/demo/sources/{schedule_export.csv, bauzeitplan.pdf, schedule.xlsx, schedule.xml, schedule.xer} are connected to plan.json.

1. They are exactly what scripts/generate_demo_sources.py builds from the current plan.json
   (regenerate after any plan change, otherwise this fails).
2. Reading the CSV back gives the same tasks, people and dependencies as plan.json.
3. The PDF contains every task, dependency and person of plan.json.
(Reading schedule.xlsx / .xml / .xer back is tested in tests/test_ingestion.py.)
"""

import csv
import importlib.util
import io
import re
from datetime import date
from pathlib import Path

import pytest

from kraftsync.contracts import ProjectPlan

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "data" / "demo" / "sources"


@pytest.fixture(scope="module")
def gen():
    spec = importlib.util.spec_from_file_location("generate_demo_sources", ROOT / "scripts" / "generate_demo_sources.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def plan() -> ProjectPlan:
    return ProjectPlan.model_validate_json((ROOT / "data" / "demo" / "plan.json").read_text(encoding="utf-8"))


def test_sources_are_up_to_date_with_plan(gen, tmp_path, capsys):
    gen.main(tmp_path)
    capsys.readouterr()
    for name in ("schedule_export.csv", "bauzeitplan.pdf", "schedule.xlsx", "schedule.xml", "schedule.xer"):
        fresh, committed = (tmp_path / name).read_bytes(), (SOURCES / name).read_bytes()
        if name.endswith((".csv", ".xml", ".xer")):
            fresh, committed = fresh.replace(b"\r\n", b"\n"), committed.replace(b"\r\n", b"\n")
        assert fresh == committed, (f"data/demo/sources/{name} is out of date with plan.json. "
                                    "Run: uv run python scripts/generate_demo_sources.py")


def _parse_ref(ref: str, successor: str) -> tuple:
    m = re.fullmatch(r"(\S+)(?: (FS|SS|FF|SF)([+-]\d+(?:\.\d+)?))?", ref.strip())
    assert m, f"bad predecessor reference {ref!r}"
    pred, typ, lag = m.groups()
    return pred, successor, typ or "FS", float(lag or 0)


def test_csv_reads_back_to_the_plan(plan):
    rows = list(csv.DictReader(io.StringIO((SOURCES / "schedule_export.csv").read_text(encoding="utf-8"))))
    tasks = {t.id: t for t in plan.tasks}
    persons = {p.id: p for p in plan.persons}
    assert sorted(r["id"] for r in rows) == sorted(tasks)

    deps = set()
    for r in rows:
        t, p = tasks[r["id"]], persons[r["responsible_person_id"]]
        assert (r["name"], r["kind"], r["trade"] or None, r["location_id"]) == (t.name, t.kind.value, t.trade, t.location_id)
        assert float(r["duration_days"]) == t.duration_days
        assert (date.fromisoformat(r["planned_start"]), date.fromisoformat(r["planned_finish"])) == (t.planned_start, t.planned_finish)
        assert (r["hold_point"] == "yes") == t.hold_point and (r["boq_ref"] or None) == t.boq_ref
        assert t.responsible_person_id == p.id and (r["responsible"], r["company"], r["phone"]) == (p.name, p.company, p.phone)
        deps |= {_parse_ref(ref, r["id"]) for ref in r["predecessors"].split(";") if ref.strip()}

    assert deps == {(d.predecessor_id, d.successor_id, d.type.value, float(d.lag_days)) for d in plan.dependencies}


def test_pdf_contains_the_whole_plan(gen, plan):
    raw = (SOURCES / "bauzeitplan.pdf").read_bytes()
    assert raw.startswith(b"%PDF-1.4") and raw.rstrip().endswith(b"%%EOF")
    assert raw.count(b"/Type /Page ") == 4

    def shown(s: str) -> bool:                 # text is drawn as "(...) Tj"
        return b"(" + gen._enc(s) + b") Tj" in raw

    missing = [x for x in ([t.id for t in plan.tasks] + [d.id for d in plan.dependencies]
                           + [p.name for p in plan.persons] + [loc.id for loc in plan.locations]) if not shown(x)]
    assert missing == []
    assert gen._enc(plan.data_date.strftime("%d.%m.%Y")) in raw
