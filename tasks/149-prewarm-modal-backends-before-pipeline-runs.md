---
id: 149-prewarm-modal-backends-before-pipeline-runs
status: pending
feature: voyage-4-and-modal-embedding-catalog
---

# Pre-warm every Modal-backed model before document 1: concurrent, fail-fast with zero documents attempted, a no-op for every other provider

Tags: `modal`, `pipeline`, `prefect`, `robustness`
Depends on: #144, #148
Blocks: #141
Implements: ADR-009 — Decision 11 (pre-warm before document 1; warm at use everywhere else)

## Scope

**EXECUTION ORDER of the rework: 143 -> 144 -> 145 -> 146 -> 147 -> 148 -> 149 -> 150 -> 141.**

This task touches neither the driver nor the deploy scripts and starts no `modal` process (the feature's rule:
none outside task 141). Every test uses fake models.

**Source of the design.** READ (read-only, never write there)
`/Users/pauliusztin/Documents/01-Projects/scrabble/scrabble/src/pulse/ingest.py`, `_prewarm` (~line 370) and
its call before document 1 (~line 437). Port it; the differences below come from THIS repo's shape.

**Why this repo needs it more than pulse did.** Here a model object is built PER PREFECT TASK by the factories
(`get_llm()`, `get_search_embedding_model()`, `get_resolution_embedding_model()` — see `pipeline.py` lines
~509, ~634, ~1204, ~2071-2072, ~2124, ~2533), so #144's single-flight is per INSTANCE: N documents against a
cold server would mean N instances each running their own 600 s poll, and N failed documents if the server is
dead. Pre-warming once at the top of the run makes every later instance's first poll a single GET answering
200 — and turns a dead server into ONE failure before any document is attempted.

**A. The helper — `async def prewarm_models(*models: object) -> None` in `src/tree/models/get_model.py`**
(already imported by every seam; needs only `asyncio` — no `modal`, no `openai` import).
- DUCK-TYPED on `ensure_warm`: only `ModalEmbeddingModel` and `ModalLLM` have it, so Voyage, Gemini,
  sentence-transformers and the mock make the call a no-op that creates no task.
- DEDUPE on `warm_key` (`getattr(model, "warm_key", id(model))`): the resolution and the search embedding may
  be the SAME Modal app — warm each distinct app once.
- CONCURRENT: one `asyncio.create_task(model.ensure_warm())` per distinct model, `await asyncio.gather(*tasks)`.
- FAIL-FAST WITH EXPLICIT CANCEL: `asyncio.gather` propagates the first failure but leaves the siblings
  running — a 403 on one server must not wait out the other's 600 s. In a `finally`: `task.cancel()` for every
  task, then `await asyncio.gather(*tasks, return_exceptions=True)` so a cancelled poll is reaped inside this
  call. The FIRST exception propagates unchanged (`ModelError` for a 4xx, `ExtractionError` for a spent deadline).
- One INFO line when there is something to warm: `Pre-warming 2 Modal server(s): ep-tree-lfm2-5-350m, ep-tree-voyage-4-nano`.
- IDEMPOTENT: on a warm server each `ensure_warm` is one `/health` GET.

**B. A plain awaited helper, NOT a Prefect task — why.** (1) A task result is cached/persisted: a cached
"warm" is exactly the "warm at t0" fallacy ADR-009 §11 rejects. (2) Task retries would multiply a budget the
poller already owns (3 retries x 600 s). (3) It must raise OUTSIDE the per-document error handling, so a dead
server fails the flow run with zero documents attempted instead of counting as failed documents. A flow-level
retry re-runs it harmlessly (idempotent).

**C. The seams (read each before editing; line numbers as of `702c08a`, re-grep).**
1. **Extraction worker** — `pipeline.py::_run_extraction_worker_body`: AFTER the `if not docs: return
   WriteSummary(documents_processed=0)` guard (an empty run must never wake a GPU) and BEFORE
   `_split_documents` / the first embed. `rag` mode: `prewarm_models(get_search_embedding_model())`;
   `graphrag`: `prewarm_models(get_llm(), get_resolution_embedding_model(), get_search_embedding_model())`.
   The instances built for the pre-warm are throw-aways (the tasks build their own, as today) — what is shared
   is the warm SERVER, not the object.
