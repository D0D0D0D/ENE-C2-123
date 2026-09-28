"""Shared fixtures for integration and manual LLM seam checks."""

from __future__ import annotations

import pytest

from src.payload_store import PayloadStore
from src.services import make_ledger


_LIVE_SCOPE = {"business_unit_id": "supply-operations-live"}
_LIVE_SESSION_ID = "live-1"
_LIVE_SERIES_ID = "gas-pressure-daily"


def _precedent(verdict: str, *, decided_at: str, rationale: str, note: str) -> dict:
    return {
        "record_kind": "adjudication",
        "classification": {"exception_category": "outlier"},
        "series_id": _LIVE_SERIES_ID,
        "operational_note": note,
        "occurred_at": "2026-01-01T00:00:00+00:00",
        "valid_to": None,
        "verdict_code": verdict,
        "rationale": rationale,
        "decided_by": "supply-data-reviewer",
        "decided_at": decided_at,
        "decision_source": "human",
        "retrieval_text": f"outlier {_LIVE_SERIES_ID}",
        "provenance": {},
    }


@pytest.fixture
def _live_runtime():
    ledger = make_ledger()
    ledger.write(
        _LIVE_SCOPE,
        _precedent(
            "accepted",
            decided_at="2026-02-02T00:00:00+00:00",
            rationale=(
                "A temporary pressure spike was accepted only after a publisher notice "
                "confirmed a maintenance-window recalculation."
            ),
            note="Check whether the current timestamp overlaps a declared maintenance window.",
        ),
    )
    ledger.write(
        _LIVE_SCOPE,
        _precedent(
            "rejected",
            decided_at="2026-03-03T00:00:00+00:00",
            rationale=(
                "A similar spike was rejected when telemetry calibration drift, not a "
                "publisher methodology change, explained the exception."
            ),
            note="Require calibration evidence and compare the notice effective date.",
        ),
    )

    payload_store = PayloadStore(scope_keys=("business_unit_id",), ttl_seconds=900)
    note_ref = payload_store.put(
        (
            "The daily supply-pressure value jumped 38 percent immediately after sensor "
            "maintenance; the operations log shows no matching physical flow change."
        ),
        scope=_LIVE_SCOPE,
        session_id=_LIVE_SESSION_ID,
    )
    revision_ref = payload_store.put(
        (
            "The publisher will change the pressure-series aggregation from hourly median "
            "to end-of-day maximum beginning 2026-09-01. Historical values are unchanged."
        ),
        scope=_LIVE_SCOPE,
        session_id=_LIVE_SESSION_ID,
    )
    return {
        "ledger": ledger,
        "payload_store": payload_store,
        "payload": {
            "schema": "ene-c2-123/v1",
            "mode": "invoke",
            **_LIVE_SCOPE,
            "customer_ref": "energy-customer-live",
            "payload": {
                "exceptions": [
                    {
                        "exception_id": "exception-pressure-aug",
                        "exception_category": "outlier",
                        "series_id": _LIVE_SERIES_ID,
                        "detected_at": "2026-08-12T00:00:00+00:00",
                        "metrics": {"observed_pressure": 138, "baseline_pressure": 100},
                        "operational_note_ref": note_ref,
                    }
                ],
                "revision_notices": [
                    {
                        "notice_id": "revision-pressure-sep",
                        "series_id": _LIVE_SERIES_ID,
                        "publisher": "regional-grid-operator",
                        "effective_date": "2026-09-01T00:00:00+00:00",
                        "body_ref": revision_ref,
                    }
                ],
            },
        },
    }


@pytest.fixture
def agent_config(_live_runtime) -> dict:
    """Seams used by RuntimeServices; the checker adds the real LLM later."""

    return {
        "ledger": _live_runtime["ledger"],
        "payload_store": _live_runtime["payload_store"],
        "memory_enabled": False,
        "hitl": {"enabled": False},
    }


@pytest.fixture
def case_payload(_live_runtime) -> dict:
    """Conflicting-precedent case that must take the LLM reconciliation route."""

    return _live_runtime["payload"]
