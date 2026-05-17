# [PR review rollup] Phase 1 multi-tenancy

Status: pending
Tags: `rollup`, `pr-review`
Refs: PR #18 (branch: `feat/multi-tenancy`)

## Scope

PR Reviewer found 1 Blocker and 6 Nits in the diff. The SWE must fix
the Blocker (and may fix Nits at their discretion) in a single
coordinated pass, then hand back to the Tester. Pipeline re-runs from
QA → PM acceptance → push → re-review.

## Acceptance Criteria

- [ ] Blocker 1: `_inject_user_id` in `apps/memory/src/tree/memory/query/nl_query.py` walks into `$lookup` sub-pipelines and injects `user_id` into every nested `$match` / `$vectorSearch` / `$graphLookup`, OR `$lookup` is removed from `_ALLOWED_STAGES` and the rejection is tested.
- [ ] A new unit test in `apps/memory/tests/unit/memory/query/test_nl_query.py` plants a pipeline of the shape `[{"$match": {...}}, {"$lookup": {"from": "knowledge_graph", "pipeline": [{"$match": {...}}], "as": "x"}}]` and asserts that the post-`validate_pipeline` `$lookup.pipeline[0]["$match"]` carries `user_id` equal to the bound tenant.
- [ ] A new integration test under `apps/memory/tests/integration/test_two_user_isolation.py` (or a sibling) seeds two tenants, has the fake LLM emit a `$lookup` whose sub-pipeline lacks `user_id`, runs `execute_nl_query` for User A, and asserts no User-B rows appear in the `as`-named array of any returned document.
- [x] Tester re-runs full QA suite and PASSES (including the new regression tests). — cycle 3 verdict; see Tester log 2026-05-17 21:45.
- [ ] PM re-runs acceptance review and ACCEPTS.
- [ ] PR Reviewer re-runs and reports `NO BLOCKERS`.

## Blockers (detail)

### 1. [Standards / Multi-tenancy isolation] — `apps/memory/src/tree/memory/query/nl_query.py:226-266` (`_inject_user_id`)

- **What's wrong:** `_inject_user_id` walks the top level of the
  pipeline and injects `user_id` into `$vectorSearch.filter`, top-level
  `$match`, and `$graphLookup.restrictSearchWithMatch`. It does **not**
  descend into `$lookup` sub-pipelines. `$lookup` is in
  `_ALLOWED_STAGES` (line 33), and `validate_pipeline` only enforces
  that `$lookup.from == "knowledge_graph"` — it does not require a
  `user_id` predicate on the join. An LLM-emitted pipeline of the
  shape:

  ```json
  [
    {"$match": {"kind": "node"}},
    {"$lookup": {
      "from": "knowledge_graph",
      "pipeline": [{"$match": {"kind": "edge"}}],
      "as": "all_edges"
    }},
    {"$limit": 10}
  ]
  ```

  produces an `all_edges` array that contains edges from **every**
  tenant in the collection. The top-level `$match` correctly receives
  `user_id`, so the *seed* documents are tenant-scoped, but the
  joined-in payload is not. The same gap applies to `$lookup` with
  `localField`/`foreignField` (no embedded sub-pipeline) — Mongo will
  match across the entire collection.
- **Why it's a Blocker:** Tenant isolation is the headline guarantee
  of this PR. The four review dimensions explicitly call out "any
  query path that could leak across user_id boundaries (… `$lookup`
  aggregates without `$match`)" — this is exactly that path, in
  production code, reachable through the `query_memory` MCP tool with
  no user opt-in needed. The `KGQuery` discipline lint catches raw
  Beanie/pymongo bypasses but does nothing about the LLM-shaped
  `$lookup` surface; the two-user isolation tests cover every other
  path but not this one (see
  `tests/integration/test_two_user_isolation.py` lines 8–25 — no
  `$lookup` row).
- **Suggested fix:** Either (a) recursively descend into
  `$lookup.pipeline` inside `_inject_user_id` and apply the same
  treatment to every nested `$match` / `$vectorSearch` /
  `$graphLookup`, AND inject a `let`-aware `user_id` predicate on
  `localField`/`foreignField` joins (e.g. wrap the join in a
  sub-pipeline that adds `$match: {user_id: <bound>}`); or (b) remove
  `$lookup` from `_ALLOWED_STAGES` and rely on `$graphLookup` (which
  is already correctly handled), updating the system prompt to match.
  Option (b) is simpler and has lower blast radius — `$graphLookup`
  covers most realistic NL-query joins.
- **Regression test (required):** A unit test that asserts
  `validate_pipeline` either (a) injects `user_id` into the nested
  `$match` of a `$lookup` sub-pipeline, or (b) raises
  `PipelineValidationError` on any `$lookup` stage. Plus an
  integration test that exercises the full `execute_nl_query` path
  for User A against a two-tenant database with the LLM emitting the
  leaking shape, and asserts no User-B rows appear in the joined
  array of any returned document. This new path must also be added
  to the dimension-comment list at the top of
  `tests/integration/test_two_user_isolation.py` so future authors
  know it's covered.

## Nits (non-blocking; will be appended to PR description if pipeline advances)

### 1. [Clean code] — `apps/memory/src/tree/memory/indexing/core.py:320`

- **Suggestion:** `except TypeError, ValueError:` is technically valid
  Python 3 syntax (it parses as `except (TypeError, ValueError):` — a
  tuple of exception classes) and runs correctly. But it reads as the
  Python-2 `except E, name:` capture form and will trip anyone
  scanning the file. Write it as `except (TypeError, ValueError):` for
  clarity and to match the project's other multi-exception handlers.

### 2. [Standards / Multi-tenancy isolation] — `apps/memory/scripts/check_kgquery_discipline.py:74-76`

- **Suggestion:** The raw-pymongo regex
  `\b(?:collection|col|kg|coll)\.(?:aggregate|find|find_one|update_many|delete_many)\(`
  only catches four specific local-variable names. A handle named
  `db_kg`, `kg_col`, or even just `c` slips through — and so does the
  common pattern `database["knowledge_graph"].aggregate(...)` (which
  `smoke_resolution_dedup.py` already uses with `db["..."]`,
  unflagged). Consider either widening the regex to match `<any
  identifier>.(aggregate|find|find_one|update_many|delete_many)(` and
  letting the allow-list do the suppression, or adding a second regex
  that matches `\[\s*["']knowledge_graph["']\s*\]\.<verb>\(` to catch
  the bracket-subscript shape. The current narrowness is a documented
  trade-off (#023 chose conservative matching to avoid false
  positives) but future leak shapes will keep slipping through —
  worth a follow-up issue at minimum.

### 3. [Documentation / Standards] — `apps/memory/src/tree/memory/query/nl_query.py:230-239`

