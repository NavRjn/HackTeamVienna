import json
import logging
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kraftsync.app import api as api_module
from kraftsync.app.api import create_app
from kraftsync.app.settings import Settings, SettingsError

DEMO = Path(__file__).resolve().parents[1] / "data" / "demo"
TOKEN = "t" * 32
H = {"X-KraftSync-Token": TOKEN}


@pytest.fixture
def hooks(monkeypatch):
    calls = []
    monkeypatch.setattr(api_module, "post_hook", lambda url, token, hook: calls.append(hook))
    return calls


PLAN = json.loads((DEMO / "plan.json").read_text(encoding="utf-8"))
TG = {p["id"]: 7000 + i for i, p in enumerate(PLAN["persons"])}   # person id -> Telegram user id (test values)
UNKNOWN_TG = 999_999_999


def settings(tmp_path, **kw):
    """Demo plan with Telegram ids filled in (the real plan gets them hardcoded by the team)."""
    plan = json.loads(json.dumps(PLAN))
    for person in plan["persons"]:
        person["telegram_chat_id"] = TG[person["id"]]
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    return Settings(token=TOKEN, plan_path=plan_path, events_path=tmp_path / "events.jsonl", **kw)


@pytest.fixture
def client(tmp_path, hooks):
    s = settings(tmp_path, webhook_url="http://n8n.invalid/hook", demo_today=date(2026, 11, 9))
    return TestClient(create_app(s))


def gold_messages():
    return [json.loads(line) for line in (DEMO / "messages.jsonl").read_text(encoding="utf-8").splitlines()]


def claim(m, confidence=0.95, suffix=""):
    g = m["gold"]
    return {
        "message_id": m["id"] + suffix, "task_id": g["task_id"], "kind": g["kind"], "confidence": confidence,
        "reported_on": "2026-11-09", "new_forecast_finish": g.get("new_forecast_finish"),
        "blocker": g.get("blocker"), "change_description": g.get("change_description"),
        "rationale": "test", "model": "gold",
    }


def body(m, confidence=0.95):
    sender_id = TG.get(m["gold"]["sender_person_id"], UNKNOWN_TG)
    return {"sender_id": sender_id, "text": m["text"], "received_at": m["received_at"],
            "updates": [claim(m, confidence)]}


def test_settings_refuse_weak_token():
    with pytest.raises(SettingsError):
        Settings(token="short").validate()
    with pytest.raises(SettingsError):
        Settings(token="").validate()


def test_auth_required_except_health(client):
    assert client.get("/health").status_code == 200
    assert client.get("/state").status_code == 401
    assert client.get("/state", headers={"X-KraftSync-Token": "x" * 32}).status_code == 401
    assert client.post("/reset").status_code == 401
    assert client.get("/state", headers=H).status_code == 200
    assert client.get("/docs").status_code == 404   # docs off by default


def ctx(client, person_id, **kw):
    return client.post("/context", json={"sender_id": TG[person_id], **kw}, headers=H).json()


def own(task, person):
    return task["responsible_person_id"] == person["id"] or (person["trade"] and task["trade"] == person["trade"])


def test_context_tasks_are_only_the_senders_own_and_active_around_today(client):
    r = ctx(client, "P-ELE", received_at="2026-11-09T07:42:00+01:00")
    assert r["person"]["id"] == "P-ELE" and r["today"] == "2026-11-09"
    ids = {t["id"] for t in r["tasks"]}
    assert all(own(t, r["person"]) for t in r["tasks"])
    assert {"T6-ELEC-RI", "T5-ELEC-RI"} <= ids          # in progress now / finished 2 days ago
    assert not {"T5-ELEC-FF", "T6-ELEC-FF"} & ids       # own, but 8 and 11 days out: outside +-3 days
    related = {t["id"] for t in r["related_tasks"]}
    assert not ids & related                             # the two lists never overlap
    assert not any(own(t, r["person"]) for t in r["related_tasks"])
    assert "T6-INSP" in related                          # direct successor of the electrician's task
    assert len(r["tasks"]) <= 15 and len(r["related_tasks"]) <= 15


