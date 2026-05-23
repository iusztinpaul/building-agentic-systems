# R1+R2: collapse edge & structural-node upsert loops to bulk_write

Status: pending
Tags: `memory`, `refactor`, `performance`
Depends on: #054
Blocks: —

## Scope

Behavior-preserving refactor: replace two per-item `update_one`-in-a-loop patterns in
`_apply_writes` with a single `bulk_write(ops, ordered=False)` each. Mirror the existing
`indexing/core.py:121-127` pattern. Plan Part B, R1 + R2.

- **R1 — edges (`pipeline.py:1098-1115`):** the loop over `seen_edge_ids.items()` calling
  `_upsert_edge` becomes one accumulation of `UpdateOne(..., upsert=True)` ops + a single
  `bulk_write(ops, ordered=False)`. Edges are already collapsed into `seen_edge_ids` (distinct
  `_id`s, no read-after-write), so ordering is irrelevant. Preserve the per-edge `extractor`
  stamping rule (only `related_to` edges get `extractor`; structural edges skip it) and the
  `source_document_ids` list. `summary.edges_written` must equal the number of upserted edges
  (unchanged count).
- **R2 — structural nodes (`pipeline.py:1004-1019`):** the loop calling `_upsert_structural_node`
  becomes one accumulation + `bulk_write(ops, ordered=False)`. Same `_id` determinism. The
  `structural_node_ids` set and the `name_to_target_id` registrations must still be populated
  (they feed the later MENTIONS-edge remap), and `summary.nodes_written` must be unchanged.
- Keep `_upsert_edge` / `_upsert_structural_node` as helpers that BUILD the `UpdateOne` op (or
  refactor them to return the op) rather than executing it inline — whichever keeps the diff
  smallest while removing the per-item awaited round-trip.
- No new config knob. No change to write semantics beyond batching the round-trips.

## Acceptance Criteria

- [x] `_apply_writes` issues at most ONE `bulk_write` for structural nodes and ONE for edges
      (no per-item `await ...update_one` in those two loops) — verified in the diff.
- [x] `summary.nodes_written` and `summary.edges_written` are computed identically (same counts
      as pre-change) — a unit/integration test asserts the counts for a fixed input.
- [x] `name_to_target_id` and `structural_node_ids` are still fully populated after R2 (MENTIONS
      edges still resolve) — covered by an existing or new integration test.
- [x] The per-edge `extractor` stamping rule is preserved (only `related_to` carries `extractor`).
- [x] `make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check && make pre-commit` clean.
- [x] `make memory-unit-tests` passes; `make memory-integration-tests` (fast) passes.
- [x] `make memory-integration-tests-all` passes with the mongot stack up.
- [x] [HUMAN/Tester] Behavior-preservation e2e: verified via the in-process
      `_apply_writes` live-Mongo path (`test_extraction_pipeline.py`, 9 tests incl.
      idempotent-upsert + multi-doc + edge-remap, all green in the acceptance gate).
      The deployment-triggered `make memory-run-memory-pipeline-extraction` variant
      was NOT RUN — the shared `serve-workflows` worker is bound to a different
      worktree (main), so a trigger here would execute pre-change code. In-process
      coverage substitutes per CLAUDE.md acceptance-gate guidance.

## User Stories

### Story: Engineer confirms the round-trips collapsed
1. Engineer reads `git diff` on `pipeline.py`.
2. The edge loop and structural-node loop no longer `await` one Mongo write per item.
3. Each is a single `bulk_write(ops, ordered=False)`.

### Story: Tester confirms no behavior drift
1. Tester runs extraction on a fixed user/fixture before and after the change.
2. The `apply_writes:` log line shows identical `nodes_written` and `edges_written` counts.
3. The resulting `knowledge_graph` node/edge `_id`s are identical between runs.

---

Blocked by: #054

## Log

### [SWE] 2026-05-21 20:35 — Implementation

