"""PB-1 verifies domain events, not framework lifecycle events."""

from __future__ import annotations

from src.nodes.pre_process_node import IntakeNode
from src.services.runtime import RuntimeServices


def test_pb_audit_intake_rejection_emits_domain_event(monkeypatch):
    events = []
    monkeypatch.setattr("src.nodes.pre_process_node.emit_trace_event", lambda event, payload, state: events.append(event))
    node = IntakeNode(config={"services": RuntimeServices()})
    result = node._extra_security_gate_input({"user_input": "not-json"})
    assert result["status"] == "error"
    assert events == ["intake_rejected"]