def test_context_window_is_three_days(client):
    # drywaller: T6-BOARD is planned 12 Nov: inside the window on 9 Nov (+3), outside on 8 Nov (+4)
    on_9 = ctx(client, "P-DRY", received_at="2026-11-09T09:00:00+01:00")
    assert "T6-BOARD" in {t["id"] for t in on_9["tasks"]}
    # (DEMO_TODAY pins "today"; check the window rule on the state helper directly)
    from datetime import date as d
    from kraftsync.app.state import _active_around
    from kraftsync.contracts import Task
    board = Task.model_validate(next(t for t in PLAN["tasks"] if t["id"] == "T6-BOARD"))
    assert _active_around(board, d(2026, 11, 9)) and not _active_around(board, d(2026, 11, 8))


def test_context_adds_tasks_at_locations_named_in_the_message_to_related_only(client):
    plain = ctx(client, "P-SM")
    named = ctx(client, "P-SM", text="Wasserschaden im Bad Top 6")
    wanted = {"T6-PLUMB-RI", "T6-WATERPROOF", "T6-TILE", "T6-SAN-FF"}
    assert wanted <= {t["id"] for t in named["related_tasks"]}
    assert not wanted <= {t["id"] for t in plain["related_tasks"]}
    assert not wanted & {t["id"] for t in named["tasks"]}   # never in the sender's own list
    assert len(named["related_tasks"]) <= 15


def test_sender_is_identified_by_telegram_id_only(client):
    assert ctx(client, "P-DRY")["person"]["id"] == "P-DRY"
    as_text = client.post("/context", json={"sender_id": str(TG["P-DRY"])}, headers=H).json()   # n8n may send a string
    assert as_text["person"]["id"] == "P-DRY"
    unknown = client.post("/context", json={"sender_id": UNKNOWN_TG}, headers=H).json()
    assert unknown["person"] is None and unknown["tasks"] == [] and unknown["related_tasks"] == []   # nothing leaks
    assert client.post("/context", json={"sender_name": "Tomasz Nowak"}, headers=H).status_code == 422   # old key


def test_today_comes_from_message_date_unless_demo_today_is_set(tmp_path, hooks):
    (tmp_path / "a").mkdir()
    c = TestClient(create_app(settings(tmp_path / "a")))   # no DEMO_TODAY
    r = c.post("/context", json={"sender_id": TG["P-SM"], "received_at": "2026-11-20T08:00:00+01:00"}, headers=H)
    assert r.json()["today"] == "2026-11-20"
    (tmp_path / "b").mkdir()
    c2 = TestClient(create_app(settings(tmp_path / "b", demo_today=date(2026, 11, 9))))
    r = c2.post("/context", json={"sender_id": TG["P-SM"], "received_at": "2026-11-20T08:00:00+01:00"}, headers=H)
    assert r.json()["today"] == "2026-11-09"


def test_one_message_can_carry_several_claims(client, hooks):
    ms = {m["id"]: m for m in gold_messages()}
    b = body(ms["demo:02"])   # plumber: delay on T6-PLUMB-RI ...
    b["updates"].append({**claim(ms["demo:02"], suffix="#2"), "task_id": "T5-PLUMB-RI", "kind": "progress",
                         "new_forecast_finish": None})   # ... plus a harmless claim about another task
    r = client.post("/updates", json=b, headers=H).json()
    assert [x["status"] for x in r["results"]] == ["applied", "applied"]
    assert [d["type"] for d in r["discrepancies"]] == ["delay"]
    assert {a["to_person_id"] for a in r["actions"]} == {"P-INS", "P-DRY", "P-SM"}
    assert len(hooks) == 1
    # claims need distinct ids
    b["updates"][1]["message_id"] = b["updates"][0]["message_id"]
    assert client.post("/updates", json=b, headers=H).status_code == 422


def test_unknown_task_id_is_rejected(client):
    m = gold_messages()[0]
    b = body(m)
    b["updates"][0]["task_id"] = "T6-INVENTED"
    assert client.post("/updates", json=b, headers=H).status_code == 422


def test_demo_replay_matches_gold(client, hooks):
    state_before = client.get("/state", headers=H).json()
    for m in gold_messages():
        confidence = 0.4 if m["gold"]["task_id"] is None else 0.95
        r = client.post("/updates", json=body(m, confidence), headers=H)
        assert r.status_code == 200, (m["id"], r.text)
        found = {d["type"] for d in r.json()["discrepancies"]}
        assert found == set(m["gold"]["expected_discrepancies"]), m["id"]

    # the ripple moved the handover date
    state = client.get("/state", headers=H).json()
    assert state["project_finish_forecast"] > state_before["project_finish_forecast"]
    assert len(state["open_discrepancies"]) == 5   # delay, scope, oos, hold, delay


