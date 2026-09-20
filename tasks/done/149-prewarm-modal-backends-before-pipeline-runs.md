---
id: 149-prewarm-modal-backends-before-pipeline-runs
status: done
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

- [x] `tests/unit/models/test_prewarm.py`:
  - `test_noop_without_ensure_warm`: `prewarm_models(object(), MockEmbeddingModel(dimensions=8))` returns, creates no task (`asyncio.all_tasks()` unchanged) and logs nothing.
  - `test_warms_concurrently`: two fakes whose `ensure_warm` await a shared `asyncio.Event` -> both are entered before either finishes.
  - `test_dedupes_on_warm_key`: two fakes with `warm_key == "ep-tree-voyage-4-nano"` -> `ensure_warm` awaited once; the INFO line says `1 Modal server(s)`.
  - `test_first_failure_cancels_the_sibling`: fake A raises `ModelError("… HTTP 403 …")` at once, fake B sleeps 600 s (injected long `asyncio.sleep`) -> `prewarm_models` raises that `ModelError` in < 1 s, B observed `asyncio.CancelledError`, and no task is left pending.
  - `test_is_idempotent`: a second call on already-warm fakes awaits `ensure_warm` again and returns (the gate, not this helper, makes it cheap).
  - `test_prewarm_helper_does_not_import_modal`: importing `tree.models.get_model` leaves `"modal"` out of `sys.modules`.
- [x] `test_worker_prewarms_before_the_first_stage` (`tests/unit/memory/test_pipeline*.py`, patched factories + a spy `prewarm_models`): graphrag -> called once with 3 models BEFORE `_split_documents`; rag -> once with the search embedding model only; ZERO documents -> not called at all.
- [x] `test_dead_server_fails_the_run_with_zero_documents_attempted`: `prewarm_models` raising `ModelError` -> the worker body raises it, `_split_documents` / `_embed_children` / `_apply_writes` were never called, and no `WriteSummary` with failed documents is produced.
- [x] `test_dream_prewarms_the_models_it_built`: called once with the SAME `llm` and `embedding_model` objects, and not at all on the "nothing to do" path.
- [x] `test_cluster_summaries_fail_open_when_the_llm_is_dead`: `prewarm_models` raising -> `_summarise_clusters` returns the fail-open result for every cluster (`n_failed == len(samples_by_cluster)`), ONE WARNING containing `Cluster summaries skipped`, and `summarise_cluster_task` is never called; empty `samples_by_cluster` -> no pre-warm.
- [x] `grep -n "prewarm_models" apps/memory/src/tree/mcp/server.py apps/memory/scripts/*.py` -> 0 hits; `grep -c "warm" apps/memory/src/tree/mcp/server.py` >= 1 (the comment that says why not).
- [x] With the shipped defaults (Voyage + Gemini) the full unit suite shows no behaviour change: `make memory-tests` green, and `test_worker_prewarm_is_a_noop_on_default_providers` asserts no task is created.
- [x] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green (LOCAL env).

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

### [SWE] 2026-09-20 18:51 — Implementation

