# ETL-task-granularity ADR + glossary verify + full acceptance + live e2e

Status: pending
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

- [ ] ADR-002 §3 carries a new amendment (style-matching #070–#074, `Status: Accepted`
      preserved) recording: batch-not-document task granularity (with the ~1000→tens
      payoff), pragmatic E/T/L (Load always separate; E+T fused for scrape pipelines;
      transform_batch where it's a pure map; enrich_batch for arXiv content; streamed read
      stays a flow loop), the per-item sub-flow collapse + thin MCP-only flow, per-element
      isolation + whole-batch-retry-safe-because-load-idempotent, retry relocation to
      batch+fetch-layer, RSS keeps feed-obtain + shares only build/load (no re-fetch),
      result-persistence-off, and the unchanged-invariants list.
- [ ] The amendment references the feature plan
      `tracker/feature-batch-etl-task-topology-plan.md`.
- [ ] The glossary **ETL-phase task** + **Thin MCP flow** rows (and the reconciled
      **Batch**/**Worker** rows) are present and accurate against the shipped code (verified,
      not re-authored).
- [ ] `make memory-format-fix && make memory-lint-fix && make memory-format-check &&
      make memory-lint-check && make pre-commit` all clean.
- [ ] `make memory-unit-tests` passes, 0 warnings.
- [ ] `make memory-integration-tests-all` passes on a quiesced + isolated mongot stack
      (LOCAL env), exit 0.
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