2. **Dream consolidation** — `memory/graph/consolidation/dream.py` ~line 666: right after `llm = get_llm()` /
   `embedding_model = get_search_embedding_model()` (already behind the "nothing to do" guard):
   `await prewarm_models(llm, embedding_model)` — here the SAME instances are reused, so the gate's hint carries over.
3. **Clustering summaries** — `pipeline.py::_summarise_clusters`, before the fan-out, only when
   `samples_by_cluster` is non-empty: `prewarm_models(get_llm())`. ADR-007 §4 makes summaries fail OPEN, so a
   pre-warm failure here must NOT fail the run: ONE WARNING (`Cluster summaries skipped: <error>`) and every
   cluster takes the function's EXISTING fail-open fallback without an LLM call — instead of N tasks each
   waiting out 600 s.
4. **NOT pre-warmed, on purpose — say so in a comment at each site:**
   - `memory_indexing` / `_embed_nodes`: ONE model, sequential batches, and whether there is anything to
     backfill is only known inside `embed_nodes`. Warm-at-use by the gate covers it: the first `embed()` waits
     out the cold start, and a dead server fails the task before any write. A pre-warm at the top of the flow
     would wake a GPU for a no-op backfill.
   - `mcp/server.py::app_lifespan`: pre-warming at server start is WRONG for a scale-to-zero server — the
     process lives for hours, the container for 5 idle minutes — and would spend Horizon's 60 s readiness
     window on a GPU boot. The gate warms at use: the first query after an idle period waits for the cold start.
   - `scripts/query_graph.py` and every other script: same — the gate.

## Out of scope
- A keep-warm ping / `min_containers > 0` / a per-process model cache (ADR-009 "what would justify upgrading").
- Changing how the factories build instances; changing task retry / cache policies.
- A user-facing MCP message while a query waits for a cold start.
- Any live call (#141 shows the pre-warm lines in a real run only as far as its selected cycles go — a pipeline
  run with `provider: modal` stays out of scope there).

## Acceptance Criteria

- [ ] `tests/unit/models/test_prewarm.py`:
  - `test_noop_without_ensure_warm`: `prewarm_models(object(), MockEmbeddingModel(dimensions=8))` returns, creates no task (`asyncio.all_tasks()` unchanged) and logs nothing.
  - `test_warms_concurrently`: two fakes whose `ensure_warm` await a shared `asyncio.Event` -> both are entered before either finishes.
  - `test_dedupes_on_warm_key`: two fakes with `warm_key == "ep-tree-voyage-4-nano"` -> `ensure_warm` awaited once; the INFO line says `1 Modal server(s)`.
  - `test_first_failure_cancels_the_sibling`: fake A raises `ModelError("… HTTP 403 …")` at once, fake B sleeps 600 s (injected long `asyncio.sleep`) -> `prewarm_models` raises that `ModelError` in < 1 s, B observed `asyncio.CancelledError`, and no task is left pending.
  - `test_is_idempotent`: a second call on already-warm fakes awaits `ensure_warm` again and returns (the gate, not this helper, makes it cheap).
  - `test_prewarm_helper_does_not_import_modal`: importing `tree.models.get_model` leaves `"modal"` out of `sys.modules`.
- [ ] `test_worker_prewarms_before_the_first_stage` (`tests/unit/memory/test_pipeline*.py`, patched factories + a spy `prewarm_models`): graphrag -> called once with 3 models BEFORE `_split_documents`; rag -> once with the search embedding model only; ZERO documents -> not called at all.
- [ ] `test_dead_server_fails_the_run_with_zero_documents_attempted`: `prewarm_models` raising `ModelError` -> the worker body raises it, `_split_documents` / `_embed_children` / `_apply_writes` were never called, and no `WriteSummary` with failed documents is produced.
- [ ] `test_dream_prewarms_the_models_it_built`: called once with the SAME `llm` and `embedding_model` objects, and not at all on the "nothing to do" path.
- [ ] `test_cluster_summaries_fail_open_when_the_llm_is_dead`: `prewarm_models` raising -> `_summarise_clusters` returns the fail-open result for every cluster (`n_failed == len(samples_by_cluster)`), ONE WARNING containing `Cluster summaries skipped`, and `summarise_cluster_task` is never called; empty `samples_by_cluster` -> no pre-warm.
- [ ] `grep -n "prewarm_models" apps/memory/src/tree/mcp/server.py apps/memory/scripts/*.py` -> 0 hits; `grep -c "warm" apps/memory/src/tree/mcp/server.py` >= 1 (the comment that says why not).
- [ ] With the shipped defaults (Voyage + Gemini) the full unit suite shows no behaviour change: `make memory-tests` green, and `test_worker_prewarm_is_a_noop_on_default_providers` asserts no task is created.
- [ ] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green (LOCAL env).