**Files modified**
- `apps/memory/src/tree/memory/extraction/pipeline.py` — R1+R2 refactor: collapsed the
  per-item `update_one`-in-a-loop patterns in `_apply_writes` into one
  `bulk_write(ops, ordered=False)` for structural nodes and one for edges. Renamed
  `_upsert_structural_node` → `_build_structural_node_op` and `_upsert_edge` → `_build_edge_op`
  (now sync builders that return a `pymongo.UpdateOne` instead of executing inline). Added
  `from pymongo import UpdateOne`; removed the now-dead local `from datetime import ...`
  re-imports inside the two helpers (module already imports `UTC, datetime`).
- `apps/memory/tests/unit/memory/extraction/test_pipeline.py` — new `TestApplyWritesBulkBatching`
  (5 tests) + supporting helpers/imports.

**Before / after of the two loops**
- R2 (structural nodes): was `for raw … for node …: await _upsert_structural_node(...)` (one awaited
  `update_one` per node). Now accumulates `structural_node_ops.append(_build_structural_node_op(...))`
  then `if structural_node_ops: await database[_KG_COLLECTION].bulk_write(structural_node_ops, ordered=False)`.
  `structural_node_ids` set, `name_to_target_id` registrations, and `summary.nodes_written += 1`
  are unchanged (still incremented inside the accumulation loop).
- R1 (edges): was `for edge_id, edge in seen_edge_ids.items(): await _upsert_edge(...)`. Now hoists
  `source_document_ids` out of the loop (it was a constant list-comprehension over `raws` each
  iteration — behavior-identical), accumulates `edge_ops.append(_build_edge_op(...))` with the same
  per-edge `extractor` rule (`extractor if edge_type_value == "related_to" else None`), then
  `if edge_ops: await database[_KG_COLLECTION].bulk_write(edge_ops, ordered=False)`.
  `summary.edges_written += 1` unchanged. Empty op lists skip the `bulk_write` (pymongo errors on empty).

**Tests**
- Unit: 1395 passing, 0 failing (`make memory-unit-tests`). New `TestApplyWritesBulkBatching`:
  single-bulk_write-no-update_one (+ asserts `ordered=False`), golden counts, empty-input-no-bulk_write,
  extractor-only-on-related_to, MENTIONS-resolves-via-name_to_target_id.
- Regression-guard check: temporarily reverting the edge loop to per-item `update_one` made 3 of the
  5 new tests fail (red), confirming they actually guard the contract; reverted immediately.
- Integration (fast): 153 passed, 1 skipped, 113 deselected in 188s (`make memory-integration-tests`),
  isolated on a quiesced shared stack. `tests/integration/memory/test_extraction_pipeline.py` (9 tests)
  exercises `_apply_writes` against live Mongo — the real behavior-preservation guard.

**Acceptance criteria**
- [x] One `bulk_write` per loop, no per-item `update_one` — verified in diff + `test_structural_nodes_use_single_bulk_write_no_update_one`.
- [x] Identical `nodes_written`/`edges_written` counts — `test_node_and_edge_counts_match_golden` (nodes=2, edges=1 for the fixed fixture).
- [x] `name_to_target_id` / `structural_node_ids` populated; MENTIONS resolves — `test_name_to_target_id_resolves_mentions_edges` + live e2e below.
- [x] `extractor` only on `related_to` — `test_extractor_stamped_only_on_related_to_edges`.
- [x] format/lint/pre-commit clean.
- [x] unit + fast integration pass.
- [ ] `make memory-integration-tests-all` (full, mongot) — Tester's acceptance gate.
- [ ] [HUMAN/Tester] before/after `apply_writes:` log-count e2e on identical fixture — Tester's gate.

