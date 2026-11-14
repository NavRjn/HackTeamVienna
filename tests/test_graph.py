"""The 'Done when' list from docs/04-graph, as tests.

plan.json is the untouched baseline (2 Nov, nothing started). The demo story's "today" (9 Nov)
is reached by feeding data/demo/history.jsonl (what happened 2-9 Nov) through apply().
"""

from datetime import date
from pathlib import Path

import pytest

from kraftsync.contracts import (
    Dependency,
    DiscrepancyType,
    Location,
    LocationKind,
    Person,
    ProjectPlan,
    Role,
    Severity,
    Task,
    TaskStatus,
    TaskUpdate,
    UpdateKind,
)
from kraftsync.graph import EventStore, ProjectGraph, feed, read_reports

DEMO = Path(__file__).resolve().parents[1] / "data" / "demo"
TODAY = date(2026, 11, 9)


@pytest.fixture
def plan() -> ProjectPlan:
    return ProjectPlan.model_validate_json((DEMO / "plan.json").read_text(encoding="utf-8"))


def at_demo_today(plan: ProjectPlan, store: EventStore | None = None) -> ProjectGraph:
    pg = ProjectGraph.from_plan(plan, store or EventStore(), today=plan.data_date)
    alerts = [d for _, found in feed(pg, read_reports(DEMO / "history.jsonl")) for d in found]
    assert alerts == [], "the 2-9 Nov history must run exactly to plan"
    assert pg.today == TODAY
    return pg


@pytest.fixture
def pg(plan) -> ProjectGraph:
    return at_demo_today(plan)


def upd(task_id, kind, conf=0.95, day=TODAY, **kw) -> TaskUpdate:
    return TaskUpdate(message_id=f"m-{task_id}-{kind}", task_id=task_id, kind=kind, confidence=conf,
                      reported_on=day, rationale="test", model="test", **kw)


# ---------------------------------------------------------------- Done when

def test_cpm_reproduces_planned_dates_on_baseline(plan):
    pg = ProjectGraph.from_plan(plan, EventStore(), today=plan.data_date)
    for t in plan.tasks:
        assert pg.schedule()[t.id] == (t.planned_start, t.planned_finish), t.id


def test_cpm_reproduces_planned_dates_after_history(pg, plan):
    sched = pg.schedule()
    for t in plan.tasks:
        assert sched[t.id] == (t.planned_start, t.planned_finish), t.id


def test_plumbing_delay_moves_handover(pg):
    before = pg.project_finish()
    events = pg.apply(upd("T6-PLUMB-RI", UpdateKind.delayed, new_forecast_finish=date(2026, 11, 13)), "P-SAN")
    delay = next(d for d in pg.detect(events) if d.type == DiscrepancyType.delay)
    assert {"T6-INSP", "T6-BOARD"} <= set(delay.impact.affected_task_ids)
    assert pg.project_finish() > before
    assert delay.impact.project_finish_after > delay.impact.project_finish_before
    assert delay.severity == Severity.critical


def test_boarding_before_inspection_is_hold_point_violation(pg):
    found = pg.detect(pg.apply(upd("T6-BOARD", UpdateKind.started), "P-DRY"))
    assert {DiscrepancyType.out_of_sequence, DiscrepancyType.hold_point_violation} <= {d.type for d in found}
    hp = next(d for d in found if d.type == DiscrepancyType.hold_point_violation)
    assert "T6-INSP" in hp.description and hp.severity == Severity.critical


def test_starting_early_behind_hold_point_never_improves_the_forecast(pg):
    pg.apply(upd("T6-PLUMB-RI", UpdateKind.delayed, new_forecast_finish=date(2026, 11, 13)), "P-SAN")
    delayed = pg.project_finish()
    pg.apply(upd("T6-BOARD", UpdateKind.started), "P-DRY")
    assert pg.project_finish() >= delayed


def test_replay_reproduces_identical_state(plan):
    store = EventStore()
    pg = at_demo_today(plan, store)
    pg.apply(upd("T6-PLUMB-RI", UpdateKind.delayed, new_forecast_finish=date(2026, 11, 13)), "P-SAN")
    pg.apply(upd("T6-ELEC-RI", UpdateKind.completed), "P-ELE")
    pg.apply(upd("T6-BOARD", UpdateKind.started), "P-DRY")
    again = ProjectGraph.from_plan(plan, store, today=TODAY)
    assert {k: v.model_dump() for k, v in again.tasks.items()} == {k: v.model_dump() for k, v in pg.tasks.items()}
    assert again.schedule() == pg.schedule()


def test_whole_demo_chat_raises_exactly_the_gold_discrepancies(pg):
    for report, found in feed(pg, read_reports(DEMO / "messages.jsonl")):
        assert sorted(d.type.value for d in found) == sorted(report.expected), report.id
    assert pg.project_finish() > date(2026, 12, 6), "the demo ends with the handover delayed"


# ---------------------------------------------------------------- rules

