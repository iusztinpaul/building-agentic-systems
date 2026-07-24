---
id: 095-batch-retry-waste-and-shard-success-masking
feature: brightdata-youtube-transcripts-followups
status: done
---

# Stop discarding paid transcripts on batch retry + stop counting failed workers as succeeded

Tags: `data`, `infra`
Depends on: None (both surfaced during PR #34; part B is older)
Implements: —

Two independent defects, both found during the `brightdata-youtube-transcripts` review
and both about failures being silently absorbed. They are bundled by the project owner's
explicit request; keep the two parts separate in the diff and in the log.

## Part A — a Gemini exception discards already-paid-for Bright Data transcripts

Found by the Tester on #092, accepted there as non-blocking because it matches the
pre-existing whole-batch-failure pattern in `apps/memory/src/tree/data/batch.py`.

In `youtube_ingest.fetch_transcripts_batch`, an unexpected exception escaping Gemini's
bulk `fetch_many` (anything other than the three named `BrightData*Error`s) fails the
whole Prefect task. Prefect then retries the task from the top — re-running the Bright
Data collection and **re-billing it** — even though those transcripts had already been
fetched successfully moments earlier. The cost is real but bounded: `retries=2` on the
task, so a single bad Gemini call can bill Bright Data three times for the same batch.

Fix so that a Gemini-side failure cannot destroy Bright Data work already done. Options
worth weighing in the log before choosing:
- Catch broadly around the Gemini fallback call and treat its failure as "no Gemini
  rescue available" — the transcript-less slots become `no_transcript:` Ingest error
  rows (already a supported outcome), the Bright Data successes still land, and the task
  completes. Simple, no new state.
- Anything requiring cross-retry persistence is almost certainly over-built for this;
  argue for it explicitly if you disagree.

Whatever you pick, a Gemini outage must not silently degrade into lost data: the
outcome has to be visible in the logs and in `ingest_error`.

## Part B — coordinator counts a hard-failed worker as succeeded

Pre-existing, unrelated to Bright Data, confirmed by the Tester via an empty
`git log main..HEAD` on the file. Observed live during #093 run 3: two workers hard-FAILED
and the coordinator still reported `succeeded=1 failed=0`.

`apps/memory/src/tree/data/offline_pipeline.py:459-465` counts a shard as succeeded
whenever `run_deployment` RETURNS, without inspecting the returned flow run's terminal
state. A worker that raises still reports success, so the fan-out summary — the operator's
main signal that an offline run worked — can read green while data is missing. Inspect
the returned flow-run state and count only genuinely-Completed runs.

Check whether the memory-pipeline coordinator shares this pattern; if it does, fix both
and say so, rather than leaving a known-broken twin.

## Acceptance criteria

- [x] **Part A:** a Gemini `fetch_many` raising an unexpected exception no longer fails
      the task; Bright Data transcripts fetched in that batch still land as Documents,
      and the un-rescued slots become `no_transcript:` Ingest error rows. Unit-asserted
      with an exception injected at the Gemini seam.
- [x] **Part A:** the Gemini failure is visible — a WARNING naming the failure and the
      affected slot count; no silent data loss.
- [x] **Part A:** the chosen approach, and why the alternatives were rejected, recorded
      in the log.
- [x] **Part B:** a shard whose `run_deployment` returns a FAILED/CRASHED flow run is
      counted as failed, not succeeded; the fan-out summary reports it. Unit-asserted
      against each non-Completed terminal state, not just one.
- [x] **Part B:** a statement in the log on whether the memory-pipeline coordinator
      shares the pattern, and if so, that it was fixed too.
- [x] Parts A and B are separable in the diff (distinct files; ideally distinct commits).
- [x] NO live Bright Data or Gemini call in any committed test (ADR-004 Decision 8).
- [x] `make memory-format-check && make memory-lint-check && make pre-commit` clean;
      `make memory-unit-tests` green, 0 warnings; `make memory-integration-tests` green
      (Part B touches the coordinator).

## Out of scope

- Rewriting the shared whole-batch-failure contract in `data/batch.py` — Part A is a
  targeted fix at the YouTube fallback chain, not a redesign of batch semantics.
- Retry-count or backoff tuning on any Prefect task.

## Log

### [SWE] 2026-07-24 19:14 — Implementation

Two defects, kept strictly separate in the diff: Part A touches only
`data/youtube/youtube_ingest.py`, Part B only `sharding.py` +
`data/offline_pipeline.py` + `memory/extraction/sharding.py`. No file carries both.

**Files modified — Part A (paid transcripts survive a dead Gemini)**

- `apps/memory/src/tree/data/youtube/youtube_ingest.py` — new `_fetch_fallback`
  helper (mirrors the existing `_fetch_primary` shape) wraps the bulk Gemini call;
  new `_STATE_FETCH_FAILED` state string feeds the normalized `no_transcript:` error.
- `apps/memory/tests/unit/data/youtube/test_youtube_ingest.py` — `TestGeminiFallbackFailure`
  (5 tests) + one end-to-end `_bulk_build_and_load` test.

**Approach chosen (Part A), and the alternatives weighed**

Chosen: catch broadly *around the Gemini call only* and treat its failure as "no
Gemini rescue available" — the spec's preferred shape. The Bright Data slots that
already have transcripts are kept, the un-rescued slots become `no_transcript:
brightdata returned empty; gemini unavailable (fetch failed)` rows, and the task
COMPLETES, so Prefect never retries and never re-runs the billable Bright Data
collection. Zero new state, and it makes the Gemini branch symmetric with the
Bright Data branch, which has absorbed batch-wide failures this way since #092.

Rejected:

- *Persisting the fetched transcripts across retries* (cache / checkpoint keyed on
  the batch): needs a store, an invalidation rule and a key design to save a
  bounded 2 extra collections; over-built exactly as the spec suspected. It also
  would not fix the visible symptom — the run still fails.
- *Splitting the Prefect task into fetch-primary / fetch-fallback tasks* so only
  the Gemini task retries: real per-phase retry granularity, but it changes the
  task topology (two task boundaries, two result serializations) for one failure
  mode, and a Gemini retry is billable too. Out of proportion.
- *Narrow `except (GoogleAPIError, httpx.HTTPError)`*: the whole point is the
  UNEXPECTED exception; enumerating today's SDK error types leaves the same hole
  open for tomorrow's.

**What is caught, precisely** — `except Exception`, so `BaseException` still
propagates: `asyncio.CancelledError` (a `BaseException` since 3.8) and
`KeyboardInterrupt` mean the RUN is going away, not that Gemini is down, and
swallowing them would write `ingest_error` rows for a batch nobody asked to
finish. Unit-asserted in `test_base_exceptions_still_propagate`. To keep a genuine
programming bug in the Gemini path visible rather than silently degraded, the
WARNING carries the exception TYPE and message plus `exc_info=True` (full
traceback), and every affected slot still produces a persisted `ingest_error` row
— a Gemini outage is loud in both the logs and the DB.

**Files modified — Part B (a hard-failed worker is no longer counted green)**

- `apps/memory/src/tree/sharding.py` — new pure `_shard_failure_reason(result)`:
  the single "did this shard's flow run genuinely COMPLETE?" rule. Uses Prefect's
  own `State.is_completed()` (no state-name string matching). Also treats a
  gathered `BaseException` as failure — the old `isinstance(result, Exception)`
  check silently counted a gathered `CancelledError` as a success.
- `apps/memory/src/tree/data/offline_pipeline.py` — `_fan_out_data` applies it.
- `apps/memory/src/tree/memory/extraction/sharding.py` — `_fan_out_extraction`
  applies it.
- `apps/memory/tests/prefect_doubles.py` (new) — `completed_flow_run()` /
  `flow_run_in_state()`, shared by the unit and integration fakes.
- `apps/memory/tests/unit/test_sharding.py`, `tests/unit/data/test_fanout_data.py`,
  `tests/unit/memory/extraction/test_fanout.py` — new tests; every pre-existing
  `run_deployment` fake in those files (plus `tests/unit/data/test_coordinator_data.py`
  and `tests/integration/memory/test_extraction_fanout.py`) now returns a real
  `FlowRun`, the shape `run_deployment` actually returns. A double that returns
  `None` is no longer faithful, and that unfaithfulness is what let this defect
  live.

**Does the memory-pipeline coordinator share the pattern? YES — fixed too.**
`_fan_out_extraction` (`memory/extraction/sharding.py:226`) had the byte-identical
loop and is fixed in the same way, through the same shared helper, with its own
parametrized Failed/Crashed/Cancelled tests. A third fan-out,
`_fan_out_dreams` (`memory/consolidation/dream.py:947`), is NOT affected: it
awaits the in-process `dream_consolidation` flow function directly (no
`run_deployment`), so a failure raises and is already counted. Left alone.

Verified against the real API before choosing the rule: `arun_deployment` with the
default `timeout=None` polls `read_flow_run` until `state.is_final()` and returns
that refreshed run, so the returned state is genuinely terminal on our call path.

**Noted, NOT fixed (adjacent, deliberately out of scope):** the trailing single
`memory-indexing-etl` dispatch in `_fan_out_extraction` also ignores its returned
state. It is not part of `FanOutStats` at all, so reporting it is a different
question (does a failed index run make the coordinator run failed?) — a behaviour
decision beyond this task. Worth a follow-up task.

**Tests**

- Unit: 1868 passing, 0 failing, 0 warnings (`make memory-unit-tests`).
- Integration fast tail: 169 passing, 1 skipped, 2 failing — both the KNOWN
  pre-existing ones named in the task (`test_indexing_pipeline.py::test_embeds_nodes`,
  `test_meta_state.py::TestRecordDreamRun::test_updated_at_is_recent`); not chased.
- Integration slow tail: 105 passing, 0 failing — includes
  `test_extraction_fanout.py` (10 passed), which exercises the changed coordinator
  spy.
- Red-first evidence for Part B: with the two fan-out source files stashed, the new
  tests fail with `DataFanOutStats(shards_total=1, succeeded=1, failed=0)` — the
  exact #093 symptom.
- NO live Bright Data or Gemini call anywhere: Part A patches the two in-task
  fetcher construction points, Part B patches `run_deployment` (ADR-004 §8).

**Acceptance criteria**

- [x] Part A: Gemini exception no longer fails the task; BD transcripts land, misses
      become `no_transcript:` rows — `test_youtube_ingest.py::TestGeminiFallbackFailure::test_brightdata_transcripts_survive_a_gemini_exception`
      and `::TestBulkBuildAndLoad::test_gemini_exception_still_persists_the_brightdata_documents`
- [x] Part A: failure visible in logs + `ingest_error` — `::TestGeminiFallbackFailure::test_warns_naming_the_failure_and_the_slot_count`,
      `::test_un_rescued_slots_name_the_failed_gemini_call`
- [x] Part A: approach + rejected alternatives recorded (above)
- [x] Part B: each non-Completed terminal state counts as failed —
      `test_fanout_data.py::TestNonCompletedWorkerRuns::test_terminal_non_completed_state_counts_as_failed[Failed|Crashed|Cancelled]`,
      the memory twin in `test_fanout.py`, and the rule itself in
      `test_sharding.py::test_non_completed_terminal_state_is_a_failure`
- [x] Part B: memory coordinator statement (above) — shares it, fixed
- [x] Parts A and B separable (disjoint file sets)
- [x] No live backend call in any test
- [x] format-check / lint-check / pre-commit clean; unit green; integration green
      modulo the two known pre-existing failures

**Evidence**

```
$ make memory-format-fix && make memory-lint-fix && make memory-format-check \
    && make memory-lint-check && make pre-commit
295 files left unchanged
All checks passed!
295 files already formatted
All checks passed!
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed

$ make memory-unit-tests
============================ 1868 passed in 42.20s =============================

$ make memory-integration-tests
FAILED tests/integration/memory/test_indexing_pipeline.py::TestMemoryIndexingPipeline::test_embeds_nodes
FAILED tests/integration/memory/test_meta_state.py::TestRecordDreamRun::test_updated_at_is_recent
===== 2 failed, 169 passed, 1 skipped, 105 deselected in 193.63s (0:03:13) =====
   (both KNOWN pre-existing / unrelated, per the task's own note)

$ make memory-integration-tests-slow
tests/integration/memory/test_extraction_fanout.py ..........            [ 51%]
=============== 105 passed, 172 deselected in 400.89s (0:06:40) ================
```

End-to-end (a scratch driver on the REAL code paths, both backends faked — no
network, no billing; the pipeline itself was run live in #093):

```
=== Part A: Gemini blows up mid-batch ===
WARNING | tree.data.youtube.youtube_ingest - Falling back to Gemini for 1/2 videos (reason=no_brightdata_transcript) — consumes Gemini tokens and incurs API cost
WARNING | tree.data.youtube.youtube_ingest - Gemini fallback failed for 1/2 videos (RuntimeError: 503 Service Unavailable from generativelanguage) — keeping the Bright Data transcripts already fetched and recording ingest_error rows for the rest
WARNING | tree.data.youtube.youtube_ingest - No transcript for https://www.youtube.com/watch?v=AAAaaaBBBcc (no_transcript: brightdata returned empty; gemini unavailable (fetch failed))
KEPT (Bright Data, already paid for): ['https://www.youtube.com/watch?v=eYaWxljC4sA']
ingest_error rows: [('https://www.youtube.com/watch?v=AAAaaaBBBcc', 'no_transcript: brightdata returned empty; gemini unavailable (fetch failed)')]

=== Part B: one worker hard-FAILS, one completes ===
ERROR | tree.data.offline_pipeline - data fan-out: shard 1 FAILED (isolated): flow run finished in state Failed (Flow run encountered an exception)
SUMMARY: DataFanOutStats(shards_total=2, succeeded=1, failed=1, failures={'1': 'flow run finished in state Failed (Flow run encountered an exception)'})
```

**Notes**

- No ADR change needed: Part A's outcome is ADR-004 Decision 6's existing
  `no_transcript: …` shape (a new backend-state phrase, not a new failure kind),
  and Decision 3's batch-wide-fallback contract is unchanged.
- Behaviour change to be aware of when reading fan-out stats: a shard is now
  counted succeeded ONLY on a COMPLETED flow run. A result with no readable state
  counts as FAILED — we never report success for an outcome we could not read.
  Unreachable on our call path (`run_deployment` always returns a stateful
  `FlowRun`), but it is why every test double had to start returning one.
- NOT COMMITTED — awaiting Tester review.

### [Tester] 2026-07-24 20:05 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`ruff format --check` 295 files unchanged, `ruff
  check` clean, `pre-commit run --all-files` all hooks Passed)
- Unit tests: 1868 passed / 0 failed
- Integration tests (fast tail): 169 passed / 1 skipped / 2 failed — both the KNOWN
  pre-existing failures named in the task (`test_indexing_pipeline.py::test_embeds_nodes`,
  `test_meta_state.py::TestRecordDreamRun::test_updated_at_is_recent`); reproduced
  independently, unrelated to this diff
- Integration tests (slow tail, incl. `test_extraction_fanout.py`): 105 passed / 0 failed
- Warnings: 0 (pytest summary line carries no "N warnings" suffix on any run; the one
  `UserWarning` seen is opik's pre-existing Python-3.14/pydantic-v1 import-time notice,
  printed before collection, not a pytest-collected test warning)

**E2E adversarial pass**

Part A (Gemini fallback failure absorption), driven directly against
`fetch_transcripts_batch.fn` / `_bulk_build_and_load` with both fetchers patched at
their construction seam (no live calls), Beanie initialized against a scratch local
Mongo DB (`init_mongodb`, dropped after):
- Happy path: `fetch_transcripts_batch.fn(items)` with BD answering every slot →
  2/2 transcribed, 0 failed, task completes cleanly (PASS)
- Break path 1 (BD already covered every slot, Gemini configured but should never be
  invoked): Gemini fetcher wired to raise `RuntimeError("SHOULD NEVER BE CALLED")` →
  0 Gemini calls recorded, 2/2 transcribed (PASS) — confirms the fallback seam is
  correctly gated on `missing`, not unconditionally invoked
- Break path 2 (Gemini is the ONLY configured backend and it dies —
  `brightdata_api_key=None`, Gemini raises `RuntimeError`): task completed (no
  exception propagated to the caller), 2/2 slots became `no_transcript: brightdata
  not configured; gemini unavailable (fetch failed)` rows (PASS)
- Break path 3 (mixed batch: BD answers 1/3, Gemini dies on the other 2, run through
  the FULL `_bulk_build_and_load` incl. `build_batch`/`load_batch`, with
  `load_video_document` faked to just record what's persisted): the BD-answered slot
  landed as a real, fully-built `Document` (`content` populated, `ingest_error=None`)
  in the persisted set and in the reported "ingested" return value; the two
  Gemini-missed slots persisted as `ingest_error` rows with `content=None` (PASS) —
  this is the acceptance-criterion-3 "must not lose data" claim, verified against the
  real build/load code path, not just the fetch seam
- `asyncio.CancelledError` / `KeyboardInterrupt` inheritance verified directly:
  `issubclass(asyncio.CancelledError, Exception) == False`,
  `issubclass(asyncio.CancelledError, BaseException) == True`,
  `issubclass(KeyboardInterrupt, Exception) == False` — confirms `except Exception`
  in `_fetch_fallback` genuinely does not catch either, matching the log's claim
  (also unit-asserted in `test_base_exceptions_still_propagate`, which independently
  passes)

Part B (shard-outcome classification), driven directly against `_fan_out_data` /
`_fan_out_extraction` / `_shard_failure_reason` with fake `run_deployment`s (no
Prefect server needed for the pure classifier; a real temporary Prefect server was
used for the Part A task-decorated calls):
- Happy path: 3-shard fan-out, all `run_deployment` fakes return a real `COMPLETED`
  `FlowRun` → `succeeded=3, failed=0` (also covered by the unchanged pre-existing
  suite, all passing)
- Break path 1 (mixed: one shard raises `RuntimeError`, one returns a `CRASHED`
  `FlowRun`, one `COMPLETED`): `DataFanOutStats(shards_total=3, succeeded=1,
  failed=2, failures={'1': 'worker process died', '2': 'flow run finished in state
  Crashed (OOM killed)'})` (PASS) — both a raised exception AND a returned-but-failed
  state are counted correctly and separately
- Break path 2 (gathered `CancelledError`, the claimed "bonus" fix): a shard's
  `run_deployment` raises `asyncio.CancelledError()` inside the gather →
  `succeeded=0, failed=1`. Confirmed the OLD `isinstance(result, Exception)` check
  would have missed this entirely (`isinstance(asyncio.CancelledError(), Exception)
  == False`), i.e. it would have been silently counted a SUCCESS pre-fix — the bonus
  defect claim is real, not just asserted
- Break path 3 (a shard result that is neither a completed run nor a recognised
  failure state — the exact case the task called out): a bare object with no
  `.state` attribute → `"flow run reported no terminal state"`; a bare `None` →
  same; a `FlowRun` in a non-terminal `RUNNING` state → `"flow run finished in
  state Running"`. None of the three is ever treated as success (PASS). Note: the
  `RUNNING` wording ("finished in state Running") reads oddly since the run hasn't
  actually finished — cosmetic only, the classifier still never mis-reports
  non-terminal/unreadable results as success, and this branch is unreachable on the
  real `run_deployment(timeout=None)` call path (verified below)
- Break path 4 (extraction fan-out: `Failed` + `Cancelled` + `Completed` across 3
  shards): `succeeded=1, failed=2`, and the single trailing `memory-indexing-etl`
  run still fired exactly once — partial-extraction-still-indexes behaviour
  preserved (PASS)

**Design-authority verification (done directly against the installed libraries, not
taken on the SWE's word)**
- `prefect.client.schemas.objects.State.is_completed()` source: `return self.type ==
  StateType.COMPLETED` — a plain type check, exactly as claimed
- `prefect.deployments.run_deployment` docstring/signature: `timeout: Optional[float]
  = None`, "Setting timeout to None will allow this function to poll indefinitely" —
  and neither of the two coordinator call sites (`offline_pipeline.py:458`,
  `memory/extraction/sharding.py:220`) passes an explicit `timeout`, so the default
  poll-to-final-state behavior applies on the real call path — confirms the "no
  readable state" branch is genuinely a defensive-only path, not a live gap
- `_fan_out_dreams` (`memory/consolidation/dream.py:920`) read directly: it calls
  `await runner(user_id=..., dry_run=..., **runner_kwargs)` under `try/except
  Exception`, and `runner=dream_consolidation` (an in-process flow function, not a
  `run_deployment` dispatch) is the only place it's wired
  (`dream_consolidation_all_users`, same file). No `run_deployment` call anywhere in
  this fan-out — the "left alone, failures already raise and are caught" claim is
  correct as read, not just as claimed
- No other `run_deployment(` call site exists in `apps/memory/src/tree` besides the
  three already accounted for (two fan-out gathers, now fixed; the trailing
  `memory-indexing-etl` dispatch, explicitly noted as out of scope and not in
  `FanOutStats`) — confirmed via grep, the blast-radius statement is complete
- `tree.data.batch.gather_isolated` (the pre-existing whole-batch-isolation helper
  Part A's log cites as precedent) already classifies with `isinstance(result,
  BaseException)`, not `Exception` — the `_shard_failure_reason` choice to do the
  same is consistent with existing house style, not a one-off

**Widened blast radius — verdict**
- Fixing the memory-extraction coordinator's twin was explicitly requested by the
  task text ("Check whether the memory-pipeline coordinator shares this pattern; if
  it does, fix both") — correct, not scope creep.
- Read every rewritten pre-existing `run_deployment` fake
  (`test_fanout_data.py`, `test_fanout.py`, `test_coordinator_data.py`,
  `test_extraction_fanout.py`): each one only gained `return completed_flow_run()`
  (or an equivalent explicit `FlowRun`) on top of its existing call-recording /
  raise-on-condition logic. Every pre-existing assertion (dispatch names, shard
  params, isolation counts, `succeeded=N`/`failed=N` stats) is byte-identical to
  before the diff — none of them now asserts less than they used to. A `None`
  double genuinely was unfaithful to the real `run_deployment` return shape, so
  updating them was the correct fix, not a way to mask a regression.

**Acceptance criteria**
- [x] PASS — Part A: Gemini `fetch_many` raising an unexpected exception no longer
      fails the task; BD transcripts land as Documents; un-rescued slots become
      `no_transcript:` rows — unit: `test_youtube_ingest.py::TestGeminiFallbackFailure::test_brightdata_transcripts_survive_a_gemini_exception`,
      `::TestBulkBuildAndLoad::test_gemini_exception_still_persists_the_brightdata_documents`
      (both pass); e2e adversarial break path 3 above re-verifies the same claim end
      to end through the real `build_batch`/`load_batch` code, not just the fetch seam
- [x] PASS — Part A: failure visible — a WARNING naming the exception type, message,
      and slot count (`exc_info=True`) —
      `::TestGeminiFallbackFailure::test_warns_naming_the_failure_and_the_slot_count`
      passes; manually observed: `Gemini fallback failed for 2/3 videos (RuntimeError:
      gemini 500) — keeping the Bright Data transcripts already fetched and recording
      ingest_error rows for the rest`
- [x] PASS — Part A: approach + rejected alternatives recorded in the log (persistence
      across retries, task-splitting, and narrow-exception-typing all explicitly
      weighed and rejected with reasons) — present above in the SWE's log entry
- [x] PASS — Part B: a shard whose `run_deployment` returns a FAILED/CRASHED/CANCELLED
      flow run is counted failed, not succeeded, asserted against each state —
      `test_sharding.py::test_non_completed_terminal_state_is_a_failure[Failed|Crashed|Cancelled]`,
      `test_fanout_data.py::TestNonCompletedWorkerRuns` (data),
      `test_fanout.py::TestNonCompletedWorkerRuns` (memory) all pass; e2e adversarial
      break paths 1 and 4 above re-verify against the real `_fan_out_data` /
      `_fan_out_extraction` functions, not just the pure classifier
- [x] PASS — Part B: memory-pipeline coordinator statement in the log — confirmed
      accurate by direct code read of `_fan_out_dreams` (shares nothing —
      `run_deployment`-free — correctly left alone) and `_fan_out_extraction`
      (byte-identical pattern, fixed with the same helper and its own
      Failed/Crashed/Cancelled tests)
- [x] PASS — Parts A and B separable: disjoint file sets confirmed via `git diff
      --stat` (Part A touches only `youtube_ingest.py` + its test; Part B touches
      `sharding.py` + `offline_pipeline.py` + `memory/extraction/sharding.py` + their
      tests + the new shared `prefect_doubles.py`); no file carries both parts' logic
- [x] PASS — no live Bright Data or Gemini call in any committed test — grepped for
      `httpx.AsyncClient()`/`requests.`/`aiohttp.` in every touched test file: none
      found; both fetchers are patched at their construction seam throughout
- [x] PASS — `make memory-format-check && make memory-lint-check && make pre-commit`
      clean; `make memory-unit-tests` 1868 passed / 0 warnings; `make
      memory-integration-tests` 169 passed / 1 skipped / 2 known-pre-existing failures
      (reproduced independently); `make memory-integration-tests-slow` 105 passed
      (incl. `test_extraction_fanout.py` 10 passed)

**Evidence**

```
$ make memory-format-check && make memory-lint-check
295 files already formatted
All checks passed!

$ make pre-commit
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed

$ make memory-unit-tests
============================ 1868 passed in 40.11s =============================

$ make memory-integration-tests
FAILED tests/integration/memory/test_indexing_pipeline.py::TestMemoryIndexingPipeline::test_embeds_nodes
FAILED tests/integration/memory/test_meta_state.py::TestRecordDreamRun::test_updated_at_is_recent
===== 2 failed, 169 passed, 1 skipped, 105 deselected in 198.64s (0:03:18) =====

$ make memory-integration-tests-slow
tests/integration/memory/test_extraction_fanout.py ..........            [ 51%]
=============== 105 passed, 172 deselected in 371.19s (0:06:11) ================
```

Adversarial scripts (scratchpad, run against the real code, no live network):
```
=== Part A break path 1: BD covers every slot ===
transcribed=2 failed=0 gemini_calls=[]
PASS: Gemini never invoked when BD covered every slot

=== Part A break path 3: mixed batch persisted end-to-end ===
ingested (reported) = ['https://www.youtube.com/watch?v=AAAAAAAAAA1']
persisted (all, incl. failure rows) = [
  ('https://www.youtube.com/watch?v=AAAAAAAAAA1', None),
  ('https://www.youtube.com/watch?v=AAAAAAAAAA2', 'no_transcript: brightdata returned empty; gemini unavailable (fetch failed)'),
  ('https://www.youtube.com/watch?v=AAAAAAAAAA3', 'no_transcript: brightdata returned empty; gemini unavailable (fetch failed)')]
PASS: BD success persisted as a real Document; Gemini misses became ingest_error rows

=== Part B break path 2: gathered CancelledError ===
DataFanOutStats(shards_total=1, succeeded=0, failed=1, failures={'0': 'CancelledError'})
PASS: CancelledError from a shard is counted as FAILED, not success

=== Part B break path 3: unrecognised/non-terminal state ===
no .state attr -> 'flow run reported no terminal state'
None result -> 'flow run reported no terminal state'
RUNNING state -> 'flow run finished in state Running'
PASS: every unrecognised/non-completed shape is treated as a failure, never a silent success
```

**Other issues found**
- Cosmetic only: `_shard_failure_reason`'s message for a non-terminal state (e.g.
  `RUNNING`) reads "flow run finished in state Running", which is self-contradictory
  wording (it hasn't finished). Never causes a mis-classification — every non-
  `COMPLETED` shape still correctly counts as a failure — and this branch is
  unreachable on the real `run_deployment(timeout=None)` call path per the SWE's own
  verification (confirmed independently above). Worth a one-line wording tweak
  (e.g. "flow run is in state …") in a follow-up, not blocking.
- `except Exception` breadth in `_fetch_fallback` (Part A) is the deliberate,
  correctly-scoped tradeoff the task asked to weigh: a genuine programming bug in
  the Gemini path (e.g. an `AttributeError` from a future SDK shape change) is now
  absorbed into a `no_transcript:` row instead of failing loud. The WARNING carries
  exception type + message + `exc_info=True` (full traceback in the logs), and every
  affected slot still produces a persisted, queryable `ingest_error` row — so the bug
  is discoverable (grep the logs, or query `ingest_error` for the
  `unavailable (fetch failed)` phrase) even though it no longer aborts the run. This
  matches the task's own explicit request ("a Gemini outage must not silently
  degrade into lost data: the outcome has to be visible in the logs and in
  `ingest_error`") and is not a regression from the pre-existing narrower behavior
  (before this fix, an unexpected Gemini exception just failed the task 3x over,
  which is arguably worse for debuggability, not better, since it hid the paid-data-
  loss problem this task exists to fix).
- The trailing `memory-indexing-etl` dispatch in `_fan_out_extraction` still ignores
  its returned state (noted, not fixed, correctly out of scope per the log) — a
  reasonable follow-up task, not a blocker for this one.

**VERDICT: PASS**
