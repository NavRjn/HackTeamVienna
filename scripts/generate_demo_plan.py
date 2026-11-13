"""Generate the demo project: 2nd-floor fit-out of two apartments (Top 5, Top 6).

Writes data/demo/{persons,locations,tasks,dependencies}.csv and plan.json.
Planned dates come from a CPM forward pass, so the plan is internally consistent.

Run:  uv run python scripts/generate_demo_plan.py
"""

from __future__ import annotations

import csv
import json
from datetime import date, timedelta
from pathlib import Path

import networkx as nx

from kraftsync.contracts import (
    Dependency, DependencyType, Location, LocationKind, Person, ProjectPlan,
    Role, Task, TaskKind, TaskStatus,
)

OUT = Path(__file__).resolve().parents[1] / "data" / "demo"
PROJECT_START = date(2026, 11, 2)   # Monday
DATA_DATE = date(2026, 11, 9)       # "today" in the demo story (Monday)

persons = [
    Person(id="P-SM", name="Anna Huber", role=Role.site_manager, company="Rhein Bau GmbH", phone="+436600000001", language="de"),
    Person(id="P-DRY", name="Tomasz Nowak", role=Role.foreman, company="Nowak Trockenbau", trade="drywall", phone="+436600000002", language="de"),
    Person(id="P-ELE", name="Marko Petrović", role=Role.foreman, company="Elektro Petrović", trade="electrical", phone="+436600000003", language="de"),
    Person(id="P-SAN", name="Emre Yılmaz", role=Role.foreman, company="Yılmaz Haustechnik", trade="plumbing", phone="+436600000004", language="de"),
    Person(id="P-INS", name="Klaus Berger", role=Role.inspector, company="ÖBA Berger ZT", trade="inspection", phone="+436600000005", language="de"),
    Person(id="P-SCR", name="Stefan Gruber", role=Role.foreman, company="Gruber Estrich", trade="screed", phone="+436600000006", language="de"),
    Person(id="P-TIL", name="Luca Bianchi", role=Role.foreman, company="Bianchi Fliesen", trade="tiling", phone="+436600000007", language="en"),
    Person(id="P-PNT", name="Jana Novak", role=Role.foreman, company="Novak Maler", trade="painting", phone="+436600000008", language="de"),
    Person(id="P-FLR", name="Ivana Kovač", role=Role.foreman, company="Kovač Böden", trade="flooring", phone="+436600000009", language="de"),
]

locations = [
    Location(id="B1", name="Wohnhaus Arsenal", kind=LocationKind.building),
    Location(id="F2", name="2. OG", kind=LocationKind.floor, parent_id="B1"),
    Location(id="T5", name="Top 5", kind=LocationKind.unit, parent_id="F2"),
    Location(id="T5-BATH", name="Top 5 Bad", kind=LocationKind.room, parent_id="T5"),
    Location(id="T5-LIV", name="Top 5 Wohnzimmer", kind=LocationKind.room, parent_id="T5"),
    Location(id="T6", name="Top 6", kind=LocationKind.unit, parent_id="F2"),
    Location(id="T6-BATH", name="Top 6 Bad", kind=LocationKind.room, parent_id="T6"),
    Location(id="T6-LIV", name="Top 6 Wohnzimmer", kind=LocationKind.room, parent_id="T6"),
]

