# ADR-002 amendment + full acceptance + live e2e

Status: in-progress
Tags: `data`, `infra`, `docs`, `adr`
Depends on: #070, #071, #072, #073
Blocks: —

## Scope

Close out the `data-platform-sharding-hf-windows` feature: VERIFY the ADR-002 §3 amendment
(already landed during `/plan` on approval), run the FULL acceptance suite, and perform the
`[HUMAN]` live end-to-end verification on the real Prefect stack. No new product code —
this is the documentation + acceptance bookend (mirrors #069's role for the prior
feature).

### 1. Verify the ADR-002 §3 amendment (landed during `/plan`)

The amendment was written to
`docs/adrs/002_pipeline_concurrency_and_voyage_rate_limiting.md` §3 during planning (the
`/plan` ADR gate), in the SAME style as the existing #055/#061/#066 amendments (an indented
"**Amendment (#… — title).**" block under §3), `Status: Accepted` preserved. This task only
CONFIRMS it is present and accurate against the shipped code — do NOT re-author or
duplicate it. It must capture:

- **The data fan-out axis shifts from count-based source-shards to group-by-platform.**
  One `data-etl-worker` run per non-HuggingFace platform bucket (substack / youtube /
  custom), each a homogeneous single-platform shard. The platform map is recorded
  verbatim. Replaces #068's `_partition_into_shards(sources, num_shards)` count-based
  partition for the DATA pipeline (the memory document-shard axis is UNCHANGED, and the
  shared `tree.sharding` helpers remain — only the data orchestrator stops using them).
- **HuggingFace gets an offset-window sub-fan-out.** `num_workers` worker runs per HF
  entry, window `i` = `(offset=i*window_size, max_samples=window_size)` with
  `window_size = max_samples // num_workers` and the last window taking the remainder.
  Implemented via `IterableDataset.skip(offset)` before the streaming loop. Record that
  `skip` is O(offset) but bounded by the `max_samples` cap, and that the
  `split_dataset_by_node` upgrade for a FUTURE uncapped whole-dataset run is the
  documented (out-of-scope) successor path.
- **The data `num_shards` knob is DROPPED** (orchestrator param + script flag + Make
  thread). Parallelism is now declared per-source (automatic platform bucketing + HF
  `num_workers`). The MEMORY pipeline keeps its `num_shards`.
- **`concurrency.runner_global_limit` raised 4 → 6** in `default.yaml`, with the
  justification: data workers are NOT Voyage-bound and the `voyage-embeddings` GCL still
  caps embedding, so over-admitting is safe; the bump accommodates the wider data fan-out
  (platform buckets + HF windows queuing through the shared `serve(limit=…)` slots). Note
  the frozen test fixture stays at 4 by design.
- **Unchanged invariants** (so Status stays Accepted): still two deployments per pipeline;
  depth-1 dispatch with NO recursion (a worker never calls `run_deployment`);
  `asyncio.gather(return_exceptions=True)` failure-isolation; NO trailing index for data;
  the `voyage-embeddings` GCL (§1) and `serve(limit=…)` admission control (§4); idempotency
  via `load_document`'s `(user_id, source_uri)` dedup over disjoint windows.

Reference this feature's plan
(`tracker/feature-data-platform-sharding-hf-windows-plan.md`) from the amendment's
context line, matching how prior amendments reference their plans.

### 2. Full acceptance suite

Run the complete acceptance gate per CLAUDE.md, on the LOCAL env, with the full
docker-compose stack up (mongot included), in isolation (the shared stack collides across
worktrees):

- `make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check`
- `make pre-commit`
- `make memory-unit-tests`
- `make memory-integration-tests-all` (~5 min; includes `@pytest.mark.slow` +
  `requires_mongot`) — the same target CI runs.

### 3. `[HUMAN]` live end-to-end

With the stack up (`make local-start`) and `make memory-serve-workflows` running
(re-served to pick up the new code), and `default.yaml` carrying a HuggingFace source with
`max_samples` and `num_workers` set (e.g. `max_samples: 40, num_workers: 4` for a fast,
visible window split) plus several Substack/YouTube/web sources:

1. Run `make memory-run-data-pipeline USER_ID=<oid>`.
2. In the Prefect UI confirm: ONE `data-etl-orchestrator` parent run; one
   `data-etl-worker` child per non-HF platform present (substack / youtube / custom); and
   exactly `num_workers` additional `data-etl-worker` children for the HuggingFace windows.
