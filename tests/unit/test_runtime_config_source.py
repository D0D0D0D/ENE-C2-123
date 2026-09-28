"""Contract: runtime parameters come from config/config.yaml, not the manifest.

AgentRegistry 準拠の static manifest への移行で `agent.config` ブロックは廃止し、
runtime 値は `config/config.yaml` へ移した。この移行で最も危険なのは
**読み手を向け直し忘れても例外にならない**ことで、`manifest.get("agent", {}).get("config", {})`
は静かに `{}` を返し、全パラメータが既定値に落ちたまま全テストが緑になりうる。

ここでは「実際に構築したグラフ / レッジャが config.yaml の値を持っていること」を
値で固定して、その silent degradation を検知する。
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

import src.graph.graph as graph_module
from src.graph import SupplyDataQualityPrecedentGraph
from src.services.adjudication_ledger import ledger_config
from src.services.runtime import RuntimeServices

_CONFIG_PATH = pathlib.Path(__file__).parents[2] / "config" / "config.yaml"
_MANIFEST_PATH = pathlib.Path(__file__).parents[2] / "config" / "agent.yaml"

_PREVIOUSLY_DEAD_KEYS = (
    "max_retry",
    "memory_enabled",
    "hitl",
    "candidate_top_k",
    "candidate_hard_cap",
    "conflict_min_distinct_verdicts",
    "conflict_min_minority_count",
    "priority_weight_verdict",
    "priority_weight_missing",
    "priority_weight_candidates",
    "priority_high_threshold",
    "priority_medium_threshold",
    "payload_ttl_seconds",
    "max_exceptions_per_invoke",
    "statement_max_chars",
    "verbatim_guard_chars",
    "min_span_chars",
)


def _runtime_config() -> dict:
    return yaml.safe_load(_CONFIG_PATH.read_text()) or {}


def test_manifest_carries_no_runtime_parameters() -> None:
    manifest = yaml.safe_load(_MANIFEST_PATH.read_text()) or {}
    assert "agent" not in manifest, "manifest must not re-introduce the legacy agent: nesting"
    assert "config" not in manifest, "runtime parameters belong in config/config.yaml"
    for key in ("max_retry", "timeout_seconds", "budget_usd", "memory_enabled", "hitl"):
        assert key not in manifest, f"{key} is a runtime parameter — config/config.yaml owns it"


def test_graph_reads_runtime_values_from_config_yaml(monkeypatch, tmp_path) -> None:
    """Every shipped runtime value, including former literals, stays effective."""
    runtime = _runtime_config()
    graph = SupplyDataQualityPrecedentGraph()
    for key in runtime:
        assert graph.config[key] == runtime[key], f"{key} did not come from config/config.yaml"

    sentinel = dict(runtime)
    sentinel["candidate_top_k"] = int(runtime["candidate_top_k"]) + 1
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(sentinel, sort_keys=False))
    monkeypatch.setattr(graph_module, "_CONFIG_PATH", config_path)
    assert SupplyDataQualityPrecedentGraph().config["candidate_top_k"] == sentinel["candidate_top_k"]


def test_file_values_override_code_defaults(monkeypatch, tmp_path) -> None:
    runtime = _runtime_config()
    runtime.update(
        {
            "max_retry": 1,
            "memory_enabled": True,
            "hitl": {"enabled": True, "max_hitl": 2},
            "candidate_top_k": 6,
            "candidate_hard_cap": 18,
            "conflict_min_distinct_verdicts": 3,
            "conflict_min_minority_count": 2,
            "priority_weight_verdict": 7,
            "priority_weight_missing": 5,
            "priority_weight_candidates": 4,
            "priority_high_threshold": 19,
            "priority_medium_threshold": 11,
            "payload_ttl_seconds": 321,
            "max_exceptions_per_invoke": 17,
            "statement_max_chars": 222,
            "verbatim_guard_chars": 44,
            "min_span_chars": 12,
        }
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(runtime, sort_keys=False))
    monkeypatch.setattr(graph_module, "_CONFIG_PATH", config_path, raising=False)

    graph = SupplyDataQualityPrecedentGraph()

    for key in _PREVIOUSLY_DEAD_KEYS:
        assert graph.config[key] == runtime[key]


def test_constructor_values_override_file(monkeypatch, tmp_path) -> None:
    runtime = _runtime_config()
    runtime["candidate_top_k"] = 5
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(runtime, sort_keys=False))
    monkeypatch.setattr(graph_module, "_CONFIG_PATH", config_path, raising=False)

    graph = SupplyDataQualityPrecedentGraph({"candidate_top_k": 13})

    assert graph.config["candidate_top_k"] == 13


def test_prebuilt_services_reconcile_service_owned_settings() -> None:
    llm = object()
    cases = (
        ("payload_ttl_seconds", 1),
        ("exception_categories", ["custom-category"]),
        ("verdict_codes", ["custom-verdict"]),
        ("llm", llm),
    )
    for key, service_value in cases:
        services = RuntimeServices({key: service_value})
        graph = SupplyDataQualityPrecedentGraph({"services": services})

        if key == "llm":
            assert graph.config[key] is service_value
        else:
            assert graph.config[key] == service_value
    assert SupplyDataQualityPrecedentGraph(
        {"services": RuntimeServices({"payload_ttl_seconds": 1})}
    ).config["services"].payload_store._ttl_seconds == 1


def test_conflicting_service_affecting_override_is_rejected() -> None:
    cases = (
        ("payload_ttl_seconds", 1, 999),
        ("exception_categories", ["service-category"], ["graph-category"]),
        ("verdict_codes", ["service-verdict"], ["graph-verdict"]),
        ("llm", object(), object()),
    )
    for key, service_value, conflicting_value in cases:
        services = RuntimeServices({key: service_value})
        with pytest.raises(ValueError, match=key):
            SupplyDataQualityPrecedentGraph(
                {"services": services, key: conflicting_value}
            )


def test_prebuilt_services_do_not_construct_discarded_container(monkeypatch) -> None:
    services = RuntimeServices()

    def unexpected_construction(config):
        raise AssertionError("RuntimeServices was eagerly constructed")

    monkeypatch.setattr(graph_module, "RuntimeServices", unexpected_construction)
    graph = SupplyDataQualityPrecedentGraph({"services": services})

    assert graph.config["services"] is services


def test_ledger_vocabulary_comes_from_config_yaml() -> None:
    runtime = _runtime_config()
    contract = ledger_config()
    assert contract.classification_codes["exception_category"] == frozenset(runtime["exception_categories"])
    assert contract.verdict_codes == frozenset(runtime["verdict_codes"])
