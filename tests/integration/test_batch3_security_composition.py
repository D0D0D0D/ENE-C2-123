"""Batch 3 regressions through the production graph and HTTP composition."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from fastapi.testclient import TestClient
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.api import server
from src.graph.graph import SupplyDataQualityPrecedentGraph
from src.services.adjudication_ledger import make_ledger
from src.services.runtime import RuntimeServices
from src.services.sqlite_ledger_backend import SQLiteLedgerBackend


class _VerifiedIdentityMiddleware:
    def __init__(self, app, *, business_unit_id: str, caller_id: str) -> None:
        self._app = app
        self._business_unit_id = business_unit_id
        self._caller_id = caller_id

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            scope = dict(scope)
            state = dict(scope.get("state", {}))
            state.update(
                {
                    "trust_level": TrustLevel.VERIFIED_EXTERNAL,
                    "caller_id": self._caller_id,
                    "business_unit_id": self._business_unit_id,
                }
            )
            scope["state"] = state
        await self._app(scope, receive, send)


class _ContractLLM:
    def __init__(self, *, violation: str | None = None) -> None:
        self.prompt: dict[str, Any] | None = None
        self._violation = violation

    def complete(self, messages):
        self.prompt = json.loads(messages[0]["content"])
        evidence_id = self.prompt["evidence_ids"][0]
        precedent_id = self.prompt["precedent_facts"][0]["adjudication_id"]
        statement: Any = "The prior evidence requires human comparison."
        if self._violation == "nested_type":
            statement = {"raw": "PRIVATE-LLM-MARKER"}
        claim = {
            "claim_id": "C1",
            "precedent_id": precedent_id,
            "support_status": "unknown" if self._violation != "enum" else "invented",
            "statement": statement,
            "evidence_ids": [evidence_id],
        }
        if self._violation == "unknown_key":
            claim["raw_document"] = "PRIVATE-LLM-MARKER"
        return {
            "content": json.dumps(
                {
                    "claims": [claim],
                    "missing_information": [
                        {
                            "item_id": f"M{index + 1}",
                            "requirement_id": requirement_id,
                            "statement": "Additional information is required.",
                            "evidence_ids": [],
                        }
                        for index, requirement_id in enumerate(self.prompt["requirement_ids"])
                    ],
                    "confirmation_questions": [],
                }
            )
        }


class _FailingLLM:
    def complete(self, _messages):
        raise RuntimeError("PRIVATE-PROVIDER-EXCEPTION-MARKER")


class _FailSecondInsertBackend(SQLiteLedgerBackend):
    def __init__(self) -> None:
        super().__init__()
        self._insert_count = 0

    def insert(self, record):
        self._insert_count += 1
        if self._insert_count == 2:
            raise RuntimeError("PRIVATE-DATABASE-EXCEPTION-MARKER")
        return super().insert(record)


def _record(*, series_id: str, verdict: str, decided_at: str) -> dict[str, Any]:
    return {
        "record_kind": "adjudication",
        "classification": {"exception_category": "missing"},
        "series_id": series_id,
        "operational_note": "A sufficiently detailed operational note for retrieval.",
        "occurred_at": "2026-01-01T00:00:00+00:00",
        "valid_to": None,
        "verdict_code": verdict,
        "rationale": "A sufficiently detailed rationale for a prior human decision.",
        "decided_by": "reviewer",
        "decided_at": decided_at,
        "decision_source": "human",
        "retrieval_text": f"missing {series_id}",
        "provenance": {},
    }


def _invoke_body(*, business_unit_id: str, series_id: str = "SERIES-A") -> str:
    return json.dumps(
        {
            "schema": "ene-c2-123/v1",
            "mode": "invoke",
            "business_unit_id": business_unit_id,
            "customer_ref": "customer-a",
            "payload": {
                "exceptions": [
                    {
                        "exception_id": "EX-1",
                        "exception_category": "missing",
                        "series_id": series_id,
                        "detected_at": "2026-02-01T00:00:00+00:00",
                        "metrics": {},
                    }
                ],
                "revision_notices": [],
            },
        }
    )


def _ctx(*, session_id: str = "batch3-session") -> InvocationContext:
    return InvocationContext(
        session_id=session_id,
        caller_id="reviewer",
        caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
    )


def _seed_conflict(graph: SupplyDataQualityPrecedentGraph, scope: dict[str, str]) -> None:
    graph.config["services"].ledger.write(
        scope,
        _record(series_id="SERIES-A", verdict="accepted", decided_at="2026-01-02T00:00:00+00:00"),
    )
    graph.config["services"].ledger.write(
        scope,
        _record(series_id="SERIES-A", verdict="rejected", decided_at="2026-01-03T00:00:00+00:00"),
    )


def _http_adjudication() -> dict[str, Any]:
    return {
        "exception_category": "missing",
        "series_id": "SERIES-HTTP",
        "occurred_at": "2026-01-01T00:00:00+00:00",
        "valid_to": None,
        "verdict_code": "accepted",
        "decided_at": "2026-01-02T00:00:00+00:00",
        "decision_source": "human",
        "rationale": "A sufficiently detailed HTTP-ingested rationale.",
        "operational_note": "A sufficiently detailed HTTP-ingested operational note.",
    }


def test_standalone_read_token_cannot_write_business_state(monkeypatch):
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "mock-entry-token")
    monkeypatch.setenv("STG_DEFAULT_BUSINESS_UNIT_ID", "unit-read-only")

    with TestClient(server.app) as client:
        response = client.post(
            "/ingest",
            headers={"authorization": "Bearer mock-entry-token"},
            json={"adjudications": [_http_adjudication()]},
        )
        rows = server._agent().config["services"].ledger.search(
            {"business_unit_id": "unit-read-only"}
        )

    assert response.status_code == 403
    assert rows == []


def test_verified_http_composition_writes_reads_and_deletes_payload_refs(monkeypatch):
    monkeypatch.delenv("INVOKE_AUTH_TOKEN", raising=False)
    app = _VerifiedIdentityMiddleware(
        server.app,
        business_unit_id="unit-http",
        caller_id="reviewer-http",
    )

    with TestClient(app) as client:
        ingest = client.post("/ingest", json={"adjudications": [_http_adjudication()]})
        invoke = client.post(
            "/invoke",
            json={
                "exceptions": [
                    {
                        "exception_id": "EX-HTTP",
                        "exception_category": "missing",
                        "series_id": "SERIES-HTTP",
                        "detected_at": "2026-02-01T00:00:00+00:00",
                        "metrics": {},
                    }
                ]
            },
        )
        store = server._agent().config["services"].payload_store

    assert ingest.status_code == 200
    assert invoke.status_code == 200
    assert invoke.json()["output"]["cards"][0]["eligibility"]["kind"] == "uniquely_determined"
    assert store._items == {}


def test_all_http_modes_delete_payloads_after_post_put_rejection(monkeypatch):
    monkeypatch.delenv("INVOKE_AUTH_TOKEN", raising=False)
    app = _VerifiedIdentityMiddleware(
        server.app,
        business_unit_id="unit-cold-path",
        caller_id="reviewer-cold-path",
    )

    with TestClient(app) as client:
        invoke = client.post(
            "/invoke",
            json={
                "exceptions": [
                    {
                        "exception_id": " ",
                        "exception_category": "missing",
                        "series_id": "SERIES-COLD",
                        "detected_at": "2026-02-01T00:00:00+00:00",
                        "metrics": {},
                        "operational_note": "PRIVATE-COLD-INVOKE-BODY",
                    }
                ]
            },
        )
        assert server._agent().config["services"].payload_store._items == {}

        ingest_payload = _http_adjudication()
        ingest_payload["rationale"] = "short"
        ingest = client.post("/ingest", json={"adjudications": [ingest_payload]})
        assert server._agent().config["services"].payload_store._items == {}

        feedback = client.post(
            "/feedback",
            json={
                "kind": "adjudication",
                "exception_id": "EX-COLD",
                "exception_category": "missing",
                "series_id": "SERIES-COLD",
                "detected_at": "2026-02-01T00:00:00+00:00",
                "confirmed_verdict_code": "accepted",
                "rationale": "PRIVATE-COLD-FEEDBACK-BODY with sufficient text.",
                "decided_at": "2026-02-02T00:00:00+00:00",
                "adopted_precedent_ids": ["unknown-record"],
            },
        )
        assert server._agent().config["services"].payload_store._items == {}

    assert invoke.status_code == 200
    assert invoke.json()["status"] == "error"
    assert ingest.status_code == 422
    assert feedback.status_code == 422


def test_real_sqlite_scope_projection_returns_one_hit_and_blocks_direct_id(tmp_path):
    backend = SQLiteLedgerBackend(str(tmp_path / "ledger.sqlite3"))
    ledger = make_ledger(backend=backend)
    scope_a = {"business_unit_id": "unit-a"}
    scope_b = {"business_unit_id": "unit-b"}
    written = ledger.write(
        scope_a,
        _record(series_id="SERIES-A", verdict="accepted", decided_at="2026-01-02T00:00:00+00:00"),
    )

    hits = ledger.search(
        scope_a,
        record_kind="adjudication",
        classification={"exception_category": "missing"},
    )

    assert [row["record_id"] for row in hits] == [written["record_id"]]
    assert ledger.search(scope_b) == []
    assert ledger.get(written["record_id"], scope_b) is None
    assert ledger.search(scope_a, classification={"exception_category": "duplicate"}) == []


def test_ingest_batch_rolls_back_first_row_when_second_database_write_fails():
    backend = _FailSecondInsertBackend()
    services = RuntimeServices({"ledger_backend": backend})
    graph = SupplyDataQualityPrecedentGraph({"services": services})
    scope = {"business_unit_id": "unit-atomic"}
    session_id = "atomic-session"
    rows = []
    for index in range(2):
        rationale_ref = services.payload_store.put(
            f"A sufficiently detailed rationale for batch row {index}.",
            scope=scope,
            session_id=session_id,
        )
        rows.append(
            {
                "exception_category": "missing",
                "series_id": f"SERIES-{index}",
                "occurred_at": "2026-01-01T00:00:00+00:00",
                "valid_to": None,
                "verdict_code": "accepted",
                "decided_at": "2026-01-02T00:00:00+00:00",
                "decision_source": "human",
                "rationale_ref": rationale_ref,
            }
        )
    body = json.dumps(
        {
            "schema": "ene-c2-123/v1",
            "mode": "ingest",
            "business_unit_id": scope["business_unit_id"],
            "payload": {"adjudications": rows, "series_meta": []},
        }
    )

    result = graph.invoke(body, ctx=_ctx(session_id=session_id))

    assert result["status"] == "error"
    assert backend.query(scope=scope) == []
    assert "PRIVATE-DATABASE-EXCEPTION-MARKER" not in json.dumps(result)


def test_prompt_contract_is_generated_and_valid_llm_output_reaches_customer_card():
    llm = _ContractLLM()
    graph = SupplyDataQualityPrecedentGraph({"llm": llm})
    scope = {"business_unit_id": "unit-contract"}
    _seed_conflict(graph, scope)

    result = graph.invoke(_invoke_body(business_unit_id=scope["business_unit_id"]), ctx=_ctx())

    assert result["status"] == "success"
    assert result["output"]["cards"][0]["claims"][0]["support_status"] == "unknown"
    assert llm.prompt is not None
    claim_contract = llm.prompt["response_contract"]["sections"]["claims"]["item"]
    assert claim_contract["additional_keys"] is False
    assert claim_contract["fields"]["support_status"]["enum"] == [
        "supports_application",
        "blocks_application",
        "insufficient",
        "unknown",
    ]
    assert llm.prompt["response_contract"]["statement_max_chars"] == 400


@pytest.mark.parametrize("violation", ["nested_type", "unknown_key", "enum"])
def test_typed_projection_rejects_invalid_llm_findings_through_full_graph(violation):
    graph = SupplyDataQualityPrecedentGraph({"llm": _ContractLLM(violation=violation)})
    scope = {"business_unit_id": "unit-malformed"}
    _seed_conflict(graph, scope)

    result = graph.invoke(_invoke_body(business_unit_id=scope["business_unit_id"]), ctx=_ctx())

    assert result["status"] == "error"
    assert "PRIVATE-LLM-MARKER" not in json.dumps(result)


def test_provider_exception_text_is_not_exposed_through_full_graph():
    graph = SupplyDataQualityPrecedentGraph({"llm": _FailingLLM()})
    scope = {"business_unit_id": "unit-provider-error"}
    _seed_conflict(graph, scope)

    result = graph.invoke(_invoke_body(business_unit_id=scope["business_unit_id"]), ctx=_ctx())

    assert result["status"] == "error"
    assert "PRIVATE-PROVIDER-EXCEPTION-MARKER" not in json.dumps(result)


def test_concurrent_feedback_retries_create_one_correction_through_full_graph():
    graph = SupplyDataQualityPrecedentGraph()
    services = graph.config["services"]
    scope = {"business_unit_id": "unit-feedback"}
    source = services.ledger.write(
        scope,
        _record(series_id="SERIES-F", verdict="accepted", decided_at="2026-01-02T00:00:00+00:00"),
    )
    session_id = "feedback-session"
    feedback_ref = services.payload_store.put(
        json.dumps(
            {
                "record_id": source["record_id"],
                "feedback_seq": "seq-1",
                "verdict_code": "rejected",
                "rationale": "A sufficiently detailed corrected rationale.",
                "decided_by": "reviewer",
                "decided_at": "2026-01-04T00:00:00+00:00",
            }
        ),
        scope=scope,
        session_id=session_id,
    )
    body = json.dumps(
        {
            "schema": "ene-c2-123/v1",
            "mode": "feedback",
            "business_unit_id": scope["business_unit_id"],
            "payload": {"kind": "correction", "feedback_payload_ref": feedback_ref},
        }
    )

    def invoke_feedback(_index: int) -> dict[str, Any]:
        return graph.invoke(body, ctx=_ctx(session_id=session_id))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(invoke_feedback, range(2)))

    written_ids = [result["output"]["feedback_summary"]["written_id"] for result in results]
    all_rows = services.ledger.search(
        scope,
        statuses=("active", "disputed", "superseded", "expired"),
        include_expired=True,
    )
    assert all(result["status"] == "success" for result in results)
    assert len(set(written_ids)) == 1
    assert len(all_rows) == 2


def test_feedback_precedent_id_from_other_scope_is_rejected_through_full_graph():
    graph = SupplyDataQualityPrecedentGraph()
    services = graph.config["services"]
    other = services.ledger.write(
        {"business_unit_id": "unit-other"},
        _record(series_id="SERIES-X", verdict="accepted", decided_at="2026-01-02T00:00:00+00:00"),
    )
    scope = {"business_unit_id": "unit-current"}
    session_id = "scope-negative-session"
    rationale_ref = services.payload_store.put(
        "A sufficiently detailed human feedback rationale.",
        scope=scope,
        session_id=session_id,
    )
    body = json.dumps(
        {
            "schema": "ene-c2-123/v1",
            "mode": "feedback",
            "business_unit_id": scope["business_unit_id"],
            "payload": {
                "kind": "adjudication",
                "exception_id": "EX-X",
                "exception_category": "missing",
                "series_id": "SERIES-X",
                "detected_at": "2026-02-01T00:00:00+00:00",
                "confirmed_verdict_code": "accepted",
                "rationale_ref": rationale_ref,
                "decided_at": "2026-02-02T00:00:00+00:00",
                "adopted_precedent_ids": [other["record_id"]],
                "rejected_precedent_ids": [],
                "decisive_difference_evidence_ids": [],
            },
        }
    )

    result = graph.invoke(body, ctx=_ctx(session_id=session_id))

    assert result["status"] == "error"
    assert services.ledger.search(scope) == []