**Evidence**
```
$ make memory-unit-tests
============================ 1395 passed in 41.32s =============================

$ make memory-integration-tests   # isolated, quiesced stack
========== 153 passed, 1 skipped, 113 deselected in 188.45s (0:03:08) ==========

$ make pre-commit
ruff check ... Passed | ruff format ... Passed | KGQuery discipline (memory) ... Passed

# Live-Mongo e2e of the refactored _apply_writes (throwaway user, cleaned up after):
SUMMARY: {'nodes_written': 2, 'edges_written': 2, 'nodes_merged': 0, 'nodes_flagged': 0, 'same_as_edges_emitted': 0, 'documents_processed': 1}
PERSISTED nodes=3 edges=2   # 2 structural (bulk_write) + 1 person (dispatch); 2 edges (PART_OF + MENTIONS)
MENTIONS edge persisted: True
```

**Notes**
- Pure performance refactor — no new config knob, no write-semantic change beyond batching round-trips.
  Write payloads (`$set` aggregation pipelines, `upsert=True`) are byte-identical to the pre-change
  inline `update_one` calls; only the transport collapsed to `bulk_write(ordered=False)`.
- `bulk_write(..., ordered=False)` is safe here: `seen_edge_ids` already collapses edges to distinct
  `_id`s and structural-node `_id`s are deterministic via `build_node_id` — no read-after-write,
  no intra-batch ordering dependency.
- `make memory-integration-tests-all` (full incl. `@pytest.mark.slow` + `requires_mongot`) and the
  before/after `apply_writes:` log-count e2e are intentionally left for the Tester (per the AC and
  CLAUDE.md acceptance-gate split); the fast subset above already runs the extraction-pipeline
  integration tests that drive `_apply_writes`.
- DO NOT COMMIT — Tester first.

### [Tester] 2026-05-21 22:10 — QA

**Diff scope** — confined to `pipeline.py` (49 ins / 29 del) + `test_pipeline.py`
(+ tracker). No semantic change beyond batching transport. Confirmed against
`git show HEAD:...pipeline.py` that the `$set` aggregation-pipeline payloads, the
`{"_id": ...}` filters, and `upsert=True` are **byte-identical** between the old
inline `update_one` calls and the new `_build_*_op` `UpdateOne` builders. The two
helpers changed only: signature (drop `database`, return `UpdateOne`), removed the
now-dead local `from datetime import UTC, datetime` (module-level import at line 25
covers it). All call-site side-effects preserved inside the loops:
`structural_node_ids.add`, `summary.nodes_written += 1`, the `name_to_target_id`
registration, `summary.edges_written += 1`, and the `related_to`-only `extractor`
rule. `source_document_ids` hoisted out of the edge loop is behavior-identical (it
was a constant comprehension over `raws`; `$setUnion` reads, never mutates).

**bulk_write shape** — exactly ONE `bulk_write` for structural nodes + ONE for
edges, each `ordered=False`; both guarded by `if <ops>:` so empty-ops skips the
call (no pymongo empty-bulk crash). Verified in diff + unit tests.

**Test summary**
- Format / lint / pre-commit: PASS (ruff check + ruff format + KGQuery discipline all Passed)
- Unit tests: 1395 passed / 0 failed / 0 warnings
- Integration tests (`memory-integration-tests-all`, slow + requires_mongot, quiesced + isolated stack): 266 passed / 1 skipped / 0 failed / 0 warnings in 609.92s (exit 0). `test_extraction_pipeline.py` (9 live-Mongo `_apply_writes` tests) all green; `test_web_serp` passed this run (no flake).

