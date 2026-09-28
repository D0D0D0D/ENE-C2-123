# ENE-C2-123 SupplyDataQualityPrecedentAgent — 設計仕様書

> 本設計仕様は 2026-07-28 に確定した。実装が設計から逸脱する場合は差分を明示し、設計への反映要否を人間が判断する。
> SDK 契約は **AgentCore SDK 1.0.0 の実コードで確認済み**（2026-07-30）。

> 本書は実装前に作成されたため、初期コードに言及する箇所は当時の設計上の差分を記録している。
> 現在の実装状態は `src/` と `tests/` を参照すること。

---

## 1. AgentCore アーキテクチャ上の位置づけ

- **Agent Class**: `SupplyDataQualityPrecedentGraph`（`src/graph/graph.py`）
- **L1 Base**: `AgentBaseGraph`（L1 直継承。`AutonomousBaseGraph` ではない）
- **Base pattern**: DocGenerationAgent（構造化ドキュメント生成パイプライン）
- **Composition Pattern**: `GraphNode`（main スロットに内側 domain workflow subgraph）
- **Error propagation strategy**: `propagate`（`GraphNode` の既定）。
  **これは内側 subgraph のエラーを親へ伝播させる（握り潰さない）という *グラフ層* の戦略であり、
  HTTP バッチ契約とは別レイヤ**である。1 リクエストに複数の例外を載せる `/invoke` の
  部分失敗の扱いは §11-2-bis に定義する（例外単位で `rejected[]` に明示・top-level status で判別可能にする）
- **三層分離**:
  - State: flat TypedDict（`AgentState` 継承。primitives ＋ JSON 直列化可能型のみ・Pydantic 禁止）
  - Node: L1 継承（Template Method — `execute(self, state) -> dict` のみ override）
  - Graph: composition（`register_nodes()` で 3 スロットを埋める。`add_edges()` は override しない）

### 固定 5 ノード backbone（framework 提供）

```
START → initialize → pre_process → main → {route} → post_process → finalize → END
                                     ↓ (RETRY, max 3)
                                  pre_process
```

`initialize` / `finalize` は `super().register_nodes()` が自動注入する。
テンプレは `pre_process` / `main` / `post_process` の 3 スロットを埋める（欠落は `compile()` で `MissingNodeError`）。

### 1-1. AGENTIC STAR Marketplace entry adapter

リポジトリ直下の `cli.py` は `SupplyDataQualityPrecedentGraph` を継承する Marketplace 専用境界である。
runner が渡す `input_context`（`conversation_history` を含む）は利用せず、`src/entry_adapter/` の
`SCOPE_KEYS` と `STG_DEFAULT_SCOPE_ENVS` から解決した scope だけで辞書を組み直す。scope の環境変数は
未設定・空・空白文字を含む値を constructor で拒否し、runner の `compile()` より前に fail closed する。

Marketplace の入力は公開 `/invoke` body（`exceptions` 1 件と任意の `revision_notices`）を JSON 化した
単一文字列である。adapter は `schema="ene-c2-123/v1"`、`mode="invoke"`、deployment scope を持つ
canonical envelope に射影する。自由記述は scope/session に束縛した一時 payload ref に置換し、呼出し後に削除する。
`mode=ingest` / `mode=feedback` は graph に渡さない。Marketplace は参照専用であり、過去裁定の登録・訂正は
既存の認証済み HTTP 経路だけが担う。

`InvocationContext.caller_trust_level` は常に `VERIFIED_EXTERNAL`、`hitl_allowed` は false とする。
Azure OpenAI は既存 `RuntimeServices` の ledger / payload store / receipt store を維持したまま LLM seam だけへ
注入する。接続値と client は LLM 呼出しごとに解決・生成し、adapter instance には保持しない。
`provision_secrets()` は runner の広い provider を破棄し、allowlist 済み process environment から Azure 3 値と
scope を再検証する。AgentCore 1.0.1 では存在しない `load_agent_config`、Marketplace bootstrap、EnvProvider、
Azure client には 1.0.1 互換経路を置く。

State の成立条件は次のとおりである。

| 読取元 | 読むノード | 必要な値 |
|---|---|---|
| initial State | framework `InitializeNode` | `state.get("input_context")` をそのまま保持。domain node はここから業務値を読まない |
| canonical `user_input` | `IntakeNode` | `schema` / `mode` / `business_unit_id` / `payload`; invoke では exception の `exception_id` / `exception_category` / `series_id` / `detected_at` / `metrics` |
| pre_process 後の State | `PrecedentWorkflowGraphNode` | `state.get("mode")` と、`validated_input` から復元する `canonical_payload`; scope、候補 ID、eligibility、evidence registry、requirement IDs、precedent facts |
| inner State | `ModeRouterNode` と `PrecedentWorkflowGraph.route()` | JSON 化された `mode`; route は `state.get("mode")` と `eligibility.kind` で invoke 経路を選択 |
| main 後の State | `CardAssemblyNode` | `mode`、`business_unit_id`、`exception_ref`、`candidate_ids`、`eligibility`、`verification_snapshot`、`requirement_ids`、`precedent_facts` と draft 欄 |

過去裁定の参照経路は `RuntimeServices.ledger` → `IntakeNode._prepare_invoke()` の scope/category/series/有効期間検索
→ `precedent_facts` → inner deterministic/LLM 経路 → `CardAssemblyNode` の決定論射影である。Marketplace adapter は
ledger を空の facade に置換せず、既存 container がある場合は同じ ledger object を引き継ぐ。また historical
`verdict_code` を除去しない。今回の結論は従来どおり出力 schema に欄を設けないことで抑止する。

---

## 2. ドメインモデルと用語

| 用語 | 定義 | 主なフィールド |
|---|---|---|
| **品質例外**（exception） | 顧客の既存品質チェック機構（dbt テスト・社内バリデータ等）が機械的に出力した 1 件の例外 | `exception_id` / `exception_category`（例外区分）/ `series_id`（系列）/ `detected_at` / `metrics`（構造化値）/ `operational_note`（自由記述） |
| **過去裁定**（adjudication） | 組織が過去に下した裁定の記録。**組織が既に確定させた事実**であり、本 AG が新たに下す判断ではない | `adjudication_id` / `exception_category` / `series_id` / `occurred_at`–`valid_to`（有効期間。**`valid_from` という列は存在しない**）/ `verdict_code`（裁定結果コード）/ `rationale`（裁定理由・自由記述）/ `operational_note` / `decided_by` / `decided_at` / `decision_source` |
| **改訂告知**（revision notice） | 発行元が出した系列定義・集計方法の改訂告知。顧客が取得済みの文書を証跡として渡す（**AG は外部ポータルを取得しない**） | `notice_id` / `publisher` / `effective_date` / `body` |
| **系列メタ**（series metadata） | 系列の単位・発行元・改訂履歴 | `series_id` / `unit` / `publisher` / `revisions[]` |
| **前例適用カード**（precedent application card） | 本 AG の出力。前例の適用を支持する事実／阻害する事実／不足情報／根拠 ID／確認質問／レビュー優先度 | §7 |
| **相反前例型**（conflicting precedent type） | 同一キー（例外区分 × 系列）内で、有効期間内の過去裁定の `verdict_code` が 2 種類以上に分かれている型 | §5 |

> **過去裁定の取り扱い（設計上の核心・設計レビューで提起された論点に対する確定方針）**
> 過去裁定は `verdict_code` を含めて **KB に保持し、検索・想起の対象とし、根拠 ID 付きで出力に提示する**。
> これは設計上の穴ではなく意図した仕様である。避けるべきは「**今回について**断定すること」であり、
> そこは §8 の非断定契約で担保する。過去裁定を蓄積して想起へ還元する経路が本テンプレの moat（G1）の実体である。
> ⚠️ 蓄積型 AG の 2 層データ統治（§9）は **PII・要配慮個人情報・秘密情報**を対象とするものであり、
> **過去の判断結果の隔離に転用しない**（転用すると moat そのものを潰す）。

---

## 3. 責務分界 — 決定論と LLM の境界（設計の要）

| 工程 | 担い手 | 理由 |
|---|---|---|
| `例外区分 × 系列 × 有効期間` による**候補集合の絞り込み** | **決定論（DB クエリ）** | キー一致で答えが出る。ここに LLM を使うと単なる決定表 lookup に還元され Tool と区別できない |
| **相反前例型の同定**（conflict predicate） | **決定論**（`verdict_code` の異なり数） | 過去裁定コードの集合演算。LLM 不要 |
| 候補内の**裁定理由・運用メモ・改訂告知 × 今回記述の意味的照合** | **LLM** | 「同じ例外区分でも今回の状況は当該前例と実質的に同じか／適用を阻む差異は何か」は**キー一致では原理的に答えられない** |
| 根拠 ID の実在検証・出力構造の閉包・レビュー優先度の算出 | **決定論** | 機械保証できるものは機械が持つ |
| 過去裁定の**事実提示文**（「前例 X では 〇〇 と裁定された」） | **決定論描画** | 事実主張を LLM に書かせない |
| 今回の裁定の確定 | **人間（データ品質担当）** | AG のスコープ外 |

**LLM が書いてよいのは、根拠 ID に紐づく関係記述（claim / 不足情報 / 確認質問）の散文だけ**である。
意味照合時の内部 `precedent_facts` には ledger の model 可視 `rationale_safe` を `rationale` として含め、
LLM が裁定コードだけでなく過去の判断理由を読めるようにする。顧客向けカードでは理由本文を再掲せず、
従来どおり `rationale_evidence_ids` だけを決定論射影する。

---

## 4. ノード構成

### 4-1. 外側グラフ（`src/graph/graph.py` — `AgentBaseGraph` 直継承）

| スロット | クラス | 継承 | `required_trust_level` | 責務 |
|---|---|---|---|---|
| `initialize` | `InitializeNode` | framework | — | framework 既定 |
| `pre_process` | `IntakeNode` | `FunctionNode` | `VERIFIED_EXTERNAL` | モード判別・スキーマ検証・入力形式 allowlist・payload_ref 確認・顧客 KB 接続解決・候補絞り込み・conflict predicate 評価・根拠レジストリ構築 |
| `main` | `PrecedentWorkflowGraphNode` | `GraphNode` | `VERIFIED_EXTERNAL`（**明示宣言。既定 ANONYMOUS のままにしない**） | 内側 domain workflow subgraph の起動と結果マージ |
| `post_process` | `CardAssemblyNode` | `FunctionNode` | `VERIFIED_EXTERNAL` | カード組立・S-3 非断定契約ゲート・根拠 ID 実在検証・レビュー優先度の決定論算出・`formatted_output` 畳み込み |
| `finalize` | `FinalizeNode` | framework | — | framework 既定 |

`add_edges()` / `route()` は **override しない**（framework 既定のパイプラインで足りる）。

`GraphNode` の必須抽象メソッド（3 つとも実装）:

| メソッド | 本テンプレでの実装 |
|---|---|
| `get_subgraph()` | 内側 `PrecedentWorkflowGraph` を **lazy 生成しキャッシュ**（毎回新規生成しない） |
| `extract_input()` | 親 state の必要フィールドを **JSON 直列化した単一 str**（`BaseGraph.invoke()` は str 入力のため） |
| `merge_output()` | **実行されたモードのフィールドだけ**を返す（None 埋め禁止）。`error_log` は非空時のみ。`node_history` は返さない（reducer 管理） |

### 4-2. 内側グラフ（`src/graph/domain_workflow_graph.py` — `BaseGraph` 継承）

```
START → mode_router ─┬─ ingest_stage         → END
                     ├─ invoke_reconcile     → END
                     ├─ invoke_deterministic → END
                     ├─ feedback_stage       → END
                     └─ error                → END
```

| 内側ノード | 責務 |
|---|---|
| `mode_router` | `mode`（`ingest` / `invoke` / `feedback`）と `eligibility`（§5）で分岐。上流 `status="error"` はパススルー |
| `ingest_stage` | 過去裁定行の品質ゲート → 書込前サニタイズ → 正規化 → ledger 書込 → `ingest_summary`（`written_ids` / `rejected[]`） |
| `invoke_reconcile` | **相反前例型のみ**。候補内自由記述 × 今回記述を LLM で意味照合し、`claims` / `missing_information` / `confirmation_questions` を根拠 ID 付きで起草 |
| `invoke_deterministic` | 一意確定型・前例なし型。**LLM を呼ばず**、決定論描画の `precedent_facts` と定型の確認質問だけを返す |
| `feedback_stage` | 確定裁定 ＋ `adopted_precedent_ids` / `rejected_precedent_ids` / `decisive_difference_evidence_ids` を ledger へ書込。ライフサイクル遷移（§9-3） |

> 内側グラフの `route()` / `get_output()` は **自スキーマ（`PrecedentWorkflowState`）で型注釈する**。
> `AgentState` で注釈するとカスタム欄が射影で消えて誤 route する。

---

## 5. 候補絞り込みと conflict predicate（決定論）

> 🔴 **順序が本質**: conflict predicate は **切り詰め前の全一致集合**に対して評価する。
> 「先に `top_k` で切ってから相反判定する」実装は、少数派 verdict が打切り境界の外に落ちた瞬間に
> `uniquely_determined` へ誤分類し、**相反しているのに LLM 照合を回避して決定論表示に落とす**
> （＝本 AG の中核価値と LLM 必須性を同時に壊す）。**5-1 → 5-2 の順で書かない。5-2 → 5-3 の順で実装する。**