## User Stories

### Story: Operator ingests 200 documents with both models on Modal, both scaled to zero
1. `models.llm` and `models.search_embedding` are `provider: modal`; they start an offline run.
2. The worker logs `Pre-warming 2 Modal server(s): ep-tree-lfm2-5-350m, ep-tree-voyage-4-nano`, then the two interleaved `Still cold (HTTP 503) …` series, then two `Warm: …` lines — about as long as the SLOWER boot, not the sum.
3. Document 1 starts on warm servers; every task's own first poll is a single 200.

### Story: One server is dead
1. The LLM app was stopped; the embedding server is merely cold.
2. Within seconds the run fails with `Failed to resolve Modal server ep-tree-lfm2-5-350m/Server. Is the model deployed … make memory-deploy-model MODEL=LiquidAI/LFM2.5-350M`; the embedding poll is cancelled, not waited out.
3. Zero documents were attempted; nothing is marked failed; the operator deploys and re-runs.

### Story: Resolution and search embeddings are the same Modal model
1. Both YAML blocks name `voyageai/voyage-4-nano`.
2. `Pre-warming 1 Modal server(s): ep-tree-voyage-4-nano` — one poll loop.

### Story: The default user notices nothing
1. Voyage + Gemini, as shipped. 2. No `Pre-warming` line, no extra task, no extra latency.

### Story: A query after a quiet afternoon
1. The MCP server has been up for 5 hours; the Modal embedding server scaled to zero long ago.
2. The first `search_memory` call waits for the cold start through the gate (`Warming …`, `Warm: … after 97s`) and answers; nothing was pre-warmed at server start, so nothing was billed all afternoon.

### Story: Clustering with a dead LLM
1. The LLM app is stopped; a clustering run reaches the summaries stage.
2. ONE `Cluster summaries skipped: …` WARNING; every cluster is stored as `Cluster <id>`; the run succeeds (ADR-007 §4) in seconds instead of N x 600 s.

---

Blocked by: #144, #148

## Log

### [PA] 2026-09-20 13:40 — Grooming (new task: addendum D11.3)

**Summary**
Before the first document of a run that uses Modal-backed models, warm every distinct Modal server concurrently and fail the run — with zero documents attempted — if one is dead. Everything else (indexing backfill, MCP queries, scripts) is covered by the gate's warm-at-use.

**Key decisions**
- A plain awaited helper at the top of the run, not a Prefect task: no cached "warm", no retry multiplication, raised outside per-document handling.
- Lives in `get_model.py`, duck-typed on `ensure_warm` + `warm_key`: no `modal` import on the MCP boot path, a true no-op for the shipped providers.
- Explicit sibling cancel — pulse's lesson about `asyncio.gather`.
- Seams: extraction worker (after the zero-docs guard), dream consolidation, cluster summaries (fail-OPEN per ADR-007 §4). Deliberately NOT: indexing backfill, MCP `app_lifespan`, scripts — each with a comment saying why.
- This repo builds a model per task, so the pre-warm shares the warm SERVER, not the instance; the per-instance first-use cost (one `get_url`, one 200 poll, one `/v1/models`) is accepted and named in ADR-009's Consequences.

**Dependencies**
- #144 (`ensure_warm` / `warm_key` on the embedding client), #148 (`ModalLLM` — the pre-warm must warm both).

**User stories**
- 6 stories: two cold servers, one dead server, same app twice, default providers, a query after idle, clustering fail-open.

**Open questions**
- None blocking.

Ready for implementation.
