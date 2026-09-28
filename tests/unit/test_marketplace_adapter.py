"""Unit contracts for the AGENTIC STAR Marketplace entry adapter."""

from __future__ import annotations

import json
import sys
import types
from typing import Any

import pytest

import cli
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.base import MissingSecret, NullProvider
from framework.secrets.context import bound_secrets
from src.graph.graph import SupplyDataQualityPrecedentGraph


@pytest.fixture(autouse=True)
def marketplace_environment(monkeypatch):
    values = {
        "STG_DEFAULT_BUSINESS_UNIT_ID": "unit-marketplace",
        "AZURE_OPENAI_API_KEY": "key-a",
        "AZURE_OPENAI_ENDPOINT": "https://azure.example.invalid",
        "AZURE_OPENAI_DEPLOYMENT": "deployment-a",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def _request(**exception_overrides: Any) -> str:
    exception = {
        "exception_id": "EX-MARKETPLACE",
        "exception_category": "missing",
        "series_id": "SERIES-MARKETPLACE",
        "detected_at": "2026-08-01T00:00:00+00:00",
        "metrics": {},
    }
    exception.update(exception_overrides)
    return json.dumps({"exceptions": [exception], "revision_notices": []})


def _capture_parent_invoke(monkeypatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def fake_invoke(self, user_input, session_id="", ctx=None, input_context=None):
        del self
        captured.update(
            user_input=user_input,
            session_id=session_id,
            ctx=ctx,
            input_context=input_context,
        )
        return {"status": "success"}

    monkeypatch.setattr(SupplyDataQualityPrecedentGraph, "invoke", fake_invoke)
    return captured


def test_invoke_rebuilds_context_forces_trust_and_builds_canonical_read_envelope(monkeypatch):
    captured = _capture_parent_invoke(monkeypatch)
    agent = cli.MarketplaceGraph(config={})
    runner_ctx = InvocationContext(
        caller_id="marketplace-caller",
        caller_trust_level=TrustLevel.INTERNAL,
        hitl_allowed=True,
    )

    result = agent.invoke(
        _request(),
        ctx=runner_ctx,
        input_context={
            "conversation_history": ["unscanned text"],
            "business_unit_id": "attacker-unit",
            "mode": "feedback",
        },
    )

    assert result == {"status": "success"}
    assert captured["input_context"] == {"business_unit_id": "unit-marketplace"}
    envelope = json.loads(captured["user_input"])
    assert envelope["schema"] == "ene-c2-123/v1"
    assert envelope["mode"] == "invoke"
    assert envelope["business_unit_id"] == "unit-marketplace"
    assert envelope["payload"]["exceptions"][0]["exception_id"] == "EX-MARKETPLACE"
    assert captured["ctx"].caller_id == "marketplace-caller"
    assert captured["ctx"].caller_trust_level is TrustLevel.VERIFIED_EXTERNAL
    assert captured["ctx"].hitl_allowed is False


def test_scope_is_built_data_driven_from_entry_adapter_declarations(monkeypatch):
    monkeypatch.setattr(cli, "SCOPE_KEYS", ("business_unit_id", "region_id"))
    monkeypatch.setattr(
        cli,
        "STG_DEFAULT_SCOPE_ENVS",
        {
            "business_unit_id": "STG_DEFAULT_BUSINESS_UNIT_ID",
            "region_id": "STG_DEFAULT_REGION_ID",
        },
    )
    monkeypatch.setenv("STG_DEFAULT_REGION_ID", "east")
    captured = _capture_parent_invoke(monkeypatch)

    agent = cli.MarketplaceGraph(config={})
    agent.invoke(_request())

    assert captured["input_context"] == {
        "business_unit_id": "unit-marketplace",
        "region_id": "east",
    }


@pytest.mark.parametrize("invalid", [None, "", "   ", "bad value", "bad\nvalue", "bad\tvalue"])
def test_scope_rejects_unset_empty_and_whitespace_at_startup(monkeypatch, invalid):
    if invalid is None:
        monkeypatch.delenv("STG_DEFAULT_BUSINESS_UNIT_ID", raising=False)
    else:
        monkeypatch.setenv("STG_DEFAULT_BUSINESS_UNIT_ID", invalid)

    with pytest.raises((MissingSecret, ValueError)) as error:
        cli.MarketplaceGraph(config={})

    assert "STG_DEFAULT_BUSINESS_UNIT_ID" in str(error.value)


def test_scope_declaration_mismatch_fails_at_startup(monkeypatch):
    monkeypatch.setattr(cli, "SCOPE_KEYS", ("business_unit_id",))
    monkeypatch.setattr(cli, "STG_DEFAULT_SCOPE_ENVS", {})

    with pytest.raises(RuntimeError, match="exactly match"):
        cli.MarketplaceGraph(config={})


@pytest.mark.parametrize("env_name", cli._AZURE_OPENAI_SECRET_KEYS)
def test_provision_secrets_rejects_missing_azure_value_at_startup(monkeypatch, env_name):
    monkeypatch.delenv(env_name)
    agent = cli.MarketplaceGraph(config={})
    agent.compile()

    with pytest.raises((MissingSecret, ValueError)) as error:
        agent.provision_secrets(NullProvider())

    assert env_name in str(error.value)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://example.cognitiveservices.azure.com/openai/responses?api-version=2025-04-01-preview",
        "https://example.services.ai.azure.example/openai",
        "https://example.services.ai.azure.example/v1",
        "https://example.services.ai.azure.example?api-version=2024-10-21",
        "example.services.ai.azure.example",
    ],
    ids=["api-path", "openai-path", "base-path", "query-only", "missing-scheme"],
)
def test_unusable_azure_endpoint_shape_fails_during_startup(monkeypatch, endpoint):
    """AzureOpenAIClient appends its own path, so anything but a bare endpoint breaks.

    The resulting error surfaces inside complete(); a node that swallows it
    degrades silently while the invocation still reports status: success, so the
    adapter has to reject the shape at startup instead.
    """

    agent = cli.MarketplaceGraph(config={})
    agent.compile()
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "gate-test-key")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gate-test-deployment")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", endpoint)

    with pytest.raises(ValueError, match="bare"):
        agent.provision_secrets(NullProvider())


