---
id: 124-hybrid-search-mode-and-search-unavailable
feature: mcp-tool-contracts
status: done
---

# Search mode on `hybrid_search` + `SearchUnavailableError` when both legs fail

Tags: `memory`, `rag`, `retrieval`
Depends on: None
Blocks: #125, #126, #128
Implements: ADR-008 — Decision 3 (retrieval contract: Search mode)

## Scope

`tree/memory/rag/search.py::_vector_search` / `_text_search` each swallow exceptions, WARNING-log
and return `[]`, so two dead legs look like "no matches". Make each leg report its own failure and
let fusion decide the mode.

- `tree/memory/rag/types.py`: `SearchMode = Literal["hybrid", "text_only", "vector_only"]`;
  `class HybridSearchResult(BaseModel)` with `hits: list[ScoredHit]` and `search_mode: SearchMode`
  (field descriptions: `text_only` = vector leg unavailable, `vector_only` = text leg unavailable).
  `RetrievalResult.search_mode: SearchMode = "hybrid"`.
- `tree/memory/rag/search.py`: `class SearchUnavailableError(RuntimeError)` (module-level, with a
  one-line docstring: both legs raised). The two legs return `list[dict] | None` — `None` means
  "the leg raised" (WARNING with `exc_info=True`, replacing today's bare message), `[]` means
  "ran, no hits". `hybrid_search` returns `HybridSearchResult`: both `None` → raise
  `SearchUnavailableError("vector and text search are both unavailable")`; vector `None` →
  `text_only`; text `None` → `vector_only`; else `hybrid`. RRF fusion unchanged.
- `tree/memory/rag/retrieval.py::retrieve_parents`: `result = await hybrid_search(...)`;
  `hits = result.hits`; every returned `RetrievalResult` carries `search_mode=result.search_mode`
  (the `top_k <= 0` early return keeps the default). `SearchUnavailableError` propagates (the MCP
  boundary catches it in #126).
- `tree/memory/graph/retrieval.py` (line ~176): read `.hits`, ignore the mode; `QueryResult` unchanged.
- `tree/cli.py` / `scripts/query_graph.py` rag printer: when `search_mode != "hybrid"`, print ONE
  first line `Search ran <mode> — the other leg was unavailable; results may miss matches.`

## Out of scope
- `outcome` / score gating (#125). Tool-boundary catch and envelope (#126). graphrag `QueryResult` fields.

## Acceptance Criteria

- [x] Vector leg raising → `HybridSearchResult.search_mode == "text_only"` with the text hits — `tests/unit/memory/rag/test_search.py::TestSearchMode::test_vector_leg_failure_reports_text_only`.
- [x] Text leg raising → `"vector_only"` — `::test_text_leg_failure_reports_vector_only`.
- [x] Both legs raising → `SearchUnavailableError` — `::test_both_legs_failing_raises`.
- [x] Both legs returning (even `[]`) → `"hybrid"`; a leg returning `[]` is NOT a failure — `::test_empty_leg_is_hybrid_not_degraded`.
- [x] The leg WARNING carries the traceback (`caplog` record `exc_info` set) — `::test_leg_failure_logs_traceback`.
- [x] `retrieve_parents` copies the mode onto `RetrievalResult` and defaults to `"hybrid"` — `tests/unit/memory/rag/test_retrieval.py::test_search_mode_copied_onto_result`, `::test_top_k_zero_keeps_default_mode`.
- [x] `tree.memory.graph.retrieval` seeds from `.hits`; existing graph retrieval tests pass unchanged — `tests/unit/memory/graph/test_retrieval.py::test_reads_hits_from_hybrid_search_result`.
- [x] `RetrievalResult.model_json_schema()` lists `search_mode` — `tests/unit/memory/rag/test_types.py::test_retrieval_result_has_search_mode`.
- [x] `tests/unit/memory/test_package_layout.py` still green (no new imports across the rag/graph valve).
- [x] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green.

Scope extension (coordinator, 2026-09-12) — an absent / not-yet-queryable `vector_index` must read as `text_only`, not `hybrid`:

- [x] Absent index (aggregate `[]`, no catalogue entry) → `text_only` — `tests/unit/memory/rag/test_search.py::TestSearchMode::test_missing_vector_index_reports_text_only`.
- [x] Building index (`{"status": "BUILDING", "queryable": False}`) → `text_only`, WARNING names the status — `::TestSearchMode::test_building_vector_index_reports_text_only`.
- [x] Empty but queryable index → stays `hybrid` — `::TestSearchMode::test_empty_but_queryable_index_stays_hybrid`.
- [x] An entry with neither `status` nor `queryable` (the LOCAL mongot shape) → stays `hybrid` — `::TestSearchMode::test_index_entry_without_status_or_queryable_stays_hybrid`.
- [x] The probe runs ONLY on an empty leg — `::TestSearchMode::test_index_probe_only_runs_on_empty_leg`.

## User Stories

### Story: Vector index is mid-rebuild, the user searches anyway
1. `vector_index` was dropped for a dimension change (or is still building), so `$vectorSearch` does
   NOT raise — mongot answers ZERO ROWS (verified against the local stack, 2026-09-12).
2. Because the vector leg came back empty, `_vector_search` probes
   `collection.list_search_indexes("vector_index")`: no entry (absent), `queryable: false`, or a
   `status` other than `READY` (building) → the leg counts as unavailable and returns `None`.
   An entry that reports neither field (what the local mongot sends) stays available.
3. `retrieve_parents(query="prefect retries")` returns the text-leg parents with
   `search_mode: "text_only"`; the WARNING names the state (`search index 'vector_index' absent`,
   or `status=BUILDING, queryable=False`).
4. When mongot itself is UNREACHABLE the aggregate does raise — same `text_only`, WARNING with the
   Mongo traceback. Nothing is silently `[]`.

### Story: Mongo is down
1. Both aggregations raise `ServerSelectionTimeoutError`.
2. `hybrid_search` raises `SearchUnavailableError`; `retrieve_parents` propagates it (no fake "0 results").

### Story: Operator queries from the CLI in rag mode during degradation
1. `TREE_MEMORY__MODE=rag make memory-query-graph QUERY="voyage rate limit"` while the vector index is missing.
2. First output line: `Search ran text_only — the other leg was unavailable; results may miss matches.`, then the ranked parents as before.

---

Blocked by: (none)

## Log

### [PA] 2026-09-12 10:10 — Grooming

**Summary**
Legs report their own failure; fusion names the mode; two dead legs raise instead of returning nothing.

**Key decisions**
- `None`-vs-`[]` return on the legs (not exceptions) keeps fusion a pure function and lets "ran, empty" stay `hybrid`.
- Exception lives in `search.py` (where it is raised); shapes in `types.py` (the rag output contract).

**Dependencies**
- None.

**User stories**
- 3 stories covering: degraded vector leg, both legs down, CLI caveat.

Ready for implementation.

### [SWE] 2026-09-12 11:58 — Implementation

**Files modified**
- `apps/memory/src/tree/memory/rag/types.py` — `SearchMode` literal, `HybridSearchResult(hits, search_mode)`, `RetrievalResult.search_mode` (default `hybrid`).
- `apps/memory/src/tree/memory/rag/search.py` — `SearchUnavailableError`; both legs now answer `list[dict] | None` (`None` = raised, WARNING with `exc_info=True`); `hybrid_search` returns `HybridSearchResult` and raises when both legs are `None`. RRF fusion untouched.
- `apps/memory/src/tree/memory/rag/retrieval.py` — reads `result.hits`, copies `result.search_mode` onto BOTH returned results (the ranked one and the "no child hits" one); the `top_k <= 0` early return keeps the default.
- `apps/memory/src/tree/memory/graph/retrieval.py` — seeds from `.hits`, ignores the mode; `QueryResult` unchanged.
- `apps/memory/scripts/query_graph.py` — `DEGRADED_SEARCH_CAVEAT`, printed as the FIRST line of the rag printer whenever `search_mode != "hybrid"` (also above `No results.`).
- `apps/memory/tests/unit/memory/rag/test_search.py` — `TestSearchMode` (5 tests) + `_break_leg` helper; existing reads moved to `.hits`; the old `test_a_text_stage_failure_degrades_to_vector_only` became `test_text_leg_failure_reports_vector_only`.
- `apps/memory/tests/unit/memory/rag/test_retrieval.py` — mocks return `HybridSearchResult`; new module-level mode tests (copied mode ×3 params, degraded-and-empty, `top_k=0` default, `SearchUnavailableError` propagation).
- `apps/memory/tests/unit/memory/graph/test_retrieval.py` — mocks return `HybridSearchResult`; new `test_reads_hits_from_hybrid_search_result`.
- `apps/memory/tests/unit/memory/rag/test_types.py` — `search_mode` in `RetrievalResult.model_json_schema()`, `HybridSearchResult` defaults.
- `apps/memory/tests/unit/scripts/test_query_graph.py` — caveat line first / degraded-and-empty / no caveat on `hybrid`.
- `apps/memory/tests/unit/mcp/test_tools.py` — the empty-answer equality assertion now expects the additive `search_mode` key (tools.py itself untouched — the envelope is #126).

**Tests**
- Unit: 2947 passing, 0 failing, no warnings summary reported — `make memory-tests` (the venv's Opik/Pydantic-V1 `UserWarning` still fires at import, as before this task).
- Integration: N/A — this repo has no integration suite (AGENTS.md); e2e ran against real Mongo instead (below).

**Acceptance criteria**
- [x] Vector leg raising → `text_only` with the text hits — `tests/unit/memory/rag/test_search.py::TestSearchMode::test_vector_leg_failure_reports_text_only`.
- [x] Text leg raising → `vector_only` — `::TestSearchMode::test_text_leg_failure_reports_vector_only`.
- [x] Both legs raising → `SearchUnavailableError` — `::TestSearchMode::test_both_legs_failing_raises`.
- [x] Both legs returning `[]` → `hybrid` — `::TestSearchMode::test_empty_leg_is_hybrid_not_degraded`.
- [x] Leg WARNING carries the traceback — `::TestSearchMode::test_leg_failure_logs_traceback` (asserts `record.exc_info is not None`).
- [x] `retrieve_parents` copies the mode and defaults to `hybrid` — `tests/unit/memory/rag/test_retrieval.py::test_search_mode_copied_onto_result`, `::test_top_k_zero_keeps_default_mode` (plus `::test_search_mode_survives_a_degraded_search_with_no_hits` for the no-hits return).
- [x] graph retrieval seeds from `.hits` — `tests/unit/memory/graph/test_retrieval.py::test_reads_hits_from_hybrid_search_result`; the four pre-existing graph tests pass with only their mock's return TYPE updated.
- [x] `RetrievalResult.model_json_schema()` lists `search_mode` — `tests/unit/memory/rag/test_types.py::test_retrieval_result_has_search_mode`.
- [x] `tests/unit/memory/test_package_layout.py` still green (no new cross-valve imports; `graph` → `rag` only).
- [x] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green.

**Evidence**

```
$ make memory-tests
============================ 2947 passed in 54.67s =============================

$ make memory-format-check && make memory-lint-check
288 files already formatted
All checks passed!

$ make pre-commit
prettier / ruff check / ruff format / biome check (harness) .......... Passed
```

End-to-end against the LOCAL stack (`make env-status` → local), all three user stories:

```
# Story 3 + Story 1 — vector leg really down (`docker stop tree-mongot`), rag mode
$ TREE_MEMORY__MODE=rag make memory-query-graph QUERY="voyage rate limit"
Vector search leg unavailable; the query runs text-only
Traceback (most recent call last):
  File ".../tree/memory/rag/search.py", line 156, in _vector_search
    cursor = await collection.aggregate(pipeline)
  ... pymongo/asynchronous/collection.py ...
Parent-document retrieval: 40 child hit(s) -> 27 parent(s), returning 10
Search ran text_only — the other leg was unavailable; results may miss matches.
[0.014] GPT-6 Astra: an automated AI Engineer you can hire for <$6 an hour
    ...

# Story 2 — both legs down (mongot stopped AND a database with no text index)
$ TREE_MEMORY__MODE=rag make memory-query-graph MONGO_INITDB_DATABASE=tree_e2e_bothdown USER_ID=... QUERY="voyage rate limit"
  File ".../tree/memory/rag/search.py", line 101, in hybrid_search
    raise SearchUnavailableError("vector and text search are both unavailable")
tree.memory.rag.search.SearchUnavailableError: vector and text search are both unavailable

# mongot restarted → hybrid again, no caveat line
$ TREE_MEMORY__MODE=rag make memory-query-graph QUERY="voyage rate limit" | grep -c "Search ran"
0

# graphrag path (reads .hits, no mode) still expands
$ TREE_MEMORY__MODE=graphrag uv run python scripts/query_graph.py --query "voyage rate limit" --top-k 5 --no-open
Graph expansion: 4 seed(s) → 68 nodes, 66 edges (1 hops)
```

**Notes**
- `tree/cli.py` has NO rag printer — `grep -n "retrieve_parents\|_print_parents\|RetrievalResult" src/tree/cli.py` → no matches; it is shared Click glue for the entry scripts. The rag printer the Scope means lives in `scripts/query_graph.py`, which is what this diff changes.
- `$vectorSearch` against a database whose `vector_index` merely does not exist returns `[]` (mongot answers, empty) — it does NOT raise, so that state reads as `hybrid`. Only an unreachable/failed search node degrades the leg. The ADR-008 §5 `queryable` poll (task #128) is what makes a still-building index show up as `text_only`.
- `tests/unit/mcp/test_tools.py` needed one assertion widened because `search_memory` serializes `RetrievalResult` wholesale, so `search_mode` now appears in the tool answer. `src/tree/mcp/tools.py` itself is untouched — the outcome field and the error envelope stay with #125 / #126.
- The two e2e probe databases (`tree_e2e_degraded`, `tree_e2e_bothdown`) were dropped and `tree-mongot` restarted; local infra is back to `hybrid`.
- Follow-up for the PA (do NOT do it here — #125/#126 both edit `tools.py`): `search_memory`'s docstring is the tool description the model reads and now under-describes its answer; `search_mode` should be named in the same sentence as #125's `outcome`.
- Harness checked for strict parsing of the (now one key wider) `search_memory` payload: `grep -rn "z.object|strict()" apps/harness/src` only finds schemas for the harness's OWN local tools (read/grep/bash/edit/...) — nothing validates the MCP answer, so the additive key is safe.
- NOT RUN — `.env.example` / settings: this task adds no env var or YAML knob.

### [SWE] 2026-09-12 12:20 — Scope extension: absent / not-yet-queryable `vector_index` → `text_only`

**Files modified**
- `apps/memory/src/tree/memory/rag/search.py` — imports `_VECTOR_INDEX_NAME` from `rag/indexing.py` (no cycle: `indexing` does not import `search`; verified by importing both modules) and uses it in the `$vectorSearch` stage. `_vector_search` now returns the rows when there ARE rows, and on ZERO rows confirms the index through the new `_vector_index_is_queryable(collection)` before calling the leg empty: not queryable → `None` (→ `text_only`), queryable → `[]` (→ `hybrid`).
- `apps/memory/tests/unit/memory/conftest.py` — `FakeMemoryCollection` grew a `list_search_indexes(name)` catalogue (`search_indexes=` ctor kwarg, default = one READY/queryable `vector_index`) plus `search_index_probes` so a test can assert the probe did NOT run; `FakeCursor.to_list()` added (the driver shape `list_search_indexes` callers use).
- `apps/memory/tests/unit/memory/rag/test_search.py` — 5 new `TestSearchMode` tests (absent / building / empty-but-queryable / field-less local entry / probe-only-on-empty-leg).

**Tests**
- Unit: 2952 passing, 0 failing (was 2947; +5) — `make memory-tests`.

**Deviation from the instruction — and the evidence for it**
The instruction was "if no entry, or the entry's `queryable` is not `True`, treat the leg as unavailable". Taken literally that breaks the LOCAL stack: the local mongot's catalogue entry carries no `status` and no `queryable` at all. Verified twice, through mongosh and through the driver this code uses:
```
$ mongosh ... --eval 'db.memory.aggregate([{$listSearchIndexes:{}}])'
[{ "id": "...", "name": "vector_index", "type": "vectorSearch", "latestDefinition": {...} }]

$ (pymongo) cursor = await coll.list_search_indexes("vector_index"); await cursor.to_list()
KEYS: [['id', 'latestDefinition', 'name', 'type']]
STATUS/QUERYABLE: [(None, None)]
```
`queryable is not True` would therefore make EVERY empty vector leg on a healthy local stack claim `text_only` — a permanent false degradation in the exact environment Chapter 4 is read in. Implemented rule, which keeps both Atlas cases the instruction targets:
- no entry → **unavailable** (the dropped / never-created index — the case this extension exists for);
- `queryable: false`, or `queryable` absent with a `status` other than `READY` → **unavailable** (Atlas's BUILDING/PENDING/FAILED window);
- neither field present → **available** (local mongot);
- the probe itself raising → **available**, WARNING with traceback (an unreachable mongot already fails the aggregate, which is the loud path).

**Evidence — e2e, local env, real Mongo**
```
# 1. Drop the index (mongot answers [] rather than raising for a missing index)
$ mongosh ... --eval 'db.memory.dropSearchIndex("vector_index")'
dropped; remaining: []

$ TREE_MEMORY__MODE=rag make memory-query-graph QUERY="voyage rate limit"
Vector search leg unavailable: search index 'vector_index' absent; the query runs text-only
Parent-document retrieval: 40 child hit(s) -> 27 parent(s), returning 10
Search ran text_only — the other leg was unavailable; results may miss matches.
[0.016] [AINews] Claude Fable/Mythos 5.1: new SOTA model, ...

# 2. Recreate the index, then query again
$ TREE_MEMORY__MODE=rag make memory-query-graph QUERY="voyage rate limit"
Parent-document retrieval: 40 child hit(s) -> 23 parent(s), returning 10
(no "Search ran" line, no probe WARNING → hybrid)
```

**Notes**
- `make memory-run-indexing-pipeline` could NOT be used to recreate the index: the served Prefect deployment is stale relative to task 123's `offline_pipeline` signature and the flow run crashes before doing any work — `SignatureMismatchError: Function expects parameters [...] but was provided with parameters [..., 'run_indexing', 'run_clustering', 'document_ids', 'source_uris']`. Not caused by this task (no pipeline file touched here); it needs a worker redeploy after #123 lands. The index was recreated through the app's own bootstrap instead (`make memory-serve-mcp USER_ID=... TRANSPORT=http`, which runs `ensure_indexes` → "Vector search index 'vector_index' ready", then stopped). Local infra verified back to `hybrid`.
- The probe costs ONE `listSearchIndexes` command, and only when the ANN stage matched nothing — `retrieve_parents` asks for `top_k * 4` (40 by default) candidates, so on a populated memory the hot path never probes (`test_index_probe_only_runs_on_empty_leg` pins that). It DOES probe once per search for a brand-new user with zero rows: the index is queryable there, so the answer stays `hybrid` — one extra command on a path that returns nothing anyway.
- `_vector_search` is `@track`-wrapped and can now return `None`; `tests/unit/test_opik_no_pollution.py` (the guard on that boundary) is green.
- Task #127's `queryable` poll and this probe are complements, not duplicates: the poll makes the INDEXING side wait, the probe makes a QUERY honest about an index that is not ready yet.

### [Tester] 2026-09-12 13:05 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check && make memory-lint-check` → "288 files already formatted" / "All checks passed!"; `make pre-commit` → prettier/ruff check/ruff format/biome all Passed)
- Unit tests: 2952 passed / 0 failed — `make memory-tests` (env: local, confirmed via `make env-status`)
- Integration tests: N/A — no integration suite in this repo (AGENTS.md); e2e substitutes, run below
- Warnings: 0 in the pytest summary (the Opik/Pydantic-V1 `UserWarning` fires at import time, outside the suite, and predates this task)

**E2E adversarial pass**
- Happy path: `TREE_MEMORY__MODE=rag make memory-query-graph QUERY="voyage rate limit"` against the healthy local stack → ranked parent blocks, no caveat line (PASS)
- Break path 1 (state edge: index says `queryable:false` but the vector aggregate still returned rows): built a `FakeMemoryCollection` with one matching child row and `search_indexes=[{"status":"BUILDING","queryable":False}]`, called `hybrid_search` directly → `search_mode == "hybrid"` and `collection.search_index_probes == []` (probe never ran — hot path untouched) (PASS)
- Break path 2 (failure mode: vector aggregate `[]` AND `list_search_indexes` itself raises): monkey-patched `list_search_indexes` to raise `RuntimeError` → `hybrid_search` returned `search_mode == "hybrid"` (fail-open) with a WARNING logged (`Could not probe search index 'vector_index'...`), never `SearchUnavailableError` (PASS)
- Break path 3 (failure mode: text leg raises AND vector index absent): empty collection (`search_indexes=[]`) plus a monkey-patched `aggregate` that raises on the `$match` (text) pipeline → `hybrid_search` raised `SearchUnavailableError("vector and text search are both unavailable")`, not a fabricated `vector_only`/`text_only` (PASS)
- Break path 4 (boundary: `top_k=0`): called `retrieve_parents(..., top_k=0)` with `tree.memory.rag.retrieval.hybrid_search` spied — 0 calls recorded, `result.search_mode == "hybrid"`, `result.parents == []` (PASS)
- Break path 5 (schema-unchanged guard): `QueryResult.model_json_schema()["properties"]` → `['nodes', 'edges']`, no `search_mode` key (PASS)
- Break path 6 (real e2e, local Mongo, story-driven): dropped `vector_index` via `mongosh ... db.memory.dropSearchIndex("vector_index")`; confirmed absent via `$listSearchIndexes` (`[]`); ran `TREE_MEMORY__MODE=rag make memory-query-graph QUERY="voyage rate limit"` with stdout/stderr split (`>out 2>err`) — first line of stdout was exactly `Search ran text_only — the other leg was unavailable; results may miss matches.`, followed by ranked parents; stderr carried the `Vector search leg unavailable: search index 'vector_index' absent...` WARNING separately. Recreated the index via `make memory-serve-mcp USER_ID=... TRANSPORT=http` (`ensure_indexes` → "Vector search index 'vector_index' ready", then stopped); re-ran the same query twice — 0 occurrences of "Search ran" in the output, confirming `hybrid`. Verified via `mongosh` that the recreated local-mongot catalogue entry carries neither `status` nor `queryable` (`{id, name, type, latestDefinition}` only) — this independently confirms the SWE's deviation rationale. (PASS)

**Acceptance criteria** (original 10 + 5 extension)
- [x] PASS — Vector leg raising → `text_only` with text hits — `tests/unit/memory/rag/test_search.py::TestSearchMode::test_vector_leg_failure_reports_text_only` (exists, passes; read: asserts `result.search_mode == "text_only"` and hit ids)
- [x] PASS — Text leg raising → `vector_only` — `::test_text_leg_failure_reports_vector_only` (same file, read and green)
- [x] PASS — Both legs raising → `SearchUnavailableError` — `::test_both_legs_failing_raises`; re-verified live via break path 3 above
- [x] PASS — Both legs returning `[]` → `hybrid`, not degraded — `::test_empty_leg_is_hybrid_not_degraded`
- [x] PASS — Leg WARNING carries the traceback (`exc_info` set) — `::test_leg_failure_logs_traceback`, asserts `warnings[0].exc_info is not None`
- [x] PASS — `retrieve_parents` copies the mode, defaults to `hybrid` — `tests/unit/memory/rag/test_retrieval.py::test_search_mode_copied_onto_result` (line 397, parametrized hybrid/text_only/vector_only, asserts `result.search_mode == search_mode`), `::test_top_k_zero_keeps_default_mode` (line 448, asserts `result.search_mode == "hybrid"` and `search.assert_not_called()`); confirmed by name with `grep -n "def test_top_k_zero_keeps_default_mode\|def test_search_mode_survives...\|def test_search_mode_copied_onto_result" tests/unit/memory/rag/test_retrieval.py` — all three resolve; re-verified live via break path 4
- [x] PASS — graph retrieval seeds from `.hits`, ignores mode — `tests/unit/memory/graph/test_retrieval.py::test_reads_hits_from_hybrid_search_result`; read: mocks `HybridSearchResult(hits=seed_hits, search_mode="text_only")`, asserts expansion still runs off `.hits`
- [x] PASS — `RetrievalResult.model_json_schema()` lists `search_mode` — `tests/unit/memory/rag/test_types.py::test_retrieval_result_has_search_mode`
- [x] PASS — `tests/unit/memory/test_package_layout.py` still green — in the 2952-passed run; no new cross-valve imports found in `git diff` (only `search.py` importing `_VECTOR_INDEX_NAME` from `rag.indexing`, same valve)
- [x] PASS — `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green — reproduced above
- [x] PASS — Absent index → `text_only` — `::test_missing_vector_index_reports_text_only`; re-verified live via break path 6 (real dropped index)
- [x] PASS — Building index (`BUILDING`/`queryable:False`) → `text_only`, WARNING names the status — `::test_building_vector_index_reports_text_only`, asserts `"status=BUILDING"` and `"queryable=False"` in `caplog.text`
- [x] PASS — Empty but queryable index → stays `hybrid` — `::test_empty_but_queryable_index_stays_hybrid`
- [x] PASS — Field-less (local mongot) entry → stays `hybrid` — `::test_index_entry_without_status_or_queryable_stays_hybrid`; independently reproduced against the real local mongot catalogue in break path 6
- [x] PASS — Probe runs ONLY on an empty leg — `::test_index_probe_only_runs_on_empty_leg`; re-verified live via break path 1 (probe did not fire when the vector leg returned rows even with a non-queryable index entry)

**Evidence**
```
$ make memory-tests
============================ 2952 passed in 51.48s =============================

$ make memory-format-check && make memory-lint-check
288 files already formatted
All checks passed!

$ make pre-commit
prettier / ruff check / ruff format / biome check (harness) .......... Passed

$ mongosh ... --eval 'db.memory.dropSearchIndex("vector_index")'
$ TREE_MEMORY__MODE=rag make memory-query-graph QUERY="voyage rate limit" >out 2>err
$ head -1 out
Search ran text_only — the other leg was unavailable; results may miss matches.

# recreate + confirm
$ make memory-serve-mcp USER_ID=... TRANSPORT=http   # -> "Vector search index 'vector_index' ready"
$ TREE_MEMORY__MODE=rag make memory-query-graph QUERY="voyage rate limit" | grep -c "Search ran"
0
```

**Other issues found**
- `code-review` plugin is enabled in `.claude/settings.json`, but this Tester's toolset (Read/Edit/Write/Bash/advisor) has no way to invoke the `/code-review` slash command — it was NOT run. Substituted a manual line-by-line diff/path read of `search.py`, `retrieval.py`, `graph/retrieval.py`, `query_graph.py` covering the same ground (dead code, error handling, logging). No findings from that manual pass beyond what's below.
- `graph/query_memory` (graphrag path) now lets `SearchUnavailableError` propagate all the way out of `scripts/query_graph.py` as a raw traceback for the graphrag branch too (previously it would have logged "No data found... run the pipelines first" and exited 1 on an empty/degraded result). This is spec-intended per the task ("Tool-boundary catch and envelope" is explicitly out of scope, deferred to #126) and matches Story 2's expectation, but the CLI-side (non-MCP) surface should be confirmed as in-scope for #126's catch, or get its own follow-up — flagging for the PA, not blocking here.
- The task file's own checkbox list was already fully checked (`- [x]`) by the SWE before this review; Step 5 (flip checkboxes) was a no-op — all 15 were independently re-verified above rather than left unchecked-but-trusted.

**VERDICT: PASS**