> **API は `parts/accumulation-ledger` v0.1.1 の実シグネチャに従う**（2026-07-30 実コード確認）。
> `LedgerService.search()` が受けるのは `scope` / `record_kind` / `classification` / `statuses` /
> `include_expired` のみで、**`conflict_key` や `valid_at` という引数は存在しない**。
> 相反判定は部品の `LedgerService.conflicts(scope)` が既に提供しているので**自前で集約クエリを書かない**。

### 5-1. 一致集合の定義（切り詰めなし）

```python
scope   = {"business_unit_id": <検証済みリクエスト値>}

# ① 相反グループ（部品が提供する決定論クエリ。active + disputed を対象にする）
groups  = ledger.conflicts(scope)              # -> [{conflict_key, record_ids, verdict_codes, records}]
group   = match(groups, exception_category=..., series_id=...)   # conflict_key で引き当て

# ② 候補行の取得（exception_category は classification、series_id は domain scalar）
matched = ledger.search(scope, record_kind="adjudication",
                        classification={"exception_category": exc.exception_category})
matched = [r for r in matched if r["series_id"] == exc.series_id]

# ③ 有効期間の適用 — 「例外を検知した時点で組織が持っていた知識」に揃える
matched = [r for r in matched
           if r["occurred_at"] <= exc.detected_at
           and (r["valid_to"] is None or r["valid_to"] >= exc.detected_at)]
```

- `search()` の既定 `statuses` は **`("active", "disputed")`**。**`disputed` を外さない** —
  相反前例こそ本 AG の対象であり、外すと候補が構造的に空になる。
- 🔴 **期間の比較は「時刻」で行う。ISO 文字列の辞書順比較にしない。**
  `2026-07-30T09:00:00+09:00` は `2026-07-30T06:00:00+00:00` より**辞書順では後・実時刻では 3 時間前**であり、
  文字列比較にすると**有効だった前例が黙って候補から落ちる**（＝conflict predicate が静かに壊れる）。
  `detected_at` / `occurred_at` / `valid_to` は **UTC オフセットを明示した ISO-8601** としてパースし、
  aware datetime で比較する（`Z` 表記も受理）。
  **naive（オフセット無し）・日付のみ・解析不能は曖昧なので受理しない**（fail-closed・`intake_rejected`）。
- 期間フィールドは部品の実体に合わせる: **`valid_from` は存在しない**。適用開始は `occurred_at`、
  終了は `valid_to`（`None` = 無期限）。期間満了の `expired` 化は部品の `expire_due(scope, now)` に委ねる。
- 監査再現（「あの時点の判断根拠は何だったか」）が要る場面では部品の
  `as_of(scope, timestamp)` を使う。`invoke` の通常経路では ③ のフィルタで足りる。

### 5-2. conflict predicate（相反前例型の同定基準 — **切り詰め前の集合**に対して評価）

```python
counts   = Counter(r["verdict_code"] for r in matched)   # ③ 適用後・top_k 適用前
V        = set(counts)
minority = sum(counts.values()) - max(counts.values())

eligibility =
  "conflicting"          if len(V) >= conflict_min_distinct_verdicts (既定 2)
                         and minority >= conflict_min_minority_count (既定 1)
  "uniquely_determined"  elif sum(counts.values()) >= 1
  "no_precedent"         else
```

- 部品の `conflicts()` は**有効期間を見ない**（scope 全体で conflict_key ごとにグループ化するだけ）。
  本 AG は「今回の検知時点で有効な前例」に限定するため、**最終判定は ③ 適用後の `matched` で行う**。
  `conflicts()` は相反キーの当たりを付ける用途（および `/feedback` 後の再評価）に使う。

### 5-3. 候補選抜（LLM に渡す集合の作り方）

```
candidates = []
for vc in V:                                   # ① 各 verdict_code から最新 1 件を必ず確保
    candidates += latest(matched, verdict_code=vc, n=1)
candidates += fill(matched - candidates, order=decided_at desc,
                   upto=candidate_top_k)       # ② 残枠を新しい順で埋める（既定 top_k=8）
```

- ① により、**少数派 verdict が打切りで消えることは構造的に起きない**。
- `len(V) > candidate_top_k` の場合は `candidate_top_k` を **`len(V)` まで自動的に引き上げる**
  （相反の全種別を必ず 1 件は見せる。上限は `candidate_hard_cap`＝既定 24）。
  `candidate_hard_cap` すら超える場合も **`eligibility` は `conflicting` のまま**とし、
  `degradation_reason="verdict_codes_truncated:<n>"` を積む。**打切りを理由に `uniquely_determined` へ降格しない。**
- 通常の残枠打切りは `degradation_reason="candidates_truncated:<切捨件数>"`（黙って捨てない）。
- 受入テスト（§8 ⑤）: 「同一 verdict が `top_k` 件、境界外に別 verdict が 1 件」の fixture で
  `eligibility == "conflicting"` かつ少数派前例が `candidates` に含まれることを検証する。

| eligibility | 経路 | LLM |
|---|---|---|
| `conflicting` | `invoke_reconcile` | 呼ぶ |
| `uniquely_determined` | `invoke_deterministic` | **呼ばない**（決定論表示へ退避。コスト抑制） |
| `no_precedent` | `invoke_deterministic` | **呼ばない**（不足情報と確認質問のみ） |

閾値 2 つは `config/agent.yaml` の `agent.config` に置き、利用者が自環境で調整する前提の初期値とする。

---

## 6. 根拠レジストリ（evidence registry）

`pre_process` と `main` の決定論部で**先に構築**し、LLM には「このレジストリの ID からのみ引用せよ」と渡す。
LLM が新しい ID を作れないため、`post_process` の実在検証で捏造 ID を機械的に落とせる。

| evidence_id 形式 | 出所 |
|---|---|
| `EV-ADJ-<adjudication_id>-S<seq>` | 過去裁定の裁定理由・運用メモの span |
| `EV-NOTE-<exception_id>-S<seq>` | 今回の例外に付された運用メモの span |
| `EV-REV-<notice_id>-S<seq>` | 改訂告知の span |
| `EV-META-<series_id>-<field>` | 系列メタの構造化属性 |

> 🔴 **識別子に `uuid4()` を使わない。** SDK の S-2 PII ゲートが uuid の桁列を電話番号等と誤検知して
> `[MASKED]` に**破壊的に書き換える**既知欠陥がある。ID は英大文字・数字・ハイフンのみで構成し、
> 英字プレフィックスを必ず持たせる。`payload_ref` も同じ規則に従う。

### 6-1. レジストリのエントリ契約（span を再解決できる最小十分な locator）

レジストリ本体（State 上）は次のフィールドのみを保持し、**本文テキストは載せない**（checkpoint 隔離。§9-1）。
本文は `execute()` 内で payload store から一時解決する。

| フィールド | 型 | 意味 |
|---|---|---|
| `evidence_id` | `str` | 上表の形式 |
| `source_kind` | `str` | `adjudication` / `exception_note` / `revision_notice` / `series_meta` |
| `source_id` | `str` | `adjudication_id` / `exception_id` / `notice_id` / `series_id` |
| `field_name` | `str` | **どの列の span か**（`rationale` / `operational_note` / `body` …）。`EV-ADJ-` は裁定理由と運用メモの 2 列を持つため、これが無いと再解決できない |
| `projection_kind` | `str` | 固定 `"safe"` — **span は必ずサニタイズ後の safe projection 上で採る**（§9-2 の 3 値分離）。raw に対して採ると、マスク・訂正・`redact` 後にオフセットがずれる |
| `offset_unit` | `str` | 固定 `"utf8_codepoint"`（バイト単位と混同しない） |
| `span_start` / `span_end` | `int` | safe projection 上の開始・終了位置 |
| `content_hash` | `str` | safe projection 本文の SHA-256 先頭 16 文字（英数字） |

**再解決時の検証**: `content_hash` が一致しなければその根拠 ID は**無効**とし、
参照している claim を落として `degradation_reason="evidence_stale:<evidence_id>"` を積む（fail-closed 側に倒す）。
本文は State にもログにも戻さない。

### 6-2. レジストリ構築時の fail-closed 条件

| # | 条件 | 理由 |
|---|---|---|
| ① | **span 長 < `min_span_chars`（既定 8）は登録しない** | 空文字・1 文字の span は `text in source` が常に真になり、逐語照合が **vacuous**（何も検証しない）になる。長さ下限が無い照合は「照合しているつもり」の典型 |
| ② | `source_kind` は **allowlist**（`adjudication` / `exception_note` / `revision_notice` / `series_meta` の 4 値のみ）。未知値は登録しない | 未知 kind を作って「空ではない」と主張させる回避を塞ぐ |
| ②-bis | **kind ごとの関係述語**（下表）を満たさない行には evidence ID を**発行しない** | kind allowlist だけでは「実在するが今回と無関係な source」を排除できない。**別系列の改訂告知**も `revision_notice` として通ってしまう |
| ③ | `span_end <= span_start` / projection 外オフセットは登録しない | 不正 span は照合を無意味化する |

**kind ごとの関係述語（レジストリ発行の必要条件）**

| source_kind | 述語 | `field_name` allowlist |
|---|---|---|
| `adjudication` | `source_id ∈ candidate_ids`（§5-3 の候補集合） | `rationale` / `operational_note` |
| `exception_note` | `source_id == 当該 exception_id` | `operational_note` |
| `series_meta` | `source_id == 当該 series_id` | `unit` / `publisher` |
| `revision_notice` | `notice.series_id == 当該 series_id` **かつ** aware datetime として解釈した `notice.effective_date <= exception.detected_at`。将来発効・日時不正の告知は根拠 registry に入れず、本文も resolve しない | `body` |

→ このため **`/invoke` の `revision_notices` 入力に `series_id` を必須フィールドとして追加**する（§11-2）。
`series_id` を持たない改訂告知は受理しない（どの系列の告知か決められないものは根拠にできない）。

**PB に必ず入れるケース**: 「**実在するが今回の例外と無関係な `source_id` を引用した claim**」。
ID 実在検証だけを実装していると、このケースは素通りする（存在する ID なので ② を通る）。
→ claim の `evidence_ids` は、**当該例外の候補集合から構築されたレジストリ**に限定されていること
（別の例外・別系列の根拠 ID を引けないこと）を検証する。

---

## 7. 出力契約 — 前例適用カード（closed allowlist）

`post_process` が `formatted_output` に畳み込んで返す（`AgentBaseGraph.get_output()` は
`formatted_output or result` しか拾わないため、カスタム State 欄は透過しない）。

```jsonc
{
  "mode": "invoke",
  "card_kind": "reconciled" | "deterministic_display",
  "exception_ref": {
    "exception_id": "...", "exception_category": "...", "series_id": "...", "detected_at": "..."
  },
  "eligibility": {
    "kind": "conflicting" | "uniquely_determined" | "no_precedent",
    "distinct_verdict_codes": ["...", "..."],
    "candidate_count": 5
  },
  "precedent_facts": [                       // 決定論描画。LLM は関与しない
    { "adjudication_id": "...", "verdict_code": "...", "decided_at": "...",
      "occurred_at": "...", "valid_to": null, "rationale_evidence_ids": ["EV-ADJ-..."] }
  ],
  "claims": [                                // LLM 起草（散文）＋ 関係の記述に固定
    { "claim_id": "C1", "precedent_id": "...",
      "support_status": "supports_application" | "blocks_application" | "insufficient" | "unknown",
      "statement": "…", "evidence_ids": ["EV-ADJ-...", "EV-NOTE-..."] }
  ],
  "missing_information": [                   // requirement_id は pre_process が決定論生成（§7-3）
    { "item_id": "M1", "requirement_id": "REQ-SERIES-META-ABSENT",
      "statement": "…", "evidence_ids": ["EV-NOTE-..."] }
  ],
  "confirmation_questions": [                // 送信しない質問草案
    { "question_id": "Q1", "statement": "…", "evidence_ids": ["EV-REV-..."] }
  ],
  "review_priority": {
    "level": "high" | "medium" | "low",
    "basis": { "distinct_verdict_count": 2, "candidate_count": 5, "missing_information_count": 3 }
  },
  "degradation_reason": null
}
```

### 7-1. 構造的に存在しないフィールド（非断定契約の中核）

出力スキーマに **今回の裁定を表すフィールドを一切定義しない**。宣言済みキー完全一致の
**allowlist** で判定し、宣言外キーは top-level・ネストの両方で **reject** する。
設計意図として排除する概念は次のとおり（＝実装のチェックリストではない）:

今回の裁定・推奨・処置・合否・適合・承認・重大度に相当するあらゆるフィールド
（`current_verdict` / `recommendation` / `decision` / `quality_result` / `conformity` / `action` / `assessment` 等）。

> 判定は **allowlist** で行う。**断定語 denylist は補助線としても持たない**
> — 言い換えで抜けるうえ「網羅している」という誤った安心を生む（既存実装の設計方針）。

### 7-2. `support_status` の意味（関係の記述に固定）

