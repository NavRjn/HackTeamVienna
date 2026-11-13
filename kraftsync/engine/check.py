"""Deterministic conflict detection. No LLM, no I/O: pure functions over contract models.

`check(before, after, deps, update, now)` compares the task map before and after an applied
TaskUpdate and returns Discrepancies. Minimal v0 (owner: detection): FS/SS dependencies only
(FF/SF are ignored), calendar days, one forward pass for the ripple.
"""

import math
from collections import defaultdict
from datetime import date, datetime, timedelta

import networkx as nx

from kraftsync.contracts import (
    Dependency,
    DependencyType,
    Discrepancy,
    DiscrepancyType,
    Impact,
    Severity,
    Task,
    TaskStatus,
    TaskUpdate,
    UpdateKind,
)

Forecast = dict[str, tuple[date, date]]   # task id -> (start, finish)


def dependency_graph(tasks: dict[str, Task], deps: list[Dependency]) -> nx.DiGraph:
    g = nx.DiGraph()
    g.add_nodes_from(tasks)
    g.add_edges_from((d.predecessor_id, d.successor_id) for d in deps)
    return g


def forecast(tasks: dict[str, Task], deps: list[Dependency]) -> Forecast:
    """Forward pass in topological order. Actuals override plan; open tasks start no earlier than
    planned_start and no earlier than their predecessors allow (FS: finish + lag + 1, SS: start + lag)."""
    incoming: dict[str, list[Dependency]] = defaultdict(list)
    for d in deps:
        incoming[d.successor_id].append(d)
    out: Forecast = {}
    for tid in nx.topological_sort(dependency_graph(tasks, deps)):
        t = tasks[tid]
        if t.status == TaskStatus.done:
            out[tid] = (t.actual_start or t.planned_start, t.actual_finish or t.planned_finish)
        else:
            started = t.actual_start is not None
            start = t.planned_start
            for d in incoming[tid]:
                if started and tasks[d.predecessor_id].status == TaskStatus.done:
                    continue   # already satisfied
                p_start, p_finish = out[d.predecessor_id]
                lag = round(d.lag_days)
                if d.type == DependencyType.FS:
                    start = max(start, p_finish + timedelta(days=lag + 1))
                elif d.type == DependencyType.SS:
                    start = max(start, p_start + timedelta(days=lag))
            finish = start + timedelta(days=max(math.ceil(t.duration_days) - 1, 0))
            if started:
                # out-of-sequence work does not count as progress: the finish still waits for open predecessors
                finish = max(finish, t.forecast_finish or t.planned_finish)
                out[tid] = (t.actual_start, finish)
                continue
            if t.forecast_finish:
                finish = max(finish, t.forecast_finish)
            out[tid] = (start, finish)
    return out


def project_finish(fc: Forecast, tasks: dict[str, Task], deps: list[Dependency]) -> date | None:
    g = dependency_graph(tasks, deps)
    ends = [fc[n][1] for n in g if g.out_degree(n) == 0]
    return max(ends) if ends else None


def unfinished_predecessors(task_id: str, tasks: dict[str, Task], deps: list[Dependency]) -> list[Task]:
    """Predecessors that should be finished (FS) or started (SS) before `task_id` starts."""
    out = []
    for d in deps:
        if d.successor_id != task_id:
            continue
        p = tasks[d.predecessor_id]
        if d.type == DependencyType.FS and p.status not in (TaskStatus.done, TaskStatus.cancelled):
            out.append(p)
        elif d.type == DependencyType.SS and p.status == TaskStatus.not_started:
            out.append(p)
    return out


def check(
    before: dict[str, Task],
    after: dict[str, Task],
    deps: list[Dependency],
    update: TaskUpdate,
    now: datetime,
) -> list[Discrepancy]:
    if update.task_id is None:
        return []
    tid = update.task_id
    b, a = before[tid], after[tid]
    found: list[Discrepancy] = []

    def raise_(kind: DiscrepancyType, severity: Severity, description: str, impact: Impact | None = None):
        found.append(Discrepancy(
            id=f"{update.message_id}:{kind.value}", type=kind, severity=severity, task_id=tid,
            detected_at=now, description=description, impact=impact,
        ))

    # 1. sequence: work starts/finishes while a predecessor is not done
    if update.kind in (UpdateKind.started, UpdateKind.completed) and b.status == TaskStatus.not_started:
        open_preds = unfinished_predecessors(tid, after, deps)
        if open_preds:
            raise_(DiscrepancyType.out_of_sequence, Severity.warning,
                   f"{a.name} is {update.kind.value} but predecessor(s) not finished: "
                   + ", ".join(p.name for p in open_preds))
        hold = [p for p in open_preds if p.hold_point]
        if hold:
            raise_(DiscrepancyType.hold_point_violation, Severity.critical,
                   f"{a.name} is {update.kind.value} before hold point passed: "
                   + ", ".join(p.name for p in hold))

    # 2. delay + ripple
    fc_b, fc_a = forecast(before, deps), forecast(after, deps)
    finish_b, finish_a = fc_b[tid][1], fc_a[tid][1]
    new_report = update.new_forecast_finish is not None and update.new_forecast_finish != b.forecast_finish
    if finish_a > a.planned_finish and (finish_a > finish_b or new_report):
        order = nx.topological_sort(dependency_graph(after, deps))
        affected = [t for t in order if t != tid and fc_a[t] != fc_b[t]]
        pf_b, pf_a = project_finish(fc_b, before, deps), project_finish(fc_a, after, deps)
        moved = pf_b is not None and pf_a is not None and pf_a > pf_b
        impact = Impact(
            delay_days=(finish_a - a.planned_finish).days,
            affected_task_ids=affected,
            critical_path_affected=moved,
            project_finish_before=pf_b,
            project_finish_after=pf_a,
        )
        text = (f"{a.name}: finish {a.planned_finish} -> {finish_a} (+{(finish_a - a.planned_finish).days}d); "
                f"{len(affected)} downstream task(s) shift")
        if moved:
            text += f"; project finish {pf_b} -> {pf_a}"
        raise_(DiscrepancyType.delay, Severity.critical if moved else Severity.warning, text, impact)

    # 3. scope change / blocked
    if update.kind == UpdateKind.modified:
        raise_(DiscrepancyType.scope_change, Severity.warning,
               f"Scope change on {a.name}: {update.change_description or update.rationale}")
    if update.kind == UpdateKind.blocked:
        raise_(DiscrepancyType.blocked, Severity.warning,
               f"{a.name} blocked: {update.blocker or update.rationale}")
    return found