def test_ripple_notifies_affected_trades_and_escalates(client, hooks):
    ms = {m["id"]: m for m in gold_messages()}
    r = client.post("/updates", json=body(ms["demo:02"]), headers=H).json()
    by_person = {a["to_person_id"]: a["kind"] for a in r["actions"]}
    assert by_person == {"P-INS": "notify", "P-DRY": "notify", "P-SM": "escalate"}
    assert "P-SAN" not in by_person   # the sender is not told about their own delay
    d = r["discrepancies"][0]
    assert d["severity"] == "critical" and "T6-BOARD" in d["impact"]["affected_task_ids"]
    assert d["impact"]["project_finish_after"] > d["impact"]["project_finish_before"]
    assert len(hooks) == 1 and hooks[0].message_id == "demo:02"


def test_hold_point_violation_escalates_immediately(client, hooks):
    ms = {m["id"]: m for m in gold_messages()}
    r = client.post("/updates", json=body(ms["demo:05"]), headers=H).json()
    types = {d["type"]: d["severity"] for d in r["discrepancies"]}
    assert types["hold_point_violation"] == "critical"
    by_person = {a["to_person_id"]: a["kind"] for a in r["actions"]}
    assert by_person["P-SM"] == "escalate" and by_person["P-INS"] == "escalate" and by_person["P-DRY"] == "ask"
    assert len(r["actions"]) == 3   # one action per person


def test_harmless_update_does_not_call_hook(client, hooks):
    ms = {m["id"]: m for m in gold_messages()}
    r = client.post("/updates", json=body(ms["demo:01"]), headers=H).json()
    assert r["status"] == "applied" and r["discrepancies"] == [] and r["actions"] == []
    assert hooks == []


def test_low_confidence_asks_instead_of_applying(client, hooks):
    ms = {m["id"]: m for m in gold_messages()}
    r = client.post("/updates", json=body(ms["demo:01"], confidence=0.3), headers=H).json()
    assert r["status"] == "needs_clarification"
    assert r["actions"][0]["kind"] == "ask" and r["actions"][0]["to_person_id"] == "P-ELE"
    task = next(t for t in client.get("/state", headers=H).json()["tasks"] if t["id"] == "T6-ELEC-RI")
    assert task["status"] == "in_progress"   # not applied
    assert len(hooks) == 1


def test_duplicate_message_is_not_applied_twice(client, hooks):
    ms = {m["id"]: m for m in gold_messages()}
    client.post("/updates", json=body(ms["demo:02"]), headers=H)
    r = client.post("/updates", json=body(ms["demo:02"]), headers=H).json()
    assert r["status"] == "duplicate" and r["actions"] == []
    assert len(hooks) == 1


def test_state_survives_restart_and_reset(tmp_path, hooks):
    s = settings(tmp_path, demo_today=date(2026, 11, 9))
    c1 = TestClient(create_app(s))
    ms = {m["id"]: m for m in gold_messages()}
    c1.post("/updates", json=body(ms["demo:02"]), headers=H)
    c2 = TestClient(create_app(s))   # replays events.jsonl
    s2 = c2.get("/state", headers=H).json()
    assert next(t for t in s2["tasks"] if t["id"] == "T6-PLUMB-RI")["forecast_finish"] == "2026-11-13"
    assert len(s2["open_discrepancies"]) == 1
    c2.post("/reset", headers=H)
    s3 = c2.get("/state", headers=H).json()
    assert next(t for t in s3["tasks"] if t["id"] == "T6-PLUMB-RI")["forecast_finish"] is None
    assert c2.get("/events", headers=H).json() == []


def test_same_delay_reported_again_does_not_notify_again(client, hooks):
    ms = {m["id"]: m for m in gold_messages()}
    first = client.post("/updates", json=body(ms["demo:02"]), headers=H).json()
    assert first["actions"]
    again = body(ms["demo:02"])
    again["updates"][0]["message_id"] = "demo:02-repeat"   # a new message with the same news
    r = client.post("/updates", json=again, headers=H).json()
    assert r["status"] == "applied" and r["discrepancies"] == [] and r["actions"] == []
    assert len(hooks) == 1


