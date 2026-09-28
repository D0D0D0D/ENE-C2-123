# ENE-C2-123 Test Specification

## Scope and execution

Tests use a real SQLite ledger for the production local path, plus injected payload-store,
receipt-store, and fake/no LLM seams. They never contact a network DB, vector store, LLM, or API.
HTTP tests pass through the production FastAPI composition and verified-middleware state.

| Group | Coverage | Result |
|---|---|---|
| TC-01 | Flat serializable State contract | PASS |
| TC-02 | SecurityViolationError on closed-schema output violation | PASS |
| TC-03 | No credential-like State field; no source credential literal | PASS |
| TC-04 | InvocationContext supplied through graph/configurable path | PASS |
| TC-05 | Nodes do not duplicate framework lifecycle events | PASS |
| TC-06 | S-2 final gate cannot be overridden | PASS |
| TC-07 | S-3 final gate cannot be overridden | PASS |
| TC-08 | `VERIFIED_EXTERNAL` trust gate is enforced | PASS |
| TC-09 | Missing scope/connection rejects before payload resolution; provisioned control succeeds | PASS |
| TC-10 | S-3 closed allowlist, missing verification block, unknown root/nested keys reject | PASS |
| TC-11 | Domain trace events occur on success, rejection, degradation and shortcut paths | PASS |

## Design §8 acceptance mapping

| §8 | Test function(s) | Result |
|---|---|---|
| ① closed schema | `test_s3_rejects_nested_undeclared_current_decision_field` | PASS |
| ② claim evidence | `test_section8_02_claim_requires_evidence_id` | PASS |
| ③ deterministic facts | `test_conflict_is_evaluated_before_top_k_and_minor_verdict_is_retained` | PASS |
| ④ permitted historical citation | `test_section8_04_prior_adjudication_fact_with_evidence_is_allowed` | PASS |
| ⑤ pre-truncation conflict | `test_conflict_is_evaluated_before_top_k_and_minor_verdict_is_retained` | PASS |
| ⑥ deterministic priority | `test_section8_06_priority_ignores_llm_missing_item_count` | PASS |
| ⑦ verbatim copy | `test_section8_07_verbatim_evidence_copy_is_rejected` | PASS |
| ⑧ numeric forms | `test_section8_08_numeric_tokenizer_handles_adjacent_and_fullwidth_values`; `test_section12_3bis_kanji_parser_preserves_large_unit_magnitude`; `test_section12_3bis_allows_number_from_one_of_multiple_cited_evidence_ids`; `test_section12_3bis_rejects_number_from_unrelated_evidence_with_multiple_citations`; `test_section12_3bis_rejects_unit_swap_with_multiple_citations` | PASS |
| ⑨ unrelated source | `test_section8_09_registry_does_not_issue_unrelated_source_id` | PASS |
| ⑩ verification fail-closed | `test_s3_rejects_missing_verification_block` | PASS |
| ⑪ minimum span | `test_section8_11_span_shorter_than_floor_is_not_registered` | PASS |
| ⑫ persistence | `test_pb_persistent_projection_scope_and_raw_retention_boundaries` | PASS |
| HTTP payload refs | `test_http_invoke_generates_note_and_matching_revision_evidence`; `test_http_invoke_excludes_mismatched_revision_series`; `test_http_invoke_marks_operational_note_requirement_only_when_absent` | PASS |
| HTTP core workflow | `test_http_ingest_invoke_feedback_core_workflow`; `test_http_ingest_invalid_category_returns_4xx_rejected_summary` | PASS |
| Invoke partial batch | `test_invoke_mixed_batch_returns_partial_and_explicit_rejection` | PASS |

## Batch 3 retrofit junction tests

