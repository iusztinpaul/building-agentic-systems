---
id: 174-offline-pipeline-fails-on-partial-ingest
status: done
feature: prod-backfill-hardening
---

# The offline pipeline fails when anything was ingested only partially

Tags: `memory`, `data`, `prefect`, `offline`
Depends on: —
Blocks: —

## Problem

On prod (2026-10-02) an extraction shard died with `AutoReconnect` after loading 4,376 of 10,081
documents. `offline-pipeline` logged `extraction: … shards=1 succeeded=0 failed=1` and still finished
**Completed**; `make memory-run-pipeline` printed `Done. Flow completed successfully.` and exited 0. A
half-ingested backfill looks green.

Today every level isolates failures and then forgets them:

- `apps/memory/src/tree/offline.py` `offline_pipeline`: per-user `try/except` around extraction,
  indexing, clustering records `{"error": …}`; `FanOutStats.failed` / `DataFanOutStats.failed` are
  logged only. The flow returns normally.
- `apps/memory/src/tree/data/batch.py` `gather_isolated` returns `(successes, failure_count)` for
  per-item (article / video / dataset row) failures — check whether that count reaches the data
  coordinator's stats; if it does not, it must.

## Scope (human decision: any partial ingest is an error)

1. Keep the isolation: every phase still runs for every user/shard (one bad shard must not stop the
   others, and indexing still embeds what DID land).
2. After all requested phases finish, `offline_pipeline` aggregates every failure:
   - data: failed shards AND failed items inside successful shards (per-item count from
     `gather_isolated`, propagated through the data worker → coordinator → `DataFanOutStats`);
   - extraction: `FanOutStats.failed > 0` or a per-user exception;
   - indexing / clustering: a per-user exception.
   If the total is non-zero, it RAISES one typed error (e.g. `PartialIngestError(RuntimeError)`) whose
   message names every failing phase + user + count + the first failure message per shard, e.g.
   `offline-pipeline finished with failures: extraction user=6abf… 1/1 shards failed (AutoReconnect: …);
   data 3 items failed in shard 2`. The flow run ends **Failed**.
3. The CLI (`tree.cli.wait_for_flow_run` / the `run_*` scripts) then exits non-zero and does NOT print
   "Flow completed successfully" for a Failed run (verify; fix if it does).
4. Same rule for the online pipeline (`apps/memory/src/tree/online.py`) if it has the same swallow
   pattern — check and apply, or note why not.
5. A clustering corpus SKIP (`skipped_reason`) stays a non-failure, and a no-op run (all phases off,
   or 0 pending docs) stays Completed.

## Acceptance criteria

- [x] Unit: extraction coordinator returns `FanOutStats(failed=1)` → `offline_pipeline` still runs
      indexing for that user, then raises `PartialIngestError`; message names `extraction`, the user and
      the shard error.
- [x] Unit: data coordinator with a per-item failure in an otherwise successful shard → the flow raises
      after all phases; the item count appears in `DataFanOutStats` and in the message.
- [x] Unit: per-user indexing / clustering exception → raises after the remaining users ran.
- [x] Unit: all clean (incl. clustering SKIP and zero pending docs) → returns the result dict, no raise.
- [x] Unit: the CLI wait path exits non-zero with the failure message on a Failed run.
- [x] Docstrings of `offline_pipeline` (Returns / Raises) updated; the README / e2e skill line that
      describes the run outcome updated if it says partial runs complete.
- [x] Local e2e (LOCAL env only): force one shard to fail (e.g. a source that 404s, or a monkeypatched
      worker) and show the `make memory-run-pipeline` terminal ends with the failure and a non-zero exit.

## Log

### [SWE] 2026-10-02 21:00 — Implementation

