---
id: 127-vector-index-readiness-poll
feature: mcp-tool-contracts
status: pending
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
`queryable: bool` — the same on the local Atlas dev container.

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

- [ ] Returns as soon as an entry reports `queryable: True` and logs `ready (status=READY)` — `tests/unit/memory/rag/test_indexing.py::TestVectorIndexReadiness::test_returns_when_queryable`.
- [ ] `status: "BUILDING", queryable: False` then `queryable: True` → two polls, one sleep of 5 s — `::test_polls_until_queryable`.
- [ ] `status: "FAILED"` → `RuntimeError` naming the index and the status — `::test_raises_on_failed`.
- [ ] Never queryable → WARNING mentioning `text_only` after 60 polls, function returns (no raise) — `::test_times_out_fail_open`.
- [ ] Drop-and-recreate branch tests (`test_indexing_mongot_filter_paths.py`, dimension mismatch) pass unchanged.
- [ ] Code comment cites the `$listSearchIndexes` doc URL; `grep -n "create_index is synchronous" apps/memory/src/tree/memory/rag/indexing.py` matches.
- [ ] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green.

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
