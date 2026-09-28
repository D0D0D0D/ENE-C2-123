"""Integration coverage for the deterministic/LLM routing boundary."""

from __future__ import annotations

import json

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.graph.graph import SupplyDataQualityPrecedentGraph


def _record(category: str, series: str, verdict: str, decided_at: str) -> dict:
    return {
        "record_kind": "adjudication",
        "classification": {"exception_category": category},
        "series_id": series,
        "operational_note": "A sufficiently detailed operational note.",
        "occurred_at": "2026-01-01T00:00:00+00:00",
        "valid_to": None,
        "verdict_code": verdict,
        "rationale": "A sufficiently detailed rationale for a prior decision.",
        "decided_by": "reviewer",
        "decided_at": decided_at,
        "decision_source": "human",
        "retrieval_text": f"{category} {series}",
        "provenance": {},
    }


def _invoke(graph: SupplyDataQualityPrecedentGraph) -> dict:
    body = {
        "schema": "ene-c2-123/v1",
        "mode": "invoke",
        "business_unit_id": "unit-a",
        "customer_ref": "customer-a",
        "payload": {
            "exceptions": [{"exception_id": "EX-1", "exception_category": "missing", "series_id": "SERIES-A", "detected_at": "2026-02-01T00:00:00+00:00", "metrics": {}}],
            "revision_notices": [],
        },
    }
    return graph.invoke(
        json.dumps(body),
        ctx=InvocationContext(session_id="workflow-session", caller_id="reviewer", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL),
    )


def test_conflict_is_evaluated_before_top_k_and_minor_verdict_is_retained():
    graph = SupplyDataQualityPrecedentGraph({"candidate_top_k": 2})
    ledger = graph.config["services"].ledger
    scope = {"business_unit_id": "unit-a"}
    ledger.write(scope, _record("missing", "SERIES-A", "accepted", "2026-01-03T00:00:00+00:00"))
    ledger.write(scope, _record("missing", "SERIES-A", "accepted", "2026-01-04T00:00:00+00:00"))
    minority = ledger.write(scope, _record("missing", "SERIES-A", "rejected", "2026-01-02T00:00:00+00:00"))

    result = _invoke(graph)

    assert result["status"] == "success"
    card = result["output"]["cards"][0]
    assert card["eligibility"]["kind"] == "conflicting"
    assert minority["record_id"] in {fact["adjudication_id"] for fact in card["precedent_facts"]}
    assert card["degradation_reason"] == "llm_unavailable"


def test_uniquely_determined_route_does_not_need_an_llm():
    graph = SupplyDataQualityPrecedentGraph()
    graph.config["services"].ledger.write(
        {"business_unit_id": "unit-a"}, _record("missing", "SERIES-A", "accepted", "2026-01-03T00:00:00+00:00")
    )

    result = _invoke(graph)

    assert result["status"] == "success"
    card = result["output"]["cards"][0]
    assert card["card_kind"] == "deterministic_display"
    assert card["claims"] == []
