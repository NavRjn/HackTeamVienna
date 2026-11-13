# 02 · Ingestion

Goal: anything a real project already has → a valid `ProjectPlan`.

## Principle

The demo runs on `data/demo/plan.json` (generated, consistent). Ingestion must **produce the same shape** and is shown working in the demo ("we import your existing schedule"), but the live pipeline does not depend on its output. Less error surface.

## Sources, in priority order

| # | Source | How | Notes |
|---|---|---|---|
| 1 | CSV set (`persons.csv`, `locations.csv`, `tasks.csv`, `dependencies.csv`) | pandas/csv → Pydantic | Same columns as `data/demo/*.csv`. Must work. |
| 2 | MS Project XML (MSPDI) / Primavera P6 XER | **MPXJ** (`pip install mpxj`, needs a Java runtime via JPype) | The credibility win: real schedulers export these. Map Task ↔ Task, Relation ↔ Dependency (type + lag), Resource ↔ Person. |
| 3 | Excel schedules | openpyxl → same as CSV | Many small contractors live in Excel. |
| 4 | PDF (Bauzeitplan, persons/contact lists, plans) | LLM/vision extraction → **draft** `ProjectPlan` → human review → freeze to JSON | Plan-time only. Never live. Show it, don't depend on it. |

Get a sample MSPDI/XER file early (GanttProject can export MS Project XML; MPXJ's repo has test files). If Java is painful, drop to CSV + Excel.

## Validation (must reject bad plans loudly)

- Unique ids; every `responsible_person_id`, `location_id`, dependency endpoint exists.
- Graph is acyclic.
- `planned_finish >= planned_start`; milestones have duration 0.
- Warn (don't fail) on tasks with no predecessors and no successors.
- Output a validation report, not just an exception.

## Interface

```python
def load_plan(path: Path) -> ProjectPlan      # dispatch on suffix / folder
def validate(plan: ProjectPlan) -> list[str]  # list of problems, empty = ok
```
CLI: `uv run python -m kraftsync.ingestion <path> -o plan.json`

## Done when

- CSV folder → `ProjectPlan` → identical to `data/demo/plan.json` (round-trip test).
- One MS Project XML or XER file imported and loaded by the graph.
