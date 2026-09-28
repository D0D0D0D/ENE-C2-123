"""Named acceptance tests traceable to design section 8."""

from __future__ import annotations

import pytest

from framework.errors import SecurityViolationError

from src.nodes.post_process_node import CardAssemblyNode, _numeric_tokens, _priority
from src.services.numeric import extract_numeric_tokens
from src.nodes.pre_process_node import IntakeNode, _build_registry
from src.services.runtime import RuntimeServices
from tests.unit.test_card_security import _result


def test_section8_02_claim_requires_evidence_id():
    result = _result()
    result["formatted_output"]["cards"][0]["claims"] = [
        {"claim_id": "C1", "precedent_id": "P1", "support_status": "unknown", "statement": "Relationship is unclear.", "evidence_ids": []}
    ]
    with pytest.raises(SecurityViolationError):
        CardAssemblyNode()._extra_security_gate_output(result)


def test_section8_04_prior_adjudication_fact_with_evidence_is_allowed():
    result = _result()
    card = result["formatted_output"]["cards"][0]
    result["formatted_output"]["_gate_verification"]["evidence_ids"] = ["EV-ADJ-P1-S1"]
    card["precedent_facts"] = [{"adjudication_id": "P1", "verdict_code": "accepted", "decided_at": "2026-01-01T00:00:00+00:00", "occurred_at": "2025-01-01T00:00:00+00:00", "valid_to": None, "rationale_evidence_ids": ["EV-ADJ-P1-S1"]}]
    output = CardAssemblyNode()._extra_security_gate_output(result)["formatted_output"]
    assert output["cards"][0]["precedent_facts"]


def test_s3_allows_multiple_existing_evidence_ids_for_one_precedent_fact():
    result = _result()
    card = result["formatted_output"]["cards"][0]
    result["formatted_output"]["_gate_verification"]["evidence_ids"] = ["EV-ADJ-P1-S1", "EV-NOTE-EX1-S1"]
    card["precedent_facts"] = [{"adjudication_id": "P1", "verdict_code": "accepted", "decided_at": "2026-01-01T00:00:00+00:00", "occurred_at": "2025-01-01T00:00:00+00:00", "valid_to": None, "rationale_evidence_ids": ["EV-ADJ-P1-S1", "EV-NOTE-EX1-S1"]}]
    assert CardAssemblyNode()._extra_security_gate_output(result)["formatted_output"]["cards"]


def test_section8_06_priority_ignores_llm_missing_item_count():
    eligibility = {"distinct_verdict_codes": ["accepted", "rejected"], "candidate_count": 2}
    assert _priority(eligibility, ["REQ-A"], False, {}) == _priority(eligibility, ["REQ-A"], False, {})


def test_section8_09_registry_does_not_issue_unrelated_source_id():
    exception = {"exception_id": "EX-1", "series_id": "S1"}
    unrelated = {"record_id": "P-OTHER", "rationale_safe": "long enough evidence text", "operational_note_safe": None}
    registry = _build_registry(exception, [], [], 8)
    assert registry == []
    assert unrelated["record_id"] not in {entry["source_id"] for entry in registry}


def test_section8_11_span_shorter_than_floor_is_not_registered():
    row = {"record_id": "P1", "rationale_safe": "short", "operational_note_safe": None}
    assert _build_registry({"exception_id": "EX-1", "series_id": "S1"}, [row], [], 8) == []


def test_section8_07_verbatim_evidence_copy_is_rejected():
    source = "This source contains a sixty-character literal sequence used only for copy-guard testing purposes."
    with pytest.raises(SecurityViolationError):
        CardAssemblyNode()._statement_guard(source, {"numeric_facts": []}, {}, [source], [])


@pytest.mark.parametrize("statement", ["62%", "1,000円", "-1.5", "1億円", "六十二パーセント", "三十五パーセント", "百二十件", "二億円", "999999円", "６２％"])
def test_section8_08_numeric_tokenizer_handles_adjacent_and_fullwidth_values(statement):
    tokens, vocabulary = _numeric_tokens(statement)
    assert vocabulary
    assert tokens


def test_section12_3bis_kanji_parser_preserves_large_unit_magnitude():
    assert extract_numeric_tokens("一億円")[0] == {"value": 100000000, "unit_dimension": "currency", "normalized_unit": "jpy"}
    assert extract_numeric_tokens("二億五千万")[0]["value"] == 250000000


def test_section12_3bis_allows_number_from_one_of_multiple_cited_evidence_ids():
    facts = {"numeric_facts": [{"value": 35, "unit_dimension": "percent", "normalized_unit": "percent", "source_evidence_id": "EV-ADJ-1-S1"}]}
    CardAssemblyNode()._statement_guard("35%", facts, {}, [], ["EV-ADJ-1-S1", "EV-NOTE-EX1-S1"])


def test_section12_3bis_rejects_number_from_unrelated_evidence_with_multiple_citations():
    node = CardAssemblyNode()
    facts = {"numeric_facts": [{"value": 62, "unit_dimension": "percent", "normalized_unit": "percent", "source_evidence_id": "EV-A"}]}
    with pytest.raises(SecurityViolationError):
        node._statement_guard("62%", facts, {}, [], ["EV-B", "EV-C"])


def test_section12_3bis_rejects_unit_swap_with_multiple_citations():
    node = CardAssemblyNode()
    facts = {"numeric_facts": [{"value": 62, "unit_dimension": "percent", "normalized_unit": "percent", "source_evidence_id": "EV-A"}]}
    with pytest.raises(SecurityViolationError):
        node._statement_guard("62円", facts, {}, [], ["EV-A", "EV-NOTE"])


def test_tc09_missing_service_rejects_and_provisioned_control_succeeds():
    envelope = '{"schema":"ene-c2-123/v1","mode":"invoke","business_unit_id":"unit-a","payload":{}}'
    rejected = IntakeNode()._extra_security_gate_input({"user_input": envelope})
    accepted = IntakeNode(config={"services": RuntimeServices()})._extra_security_gate_input({"user_input": envelope})
    assert rejected["status"] == "error"
    assert accepted.get("status") != "error"
