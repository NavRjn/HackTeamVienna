"""Resolution policy v0: Discrepancy -> Actions. Deterministic; rules and texts come from policy.yaml.

Who hears what: people whose work MOVES get an FYI, the site manager gets decisions,
the sender is only asked to clarify. One action per person per message (highest priority wins).
Dates, ripple and predecessor facts are read from the graph (`kraftsync.graph.ProjectGraph`); nothing is recomputed here.
"""

from datetime import date, datetime
from functools import lru_cache
from pathlib import Path

import yaml

from kraftsync.contracts import (
    Action,
    ActionKind,
    Channel,
    Discrepancy,
    DiscrepancyType,
    Person,
    Role,
    Task,
    TaskUpdate,
)
from kraftsync.graph import ProjectGraph

Schedule = dict[str, tuple[date, date]]   # task id -> (start, finish), as returned by ProjectGraph.schedule()

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


def acknowledge(sender: Person, task_names: list[str], informed: list[str], message_id: str, now: datetime) -> Action:
    """Confirm to the sender that their answer to a clarification question was applied (and who was told)."""
    text = _text(sender, "ack", tasks=", ".join(task_names))
    if informed:
        text += _text(sender, "ack_informed", names=", ".join(informed))
    return _action(f"{message_id}:ack", sender, ActionKind.notify, text, now)


def resolve_all(
    discrepancies: list[Discrepancy],
    pg: ProjectGraph,
    before: Schedule,
    after: Schedule,
    update: TaskUpdate,
    sender: Person,
    now: datetime,
) -> list[Action]:
    """`before` / `after` = pg.schedule() taken around the applied update."""
    persons, tasks = pg.persons, pg.tasks
    managers = [p for p in persons.values() if p.role == Role.site_manager]
    inspectors = [p for p in persons.values() if p.role == Role.inspector]
    levels = _policy().get("downstream_levels_to_notify", 2)
    cause = update.blocker or update.rationale
    actions: list[Action] = []

    for d in discrepancies:
        task = tasks[d.task_id] if d.task_id else None
        if d.type == DiscrepancyType.delay and d.impact:
            reach = pg.reach(d.task_id, levels)
            told: set[str] = set()
            for tid in d.impact.affected_task_ids:
                person = persons.get(tasks[tid].responsible_person_id)
                if tid not in reach or person is None or person.id in told \
                        or person.id == sender.id or person.role == Role.site_manager:
                    continue
                told.add(person.id)
                actions.append(_action(d.id, person, ActionKind.notify, _text(
                    person, "delay_notify", task_name=tasks[tid].name,
                    old_start=before[tid][0], new_start=after[tid][0], cause=cause), now))
            pf_b, pf_a = d.impact.project_finish_before, d.impact.project_finish_after
            if pf_b and pf_a and pf_a > pf_b:   # the handover moved: the site manager decides
                end = pg.final_task(after)
                for sm in managers:
                    actions.append(_action(d.id, sm, ActionKind.escalate, _text(
                        sm, "delay_escalate", milestone=tasks[end].name,
                        old=pf_b, new=pf_a, days=(pf_a - pf_b).days, cause=cause), now))
        elif d.type == DiscrepancyType.hold_point_violation and task:
            hold = [tasks[p] for p in pg.open_predecessors(task.id) if tasks[p].hold_point]
            for p in managers + inspectors:
                actions.append(_action(d.id, p, ActionKind.escalate, _text(
                    p, "hold_point", task_name=task.name, inspection_name=hold[0].name), now))
            actions.append(_action(d.id, sender, ActionKind.ask, _text(
                sender, "hold_point", task_name=task.name, inspection_name=hold[0].name), now))
        elif d.type == DiscrepancyType.out_of_sequence and task:
            open_preds = [tasks[p] for p in pg.open_predecessors(task.id)]
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