**Design**
- Per-item count: `gather_isolated` (the ONE function every per-item failure routes through — ~10 call sites in 5 platform pipelines) also adds to an `ItemFailureTally` held in a `ContextVar`, active only inside `track_item_failures()`. The tally object is mutable, so it survives Prefect copying the context into `@task`s and inline subflows (proved by a test through a real `@task` + `@flow`). No return-type plumbing.
- Worker → coordinator channel: there is no result persistence, so the flow-run state message is the only channel across the `run_deployment` hop. `data_etl_worker` runs the whole shard, then raises `PartialIngestError("items_failed=<n>; first: <Type: msg>")` (worker run ends Failed). `_fan_out_data` parses the token (`parse_items_failed`, next to its formatter `item_failures_message` in `tree/data/batch.py`): a match counts the shard `succeeded` and adds `n` to the new `DataFanOutStats.items_failed` / `item_failures`; anything else stays a failed shard. `_shard_failure_reason` (shared with extraction) untouched.
- `PartialIngestError(RuntimeError)` lives in `tree/flow_runs.py` (neutral top-level module; `tree.data` and `tree.offline` both import it).
- `offline_pipeline`: isolation unchanged (every phase still runs for every user); AFTER all phases, `_partial_ingest_failures(result)` collects data failed shards + failed items, extraction `failed > 0` or per-user exception, indexing/clustering per-user exception, and the flow raises one `PartialIngestError` (inside the existing `try/finally`, so `flush_opik` still runs). Clustering SKIP, `summaries_failed`, zero sources / zero pending docs, all-phases-off stay Completed. Added one parent-level `data: shards=… succeeded=… failed=… items_failed=…` line (the data phase had none, so its outcome never reached the terminal).
- CLI: `wait_for_flow_run` already exited 1 on a non-Completed run; it now also logs the state message (`Flow finished with state: Failed — <message>`).
- Online pipeline (Scope 4): NOT changed. Its only swallow is `_run_indexing`'s fail-open, which ADR-002 (Accepted, §3 amendment) states explicitly: "Its FAIL-OPEN contract is unchanged: a failure is WARNING-logged and never fails the ingest". A single online ingest otherwise has no isolation (data step and extraction failures already fail the run). Changing it means amending ADR-002 — PA call.

**Files modified**
- `apps/memory/src/tree/data/batch.py` — `ItemFailureTally`, `track_item_failures`, `item_failures_message` / `parse_items_failed`; `gather_isolated` feeds the active tally.
- `apps/memory/src/tree/flow_runs.py` — `PartialIngestError`.
- `apps/memory/src/tree/data/offline_pipeline.py` — worker tallies + raises; `DataFanOutStats.items_failed` / `item_failures`; fan-out classification; coordinator aggregation (keyed `user_id:shard`).
- `apps/memory/src/tree/offline.py` — `_partial_ingest_failures`, final raise, data parent log line, docstring (Returns / Raises).
- `apps/memory/src/tree/cli.py` — failure message on the exit-1 line.
- Tests: `tests/unit/data/test_batch.py`, `tests/unit/data/test_offline_pipeline.py`, `tests/unit/data/test_fanout_data.py`, `tests/unit/data/test_coordinator_data.py`, `tests/unit/test_offline.py`, `tests/unit/test_cli.py`.

**Tests**
- Unit: 4672 passing, 0 failing (`make memory-tests`). New tests confirmed red against the old code (offline: 9 failed; CLI message test: 1 failed).
- Integration: N/A — the project has no integration suite.

**Acceptance criteria**
- [x] Extraction `FanOutStats(failed=1)` → indexing still runs, then raises — `test_offline.py::TestPartialIngestFailsTheRun::test_a_failed_extraction_shard_is_indexed_then_fails_the_run`
- [x] Per-item failure in an otherwise successful shard → raises after all phases; count in `DataFanOutStats` + message — `test_offline.py::TestPartialIngestFailsTheRun::test_failed_items_in_a_data_shard_fail_the_run_after_every_phase`, `test_fanout_data.py::TestItemFailuresInsideAShard::test_counts_the_shard_succeeded_and_its_items_failed`, `test_coordinator_data.py::test_failed_items_are_aggregated_per_user_and_shard`, `data/test_offline_pipeline.py::TestDataWorkerItemFailures::test_failed_items_fail_the_run_after_the_whole_shard_ran`, `test_batch.py::TestItemFailureTally::*`, `test_batch.py::TestItemFailuresMessage::*`
- [x] Per-user indexing / clustering exception → raises after the remaining users ran — `test_offline.py::TestOfflineIndexingPhase::test_one_users_indexing_failure_is_isolated`, `TestOfflineClusteringPhase::test_one_users_clustering_failure_is_isolated`, `TestEtlOffline::test_one_users_extraction_failure_is_isolated`
- [x] All clean (clustering SKIP, zero pending docs) → result dict, no raise — `TestPartialIngestFailsTheRun::test_a_clean_run_with_a_clustering_skip_returns_its_result`, `test_a_cluster_summary_fallback_is_not_a_failure`, `test_zero_pending_documents_and_zero_sources_complete`, plus the existing all-phases-off test
- [x] CLI wait path exits non-zero with the failure message — `test_cli.py::TestWaitForFlowRun::test_a_failed_run_exits_non_zero_with_its_failure_message` (+ `test_a_completed_run_reports_success_without_exiting`)
- [x] Docstrings (Returns / Raises) updated. README / `run-pipelines-e2e` skill grepped: neither says a partial run completes, so nothing to change there.
- [x] Local e2e — see Evidence.

