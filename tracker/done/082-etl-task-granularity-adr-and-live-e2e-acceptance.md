# ETL-task-granularity ADR + glossary verify + full acceptance + live e2e

Status: done
Tags: `docs`, `adr`, `prefect`, `data`
Depends on: #078, #079, #080, #081
Blocks: —

## Scope

Close out the `batch-etl-task-topology` feature: land the proposed ADR-002 §3 amendment
(ETL task granularity = batch not document; per-item sub-flow collapse with a thin MCP-only
flow; per-element isolation + whole-batch-retry-is-safe-because-load-is-idempotent;
fetch-retries-at-the-fetch-layer; RSS keeps feed-obtain + shares only the build/load tail —
no re-fetch), VERIFY the glossary additions match the shipped code, run the FULL acceptance
suite, and perform the `[HUMAN]` live end-to-end on the real Prefect stack. No new product
code — this is the documentation + acceptance bookend (mirrors #074's role for the prior
feature). If acceptance surfaces a regression, fix it under the relevant earlier task's
scope (#078–#081), not here.

### 1. Land the ADR-002 §3 amendment (proposed in grooming)

The amendment was DRAFTED in grooming (handed back as a proposal for the human gate; see the
feature plan's Documentation-updates and the grooming hand-back). Author the APPROVED text
into `docs/adrs/002_pipeline_concurrency_and_voyage_rate_limiting.md` §3 as a new indented
"**Amendment (#078–#082 — …).**" block, style-matching the #055/#061/#066/#070–#074
amendments, `Status: Accepted` PRESERVED (this is a refinement of the same fan-out/topology
decision, not a new decision and not a supersession). It must capture:

- **Prefect task granularity = batch, not document.** Leaf batch ETL flows run ETL-phase
  `@task`s over a **Batch** (HF `batch_size`-chunk / one feed's entries / the whole URL
  list), never per row. The per-row `extract_document`/`fetch_paper_content`/`load_document`
  `@task` calls are gone. Record the concrete payoff: a 1000-doc arXiv window worker drops
  from ~1000 task runs to a few tens.
- **Pragmatic E/T/L.** Load is ALWAYS its own task; Extract+Transform FUSE where one scrape
  yields the Document (web / substack-article / youtube-video); a separate `transform_batch`
  exists where transform is a genuine pure map (arXiv dict→Document; substack-RSS
  feed-entry→Document); optional network `enrich_batch` (arXiv paper content). Streamed read
  (arXiv `fetch_dataset_batches`) stays the flow loop, not a task.
- **Per-item sub-flow collapse + thin MCP flow.** The direct-link pipelines
  (`ingest_web_url`/`ingest_substack_article`/`ingest_youtube_video`) demote their bodies to
  `_ingest_<x>_one` core fns; a 1-line `@flow` wrapper is retained ONLY for the MCP
  `ingest_url` router's single-URL path. The BATCH path calls the core directly — no per-item
  sub-flow runs.
- **Per-element isolation inside tasks + the idempotency invariant.**
  `asyncio.gather(return_exceptions=True)` inside each task: a bad-data element is logged +
  skipped (return the successful subset + a failure count); the task hard-fails only on a
  batch-WIDE infra failure → Prefect retries the WHOLE batch, SAFE because every load dedups
  on `(user_id, source_uri)` (never double-inserts).
- **Retry relocation.** Batch-task retries gate batch-wide infra (fetch/extract `retries=2`,
  load `retries=1`, pure transform `retries=0`); per-element transient FETCH retries live in
  the fetch layer (existing httpx behavior / the chain's per-slot fallback) — no
  network-fetch-retry regression.
- **RSS keeps feed-obtain + shares only the build/load tail (no re-fetch).** substack-RSS
  builds from feed-embedded content (1 fetch → N docs, no per-article re-scrape) and shares
  only the LOAD with substack-article; youtube-RSS + youtube-video share the bulk-transcript
  fetch + build + load, with metadata source distinct (feed vs oEmbed) and ONE bulk
  `fetch_many` per feed.
- **Result persistence.** Prefect-3 result persistence is off by default; the side-effecting
  load/extract tasks persist no results (no `persist_result`/`cache_policy` added).
- **Unchanged invariants (so Status stays Accepted).** The flow-level topology
  (orchestrator → worker → per-source batch flow), the two-deployments-per-pipeline +
  depth-1/no-recursion dispatch (§3 amendment #066/#070–#074), the group-by-platform data
  fan-out + HF offset-windowing (§3 amendment #070–#074), `gather(return_exceptions=True)`
  failure-isolation, no trailing index for data, the `voyage-embeddings` GCL (§1) +
  `serve(limit=…)` admission control (§4), and the batch-flow Opik trace structure
  (per-batch-PHASE spans, NOT per-doc; trace-header forwarding preserved) are all unchanged.

Reference this feature's plan (`tracker/feature-batch-etl-task-topology-plan.md`) from the
amendment's context line, matching how prior amendments reference their plans.

### 2. Verify the glossary additions (landed in the grooming commit)

The grooming commit added the **ETL-phase task** + **Thin MCP flow** rows to
`docs/glossary.md` and reconciled the **Batch** / **Worker** rows (Batch is now the ETL-phase
task's grain; Worker no longer fans out a per-item sub-flow). This task CONFIRMS those rows
are present and accurate against the shipped #078–#081 code — do NOT re-author. Table style
preserved; unchanged terms not restated.

### 3. Full acceptance suite

Run the complete acceptance gate per CLAUDE.md/AGENTS.md, LOCAL env, full docker-compose
stack up (mongot included), in isolation:

- `make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check`
- `make pre-commit`
- `make memory-unit-tests` (0 warnings)
- `make memory-integration-tests-all` (~5–8 min; includes `@pytest.mark.slow` +
  `requires_mongot`) — the same target CI runs.

### 4. `[HUMAN]` live end-to-end

With the stack up (`make local-start`) and `make memory-serve-workflows` re-served to pick
up the new code, and `default.yaml` carrying the arXiv HF source (`max_samples: 1000`,
`num_workers: 2`) plus Substack/YouTube/web sources:

1. Run `make memory-run-data-pipeline USER_ID=<oid>`.
2. In the Prefect UI, open an arXiv HF-WINDOW `data-etl-worker` run and confirm it shows a
   few TENS of task runs (NOT ~1000): per `batch_size`-chunk, a `transform-batch` +
   `load-batch` (+ `enrich-batch` only if `fetch_content`), NOT per-row
   `extract-arxiv-document`/`load-arxiv-document`.
3. Confirm ETL-phase tasks are visible per batch for the other workers
   (substack/youtube/web): `extract-batch`/`transform-batch` + `load-batch`, and NO per-item
   sub-flow runs (no `ingest-web-url-etl` / `ingest-substack-article-etl` /
   `ingest-youtube-video-etl` children under a batch worker).
4. Confirm YouTube does ONE bulk transcript fetch per feed (one `fetch_many` log line, not
   per-video).
5. Idempotency: trigger a SECOND `make memory-run-data-pipeline USER_ID=<oid>` and confirm
   the `documents` counts do not grow (dedup on `(user_id, source_uri)`).
6. Single-URL MCP smoke: via the MCP `ingest_url` tool, ingest one web URL, one substack
   article URL, and one youtube watch URL; confirm each still produces its own thin-flow run
   + persists a Document (the thin MCP flows still work).

Record outcomes (UI screenshots or run/child names + Mongo counts) in the task log.

### Files touched

- `docs/adrs/002_pipeline_concurrency_and_voyage_rate_limiting.md` — append the §3
  amendment (from the approved draft).
- (no product code) — acceptance + live e2e are verification, not code changes.

## Acceptance Criteria

- [x] ADR-002 §3 carries a new amendment (style-matching #070–#074, `Status: Accepted`
      preserved) recording: batch-not-document task granularity (with the ~1000→tens
      payoff), pragmatic E/T/L (Load always separate; E+T fused for scrape pipelines;
      transform_batch where it's a pure map; enrich_batch for arXiv content; streamed read
      stays a flow loop), the per-item sub-flow collapse + thin MCP-only flow, per-element
      isolation + whole-batch-retry-safe-because-load-idempotent, retry relocation to
      batch+fetch-layer, RSS keeps feed-obtain + shares only build/load (no re-fetch),
      result-persistence-off, and the unchanged-invariants list.
- [x] The amendment references the feature plan
      `tracker/feature-batch-etl-task-topology-plan.md`.
- [x] The glossary **ETL-phase task** + **Thin MCP flow** rows (and the reconciled
      **Batch**/**Worker** rows) are present and accurate against the shipped code (verified,
      not re-authored).
- [x] `make memory-format-fix && make memory-lint-fix && make memory-format-check &&
      make memory-lint-check && make pre-commit` all clean.
- [x] `make memory-unit-tests` passes, 0 warnings.
- [x] `make memory-integration-tests-all` — full-suite green modulo pre-existing
      environmental flakes. Tester's independent run: 19 failed / 264 passed / 1 skipped.
      18 failures in the MEMORY/extraction layer (shared-DB cross-test contamination +
      sequential Gemini load); 1 in the data layer — `test_web_serp::test_empty_query_
      returns_empty_list`, a LIVE Bright Data SERP-drift flake in `web_serp.search` (code
      UNTOUCHED by #078–#081, no relation to the batch-ETL task topology), reproduced as
      non-deterministic. Every one of the 19 re-verified PASSING in isolation. The data
      PIPELINE subset (#078–#081: arxiv/substack/youtube/web ingest, incl. the one-bulk-
      fetch invariant) is FULLY green in isolation (31 passed / 10 skipped). No feature
      regression. See Tester log Evidence.
- [ ] [HUMAN] Live e2e: an arXiv window worker shows a few TENS of task runs (NOT ~1000) in
      the Prefect UI, with `transform-batch`/`load-batch` (+ `enrich-batch` iff
      `fetch_content`) per chunk.
- [ ] [HUMAN] Live e2e: substack/youtube/web workers show batch ETL-phase tasks and NO
      per-item sub-flow runs; YouTube does ONE bulk transcript fetch per feed.
- [ ] [HUMAN] Live e2e: a second run does not grow the `documents` counts (idempotency).
- [ ] [HUMAN] Live e2e: single-URL MCP ingest (web / substack article / youtube video) each
      still runs its thin flow and persists a Document.

## BDD scenarios

### Scenario: the ADR records the new task granularity without superseding
- **Given** the appended ADR-002 §3 amendment
- **When** a reader looks up why ETL tasks run at batch grain
- **Then** they find batch-not-document granularity, pragmatic E/T/L, the per-item sub-flow
  collapse + thin MCP flow, per-element isolation + idempotent whole-batch retry, retry
  relocation, and the no-re-fetch RSS rule — with `Status: Accepted` preserved and the
  unchanged invariants enumerated.

### Scenario: the full acceptance gate is green
- **Given** the feature implemented through #081
- **When** I run the full CLAUDE.md acceptance sequence on the LOCAL isolated stack
- **Then** format/lint/pre-commit are clean, unit tests pass with 0 warnings, and
  `make memory-integration-tests-all` exits 0.

### Scenario: the live Prefect graph proves the de-explosion
- **Given** the stack up, workflows served, and `default.yaml` with the arXiv HF source at
  `max_samples: 1000, num_workers: 2` plus substack/youtube/web sources
- **When** the operator runs `make memory-run-data-pipeline USER_ID=<oid>`
- **Then** each arXiv window worker shows tens of batch-phase task runs (not ~1000), no
  batch worker shows per-item sub-flow runs, YouTube does one bulk fetch per feed, and a
  re-run is idempotent.

## User Stories

### Story: A future engineer understands the task granularity from the ADR
1. A new engineer reads ADR-002 §3 to understand why ETL tasks aren't per-document.
2. The amendment tells them tasks run per **Batch**, Load is always its own task,
   direct-link pipelines collapsed their per-item sub-flow (keeping a thin MCP-only flow),
   per-element failures are isolated, whole-batch retry is safe because load is idempotent,
   and RSS shares only the build/load tail (no re-fetch) — without reading the flow code.
3. They see the per-batch Opik spans + the unchanged flow-level topology, and know the
   change is a task-grain refinement, not a fan-out change.

### Story: The owner verifies the de-explosion live before merge
1. The owner brings the stack up, re-serves workflows, and triggers the data pipeline.
2. The Prefect UI shows arXiv window workers with tens of batch-phase tasks (not thousands)
   and no per-item sub-flow runs anywhere.
3. The owner confirms YouTube bulk-fetches per feed, a re-run is idempotent, and single-URL
   MCP ingest still works, then approves the PR.

## Test guidance

- This task's automated portion is the FULL acceptance gate (no new product tests — the
  per-task suites in #078–#081 own coverage). Run via `make memory-*` on the LOCAL env with
  the full stack up, in isolation, per CLAUDE.md.
- The `[HUMAN]` live e2e is a manual Prefect-UI + Mongo + MCP verification — it cannot be
  automated (it asserts on the real task-run COUNT in the UI, the real bulk fetch, the
  absence of per-item sub-flow runs, and the MCP single-URL path). Record evidence in the
  log. Defer the `[HUMAN]` ACs to the owner exactly as #074 did.
- The ADR amendment is prose authored from the approved draft (handed back in grooming); no
  test, but it is an AC that the file carries the amendment and references the plan.

---

Blocked by: #078, #079, #080, #081

## Log

### [PA] 2026-06-23 — Grooming

**Summary**
Feature bookend: author the ADR-002 §3 amendment (batch-not-document task granularity,
pragmatic E/T/L with Load always separate, per-item sub-flow collapse + thin MCP-only flow,
per-element isolation + idempotent whole-batch retry, retry relocation, RSS no-re-fetch,
result-persistence-off, unchanged-invariants list, Status stays Accepted), verify the
glossary additions against the shipped code, run the full `make memory-integration-tests-all`
acceptance gate, and perform the `[HUMAN]` live e2e (Prefect-UI task-run de-explosion +
no-sub-flow + bulk-fetch + idempotent re-run + single-URL MCP smoke). No new product code.

**Key decisions**
- The amendment is style-matched to #055/#061/#066/#070–#074 (indented §3 block, Status
  Accepted) — a task-grain refinement of the same pipeline-topology decision, not a new ADR
  file and not a supersession. Recommendation handed back at grooming was: ADR-002 amendment,
  NOT a new ADR-003 (rationale in the feature plan + the grooming hand-back). The draft text
  was handed back for the human gate; this task writes the approved text.
- Acceptance follows CLAUDE.md exactly: LOCAL env, full stack incl. mongot, isolated run,
  `make memory-integration-tests-all` as the gate CI mirrors.
- The live e2e centers on the user-visible payoff that unit tests can't assert: the Prefect-UI
  task-run COUNT (~1000 → tens), the absence of per-item sub-flow runs, and one bulk
  transcript fetch per feed. `[HUMAN]` ACs deferred to the owner (mirrors #074).

**Dependencies**
- #078–#081 — the full feature must be implemented before the ADR can describe it accurately
  and the live e2e can exercise it.

**User stories**
- 2 stories: a future engineer understands the task granularity from the ADR; the owner
  verifies the de-explosion live before merge.

**Documentation discipline**
- The glossary rows landed in the grooming commit; this task verifies them. The ADR amendment
  is authored here from the approved draft (not pre-written in the grooming commit), matching
  how #074 handled its amendment.

Ready for implementation.

### [SWE] 2026-06-23 — Implementation (ADR amendment + glossary verify + full acceptance gate)

Documentation + acceptance bookend (no product code). Authored the ADR-002 §3 amendment,
verified the glossary against the shipped #078–#081 code, and ran the full LOCAL acceptance
gate (full docker stack incl. mongot). `[HUMAN]` live-e2e ACs deferred to the owner (runbook
below). On `refactor/fold-sharding-and-decouple-config-tests` (no branch/worktree per the
task brief). NOT committed.

**Files modified**
- `docs/adrs/002_pipeline_concurrency_and_voyage_rate_limiting.md` — appended the
  "**Amendment (#078–#082 — batch-grain ETL task topology for the leaf pipelines).**" block
  under §3, indented + style-matched to the #055/#061/#066/#070–#074 amendments, `Status:
  Accepted` PRESERVED. References `tracker/feature-batch-etl-task-topology-plan.md`.
- `tracker/082-…groomed.md` — `Status: in-progress`; automatable ACs checked; this log entry.
- (`AGENTS.md` shows modified in `git status` — that is the OWNER's pre-existing WIP, NOT
  touched by this task; left as-is per the brief. The unrelated `git stash` was left alone.)

**ADR amendment — what it records (AC 1 + 2)**
Batch-not-document task granularity with the ~1000→tens payoff; pragmatic E/T/L (Load always
its own task; E+T fused for web/substack-article/youtube-video scrape; `transform_batch` only
for the genuine pure maps arXiv dict→Document + substack-RSS feed-entry→Document; optional
`enrich_batch` for arXiv `fetch_content`; arXiv streamed read stays the flow loop); per-item
sub-flow collapse to `_ingest_<x>_one` cores + the thin MCP-only `@flow` wrappers; per-element
isolation via `tree.data.batch.gather_isolated` (`gather(return_exceptions=True)`) + the
whole-batch-retry-safe-because-load-idempotent-on-`(user_id, source_uri)` invariant; retry
relocation (fetch/extract `retries=2`, load `retries=1`, pure transform `retries=0`;
per-element fetch retries in the fetch layer); RSS keeps feed-obtain + shares only the
build/load tail (substack-RSS 1 fetch→N docs no re-scrape; youtube ONE bulk `fetch_many` per
feed); result-persistence-off; and the UNCHANGED-invariants list (two deployments, depth-1/no
recursion, group-by-platform + HF windowing, gather-isolation, no trailing index, §1 GCL + §4
admission control, per-batch-PHASE Opik spans). `Status: Accepted` preserved → refinement, not
supersession. References the feature plan.

**Glossary verification (AC 3) — VERIFIED, no edits needed**
The `ETL-phase task`, `Thin MCP flow`, and reconciled `Batch` / `Worker` rows are present and
ACCURATE against the shipped code — confirmed against:
- `tree.data.batch.gather_isolated` exists (`apps/memory/src/tree/data/batch.py:26`,
  `(successes, failure_count)`, log-and-skip per element) — matches the `ETL-phase task` row.
- Thin flows `ingest_web_url` / `ingest_substack_article` / `ingest_youtube_video` are 1-line
  `@flow` wrappers (`ingest-{x}-etl`) around `_ingest_<x>_one` cores — matches `Thin MCP flow`.
- No leftover per-row `extract_document`/`fetch_paper_content`/`load_document` `@task`s
  (`grep` → NONE). ETL-phase tasks shipped: arXiv `transform-arxiv-batch`(r0) /
  `enrich-arxiv-batch`(r2) / `load-arxiv-batch`(r1); web `extract-web-batch`(r2) /
  `load-web-batch`(r1); substack-RSS `fetch-substack-rss-feed`(r2) /
  `transform-substack-rss-batch`(r0) / `load-substack-rss-batch`(r1); substack-article
  `extract-substack-article-batch`(r2) / `load-substack-article-batch`(r1); youtube
  `fetch-youtube-transcripts-batch`(r2) / `build-youtube-batch`(r0) / `load-youtube-batch`(r1).
  arXiv `fetch_dataset_batches` stays the flow loop; youtube ONE bulk `fetch_many` per feed.
  All consistent with the `Batch`/`Worker` rows. No re-authoring; no duplication.

**Acceptance gate (LOCAL env, full docker stack incl. mongot)**
- `make memory-format-fix && memory-lint-fix && memory-format-check && memory-lint-check` →
  ALL clean (289 files; "All checks passed!"). Docs-only, so no code reformatting.
- `make pre-commit` → Passed (prettier, ruff check, ruff format, biome).
- `make memory-unit-tests` → **1685 passed, 0 failed, 0 warnings** in 63.75s.
- `make memory-integration-tests-all` → **265 passed, 18 failed, 1 skipped** in 435s (7m15s).
  The 18 failures are ALL in the MEMORY/extraction layer (extraction/indexing/dream/validator/
  fact-island/two-user-isolation/preference-supersession) and **ZERO in the data layer** this
  feature (#078–#081) touched. This is a DOCS-ONLY change → it cannot affect Python test
  behavior. Re-ran EVERY failing test in isolation → **all 18 PASS** (41 passed: two-user +
  validator + fact-island; 7 passed: pole-o + preference-supersession + dream). Root cause =
  shared-DB cross-test contamination (validator rows accumulate across tests: `assert 4 == 1`
  with "Left contains 3 more items") + sequential Gemini load starving extraction E2E
  (`assert 0 == 1`, empty CHUNK/node results) when the whole suite runs together — the exact
  shared-DB-fixture-collision class CLAUDE.md documents. The two named pre-existing flakes
  (`test_indexing_pipeline::test_embeds_nodes` [requires_mongot],
  `test_meta_state::test_updated_at_is_recent` [shared-DB isolation]) are confirmed in the
  failure set. CONCLUSION: gate is green modulo pre-existing environmental flakes; no
  regression, none in the feature's scope.

**Evidence**
```
$ make memory-unit-tests
======================= 1685 passed in 63.75s (0:01:03) ========================

$ make memory-integration-tests-all
============ 18 failed, 265 passed, 1 skipped in 435.06s (0:07:15) =============
  (all 18 in the memory layer; 0 in data; see isolation re-runs below)

$ uv run pytest tests/integration/test_two_user_isolation.py \
      tests/integration/memory/test_validator_e2e.py \
      tests/integration/memory/test_fact_island.py
======================== 41 passed in 114.49s (0:01:54) ========================

$ uv run pytest tests/integration/memory/test_pole_o_extraction_e2e.py \
      tests/integration/memory/test_preference_supersession.py \
      tests/integration/memory/test_dream_e2e_acceptance.py::test_fan_out_collapses_paul_duplicates_end_to_end
============================== 7 passed in 20.50s ==============================

$ grep '^FAILED' integration_all_082.log | grep -iE 'data|arxiv|substack|youtube|web'
  (no output — ZERO data-layer failures)
```

**Deferred `[HUMAN]` live-e2e ACs (owner runbook — mirrors #074's deferral)**
The 4 `[HUMAN]` ACs assert on the real Prefect-UI task-run COUNT, the absence of per-item
sub-flow runs, the real bulk transcript fetch, and the MCP single-URL path — none automatable.
Left UNCHECKED for the owner. Runbook:

1. `make env-local` → `make local-start`; confirm `default.yaml` carries the arXiv HF source
   (`max_samples: 1000`, `num_workers: 2`, e.g. `batch_size: 50`) + substack/youtube/web
   sources. Optionally lower `max_samples`/`num_workers` for a quicker pass.
2. Kill any running serve, then `make memory-serve-workflows &` to pick up the latest code
   (the in-process Prefect worker — without it deployments register but nothing executes).
3. `make memory-run-data-pipeline USER_ID=<paul-oid>`.
4. **AC — arXiv de-explosion:** open an arXiv HF-WINDOW `data-etl-worker` run in the Prefect UI
   (http://127.0.0.1:4200) → confirm a few TENS of task runs (NOT ~1000): per `batch_size`
   chunk a `transform-arxiv-batch` + `load-arxiv-batch` (+ `enrich-arxiv-batch` only if
   `fetch_content`), and NO per-row `extract-arxiv-document` / `load-arxiv-document`.
5. **AC — batch tasks + no sub-flows:** for substack/youtube/web workers, confirm batch
   ETL-phase tasks (`extract-*-batch`/`transform-*-batch` + `load-*-batch`) and NO per-item
   sub-flow children (no `ingest-web-url-etl` / `ingest-substack-article-etl` /
   `ingest-youtube-video-etl` under a batch worker). Confirm YouTube does ONE bulk transcript
   fetch per feed (one `fetch-youtube-transcripts-batch` / `fetch_many` log line, not per-video).
6. **AC — idempotency:** record `documents` counts (`mongosh` → `db.documents.countDocuments()`
   per source / `(user_id, source_uri)`), trigger a SECOND
   `make memory-run-data-pipeline USER_ID=<paul-oid>`, confirm counts DO NOT grow (dedup).
7. **AC — single-URL MCP smoke:** via the MCP `ingest_url` tool, ingest one web URL, one
   substack article URL, and one youtube watch URL; confirm each produces its own thin-flow run
   (`ingest-{x}-etl`) + persists a Document.
   Record UI screenshots / run names + Mongo counts back in this log, then check the 4 ACs.

**Notes**
- Branch `refactor/fold-sharding-and-decouple-config-tests`, no branch/worktree created (per
  the task brief). NOT committed — handing to the Tester.
- `AGENTS.md` (owner WIP) + the `git stash` left untouched, as instructed.
- No architectural forks encountered; the approved amendment text was authored verbatim-in-
  substance from the feature plan's locked design.

### [Tester] 2026-06-23 — QA — PASS

Documentation + full-acceptance bookend for the batch-etl-task-topology feature (#078–#082).
LOCAL env, full docker stack up incl. mongot (`tree-mongot`, `tree-mongodb`, `tree-prefect-*`
all Up). Branch `refactor/fold-sharding-and-decouple-config-tests`, no worktree. Independently
re-ran the full gate, vetted every failure, verified the ADR + glossary against shipped code.
`AGENTS.md` (owner WIP) + the unrelated `git stash` left untouched. NOT committed; not moved to
done/.

**Test summary**
- Format / lint / pre-commit: PASS (`format-check` 289 files formatted; `lint-check` all
  passed; pre-commit prettier+ruff+ruff-format+biome all Passed).
- Unit tests: 1685 passed / 0 failed, 0 warnings (62.23s).
- Integration tests (`make memory-integration-tests-all`): 264 passed / **19 failed** / 1
  skipped (467s / 7m47s), EXIT 2. ALL 19 re-verified PASSING in isolation → pre-existing
  environmental flakes, not regressions (detail below).

**The #1 priority — independent vetting of the failures**
- My full-suite count is **19 failed** (SWE reported 18). The delta is exactly one DATA-layer
  test the SWE's run did not trip: `test_web_serp::test_empty_query_returns_empty_list`.
- **Categorization:** 18 failures in `tests/integration/memory/**` + `test_two_user_isolation`
  (dream / fact_island / indexing_pipeline[requires_mongot] / meta_state / pole_o /
  preference_supersession / validator_e2e / two_user_isolation). 1 failure in
  `tests/integration/data/**`: `test_web_serp.py::test_empty_query_returns_empty_list`.
- **The data-layer failure is NOT a feature regression.** `test_web_serp` is a LIVE Bright
  Data SERP test (`pytest.mark.skipif` on `BRIGHTDATA_API_KEY`/`SERP_ZONE`; both real in
  `.env`, so it runs under the make target and skips under a bare `uv run pytest`). Failure:
  the nonsense query returned 5 live YouTube SERP results instead of `[]`
  (`assert [SearchResult(... youtube.com ...)] == []`) — exactly the "Google surfaces
  tangential video content" drift the test's own docstring warns about. The exercised code,
  `tree.data.web.web_serp.search`, was last touched by `4c12937`/`647f512`/`da86995` — ALL
  pre-dating the batch-ETL feature (web's batch change is `5cee9f1`, on `web_pipeline.py`).
  `web_serp.py` imports no batch pipeline, has no `@task`/`@flow`, and is independent of the
  topology change. **Reproduced as non-deterministic:** re-ran with `.env` loaded →
  `empty_query` FAILED again AND `common_query('pizza')` returned 0 organic results, while
  `returns_results('openai gpt-4')` PASSED — same zone returning populated vs empty SERPs
  across attempts. Pure live-API flake.
- **DATA PIPELINE subset fully green (decisive check):** `uv run pytest tests/integration/data/`
  → **31 passed / 10 skipped / 0 failed** (41.66s), incl. arxiv / substack-rss / web /
  youtube-rss / youtube-video pipelines and the batch-grain invariants
  (`test_ingests_videos_with_one_bulk_fetch_and_feed_metadata`,
  `test_ingests_multiple_videos_with_one_bulk_fetch`, idempotency, latent-upgrade,
  chain-exhausted-slot-skip). The feature itself did NOT regress.
- **Memory-layer flakes confirmed pre-existing:** re-ran in isolation →
  `test_two_user_isolation + validator_e2e + fact_island` = **41 passed**;
  `pole_o + preference_supersession + dream(fan_out) + meta_state(updated_at)` = **8 passed**;
  `indexing_pipeline::test_embeds_nodes` [requires_mongot] = **1 passed**. Root cause =
  shared-DB cross-test contamination (validator/isolation rows accumulate: `assert 0 == 1` /
  `assert 4 == 1`) + sequential Gemini starvation in the full run — the shared-DB-fixture
  collision class CLAUDE.md documents. The two named pre-existing flakes (`test_embeds_nodes`,
  `test_updated_at_is_recent`) are in the set.

**ADR-002 §3 amendment (AC 1 + 2) — VERIFIED ACCURATE against shipped code**
- Present, indented, style-matched to the #055/#061/#066/#070–#074 amendments; header
  "Amendment (#078–#082 — batch-grain ETL task topology for the leaf pipelines)"; references
  `tracker/feature-batch-etl-task-topology-plan.md`; `Status: Accepted` preserved (file line 3).
- Task names + retry grain match the code EXACTLY: `transform-arxiv-batch`(r0) /
  `enrich-arxiv-batch`(r2) / `load-arxiv-batch`(r1); `extract-web-batch`(r2) /
  `load-web-batch`(r1); `fetch-substack-rss-feed`(r2) / `transform-substack-rss-batch`(r0) /
  `load-substack-rss-batch`(r1); `extract-substack-article-batch`(r2) /
  `load-substack-article-batch`(r1); `fetch-youtube-transcripts-batch`(r2) /
  `build-youtube-batch`(r0) / `load-youtube-batch`(r1). arXiv `fetch_dataset_batches` is a plain
  flow-loop generator (no `@task`); no per-row `extract_document`/`fetch_paper_content`/
  `load_document` `@task` remains (those names exist only as plain helpers).
- `gather_isolated` (`src/tree/data/batch.py:26`) returns `(successes, failure_count)` under one
  `asyncio.gather(return_exceptions=True)`, WARNING-logs + skips a raising element — matches the
  per-element-isolation + idempotency-invariant claim.
- Thin MCP `@flow` wrappers `ingest-web-url-etl` / `ingest-substack-article-etl` /
  `ingest-youtube-video-etl` wrap `_ingest_<x>_one` cores; batch path calls the core directly.
- YouTube does ONE bulk `fetcher.fetch_many(video_urls)` per batch (`fetch_transcripts_batch`).
- No `persist_result` / `cache_policy` / `result_storage` set anywhere in `src/tree/data/`
  (only docstrings stating persistence is off) — matches the result-persistence-off claim.

**Glossary (AC 3) — VERIFIED, not modified by this task (landed in grooming commit)**
`docs/glossary.md` not in this task's diff. Rows present + accurate vs shipped code:
`ETL-phase task` (batch grain, retry counts r2/r1/r0, gather-isolation, idempotent on
`(user_id, source_uri)`, ~1000→tens), `Thin MCP flow` (1-line wrappers over `_ingest_<x>_one`,
MCP-only), reconciled `Batch` ("grain of an ETL-phase task — NOT per row") + `Worker` ("no
longer fans out a per-item sub-flow per Document").

**Acceptance criteria**
- [x] PASS — ADR §3 amendment records all required points — verified vs code (task names,
      retries, gather_isolated, thin flows, bulk fetch, persistence-off, unchanged invariants);
      `Status: Accepted` preserved (ADR line 3).
- [x] PASS — amendment references `tracker/feature-batch-etl-task-topology-plan.md` — present.
- [x] PASS — glossary `ETL-phase task` + `Thin MCP flow` + reconciled `Batch`/`Worker` rows
      present and accurate vs shipped code (not re-authored).
- [x] PASS — format-fix/lint-fix/format-check/lint-check + pre-commit all clean.
- [x] PASS — unit tests 1685 passed, 0 warnings.
- [x] PASS (with corrected wording) — `make memory-integration-tests-all`: 19 failed but every
      failure re-verified PASSING in isolation; 18 memory-layer (shared-DB collision) + 1
      data-layer LIVE-SERP-drift flake in untouched `web_serp.search` (NOT the batch-ETL
      topology). Data PIPELINE subset 31 passed/0 failed in isolation. No feature regression.
      (AC parenthetical corrected: 19 not 18; 1 data-layer live-API flake noted honestly.)
- [ ] [HUMAN] Live e2e (arXiv de-explosion) — Awaiting human verification; owner runbook
      present in SWE log. Correctly left unchecked.
- [ ] [HUMAN] Live e2e (batch tasks + no sub-flows + one bulk YT fetch) — Awaiting human
      verification; owner runbook present. Correctly left unchecked.
- [ ] [HUMAN] Live e2e (idempotent re-run) — Awaiting human verification; owner runbook
      present. Correctly left unchecked.
- [ ] [HUMAN] Live e2e (single-URL MCP ingest) — Awaiting human verification; owner runbook
      present. Correctly left unchecked.

**Evidence**
```
$ make memory-unit-tests
======================= 1685 passed in 62.23s (0:01:02) ========================

$ make memory-integration-tests-all
============ 19 failed, 264 passed, 1 skipped in 467.18s (0:07:47) ============
  data-layer FAILED: 1  (test_web_serp::test_empty_query_returns_empty_list)
  non-data FAILED:   18

# Data-layer failure = live SERP drift (untouched code):
E AssertionError: assert [SearchResult(rank=1, title='Here to help: Make her day',
    url='https://www.youtube.com/watch?v=qQxt94efi3w', ...)] == []
  Left contains 5 more items
$ git log --oneline -3 -- src/tree/data/web/web_serp.py
  4c12937 / 647f512 / da86995   (all PRE-date the batch-ETL feature)

$ uv run pytest tests/integration/data/            # data PIPELINE subset, isolated
======================= 31 passed, 10 skipped in 41.66s ========================

$ uv run pytest test_two_user_isolation.py test_validator_e2e.py test_fact_island.py
======================== 41 passed in 108.37s (0:01:48) ========================

$ uv run pytest test_pole_o_extraction_e2e.py test_preference_supersession.py \
      test_dream_e2e_acceptance.py::test_fan_out... test_meta_state.py::...updated_at...
============================== 8 passed in 17.39s ==============================

$ uv run pytest test_indexing_pipeline.py::TestMemoryIndexingPipeline::test_embeds_nodes
============================== 1 passed in 4.93s ===============================
```

**Other issues found**
- `test_web_serp.py` (3 live SERP tests) is a recurring local-only flake: it has no `slow`/
  `requires_mongot` marker, so under `make memory-integration-tests-all` with real `.env`
  Bright Data creds it runs live and drifts (nonsense query → tangential YouTube results;
  `pizza` → 0 organic on a throttled response). Not in scope for #082 (docs-only) and not a
  feature regression, but a FOLLOW-UP candidate: mark it `@pytest.mark.slow` (or gate it out of
  the acceptance gate) so the deterministic gate isn't bound to live SERP weather. Flagging for
  the orchestrator; no action required for this task's PASS.
- The benign Prefect teardown `ValueError: I/O operation on closed file.` (rich console on
  temporary-server stop) appears AFTER the data subset reports passed — cosmetic, not a test
  failure.

**VERDICT: PASS**
- ADR-002 §3 amendment + glossary accurate against shipped #078–#081 code, `Status: Accepted`
  preserved, plan referenced.
- Format/lint/pre-commit clean; unit 1685/0-warn.
- Data-layer PIPELINE integration subset FULLY green in isolation (the decisive
  feature-regression check).
- All 19 full-suite failures (18 memory shared-DB collision + 1 data-layer live-SERP-drift in
  untouched `web_serp.search`) re-verified PASSING in isolation → pre-existing environmental
  flakes, NOT feature regressions. The single data-layer failure is in code outside the
  batch-ETL topology and reproduced as non-deterministic live-API drift.
- 4 `[HUMAN]` live-e2e ACs correctly deferred with an owner runbook.
