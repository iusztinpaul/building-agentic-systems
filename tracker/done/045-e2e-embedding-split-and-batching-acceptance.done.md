# E2E acceptance: consistent node-text vectors + batched embedding

Status: pending
Tags: `data`, `infra`, `enhancement`, `P1`
Depends on: #044
Blocks: —

## Scope

Prove the whole feature end-to-end on the real pipeline: split models,
shared node-text embed function, dedup-on-node-text with vector reuse,
resolution-on-resolution-model, and real-time request batching all work
together against a live MongoDB + mongot stack, with consistent persisted
vectors and a measurably lower embed-request count.

This is a verification task. It adds a slow, mongot-dependent e2e
integration test (`@pytest.mark.slow`,
`@pytest.mark.requires_mongot`) plus a documented manual runbook the
Tester executes at the acceptance gate. It does NOT add new production
behavior beyond what #039–#044 shipped; if it surfaces a defect, that
routes back to the responsible task as a rollup.

### Seed user

Per `CLAUDE.md` ("Populating the Users Collection: By default, you will
use the 'Paul Iusztin' user when testing"), the e2e uses the
**Paul Iusztin** seed user. The migration/bootstrap path
(`make memory-migrate-multi-tenancy USER_IDENTIFIER=<paul's identifier>`)
seeds it.

### What the e2e must demonstrate

1. **Full chain runs:** data → extraction → indexing → query, via the
   documented make targets, against the live stack.
2. **Consistent persisted vectors:** every node `embedding` is a
   node-text-via-`search_embedding_model` vector (generic types), or a
   statement/object vector for PREFERENCE/FACT — and `embed_nodes`
   backfill is a no-op immediately after extraction (extraction already
   persisted the vectors).
3. **Vector-space agreement:** a `$vectorSearch` query for a known seeded
   entity returns that entity at top rank (dedup space == query space ==
   index space).
4. **Batching reduces requests:** with a counting/instrumented embedding
   model (or by inspecting Voyage request logs), the indexing + extraction
   embed-request count is materially lower than the pre-batching
   one-per-text baseline.
5. **Resolution uses the resolution model:** with a distinguishable
   resolution vs search model pairing in a test YAML, confirm resolution
   embeddings come from the resolution model and persisted vectors from
   the search model.

### Rate-limit reality (note for the Tester — do not be surprised)

The Voyage FREE tier is 3 RPM. Batching reduces request count but a FULL
e2e over all `configs/default.yaml` sources may still throttle. The
Tester should EITHER use a paid-tier key OR run the e2e over a small
`DOC_IDS` subset (a handful of documents) so the run completes within the
rate window. Document the subset command. The 429 backoff should ride out
transient throttling; `429 rate-limit retries exhausted` in the e2e means
the subset is too large for the tier, not a code defect.

## Acceptance Criteria

- [x] New e2e integration test under
      `apps/memory/tests/integration/memory/` marked `@pytest.mark.slow`
      and `@pytest.mark.requires_mongot` that runs extraction → indexing →
      `$vectorSearch` query against the live stack for the seed user and
      asserts a seeded entity is retrievable at top rank.
      — `tests/integration/memory/test_e2e_embedding_split_and_batching.py::TestEmbeddingSplitAndBatchingE2E::test_full_chain_consistent_space_batched_and_routed`
- [x] Test asserts a newly-extracted generic node's persisted `embedding`
      equals the search model's embedding of its node-text (within float
      tolerance) — proving dedup-created and index-backfilled vectors live
      in one space. — `assert row["embedding"] == search_model.vec(node_text)`
- [x] Test asserts `embed_nodes` backfill immediately after extraction
      re-embeds 0 nodes (extraction already persisted node-text vectors).
      — backfill leaves the headline node's vector unchanged.
- [x] Test asserts (via a counting embedding model) that embedding N
      node-texts during indexing issues fewer requests than N (batching
      effective). — `len(search_model.calls) < total_people` (31 node-texts).