def test_low_confidence_is_never_applied(pg):
    events = pg.apply(upd("T6-BOARD", UpdateKind.started, conf=0.3), "P-DRY")
    assert len(events) == 1 and events[0].payload["applied"] is False
    assert pg.tasks["T6-BOARD"].status == TaskStatus.not_started
    assert pg.detect(events) == []


def test_low_confidence_scope_change_raises_nothing(pg):
    events = pg.apply(upd("T6-ELEC-RI", UpdateKind.modified, conf=0.3, change_description="?"), "P-ELE")
    assert pg.detect(events) == []


def test_scope_change_and_unknown_task(pg):
    sc = pg.detect(pg.apply(upd("T6-ELEC-RI", UpdateKind.modified, change_description="2 more sockets"), "P-ELE"))
    assert [d.type for d in sc] == [DiscrepancyType.scope_change]
    unk = pg.detect(pg.apply(upd(None, UpdateKind.completed), "P-ELE"))
    assert [d.type for d in unk] == [DiscrepancyType.unknown_task]


def test_completing_in_order_raises_nothing(pg):
    assert pg.detect(pg.apply(upd("T6-ELEC-RI", UpdateKind.completed), "P-ELE")) == []


def test_progress_confirmation_does_not_start_a_task(pg):
    pg.apply(upd("T5-SCREED", UpdateKind.progress), "P-SCR")
    assert pg.tasks["T5-SCREED"].status == TaskStatus.not_started


def test_blocked_on_critical_path_is_critical(pg):
    found = pg.detect(pg.apply(upd("T6-PLUMB-RI", UpdateKind.blocked, blocker="no pre-wall elements"), "P-SAN"))
    assert [d.type for d in found] == [DiscrepancyType.blocked]
    assert found[0].severity == Severity.critical and "no pre-wall elements" in found[0].description


def test_silent_overdue_without_history(plan):
    pg = ProjectGraph.from_plan(plan, EventStore(), today=TODAY)   # nobody reported anything 2-9 Nov
    over = {d.task_id: d for d in pg.check_overdue()}
    assert {"T5-FRAME", "T5-ELEC-RI", "T5-PLUMB-RI", "T6-FRAME", "F2-DEL-BOARDS"} <= set(over)
    assert over["T5-FRAME"].severity == Severity.warning
    assert pg.check_overdue() == [], "raised at most once per task per day"


def test_no_silent_overdue_when_on_track(pg):
    assert pg.check_overdue() == []


def test_downstream_and_critical_path(pg):
    assert pg.downstream("T6-INSP")[0] == "T6-BOARD"
    assert "F2-HANDOVER" in pg.downstream("T5-FRAME")
    cp = pg.critical_path()
    assert cp and cp[-1] == "F2-HANDOVER"


def test_schedule_cache_follows_today(pg):
    first = pg.schedule()["T6-INSP"]
    pg.today = date(2026, 11, 20)
    assert pg.schedule()["T6-INSP"] != first


# ---------------------------------------------------------------- CPM edge cases

def _mini_plan(tasks: list[Task], deps: list[Dependency]) -> ProjectPlan:
    return ProjectPlan(project_id="t", name="t", data_date=date(2026, 1, 1),
                       persons=[Person(id="P", name="p", role=Role.foreman)],
                       locations=[Location(id="L", name="l", kind=LocationKind.unit)],
                       tasks=tasks, dependencies=deps)


def _task(tid: str, start: str, dur: int, **kw) -> Task:
    s = date.fromisoformat(start)
    return Task(id=tid, name=tid, location_id="L", responsible_person_id="P", duration_days=dur,
                planned_start=s, planned_finish=date.fromordinal(s.toordinal() + max(dur, 1) - 1), **kw)


def test_done_task_without_actual_start_uses_its_actual_finish():
    a = _task("A", "2026-01-01", 3, status=TaskStatus.done, actual_finish=date(2026, 1, 5))
    b = _task("B", "2026-01-04", 1)
    pg = ProjectGraph(_mini_plan([a, b], [Dependency(id="d", predecessor_id="A", successor_id="B")]),
                      EventStore(), today=date(2026, 1, 1))
    assert pg.schedule()["A"] == (date(2026, 1, 3), date(2026, 1, 5))
    assert pg.schedule()["B"][0] == date(2026, 1, 6)


def test_cycle_and_dangling_dependency_are_rejected():
    a, b = _task("A", "2026-01-01", 1), _task("B", "2026-01-02", 1)
    cyc = [Dependency(id="1", predecessor_id="A", successor_id="B"),
           Dependency(id="2", predecessor_id="B", successor_id="A")]
    with pytest.raises(ValueError, match="cycle"):
        ProjectGraph(_mini_plan([a, b], cyc), EventStore())
    with pytest.raises(ValueError, match="unknown task"):
        ProjectGraph(_mini_plan([a], [Dependency(id="1", predecessor_id="A", successor_id="X")]), EventStore())
