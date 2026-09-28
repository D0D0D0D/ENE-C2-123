"""AgentCore Platform v1.0"""

# ADR-005: State must be a flat TypedDict (see ADR-005 for the prohibited
# alternatives). LangGraph checkpoints use msgpack serialization, so only
# plain serializable fields are allowed. Do NOT add credentials or secrets.

from typing import Any

from framework.schemas.agent_state import AgentState


class State(AgentState):  # type: ignore[misc]  # AgentCore wheel has no typed AgentState base.
    """Agent state.

    Add only fields specific to this agent below.
    All shared fields (user_input, status, session_id, node_history,
    error_log, hitl_*, etc.) are inherited from AgentState.
    """

    mode: str
    business_unit_id: str
    customer_ref: str
    exception_ref: dict[str, Any]
    payload_refs: dict[str, str]
    candidate_ids: list[str]
    eligibility: dict[str, Any]
    evidence_registry: list[dict[str, Any]]
    verification_snapshot: dict[str, Any]
    requirement_ids: list[str]
    precedent_facts: list[dict[str, Any]]
    claims: list[dict[str, Any]]
    missing_information: list[dict[str, Any]]
    confirmation_questions: list[dict[str, Any]]
    review_priority: dict[str, Any]
    ingest_summary: dict[str, Any]
    feedback_summary: dict[str, Any]
    degradation_reason: str | None
    error_code: str | None
