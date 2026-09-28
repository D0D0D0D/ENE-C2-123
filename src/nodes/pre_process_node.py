"""Intake validation and deterministic precedent narrowing."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping
from datetime import datetime
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from shared.security.pii_detector import detect_pii

from src.services.runtime import services_from_config
from src.services.numeric import extract_numeric_tokens
from src.payload_store import is_reference


_MODES = frozenset({"ingest", "invoke", "feedback"})


class IntakeNode(FunctionNode):  # type: ignore[misc]  # AgentCore wheel has no typed FunctionNode base.
    """Accept only the server-issued canonical envelope and build trusted state."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, config: Mapping[str, Any] | None = None) -> None:
        super().__init__()
        self._config = dict(config or {})

    def _extra_security_gate_input(self, state: dict[str, Any]) -> dict[str, Any]:
        # The connection/seam check happens before any payload reference can be
        # resolved.  The envelope has no free text by contract.
        raw = state.get("user_input")
        if not isinstance(raw, str) or not raw:
            return state
        services = services_from_config(self._config)
        if services is None:
            emit_trace_event("connection_unresolved_rejected", {"reason_code": "customer_connection_unresolved"}, state)
            return {**state, "status": AgentStatus.ERROR.value, "error_log": ["customer_connection_unresolved"]}
        try:
            envelope = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            emit_trace_event("intake_rejected", {"reason_code": "canonical_envelope_invalid"}, state)
            return {**state, "status": AgentStatus.ERROR.value, "error_log": ["canonical_envelope_invalid"]}
        if not isinstance(envelope, dict) or envelope.get("schema") != "ene-c2-123/v1":
            emit_trace_event("intake_rejected", {"reason_code": "canonical_envelope_invalid"}, state)
            return {**state, "status": AgentStatus.ERROR.value, "error_log": ["canonical_envelope_invalid"]}
        mode = envelope.get("mode")
        scope = envelope.get("business_unit_id")
        if mode not in _MODES or not _clean(scope):
            event = "scope_missing_rejected" if not _clean(scope) else "intake_rejected"
            emit_trace_event(event, {"reason_code": "scope_or_mode_invalid"}, state)
            return {**state, "status": AgentStatus.ERROR.value, "error_log": ["scope_or_mode_invalid"]}
        payload = envelope.get("payload")
        if not isinstance(payload, dict):
            emit_trace_event("intake_rejected", {"reason_code": "payload_invalid"}, state)
            return {**state, "status": AgentStatus.ERROR.value, "error_log": ["payload_invalid"]}
        return state

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        from shared.utils.audit_logger import emit_trace_event

        if state.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("upstream_error_passthrough", {"node": "intake"}, state)
            return {}
        raw = state.get("user_input", "")
        if not isinstance(raw, str) or not raw:
            emit_trace_event("intake_rejected", {"reason_code": "canonical_envelope_missing"}, state)
            return {"status": AgentStatus.ERROR.value, "error_log": ["canonical_envelope_missing"]}
        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError:
            emit_trace_event("intake_rejected", {"reason_code": "canonical_envelope_invalid"}, state)
            return {"status": AgentStatus.ERROR.value, "error_log": ["canonical_envelope_invalid"]}
        mode = envelope["mode"]
        payload = envelope["payload"]
        result: dict[str, Any] = {
            "mode": mode,
            "business_unit_id": envelope["business_unit_id"],
            "customer_ref": envelope.get("customer_ref", ""),
            "payload_refs": _collect_payload_refs(payload),
            "validated_input": json.dumps(envelope, ensure_ascii=False, sort_keys=True, default=str),
            "status": AgentStatus.SUCCESS.value,
        }
        if mode == "invoke":
            invoke_result = self._prepare_invoke(envelope, state)
            if invoke_result.get("status") == AgentStatus.ERROR.value:
                return invoke_result
            result.update(invoke_result)
        emit_trace_event("intake_validated", {"mode": mode}, state)
        return result

    def _prepare_invoke(self, envelope: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        payload = envelope["payload"]
        exceptions = payload.get("exceptions")
        if not isinstance(exceptions, list) or not exceptions:
            emit_trace_event("intake_rejected", {"reason_code": "exceptions_required"}, state)
            return {"status": AgentStatus.ERROR.value, "error_log": ["exceptions_required"]}
        maximum = int(self._config.get("max_exceptions_per_invoke", 50))
        if len(exceptions) > maximum or not isinstance(exceptions[0], dict):
            emit_trace_event("intake_rejected", {"reason_code": "exceptions_invalid"}, state)
            return {"status": AgentStatus.ERROR.value, "error_log": ["exceptions_invalid"]}
        exc = _exception_projection(exceptions[0])
        if exc is None:
            emit_trace_event("intake_rejected", {"reason_code": "exception_schema_invalid"}, state)
            return {"status": AgentStatus.ERROR.value, "error_log": ["exception_schema_invalid"]}
        services = services_from_config(self._config)
        if services is None:
            emit_trace_event("connection_unresolved_rejected", {"reason_code": "customer_connection_unresolved"}, state)
            return {"status": AgentStatus.ERROR.value, "error_log": ["customer_connection_unresolved"]}
        scope = {"business_unit_id": envelope["business_unit_id"]}
        try:
            matched = services.ledger.search(
                scope, record_kind="adjudication", classification={"exception_category": exc["exception_category"]}, state=state
            )
        except ValueError:
            emit_trace_event("scope_missing_rejected", {"reason_code": "ledger_scope_invalid"}, state)
            return {"status": AgentStatus.ERROR.value, "error_log": ["ledger_scope_invalid"]}
        detected_at = _instant(exc["detected_at"])
        matched = [
            row
            for row in matched
            if row.get("series_id") == exc["series_id"]
            and _in_validity_window(row, detected_at)
        ]
        counts = Counter(str(row["verdict_code"]) for row in matched)
        verdicts = sorted(counts)
        minority = sum(counts.values()) - max(counts.values(), default=0)
        if len(verdicts) >= int(self._config.get("conflict_min_distinct_verdicts", 2)) and minority >= int(
            self._config.get("conflict_min_minority_count", 1)
        ):
            kind = "conflicting"
        elif matched:
            kind = "uniquely_determined"
        else:
            kind = "no_precedent"
        selected, degradation = _select_candidates(
            matched,
            top_k=int(self._config.get("candidate_top_k", 8)),
            hard_cap=int(self._config.get("candidate_hard_cap", 24)),
        )
        note_text, note_degradation = _resolve_invoke_text(services, exc.get("operational_note_ref"), scope, state)
        notices, notice_degradations = _resolve_notices(
            services,
            payload.get("revision_notices", []),
            scope,
            state,
            series_id=exc["series_id"],
            detected_at=detected_at,
        )
        all_degradations = [value for value in (degradation, note_degradation, *notice_degradations) if value]
        registry = _build_registry(exc, selected, notices, int(self._config.get("min_span_chars", 8)), note_text)
        requirement_ids = _requirements(exc, selected, payload, degradation, note_text, note_degradation)
        numeric_facts = _numeric_facts(exc.get("metrics", {}), registry, selected, notices, note_text)
        snapshot = {
            "mode": "invoke",
            "evidence_ids": [entry["evidence_id"] for entry in registry] + (["EV-META-METRICS-value"] if numeric_facts else []),
            "requirement_ids": sorted(requirement_ids),
            "distinct_verdict_count": len(verdicts),
            "candidate_count": len(selected),
            "candidate_ids": [str(row["record_id"]) for row in selected],
            "truncated": bool(degradation),
            "numeric_facts": numeric_facts,
        }
        emit_trace_event("candidates_narrowed", {"candidate_count": len(selected)}, state)
        emit_trace_event("conflict_predicate_evaluated", {"kind": kind, "distinct_verdict_count": len(verdicts)}, state)
        if degradation:
            emit_trace_event("candidates_truncated", {"reason": degradation}, state)
        return {
            "exception_ref": {key: value for key, value in exc.items() if key != "operational_note_ref"},
            "candidate_ids": [str(row["record_id"]) for row in selected],
            "candidate_records": selected,
            "eligibility": {"kind": kind, "distinct_verdict_codes": verdicts, "candidate_count": len(selected)},
            "evidence_registry": registry,
            "requirement_ids": sorted(requirement_ids),
            "verification_snapshot": snapshot,
            "precedent_facts": _precedent_facts(selected, registry),
            "degradation_reason": "+".join(all_degradations) or ("no_precedent_records" if kind == "no_precedent" else None),
            "status": AgentStatus.SUCCESS.value,
        }


def _clean(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value == value.strip() and "\n" not in value and "\r" not in value


def _instant(value: Any) -> datetime | None:
    """Parse an ISO-8601 instant that carries an explicit UTC offset.

    Timestamps are compared to decide which precedents were valid when the
    exception was detected (design section 5-1 step 3).  Comparing the raw
    strings is wrong: ``2026-07-30T09:00:00+09:00`` sorts *after*
    ``2026-07-30T06:00:00+00:00`` lexicographically while being *earlier* in
    real time, so a valid precedent would be silently dropped.  Naive or
    date-only values are ambiguous, so they are rejected rather than guessed
    (fail-closed).
    """

    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _in_validity_window(row: Mapping[str, Any], detected_at: datetime | None) -> bool:
    """Return True when *row* was in force at *detected_at* (instant comparison)."""

    if detected_at is None:
        return False
    occurred = _instant(row.get("occurred_at"))
    if occurred is None or occurred > detected_at:
        return False
    valid_to_raw = row.get("valid_to")
    if valid_to_raw is None:
        return True
    valid_to = _instant(valid_to_raw)
    return valid_to is not None and valid_to >= detected_at


def _exception_projection(value: Mapping[str, Any]) -> dict[str, Any] | None:
    required = ("exception_id", "exception_category", "series_id", "detected_at")
    if any(not _clean(value.get(key)) for key in required):
        return None
    if _instant(value.get("detected_at")) is None:
        return None
    metrics = value.get("metrics", {})
    if not isinstance(metrics, dict) or any(not isinstance(item, (int, float)) or isinstance(item, bool) for item in metrics.values()):
        return None
    result = {key: value[key] for key in required} | {"metrics": dict(metrics)}
    ref = value.get("operational_note_ref")
    if ref is not None:
        if not is_reference(ref):
            return None
        result["operational_note_ref"] = ref
    return result


def _collect_payload_refs(payload: Mapping[str, Any]) -> dict[str, str]:
    refs: dict[str, str] = {}
    for key, value in payload.items():
        if isinstance(value, str) and key.endswith("_ref"):
            refs[key] = value
        elif isinstance(value, list):
            for index, item in enumerate(value):
                if isinstance(item, dict):
                    for nested_key, nested_value in item.items():
                        if nested_key.endswith("_ref") and isinstance(nested_value, str):
                            refs[f"{key}.{index}.{nested_key}"] = nested_value
    return refs


def _select_candidates(rows: list[dict[str, Any]], *, top_k: int, hard_cap: int) -> tuple[list[dict[str, Any]], str | None]:
    by_verdict: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_verdict.setdefault(str(row["verdict_code"]), []).append(row)
    for group in by_verdict.values():
        group.sort(key=lambda item: (str(item.get("decided_at", "")), str(item["record_id"])), reverse=True)
    required = [group[0] for _, group in sorted(by_verdict.items())]
    target = max(top_k, len(required))
    selected = required[:hard_cap]
    remaining = sorted(
        [row for row in rows if row not in selected],
        key=lambda item: (str(item.get("decided_at", "")), str(item["record_id"])),
        reverse=True,
    )
    selected.extend(remaining[: max(0, min(target, hard_cap) - len(selected))])
    if len(by_verdict) > hard_cap:
        return selected, f"verdict_codes_truncated:{len(by_verdict) - hard_cap}"
    if len(rows) > len(selected):
        return selected, f"candidates_truncated:{len(rows) - len(selected)}"
    return selected, None


def _build_registry(exc: dict[str, Any], rows: list[dict[str, Any]], notices: Any, minimum: int, note_text: str | None = None) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for row in rows:
        for field in ("rationale", "operational_note"):
            text = row.get(f"{field}_safe")
            if isinstance(text, str) and len(text) >= minimum:
                entries.append(_entry(f"EV-ADJ-{row['record_id']}-S{len(entries)+1}", "adjudication", row["record_id"], field, text))
    if isinstance(note_text, str) and len(note_text) >= minimum:
        entries.append(_entry(f"EV-NOTE-{exc['exception_id']}-S{len(entries)+1}", "exception_note", exc["exception_id"], "operational_note", note_text))
    if isinstance(notices, list):
        for notice in notices:
            if not isinstance(notice, dict) or notice.get("series_id") != exc["series_id"]:
                continue
            text = notice.get("body_safe")
            if isinstance(text, str) and len(text) >= minimum:
                entries.append(_entry(f"EV-REV-{notice.get('notice_id', 'NOTICE')}-S{len(entries)+1}", "revision_notice", str(notice.get("notice_id")), "body", text))
    return entries


def _entry(evidence_id: str, source_kind: str, source_id: str, field: str, text: str) -> dict[str, Any]:
    return {
        "evidence_id": evidence_id,
        "source_kind": source_kind,
        "source_id": str(source_id),
        "field_name": field,
        "projection_kind": "safe",
        "offset_unit": "utf8_codepoint",
        "span_start": 0,
        "span_end": len(text),
        "content_hash": hashlib.sha256(text.encode()).hexdigest()[:16],
    }


def _precedent_facts(rows: list[dict[str, Any]], registry: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_source: dict[str, list[str]] = {}
    for entry in registry:
        if entry["source_kind"] == "adjudication":
            by_source.setdefault(entry["source_id"], []).append(entry["evidence_id"])
    return [
        {
            "adjudication_id": row["record_id"], "verdict_code": row["verdict_code"], "decided_at": row["decided_at"],
            "occurred_at": row["occurred_at"], "valid_to": row.get("valid_to"),
            # The ledger search path provides the sanitized model-visible
            # projection.  The fallback is retained for isolated callers that
            # supply an already-trusted adjudication row.
            "rationale": row.get("rationale_safe")
            if isinstance(row.get("rationale_safe"), str)
            else row.get("rationale", ""),
            "rationale_evidence_ids": by_source.get(str(row["record_id"]), []),
        }
        for row in rows
    ]


def _requirements(exc: dict[str, Any], rows: list[dict[str, Any]], payload: Mapping[str, Any], degradation: str | None, note_text: str | None, note_degradation: str | None) -> set[str]:
    values: set[str] = set()
    if note_degradation:
        values.add(f"REQ-PAYLOAD-UNRESOLVED:{exc.get('operational_note_ref', '')}")
    elif note_text is None:
        values.add("REQ-OPERATIONAL-NOTE-ABSENT")
    series_meta = payload.get("series_meta")
    if not isinstance(series_meta, list) or not any(
        isinstance(item, dict) and item.get("series_id") == exc["series_id"]
        for item in series_meta
    ):
        values.add("REQ-SERIES-META-ABSENT")
    if degradation:
        values.add("REQ-CANDIDATES-TRUNCATED")
    for row in rows:
        if not row.get("rationale_safe"):
            values.add(f"REQ-RATIONALE-EMPTY:{row['record_id']}")
    return values


def _numeric_facts(metrics: Mapping[str, Any], registry: list[dict[str, Any]], rows: list[dict[str, Any]], notices: Any, note_text: str | None) -> list[dict[str, Any]]:
    """Build the §12-3-bis facts with an evidence-specific source binding."""

    texts: dict[tuple[str, str], str] = {}
    for row in rows:
        for field in ("rationale", "operational_note"):
            value = row.get(f"{field}_safe")
            if isinstance(value, str):
                texts[(str(row["record_id"]), field)] = value
    if isinstance(note_text, str):
        texts[("__exception_note__", "operational_note")] = note_text
    if isinstance(notices, list):
        for notice in notices:
            if isinstance(notice, dict) and isinstance(notice.get("body_safe"), str):
                texts[(str(notice.get("notice_id")), "body")] = notice["body_safe"]
    facts: list[dict[str, Any]] = []
    for entry in registry:
        source_id = "__exception_note__" if entry["source_kind"] == "exception_note" else entry["source_id"]
        text = texts.get((source_id, entry["field_name"]), "")
        for token in extract_numeric_tokens(text):
            facts.append({**token, "source_evidence_id": entry["evidence_id"]})
    for value in metrics.values():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            facts.append({"value": value, "unit_dimension": "number", "normalized_unit": "number", "source_evidence_id": "EV-META-METRICS-value"})
    return facts


def _resolve_invoke_text(services: Any, ref: Any, scope: dict[str, str], state: dict[str, Any]) -> tuple[str | None, str | None]:
    if ref is None:
        return None, None
    try:
        value = services.payload_store.resolve(ref, scope=scope, session_id=state["session_id"], consume=False)
    except ValueError:
        emit_trace_event("payload_unresolved_skipped", {"reason_code": "payload_ref_unresolvable"}, state)
        return None, f"payload_unresolved:{state.get('exception_ref', {}).get('exception_id', 'exception')}"
    if not isinstance(value, str):
        return None, "payload_unresolved:exception"
    emit_trace_event("payload_resolved", {"field": "invoke_operational_note"}, state)
    return _safe_text(value), None


def _resolve_notices(
    services: Any,
    notices: Any,
    scope: dict[str, str],
    state: dict[str, Any],
    *,
    series_id: str,
    detected_at: datetime | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    resolved: list[dict[str, Any]] = []
    degradations: list[str] = []
    for notice in notices if isinstance(notices, list) else []:
        if (
            not isinstance(notice, dict)
            or not _clean(notice.get("notice_id"))
            or notice.get("series_id") != series_id
            or not is_reference(notice.get("body_ref"))
        ):
            continue
        effective_at = _instant(notice.get("effective_date"))
        if detected_at is None or effective_at is None or effective_at > detected_at:
            continue
        try:
            body = services.payload_store.resolve(notice["body_ref"], scope=scope, session_id=state["session_id"], consume=False)
        except ValueError:
            degradations.append(f"payload_unresolved:{notice['notice_id']}")
            emit_trace_event("payload_unresolved_skipped", {"reason_code": "payload_ref_unresolvable"}, state)
            continue
        if isinstance(body, str):
            resolved.append({"notice_id": notice["notice_id"], "series_id": notice["series_id"], "effective_date": notice.get("effective_date"), "body_safe": _safe_text(body)})
            emit_trace_event("payload_resolved", {"field": "revision_notice"}, state)
    return resolved, degradations


def _safe_text(value: str) -> str:
    masked = value
    for finding in reversed(detect_pii(value) or []):
        start, end = finding.get("start"), finding.get("end")
        if isinstance(start, int) and isinstance(end, int):
            masked = masked[:start] + "[MASKED]" + masked[end:]
    return masked


PreProcessNode = IntakeNode