**Files modified**
- `apps/memory/src/tree/models/get_model.py` — the **Pre-warm** helper `prewarm_models(*models)`: duck-typed on `ensure_warm`, deduped on `warm_key`, concurrent, fail-fast with an explicit sibling cancel + reap in `finally`. Only new import is stdlib `asyncio` (+ `collections.abc` types) — no `modal`, no `openai`.
- `apps/memory/src/tree/memory/pipeline.py` — seam ① extraction worker (`_run_extraction_worker_body`, right after the `if not docs` guard, before `_split_documents`; `rag` → search model only, `graphrag` → llm + resolution + search); seam ③ `_summarise_clusters` (before the fan-out, only when `samples_by_cluster` is non-empty, fail-OPEN per ADR-007 §4); the "NOT pre-warmed, on purpose" comment at `_embed_nodes`; `from tree.models.exceptions import ModelError`.
- `apps/memory/src/tree/memory/graph/consolidation/dream.py` — seam ② `_supersession_sweep`: `await prewarm_models(llm, embedding_model)` right after both are built (behind the dry-run + "no delta" guards), so the SAME instances carry the gate's warm hint into the sweep.
- `apps/memory/src/tree/mcp/server.py`, `apps/memory/scripts/query_graph.py` — comments saying why neither pre-warms (a process that lives for hours must not boot a GPU that idles out in 5 minutes; a one-query script is already covered by warm-at-use). Neither file names the helper, so the AC's `grep -n "prewarm_models"` stays at 0 hits; `grep -c "warm" …/mcp/server.py` = 3.
- `apps/memory/tests/unit/models/test_prewarm.py` (new, 8 tests) — the helper: no-op, concurrency, dedupe + its INFO line, the 2-server INFO line, sibling cancellation, idempotence, the shipped-default factories having no gate, and a fresh-interpreter probe that `tree.models.get_model` leaves `modal` / `modal_embedding` / `modal_llm` out of `sys.modules`.
- `apps/memory/tests/unit/memory/test_pipeline.py` — `TestWorkerPrewarm` (5) + `TestSummariseClustersPrewarm` (3). Also ONE pre-existing-double fix: `TestFlowEmbeddingModelSplit`'s `get_llm` stub was a bare `MagicMock()`, which answers to `ensure_warm` and so looked like a Modal client to the pre-warm — now `MagicMock(spec=BaseLLM)`, matching the embedding doubles beside it.
- `apps/memory/tests/unit/memory/graph/consolidation/test_dream.py` — 2 tests on the sweep seam (warms the two instances it built, before the resolver; never warms on the "no delta" path).

**Tests**
- Unit: 3789 passing, 0 failing (`make memory-tests`, LOCAL env) — 18 of them new.
- Integration: N/A — this repo has no integration suite (AGENTS.md); no infra touched.

**Acceptance criteria**
- [x] `tests/unit/models/test_prewarm.py` — all six named tests exist verbatim (`test_noop_without_ensure_warm`, `test_warms_concurrently`, `test_dedupes_on_warm_key`, `test_first_failure_cancels_the_sibling`, `test_is_idempotent`, `test_prewarm_helper_does_not_import_modal`), plus `test_logs_every_distinct_server_it_warms` and `test_default_providers_have_no_gate_to_warm`.
- [x] worker pre-warms before the first stage — `tests/unit/memory/test_pipeline.py::TestWorkerPrewarm::test_worker_prewarms_before_the_first_stage` (graphrag, 3 models, before `_split_documents`) plus `::test_rag_warms_the_search_embedding_model_only` and `::test_zero_documents_never_wakes_a_gpu` — the AC's single test split into three, one claim each.
- [x] dead server, zero documents attempted — `::TestWorkerPrewarm::test_dead_server_fails_the_run_with_zero_documents_attempted`.
- [x] dream pre-warms the models it built — `tests/unit/memory/graph/consolidation/test_dream.py::TestSupersessionSweep::test_dream_prewarms_the_models_it_built` + `::test_no_delta_never_prewarms`.
- [x] cluster summaries fail open on a dead LLM — `::TestSummariseClustersPrewarm::test_cluster_summaries_fail_open_when_the_llm_is_dead` (+ `::test_no_clusters_never_wakes_a_gpu`, `::test_the_llm_is_prewarmed_once_before_the_fan_out`).
- [x] the grep ACs — verified below.
- [x] no behaviour change on the shipped defaults — `::TestWorkerPrewarm::test_worker_prewarm_is_a_noop_on_default_providers` runs the REAL helper inside a full rag worker run and asserts the task set is unchanged across the call.
- [x] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green (LOCAL env).

**Evidence**
```
$ make memory-tests
============================ 3789 passed in 49.17s =============================

$ make memory-format-check && make memory-lint-check
312 files already formatted
All checks passed!

$ make pre-commit
prettier / ruff check / ruff format / biome check (harness) ......... Passed

$ grep -n "prewarm_models" apps/memory/src/tree/mcp/server.py apps/memory/scripts/*.py; echo "exit=$?"
exit=1                    # 0 hits
$ grep -c "warm" apps/memory/src/tree/mcp/server.py
3
```

