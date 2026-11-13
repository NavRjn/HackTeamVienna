"""Resolution policy v0: Discrepancy -> Actions. Deterministic; rules and texts come from policy.yaml.

Who hears what: people whose work MOVES get an FYI, the site manager gets decisions,
the sender is only asked to clarify. One action per person per message (highest priority wins).
"""

from datetime import datetime
from functools import lru_cache
from pathlib import Path

import networkx as nx
import yaml

from kraftsync.contracts import (
    Action,
    ActionKind,
    Channel,
    Discrepancy,
    DiscrepancyType,
    Dependency,
    Person,
    Role,
    Task,
    TaskUpdate,
)
from kraftsync.engine.check import dependency_graph, forecast, unfinished_predecessors

POLICY_PATH = Path(__file__).resolve().parents[1] / "policy" / "policy.yaml"
_PRIORITY = {ActionKind.escalate: 0, ActionKind.ask: 1, ActionKind.notify: 2}


@lru_cache(maxsize=1)
def _policy() -> dict:
    return yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))


def _text(person: Person, key: str, **kw) -> str:
    templates = _policy()["templates"]
    lang = person.language if person.language in templates else "de"
    return templates[lang][key].format(**kw)


def _action(d_id: str | None, person: Person, kind: ActionKind, text: str, now: datetime) -> Action:
    return Action(
        id=f"{d_id or 'ask'}:{person.id}", discrepancy_id=d_id, kind=kind, to_person_id=person.id,
        channel=Channel.telegram, text=text, created_at=now, reply_expected=kind != ActionKind.notify,
    )


def ask_clarification(sender: Person, options: list[Task], message_id: str, now: datetime) -> Action:
    names = ", ".join(t.name for t in options[:5]) or "-"
    return _action(f"{message_id}:clarify", sender, ActionKind.ask, _text(sender, "clarify", options=names), now)


def resolve_all(
    discrepancies: list[Discrepancy],
    before: dict[str, Task],
    after: dict[str, Task],
    deps: list[Dependency],
    persons: dict[str, Person],
    update: TaskUpdate,
    sender: Person,
    now: datetime,
) -> list[Action]:
    managers = [p for p in persons.values() if p.role == Role.site_manager]
    inspectors = [p for p in persons.values() if p.role == Role.inspector]
    fc_b, fc_a = forecast(before, deps), forecast(after, deps)
    graph = dependency_graph(after, deps)
    levels = _policy().get("downstream_levels_to_notify", 2)
    cause = update.blocker or update.rationale
    actions: list[Action] = []

    for d in discrepancies:
        task = after[d.task_id] if d.task_id else None
        if d.type == DiscrepancyType.delay and d.impact:
            reach = nx.single_source_shortest_path_length(graph, d.task_id, cutoff=levels)
            told: set[str] = set()
            for tid in d.impact.affected_task_ids:
                person = persons.get(after[tid].responsible_person_id)
                if tid not in reach or person is None or person.id in told \
                        or person.id == sender.id or person.role == Role.site_manager:
                    continue
                told.add(person.id)
                actions.append(_action(d.id, person, ActionKind.notify, _text(
                    person, "delay_notify", task_name=after[tid].name,
                    old_start=fc_b[tid][0], new_start=fc_a[tid][0], cause=cause), now))
            if d.impact.critical_path_affected:
                end = max((n for n in graph if graph.out_degree(n) == 0), key=lambda n: fc_a[n][1])
                for sm in managers:
                    actions.append(_action(d.id, sm, ActionKind.escalate, _text(
                        sm, "delay_escalate", milestone=after[end].name,
                        old=d.impact.project_finish_before, new=d.impact.project_finish_after,
                        days=(d.impact.project_finish_after - d.impact.project_finish_before).days,
                        cause=cause), now))
        elif d.type == DiscrepancyType.hold_point_violation and task:
            hold = [p for p in unfinished_predecessors(task.id, after, deps) if p.hold_point]
            for p in managers + inspectors:
                actions.append(_action(d.id, p, ActionKind.escalate, _text(
                    p, "hold_point", task_name=task.name, inspection_name=hold[0].name), now))
            actions.append(_action(d.id, sender, ActionKind.ask, _text(
                sender, "hold_point", task_name=task.name, inspection_name=hold[0].name), now))
        elif d.type == DiscrepancyType.out_of_sequence and task:
            open_preds = unfinished_predecessors(task.id, after, deps)
            names = ", ".join(p.name for p in open_preds)
            owners = {sender.id: sender} | {
                p.responsible_person_id: persons[p.responsible_person_id]
                for p in open_preds if p.responsible_person_id in persons}
            for person in owners.values():
                actions.append(_action(d.id, person, ActionKind.ask, _text(
                    person, "out_of_sequence", task_name=task.name, pred_names=names), now))
        elif d.type == DiscrepancyType.scope_change and task:
            for sm in managers:
                actions.append(_action(d.id, sm, ActionKind.escalate, _text(
                    sm, "scope_change", task_name=task.name,
                    description=update.change_description or update.rationale, who=sender.name), now))
        elif d.type == DiscrepancyType.blocked and task:
            for sm in managers:
                actions.append(_action(d.id, sm, ActionKind.escalate, _text(
                    sm, "blocked", task_name=task.name, blocker=update.blocker or update.rationale), now))

    best: dict[str, Action] = {}
    for a in actions:
        if a.to_person_id not in best or _PRIORITY[a.kind] < _PRIORITY[best[a.to_person_id].kind]:
            best[a.to_person_id] = a
    return list(best.values())
