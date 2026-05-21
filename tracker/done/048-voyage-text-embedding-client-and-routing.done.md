# Voyage TEXT embedding client + model-id routing + voyage-3.5 YAML default

Status: pending
Tags: `models`, `embedding`, `config`
Depends on: None
Blocks: #050, #052, #053

## Scope

Re-introduce a dedicated Voyage **text** embedding client and route the
`voyage-3` model family to it, then flip the YAML default to a text model
(`voyage-3.5`, 1024-d). Part A of the feature. This is a *partial* revert of
#038's client consolidation: we keep BOTH the multimodal client (for
`voyage-multimodal-*`) and re-add a text client (for everything else).

Reference implementation: the deleted `VoyageEmbeddingModel` /
`voyage_embedding.py` exists in git history at commit `bc89a28`
(`git show bc89a28:apps/memory/src/tree/models/get_model.py` shows the
pre-#038 routing; the client file was deleted in #038). Resurrect the client
but DO NOT resurrect the rate-limit/backoff loop as a separate copy — fold the
proven resilience that landed since into the new client, matching the current
`VoyageMultimodalEmbeddingModel` structure.

### New client: `VoyageTextEmbeddingModel`

- New file `apps/memory/src/tree/models/voyage_embedding.py`.
- Implements `BaseEmbeddingModel` (`apps/memory/src/tree/models/base.py`):
  `dimensions` property + `async def embed(self, texts: list[str]) -> list[list[float]]`.
- Calls `POST https://api.voyageai.com/v1/embeddings` (NOT
  `/v1/multimodalembeddings`). Docs: https://docs.voyageai.com/docs/embeddings ;
  API ref: https://docs.voyageai.com/reference/embeddings-api .
- **Text payload shape**: `{"input": [<texts>], "model": <model>, ...}` — the
  flat `input` list of strings. This is DIFFERENT from the multimodal client's
  `{"inputs": [{"content": [{"type": "text", "text": t}]}], ...}` shape. Getting
  this wrong is the headline bug this task fixes (multimodal endpoint 400'd on
  `voyage-3` per `bc89a28`, and 400'd on scraped content in #047).
- Constructor mirrors `VoyageMultimodalEmbeddingModel.__init__`: `api_key`
  (raise `ModelError` "Voyage API key is required." when empty), `model`
  (default `voyage-3.5`), optional `input_type` (`"query"`/`"document"`),
  optional `output_dimension` (Matryoshka truncation), `truncation: bool = True`,
  `timeout: float = 120.0`, and `rate_limit_backoff_seconds` with the same
  default schedule.
- **Resilience parity (carry forward, do NOT regress):**
  - The HTTP-429 exponential-backoff loop inside `embed`, identical in behavior
    to the multimodal client (retries 429 per `rate_limit_backoff_seconds`;
    fails fast on every other non-200; raises `ExtractionError` with the literal
    anchor `"rate-limit retries exhausted"` when the schedule is exhausted).
  - The structured `ExtractionError.status_code` discriminator
    (`apps/memory/src/tree/models/exceptions.py`): a content-rejection 400 must
    raise `ExtractionError(..., status_code=400)`; a 429 must raise/loop with
    `status_code=429`. This is the contract `_embed_chunk_resilient` keys off.
- `dimensions` property: return `output_dimension` when set; otherwise a native
  default for the configured model id from a `_MODEL_NATIVE_DIMENSIONS` map.
  Seed at least `voyage-3.5: 1024`, `voyage-3: 1024`, `voyage-3-lite: 512`,
  `voyage-code-3: 1024` (source: https://docs.voyageai.com/docs/embeddings —
  keep in lockstep with the docs). Raise `ModelError` for an unknown id with no
  explicit `output_dimension`, matching the multimodal client's message style.

### NOTE on the batching / sanitization / skip-and-continue layer

`embed_in_batches`, `_sanitize_for_embedding`, and `_embed_chunk_resilient` do
**NOT** live inside the embedding client — they live at
`apps/memory/src/tree/memory/embedding_text.py` and wrap ANY
`BaseEmbeddingModel`. The feature spec frames them as "resilience that landed
on the client"; in this codebase they are call-site wrappers. The new text
client therefore inherits all of #044 batching + #047 sanitization + bisect
skip-and-continue **for free**, the moment it implements `embed()` + the
`status_code` discriminator correctly. Do NOT duplicate that layer into the
client. The one client-side obligation is the `status_code` contract above so
`_embed_chunk_resilient`'s 429-vs-400 branch works.

### Routing: `get_model.py::_build_embedding_model`

- File `apps/memory/src/tree/models/get_model.py`, the `provider == "voyage"`
  branch.
- Branch on the model id:
  - `cfg.model` starts with `voyage-multimodal` ⇒ `VoyageMultimodalEmbeddingModel`
    (unchanged construction: `api_key`, `model`, `output_dimension=cfg.dimensions`).
  - everything else (`voyage-3`, `voyage-3.5`, `voyage-3-lite`, `voyage-code-3`,
    …) ⇒ `VoyageTextEmbeddingModel(api_key=..., model=cfg.model,
    output_dimension=cfg.dimensions)`.
- This mirrors the pre-#038 routing visible in `bc89a28` but keeps both clients.

### YAML default flip

- `apps/memory/configs/default.yaml`: set BOTH `models.resolution_embedding`
  and `models.search_embedding` to `provider: voyage`, `model: voyage-3.5`,
  `dimensions: 1024`. Update the surrounding comments (they currently say
  `voyage-multimodal-3`).
- Pydantic defaults in `apps/memory/src/tree/config/app_config.py`
  (`EmbeddingConfig.model` default is `"voyage-multimodal-3"`) — flip to
  `"voyage-3.5"` so code-level defaults track the YAML.
- 1024-d is unchanged ⇒ NO vector-index `numDimensions` change ⇒ the dim-guard
  `assert_settings_match_live_vector_index` stays satisfied. Do NOT touch the
  mongot index in this task.

## Acceptance Criteria

- [x] New file `apps/memory/src/tree/models/voyage_embedding.py` defines
      `VoyageTextEmbeddingModel(BaseEmbeddingModel)`.
- [x] `embed(["hello"])` builds the payload `{"input": ["hello"], "model":
      "voyage-3.5", ...}` and POSTs to `https://api.voyageai.com/v1/embeddings`
      (verify via a mocked `aiohttp` session in a unit test asserting URL + body).
      — `test_voyage_embedding.py::TestVoyageTextEmbed::test_embed_payload_uses_text_endpoint_shape`
- [x] A mocked HTTP 400 response makes `embed` raise `ExtractionError` with
      `status_code == 400`. — `test_embed_400_raises_with_status_code`
- [x] A mocked HTTP 429 is retried per `rate_limit_backoff_seconds`; when the
      schedule is exhausted `embed` raises `ExtractionError` whose message
      contains `"rate-limit retries exhausted"` and `status_code == 429`.
      — `test_embed_retries_on_429_then_succeeds` + `test_embed_raises_when_429_backoff_exhausted`
- [x] `dimensions` returns `output_dimension` when set, else the native default
      for the model id; unknown id without `output_dimension` raises `ModelError`.
      — `TestVoyageTextDimensions`
- [x] Empty `api_key` raises `ModelError` containing `"Voyage API key is required"`.
      — `TestVoyageTextInit::test_raises_on_empty_api_key`
- [x] `embed([])` returns `[]` without an HTTP call.
      — `test_embed_empty_input_makes_no_http_call`
- [x] `_build_embedding_model` returns `VoyageTextEmbeddingModel` for
      `model="voyage-3.5"` (and `voyage-3`, `voyage-3-lite`, `voyage-code-3`)
      and `VoyageMultimodalEmbeddingModel` for `model="voyage-multimodal-3"`
      (parametrized unit test over the routing branch).
      — `test_get_model.py::TestVoyageModelIdRouting` (both directions, parametrized)
- [x] `apps/memory/configs/default.yaml` `models.resolution_embedding` and
      `models.search_embedding` both read `voyage-3.5` / `1024`; comments updated.
- [x] `EmbeddingConfig` Pydantic default model is `voyage-3.5`.
- [x] `embed_in_batches(... , VoyageTextEmbeddingModel(...))` works unchanged: a
      mocked-400 single input is skipped to `[]` (bisect skip-and-continue) and a
      mocked-429 propagates as a retry — confirming the
      `embedding_text.py` resilience layer composes with the new client. (No new
      code in `embedding_text.py`; this is a composition test.)
      — `test_voyage_embedding.py::TestVoyageTextComposesWithEmbeddingTextResilience`
- [x] `make memory-format-check && make memory-lint-check && make memory-unit-tests`
      pass; `make memory-integration-tests` (fast loop) shows no regressions.
      (See Notes — the Voyage-path integration tests pass in isolation; the
      noisy concurrent-run failures are shared-infra contention, not this change.)
- [x] [HUMAN] One live smoke call against the real Voyage `/v1/embeddings`
      endpoint with `voyage-3.5` returns a 1024-d vector (free-tier 3 RPM — keep
      it to a single 1-2 text call). Captured in the Tester log if a key is
      available; otherwise marked `NOT RUN — no live VOYAGE_API_KEY`.
      — Tester ran a live `embed(["hello world"])` via `get_search_embedding_model()`
      (VOYAGE_API_KEY from worktree .env); returned exactly one 1024-d vector
      (first floats `[-0.0093, 0.0701, -0.0145, 0.0742, 0.0337]`). See Tester log.

## User Stories

### Story: Developer configures voyage-3.5 and ingestion routes to the text endpoint
1. Developer leaves `apps/memory/configs/default.yaml` at the new default
   (`models.search_embedding.model: voyage-3.5`).
2. Developer constructs the search model via `get_search_embedding_model()`.
3. The returned object is a `VoyageTextEmbeddingModel`, not a multimodal client.
4. Calling `embed(["Paul Iusztin"])` issues a POST to
   `https://api.voyageai.com/v1/embeddings` with body `{"input": ["Paul
   Iusztin"], "model": "voyage-3.5", ...}` and returns one 1024-float vector.

### Story: Developer keeps a multimodal block and it still routes to multimodal
1. Developer sets an embedding block's `model` to `voyage-multimodal-3`.
2. `_build_embedding_model` returns `VoyageMultimodalEmbeddingModel`.
3. `embed(["x"])` POSTs to `/v1/multimodalembeddings` with the nested
   `{"inputs": [{"content": [...]}]}` shape — proving both clients coexist.

### Story: A poison chunk is skipped, a rate limit is ridden out
1. A batch of node texts contains one chunk the endpoint rejects with HTTP 400.
2. `embed_in_batches` over the text client bisects and skips only the bad input
   (aligned `[]` placeholder), embedding the rest.
3. A separate batch hits HTTP 429; the client's backoff loop retries rather than
   dropping data, and the batch eventually succeeds (or surfaces
   `"rate-limit retries exhausted"` if the schedule runs out) — never silently
   drops a vector.

### Story: Operator boots the pipeline after the default flip without a dim error
1. Operator pulls the branch (dimensions unchanged at 1024).
2. Operator runs the data/extraction pipeline.
3. `assert_settings_match_live_vector_index` does NOT raise — the dim-guard is
   satisfied because `numDimensions` is still 1024. (Stale-vector handling is
   Task #049's runbook, out of scope here.)

---

Blocked by: (none)

## Log

### [SWE] 2026-05-20 23:20 — Implementation

**Files modified**
- `apps/memory/src/tree/models/voyage_embedding.py` — NEW. Resurrected the Voyage
  TEXT client as `VoyageTextEmbeddingModel(BaseEmbeddingModel)` (from `bc89a28`),
  POSTing the flat `{"input": [...], "model": ...}` shape to
  `https://api.voyageai.com/v1/embeddings`. Mirrors the multimodal client's 429
  exponential-backoff loop. KEY DIFF vs. the deleted client: every error raise
  (429 exhaustion AND non-200) now passes `status_code=resp.status` so the shared
  `_embed_chunk_resilient` 400-vs-429 discriminator works. Default model `voyage-3.5`.
- `apps/memory/src/tree/models/get_model.py` — restored model-id routing in the
  `provider == "voyage"` branch of `_build_embedding_model`: `voyage-multimodal*`
  → `VoyageMultimodalEmbeddingModel`; everything else → `VoyageTextEmbeddingModel`.
  Both clients now imported and reachable.
- `apps/memory/configs/default.yaml` — flipped `models.resolution_embedding` and
  `models.search_embedding` from `voyage-multimodal-3` to `voyage-3.5` (dims 1024
  unchanged); updated comments.
- `apps/memory/src/tree/config/app_config.py` — `EmbeddingConfig.model` default
  `voyage-multimodal-3` → `voyage-3.5` so code-level defaults track YAML.
- `apps/memory/tests/unit/models/test_voyage_embedding.py` — NEW. Payload/URL/dim,
  status_code 400/429 discriminator, 429 backoff (retry + exhaustion), empty-input
  no-HTTP-call, and a `TestVoyageTextComposesWithEmbeddingTextResilience` class
  proving the real client composes through `embed_in_batches` (skip-on-400 →
  aligned `[]`, 429 re-raises).
- `apps/memory/tests/unit/models/test_get_model.py` — replaced the stale
  multimodal-only routing test; added `TestVoyageModelIdRouting` (parametrized,
  both directions) and `test_returns_voyage_text_for_text_model`.
- `apps/memory/tests/unit/config/test_app_config.py` — updated the two assertions
  that pinned the old `voyage-multimodal-3` default to `voyage-3.5`.

**Tests**
- Unit: 1288 passing, 0 failing — `make memory-unit-tests`.
- Integration (Voyage path, run sequentially in isolation): `tests/integration/memory`
  41 passing; `test_indexing_pipeline.py` + `test_extraction_pipeline.py` 9 passing —
  including `test_text_index_created` / `test_structural_edges_created` that flaked in
  the concurrent run.

**Evidence**
```
$ make memory-unit-tests
... tests/unit/models/test_voyage_embedding.py .......................   [ 97%]
... tests/unit/models/test_get_model.py ......................           [ 93%]
============================ 1288 passed in 44.12s =============================

$ make memory-format-check && make memory-lint-check
246 files already formatted
All checks passed!

$ make pre-commit
ruff check ... Passed | ruff format ... Passed | KGQuery discipline (memory) ... Passed

# e2e runtime check (real default.yaml, mocked aiohttp):
YAML search model: voyage-3.5 dims: 1024
search getter -> VoyageTextEmbeddingModel dims 1024
resolution getter -> VoyageTextEmbeddingModel dims 1024
voyage-multimodal-3 routes to -> VoyageMultimodalEmbeddingModel
POST url: https://api.voyageai.com/v1/embeddings
payload: {'model': 'voyage-3.5', 'input': ['Paul Iusztin'], 'truncation': True}
returned vector len: 1024
E2E OK

# YAML / both-clients-reachable grep evidence:
$ grep -n "model:" apps/memory/configs/default.yaml | grep voyage
69:    model: voyage-3.5      # resolution_embedding
76:    model: voyage-3.5      # search_embedding
$ grep -n "Voyage.*EmbeddingModel" apps/memory/src/tree/models/get_model.py
  (imports + routing: VoyageMultimodalEmbeddingModel for voyage-multimodal*,
   VoyageTextEmbeddingModel otherwise)

# Voyage-path integration tests in isolation (sequential, no concurrent worktree):
$ uv run pytest tests/integration/memory/test_indexing_pipeline.py \
      tests/integration/memory/test_extraction_pipeline.py -m "not requires_mongot and not slow" -p no:xdist -q
9 passed, 6 deselected in 17.11s
$ uv run pytest tests/integration/memory -m "not requires_mongot and not slow" -p no:xdist -q
41 passed, 47 deselected in 18.47s
```

**Notes (for the Tester)**
- DIMENSION UNCHANGED: voyage-3.5 is 1024-d, identical to voyage-multimodal-3, so
  the mongot `vector_index` `numDimensions` is untouched and
  `assert_settings_match_live_vector_index` stays satisfied — the dim-guard will
  NOT flag the vector-space change. Handling stale multimodal-era vectors after
  switching embedding *space* is #049's migration runbook, explicitly out of scope
  here. I did NOT touch the mongot index.
- The text payload shape is `{"input": [...], "model": ...}` (flat list of
  strings), NOT the multimodal `{"inputs": [{"content": [...]}]}`. This is the
  headline bug fixed.
- NO new code in `embedding_text.py` — the batching/sanitize/skip-and-continue
  layer wraps any `BaseEmbeddingModel`; the text client inherits it for free via
  the `status_code` contract. The composition AC is covered by a test that runs
  the real client through `embed_in_batches`.
- `make memory-integration-tests` (full fast loop) showed flaky failures that
  DIFFERED between two runs (web/serp/scrape live-network, dedup, validator
  index-presence) — caused by a sibling worktree concurrently using the shared
  `tree-mongodb`/`tree-mongot` containers, which races the autouse
  `_clean_collections` fixture (documented in CLAUDE.md). The Voyage-path tests
  pass cleanly when run sequentially in isolation (above). Tester should run the
  acceptance gate on a quiesced stack (no other worktree active).
- [HUMAN] live `/v1/embeddings` smoke (1024-d via voyage-3.5) NOT RUN by SWE —
  left for the Tester per the AC (free-tier 3 RPM; single 1-2 text call).
- DID NOT COMMIT — Tester goes first. Tracker renamed to `.in-progress.md`.

### [Tester] 2026-05-20 23:55 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`246 files already formatted`; `All checks
  passed!`; pre-commit `ruff check / ruff format / prettier / biome / KGQuery
  discipline` all Passed).
- Unit tests: 1288 passed / 0 failed (`make memory-unit-tests`, 40.49s), 0 warnings.
- Integration tests: 222 passed / 1 skipped / 0 failed (`make
  memory-integration-tests-all`, 498.10s) — **clean in a single run**, including
  every Voyage/embedding/indexing test (`test_dedup_node_text_embedding`,
  `test_e2e_embedding_split_and_batching`, `test_embedding_batching`,
  `test_extraction_pipeline`, `test_indexing_pipeline`). No flakes observed. The
  one skip is `test_web_search_ingest` (network-gated `s`), unrelated to #048.
- Infra note: a leftover `tree.orchestrator` serve-workflows worker (PID 25511)
  was running and the sibling worktree (on `main`) shares the docker stack. It is
  a passive Prefect worker — it does not write collections unless a deployment is
  triggered — so it did NOT race `_clean_collections`; the full suite passed
  green on the first run. No shared-stack contention surfaced this run.

**E2E adversarial pass**
- Happy path (live, the [HUMAN] AC): `get_search_embedding_model().embed(["hello
  world"])` against the REAL Voyage `/v1/embeddings` → returned 1 vector of dim
  1024 (`VoyageTextEmbeddingModel`, model `voyage-3.5`); first floats
  `[-0.0093, 0.0701, -0.0145, 0.0742, 0.0337]`. (PASS) — proves the headline
  regression is fixed: a `voyage-3` family id now hits the text endpoint and
  returns a real vector instead of 400ing against the multimodal endpoint.
- Break path 1 (routing crux, both directions, against REAL default.yaml via
  `_build_embedding_model`): `voyage-3.5 / voyage-3 / voyage-code-3 /
  voyage-3-lite` → `VoyageTextEmbeddingModel`; `voyage-multimodal-3 /
  voyage-multimodal-3.5` → `VoyageMultimodalEmbeddingModel`. No cross-wiring in
  either direction. (PASS)
- Break path 2 (payload shape): text client POSTs `{"model":"voyage-3.5",
  "input":["Paul Iusztin"], "truncation":true, "output_dimension":1024}` to
  `https://api.voyageai.com/v1/embeddings` — FLAT `input` list, `"inputs"` and
  nested `content` blocks absent. (PASS)
- Break path 3 (status_code discriminator end-to-end through
  `embed_in_batches`/`_embed_chunk_resilient`): mocked HTTP 400 →
  `status_code=400` → bisected & skipped to aligned `[]`
  (`[[0.1,0.2],[],[0.1,0.2]]`, skippable); mocked HTTP 429 → `status_code=429`
  → propagated with `"rate-limit retries exhausted"` (re-raised, never dropped).
  (PASS)
- Break path 4 (boundary inputs): `embed([])` → `[]` with no HTTP call;
  empty-string + Unicode (`café ☃ 日本語`) + 50k-char input all pass through
  verbatim into the flat `input` list, 3 vectors returned. (PASS)
- Break path 5 (no #038 regression): multimodal client still POSTs the nested
  `{"inputs":[{"content":[{"type":"text","text":"x"}]}]}` shape to
  `/v1/multimodalembeddings`; both clients importable and reachable in
  `get_model.py`. (PASS)

**Acceptance criteria**
- [x] PASS — `VoyageTextEmbeddingModel(BaseEmbeddingModel)` in new file
      `apps/memory/src/tree/models/voyage_embedding.py:86`.
- [x] PASS — flat `{"input":[...],"model":...}` body to `/v1/embeddings` —
      `test_voyage_embedding.py::TestVoyageTextEmbed::test_embed_payload_uses_text_endpoint_shape`
      + live adversarial payload capture.
- [x] PASS — HTTP 400 → `ExtractionError(status_code==400)` —
      `test_embed_400_raises_with_status_code` + adversarial D1.
- [x] PASS — HTTP 429 retried per schedule, exhaustion raises
      `"rate-limit retries exhausted"` w/ `status_code==429` —
      `test_embed_retries_on_429_then_succeeds` +
      `test_embed_raises_when_429_backoff_exhausted` + adversarial D2.
- [x] PASS — `dimensions` returns `output_dimension` else native map; unknown id
      raises `ModelError` — `TestVoyageTextDimensions` (6 cases).
- [x] PASS — empty `api_key` raises `ModelError` "Voyage API key is required" —
      `TestVoyageTextInit::test_raises_on_empty_api_key`.
- [x] PASS — `embed([])` → `[]`, no HTTP — `test_embed_empty_input_makes_no_http_call`
      + adversarial boundary check.
- [x] PASS — routing both directions — `test_get_model.py::TestVoyageModelIdRouting`
      (6 parametrized) + live `_build_embedding_model` adversarial check.
- [x] PASS — `default.yaml` `resolution_embedding`/`search_embedding` =
      `voyage-3.5`/`1024`, comments updated — `configs/default.yaml:67-79`;
      loaded config confirms `voyage-3.5 1024` for both.
- [x] PASS — `EmbeddingConfig` default model `voyage-3.5` — `app_config.py:50`
      (single-line diff) + `test_app_config.py`.
- [x] PASS — composition with `embedding_text.py` resilience (no new code there)
      — `TestVoyageTextComposesWithEmbeddingTextResilience` + adversarial D1/D2.
- [x] PASS — format/lint/unit clean; integration no regressions (full
      `-all` suite green, see above).
- [x] PASS — [HUMAN] live `/v1/embeddings` smoke returns 1024-d via voyage-3.5
      (see Happy path above).

**Evidence**
```
$ make memory-unit-tests
============================ 1288 passed in 40.49s =============================
$ make memory-integration-tests-all
================== 222 passed, 1 skipped in 498.10s (0:08:18) ==================
$ make memory-format-check && make memory-lint-check
246 files already formatted | All checks passed!
$ make pre-commit
ruff check ... Passed | ruff format ... Passed | KGQuery discipline ... Passed

# live smoke (real /v1/embeddings, voyage-3.5)
LIVE SMOKE OK: returned 1 vector of dim 1024
first 5 floats: [-0.009320608, 0.070138641, -0.01447035, 0.074224383, 0.033707403]
```

**Dim / index churn check (verification D)**
- voyage-3.5 is 1024-d (loaded config + live vector both 1024). The diff touches
  NO mongot vector_index, `indexing/core.py`, or `assert_settings_match_live_vector_index`
  — confirmed via `git diff --name-only`. Dim-guard stays satisfied; vector-space
  staleness correctly deferred to #049. #048 did NOT silently try to handle it.

**Other issues found**
- None blocking. Diff is tightly scoped (5 code/test files + the tracker/plan
  files); no stray `git add -A`, no `print()` in library code, full type
  annotations present, both clients import cleanly. The text and multimodal
  clients are near-identical (backoff loop + embed structure duplicated); the
  task explicitly accepts this to keep the two endpoints decoupled — noted as a
  future-refactor candidate, not a blocker.

**VERDICT: PASS**
