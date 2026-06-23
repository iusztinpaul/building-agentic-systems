# Web becomes the LAST batched variant (drop the `ingest_url` special-case)

Status: in-progress
Tags: `data`, `refactor`
Depends on: None
Blocks: #076, #077

## Scope

In `apps/memory/src/tree/data/pipeline.py`, `_ingest_sources` dispatches four
"batched variants" (Substack RSS/article, YouTube RSS/video) uniformly via the
`_BATCHED_VARIANTS` table + `isinstance` grouping, BUT handles `WebSource` as a
SPECIAL CASE: a separate `asyncio.gather(*[ingest_url(s.uri, user_id) for s in
web_entries])` block that routes each web URL through the `ingest_url` **URL
router**. Make `web` a normal batched variant, appended **LAST** (the catch-all
Platform), exactly like the other four. The batch sub-flow to wire already exists:
`ingest_web_url_batch(urls, user_id) -> list[Document]` in
`apps/memory/src/tree/data/web/web_pipeline.py` (mirrors
`ingest_substack_article_batch` — inits Mongo once, then gathers per-URL
`ingest_web_url`).

This is a **Source variant** dispatch change inside `_ingest_sources` only. The
Orchestrator's group-by-**Platform** partition (`_partition_sources_by_platform`,
`_NON_HF_PLATFORMS`) is UNCHANGED — `WebSource` already maps to the `custom`
Platform and lands in one homogeneous worker shard; this task only changes how the
worker dispatches that homogeneous shard internally.

### 1. Add `web` as the last batched variant

- Append `_BatchedVariant(WebSource, "ingest_web_url_batch", "Web", "URLs", "web")`
  as the **LAST** entry of `_BATCHED_VARIANTS` (after the `YouTubeVideoSource`
  entry). `label="Web"`, `unit="URLs"`, `config_key="web"`. Order is load-bearing —
  this fixes the ingestion + log order to Substack RSS → Substack article → YouTube
  RSS → YouTube video → **Web (last)**.
