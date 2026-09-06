---
id: 114-explicit-offline-indexing-phase
feature: embedding-clusters-viz
status: pending
---

# Indexing becomes an explicit **Offline phase** (`run_indexing`) — out of the extraction Coordinator

Tags: `pipeline`, `offline`, `scripts`, `docs`
Depends on: None
Blocks: #117
Implements: ADR-007 (`embedding-clusters-viz`) — Decision 5 (pipeline shape); supersedes ADR-002 §3's single-trailing-index rule

## Scope

Fix the pipeline-shape bug: today `memory_indexing` is hidden inside the extraction
Coordinator (`_fan_out_extraction` in `tree/memory/graph/sharding.py` runs it inline once after
the shards) and `scripts/run_indexing_pipeline.py` runs the flow in-process, unlike every other
script. After this task `tree.offline.offline_pipeline` is the "mother" pipeline with THREE
explicit, independently switchable **Offline phases** (the fourth, clustering, lands in #117):

**1. `offline_pipeline` (`tree/offline.py`)** — new parameter `run_indexing: bool = True`,
placed after `run_extraction`, forwarded by `dispatch_offline_pipeline`:
- Phase 1 `run_data` (unchanged) → phase 2 `run_extraction` (unchanged: one
  `memory_extract_etl_coordinator` inline subflow per target user) → **phase 3 `run_indexing`**:
  for each target user, ONE `memory_indexing(user_id=uid)` inline subflow under
  `with tags(*TAGS_INDEXING)`, per-user failure isolation identical to phase 2
  (`logger.exception` + `{"error": str(exc)}`), result key `"indexing": {user_id: {"embedded": n}
  | {"error": ...}}`.
- Phases run as sequential blocks (extraction for every user, THEN indexing for every user);
  `resolve_target_user_ids` is called once when `run_extraction or run_indexing`.
- All phases off → logged no-op returning `{"data": None, "extraction": {}, "indexing": {}}`.
- `memory_indexing` returns the embedded-row count (`int`) instead of `None` (the flow already
  computes it) so the phase can report it.

**2. Coordinator stops indexing.** Delete the trailing `memory_indexing` call, its function-scope
import and the `TAGS_INDEXING` import from `_fan_out_extraction`; update the module docstring
("4. Index ONCE" paragraph), `memory_extract_etl_coordinator`'s and `pipeline.py`'s docstrings.
`FanOutStats`, partitioning, the gather and failure isolation are untouched.
`tree/online.py` is UNTOUCHED (single-doc path keeps its inline `_run_indexing`).

**3. Scripts (glue only) + Make targets:**
- `scripts/run_indexing_pipeline.py` → `dispatch_offline_pipeline(user_id=..., run_data=False,
  run_extraction=False, run_indexing=True)` + `wait_for_dispatch` + `flush_opik()`, same shape as
  `run_memory_pipeline.py`; docstring rewritten (needs served workflows; no in-process path).
- `scripts/run_data_pipeline.py` offline branch passes `run_indexing=False` too (a data-only run
  must not index).
- `run_memory_pipeline.py` / `run_pipeline.py` keep their calls (defaults → extraction + indexing);
  docstrings say "then the indexing phase" instead of "trailing index run".
- `apps/memory/Makefile`: `run-indexing-pipeline` help text → "INDEXING phase only, as ONE
  offline-pipeline run (needs served workflows)"; `run-memory-pipeline` help → "+ the indexing
  phase".
- The nightly cron keeps defaults → data + extraction + indexing (behaviour unchanged).

**4. Docs:** `apps/memory/README.md` ("Serving workflows" paragraph about `memory_indexing`,
"Pipelines at a glance" table, "Memory pipeline" paragraph, "Memory indexing" section),
`docs/notes/prefect-execution-topologies.md` (the `memory_indexing` paragraph),
`docs/notes/deployment-runbook.md` step 3 ("first indexing run" now dispatches `offline-pipeline`,
which `GROUPS=data` already registers), `.agents/skills/run-pipelines-e2e/SKILL.md` step 2.
Glossary edits (**Offline phase**, **Coordinator**) are in the grooming commit.

## Acceptance Criteria

- [ ] `inspect.signature(offline_pipeline).parameters` contains `run_indexing` with default `True`, positioned between `run_extraction` and `document_ids`; `dispatch_offline_pipeline` forwards it in the deployment `parameters` dict (`test_offline.py` dispatcher test extended: `parameters["run_indexing"] is False` when passed `False`).
- [ ] With the coordinators patched, `offline_pipeline(user_id=U)` awaits `memory_indexing(user_id=U)` exactly once AFTER `memory_extract_etl_coordinator`; the result carries `result["indexing"][str(U)] == {"embedded": <returned int>}`.
- [ ] `offline_pipeline(user_id=None)` with two active users awaits `memory_indexing` once per user, after BOTH extraction calls (call-order assertion on a shared mock).
- [ ] `offline_pipeline(run_indexing=False)` never awaits `memory_indexing` and returns `"indexing": {}`; `offline_pipeline(run_data=False, run_extraction=False)` still resolves target users and indexes them.
- [ ] One user's `memory_indexing` raising `RuntimeError("boom")` is isolated: the other user is still indexed and the result carries `{"error": "boom"}` for the failed one; the flow completes.
- [ ] All three flags `False` → no coordinator, no user resolution, no indexing; returns `{"data": None, "extraction": {}, "indexing": {}}` and logs `both phases disabled` → wording updated to `all phases disabled`.
- [ ] `grep -n "memory_indexing" apps/memory/src/tree/memory/graph/sharding.py` returns nothing; `test_fanout.py` no longer patches `tree.memory.pipeline.memory_indexing` and all fan-out tests pass (index-ordering tests deleted, not skipped).
- [ ] `memory_indexing` returns an `int` equal to `embed_nodes`'s count (unit test with both tasks patched).
- [ ] `tests/unit/scripts/test_run_indexing_pipeline.py`: the script calls `dispatch_offline_pipeline` with `run_data=False, run_extraction=False, run_indexing=True` and the resolved `user_id`, then `wait_for_dispatch`; a failed run exits non-zero; `memory_indexing` is NOT imported by the script (`grep -n "memory_indexing" apps/memory/scripts/run_indexing_pipeline.py` empty).
- [ ] `tests/unit/scripts/test_run_data_pipeline.py`: the offline branch dispatches with `run_extraction=False` AND `run_indexing=False`.
- [ ] `tree/online.py` diff is empty (`git diff --stat apps/memory/src/tree/online.py` shows nothing) and `test_online.py` passes unchanged.
- [ ] `orchestrator._DEPLOYMENT_SPECS` still has exactly 5 entries (existing test).
- [ ] `grep -rn "trailing index" apps/memory/README.md docs/notes .agents/skills apps/memory/scripts apps/memory/Makefile` returns nothing; README "Pipelines at a glance" lists `index` as a phase of `offline-pipeline`.
- [ ] `make memory-format-check && make memory-lint-check && make memory-tests` green; test count does not drop below the post-#113 count minus the deleted index-ordering tests (state the numbers in the log).

## User Stories

### Story: Operator indexes without touching extraction
1. Operator runs `make memory-serve-workflows` in one terminal and `make memory-run-indexing-pipeline USER_IDENTIFIER=paul` in another.
2. The Prefect UI shows ONE `offline-pipeline` run whose only child is a `memory-indexing-etl` subflow (no data, no extraction coordinator).
3. The CLI streams the run's logs and exits 0 with `Indexing pipeline finished for user_id=… Embedded N nodes.` in the output.

### Story: Nightly cron behaves exactly as before
1. The `offline-pipeline` cron fires with `source_files=["sources/listen.yaml"]` and no `user_id`.
2. The run shows, per active user, an extraction coordinator subflow followed by a `memory-indexing-etl` subflow — the same work as yesterday, now as explicit siblings.

### Story: Data-only run never indexes
1. Operator runs `make memory-run-data-pipeline SOURCE_FILE="sources/listen.yaml"`.
2. The flow run contains the data coordinator only; the result JSON shows `"extraction": {}` and `"indexing": {}`.

### Story: One user's index failure does not sink the nightly run
1. The nightly run indexes users A and B; A's `ensure_indexes` raises (mongot down for that tenant's namespace).
2. B is still indexed; the flow result carries `{"error": ...}` under A, and the flow run is Completed, not Failed.

### Story: Reader follows the offline pipeline in code
1. Reader opens `tree/offline.py` and sees three `if run_<phase>:` blocks in a row — data, extraction, indexing — with one docstring paragraph per phase.
2. Reader opens `graph/sharding.py` and finds no indexing at all.

## Out of scope

- The clustering phase, `run_clustering`, and its script (#117).
- Changing `tree/online.py`'s inline indexing.
- Any change to `memory_indexing`'s tasks, index definitions or the dimension gate.
- New Prefect deployments or renames.

---

Blocked by: (none)

## Log
