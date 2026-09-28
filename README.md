# ENE-C2-123 — SupplyDataQualityPrecedentAgent

> **Category**: Cat 2 (domain workflow)
> **Industry**: ENE

## Overview

When a data-quality exception is raised on an energy supply series, the useful question is rarely
"is this value wrong?" but "how did we adjudicate this last time, and does that precedent actually
apply here?" This agent answers the second question: it retrieves the organisation's own past
adjudications for the same exception category and series, and returns a precedent-application card
that links each statement to the evidence it came from, together with a deterministic review
priority.

It does not adjudicate. There is no verdict, recommendation, action, conformity or severity field
anywhere in the output schema — the schema simply has no place to put one, and an undeclared key is
rejected at the top level and in every nested object. What the card says about a precedent is a
*relation*: the cited evidence supports applying it, blocks applying it, is insufficient, or —
failing closed — unknown.

The split between what is computed and what is generated is the design:

- **Deterministic**: narrowing candidates by exception category, series and validity window;
  identifying a conflicting-precedent case (more than one distinct past verdict code); verifying
  that every cited evidence identifier really exists; closing the output schema; and computing the
  review priority.
- **Generated**: only the prose of the relation — the claims, the missing-information items and the
  confirmation questions, each anchored to an evidence identifier.
- **Rendered from records, never written by the model**: statements of past fact ("precedent X was
  adjudicated as …").

The priority calculation deliberately takes **no** input the model produced. Its inputs — the
distinct verdict count, the deterministic missing-requirement identifiers, whether the candidate
set was truncated — are all fixed before the model is called, because a priority that moves when
the model splits one missing item into two is not a deterministic priority, and re-computing it
from the model's own numbers would verify nothing.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Integration

| Endpoint | Purpose | Who may call it |
|---|---|---|
| `POST /invoke` | Compare current exceptions against precedent, return the cards | Any verified caller in the request's scope |
| `POST /ingest` | Store sanitised past adjudications and series metadata | Verified middleware identity only |
| `POST /feedback` | Record a new human adjudication, or correct an existing precedent | Verified middleware identity only |
| `GET /health` | Liveness | Unauthenticated |

Provide:

- an AgentCore runtime;
- a `BaseLLM` implementation injected through the graph configuration. Without one the agent still
  answers, returning the documented deterministic card with `degradation_reason: llm_unavailable`
  rather than an error or a silently thinner result;
- authentication middleware that stamps a verified trust level, a `caller_id` and the
  `business_unit_id` scope axis. Scope is mandatory and never read from the request body;
- durable storage for the precedent ledger and the payload store; both ship as in-memory seams.

Runtime parameters live in `config/config.yaml`. Besides the backbone settings (`max_retry`,
`timeout_seconds`, `budget_usd`, `memory_enabled`, `hitl.enabled: false`), the domain knobs are:

| Key | Meaning |
|---|---|
| `candidate_top_k` (`8`) / `candidate_hard_cap` (`24`) | Candidate selection size and hard ceiling; truncation is reported, not hidden |
| `conflict_min_distinct_verdicts` (`2`) / `conflict_min_minority_count` (`1`) | When a candidate set counts as a conflicting-precedent case |
| `priority_weight_*`, `priority_high_threshold` (`4`), `priority_medium_threshold` (`2`) | Weights and thresholds of the deterministic priority score |
| `payload_ttl_seconds` (`900`) | Lifetime of a server-minted free-text reference |
| `max_exceptions_per_invoke` (`50`), `statement_max_chars`, `min_span_chars`, `max_body_chars`, … | Input and output size bounds |
| `max_mojibake_ratio`, `max_control_char_ratio`, `min_readable_char_ratio` | Text-quality gates applied to ingested bodies |
| `exception_categories`, `verdict_codes` | Closed vocabularies for categories and past verdict codes |

The template does not decide whether data passes a quality check, does not assert regulatory
conformity, does not correct or republish the underlying series, and does not act on the exception.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent is designed to run on the platform; deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Supplied by the platform's own package registry at build time. It is not published on public PyPI, so `pip install -e .` alone does not fetch it. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The standalone server in `src/api/server.py` starts without the platform, and `/health` answers
`{"status": "ok"}`. It does not, however, invent an identity or a model. A request with no verified
caller and no matching bearer token is **401**; a caller with no resolvable `business_unit_id`
scope is **403**; and the write endpoints stay closed to the standalone token path entirely
(`/ingest` and `/feedback` answer *"Verified middleware identity is required."*). A read call that
does reach the graph with no injected model and an empty ledger returns `status: success` carrying
the deterministic card — eligibility `no_precedent`, the deterministic missing-information items, a
computed `review_priority`, and a `degradation_reason` naming what was absent. The test suite runs
without a platform connection.

## Input

`POST /invoke` takes `exceptions[]` (at most `max_exceptions_per_invoke`), optional
`revision_notices[]` and an optional `session_id`. Unknown fields are rejected. Each exception
carries `exception_id`, `exception_category`, `series_id`, `detected_at`, optional numeric
`metrics`, and an optional `operational_note` — the free text. Free text never travels in graph
state: the adapter mints a scoped, expiring reference for it and deletes the reference when the
request finishes.

```bash
curl -X POST http://127.0.0.1:8000/invoke \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${INVOKE_AUTH_TOKEN}" \
  -d '{
    "exceptions": [{
      "exception_id": "E1",
      "exception_category": "outlier",
      "series_id": "S1",
      "detected_at": "2026-09-01T00:00:00Z",
      "metrics": {"value": 12.5}
    }],
    "session_id": "example-session"
  }'
```

`POST /ingest` takes `adjudications[]` and optional `series_meta[]`. `POST /feedback` takes either
a new adjudication (`kind: "adjudication"`, with the confirmed verdict, its rationale, and which
precedents were adopted or rejected) or a correction of an existing record (`kind: "correction"`,
with `record_id` and `feedback_seq`). A graph-level rejection is returned as a **422** with a fixed
error envelope — framework traces never reach the caller.

## Output

The standard AgentCore envelope is preserved. `output` carries `mode`, `cards[]`, `rejected[]` and
`degradation_reason`. Each card holds:

| Field | Content |
|---|---|
| `card_kind` | Which card this is — for example a deterministic display card when no model was available |
| `exception_ref` | The exception the card is about |
| `eligibility` | `kind` (including `no_precedent`), the distinct past verdict codes, and the candidate count |
| `precedent_facts` | Past adjudications, rendered deterministically from the ledger |
| `claims` | Model-written relation statements, each with `support_status` and evidence identifiers |
| `missing_information` | Deterministic requirement identifiers plus a prose statement |
| `confirmation_questions` | What to check before applying a precedent |
| `review_priority` | `level` plus the `basis` it was computed from |
| `degradation_reason` | Why the card is degraded, when it is |

`support_status` describes the relation only: `supports_application`, `blocks_application`,
`insufficient`, or `unknown` as the fail-closed default. It never states this exception's outcome.

## Security and Limitations

Scope is mandatory: every call resolves a `business_unit_id` from the verified caller, and customer
isolation is a property of the connection rather than a row filter. Write paths require a verified
middleware identity, so the standalone token cannot reach them. Free text is held behind
server-minted, scope-bound, expiring references and is deleted when the request ends, so raw bodies
do not sit in graph state. On the way out, evidence identifiers are verified to exist, personal
information is masked, credential-shaped content blocks the response, and the schema is closed by
allowlist — with no assertive-word denylist, which paraphrase defeats while creating a false sense
of coverage.

A precedent whose adjudication is disputed is not deleted: it moves into a `review_required`
lifecycle so a conflicting historical decision stays visible for review instead of quietly
disappearing.

Known limits, recorded rather than hidden:

- the ledger and payload store are in-memory seams; nothing survives a restart until a deployment
  supplies durable implementations;
- evidence is verified to *exist and match its recorded content hash*; a stale hash invalidates the
  evidence and becomes a missing-requirement item rather than being silently used;
- candidate selection is bounded, and a truncated candidate set is reported and raises the review
  priority rather than being presented as complete;
- the semantic comparison is model-generated and assistive. The adjudication itself remains with
  the data-quality team.

## Release

- Version: 1.0.0
- Category: fixed multi-step domain workflow (Cat 2), inner workflow subgraph
- Generation mode: `llm` (relation prose only; retrieval, conflict detection, evidence verification and priority are deterministic)
- HITL: not enabled — human adjudication is recorded through `/feedback`
- Persistence: none in-process — no checkpointer; durable state lives in the injected stores
- External API clients: deployment-injected; none bundled

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection, a model, a database or network access. Running the agent
itself needs the platform, an injected `BaseLLM`, and durable stores.

## Documentation

`docs/02_design.md` carries the domain model, the deterministic/generated responsibility split, the
node structure, the candidate-narrowing and conflict rules, the evidence registry, the output
contract and the priority calculation. `docs/03_test_spec.md` records the test specification and
its results.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent configuration
docs/         design and test specifications
```

See `docs/02_design.md` for the design and `docs/03_test_spec.md` for the test specification.

## Customising

1. Adjust `config/` for your own environment and policies.
2. Replace the knowledge sources and sample data with your own.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
