"""HTTP regression tests: refs must become production evidence IDs."""

from __future__ import annotations

from fastapi.testclient import TestClient

from src.api.server import app
from src.nodes import pre_process_node


def _headers(monkeypatch):
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "mock-entry-token")
    monkeypatch.setenv("STG_DEFAULT_BUSINESS_UNIT_ID", "unit-registry")
    return {"authorization": "Bearer mock-entry-token"}


def test_http_invoke_generates_note_and_matching_revision_evidence(monkeypatch):
    captured = []
    original = pre_process_node._build_registry

    def spy(*args, **kwargs):
        value = original(*args, **kwargs)
        captured.extend(value)
        return value

    monkeypatch.setattr(pre_process_node, "_build_registry", spy)
    payload = {"exceptions": [{"exception_id": "EX-HTTP", "exception_category": "missing", "series_id": "S-HTTP", "detected_at": "2026-01-01T00:00:00+00:00", "metrics": {}, "operational_note": "Current operational note contains enough evidence text."}], "revision_notices": [{"notice_id": "REV-HTTP", "series_id": "S-HTTP", "publisher": "publisher", "effective_date": "2026-01-01T00:00:00+00:00", "body": "Matching revision notice contains enough evidence text."}]}
    with TestClient(app) as client:
        response = client.post("/invoke", json=payload, headers=_headers(monkeypatch))
    assert response.status_code == 200
    card = response.json()["output"]["cards"][0]
    assert "REQ-OPERATIONAL-NOTE-ABSENT" not in {item["requirement_id"] for item in card["missing_information"]}
    assert any(item["evidence_id"].startswith("EV-NOTE-EX-HTTP-") for item in captured)
    assert any(item["evidence_id"].startswith("EV-REV-REV-HTTP-") for item in captured)


def test_http_invoke_excludes_mismatched_revision_series(monkeypatch):
    captured = []
    original = pre_process_node._build_registry

    def spy(*args, **kwargs):
        value = original(*args, **kwargs)
        captured.extend(value)
        return value

    monkeypatch.setattr(pre_process_node, "_build_registry", spy)
    payload = {"exceptions": [{"exception_id": "EX-NOTE", "exception_category": "missing", "series_id": "S-ONE", "detected_at": "2026-01-01T00:00:00+00:00", "metrics": {}, "operational_note": "Current operational note contains enough evidence text."}], "revision_notices": [{"notice_id": "REV-OTHER", "series_id": "S-OTHER", "publisher": "publisher", "effective_date": "2026-01-01T00:00:00+00:00", "body": "Unrelated revision notice contains enough evidence text."}]}
    with TestClient(app) as client:
        response = client.post("/invoke", json=payload, headers=_headers(monkeypatch))
    assert response.status_code == 200
    assert not any(item["evidence_id"].startswith("EV-REV-") for item in captured)


def test_http_invoke_marks_operational_note_requirement_only_when_absent(monkeypatch):
    payload = {"exceptions": [{"exception_id": "EX-ABSENT", "exception_category": "missing", "series_id": "S-ABSENT", "detected_at": "2026-01-01T00:00:00+00:00", "metrics": {}}]}
    with TestClient(app) as client:
        response = client.post("/invoke", json=payload, headers=_headers(monkeypatch))
    assert response.status_code == 200
    requirements = {item["requirement_id"] for item in response.json()["output"]["cards"][0]["missing_information"]}
    assert "REQ-OPERATIONAL-NOTE-ABSENT" in requirements