def test_own_task_outside_the_window_appears_in_related_only_when_its_location_is_named(client):
    # tiler asks about waterproofing Top 5 on 9 Nov; T5-WATERPROOF is planned 27 Nov, far outside +-3 days
    plain = ctx(client, "P-TIL", received_at="2026-11-09T14:02:00+01:00")
    named = ctx(client, "P-TIL", received_at="2026-11-09T14:02:00+01:00",
                text="Hi, can I start waterproofing in Top 5 bathroom next Monday?")
    assert "T5-WATERPROOF" not in {t["id"] for t in plain["tasks"] + plain["related_tasks"]}
    assert "T5-WATERPROOF" in {t["id"] for t in named["related_tasks"]}
    assert "T5-WATERPROOF" not in {t["id"] for t in named["tasks"]}


# ---- persons.csv telegram ids + call log ---------------------------------------------------------

def _plan_without_ids(tmp_path):
    (tmp_path / "plan.json").write_text(json.dumps(PLAN), encoding="utf-8")   # all telegram_chat_id are null
    return Settings(token=TOKEN, plan_path=tmp_path / "plan.json", events_path=tmp_path / "events.jsonl",
                    demo_today=date(2026, 11, 9))


def test_telegram_ids_typed_into_persons_csv_are_used(tmp_path, hooks):
    (tmp_path / "persons.csv").write_text(
        "id,name,telegram_chat_id\n"
        "P-ELE,Marko Petrovic,1125811575\n"      # works
        'P-INS,Klaus Berger,""\n'                # literal empty quotes: ignored
        "P-DRY,Tomasz Nowak,\n"                  # blank: ignored
        "P-SCR,Stefan Gruber,not-a-number\n"     # invalid: ignored, no crash
        "P-NOBODY,Ghost,42\n",                   # not in the plan: ignored
        encoding="utf-8")
    c = TestClient(create_app(_plan_without_ids(tmp_path)))
    r = c.post("/context", json={"sender_id": 1125811575}, headers=H).json()
    assert r["person"]["id"] == "P-ELE" and r["person"]["telegram_chat_id"] == 1125811575
    assert c.post("/context", json={"sender_id": 42}, headers=H).json()["person"] is None


def test_without_ids_everyone_is_unknown(tmp_path, hooks):
    c = TestClient(create_app(_plan_without_ids(tmp_path)))   # no persons.csv, plan.json has no ids
    assert c.post("/context", json={"sender_id": 1125811575}, headers=H).json()["person"] is None


class _Lines(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


@pytest.fixture
def log_lines(client):
    h = _Lines()
    logging.getLogger("kraftsync").addHandler(h)
    yield h.lines
    logging.getLogger("kraftsync").removeHandler(h)


def test_every_call_is_logged_compactly_without_secrets_or_text(client, log_lines):
    ms = {m["id"]: m for m in gold_messages()}
    client.post("/context", json={"sender_id": TG["P-SAN"], "text": "GEHEIMER-TEXT-1"}, headers=H)
    client.post("/context", json={"sender_id": UNKNOWN_TG}, headers=H)
    client.post("/updates", json=body(ms["demo:02"]), headers=H)
    client.get("/state", headers={"X-KraftSync-Token": "wrong-token-value-wrong-token-value"})
    client.post("/context", json={"sender_name": "x"}, headers=H)   # invalid body
    client.get("/health")
    client.get("/state", headers=H)   # polling: not logged
    text = "\n".join(log_lines)
    assert f"sender={TG['P-SAN']} -> P-SAN" in text and "tasks=" in text
    assert f"sender={UNKNOWN_TG} -> UNKNOWN" in text
    assert "demo:02 delayed T6-PLUMB-RI" in text and "-> applied [delay]" in text and "actions=3" in text
    assert "GET /state 401" in text and "invalid or missing token" in text
    assert "POST /context 422" in text and "invalid request" in text and "sender_id" in text
    assert "/health" not in text and "GET /state 200" not in text
    assert TOKEN not in text and "wrong-token-value" not in text
    assert "GEHEIMER-TEXT-1" not in text and ms["demo:02"]["text"] not in text
