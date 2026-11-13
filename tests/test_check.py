from pathlib import Path

from kraftsync.contracts import ProjectPlan, TaskStatus
from kraftsync.engine.check import forecast, project_finish

DEMO = Path(__file__).resolve().parents[1] / "data" / "demo"


def test_forecast_reproduces_planned_dates_of_the_demo_plan():
    plan = ProjectPlan.model_validate_json((DEMO / "plan.json").read_text(encoding="utf-8"))
    tasks = {t.id: t for t in plan.tasks}
    fc = forecast(tasks, plan.dependencies)
    for t in plan.tasks:
        if t.status != TaskStatus.done:
            assert fc[t.id] == (t.planned_start, t.planned_finish), t.id
    assert str(project_finish(fc, tasks, plan.dependencies)) == "2026-12-06"
