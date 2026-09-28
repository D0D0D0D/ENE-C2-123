"""Design section 5-1 step 3: the validity window is an *instant* comparison.

Comparing the raw ISO strings is wrong.  ``2026-07-30T09:00:00+09:00`` sorts
*after* ``2026-07-30T06:00:00+00:00`` lexicographically while being three hours
*earlier* in real time, so a precedent that was in force would be dropped from
the candidate set — silently corrupting the conflict predicate that the whole
agent is built on.  These tests fail if the comparison regresses to strings.
"""

from __future__ import annotations

import json

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.graph.graph import SupplyDataQualityPrecedentGraph


def _record(verdict: str, decided_at: str) -> dict:
    return {
        "record_kind": "adjudication",
        "classification": {"exception_category": "missing"},
        "series_id": "SERIES-TZ",
        "operational_note": "A sufficiently detailed operational note.",
        "occurred_at": "2026-01-01T00:00:00+00:00",
        # In force until 06:00 UTC on the day the exception is detected.
        "valid_to": "2026-07-30T06:00:00+00:00",
        "verdict_code": verdict,
        "rationale": "A sufficiently detailed rationale for a prior decision.",
        "decided_by": "reviewer",
        "decided_at": decided_at,
        "decision_source": "human",
        "retrieval_text": "missing SERIES-TZ",
        "provenance": {},
    }


def _invoke(graph: SupplyDataQualityPrecedentGraph, detected_at: str) -> dict:
    body = {
        "schema": "ene-c2-123/v1",
        "mode": "invoke",
        "business_unit_id": "unit-a",
        "customer_ref": "customer-a",
        "payload": {
            "exceptions": [
                {
                    "exception_id": "EX-TZ",
                    "exception_category": "missing",
                    "series_id": "SERIES-TZ",
                    "detected_at": detected_at,
                    "metrics": {},
                }
            ],
            "revision_notices": [],
        },
    }
    return graph.invoke(
        json.dumps(body),
        ctx=InvocationContext(
            session_id="tz-session",
            caller_id="reviewer",
            caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
        ),
    )


def _graph_with_conflicting_precedents() -> SupplyDataQualityPrecedentGraph:
    graph = SupplyDataQualityPrecedentGraph()
    graph.compile()
    ledger = graph.config["services"].ledger
    scope = {"business_unit_id": "unit-a"}
    ledger.write(scope, _record("accepted", "2026-01-02T00:00:00+00:00"))
    ledger.write(scope, _record("rejected", "2026-01-03T00:00:00+00:00"))
    return graph


def test_offset_timestamp_is_compared_as_an_instant_not_a_string():
    """+09:00 at 09:00 is 00:00 UTC — inside a window that closes at 06:00 UTC.

    A lexicographic comparison puts "09:00:00+09:00" after "06:00:00+00:00" and
    drops both precedents, collapsing the card to ``no_precedent``.
    """

    graph = _graph_with_conflicting_precedents()
    card = _invoke(graph, "2026-07-30T09:00:00+09:00")["output"]["cards"][0]

    assert card["eligibility"]["kind"] == "conflicting"
    assert card["eligibility"]["candidate_count"] == 2
    assert sorted(card["eligibility"]["distinct_verdict_codes"]) == ["accepted", "rejected"]


def test_zulu_and_offset_spellings_of_the_same_instant_agree():
    """``Z`` and ``+00:00`` denote the same instant and must select the same set."""

    graph = _graph_with_conflicting_precedents()
    zulu = _invoke(graph, "2026-07-30T00:00:00Z")["output"]["cards"][0]["eligibility"]
    offset = _invoke(graph, "2026-07-30T00:00:00+00:00")["output"]["cards"][0]["eligibility"]

    assert zulu == offset
    assert zulu["kind"] == "conflicting"


def test_precedent_that_expired_before_detection_is_excluded():
    """The window must still exclude — the fix must not simply accept everything."""

    graph = _graph_with_conflicting_precedents()
    card = _invoke(graph, "2026-07-30T12:00:00+00:00")["output"]["cards"][0]

    assert card["eligibility"]["kind"] == "no_precedent"
    assert card["eligibility"]["candidate_count"] == 0


def test_ambiguous_or_unparseable_detection_timestamp_is_rejected():
    """Naive, date-only and garbage timestamps are ambiguous — fail closed."""

    graph = _graph_with_conflicting_precedents()
    for detected_at in ("not-a-timestamp", "2026-07-30", "2026-07-30T00:00:00"):
        result = _invoke(graph, detected_at)
        assert result.get("status") == "error", detected_at