| 値 | 意味（**今回の裁定を述べない**） |
|---|---|
| `supports_application` | 「前例 X の適用を**支持する事実**が、指定根拠に存在する」 |
| `blocks_application` | 「前例 X の適用を**阻む差異**が、指定根拠に存在する」 |
| `insufficient` | 「判断に必要な情報が指定根拠に不足している」 |
| `unknown` | 判定できない（**fail-closed の既定値**） |

### 7-3. レビュー優先度の決定論算出

> 🔴 **LLM が返した件数を優先度の入力にしない。** LLM が不足項目を分割・統合して返すだけで優先度が
> 揺れるなら「決定論算出」ではなく、S-3 での再計算も**同じ LLM 生成値を再計算するだけ**で何も検証しない。
> 優先度の入力は **LLM を呼ぶ前に確定している値だけ**に限る。

**`requirement_id`（決定論の不足要件）** — `pre_process` が有限集合から生成する。LLM は関与しない。

| requirement_id | 生成条件 |
|---|---|
| `REQ-SERIES-META-ABSENT` | 対象 `series_id` の系列メタが KB に無い |
| `REQ-OPERATIONAL-NOTE-ABSENT` | 今回の例外に運用メモが付いていない |
| `REQ-REVISION-NOTICE-ABSENT` | 有効期間内に系列改訂があるのに改訂告知が入力に無い |
| `REQ-RATIONALE-EMPTY:<adjudication_id>` | 候補前例の裁定理由が空（コードのみの記録） |
| `REQ-PAYLOAD-UNRESOLVED:<ref>` | 本文の payload 解決に失敗した |
| `REQ-CANDIDATES-TRUNCATED` | 候補が打ち切られた |
| `REQ-EVIDENCE-STALE:<evidence_id>` | `content_hash` 不一致で根拠が無効化された |

```
requirement_count = |unique(requirement_ids)|          # pre_process が確定・LLM 非依存

score = w_verdict     * max(0, distinct_verdict_count - 1)   # 5-2 の集約値（全一致集合）
      + w_missing     * requirement_count
      + w_candidates  * (1 if truncated else 0)

level = "high"   if score >= priority_high_threshold   (既定 4)
        "medium" if score >= priority_medium_threshold (既定 2)
        "low"    otherwise
```

- 重み・閾値は `agent.config`（既定 `w_verdict=2, w_missing=1, w_candidates=1`）。
- **`support_status` / 確定裁定 / LLM 起草の `missing_information` 件数は入力に取らない。**
- LLM は `missing_information[]` の `statement`（人間向けの言い換え）を書けるが、
  **各項目は既存の `requirement_id` を必ず参照**し、`requirement_id` を新設できない
  （未登録 `requirement_id` は S-3 で reject）。
- `post_process` の S-3 ゲートは `requirement_ids` と 5-2 の集約値から**同じ式で再計算して一致を検証**する。
- 受入テスト（§8 ⑥）: **同一入力に対し LLM 応答の `missing_information` 件数だけを変えた 2 ケースで
  `review_priority` が変わらない**ことを検証する。

---

## 8. 非断定契約の受入テスト（Design 入場条件・decision §12-4）

固定 fixture ごとに以下 4 点を検証する。

| # | 検証 |
|---|---|
| ① | 出力スキーマに**今回の裁定を表すフィールドが存在しない**（宣言外キー注入が reject される。ネスト内密輸も） |
| ② | 各 claim は定義済み `support_status` と **1 件以上の根拠 ID** のみを持つ（0 件 → reject） |
| ③ | `precedent_facts` は ledger レコードから**決定論描画**され、LLM 出力から生成されない（LLM 応答に細工を仕込んでも `verdict_code` / `adjudication_id` が変化しない） |
| ④ | 過去裁定の結果コード・理由の**引用は根拠 ID 付きで許容**される（過剰防御で moat を潰していないことの逆方向テスト） |
| ⑤ | **打切り境界の外に少数派 verdict がある** fixture で `eligibility == "conflicting"` を維持し、少数派前例が候補に含まれる（§5-3） |
| ⑥ | LLM 応答の `missing_information` 件数だけを変えても `review_priority` が**変わらない**（§7-3） |
| ⑦ | **逐語コピーガード**: `statement` に根拠本文の連続 `verbatim_guard_chars`（既定 60）文字以上の literal 部分文字列が含まれる claim は reject される（§8-1） |
| ⑧ | **数値忠実性の双方向照合**（§12-3-bis）: ①`statement` の数値を入力と違う値へ書き換えた LLM 応答が reject される ②数値を散文だけに書いて `numeric_facts` に載せない回避も reject される ③`六十二パーセント` / `６２％` / `62 %` が同一視される ④**単位隣接（`999999円`）で抽出が空集合にならない**こと（空集合は合格でなく reject） |
| ⑨ | **実在するが無関係な `source_id` の引用**（別の例外・別系列の根拠 ID）が reject される（§6-2）。ID 実在検証だけでは通ってしまうケース |
| ⑩ | **`_verification` ブロックの欠落・型不正・未知 `mode` で `raise`**（fail-open に落ちない・§12-3 ⑩） |
| ⑪ | **span 長 < `min_span_chars` の根拠が登録されない**（逐語照合が vacuous にならない・§6-2 ①） |
| ⑫ | **永続層 PB**: ①`retrieval_text` と model 可視 projection に PII / credential が入らない ②スコープ A の record がスコープ B の `search()` / `conflicts()` / `get()` から見えない ③`redact` / `purge_expired_raw` 後に `*_raw` が実際に消える（§9-2） |

### 8-1. 自由文 `statement` に置く境界（採用しなかった案の記録）

`statement` を廃止して LLM 出力を enum ＋ 根拠 ID だけに絞り、表示文を決定論テンプレートで描画する案を検討したが
**採用しない**。理由:

- 既存実装で一般的なのは「**LLM が散文を書き、evidence_map と引用 ID 実在検証で人間レビューを支援する**」穏当設計であり、
  「LLM に文字列を書かせない」構造化描画は過剰設計として採用されなかった。
  既定にしてはならない一選択肢であり、採るのは「1 文字の数値ミスも許されない規制文書」等で**明示要求があるとき**に限る。
- 本 AG が扱うのは「前例の適用を阻む差異は何か」という**解釈の言語化**であり、
  enum ＋ ID だけに落とすと担当者が読む価値が消えて Tool に退化する
  （この提案は過剰防御で中核価値を損ないかねないため採用しない）。

代わりに、自由文が持ち込む**2 つの具体的な危険だけ**を境界で塞ぐ:

| 危険 | 対処 |
|---|---|
| 根拠本文の丸写しで payload_ref 隔離が checkpoint 上で崩れる | **逐語コピーガード**（`statement` 内に根拠本文の連続 60 文字以上の literal 一致があれば reject）＋ `statement` 長上限 `statement_max_chars`（既定 400） |
| 生成文に PII / credential が混じる | カード内**全文字列**に `detect_pii`（マスク）と `detect_credentials`（block）を**再帰適用**（§12 S-3 ⑤） |

**残余（設計として受容する）**: LLM が言い回しで今回の結論に近い散文を書く可能性は 0 にできない。
**禁止語 lexicon による網羅は追わない**（言い換えで抜けるうえ「網羅している」という誤った安心を生む）。
緩和は ①今回の裁定フィールドが構造的に存在しない ②全 claim が `support_status` と根拠 ID に縛られる
③裁定の確定は人間が行う、の 3 点。この責務分界は `docs/07_operation_guide.md` に明記する。

> ⚠️ ゲートテストは **本番 `config/agent.yaml` を読んで**書く（テスト専用 config で緑にしない）。
> fail-closed 側テストには必ず **provisioned 対照実験**（正常系が success になること）を添える。
> 仕上げに**変異検査**（production 側のガードを外すとテストが赤くなる）を行う。

**残余リスクの正直な位置づけ**: LLM が `statement` の散文で「今回も許容してよい」に近い言い回しを
書く可能性は、構造ゲートでは 0 にできない。本設計はそれを **①今回の裁定フィールドが構造的に存在しない
②全 claim が関係記述と根拠 ID に縛られる ③裁定の確定は人間が行う** の 3 点で緩和し、
**禁止語 lexicon による網羅は追わない**。この残余は `docs/07_operation_guide.md` の責務分界に明記する。

---

## 9. 蓄積 KB の統治

### 9-1. 層 1 — checkpoint 隔離（`payload_ref`）

- 入口アダプタ（`src/api/server.py`）が**自由記述本文**（`operational_note` / `rationale` / 改訂告知 `body`）を
  transient payload store に預け、State には**不透明 ref のみ**を載せる。
- `validated_input` に**生本文を入れない**（構造化フィールドと ref のみ）。
  処理ノードが `validated_input` へ生本文を書き戻すと checkpoint に残留する。
- 本文の解決は各ノードの `execute()` 内で一時的に行い、戻り値には載せない。
- ingest / feedback は解決失敗時 **fail-closed**（1 件も書かない）。invoke は縮退（`degradation_reason`）。
- 🔴 **checkpoint スクラバは allowlist で書く。** 「機微キーを列挙して退避する」denylist は必ず漏れる
  （呼び出し側が送りうるキーを列挙し切れない）。**checkpoint に載せてよいキーだけを列挙**し
  （`_CHECKPOINT_SAFE_KEYS`）、それ以外の文字列値は**中身を問わず** transient store へ退避する。再帰で全ノードを走査。
  非 JSON もそのまま返さず `__unparsable__` envelope で退避する。
  載せてよい基準 = 「決定論部が構造化フィールドとして読む」**かつ**「人が文章を書き込めない型」
  （ID・列挙値・日付・コード）。**迷ったら足さない**（新キーは自動的に退避側＝fail-safe）。
  🔴 **キー名の allowlist ではなく JSON Pointer 単位の closed schema にする。**
  キー名だけで許可すると、自由形式の `metrics` に文字列が入った瞬間 PII が checkpoint に残る。
  逆に map 全体を退避すると後段の決定論処理が壊れる（既存実装で回帰が確認されている）。
  各 path に**許容型まで**宣言する（例: `/exception_ref/exception_id` = str・ID 形式、
  `/exception_ref/metrics/*` = number のみ、`/payload_refs/*` = str・ref 形式、
  `/evidence_registry/*/span_start` = int）。宣言外 path の文字列値は**中身を問わず** transient store へ。
  **適用点は checkpoint writer の直前 1 箇所に集約**する（各ノードに散らすと適用漏れが起きる）。
  PB は「各ノード実行後の**実 checkpoint** を復元し、ネスト dict・配列・LLM 応答に仕込んだ
  PII / 本文センチネルが存在しない」ことを確認する（`repr()` 検索ではなく実デシリアライズ）。

### 9-2. 層 2 — 永続 KB の統治

| 項目 | 方針 |
|---|---|
| スコープ | `business_unit_id` を宣言スコープキーとし、**全読み書きで必須・欠落は fail-closed**（検索せず `status="error"`） |
| 顧客分離 | **接続レベル**（顧客専用 DB/KB 接続。認証 `caller_id` → 認可顧客 → provision 済み接続）。入力の顧客 ID は**照合のみ**。default KB は持たない |
| 書込前サニタイズ（PII） | 全書込経路（`/ingest` も `/feedback` も）で本文を SDK 標準 `detect_pii` に通しマスクする（部品の `sanitize_text()` が担当）。**日本語氏名補完サニタイザは実装しない**（共通方針。既知限界は `docs/07` に明記） |
| 🔴 書込前 credential スキャン | 部品の `sanitize_text()` は **PII しかマスクしない**。本文は `execute()` 内で解決するため **FunctionNode の S-2（実行前）では credential を検査できず**、そのまま `LedgerService.write()` すると credential が `*_raw` / `*_audit` / `*_safe` に**永続化される**。→ **ingest / feedback の write 直前に、解決済みの全自由文へ `detect_credentials` を適用し、検出したら当該書込を丸ごと reject**（独自 regex は書かない）。PB は backend の raw/audit/safe・`search()` 結果・`retrieval_text` すべてに credential センチネルが残らないことを確認する |
| 本文 3 値分離 | `*_raw`（アクセス制御下）/ `*_audit`（監査用）/ `*_safe`（model 可視 projection）。**projection は allowlist** — 未宣言キーが素通りしない |
| 保持・訂正・削除 | 原文の保持期間・`redact` / `purge` アクション・原文 resolve の監査イベントを持つ |
| 監査 | 全書込・全 resolve で `emit_trace_event()` |

> 🔴 **永続層に対する PB テストを別に置く（State 非格納 PB では永続層を守れない）。**
> checkpoint 側の PB（センチネル not-in）は「State に載っていないこと」しか証明しない。
> KB に何が書かれ続けるかは別の層であり、次の 3 点を実 backend（または backend 契約テストを満たす
> in-memory 実装）に対して検証する:
> ① **PII / credential が索引テキスト（`retrieval_text`）と model 可視 projection に入っていないこと**
> ② **スコープ A で書いた record がスコープ B の `search()` / `conflicts()` / `get()` から見えないこと**
> ③ `redact` / `purge_expired_raw` の後に `*_raw` が実際に消えていること
> ②は宣言スコープキーを 1 軸しか持たない本 AG でも必須（省略可能にした瞬間、呼び忘れが全件検索になる）。

### 9-2-bis. `accumulation-ledger` の実契約に合わせる点（v0.1.1 実コード確認 2026-07-30）

