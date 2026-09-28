"""Cat2 main-slot node that adapts the outer state to the domain subgraph."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, ClassVar

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from src.cat2_graph_skeleton import DomainWorkflowGraphNode, ModeSpec
from src.graph.domain_workflow_graph import PrecedentWorkflowGraph


class PrecedentWorkflowGraphNode(DomainWorkflowGraphNode):
    """Cached, non-HITL GraphNode for the three domain modes."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL
    propagate_hitl: ClassVar[bool] = False
    mode_specs: ClassVar[dict[str, ModeSpec]] = {
        "ingest": ModeSpec("ingest", {"ingest_summary": "ingest_summary", "error_log": "error_log"}),
        "feedback": ModeSpec("feedback", {"feedback_summary": "feedback_summary", "error_log": "error_log"}),
        "invoke": ModeSpec(
            "draft",
            {
                "precedent_facts": "precedent_facts",
                "claims": "claims",
                "missing_information": "missing_information",
                "confirmation_questions": "confirmation_questions",
                "degradation_reason": "degradation_reason",
                "error_code": "error_code",
                "error_log": "error_log",
            },
        ),
    }

    def __init__(self, *, config: Mapping[str, Any] | None = None) -> None:
        super().__init__(config=dict(config or {}), subgraph_factory=lambda values: PrecedentWorkflowGraph(values))

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        from shared.utils.audit_logger import emit_trace_event

        if state.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("upstream_error_passthrough", {"node": "main"}, state)
            return {}
        if not state.get("mode") and not state.get("user_input"):
            # Framework/PB probes call every concrete node with only lifecycle
            # fields.  This is not an invocation path because no envelope was
            # supplied; keep the probe deterministic without constructing a
            # subgraph context.
            emit_trace_event("domain_workflow_probe", {}, state)
            return {"result": "canonical envelope required", "status": AgentStatus.SUCCESS.value}
        emit_trace_event("domain_workflow_started", {"mode": state.get("mode", "")}, state)
        return super().execute(state)

    def extract_input(self, state: dict[str, Any]) -> str:
        # BaseGraph accepts str only.  This closed projection deliberately
        # excludes trusted verification material and arbitrary inner keys.
        fields = (
            "mode", "business_unit_id", "customer_ref", "exception_ref", "payload_refs", "candidate_ids",
            "eligibility", "evidence_registry", "requirement_ids", "precedent_facts", "degradation_reason",
        )
        payload = {key: state[key] for key in fields if key in state}
        if "validated_input" in state:
            try:
                payload["canonical_payload"] = json.loads(state["validated_input"]).get("payload", {})
            except (TypeError, json.JSONDecodeError):
                payload["canonical_payload"] = {}
        return json.dumps(payload, ensure_ascii=False, default=str, sort_keys=True)

    def merge_output(self, state: dict[str, Any], sub_result: dict[str, Any]) -> dict[str, Any]:
        # Preserve the skeleton's closed mode mapping, but never return values
        # which an inner/LLM result could use to overwrite trusted state.
        delta = super().merge_output(state, sub_result)
        for protected in ("verification_snapshot", "evidence_registry", "eligibility", "review_priority", "requirement_ids"):
            delta.pop(protected, None)
        return delta


MainNode = PrecedentWorkflowGraphNode