3. Confirm NO index run fires.
4. Confirm the arXiv windows are DISJOINT: inspect the worker logs for the
   `offset=0/10/20/30`-style window markers (#071's log line), and/or query Mongo to
   confirm the ingested arXiv `documents` count ≈ `max_samples` (not `max_samples *
   num_workers` and not a single window's worth) and that no `source_uri` is duplicated.
5. Trigger a SECOND run and confirm idempotency: the arXiv `documents` count does not grow
   (dedup on `(user_id, source_uri)` over the same disjoint windows).

Record the outcomes (UI screenshots or the run/child names + the Mongo counts) in the task
log.

### Files touched

- `docs/adrs/002_pipeline_concurrency_and_voyage_rate_limiting.md` — append the §3
  amendment.
- (no product code) — acceptance + live e2e are verification, not code changes. If the
  acceptance run surfaces a regression, fix it under the relevant earlier task's scope, not
  here.

## Acceptance Criteria

- [x] ADR-002 §3 carries a new amendment (style-matching #055/#061/#066, `Status:
      Accepted`) recording: the group-by-platform data fan-out axis (with the platform
      map), the HuggingFace offset-window sub-fan-out (window math + `skip` + the O(offset)
      caveat + the `split_dataset_by_node` future path), the dropped data `num_shards`, and
      the `runner_global_limit` 4→6 bump with its justification (incl. the fixture-stays-at-4
      note).
- [x] The amendment explicitly lists the UNCHANGED invariants (two deployments, depth-1 /
      no-recursion dispatch, `gather(return_exceptions=True)`, no trailing index for data,
      §1 GCL + §4 admission control, idempotency) and references the feature plan.
- [x] `make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check && make pre-commit` all clean.
- [x] `make memory-unit-tests` passes, 0 warnings.
- [x] `make memory-integration-tests-all` passes on a quiesced + isolated mongot stack
      (LOCAL env), exit 0.
- [ ] [HUMAN] Live e2e: with the stack up + workflows served, `make memory-run-data-pipeline
      USER_ID=<oid>` shows ONE `data-etl-orchestrator` parent, one `data-etl-worker` per
      non-HF platform, and exactly `num_workers` HF window workers in the Prefect UI; NO
      index run fires.
- [ ] [HUMAN] The arXiv windows are disjoint: ingested arXiv `documents` count ≈
      `max_samples` (not `max_samples * num_workers`), no duplicate `source_uri`, and the
      per-worker logs show the distinct `offset` window markers.
- [ ] [HUMAN] A second run does not grow the arXiv `documents` count (idempotency over
      disjoint windows + `(user_id, source_uri)` dedup).

## BDD scenarios

### Scenario: the ADR records the new topology without superseding
- **Given** the appended ADR-002 §3 amendment
- **When** a reader looks up how the data pipeline fans out
- **Then** they find the group-by-platform axis + HF offset windows + dropped `num_shards`
  + the `runner_global_limit` bump, with `Status: Accepted` preserved and the unchanged
  invariants enumerated.

### Scenario: the full acceptance gate is green
- **Given** the feature implemented through #073
- **When** I run the full CLAUDE.md acceptance sequence on the LOCAL isolated stack
- **Then** format/lint/pre-commit are clean, unit tests pass with 0 warnings, and
  `make memory-integration-tests-all` exits 0.

### Scenario: live fan-out matches the spec in the Prefect UI
- **Given** the stack up, workflows served, and `default.yaml` with a HF source at
  `max_samples: 40, num_workers: 4` plus substack/youtube/web sources
- **When** the operator runs `make memory-run-data-pipeline USER_ID=<oid>`
- **Then** the UI shows one orchestrator parent, one worker per non-HF platform, and 4 HF
  window workers; no index run fires; and the arXiv windows are disjoint (count ≈ 40, no
  duplicate `source_uri`).

## User Stories

### Story: A future engineer understands the data fan-out from the ADR
1. A new engineer reads ADR-002 §3 to understand pipeline parallelism.
2. The amendment tells them the data pipeline groups sources by platform and windows
   HuggingFace by offset, that `num_shards` was dropped for data (kept for memory), and why
   `runner_global_limit` is 6 — without having to read the flow code.
3. They see the documented `split_dataset_by_node` upgrade path for an uncapped run and
   know it's intentionally not implemented yet.

### Story: The owner verifies the feature live before merge
1. The owner brings the stack up, re-serves workflows, and triggers the data pipeline.
2. The Prefect UI shows the expected platform + HF-window worker topology and no index run.
3. The owner confirms the arXiv windows are disjoint and a re-run is idempotent, then
   approves the PR.

## Test guidance

- This task's automated portion is the FULL acceptance gate (no new product tests — the
  per-task suites in #070–#073 own coverage). Run via `make memory-*` targets on the LOCAL
  env with the full stack up, in isolation per CLAUDE.md.
- The `[HUMAN]` live e2e is a manual Prefect-UI + Mongo verification — it cannot be
  automated (it asserts on the real dataset stream, the real worker fan-out, and the UI
  parent/child rendering). Record evidence in the log.
- The ADR amendment is prose authored from the approved draft (handed back in grooming);
  no test, but it is an AC that the file carries the amendment.

---

Blocked by: #070, #071, #072, #073

## Log

### [PA] 2026-06-22 14:20 — Grooming

**Summary**
Feature bookend: author the ADR-002 §3 amendment (group-by-platform data fan-out, HF
offset-window sub-fan-out, dropped data `num_shards`, `runner_global_limit` 4→6 with
justification, unchanged-invariants list, Status stays Accepted), run the full
`make memory-integration-tests-all` acceptance gate, and perform the `[HUMAN]` live e2e
(Prefect-UI parent/child topology + disjoint arXiv windows + idempotent re-run). No new
product code.

**Key decisions**
- The amendment is style-matched to #055/#061/#066 (indented §3 block, Status Accepted) —
  a topology refinement of the same fan-out decision, not a new ADR file, per the owner's
  brief. Draft text was handed back at grooming for the human gate; this task writes the
  approved text.
- Acceptance follows CLAUDE.md exactly: LOCAL env, full stack incl. mongot, isolated run,
  `make memory-integration-tests-all` as the gate CI mirrors.
- The live e2e adds a disjoint-window + idempotency check (Mongo count ≈ max_samples, no
  duplicate source_uri, re-run doesn't grow) because that's the user-visible payoff of the
  HF windowing and can't be asserted in unit tests.

**Dependencies**
- #070–#073 — the full feature must be implemented before the ADR can describe it
  accurately and the live e2e can exercise it.

**User stories**
- 2 stories: a future engineer understands the fan-out from the ADR; the owner verifies
  the feature live before merge.

Ready for implementation.

### [SWE] 2026-06-22 21:05 — Acceptance bookend (verification + full gate; NO product code)

**Files modified**
- `tracker/074-adr-amendment-and-live-e2e-acceptance.groomed.md` — checked the 5 automatable
  ACs, set status `in-progress`, appended this log (the 3 `[HUMAN]` ACs stay UNCHECKED —
  deferred below). No product code, no ADR edit (amendment verified accurate as-shipped).

**ADR-002 §3 amendment accuracy — VERDICT: ACCURATE (no edit needed)**
The "Amendment (#070–#074 — platform-grouped data fan-out + HuggingFace offset-windowing)"
block (`docs/adrs/002_pipeline_concurrency_and_voyage_rate_limiting.md` lines 209–272,
`Status: Accepted` preserved) was cross-checked against the committed #070–#073 code; every
claim matches:
- Group-by-platform fan-out + platform map → `_NON_HF_PLATFORMS` in `data/pipeline.py:392`
  (`substack` {RSS,article} / `youtube` {RSS,video} / `custom` {web}); HF handled separately.
- HF offset-window sub-fan-out (window math, `skip`, O(offset) caveat, `split_dataset_by_node`
  future path) → `arxiv_window_entries` (`huggingface/arxiv_dataset_pipeline.py:23`) +
  `fetch_dataset_batches(..., offset)` using `IterableDataset.skip(offset)`
  (`huggingface/arxiv_dataset.py:108`). `window_size = max_samples // num_workers`, last window
  takes the remainder; `num_workers=1` ⇒ `offset` unset (byte-identical to prior run).
- New config fields `num_workers: int = Field(default=1, ge=1)` + `offset: int | None = None`
  on `HuggingFaceDatasetSource` (`app_config.py:350,353`).
- Data `num_shards` DROPPED (orchestrator param + `run_data_pipeline.py` flag + Makefile
  `run-data-pipeline` `NUM_SHARDS` thread all gone); memory keeps its own (still threaded in
  `run_memory_pipeline.py` + `run-memory-pipeline-extraction`). Shared `tree.sharding` helpers
  retained.
- `runner_global_limit` 4→6 in `configs/default.yaml:181`; typed `ConcurrencyConfig` default
  stays `4` (`app_config.py:242`) and the frozen fixture stays `4`
  (`tests/unit/config/fixtures/frozen_config.yaml:110`, asserted by
  `test_app_config.py::test_typed_default_runner_global_limit_unchanged`).
- Unchanged-invariants list (2 deployments, depth-1/no-recursion, `gather(return_exceptions=True)`,
  no trailing index, §1 GCL + §4 admission, `(user_id, source_uri)` idempotency) — all hold in
  `_fan_out_data` / `data_etl_worker` / `load_document`.
- References the feature plan `tracker/feature-data-platform-sharding-hf-windows-plan.md`
  (ADR line 217; plan file present).

**Acceptance gate (LOCAL env, full docker stack incl. mongot — verified `make env-status` →
local; `tree-mongodb`/`tree-mongot`/`tree-prefect-server`/`tree-prefect-worker` all Up)**
- `make memory-format-check` → exit 0 (282 files already formatted).
- `make memory-lint-check` → exit 0 (All checks passed).
- `make pre-commit` → all hooks Passed (prettier, ruff check, ruff format, biome).
- `make memory-unit-tests` → **1597 passed**, 0 failed, 0 warnings (46.61s).
- `make memory-integration-tests-all` → **282 passed, 1 skipped, 0 failed** in 461.83s
  (~7.7 min), exit 0. The 1 skip is `data/web/test_web_search_ingest.py` (network/credential
  gated). This is the same target CI runs.

**Acceptance criteria**
- [x] ADR-002 §3 amendment present + accurate — verified against shipped #070–#073 code (above).
- [x] Amendment lists unchanged invariants + references the feature plan.
- [x] format/lint/pre-commit clean.
- [x] `make memory-unit-tests` passes, 0 warnings (1597 passed).
- [x] `make memory-integration-tests-all` passes (282 passed / 1 skipped), exit 0.
- [ ] [HUMAN] Live e2e Prefect-UI fan-out topology — DEFERRED (cannot automate).
- [ ] [HUMAN] Disjoint arXiv windows in Mongo — DEFERRED (cannot automate).
- [ ] [HUMAN] Idempotent re-run — DEFERRED (cannot automate).

**DEFERRED TO [HUMAN] — live e2e (3 ACs above)**
These assert on the real HuggingFace stream, real worker fan-out, and the Prefect-UI
parent/child rendering; they cannot be automated. Owner steps to close them:
1. Ensure `configs/default.yaml` carries the arXiv HF source with `max_samples: 40` and
   `num_workers: 4`, plus several Substack/YouTube/web sources (one per platform you want to
   see fan out). `num_workers` is YAML-authored; `offset` must NOT be set in YAML.
2. Bring the stack up: `make local-start`. Re-serve to pick up current code:
   `make memory-serve-workflows &` (kill any prior serve first so it loads the latest code).
3. Trigger: `make memory-run-data-pipeline USER_ID=<oid>` (or `USER_IDENTIFIER=<handle>`).
4. In the Prefect UI confirm the topology: ONE `data-etl-orchestrator` parent run; one
   `data-etl-worker` child per non-HF platform present (`substack` / `youtube` / `custom`);
   and exactly **4** additional `data-etl-worker` children for the HF offset-windows. Confirm
   NO index run fires (data pipeline has no trailing index).
5. Confirm DISJOINT windows: in the worker logs look for the `offset=0/10/20/30` window markers
   (`window_size = 40 // 4 = 10`), and/or query Mongo:
   `db.documents.countDocuments({ user_id: ObjectId("<oid>"), source_type: "huggingface" })`
   → should be ≈ **40** (NOT 160 = 40×4, NOT 10 = one window), with no duplicate `source_uri`
   (e.g. `db.documents.aggregate([{ $match: { user_id: ObjectId("<oid>"), source_type:
   "huggingface" } }, { $group: { _id: "$source_uri", n: { $sum: 1 } } }, { $match: { n: { $gt:
   1 } } }])` returns empty).
6. Idempotency: trigger a SECOND `make memory-run-data-pipeline USER_ID=<oid>` and re-run the
   count from step 5 — it must NOT grow (dedup on `(user_id, source_uri)` over the same disjoint
   windows; upsert, never double-insert).

**Notes**
- NO product code changed and NO ADR edit needed — the amendment was already authored during
  `/plan` and is accurate as-shipped. This task is the verification + acceptance bookend only.
- Observed (out-of-scope, NOT a regression introduced here): `huggingface/arxiv_dataset.py:33`
  uses `except ValueError, TypeError:` (Python-2-style two-class except without parens). The
  pinned interpreter in this repo (CPython 3.14.4, "Clang 22.1.3" build) ACCEPTS this grammar
  and catches BOTH classes at runtime (verified), and it's a pre-existing line outside the
  #070–#073 window change set, so I left it untouched. Worth a follow-up to parenthesize
  (`except (ValueError, TypeError):`) for portability to stock CPython, but it is not in this
  task's scope and does not affect the gate (full suite green on the deployed interpreter).
- DO NOT COMMIT / DO NOT move to done — handed back to the orchestrator for the commit and the
  `[HUMAN]` live e2e sign-off.
