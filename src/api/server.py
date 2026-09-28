"""HTTP adapter: authenticate, project strict requests, and mint payload refs."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from framework.schemas.invocation_context import InvocationContext
from framework.secrets.context import bound_secrets
from shared.secrets import factory as secrets_factory

from src.entry_adapter import Operation, authenticate
from src.graph.graph import SupplyDataQualityPrecedentGraph


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AdjudicationInput(StrictModel):
    adjudication_id: str | None = None
    exception_category: str
    series_id: str
    occurred_at: str
    valid_to: str | None = None
    verdict_code: str
    decided_at: str
    decision_source: str = "human"
    supersedes_record_id: str | None = None
    rationale: str
    operational_note: str | None = None


class SeriesMetaInput(StrictModel):
    series_id: str
    publisher: str
    unit: str


class IngestRequest(StrictModel):
    adjudications: list[AdjudicationInput]
    series_meta: list[SeriesMetaInput] = Field(default_factory=list)
    session_id: str = ""


class ExceptionInput(StrictModel):
    exception_id: str
    exception_category: str
    series_id: str
    detected_at: str
    metrics: dict[str, float | int] = Field(default_factory=dict)
    operational_note: str | None = None


class RevisionNoticeInput(StrictModel):
    notice_id: str
    series_id: str
    publisher: str
    effective_date: str
    body: str


class InvokeRequest(StrictModel):
    exceptions: list[ExceptionInput]
    revision_notices: list[RevisionNoticeInput] = Field(default_factory=list)
    session_id: str = ""


class FeedbackAdjudicationRequest(StrictModel):
    kind: Literal["adjudication"] = "adjudication"
    exception_id: str
    exception_category: str
    series_id: str
    detected_at: str
    confirmed_verdict_code: str
    rationale: str
    decided_at: str
    adopted_precedent_ids: list[str] = Field(default_factory=list)
    rejected_precedent_ids: list[str] = Field(default_factory=list)
    decisive_difference_evidence_ids: list[str] = Field(default_factory=list)
    session_id: str = ""


class FeedbackCorrectionRequest(StrictModel):
    kind: Literal["correction"] = "correction"
    record_id: str
    feedback_seq: int
    verdict_code: str
    rationale: str
    decided_at: str
    session_id: str = ""


app = FastAPI(title="SupplyDataQualityPrecedentAgent")
app.state.runtime_config = {}


def _build_agent() -> SupplyDataQualityPrecedentGraph:
    config = dict(app.state.runtime_config)
    graph = SupplyDataQualityPrecedentGraph(config)
    graph.compile()
    graph.provision_secrets(secrets_factory(namespace="ene", agent_name="supply_data_quality_precedent"))
    return graph


@app.on_event("startup")
def startup() -> None:
    # Deployment provisioning may set runtime_config (including a BaseLLM and
    # customer backend seams) before TestClient/startup; no concrete model is
    # named in this template.
    app.state.agent = _build_agent()


def _agent() -> SupplyDataQualityPrecedentGraph:
    graph = getattr(app.state, "agent", None)
    if graph is None:
        graph = _build_agent()
        app.state.agent = graph
    return graph


def _scope_and_context(
    request: Request,
    session_id: str,
    operation: Operation,
) -> tuple[dict[str, str], InvocationContext]:
    """Bind the verified customer-internal partition to the real operation."""

    auth = authenticate(request, operation=operation)
    actual_session = session_id or str(uuid4())
    return auth.scope, InvocationContext(
        session_id=actual_session,
        caller_trust_level=auth.trust,
        caller_id=auth.principal["caller_id"],
    )


def _put(value: Any, *, scope: Mapping[str, str], session_id: str) -> str:
    return str(_agent().config["services"].payload_store.put(value, scope=scope, session_id=session_id))


def _put_tracked(
    value: Any,
    refs: list[str],
    *,
    scope: Mapping[str, str],
    session_id: str,
) -> str:
    ref = _put(value, scope=scope, session_id=session_id)
    refs.append(ref)
    return ref


def _delete_refs(refs: list[str], *, scope: Mapping[str, str], session_id: str) -> None:
    store = _agent().config["services"].payload_store
    for ref in refs:
        try:
            store.delete(ref, scope=scope, session_id=session_id)
        except ValueError:
            # Resolution may already have tombstoned the value. Deletion is a
            # best-effort finalizer, and no store exception text reaches output.
            continue


def _invoke(envelope: dict[str, Any], ctx: InvocationContext) -> dict[str, Any]:
    with bound_secrets(_agent()._secrets_provider):
        result = _agent().invoke(json.dumps(envelope, ensure_ascii=False, sort_keys=True), ctx=ctx)
    return dict(result)


def _write_result(result: dict[str, Any]) -> dict[str, Any] | JSONResponse:
    if result.get("status") != "error":
        return result
    # Graph errors may contain framework traces; never expose them.  The safe
    # domain code is returned through the documented HTTP error envelope.
    return JSONResponse(status_code=422, content={"error": {"code": "request_rejected", "message": "Request was rejected.", "detail": {}}})


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request) -> dict[str, Any]:
    scope, ctx = _scope_and_context(request, req.session_id, Operation.READ_INVOKE)
    if len(req.exceptions) > int(_agent().config["max_exceptions_per_invoke"]):
        raise HTTPException(status_code=422, detail="exceptions exceed configured maximum")
    refs: list[str] = []
    try:
        exceptions: list[dict[str, Any]] = []
        for exception_item in req.exceptions:
            data = exception_item.model_dump(exclude={"operational_note"})
            if exception_item.operational_note is not None:
                data["operational_note_ref"] = _put_tracked(
                    exception_item.operational_note,
                    refs,
                    scope=scope,
                    session_id=ctx.session_id,
                )
            exceptions.append(data)
        notices: list[dict[str, Any]] = []
        for notice_item in req.revision_notices:
            data = notice_item.model_dump(exclude={"body"})
            data["body_ref"] = _put_tracked(
                notice_item.body,
                refs,
                scope=scope,
                session_id=ctx.session_id,
            )
            notices.append(data)
        # The Cat2 state represents one exception card.  The public batch contract
        # invokes that workflow once per accepted exception.
        cards: list[dict[str, Any]] = []
        rejected: list[dict[str, str]] = []
        degradations: list[str] = []
        for exception in exceptions:
            response = _invoke(
                {
                    "schema": "ene-c2-123/v1",
                    "mode": "invoke",
                    "business_unit_id": scope["business_unit_id"],
                    "customer_ref": getattr(request.state, "customer_ref", ""),
                    "payload": {"exceptions": [exception], "revision_notices": notices},
                },
                ctx,
            )
            output = response.get("output")
            if response.get("status") != "success" or not isinstance(output, dict):
                rejected.append(
                    {
                        "exception_id": exception["exception_id"],
                        "reason_code": _invoke_reason_code(response),
                    }
                )
                continue
            returned = output.get("cards", [])
            if isinstance(returned, list):
                cards.extend(item for item in returned if isinstance(item, dict))
            reason = output.get("degradation_reason")
            if isinstance(reason, str) and reason:
                degradations.append(reason)
        status = "success" if not rejected else "partial" if cards else "error"
        return {
            "output": {
                "mode": "invoke",
                "cards": cards,
                "rejected": rejected,
                "degradation_reason": "+".join(sorted(set(degradations))) or None,
            },
            "status": status,
        }
    finally:
        _delete_refs(refs, scope=scope, session_id=ctx.session_id)


def _invoke_reason_code(response: Mapping[str, Any]) -> str:
    """Map graph failure to a safe S-4-compatible code without echoing logs."""

    errors = response.get("error_log")
    text = " ".join(str(item) for item in errors) if isinstance(errors, list) else ""
    if "payload" in text:
        return "payload_unresolved"
    if "s3" in text.lower() or "security" in text.lower():
        return "s3_gate_rejected"
    return "intake_rejected"


@app.post("/ingest", response_model=None)
async def ingest(req: IngestRequest, request: Request) -> dict[str, Any] | JSONResponse:
    scope, ctx = _scope_and_context(request, req.session_id, Operation.WRITE_INGEST)
    categories = set(_agent().config.get("exception_categories", []))
    invalid = [index for index, item in enumerate(req.adjudications) if item.exception_category not in categories]
    if invalid:
        return JSONResponse(status_code=422, content={"error": {"code": "ingest_row_rejected", "message": "One or more rows were rejected.", "detail": {}}, "ingest_summary": {"written_ids": [], "rejected": [{"row_index": index, "reason_code": "ledger_classification_invalid", "detail": "row rejected"} for index in invalid], "lifecycle_transitions": []}})
    refs: list[str] = []
    try:
        rows: list[dict[str, Any]] = []
        for item in req.adjudications:
            data = item.model_dump(exclude={"rationale", "operational_note"})
            data["decided_by"] = ctx.caller_id
            data["rationale_ref"] = _put_tracked(
                item.rationale,
                refs,
                scope=scope,
                session_id=ctx.session_id,
            )
            if item.operational_note is not None:
                data["operational_note_ref"] = _put_tracked(
                    item.operational_note,
                    refs,
                    scope=scope,
                    session_id=ctx.session_id,
                )
            rows.append(data)
        return _write_result(
            _invoke(
                {
                    "schema": "ene-c2-123/v1",
                    "mode": "ingest",
                    "business_unit_id": scope["business_unit_id"],
                    "customer_ref": getattr(request.state, "customer_ref", ""),
                    "payload": {
                        "adjudications": rows,
                        "series_meta": [item.model_dump() for item in req.series_meta],
                    },
                },
                ctx,
            )
        )
    finally:
        _delete_refs(refs, scope=scope, session_id=ctx.session_id)


@app.post("/feedback", response_model=None)
async def feedback(req: FeedbackAdjudicationRequest | FeedbackCorrectionRequest, request: Request) -> dict[str, Any] | JSONResponse:
    scope, ctx = _scope_and_context(request, req.session_id, Operation.WRITE_FEEDBACK)
    refs: list[str] = []
    try:
        data = req.model_dump(exclude={"rationale", "session_id"})
        if data["kind"] == "adjudication":
            data["rationale_ref"] = _put_tracked(
                req.rationale,
                refs,
                scope=scope,
                session_id=ctx.session_id,
            )
        else:
            correction = {
                "record_id": data.pop("record_id"),
                "feedback_seq": str(data.pop("feedback_seq")),
                "verdict_code": data.pop("verdict_code"),
                "rationale": req.rationale,
                "decided_by": ctx.caller_id,
                "decided_at": data.pop("decided_at"),
            }
            data["feedback_payload_ref"] = _put_tracked(
                json.dumps(correction),
                refs,
                scope=scope,
                session_id=ctx.session_id,
            )
        return _write_result(
            _invoke(
                {
                    "schema": "ene-c2-123/v1",
                    "mode": "feedback",
                    "business_unit_id": scope["business_unit_id"],
                    "customer_ref": getattr(request.state, "customer_ref", ""),
                    "payload": data,
                },
                ctx,
            )
        )
    finally:
        _delete_refs(refs, scope=scope, session_id=ctx.session_id)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "agent": "supply_data_quality_precedent"}
