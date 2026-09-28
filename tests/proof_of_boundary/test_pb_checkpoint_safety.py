"""PB-5 uses a real checkpointer and restored state, not repr inspection."""

from __future__ import annotations

import json

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from langgraph.checkpoint.memory import InMemorySaver

from src.graph.graph import SupplyDataQualityPrecedentGraph


def _strings(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, str):
        yield value


def test_pb_checkpoint_restores_without_free_text_sentinels():
    graph = SupplyDataQualityPrecedentGraph({"memory_enabled": True})
    saver = InMemorySaver()
    graph.compile(checkpointer=saver)
    services = graph.config["services"]
    scope, session = {"business_unit_id": "unit-a"}, "checkpoint-session"
    ref = services.payload_store.put("PRIVATE-BODY-SENTINEL alice@example.com", scope=scope, session_id=session)
    envelope = {"schema": "ene-c2-123/v1", "mode": "invoke", "business_unit_id": "unit-a", "payload": {"exceptions": [{"exception_id": "EX-1", "exception_category": "missing", "series_id": "S1", "detected_at": "2026-01-01T00:00:00+00:00", "metrics": {}, "operational_note_ref": ref}], "revision_notices": []}}
    context = InvocationContext(session_id=session, thread_id="checkpoint-thread", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
    graph.invoke(json.dumps(envelope), ctx=context)
    restored = graph._compiled.get_state({"configurable": {"thread_id": "checkpoint-thread"}}).values
    assert restored
    assert all("PRIVATE-BODY-SENTINEL" not in value and "alice@example.com" not in value for value in _strings(restored))
