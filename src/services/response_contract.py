"""Single source of truth for the LLM reconciliation response contract."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from framework.errors import SecurityViolationError

from src.evidence_gate import FindingField, project_typed_finding


SUPPORT_STATUSES = ("supports_application", "blocks_application", "insufficient", "unknown")

CLAIM_FIELDS = (
    FindingField("claim_id", str, required=True),
    FindingField("precedent_id", str, required=True),
    FindingField("support_status", str, required=True, allowed_values=SUPPORT_STATUSES),
    FindingField("statement", str, required=True),
    FindingField("evidence_ids", list, required=True, item_type=str),
)
MISSING_INFORMATION_FIELDS = (
    FindingField("item_id", str, required=True),
    FindingField("requirement_id", str, required=True),
    FindingField("statement", str, required=True),
    FindingField("evidence_ids", list, required=True, item_type=str),
)
CONFIRMATION_QUESTION_FIELDS = (
    FindingField("question_id", str, required=True),
    FindingField("statement", str, required=True),
    FindingField("evidence_ids", list, required=True, item_type=str),
)

SECTION_FIELDS: dict[str, tuple[FindingField, ...]] = {
    "claims": CLAIM_FIELDS,
    "missing_information": MISSING_INFORMATION_FIELDS,
    "confirmation_questions": CONFIRMATION_QUESTION_FIELDS,
}


def response_contract(*, max_statement_chars: int) -> dict[str, Any]:
    """Generate the prompt-visible JSON contract from validation declarations."""

    return {
        "type": "object",
        "required_keys": list(SECTION_FIELDS),
        "additional_keys": False,
        "sections": {
            section: {
                "type": "array",
                "item": {
                    "type": "object",
                    "required_keys": [field.name for field in fields if field.required],
                    "additional_keys": False,
                    "fields": {field.name: _field_contract(field) for field in fields},
                },
            }
            for section, fields in SECTION_FIELDS.items()
        },
        "statement_max_chars": max_statement_chars,
    }


def project_response_draft(value: Any, *, max_statement_chars: int) -> dict[str, list[dict[str, Any]]]:
    """Fail closed and return a newly projected, typed reconciliation draft."""

    if not isinstance(value, Mapping) or set(value) != set(SECTION_FIELDS):
        raise SecurityViolationError("S-3 reconciliation response contract violation")
    projected: dict[str, list[dict[str, Any]]] = {}
    for section, fields in SECTION_FIELDS.items():
        items = value[section]
        if not isinstance(items, list):
            raise SecurityViolationError("S-3 reconciliation response section must be list")
        projected[section] = [project_typed_finding(item, fields) for item in items]
    for items in projected.values():
        for item in items:
            statement = item.get("statement")
            if isinstance(statement, str) and len(statement) > max_statement_chars:
                raise SecurityViolationError("S-3 reconciliation statement exceeds declared limit")
    return projected


def field_names(fields: tuple[FindingField, ...]) -> frozenset[str]:
    return frozenset(field.name for field in fields)


def _field_contract(field: FindingField) -> dict[str, Any]:
    contract: dict[str, Any] = {"type": _json_type(field.expected_type)}
    if field.item_type is not None:
        contract["items"] = {"type": _json_type(field.item_type)}
    if field.allowed_values:
        contract["enum"] = list(field.allowed_values)
    return contract


def _json_type(declared: type | tuple[type, ...]) -> str | list[str]:
    declared_types = declared if isinstance(declared, tuple) else (declared,)
    names = [_JSON_TYPES.get(item, item.__name__) for item in declared_types]
    return names[0] if len(names) == 1 else names


_JSON_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean", list: "array", dict: "object"}