# (id, name, kind, trade, location, responsible, duration, hold_point, boq_ref)
raw_tasks: list[tuple] = [
    ("F2-DEL-BOARDS", "Lieferung Gipskartonplatten 2. OG", TaskKind.delivery, "logistics", "F2", "P-SM", 1, False, None),
    ("F2-HANDOVER", "Übergabe 2. OG", TaskKind.milestone, None, "F2", "P-SM", 0, False, None),
]
for u in ("T5", "T6"):
    raw_tasks += [
        (f"{u}-FRAME", f"{u} Ständerwerk Trockenbau", TaskKind.work, "drywall", u, "P-DRY", 3, False, "LG39.01"),
        (f"{u}-ELEC-RI", f"{u} Elektro Rohinstallation", TaskKind.work, "electrical", u, "P-ELE", 3, False, "LG41.02"),
        (f"{u}-PLUMB-RI", f"{u} Sanitär Rohinstallation + Vorwand", TaskKind.work, "plumbing", f"{u}-BATH", "P-SAN", 3, False, "LG42.01"),
        (f"{u}-INSP", f"{u} Abnahme Installationen (vor Beplankung)", TaskKind.inspection, "inspection", u, "P-INS", 1, True, None),
        (f"{u}-BOARD", f"{u} Beplankung + Dämmung", TaskKind.work, "drywall", u, "P-DRY", 3, False, "LG39.02"),
        (f"{u}-SCREED", f"{u} Estrich", TaskKind.work, "screed", u, "P-SCR", 1, False, "LG11.01"),
        (f"{u}-WATERPROOF", f"{u} Abdichtung Bad", TaskKind.work, "tiling", f"{u}-BATH", "P-TIL", 1, False, "LG24.01"),
        (f"{u}-TILE", f"{u} Fliesen Bad", TaskKind.work, "tiling", f"{u}-BATH", "P-TIL", 3, False, "LG24.02"),
        (f"{u}-PAINT", f"{u} Malerarbeiten", TaskKind.work, "painting", u, "P-PNT", 3, False, "LG45.01"),
        (f"{u}-FLOOR", f"{u} Parkett", TaskKind.work, "flooring", f"{u}-LIV", "P-FLR", 2, False, "LG32.01"),
        (f"{u}-ELEC-FF", f"{u} Elektro Fertiginstallation", TaskKind.work, "electrical", u, "P-ELE", 1, False, "LG41.05"),
        (f"{u}-SAN-FF", f"{u} Sanitär Fertigmontage", TaskKind.work, "plumbing", f"{u}-BATH", "P-SAN", 1, False, "LG42.05"),
    ]

# (pred, succ, type, lag, reason)
raw_deps: list[tuple] = []
for u in ("T5", "T6"):
    raw_deps += [
        (f"{u}-FRAME", f"{u}-ELEC-RI", "FS", 0, None),
        (f"{u}-FRAME", f"{u}-PLUMB-RI", "FS", 0, None),
        (f"{u}-ELEC-RI", f"{u}-INSP", "FS", 0, "hold point"),
        (f"{u}-PLUMB-RI", f"{u}-INSP", "FS", 0, "hold point"),
        (f"{u}-INSP", f"{u}-BOARD", "FS", 0, "do not close walls before inspection"),
        ("F2-DEL-BOARDS", f"{u}-BOARD", "FS", 0, "material"),
        (f"{u}-BOARD", f"{u}-SCREED", "FS", 0, None),
        (f"{u}-SCREED", f"{u}-WATERPROOF", "FS", 14, "screed drying (shortened for demo)"),
        (f"{u}-WATERPROOF", f"{u}-TILE", "FS", 1, "waterproofing cure"),
        (f"{u}-BOARD", f"{u}-PAINT", "FS", 2, "filling/sanding"),
        (f"{u}-SCREED", f"{u}-FLOOR", "FS", 14, "screed drying (shortened for demo)"),
        (f"{u}-PAINT", f"{u}-FLOOR", "FS", 0, None),
        (f"{u}-PAINT", f"{u}-ELEC-FF", "FS", 0, None),
        (f"{u}-TILE", f"{u}-SAN-FF", "FS", 0, None),
        (f"{u}-ELEC-FF", "F2-HANDOVER", "FS", 0, None),
        (f"{u}-SAN-FF", "F2-HANDOVER", "FS", 0, None),
        (f"{u}-FLOOR", "F2-HANDOVER", "FS", 0, None),
    ]
