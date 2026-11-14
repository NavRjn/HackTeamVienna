"""Critical Path Method: forecast dates + float, with actuals overriding the plan.

Internally every date is a day number (date.toordinal()); `ef`/`lf` are EXCLUSIVE ends,
so a 3-day task starting on day 10 has es=10, ef=13 and finishes (inclusive) on day 12.

The graph structure never changes after load, so `Net.compile()` does the topological sort
and builds plain adjacency lists ONCE. Each schedule() is then a single O(V+E) pass over lists,
with no NetworkX calls and no re-sorting.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import networkx as nx

from kraftsync.contracts import Task, TaskStatus

Edge = tuple[str, str, int]   # (other task id, dependency type, lag in days)


@dataclass(frozen=True)
class Slot:
    start: date
    finish: date          # inclusive (milestones: finish == start)
    total_float: float    # days of slack; <= 0 means critical


@dataclass(frozen=True)
class Net:
    """Immutable, precomputed view of the dependency graph."""
    order: tuple[str, ...]                    # topological order
    index: dict[str, int]                     # task id -> position in `order`
    preds: dict[str, tuple[Edge, ...]]
    succs: dict[str, tuple[Edge, ...]]

    @classmethod
    def compile(cls, g: nx.DiGraph) -> Net:
        order = tuple(nx.lexicographical_topological_sort(g))   # deterministic tie-break
        preds = {n: tuple((p, g.edges[p, n]["type"], round(g.edges[p, n]["lag"])) for p in g.predecessors(n))
                 for n in order}
        succs = {n: tuple((s, g.edges[n, s]["type"], round(g.edges[n, s]["lag"])) for s in g.successors(n))
                 for n in order}
        return cls(order, {n: i for i, n in enumerate(order)}, preds, succs)

    def sort(self, ids) -> list[str]:
        """Any set of task ids in topological order, without re-sorting the graph."""
        return sorted(ids, key=self.index.__getitem__)


def durations(tasks: dict[str, Task]) -> dict[str, int]:
    return {n: 0 if t.status == TaskStatus.cancelled else round(t.duration_days) for n, t in tasks.items()}


def _inclusive_finish(es: int, ef: int) -> date:
    return date.fromordinal(es if ef == es else ef - 1)


def forward_pass(net: Net, tasks: dict[str, Task], today: date,
                 dur: dict[str, int] | None = None) -> dict[str, tuple[int, int]]:
    """{task_id: (es, ef)} as day numbers. Rules: docs/04-graph -> CPM rules."""
    dur = dur or durations(tasks)
    t0 = today.toordinal()
    es: dict[str, int] = {}
    ef: dict[str, int] = {}
    for n in net.order:
        t = tasks[n]
        d = dur[n]

        if t.status == TaskStatus.done and (t.actual_start or t.actual_finish):
            f = t.actual_finish.toordinal() + 1 if t.actual_finish else t.actual_start.toordinal() + d
            s = t.actual_start.toordinal() if t.actual_start else f - d
            es[n], ef[n] = s, max(f, s)
            continue

        if t.status in (TaskStatus.in_progress, TaskStatus.blocked) and t.actual_start:
            s = t.actual_start.toordinal()
            f = max(s + d, t0 + 1)                       # not done yet -> finishes today at the earliest
            if t.forecast_finish:
                f = max(f, t.forecast_finish.toordinal() + 1)
            # Started out of sequence: it still cannot finish before its unfinished predecessors.
            # Behind an open hold point the work must be reopened and redone after the inspection,
            # so starting early never makes the forecast look better.
            for p, typ, lag in net.preds[n]:
                if typ in ("FS", "FF") and tasks[p].status != TaskStatus.done:
                    f = max(f, ef[p] + lag + (d if tasks[p].hold_point and typ == "FS" else 0))
            es[n], ef[n] = s, f
            continue

        # not started: earliest of today, its planned slot (crews are booked), and its predecessors
        s = max(t0, t.planned_start.toordinal())
        for p, typ, lag in net.preds[n]:
            if typ == "FS":
                s = max(s, ef[p] + lag)
            elif typ == "SS":
                s = max(s, es[p] + lag)
            elif typ == "FF":
                s = max(s, ef[p] + lag - d)
            elif typ == "SF":
                s = max(s, es[p] + lag - d)
        if t.forecast_finish:                             # "can't do it before Friday"
            s = max(s, t.forecast_finish.toordinal() + 1 - d)
        es[n], ef[n] = s, s + d
    return {n: (es[n], ef[n]) for n in net.order}


def backward_pass(net: Net, tasks: dict[str, Task], fwd: dict[str, tuple[int, int]],
                  dur: dict[str, int] | None = None) -> dict[str, float]:
    """Total float per task, measured against the project's finish."""
    dur = dur or durations(tasks)
    project_end = max(f for _, f in fwd.values())
    lf: dict[str, int] = {}
    for n in reversed(net.order):
        d = dur[n]
        late = project_end
        for s, typ, lag in net.succs[n]:
            ls_s = lf[s] - dur[s]
            if typ == "FS":
                late = min(late, ls_s - lag)
            elif typ == "SS":
                late = min(late, ls_s - lag + d)
            elif typ == "FF":
                late = min(late, lf[s] - lag)
            elif typ == "SF":
                late = min(late, lf[s] - lag + d)
        lf[n] = late
    return {n: float(lf[n] - fwd[n][1]) for n in net.order}


def schedule(net: Net, tasks: dict[str, Task], today: date) -> dict[str, Slot]:
    dur = durations(tasks)
    fwd = forward_pass(net, tasks, today, dur)
    flt = backward_pass(net, tasks, fwd, dur)
    return {n: Slot(date.fromordinal(es), _inclusive_finish(es, ef), flt[n]) for n, (es, ef) in fwd.items()}