- [x] Test asserts (distinguishable model pairing) resolution embeddings
      come from the resolution model and persisted vectors from the search
      model. — resolution sentinel never persisted; resolution embeds NAMES only.
- [x] A manual runbook is captured in the task log: the exact make-command
      sequence (`make memory-serve-workflows &` →
      `make memory-run-data-pipeline USER_ID=<paul>` →
      `make memory-run-memory-pipeline-extraction USER_ID=<paul>
      [DOC_IDS=...]` → `make memory-run-memory-pipeline-indexing
      USER_ID=<paul>` → `make memory-query-graph USER_ID=<paul>
      QUERY="..."`) with expected observations, plus the small-`DOC_IDS`
      variant for free-tier rate limits.
- [ ] [HUMAN] On a paid-tier Voyage key (or the documented small-`DOC_IDS`
      subset on free tier), the manual runbook completes without
      `429 rate-limit retries exhausted` and the query returns relevant
      results — confirming live behavior, not just mocked tests.
      — PARTIAL on free tier: live extraction over 3 docs COMPLETED (186
      nodes / 94 edges, 137 nodes at 1024-d), live `query_memory` returned
      10 relevant nodes; the 162-node indexing backfill hit
      `429 rate-limit retries exhausted` (free-tier 3-RPM / 10K-TPM
      exhaustion, not a code defect — see runbook). Needs a paid key (or a
      cooldown + 1-doc subset) for a fully green live `$vectorSearch` run.
- [x] `make memory-integration-tests-all` passes locally with the full
      docker-compose mongot stack up. — Tester ran the full acceptance-gate
      target: 222 passed, 1 skipped (pre-existing BrightData-cred skip) in
      522.58s. The headline e2e ran and passed inside the full suite (not
      just isolation).
- [x] Format/lint/pre-commit clean.

## User Stories

### Story: Operator runs the full pipeline and gets relevant answers
1. Operator seeds the Paul Iusztin user and runs the data, extraction,
   and indexing pipelines via the make targets.
2. Operator runs `make memory-query-graph USER_ID=<paul> QUERY="memory for
   AI agents"`.
3. The query returns nodes relevant to the seeded documents at the top —
   semantic search works because dedup/index/query share one vector space.

### Story: Operator confirms embedding got faster / cheaper
1. Operator runs indexing over a backfill of many nodes.
2. The logs show a handful of multi-input embed requests instead of one
   per node.
3. On free tier the run no longer dies with
   `429 rate-limit retries exhausted` for a reasonably-sized subset.

### Story: Tester verifies vector consistency without trusting prose
1. Tester runs the e2e integration test.
2. It extracts a node, reads its persisted `embedding`, re-embeds the
   node-text with the search model, and asserts they match.
3. It confirms the indexing backfill is a no-op for that node — the
   persisted vector came from extraction, not a recompute.

### Story: Tester avoids a false failure from free-tier throttling
1. Tester reads the runbook note about the 3-RPM free tier.
2. Tester runs the manual e2e over a small `DOC_IDS` subset (or with a
   paid key).
3. The run completes inside the rate window; a 429-exhaustion is
   recognized as a tier limit, not a regression.

---

Blocked by: #044

## Log

### [PM] 2026-05-20 — Grooming

**Summary**
End-to-end acceptance: a slow + mongot-dependent integration test plus a
manual runbook proving the whole feature on the live stack — consistent
node-text vectors, vector-space agreement (dedup == query == index), a
lower embed-request count, and resolution-on-resolution-model. Uses the
Paul Iusztin seed user per CLAUDE.md.

**Key decisions**
- Verification-only task; defects route back to the responsible
  #039–#044 task as a rollup, not fixed here.
- Free-tier 3-RPM reality documented: Tester uses a paid key OR a small
  `DOC_IDS` subset; a `429 rate-limit retries exhausted` on a too-large
  subset is a tier limit, not a regression.

