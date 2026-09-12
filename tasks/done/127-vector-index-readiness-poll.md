---
id: 127-vector-index-readiness-poll
feature: mcp-tool-contracts
status: done
---

# Vector-index readiness poll: wait for `queryable`, fail on `FAILED`, 5-minute fail-open cap

Tags: `memory`, `rag`, `indexing`
Depends on: None
Blocks: —
Implements: ADR-008 — Consequence "degraded, not broken" (readiness is observed, not guessed)

## Scope

`tree/memory/rag/indexing.py::_ensure_vector_index` polls `list_search_indexes(name)` until the
index merely EXISTS, sleeps 3 s and logs "ready" (30 × 2 s cap). Atlas `$listSearchIndexes`
returns `status` (`PENDING | BUILDING | READY | STALE | FAILED | DELETING | DOES_NOT_EXIST`) and
`queryable: bool`.

**Correction (SWE, verified 2026-09-12 — the grooming line "the same on the local Atlas dev
container" is wrong):** the LOCAL mongot entry carries NEITHER field. `mongosh` and a real index
build both return exactly `{id, latestDefinition, name, type}` for `vector_index`; Atlas carries
`status` + `queryable`. So a literal wait-for-`queryable` would burn the full 300 s cap on EVERY
local indexing run. Readiness rule implemented (mirrors #124's `_vector_index_is_queryable`):
entry absent → keep polling; `queryable is True` → ready; `status == "FAILED"` → raise; entry with
NEITHER field → ready (INFO that readiness fields are absent); anything else → keep polling.

- **Before coding:** verify the two field names and the status set via the `tech-docs` skill
  (context7 → MongoDB `$listSearchIndexes` output fields); record the doc URL in a code comment.
- Replace the tail loop: constants `_VECTOR_INDEX_READY_TIMEOUT_S = 300`, `_VECTOR_INDEX_POLL_S = 5`.
  Loop: `entry = first of list_search_indexes(_VECTOR_INDEX_NAME)`; `status = entry.get("status")`,
  `queryable = entry.get("queryable")`. `queryable is True` → INFO
  `Vector search index '%s' ready (status=%s)` and return. `status == "FAILED"` → raise
  `RuntimeError(f"Vector search index '{name}' build failed (status=FAILED)")`. Otherwise DEBUG the
  status and sleep. Past the cap → WARNING `… not queryable after 300 s (last status=%s); retrieval
  runs text_only until it is` and return (fail-open, as today).
- Keep drop-and-recreate unchanged. One comment above the `$text` index creation
  (`create_index`, line ~262): the standard text index needs no poll — `create_index` is synchronous.
- The sleep must be patchable in tests (`asyncio.sleep` referenced through the module).

## Out of scope
- Polling at MCP boot differently from the indexing phase (same function; `MCP_SKIP_INDEX_BOOTSTRAP`
  already covers serverless boot). Making the cap a YAML knob.

## Acceptance Criteria

- [x] Returns as soon as an entry reports `queryable: True` and logs `ready (status=READY)` — `tests/unit/memory/rag/test_indexing.py::TestVectorIndexReadiness::test_returns_when_queryable`.
- [x] `status: "BUILDING", queryable: False` then `queryable: True` → two polls, one sleep of 5 s — `::test_polls_until_queryable`.
- [x] `status: "FAILED"` → `RuntimeError` naming the index and the status — `::test_raises_on_failed`.
- [x] Never queryable → WARNING mentioning `text_only` after 60 polls, function returns (no raise) — `::test_times_out_fail_open`.
- [x] Entry with NEITHER `status` nor `queryable` (local mongot) → ready, no polling — `::test_entry_without_readiness_fields_is_ready` (added per the Scope correction above).
- [x] Drop-and-recreate branch tests (`test_indexing_mongot_filter_paths.py`, dimension mismatch) pass unchanged.
- [x] Code comment cites the `$listSearchIndexes` doc URL; `grep -n "create_index is synchronous" apps/memory/src/tree/memory/rag/indexing.py` matches.
- [x] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green — clean repo-wide, 2980 tests passed.

## User Stories

### Story: Fresh local stack, first indexing run
1. Operator runs `make memory-run-indexing-pipeline` on an empty `memory` collection.
2. Logs show `Waiting for vector search index to be ready…`, a few DEBUG `status=BUILDING`, then `Vector search index 'vector_index' ready (status=READY)`.
3. The immediately following `search_memory` answers `search_mode: "hybrid"`.

### Story: Index build fails on the server
1. mongot reports `status: FAILED` for `vector_index`.
2. The indexing phase fails with `Vector search index 'vector_index' build failed (status=FAILED)` — visible in the Prefect run, not a silent "ready".

### Story: Slow build on a large corpus
1. The build takes 7 minutes.
2. After 5 minutes the phase logs the WARNING and completes; retrieval reports `text_only` until the index is queryable; the next indexing run finds it up-to-date and returns immediately.

---

Blocked by: (none)

## Log

### [PA] 2026-09-12 10:25 — Grooming

**Summary**
Readiness is read from `queryable`, failure from `status`; the cap grows to 5 minutes and stays fail-open because #124 makes a not-yet-queryable index visible as `text_only`.

**Key decisions**
- Fail-open on timeout, fail-loud on `FAILED` — a timeout is "not yet", a FAILED status is "never".
- No YAML knob for the cap: two constants, a measured need would promote them.

**Dependencies**
- None (pairs with #124 at runtime, no code dependency).

**User stories**
- 3 stories covering: first run, failed build, slow build.

Ready for implementation.

### [SWE] 2026-09-12 14:05 — Implementation

**Files modified**
- `apps/memory/src/tree/memory/rag/indexing.py` — `_VECTOR_INDEX_READY_TIMEOUT_S = 300` /
  `_VECTOR_INDEX_POLL_S = 5`; new `index_entry_is_queryable(entry) -> bool | None` (the shared
  readiness rule) and `_wait_for_vector_index_ready(collection)` replacing the "entry exists →
  sleep 3 s → ready" tail loop; `create_index is synchronous` comment above the `$text` index.
- `apps/memory/tests/unit/memory/rag/test_indexing.py` — `TestVectorIndexReadiness` (6 tests) and
  `TestIndexEntryIsQueryable` (7-case truth table); the autouse `_no_mongot_sync_sleeps` fixture
  now RETURNS its `AsyncMock` so a test can assert how long the poll slept.

**Doc verification (task-required)**
`$listSearchIndexes` output fields confirmed against
<https://www.mongodb.com/docs/manual/reference/operator/aggregation/listSearchIndexes/> (cited in
`index_entry_is_queryable`'s docstring): `status` *string* and `queryable` *boolean* ("indicates
whether the index is ready to be queried"), with the status set exactly as groomed — `BUILDING`,
`DOES_NOT_EXIST`, `DELETING`, `FAILED`, `PENDING`, `READY`, `STALE`. context7 MCP tools were not
exposed to this agent, so the fallback in `.agents/skills/tech-docs` was used (MongoDB `llms.txt`
→ the manual page above).

**Local vs Atlas catalogue shape (the Scope correction above)**
```
$ mongosh … --eval 'db.memory.aggregate([{$listSearchIndexes:{}}]).toArray()…'
[{"keys":["id","name","type","latestDefinition"],"name":"vector_index"}]
```
No `status`, no `queryable` on local mongot — Atlas sends both. Hence the three-valued helper:
`None` = "this deployment does not report readiness" (read as ready), NOT "not ready".

**Deviation from the literal spec (please read as intent)**
`status == "FAILED"` is checked BEFORE `queryable`, the reverse of the Scope's loop order. The docs
state an index in `FAILED` "may be queryable" — right after a create/recreate that means mongot is
serving the PREVIOUS definition (the stale-dimensions state
`assert_settings_match_live_vector_index` exists to catch), so fail-loud wins. Pinned by
`::test_raises_on_failed_even_when_queryable`.

**Tests**
- Unit, reproducible claim: **39 passing, 0 failing** across the three indexing test files — the
  only files this task's diff can affect (`indexing.py` imports nothing from `search.py` /
  `retrieval.py` / `mcp/tools.py`).
- Full suite `make memory-tests`: **2980 passed, 0 failed at 14:20** — but that snapshot includes
  #125's UNCOMMITTED `min_vector_score` diff (`configs/default.yaml`, `app_config.py`,
  `mcp/tools.py`, `search.py`, `retrieval.py`, `types.py` + their tests were all modified on disk),
  so it is not reproducible on demand. **Tester: re-run the full suite once #125 commits.**
- Integration: N/A — no integration suite in this repo (AGENTS.md).

**Acceptance criteria**
- [x] `test_returns_when_queryable` — returns on poll 1, logs `ready (status=READY)`.
- [x] `test_polls_until_queryable` — 2 probes, `sleep.await_args_list == [call(5)]`.
- [x] `test_raises_on_failed` (+ `test_raises_on_failed_even_when_queryable`).
- [x] `test_times_out_fail_open` — 60 probes, 60 sleeps, WARNING with `text_only` / `not queryable
  after 300 s` / `last status=BUILDING`, no raise.
- [x] `test_entry_without_readiness_fields_is_ready` — through `_ensure_vector_index`, zero sleeps.
- [x] Drop-and-recreate + dimension-mismatch tests unchanged and green.
- [x] `grep -n "create_index is synchronous" …/indexing.py` → line 271; doc URL at line 485.
- [x] QA loop green — format/lint/pre-commit clean repo-wide; 39/39 indexing tests, full suite
  2980 passed with #125's uncommitted diff present (see Tests).

**Evidence**
```
$ uv run pytest tests/unit/memory/rag/test_indexing.py \
    tests/unit/memory/rag/test_indexing_mongot_filter_paths.py \
    tests/unit/memory/rag/test_indexing_settings_vector_index_check.py -q
39 passed in 0.66s

$ make memory-format-fix && make memory-lint-fix && make memory-format-check \
  && make memory-lint-check && make pre-commit
288 files left unchanged / All checks passed! / 288 files already formatted /
All checks passed! / ruff check Passed / ruff format Passed / biome check Passed

$ grep -n "create_index is synchronous" apps/memory/src/tree/memory/rag/indexing.py
271:    # create_index is synchronous: the standard $text index is built by mongod

$ make memory-tests
============================ 2980 passed in 50.02s =============================
```

**Evidence — end-to-end against the live local mongot** (scratch script; `_wait_for_vector_index_ready`
on the real `memory` collection, then a REAL `create_search_index` + poll on a throwaway collection
that was dropped afterwards; infra left as found):
```
Waiting for vector search index 'vector_index' to be ready (up to 300 s)...
Vector search index 'vector_index' reports neither 'status' nor 'queryable' (local mongot); treating it as ready
--- 1. live `memory` collection, index already built ---
--- 2. fresh index on a throwaway collection (real build) ---
Waiting for vector search index 'vector_index' to be ready (up to 300 s)...
Vector search index 'vector_index' reports neither 'status' nor 'queryable' (local mongot); treating it as ready
catalogue entry keys: [['id', 'latestDefinition', 'name', 'type']]
--- scratch collection dropped ---
```
A fresh build publishes its entry on the FIRST poll with no readiness fields — under a literal
"wait for `queryable is True`" this run would have cost the full 300 s cap.

**Notes**
- **`test_absent_entry_keeps_polling` is unit-only coverage** of a branch the local stack never
  takes: in the e2e above a freshly created index published its catalogue entry on the FIRST poll,
  so "entry not there yet" is an Atlas / publish-race path verified by the unit test, not by a real
  deployment.
- **Mid-run flakiness, resolved:** intermediate suite runs were red on `test_search.py`,
  `test_retrieval.py` and `tests/unit/mcp/test_tools.py` with a DIFFERENT failing set each time
  (7 → 1 → 4 → 2) — the concurrent #124/#125 agent was editing those files live. Once its edits
  settled the suite came back fully green (2980 passed). Nothing in this task's diff touches those
  files.
- **Story 1 is only partly verifiable on local:** the `ready (status=READY)` log line is
  Atlas-only; a local run gets the "readiness fields absent" INFO instead (evidence above). The
  `status=READY` path is covered by unit tests, not by a local pipeline run.
- **Follow-up for `rag/search.py` (NOT done here — file owned by #124 right now):**
  `_vector_index_is_queryable` should delegate its per-entry decision to
  `index_entry_is_queryable`, keeping its own "entry absent → unavailable" and probe-raises →
  fail-open branches. **Required when adopting it:** branch on `is None` BEFORE any truthiness
  test — `None` is falsy, and a plain `if entry_is_queryable(entry):` would report `text_only` for
  every empty vector leg on a healthy local stack (the exact bug #124 fixed, relocated).
- ADR-008 §5 says the poll "waits for `queryable`". That sentence is imprecise for local mongot,
  which reports no readiness fields at all; PA may want to amend it. Not edited (SWE is read-only
  on ADRs), and not escalated as a fork — the rule was decided in grooming and by #124's precedent.
- `pre-commit` was ALSO run file-scoped first (`--files` on this task's two files) to avoid
  rewriting the other agent's in-flight files; the repo-wide run then came back clean with zero
  churn, so no foreign file was touched.
- Prefect deployment was NOT restarted: the e2e above exercises the changed code path directly
  against real mongot, so `make memory-run-indexing-pipeline` was unnecessary.

### [Tester] 2026-09-12 15:40 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`ruff format` 288 files unchanged, `ruff check` all passed, `pre-commit --all-files` green: prettier / ruff check / ruff format / biome all Passed).
- Unit tests: `make memory-tests` → 2980 passed, 0 failed, run twice (once before, once after the live-mongot e2e below) — identical result both times.
  **Reproducibility caveat (per SWE's own flag):** 2980 includes #125's uncommitted diff on `search.py`/`retrieval.py`/`types.py`/`app_config.py`/`configs/default.yaml`/`mcp/tools.py` + their tests, which is NOT part of 127 and not reproducible on demand. The reproducible claim for 127 alone is the 39 tests across `test_indexing.py` (33) + `test_indexing_mongot_filter_paths.py` (2) + `test_indexing_settings_vector_index_check.py` (4), independently re-run here (`14 selected` readiness/truth-table subset shown below, remaining 25 confirmed green in the full-file run).
- Integration tests: N/A — no integration suite in this repo (AGENTS.md); e2e verification performed instead (below).
- Warnings: 0.

**E2E adversarial pass**
- Happy path: real local mongot, dropped `vector_index` via `mongosh`, then called `ensure_indexes(...)` directly (not through a stale Prefect deployment) → log line `Vector search index 'vector_index' reports neither 'status' nor 'queryable' (local mongot); treating it as ready` fired on the FIRST poll (no 300 s wait); index restored (see below). PASS.
- Break path 1 (deviation-pinned ordering: `status="FAILED", queryable=True`): ran `_wait_for_vector_index_ready` directly against a scripted collection → raised `RuntimeError: Vector search index 'vector_index' build failed (status=FAILED)`. Matches the pinned deviation (FAILED checked before queryable). PASS.
- Break path 2 (`status="READY", queryable=False`): scripted collection returns this state on every poll → 60 probes, 60 sleeps of 5s, ends via fail-open WARNING, no raise, no false "ready". Confirms `queryable=False` overrides a `READY` status label. PASS.
- Break path 3 (`status="STALE", queryable=True`): single probe → returns immediately as ready (`queryable` wins for any non-FAILED status). PASS.
- Break path 4 (timeout path): scripted `status="BUILDING", queryable=False` forever → exactly 60 probes, 60 `sleep(5)` calls, WARNING contains `text_only` and `not queryable after 300 s` and `last status=BUILDING`, function returns without raising. PASS.
- Break path 5 (drop-and-recreate still ends in the NEW wait, not just call-count assertions): built a collection double reporting `numDimensions=1536` on the unnamed reconcile probe (mismatch vs. target 8) and a no-readiness-fields entry on the post-recreate named probe → `drop_search_index` + `create_search_index` both fired, exactly one `sleep(2)` (the pre-recreate settle sleep) and exactly one named `list_search_indexes("vector_index")` probe that resolved to "ready" via the no-readiness-fields branch — i.e. the code path genuinely runs through `_wait_for_vector_index_ready` (not the deleted old tail loop, which slept 3s post-ready) after a real drop+recreate. PASS.
- Break path 6 (advisor-flagged: does "ready" via the no-readiness-fields rule actually mean queryable?): after the happy-path drop+recreate above, issued a real `$vectorSearch` aggregate against the freshly (re)created index with a non-zero 1024-dim query vector → `QUERY_OK 3 docs returned` with real `_id`s from the tenant's data. Confirms the local "neither field → ready" shortcut is not a false positive on this stack: catalogue-entry presence does correlate with query-serving readiness here. Noted caveat: this proves catalogue presence ≈ ingestion-far-enough-along on THIS collection (6337 docs, index already warm before the drop), not a guarantee for a brand-new, empty collection — out of scope for this task (SWE's e2e already showed the same on a throwaway fresh collection with a real build).
- Extra adversarial (malformed entries into `index_entry_is_queryable`, run directly): `{}` → `None`, `{"status": None, "queryable": None}` → `None`, `{"status": "unknown_future_status"}` → `False` (unrecognized status is NOT read as ready — safe default), `{"queryable": False, "status": "READY"}` → `False` (explicit `queryable` overrides label), `{"queryable": "true"}` (string, not bool) → `False` via the `is True` identity check (no truthy-string coercion bug). All PASS / no crashes.
- Infra restored: `vector_index` recreated by `ensure_indexes` itself with a byte-identical definition (1024 dims, same 5 filter paths — new `indexID`/timestamp only, since MongoDB assigns those); `memory` collection count unchanged (6337 before and after); `git status` unchanged (only the pre-existing 125/127 uncommitted files, no stray scratch artifacts in the repo).

**Acceptance criteria**
- [x] PASS — Returns as soon as an entry reports `queryable: True` and logs `ready (status=READY)` — `test_returns_when_queryable` passes; independently reproduced against a scripted collection (break path 3 above, `STALE`/`queryable=True` variant).
- [x] PASS — `status: "BUILDING", queryable: False` then `queryable: True` → two polls, one sleep of 5 s — `test_polls_until_queryable` passes (`sleep.await_args_list == [call(5)]`).
- [x] PASS — `status: "FAILED"` → `RuntimeError` naming the index and status — `test_raises_on_failed` + `test_raises_on_failed_even_when_queryable` pass; independently reproduced (break path 1).
- [x] PASS — Never queryable → WARNING mentioning `text_only` after 60 polls, no raise — `test_times_out_fail_open` passes; independently reproduced (break path 4), including the `queryable=False`-despite-`READY`-status variant (break path 2, not in the original test file but consistent with the same code path).
- [x] PASS — Entry with neither `status` nor `queryable` → ready, no polling — `test_entry_without_readiness_fields_is_ready` passes; independently reproduced live against real local mongot (happy path above) AND confirmed the resulting index actually serves `$vectorSearch` (break path 6).
- [x] PASS — Drop-and-recreate branch tests pass unchanged — `test_dimension_mismatch_drops_and_recreates_with_warning`, `test_dimension_match_with_full_filters_is_noop`, `test_pre_adr006_index_missing_subtype_self_heals`, `test_missing_filter_paths_triggers_recreate_without_warning` all pass; additionally exercised the NEW wait's actual invocation on this branch by execution, not just call-count assertions (break path 5).
- [x] PASS — Code comment cites the `$listSearchIndexes` doc URL; `grep -n "create_index is synchronous"` matches — `grep -n "create_index is synchronous" apps/memory/src/tree/memory/rag/indexing.py` → `271:    # create_index is synchronous: the standard $text index is built by mongod`; doc URL `https://www.mongodb.com/docs/manual/reference/operator/aggregation/listSearchIndexes/` present at `indexing.py:485` in `index_entry_is_queryable`'s docstring.
- [x] PASS — `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green — reproduced twice, 2980 passed both times, 0 warnings (see reproducibility caveat above re: #125's uncommitted diff being included).

**User Stories**
- Story 1 (fresh local stack, first indexing run): step 2's `Waiting for vector search index to be ready…` DEBUG/INFO sequence and step 3 (immediately-following `search_mode: "hybrid"`) are NOT independently re-verified end-to-end here — step 3 crosses into #124/#125 territory (`search.py`, out of scope per the concurrency note). Step 2's local variant (no `status=READY` log, "readiness fields absent" INFO instead) is verified live (happy path above); the `status=READY` variant is Atlas-only and unit-tested only (`test_returns_when_queryable`), consistent with the SWE's own note.
- Story 2 (index build fails on server): unit-tested only (`test_raises_on_failed`); NOT verifiable on the local stack because local mongot never reports a `status` field at all (confirmed via `mongosh` — the entry is exactly `{id, name, type, latestDefinition}`), so a real FAILED state cannot be induced locally. Independently reproduced via direct function call (break path 1) as the closest available substitute.
- Story 3 (slow build, 7-minute case): the WARNING-then-continue half is verified by `test_times_out_fail_open` and independently reproduced (break path 4, 60×5s). The second half ("next indexing run finds it up-to-date and returns immediately") is covered by `test_dimension_match_with_full_filters_is_noop` plus the early `return` in `_ensure_vector_index` before `create_search_index` is ever called (`indexing.py:436-438`) — an up-to-date index never re-enters `_wait_for_vector_index_ready`.

**Evidence**
```
$ make memory-format-check && make memory-lint-check
288 files already formatted / All checks passed!

$ make pre-commit
prettier: Passed / ruff check: Passed / ruff format: Passed / biome check: Passed

$ make memory-tests   (run #1, before live e2e)
2980 passed in 50.49s

$ make memory-tests   (run #2, after live e2e + index drop/recreate)
2980 passed in 48.48s

$ grep -n "create_index is synchronous" apps/memory/src/tree/memory/rag/indexing.py
271:    # create_index is synchronous: the standard $text index is built by mongod

$ uv run pytest tests/unit/memory/rag/test_indexing.py \
    tests/unit/memory/rag/test_indexing_mongot_filter_paths.py \
    tests/unit/memory/rag/test_indexing_settings_vector_index_check.py \
    -v -k "Readiness or IndexEntryIsQueryable"
14 passed, 25 deselected in 0.55s

# Live mongot: drop vector_index, then ensure_indexes() directly (real mongot, not through Prefect)
INFO Waiting for vector search index 'vector_index' to be ready (up to 300 s)...
INFO Vector search index 'vector_index' reports neither 'status' nor 'queryable' (local mongot); treating it as ready
ENSURE_INDEXES DONE

# Real $vectorSearch against the just-recreated index (break path 6)
QUERY_OK 3 docs returned
 - 6a8ea9579a7aeb13175955c8:object:ai skills for real engineers
 - 6a8ea9579a7aeb13175955c8:object:gemini pro
 - 6a8ea9579a7aeb13175955c8:object:grok bot

# Break path 5: drop-and-recreate genuinely runs the NEW wait
dropped=True created=True probes(named)=['vector_index']
sleep_calls=[call(2)]   # only the pre-recreate settle sleep; no post-ready sleep(3) — confirms the OLD tail loop is gone

# Infra restored (byte-identical definition, new indexID/timestamp only)
{"name":"vector_index","fields":[...numDimensions:1024...user_id,kind,type,subtype,merged_into...]}
memory count: 6337 (unchanged, before and after)
```

**Other issues found**
- None blocking. `index_entry_is_queryable` is public but has zero callers outside `indexing.py` today — the SWE's own follow-up note says `rag/search.py` should adopt it later; not a defect, just unexercised-by-other-callers for now.
- `{"queryable": "true"}` (string) → `False` via the `is True` identity check is a defensible strict-typing choice given the doc says `queryable` is boolean, but worth keeping in mind if a future driver/version ever surfaces it as a string.
- ADR-008 §5's "waits for `queryable`" wording is imprecise for local mongot (SWE flagged this as PA's call, correctly not self-edited).

**VERDICT: PASS**
