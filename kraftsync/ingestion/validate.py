"""validate(plan) -> readable problems. Errors make a plan unusable; warnings are worth a look.

Deterministic, no graph import (ingestion must not depend on graph): own cycle check via networkx.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import timedelta

import networkx as nx

from kraftsync.contracts import ProjectPlan, TaskKind

UNASSIGNED = "P-UNASSIGNED"     # placeholder person for tasks a source file assigns to nobody


@dataclass(frozen=True)
class Problem:
    level: str      # "error" | "warning"
    message: str

    def __str__(self) -> str:
        return f"{self.level.upper():7} {self.message}"


def errors(problems: list[Problem]) -> list[Problem]:
    return [p for p in problems if p.level == "error"]


def validate(plan: ProjectPlan) -> list[Problem]:
    out: list[Problem] = []

    def err(msg: str) -> None:
        out.append(Problem("error", msg))

    def warn(msg: str) -> None:
        out.append(Problem("warning", msg))

    # duplicate ids
    for what, ids in (("task", [t.id for t in plan.tasks]), ("person", [p.id for p in plan.persons]),
                      ("location", [loc.id for loc in plan.locations]),
                      ("dependency", [d.id for d in plan.dependencies])):
        for i, n in Counter(ids).items():
            if n > 1:
                err(f"duplicate {what} id {i!r} ({n}x)")

    tasks = {t.id: t for t in plan.tasks}
    persons = {p.id for p in plan.persons}
    locations = {loc.id for loc in plan.locations}

    # dangling references
    for loc in plan.locations:
        if loc.parent_id and loc.parent_id not in locations:
            err(f"location {loc.id}: parent {loc.parent_id!r} does not exist")
    for t in plan.tasks:
        if t.responsible_person_id not in persons:
            err(f"task {t.id}: responsible person {t.responsible_person_id!r} does not exist")
        if t.location_id not in locations:
            err(f"task {t.id}: location {t.location_id!r} does not exist")
        if t.planned_finish < t.planned_start:
            err(f"task {t.id}: planned finish {t.planned_finish} is before planned start {t.planned_start}")
        if t.kind == TaskKind.milestone and t.duration_days != 0:
            err(f"task {t.id}: milestone must have duration 0, has {t.duration_days:g}")
        if t.responsible_person_id == UNASSIGNED:
            warn(f"task {t.id}: nobody is responsible (no resource assigned in the source)")
        if t.kind != TaskKind.milestone and t.duration_days == 0:
            warn(f"task {t.id}: duration 0 but kind is {t.kind.value} (milestone?)")
    for d in plan.dependencies:
        for end in (d.predecessor_id, d.successor_id):
            if end not in tasks:
                err(f"dependency {d.id}: task {end!r} does not exist")
        if d.predecessor_id == d.successor_id:
            err(f"dependency {d.id}: task {d.predecessor_id} depends on itself")

    # graph shape
    g = nx.DiGraph()
    g.add_nodes_from(tasks)
    g.add_edges_from((d.predecessor_id, d.successor_id) for d in plan.dependencies
                     if d.predecessor_id in tasks and d.successor_id in tasks)
    if not nx.is_directed_acyclic_graph(g):
        cycle = " -> ".join(u for u, _ in nx.find_cycle(g))
        err(f"dependency cycle: {cycle}")
    for n in tasks:
        if g.degree(n) == 0 and len(tasks) > 1:
            warn(f"task {n}: no predecessors and no successors (not connected to the plan)")

    # dates consistent with the links (calendar days, matches the graph's CPM rules)
    for d in plan.dependencies:
        p, s = tasks.get(d.predecessor_id), tasks.get(d.successor_id)
        if not (p and s) or d.type.value != "FS":
            continue
        p_end = p.planned_start if p.duration_days == 0 else p.planned_finish + timedelta(days=1)
        earliest = p_end + timedelta(days=d.lag_days)
        if s.planned_start < earliest:
            warn(f"dependency {d.id}: {s.id} is planned to start {s.planned_start} but {p.id} "
                 f"(+{d.lag_days:g} d) only allows {earliest}")

    # baseline should carry no live state
    started = [t.id for t in plan.tasks if t.status.value != "not_started" or t.actual_start or t.actual_finish]
    if started:
        warn(f"{len(started)} task(s) already carry progress ({', '.join(started[:5])}"
             f"{', …' if len(started) > 5 else ''}); a baseline should start with nothing done")
    return out
