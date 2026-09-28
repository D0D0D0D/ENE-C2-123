"""先例の意味照合と有効期間照合を固定する。

由来: 2026-08-13 の独立レビュー（Batch 3）。レビュアーの再現 probe を回帰テストにしている
（実装者が書くと実装の写しになるため）。

## H2 — LLM が過去の判断理由を一度も読めない

`_precedent_facts()`（`src/nodes/pre_process_node.py:308`）が組むのは
`adjudication_id` / `verdict_code` / `decided_at` / `occurred_at` / `valid_to` /
`rationale_evidence_ids`（**ID の列**）だけで、**rationale 本文が一切入らない**。
`_prompt()` もそれをそのまま渡すため、LLM は「過去にどういう理由でどう裁定されたか」を
読めないまま応答する。設計が謳う「候補内自由記述 × 今回記述の意味照合」が成立していない。

既存 fake は ID だけから応答を組み立てるため、この欠陥を隠していた。

## H3 — 将来発効の改訂告知が現在案件の有効な根拠になる

`_resolve_notices()`（`src/nodes/pre_process_node.py:380`）は `series_id` しか照合せず、
`effective_date` と案件の `detected_at` を比較しない。2035 年発効の告知が 2026 年案件で通る。
"""
from __future__ import annotations

from src.nodes.pre_process_node import _precedent_facts

OLD_RATIONALE = "OLD_RATIONALE_MARKER"


def _row(record_id: str, verdict: str) -> dict:
    return {
        "record_id": record_id, "verdict_code": verdict,
        "decided_at": "2025-04-01", "occurred_at": "2025-03-20", "valid_to": None,
        "rationale": OLD_RATIONALE,          # 上流の裁定レコードは理由を持っている
    }


def test_precedent_facts_carry_the_rationale_the_llm_must_compare():
    """先例の *判断理由* が LLM に届く形で組まれること。

    ID だけでは意味照合できない。この AG の中核は「過去の理由と今回の記述を突き合わせる」
    ことなので、理由が prompt に届かないと中核が成立しない。
    """
    facts = _precedent_facts([_row("P-1", "accept")], registry=[])
    blob = str(facts)
    assert OLD_RATIONALE in blob, (
        "先例の rationale 本文が precedent_facts に含まれていない（ID だけ）: " + blob
    )


def test_precedent_facts_keep_the_verdict_and_provenance():
    """理由を足しても、裁定コードと出所 ID は保持されること（既存契約の維持）。"""
    facts = _precedent_facts([_row("P-1", "accept")], registry=[])
    assert facts and facts[0].get("verdict_code") == "accept"
    assert facts[0].get("adjudication_id") == "P-1"

# H4（feedback の evidence_id が prefix 一致だけ）は、レビュアーの再現手順を受領してから
# 追加する。手元で書いた版は対照実験（空の evidence_ids でも同じく error）で vacuous と判明した。


# --- H3 / H4 / Medium: レビュアーの再現 probe をそのまま回帰テスト化 -----------
# 🔴 到達条件（H4 で二度 vacuous を作った教訓）:
#   - `adopted_precedent_ids` が同一 scope に実在すること
#   - `rationale_ref` が feedback と同じ scope / session_id で発行されていること
#   - `InvocationContext` の session_id / thread_id が一致し、**caller_id が非空**であること
# これらが欠けると別の理由で error になり、検証したい経路に到達しない。

import json

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.graph.graph import SupplyDataQualityPrecedentGraph

_ADJ = {
    "record_kind": "adjudication", "classification": {"exception_category": "missing"},
    "operational_note": "A sufficiently detailed operational note for retrieval.",
    "occurred_at": "2026-01-01T00:00:00+00:00", "valid_to": None,
    "rationale": "A sufficiently detailed rationale for a prior human decision.",
    "decided_by": "reviewer", "decision_source": "human", "provenance": {},
}


def _ctx(session_id: str) -> InvocationContext:
    return InvocationContext(
        session_id=session_id, thread_id=session_id, caller_id="reviewer",
        caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
    )