| 項目 | 契約 | 本 AG での扱い |
|---|---|---|
| 本文 3 値分離の対象 | 組込みの `body` / `rationale` と、**`domain_fields` に `"text"` と宣言した列だけ**が `*_raw` / `*_audit` / `*_safe` に分離される | 裁定理由は組込み `rationale` に載せる。運用メモは `domain_fields={"operational_note": "text"}` と**必ず宣言**する |
| `_public_record` の projection | **落とすキーを列挙する denylist 方式**。未宣言の自由文列は raw のまま model 可視 projection に載る | **自由文は必ず宣言する**。宣言漏れが即ちデータ漏れになる（部品の既知の性質） |
| `retrieval_text` | `_reject_retrieval_text_content()` が `verdict_code` や本文の内容混入を reject する | 索引テキストは**構造化属性のみ**（例外区分・系列・単位）。裁定理由を混ぜない |
| `record_id` | `_mint_record_id()` は 英字 prefix ＋ base32（**uuid hex ではない**） | S-2 の `[MASKED]` 破壊書換の懸念なし。`prefix="adj"` |
| `ai_suggested_verdict` / `operator_edited` | 部品が持つ任意フィールド（AI 提案の裁定を記録できる） | 🔴 **使わない。** 本 AG は今回の裁定を出さないので、AI 提案裁定を書けば非断定契約（§7-1）を側面から破る。書込パスで常に未設定にし、テストで固定する |
| 期間満了 | `expire_due(scope, now)` が `valid_to` 経過を `expired` にする | 定期実行に委ねる。テンプレで自前実装しない |

### 9-3. ライフサイクル（対称／非対称の区別 — 事故防止の要）

| 種別 | 例 | 扱い |
|---|---|---|
| **対称** | 同一キーで `verdict_code` が矛盾し、双方 `decision_source="human"` かつ lineage なし | **新旧とも `disputed`**（優劣を付けない）。※本 AG では**これが正常状態**であり conflict predicate の入力になる |
| **非対称①** | 系列メタ・改訂告知の版が上がった | **旧のみ `disputed`**（再評価要）。新は `active` |
| **非対称②** | 人が名指しで訂正（`supersedes_record_id` あり） | 名指しした 1 件を `superseded`、新は `active`。**どの自動判定よりも優先** |
| 期間満了 | `valid_to` 経過 | `expired` |

- `search()` の既定対象は **`active` + `disputed`**。`superseded` / `expired` のみ明示指定で取得。
- `conflicts()`（矛盾候補の決定論クエリ）も **`active` + `disputed`** を対象にする
  （`active` のみだと auto モードで構造的に常に空になる）。

> 🔴 **`supersede_mode` は `"review_required"` を選ぶ（`"auto"` にしない）— 実害あり**
>
> 部品の `_apply_write_lifecycle()` は `supersede_mode="auto"` のとき、同一 `conflict_key` の
> **active な既存レコードを「新しい方が正しい」として自動 `superseded` にする**（`automatic_newer_record`）。
> `search()` の既定 statuses は `("active","disputed")` で `superseded` を含まないため、
> **自動 supersede された過去裁定は既定検索から消える**。
>
> 本 AG では**過去裁定の 1 件 1 件が前例という資産**であり、同じ裁定が繰り返されている履歴そのものが
> conflict predicate の入力になる。`auto` のままだと同一 verdict の古い裁定が次々 `superseded` になり、
> **verdict 分布を過小評価して `uniquely_determined` へ誤判定する**（§5 冒頭の High-1 と同じ結末に、
> 別経路で到達する）。
>
> `"review_required"` にすると、非対称①（基準版更新）と対称（human×human で verdict 相違）の
> 自動遷移は**そのまま働き**、それ以外の同一キー peer は `ledger_supersede_review_required` イベントを
> 出すだけで `active` のまま残る。名指し訂正（`supersedes_record_id`）による `explicit_correction` も
> mode に関係なく働く。**本 AG が要る挙動はこれ。**
>
> ※ 部品 USAGE の「通常は auto、承認済み基準表への当てはめ型だけ review_required」という指針は、
> *1 事案 1 レコード*型（インシデント想起・検収証跡）を前提にしている。**前例コーパス型はこの前提に
> 当てはまらない**。この読み替えは部品側の USAGE に追記する価値があるので、起票候補として記録する
> 設計上の改善候補として記録し、テンプレ側で部品コードを書き換えることはしない。

### 9-4. 遷移の適用範囲とトランザクション境界

遷移そのものは部品が実装している（`_apply_write_lifecycle` / `_explicit_correction_target` /
`dispute` / `supersede` / `un_supersede` / `expire_due`）。**テンプレ側で再実装しない。**
以下はテンプレが満たすべき前提と、部品に委ねきれない部分の契約である。

**部品の実挙動（v0.1.1 実コードで確認）— 設計はここに合わせる**

| 実挙動 | 出典 |
|---|---|
| 明示訂正（`supersedes_record_id`）を処理した**後も** `_apply_write_lifecycle()` は必ず走る | `ledger.py` `write()` |
| peer は「同一 conflict_key の **active** 行」全部。**有効期間の重なりは見ない** | `ledger.py` `_apply_write_lifecycle()` |
| `LedgerBackend` Protocol は `insert` / `get` / `update` / `query` / `purge_raw` のみ。transaction はテンプレ側 batch seam が所有する | `backend.py` / `src/services/runtime.py` |

**部品単体では次の保証を主張しない:**
optimistic lock ／期間重複に限定した遷移／
「明示訂正が成立したら下位の自動判定を走らせない」。

ingest のバッチ原子性だけは `ingest-row-pipeline` v0.4.0 の `write_batch` を
`RuntimeServices.write_ledger_batch()` へ配線してテンプレ側で閉じる。ローカル/STG の
`SQLiteLedgerBackend.run_atomically()` は `BEGIN IMMEDIATE` から commit/rollback までを所有し、
顧客 backend は同等の `ledger_batch_writer` を注入する。どちらも無い場合は
`atomic_ledger_batch_writer_not_configured` で fail-closed とし、行単位 write へ退化させない。

**受け入れる帰結と緩和策**

| 帰結 | 緩和 |
|---|---|
| 明示訂正した直後でも、同一 conflict_key に別の active peer があり human×human で verdict が異なると、**新レコードも `disputed` になり得る** | 既定 `search()` は `active` + `disputed` を対象にするため**想起は成立する**。`provenance.feedback_source_record_id` で訂正系列は追跡できる |
| 有効期間が重ならない裁定も自動遷移の対象になり得る | 期間の絞り込みは**読み出し側**（§5-1 ③）で行う。書込側の status は「候補に出すか」を決めないため実害は限定的 |
| feedback receipt と ledger が別 backend の場合は原子でない | production は receipt unique key と ledger write を同一 transaction に置く。Test/STG の in-memory receipt は同一 key の並行実行を直列化する |

**部品拡張の候補（ユーザー判断・テンプレ側では実装しない）**:
`LedgerBackend` への transaction / conditional-version update、`_apply_write_lifecycle()` への
期間重複 predicate、明示訂正時の自動 lifecycle 抑止。**3 テンプレ以上で再出したら部品側で対応する**のが筋。

受入テストは**実挙動に対して**書く: 各遷移の前後 status 集合を fixture 化し、
「対称 conflict で `search()` が 0 件にならない（`disputed` が既定対象に入っている）」を必ず含める
（対称／非対称の取り違えで `active` が 0 件になる回帰が確認されている）。

---

## 10. State 定義

`src/schemas/state.py` — `AgentState` を継承した flat TypedDict（`total=False`）。
**primitives と JSON 直列化可能型のみ**。Pydantic / dataclass / 任意 Python オブジェクト / credential は禁止。

| フィールド | 型 | 用途 |
|---|---|---|
| `mode` | `str` | `ingest` / `invoke` / `feedback` |
| `business_unit_id` | `str` | 宣言スコープキー（検証済みリクエスト値。**`ctx` からは取れない**ため State 経由） |
| `customer_ref` | `str` | 認可済み顧客の照合用参照（接続選択には使わない） |
| `exception_ref` | `dict` | `exception_id` / `exception_category` / `series_id` / `detected_at` / `metrics` |
| `payload_refs` | `dict[str, str]` | 自由記述本文の不透明 ref（英数字・ハイフンのみ） |
| `candidate_ids` | `list[str]` | 絞り込み済み過去裁定 ID |
| `eligibility` | `dict` | `kind` / `distinct_verdict_codes` / `candidate_count` |
| `evidence_registry` | `list[dict]` | `evidence_id` / `source_kind` / `source_id` / `span_start` / `span_end`（**本文なし**） |
| `precedent_facts` | `list[dict]` | 決定論描画済みの過去裁定事実 |
| `claims` | `list[dict]` | LLM 起草 claim（`support_status` ＋ `evidence_ids` 必須） |
| `missing_information` | `list[dict]` | 不足情報 |
| `confirmation_questions` | `list[dict]` | 確認質問草案（送信しない） |
| `review_priority` | `dict` | `level` / `basis` |
| `ingest_summary` | `dict` | `written_ids` / `rejected[]`（`/ingest` のみ） |
| `feedback_summary` | `dict` | `written_id` / `lifecycle_transitions[]`（`/feedback` のみ） |
| `degradation_reason` | `str \| None` | 縮退理由（`"+"` 連結） |

**State に置かないもの**: 自由記述の生本文・`InvocationContext`・接続文字列/DSN・API キー・確定裁定の生テキスト。

---

## 11. モード契約

| モード | HTTP | 入力 | 出力 | trust |
|---|---|---|---|---|
| `/ingest` | POST | 過去裁定行（CSV/JSON）＋ 系列メタ | `ingest_summary` | `VERIFIED_EXTERNAL` |
| `/invoke` | POST | 例外リスト ＋ 運用メモ ＋ 改訂告知（任意） | 前例適用カード（例外ごと・配列） | `VERIFIED_EXTERNAL` |
| `/feedback` | POST | 確定裁定 ＋ `adopted_precedent_ids` / `rejected_precedent_ids` / `decisive_difference_evidence_ids` | `feedback_summary` | `VERIFIED_EXTERNAL` |

`/ingest` と `/feedback` は顧客 KB へ**書き込む**ため、入口アダプタの middleware 認証を必須とする
（STG の standalone token で開けるのは read-only の `/invoke` のみ）。

### 11-0. 入口アダプタの置き換え

> 🔴 `src/api/server.py` が `input: str` を受けて `agent.invoke(req.input)` に**そのまま渡す**経路では、
> 運用メモ等の自由記述が `user_input` に載り、**§9-1 の checkpoint 隔離が成立しない**。
> 入口アダプタは以下の経路を実装する。

```
POST /invoke | /ingest | /feedback
  ↓ ① 入口認証（middleware / standalone token）→ trust と caller_id を確定
  ↓ ② 認可顧客の解決 → provision 済み KB 接続を確定（失敗なら 403。本文に触れない）
  ↓ ③ 入力形式 allowlist（§11-3）＋ サイズ上限チェック
  ↓ ④ 自由記述本文を payload store へ保存 → payload_ref を得る（本文はここから先へ渡さない）
  ↓ ⑤ canonical envelope（下記）を JSON 文字列に直列化
  ↓ ⑥ agent.invoke(<canonical JSON str>, ctx=...)   # BaseGraph.invoke() は str 入力
```

**canonical envelope（`BaseGraph.invoke()` に渡す唯一の形）**

```jsonc
{
  "schema": "ene-c2-123/v1",
  "mode": "invoke" | "ingest" | "feedback",
  "business_unit_id": "...",              // 検証済みリクエスト値（クライアントヘッダ由来は不可）
  "customer_ref": "...",
  "payload": { /* mode ごとに下表 */ }
}
```

### 11-2. モード別 I/O 契約

> 🔴 **`*_ref` はクライアントから受け取らない（S-3 裏口チャネル #2）。**
> HTTP リクエストが載せるのは**本文そのもの**（`operational_note` / `rationale` / `body`）であり、
> `payload_ref` は**サーバが payload store に預けた結果として発行する内部トークン**である。
> クライアントが `*_ref` を自己申告してきた場合は**無条件に破棄**して本文から再発行する
> （「既に ref があるならスクラブ済み」と早期 return すると、偽装 ref で層 1 スクラブを迂回され
> 生本文が checkpoint へ平文永続化される）。スクラブは冪等に、常に通す。
> 下表の「HTTP 入力」と「canonical envelope」は**別物**である。

| mode | HTTP 入力（クライアント → 入口アダプタ） | canonical envelope の `payload`（アダプタ → グラフ） | 出力（`formatted_output`） |
|---|---|---|---|
| `ingest` | `{ "adjudications": [ {adjudication_id, exception_category, series_id, occurred_at, valid_to, verdict_code, decided_at, decision_source, supersedes_record_id?, **rationale**, **operational_note**} ], "series_meta": [ {...} ] }` | 同左だが `rationale` / `operational_note` を **`rationale_ref` / `operational_note_ref`** に置換（本文はストアへ）。`decided_by` は認証済み `caller_id` で上書き | `{ "mode":"ingest", "ingest_summary": { "written_ids": [...], "rejected": [ {"row_index":n, "reason_code":"...", "detail":"..."} ], "lifecycle_transitions": [ {"record_id":"...", "from":"active", "to":"disputed", "cause":"symmetric"} ] } }` |
| `invoke` | `{ "exceptions": [ {exception_id, exception_category, series_id, detected_at, metrics, **operational_note**} ], "revision_notices": [ {notice_id, **series_id**, publisher, effective_date, **body**} ] }`（`series_id` 必須 — §6-2 の関係述語） | 本文を `operational_note_ref` / `body_ref` に置換 | `{ "mode":"invoke", "cards": [ <§7 のカード> ], "degradation_reason": null }` |
| `feedback` | §11-4 の 2 サブモード | §11-4 | `{ "mode":"feedback", "feedback_summary": { "kind":"adjudication"\|"correction", "written_id":"...", "recall_hit": true, "lifecycle_transitions": [...] } }` |