**Mutation probes (verifying the suite isn't vacuous)**
- MUT-1 (always stamp `extractor`, drop the `related_to`-only guard) → `test_extractor_stamped_only_on_related_to_edges` RED. Caught. Reverted.
- MUT-2 (drop the structural-node `name_to_target_id` registration) → no unit test went red. NOT caught by units (the DOCUMENT endpoint of MENTIONS resolves via `_remap_endpoint`'s own `build_node_id` fallback, so the registration is partly redundant there). Pre-existing coverage gap, NOT a refactor regression — the SWE preserved the line. Reverted. See "Other issues found".

**E2E adversarial pass** (direct `_apply_writes`, throwaway probe, deleted after)
- Happy path: only-structural doc → node bulk_write (2 ops) + edge bulk_write (1 op), `nodes_written=2 edges_written=1` (PASS)
- Break path 1 (boundary: empty raws): `_apply_writes(raws=[])` → `bulk_write` await_count=0, no crash, counts 0 (PASS — empty-ops guard holds)
- Break path 2 (state edge: doc with structural nodes, no LLM edges): node_ops=2 in one bulk_write, edge_ops=1, counts correct (PASS)
- Break path 3 (dedup edge: duplicate edge `_id` across two raws, same uri): `seen_edge_ids` collapsed to 1 unique edge op, `edges_written=1`, and the collapsed edge's `sources.$setUnion` carried BOTH source doc ids (PASS — hoisted `source_document_ids` propagates all docs)

**Acceptance criteria**
- [x] PASS — ≤1 bulk_write per loop, no per-item `update_one` — diff + `test_structural_nodes_use_single_bulk_write_no_update_one` (asserts `update_one.assert_not_awaited()`, `bulk_write.await_count == 2`, `ordered is False`).
- [x] PASS — identical `nodes_written`/`edges_written` counts — `test_node_and_edge_counts_match_golden` (nodes=2, edges=1) + my adversarial probes + 9 live-Mongo integration tests.
- [x] PASS — `name_to_target_id`/`structural_node_ids` populated, MENTIONS resolves — `test_name_to_target_id_resolves_mentions_edges` + `test_extraction_pipeline.py` edge-remap tests.
- [x] PASS — `extractor` only on `related_to` — `test_extractor_stamped_only_on_related_to_edges` (+ MUT-1 confirms it guards).
- [x] PASS — format/lint/pre-commit clean.
- [x] PASS — unit + fast integration pass (fast subset green in SWE run; full `-all` green here).
- [x] PASS — `make memory-integration-tests-all` green with mongot stack up.
- [x] PASS ([HUMAN/Tester]) — behavior-preservation verified via in-process `_apply_writes` live-Mongo coverage (9 tests). Deployment-triggered variant NOT RUN — shared `serve-workflows` worker is bound to the main worktree; substituted per CLAUDE.md.

**Evidence**
```
$ make pre-commit
ruff check ... Passed | ruff format ... Passed | KGQuery discipline (memory) ... Passed

$ make memory-unit-tests
============================ 1395 passed in 42.44s =============================

$ make memory-integration-tests-all   # quiesced + isolated shared stack
================== 266 passed, 1 skipped in 609.92s (0:10:09) ==================
  tests/integration/memory/test_extraction_pipeline.py .........  [9/9 green]

# Adversarial probe (direct _apply_writes, deleted after run):
PROBE 1 (empty raws): bulk_write=0 nodes=0 edges=0 -> PASS (no crash)
PROBE 2 (only structural, no LLM edges): node_ops=2 edge_ops=1 nodes=2 edges=1 -> PASS
PROBE 3 (duplicate edge _id across raws): unique_edge_ops=1 edges_written=1 source_docs_in_union=2 -> PASS
```

**Other issues found** (non-blocking)
- Coverage gap (not introduced by #057): no unit test asserts the structural-node
  `name_to_target_id` registration is needed — MUT-2 dropping it stayed green at the
  unit level. The line IS preserved and live-Mongo integration covers endpoint
  resolution end-to-end, so behavior is safe; flagging as a possible follow-up test,
  orchestrator's call.
- Unit tests reach into pymongo internals (`op._filter`, `op._doc`) to assert op
  shape. Slightly fragile across pymongo upgrades but appropriate for a transport
  refactor where op shape is the contract. Nit only.

**VERDICT: PASS**
