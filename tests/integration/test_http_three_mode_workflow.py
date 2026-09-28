"""HTTP core scenario: human precedents are ingested, recalled, then fed back."""

from __future__ import annotations

from fastapi.testclient import TestClient
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.api import server


def _auth_seam(request, session_id, operation):
    request.state.caller_id = "reviewer-http"
    return {"business_unit_id": "unit-workflow"}, InvocationContext(session_id=session_id or "workflow-session", caller_id="reviewer-http", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)


def _adjudication(verdict, rationale):
    return {"exception_category": "missing", "series_id": "S-WORK", "occurred_at": "2025-01-01T00:00:00+00:00", "valid_to": None, "verdict_code": verdict, "decided_at": "2025-01-02T00:00:00+00:00", "decision_source": "human", "rationale": rationale, "operational_note": "Operational note contains enough text for the quality gate."}


def test_http_ingest_invoke_feedback_core_workflow(monkeypatch):
    monkeypatch.setattr(server, "_scope_and_context", _auth_seam)
    with TestClient(server.app) as client:
        ingest = client.post("/ingest", json={"adjudications": [_adjudication("accepted", "Accepted rationale has sufficient text."), _adjudication("rejected", "Rejected rationale has sufficient text.")]})
        assert ingest.status_code == 200
        assert len(ingest.json()["output"]["ingest_summary"]["written_ids"]) == 2
        invoke = client.post("/invoke", json={"exceptions": [{"exception_id": "EX-WORK", "exception_category": "missing", "series_id": "S-WORK", "detected_at": "2026-01-01T00:00:00+00:00", "metrics": {}, "operational_note": "Current note contains enough text for reconciliation."}]})
        assert invoke.status_code == 200
        card = invoke.json()["output"]["cards"][0]
        assert card["eligibility"]["kind"] == "conflicting"
        assert {fact["verdict_code"] for fact in card["precedent_facts"]} == {"accepted", "rejected"}
        feedback = client.post("/feedback", json={"kind": "adjudication", "exception_id": "EX-WORK", "exception_category": "missing", "series_id": "S-WORK", "detected_at": "2026-01-01T00:00:00+00:00", "confirmed_verdict_code": "accepted", "rationale": "Human confirmed rationale has sufficient text.", "decided_at": "2026-01-03T00:00:00+00:00"})
    assert feedback.status_code == 200
    assert feedback.json()["output"]["feedback_summary"]["kind"] == "adjudication"


def test_http_ingest_invalid_category_returns_4xx_rejected_summary(monkeypatch):
    monkeypatch.setattr(server, "_scope_and_context", _auth_seam)
    with TestClient(server.app) as client:
        response = client.post("/ingest", json={"adjudications": [_adjudication("accepted", "Valid length rationale.") | {"exception_category": "unconfigured"}]})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ingest_row_rejected"
    assert response.json()["ingest_summary"]["rejected"][0]["reason_code"] == "ledger_classification_invalid"