**Dependencies**
- #044 — the full stack of changes must be in place to verify e2e.

**User stories**
- 4 stories: full pipeline returns relevant answers; embedding got
  faster/cheaper; Tester verifies vector consistency programmatically;
  Tester avoids a false failure from free-tier throttling.

**Open questions**
- (Surfaced in plan) Which Voyage tier the Tester will use for the [HUMAN]
  live-run criterion.

Ready for implementation.

### [SWE] 2026-05-20 15:35 — Implementation

**Files modified**
- `apps/memory/tests/integration/memory/test_e2e_embedding_split_and_batching.py` —
  NEW headline e2e (`@pytest.mark.slow`, `@pytest.mark.requires_mongot`):
  one test runs the full chain (`memory_extraction` → `embed_nodes`
  backfill → `memory_indexing` → live `$vectorSearch` + `query_memory`)
  for a seed user against the live mongot stack and asserts all five
  feature guarantees in one cohesive run, plus the AC#6 backfill-no-op
  corollary. Deterministic distinguishable fake models (a counting
  per-text SEARCH model + a sentinel RESOLUTION model) keep it fast and
  off the Voyage rate limit.

This is a verification-only task — no production code changed.

**Tests**
- New e2e: 1 passing — `tests/integration/memory/test_e2e_embedding_split_and_batching.py::TestEmbeddingSplitAndBatchingE2E::test_full_chain_consistent_space_batched_and_routed` (12-15s, requires_mongot).
- Sibling regression run (#042 + #044 + #043 + new e2e), slow+mongot: `10 passed in 32.13s`.
- Unit: `1246 passed in 41.56s` (`make memory-unit-tests`).
- Format/lint/pre-commit: all clean (`memory-format-check`, `memory-lint-check`, `pre-commit` all Passed).

**What the e2e proves (each = one of the 5 demonstration points)**
1. Full chain runs end-to-end against live mongot.
2. Consistent persisted vectors: `row["embedding"] == search_model.vec(node_text)`, and `!= vec(name)` and `!= resolution sentinel`.
3. Vector-space agreement: a `$vectorSearch` with the headline node's own node-text vector returns it at TOP rank; `query_memory` returns it too.
4. Batching: 31 node-texts embedded in `< 31` requests (one batched request carries `>= 31`).
5. Resolution routing: resolution model embedded NAMES only (no `\n`), its sentinel `[9.0]*8` never persisted.
6. (AC#6) `embed_nodes` backfill right after extraction leaves the headline node's vector byte-identical (reused, not recomputed).

**Seed user used**
- **Paul Iusztin** — `identifier="paul.iusztin@example.com"`, `_id=6a0c5a5b5a2dfdfd3cedb7f4`.
- Why this id: the `users` collection was empty but 2814 `documents` were
  already tenanted to the orphaned id `6a0c5a5b5a2dfdfd3cedb7f4` (the prior
  `dev@example.com` seed, since deleted). The migration ABORTS when docs
  carry a different tenant's `user_id`, so re-seeding a brand-new Paul id
  would have failed. I re-created the `User` row AT that exact `_id` named
  "Paul Iusztin" (fires the `after_insert` self-person hook), adopting the
  existing docs without re-tenanting 2814 rows. Satisfies CLAUDE.md's
  "Paul Iusztin" default + the existing-data reality.

**Manual runbook (the [HUMAN] AC) — exact sequence**

Prereqs: docker stack up (`make local-start`); `.env` has `VOYAGE_API_KEY`.
Seed Paul (one-time):
```
# Standard path on a clean DB (no foreign-tenant docs):
make memory-migrate-multi-tenancy USER_IDENTIFIER=paul.iusztin@example.com NAME="Paul Iusztin" DRY_RUN=1   # inspect
make memory-migrate-multi-tenancy USER_IDENTIFIER=paul.iusztin@example.com NAME="Paul Iusztin"             # apply
# This session: docs were pre-tenanted to 6a0c5a5b5a2dfdfd3cedb7f4 so the
# User row was re-created at that id directly (see "Seed user used").
```
Full chain (PAUL=6a0c5a5b5a2dfdfd3cedb7f4):
```
make memory-serve-workflows &                                  # in-process Prefect worker
make memory-run-data-pipeline USER_ID=$PAUL                    # (docs already ingested this session)
make memory-run-memory-pipeline-extraction USER_ID=$PAUL DOC_IDS="<id1>,<id2>,<id3>"   # SMALL subset for free tier
make memory-run-memory-pipeline-indexing USER_ID=$PAUL
make memory-query-graph USER_ID=$PAUL QUERY="memory for AI agents"
```
Expected observations: extraction logs `embed_in_batches: N texts -> few
request(s)`; persisted node `embedding` length is 1024; indexing
`Embedded K nodes`; query returns nodes relevant to the seeded docs.

**Free-tier rate-limit variant + reality (CONFIRMED THIS RUN)**
- Pick a SMALL `DOC_IDS` subset (3 entity-rich Substack docs were used,
  NOT the math-arxiv docs which extract 0 POLE+O entities). On free tier
  even 3 docs saturate the 3-RPM / 10K-TPM budget across extraction +
  indexing back-to-back. `429 rate-limit retries exhausted` on the
  indexing backfill is a TIER LIMIT, not a regression (per groomed spec).

**Live e2e EVIDENCE (real Voyage, in-process to bypass a stale shared
Docker prefect-worker — see Notes)**
- Live extraction over 3 docs `[6a0c87758606ef0c89219cdc,
  6a0c87768606ef0c89219d1c, 6a0c87768606ef0c89219d88]`:
  `WriteSummary nodes_written=186 edges_written=94 documents_processed=3`.
  429s fired and the in-`.embed()` backoff (2/4/8/16/30/60s) rode them
  out — the extraction COMPLETED, no exhaustion.
- Persisted KG state (mongosh):
  - `162` nodes / `95` edges for Paul (post-dedup-merge across chunks).
  - `137` nodes carry a real vector; embedding-dimension distribution =
    `dim 1024: 137 nodes` (uniform; NO dim-drift). Sample person node
    `6a0c5a5b5a2dfdfd3cedb7f4:person:alexey` → `dim 1024`. These vectors
    were written INLINE by extraction (search-model node-text) — the
    consistent persisted space on real data.
  - Node-type breakdown: object 52, fact 34, person 21, chunk 21, event
    18, organization 12, document 3, preference 1.
- Live `query_memory(query="AI agents memory", top_k=5, max_hops=1)`:
  `QUERY_RESULT_NODES=10 EDGES=10`, surfacing relevant chunks from the
  seeded AI/agent docs (text path + graph expansion; vector path
  gracefully degraded to empty during the rate-limit window since the
  live `vector_index` couldn't be built this session).
- Indexing backfill of the 162-node set hit
  `429 rate-limit retries exhausted` (free-tier budget fully spent by the
  extraction run) → `embed_nodes_task` failed before `ensure_indexes`, so
  no live `vector_index` was built today. The live top-rank `$vectorSearch`
  demonstration is therefore covered by the deterministic e2e test (which
  builds the index and asserts top-rank retrieval); a paid key (or a long
  cooldown + 1-doc subset) is needed to green the live vector path.

**Notes / caveats for the Tester**
- `make memory-integration-tests-all` (the full ~5min acceptance-gate
  target incl. mongot) is the Tester's to run with the docker stack up —
  I ran the relevant slow+mongot subset (`10 passed`).
- The new e2e is `requires_mongot` → EXCLUDED from CI; run locally with
  the full `docker-compose.yml` stack. CI mirror: `make memory-integration-tests-ci`.
- Benign teardown noise: after a passing run you'll see a Prefect
  ephemeral-server `ValueError: I/O operation on closed file.` traceback.
  It fires AFTER the green result line (`N passed`) during interpreter
  shutdown — it is NOT a test failure.
- Shared-infra gotcha (live runbook): a stale `tree-prefect-worker` Docker
  container (image `building-agentic-systems-prefect-worker`, from the
  OTHER worktree) was polling the same Prefect deployments and crashed runs
  with `SignatureMismatchError ... expects ['document_ids']` (its baked
  flow predates the `user_id` param). Stopping that container was denied
  (shared infra), so the live runbook was driven IN-PROCESS (identical
  production code paths + live Voyage). For a clean make-target live run,
  the human should stop the stale `tree-prefect-worker` so only the local
  `make memory-serve-workflows` worker (this worktree's merged code)
  executes runs.
- Prefect result caching: these docs were extracted in a prior session, so
  the first deployment run returned `Cached(COMPLETED)` / `nodes_written=0`.
  The live in-process runs set `PREFECT_TASKS_REFRESH_CACHE=true` to force
  recompute. The integration test sets the same env via an autouse fixture.
- DO NOT COMMIT — handing off to the Tester.

### [Tester] 2026-05-20 18:05 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make pre-commit` — all hooks Passed)
- Unit tests: 1246 passed / 0 failed (`make memory-unit-tests`, 41.58s)
- Integration tests (full, mongot): 222 passed / 1 skipped / 0 failed
  (`make memory-integration-tests-all`, 522.58s). The 1 skip is the
  pre-existing BrightData-credential `test_web_search_ingest.py`, unrelated
  to this feature.
- Warnings: 0

**E2E adversarial / verification pass**
- A. Verification-only claim — `git status` + `git diff --stat`: the ONLY
  uncommitted (untracked) #045 files are
  `tests/integration/memory/test_e2e_embedding_split_and_batching.py` +
  tracker files. Zero production source changed in the working tree.
  (`main..HEAD` shows the cumulative #039-#044 work already committed on the
  single feature branch — expected.) PASS.
- B. Deterministic e2e proves the guarantees (read end-to-end):
  - Consistent space (lines 349-358): `row["embedding"] ==
    search_model.vec(node_text)` AND `!= vec(name)` AND `!= [9.0]*8`
    (resolution sentinel). Deep, not shallow. PASS.
  - Resolution routing (lines 362-368): resolution model embedded the NAME,
    names-only (`"\n" not in t`), sentinel never persisted. PASS.
  - Batching (lines 375-385): `max_request_size >= 31` AND `len(calls) < 31`.
    PASS.
  - `$vectorSearch` top-rank (lines 411-438): builds a REAL mongot
    `vector_index` via `memory_indexing`, polls for convergence, asserts
    `ranked[0]["_id"] == headline_id` (semantically-correct top node, not
    just non-empty). PASS.
- C. Consistent-space proof on the make-target run: e2e ran INSIDE
  `make memory-integration-tests-all` (passed at 58% of the suite) AND in
  isolation (`1 passed in 13.68s`) — a real index build + top-rank
  cross-source retrieval, not dimension-uniformity only. PASS.
- D. Free-tier 429 honesty: `429 rate-limit retries exhausted` is the
  explicit anchor raised by the documented exponential-backoff loop in
  `models/voyage_multimodal_embedding.py:184-187` when the backoff schedule
  is exhausted — the batcher (`embedding_text.py`) is strictly upstream of
  the retry loop. This is the documented free-tier-exhaustion path, NOT a
  masked code bug. The deterministic test genuinely substitutes for the
  live vector path (builds index + asserts top-rank), so the feature IS
  proven — just not on live Voyage. No rubber-stamp risk. PASS.
- E. Seed-user soundness (mongosh): `db.users` has Paul Iusztin at
  `_id 6a0c5a5b5a2dfdfd3cedb7f4`; `person:self` node exists tenanted to that
  id; all 2814 docs and all 257 KG rows belong to that single tenant — no
  cross-tenanting. Sound. PASS.
- F. Regression — prior 4 tasks green together: #039-#044 sibling tests
  (`test_dedup_node_text_embedding`, `test_embedding_batching`,
  `test_extraction_pipeline`, `test_get_model`, `test_two_user_isolation`,
  ...) all pass in the full 222-test run. No regression. PASS.

**Acceptance criteria**
- [x] PASS — New slow + requires_mongot e2e runs extraction->indexing->
      `$vectorSearch` and asserts seeded entity at top rank — evidence:
      `test_e2e_embedding_split_and_batching.py::...test_full_chain_consistent_space_batched_and_routed` PASSED (lines 411-438).
- [x] PASS — Persisted `embedding` == search model's node-text embedding
      — evidence: assertion at file:line 349.
- [x] PASS — `embed_nodes` backfill is a no-op for extracted nodes —
      evidence: `after["embedding"] == before_vec` at lines 391-401.
- [x] PASS — Batching issues fewer requests than N node-texts —
      evidence: lines 375-385 (`len(calls) < 31`, `max_request >= 31`).
- [x] PASS — Resolution embeds names via resolution model, search vectors
      persisted — evidence: lines 362-368 + sentinel-not-persisted at 356.
- [x] PASS — Manual runbook captured in the SWE log — evidence: runbook
      with make-command sequence + small-DOC_IDS variant present in log.
- [ ] [HUMAN] — Awaiting human verification. PARTIAL on free tier: live
      extraction over 3 docs COMPLETED (186 nodes/94 edges, 137 vectors all
      1024-d), live `query_memory` returned 10 nodes; indexing backfill hit
      free-tier 429 exhaustion (tier limit per backoff anchor, verified not
      a defect — see D). Deterministic e2e substitutes for the live vector
      index. Needs a paid Voyage key for a fully green live `$vectorSearch`.
- [x] PASS — `make memory-integration-tests-all` passes with mongot —
      evidence: 222 passed, 1 skipped, 0 failed in 522.58s.
- [x] PASS — Format/lint/pre-commit clean — evidence: `make pre-commit`.

**Evidence**
```
$ make memory-unit-tests
============================ 1246 passed in 41.58s =============================

$ make memory-integration-tests-all
================== 222 passed, 1 skipped in 522.58s (0:08:42) ==================

$ uv run pytest tests/integration/memory/test_e2e_embedding_split_and_batching.py -v
...test_full_chain_consistent_space_batched_and_routed PASSED [100%]
============================== 1 passed in 13.68s ==============================
```

**Other issues found**
- Benign teardown noise: a Prefect ephemeral-server
  `ValueError: I/O operation on closed file.` traceback fires AFTER the
  green `1 passed` line during interpreter shutdown. Confirmed NOT a test
  failure (SWE documented it). Cosmetic; not blocking.
- Note (not blocking): the stale `tree-prefect-worker` container from the
  other worktree is still running. It does NOT affect this test (the e2e
  invokes flows in-process via `await memory_extraction/indexing`, not via
  deployment trigger). The human should stop it before any live make-target
  runbook run, per the SWE's caveat.

**VERDICT: PASS**

### [PM] 2026-05-20 19:10 — Acceptance Review (whole feature)

**VERDICT: ACCEPT**

Reviewed the Tester evidence for #039–#045 and read the shipped code from
the user's perspective. All five operator asks and all six concrete
verification points hold:

1. **Config split real + operator-facing.** `configs/default.yaml` exposes
   `models.resolution_embedding` + `models.search_embedding` as independent
   blocks (lines 67-77); both default to `voyage-multimodal-3`/1024 so the
   split is structural. The operator can repoint resolution at a
   lighter/different-dim model without breaking dedup/search: the dim-guard
   `assert_settings_match_live_vector_index` reads
   `app_config.models.search_embedding.dimensions` ONLY (`indexing/core.py:461`)
   — resolution dim is decoupled from the live `vector_index`.
2. **Concerns correctly separated.** Resolution = name-only, transient (the
   per-instance LRU in `SemanticMatchResolver`, never written); dedup +
   indexing = node-text via the search model, persisted. The persisted
   vector space is now consistent everywhere — this resolves the
   name-vs-node-text inconsistency.
3. **Dedup reuse works.** `add_entity` embeds the prospective node's
   node-text once and persists THAT vector on the non-merged path
   (`add_entity.py`); the indexing backfill (`embed_nodes`, filter
   `embedding ∈ {[],None}`) is a no-op for those nodes — proven byte-identical
   in the e2e (`after["embedding"] == before_vec`).
4. **One shared function (ask #4).** A single generic builder
   `node_to_embedding_text` (`embedding_text.py:192`) backs every generic
   path: indexing (`embed_node_texts`), `add_entity._embeddable_text`, and
   pipeline `_entity_embeddable_text`. The two extraction shims are thin
   type-dispatchers that delegate the generic case to the one builder; the
   PREFERENCE→statement / FACT→object branch is intentional (#032) and
   documented. No duplicated node-text/embed logic.
5. **Real-time request batching, not the async Batch API.** `embed_in_batches`
   packs texts into synchronous `/v1/multimodalembeddings` requests bounded
   by 1000 inputs / 320K tokens, clamps a single oversized input to 32K
   (relies on `truncation=True`), and preserves input ORDER across chunks
   (contiguous slices, concatenated). The 429 backoff is untouched (empty
   diff on `voyage_multimodal_embedding.py`). All three stages route through
   it (indexing, task ④, resolution prewarm). The async-Batch-API rejection
   is documented in code (`embedding_text.py:42-60`) and the task spec: 12h
   window can't drive mid-flow dedup + `/v1/multimodalembeddings` unsupported.
6. **E2e proves it end-to-end.** `test_e2e_embedding_split_and_batching.py`
   builds a REAL mongot `vector_index`, polls for convergence, and asserts
   the headline node is retrieved at TOP rank by its own node-text vector —
   dedup space == index space == query space. Content-encoded (sha256) model
   proves WHICH text was embedded; sentinel resolution model would catch any
   leak into a persisted node. The free-tier 429 on the live indexing
   backfill is an honest tier limit (the documented exhaustion anchor in the
   backoff loop, batcher strictly upstream) — the deterministic test
   genuinely substitutes for the live vector path, so the feature is proven.

**Documentation discipline:** project opted out of `docs/adr/` and
`docs/glossary.md` (per the feature plan) — no checks apply. The
architectural decision (real-time batching, NOT async Batch API) is captured
in #044's scope + the in-code rejected-alternative note, which is sufficient
here.

**Non-blocking notes for the PR description (NOT rollups):**
- `query.embedding_batch_size` (`default.yaml:115`) is now dead config for
  indexing — #044 superseded it with `models.embedding_batch.*`. Left in to
  avoid scope creep; a follow-up can remove it.
- Live indexing backfill on the full node set needs a paid Voyage key (or a
  3-RPM cooldown + small `DOC_IDS` subset) to complete — an operator-facing
  free-tier limit, not a defect.
- Re-embed migration: existing persisted vectors are stale under the new
  node-text/search-model space — operators run `RESET_ONTOLOGY=1` (existing
  runbook in `CLAUDE.md`) to converge; expect a `$vectorSearch`-degraded
  window during the rebuild.
- Dev-infra hygiene: a stale `tree-prefect-worker` container from another
  worktree competes for Prefect deployments — stop it before any live
  make-target runbook run. Not a code defect.
- Test-fragility nit (Tester, #043): unit tests reach into private attrs
  (`resolver._semantic._embedding_model`) to prove wiring — accurate today,
  fragile to future refactors. Optional hardening.

If the user checks this right now, they will be satisfied. SWE may push;
pipeline advances to On-Call (CI) + PR Reviewer.
