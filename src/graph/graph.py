"""AgentCore Platform v1.0"""

# ─────────────────────────────────────────────────────────────────────────────
# Template Category (Cat) — choose ONE based on your Cat judgment:
#
# Cat 1 — Single technical capability (use-case-agnostic)
#   Purpose : Delivers one reusable, domain-independent capability.
#             The same template can be dropped into any project unchanged.
#   Examples: TextSummarizer, EmbeddingGenerator, LanguageDetector,
#             SentimentAnalyzer, KeywordExtractor
#   Parent  : AgentBaseGraph
#   Pipeline: START → initialize → pre_process → main → {route} → post_process → finalize → END
#                                             ↓ (RETRY, max 3)
#                                          pre_process
#   See src/examples/graph_cat1_sample.py for a full example.
#
# Cat 2 — Multi-step domain workflow (job-to-be-done)
#   Purpose : Orchestrates multiple steps to accomplish a specific business
#             outcome. The template name describes the outcome, not the
#             individual capabilities it uses.
#   Examples: InvoiceTriageAgent, ContractGapDetectionAgent,
#             ResumeScreeningAgent, SupportTicketResolutionAgent
#   Parent  : AgentBaseGraph (outer graph) + GraphNode in the `main` slot
#             wrapping an inner BaseGraph/AgentBaseGraph (domain workflow)
#   Pipeline: Same fixed 5-node backbone as Cat 1; domain complexity is
#             encapsulated inside DomainWorkflowGraphNode.get_subgraph().
#   Layout  : src/graph/graph.py                 ← outer graph (this file)
#             src/graph/domain_workflow_graph.py ← inner graph
#   See src/examples/graph_cat2_sample.py and src/examples/domain_workflow_graph_sample.py for examples.
#
# Cat 3 — Autonomous think→act→observe loop (self-directed)
#   Purpose : Runs an LLM-driven loop that decides its own next action,
#             executes tools, observes results, and terminates when the task
#             is complete or a budget/iteration ceiling is hit.
#   Examples: ResearchAgent, AutonomousCodeReviewAgent,
#             DataExplorationAgent, MultiStepPlannerAgent
#   Parent  : AutonomousBaseGraph
#   Pipeline: START → initialize → think ⇄ act → finalize → END
#   Config  : budget_usd and llm are required in config.yaml.
#   See src/examples/graph_cat3_sample.py for a full example.
#
# ── How to choose ─────────────────────────────────────────────────────────────
#
#   Ask: "Does this template solve a specific business problem end-to-end?"
#     No  → Cat 1 (generic capability)
#     Yes → "Does it need an autonomous reasoning loop to decide its steps?"
#       No  → Cat 2 (fixed multi-step workflow)
#       Yes → Cat 3 (LLM-driven loop)
#
# framework.* imports are unchanged; agent-local imports use the src. prefix.
# ─────────────────────────────────────────────────────────────────────────────

from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

from framework.graph.agent_base_graph import AgentBaseGraph
from src.nodes.pre_process_node import IntakeNode
from src.nodes.main_node import PrecedentWorkflowGraphNode
from src.nodes.post_process_node import CardAssemblyNode
from src.schemas.state import State
from src.services.runtime import RuntimeServices


_CONFIG_PATH = Path(__file__).parents[2] / "config" / "config.yaml"

_CODE_DEFAULTS: dict[str, Any] = {
    "max_retry": 3,
    "memory_enabled": False,
    "hitl": {"enabled": False},
    "candidate_top_k": 8,
    "candidate_hard_cap": 24,
    "conflict_min_distinct_verdicts": 2,
    "conflict_min_minority_count": 1,
    "priority_weight_verdict": 2,
    "priority_weight_missing": 1,
    "priority_weight_candidates": 1,
    "priority_high_threshold": 4,
    "priority_medium_threshold": 2,
    "payload_ttl_seconds": 900,
    "max_exceptions_per_invoke": 50,
    "statement_max_chars": 400,
    "verbatim_guard_chars": 60,
    "min_span_chars": 8,
}

_SERVICE_AFFECTING_KEYS = (
    "payload_ttl_seconds",
    "exception_categories",
    "verdict_codes",
    "llm",
)


class SupplyDataQualityPrecedentGraph(AgentBaseGraph):  # type: ignore[misc]  # AgentCore wheel has no typed graph base.
    """Fixed-pipeline graph."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        overrides = dict(config or {})
        values = deepcopy(_CODE_DEFAULTS)
        values.update(_load_file_config())
        values.update(overrides)
        if "services" not in values:
            values["services"] = RuntimeServices(values)
        else:
            _reconcile_prebuilt_services(values, overrides)
        super().__init__(values)

    @property
    def name(self) -> str:
        return "ene_c2_123_supply_data_quality_precedent"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        # super() injects "initialize" and "finalize" slots automatically.
        # They are shown here as sample code only — do NOT re-register them
        # unless you are providing a custom subclass.
        #
        # Sample (for reference — already injected by super()):
        #   self._nodes["initialize"] = InitializeNode()   # sets schema_version,
        #                                                    # session_id, trust_level
        #   self._nodes["finalize"]   = FinalizeNode()     # builds response_metadata,
        #                                                    # total_time_ms
        #
        # To customise: subclass InitializeNode / FinalizeNode and override
        # on_initialize() / on_finalize() respectively; then register here:
        #   self._nodes["initialize"] = CustomInitializeNode()
        #   self._nodes["finalize"]   = CustomFinalizeNode()
        super().register_nodes()  # injects InitializeNode + FinalizeNode

        # Domain pipeline slots (required — fill all three):
        self._nodes["pre_process"] = IntakeNode(config=self.config)
        self._nodes["main"] = PrecedentWorkflowGraphNode(config=self.config)
        self._nodes["post_process"] = CardAssemblyNode(config=self.config)


def _load_file_config() -> dict[str, Any]:
    if not _CONFIG_PATH.exists():
        return {}
    loaded = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8"))
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError("config/config.yaml must contain a mapping")
    return loaded


def _reconcile_prebuilt_services(
    values: dict[str, Any],
    overrides: dict[str, Any],
) -> None:
    """Make a prebuilt live container authoritative without split-brain config."""

    services = values["services"]
    service_config = getattr(services, "configuration", None)
    if service_config is None:
        raise TypeError("prebuilt services must expose configuration")
    for key in _SERVICE_AFFECTING_KEYS:
        if key not in service_config:
            continue
        service_value = service_config[key]
        if key in overrides and not _same_service_value(key, overrides[key], service_value):
            raise ValueError(
                f"{key} conflicts with the supplied RuntimeServices container"
            )
        if key in {"exception_categories", "verdict_codes"}:
            values[key] = list(service_value)
        else:
            values[key] = service_value


def _same_service_value(key: str, left: Any, right: Any) -> bool:
    if key in {"exception_categories", "verdict_codes"}:
        return tuple(left) == tuple(right)
    if key == "llm":
        return left is right
    return left == right