**Evidence**
```
$ make env-status
Env target: local (.env)
$ make memory-serve-workflows &     # from this working tree; stopped afterwards
$ make memory-run-pipeline URI="https://feed.invalid/feed=substack_rss"; echo "exit=$?"
/Library/Developer/CommandLineTools/usr/bin/make -C apps/memory run-pipeline
uv run python scripts/run_pipeline.py --mode "offline"    --uri "https://feed.invalid/feed=substack_rss"   
/Users/pauliusztin/Documents/01-Projects/AI-Engineer-Handbook/building-agentic-systems/building-agentic-systems/apps/memory/.venv/lib/python3.14/site-packages/opik/rest_api/core/pydantic_utilities.py:13: UserWarning: Core Pydantic V1 functionality isn't compatible with Python 3.14 or greater.
  from pydantic.v1.datetime_parse import parse_date as parse_date
Resolved target user: id=6ac01ab8a41fbcbc30f22418 identifier=paul-iusztin-e2e
Submitted flow run afb08a14-c7d4-416e-b933-10d60b9d10aa (scheduled); waiting for it...
Track at: http://127.0.0.1:4200/runs/flow-run/afb08a14-c7d4-416e-b933-10d60b9d10aa
2026-10-02 20:57:51 | INFO    | Runner 'runner-dd65fa6f-2c49-400c-8ead-3f5545458f70' submitting flow run 'afb08a14-c7d4-416e-b933-10d60b9d10aa'
2026-10-02 20:57:51 | INFO    | Opening process...
2026-10-02 20:57:51 | INFO    | Completed submission of flow run 'afb08a14-c7d4-416e-b933-10d60b9d10aa'
2026-10-02 20:57:52 | INFO    | Downloading flow code from storage at '.'
2026-10-02 20:57:53 | INFO    | Beginning flow run 'icy-flamingo' for flow 'offline-pipeline'
2026-10-02 20:58:31 | INFO    | data: shards=1 succeeded=1 failed=0 items_failed=1
2026-10-02 20:58:32 | INFO    | extraction: user_id=6ac01ab8a41fbcbc30f22418 shards=0 succeeded=0 failed=0
2026-10-02 20:58:32 | INFO    | indexing: user_id=6ac01ab8a41fbcbc30f22418 embedded=0
2026-10-02 20:58:33 | ERROR   | Encountered exception during execution: PartialIngestError('offline-pipeline finished with failures: data 1 items failed (user=6ac01ab8a41fbcbc30f22418 shard=0: flow run finished in state Failed (Flow run encountered an exception: PartialIngestError: items_failed=1; first: ConnectError: [Errno 8] nodename nor servname provided, or not known))')
Traceback (most recent call last):
  File "/Users/pauliusztin/Documents/01-Projects/AI-Engineer-Handbook/building-agentic-systems/building-agentic-systems/apps/memory/.venv/lib/python3.14/site-packages/prefect/flow_engine.py", line 1596, in run_context
    yield self
  File "/Users/pauliusztin/Documents/01-Projects/AI-Engineer-Handbook/building-agentic-systems/building-agentic-systems/apps/memory/.venv/lib/python3.14/site-packages/prefect/flow_engine.py", line 1658, in run_flow_async
    await engine.call_flow_fn()
  File "/Users/pauliusztin/Documents/01-Projects/AI-Engineer-Handbook/building-agentic-systems/building-agentic-systems/apps/memory/.venv/lib/python3.14/site-packages/prefect/flow_engine.py", line 1610, in call_flow_fn
    result = await call_with_parameters(self.flow.fn, self.parameters)
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/Users/pauliusztin/Documents/01-Projects/AI-Engineer-Handbook/building-agentic-systems/building-agentic-systems/apps/memory/src/tree/offline.py", line 496, in offline_pipeline
    raise PartialIngestError(
        "offline-pipeline finished with failures: " + "; ".join(failures)
    )
tree.flow_runs.PartialIngestError: offline-pipeline finished with failures: data 1 items failed (user=6ac01ab8a41fbcbc30f22418 shard=0: flow run finished in state Failed (Flow run encountered an exception: PartialIngestError: items_failed=1; first: ConnectError: [Errno 8] nodename nor servname provided, or not known))
2026-10-02 20:58:33 | ERROR   | Finished in state Failed('Flow run encountered an exception: PartialIngestError: offline-pipeline finished with failures: data 1 items failed (user=6ac01ab8a41fbcbc30f22418 shard=0: flow run finished in state Failed (Flow run encountered an exception: PartialIngestError: items_failed=1; first: ConnectError: [Errno 8] nodename nor servname provided, or not known))')
Flow finished with state: Failed — Flow run encountered an exception: PartialIngestError: offline-pipeline finished with failures: data 1 items failed (user=6ac01ab8a41fbcbc30f22418 shard=0: flow run finished in state Failed (Flow run encountered an exception: PartialIngestError: items_failed=1; first: ConnectError: [Errno 8] nodename nor servname provided, or not known))
make[1]: *** [run-pipeline] Error 1
make: *** [memory-run-pipeline] Error 2
exit=2

# Clean control (nothing to do) still completes:
$ make memory-run-indexing-pipeline; echo "exit=$?"
2026-10-02 20:58:49 | INFO    | indexing: user_id=6ac01ab8a41fbcbc30f22418 embedded=0
2026-10-02 20:58:49 | INFO    | Finished in state Completed()
Done. Flow completed successfully.
exit=0
```

