"""Local wiring for the reviewed accumulation-ledger part."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from src.accumulation_ledger import InMemoryLedgerBackend, LedgerConfig, LedgerService


def ledger_config(config: Mapping[str, Any] | None = None) -> LedgerConfig:
    """Build the declared ledger contract from deployment-provisioned code sets."""

    runtime_config = yaml.safe_load((Path(__file__).parents[2] / "config" / "config.yaml").read_text()) or {}
    values = dict(runtime_config)
    values.update(config or {})
    categories = frozenset(values.get("exception_categories", ()))
    verdicts = frozenset(values.get("verdict_codes", ()))
    if not categories or not verdicts:
        raise ValueError("ledger vocabulary must be supplied by config/config.yaml")
    return LedgerConfig(
        scope_keys=("business_unit_id",),
        record_kinds=frozenset({"adjudication"}),
        classification_codes={"exception_category": categories},
        verdict_codes=verdicts,
        conflict_key_fields=("series_id",),
        domain_fields={"series_id": "scalar", "operational_note": "text"},
        schema_version="1.0",
        supersede_mode="review_required",
        record_id_prefix="adj",
    )


def make_ledger(config: Mapping[str, Any] | None = None, *, backend: Any | None = None) -> LedgerService:
    return LedgerService(ledger_config(config), backend=backend or InMemoryLedgerBackend())