| ID | Test | Expected result |
|---|---|---|
| B3-01 | `test_standalone_read_token_cannot_write_business_state` | read-only token cannot call ingest; ledger remains empty |
| B3-02 | `test_verified_http_composition_writes_reads_and_deletes_payload_refs` | actual auth → payload store → graph → ledger path succeeds and transient refs are deleted |
| B3-03 | `test_real_sqlite_scope_projection_returns_one_hit_and_blocks_direct_id` | SQLite returns exactly one same-scope hit; other scope search/direct ID return none |
| B3-04 | `test_ingest_batch_rolls_back_first_row_when_second_database_write_fails` | one v0.4 batch hook call; second-row fault rolls back first row and exception text is absent |
| B3-05 | `test_prompt_contract_is_generated_and_valid_llm_output_reaches_customer_card` | prompt contract is generated from validation declarations and a valid response reaches output |
| B3-06 | `test_typed_projection_rejects_invalid_llm_findings_through_full_graph` | nested type, unknown key, and enum-outside responses fail closed without raw marker leakage |
| B3-07 | `test_provider_exception_text_is_not_exposed_through_full_graph` | provider exception marker is absent from the full graph result |
| B3-08 | `test_concurrent_feedback_retries_create_one_correction_through_full_graph` | two concurrent same-key requests create one correction and return the same ID |
| B3-09 | `test_feedback_precedent_id_from_other_scope_is_rejected_through_full_graph` | cross-scope direct ID cannot become feedback provenance |

The vendored accumulation-ledger backend conformance suite is parametrized over both
`InMemoryLedgerBackend` and the production-local `SQLiteLedgerBackend`; scope, copy boundary,
deterministic query, raw-only purge, and timezone-aware cutoff behavior run against both.

## Proof of boundary

| ID | Boundary | Expected result | Result |
|---|---|---|---|
| PB-1 | Audit | `test_pb_audit_intake_rejection_emits_domain_event` | PASS |
| PB-2 | State serialization | State schema contains JSON-compatible values only | PASS |
| PB-3 | External seam | `test_pb_external_ledger_seam_is_injected_not_constructed_by_node` | PASS |
| PB-4 | Import isolation | No Level-0 import | PASS |
| PB-5 | Checkpoint safety | `test_pb_checkpoint_restores_without_free_text_sentinels` | PASS |
| PB-6 | Invocation order | Dynamic scan of concrete nodes verifies S-1 → S-2 → execute → S-3 | PASS |
| PB-7 | HITL propagation | Auto-waived: `hitl.enabled: false`; the conditional test module remains and skips | PASS (waived) |

## Execution record

- Command: `pytest -q --ignore=scripts/test_stg_invoke_evidence.py`
- Result: **542 passed, 2 skipped**.
- Command: `pytest -q`
- Result: **543 passed, 2 skipped, 11 setup errors**. All 11 errors are the
  unchanged staging-evidence tests failing to bind their local HTTP server in the
  restricted workspace (`PermissionError: [Errno 1] Operation not permitted`); those 11
  fixture-dependent test bodies did not run. One test that does not bind a socket passed.
- Command: `pytest tests/proof_of_boundary/ -q`
- Result: **7 passed, 2 skipped** (PB-7 is the expected non-HITL skip).
- Command: `ruff check src tests`
- Result: **All checks passed**.
- Command: `mypy src`
- Result: **Success: no issues found in 20 source files**.

## 実 LLM 1 回転検証

モックだけで固めた実装は実 LLM を繋いだ瞬間に動かないことがあるため、注入シームへ実 LLM クライアントを刺して 1 回 invoke し、**中身のある出力が出ること**を確認している。

| 項目 | 内容 |
|---|---|
| 実施日 | 2026-08-19 |
| 使用 LLM | ローカル CLI（Azure OpenAI `gpt-5.5`）を `BaseLLM` 実装として注入 |
| 注入シーム | `config[llm]` |
| 実 LLM 呼び出し | 1 回 |
| 出力 | ['cards', 'mode'] が非空。縮退理由なし |

本番の LLM は `AnthropicClient` 等で、これは検証用シーム。テンプレの `src/` は変更していない（一時テストを生成→実行→削除する方式）。CI には入れていない（ローカル CLI と鍵が要るため）。

## llm-draft-stage 0.3.0 の回帰テスト（LLM 出力の複数 JSON）

AGENTIC STAR の登録モデルが draft JSON を改行区切りで 2 回連結して返す、または JSON の後ろに `{` `}` を含む散文を付ける応答で、
Stage の parse が `Extra data` になり fail-closed していた。部品 `llm-draft-stage` を 0.3.0 に更新し、複数候補を走査して
一意な非空 object だけを受理する（相異なる複数は `AmbiguousJsonOutput` で fail-closed・`{}` / NaN / 配列は不受理）。
対応テスト: `tests/parts/llm-draft-stage/test_draft_node.py`（実物 fixture `fixtures/gpt54mini_duplicated_decision.txt` を含む）。
