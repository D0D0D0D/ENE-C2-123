"""The startup hook is exercised because normal graph tests do not run it."""

from fastapi.testclient import TestClient

from src.api.server import app


def test_startup_compiles_agent_and_health_is_available():
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_invoke_returns_a_card_for_each_exception(monkeypatch):
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "mock-entry-token")
    monkeypatch.setenv("STG_DEFAULT_BUSINESS_UNIT_ID", "unit-a")
    payload = {"exceptions": [
        {"exception_id": "EX-1", "exception_category": "missing", "series_id": "S1", "detected_at": "2026-01-01T00:00:00+00:00", "metrics": {}},
        {"exception_id": "EX-2", "exception_category": "missing", "series_id": "S2", "detected_at": "2026-01-01T00:00:00+00:00", "metrics": {}},
    ]}
    with TestClient(app) as client:
        response = client.post("/invoke", json=payload, headers={"authorization": "Bearer mock-entry-token"})
    assert response.status_code == 200
    assert len(response.json()["output"]["cards"]) == 2


def test_invoke_mixed_batch_returns_partial_and_explicit_rejection(monkeypatch):
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "mock-entry-token")
    monkeypatch.setenv("STG_DEFAULT_BUSINESS_UNIT_ID", "unit-a")
    payload = {"exceptions": [
        {"exception_id": "EX-OK", "exception_category": "missing", "series_id": "S1", "detected_at": "2026-01-01T00:00:00+00:00", "metrics": {}},
        {"exception_id": " ", "exception_category": "missing", "series_id": "S2", "detected_at": "2026-01-01T00:00:00+00:00", "metrics": {}},
    ]}
    with TestClient(app) as client:
        response = client.post("/invoke", json=payload, headers={"authorization": "Bearer mock-entry-token"})
    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "partial"
    assert len(body["output"]["cards"]) == 1
    assert body["output"]["rejected"] == [{"exception_id": " ", "reason_code": "intake_rejected"}]
    assert "exception_skipped" not in str(body["output"]["degradation_reason"])