**共通契約**

- 🔴 **canonical envelope は「HTTP JSON を加工して作る」のではなく、mode ごとの厳格な request schema から
  既知フィールドだけを新しいオブジェクトへ投影して作る**（allowlist 投影）。
  再帰的に `*_ref` を削除する実装は、`metrics` や `series_meta[]` / `exceptions[]` の**配列要素やネスト dict**の中に
  紛れ込んだ ref を取り逃す。未知キーは投影されない＝自動的に落ちる（fail-safe）。
  `metrics` / `series_meta` も**閉じたスキーマ**で型まで宣言する。
- `payload_ref` は**サーバ発行の内部トークンのみ**（英字プレフィックス＋英数字ハイフン。`uuid4` を使わない）。
  ref そのものの**文字形式だけでは発行元を証明できない**ため、payload store は ref に
  **`{customer_ref, business_unit_id, session_id, field_kind, expiry}` を束縛**して保存し、
  `resolve()` は**全項目の完全一致**を要求する（他セッション・他用途で発行された ref を解決しない）。
- PB: ①top-level ②ネスト dict ③配列要素 ④未知キー ⑤他セッション発行 ref — **すべてで 422 または破棄**を確認。
- `exceptions` は最大 `max_exceptions_per_invoke`（既定 50）。超過は `422`。
- **部分失敗の扱い**: §11-2-bis のとおり。`ingest` / `feedback` は**書込の原子性を優先し
  全件 fail-closed**（1 件でも検証に落ちたら `rejected[]` を返して 1 件も書かない・`status="error"`）。
- **エラー形式**: `{ "error": {"code": "...", "message": "...", "detail": {...}} }`。
  HTTP は 400（スキーマ）/ 401・403（認証・認可・接続未解決）/ 413（サイズ超過）/ 422（件数超過・形式不正）/ 500。
  `detail` に**本文・生値を含めない**。
- 出力は必ず上表の形にモードスコープで返す（他モードのキーを `null` で埋めない）。

### 11-4. `/feedback` の 2 サブモード（`feedback-intake` の適合範囲）

> 🔴 **`feedback-intake` v0.1.0 は「人間が *名指しした既存レコード* を訂正する」部品である。**
> 実コードは payload を `{record_id, feedback_seq, verdict_code, rationale, decided_by, decided_at}` の
> **完全一致キー集合**でしか受け付けず、`FeedbackIntakeService` は
> **常に `supersedes_record_id = source_record_id` を設定して当該レコードを supersede する**。
> 本 AG の主要な `/feedback`（今回の例外に対する確定裁定＝**新しい前例**の記録）にこれを使うと、
> **フィードバックのたびに既存の前例が supersede されてコーパスが壊れる**（P-1 と同種の害）。
> → **サブモードを分け、部品は適合する側にだけ使う。**

| サブモード | 用途 | 実装 | `supersedes_record_id` |
|---|---|---|---|
| `kind: "adjudication"`（既定） | 今回の例外に対する**確定裁定を新しい前例として記録**する。あわせて採用/棄却前例・決め手の差異を残す | **`LedgerService.write()` を直接呼ぶ**（部品は使わない） | `None` |
| `kind: "correction"` | **過去裁定そのものを人間が訂正**する（誤った前例が想起を汚染するのを止める） | **`feedback-intake` を使う**（部品の適合ケース） | 部品が `record_id` を設定 |

**`kind: "adjudication"` の HTTP 入力 → ledger レコード対応**

| HTTP 入力 | 書込先 |
|---|---|
| `exception_id` / `exception_category` / `series_id` / `detected_at` | `classification.exception_category` / `domain_fields.series_id` / `occurred_at` |
| `confirmed_verdict_code` | `verdict_code` |
| `rationale`（本文） | サーバが payload store へ → `rationale`（部品が raw/audit/safe に分離） |
| `adopted_precedent_ids` / `rejected_precedent_ids` / `decisive_difference_evidence_ids` | `provenance`（構造化 ID のみ。自由文を入れない） |
| （認証済み `caller_id`） | `decided_by`（**クライアント自己申告値は使わない**） |
| — | `decision_source = "human"` / `supersedes_record_id = None` / `ai_suggested_verdict` は**未設定** |

**`kind: "correction"` の HTTP 入力**

`{ "kind":"correction", "record_id":"adj_...", "feedback_seq":<int>, "verdict_code":"...", "rationale":<本文>, "decided_at":"..." }`
→ アダプタが `decided_by` を認証済み `caller_id` で注入し、**部品が要求する完全一致キー集合**に整えてから
payload store へ預け、`feedback_payload_ref` を canonical envelope に載せる。
`feedback_seq` は idempotency receipt store の `(scope, record_id, feedback_seq)` 一意制約に使う
（クライアントが採番し、同一値の再送は同一結果を返す）。

`kind: "adjudication"` の `decisive_difference_evidence_ids` は、同一 scope・例外区分・系列・
`detected_at` で候補集合を再構築して得た**信頼側 evidence registry の完全一致 ID**だけを受理する。
`EV-ADJ-<record_id>-S` という prefix 一致だけでは受理しない。

### 11-2-bis. `/invoke` バッチの部分失敗契約

> 🔴 **失敗した例外を「無かったこと」にして `status: "success"` を返さない。**
> 月次レビューは 1 リクエストに数十件の例外を載せるため、1 件の失敗で全体を捨てるのは実運用に合わない。
> 一方、失敗を良性の縮退（`no_precedent_records` 等）と同じ `degradation_reason` に混ぜると、
> 呼び出し元は **「カードが無い」と「例外が処理されなかった」を区別できない**。
> **両者を分けて機械判別可能にする。**

```jsonc
{
  "output": {
    "mode": "invoke",
    "cards": [ /* 成功した例外のカード */ ],
    "rejected": [ {"exception_id": "...", "reason_code": "..."} ],   // 失敗した例外を明示
    "degradation_reason": "no_precedent_records"                     // 良性の縮退のみ。失敗は入れない
  },
  "status": "success" | "partial" | "error"
}
```

| top-level `status` | 条件 |
|---|---|
| `success` | `rejected` が空（全件カード化できた） |
| `partial` | `cards` と `rejected` の**両方が非空**（一部失敗） |
| `error` | `cards` が空で `rejected` が非空（全件失敗） |

- `reason_code` は S-4 イベントの語彙と揃える（`payload_unresolved` / `intake_rejected` /
  `s3_gate_rejected` / `evidence_stale` …）。**本文・生値を含めない。**
- `degradation_reason` から **`exception_skipped:*` を廃止**する（`rejected[]` が正本）。
- `partial` は HTTP アダプタの応答 envelope の値であり、`AgentState.status`（`AgentStatus` の値）
  とは別レイヤである。ノードが state に書く `status` は従来どおり `AgentStatus` の値のみを使う。
- 受入テスト: **成功 1 件・失敗 1 件の混在バッチ**で ①`cards` が 1 件 ②`rejected` が 1 件で
  `reason_code` を持つ ③top-level が `partial` ④`degradation_reason` に失敗が混ざっていない、を検証する。

### 11-3. 入力形式 allowlist

- 受け付けるのは `application/json` ＋ CSV 等の**軽量テキスト形式**のみ。
- **PDF・Excel（xlsx）は受け付けない**（攻撃面を抑えるため）。マジックバイト判定で拒否し、拡張子・Content-Type の
  自己申告を信用しない。パーサを持たない選択そのものが攻撃面の削減になる。
- 🔴 **索引に入るテキストそのものに品質ゲート**を掛ける。経路（PDF/xlsx）を塞いでも、
  **OCR 崩れのテキストを貼り付けられたら同じ事故**になるため、形式チェックだけでは足りない。
  次を決定論的に判定し、外れた行は `rejected[]` に `reason_code` 付きで返す（黙って索引に入れない）:

  | 指標 | 既定しきい値 | reason_code |
  |---|---|---|
  | 置換文字 U+FFFD の比率 | > 0.5% | `text_mojibake` |
  | 制御文字（改行・タブ除く）の比率 | > 0.1% | `text_control_chars` |
  | 可読文字（かな・漢字・英数・一般句読点）の比率 | < 80% | `text_unreadable` |
  | 本文長 | < `min_body_chars`(20) または > `max_body_chars`(4000) | `text_too_short` / `text_too_long` |

  しきい値は `agent.config` に置く。判定は NFKC 正規化**後**に行う。
- 改訂告知は**顧客が取得済みの文書**を証跡として渡す形とし、AG が外部ポータルを取得することはしない。

---

## 12. セキュリティ設計（S-1 〜 S-5）

| 層 | 実装 |
|---|---|
| **S-1** | 全 `FunctionNode` サブクラスで `required_trust_level` を**クラス本体に明示宣言**（`VERIFIED_EXTERNAL`）。`GraphNode` は既定 `ANONYMOUS` なので main スロットでも**明示宣言**する。有効値は `ANONYMOUS` / `VERIFIED_EXTERNAL` / `INTERNAL` のみ（文字列 enum・int 比較不可） |
| **S-2** | `FunctionNode` の `@final` 既定ゲート（`user_input` / `validated_input` / `llm_response` の PII マスク）＋ `_extra_security_gate_input()` で ①**顧客接続の解決を最初に行い**、失敗時は本文を一切参照せず拒否（順序の穴を塞ぐ）②宣言スコープキー欠落は fail-closed ③モード別スキーマ検証 ④入力形式 allowlist |
| **S-3** | §12-3 のとおり |
| **S-4** | 全ノードの `execute()` 内で `emit_trace_event()` を最低 1 回（`from shared.utils.audit_logger import emit_trace_event`）。`node_start`/`node_complete`/`node_error` は framework が出すので**重複させない**。§12-4 のとおり**失敗経路にも必ず置く** |
| **S-5** | credential リテラルを `src/` に置かない。`pyproject.toml` の依存は全て `==` 完全ピン |

### 12-1. secrets の扱い

- 秘密は `ctx.secrets.require("KEY")` 経由（`os.environ` 禁止）。
  例外は入口アダプタが caller を認証するために読む `INVOKE_AUTH_TOKEN` のみ。
- 🔴 **`config/agent.yaml` の `requires.secrets` は `[]` とする。**

  **判断軸は「その鍵が無いときプロセスを *起動させたくない* か」**であって、
  「`require()` するか」ではない。`requires.secrets` に列挙した鍵は
  `SecretProvider.require_at_compile()` が**起動時に**解決を試み、1 つでも欠けると `MissingSecret` を送出する。

  本 AG が要る顧客 KB の DSN は**リクエストごとに解決する**もので、起動時には存在しない。
  これを宣言すると STG でプロセスが起動できず `health_200: false` になる。

  **SDK 1.0.0 実体で確認（2026-07-30）**:
  `shared.secrets.DotenvProvider` は `env/platform/.env.{env}` / `env/namespaces/{ns}/.env.{env}` /
  `env/agents/{ns}/{agent}/.env.{env}` の **dotenv 3 層のみ**を読み、**プロセス環境変数を一切読まない**。
  したがって CI 変数や STG のコンテナ環境変数に DSN を置いても解決されない。
  実測: `require_at_compile(["CUSTOMER_KB_DSN"])` →
  `MissingSecret: ... not found (checked: platform, namespaces/ene, agents/ene/<agent>)`。

  > 補足: SDK ドキュメント（agent.yaml スキーマと secrets 設定の SDK ドキュメント）は
  > 「`require()` する鍵を宣言せよ」と読める。本設計はその記述と食い違うが、
  > **上記の実測挙動を優先**する。文言の整合は framework documentation 側の課題であり、
  > テンプレ側で `env/` を捏造して回避しない。

  **代償として塞ぐこと**: 起動時の門を外した以上、**リクエスト経路の最初の門が唯一の関門**になる。
  `_extra_security_gate_input()` は**冒頭で顧客接続を解決**し、失敗時は
  `payload_store.get()` を含め**本文に一切触れずに**拒否する。
  テストは「秘密なしで `startup()` が通る」「秘密なしの invoke で `payload_store.get` 呼び出し回数が 0」
  「秘密ありの対照実験で success」の 3 点セットで固定する。

### 12-2. LLM の注入経路

> 🔴 `server.py` が `agent = Graph()` と **config なしで**グラフを生成すると、
> このままだと `config["llm"]` が永久に未注入で、本番でも常に `degradation_reason="llm_unavailable"` の
> 縮退経路に落ちる（縮退が「正常動作」に見えてしまい気づけない）。

