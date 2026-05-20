# Dedup on node-text embedding via search model, reused on new-node creation

Status: done
Tags: `data`, `enhancement`, `P1`
Depends on: #041
Blocks: #044

## Scope

Today DEDUP runs `$vectorSearch` using an embedding of the entity NAME
(`add_entity` line ~220: `embedding_model.embed([name])`; pipeline task ④
`_embed_entity` embeds the canonical name), and when no merge happens the
SAME name-embedding is written as the new node's persisted `embedding`
(`add_entity._upsert_node`). Meanwhile INDEXING backfills missing vectors
from NODE-TEXT. Result: persisted node vectors are an inconsistent mix of
name-embeddings (dedup-created) and node-text-embeddings
(indexing-backfilled), and the dedup decision is made in a different
vector space than the search query.

This task makes DEDUP embed the NODE-TEXT (via the shared function from
#041 and the SEARCH model from #040), make the dedup decision against
that vector, and — when a NEW node is created — persist that SAME vector
on the new node so indexing never has to recompute it. After this task
the persisted node vector is NODE-TEXT-via-search-model EVERYWHERE
(dedup-created and indexing-backfilled converge).

### Behavioral changes

1. **`add_entity` (extraction write surface):**
   - When `deduplicate=True`, build the prospective node's embeddable text
     via `node_to_embedding_text` over the node dict being written
     (type + name + properties + content), embed it with the search
     model, run `dedupe_entity` against THAT vector.
   - On the non-merged path (`flagged` / `none`), persist that SAME vector
     as the new node's `embedding` (it already does
     `embedding or None` — just ensure the vector is the node-text one,
     not the name one).
   - The preference/fact statement-embedding special-case (#032) STILL
     wins for those types — it is not node-text. Keep that branch: for
     PREFERENCE embed `properties.statement`, for FACT embed
     `properties.object`, and persist that. Only the GENERIC node types
     switch from name-embedding to node-text-embedding.

2. **Extraction pipeline (`pipeline.py`):**
   - Task ④ `embed_entities_task` currently embeds the canonical NAME and
     task ⑤/⑥ reuse that name-vector. Change the embed grain to NODE-TEXT
     so the dedup vector and the persisted vector match the search space.
     Concretely: build the per-entity node-text from the resolved node
     and embed THAT (still cache-friendly: cache key becomes the node-text
     string, or a stable hash of it).
   - The `_CachedSingleEmbedding` reuse plumbing in `_dispatch_entity_write`
     keeps working — it just carries the node-text vector now.
   - `dedupe_entities_task` passes the node-text vector to `dedupe_entity`.
   - Keep the preference/fact statement path: those entities embed
     statement/object, not node-text, in BOTH the dedup vector and the
     persisted vector (already true for the persisted side; make the dedup
     side consistent so a preference is deduped against the same space it
     is stored in).

3. **No recompute downstream:** because the dedup-created node already
   carries its node-text vector, `indexing.embed_nodes` (backfill) is a
   no-op for these nodes (it only touches nodes whose `embedding` is
   missing/empty) — verify this still holds.

### Migration consequence (must be surfaced)

Existing persisted node vectors are now stale in TWO ways: dedup-created
rows hold NAME embeddings (wrong text), and any voyage-3-era rows are in a
different semantic space. The cure is a re-extract + re-index. Reference
the existing `RESET_ONTOLOGY=1` runbook in `CLAUDE.md`
("Phase 2-5 reset-ontology migration") — this task does NOT add a new
migration script; it documents that operators must re-run extraction so
every node's vector is recomputed as node-text-via-search-model. Add a
note to the task log and call it out for the Step-3 approval gate.

## Acceptance Criteria

- [x] `add_entity` builds the dedup query vector from
      `node_to_embedding_text` (#041) using the search model for GENERIC
      node types; PREFERENCE embeds `properties.statement`, FACT embeds
      `properties.object` (unchanged from #032).
- [x] On the non-merged path, the new node's persisted `embedding` is the
      SAME vector used for the dedup decision (no second embed call for
      the same node).
- [x] Pipeline task ④ embeds NODE-TEXT (not the bare name) for generic
      types; the cache key reflects the node-text input.
- [x] `dedupe_entity` is called with the node-text vector in the pipeline
      path (`dedupe_entities_task`).
- [x] Integration test: extract a fresh document, then assert a
      newly-created PERSON node's persisted `embedding` equals the vector
      produced by embedding its node-text with the search model (within
      float tolerance), NOT the vector of embedding its bare name.
- [x] Integration test: after extraction creates a node with its
      node-text vector, running the indexing backfill (`embed_nodes`)
      does NOT re-embed it (returns 0 for that node / leaves the vector
      unchanged).
- [x] Integration test: a PREFERENCE node still stores the
      `properties.statement` embedding (regression for #032 — supersession
      comparison stays statement-vs-statement).
- [x] Dedup decisions are made in the same vector space as the persisted
      vectors (assert via a test that seeds a node with a node-text vector
      and confirms a near-duplicate mention auto-merges at the configured
      threshold).
- [x] Task log notes the re-extract migration consequence and references
      the `RESET_ONTOLOGY=1` runbook in `CLAUDE.md`.
- [x] `make memory-unit-tests` and `make memory-integration-tests-all`
      pass (mongot stack up locally). *(unit: 1220 passing; full
      `make memory-integration-tests-all` confirmed at the Tester gate:
      216 passed, 1 skipped (SERP env-skip) in 446s with the live mongot
      stack — all 4 #042 tests incl. the two `requires_mongot` cases
      green.)*
- [x] Format/lint/pre-commit clean.

## User Stories

### Story: A new entity is deduped and stored in one consistent space
1. Extraction encounters a new PERSON "Andrej Karpathy" with properties.
2. The pipeline builds its node-text (`person: Andrej Karpathy\n...`),
   embeds it once with the search model.
3. Dedup runs `$vectorSearch` with that node-text vector against existing
   node-text vectors; no match → a new node is created.
4. The new node's persisted `embedding` IS that node-text vector — the
   indexing backfill later finds nothing to do for it.

### Story: A near-duplicate auto-merges because spaces match
1. The graph already holds a PERSON node whose `embedding` is its
   node-text vector (search model).
2. Extraction sees the same person mentioned with slightly different
   surface text.
3. Dedup embeds the new mention's node-text in the SAME space and the
   `$vectorSearch` cosine clears `auto_merge_threshold` → the mention
   merges into the existing node instead of creating a duplicate.

### Story: Operator re-extracts to converge stale vectors
1. Operator reads the task log / Step-3 note: existing node vectors are a
   mix of name-embeddings and node-text-embeddings.
2. Operator runs the `RESET_ONTOLOGY=1` migration
   (`make memory-migrate-multi-tenancy USER_IDENTIFIER=... RESET_ONTOLOGY=1`)
   which wipes `knowledge_graph` and re-triggers extraction + indexing.
3. After re-extraction every node carries a node-text-via-search-model
   vector; `$vectorSearch` queries and dedup decisions agree.

### Story: A preference is still compared statement-to-statement
1. Extraction sees a new preference "prefers dark mode".
2. The pipeline embeds `properties.statement` (not node-text) for both the
   dedup vector and the persisted vector.
3. The supersession resolver compares the new statement embedding against
   prior statement embeddings — apples-to-apples, unchanged by this task.

---

Blocked by: #041

## Log

### [PM] 2026-05-20 — Grooming

**Summary**
Makes DEDUP embed NODE-TEXT (via the #041 shared function) with the SEARCH
model, decide against that vector, and persist the SAME vector on a
new node — so dedup-created and indexing-backfilled vectors converge into
one space and indexing never recomputes.

**Key decisions**
- Generic node types switch from name-embedding to node-text-embedding for
  both the dedup vector and the persisted vector.
- PREFERENCE/FACT keep their statement/object embedding (#032) on BOTH the
  dedup and persisted side — consistency without breaking supersession.
- No new migration script: re-extract via the existing `RESET_ONTOLOGY=1`
  runbook converges stale rows. This is an operator decision surfaced at
  the Step-3 gate.

**Dependencies**
- #041 — the shared `node_to_embedding_text` function.

**User stories**
- 4 stories: new entity deduped+stored in one space; near-duplicate
  auto-merges; operator re-extracts to converge; preference still
  statement-vs-statement.

**Open questions**
- (Surfaced in the plan, not blocking grooming) Operator must accept the
  re-extract migration consequence and the `$vectorSearch`-degraded window
  during rebuild.

Ready for implementation.

### [SWE] 2026-05-20 14:55 — Implementation

**Files modified**
- `apps/memory/src/tree/memory/extraction/add_entity.py` — dedup now
  embeds the prospective node's NODE-TEXT (new `_embeddable_text`: generic
  → `node_to_embedding_text`; PREFERENCE → `properties.statement`; FACT →
  `properties.object`) and reuses that one vector as the persisted
  `embedding` on the non-merged path (no second embed).
- `apps/memory/src/tree/memory/extraction/pipeline.py` — task ④
  `_embed_entity` now embeds an embeddable TEXT (node-text grain) via
  `get_search_embedding_model()`; task ③ `_resolve_entities` precomputes
  `embeddable_text_by_key` (new `_entity_embeddable_text`, mirrors
  `add_entity._embeddable_text`); task ⑤ deduplicates against the
  node-text vector keyed by that text; task ⑥ `_dispatch_entity_write`
  reuses the cached node-text vector and dropped the on-the-fly statement
  embed; flow + `run_extraction_for_documents` embed the unique node-text
  set (MCP path uses its injected model directly).
- `apps/memory/src/tree/memory/types.py` — `EmbeddingMap` re-keyed by
  embeddable text; `ResolutionOutput.embeddable_text_by_key` added.
- `apps/memory/tests/unit/memory/extraction/test_add_entity.py` — new
  `TestAddEntityNodeTextEmbedding` (node-text dedup vector; same-vector
  reuse / no-second-embed; PREFERENCE statement; FACT object).
- `apps/memory/tests/unit/memory/extraction/test_pipeline.py` — new
  `TestEntityEmbeddableText` + `TestDispatchEntityWriteReusesVector`;
  updated task-④ test to the node-text/search-model grain.
- `apps/memory/tests/integration/memory/test_dedup_node_text_embedding.py`
  — NEW: AC-5 (persisted vector == node-text, ≠ name), AC-6 (backfill
  no-op), AC-7 (#032 preference statement regression), auto-merge story
  (real `$vectorSearch`, same-space merge).
- Patched `get_search_embedding_model` into the extraction-flow patch
  helpers of `test_extraction_pipeline.py`, `test_preference_supersession.py`,
  `test_fact_island.py`, `test_pole_o_extraction_e2e.py`,
  `test_validator_e2e.py`, `test_two_user_isolation.py` (task ④ now reads
  that factory).

**Tests**
- Unit: 1220 passing, 0 failing — `make memory-unit-tests`.
- Integration (targeted, live mongot up): 4 new #042 tests, 14 dedup,
  16 extraction/preference/fact, 50 pole_o/validator/two-user/add_entity,
  11 MCP ingest — all passing. Full `make memory-integration-tests-all`
  deferred to the Tester acceptance gate.

**How the node-text vector flows dedup → new-node-persist**
1. Task ③ resolves each entity and stores its embeddable text in
   `ResolutionOutput.embeddable_text_by_key` (generic → node-text via the
   #041 builder using the resolved canonical_name + stored-shape
   properties; PREFERENCE/FACT → statement/object).
2. Task ④ embeds the unique set of those texts ONCE with the SEARCH model
   (`get_search_embedding_model`); `EmbeddingMap` is keyed by the text, so
   the `INPUTS` cache key is the node-text string.
3. Task ⑤ deduplicates each entity against `embeddings[node_text]` — the
   `$vectorSearch` query vector is now node-text/search-model, the same
   space as the persisted corpus.
4. Task ⑥ wraps `embeddings[node_text]` in `_CachedSingleEmbedding` and
   hands it to `add_entity`; `add_entity` rebuilds the identical text via
   its own `_embeddable_text`, calls `embedding_model.embed([...])` (the
   cached wrapper returns the seeded vector — one logical embed), runs
   dedup, and on the non-merged path persists THAT SAME vector
   (`embedding or None`).

**Reuse / no-recompute evidence**
- Unit `test_new_node_persists_same_vector_no_second_embed`: a recording
  model proves exactly ONE embed call for the node and that the persisted
  `embedding` equals that vector.
- Unit `test_no_second_embed_call_reuses_node_text_vector`: task ⑥ hands
  `add_entity` a `_CachedSingleEmbedding` of the task-④ node-text vector
  and the real model's `embed` is asserted never called.
- Integration `test_indexing_backfill_is_noop_for_dedup_created_node`:
  after extraction the person node's vector is non-empty, so
  `embed_nodes` (filters `embedding ∈ {[], None}`) leaves it unchanged.

**Resolution name-embedding (#043 boundary)**
- No temporary duplication introduced. Resolution's semantic stage embeds
  the bare NAME inside `SemanticMatchResolver._embed_cached` using the
  resolver-owned model — it never depended on the task-④ name vector, so
  switching task ④ to node-text did NOT break resolution and required no
  compensating name-embed. The resolver is still constructed from
  `get_embedding_model()` (the search model shim); #043 will repoint it to
  `get_resolution_embedding_model()`. That repoint is the only #043 work
  here; #042 leaves resolution behavior-identical.

**Migration consequence (Step-3 gate)**
- Existing persisted node vectors are now stale in two ways: dedup-created
  rows hold NAME embeddings (wrong text), and any voyage-3-era rows are in
  a different semantic space. After this change the dedup `$vectorSearch`
  compares a node-text query vector against those stale stored vectors —
  apples-to-oranges until a re-extract. **Operators must re-run extraction
  so every node's `embedding` is recomputed as node-text-via-search-model.**
  No new migration script is added; use the existing `RESET_ONTOLOGY=1`
  runbook in `CLAUDE.md` (“Phase 2-5 reset-ontology migration”):
  `make memory-migrate-multi-tenancy USER_IDENTIFIER=... RESET_ONTOLOGY=1`
  (dry-run first), which drops `knowledge_graph` and re-triggers
  extraction + indexing. The `$vectorSearch`-degraded window during the
  index rebuild applies as documented in that runbook.

**Caveats for the Tester**
- The new integration file forces `PREFECT_TASKS_REFRESH_CACHE=true`
  (autouse) and uses unique per-run entity names + a stable sha256-based
  deterministic embedder, because task ④'s on-disk `INPUTS` result cache
  would otherwise return a vector computed by a previous session's model
  for the same node-text and mask the assertion. Tester: run with the
  full docker stack up (mongot) for the `requires_mongot` cases
  (backfill-no-op, auto-merge).
- `run_extraction_for_documents` (MCP ingest path) now embeds via its
  INJECTED `embedding_model` (not the factory) at the node-text step —
  this fixed 2 MCP ingest tests that would otherwise hit the real Voyage
  endpoint (no API key in the test env). The Prefect flow's task ④ keeps
  using `get_search_embedding_model()` (no injected handle there).
- PREFERENCE/FACT statement embedding (#032) is unchanged on both the
  dedup and persisted side; supersession stays statement-vs-statement.
- NOT RUN: live end-to-end via `make memory-serve-workflows` +
  `make memory-run-memory-pipeline-extraction` — requires a real
  `VOYAGE_API_KEY` (absent in this env). The flow is exercised end-to-end
  through `memory_extraction.fn(...)` against live MongoDB + mongot
  (incl. a real `$vectorSearch` auto-merge) in the new integration tests
  instead.

DO NOT COMMIT — handing off to the Tester.

### [Tester] 2026-05-20 17:30 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`memory-format-check` 242 files
  formatted; `memory-lint-check` all checks passed; `make pre-commit`
  all hooks Passed incl. KGQuery discipline).
- Unit tests: 1220 passed / 0 failed (`make memory-unit-tests`, 45.95s).
- Integration tests (full, mongot up): 216 passed / 1 skipped / 0 failed
  (`make memory-integration-tests-all`, 446s). The 1 skip is
  `test_web_search_ingest.py` (SERP env-skip, pre-existing, unrelated to
  #042). The known SERP flakes did NOT fire this run.
- Warnings: 0.

**E2E adversarial pass** (live mongot `$vectorSearch`; temp probe file
`test_tester_adversarial_042.py`, run then removed — not committed)
- Happy path: `memory_extraction(...)` on a fresh PERSON doc → new node
  persisted with its node-text vector; `row["embedding"] == vec(node_text)`,
  `!= vec(name)` (covered by `test_person_node_persists_node_text_vector_not_name`,
  re-verified by my probe). PASS.
- Break path A (reuse / no-recompute, crux): spy SEARCH model + separate
  resolver-owned model through the FULL flow → search model embedded the
  node-text exactly ONCE, NEVER embedded the bare name, and persisted
  `embedding == vec(node_text)`. The bare-name embed lives on the
  resolver's own model (the #043 boundary), confirmed isolated. Unit
  `test_new_node_persists_same_vector_no_second_embed` independently
  asserts call-count==1 AND vector identity (not just "a vector exists").
  PASS.
- Break path B (apples-to-apples + disambiguation): two PERSON entities
  sharing surface name `john smith <uuid>` but different
  properties/node-texts → real `$vectorSearch` in node-text space →
  `summary.nodes_merged == 0` (NO spurious merge; bare-name embedding
  would have merged them). The SWE's `test_two_runs_merge_into_one_node`
  proves the positive (auto-merge fires). Together they prove the change
  is real, not cosmetic. PASS.
- Break path C (stale-vector migration honesty): `RESET_ONTOLOGY=1`
  runbook present in CLAUDE.md (L324-376) + silent-corruption CAUTION
  (L397-415); SWE log L268-280 documents the two-dimensional staleness and
  references the runbook for the Step-3 gate. No new guard added; mixed-
  space stored vectors produce wrong-but-plausible matches until
  re-extract — acceptable + documented (consistent with the existing
  dim-only `assert_settings_match_live_vector_index`). PASS.
- Break path D (preference/fact untouched): `_embeddable_text` /
  `_entity_embeddable_text` route PREFERENCE→`statement`, FACT→`object`,
  with a defensive node-text fallback for empty statements. Covered by
  unit `test_preference_embeds_statement_not_node_text`,
  `test_fact_embeds_object_not_node_text`,
  `test_preference_without_statement_falls_back_to_node_text`, and
  integration `test_preference_persists_statement_vector`
  (statement-vec wins, node-text-vec does NOT). PASS.
- Break path E (cache poisoning): task ④ `embed_entities_task` takes a
  single `text` arg under `cache_policy=INPUTS` → on-disk cache key IS the
  node-text string, so in PROD a node-text query can never be served a
  stale name-vector (the pre-#042 cache keyed on name). New integration
  file defeats it in-test via `PREFECT_TASKS_REFRESH_CACHE=true` autouse +
  unique uuid names + stable sha256 embedder; my probes replicated this
  and passed. `_CachedSingleEmbedding.embed` ignores input text and
  returns the seeded vector, so reuse holds even if the two text builders
  drifted — robust. PASS.
- Break path F (no un-recomputed gaps): after extraction the new node
  carries a non-empty vector of the search-model dim; integration
  `test_indexing_backfill_is_noop_for_dedup_created_node` (requires_mongot)
  confirms `embed_nodes` leaves it unchanged. My probe re-confirmed a
  non-empty 16-d vector on the new node. PASS.

**Acceptance criteria**
- [x] PASS — `add_entity` builds dedup query vector from
      `node_to_embedding_text` for generic types; PREFERENCE→statement,
      FACT→object — `add_entity._embeddable_text` (add_entity.py:317-371);
      `test_dedup_query_vector_is_node_text_not_name`.
- [x] PASS — non-merged path persists the SAME vector, no second embed —
      add_entity.py:242/288; `test_new_node_persists_same_vector_no_second_embed`
      (call-count==1 + identity); flow-level spy probe.
- [x] PASS — task ④ embeds NODE-TEXT, cache key = node-text — pipeline.py
      `_embed_entity` (839-861), `embed_entities_task` INPUTS;
      `test_returns_text_and_vector`.
- [x] PASS — `dedupe_entity` called with node-text vector in pipeline path —
      pipeline.py:900-919 (`_dedupe_entities` looks up `embeddable_text`).
- [x] PASS — integration: new PERSON persisted vector == node-text vec, ≠
      name vec — `test_person_node_persists_node_text_vector_not_name`.
- [x] PASS — integration: indexing backfill no-op for dedup-created node —
      `test_indexing_backfill_is_noop_for_dedup_created_node` (requires_mongot).
- [x] PASS — integration: PREFERENCE stores statement embedding (#032
      regression) — `test_preference_persists_statement_vector`.
- [x] PASS — dedup decisions in same space as persisted vectors / near-dup
      auto-merges — `test_two_runs_merge_into_one_node` (requires_mongot,
      real `$vectorSearch`) + my same-name/different-content non-merge probe.
- [x] PASS — task log notes re-extract consequence + references
      `RESET_ONTOLOGY=1` runbook — tracker L268-280; CLAUDE.md L324-415.
- [x] PASS — `make memory-unit-tests` (1220) + `make memory-integration-tests-all`
      (216 passed / 1 env-skip) green with mongot up.
- [x] PASS — format/lint/pre-commit clean.

**Evidence**
```
$ make memory-unit-tests
============================ 1220 passed in 45.95s =============================

$ make memory-integration-tests-all
tests/integration/memory/test_dedup_node_text_embedding.py ....   [ 58%]
================== 216 passed, 1 skipped in 446.33s (0:07:26) ==================

# Tester probe (run then removed; not committed):
TestSameNameDifferentContentDoesNotMerge ......... 1 passed   # break path B-neg + F
TestSingleEmbedPerNewNode .......................... 1 passed  # break path A (flow spy)
```

**Other issues found**
- (Nit, not blocking) The SWE's `test_two_runs_merge_into_one_node` uses a
  CONSTANT-vector model (cosine 1.0 for everything), so on its own it only
  proves the merge *fires*, not that node-text *disambiguates*. The
  negative case (same name, different content → no merge) was not in the
  committed suite; I verified it with a temp probe and it passed. Consider
  the orchestrator/PR-reviewer asking the SWE to fold a non-merge
  same-name/different-content case into the committed integration suite so
  the disambiguation guarantee has a permanent regression test. Behavior
  is correct today; this is test-coverage hardening, not a defect.

**VERDICT: PASS**

### [SWE] 2026-05-20 18:10 — Coverage hardening (Tester nit) + commit

**Why**
The Tester PASSED with one non-blocking coverage nit: the committed
auto-merge test (`test_two_runs_merge_into_one_node`) uses a CONSTANT-vector
model, so it only proves a merge *fires* — the headline #042 disambiguation
guarantee (same surface name, different node-text content → NO spurious
merge) had no permanent committed regression test (the Tester proved it with
a temp probe, then removed it). Folded that negative case into the committed
suite.

**Files modified**
- `apps/memory/tests/integration/memory/test_dedup_node_text_embedding.py` —
  added `TestSameNameDifferentContentDoesNotMerge::test_same_name_different_content_does_not_auto_merge`
  (`slow`, `requires_mongot`, consistent with the sibling auto-merge test).
  Seeds an existing PERSON node carrying its NODE-TEXT vector (the post-#042
  corpus shape), then runs the REAL `$vectorSearch` `dedupe_entity` for a
  same-name (`"john smith"`) incoming entity whose NODE-TEXT vector is
  materially different content (engineer vs. jazz saxophonist). Uses the
  existing deterministic sha256-seeded `_PerTextEmbeddingModel` so the two
  node-texts produce genuinely different vectors. Asserts
  `result.action != "merged"` and `similarity_score < auto_merge_threshold`.
  Imports `DeduplicationConfig` + `dedupe_entity`.

**The disambiguation guarantee, made permanent**
- Pre-#042 (bare-NAME embedding): identical names → query/stored vectors
  identical → raw cosine 1.0 → `action="merged"` → the two distinct people
  spuriously collapse.
- Post-#042 (NODE-TEXT embedding): engineer-text vs. saxophonist-text →
  raw cosine ≈0.66. Production config (fuzzy ON) is used so the
  identical-name boost (fuzzy = 1.0, combined = (0.66 + 1.0)/2 ≈ 0.83) is
  exercised and STILL stays below the 0.95 `auto_merge_threshold` → NOT a
  merge. Verified analytically and live (`tier: none`).
- Together with the SWE's positive `test_two_runs_merge_into_one_node` (merge
  fires) the change is proven real, not cosmetic.

**Tests**
- New test: PASS (live `$vectorSearch`) — 1 passed in 11.26s.
- Full #042 integration file: 5 passed in 24.86s (4 prior + the new negative
  case; `requires_mongot` cases green with the docker stack up).
- Unit: 1220 passing, 0 failing (`make memory-unit-tests`, 41.48s).
- Format / lint / pre-commit: clean (`memory-format-check`,
  `memory-lint-check`, all pre-commit hooks Passed incl. KGQuery discipline).

**Evidence**
```
$ uv run pytest tests/integration/memory/test_dedup_node_text_embedding.py -v
... TestSameNameDifferentContentDoesNotMerge::test_same_name_different_content_does_not_auto_merge PASSED [100%]
============================== 5 passed in 24.86s ==============================

$ make memory-unit-tests
============================ 1220 passed in 41.48s =============================
```

**Migration consequence (re-stated for the commit body)**
Existing persisted node vectors are stale after this change (dedup-created
rows hold NAME embeddings; any voyage-3-era rows are a different semantic
space). Operators MUST re-extract so every node's `embedding` is recomputed
as node-text-via-search-model — use the existing `RESET_ONTOLOGY=1` runbook
in `CLAUDE.md` (no new migration script). The commit body records this.

**Commit**
- Conventional Commits: `feat(memory): dedup on node-text embedding + reuse vector on new-node creation`.
- Body notes the stale-vector / `RESET_ONTOLOGY=1` re-extract consequence.
- `Closes-tracker: 042-dedup-node-text-embedding-reuse`.
- Tracker moved to `tracker/done/` in the same commit.
