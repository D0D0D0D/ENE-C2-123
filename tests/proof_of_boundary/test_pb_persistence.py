"""PB-1/PB-3: persistence and injected service boundaries."""

from __future__ import annotations

from src.services.adjudication_ledger import make_ledger


def _record() -> dict:
    return {"record_kind": "adjudication", "classification": {"exception_category": "missing"}, "series_id": "S1", "operational_note": "Contact alice@example.com for operational details.", "occurred_at": "2026-01-01T00:00:00+00:00", "valid_to": None, "verdict_code": "accepted", "rationale": "Rationale with alice@example.com is masked for model use.", "decided_by": "reviewer", "decided_at": "2026-01-02T00:00:00+00:00", "decision_source": "human", "retrieval_text": "missing S1", "provenance": {}}


def test_pb_persistent_projection_scope_and_raw_retention_boundaries():
    ledger = make_ledger()
    scope_a, scope_b = {"business_unit_id": "unit-a"}, {"business_unit_id": "unit-b"}
    written = ledger.write(scope_a, _record())

    public = ledger.get(written["record_id"], scope_a)
    assert public is not None
    assert "alice@example.com" not in public["rationale_safe"]
    assert "alice@example.com" not in public["operational_note_safe"]
    assert "alice@example.com" not in public["retrieval_text"]
    assert ledger.get(written["record_id"], scope_b) is None
    assert ledger.search(scope_b) == []
    assert ledger.conflicts(scope_b) == []

    assert ledger.redact(written["record_id"], scope_a)
    backend_record = ledger._backend.get(written["record_id"], scope=scope_a)  # type: ignore[attr-defined]
    assert backend_record is not None and backend_record["rationale_raw"] is None
    # Purge also physically removes raw text for a new, non-redacted record.
    second = ledger.write(scope_a, _record())
    assert ledger.purge_expired_raw(scope_a, "2030-01-01T00:00:00+00:00") >= 1
    second_raw = ledger._backend.get(second["record_id"], scope=scope_a)  # type: ignore[attr-defined]
    assert second_raw is not None and second_raw["rationale_raw"] is None


def test_pb_external_ledger_seam_is_injected_not_constructed_by_node():
    class Backend:
        def __init__(self):
            self.rows = {}

        def insert(self, record):
            self.rows[record["record_id"]] = dict(record)
            return record["record_id"]

        def get(self, record_id, *, scope):
            row = self.rows.get(record_id)
            return dict(row) if row and row["scope"] == dict(scope) else None

        def update(self, record_id, *, scope, changes):
            row = self.get(record_id, scope=scope)
            if row is None:
                return None
            self.rows[record_id].update(dict(changes))
            return dict(self.rows[record_id])

        def query(self, *, scope):
            return [dict(row) for row in self.rows.values() if row["scope"] == dict(scope)]

        def purge_raw(self, *, scope, cutoff):
            return []
    backend = Backend()
    ledger = make_ledger(backend=backend)
    ledger.write({"business_unit_id": "unit-a"}, _record())
    assert backend.rows
