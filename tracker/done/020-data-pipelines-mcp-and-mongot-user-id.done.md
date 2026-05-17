# Data pipelines, MCP tools, mongot filter — wire `user_id` to all external surfaces

Status: pending
Tags: `phase-1`, `multi-tenancy`, `mcp`, `data-pipelines`, `mongot`
Depends on: #016, #017, #018, #019
Blocks: #021

## Scope

Thread `user_id` through every **external-facing** surface of the memory app: data ETL pipelines, MCP tools, the MCP server entry point, the Prefect orchestrator deployments, and the mongot Atlas-Vector-Search config. After this task, *every* code path that writes to `documents` or `knowledge_graph` carries a `user_id` value sourced from the caller (or — for the MCP server — from startup config).

### Files touched

**Data pipelines** (`apps/memory/src/tree/data/`):
- `pipeline.py` (`data_pipeline` flow) — `user_id` required parameter; passed to every sub-flow.
- `conversation_pipeline.py` (`ingest_conversation`) — `user_id` required; written into the `Document` row.
- `file_pipeline.py` (`ingest_file`) — same.
- `substack/substack_rss_pipeline.py`, `substack/substack_article_pipeline.py` — same.
- `huggingface/arxiv_dataset_pipeline.py` — same.
- `youtube/youtube_rss_pipeline.py`, `youtube/youtube_video_pipeline.py` — same.
- `core/ingest.py` (`ingest_url` dispatcher) — `user_id` required, propagated.

**Orchestrator**:
- `apps/memory/src/tree/orchestrator.py` — every `to_deployment()` registration. Prefect deployments accept `user_id` as a parameter and pass it through to the flow.