- 入口アダプタは起動時に **`config={"llm": <BaseLLM 実装>}` を渡してグラフを構築**する。
- LLM クライアントの選択（`shared.services.llm` の Anthropic / OpenAI / Bedrock、または `BaseLLM` 実装）と
  その資格情報は**起動時に必要**なので、これは §12-1 と違い **`ctx.secrets` ではなく起動時 provisioning** の対象。
  ただし本テンプレは**モデル ID も具体クライアントも `src/` に書かない** — 注入シームのみを持ち、
  どのクライアントを注入するかはデプロイ側の責務とする。
- LLM 未注入の状態は**テスト用と STG provisional 用にだけ許される**縮退であり、
  `docs/07_operation_guide.md` に「実配線時に必ず解消する事項」として明記する。

### 12-3. S-3 ゲートの実装契約（`_extra_security_gate_output`）

> 🔴 **`_extra_security_gate_output(result)` は `state` を受け取らない**（wheel 実機。S-3 裏口チャネル #3）。
> 「state のレジストリと照合する」と設計に書くと実装不能になり、実装者はキーの有無分岐（fail-open）へ逃げる。
> → **`execute()` が検証材料を `result` 側に宣言し、ゲートはその宣言値だけで検証する**。
> 宣言ブロックが**欠落・型不正・未知値のときは `raise`（fail-closed）**。「無ければスキップ」は書かない。

> 🔴 **「result に置けば安全」ではない。** 検証材料が LLM 出力から転送されうるなら、ゲートは
> 攻撃者制御下の自己申告を検証するだけになる（fail-open）。**生成元の隔離をデータフローで固定する。**

**データフロー契約（この 4 点をすべて満たす）**

1. `pre_process` が決定論的に **`verification_snapshot`** を作り、**親 state に置く**（LLM 経路を通らない）。
2. `PrecedentWorkflowGraphNode.merge_output()` は**閉じたマッピング**で、
   `verification_snapshot` / `evidence_registry` / `eligibility` / 優先度入力を**戻り値から除外**する
   （内側グラフがこれらのキーを返しても親 state を上書きできない）。
3. `CardAssemblyNode.execute()` は **LLM 出力に `_verification` / `_gate_verification` / `review_priority` /
   `eligibility` 相当のキーがあれば無条件 reject** し、`result` の検証ブロックは
   **trusted な `verification_snapshot` からのみ再構築**する。
4. ゲートは `_gate_verification` の型・**完全キー集合**・有限値を検証したのち**削除**する
   （利用者向け `formatted_output` に残さない）。

`post_process.execute()` が返す `formatted_output` に同梱する検証ブロック:

```jsonc
"_gate_verification": {                 // pre_process の verification_snapshot 由来。LLM は一切触らない
  "mode": "invoke",
  "evidence_ids": ["EV-ADJ-...", "EV-NOTE-..."],   // 根拠レジストリの全 ID
  "requirement_ids": ["REQ-..."],                   // §7-3 の決定論不足要件
  "distinct_verdict_count": 2,
  "candidate_count": 5,
  "truncated": false,
  "numeric_facts": [ {"token": "12.4", "source": "EV-NOTE-...-S3"} ]  // §12-4-bis
}
```

ゲートが行う検証（すべて `result` 内で完結）:

| # | 検証 | 失敗時 |
|---|---|---|
| ① | **出力キーの closed allowlist**（top-level ＋ ネスト再帰。未宣言キーは全 reject）。**免除条件（`.*` 相当のワイルドカード除外）を一切作らない** — 免除を書いた時点でそれがチャネルになる（裏口 #1） | `raise` |
| ② | 全 claim / 不足情報 / 確認質問の `evidence_ids` ⊆ `_gate_verification.evidence_ids`（捏造 ID 検出） | `raise` |
| ③ | 各 claim の `evidence_ids` が **1 件以上**、かつ `support_status` が有限集合 | `raise` |
| ④ | 各 `missing_information[].requirement_id` ∈ `_gate_verification.requirement_ids` | `raise` |
| ⑤ | `review_priority` を `_gate_verification` の値から**再計算して一致検証**（LLM が優先度を書けない） | `raise` |
| ⑥ | `precedent_facts` の `adjudication_id` / `verdict_code` が `_gate_verification.evidence_ids` の出所と整合 | `raise` |
| ⑦ | **数値忠実性の双方向照合**（§12-3-bis） | `raise` |
| ⑧ | **自由文パスを全列挙**して `detect_credentials`（block）と `detect_pii`（マスク）を再帰適用。framework 既定は top-level の str のみなので自前再帰が必須。主フィールド 1 本だけに掛けると脇の自由文が漏れる（裏口 #4） | credential=`raise` / PII=マスク |
| ⑨ | 逐語コピーガード（§8-1）・`statement` 長上限 | 当該 claim を reject |
| ⑩ | `_gate_verification` の欠落・型不正・**未知/不足キー**・未知 `mode` | `raise` |
| ⑪ | LLM 出力に `_verification` / `_gate_verification` / `review_priority` / `eligibility` 相当のキーが含まれていた | `raise`（注入検出） |

**自由文パスの明示列挙**（`*` = list 展開。増やしたら必ずここに足す）:
`("claims","*","statement")` / `("missing_information","*","statement")` /
`("confirmation_questions","*","statement")` / `("degradation_reason",)`

`_gate_verification` は S-3 通過後、`get_output()` へ渡す前に取り除く（利用者向け出力には含めない）。

### 12-3-bis. 数値・属性の忠実性（provenance を「ID 実在」で終わらせない）

ID 実在検証は**中身の忠実性を何も担保しない**。LLM が `statement` に入力と違う数値を書いても ID 照合は発火しない。

- **方向 A（params → 本文）**: `_gate_verification.numeric_facts` は pre_process が
  `exception_ref.metrics` と候補前例の構造化属性、および根拠 span から**決定論的に**抽出した
  **`{value, unit_dimension, normalized_unit, source_evidence_id}`** の集合。LLM は生成しない。
  値だけを比べると **`62%` を `62円` に書き換えても通る**ため、**単位次元まで正規形に含める**。
- **方向 B（本文 → params）**: `statement` から抽出した数値トークンが**すべて**
  `numeric_facts` に存在すること。1 つでも未登録なら当該 claim を reject。
  片方向だけだと「無関係な claim を 1 件入れて数値は散文だけに書く」で全回避される。
- **抽出の正規化**: **NFKC** ＋ 全角/半角 ＋ **漢数字変換** ＋ 日本語単位同義語
  （`六十二パーセント` / `６２％` / `62 %` を同一視）。
- 🔴 **単語境界（`\w` / `\b`）で切らない。** 「999999円」のように数値へ日本語単位・助詞・句読点が
  隣接すると抽出結果が空集合になり、`issubset` が常に True＝**ゲートが常時素通り**する。
  トークナイズは「数字列 ＋ 直後の単位/助詞を含みうる」前提で書く。
- 🔴 **空集合 reject の適用範囲を限定する。** 数値を含まない正当な `statement` まで落とさないため、
  判定は「**数値語彙は検出されたが token 化が 0 件**」のときだけ reject とする（数値語彙なしは許可）。
  **数値語彙の検出と token 化は同一関数で行う**（別実装にすると抽出漏れで再び vacuous になる）。
- PB に入れる入力: `62%`→`62円` / `1,000` / `-1.5` / `1億円` / 漢数字 / 単位隣接（`999999円`）/ 全角。

### 12-4. S-4 監査イベント（失敗経路にも置く）

成功経路だけに置くと、**拒否・縮退・短絡が監査ログから消える**。

| 経路 | イベント |
|---|---|
| 成功 | `intake_validated` / `candidates_narrowed` / `conflict_predicate_evaluated` / `reconcile_drafted` / `card_assembled` / `ledger_written` / `payload_resolved` |
| 拒否（fail-closed） | `intake_rejected`（reason_code 付き）/ `scope_missing_rejected` / `connection_unresolved_rejected` / `s3_gate_rejected`（検証番号付き）/ `ingest_row_rejected` |
| 上流 error 短絡 | `upstream_error_passthrough`（どのノードで短絡したか） |
| 降格・縮退 | `llm_unavailable_degraded` / `candidates_truncated` / `evidence_stale_dropped` / `payload_unresolved_skipped` |
| ライフサイクル | 部品側が `ledger_auto_disputed` / `ledger_explicit_correction_superseded` / `ledger_supersede_review_required` 等を発火する（テンプレで重複させない） |

> 🔴 **発火責任者を必ず割り当てる。`status="error"` のとき `route()` は `post_process` を飛ばして
> `finalize` へ行く**（SDK 1.0.0 実測）。したがって `upstream_error_passthrough` / `s3_gate_rejected` /
> `*_rejected` を **post_process に置くと到達しない**。

| イベント | 発火箇所 |
|---|---|
| `intake_validated` / `intake_rejected` / `scope_missing_rejected` / `connection_unresolved_rejected` | `IntakeNode.execute()` および `_extra_security_gate_input()`（**拒否を返すその場**） |
| `candidates_narrowed` / `conflict_predicate_evaluated` / `candidates_truncated` / `evidence_stale_dropped` | `IntakeNode.execute()` |
| `reconcile_drafted` / `llm_unavailable_degraded` / `payload_unresolved_skipped` | 内側 `invoke_reconcile` / `invoke_deterministic` |
| `ingest_row_rejected` / `ledger_written` / `payload_resolved` | 内側 `ingest_stage` / `feedback_stage` |
| `upstream_error_passthrough` | **上流 error を検知した各ノードの `execute()` 冒頭**（post_process ではなく `main` 内側の `mode_router` と `IntakeNode`） |
| `s3_gate_rejected` | `_extra_security_gate_output()` の **`raise` 直前**（検証番号 ①〜⑪ を payload に含める） |

PB は「scope 不足 / 接続失敗 / S-3 reject / payload 解決失敗」の 4 経路それぞれで
**ドメインイベントが 1 件以上出ている**ことを確認する（成功経路だけの検証にしない）。

### 12-5. Import Isolation

- [x] Level 0（`agenticstar`）を import しない
- [x] import 先は `framework/` と `shared/` のみ（他テンプレの import なし）
- [x] 写経部品の import identity は **`src.<part>` に一本化**し、root `conftest.py` は **repo root のみ**を
      `sys.path` に載せる（root と `src` の両方を載せると同一ファイルが二重 identity になり
      `except` が効かない潜在 fail-open）

---

## 13. 適用部品一覧（`parts/`）

実装では共通部品の `adaptation_points` のみ本テンプレ仕様で埋め、骨格は変更しない。

| part-id | version | 適用先 | adaptation_points の埋め方 |
|---|---|---|---|
| `cat2-graph-skeleton` | 0.1.4 | `src/nodes/main_node.py` / `src/graph/domain_workflow_graph.py` | 内側 3 モード分岐・double-checked-lock cache・上流 error パススルー。HITL 非採用のため `propagate_hitl=False` |
| `output-envelope` | 0.1.2 | `src/nodes/post_process_node.py` | `mode` / `card_kind` 宣言・`degradation_reason` 畳み込み・モードスコープ返却 |
| `entry-adapter` | 0.4.0 | `src/api/server.py` | `READ_INVOKE` / `WRITE_INGEST` / `WRITE_FEEDBACK` を実副作用から宣言。**standalone token は read のみ、write は caller_id 付き middleware 必須** |
| `payload-store` | 0.4.0 | `src/services/payload_store.py`（写経） | `scope_keys=("business_unit_id",)` ＋ session binding・TTL・single-use。**ref は英字プレフィックス＋英数字ハイフンのみ** |
| `ingest-file-sanitizer` | 0.1.1 | `src/services/input_sanitizer.py` | `TextQualityPolicy` を索引本文の本番品質ゲートへ配線。`defusedxml==0.7.1` は必須依存 |
| `ingest-row-pipeline` | 0.4.0 | 内側 `ingest_stage` | 全行を検証後、`write_batch` 1 回で SQLite/customer transaction へ渡す。途中失敗は全 rollback |
| `accumulation-ledger` | 0.1.1 | `src/services/adjudication_ledger.py` | 下記 `LedgerConfig` のとおり。**`supersede_mode="review_required"`（`auto` にしない — §9-3 の 🔴）**。`LedgerBackend` Protocol（`insert`/`get`/`update`/`query`/`purge_raw`）に顧客 KB 接続を注入し、同梱の backend 契約テストを緑にする |
| `feedback-intake` | 0.2.0 | 内側 `feedback_stage` | `FeedbackIntakeNode` を実 inner graph 経路で呼び、同一 key の並行 retry を1書込へ収束。🔴 `decided_by` は認証済み caller_id で上書き |
| `evidence-gate` | 0.3.0 | `src/services/response_contract.py` / `src/nodes/post_process_node.py` | `FindingField` の同一定義から prompt 契約を生成し、LLM dict を `project_typed_finding` で型付き射影。未知キー・型違い・enum 外を fail-closed |
| `llm-draft-stage` | 0.3.0 | 内側 `invoke_reconcile` | LLM 不在だけ deterministic fallback。provider/auth/config/runtime は `status=error` + 安定 error_code、例外文は state/trace/output 非露出 |

**`LedgerConfig`（`accumulation-ledger` の adaptation point）**

