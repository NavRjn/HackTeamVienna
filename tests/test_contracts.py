import json
from pathlib import Path

import networkx as nx

from kraftsync.contracts import InboundMessage, ProjectPlan

DEMO = Path(__file__).resolve().parents[1] / "data" / "demo"


def test_demo_plan_is_valid_and_acyclic():
    plan = ProjectPlan.model_validate_json((DEMO / "plan.json").read_text(encoding="utf-8"))
    ids = {t.id for t in plan.tasks}
    persons = {p.id for p in plan.persons}
    locations = {loc.id for loc in plan.locations}
    assert len(ids) == len(plan.tasks)
    for t in plan.tasks:
        assert t.responsible_person_id in persons
        assert t.location_id in locations
        assert t.planned_finish >= t.planned_start
    g = nx.DiGraph((d.predecessor_id, d.successor_id) for d in plan.dependencies)
    assert set(g.nodes) <= ids
    assert nx.is_directed_acyclic_graph(g)


def test_demo_messages_parse_and_gold_refers_to_real_tasks():
    plan = ProjectPlan.model_validate_json((DEMO / "plan.json").read_text(encoding="utf-8"))
    ids = {t.id for t in plan.tasks}
    for line in (DEMO / "messages.jsonl").read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        gold = row.pop("gold")
        InboundMessage.model_validate(row)
        assert gold["task_id"] is None or gold["task_id"] in ids
