"""Inner domain workflow for ingest, invoke, and feedback."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, ClassVar

from framework.errors import SecurityViolationError
from framework.graph.base_graph import BaseGraph
from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from langgraph.graph import END, START
from shared.security.credential_detector import detect_credentials
from shared.utils.audit_logger import emit_trace_event

from src.feedback_intake import FeedbackIntakeNode, FeedbackIntakeService
from src.ingest_row_pipeline import IngestRowPipelineNode, RowPipelineHooks
from src.llm_draft_stage import DraftStageHooks, LlmDraftStageNode
from src.nodes.pre_process_node import (
    _build_registry,
    _in_validity_window,
    _instant,
    _select_candidates,
)
from src.services.input_sanitizer import text_quality_reason
from src.services.response_contract import project_response_draft, response_contract
from src.services.runtime import RuntimeServices, services_from_config


class PrecedentWorkflowState(AgentState):  # type: ignore[misc]  # AgentCore wheel has no typed state base.
    mode: str
    business_unit_id: str
    canonical_payload: dict[str, Any]
    exception_ref: dict[str, Any]
    candidate_ids: list[str]
    eligibility: dict[str, Any]
    evidence_registry: list[dict[str, Any]]
    requirement_ids: list[str]
    precedent_facts: list[dict[str, Any]]
    claims: list[dict[str, Any]]
    missing_information: list[dict[str, Any]]
    confirmation_questions: list[dict[str, Any]]
    ingest_summary: dict[str, Any]
    feedback_summary: dict[str, Any]
    degradation_reason: str | None
    error_code: str | None


class ModeRouterNode(FunctionNode):  # type: ignore[misc]  # AgentCore wheel has no typed node base.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        from shared.utils.audit_logger import emit_trace_event

        if state.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("upstream_error_passthrough", {"node": "mode_router"}, state)
            return {}
        try:
            parsed = json.loads(state.get("user_input", ""))
        except (TypeError, json.JSONDecodeError):
            emit_trace_event("intake_rejected", {"reason_code": "subgraph_envelope_invalid"}, state)
            return {"status": AgentStatus.ERROR.value, "error_log": ["subgraph_envelope_invalid"]}
        if not isinstance(parsed, dict) or parsed.get("mode") not in {"ingest", "invoke", "feedback"}:
            emit_trace_event("intake_rejected", {"reason_code": "subgraph_mode_invalid"}, state)
            return {"status": AgentStatus.ERROR.value, "error_log": ["subgraph_mode_invalid"]}
        emit_trace_event("domain_mode_routed", {"mode": parsed["mode"]}, state)
        return {**parsed, "status": AgentStatus.SUCCESS.value}


class InvokeDeterministicNode(FunctionNode):  # type: ignore[misc]  # AgentCore wheel has no typed node base.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        from shared.utils.audit_logger import emit_trace_event

        if state.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("upstream_error_passthrough", {"node": "invoke_deterministic"}, state)
            return {}
        kind = state.get("eligibility", {}).get("kind")
        facts = list(state.get("precedent_facts", []))
        question = "対象系列の運用メモと改訂告知を確認してください。"
        emit_trace_event("deterministic_display_built", {"eligibility": kind}, state)
        return {
            "precedent_facts": facts,
            "claims": [],
            "missing_information": [
                {"item_id": f"M{index + 1}", "requirement_id": value, "statement": "確認が必要な情報です。", "evidence_ids": []}
                for index, value in enumerate(state.get("requirement_ids", []))
            ],
            "confirmation_questions": [{"question_id": "Q1", "statement": question, "evidence_ids": []}],
            "status": AgentStatus.SUCCESS.value,
        }


class InvokeReconcileNode(LlmDraftStageNode):
    """Use the copied LLM draft stage, then project only card drafting fields."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, config: Mapping[str, Any] | None = None) -> None:
        values = dict(config or {})
        max_statement_chars = int(values.get("statement_max_chars", 400))
        hooks = DraftStageHooks(
            build_prompt=lambda state: _prompt(state, max_statement_chars=max_statement_chars),
            build_fallback=_fallback,
            build_evidence=_evidence,
        )
        super().__init__(config=values, hooks=hooks)
        self._max_statement_chars = max_statement_chars

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        from shared.utils.audit_logger import emit_trace_event

        result = super().execute(state)
        if result.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("reconcile_rejected", {"reason_code": "llm_draft_invalid"}, state)
            return result
        try:
            draft = project_response_draft(
                result.get("draft", {}),
                max_statement_chars=self._max_statement_chars,
            )
        except SecurityViolationError:
            emit_trace_event(
                "reconcile_rejected",
                {"reason_code": "llm_output_contract_failed"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_code": "llm_output_contract_failed",
                "error_log": ["InvokeReconcileNode: llm_output_contract_failed"],
            }
        degradation = result.get("degradation_reason")
        if degradation:
            degradation = "llm_unavailable"
            emit_trace_event("llm_unavailable_degraded", {"mode": "invoke"}, state)
        emit_trace_event("reconcile_drafted", {"claim_count": len(draft.get("claims", []))}, state)
        return {
            "precedent_facts": list(state.get("precedent_facts", [])),
            "claims": draft.get("claims", []),
            "missing_information": draft.get("missing_information", []),
            "confirmation_questions": draft.get("confirmation_questions", []),
            "degradation_reason": degradation,
            "status": AgentStatus.SUCCESS.value,
        }


class IngestStageNode(FunctionNode):  # type: ignore[misc]  # AgentCore wheel has no typed node base.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, config: Mapping[str, Any] | None = None) -> None:
        super().__init__()
        self._config = dict(config or {})

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        from shared.utils.audit_logger import emit_trace_event

        services = _services(self._config)
        if services is None:
            emit_trace_event("connection_unresolved_rejected", {"reason_code": "customer_connection_unresolved"}, state)
            return {"status": AgentStatus.ERROR.value, "error_log": ["customer_connection_unresolved"]}
        rows = state.get("canonical_payload", {}).get("adjudications", [])
        if not isinstance(rows, list) or not rows:
            return {"status": AgentStatus.ERROR.value, "error_log": ["ingest_rows_required"]}
        scope = {"business_unit_id": state["business_unit_id"]}
        prepared: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        for index, row in enumerate(rows):
            try:
                prepared.append(_prepare_ingest_row(row, services, scope, state, self._config))
            except ValueError as exc:
                rejected.append({"row_index": index, "reason_code": str(exc), "detail": "row rejected"})
                emit_trace_event("ingest_row_rejected", {"row_index": index, "reason_code": str(exc)}, state)
        if rejected:
            return {"ingest_summary": {"written_ids": [], "rejected": rejected, "lifecycle_transitions": []}, "status": AgentStatus.ERROR.value, "error_log": ["ingest_rejected"]}
        pipeline = _AdjudicationRowPipeline(
            hooks=RowPipelineHooks(
                sanitize=lambda _local_state, row: row,
                normalize=lambda _local_state, row: row,
                write_batch=lambda _local_state, batch: services.write_ledger_batch(
                    scope,
                    batch,
                    state=state,
                ),
            ),
            reject_batch_on_validation_error=True,
        )
        pipeline_result = pipeline.execute({"rows": prepared, "status": AgentStatus.SUCCESS.value})
        summary = pipeline_result["ingest_summary"]
        if summary["rejected"]:
            return {"ingest_summary": {"written_ids": [], "rejected": summary["rejected"], "lifecycle_transitions": []}, "status": AgentStatus.ERROR.value, "error_log": ["ingest_rejected"]}
        emit_trace_event("ledger_written", {"count": len(summary["written_ids"])}, state)
        return {"ingest_summary": {"written_ids": summary["written_ids"], "rejected": [], "lifecycle_transitions": []}, "status": AgentStatus.SUCCESS.value}


class FeedbackStageNode(FunctionNode):  # type: ignore[misc]  # AgentCore wheel has no typed node base.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, config: Mapping[str, Any] | None = None) -> None:
        super().__init__()
        self._config = dict(config or {})

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        from shared.utils.audit_logger import emit_trace_event

        services = _services(self._config)
        if services is None:
            emit_trace_event("connection_unresolved_rejected", {"reason_code": "customer_connection_unresolved"}, state)
            return {"status": AgentStatus.ERROR.value, "error_log": ["customer_connection_unresolved"]}
        payload = state.get("canonical_payload", {})
        scope = {"business_unit_id": state["business_unit_id"]}
        kind = payload.get("kind", "adjudication")
        try:
            if kind == "adjudication":
                rationale = _resolve_ref(services, payload.get("rationale_ref"), scope, state)
                _reject_credentials(rationale)
                adopted_ids = payload.get("adopted_precedent_ids", [])
                rejected_ids = payload.get("rejected_precedent_ids", [])
                evidence_ids = payload.get("decisive_difference_evidence_ids", [])
                if not all(isinstance(values, list) for values in (adopted_ids, rejected_ids, evidence_ids)):
                    raise ValueError("feedback_reference_invalid")
                precedent_ids = [*adopted_ids, *rejected_ids]
                if any(not isinstance(record_id, str) for record_id in precedent_ids):
                    raise ValueError("feedback_precedent_not_owned")
                matched = services.ledger.search(
                    scope,
                    record_kind="adjudication",
                    classification={"exception_category": payload["exception_category"]},
                    state=state,
                )
                detected_at = _instant(payload.get("detected_at"))
                matched = [
                    row
                    for row in matched
                    if row.get("series_id") == payload.get("series_id")
                    and _in_validity_window(row, detected_at)
                ]
                selected, _degradation = _select_candidates(
                    matched,
                    top_k=int(self._config.get("candidate_top_k", 8)),
                    hard_cap=int(self._config.get("candidate_hard_cap", 24)),
                )
                selected_ids = {str(row.get("record_id")) for row in selected}
                if not set(precedent_ids) <= selected_ids:
                    raise ValueError("feedback_precedent_not_owned")
                trusted_registry = _build_registry(
                    {
                        "exception_id": payload.get("exception_id", ""),
                        "series_id": payload.get("series_id", ""),
                    },
                    selected,
                    [],
                    int(self._config.get("min_span_chars", 8)),
                )
                trusted_evidence_ids = {
                    entry["evidence_id"]
                    for entry in trusted_registry
                    if entry.get("source_kind") == "adjudication"
                    and entry.get("source_id") in set(precedent_ids)
                }
                if any(
                    not isinstance(evidence_id, str)
                    or evidence_id not in trusted_evidence_ids
                    for evidence_id in evidence_ids
                ):
                    raise ValueError("feedback_evidence_not_owned")
                record = {
                    "record_kind": "adjudication", "classification": {"exception_category": payload["exception_category"]},
                    "series_id": payload["series_id"], "operational_note": None, "occurred_at": payload["detected_at"],
                    "valid_to": None, "verdict_code": payload["confirmed_verdict_code"], "rationale": rationale,
                    "decided_by": state.get("caller_id", ""), "decided_at": payload["decided_at"], "decision_source": "human",
                    "retrieval_text": f"{payload['exception_category']} {payload['series_id']}",
                    "provenance": {key: payload.get(key, []) for key in ("adopted_precedent_ids", "rejected_precedent_ids", "decisive_difference_evidence_ids")},
                    "supersedes_record_id": None,
                }
                written = services.ledger.write(scope, record, state=state)
                summary = {"kind": kind, "written_id": written["record_id"], "recall_hit": False, "lifecycle_transitions": []}
            elif kind == "correction":
                service = FeedbackIntakeService(
                    ledger=services.ledger,
                    receipt_store=services.receipt_store,
                    domain_fields={"series_id": "scalar", "operational_note": "text"},
                )
                node = FeedbackIntakeNode(
                    service=service,
                    payload_store=services.payload_store,
                    scope_keys=("business_unit_id",),
                )
                feedback_result = node(
                    {
                        **state,
                        "feedback_payload_ref": payload.get("feedback_payload_ref"),
                    }
                )
                if not isinstance(feedback_result, dict):
                    return {
                        "status": AgentStatus.ERROR.value,
                        "error_log": ["feedback_node_result_invalid"],
                    }
                if feedback_result.get("status") != AgentStatus.SUCCESS.value:
                    return feedback_result
                result = feedback_result["feedback_result"]
                summary = {
                    "kind": kind,
                    "written_id": result["record_id"],
                    "recall_hit": True,
                    "lifecycle_transitions": [],
                }
            else:
                raise ValueError("feedback_kind_invalid")
        except (KeyError, ValueError) as exc:
            emit_trace_event("ingest_row_rejected", {"reason_code": str(exc)}, state)
            return {"status": AgentStatus.ERROR.value, "error_log": [str(exc)]}
        emit_trace_event("ledger_written", {"mode": "feedback"}, state)
        return {"feedback_summary": summary, "status": AgentStatus.SUCCESS.value}


class _AdjudicationRowPipeline(IngestRowPipelineNode):
    """Part pipeline with the template's adjudication row contract."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL
    # IngestStageNode has already checked the external row and resolved the
    # text refs before handing this part its normalized ledger record.
    required_fields: ClassVar[tuple[str, ...]] = ()
    required_any_fields: ClassVar[tuple[tuple[str, ...], ...]] = ()


class PrecedentWorkflowGraph(BaseGraph):  # type: ignore[misc]  # AgentCore wheel has no typed graph base.
    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        super().__init__(dict(config or {}))

    @property
    def name(self) -> str:
        return "supply_data_quality_precedent_workflow"

    @property
    def state_schema(self) -> type:
        return PrecedentWorkflowState

    def _validate_config(self) -> None:
        return None

    def register_nodes(self) -> None:
        self._nodes = {
            "mode_router": ModeRouterNode(), "ingest_stage": IngestStageNode(config=self.config),
            "invoke_reconcile": InvokeReconcileNode(config=self.config), "invoke_deterministic": InvokeDeterministicNode(),
            "feedback_stage": FeedbackStageNode(config=self.config),
        }

    def add_edges(self) -> None:
        self._sg.add_edge(START, "mode_router")
        self._sg.add_conditional_edges("mode_router", self.route, {
            "ingest_stage": "ingest_stage", "invoke_reconcile": "invoke_reconcile", "invoke_deterministic": "invoke_deterministic",
            "feedback_stage": "feedback_stage", "end": END,
        })
        for name in ("ingest_stage", "invoke_reconcile", "invoke_deterministic", "feedback_stage"):
            self._sg.add_edge(name, END)

    def route(self, state: PrecedentWorkflowState) -> str:
        if state.get("status") == AgentStatus.ERROR.value:
            return "end"
        mode = state.get("mode")
        if mode == "invoke":
            return "invoke_reconcile" if state.get("eligibility", {}).get("kind") == "conflicting" else "invoke_deterministic"
        return {"ingest": "ingest_stage", "feedback": "feedback_stage"}.get(mode, "end")

    def get_output(self, state: PrecedentWorkflowState) -> dict[str, Any]:
        fields = ("ingest_summary", "feedback_summary", "precedent_facts", "claims", "missing_information", "confirmation_questions", "degradation_reason", "error_code", "error_log")
        return {key: state[key] for key in fields if key in state} | {"mode": state.get("mode"), "status": state.get("status")}


def _services(config: Mapping[str, Any]) -> RuntimeServices | None:
    return services_from_config(config)


def _prompt(state: dict[str, Any], *, max_statement_chars: int) -> str:
    return json.dumps(
        {
            "task": "Relate supplied precedent evidence without deciding the current case.",
            "precedent_facts": state.get("precedent_facts", []),
            "eligibility": state.get("eligibility", {}),
            "evidence_ids": [
                item.get("evidence_id") for item in state.get("evidence_registry", [])
            ],
            "requirement_ids": state.get("requirement_ids", []),
            "response_contract": response_contract(max_statement_chars=max_statement_chars),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _fallback(state: dict[str, Any]) -> dict[str, Any]:
    return {"claims": [], "missing_information": [{"item_id": f"M{index+1}", "requirement_id": item, "statement": "確認が必要な情報です。", "evidence_ids": []} for index, item in enumerate(state.get("requirement_ids", []))], "confirmation_questions": []}


def _evidence(state: dict[str, Any], draft: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    ids: list[str] = []
    for section in ("claims", "missing_information", "confirmation_questions"):
        for item in draft.get(section, []) if isinstance(draft.get(section), list) else []:
            if isinstance(item, dict):
                ids.extend(str(value) for value in item.get("evidence_ids", []) if isinstance(value, str))
    return [], sorted(set(ids))


def _resolve_ref(services: RuntimeServices, ref: Any, scope: dict[str, str], state: dict[str, Any]) -> Any:
    if not isinstance(ref, str):
        raise ValueError("payload_ref_unresolvable")
    try:
        value = services.payload_store.resolve(ref, scope=scope, session_id=state["session_id"], consume=True)
    except ValueError as exc:
        emit_trace_event("payload_unresolved_skipped", {"reason_code": str(exc)}, state)
        raise ValueError("payload_ref_unresolvable") from exc
    emit_trace_event("payload_resolved", {"field": "free_text"}, state)
    return value


def _reject_credentials(text: Any) -> None:
    if isinstance(text, str) and detect_credentials(text):
        raise ValueError("credential_detected")


def _prepare_ingest_row(
    row: Any, services: RuntimeServices, scope: dict[str, str], state: dict[str, Any], config: Mapping[str, Any]
) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise ValueError("row_not_object")
    required = ("exception_category", "series_id", "occurred_at", "verdict_code", "decided_at")
    if any(not isinstance(row.get(key), str) or not row[key] for key in required):
        raise ValueError("row_schema_invalid")
    rationale = _resolve_ref(services, row.get("rationale_ref"), scope, state)
    note = _resolve_ref(services, row.get("operational_note_ref"), scope, state) if row.get("operational_note_ref") else None
    reason = text_quality_reason(rationale, config)
    if reason:
        raise ValueError(reason)
    if note is not None:
        reason = text_quality_reason(note, config)
        if reason:
            raise ValueError(reason)
    _reject_credentials(rationale)
    _reject_credentials(note)
    return {
        "record_kind": "adjudication", "classification": {"exception_category": row["exception_category"]},
        "series_id": row["series_id"], "operational_note": note, "occurred_at": row["occurred_at"], "valid_to": row.get("valid_to"),
        "verdict_code": row["verdict_code"], "rationale": rationale, "decided_by": row.get("decided_by") or state.get("caller_id", ""),
        "decided_at": row["decided_at"], "decision_source": row.get("decision_source", "human"),
        "retrieval_text": f"{row['exception_category']} {row['series_id']}", "provenance": {}, "supersedes_record_id": row.get("supersedes_record_id"),
    }