```python
LedgerConfig(
    scope_keys          = ("business_unit_id",),
    record_kinds        = frozenset({"adjudication"}),
    classification_codes= {"exception_category": frozenset(<顧客の例外区分・導入時 config>)},
    verdict_codes       = frozenset(<顧客の裁定結果コード・導入時 config>),
    conflict_key_fields = ("series_id",),       # classification の exception_category と併せて
                                                #   conflict_key = (scope, classification, (series_id,))
    domain_fields       = {"series_id": "scalar",        # conflict_key に使う列は scalar 宣言が必須
                           "operational_note": "text"},  # 自由文は必ず宣言（未宣言だと safe 分離されない）
    schema_version      = "1.0",
    supersede_mode      = "review_required",    # 🔴 auto にしない（§9-3）
    record_id_prefix    = "adj",
)
```

裁定理由は組込みの `rationale` に載せる（`domain_fields` に重ねて宣言しない）。
`retrieval_text` には構造化属性のみ（例外区分・系列・単位）を入れる。

**不採用の部品と理由**

- `scoped-retrieval` v0.3.0 — **不採用**。本 AG の候補絞り込みは
  `例外区分 × 系列 × 有効期間` の**決定論クエリ**であり、意味検索ステージを持たない（§3 の責務分界そのもの）。
  スコープ fail-closed は `accumulation-ledger` の宣言スコープで担保される。
  部品を入れると「意味検索で候補を作っている」という実体と異なる設計に読まれ、指摘を誘発する。

> ⚠️ 写経部品の lint 違反は**上流 `parts/` master を先に見る**。上流で直っていれば同期して
> byte-identity を回復する。上流にも無ければ `[tool.ruff.lint.per-file-ignores]` で当該ファイル限定に抑止する
> （ローカル独自書き換えは最後の手段）。

---

## 14. `config/agent.yaml` 設計値

> 設計時点では `config/config.yaml` を追加せず、閾値・runtime パラメータを `agent.config` ブロックへ置く方針とした。

```yaml
agent:
  id: "ENE-C2-123"
  name: "SupplyDataQualityPrecedentAgent"
  version: "0.1.0"
  category: "Cat 2"
  industry: "ENE"
  base_type: "AgentBaseGraph"          # L1 クラス名（初期コメントの L2 名に引きずられない）
  module: "src.graph"
  class: "SupplyDataQualityPrecedentGraph"
  config:
    max_retry: 3
    timeout_seconds: 60
    budget_usd: 1.00
    memory_enabled: false
    candidate_top_k: 8
    candidate_hard_cap: 24
    conflict_min_distinct_verdicts: 2
    conflict_min_minority_count: 1
    priority_weight_verdict: 2
    priority_weight_missing: 1
    priority_weight_candidates: 1
    priority_high_threshold: 4
    priority_medium_threshold: 2
    payload_ttl_seconds: 900
    max_exceptions_per_invoke: 50
    # free-text guards (design section 8-1)
    statement_max_chars: 400
    verbatim_guard_chars: 60
    # 顧客語彙（導入時 config。未宣言なら ledger_config() が起動時 fail-closed）
    exception_categories: ["missing", "duplicate", "outlier", "timeliness",
                           "completeness", "consistency", "validity", "other"]
    verdict_codes: ["accepted", "rejected", "waived", "exception"]
    # evidence registry / input quality gates (design sections 6-2, 11-3)
    min_span_chars: 8
    min_body_chars: 20
    max_body_chars: 4000
    max_mojibake_ratio: 0.005
    max_control_char_ratio: 0.001
    min_readable_char_ratio: 0.80
  requires:
    extras: []
    secrets: []                        # §12-1 参照 — 起動時必須の秘密は無い
  required_trust_level: "VERIFIED_EXTERNAL"

hitl:                                   # top-level（PB-7 スタブが読む位置）
  enabled: false
```

`module: "src.graph"` で manifest から解決できるよう、`src/graph/__init__.py` で
`SupplyDataQualityPrecedentGraph` を re-export する。

> **`timeout_seconds` について（SDK 1.0.0 実測 2026-07-30）**: `AgentBaseGraph._validate_config()` が
> 検証するのは `max_retry` / `memory_enabled` / `hitl` のみで、**timeout 系のキーは
> `timeout_s` も `timeout_seconds` も読まれない**（`timeout_s` は docstring の例示に出るだけ）。
> つまりキー名を変えても timeout は有効化されない。**テンプレが LLM / DB 呼び出し側で自分で適用する**。
> キー名は既存の設定規約（`timeout_seconds`）に合わせ、表記を統一する。

---

## 15. 縮退・エラー方針

| 事象 | 挙動 |
|---|---|
| LLM 未注入 / LLM 呼び出し失敗 | `status="success"` ＋ 全 claim `unknown` ＋ `degradation_reason="llm_unavailable"` |
| 過去裁定 KB が空・薄い | `eligibility="no_precedent"` として不足情報と確認質問のみ返す（**導入初期の正常動作**）＋ `degradation_reason="no_precedent_records"` |
| 候補が `candidate_top_k` を超過 | 切り捨てて `degradation_reason="candidates_truncated:<n>"` |
| payload 解決失敗（invoke） | 該当例外を skip し `degradation_reason="payload_unresolved:<exception_id>"`（`status="success"`） |
| payload 解決失敗（ingest/feedback） | **fail-closed**（1 件も書かない・`status="error"`） |
| スコープキー欠落 / 顧客接続未解決 | **fail-closed**（`status="error"`・本文を参照しない） |
| 根拠 ID 実在検証の不一致 | **fail-closed**（`status="error"`。捏造 ID を出力しない） |

「データが薄いので価値が出ない」ことと「システムエラー」を混同しない。前者は縮退（success）、後者は error。

---

## 16. 設計判断記録（Design Decision Record）

| # | 判断 | 選択肢 A | 選択肢 B | 採用 | 理由 |
|---|---|---|---|---|---|
| D1 | L1 base type | `AgentBaseGraph` | `AutonomousBaseGraph` | **A** | 固定パイプラインで終了が決まる。自律ループなし |
| D2 | Composition pattern | `GraphNode`（内側 subgraph） | 単一 `FunctionNode` | **A** | 3 モード分岐と多段パイプラインを main に隠蔽する Cat2 標準 |
| D3 | 候補絞り込み | 決定論 DB クエリ | 意味検索（vector） | **A** | 決定表 lookup 還元の回避が成立条件。絞り込みを LLM に渡すと責務境界が崩れる |
| D4 | HITL | 非採用（`/feedback` で非同期担保） | D6 `interrupt()` | **A** | `docs/01_proposal.md` の 4 問すべて No。初期案からの変更点として明示 |
| D5 | 過去裁定の取り扱い | 事実として保持・提示（根拠 ID 付き） | 検索・LLM payload から除外 | **A** | 過去裁定は組織が既に確定させた事実。除外すると conflict predicate が成立せず moat も消える |
| D6 | S-3 の主防御 | 出力キーの closed allowlist | 断定語 denylist | **A** | 自然言語の断定は lexicon では原理的に閉じない。denylist は補助線としても持たない |
| D7 | 事実提示文の生成 | 決定論描画 | LLM 生成 | **A** | 「前例 X では 〇〇 と裁定された」は事実主張。LLM に書かせない |
| D8 | 顧客分離 | 接続レベル（顧客専用 KB） | scope-partition 部品でマルチテナント | **A** | 既存実装で採用されている方式。`business_unit_id` は**顧客内区画**であって顧客分離ではない |
| D9 | `requires.secrets` | `[]` | DSN 等を宣言 | **A** | 起動時に必須でない鍵を宣言すると STG で起動不能（§12-1） |
| D10 | 入力形式 | JSON/CSV 等の軽量テキストのみ | PDF/xlsx も受理 | **A** | パーサ増・OCR 崩れ・依存膨張を避ける |
| D11 | 一意確定型の扱い | LLM を呼ばず決定論表示 | 常に LLM を呼ぶ | **A** | LLM 必須性の説明とコスト抑制の両立 |
| D12 | conflict predicate の評価対象 | 切り詰め前の全一致集合（集約クエリ） | `top_k` 適用後の候補集合 | **A** | B だと少数派 verdict が打切りで消え、相反しているのに決定論表示へ誤降格する（§5 冒頭） |
| D13 | claim の `statement` | LLM が散文を書く（根拠 ID 必須・逐語コピーガード付き） | enum ＋ 根拠 ID のみにして決定論描画 | **A** | B は過剰設計として採用されなかった。解釈の言語化が価値の核であり、落とすと Tool に退化（§8-1） |
| D14 | レビュー優先度の入力 | pre_process が確定する `requirement_id` 集合 | LLM 起草の `missing_information` 件数 | **A** | B は LLM の分割・統合で優先度が揺れ、S-3 の再計算も同じ値の再計算にすぎず何も検証しない（§7-3） |
| D15 | runtime パラメータの置き場 | `config/agent.yaml` の `agent.config` | `config/config.yaml` を新設 | **A** | 設計時点の初期構成に `config.yaml` はなく、既存構成との整合を優先（§14） |
| D20 | 有効期間の比較 | ISO-8601 を aware datetime にパースして**時刻比較**。オフセット必須（naive/日付のみは reject） | ISO 文字列の辞書順比較 | **A** | B は `+09:00` 表記で有効な前例を黙って除外する（実測で確認）。日本の事業者が自然に送る形式で核心が壊れる |
| D19 | 型チェック（mypy strict）の範囲 | 写経部品・`src/examples/` を除外し、framework/shared の `import-untyped` を module override で無効化したうえで**自作コードは strict クリーン**にする | `mypy src` 全体をクリーンにする | **A** | 写経部品は byte-identity 維持のため編集禁止（142 件）。framework wheel が `py.typed` 未同梱で `import-untyped` 29 件は**テンプレ側で解消不能**。除外理由を pyproject に明記する |
| D18 | `/invoke` バッチの部分失敗 | `rejected[]` を明示 ＋ top-level `status` を `success`/`partial`/`error` で判別可能に | 失敗を `degradation_reason` に混ぜて常に `success` | **A** | B は失敗と良性縮退が区別できず、検証・セキュリティ・ワークフローの失敗が「完了した月次レビュー」に見える（レビューで重大な問題と評価）。全件 fail は月次バッチの実運用に合わない |
| D17 | 顧客語彙（`exception_categories` / `verdict_codes`）の置き場 | `config/agent.yaml` の `agent.config`（未宣言なら起動時 fail-closed） | `src/services/` に DEFAULT_* を持つ | **A** | §19「テンプレに固定値を持たない」＋§14「閾値・runtime は agent.yaml」。src の既定値は「黙って別語彙で動く」事故になる |
| D16 | ledger の `supersede_mode` | `review_required`（明示訂正と対称/版更新のみ自動遷移） | `auto`（部品 USAGE の既定推奨） | **A** | 前例コーパス型では B が同一キーの古い裁定を自動 `superseded` にし、既定検索から落として verdict 分布を過小評価する（§9-3 の 🔴・§20-bis P-1） |

---

## 17. ユーザビリティ設計レビュー（設計中に自ら見直して修正した点）

| 観点 | 当初案 | 修正後 | 理由 |
|---|---|---|---|
| 入力 | 例外 1 件ずつ `/invoke` | **例外リスト（最大 `max_exceptions_per_invoke=50`）を 1 回で受理**し、カードを配列で返す | 月次レビューは数十〜数百件を通しで処理する。1 件ずつの呼び出しは実運用に合わない |
| 出力 | claim の羅列 | **`review_priority` を付けて返す** | 担当者は「どれから見るか」を最初に決めたい。優先度が無いと結局全件を目で追うことになる |
| 出力 | 過去裁定を LLM の散文に溶かす | **`precedent_facts` を独立ブロックで決定論描画** | 「前例が何だったか」は最も確認したい情報であり、散文に埋もれると再確認コストが増える。事実と解釈の分離にもなる |
| 縮退 | 前例なし → エラー | **`no_precedent` として不足情報＋確認質問を返す（success）** | 導入初期は KB が空。エラーを返すと「壊れている」と誤解され導入が止まる |
| コスト | 全例外で LLM を呼ぶ | **相反前例型のみ LLM。一意確定型は決定論表示** | 反復例外の大半は一意確定型。全件 LLM は費用対効果が悪くレスポンスも遅い |
| 対話 | HITL 対話ループ | **持たない**。条件変更は `/invoke` の再呼び出し、判断結果の還元は `/feedback` | ターン間隔が数時間〜数日の業務に `interrupt()` は不適合（D4） |

---

## 18. スコープ外（本 AG が行わないこと）

- 今回の品質**合否結論**・**規制適合判定**の出力
- 系統評価・需給予測・市場行動
- 関係部署への照会の**送信**（確認質問は「送信しない草案」）
- 外部ポータルからの改訂告知の取得（顧客が証跡として渡す）
- 品質チェック機構そのものの代替（既存機構の出力を入力として受ける）

---

## 19. 未確定事項（Proposal 前進条件・decision §8）

- エネルギー業界における**起動頻度・レビュー体制**は判定時点で一次調査未実施。
  導入前に顧客側の例外件数・レビュー体制を確認する必要がある。
  設計上は `max_exceptions_per_invoke` / `candidate_top_k` を config 化し、実測後に調整できる形にしてある。
### 19-bis. ドメイン実態の調査結果と仮置き（2026-07-30・公開一次情報で照会）

実装時にドメイン実務の裏取りを行った。**裏取りできた事実・推測・裏取り不可を分けて記録する。**
現プロジェクト体制で顧客ヒアリングは実施できないため、以下は**仮置き**として進める（config で後から調整可能）。

