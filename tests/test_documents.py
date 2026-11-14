from datetime import date

import pytest
from fastapi.testclient import TestClient

from kraftsync.app import documents as documents_module
from kraftsync.app.api import create_app
from kraftsync.app.settings import Settings

TOKEN = "d" * 32
HEADERS = {"X-KraftSync-Token": TOKEN}


@pytest.fixture
def settings(tmp_path):
    return Settings(token=TOKEN, events_path=tmp_path / "events.jsonl",
                    documents_path=tmp_path / "documents", demo_today=date(2026, 11, 9))


@pytest.fixture
def client(settings):
    return TestClient(create_app(settings))


def upload(client, name, content, category="drawings_plans", **fields):
    return client.post("/documents", headers=HEADERS, data={"category": category, **fields},
                       files={"file": (name, content, "application/octet-stream")})


def test_document_upload_download_and_persistence(client, settings):
    assert client.get("/documents").status_code == 401
    result = upload(client, "plan.pdf", b"%PDF-1.7 plan", revision_label="Rev 1", location_ref="T6")
    assert result.status_code == 201
    saved = result.json()
    assert saved["original_filename"] == "plan.pdf"
    assert saved["revision_label"] == "Rev 1" and saved["location_ref"] == "T6"

    downloaded = client.get(f"/documents/{saved['id']}/content", headers=HEADERS)
    assert downloaded.status_code == 200 and downloaded.content == b"%PDF-1.7 plan"
    assert "attachment" in downloaded.headers["content-disposition"]
    preview = client.get(f"/documents/{saved['id']}/content?inline=true", headers=HEADERS)
    assert preview.status_code == 200 and "inline" in preview.headers["content-disposition"]

    restarted = TestClient(create_app(settings))
    assert restarted.get("/documents", headers=HEADERS).json() == [saved]
    assert upload(restarted, "plan.pdf", b"%PDF-1.7 revision 2", revision_label="Rev 2").status_code == 201
    assert len(restarted.get("/documents", headers=HEADERS).json()) == 2


def test_ifc_is_stored_and_invalid_content_is_rejected(client):
    model = upload(client, "building.ifc", b"ISO-10303-21;\nDATA;\nENDSEC;\nEND-ISO-10303-21;",
                   category="bim_models")
    assert model.status_code == 201
    stored = client.get(f"/documents/{model.json()['id']}/content?inline=true", headers=HEADERS)
    assert stored.status_code == 200 and stored.content.startswith(b"ISO-10303-21;")
    assert "inline" in stored.headers["content-disposition"]
    assert upload(client, "bad.pdf", b"not a PDF").status_code == 415
    assert upload(client, "bad.ifc", b"ISO-10303-21;", category="drawings_plans").status_code == 415


def test_document_upload_size_limit(client, monkeypatch):
    monkeypatch.setattr(documents_module, "MAX_DOCUMENT_BYTES", 8)
    assert upload(client, "large.pdf", b"%PDF-1.7 too large").status_code == 413