**Notes**
- The local DB was empty; I created a local user via `make memory-signup USER_IDENTIFIER=paul-iusztin-e2e NAME="Paul Iusztin"` (localhost only).
- Retries checked: neither `data-etl-worker` nor `offline-pipeline` carries flow-level retries (decorators, `orchestrator.py`, `deploy/`), so turning those runs Failed triggers no shard or pipeline re-run. The only flow retries are on the MCP-only `ingest-web-url-etl`, which is off the batch path. In the e2e serve log the only retries are the existing `fetch-substack-rss-feed` task retries (3 x 5 s).
- Known edge: a task retried after a BATCH-wide failure that came AFTER a `gather_isolated` call in the same attempt would count that attempt's item failures twice. No current call site raises after its gather, so no change; noted for the Tester.
- Not counted as failed items, by design: arxiv `enrich_batch` fetch failures (the doc passes through with empty content, per its own isolation comment) and YouTube **Ingest error** rows (persisted markers, per the glossary). Neither goes through `gather_isolated`.
- The task named `PartialIngestError`; it is not in `docs/glossary.md`. PA may want a glossary row, and the **Offline phase** row could say that isolated failures fail the run at the end.
- Online pipeline unchanged — see Design (ADR-002 fail-open contract).


### [Tester] 2026-10-02 21:30 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`ruff format --check` 329 files clean, `ruff check` clean, pre-commit prettier/ruff/biome Passed)
- Unit tests: 4672 passed / 0 failed (`make memory-tests`)
- Integration tests: N/A (project has none, per AGENTS.md)
- Warnings: 0

