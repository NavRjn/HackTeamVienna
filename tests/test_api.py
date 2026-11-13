import json
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


@pytest.fixture
def client(tmp_path, hooks):
    s = Settings(token=TOKEN, events_path=tmp_path / "events.jsonl", webhook_url="http://n8n.invalid/hook",
                 demo_today=date(2026, 11, 9))
    return TestClient(create_app(s))


def gold_messages():
    return [json.loads(line) for line in (DEMO / "messages.jsonl").read_text(encoding="utf-8").splitlines()]


def body(m, confidence=0.95):
    g = m["gold"]
    update = {
        "message_id": m["id"], "task_id": g["task_id"], "kind": g["kind"], "confidence": confidence,
        "reported_on": "2026-11-09", "new_forecast_finish": g.get("new_forecast_finish"),
        "blocker": g.get("blocker"), "change_description": g.get("change_description"),
        "rationale": "test", "model": "gold",
    }
    return {"sender_ref": m["sender_ref"], "text": m["text"], "received_at": m["received_at"], "update": update}


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


def test_context_returns_senders_candidates(client):
    r = client.post("/context", json={"sender_ref": "+436600000002"}, headers=H).json()
    assert r["person"]["id"] == "P-DRY"
    ids = [t["id"] for t in r["candidates"]]
    assert "T6-BOARD" in ids and len(ids) <= 15
    assert client.post("/context", json={"sender_ref": "+43999"}, headers=H).json()["person"] is None


def test_unknown_task_id_is_rejected(client):
    m = gold_messages()[0]
    b = body(m)
    b["update"]["task_id"] = "T6-INVENTED"
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
    s = Settings(token=TOKEN, events_path=tmp_path / "events.jsonl", demo_today=date(2026, 11, 9))
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