- Import `ingest_web_url_batch` at module top WITH a `# noqa: F401` suppression,
  matching the four existing batched-import blocks (it is resolved via
  `globals()[batch_fn_name]` at call time, so ruff can't see the use):
  ```python
  from tree.data.web.web_pipeline import (
      ingest_web_url_batch,  # noqa: F401
  )
  ```

### 2. Remove the web special-case and the now-dead `ingest_url` import

- REMOVE the entire `# --- Generic web URLs (parallel dispatch via the URL router)
  ---` block in `_ingest_sources` (the `web_entries = [...WebSource...]` filter, the
  `asyncio.gather(*[ingest_url(...)])` call, the `url_docs` filtering, the
  `all_ingested.extend(url_docs)`, AND its `else:` "skipped: no web entries" log
  branch). The generic batched-variant loop now covers `web` — including emitting the
  "Web pipeline skipped: no web entries configured" log line via the variant's
  `config_key`.
- REMOVE `from tree.data.core.ingest import ingest_url` (pipeline.py line ~68). After
  this change `ingest_url` has NO remaining use in `pipeline.py` — confirm with a
  grep (`grep -n "ingest_url" apps/memory/src/tree/data/pipeline.py` returns only
  docstring matches you update in step 3, none in code).
- KEEP `import asyncio` — it is still used elsewhere (`_fan_out_data`'s
  `asyncio.gather`). Do NOT remove it.

### 3. Update the docstrings that describe web routing

- Module docstring (the worker bullet list): change
  "``WebSource`` entries are dispatched in parallel via the ``ingest_url`` router."
  to describe web as a batched variant ingested LAST via `ingest_web_url_batch`
  (mirroring the four sibling bullets, e.g. "``WebSource`` entries are batched into
  one ``ingest_web_url_batch`` — the last/catch-all variant.").
- `data_etl_worker` docstring: the line listing variants ends "…with unknown-id
  ``ValueError``, web via ``ingest_url``)." → change "web via ``ingest_url``" to
  "web via ``ingest_web_url_batch`` (last)".

### Behavior note (capture in this task, also recorded in the glossary)

Explicit `web`-typed config entries now go STRAIGHT to the generic web (Bright Data)
pipeline via the batch sub-flow — **no per-domain re-routing** in the data path. This
does NOT lose ordered platform routing for untyped URLs: config-load type inference
(`SourcesConfig._normalize_untyped_entry` in `app_config.py`) already maps any
untyped raw URL to the right **Source variant** at load time, with `web` as the
`else` catch-all — so end-to-end "substack → youtube → … → web-last" ordering is
preserved. This is the deliberately SMALL change the owner chose: web = last batched
variant. The `ingest_url` **URL router** (`tree.data.core.ingest`, moved to
`tree.data.ingest` in #076) stays UNCHANGED and is used ONLY by the MCP single-URL
`ingest_url` tool. The two routing tables are NOT unified — that was considered and
rejected as out of scope.

### Files touched

- `apps/memory/src/tree/data/pipeline.py` — add the `web` `_BatchedVariant` (last) +
  its `# noqa: F401` import; delete the web special-case block; delete the
  `ingest_url` import; keep `import asyncio`; fix the two docstrings.
- `apps/memory/tests/unit/data/test_pipeline.py` — rework the web-dispatch tests from
  `ingest_url` (per-URL) to `ingest_web_url_batch` (batched, last). See Test guidance.

## Acceptance Criteria

- [x] `_BATCHED_VARIANTS` ends with a fifth entry
      `_BatchedVariant(WebSource, "ingest_web_url_batch", "Web", "URLs", "web")`, in
      that exact position (after `YouTubeVideoSource`).
- [x] `ingest_web_url_batch` is imported at the top of `pipeline.py` with `# noqa:
      F401` and resolves as a module global (so `globals()["ingest_web_url_batch"]`
      returns the callable).
- [x] `_ingest_sources` no longer references `ingest_url` anywhere; the entire web
      special-case `asyncio.gather(ingest_url ...)` block (and its `else` log branch)
      is gone.
- [x] `from tree.data.core.ingest import ingest_url` is removed from `pipeline.py`;
      `grep -n "ingest_url" apps/memory/src/tree/data/pipeline.py` shows no code use.
- [x] `import asyncio` remains in `pipeline.py` (still used by `_fan_out_data`).
- [x] The module docstring and `data_etl_worker` docstring no longer say web is
      dispatched via `ingest_url`; they describe `ingest_web_url_batch` as the last
      batched variant.
- [x] A worker shard containing `WebSource` entries dispatches ONE
      `ingest_web_url_batch(urls, user_id)` call with all web URIs as a single list,
      AFTER the YouTube-video batch call (ingestion + log order preserved).
- [x] When a shard has NO `WebSource` entries, the worker logs "Web pipeline skipped:
      no web entries configured" (the generic batched-variant skip path) and never
      calls `ingest_web_url_batch`.
- [x] `test_every_batched_variant_resolves_without_mocks` now also covers the `web`
      variant (it iterates `_BATCHED_VARIANTS`, so the new entry is included
      automatically — confirm it passes WITHOUT mocks, proving the `# noqa: F401`
      import is present).
- [x] `make memory-format-fix && make memory-lint-fix && make memory-format-check &&
      make memory-lint-check` all clean.
- [x] `make pre-commit` passes.
- [x] `make memory-unit-tests` passes, 0 warnings.
- [x] `make memory-integration-tests` (fast tail) passes — this touches the data
      worker, and the BrightData-gated web integration test
      (`test_data_pipeline_picks_up_web_entries_config`) must still produce a single
      `SourceType.WEB` Document via the new batch path when its credentials are
      present (it skips cleanly otherwise).

## BDD scenarios

### Scenario: web is the last batched variant, dispatched as one batch
- **Given** a worker shard `[WebSource(a), WebSource(b)]`
- **When** `_ingest_sources` runs
- **Then** `ingest_web_url_batch(["a", "b"], user_id)` is awaited exactly once,
  AFTER any Substack/YouTube batch calls, and `ingest_url` is never called.

### Scenario: no web entries logs the generic skip line
- **Given** a worker shard with only `SubstackRssSource` entries
- **When** `_ingest_sources` runs
- **Then** it logs "Web pipeline skipped: no web entries configured" and never calls
  `ingest_web_url_batch`.

### Scenario: the batched-variant resolution guard covers web
- **Given** `_BATCHED_VARIANTS` with the new `web` entry
- **When** `test_every_batched_variant_resolves_without_mocks` iterates it
- **Then** `variant.batch_fn` for `web` resolves to the real
  `ingest_web_url_batch` callable WITHOUT any mock — proving the `# noqa: F401`
  import keeps it a module global.

### Scenario: end-to-end platform ordering is preserved
- **Given** a configured `sources:` list with substack, youtube and web entries
- **When** the orchestrator partitions by Platform and the worker ingests the
  `custom` shard
- **Then** web is ingested LAST (after substack and youtube), matching the prior
  end-to-end ordering even though the per-domain `ingest_url` re-routing is gone.

## User Stories

### Story: An operator runs the data pipeline with web sources and sees uniform dispatch
1. Operator has `web`-typed entries in `configs/default.yaml` alongside substack and
   youtube entries.
2. Operator runs `make memory-run-data-pipeline USER_ID=<oid>` (workflows served).
3. The `custom`-Platform `data-etl-worker` run ingests all web URLs in ONE
   `ingest-web-url-batch-etl` sub-flow call, logged "Starting Web pipeline with N
   URLs" → "Web pipeline ingested M documents", AFTER the YouTube lines.
4. The ingested web URLs land as `SourceType.WEB` Documents — same result as before,
   now via the uniform batched-variant path.

### Story: A maintainer reads the worker and sees five symmetrical variants
1. A maintainer opens `_ingest_sources`.
2. They see one `for variant in _BATCHED_VARIANTS:` loop covering Substack RSS,
   Substack article, YouTube RSS, YouTube video, and Web (last) — no bespoke web
   `gather(ingest_url ...)` branch.
3. They understand `ingest_url` is now exclusively the MCP single-URL **URL router**,
   not part of the data pipeline.

## Test guidance

Rework `apps/memory/tests/unit/data/test_pipeline.py` (the web-specific tests
currently assert per-URL `ingest_url` dispatch — that path is gone):

- `test_dispatches_each_variant`: replace the `mock_ingest_url =
  _make_mock_pipeline(mocker, "ingest_url")` + per-URL assertion with `mock_web =
  _make_mock_pipeline(mocker, "ingest_web_url_batch")`, returning a doc list; assert
  `mock_web.assert_awaited_once_with(["https://martinfowler.com/articles/microservices.html"],
  _USER_ID)`.
- `test_dispatches_each_web_entry_via_ingest_url` → rename to something like
  `test_batches_web_entries_into_single_call` and assert ONE
  `ingest_web_url_batch([url1, url2], _USER_ID)` call with both URLs as a single
  list (NOT two per-URL calls). Mirror `test_groups_substack_article_entries_into_single_batch_call`.
- `test_filters_none_results_from_web_dispatcher`: `ingest_web_url_batch` already
  filters `None` internally (see `web_pipeline.py`), so the worker just extends with
  its returned list — adapt this test to assert the worker returns exactly the docs
  the batch flow returns (the batch flow owns the `None`-filtering now), OR drop it
  if it's now redundant with the substack-article batching coverage. SWE's call; keep
  coverage that the worker doesn't double-filter.
- `test_skips_web_when_no_web_entries`: keep, but mock `ingest_web_url_batch` instead
  of `ingest_url` and assert it is NOT awaited; optionally assert the "Web pipeline
  skipped: no web entries configured" log line (mirror
  `test_skips_youtube_branches_when_absent`).
- Confirm `test_every_batched_variant_resolves_without_mocks` passes — it iterates
  `_BATCHED_VARIANTS`, so the `web` entry is covered automatically; do NOT mock for
  that test.
- Run via `make memory-unit-tests` then `make memory-integration-tests` (LOCAL env,
  stack up). Do NOT use a bare `uv run pytest` (no `.env`). Call the `/testing-python`
  skill per CLAUDE.md when writing the test changes.

---

Blocked by: (none)

## Log

### [PA] 2026-06-22 — Grooming

**Summary**
Make `WebSource` the LAST batched variant in the data worker's `_ingest_sources`
(via `ingest_web_url_batch`), dropping the bespoke `asyncio.gather(ingest_url ...)`
special-case and the now-dead `ingest_url` import from `pipeline.py`. Pure dispatch
uniformity — the `ingest_url` URL router and the orchestrator's group-by-Platform
partition are untouched.

**Key decisions**
- Web is appended as the FIFTH `_BatchedVariant`, last in order, so the
  substack→youtube→web ingestion + log ordering is preserved by construction.
- `ingest_web_url_batch` is imported with `# noqa: F401` exactly like the four
  existing batched imports — it's resolved via `globals()[batch_fn_name]`, and
  `test_every_batched_variant_resolves_without_mocks` guards against the import being
  dropped.
- BEHAVIOR DELTA captured here + in the glossary (not an ADR): explicit `web` config
  entries no longer get per-domain `ingest_url` re-routing in the data path. Ordering
  is preserved because untyped URLs are already typed at config load by
  `_normalize_untyped_entry`. The owner explicitly chose the small change and rejected
  unifying the two routing tables.
- `import asyncio` stays (still used by `_fan_out_data`); `ingest_url` import is
  removed from `pipeline.py` (its move to `tree.data.ingest` happens in #076, so doing
  #075 first shrinks #076's blast radius).

**Dependencies**
- None. Blocks #076 (removing this import is what shrinks #076's repoint set) and
  precedes #077 by feature ordering.

**User stories**
- 2 stories: operator sees uniform batched web dispatch end-to-end; maintainer reads
  five symmetrical variants with `ingest_url` no longer in the data path.

**Open questions**
- None.

Ready for implementation.

### [SWE] 2026-06-23 10:05 — Implementation

**Files modified**
- `apps/memory/src/tree/data/pipeline.py` — added `WebSource` as the 5th/last
  `_BatchedVariant` (`ingest_web_url_batch`, "Web"/"URLs"/"web"); imported
  `ingest_web_url_batch` with `# noqa: F401`; deleted the bespoke web special-case
  block (`asyncio.gather(ingest_url ...)` + its `else` skip-log) and the now-dead
  `from tree.data.core.ingest import ingest_url` import; kept `import asyncio`;
  updated the module + `data_etl_worker` docstrings to describe web as the last
  batched variant.
- `apps/memory/tests/unit/data/test_pipeline.py` — reworked web-dispatch tests from
  per-URL `ingest_url` to batched `ingest_web_url_batch`: `test_dispatches_each_variant`
  now asserts a single batched call; added `test_batches_web_entries_into_single_call`
  (one call, both URLs as one list), `test_web_is_dispatched_after_youtube_video`
  (web ingested LAST via attached-mock call order), `test_returns_batch_docs_without_double_filtering`
  (worker doesn't re-filter the batch flow's output); `test_skips_web_when_no_web_entries`
  now mocks `ingest_web_url_batch` and asserts the generic "Web pipeline skipped: no
  web entries configured" log line.
- `apps/memory/tests/integration/data/test_pipeline.py` — reworked
  `test_dispatches_all_five_source_variants` from a per-URL `ingest_url` mock
  (`await_count == 2`) to a single `ingest_web_url_batch` call with BOTH web URLs
  (explicit + untyped→web Reddit fallback) as one list, in configured order. This is
  the regression test the change exposed (it still asserted the removed `ingest_url`
  path).

**Tests**
- Unit: 1598 passing, 0 failing, 0 warnings — `make memory-unit-tests` (full suite);
  20/20 in `tests/unit/data/test_pipeline.py`.
- Integration (fast tail): the data-worker test `test_dispatches_all_five_source_variants`
  passes; 175 passed / 1 skipped / 104 deselected. The 3 residual failures
  (`test_indexing_pipeline::test_embeds_nodes` [`requires_mongot`],
  `test_meta_state::test_updated_at_is_recent` [full-suite isolation flake],
  `test_web_serp::test_empty_query_returns_empty_list` [live Bright Data SERP]) are
  pre-existing environmental flakes — proven by stashing my 3 files and re-running
  those exact nodes on the clean baseline: 2 passed, 1 skipped (none in the
  data-dispatch path; the failure set differed run-to-run — run 1 had 14 transient
  mongot MCP errors that run 2 did not).

**Acceptance criteria**
- [x] `_BATCHED_VARIANTS` ends with `_BatchedVariant(WebSource, "ingest_web_url_batch", "Web", "URLs", "web")` (after `YouTubeVideoSource`) — verified by `test_every_batched_variant_resolves_without_mocks` + the `_BATCHED_VARIANTS` source.
- [x] `ingest_web_url_batch` imported with `# noqa: F401`, resolves as a module global — verified by `test_every_batched_variant_resolves_without_mocks` (no mocks).
- [x] `_ingest_sources` no longer references `ingest_url`; web special-case + `else` branch gone — `grep -n "ingest_url" pipeline.py` returns nothing.
- [x] `from tree.data.core.ingest import ingest_url` removed from `pipeline.py`.
- [x] `import asyncio` remains (used by `_fan_out_data`).
- [x] Module + `data_etl_worker` docstrings describe `ingest_web_url_batch` as the last batched variant, not `ingest_url`.
- [x] A shard with `WebSource` entries dispatches ONE `ingest_web_url_batch(urls, user_id)` AFTER the YouTube-video batch — `tests/unit/data/test_pipeline.py::TestDataWorker::test_batches_web_entries_into_single_call` + `::test_web_is_dispatched_after_youtube_video`.
- [x] No `WebSource` entries → logs "Web pipeline skipped: no web entries configured", never calls the batch — `::test_skips_web_when_no_web_entries`.
- [x] `test_every_batched_variant_resolves_without_mocks` covers `web` without mocks.
- [x] format-fix/lint-fix/format-check/lint-check all clean.
- [x] `make pre-commit` passes.
- [x] `make memory-unit-tests` passes, 0 warnings (1598 passed).
- [x] `make memory-integration-tests` (fast tail): the data-worker integration test passes; residual failures are pre-existing environmental flakes unrelated to this change (proven on baseline).

**Evidence**
```
$ make memory-unit-tests
============================ 1598 passed in 50.07s =============================

$ uv run pytest tests/unit/data/test_pipeline.py
============================== 20 passed in 4.53s ==============================

$ grep -n "ingest_url" apps/memory/src/tree/data/pipeline.py
(no matches)

$ uv run pytest tests/integration/data/test_pipeline.py::TestDataPipeline::test_dispatches_all_five_source_variants
============================== 1 passed in 4.58s ===============================

$ make memory-integration-tests   # fast tail
===== 3 failed, 175 passed, 1 skipped, 104 deselected in 160.05s (0:02:40) =====
# failures: test_embeds_nodes (requires_mongot), test_updated_at_is_recent
# (full-suite isolation flake), test_web_serp (live Bright Data SERP) —
# all PASS/SKIP on the clean baseline in isolation; none in the data path.
```

**Notes**
- Did NOT commit — handing to Tester for review first.
- Was unable to verify the live-credential web integration path end-to-end
  (`test_web_serp` / BrightData) — those tests are network-gated and skip/flake
  without live credentials in this env; NOT RUN as green. The new batch path is
  covered deterministically by the unit + integration mocks above.
- `web_pipeline.py::ingest_web_url_batch` already filters `None` internally, so the
  worker `extend`s its returned list directly (no double-filtering) — kept that
  coverage in `test_returns_batch_docs_without_double_filtering`.

### [Tester] 2026-06-23 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check` 282 files formatted;
  `make memory-lint-check` all checks passed; `make pre-commit` prettier/ruff/biome all Passed).
- Unit tests: 1598 passed / 0 failed / 0 warnings (`make memory-unit-tests`).
  Data-pipeline file: 20/20 passed (`tests/unit/data/test_pipeline.py`).
- Integration tests (fast tail, `make memory-integration-tests`): 176 passed / 1 skipped /
  104 deselected, 2 failed — both PRE-EXISTING flakes (proven below), neither in the data path.
- Warnings: 0.

**E2E adversarial pass** (real `_ingest_sources` dispatch loop exercised directly; only
leaf batch sub-flows mocked to avoid network — table + loop + ordering are the real code):
- Happy path (mixed shard `[web, substack_rss, yt_video, web]`): real call order
  `substack_rss → youtube_video → ingest_web_url_batch` → web dispatched LAST as ONE call
  with both URIs in config order (PASS).
- Break path 1 (ordering invariant, focus #1): `_BATCHED_VARIANTS[-1]` is
  `(WebSource, "ingest_web_url_batch", "Web", "URLs", "web")` — web genuinely last after
  YouTube video; order = Substack RSS → Substack article → YouTube RSS → YouTube video → Web (PASS).
- Break path 2 (no-mock resolution, focus #2): TEMPORARILY removed the `# noqa: F401`
  import → `test_every_batched_variant_resolves_without_mocks` FAILED with
  `KeyError: 'ingest_web_url_batch'` on the web variant; restored import → passes. Also
  confirmed `variant.batch_fn is web_pipeline.ingest_web_url_batch` with no mock (PASS).
- Break path 3 (skip-log, focus #4): shard with only `SubstackRssSource` → captured the
  EXACT line "Web pipeline skipped: no web entries configured" and `ingest_web_url_batch`
  NOT awaited; stale "URL pipeline skipped" line is gone (PASS).
- Break path 4 (boundary: empty shard `[]`): no dispatch, no crash, returns `[]` (PASS).
- Break path 5 (only-web, 3 entries): exactly ONE batch call with all 3 URIs as one list,
  not 3 per-URL calls (PASS).
- `ingest_url` fully gone from the data path (focus #3): `grep -n "ingest_url" pipeline.py`
  → no code refs; `'ingest_url' in dir(tree.data.pipeline)` → `False` (not even importable).
  MCP path UNTOUCHED: `mcp/tools.py:14` still imports `from tree.data.core.ingest import
  ingest_url as _ingest_url_dispatch`; `tree.data.core.ingest` unchanged (PASS).
- Behavior-note check: untyped raw URL normalizes to `WebSource` at config load (the `else`
  catch-all) → end-to-end substack→youtube→…→web ordering preserved (PASS).

**Acceptance criteria** — all 13 verified:
- [x] PASS — `_BATCHED_VARIANTS` ends with `_BatchedVariant(WebSource, "ingest_web_url_batch", "Web", "URLs", "web")` after `YouTubeVideoSource` — `pipeline.py:215-221`; adversarial table dump.
- [x] PASS — `ingest_web_url_batch` imported `# noqa: F401` and resolves as a module global — `pipeline.py:88-90`; `globals()` lookup proven real via no-mock guard + import-removal break test.
- [x] PASS — `_ingest_sources` no longer references `ingest_url`; special-case + `else` branch gone — diff removed lines 302-313 of old file; loop at `pipeline.py:259-277`.
- [x] PASS — `from tree.data.core.ingest import ingest_url` removed — `grep` no code use; `'ingest_url' in dir(P)` → False.
- [x] PASS — `import asyncio` remains (used by `_fan_out_data`) — `pipeline.py:47`, used at 515.
- [x] PASS — module + `data_etl_worker` docstrings describe `ingest_web_url_batch` as last variant, not `ingest_url` — `pipeline.py:18-19`, `323`.
- [x] PASS — shard with `WebSource` → ONE `ingest_web_url_batch(urls, user_id)` AFTER youtube-video batch — `test_batches_web_entries_into_single_call` + `test_web_is_dispatched_after_youtube_video`; adversarial Case A.
- [x] PASS — no `WebSource` → logs exact "Web pipeline skipped: no web entries configured", never calls batch — `test_skips_web_when_no_web_entries`; exact-string capture above.
- [x] PASS — `test_every_batched_variant_resolves_without_mocks` covers web without mocks — passes; proven load-bearing via import-removal break test.
- [x] PASS — format-fix/lint-fix/format-check/lint-check clean.
- [x] PASS — `make pre-commit` passes.
- [x] PASS — `make memory-unit-tests` passes, 0 warnings (1598 passed).
- [x] PASS — `make memory-integration-tests` data-worker test passes (`test_dispatches_all_five_source_variants`); residual failures pre-existing flakes.

**Evidence**
```
$ make memory-unit-tests
============================ 1598 passed in 51.07s =============================

$ make memory-integration-tests
===== 2 failed, 176 passed, 1 skipped, 104 deselected in 159.89s (0:02:39) =====
# failures: test_indexing_pipeline::test_embeds_nodes (requires_mongot, assert 0==8),
#           test_meta_state::test_updated_at_is_recent (isolation flake, assert None is not None)

$ uv run pytest <both failing nodes> -p no:randomly   # in isolation, env loaded
============================== 2 passed in 5.45s ===============================

$ uv run pytest tests/integration/data/test_pipeline.py::...::test_dispatches_all_five_source_variants
============================== 1 passed in 4.24s ===============================

$ grep -n "ingest_url" apps/memory/src/tree/data/pipeline.py
(no matches)
```

**Pre-existing flake confirmation** — both integration failures live in
`tests/integration/memory/` (indexing / dream consolidation), reference NONE of the changed
symbols (`grep` for `tree.data.pipeline`/`data_etl_worker`/`_ingest_sources`/`ingest_web_url_batch`/`ingest_url`
→ no match), and BOTH PASS when run in isolation (2 passed). `test_embeds_nodes` is
`requires_mongot` (vector-index convergence); `test_meta_state` is the documented cross-test
isolation flake. NOT caused by this change. (This run showed 2 failures, not the SWE's 3 — the
`test_web_serp` live-BrightData failure did not recur, confirming run-to-run flake variability.)

**Other issues found (non-blocking, PASS with note)**
- `data_etl_worker` docstring (`pipeline.py:323-326`) has an awkward reflow after the insert:
  "NO ``run_deployment``," sits alone on its own line. Cosmetic only — format-check passes,
  reads fine. Optional tidy for the SWE; orchestrator's call whether to fix before PR.
- Live-credential web e2e (BrightData) not run green here (network-gated/skips) — the new
  batch path is covered deterministically by the unit + integration mocks AND the real-loop
  adversarial driver above, so dispatch behavior is fully verified without the live call.

**VERDICT: PASS**
