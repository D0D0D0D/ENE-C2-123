"""Injected runtime seams for customer KB, transient payloads, and receipts."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from src.feedback_intake import InMemoryFeedbackReceiptStore
from src.payload_store import PayloadStore
from src.services.adjudication_ledger import make_ledger
from src.services.sqlite_ledger_backend import SQLiteLedgerBackend


class RuntimeServices:
    """Container assembled at startup; nodes never create external clients."""

    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        runtime_config = yaml.safe_load((Path(__file__).parents[2] / "config" / "config.yaml").read_text()) or {}
        values = dict(runtime_config)
        values.update(config or {})
        provided_ledger = values.get("ledger")
        if provided_ledger is None:
            self.ledger_backend = values.get("ledger_backend") or SQLiteLedgerBackend()
            self.ledger = make_ledger(values, backend=self.ledger_backend)
        else:
            self.ledger_backend = values.get("ledger_backend")
            self.ledger = provided_ledger
        self._ledger_batch_writer = values.get("ledger_batch_writer")
        self.payload_store = values.get("payload_store") or PayloadStore(
            scope_keys=("business_unit_id",), ttl_seconds=int(values.get("payload_ttl_seconds", 900))
        )
        self.receipt_store = values.get("receipt_store") or InMemoryFeedbackReceiptStore()
        self.llm = values.get("llm")
        self._configuration = MappingProxyType(
            {
                "payload_ttl_seconds": int(values.get("payload_ttl_seconds", 900)),
                "exception_categories": tuple(values.get("exception_categories", ())),
                "verdict_codes": tuple(values.get("verdict_codes", ())),
                "llm": self.llm,
            }
        )

    @property
    def configuration(self) -> Mapping[str, Any]:
        """Service-affecting values fixed when this live container was built."""

        return self._configuration

    def write_ledger_batch(
        self,
        scope: Mapping[str, str],
        rows: list[dict[str, Any]],
        *,
        state: dict[str, Any],
    ) -> list[str]:
        """Persist a prepared batch through one template-owned transaction boundary."""

        try:
            if callable(self._ledger_batch_writer):
                written_ids = self._ledger_batch_writer(scope, rows, state)
            else:
                transaction = getattr(self.ledger_backend, "run_atomically", None)
                if not callable(transaction):
                    raise RuntimeError("atomic_ledger_batch_writer_not_configured")
                written_ids = transaction(
                    lambda: [self.ledger.write(scope, row, state=state)["record_id"] for row in rows]
                )
        except Exception as exc:
            # Backend/provider exceptions are untrusted free text. Preserve only
            # a stable code in graph state and chain the type for local debugging.
            raise RuntimeError("atomic_ledger_batch_failed") from exc
        if (
            not isinstance(written_ids, list)
            or len(written_ids) != len(rows)
            or any(not isinstance(record_id, str) or not record_id for record_id in written_ids)
        ):
            raise RuntimeError("atomic_ledger_batch_writer_returned_invalid_ids")
        return written_ids


def services_from_config(config: Mapping[str, Any]) -> RuntimeServices | None:
    services = config.get("services")
    return services if isinstance(services, RuntimeServices) else None