- **Suggestion:** The `_inject_user_id` docstring claims "every
  `$vectorSearch` filter and `$match`" gets the user_id, plus
  `$graphLookup`. The docstring does NOT call out that `$lookup`
  sub-pipelines are NOT walked — exactly the gap that produced
  Blocker #1. Even after Blocker #1 is fixed, the docstring should
  enumerate every stage type that is and is not walked, so the next
  author of an allowed stage knows the contract. (Related: the system
  prompt at line 146-152 already warns about safety but doesn't say
  "do not use `$lookup` for cross-document joins" — adjust to match
  whichever direction Blocker #1 is fixed.)

### 4. [Clean code] — `apps/memory/src/tree/memory/indexing/core.py:206`

- **Suggestion:** `_ = user_id  # noqa: F841 — required for parameter-shape consistency`
  is a code smell — the parameter is genuinely unused. If the
  parameter is only there for "signature mirroring" then the function
  contract is misleading (callers expect their `user_id` to influence
  behavior). Two cleaner options: (a) drop the parameter and update
  callers, or (b) use it — at minimum, log it so the operator can
  correlate index runs with tenants. The current shape leaves a
  type-checker noise marker in production code.

### 5. [Untested] — `apps/memory/src/tree/memory/query/kgquery.py:135-186` (`find_neighbors`)

- **Suggestion:** `find_neighbors` implements multi-hop BFS with an
  `edge_types` filter and a `max_hops` parameter. The unit tests under
  `tests/unit/memory/query/test_kgquery.py` cover the single-method
  shape, but I didn't see a test that walks a 2- or 3-hop cycle with
  `edge_types` filtering, asserting (a) `seen_edge_ids` correctly
  deduplicates, (b) `visited_nodes` prevents re-traversal back through
  an already-visited node, and (c) cross-tenant neighbors of a
  cross-tenant edge endpoint never appear (the latter is implicit in
  the two-user integration test, but a focused unit test would catch
  a future refactor cheaper). Non-blocking because the integration
  test does exercise the general isolation contract — but the
  multi-hop branch logic deserves a dedicated unit test.

### 6. [Standards] — `apps/memory/src/tree/memory/query/kgquery.py:40`

- **Suggestion:** `if user_id is None:  # type: ignore[unreachable]`
  with a `ValueError` is good defense, but the `type: ignore` comment
  hides the fact that runtime callers can and do pass `None` (e.g. a
  freshly-instantiated `User` whose `.id` hasn't been populated). The
  type checker rules out `None` based on the signature; runtime
  doesn't. Drop the `type: ignore` or change the signature to
  `PydanticObjectId | None` and add a real type-narrowing branch.
  Current shape is defensible but the `unreachable` annotation
  contradicts the purpose of the check.

---

## Log

### [PR Reviewer] 2026-05-17 — Review (rollup, cycle 1)

**VERDICT: 1 Blocker, 6 Nits**

Reviewed the full diff vs `origin/main` (129 files, ~10,940 added /
~674 removed lines). Walked every changed source file. Spot-checked
the allow-listed raw-pymongo paths (`review/core.py`, `query/core.py`,
`extraction/dedup.py`, `extraction/pipeline.py`, `indexing/core.py`,
`scripts/query_graph.py`) — every one threads `user_id` into every
`$match` / `$lookup` / `find()` / `update_one` / `delete_one` call.
`first_person_resolver` correctly redirects to `name="self"` without
crossing tenants (the redirect only mutates the in-memory
`ExtractedNode`; the eventual `_id` is `build_node_id(user_id, …)`).
The migration script is idempotent and safe to re-run (Step 2 is a
no-op `$set`; Step 3 drops an idempotently-rebuildable collection;
Step 4 uses `$setOnInsert`; Step 5 is fire-and-forget). The discipline
pre-commit hook catches the four enumerated local-handle var names but
has a known narrowness gap (Nit 2).

The Blocker is on the **single LLM-shaped query path** that the lint
cannot police: `nl_query.execute_nl_query` allows `$lookup` and only
injects `user_id` into top-level `$match` / `$vectorSearch` /
`$graphLookup`. A `$lookup` with `from: "knowledge_graph"` and a
sub-pipeline produces a cross-tenant join with no warning. The
two-user isolation tests cover 14 query paths but not this one.

Pipeline re-runs from inner loop on this rollup; re-invoke me after
PM ACCEPT + re-push.

### [SWE] 2026-05-17 — Implementation (rollup, cycle 1)

**Option chosen:** **(b) Remove `$lookup` from `_ALLOWED_STAGES`.** Rationale: simplest, smallest blast radius, and `$graphLookup` already carries `restrictSearchWithMatch` which `_inject_user_id` enforces. Verified no existing test or doc relied on `$lookup` shapes — `test_lookup_wrong_collection_raises` was the only `$lookup`-shaped test in `test_nl_query.py` and has been replaced with two rejection tests (sub-pipeline form + `localField`/`foreignField` form). The system prompt was the only doc that mentioned `$lookup`; it now actively warns the LLM off it.

**Files modified**
- `apps/memory/src/tree/memory/query/nl_query.py` — dropped `$lookup` from `_ALLOWED_STAGES`; added a block comment explaining why; updated the system-prompt "Safety rules" section to forbid `$lookup` and steer the LLM to `$graphLookup`; rewrote the `_inject_user_id` docstring to enumerate every walked vs. not-walked stage (Nit 3); narrowed the `validate_pipeline` `from`-collection check to `$graphLookup` only (since `$lookup` is now rejected upstream by the allow-list).
- `apps/memory/src/tree/memory/indexing/core.py` — Nit 1: `except TypeError, ValueError:` → `except (TypeError, ValueError):` on line 320 (the old form was technically valid Python 3 but parsed confusingly). Nit 4: replaced the `_ = user_id  # noqa: F841` smell in `ensure_indexes` with a `logger.info` line that records which tenant triggered the index-reconcile run; preserves the parameter shape and gives operator-side traceability.
- `apps/memory/src/tree/memory/query/kgquery.py` — Nit 6: widened the `__init__(user_id)` signature to `PydanticObjectId | None`, dropped the `# type: ignore[unreachable]` marker, and documented why the guard is honest (real callers DO pass `None` from freshly-instantiated `User` objects). Existing `test_none_user_id_rejected` continues to pass.
- `apps/memory/tests/unit/memory/query/test_nl_query.py` — replaced `test_lookup_wrong_collection_raises` with two regression tests: `test_lookup_stage_rejected` (sub-pipeline form) and `test_lookup_with_localfield_also_rejected` (localField/foreignField form). Both assert `PipelineValidationError` with `match="not allowed"`.
- `apps/memory/tests/integration/test_two_user_isolation.py` — added the `$lookup` row (path 15) to the module-docstring dimension-comment list; added integration test `test_nl_query_lookup_stage_is_rejected_no_b_leak` that has the fake LLM emit a leaking `$lookup` (sub-pipeline lacks `user_id`), then a clean `$graphLookup`-shaped follow-up. Asserts top-level + every joined array is free of User-B rows/tokens, and that the validator forced a retry (LLM call count == 2).

**Nits applied:** 1 (parens), 3 (docs), 4 (log instead of silently ignore), 6 (drop type-ignore + honest signature).
**Nits skipped:** 2 (KGQuery discipline lint widening — larger scope, tracker-defer per orchestrator), 5 (multi-hop `find_neighbors` unit test — larger scope, tracker-defer per orchestrator).

**Tests**
- Unit: 832 passing, 0 failing (`make memory-unit-tests`, 42.56s).
- Integration (focused subset I touched):
  - `tests/integration/test_two_user_isolation.py`: 17 passing, 0 failing (85s) — includes the new `test_nl_query_lookup_stage_is_rejected_no_b_leak`.
  - `tests/integration/memory/test_indexing_pipeline.py`: 6 passing, 0 failing (37s) — covers the modified `ensure_indexes` path.
- Pre-commit: `make pre-commit` passes (prettier, ruff check, ruff format, biome, KGQuery discipline).
- Full integration suite: NOT RUN end-to-end here (15-min cost; the touched-path subsets and the unit-test full sweep are clean — Tester should run the full sweep).

**TDD evidence (red → green)**
1. Wrote `test_lookup_stage_rejected` + `test_lookup_with_localfield_also_rejected` against the unmodified `_ALLOWED_STAGES`.
2. `uv run pytest tests/unit/memory/query/test_nl_query.py -k lookup -q` — `2 failed, 2 passed` (the two new tests failed with `DID NOT RAISE PipelineValidationError`).
3. Removed `$lookup` from `_ALLOWED_STAGES`. Re-ran: `37 passed in 0.19s`.

**Acceptance criteria**
- [x] Blocker 1 fixed — option (b): `$lookup` removed from `_ALLOWED_STAGES`, system prompt updated.
- [x] Unit regression test added — `test_lookup_stage_rejected` (planted shape from the Blocker writeup) asserts `PipelineValidationError`. Companion `test_lookup_with_localfield_also_rejected` covers the no-sub-pipeline form too.
- [x] Integration regression test added — `test_nl_query_lookup_stage_is_rejected_no_b_leak` in `test_two_user_isolation.py` exercises the full `execute_nl_query` path with a fake LLM emitting `$lookup`, asserts no B-rows / B-tokens in any returned doc or joined array.
- [x] Dimension-comment list at top of `test_two_user_isolation.py` extended with the `$lookup`-rejection row (path 15).
- [ ] Tester re-runs full QA suite and PASSES — pending.
- [ ] PM re-runs acceptance review and ACCEPTS — pending.
- [ ] PR Reviewer re-runs and reports `NO BLOCKERS` — pending.

**Evidence**
```
$ make memory-unit-tests
... 832 passed in 42.56s ===

$ uv run pytest tests/integration/test_two_user_isolation.py --timeout=600
... 17 passed in 85.43s ===

$ uv run pytest tests/integration/memory/test_indexing_pipeline.py --timeout=600
... 6 passed in 37.12s ===

$ make pre-commit
... ruff check  Passed; ruff format  Passed; KGQuery discipline (memory)  Passed
```

**Notes**
- I did NOT commit. Per the workflow, the Tester goes first; the orchestrator will gate on Tester PASS + PM ACCEPT before commit/push.
- The Prefect-shutdown `ValueError: I/O operation on closed file.` stack traces that appear after each integration test run are pre-existing logging-handler noise unrelated to this PR — the test result (`passed`) is captured before the traceback prints.
- Considered Nit 4 alternative: drop the `user_id` parameter entirely. Rejected because (a) the parameter is part of a deliberately-mirrored pipeline-entry-point signature (memory_indexing → ensure_indexes_task → ensure_indexes all take user_id) and dropping it would break that shape, and (b) the test suite for `ensure_indexes` passes `user_id=` by name in every call — silently dropping it would force a wider change. Logging is the cheapest "use it for something" option.

### [Tester] 2026-05-17 17:25 — QA (rollup, cycle 1)

**Test summary (wall-time matrix)**
- `make memory-format-check` — PASS (218 files formatted)
- `make memory-lint-check` — PASS (ruff: all checks passed)
- `make pre-commit` — PASS (prettier / ruff check / ruff format / biome / KGQuery discipline all green)
- `make memory-unit-tests` — PASS, **832 passed in 43.04s, 0 warnings**
- `make memory-integration-tests` (fast subset) — PASS, **119 passed, 12 skipped, 37 deselected in 124.25s**
- `make memory-integration-tests-ci` (CI-mirror, no mongot) — PASS, **108 passed, 12 skipped, 48 deselected in 67.88s**
- `make memory-integration-tests-all` (full incl. slow + mongot) — PASS, **156 passed, 12 skipped in 325.59s**

**Non-vacuousness verification (the #023 trap)**
Per orchestrator instruction, temporarily stashed `apps/memory/src/tree/memory/query/nl_query.py` to put `$lookup` back into `_ALLOWED_STAGES` (confirmed by `grep -n '"\$lookup"' apps/memory/src/tree/memory/query/nl_query.py` → line 33 and 202 present), then re-ran the new regressions:

- Unit, fix backed out: `uv run pytest tests/unit/memory/query/test_nl_query.py -k lookup -v` →
  `FAILED test_lookup_stage_rejected` — `Failed: DID NOT RAISE PipelineValidationError`
  `FAILED test_lookup_with_localfield_also_rejected` — `Failed: DID NOT RAISE PipelineValidationError`
  (`2 failed, 2 passed, 33 deselected in 0.21s`)
- Integration, fix backed out: `uv run pytest tests/integration/test_two_user_isolation.py::TestTwoUserIsolation::test_nl_query_lookup_stage_is_rejected_no_b_leak --timeout=300` →
  `FAILED` with `AssertionError: LEAK — token 'badger' from User B appears in a User-A query result` (B's edges surfaced inside `leaked_edges`). Real cross-tenant leak observed, not a vacuous "the validator emitted a different error" pass.
- After `git stash pop`, re-ran the same selectors: `4 passed` / `1 passed`. Fix is in place and the regressions are real.

**Caller-side validation**
`grep -rn '\$lookup' apps/memory/src apps/memory/tests --include='*.py'` shows the only LLM-driven `$lookup` users were the NL-query path (now removed) and the new regression tests. The remaining `$lookup` usages in `apps/memory/src/tree/memory/review/core.py`, `apps/memory/src/tree/memory/extraction/dedup.py`, and `apps/memory/src/tree/mcp/tools.py` are raw-pymongo aggregations that thread `user_id` directly, are allow-listed in the KGQuery discipline check, and do not pass through `_ALLOWED_STAGES`. Option (b) does not break them.

**E2E adversarial pass**
Seeded a two-tenant fixture in a throwaway DB (`tester_adversarial_facet`) with user_a and user_b each owning one `person` node and two `edge` rows tagged with distinct secrets (`apples` / `apricots` for A; `badger` / `banana` for B), then ran each break path against the actual MongoDB via `pymongo.AsyncMongoClient` after passing the pipeline through `validate_pipeline`:

- **Happy path** — `$match → $graphLookup(restrictSearchWithMatch via validator) → $limit` for user_a → returns 1 doc with `connected = [edge(secret_a='apples')]`. No B rows. PASS.
- **Break path 1 — `$lookup` with `localField`/`foreignField`** — `validate_pipeline` raises `PipelineValidationError: Stage '$lookup' is not allowed`. REJECTED before reaching Mongo. PASS.
- **Break path 2 — `$lookup` with sub-pipeline (top level)** — same rejection. PASS.
- **Break path 3 — `$graphLookup` from `knowledge_graph` for user_a with user_b data present** — `restrictSearchWithMatch={'user_id': <user_a>}` injected by the validator; runtime returns only A's edge `secret_a='apples'`, no `secret_b`. PASS.
- **Break path 4 — write/eval stages (`$out`, `$merge`, `$where`)** — all three rejected by `_ALLOWED_STAGES`. PASS.
- **Break path 5 — `$graphLookup` pointing at `evil_collection`** — rejected by the `from`-collection check. PASS.
- **Break path 6 (the one the spec asked about) — `$facet` with a nested `$lookup` in its sub-pipeline:**
  Pipeline:
  ```python
  [
      {"$match": {"kind": "node"}},
      {"$facet": {"leaked": [
          {"$lookup": {"from": "knowledge_graph",
                       "pipeline": [{"$match": {"kind": "edge"}}],
                       "as": "all"}},
          {"$limit": 5},
      ]}},
      {"$limit": 5},
  ]
  ```
  `validate_pipeline` **accepted** this pipeline as safe for user_a. Runtime aggregation returned:
  ```
  ** FACET+LOOKUP LEAKS B DATA TO A **
  LEAK -> {'_id': '...:e1', 'user_id': <user_b>, 'kind': 'edge', 'name': 'b-edge-1', 'secret_b': 'badger'}
  LEAK -> {'_id': '...:e2', 'user_id': <user_b>, 'kind': 'edge', 'name': 'b-edge-2', 'secret_b': 'banana'}
  ```
  **FAIL — the validator silently accepts a `$facet`-wrapped `$lookup` and the join returns User B's edges (including B's secret tokens) inside a User-A query result.** This is the exact same blocker the PR Reviewer raised, just hiding one level deep behind `$facet`. The fix removed `$lookup` from the top-level allow-list but `_inject_user_id`'s own docstring states it walks the top level only; `$facet` is allow-listed and its sub-pipelines are not validated, so an LLM (or a prompt-injection attack on the LLM) can re-introduce the exact leak the fix was supposed to close.

**Acceptance criteria**
- [x] PASS — Blocker 1 (option b): `$lookup` removed from `_ALLOWED_STAGES`. Evidence: `apps/memory/src/tree/memory/query/nl_query.py:25-43` (the new comment block + the allow-list without `$lookup`); system prompt updated at line 152-159.
- [x] PASS — Unit regression test plants the exact Blocker shape and asserts rejection. Evidence: `apps/memory/tests/unit/memory/query/test_nl_query.py:49-89` (`test_lookup_stage_rejected` + `test_lookup_with_localfield_also_rejected`); proved non-vacuous by backing out the fix (`2 failed` above).
- [ ] **FAIL — Integration regression test for the leak surface is incomplete.** The new `test_nl_query_lookup_stage_is_rejected_no_b_leak` (lines 666-751) covers only **top-level** `$lookup`. It does not exercise `$facet` with a nested `$lookup`, which the e2e adversarial pass proved still leaks at runtime.
      Expected: integration test covering the `$facet → $lookup → no user_id` shape (or, equivalently, validator rejection of any `$lookup` regardless of nesting depth).
      Actual: validator accepts `$facet`-wrapped `$lookup`; runtime returns User-B rows inside a User-A result.
      Fix options (SWE picks):
      (a) Walk into `$facet.*` sub-pipelines inside `validate_pipeline` and re-validate every nested stage against `_ALLOWED_STAGES` (rejects nested `$lookup` and any other smuggled write/eval stage); add the AC-shaped regression test under `test_two_user_isolation.py`.
      (b) Remove `$facet` from `_ALLOWED_STAGES` for the same reason `$lookup` was removed (any sub-pipeline-bearing stage is unsafe under the current top-level-only injector); add a regression test asserting `$facet` is rejected.
      (c) Recursively walk into `$facet.*` sub-pipelines inside `_inject_user_id` too, so any nested `$match`/`$vectorSearch`/`$graphLookup` also gets the tenant predicate (more permissive, more code to maintain).
      The new docstring at `nl_query.py:267-271` explicitly anticipates this class of bug ("Any new stage added to `_ALLOWED_STAGES` that could reach into the `knowledge_graph` collection … MUST also be added here or the tenant-isolation contract is broken") — `$facet` already qualifies and is unhandled.
- [x] PASS — Dimension-comment list at top of `test_two_user_isolation.py` extended (path 15 added at lines 24-28).
- [ ] FAIL — Tester re-runs full QA suite and PASSES — blocked by the leak above. Suites themselves are green but the AC's intent ("no cross-tenant leak via any LLM-shaped pipeline") is not met.
- [ ] Awaiting — PM re-runs acceptance review and ACCEPTS.
- [ ] Awaiting — PR Reviewer re-runs and reports `NO BLOCKERS`.

**Evidence**
```
$ make memory-format-check && make memory-lint-check && make pre-commit
... all green ...

$ make memory-unit-tests
... 832 passed in 43.04s ===

$ make memory-integration-tests
... 119 passed, 12 skipped, 37 deselected in 124.25s ===

$ make memory-integration-tests-ci
... 108 passed, 12 skipped, 48 deselected in 67.88s ===

$ make memory-integration-tests-all
... 156 passed, 12 skipped in 325.59s ===

$ uv run pytest tests/integration/test_two_user_isolation.py --timeout=600
... 17 passed in 86.48s ===

# Non-vacuous verification, fix stashed:
$ uv run pytest tests/unit/memory/query/test_nl_query.py -k lookup -v
... 2 failed, 2 passed (the new tests fail with DID NOT RAISE) ...

$ uv run pytest tests/integration/...test_nl_query_lookup_stage_is_rejected_no_b_leak --tb=short
... FAILED — AssertionError: LEAK — token 'badger' from User B appears in a User-A query result ...

# Adversarial pass, $facet wrapping $lookup, seeded two-tenant DB:
validate accepted; safe = [{'$match': {'kind': 'node', 'user_id': <user_a>}},
                            {'$facet': {'leaked': [
                                {'$lookup': {'from': 'knowledge_graph',
                                             'pipeline': [{'$match': {'kind': 'edge'}}],
                                             'as': 'all'}},
                                {'$limit': 5}]}},
                            {'$limit': 5},
                            {'$project': {'embedding': 0}}]
top-level docs: 1
  LEAK -> {'_id': '...:e1', 'user_id': <user_b>, 'kind': 'edge', 'name': 'b-edge-1', 'secret_b': 'badger'}
  LEAK -> {'_id': '...:e2', 'user_id': <user_b>, 'kind': 'edge', 'name': 'b-edge-2', 'secret_b': 'banana'}
** FACET+LOOKUP LEAKS B DATA TO A **
```

**Other issues found (non-blocking; flagged for follow-up)**
- The Prefect-shutdown `ValueError: I/O operation on closed file.` traceback noted by SWE is reproducible and noisy but does not affect outcomes — agreed it is pre-existing and out of scope.
- `_inject_user_id` docstring at `nl_query.py:267-271` already anticipates the failure class found above, which strengthens the case for fix option (a) or (b) — the contract is already written; the implementation just needs to match.

**VERDICT: FAIL**

Reason: the headline guarantee of this rollup ("no cross-tenant leak via any LLM-shaped pipeline") is not met. `$facet` is allow-listed and its sub-pipelines are not validated, so a `$facet` wrapping a `$lookup` slips through the new check and returns User-B rows inside a User-A result. Adversarial test seeded a two-tenant DB and observed the leak directly (User B's tokens `badger` and `banana` surfaced in User A's query result). Suites are green and the original Blocker is fixed for the top-level surface, but the same class of bug remains one level deep. SWE should pick fix option (a), (b), or (c) above and add the matching regression test, then re-submit.

---

## SWE Log — Cycle 2 (2026-05-17)

### [SWE] 2026-05-17 — Implementation (cycle 2: $facet leak)

**Summary**
Cycle 1 closed the top-level `$lookup` blocker but the Tester's adversarial e2e
pass found the same class of leak still worked via `$facet`, which is in
`_ALLOWED_STAGES` and whose per-field sub-pipelines are never walked by
`_inject_user_id`. Applied the same shape of fix as cycle 1: remove
sub-pipeline-bearing stages from the allow-list rather than recursively
validate them. Audited the full allow-list — `$facet` was the only one
present that carries a sub-pipeline (`$unionWith` was already absent).

**Audit of `_ALLOWED_STAGES` for sub-pipeline-bearing stages**
- `$lookup` — removed in cycle 1.
- `$facet` — REMOVED in cycle 2 (planted leak proved by Tester).
- `$unionWith` — was already absent from the allow-list; regression test
  pins that decision so a future maintainer can't quietly add it.
- `$graphLookup` — kept. It does NOT carry a sub-pipeline; it has a flat
  `restrictSearchWithMatch` predicate that `_inject_user_id` enforces.
- Every other stage in the allow-list (`$vectorSearch`, `$match`, `$project`,
  `$sort`, `$limit`, `$skip`, `$count`, `$group`, `$unwind`, `$addFields`,
  `$sample`, `$sortByCount`, `$bucket`, `$bucketAuto`, `$redact`) is flat —
  none carries a nested `pipeline` array or per-field sub-pipelines.

Nothing else found beyond `$facet` (and `$unionWith`, which was already absent).

**Files modified**
- `apps/memory/src/tree/memory/query/nl_query.py`
  - Removed `$facet` from `_ALLOWED_STAGES`.
  - Rewrote the comment above `_ALLOWED_STAGES` to enumerate every
    sub-pipeline-bearing stage (`$lookup`, `$facet`, `$unionWith`) with the
    specific leak shape each one enables, and to spell out the contract:
    flat stages only, no nested `pipeline`.
  - Updated `_inject_user_id` docstring to:
    * drop the stale "$facet is safe because it only transforms upstream
      docs" claim (it was wrong — $facet sub-pipelines can carry $lookup),
    * list every removed sub-pipeline-bearing stage with the concrete leak
      shape it enables (including the adversarial $facet+$lookup shape the
      Tester planted),
    * lay out the maintainer contract: any new allow-listed stage must be
      flat, and the preferred fix for any future sub-pipeline-bearing stage
      is to omit it from the allow-list (low blast radius), not to teach
      this function to walk it.
  - Updated the system prompt to forbid `$lookup`, `$facet`, `$unionWith`
    explicitly and steer the LLM toward `$graphLookup` for joins,
    separate flat queries instead of `$facet`/`$unionWith`, and "keep
    pipelines flat: no nested `pipeline` arrays anywhere".
- `apps/memory/tests/unit/memory/query/test_nl_query.py`
  - Added `test_facet_stage_rejected` planting the exact Tester-found shape
    (`[$match, $facet{leaked: [$lookup{pipeline: [$match {kind: edge}]}]}, $limit]`).
  - Added `test_unionwith_stage_rejected` pinning that `$unionWith` is and
    stays rejected.
- `apps/memory/tests/integration/test_two_user_isolation.py`
  - Extended the module docstring to document path 16: NL query with
    `$facet` wrapping `$lookup` rejected by `validate_pipeline`.
  - Added `test_nl_query_facet_wrapped_lookup_is_rejected_no_b_leak`
    mirroring the existing `$lookup` regression test: the fake LLM emits
    the planted `$facet+$lookup` shape first, the validator rejects it,
    the retry yields a clean flat pipeline, and the final result set is
    asserted free of B rows / B tokens in BOTH the top-level documents
    and any nested array. Also asserts `llm.call_count == 2` to prove
    the rejection actually drove a retry (not a vacuous pass).

**Test names added**
- Unit: `TestValidatePipeline::test_facet_stage_rejected`
- Unit: `TestValidatePipeline::test_unionwith_stage_rejected`
- Integration: `TestTwoUserIsolation::test_nl_query_facet_wrapped_lookup_is_rejected_no_b_leak`

**TDD discipline**
- Wrote both unit regression tests FIRST. Ran them against unmodified
  code: `test_facet_stage_rejected` failed with `DID NOT RAISE
  PipelineValidationError` (red, for the right reason — the planted
  pipeline passed validation). `test_unionwith_stage_rejected` already
  passed (since `$unionWith` was never in the allow-list).
- Then removed `$facet` from `_ALLOWED_STAGES`. Re-ran: both tests pass
  (green).

**QA loop**
```
$ make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check && make pre-commit
218 files left unchanged
All checks passed!
218 files already formatted
All checks passed!
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed
KGQuery discipline (memory)..............................................Passed

$ make memory-unit-tests
... 834 passed in 41.26s ...

$ uv --directory apps/memory run pytest tests/integration/test_two_user_isolation.py -v
... 18 passed in 85.18s ...
```

(The trailing Prefect `ValueError: I/O operation on closed file.` traceback
is the same pre-existing teardown-race noise the Tester already flagged
as out of scope; not a test failure.)

**Acceptance criteria — cycle 2**
- [x] Audit `_ALLOWED_STAGES` for sub-pipeline-bearing stages — done;
      `$facet` removed, `$unionWith` confirmed-absent, everything else flat.
- [x] Apply the same shape of fix as cycle 1: remove from allow-list,
      update prompt + docstring to be concrete.
- [x] Regression unit tests `test_facet_stage_rejected` and
      `test_unionwith_stage_rejected` added.
- [x] Regression integration test
      `test_nl_query_facet_wrapped_lookup_is_rejected_no_b_leak` added,
      mirrors the existing `$lookup` test, plants the exact Tester shape,
      and asserts the validator rejects + the LLM is re-prompted.
- [x] Module-docstring path enumeration extended (path 16).
- [x] Local QA loop clean (format/lint/pre-commit/unit) and touched
      integration green.
- [ ] Tester re-runs and PASSES.

**Notes**
- The hard architectural decision (recursive sub-pipeline validation vs
  flat-stages-only allow-list) was already chosen in cycle 1 — flat
  allow-list. Sticking to it in cycle 2 keeps consistency, keeps the
  validator simple, and makes the maintainer contract easy to express
  ("no nested `pipeline` arrays anywhere"). The system-prompt and
  docstring updates make that contract loud so a future maintainer
  doesn't quietly re-add `$facet` or `$unionWith` for "ergonomics".
- DID NOT commit. Tester goes again.

### [Tester] 2026-05-17 20:10 — QA (rollup, cycle 2)

**Test summary (wall-time matrix)**
- `make memory-format-check` — PASS (218 files formatted)
- `make memory-lint-check` — PASS (ruff: all checks passed)
- `make pre-commit` — PASS (prettier / ruff check / ruff format / biome / KGQuery discipline all green)
- `make memory-unit-tests` — PASS, **834 passed in 42.27s, 0 warnings**
- `make memory-integration-tests` (fast subset) — PASS, **119 passed, 12 skipped, 38 deselected in 122.82s**
- `make memory-integration-tests-ci` (CI-mirror, no mongot) — PASS, **108 passed, 12 skipped, 49 deselected in 67.76s**
- `make memory-integration-tests-all` (full incl. slow + mongot) — PASS, **157 passed, 12 skipped in 304.15s**

**Non-vacuousness verification of the cycle-2 fix (the $facet leak)**
Per orchestrator instruction, `git stash push apps/memory/src/tree/memory/query/nl_query.py` reverted the cycle-2 fix (and the cycle-1 fix together — the file was pre-rollup, so both `$lookup` and `$facet` were back in `_ALLOWED_STAGES`). Re-ran the new regressions against the backed-out file:

- Unit, fix backed out: `uv --directory apps/memory run pytest tests/unit/memory/query/test_nl_query.py -k "facet or unionwith" -v` →
  `FAILED test_facet_stage_rejected` — `Failed: DID NOT RAISE PipelineValidationError` (the planted `$facet+$lookup` shape validated cleanly, confirming the test would have caught the regression).
  `PASSED test_unionwith_stage_rejected` — `$unionWith` was never in the allow-list, so the pinning test is green in both states. That's its intended purpose: it pins the absence, not the cycle-2 removal. Flagged as a Nit below.
  (`1 failed, 1 passed, 37 deselected in 0.21s`)
- Integration, fix backed out: `uv --directory apps/memory run pytest tests/integration/test_two_user_isolation.py::TestTwoUserIsolation::test_nl_query_facet_wrapped_lookup_is_rejected_no_b_leak --timeout=300` → `FAILED in 15.66s`. The failure mode is `KeyError: 'user_id'` raised inside `_assert_no_b_rows`, because the leaked `$facet` output is shaped `{"leaked": [...]}` with no top-level `user_id` field — so the helper crashes before it can emit a clean `LEAK -> ...` message. The test still fails non-vacuously (would catch the regression), but the failure message is opaque. Logged as Nit 2 below.
- After `git stash pop`, re-ran both selectors with the fix in place: `2 passed` / `1 passed`. The fix is required for both to be green.

**Audit of `_ALLOWED_STAGES` for hidden cross-tenant escape hatches**
Walked every stage remaining in the allow-list and cross-referenced Mongo's reference (`https://www.mongodb.com/docs/manual/reference/operator/aggregation-pipeline/`). The cycle-2 SWE summary is correct that none of the remaining stages carry a nested `pipeline` argument or per-field sub-pipelines. Specific scrutiny:
- `$group` — no sub-pipeline. Accumulators like `$accumulator` and `$function` would be code-execution vectors, but they are NOT in any allow-list inside `_ALLOWED_STAGES` (the validator only inspects stage names, not accumulator expressions). However, `validate_pipeline` does not lint expression-level operators inside `$group` either — so an LLM emitting `{"$group": {"_id": null, "x": {"$accumulator": {...}}}}` would currently pass the stage-name check and reach Mongo. Mongo's server-side `$accumulator` requires `$function` JS execution which is disabled by default in our deployment, so this is theoretical, but the validator doesn't enforce it. Recording as Nit 3.
- `$bucket` / `$bucketAuto` — no sub-pipeline; the `output` field takes accumulator expressions (same theoretical `$accumulator`/`$function` shape as above).
- `$redact` — uses `$$DESCEND` / `$$PRUNE` / `$$KEEP` system variables; no foreign-collection reach. Confirmed flat.
- `$vectorSearch` — has a flat `filter` that `_inject_user_id` overwrites.
- `$graphLookup` — has flat `restrictSearchWithMatch` that `_inject_user_id` overwrites; `from` is checked === `knowledge_graph`.
- `$match`, `$project`, `$sort`, `$limit`, `$skip`, `$count`, `$unwind`, `$addFields`, `$sample`, `$sortByCount` — flat, no sub-pipelines.

Nothing else carries a sub-pipeline. Cycle-2 fix is correct for the sub-pipeline-bearing class of leak.

**E2E adversarial pass — Blocker found**
Spun up a throwaway DB (`tester_adversarial_first_stages`) with two tenants A and B, each holding two nodes tagged with distinct secrets (`A_apples`/`A_apricot` for A; `B_badger`/`B_banana` for B). Pushed each candidate pipeline through `validate_pipeline(..., user_a)` and executed against the actual `knowledge_graph` collection.

The new break paths I tried (in addition to the spec's three):

- **`$expr` inside `$match`** — `[{"$match": {"$expr": {"$eq": ["$type", "person"]}, "kind": "node"}}, {"$limit": 10}]`. `_inject_user_id` correctly adds `user_id` alongside `$expr` in the same `$match` predicate; runtime returned only A's docs. PASS.
- **`$match` with attacker-supplied `user_id: {$ne: <bound>}`** — `[{"$match": {"user_id": {"$ne": A}, "kind": "node"}}, {"$limit": 10}]`. The validator's spread-then-key pattern (`{**match, "user_id": user_id}`) OVERWRITES the LLM's `$ne` with the bound id. Runtime returned only A's docs. PASS.
- **`$vectorSearch.filter` with attacker-supplied `user_id: <victim>`** — overwritten by `_inject_user_id`. PASS.
- **`$graphLookup.restrictSearchWithMatch` with attacker-supplied `user_id: <victim>`** — overwritten by `_inject_user_id`. PASS.
- **`$group` placement** — when `$match` precedes `$group`, the injected filter sits in front. PASS.
- **`$group` as the first stage with NO `$match`** — `[{"$group": {"_id": "$type", "items": {"$push": "$$ROOT"}}}, {"$limit": 10}]`. **LEAK observed.** `_inject_user_id` walks the existing stages and modifies only `$match`/`$vectorSearch`/`$graphLookup` that are present — it does NOT prepend a `$match` if none exists. The pipeline runs `$group` against the unfiltered collection. Runtime returned User B's `B_badger` and `B_banana` rows inside the `items` array of the cross-tenant group. **FAIL.**
- **Other "no leading `$match`" first stages — confirmed leak surface (same root cause):**
  - `[{"$sample": {"size": 10}}, {"$limit": 10}]` → returned both B's nodes (`B_badger`, `B_banana`).
  - `[{"$sort": {"name": 1}}, {"$limit": 10}]` → returned both B's nodes (clean sorted output, cross-tenant).
  - `[{"$project": {"secret": 1, "user_id": 1}}, {"$limit": 10}]` → returned B's secrets directly.
  - `[{"$unwind": "$tags"}, {"$limit": 10}]` → returned B's nodes with tags unwound.
  - `[{"$bucket": {"groupBy": "$score", "boundaries": [0, 5, 10], "output": {"items": {"$push": "$$ROOT"}}}}, {"$limit": 10}]` → cross-tenant rows in the bucket's `items` array.
  - `[{"$count": "total"}]` → returned `total=4` instead of `total=2` (count of B's tenant included).
  - `[{"$sortByCount": "$user_id"}, {"$limit": 10}]` → returned a grouping over `$user_id` that reveals B's tenant exists (`{_id: A, count: 2}, {_id: B, count: 2}`).

The pattern is consistent: **ANY pipeline that does not start with `$match` or `$vectorSearch` (the only two stages that produce the initial tenant filter — `$graphLookup` requires a seed which itself comes from an upstream stage) executes its first stage against the unfiltered collection.** The current `_inject_user_id` is "modify-existing" semantics; it needs "ensure-present" semantics — either prepend `{"$match": {"user_id": user_id}}` when no leading filter exists, or reject the pipeline.

This is the same class of bug the cycle-2 SWE log warns against ("any new stage added to `_ALLOWED_STAGES` must… not reach into the `knowledge_graph` collection on its own"). The bug isn't about new stages — it's about EXISTING stages running before the injection happens. The docstring at `nl_query.py:281-287` claims "Every other allow-listed stage … cannot reach across tenants because they only transform documents already filtered upstream." That claim is false when the pipeline lacks any upstream filter at all.

**Caller-side validation**
Confirmed via `grep -rn '\$facet\|\$lookup\|\$unionWith' apps/memory/src apps/memory/tests --include='*.py'` that no production code path emits these stages (only the regression tests + the docstring + the system prompt mention them). The remaining `$lookup` references in `apps/memory/src/tree/memory/review/core.py`, `apps/memory/src/tree/memory/extraction/dedup.py`, and `apps/memory/src/tree/mcp/tools.py` are raw-pymongo aggregations that thread `user_id` directly into every `$lookup.pipeline` `$match` they emit — they do not pass through `_ALLOWED_STAGES` and are allow-listed in the KGQuery discipline check.

**Acceptance criteria**
- [x] PASS — `$facet` removed from `_ALLOWED_STAGES`. Evidence: `apps/memory/src/tree/memory/query/nl_query.py:47-66` (allow-list without `$facet`); `apps/memory/src/tree/memory/query/nl_query.py:34-39` (block comment documenting the cycle-2 fix).
- [x] PASS — `$unionWith` confirmed absent from `_ALLOWED_STAGES`; regression test pins it. Evidence: `apps/memory/tests/unit/memory/query/test_nl_query.py:125-146` (`test_unionwith_stage_rejected`).
- [x] PASS — Unit regression `test_facet_stage_rejected` plants the Tester-found shape and is non-vacuous (failed with `DID NOT RAISE` when fix was backed out). Evidence: `apps/memory/tests/unit/memory/query/test_nl_query.py:91-123`.
- [x] PASS — Integration regression `test_nl_query_facet_wrapped_lookup_is_rejected_no_b_leak` exercises the full `execute_nl_query` path with a fake LLM emitting `$facet+$lookup`, asserts the validator rejects + the LLM is re-prompted (call_count == 2). Evidence: `apps/memory/tests/integration/test_two_user_isolation.py:764-847`. Non-vacuous: failed with `KeyError: 'user_id'` when fix was backed out (would catch a regression, though the message is opaque — see Nit 2).
- [x] PASS — System prompt + `_inject_user_id` docstring rewritten to enumerate every sub-pipeline-bearing stage and the leak shape it enables. Evidence: `apps/memory/src/tree/memory/query/nl_query.py:172-187` (system prompt safety rules) and `apps/memory/src/tree/memory/query/nl_query.py:267-311` (`_inject_user_id` docstring).
- [x] PASS — Module-docstring dimension list extended (path 16). Evidence: `apps/memory/tests/integration/test_two_user_isolation.py:28-34`.
- [ ] **FAIL — Headline guarantee ("no cross-tenant leak via any LLM-shaped pipeline") is still not met.** The cycle-2 fix closed the sub-pipeline-bearing class of leak, but my adversarial pass surfaced a NEW class: any pipeline that does not start with `$match` (or `$vectorSearch`) skips the tenant filter entirely. Confirmed at runtime with `$group`, `$sample`, `$sort`, `$project`, `$unwind`, `$bucket`, `$count`, `$sortByCount` as the first stage — every one returns B-tenant rows under an A-user query.
      Expected: every emitted pipeline either starts with a tenant-scoping stage (`$match` with `user_id`, or `$vectorSearch` whose `filter` gets `user_id` injected), or `validate_pipeline` prepends `{"$match": {"user_id": <bound>}}` as the first stage, OR rejects the pipeline.
      Actual: `_inject_user_id` is "modify-existing" only. Pipelines without a leading `$match`/`$vectorSearch` run their first stage against the unfiltered collection. Concrete demo:
      ```
      pipeline = [{"$group": {"_id": "$type", "items": {"$push": "$$ROOT"}}}, {"$limit": 10}]
      safe = validate_pipeline(pipeline, user_a)
      # safe = [{'$group': ...}, {'$limit': 10}, {'$project': {'embedding': 0}}]
      # No $match injected.
      runtime → [{'_id': 'person', 'items': [..A.., ..B..]}, {'_id': 'task', 'items': [..A.., ..B..]}]
      ```
      Fix options (SWE picks one):
      (a) Prepend `{"$match": {"user_id": user_id}}` as the first stage of `safe_pipeline` IFF the pipeline doesn't already begin with a `$match` (whose injected `user_id` we trust) or `$vectorSearch` (whose `filter` is injected with `user_id`). Cheapest and most surgical; correct by construction.
      (b) Make `validate_pipeline` reject any pipeline whose first stage isn't `$match` or `$vectorSearch`. Loud failure for the LLM to recover from on retry; requires a system-prompt update too.
      (c) Walk every stage in `_inject_user_id` and inject `user_id` into every accumulator/output expression. High blast radius, not recommended.
      Regression test (required): plant `[{"$group": ...}, {"$limit": 10}]` against a two-tenant DB and assert no B rows surface in `items`. Mirror tests for `$sort`, `$project`, `$sample`, `$unwind`, `$bucket` first-stage shapes (a parametrize will keep it tight). The `nl_query.py:281-287` docstring claim about "filtered upstream" must be reconciled with whichever fix is applied. The dimension-comment list at the top of `tests/integration/test_two_user_isolation.py` must gain a new path 17 row for "NL query with first stage other than `$match`/`$vectorSearch`".
- [ ] FAIL — Tester re-runs full QA suite and PASSES — blocked by the leak above. Suites are green but the AC's intent is not met.
- [ ] Awaiting — PM re-runs acceptance review and ACCEPTS.
- [ ] Awaiting — PR Reviewer re-runs and reports `NO BLOCKERS`.

**Evidence**
```
$ make memory-format-check && make memory-lint-check && make pre-commit
... all green ...

$ make memory-unit-tests
... 834 passed in 42.27s ...

$ make memory-integration-tests
... 119 passed, 12 skipped, 38 deselected in 122.82s ...

$ make memory-integration-tests-ci
... 108 passed, 12 skipped, 49 deselected in 67.76s ...

$ make memory-integration-tests-all
... 157 passed, 12 skipped in 304.15s ...

# Non-vacuous verification, fix stashed (file reverted to pre-cycle-1 state, $lookup AND $facet both back in allow-list):
$ uv --directory apps/memory run pytest tests/unit/memory/query/test_nl_query.py -k "facet or unionwith" -v
... 1 failed (test_facet_stage_rejected: DID NOT RAISE), 1 passed (test_unionwith pins absence; non-vacuous only in concept), 37 deselected ...

$ uv --directory apps/memory run pytest tests/integration/test_two_user_isolation.py::TestTwoUserIsolation::test_nl_query_facet_wrapped_lookup_is_rejected_no_b_leak --timeout=300
... 1 failed in 15.66s — KeyError: 'user_id' (B rows in $facet output dict have no top-level user_id key; the LEAK assertion never runs because the helper crashes first) ...

# After fix restored:
$ uv --directory apps/memory run pytest tests/unit/memory/query/test_nl_query.py -k "facet or unionwith" -v
... 2 passed in 0.26s ...
$ uv --directory apps/memory run pytest tests/integration/test_two_user_isolation.py::TestTwoUserIsolation::test_nl_query_facet_wrapped_lookup_is_rejected_no_b_leak --timeout=300
... 1 passed in 17.95s ...

# Adversarial pass, no-leading-$match against seeded two-tenant DB:
[$group first]    LEAK -> B_badger, B_banana surface inside items array
[$sample first]   LEAK -> B's two nodes returned directly
[$sort first]     LEAK -> B's two nodes returned directly
[$project first]  LEAK -> B's two nodes with selected fields
[$unwind first]   LEAK -> B's two nodes with tags unwound
[$bucket first]   LEAK -> B's nodes in bucket items
[$count first]    LEAK -> total=4 (correct for A would be 2)
[$sortByCount on $user_id] LEAK -> reveals B's tenant id and node count
```

**Other issues found (Nits — non-blocking, flagged for follow-up)**
- **Nit 1 (test quality):** `test_unionwith_stage_rejected` is described as "belt-and-braces" but it passes in BOTH the with-fix and without-fix state (because `$unionWith` was never in the allow-list to begin with). It pins the decision against a future maintainer who tries to add it, which is valuable, but it's not a non-vacuous regression test for the cycle-2 work. Worth mentioning in the test docstring.
- **Nit 2 (test diagnostics):** When the integration test `test_nl_query_facet_wrapped_lookup_is_rejected_no_b_leak` fails non-vacuously, it fails with `KeyError: 'user_id'` from `_assert_no_b_rows` because the leaked `$facet` output shape `{"leaked": [...]}` has no top-level `user_id`. The test still fails (good), but the diagnostic is opaque ("KeyError" rather than "LEAK -> token 'badger'"). Consider extending `_assert_no_b_rows` to skip rows that don't have a `user_id` field and add a separate "if no user_id was found in the row, walk all dict-valued children" hook so the failure prints the actual leaked tokens. Non-blocking.
- **Nit 3 (potential code-execution surface):** `validate_pipeline` only checks the top-level stage name, not expression operators inside `$group`/`$bucket`/`$bucketAuto`. The system prompt forbids `$accumulator` and `$function`, but the validator does not enforce it. Mongo's deployment-time JS-execution flag would catch this in production, but the validator should hold the contract too. Non-blocking, but worth a tracker note.

**VERDICT: FAIL**

Reason: the cycle-2 fix correctly closed the sub-pipeline-bearing class of leak (`$facet`, `$unionWith`), and the new regression tests are well-targeted and non-vacuous. But my adversarial pass found a NEW class of leak that's broader in scope: any pipeline whose first stage is not `$match` or `$vectorSearch` runs that stage against the unfiltered collection — `_inject_user_id` only modifies existing tenant-filterable stages, it doesn't prepend one. I confirmed at runtime that `$group`, `$sample`, `$sort`, `$project`, `$unwind`, `$bucket`, `$count`, and `$sortByCount` all leak when used as the first stage. The same headline guarantee that motivated the rollup ("no cross-tenant leak via any LLM-shaped pipeline") is therefore still not met. SWE should pick fix option (a) (prepend a tenant `$match`) or (b) (reject pipelines without a leading `$match`/`$vectorSearch`), add the matching parametrized regression test, then re-submit.

### [SWE] 2026-05-17 20:30 — Implementation (rollup, cycle 3)

**Blocker addressed**
Cycle-2 Tester found `_inject_user_id` had "modify-existing" semantics only — when a pipeline didn't lead with `$match` or `$vectorSearch`, no tenant filter was injected, and the first stage ran against the unfiltered `knowledge_graph` collection. Confirmed runtime leaks for `$group`, `$sample`, `$sort`, `$project`, `$unwind`, `$bucket`, `$count`, and `$sortByCount` as the first stage.

**Fix applied — option (a)**
In `_inject_user_id`, prepend `{"$match": {"user_id": user_id}}` as the new leading stage whenever the pipeline doesn't already lead with `$match` (which is augmented with `user_id` in place) or `$vectorSearch` (which gets `user_id` injected into its `filter` and must remain the first stage). The prepended `$match` flows through the same overwrite loop downstream so the bound tenant id always wins. This is the minimal correct-by-construction change; it doesn't reject any legitimate query shape, and the existing "modify-existing" treatment for downstream `$match`/`$vectorSearch`/`$graphLookup` stays as-is.

**Files modified**
- `apps/memory/src/tree/memory/query/nl_query.py`
  - `_inject_user_id`: added the prepend step (Step 1) that injects `{"$match": {"user_id": user_id}}` as the leading stage when the pipeline doesn't lead with `$match` or `$vectorSearch`. The overwrite loop (Step 2) is unchanged.
  - `_inject_user_id` docstring: rewritten to describe both mechanisms (prepend + overwrite), remove the now-incorrect "filtered upstream" claim about non-walked stages, and spell out why prepending is required for stages that otherwise have nowhere to attach a tenant predicate.
  - `validate_pipeline` docstring: updated to surface the prepend contract (so callers don't have to read `_inject_user_id` to know what `validate_pipeline` guarantees).
  - System prompt: appended a "Tenant scoping is enforced by the server" rule that tells the LLM the server will prepend a leading `$match` and overwrite any `user_id` value it supplies. The LLM may still emit its own leading `$match`, just not try to choose the tenant.
- `apps/memory/tests/unit/memory/query/test_nl_query.py`
  - Added parametrized `test_tenant_match_prepended_when_first_stage_is_not_match_or_vectorsearch` covering `$group`, `$sample`, `$sort`, `$project`, `$unwind`, `$bucket`, `$count`, `$sortByCount`, `$addFields` — asserts the output of `validate_pipeline` starts with `{"$match": {"user_id": <bound>}}`.
  - Added `test_tenant_match_not_prepended_when_first_stage_is_match` — asserts the in-place merge path keeps a single `$match` leader (no redundant prepend).
  - Added `test_tenant_match_not_prepended_when_first_stage_is_vectorsearch` — asserts the `filter` injection path is used and `$vectorSearch` remains the leading stage (Mongo invariant).
- `apps/memory/tests/integration/test_two_user_isolation.py`
  - Added path 17 to the module docstring's dimension list.
  - Added parametrized `test_nl_query_first_stage_without_match_does_not_leak` exercising the full `execute_nl_query` path with `$group`, `$sample`, `$sort`, `$project`, `$unwind`, `$bucket`, `$count`, `$sortByCount` as first stages. Each parametrized case asserts no B rows / B tokens in the top-level results or any nested array (faceted/grouped/bucketed shapes).
  - Hardened `_assert_no_b_rows` to recursively walk dict/list values so leaks inside faceted / grouped / bucketed `items` arrays surface as a clean `LEAK — row from user_id=…` message instead of an opaque `KeyError: 'user_id'` (addresses Tester Nit 2).
  - Hardened `_assert_no_b_tokens` to also check the `secret_b` family of tokens (forward-compat with Tester's adversarial seed shape).

**Test names added**
- Unit: `TestValidatePipeline::test_tenant_match_prepended_when_first_stage_is_not_match_or_vectorsearch[group|sample|sort|project|unwind|bucket|count|sortbycount|addfields]` (9 parametrized cases)
- Unit: `TestValidatePipeline::test_tenant_match_not_prepended_when_first_stage_is_match`
- Unit: `TestValidatePipeline::test_tenant_match_not_prepended_when_first_stage_is_vectorsearch`
- Integration: `TestTwoUserIsolation::test_nl_query_first_stage_without_match_does_not_leak[group|sample|sort|project|unwind|bucket|count|sortbycount]` (8 parametrized cases)

**TDD discipline**
- Wrote the unit parametrized test FIRST. Ran it against the cycle-2 code; all 9 cases failed with `AssertionError: Expected leading tenant $match, got {…}` (red, for the right reason — the validator was returning the pipeline unchanged for leading non-`$match` stages).
- Wrote the integration parametrized test FIRST (added the module-docstring path-17 entry at the same time so the index stayed in sync). Ran one case (`$bucket`) and found the boundary was misconfigured; reshaped to `groupBy: {$strLenCP: ...}` with boundaries `[1, 200]`/default `999` so the executor accepts the pipeline. With that fix in place but the production fix NOT yet applied, the integration cases failed with the leak symptoms.
- Applied the production fix (prepend in `_inject_user_id`). Re-ran: all 11 unit cases green, all 8 integration cases green.

**Non-vacuousness check (back-out verification)**
Stashed `apps/memory/src/tree/memory/query/nl_query.py` via `git stash push` (the file-scoped stash takes the cycle-3 fix out while leaving the regression tests in place):
```
$ uv --directory apps/memory run pytest \
    tests/unit/memory/query/test_nl_query.py::TestValidatePipeline::test_tenant_match_prepended_when_first_stage_is_not_match_or_vectorsearch -v
9 failed in 0.18s
  — every parametrized case fails with `AssertionError: Expected leading tenant $match, got {<first_stage>}`

$ uv --directory apps/memory run pytest \
    tests/integration/test_two_user_isolation.py::TestTwoUserIsolation::test_nl_query_first_stage_without_match_does_not_leak --timeout=600
5 failed, 3 passed in 37.85s
  — `$group`, `$sample`, `$sort`, `$project`, `$bucket` all fail with a clean
    `LEAK — row from user_id=<B's id> surfaced in a User-A query: {<B row>}`
    message (the hardened `_assert_no_b_rows` walks the nested arrays now).
  — `$unwind` happens to surface zero rows in this test DB (B's nodes have no
    `tags` field, so `$unwind` produces an empty stream), and `$count` /
    `$sortByCount` aggregate the leak into a scalar/grouping output whose
    shape doesn't carry B's tenant id where the integration helpers look —
    those three are tightly covered at the unit level instead (the unit
    parametrized test asserts the leading stage IS a tenant `$match`, which
    is the actual contract being enforced).
```
After `git stash pop`, all 17 (9 unit + 8 integration) pass:
```
$ uv --directory apps/memory run pytest \
    tests/unit/memory/query/test_nl_query.py::TestValidatePipeline::test_tenant_match_prepended_when_first_stage_is_not_match_or_vectorsearch \
    tests/integration/test_two_user_isolation.py::TestTwoUserIsolation::test_nl_query_first_stage_without_match_does_not_leak --timeout=600
17 passed in 34.78s
```

**QA loop**
```
$ make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check && make pre-commit
218 files left unchanged / 218 files already formatted / All checks passed
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed
KGQuery discipline (memory)..............................................Passed

$ make memory-unit-tests
... 845 passed in 40.83s ...

$ uv --directory apps/memory run pytest tests/integration/test_two_user_isolation.py --timeout=600
... 26 passed in 79.67s ...

$ make memory-integration-tests
... 119 passed, 12 skipped, 46 deselected in 116.84s ...

$ make memory-integration-tests-ci
... 108 passed, 12 skipped, 57 deselected in 67.35s ...
```

**Acceptance criteria — cycle 3**
- [x] `validate_pipeline` / `_inject_user_id` now prepends `{"$match": {"user_id": user_id}}` as the leading stage when the pipeline doesn't lead with `$match` or `$vectorSearch`. Evidence: `apps/memory/src/tree/memory/query/nl_query.py` Step 1 in `_inject_user_id`.
- [x] Unit regression `test_tenant_match_prepended_when_first_stage_is_not_match_or_vectorsearch` added (parametrized across the Tester-listed first stages) — non-vacuous (9 failures when the fix is backed out).
- [x] Integration regression `test_nl_query_first_stage_without_match_does_not_leak` added (parametrized across `$group`, `$sample`, `$sort`, `$project`, `$unwind`, `$bucket`, `$count`, `$sortByCount`) — non-vacuous (5 of 8 cases fail with clean `LEAK` messages when the fix is backed out; the other 3 are aggregate-output shapes whose contract is enforced via the unit parametrized test).
- [x] Path 17 added to the integration-test module-docstring dimension list.
- [x] System prompt updated to declare the server enforces tenant scoping (prepend + overwrite) — the LLM should never try to choose the tenant.
- [x] `_inject_user_id` docstring rewritten — the false "filtered upstream" claim removed, the prepend mechanism explained, contract preserved for downstream maintainers.
- [x] Tester Nit 2 addressed — `_assert_no_b_rows` recursively walks dict/list values, so faceted/grouped/bucketed leaks surface as `LEAK — row from user_id=…` instead of `KeyError: 'user_id'`.
- [x] Local QA loop clean (format/lint/pre-commit/unit). Touched integration suite green (`test_two_user_isolation`, fast integration loop, CI-mirror loop).
- [ ] Tester re-runs and PASSES.

**Notes**
- **Cycle-2 fix preserved.** The `$facet`/`$unionWith` allow-list removal stays as-is; the cycle-3 change is purely additive in `_inject_user_id`.
- **Tester Nit 1 (cycle 2) — `test_unionwith_stage_rejected` "passes in both states"**: not addressed in code. The existing docstring already calls out that the test pins the absence, not a removal. Re-reading the orchestrator instructions, this is explicitly "defer / don't address now" — leaving as-is.
- **Tester Nit 3 (cycle 2) — expression-level validator for `$accumulator`/`$function`**: not addressed in code per the orchestrator's explicit "defer / don't address now" instruction (Mongo's deployment-time JS flag is off; theoretical risk only). Should be filed as a follow-up tracker item by the PM.
- DID NOT commit. Tester goes again.

### [Tester] 2026-05-17 21:45 — QA (rollup, cycle 3)

**Test summary (wall-time matrix)**
- `make memory-format-check` — PASS (218 files already formatted)
- `make memory-lint-check` — PASS (ruff: all checks passed)
- `make pre-commit` — PASS (prettier / ruff check / ruff format / biome / KGQuery discipline all green)
- `make memory-unit-tests` — PASS, **845 passed in 41.22s, 0 warnings** (matches SWE's claim of 845 / ≈42s)
- `make memory-integration-tests` (fast subset) — PASS, **119 passed, 12 skipped, 46 deselected in 114.64s** (matches SWE's claim of 119 / ≈2:00)
- `make memory-integration-tests-ci` (CI-mirror, no mongot) — PASS, **108 passed, 12 skipped, 57 deselected in 65.38s** (matches SWE's claim of 108 / ≈66s)
- `tests/integration/test_two_user_isolation.py` (targeted) — PASS, **26 passed in 88.63s** after restoring fix.

**Non-vacuousness verification (back-out + restore)**
Per orchestrator instruction, independently re-verified the SWE's claim. Stashed `apps/memory/src/tree/memory/query/nl_query.py` via `git stash push -- apps/memory/src/tree/memory/query/nl_query.py` (note: this stash reverts the file all the way to pre-rollup, so cycle-1 `$lookup` and cycle-2 `$facet` allow-list removals AND the cycle-3 prepend all come out together). With the fix backed out:

- Unit: `uv --directory apps/memory run pytest tests/unit/memory/query/test_nl_query.py::TestValidatePipeline::test_tenant_match_prepended_when_first_stage_is_not_match_or_vectorsearch tests/unit/memory/query/test_nl_query.py::TestValidatePipeline::test_tenant_match_not_prepended_when_first_stage_is_match tests/unit/memory/query/test_nl_query.py::TestValidatePipeline::test_tenant_match_not_prepended_when_first_stage_is_vectorsearch -v` → **9 failed, 2 passed** in 0.19s. Every parametrized first-stage shape (`group`, `sample`, `sort`, `project`, `unwind`, `bucket`, `count`, `sortbycount`, `addfields`) fails with `AssertionError: Expected leading tenant $match, got {<first_stage>}`. The two not-prepended tests (`first_stage_is_match`, `first_stage_is_vectorsearch`) stay green because their contract (in-place merge / `$vectorSearch.filter` overwrite) is unchanged. Non-vacuousness CONFIRMED.

- Integration: `uv --directory apps/memory run pytest tests/integration/test_two_user_isolation.py::TestTwoUserIsolation::test_nl_query_first_stage_without_match_does_not_leak --timeout=300` → **5 failed, 3 passed** in 44.06s. The 5 cases that fail (`group`, `sample`, `sort`, `project`, `bucket`) all surface clean `LEAK — row from user_id=<B's id>` messages thanks to the hardened `_assert_no_b_rows`. The 3 cases that "pass" while the fix is backed out (`unwind`, `count`, `sortbycount`) do so because their output shape doesn't carry a top-level `user_id` field that the integration helpers can grep for — `$unwind` over `$properties.aliases` produces an empty stream in this seed DB (B's nodes have no aliases), `$count` returns `[{"total": N}]`, and `$sortByCount: "$user_id"` returns `[{"_id": <some_uid>, "count": N}]`. **Critically, all three of these shapes ARE caught at the unit level** — confirmed above (`group`, `sample`, `sort`, `project`, `unwind`, `bucket`, `count`, `sortbycount`, `addfields` are all in the parametrized unit test, and all 9 went red when the fix was backed out). The unit-level contract ("`validate_pipeline` MUST return a pipeline whose leading stage is `{"$match": {"user_id": <bound>}}`") is the actual guarantee being enforced; the integration test is a runtime sanity check for the shapes where a row-level leak can be observed. SWE's claim of "the other 3 — `$unwind`/`$count`/`$sortByCount` — emit aggregate-output shapes; their contract is enforced at unit level" CONFIRMED.

After `git stash pop`, re-ran every cycle-3 selector: **19 passed in 38.68s** (11 unit + 8 integration). The fix is required for every one to be green.

**E2E adversarial pass — break-it attempts against the cycle-3 fix**

Wrote `/tmp/tester_cycle3_adversarial.py` to push each spec-listed break path through `validate_pipeline(..., user_a)` (logic-level), and `/tmp/tester_case{2,4,8}_runtime.py` to push the borderline cases through real MongoDB with a seeded two-tenant collection. Results:

- **[1] EMPTY pipeline `[]`** — `validate_pipeline([], USER_A)` raises `PipelineValidationError: Pipeline is empty`. Existing `test_empty_pipeline_raises` covers this. No silent pass-through, no prepend on empty. PASS.

- **[2] `$match: {$or: [{user_id: A}, {user_id: B}]}` as first stage** — validator output: `{"$match": {"$or": [...], "user_id": A}}` (the spread overwrite ADDS `user_id=A` alongside the `$or`). Mongo evaluates `$match` as AND of all top-level keys, so `user_id=A AND (user_id=A OR user_id=B)` simplifies to `user_id=A`. **Confirmed at runtime**: seeded DB with 2 A-rows + 2 B-rows, ran pipeline as user A → returned exactly 2 A-rows, 0 B-rows. Also tested `$nor: [{user_id: A}]` (the inverse smuggling shape) → returns 0 rows because `user_id=A AND NOT(user_id=A)` is empty. PASS — the `$or`/`$nor` is dead under the forced AND. Safe by Mongo's semantics, not by the validator's awareness, but the property is robust.

- **[3] `$vectorSearch` first, `filter` omits `user_id`** — prepend correctly SKIPPED (Mongo requires `$vectorSearch` to be the first stage; a prepended `$match` would break the pipeline). The downstream `_inject_user_id` Step 2 patches `$vectorSearch.filter` with `{**existing_filter, "user_id": user_id}` → final filter is `{"kind": "node", "user_id": A}`. PASS.

- **[4] TWO `$match` stages back-to-back** — first stage gets `user_id=A` via in-place spread merge (no prepend). Second `$match` ALSO walked by Step 2 loop and gets `user_id=A` (overwriting the attacker's `USER_B`). **Confirmed at runtime**: pipeline `[{$match: {kind: node}}, {$match: {user_id: B}}, {$limit: 10}]` with 1 A-row + 1 B-row seeded → returned 1 doc, owner A. PASS.

- **[5] `$facet` rejection (cycle-2)** — `validate_pipeline` raises `PipelineValidationError: Stage '$facet' is not allowed`. Cycle-2 fix intact. PASS.

- **[6] `$lookup` rejection (cycle-1)** — `validate_pipeline` raises `PipelineValidationError: Stage '$lookup' is not allowed`. Cycle-1 fix intact. PASS.

- **[7] Pipeline starts with `$limit` only** — prepend fires; validator output: `[{"$match": {"user_id": A}}, {"$limit": 5}, {"$project": {"embedding": 0}}]`. Covered indirectly by the unit `$count` / `$sortByCount` cases. PASS.

- **[8] `$match` with non-dict value (`"not-a-dict"`, `[...]`, `None`)** — the first-stage check at `nl_query.py:347-349` looks at the stage NAME (`$match`), so `needs_prepend = False`, and the Step 2 loop sees `not isinstance(match, dict)` and passes the stage through untouched. **Validator output therefore lacks a tenant filter.** Pushed each of the three shapes to real Mongo: all three raise `OperationFailure: the match filter must be an expression in an object` (Location15959) — Mongo rejects the pipeline before any data is returned. **No leak.** Recording as Nit 1 below: validator-level defense-in-depth gap (the first-stage check should also verify `isinstance(match_value, dict)` before suppressing the prepend), but no proven exploit — Mongo backstops it.

- **[9] `$match` first stage with attacker-supplied `user_id=B` already present** — Step 2 spread `{**match, "user_id": user_id}` puts `user_id=A` LAST, which overwrites the attacker's `user_id=B` (Python dict semantics). Validator output: `{"$match": {"user_id": A, "kind": "node"}}`. PASS.

**Audit of `_inject_user_id` Step 1 branch — what could skip the prepend?**

The check at line 348 is `first_stage_keys[0] in ("$match", "$vectorSearch")`. The "first key" comes from `list(pipeline[0].keys())[0]`. If an LLM emitted a stage with multiple top-level keys like `{"$match": {...}, "$group": {...}}`, Mongo would reject the dual-stage stage at parse time (an aggregation stage doc must have exactly one operator). The current `validate_pipeline` does not pre-check stage-name cardinality — but Mongo does. No exploit found.

I also confirmed via grep that no production caller passes a hand-built pipeline to `validate_pipeline`: every call site goes through the LLM → JSON parse path, which guarantees a JSON dict shape (never a tuple or other oddity).

**Acceptance criteria**
- [x] PASS — `validate_pipeline` / `_inject_user_id` prepends `{"$match": {"user_id": user_id}}` as the leading stage when the pipeline doesn't lead with `$match` or `$vectorSearch`. Evidence: `apps/memory/src/tree/memory/query/nl_query.py:340-353` (Step 1 in `_inject_user_id`); verified by independent back-out (`9 failed` unit; `5 failed` integration).
- [x] PASS — Unit regression `test_tenant_match_prepended_when_first_stage_is_not_match_or_vectorsearch` (parametrized × 9: `group`, `sample`, `sort`, `project`, `unwind`, `bucket`, `count`, `sortbycount`, `addfields`) — non-vacuous (9 failures when backed out). Evidence: `apps/memory/tests/unit/memory/query/test_nl_query.py:261-313`.
- [x] PASS — Unit regression `test_tenant_match_not_prepended_when_first_stage_is_match` confirms the in-place merge path. Evidence: `apps/memory/tests/unit/memory/query/test_nl_query.py:315-335`.
- [x] PASS — Unit regression `test_tenant_match_not_prepended_when_first_stage_is_vectorsearch` confirms the `$vectorSearch.filter` injection path is used (Mongo's first-stage invariant respected). Evidence: `apps/memory/tests/unit/memory/query/test_nl_query.py:337-360`.
- [x] PASS — Integration regression `test_nl_query_first_stage_without_match_does_not_leak` (parametrized × 8) exercises the full `execute_nl_query` path with each adversarial first stage; 5 of 8 cases would surface a clean `LEAK -> row from user_id=<B>` message at runtime when the fix is backed out (the other 3 — aggregate output shapes — have their contract enforced at the unit level). Evidence: `apps/memory/tests/integration/test_two_user_isolation.py:901-989`.
- [x] PASS — Path 17 added to module-docstring dimension list. Evidence: confirmed by grep at line 42 ("See `test_nl_query_first_stage_without_match_does_not_leak`").
- [x] PASS — `_assert_no_b_rows` and `_assert_no_b_tokens` hardened to recursively walk dict/list values (closing my cycle-2 Nit 2 about `KeyError: 'user_id'` opaque failures). Evidence: `apps/memory/tests/integration/test_two_user_isolation.py:390-452`; confirmed at runtime — the 5 backed-out integration failures emitted clean `LEAK — row from user_id=<B's id> surfaced in a User-A query: {<row>}` messages instead of opaque tracebacks.
- [x] PASS — `_inject_user_id` docstring rewritten to describe both mechanisms (prepend + overwrite); maintainer contract preserved.
- [x] PASS — System prompt updated to declare "Tenant scoping is enforced by the server".
- [x] PASS — Full QA suite (format-check, lint-check, pre-commit, unit, fast integration, CI-mirror integration) green; 0 warnings; 0 failures.

**Other issues found (Nits — non-blocking, flagged for follow-up; no proven exploit)**

- **Nit 1 (validator defense-in-depth — `$match` with non-dict value).** The first-stage check at `nl_query.py:347-349` only inspects the stage key name; if an LLM emits `{"$match": "not-a-dict"}` (or a list, or `None`) as the first stage, the validator suppresses the prepend AND Step 2 passes the stage through untouched (`isinstance(match, dict)` is False). The validator-level output therefore lacks a tenant filter for these shapes. **No exploit observed**: I pushed all three malformed-`$match` shapes through real Mongo and all three are rejected at execute time with `OperationFailure: the match filter must be an expression in an object` (Location15959). Mongo backstops the leak. Defense-in-depth fix: in Step 1, treat the first stage as "tenant-scoping" only when both the key is `$match`/`$vectorSearch` AND the value is a dict (and for `$vectorSearch`, the value contains a `filter` field that the loop will patch). Cheap one-liner. Non-blocking because Mongo's rejection is reliable and observable.

- **Nit 2 (test quality — `test_unionwith_stage_rejected` from cycle 2 unchanged).** Carried over from cycle-2 Tester log; the test passes in both fix-applied and fix-backed-out states because `$unionWith` was never in the allow-list. Its value is pinning the absence against a future maintainer, which is real, but the docstring should call out that property. Out of cycle-3 scope.

- **Nit 3 (validator expression-operator audit — carried over from cycle 2).** `validate_pipeline` only inspects top-level stage names; expression operators inside `$group`/`$bucket`/`$bucketAuto` (e.g. `$accumulator`, `$function`) are not validated. Mongo's deployment-time JS flag is off, but the validator should hold the contract too. Out of cycle-3 scope.

**Evidence**
```
$ make memory-format-check && make memory-lint-check && make pre-commit
... all green ...

$ time make memory-unit-tests
... 845 passed in 41.22s ===
make memory-unit-tests  8.34s user 1.60s system 22% cpu 44.511 total

$ time make memory-integration-tests
... 119 passed, 12 skipped, 46 deselected in 114.64s ===
make memory-integration-tests  9.29s user 1.68s system 9% cpu 1:57.69 total

$ time make memory-integration-tests-ci
... 108 passed, 12 skipped, 57 deselected in 65.38s ===
make memory-integration-tests-ci  9.87s user 1.84s system 17% cpu 1:08.27 total

# Non-vacuousness verification, cycle-3 fix backed out:
$ git stash push -- apps/memory/src/tree/memory/query/nl_query.py
$ uv --directory apps/memory run pytest \
    tests/unit/memory/query/test_nl_query.py::TestValidatePipeline::test_tenant_match_prepended_when_first_stage_is_not_match_or_vectorsearch \
    tests/unit/memory/query/test_nl_query.py::TestValidatePipeline::test_tenant_match_not_prepended_when_first_stage_is_match \
    tests/unit/memory/query/test_nl_query.py::TestValidatePipeline::test_tenant_match_not_prepended_when_first_stage_is_vectorsearch -v
... 9 failed, 2 passed in 0.19s  (every parametrized case fails: Expected leading tenant $match) ...

$ uv --directory apps/memory run pytest \
    tests/integration/test_two_user_isolation.py::TestTwoUserIsolation::test_nl_query_first_stage_without_match_does_not_leak \
    --timeout=300
... 5 failed, 3 passed in 44.06s  (group/sample/sort/project/bucket → clean LEAK messages;
                                    unwind/count/sortbycount → aggregate shapes, unit-covered) ...

$ git stash pop
$ uv --directory apps/memory run pytest \
    tests/unit/memory/query/test_nl_query.py::TestValidatePipeline::test_tenant_match_prepended_when_first_stage_is_not_match_or_vectorsearch \
    tests/unit/memory/query/test_nl_query.py::TestValidatePipeline::test_tenant_match_not_prepended_when_first_stage_is_match \
    tests/unit/memory/query/test_nl_query.py::TestValidatePipeline::test_tenant_match_not_prepended_when_first_stage_is_vectorsearch \
    tests/integration/test_two_user_isolation.py::TestTwoUserIsolation::test_nl_query_first_stage_without_match_does_not_leak \
    --timeout=300
... 19 passed in 38.68s ===

# Adversarial pass (cycle-3 fix in place):
[1] EMPTY pipeline       → REJECTED: Pipeline is empty (PASS)
[2] $or smuggling        → spread forces user_id=A; runtime returns only A's rows (PASS)
[3] $vectorSearch first  → filter patched with user_id=A; prepend correctly skipped (PASS)
[4] two $match           → both stages get user_id=A; runtime returns only A's rows (PASS)
[5] $facet wrap $lookup  → REJECTED: $facet not allowed (cycle-2 intact) (PASS)
[6] top-level $lookup    → REJECTED: $lookup not allowed (cycle-1 intact) (PASS)
[7] $limit only          → tenant $match prepended (PASS)
[8] $match: 'not-a-dict' → validator passes through unprepended; Mongo rejects at execute time
                            with OperationFailure (Location15959). No leak. (Nit 1 — defense-in-depth)
[9] $match: {user_id: B} → spread overwrites to user_id=A (PASS)

# Final restore sanity:
$ uv --directory apps/memory run pytest tests/integration/test_two_user_isolation.py --timeout=600
... 26 passed in 88.63s ===
$ uv --directory apps/memory run pytest tests/unit/
... 845 passed in 40.64s ===
```

**VERDICT: PASS**

The cycle-3 fix correctly closes the "first stage is not `$match`/`$vectorSearch`" class of leak that I surfaced in cycle 2. Independent back-out + restore confirms non-vacuousness across all 9 unit parametrized cases and 5 of 8 integration cases (the remaining 3 are aggregate-output shapes whose contract is enforced at the unit level — SWE's claim verified). Cycle-1 (`$lookup` rejection) and cycle-2 (`$facet` rejection) fixes are intact. My adversarial pass exercised every spec-listed break path (empty pipeline, `$or` smuggling, leading `$vectorSearch` without `user_id`, two `$match` back-to-back, `$facet` rejection) plus three additional shapes (`$limit`-only, attacker `user_id=B` in leading `$match`, `$match` with non-dict value) — all behave correctly at runtime against real MongoDB. The one defense-in-depth concern (Nit 1: `$match` with non-dict value bypasses the validator's prepend logic) has no exploitable consequence because Mongo rejects the malformed pipeline before any data is returned.

Wall times match SWE's report (unit ≈ 41s vs. 42s claimed; CI-mirror ≈ 65s vs. 66s claimed; fast integration ≈ 115s vs. 2:00 claimed). 0 warnings. The cycle-3 changes (`_assert_no_b_rows` / `_assert_no_b_tokens` recursive walk) addressed my cycle-2 Nit 2 about opaque `KeyError` failures — confirmed by the clean `LEAK — row from user_id=<B>` messages observed during back-out testing.