def test_a_future_dated_revision_notice_is_not_usable_as_current_evidence():
    """2035 年発効の改訂告知が 2026 年の案件で根拠になれないこと。

    `_resolve_notices()` が `series_id` しか照合せず `effective_date` を見ていない。
    """
    captured: dict = {}

    class _CaptureLlm:
        def complete(self, messages):
            prompt = json.loads(messages[0]["content"])
            captured["prompt"] = prompt
            future = [e for e in prompt["evidence_ids"] if e.startswith("EV-REV-REV-FUTURE-")]
            return {"content": json.dumps({
                "claims": [{
                    "claim_id": "C-FUTURE",
                    "precedent_id": prompt["precedent_facts"][0]["adjudication_id"],
                    "support_status": "unknown",
                    "statement": "The future revision was accepted as current evidence.",
                    "evidence_ids": future[:1],
                }],
                "missing_information": [
                    {"item_id": f"M{i+1}", "requirement_id": r,
                     "statement": "Additional information is required.", "evidence_ids": []}
                    for i, r in enumerate(prompt["requirement_ids"])],
                "confirmation_questions": [],
            })}

    graph = SupplyDataQualityPrecedentGraph({"llm": _CaptureLlm()})
    services = graph.config["services"]
    scope = {"business_unit_id": "unit-future"}
    session_id = "future-revision-session"
    for verdict, decided in (("accepted", "2025-01-02T00:00:00+00:00"),
                             ("rejected", "2025-01-03T00:00:00+00:00")):
        services.ledger.write(scope, {**_ADJ, "series_id": "SERIES-FUTURE",
                                      "verdict_code": verdict, "decided_at": decided,
                                      "retrieval_text": "missing SERIES-FUTURE"})
    body_ref = services.payload_store.put(
        "A future revision notice that must not apply to the current exception.",
        scope=scope, session_id=session_id)

    result = graph.invoke(json.dumps({
        "schema": "ene-c2-123/v1", "mode": "invoke", "business_unit_id": scope["business_unit_id"],
        "payload": {
            "exceptions": [{"exception_id": "EX-FUTURE", "exception_category": "missing",
                            "series_id": "SERIES-FUTURE",
                            "detected_at": "2026-02-01T00:00:00+00:00", "metrics": {}}],
            "revision_notices": [{"notice_id": "REV-FUTURE", "series_id": "SERIES-FUTURE",
                                  "publisher": "publisher",
                                  "effective_date": "2035-01-01T00:00:00+00:00",
                                  "body_ref": body_ref}],
        }}), ctx=_ctx(session_id))

    prompt_ids = [e for e in captured.get("prompt", {}).get("evidence_ids", [])
                  if e.startswith("EV-REV-REV-FUTURE-")]
    assert not prompt_ids, f"将来発効の告知が prompt の evidence_ids に載った: {prompt_ids}"
    blob = json.dumps(result.get("output"), ensure_ascii=False, default=str)
    assert "EV-REV-REV-FUTURE-" not in blob, "将来発効の告知 ID が顧客出力に出た"


def test_feedback_rejects_an_evidence_span_that_does_not_exist():
    """prefix だけ合う実在しない span が拒否され、ledger に残らないこと。"""
    graph = SupplyDataQualityPrecedentGraph()
    services = graph.config["services"]
    scope = {"business_unit_id": "unit-current"}
    session_id = "fake-span-session"
    own = services.ledger.write(scope, {**_ADJ, "series_id": "SERIES-X",
                                        "verdict_code": "accepted",
                                        "decided_at": "2026-01-02T00:00:00+00:00",
                                        "retrieval_text": "missing SERIES-X"})
    record_id = own["record_id"] if isinstance(own, dict) else str(own)
    rationale_ref = services.payload_store.put(
        "A sufficiently detailed human feedback rationale.", scope=scope, session_id=session_id)
    before = len(services.ledger.search(scope))

    result = graph.invoke(json.dumps({
        "schema": "ene-c2-123/v1", "mode": "feedback", "business_unit_id": scope["business_unit_id"],
        "payload": {"kind": "adjudication", "exception_id": "EX-X", "exception_category": "missing",
                    "series_id": "SERIES-X", "detected_at": "2026-02-01T00:00:00+00:00",
                    "confirmed_verdict_code": "accepted", "rationale_ref": rationale_ref,
                    "decided_at": "2026-02-02T00:00:00+00:00",
                    "adopted_precedent_ids": [record_id], "rejected_precedent_ids": [],
                    "decisive_difference_evidence_ids": [f"EV-ADJ-{record_id}-S999999"]},
    }), ctx=_ctx(session_id))

    assert result.get("status") == "error", "実在しない span が prefix 一致だけで受理された"
    assert len(services.ledger.search(scope)) == before, "拒否されるべき feedback が ledger に書かれた"


def test_series_meta_for_a_different_series_does_not_satisfy_the_requirement():
    """別 series の metadata が対象 series の不足要件を消さないこと。"""
    graph = SupplyDataQualityPrecedentGraph()
    result = graph.invoke(json.dumps({
        "schema": "ene-c2-123/v1", "mode": "invoke", "business_unit_id": "unit-series-meta",
        "payload": {
            "exceptions": [{"exception_id": "EX-SERIES-A", "exception_category": "missing",
                            "series_id": "SERIES-A",
                            "detected_at": "2026-02-01T00:00:00+00:00", "metrics": {}}],
            "revision_notices": [],
            "series_meta": [{"series_id": "SERIES-B", "publisher": "unrelated-publisher", "unit": "MW"}],
        }}), ctx=_ctx("series-meta-session"))

    requirements = [i["requirement_id"] for i in result["output"]["cards"][0]["missing_information"]]
    assert "REQ-SERIES-META-ABSENT" in requirements, (
        f"別 series の metadata が要件を消した: {requirements}"
    )