# same crews move from Top 5 to Top 6 -> cross-unit coupling
raw_deps += [
    ("T5-FRAME", "T6-FRAME", "FS", 0, "same drywall crew"),
    ("T5-BOARD", "T6-BOARD", "FS", 0, "same drywall crew"),
    ("T5-ELEC-RI", "T6-ELEC-RI", "FS", 0, "same electrical crew"),
    ("T5-PLUMB-RI", "T6-PLUMB-RI", "FS", 0, "same plumbing crew"),
]
# boards arrive on day 6 (fixed delivery slot)
DELIVERY_OFFSET = 6


def forward_pass(durations: dict[str, float], deps: list[tuple]) -> dict[str, tuple[float, float]]:
    """Return {task: (ES, EF)} in days from project start. EF is exclusive."""
    g = nx.DiGraph()
    g.add_nodes_from(durations)
    for p, s, t, lag, _ in deps:
        g.add_edge(p, s, type=t, lag=lag)
    assert nx.is_directed_acyclic_graph(g), "plan has a cycle"
    es: dict[str, float] = {}
    ef: dict[str, float] = {}
    for n in nx.topological_sort(g):
        d = durations[n]
        start = DELIVERY_OFFSET if n == "F2-DEL-BOARDS" else 0.0
        for p in g.predecessors(n):
            e = g.edges[p, n]
            t, lag = e["type"], e["lag"]
            if t == "FS":
                start = max(start, ef[p] + lag)
            elif t == "SS":
                start = max(start, es[p] + lag)
            elif t == "FF":
                start = max(start, ef[p] + lag - d)
            elif t == "SF":
                start = max(start, es[p] + lag - d)
        es[n], ef[n] = start, start + d
    return {n: (es[n], ef[n]) for n in durations}


def main() -> None:
    durations = {t[0]: float(t[6]) for t in raw_tasks}
    sched = forward_pass(durations, raw_deps)

    tasks: list[Task] = []
    for (tid, name, kind, trade, loc, resp, dur, hp, boq) in raw_tasks:
        es, ef = sched[tid]
        start = PROJECT_START + timedelta(days=es)
        finish = start if dur == 0 else PROJECT_START + timedelta(days=ef - 1)
        tasks.append(Task(
            id=tid, name=name, kind=kind, trade=trade, location_id=loc,
            responsible_person_id=resp, duration_days=dur,
            planned_start=start, planned_finish=finish, hold_point=hp, boq_ref=boq,
        ))

    # Live state as of DATA_DATE, chosen so the demo story has something to find:
    # - everything finishing before DATA_DATE is done on time
    # - tasks spanning DATA_DATE are in progress
    for t in tasks:
        if t.planned_finish < DATA_DATE:
            t.status, t.actual_start, t.actual_finish, t.percent_complete = (
                TaskStatus.done, t.planned_start, t.planned_finish, 100)
        elif t.planned_start <= DATA_DATE <= t.planned_finish:
            t.status, t.actual_start, t.percent_complete = TaskStatus.in_progress, t.planned_start, 50

    deps = [
        Dependency(id=f"D{i:03d}", predecessor_id=p, successor_id=s,
                   type=DependencyType(t), lag_days=lag, reason=r)
        for i, (p, s, t, lag, r) in enumerate(raw_deps, 1)
    ]
    plan = ProjectPlan(
        project_id="arsenal-f2", name="Wohnhaus Arsenal – Ausbau 2. OG",
        data_date=DATA_DATE, persons=persons, locations=locations,
        tasks=tasks, dependencies=deps,
    )

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "plan.json").write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    for fname, rows in (("persons.csv", persons), ("locations.csv", locations),
                        ("tasks.csv", tasks), ("dependencies.csv", deps)):
        dicts = [json.loads(r.model_dump_json()) for r in rows]
        with open(OUT / fname, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(dicts[0].keys()))
            w.writeheader()
            for d in dicts:
                w.writerow({k: (";".join(v) if isinstance(v, list) else v) for k, v in d.items()})

    print(f"{len(tasks)} tasks, {len(deps)} dependencies -> {OUT}")
    for t in sorted(tasks, key=lambda t: t.planned_start):
        print(f"  {t.planned_start} → {t.planned_finish}  {t.status.value:12} {t.id}")


if __name__ == "__main__":
    main()