**E2E adversarial pass** (LOCAL env, `make env-status` = local; serve process run from this working tree, stopped afterwards)
- Happy path (clean): after the failing run, `make memory-run-memory-pipeline` -> `extraction: shards=1 succeeded=1 failed=0`, `indexing: embedded=0`, `Finished in state Completed()`, `Done. Flow completed successfully.`, exit 0. PASS. This also proves the failed run is recoverable: docs left pending by the failed extraction shard were picked up on the next run.
- Break 1 (state/failure mode: prod scenario, extraction shard dies with AutoReconnect): served workflows with a scratchpad `sitecustomize` that makes `init_mongodb` raise `AutoReconnect` inside `_run_extraction_worker_body` only (flag-file gated; nothing in the repo touched). After `make memory-reset-mode CONFIRM=yes` (local), `make memory-run-pipeline`: log `extraction: ... shards=1 succeeded=0 failed=1`, THEN `indexing: ... embedded=0` (phase still ran), then `offline-pipeline finished with failures: ... extraction user=6ac01ab8... 1/1 shards failed (shard=0: ... AutoReconnect: simulated ...)`, `Finished in state Failed(...)`, `Flow finished with state: Failed — ...`, no "Flow completed successfully", make exit 2. PASS. The same run also hit a REAL data item failure (default backfill feed `ReadTimeout`), reported as `data 1 items failed (user=... shard=2: ... PartialIngestError: items_failed=1; first: ReadTimeout: )` while the data log line read `shards=5 succeeded=5 failed=0 items_failed=1`; both failure classes appeared in ONE message. PASS.
- Break 2 (concurrency: tally leak across concurrent shards/tasks; script `scratchpad/qa_tally.py`): 6 concurrent asyncio tasks each in own `track_item_failures()` -> tallies [0,1,2,3,4,5] (no leak); `gather_isolated` outside a tracker returns its count and records nowhere; ThreadPoolTaskRunner flow with 3 `.submit()`ed async tasks inside a tracker -> tally 5 == 2+3+0; 3 concurrent Prefect flows with ThreadPool runners -> [3,6,12] exactly. PASS.
- Break 3 (malformed input: message parsing): `parse_items_failed` on a 100 KB multi-line failed-state message built with Prefect's `exception_to_failed_state` -> count 7 recovered (no truncation; token is at the head of the message). First error containing a second `items_failed=99` -> first (real) token wins. PASS. FINDING (low): `parse_items_failed("flow run finished in state Crashed (OOM while items_failed=0)")` returns 0, which `_fan_out_data` treats as "succeeded with 0 failed items", so a hard-failed shard whose state message coincidentally contains `items_failed=<n>` would be counted succeeded and, with n=0, be silent (green). Contrived (needs an unrelated exception text with that literal), see Other issues.
- Break 4 (state edge: successes persisted / per-user isolation): worker raises only AFTER `offline_ingest_batch` returned (batch.py/offline_pipeline.py diff), so every successful item is already persisted; e2e: the same run's data shards all `succeeded=5` and indexing still ran after the failed extraction. Per-user isolation covered by the pre-existing `TestOfflineIndexingPhase/TestOfflineClusteringPhase/TestEtlOffline` isolation tests, now asserting the raise after the remaining users ran. PASS.
- Break 5 (nightly cron `offline-pipeline` deployment): spec/schedule in `orchestrator.py` unchanged (not in the diff); only the final state differs. PASS. See note on cron below.

**Acceptance criteria**
- [x] PASS — extraction `FanOutStats(failed=1)` -> indexing runs, then PartialIngestError naming extraction/user/shard error — `test_offline.py::TestPartialIngestFailsTheRun::test_a_failed_extraction_shard_is_indexed_then_fails_the_run`, `test_the_message_names_every_failing_phase`; confirmed live (Break 1)
- [x] PASS — per-item failure in successful shard -> raise after all phases, count in `DataFanOutStats` + message — `test_offline.py::...test_failed_items_in_a_data_shard_fail_the_run_after_every_phase`, `test_fanout_data.py::TestItemFailuresInsideAShard`, `test_coordinator_data.py::test_failed_items_are_aggregated_per_user_and_shard`; confirmed live (real ReadTimeout)
- [x] PASS — per-user indexing / clustering exception -> raises after remaining users ran — `test_offline.py::TestOfflineIndexingPhase::test_one_users_indexing_failure_is_isolated`, `TestOfflineClusteringPhase::test_one_users_clustering_failure_is_isolated`, `TestEtlOffline::test_one_users_extraction_failure_is_isolated`
- [x] PASS — all clean (clustering SKIP, zero pending, zero sources) -> result dict, no raise — `TestPartialIngestFailsTheRun::test_a_clean_run_with_a_clustering_skip_returns_its_result`, `test_a_cluster_summary_fallback_is_not_a_failure`, `test_zero_pending_documents_and_zero_sources_complete`; clean e2e run Completed/exit 0
- [x] PASS — CLI wait path exits non-zero with failure message — `test_cli.py::TestWaitForFlowRun::test_a_failed_run_exits_non_zero_with_its_failure_message`; live: `Flow finished with state: Failed — ...`, make exit 2, no success line
- [x] PASS — docstrings (Returns / Raises) of `offline_pipeline` updated (offline.py diff); README / e2e skill grepped, no "partial run completes" wording
- [x] PASS — local e2e: forced failure shows terminal ending with the failure and non-zero exit (Break 1)
- Scope 4 (online pipeline): not changed; rationale (ADR-002 fail-open indexing) accepted as documented, PA decision.