| # | 論点 | 調査結果 | 本テンプレの扱い |
|---|---|---|---|
| 1 | 改訂告知は系列単位で出るか | **裏取り済み: 系列 1:1 ではない。** 電力の託送供給は「託送供給等約款」、ガスも導管事業者の託送供給約款という**制度・約款・様式の単位**で改訂・公表される（電気事業法／ガス事業法）。OCCTO は広域機関システムの**システム変更・運用情報**として告知する。エネ庁の総合エネルギー統計は**統計全体／表の単位**で定義・推計方法の改訂を管理する | 🟡 **仮置き = `series_id` 完全一致のみ採用（現状実装）。** 制度・様式単位の広い告知は**構造的に取りこぼす**ことを既知の限界として明記する（下記 §19-ter） |
| 2 | 適用開始日より前に告知が出るのが通例か | **裏取り不可。** 法令からは「適用開始の N 日前までに通知する」共通ルールは確認できない | ✅ 公表時点は推測せず、入力で明示された `effective_date` だけを使う。現在案件の根拠にできるのは `effective_date <= detected_at` の告知のみ。将来発効の告知は将来変更の予告であり、現在時点の根拠 registry には入れない |
| 3 | 月次の例外件数のオーダー | **裏取り不可。** 品質例外の件数・分類・裁定結果を横断公開する法定指標は存在しない | 🟡 `max_exceptions_per_invoke=50` / `candidate_top_k=8` は**仮置き**。導入時に実測で較正する（config 化済み） |
| 4 | 同一区分で裁定が分かれるか | **推測にとどまる**（対象地点・計量/推定/代替値・対象期間・適用約款版で分岐しうる、という構造的説明は成立するが「一般的」とは公開情報から言えない） | ✅ conflict predicate は**顧客の実データが決める**設計（閾値 config 化）なので仮置きで進行可 |
| 5 | 不足情報の類型（7 種）の過不足 | **業界共通 taxonomy は裏取り不可。** ただし「例外ルール版」「例外値そのもの（値・単位・品質フラグ）」「対象と来歴（供給地点/計器/推定履歴）」「時点整合性（計量時点/受領時点/改訂適用時点）」「適用条件（約款・契約・制度）」「下流影響・訂正完了状況」の追加候補が指摘された | 🟡 **7 種のまま進行**（仮置き）。`requirement_id` は有限集合として config 化されていないため、追加は将来の改修とする（§19-ter に候補を記録） |

### 19-ter. 既知の限界（ドメイン起因・顧客確認事項）

1. **広い単位の改訂告知を取りこぼす** — `revision_notice` は `series_id` 完全一致でのみ根拠採用する。
   制度改正・様式改訂・統計全体の改訂として出た告知は、`series_id` が付かないため採用されない。
   実務では告知がこの単位で出ることが公開資料から確認できているため、**導入時に
   「告知 → 対象系列」のマッピングを顧客側で与えるか、`affected_series` を持つ入力形へ拡張する**必要がある。
2. **`REQ-REVISION-NOTICE-ABSENT` は「改訂が無かった」を意味しない** — 正しくは
   「**入力として渡された告知の中に、当該系列に一致するものが無かった**」である。
   「探索範囲外」「未発見」「取得不能」を区別していない。運用時はこの語の意味を取り違えないこと。
3. **不足情報 7 類型は網羅的でない** — 上表 #5 の追加候補（例外ルール版 / 例外値そのもの / 対象と来歴 /
   時点整合性 / 適用条件 / 下流影響）は本テンプレでは扱っていない。

- 顧客の `verdict_code` 集合（裁定結果コードの語彙）は顧客ごとに異なるため、
  `accumulation-ledger` の `verdict_codes` と `classification_codes.exception_category` は
  **`config/agent.yaml` の `agent.config`（`verdict_codes` / `exception_categories`）で導入時に与える**。
  テンプレに固定値を持たず、**未宣言なら `ledger_config()` が起動時に fail-closed** する（D17）。

---

## 20. 敵対レビュー対応記録（push 前レビュー・2026-07-30）

| # | 指摘 | 対応 | 反映先 |
|---|---|---|---|
| High-1 | `candidate_top_k` 切り詰め後に conflict predicate を評価しており、少数派 verdict が境界外に落ちると `uniquely_determined` へ誤降格して LLM 照合を回避できる | **受入** | §5 冒頭・§5-1〜5-3・§8 ⑤・D12 |
| High-2 | 3 モードの API / 出力契約が未定義。初期版の `server.py` は自由記述を `user_input` に載せるため checkpoint 隔離が成立しない | **受入** | §11-0（入口アダプタ置換）・§11-2（モード別 I/O・部分失敗・エラー形式） |
| High-3a | runtime パラメータは SDK doc 上 `config/config.yaml` に置くべき | **棄却** — 設計時点の初期構成に `config.yaml` はなく、既存構成との整合を優先 | D15・§14 |
| High-3b | `requires.secrets` は `require()` する鍵の宣言欄なので DSN を列挙すべき | **棄却** — 判断軸は「起動させたくないか」。SDK 1.0.0 実測で `DotenvProvider` は `env/` 3 層のみ参照し**プロセス環境変数を読まない**（`require_at_compile(["CUSTOMER_KB_DSN"])` → `MissingSecret`）。宣言すると STG で起動不能。根拠を実測値つきで明記 | §12-1（実測根拠を追記） |
| High-3c | `server.py` が config なしでグラフを生成するため `config["llm"]` が永久に未注入 | **受入** | §12-2（LLM 注入経路を新設） |
| High-4a | LLM の自由文 `statement` を廃止し enum ＋ 決定論描画にすべき | **棄却** — 「LLM に文字列を書かせない」構造化描画は過剰設計として採用されなかった。本 AG では解釈の言語化が価値の核で、落とすと Tool に退化する | §8-1（不採用理由を明記） |
| High-4b | LLM が根拠本文を丸写しすると payload_ref 隔離が checkpoint 上で崩れる | **受入** — 逐語コピーガード（連続 60 文字以上の literal 一致を reject）＋ `statement` 長上限を導入 | §8-1・§8 ⑦・`agent.config` |
| Medium-1 | 「決定論」優先度が LLM 起草の件数に依存している | **受入** | §7-3（`requirement_id` 方式へ全面改稿）・§8 ⑥・D14 |
| Medium-2 | evidence span の locator 契約（field / projection / offset 単位 / 版）が不足 | **受入** | §6-1（レジストリのエントリ契約を新設） |
| Medium-3 | ライフサイクル遷移の有効期間・スコープ・同時書込みが未定義 | **受入** | §9-4（適用範囲とトランザクション境界を新設） |

棄却した 3 件は「敵対レビューは網羅性の確認であって設計方針の決定装置ではない」に従い、
根拠（既存実装・実測値）を上記に残したうえで方針を維持している。

### 20-bis. 共通部品との突合で判明した設計修正（2026-07-30）

`parts/accumulation-ledger` v0.1.1・`parts/feedback-intake` v0.1.0（いずれも 2026-07-29 追加）の
**実コードを読んで**設計と突き合わせた結果の修正。本テンプレが両部品の初適用にあたる。

| # | 内容 | 影響 |
|---|---|---|
| P-1 | **`supersede_mode` を `auto` → `review_required` に変更** | 🔴 実害。`auto` は同一 conflict_key の古い active レコードを自動 `superseded` にし、既定 `search()`（active+disputed）から消す。前例コーパス型の本 AG では verdict 分布を過小評価して `uniquely_determined` へ誤判定する（§9-3） |
| P-2 | `search()` の引数を実シグネチャに修正 | 設計が書いていた `conflict_key` / `valid_at` 引数は**存在しない**。narrowing は `classification` ＋ テンプレ側フィルタ、期間は `occurred_at` / `valid_to` で行う（§5-1） |
| P-3 | 相反判定に部品の `conflicts(scope)` を使う | 自前の集約クエリは再発明だった。ただし `conflicts()` は有効期間を見ないため、最終判定は期間フィルタ後の集合で行う（§5-2） |
| P-4 | 期間フィールドを `occurred_at` / `valid_to` に修正 | 部品に **`valid_from` は存在しない**（§5-1） |
| P-5 | 自由文列の `domain_fields` `"text"` 宣言を必須と明記 | `_public_record` は denylist projection のため、**未宣言の自由文列は raw のまま model 可視 projection に載る**（§9-2-bis） |
| P-6 | `ai_suggested_verdict` を**使わない**と明記 | 部品にこのフィールドがある。書けば非断定契約（§7-1）を側面から破る（§9-2-bis） |
| P-7 | `feedback-intake` に idempotency receipt store と `decided_by` 上書きを明記 | client 自己申告の `decided_by` を監査由来に使わない（部品の adaptation point）（§13） |
| P-8 | evidence_id / payload_ref の英字プレフィックス規則は維持、ledger の `record_id` は対象外 | 部品の `_mint_record_id` は base32 ＋ 英字 prefix で uuid hex を使わない（§9-2-bis） |

**部品側への起票候補（テンプレでは対処しない）**: `accumulation-ledger` の USAGE は
「通常は `auto`、承認済み基準表への当てはめ型だけ `review_required`」と案内しているが、
この指針は *1 事案 1 レコード*型（インシデント想起・検収証跡）を前提にしている。
**前例コーパス型（履歴 1 件 1 件が資産）では `auto` がデータを既定検索から落とす**ため、
USAGE に判断軸（「同一 conflict_key の古いレコードは資産か、置き換え対象か」）を追記する価値がある。
部品コードの変更は不要。


### 20-ter. 敵対レビュー対応記録 — 2 巡目（先回りセット照合後のレビュー・2026-07-30）

先回りセット照合で追加した節（§6-2 / §9-1 / §9-2 / §11-2 / §11-3 / §12-3 / §12-3-bis / §12-4）に対する 2 回目のレビュー。
**High 6 / Medium 3 / Low 1 — 受入 9・部分棄却 1。**

| # | 指摘 | 対応 | 反映先 |
|---|---|---|---|
| H-1 | `_verification` が LLM 非介入であることを構造的に保証していない（result に置けば安全ではない） | **受入** — `pre_process` が trusted `verification_snapshot` を作り、`merge_output()` の閉じたマッピングで除外、`post_process` が LLM 由来の注入を reject して snapshot からのみ `_gate_verification` を再構築 | §12-3 データフロー契約・§12-3 ⑪ |
| H-2 | `source_kind` allowlist だけでは無関係な根拠を排除できない（別系列の改訂告知が通る） | **受入** — kind ごとの関係述語表を追加。`revision_notices` 入力に `series_id` を必須化 | §6-2 ②-bis・§11-2 |
| H-3 | クライアント由来 ref の破棄がネスト・配列まで閉じていない | **受入** — 再帰削除をやめ、厳格 request schema からの **allowlist 投影**へ。ref に `{customer_ref, business_unit_id, session_id, field_kind, expiry}` を束縛し resolve で完全一致要求 | §11-2 |
| H-4 | `review_required` の選択は正しいが §9-4 の遷移保証（原子性・optimistic lock・期間限定・明示訂正優先）は実部品で実現不能 | **受入（保証を撤回）** — 実挙動に合わせて記述し直し、帰結と緩和策を明記。部品拡張は候補として記録（3 テンプレ以上で再出したら上流対応） | §9-4 |
| H-5 | `/feedback` 契約が `feedback-intake` の実 API と一致しない（部品は常に source record を supersede する） | **受入** — `/feedback` を 2 サブモードに分割。**主要な確定裁定記録は `LedgerService.write()` 直接**、部品は「過去裁定の訂正」サブモードにのみ適用 | §11-4・§13 |
| H-6 | 永続層 PB の「credential 非混入」が現設計では満たせない（部品の `sanitize_text()` は PII のみ） | **受入** — ingest / feedback の write 直前に `detect_credentials` を適用し検出で書込を丸ごと reject | §9-2 |
| M-1 | 数値照合が単位・根拠対応を持たず、空集合 reject の適用範囲が曖昧 | **受入** — 正規形を `{value, unit_dimension, normalized_unit, source_evidence_id}` に。`source_evidence_id` が claim の `evidence_ids` に含まれることを要求。reject は「数値語彙あり・token 0 件」のみ | §12-3-bis |
| M-2 | checkpoint allowlist の path 単位・適用時点・PB が未確定 | **受入** — JSON Pointer 単位の closed schema（型まで宣言）＋ **適用点を checkpoint writer 直前 1 箇所に集約**＋ 実デシリアライズ PB | §9-1 |
| M-3a | 出力 schema に `valid_from` が残っている | **受入** — `occurred_at` へ統一 | §2・§7 |
| M-3b | runtime key は `timeout_s` であり `timeout_seconds` は無効 | **部分棄却** — SDK 1.0.0 実測で `_validate_config()` が読むのは `max_retry` / `memory_enabled` / `hitl` のみ。**`timeout_s` も `timeout_seconds` も読まれない**ため改名しても timeout は有効化されない。指摘の本質（timeout が誰にも強制されない）は受け入れ、**テンプレが呼び出し側で適用する**旨を明記。キー名は既存の設定規約を維持 | §14 注記 |
| L-1 | S-4 失敗イベントに発火責任者がいない（error 時 `route()` は post_process を飛ばす） | **受入** — 発火箇所の割当表を追加。`s3_gate_rejected` は `raise` 直前に発火 | §12-4 |
