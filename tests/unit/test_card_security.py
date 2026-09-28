"""S-3 closed-allowlist and deterministic-priority checks."""

from __future__ import annotations

import pytest

from framework.errors import SecurityViolationError

from src.nodes.post_process_node import CardAssemblyNode


def _result() -> dict:
    return {
        "status": "success",
        "formatted_output": {
            "mode": "invoke",
            "cards": [{
            "mode": "invoke",
            "card_kind": "reconciled",
            "exception_ref": {"exception_id": "EX-1", "exception_category": "missing", "series_id": "S1", "detected_at": "2026-01-01T00:00:00+00:00", "metrics": {}},
            "eligibility": {"kind": "conflicting", "distinct_verdict_codes": ["accepted", "rejected"], "candidate_count": 2},
            "precedent_facts": [],
            "claims": [],
            "missing_information": [],
            "confirmation_questions": [],
            "review_priority": {"level": "medium", "basis": {"distinct_verdict_count": 2, "candidate_count": 2, "missing_information_count": 0}},
            "degradation_reason": None,
            }],
            "degradation_reason": None,
            "_gate_verification": {"mode": "invoke", "evidence_ids": [], "requirement_ids": [], "distinct_verdict_count": 2, "candidate_count": 2, "candidate_ids": ["P1", "P2"], "truncated": False, "numeric_facts": []},
        },
    }


def test_s3_rejects_nested_undeclared_current_decision_field():
    result = _result()
    result["formatted_output"]["cards"][0]["claims"] = [{"claim_id": "C1", "precedent_id": "P1", "support_status": "unknown", "statement": "Evidence relationship is unclear.", "evidence_ids": [], "decision": "injected"}]
    with pytest.raises(SecurityViolationError):
        CardAssemblyNode()._extra_security_gate_output(result)


def test_s3_rejects_missing_verification_block():
    result = _result()
    result["formatted_output"].pop("_gate_verification")
    with pytest.raises(SecurityViolationError):
        CardAssemblyNode()._extra_security_gate_output(result)


def test_s3_rejects_claim_precedent_id_outside_scoped_candidate_set():
    result = _result()
    result["formatted_output"]["_gate_verification"]["evidence_ids"] = ["EV-ADJ-P1-S1"]
    result["formatted_output"]["cards"][0]["claims"] = [
        {
            "claim_id": "C1",
            "precedent_id": "OTHER-SCOPE-ID",
            "support_status": "unknown",
            "statement": "Evidence relationship is unclear.",
            "evidence_ids": ["EV-ADJ-P1-S1"],
        }
    ]

    with pytest.raises(SecurityViolationError):
        CardAssemblyNode()._extra_security_gate_output(result)