**Evidence**
```
$ make memory-tests  -> 4672 passed in 56.95s
$ make memory-run-pipeline (extraction injected AutoReconnect)
... | INFO  | data: shards=5 succeeded=5 failed=0 items_failed=1
... | INFO  | extraction: user_id=6ac01ab8... shards=1 succeeded=0 failed=1
... | INFO  | indexing: user_id=6ac01ab8... embedded=0
... | ERROR | Encountered exception during execution: PartialIngestError('offline-pipeline finished with failures: data 1 items failed (...ReadTimeout: ...); extraction user=6ac01ab8... 1/1 shards failed (shard=0: ... AutoReconnect: simulated ...)')
Flow finished with state: Failed — ...
make: *** [memory-run-pipeline] Error 2      (exit 2)
```

**Other issues found (non-blocking, orchestrator decides)**
- Low: `parse_items_failed` is an unanchored search and accepts `0`. Suggest anchoring on `PartialIngestError: items_failed=(\d+);` (or at least ignoring `0`) in `tree/data/batch.py` so an unrelated failure text containing the literal cannot flip a hard-failed shard to "succeeded".
- Cosmetic: item exceptions with an empty message render `first: ReadTimeout: ` (trailing colon-space, no detail) and give no hint WHICH source/URL failed; operators must open the worker run logs. Consider `repr(exc)` or including the item in the first-error text.
- Cosmetic: the full failure text is printed 3x on the terminal (ERROR log, traceback, `Flow finished with state`).
- Behavioural note for PA: the nightly `offline-pipeline` cron (listen.yaml) will now end Failed on ANY transient item failure (a real ReadTimeout occurred on the default backfill during this QA run), and a permanently dead feed URL makes every night Failed. That is the human's stated rule ("any partial ingest is an error"), but expect noisy Failed runs / alerts.
- Known (SWE-noted): task retry after a batch-wide failure following a gather would double-count items; verified by reading call sites (substack/web/youtube/arxiv) that none raise after their gather, and over-counting would still only yield Failed, never a false green.
- Docs: glossary row for `PartialIngestError` / Offline phase suggested by SWE, left to PA (not an AC).
- Local DB side effect: e2e left the local `paul-iusztin-e2e` user with a backfilled corpus; no prod access, no repo files modified by QA (injection lived in the scratchpad).

**VERDICT: PASS**

### [SWE] 2026-10-03 09:00 — Fixes (Tester low finding)

- `apps/memory/src/tree/data/batch.py` — `parse_items_failed` now anchored to `PartialIngestError: items_failed=(\d+);` and returns `None` for a count < 1, so a Crashed/Failed shard whose message merely mentions `items_failed=0` counts as a FAILED shard. Item errors with an empty message now render `Type on <item repr, cut at 200 chars>` instead of `Type: `.
- Tests: `tests/unit/data/test_batch.py` (crash mentioning the token → None; `items_failed=0` → None; raw worker message parses; empty-message first error names the item), `tests/unit/data/test_fanout_data.py::TestItemFailuresInsideAShard::test_a_message_without_a_real_item_count_is_a_failed_shard` (Crashed + zero-count → `failed=1`).
- `make memory-format-check` / `memory-lint-check` / `make pre-commit` clean; `make memory-tests` → 4678 passed.
