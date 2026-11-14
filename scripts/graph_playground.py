"""Step-by-step tour of the dependency graph. Run:

    uv run python scripts/graph_playground.py

Read the output top to bottom, then change things below and run it again.
"""

import sys
from datetime import date
from pathlib import Path

from kraftsync.contracts import ProjectPlan, TaskUpdate, UpdateKind
from kraftsync.graph import EventStore, ProjectGraph, feed, read_reports

sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # Windows consoles (cp1251/cp1252)
DEMO = Path(__file__).resolve().parents[1] / "data" / "demo"
TODAY = date(2026, 11, 9)  # the demo story's "today"


def title(text: str) -> None:
    print("\n" + "=" * 70 + f"\n{text}\n" + "=" * 70)


def message(task_id, kind, sender, **extra) -> TaskUpdate:
    """Pretend the engine already read a chat message and understood it."""
    return TaskUpdate(message_id=f"demo-{task_id}", task_id=task_id, kind=kind, confidence=0.9,
                      reported_on=TODAY, rationale="playground", model="manual", **extra)


# --- STEP 1: load the plan and build the graph ---------------------------------
title("STEP 1 - load plan.json (baseline, 2 Nov) and replay what happened 2-9 Nov")
plan = ProjectPlan.model_validate_json((DEMO / "plan.json").read_text(encoding="utf-8"))
store = EventStore()                    # the diary, in memory (nothing written to disk)
pg = ProjectGraph.from_plan(plan, store, today=plan.data_date)
history = feed(pg, read_reports(DEMO / "history.jsonl"))
print(f"History messages:     {len(history)} (now {pg.today})")
print(f"Nodes (tasks):        {pg.g.number_of_nodes()}")
print(f"Edges (dependencies): {pg.g.number_of_edges()}")
print(f"Planned handover:     {pg.project_finish()}")

# --- STEP 2: look at one node and its arrows ---------------------------------
title("STEP 2 - one node and its arrows: T6-BOARD (Top 6 drywall boarding)")
t = pg.tasks["T6-BOARD"]
print(f"Name:     {t.name}")
print(f"Who:      {pg.persons[t.responsible_person_id].name}")
print(f"Status:   {t.status.value}")
print(f"Planned:  {t.planned_start} -> {t.planned_finish}")
print(f"Must wait for (arrows in):  {list(pg.g.predecessors('T6-BOARD'))}")
print(f"Others wait for it (out):   {list(pg.g.successors('T6-BOARD'))}")
print(f"Everything downstream:      {pg.downstream('T6-BOARD')}")

# --- STEP 3: the status of every task today ----------------------------------
title("STEP 3 - status of every task on " + TODAY.isoformat())
for task_id, (start, finish) in pg.schedule().items():
    print(f"  {task_id:15} {pg.tasks[task_id].status.value:12} {start} -> {finish}")

# --- STEP 4: a message arrives - plumber is late -----------------------------
title('STEP 4 - plumber writes: "Top 6 plumbing won\'t be done, Thursday at the earliest"')
events = pg.apply(message("T6-PLUMB-RI", UpdateKind.delayed, "P-SAN",
                          new_forecast_finish=date(2026, 11, 13)), actor="P-SAN")
for e in events:
    print(f"  diary entry #{e.id}: {e.kind.value}  before={e.before}  after={e.after}")
for d in pg.detect(events):
    print(f"\n  ALERT [{d.severity.value}] {d.type.value}\n  {d.description}")
    print(f"  affected: {d.impact.affected_task_ids}")
print(f"\nHandover is now: {pg.project_finish()}")

# --- STEP 5: a message arrives - drywall starts too early --------------------
title('STEP 5 - drywaller writes: "Started boarding the Top 6 bathroom"')
for d in pg.detect(pg.apply(message("T6-BOARD", UpdateKind.started, "P-DRY"), actor="P-DRY")):
    print(f"\n  ALERT [{d.severity.value}] {d.type.value}\n  {d.description}")

# --- STEP 6: a message the AI wasn't sure about -------------------------------
title("STEP 6 - unsure interpretation (confidence 0.3) is NOT applied")
unsure = message("T5-PAINT", UpdateKind.started, "P-PNT").model_copy(update={"confidence": 0.3})
events = pg.apply(unsure, actor="P-PNT")
print(f"  applied? {events[0].payload['applied']}   T5-PAINT status: {pg.tasks['T5-PAINT'].status.value}")

# --- STEP 7: the diary ---------------------------------------------------------
title("STEP 7 - the full diary (append-only event log)")
for e in store.all():
    print(f"  #{e.id:<3} {e.kind.value:20} task={e.task_id}")

# --- STEP 8: restart = reread the diary ---------------------------------------
title("STEP 8 - 'restart the server': rebuild from the diary only")
again = ProjectGraph.from_plan(plan, store, today=TODAY)
print(f"  T6-BOARD status after restart: {again.tasks['T6-BOARD'].status.value}")
print(f"  Same state as before restart?  {again.schedule() == pg.schedule()}")