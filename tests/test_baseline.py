"""Freeze the demo baseline: data/demo/{plan.json, *.csv} is the original plan and must not drift.

Three guards:
1. The committed files are exactly what scripts/generate_demo_plan.py produces (no hand edits).
2. The baseline carries no live state: nothing started, no actuals, data_date = project start.
3. A fingerprint of the schedule-relevant fields matches BASELINE_SHA256.

Changing the baseline on purpose: edit the generator, run it, run this test, then copy the
new hash from the failure message into BASELINE_SHA256 and say so in the commit message.
"""

import csv
import hashlib
import importlib.util
import json
from datetime import date
from pathlib import Path

import pytest

from kraftsync.contracts import ProjectPlan, TaskStatus

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "data" / "demo"
GENERATED = ("plan.json", "persons.csv", "locations.csv", "tasks.csv", "dependencies.csv")

BASELINE_SHA256 = "74f02cf824ce85e82bcb4445fd1716984b2e7b5a23b970df4997e4cf512e0c1a"


def _plan() -> ProjectPlan:
    return ProjectPlan.model_validate_json((DEMO / "plan.json").read_text(encoding="utf-8"))


def _text(path: Path) -> str:
    """Content without line-ending / trailing-newline noise (Windows vs Linux checkouts). The persons.csv column
    `telegram_chat_id` holds the ids typed in for this deployment (a runtime setting), so it is blanked."""
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n").rstrip("\n")
    if path.name != "persons.csv":
        return text
    rows = list(csv.reader(text.split("\n")))
    col = rows[0].index("telegram_chat_id")
    for row in rows[1:]:
        row[col] = ""
    return "\n".join(",".join(row) for row in rows)


def fingerprint(plan: ProjectPlan) -> str:
    """Hash of everything that defines the schedule; ignores names, phones and other cosmetic fields."""
    data = {
        "project_id": plan.project_id,
        "data_date": plan.data_date.isoformat(),
        "tasks": sorted(
            [t.id, t.kind.value, t.location_id, t.responsible_person_id, t.duration_days,
             t.planned_start.isoformat(), t.planned_finish.isoformat(), t.hold_point]
            for t in plan.tasks),
        "dependencies": sorted(
            [d.predecessor_id, d.successor_id, d.type.value, d.lag_days] for d in plan.dependencies),
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def test_committed_files_match_the_generator(tmp_path, monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location("generate_demo_plan", ROOT / "scripts" / "generate_demo_plan.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    monkeypatch.setattr(gen, "OUT", tmp_path)
    gen.main()
    capsys.readouterr()  # silence the generator's printout
    for name in GENERATED:
        assert _text(tmp_path / name) == _text(DEMO / name), (
            f"data/demo/{name} differs from scripts/generate_demo_plan.py output. "
            "Do not hand-edit generated files: change the generator and re-run it.")


def test_baseline_has_no_live_state():
    plan = _plan()
    assert plan.data_date == date(2026, 11, 2) == min(t.planned_start for t in plan.tasks)
    for t in plan.tasks:
        assert t.status == TaskStatus.not_started, t.id
        assert (t.actual_start, t.actual_finish, t.forecast_finish, t.percent_complete) == (None, None, None, 0), t.id


def test_baseline_fingerprint_is_frozen():
    got = fingerprint(_plan())
    if got != BASELINE_SHA256:
        pytest.fail(f"The demo baseline changed (tasks, dates, durations or dependencies).\n"
                    f"If this is intended, set BASELINE_SHA256 = \"{got}\" in tests/test_baseline.py.")