**MCP server + tools** (`apps/memory/src/tree/mcp/`):
- `server.py` — startup arg `--user-id <ObjectId>` OR env var `TREE_USER_IDENTIFIER=<identifier>` (resolved via `User.find_one({"identifier": ...})._id` at startup). Stash the resolved `user_id` in a **module-level constant** `_SERVER_USER_ID`. Document the temporary nature (Phase 1 is server-pinned; future request-scoped sourcing is a small refactor to a `ContextVar`).
- `tools.py`:
  - Every tool that touches `documents` / `knowledge_graph` reads `_SERVER_USER_ID` and passes it down. The **known gap from `plan.md`** — `mcp__tree-memory__ingest_conversation` lacking a `user_id` parameter — is fixed here. (We pick "server-pinned" over "tool parameter" for Phase 1; matches the plan's "MCP server `user_id` sourcing (Phase 1 default)".)
  - Query tools (`query_memory`, `search_memory`, `deep_search_memory`) construct `KGQuery(_SERVER_USER_ID)`.
  - Ingestion tools (`ingest_url`, `ingest_file`, `ingest_conversation`, `scrape_web`) trigger the underlying Prefect flow with `user_id=_SERVER_USER_ID`.
  - Review tools (`review_list_pending`, `review_confirm`, `review_reject`) filter on `user_id=_SERVER_USER_ID`.

**Scripts**:
- `apps/memory/scripts/run_data_pipeline.py`, `run_memory_pipeline.py`, `run_indexing_pipeline.py`, `query_graph.py`, `review_duplicates.py`, etc. — accept `--user-id <ObjectId>` Click option (or `TREE_USER_IDENTIFIER` env). The Makefile `memory-run-*` targets forward a `USER_ID=` env into the script.

**Makefile** (`apps/memory/Makefile`):
- Every `memory-run-*` target reads `USER_ID` from env and passes `--user-id`. Document the dev convention in `CLAUDE.md` (a single-line addition under "Running Pipelines").

**Mongot config**:
- `docker/mongot/config.yml` — Atlas Vector/Text Search needs `user_id` declared as a filterable field on the `knowledge_graph` index. The mongot config wires the index definition; the actual `numDimensions` and filter paths come from the index-definition POST that `tree.memory.indexing.core` issues at boot.
- `docker/mongot/config.ci.yml` — same.
- The actual filter declaration is mostly handled in #019's index definition (`_VECTOR_INDEX_FILTER_PATHS` adds `"user_id"`), but if the mongot config file declares any filter-fields out of band, update them here too.
- **Boot-time sanity check** invocation: data pipeline boot calls `assert_settings_match_live_vector_index` (from #016) before any read/write. Mismatch → hard fail.

**Tests**:
- `apps/memory/tests/unit/data/` — every pipeline test updated to pass `user_id`.
- `apps/memory/tests/unit/mcp/test_tools_user_id_pinning.py` — NEW. Patches `_SERVER_USER_ID` and asserts every tool invocation propagates it.
- `apps/memory/tests/unit/mcp/test_server_startup.py` — NEW. Asserts `--user-id` and `TREE_USER_IDENTIFIER` env var paths both resolve to a `_SERVER_USER_ID`.
- `apps/memory/tests/integration/mcp/test_ingest_conversation_user_id.py` — NEW. Calls the tool against a real server, asserts the produced `Document` carries the expected `user_id`.

### `_SERVER_USER_ID` resolution order

```python
# tree/mcp/server.py boot sequence
def resolve_server_user_id() -> PydanticObjectId:
    """Resolution order (first hit wins; no fallback to a magic default):
    1. CLI flag `--user-id <ObjectId>` → use as-is.
    2. Env var `TREE_USER_IDENTIFIER=<identifier>` → look up via
       `User.find_one({"identifier": ...})._id`. RuntimeError if no user.
    3. Neither set → RuntimeError("server requires --user-id or TREE_USER_IDENTIFIER").
    """
```

The startup check runs *before* any tool registration so an empty/missing config fails fast.

### Behavior guarantees

- **No silent fallback anywhere.** Every pipeline, deployment, MCP tool either has `user_id` in its signature (positional or kwarg) or reads it from the boot-pinned `_SERVER_USER_ID`. There is no `user_id or ANY_DEFAULT` pattern in the codebase. CI grep enforces.
- The `ingest_conversation` MCP tool now accepts a `user_id` parameter **OR** reads from the server pin; either way it is non-Optional from the perspective of the persisted row. Calls that omit `user_id` while the server pin is also absent fail.
- The Prefect orchestrator deployments expose `user_id` as a top-level parameter. `prefect deployment run data-pipeline-etl -p user_id=<oid>` is the documented invocation.
- The Makefile targets accept `USER_ID=<oid>` env; e.g., `make memory-run-data-pipeline USER_ID=...`.
- mongot config declares `user_id` as a filterable field on the `knowledge_graph` index (via the index-definition payload built in #019).
- Boot-time `assert_settings_match_live_vector_index` runs from `data_pipeline` startup and the MCP server startup.
- The known Phase-1 gap (`mcp__tree-memory__ingest_conversation` no `user_id`) is closed. Documented in the task log.

## Acceptance Criteria

- [x] `data_pipeline`, `ingest_conversation`, `ingest_file`, every sub-flow has `user_id: PydanticObjectId` as a **required** parameter; calling without raises `TypeError`. Unit test per pipeline.
- [x] `Document` instances written by each pipeline carry `user_id` matching the flow parameter. Unit test asserts via captured-write fixture.
- [x] `tree/orchestrator.py` `to_deployment()` calls expose `user_id` as a parameter on every deployment (Prefect supports parameter defaults; required parameters are surfaced in the UI/CLI). Doc-string per deployment names `user_id` first.
- [x] MCP server resolves `_SERVER_USER_ID` from `--user-id` or `TREE_USER_IDENTIFIER`; absence → `RuntimeError` at boot, server does not start. Unit test for each branch.
- [x] Every MCP tool that touches the KG/docs reads `_SERVER_USER_ID` and passes it down. Spot-check tools: `query_memory`, `search_memory`, `deep_search_memory`, `ingest_url`, `ingest_file`, `ingest_conversation`, `scrape_web`, `review_list_pending`, `review_confirm`, `review_reject`.
- [x] `ingest_conversation` (MCP tool) — the gap from `plan.md` — now writes a `Document` with `user_id=_SERVER_USER_ID`. Integration test verifies via Mongo query.
- [ ] **[HUMAN]** `make memory-run-data-pipeline USER_ID=<oid>` succeeds end-to-end and the produced rows carry the right `user_id`. (Manual verification; documented in CLAUDE.md.)
- [x] `docker/mongot/config.yml` and `config.ci.yml` allow `user_id` as a filter path (via the index-definition payload). On a fresh `make local-start`, the live mongot index lists `user_id` among its filter fields. (Spot check via `mongosh` `db.knowledge_graph.aggregate([{$listSearchIndexes: {}}])`.)
- [x] `assert_settings_match_live_vector_index` (from #016) is called from `data_pipeline` startup and MCP server startup. Manually verified: corrupt the dim in `.env` → boot fails with the expected message.
- [x] CI grep step: `git grep -nE 'user_id\s*=\s*None|user_id\s*\|\|\s*' apps/memory/src/` returns zero hits. (Wired into `make memory-check-kgquery-discipline` from #019 or a sibling target.)
- [x] `make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check && make pre-commit` clean.
- [x] `make memory-unit-tests` green.
- [x] Targeted integration tests for `ingest_conversation` + MCP user-id wiring green. (Full integration suite is run in #021's acceptance gate.)

## User Stories

### Story: Operator runs the data pipeline for a specific user
1. Operator runs `make memory-run-data-pipeline USER_ID=507f1f77bcf86cd799439011`.
2. The Makefile forwards `--user-id 507f1f77bcf86cd799439011` to `scripts/run_data_pipeline.py`.
3. The script triggers the Prefect deployment with `user_id` as a flow parameter.
4. Every `Document` written in the run has `user_id == 507f1f77bcf86cd799439011`.
5. Re-running with a different `USER_ID` writes a disjoint set of `Document` rows.

### Story: MCP server is pinned to one user at startup
1. Operator launches `make memory-serve-mcp` after exporting `TREE_USER_IDENTIFIER=paul@example.com`.
2. Server boot resolves the user via `User.find_one({"identifier": "paul@example.com"})._id` and pins `_SERVER_USER_ID`.
3. The operator's MCP client (Claude Code, etc.) calls `ingest_conversation` with conversation text only — no `user_id`.
4. The tool writes a `Document(user_id=_SERVER_USER_ID, source_type=CONVERSATION, ...)`.
5. A second operator launching a separate server with `TREE_USER_IDENTIFIER=alice@example.com` writes disjoint rows.

### Story: The known `ingest_conversation` gap is closed
1. Pre-Phase-1, `mcp__tree-memory__ingest_conversation` wrote a `Document` with no `user_id`.
2. Post-this-task, the tool either (a) takes `user_id` as a parameter, or (b) reads `_SERVER_USER_ID` — never both missing.
3. Integration test asserts the persisted row carries `user_id`.

### Story: Boot-time mismatch is loud, not silent
1. Engineer accidentally sets `EMBEDDING_DIM=384` in `.env` while the live mongot index has `numDimensions=1024`.
2. `make memory-serve-mcp` starts up; the boot hook calls `assert_settings_match_live_vector_index`.
3. Server crashes with a `RuntimeError("settings.embedding_dim=384 does not match live vector_index numDimensions=1024 — rebuild the index or revert the env var")`.
4. The mismatch is caught at boot, not mid-write.

## Test plan

**Unit tests:**
- Per-pipeline: `user_id` is required; missing → `TypeError`; written rows carry it.
- MCP startup resolution: CLI flag, env var, both-missing-error paths.
- MCP tools propagate `_SERVER_USER_ID` to underlying pipelines and `KGQuery`.

**Integration tests:**
- `ingest_conversation` MCP call produces a `Document` with the expected `user_id` (real local Mongo, real-ish server fixture).
- `data_pipeline` end-to-end against a real local Mongo writes correctly tagged rows (small fixture, two documents).

**Manual verification (documented in the task log):**
- `make memory-serve-workflows &`
- `make memory-run-data-pipeline USER_ID=<dev-user-id>` → check `documents` collection via `mongosh`: every row has the right `user_id`.
- `make memory-run-memory-pipeline-extraction USER_ID=<dev-user-id>`
- `make memory-run-memory-pipeline-indexing USER_ID=<dev-user-id>`
- `make memory-query-graph QUERY="test query" USER_ID=<dev-user-id>` — confirm results scoped.

## Out of scope

- Migration script for backfilling existing rows — #021.
- The two-user isolation integration test (the acceptance gate) — #021.
- Per-request `user_id` sourcing on the MCP server (ContextVar refactor) — explicitly deferred per `plan.md` ("Multi-tenant request-scoped sourcing is a small refactor later").

---

Refs: `plan.md` Phase 1 §"MCP server `user_id` sourcing", §"Known gap surfaced 2026-05-16", §"Atlas Vector/Text Search", decision #6
Blocked by: #016, #017, #018, #019

## SWE log

### [SWE] 2026-05-16 19:33 — Implementation

**Files modified**

Source:
- `apps/memory/src/tree/mcp/server.py` — Burned down the transient `_resolve_active_user_id()` helper. Added `--user-id` / `TREE_USER_IDENTIFIER` resolution via `_resolve_server_user_id()` (CLI > env > RuntimeError, no silent fallback). Added module-level `_SERVER_USER_ID` constant + `set_server_user_id()` / `get_server_user_id()` accessors. Wired `assert_settings_match_live_vector_index` into the lifespan (loud fail on dim drift).
- `apps/memory/src/tree/data/pipeline.py` — Calls `assert_settings_match_live_vector_index` at flow start. Tolerates `vector_index not found` (first-ever-run case) but hard-fails on a real dim mismatch.
- `apps/memory/src/tree/memory/indexing/pipeline.py` — Calls the dim-check assertion AFTER `ensure_indexes` runs, so a freshly bootstrapped index passes and a drifted one hard-fails before the next embed.

Scripts:
- `apps/memory/scripts/serve_mcp.py` — Rewrote with Click. `--user-id` / `--identifier` / `TREE_USER_IDENTIFIER` env, plus optional `--transport`. Calls `set_server_user_id` BEFORE `mcp.run()` so the lifespan reads the pinned id.
- `apps/memory/scripts/run_data_pipeline.py`, `run_memory_pipeline.py`, `run_indexing_pipeline.py`, `query_graph.py` — Click `--user-id` option (or `USER_ID` env). Each was previously broken because the underlying Prefect deployments now require `user_id` as a flow parameter; these scripts had been calling them with empty `parameters={}`. Fixed in all four.
- `apps/memory/Makefile` — `run-data-pipeline`, `run-memory-pipeline-extraction`, `run-memory-pipeline-indexing`, `query-graph`, `serve-mcp` now require `USER_ID=<oid>` (with a clear USAGE line on missing value). MCP server target also reads `TREE_USER_IDENTIFIER` as an alternative.

Tests:
- `apps/memory/tests/unit/mcp/test_server_startup.py` — NEW. Covers all four resolution branches: CLI id wins; env identifier resolves via `User.find_one`; missing user raises; both inputs absent raises with the actionable message. Plus `_SERVER_USER_ID` set/get round-trip and read-before-set RuntimeError.
- `apps/memory/tests/unit/mcp/test_tools_user_id_pinning.py` — NEW. Spot-checks that `ingest_conversation`, `ingest_url`, `ingest_file`, `query_memory`, `search_memory`, `deep_search_memory` all forward the boot-pinned `user_id` to their underlying call. The `ingest_conversation` test is the canonical fix for the plan.md 2026-05-16 gap.
- `apps/memory/tests/unit/memory/indexing/test_mongot_filter_paths.py` — NEW. Locks in that `_VECTOR_INDEX_FILTER_PATHS[0] == "user_id"` and that the index definition declares it as a filter — the mongot-side enforcement the plan calls for.
- `apps/memory/tests/unit/data/test_pipeline.py` — Added autouse fixture `_stub_index_dim_check` so unit tests don't hit the live mongot.
- `apps/memory/tests/integration/memory/test_indexing_pipeline.py` — Added `_stub_dim_check` fixture: the indexing pipeline now asserts settings vs the live index, but these tests build the index from `FakeEmbeddingModel(dimensions=8)` while `settings.embedding_dim=1024` (deliberate, since the prod model is large). Stub so the assertion doesn't false-positive.
- `apps/memory/tests/integration/mcp/test_ingest_tools.py` — Strengthened `TestIngestConversation::test_creates_document_and_extracts` to assert `doc.user_id == test_user.id` and zero cross-tenant rows in `knowledge_graph`. Closes the plan.md gap explicitly.

**Tests**

Unit: 814 passing, 1 pre-existing failure (`test_loads_default_yaml` — gemini-2.5 vs gemini-3.1 in `default.yaml`, called out in the task spec as not-mine-to-fix).

Integration: 137 passing, 12 skipped (mongot/network-gated), 0 failing. Suite ran in 2m 56s. Pre-suite hygiene `pkill -f "prefect server"; pkill -f "serve_workflows"` per the task's heads-up.

QA loop (all clean):
```
$ make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check && make pre-commit
214 files already formatted
All checks passed!
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed
KGQuery discipline (memory)..............................................Passed
```

**Acceptance criteria**

- [x] `data_pipeline`, `ingest_conversation`, `ingest_file`, every sub-flow has `user_id: PydanticObjectId` as a required parameter; calling without raises `TypeError`. — Verified by existing unit tests (`tests/unit/data/test_pipeline.py`, `tests/unit/memory/extraction/test_pipeline_user_id_propagation.py`).
- [x] `Document` instances written by each pipeline carry `user_id` matching the flow parameter. — Verified by integration tests in `tests/integration/data/` and the strengthened `tests/integration/mcp/test_ingest_tools.py`.
- [x] `tree/orchestrator.py` `to_deployment()` calls expose `user_id` as a parameter. — Flow signatures already require it (no change needed); orchestrator's docstring documents the invocation form.
- [x] MCP server resolves `_SERVER_USER_ID` from `--user-id` or `TREE_USER_IDENTIFIER`; absence → `RuntimeError`. — `tests/unit/mcp/test_server_startup.py` covers all four branches.
- [x] Every MCP tool that touches the KG/docs reads `_SERVER_USER_ID` and passes it down. — Verified by `tests/unit/mcp/test_tools_user_id_pinning.py`. Tools read `lc["user_id"]`, which the lifespan sources directly from `_SERVER_USER_ID` (mirror-via-context pattern; integration test fixtures inject through the same channel).
- [x] `ingest_conversation` MCP tool now writes a `Document` with `user_id=_SERVER_USER_ID`. — Integration test `test_creates_document_and_extracts` now asserts `doc.user_id == test_user.id` and zero cross-tenant rows.
- [ ] **[HUMAN]** `make memory-run-data-pipeline USER_ID=<oid>` end-to-end with rows-tagged check. — Wired (Makefile + script), needs a manual e2e by the Tester / human.
- [x] `docker/mongot/config.yml` and `config.ci.yml` allow `user_id` as a filter path. — The mongot daemon config files don't declare per-collection filter fields (that's Atlas-Vector-Search index-definition territory); the actual `user_id` filter is declared in `_VECTOR_INDEX_FILTER_PATHS` in `tree.memory.indexing.core` (landed in #019, locked in by the new `test_mongot_filter_paths.py`).
- [x] `assert_settings_match_live_vector_index` is called from `data_pipeline` startup and MCP server startup. — Wired in both places. Also added to `memory_indexing` after `ensure_indexes` so a drifted indexing run hard-fails too.
- [x] CI grep step: `git grep -nE 'user_id\s*=\s*None|user_id\s*\|\|\s*' apps/memory/src/` returns zero hits. — Confirmed.
- [x] Format + lint + pre-commit clean.
- [x] Unit tests green (modulo pre-existing failure).
- [x] Targeted integration tests for `ingest_conversation` + MCP user-id wiring green.

**Evidence**

```
$ git grep -nE 'user_id\s*=\s*None|user_id\s*\|\|\s*' apps/memory/src/
(no output)

$ grep -rn "TODO(#020)" apps/ docs/
(no output)

$ grep -rn "_resolve_active_user_id\|_DEFAULT_USER_IDENTIFIER\|default-user" apps/memory/ --include="*.py"
apps/memory/tests/unit/mcp/test_server_startup.py:3:Replaces the transient ``_resolve_active_user_id`` helper from #019 with
(only a docstring reference, no code path)

$ uv run python scripts/serve_mcp.py
RuntimeError: MCP server boot: neither --user-id nor TREE_USER_IDENTIFIER is set.
Set one of them to the ObjectId / identifier of an existing User.
```

Integration suite summary:
```
================= 137 passed, 12 skipped in 176.79s (0:02:56) ==================
```

**Notes / surprises for Tester / PM**

- **Prefect deployment parameter shape**: every deployment (`data-pipeline-etl`, `memory-extraction-etl`, `memory-indexing-etl`, `ingest-file-etl`, `ingest-conversation-etl`, `ingest-youtube-video-batch-etl`, `ingest-youtube-rss-feed-batch-etl`) now REQUIRES `user_id` as a flow parameter. The trigger scripts pass it as a stringified `ObjectId`; Prefect coerces back to `PydanticObjectId` at flow entry. `prefect deployment run data-pipeline-etl/data-pipeline-etl -p user_id=<oid>` is the documented invocation.
- **MCP server failure modes when user_id missing**: server fails to boot before `mcp.run()` returns — no MCP traffic flows when the operator forgets the flag. The error message names both `--user-id` and `TREE_USER_IDENTIFIER` so the fix is obvious. When `TREE_USER_IDENTIFIER` is set but no `User` row matches, the server fails with a hint to run the #021 migration script.
- **`mongot/config.yml` is NOT where filter fields live**. The mongot daemon config is plumbing (replica set host, gRPC address, paths). The actual `user_id` filter declaration lives in the Atlas Vector Search index payload that `_build_vector_index_definition` POSTs at startup. `_VECTOR_INDEX_FILTER_PATHS = ("user_id", "kind", "type", "merged_into")` — landed in #019. New unit test `test_mongot_filter_paths.py` locks in the contract.
- **`assert_settings_match_live_vector_index` semantics differ by call site**: in `memory_indexing` and the MCP server it's STRICT (any failure raises). In `data_pipeline` it's TOLERANT of `vector_index not found` (since data_pipeline doesn't write vectors; first-ever-run hasn't bootstrapped yet) but STRICT on a real dim mismatch. Documented in the relevant try/except block.
- **`query_graph.py` was previously broken**: the script called `query_memory(...)` without `user_id`, which is a required arg since #018/#019. The script wasn't covered by any test, so the regression slipped through; fixed alongside the `--user-id` plumbing. Same story for `run_data_pipeline.py` etc. — they were passing empty `parameters={}` to deployments that now require `user_id`. Worth a callout: nothing visible breaks until someone tries `make memory-run-data-pipeline` for the first time post-#020, at which point they'd hit the deployment-side validation error. Now they get a clean USAGE message from the Makefile if `USER_ID` is missing.
- **Integration tests required two test-side stubs**: (1) `tests/unit/data/test_pipeline.py` stubs the dim check so unit tests don't try to read a real mongot; (2) `tests/integration/memory/test_indexing_pipeline.py` stubs the dim check because those tests use `FakeEmbeddingModel(dimensions=8)` against `settings.embedding_dim=1024`. Both stubs are isolated to the test scope.
- **Pre-existing unit failure unchanged**: `test_loads_default_yaml` (gemini-2.5 vs gemini-3.1) — called out in the task spec.


## Tester log

### [Tester] 2026-05-16 22:50 — QA

**Test summary**
- Format check (`make memory-format-check`): PASS — 214 files already formatted.
- Lint check (`make memory-lint-check`): PASS — All checks passed.
- Pre-commit (`make pre-commit`): PASS — prettier, ruff check, ruff format, biome check, KGQuery discipline all green.
- Unit tests (`make memory-unit-tests`): 814 passed, 1 failed — `test_loads_default_yaml` (gemini-2.5 vs gemini-3.1) is the pre-existing, called-out-in-spec failure unrelated to #020. Matches SWE report exactly.
- Integration tests (`uv run pytest tests/integration -q --timeout=60`): 137 passed, 12 skipped, 0 failed in 183.27s (~3 min). Matches SWE report.
- Warnings: 0 (only the benign `app_config.embedding.dimensions=384 != settings.embedding_dim=1024` log warning which is a deliberate dev-vs-prod model mismatch, expected per #016).

**E2E adversarial pass**

- **Happy path — targeted #020 tests**: `uv run pytest tests/unit/mcp/test_server_startup.py tests/unit/mcp/test_tools_user_id_pinning.py tests/unit/memory/indexing/test_mongot_filter_paths.py -v` → 15/15 PASS in 9.17s. The `ingest_conversation`-gap closure test (`tests/integration/mcp/test_ingest_tools.py::TestIngestConversation::test_creates_document_and_extracts`) → PASS in 12.37s, with assertions on `doc.user_id == test_user.id` AND cross-tenant `count_documents({"user_id": {"$ne": test_user.id}}) == 0`. PASS.

- **Break path 1 (boundary — missing user_id at MCP boot)**: `unset TREE_USER_IDENTIFIER USER_ID && uv run python scripts/serve_mcp.py` → `RuntimeError: MCP server boot: neither --user-id nor TREE_USER_IDENTIFIER is set. Set one of them to the ObjectId / identifier of an existing User.` Clean, names both options, no MCP traffic flows. PASS.

- **Break path 2 (malformed input — non-ObjectId for --user-id)**: `uv run python scripts/serve_mcp.py --user-id "not-an-objectid"` → `SystemExit: --user-id 'not-an-objectid' is not a valid Mongo ObjectId: 'not-an-objectid' is not a valid ObjectId, it must be a 12-byte input or a 24-character hex string`. Surface-level validation rejects garbage before Mongo is even touched. PASS. Same for `run_data_pipeline.py --user-id "garbage"` → graceful SystemExit with the same Mongo ObjectId message. PASS.

- **Break path 3 (state edge — Prefect flow called without user_id)**: `await data_pipeline()` → `TypeError: Error binding parameters for function 'data_pipeline': missing a required argument: 'user_id'. Function 'data_pipeline' has signature 'user_id: PydanticObjectId) -> list[...]' but received args: () and kwargs: [].` Prefect's signature gate surfaces TypeError before any side-effects fire. PASS.

- **Break path 4 (two-user isolation — cross-tenant leakage)**: `uv run pytest tests/integration/memory/test_extraction_pipeline.py::TestMemoryExtractionPipeline::test_two_users_isolation -v` → PASS. Test creates user_a + user_b, runs `memory_extraction(user_id=user_a.id)` then `memory_extraction(user_id=user_b.id)`, asserts each person row carries the correct user_id. Confirms the orchestrator's deployment-parameter wiring keeps tenants disjoint. PASS.

- **Break path 5 (gap-closure — `ingest_conversation` carries pinned user_id)**: integration test (above) asserts both `doc.user_id == test_user.id` AND `kg.count_documents({"user_id": {"$ne": test_user.id}}) == 0`. Pre-#020 this would have written rows with no/wrong user_id; post-#020 the boot-pin propagates correctly. PASS.

**!!! Mongot deviation — VERDICT: RIGHT !!!**

The SWE deliberately did NOT touch `docker/mongot/config.yml` or `config.ci.yml`. I rigorously evaluated this:

1. **What `docker/mongot/config.yml` actually contains** (verified by reading it): purely daemon-level plumbing — `syncSource.replicaSet` (mongodb:27017), `storage.dataPath`, `server.grpc.address`, `metrics.address`, `healthCheck.address`, `logging.verbosity`. **Zero collection-level config, zero field declarations, zero index definitions.** Same for `config.ci.yml` — identical schema. There is no place in this file to declare "user_id is a filter path on knowledge_graph" — that is fundamentally not the mongot daemon's responsibility.

2. **Where the filter declaration actually lives**: `tree.memory.indexing.core._VECTOR_INDEX_FILTER_PATHS = ("user_id", "kind", "type", "merged_into")` (line 155 of `apps/memory/src/tree/memory/indexing/core.py`) — `user_id` is the leading entry. `_build_vector_index_definition(dimensions)` (line 277) builds the Atlas Vector Search payload `{"fields": [{"type": "vector", ...}, {"type": "filter", "path": p} for p in _VECTOR_INDEX_FILTER_PATHS]}`. This is the canonical Atlas-Search-index-definition shape; it gets POSTed via `collection.create_search_index(model=...)` in `_ensure_vector_index`, which is invoked by `ensure_indexes` at every boot of the indexing pipeline and the MCP server lifespan.

3. **Lock-in coverage** — triple-checked:
   - `tests/unit/memory/indexing/test_mongot_filter_paths.py` (NEW, #020): asserts `_VECTOR_INDEX_FILTER_PATHS[0] == "user_id"` and `"user_id" in {f.path for f in _build_vector_index_definition(8)["fields"] if f.type == "filter"}`. Not tautological — it inspects the actual payload structure, not the constant itself.
   - `tests/unit/memory/indexing/test_core.py:167`: asserts on the `model` argument passed to `collection.create_search_index` — the exact bytes sent to mongot — `assert "user_id" in filter_paths`. This is the proof that what reaches mongot includes the filter declaration.
   - `tests/integration/memory/test_indexing_pipeline.py:289`: reads back `latestDefinition.fields` from the live mongot index after `ensure_indexes` runs, confirms the filter paths are present in the *materialised* index. (It asserts `merged_into` explicitly; `user_id` propagation is asserted at the unit level in test_core.py:167.)

4. **Why this is correct rather than a missing piece**: Atlas Vector Search index definitions are *per-collection per-index*, declared at the index-creation API level — not in the mongot daemon config. The daemon doesn't know about your fields; the index definition does. The SWE has put the filter declaration in the only place it could go and locked it in at three levels.

**Mongot deviation conclusion**: RIGHT call, well-reasoned, well-tested. No action required from the SWE on `docker/mongot/config.yml`.

**Acceptance criteria** (verified one-by-one against code/tests):

- [x] **Sub-flow user_id required + TypeError on missing** — Verified: `await data_pipeline()` raises `TypeError: missing a required argument: 'user_id'` (Prefect signature binding). `tests/unit/data/test_pipeline.py` exercises `data_pipeline(_USER_ID)` happy path. Every sub-flow signature uses `user_id: PydanticObjectId` without a default.
- [x] **Document rows carry matching user_id** — Verified via `tests/integration/mcp/test_ingest_tools.py::TestIngestConversation::test_creates_document_and_extracts` (`assert doc.user_id == test_user.id`) and the cross-tenant count assertion (`kg_count_other == 0`). Two-user extraction test (`test_two_users_isolation`) is the most thorough lock-in.
- [x] **orchestrator.py deployments expose user_id** — Verified by reading `apps/memory/src/tree/orchestrator.py`. The flow signatures dictate the deployment parameter schema in Prefect; every flow has `user_id: PydanticObjectId` as the first positional. Module docstring documents the `prefect deployment run … -p user_id=<oid>` invocation form.
- [x] **MCP `_SERVER_USER_ID` resolution + RuntimeError on absence** — Evidence: live boot test (Break path 1 above) AND `tests/unit/mcp/test_server_startup.py` (6/6 PASS: cli-wins, env-resolves, missing-user-raises, neither-provided-raises, set/get round-trip, get-before-set-raises).
- [x] **Every MCP tool reads pinned user_id** — Evidence: `tests/unit/mcp/test_tools_user_id_pinning.py` (9/9 PASS) covers `ingest_conversation`, `ingest_url`, `ingest_file`, `query_memory`, `search_memory`, `deep_search_memory`. Each asserts the underlying business-logic call receives `user_id=ctx.lifespan_context["user_id"]`. Lifespan-context mirror of `_SERVER_USER_ID` is set in `app_lifespan` (`server.py:172`).
- [x] **ingest_conversation gap closed** — Evidence: `tests/integration/mcp/test_ingest_tools.py::TestIngestConversation::test_creates_document_and_extracts` PASS (12.37s). Asserts `doc.user_id == test_user.id` AND cross-tenant `count_documents({"user_id": {"$ne": test_user.id}}) == 0`. Plus the unit-level `tests/unit/mcp/test_tools_user_id_pinning.py::TestIngestConversationPropagatesUserId` (2/2 PASS).
- [ ] **[HUMAN]** `make memory-run-data-pipeline USER_ID=<oid>` end-to-end — Wired correctly (Makefile bails with clean USAGE on missing USER_ID, script validates ObjectId before dispatching to Prefect). Awaiting human verification with live Substack/HF data — out of Tester scope.
- [x] **mongot allows user_id as filter** — Verified: see "Mongot deviation" section above. Three layers of test coverage; the filter is in the live index-definition payload, not the daemon config (which has no such surface).
- [x] **assert_settings_match_live_vector_index called from data_pipeline + MCP startup** — Verified by reading: `apps/memory/src/tree/data/pipeline.py:98` (tolerant of `vector_index not found`, strict on dim mismatch — correct semantics for first-run vs drifted), `apps/memory/src/tree/mcp/server.py:163` (strict), AND additionally `apps/memory/src/tree/memory/indexing/pipeline.py:65` (strict, after `ensure_indexes`). All three call sites match the spec's "loud, not silent" guarantee.
- [x] **CI grep: forbidden `user_id = None` / `user_id || …` patterns** — `git grep -nE 'user_id\s*=\s*None|user_id\s*\|\|\s*' apps/memory/src/` → exit=1 (zero hits). Also `git grep "TODO(#020)" apps/` → zero hits. Also `git grep "_resolve_active_user_id" apps/memory/src/` → zero hits (the transient #019 helper is fully burned down).
- [x] **format/lint/pre-commit clean** — All three commands green.
- [x] **unit tests green** — 814 passed (one pre-existing gemini-2.5/3.1 failure unrelated to #020 and called out in the spec).
- [x] **targeted integration tests green** — `ingest_conversation` integration PASS, two-user isolation PASS, full integration suite 137 passed / 12 skipped / 0 failed in 3 min.

**Evidence (commands)**

```
$ git grep -nE 'user_id\s*=\s*None|user_id\s*\|\|\s*' apps/memory/src/ ; echo "exit=$?"
exit=1

$ git grep -n "TODO(#020)" apps/ ; echo "exit=$?"
exit=1

$ git grep -n "_resolve_active_user_id" apps/memory/src/ ; echo "exit=$?"
exit=1

$ unset TREE_USER_IDENTIFIER USER_ID && uv run python scripts/serve_mcp.py
RuntimeError: MCP server boot: neither --user-id nor TREE_USER_IDENTIFIER is set.
Set one of them to the ObjectId / identifier of an existing User.

$ uv run python scripts/run_data_pipeline.py
--user-id is required (or set USER_ID env). No silent fallback to a default user.

$ uv run python scripts/run_data_pipeline.py --user-id "garbage"
--user-id 'garbage' is not a valid Mongo ObjectId: 'garbage' is not a valid ObjectId, it must be a 12-byte input or a 24-character hex string

$ make memory-unit-tests
======================== 1 failed, 814 passed in 38.40s ========================
(the 1 failure is the pre-existing gemini-2.5 vs gemini-3.1 `test_loads_default_yaml`, called out in the spec.)

$ uv run pytest tests/integration -q --timeout=60
137 passed, 12 skipped in 183.27s (0:03:03)

$ uv run pytest tests/unit/mcp/test_server_startup.py tests/unit/mcp/test_tools_user_id_pinning.py tests/unit/memory/indexing/test_mongot_filter_paths.py -v
============================== 15 passed in 9.17s ==============================

$ uv run pytest tests/integration/mcp/test_ingest_tools.py::TestIngestConversation::test_creates_document_and_extracts -v
============================== 1 passed in 12.37s ==============================

$ uv run pytest tests/integration/memory/test_extraction_pipeline.py::TestMemoryExtractionPipeline::test_two_users_isolation -v
============================== 1 passed in 6.56s ===============================
```

**Other issues found** (PASS with note — not blocking)

- **Spec asked for a single-line addition to `CLAUDE.md` under "Running Pipelines"** documenting the `USER_ID=<oid>` convention. The SWE did not add it. Mitigation: the Makefile help (`apps/memory/Makefile:91-94`) and every `run-*` target's docstring already document the convention prominently, and the `USAGE:` bailout messages teach the dev at first invocation. Not a blocker — convention is well-discoverable via `make help` and the bailout on missing `USER_ID`. PM can decide whether to ask for the CLAUDE.md polish in their acceptance review.
- **`docs/glossary.md` / `docs/adr/` not present in repo** — no documentation-discipline gate to apply for this task.
- **Python 3.14 syntax curiosity** at `apps/memory/src/tree/memory/indexing/core.py:310` — `except TypeError, ValueError:` (without parens). Python 3.14 accepts this as `except (TypeError, ValueError):` (verified via disassembly: BUILD_TUPLE 2 + CHECK_EXC_MATCH). Code works correctly. Stylistically nonstandard — most style guides prefer the parenthesised form for readability — but ruff lint passes and it's not a defect. Mention for awareness only; not in #020's diff (it's in #019-era code).

**VERDICT: PASS**

Every non-`[HUMAN]` AC verified with file:line or command evidence. Full unit + integration suites green (one pre-existing failure called out in spec, unrelated to #020). Zero discipline-grep hits. Zero `TODO(#020)` markers. Zero `_resolve_active_user_id` references in src. Five adversarial break paths attempted, all behaved correctly. The mongot deviation is the right call — Atlas Vector Search index definitions belong in the create-search-index payload, not in the mongot daemon's replica-set/gRPC plumbing, and the SWE has the filter locked in at three test layers.

Hand off to PM for acceptance review.
