"""Closed-schema precedent-card assembly and S-3 enforcement."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar, NoReturn

from framework.errors import SecurityViolationError
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.security.credential_detector import detect_credentials
from shared.security.pii_detector import detect_pii
from shared.utils.audit_logger import emit_trace_event

from src.output_envelope import OutputEnvelopeNode
from src.services.numeric import extract_numeric_tokens, numeric_vocabulary
from src.services.response_contract import (
    CLAIM_FIELDS,
    CONFIRMATION_QUESTION_FIELDS,
    MISSING_INFORMATION_FIELDS,
    SUPPORT_STATUSES,
    field_names,
)
_TOP = frozenset({"mode", "card_kind", "exception_ref", "eligibility", "precedent_facts", "claims", "missing_information", "confirmation_questions", "review_priority", "degradation_reason", "_gate_verification"})
_INVOKE_OUTPUT = frozenset({"mode", "cards", "degradation_reason", "_gate_verification"})
_GATE = frozenset({"mode", "evidence_ids", "requirement_ids", "distinct_verdict_count", "candidate_count", "candidate_ids", "truncated", "numeric_facts"})


class CardAssemblyNode(OutputEnvelopeNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, config: Mapping[str, Any] | None = None) -> None:
        super().__init__()
        self._config = dict(config or {})

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        from shared.utils.audit_logger import emit_trace_event

        if state.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("upstream_error_passthrough", {"node": "post_process"}, state)
            return {"formatted_output": {"mode": state.get("mode", ""), "error": {"code": "processing_failed", "message": "Request could not be processed.", "detail": {}}}, "status": AgentStatus.ERROR.value}
        if not state.get("mode") and not state.get("user_input"):
            emit_trace_event("card_assembly_probe", {}, state)
            return {"formatted_output": {"mode": "ingest", "ingest_summary": {}}, "status": AgentStatus.SUCCESS.value}
        result = super().execute(state)
        formatted = result["formatted_output"]
        formatted.pop("gate_kind", None)
        if formatted.get("mode") == "invoke":
            snapshot = state.get("verification_snapshot")
            if not isinstance(snapshot, dict):
                self._reject(10, "verification_snapshot_missing", state)
            formatted["_gate_verification"] = dict(snapshot)
            result["_verbatim_sources"] = self._safe_evidence_texts(state)
        emit_trace_event("card_assembled", {"mode": formatted.get("mode", "")}, state)
        return result

    def format_invoke(self, state: dict[str, Any]) -> dict[str, Any]:
        eligibility = dict(state.get("eligibility", {}))
        requirements = list(state.get("requirement_ids", []))
        priority = _priority(eligibility, requirements, bool(state.get("verification_snapshot", {}).get("truncated", False)), self._config)
        return {"cards": [{
            "mode": "invoke",
            "card_kind": "reconciled" if eligibility.get("kind") == "conflicting" else "deterministic_display",
            "exception_ref": dict(state.get("exception_ref", {})),
            "eligibility": eligibility,
            # The internal facts also carry sanitized rationale text so the LLM
            # can perform the semantic comparison.  The customer card keeps the
            # established deterministic fact schema and provenance IDs only.
            "precedent_facts": [
                {
                    key: fact.get(key)
                    for key in (
                        "adjudication_id",
                        "verdict_code",
                        "decided_at",
                        "occurred_at",
                        "valid_to",
                        "rationale_evidence_ids",
                    )
                }
                for fact in state.get("precedent_facts", [])
                if isinstance(fact, dict)
            ],
            "claims": list(state.get("claims", [])),
            "missing_information": list(state.get("missing_information", [])),
            "confirmation_questions": list(state.get("confirmation_questions", [])),
            "review_priority": priority,
            "degradation_reason": state.get("degradation_reason"),
        }], "degradation_reason": state.get("degradation_reason")}

    def format_ingest(self, state: dict[str, Any]) -> dict[str, Any]:
        return {"ingest_summary": dict(state.get("ingest_summary", {}))}

    def format_feedback(self, state: dict[str, Any]) -> dict[str, Any]:
        return {"feedback_summary": dict(state.get("feedback_summary", {}))}

    def _extra_security_gate_output(self, result: dict[str, Any]) -> dict[str, Any]:
        formatted = result.get("formatted_output")
        if result.get("status") == AgentStatus.ERROR.value:
            return result
        if not isinstance(formatted, dict):
            self._reject(1, "formatted_output_invalid", result)
        mode = formatted.get("mode")
        if mode in {"ingest", "feedback"}:
            _assert_exact(formatted, {"mode", "ingest_summary"} if mode == "ingest" else {"mode", "feedback_summary"})
            return result
        if mode != "invoke":
            self._reject(10, "mode_unknown", result)
        _assert_exact(formatted, _INVOKE_OUTPUT)
        gate = formatted.get("_gate_verification")
        if not isinstance(gate, dict) or set(gate) != _GATE or gate.get("mode") != "invoke":
            self._reject(10, "verification_invalid", result)
        if not all(isinstance(gate[key], list) for key in ("evidence_ids", "requirement_ids", "candidate_ids")):
            self._reject(10, "verification_invalid", result)
        cards = formatted.get("cards")
        if not isinstance(cards, list) or not cards:
            self._reject(1, "cards_invalid", result)
        sources = result.pop("_verbatim_sources", [])
        if not isinstance(sources, list) or any(not isinstance(value, str) for value in sources):
            self._reject(9, "verbatim_sources_invalid", result)
        for card in cards:
            self._validate_card(card, gate, result, sources)
        formatted.pop("_gate_verification", None)
        result["formatted_output"] = _mask_nested(formatted)
        return result

    def _validate_card(self, card: dict[str, Any], gate: dict[str, Any], result: dict[str, Any], sources: list[str]) -> None:
        _assert_exact(card["exception_ref"], {"exception_id", "exception_category", "series_id", "detected_at", "metrics"})
        _assert_exact(card["eligibility"], {"kind", "distinct_verdict_codes", "candidate_count"})
        evidence_ids = set(gate["evidence_ids"])
        requirement_ids = set(gate["requirement_ids"])
        candidate_ids = set(gate["candidate_ids"])
        for fact in card["precedent_facts"]:
            _assert_exact(fact, {"adjudication_id", "verdict_code", "decided_at", "occurred_at", "valid_to", "rationale_evidence_ids"})
            if not set(fact["rationale_evidence_ids"]) <= evidence_ids:
                self._reject(6, "precedent_evidence_unknown", result)
        for claim in card["claims"]:
            _assert_exact(claim, field_names(CLAIM_FIELDS))
            if claim.get("support_status") not in SUPPORT_STATUSES or not isinstance(claim.get("evidence_ids"), list) or not claim["evidence_ids"]:
                self._reject(3, "claim_contract_invalid", result)
            if claim.get("precedent_id") not in candidate_ids:
                self._reject(6, "claim_precedent_unknown", result)
            if not set(claim["evidence_ids"]) <= evidence_ids:
                self._reject(2, "claim_evidence_unknown", result)
            self._statement_guard(claim["statement"], gate, result, sources, claim["evidence_ids"])
        for item in card["missing_information"]:
            _assert_exact(item, field_names(MISSING_INFORMATION_FIELDS))
            if item.get("requirement_id") not in requirement_ids or not set(item.get("evidence_ids", [])) <= evidence_ids:
                self._reject(4, "missing_information_invalid", result)
            self._statement_guard(item.get("statement"), gate, result, sources, item.get("evidence_ids", []))
        for question in card["confirmation_questions"]:
            _assert_exact(question, field_names(CONFIRMATION_QUESTION_FIELDS))
            if not set(question.get("evidence_ids", [])) <= evidence_ids:
                self._reject(2, "question_evidence_unknown", result)
            self._statement_guard(question.get("statement"), gate, result, sources, question.get("evidence_ids", []))
        expected = _priority(card["eligibility"], gate["requirement_ids"], bool(gate["truncated"]), self._config)
        if card.get("review_priority") != expected:
            self._reject(5, "priority_mismatch", result)

    def _statement_guard(self, statement: Any, gate: dict[str, Any], result: dict[str, Any], sources: list[str], evidence_ids: list[str]) -> None:
        if not isinstance(statement, str) or len(statement) > int(self._config.get("statement_max_chars", 400)):
            self._reject(9, "statement_invalid", result)
        if detect_credentials(statement):
            self._reject(8, "credential_detected", result)
        span = int(self._config.get("verbatim_guard_chars", 60))
        for source in sources:
            if len(source) >= span and any(source[index : index + span] in statement for index in range(len(source) - span + 1)):
                self._reject(9, "verbatim_copy_detected", result)
        tokens, vocabulary = _numeric_tokens(statement)
        if vocabulary and not tokens:
            self._reject(7, "numeric_tokenization_failed", result)
        facts = gate.get("numeric_facts", [])
        if not isinstance(facts, list) or any(not isinstance(item, dict) for item in facts):
            self._reject(7, "numeric_facts_invalid", result)
        allowed = {
            (item.get("value"), item.get("unit_dimension"), item.get("normalized_unit"), item.get("source_evidence_id"))
            for item in facts
        }
        if any(
            all(
                (token["value"], token["unit_dimension"], token["normalized_unit"], source) not in allowed
                for source in evidence_ids
            )
            for token in tokens
        ):
            self._reject(7, "numeric_fact_unknown", result)

    @staticmethod
    def _reject(check: int, reason: str, state: dict[str, Any]) -> NoReturn:
        emit_trace_event("s3_gate_rejected", {"check": check, "reason_code": reason}, state)
        raise SecurityViolationError(f"S-3 check {check}: {reason}")

    def _safe_evidence_texts(self, state: dict[str, Any]) -> list[str]:
        services = self._config.get("services")
        if services is None or not isinstance(state.get("business_unit_id"), str):
            return []
        scope = {"business_unit_id": state["business_unit_id"]}
        values: list[str] = []
        for record_id in state.get("candidate_ids", []):
            record = services.ledger.get(record_id, scope, state=state)
            if isinstance(record, dict):
                values.extend(value for value in (record.get("rationale_safe"), record.get("operational_note_safe")) if isinstance(value, str))
        return values


def _assert_exact(value: Any, keys: set[str] | frozenset[str]) -> None:
    if not isinstance(value, dict) or set(value) != set(keys):
        raise SecurityViolationError("S-3 closed allowlist violation")


def _priority(eligibility: Mapping[str, Any], requirement_ids: list[str], truncated: bool, config: Mapping[str, Any]) -> dict[str, Any]:
    count = len(eligibility.get("distinct_verdict_codes", []))
    candidates = int(eligibility.get("candidate_count", 0))
    score = int(config.get("priority_weight_verdict", 2)) * max(0, count - 1)
    score += int(config.get("priority_weight_missing", 1)) * len(set(requirement_ids))
    score += int(config.get("priority_weight_candidates", 1)) * int(truncated)
    level = "high" if score >= int(config.get("priority_high_threshold", 4)) else "medium" if score >= int(config.get("priority_medium_threshold", 2)) else "low"
    return {"level": level, "basis": {"distinct_verdict_count": count, "candidate_count": candidates, "missing_information_count": len(set(requirement_ids))}}


def _numeric_tokens(value: str) -> tuple[list[dict[str, Any]], bool]:
    return extract_numeric_tokens(value), numeric_vocabulary(value)


def _mask_nested(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _mask_nested(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_mask_nested(item) for item in value]
    if not isinstance(value, str):
        return value
    findings = detect_pii(value)
    masked = value
    for finding in reversed(findings or []):
        start, end = finding.get("start"), finding.get("end")
        if isinstance(start, int) and isinstance(end, int):
            masked = masked[:start] + "[MASKED]" + masked[end:]
    return masked


PostProcessNode = CardAssemblyNode