End-to-end (Step 7) — no pipeline run, no `modal` process, no `*.modal.run` request (the feature's rule: none outside #141). The helper was driven in-process by a throwaway script with Modal-SHAPED stand-ins (`warm_key` + an `ensure_warm` that sleeps / raises), through `uv run python`:
```
--- 1. two Modal servers, cold: concurrent, ~the slower boot ---
Pre-warming 2 Modal server(s): ep-tree-lfm2-5-350m, ep-tree-voyage-4-nano
Warming ep-tree-lfm2-5-350m — polling for up to 600s
Warming ep-tree-voyage-4-nano — polling for up to 600s
Warm: ep-tree-voyage-4-nano answered HTTP 200 after 0s
Warm: ep-tree-lfm2-5-350m answered HTTP 200 after 0s
both warm in 0.30s (sum of boots would be 0.40s)
--- 2. same app twice (resolution + search): ONE boot ---
Pre-warming 1 Modal server(s): ep-tree-voyage-4-nano
--- 3. one dead server: fail fast, sibling CANCELLED ---
Pre-warming 2 Modal server(s): ep-tree-lfm2-5-350m, ep-tree-voyage-4-nano
Warming ep-tree-voyage-4-nano — polling for up to 600s
CANCELLED mid-poll: ep-tree-voyage-4-nano
run failed after 0.00s with: Health poll of ep-tree-lfm2-5-350m/health returned HTTP 403 …
tasks still pending: 0
--- 4. shipped defaults (Voyage + Gemini): silence ---
no 'Pre-warming' line above = no task, no latency
```

**Notes**
- **Prefect (constraint 4) is satisfied structurally**: all three seams (`_run_extraction_worker_body`, `_supersession_sweep`, `_summarise_clusters`) are plain async functions, NOT `task(...)` objects — nothing to cache, nothing to retry, and the raise happens outside per-document handling. `prewarm_models` itself is a plain awaited helper.
- **Cluster-summary catch is `ModelError`, not `Exception`** (ruff BLE001 would flag the latter anyway): `ExtractionError` subclasses `ModelError`, and the whole warm path (`resolve_server_url` → `poll_health` → `served_model_id` → client construction) raises only those two. `get_llm()` sits OUTSIDE the `try` on purpose — a missing **Proxy token** is a configuration error and must fail the run loudly; only the WARM fails open.
- **One behaviour delta on the default path worth a Tester eye**: in `rag` mode the worker now calls `get_search_embedding_model()` once at the top (it previously built it only inside the embed task). For Voyage that is object construction only — no network, no task (proved by `test_worker_prewarm_is_a_noop_on_default_providers`) — but a missing `VOYAGE_API_KEY` would now surface a few milliseconds earlier in the run.
- **Test-double contract**: duck-typing means an UNSPEC'd `MagicMock` looks like a Modal client. One pre-existing stub had to be spec'd (see Files modified); any new double for a model should carry `spec=BaseLLM` / `spec=BaseEmbeddingModel`.
- **Seam completeness checked by grep, not by assumption**: `src/tree/offline.py` and `src/tree/online.py` build NO model at all (0 hits for `get_llm` / `get_*_embedding_model`) — they dispatch `memory_extract_etl_worker` / `memory_indexing` and inherit the worker's seam, so there was nothing to add there. `dream.py` builds models at exactly ONE call site (lines 670-671, inside `_supersession_sweep`).
- Not committed — the Tester goes first. `docs/adrs/009`, `docs/glossary.md`, `tasks/141`, `tasks/150` untouched.

### [Tester] 2026-09-20 — QA

**Commands run (LOCAL env, one at a time, no `modal`/pipeline invocation)**
```
make env-status                              # -> "Env target: local (.env)"
make memory-format-check
make memory-lint-check
make pre-commit
make memory-tests                            # 3789 passed in 50.72s (re-run once for the warning grep, same result)
grep -n "prewarm_models" apps/memory/src/tree/mcp/server.py apps/memory/scripts/*.py   # exit 1 / 0 hits
grep -c "warm" apps/memory/src/tree/mcp/server.py                                       # 3
grep -rn "prewarm_models" apps/memory/src apps/memory/scripts                           # helper + 3 call sites only
cd apps/memory && uv run --group dev pytest tests/unit/models/test_prewarm.py -q                                            # 8 passed in 2.25s
cd apps/memory && uv run --group dev pytest tests/unit/memory/test_pipeline.py -k "TestWorkerPrewarm or TestSummariseClustersPrewarm" -q   # 8 passed in 1.63s
cd apps/memory && uv run --group dev pytest tests/unit/memory/graph/consolidation/test_dream.py -k "prewarm or no_delta_never" -q          # 2 passed in 1.08s
cd apps/memory && uv run python -c "import sys, tree.memory.pipeline; print([m for m in ('modal','tree.models.modal_embedding','tree.models.modal_llm') if m in sys.modules])"   # []
cd apps/memory && uv run python -c "import sys, tree.mcp.server; print([m for m in ('modal','tree.models.modal_embedding','tree.models.modal_llm') if m in sys.modules])"        # []
uv run python <scratch adversarial script, session scratchpad, not committed>          # see "E2E adversarial pass" below
git diff -- <9 changed files> | grep -Eic "api[_-]?key|secret|token|bearer|password"    # 2 (both are the phrase "Proxy token" in comments, no literal secret values)
git status --short                                                                       # clean of scratch files at the end
```

**Reference comparison — no guarantee lost in the port.** `pulse/ingest.py::_prewarm` (~line 370) and its call site (~line 437) match `prewarm_models` structurally: no-op list on `hasattr(..., "ensure_warm")`, `asyncio.gather` with explicit `task.cancel()` + a reaping `gather(..., return_exceptions=True)` in `finally`, called once per batch before the window opens. The port ADDS two things pulse's 2-model case never needed: (1) dedup on `warm_key` (pulse never needed it — model and extractor are never the same app); (2) the one INFO line. Both are net-new guarantees, not losses.

**E2E adversarial pass** (in-process, stdlib-only `prewarm_models`, no Modal/network — per the ABSOLUTE RULES)
- Happy path: unit suite drives `prewarm_models` through all 3 seams with fakes — PASS (evidence above).
- Break path 1 (0 models, `prewarm_models()`): returns immediately, `asyncio.all_tasks()` unchanged, no log line — PASS.
- Break path 2 (same instance passed twice, `prewarm_models(w, w)`): `ensure_warm` awaited once (dedup by `warm_key`/`id`) — PASS.
- Break path 3 (both models fail): first exception (`RuntimeError`/`ModelError`) propagates, sibling's exception is retrieved by the `finally`'s `gather(..., return_exceptions=True)` (no "exception was never retrieved" warning observed) — PASS.
- Break path 4 (caller cancelled mid-prewarm, e.g. a Prefect flow cancel): `CancelledError` propagates out of `prewarm_models`, no pending tasks survive — PASS.
- Break path 5 (a duck-typed `ensure_warm` that is a plain sync function / returns a non-awaitable): raises `TypeError: a coroutine was expected, got …` — a loud, clear error, not a silent skip — PASS on "clear error" ✅, but see "Other issues found" below for a task-leak this same case causes when it's the SECOND model in a multi-model call.
- Import hygiene: `tree.memory.pipeline` and `tree.mcp.server` (not just `tree.models.get_model`, which the SWE's own test covers) both leave `modal`/`modal_embedding`/`modal_llm` out of `sys.modules` on import — PASS, closes a gap the SWE's test didn't reach.

**Acceptance criteria**
- [x] PASS — `tests/unit/models/test_prewarm.py` (8 named tests) — file exists, all 8 pass in 2.25s with no real sleeping (`test_first_failure_cancels_the_sibling` completes in <1s despite an injected 600s sleep).
- [x] PASS — `test_worker_prewarms_before_the_first_stage` (split into 3: graphrag/rag/zero-docs) — `tests/unit/memory/test_pipeline.py::TestWorkerPrewarm`, all pass; verified `pipeline.py:2015-2033` puts the pre-warm strictly after the `if not docs` guard and before `_split_documents`.
- [x] PASS — `test_dead_server_fails_the_run_with_zero_documents_attempted` — passes; verified `pipeline.py:1991-2029` (config load, DB connect, `Document.find(...).to_list()`) is entirely read-only before the pre-warm, so a `ModelError` there leaves no write, no status mutation, no checkpoint advanced.
- [x] PASS — `test_dream_prewarms_the_models_it_built` (+ no-delta) — `dream.py:669-678`: pre-warm sits after both instances are built and after both the dry-run and "no delta" guards; same `llm`/`embedding_model` objects are reused by the resolver.
- [x] PASS — `test_cluster_summaries_fail_open_when_the_llm_is_dead` (+ no-clusters) — `pipeline.py:2607-2621`; confirmed `ExtractionError(ModelError)` (`tree/models/exceptions.py`), so the `except ModelError` at line 2619 also catches a spent-deadline timeout, not just a fail-fast 4xx — ADR-007 §4 fail-open holds for both failure shapes.
- [x] PASS — hygiene greps — `grep -n "prewarm_models" .../mcp/server.py .../scripts/*.py` = 0 hits; `grep -c "warm" .../mcp/server.py` = 3; `grep -rn "prewarm_models" apps/memory/src apps/memory/scripts` shows exactly the helper (`get_model.py:179`) + 3 call sites (`pipeline.py:2029,2031,2618`, `dream.py:678`) plus the 2 import lines.
- [x] PASS — shipped-defaults no-op — `test_worker_prewarm_is_a_noop_on_default_providers` passes; `make memory-tests` green at 3789/3789.
- [x] PASS — `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green (LOCAL env, run one at a time).

**Evidence**
```
$ make memory-tests
============================ 3789 passed in 50.72s =============================
$ make memory-format-check
312 files already formatted
$ make memory-lint-check
All checks passed!
$ make pre-commit
prettier / ruff check / ruff format / biome check (harness) ......... Passed
```

**Other issues found (not blocking — PASS with note)**
1. **Task leak on a duck-type collision, not reachable in production today.** `get_model.py:229`: `tasks = [asyncio.create_task(warm()) for warm in warms.values()]` sits OUTSIDE the `try/finally`. If a LATER model's `ensure_warm` is a sync function / returns a non-awaitable (verified with a scratch double: `warm()` raises `TypeError: a coroutine was expected, got None` at `create_task` time), the exception propagates before the `try` is entered, so an EARLIER model's already-scheduled real coroutine (proved with a fake polling `asyncio.sleep(600)`) is never cancelled/reaped — a live leaked task, contradicting this same function's own "FAIL-FAST WITH EXPLICIT CANCEL" docstring guarantee for this specific failure shape. Not reachable with the two real production classes (`ModalEmbeddingModel.ensure_warm` / `ModalLLM.ensure_warm` are both proper `async def`, no `__getattr__` anywhere in `tree/models/*.py`), and pulse's `_prewarm` has byte-identical structure (same bug, if any, pre-exists the port — not a guarantee the port lost). Risk vector is future duck-typed test doubles or objects (the SWE already had to fix one bare-`MagicMock()` collision for exactly this reason). One-line fix: build `tasks` incrementally inside the `try` so the `finally` still reaps partial construction. Recommend a follow-up test/fix, not a blocker for this task.
2. **Construction-cost delta on the `rag`/`graphrag` pre-warm is unconditional, not gated like its siblings.** `pipeline.py:2029` / `:2031-2033` calls `get_search_embedding_model()` (and `get_resolution_embedding_model()`, `get_llm()`) BEFORE it's known whether the downstream task has any work: `_embed_children` (`pipeline.py:507-511`) and `_embed_entities` (`pipeline.py:1197-1206`) both guard `if not texts: return {}` before constructing the model, and both tasks carry a 90-day `INPUTS` cache (`embed_children_task`/`embed_entities_task`), so a previously fully-cached or zero-child run used to skip model construction entirely. The pre-warm now always builds it. For Voyage/Gemini this is free (client construction only, proved by `test_worker_prewarm_is_a_noop_on_default_providers`); for the `sentence-transformers` provider (a real, supported option per `get_model.py:69-76`, not a mock) construction is `SentenceTransformer(...)` (`tree/models/sentence_transformer.py:23-28`), which loads real model weights and is measured in seconds plus memory — every `rag`/`graphrag` run now pays that even on a fully-cached re-run. This is written verbatim into task §C.1 (`prewarm_models(get_search_embedding_model())` right after the zero-docs guard, unconditionally) — a spec-level trade-off inherited faithfully, not an SWE implementation defect — but it is asymmetric with the explicit "must not wake a GPU for nothing" reasoning applied at the `_embed_nodes` NOT-pre-warmed site one page over. Recommend a follow-up decision (ADR-009 amendment or task) on whether `sentence-transformers` should be excluded from the pre-warm the same way `_embed_nodes` is.
3. Pre-existing, unrelated `UserWarning` from `opik`'s `pydantic_utilities` (`Core Pydantic V1 functionality isn't compatible with Python 3.14`) fires on every test collection, including bare `uv run python` imports of unrelated modules — confirmed unrelated to this diff (no opik/pydantic files touched), not introduced by this change.

**VERDICT: PASS**