def test_allowlisted_provider_hides_undeclared_environment(monkeypatch):
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-leak")
    assert cli._AllowlistedEnvironmentProvider().get("UNRELATED_SECRET") is None


def test_azure_llm_creates_fresh_client_and_resolves_connection_per_call(monkeypatch):
    constructed: list[dict[str, str]] = []

    class RecordingAzureClient:
        def __init__(self, config):
            constructed.append(dict(config))
            self.config = config

        def complete(self, messages):
            assert messages == [{"role": "user", "content": "compare"}]
            return {"content": self.config["api_key"]}

    fake_module = types.ModuleType("shared.services.llm.azure_openai_client")
    fake_module.AzureOpenAIClient = RecordingAzureClient
    monkeypatch.setitem(sys.modules, fake_module.__name__, fake_module)

    llm = cli._AzureMarketplaceLLM()
    with bound_secrets(cli._AllowlistedEnvironmentProvider()):
        assert llm.complete([{"role": "user", "content": "compare"}]) == {"content": "key-a"}
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", "key-b")
        assert llm.complete([{"role": "user", "content": "compare"}]) == {"content": "key-b"}

    assert [config["api_key"] for config in constructed] == ["key-a", "key-b"]
    assert not hasattr(llm, "_client")
    assert not hasattr(llm, "_cached_client")


@pytest.mark.parametrize("mode", ["ingest", "feedback"])
def test_marketplace_rejects_write_modes_before_graph_invocation(monkeypatch, mode):
    captured = _capture_parent_invoke(monkeypatch)
    agent = cli.MarketplaceGraph(config={})

    with pytest.raises(ValueError, match="read-only invoke"):
        agent.invoke(json.dumps({"mode": mode, "exceptions": []}))

    assert captured == {}


def test_compile_wires_azure_without_replacing_precedent_storage():
    original = SupplyDataQualityPrecedentGraph()
    ledger = original.config["services"].ledger
    ledger.write(
        {"business_unit_id": "unit-marketplace"},
        {
            "record_kind": "adjudication",
            "classification": {"exception_category": "missing"},
            "series_id": "SERIES-MARKETPLACE",
            "operational_note": "Prior operational note with sufficient detail.",
            "occurred_at": "2026-01-01T00:00:00+00:00",
            "valid_to": None,
            "verdict_code": "accepted",
            "rationale": "Prior human rationale with sufficient detail.",
            "decided_by": "reviewer",
            "decided_at": "2026-01-02T00:00:00+00:00",
            "decision_source": "human",
            "retrieval_text": "missing SERIES-MARKETPLACE",
            "provenance": {},
        },
    )

    agent = cli.MarketplaceGraph(config={"services": original.config["services"]})
    agent.compile()

    assert agent.config["services"].ledger is ledger
    assert isinstance(agent.config["services"].llm, cli._AzureMarketplaceLLM)


def test_full_invoke_keeps_historical_verdict_visible_and_never_emits_current_verdict():
    agent = cli.MarketplaceGraph(config={})
    agent.config["services"].ledger.write(
        {"business_unit_id": "unit-marketplace"},
        {
            "record_kind": "adjudication",
            "classification": {"exception_category": "missing"},
            "series_id": "SERIES-MARKETPLACE",
            "operational_note": "Prior operational note with sufficient detail.",
            "occurred_at": "2026-01-01T00:00:00+00:00",
            "valid_to": None,
            "verdict_code": "accepted",
            "rationale": "Prior human rationale with sufficient detail.",
            "decided_by": "reviewer",
            "decided_at": "2026-01-02T00:00:00+00:00",
            "decision_source": "human",
            "retrieval_text": "missing SERIES-MARKETPLACE",
            "provenance": {},
        },
    )
    agent.compile()
    agent.provision_secrets(NullProvider())

    with bound_secrets(agent._secrets_provider):
        result = agent.invoke(_request(operational_note="Current exception note."))

    card = result["output"]["cards"][0]
    assert result["status"] == "success"
    assert card["precedent_facts"][0]["verdict_code"] == "accepted"
    assert "current_verdict" not in card
    assert "confirmed_verdict_code" not in card


def test_marketplace_main_uses_agentcore_101_config_loader_fallback(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_runner(agent_cls, **kwargs):
        captured.update(agent_cls=agent_cls, **kwargs)

    monkeypatch.setattr(cli, "run_agent_marketplace", fake_runner)
    cli.main()

    assert captured["agent_cls"] is cli.MarketplaceGraph
    assert captured["agent_name"] == "ENE-C2-123"
    assert captured["config"]["candidate_top_k"] == 8
