# Dream supersession-judge flag + scheduled per-user fan-out deployment

Status: pending
Tags: `memory`, `dream`, `prefect`, `config`
Depends on: #051
Blocks: #053

## Scope

Two related additions on top of #051's flow:
1. The optional LLM contradiction-judge supersession sweep, **behind a flag,
   OFF by default**.
2. The Prefect scheduled deployment + per-user fan-out so the dream runs
   automatically (cron) for every active user, tenant-scoped.

### Part 1 — supersession sweep behind `dream.enable_supersession_judge`

- Add a `sweep_supersession` task to the dream flow (`consolidation/dream.py`),
  gated on `app_config.dream.enable_supersession_judge` (added in #051, default
  `False`).
- **OFF (default):** prefs/facts get the SAME semantic + fuzzy dedup as every
  other node type via #051's `sweep_node_duplicates` — zero LLM calls. This is
  already true after #051; this task just makes the gate explicit and ensures no
  LLM is invoked when the flag is false.
- **ON:** run
  `tree/memory/extraction/preference_supersession.py::resolve_supersessions`
  over the **delta** prefs/facts (the watermark-fresh driving set for the
  PREFERENCE and FACT node types) against their full partitions. Signature:
  `resolve_supersessions(*, database, user_id, llm, embedding_model, raws,
  now=None)`. It partitions by `(user_id, category)` for prefs and
  `(user_id, subject, predicate)` for facts, takes the K most-recent active
  candidates (`app_config.extraction.dedup.supersession_candidate_cap`),
  first-contradiction-wins, and writes `superseded_by` + bi-temporal
  `valid_until`.
  - Build `llm` via `get_llm()` and `embedding_model` via
    `get_search_embedding_model()` (the #048 text client) ONLY when the flag is
    on — never construct them on the default path.
  - Feed `resolve_supersessions` the delta prefs/facts as its `raws` iterable.
    Inspect the function's `RawExtraction` input contract and adapt stored
    PREFERENCE/FACT nodes into that shape; if the adaptation is non-trivial,
    that is in-scope for this task (read the module first).
- `dry_run=True` still means no writes and no watermark advance (supersession
  writes are gated behind `not dry_run` exactly like the merge/flag path).

### Part 2 — scheduled deployment + per-user fan-out

- **Per-user fan-out flow.** The dream is tenant-scoped (watermark + cost). Add a
  thin parent flow, e.g. `dream_consolidation_all_users()`, that:
  - Lists active users. The project's active-user signal is the KG `person` node
    with `properties.is_active_user=True` (one per `User`; see
    `entities/users.py`). Enumerate `User` documents (Beanie `User` model,
    collection `users`) and run `dream_consolidation(user_id=user.id,
    dry_run=app_config.dream.dry_run)` per user.
  - Skips the whole run when `app_config.dream.enabled` is `False`.
  - Isolates failures: one user's failure must not abort the others (gather with
    return_exceptions or per-user try/except + logged failure in stats).
- **Deployment.** In `apps/memory/src/tree/orchestrator.py`, register:
  ```python
  dream_consolidation_all_users.to_deployment(
      name="dream-consolidation-etl",
      cron=app_config.dream.cron,   # default "0 4 * * *"
      tags=["dream"],
  )
  ```
  (Prefect 3 `to_deployment` accepts `cron=`/`interval=`. Match the existing
  `serve(...)` registration style in `orchestrator.py`.)
- Add a Make target mirroring the existing `make memory-run-*` pattern, e.g.
  `make memory-run-dream-consolidation` (streams logs like the other pipeline
  triggers), so the Tester/operator can trigger it without the Prefect UI.
  Inspect `apps/memory/Makefile` for the existing `run-*` target shape.

### Reuse / constraints

- Do NOT duplicate the per-user dream logic — the parent flow only fans out.
- The default-OFF supersession path must construct NO LLM and NO embedding
  client (cost + free-tier safety).
- Per CLAUDE.md, do NOT unit-test Prefect/Modal wiring; cover fan-out selection
  logic (active-user enumeration, enabled-gate, failure isolation) with unit
  tests on the pure helper, and the scheduled e2e in integration (#053).

## Acceptance Criteria

- [x] `sweep_supersession` is gated on `app_config.dream.enable_supersession_judge`.
- [x] Flag OFF (default): `dream_consolidation` constructs NO LLM and NO
      embedding client, and prefs/facts are deduped via the normal
      semantic+fuzzy path only (assert no `resolve_supersessions` call).
- [x] Flag ON: `resolve_supersessions` is invoked over the delta PREFERENCE/FACT
      driving set; a contradicting newer preference supersedes the older one
      (`superseded_by` + `valid_until` written) in an integration test.
- [x] Flag ON + `dry_run=True`: no supersession writes, watermark not advanced.
- [x] `dream_consolidation_all_users` enumerates active users and runs the
      per-user flow once per user.
- [x] `app_config.dream.enabled=False` ⇒ the fan-out flow runs zero per-user
      dreams.
- [x] One user's per-user failure does not prevent the others from running
      (failure-isolation unit test on the fan-out helper).
- [x] `orchestrator.py` registers `dream-consolidation-etl` with
      `cron=app_config.dream.cron` and `tags=["dream"]`; `make memory-serve-workflows`
      registers it without error (Tester verifies the deployment appears via
      `uv run prefect deployment ls` / the serve logs).
- [x] A `make memory-run-dream-consolidation` target exists and triggers the
      deployment, streaming logs.
- [x] `make memory-format-check && make memory-lint-check && make memory-unit-tests`
      pass; `make memory-integration-tests-all` green locally.
- [ ] [HUMAN] Confirm the cron `0 4 * * *` (04:00 UTC daily) is the desired
      schedule, or override at the Step-3 gate. (Operator decision surfaced in
      the plan.)

## User Stories

### Story: Scheduled dream runs nightly for every active user
1. Operator runs `make memory-serve-workflows`.
2. The `dream-consolidation-etl` deployment registers with cron `0 4 * * *`.
3. At 04:00 UTC the parent flow fans out one `dream_consolidation` run per active
   user (Paul Iusztin included), each with its own tenant-scoped watermark.
4. A failure in one user's run is logged and isolated; the others still complete.

### Story: Operator turns on the contradiction judge for preferences
1. Operator sets `dream.enable_supersession_judge: true` in `default.yaml`.
2. Dream runs; for the Paul Iusztin user a newer preference contradicts an older
   one in the same `(user_id, category)` partition.
3. `resolve_supersessions` marks the older preference `superseded_by` the newer
   and stamps `valid_until` — only on the watermark-fresh delta, capped at K.

### Story: Default install pays nothing for the LLM judge
1. Operator leaves `enable_supersession_judge: false` (default).
2. A dream run processes prefs/facts via plain semantic+fuzzy dedup.
3. No Gemini / Voyage calls are made on the supersession path — verified by the
   no-client assertion.

### Story: Operator triggers a dream run on demand
1. Operator runs `make memory-run-dream-consolidation`.
2. Logs stream to the terminal (no Prefect UI needed), showing per-user fan-out
   and per-user stats.

---

Blocked by: #051

## Log

### [SWE] 2026-05-21 00:35 — Implementation

**Files modified**
- `apps/memory/src/tree/memory/consolidation/dream.py` — filled the `_supersession_sweep` seam with the flag-gated LLM supersession sweep (drives the reused `resolve_supersessions` over the watermark-fresh PREFERENCE/FACT delta); added the `_select_active_user_ids` + `_fan_out_dreams` helpers, the `FanOutStats` type, and the `dream_consolidation_all_users` parent flow.
- `apps/memory/src/tree/orchestrator.py` — registered `dream-consolidation-etl` (`cron=app_config.dream.cron`, `tags=["dream"]`) serving the fan-out parent flow.
- `apps/memory/scripts/run_dream_consolidation.py` — new trigger script (no `user_id`; streams logs like the other `run_*` scripts).
- `apps/memory/Makefile` — added `run-dream-consolidation` target.
- `apps/memory/configs/default.yaml` — refreshed stale `dream:` comments (#052 now owns the deployment + supersession sweep).
- `apps/memory/tests/unit/memory/consolidation/test_dream.py` — added `TestSupersessionSweep`, `_stored_node_to_extracted` test, `TestActiveUserSelection`, `TestFanOut`.
- `apps/memory/tests/integration/memory/test_dream_supersession_and_fanout.py` — new CI-runnable (no-mongot) integration file: flag-on supersedes / no-contradiction / flag-off-no-LLM / dry-run, and the three fan-out flows.

**How the supersession flag was wired (Option B + why)**
Chose **option (b)**: wrap each delta PREFERENCE/FACT stored node in a one-node `RawExtraction` envelope (`_stored_node_to_extracted`) and call `resolve_supersessions` AS-IS. Rationale: the resolver already implements the exact contract the spec wants — per-partition candidate finding (`(user_id, category)` / `(user_id, subject, predicate)`), the `supersession_candidate_cap`, the LLM judge, first-contradiction-wins, and the `superseded_by` + bi-temporal `valid_until` writes. Option (b) reuses that verbatim with zero refactor of the extraction module and no new public surface (lowest risk). The "delta DRIVES, full partition is the search space" rule holds because I only feed the watermark-fresh nodes as `raws`, while the resolver's `_find_*_candidates` queries the full active partition. The stored row's `_id` matches the resolver's re-derived `build_node_id(user_id, type, _normalize(name))`, so the `exclude_id` self-skip lines up.

**Fan-out shape**
Parent flow `dream_consolidation_all_users()` (one cron, loops active users) per the spec default. Active-user signal is the KG `person:self` node with `properties.is_active_user=True` (`_select_active_user_ids`, dedup + deterministic sort). `_fan_out_dreams` is a pure orchestration core (no DB/Prefect) that runs `dream_consolidation(user_id=..., dry_run=...)` per user with per-user try/except isolation, recording failures in `FanOutStats.failures`. Enabled-gate (`dream.enabled=False` ⇒ zero dreams) and `dream.dry_run` propagation live in the flow.

**Deployment registration**
`dream_consolidation_all_users.to_deployment(name="dream-consolidation-etl", cron=app_config.dream.cron, tags=["dream"])` in `orchestrator.py`'s `serve(...)`. Live-verified: `make memory-serve-workflows` lists `dream-consolidation-all-users/dream-consolidation-etl`; `prefect deployment inspect` shows cron `0 4 * * *` + `tags=['dream']`.

**Tests**
- Unit: 1327 passing, 0 failing (`make memory-unit-tests`). New: flag-off-no-LLM (dry-run + no-delta no-ops never construct the LLM), flag-on drives only delta nodes into the resolver, adapter round-trip, active-user selection, fan-out runs/isolation/no-op.
- Integration (new file, no mongot): 7 passing — flag-on-supersedes (judge fires once, `superseded_by` + `valid_until` written), flag-on-no-contradiction (nothing written), flag-off-default (no LLM/embedding/resolver touched), flag-on-dry-run (sweep skipped, watermark held), fan-out per-active-user + dry_run propagation, disabled ⇒ zero dreams, one-user-failure isolated.
- Regression: original `test_dream_consolidation.py` (requires_mongot + slow) still 7 passing.

**Evidence**
```
$ make memory-unit-tests
... 1327 passed in 40.39s

$ uv run pytest tests/integration/memory/test_dream_supersession_and_fanout.py
... 7 passed in 5.63s

$ uv run pytest tests/integration/memory/test_dream_consolidation.py   # requires_mongot
... 7 passed in 41.07s

$ make memory-format-check && make memory-lint-check && make pre-commit
... All checks passed! / all hooks Passed

$ make memory-run-dream-consolidation   # live, against real DB
... dream_consolidation_all_users: 1 active user(s) to fan out (dry_run=False)
... dream fan-out: users_total=1 succeeded=1 failed=0
... Done. Flow completed successfully.
```

**Notes / caveats for the Tester**
- **mongot needs**: the NEW integration file (`test_dream_supersession_and_fanout.py`) is deliberately mongot-FREE — it isolates the supersession path from the duplicate sweep by seeding prefs with EMPTY embeddings (the duplicate sweep's driving query needs a non-empty `embedding`, so it never issues `$vectorSearch`), and the resolver uses a plain partition `find`. It runs in CI. The pre-existing `test_dream_consolidation.py` is still `requires_mongot + slow` and needs the full local docker stack — run `make memory-integration-tests-all` with mongot up for the acceptance gate.
- **Fan-out flow testable without a live scheduler?** Yes. The fan-out flow is invoked directly (`await dream_consolidation_all_users()`) in the new integration tests with `dream_consolidation` mocked — no scheduler/cron involved. Deployment registration is verified separately at serve time (`prefect deployment inspect`), per CLAUDE.md "do not unit-test Prefect wiring".
- **Config seam gotcha**: the flow reads config via `_live_app_config()` (a fresh `load_app_config()`), NOT the cached `app_config` singleton — so tests patch `tree.memory.consolidation.dream._live_app_config` (the `dream_cfg` fixture), not `app_config.dream`. Worth knowing if the Tester adds more flag-toggling cases.
- **Live config**: `default.yaml` ships `dream.dry_run: false` and `enabled: true` in this environment, so the live `make memory-run-dream-consolidation` ran a REAL (non-dry) fan-out. `enable_supersession_judge` is `false` (default-OFF), so no LLM/Voyage was called on that live run.
- **Not committed** — awaiting Tester PASS + PM ACCEPT per process.

### [Tester] 2026-05-21 02:05 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check && make memory-lint-check && make pre-commit` — 258 files formatted, all ruff checks passed, all hooks Passed)
- Unit tests: 1327 passed / 0 failed (`make memory-unit-tests`, 41.78s)
- Integration tests (full, mongot up): 251 passed / 1 skipped / 0 failed (`make memory-integration-tests-all`, 565.34s / 9m25s). The 1 skip is the pre-existing live-SERP `test_web_search_ingest.py`, unrelated to #052.
- Warnings: 0

**E2E adversarial pass** (real DB, throwaway `tester_adversarial_052*`, run AFTER the shared suite finished — no concurrent contention)
- Happy path (B) flag-ON judge supersedes via reused `resolve_supersessions`: contradicting newer pref → `judge_calls=1`, `valid_until` set on loser, `superseded_by` edge new→old written. PASS
- Break path 1 (default-path / make-or-break A): flag OFF default, contradicting prefs, independent tripwires on `get_llm` / `get_search_embedding_model` / `resolve_supersessions` → tripwire never fired, no `superseded_by` edge, no `valid_until`. Airtight: NO LLM/embedding/resolver constructed on the default path. PASS
- Break path 2 (incremental driving C): two contradicting prefs BOTH ≤ watermark, flag ON + a judge that WOULD say "contradiction" → `judge_calls=0`, no edge, no `valid_until`. Old↔old is never re-superseded; only the delta drives. PASS
- Break path 3 (dry_run F): flag ON + `dry_run=True` with `get_llm`/resolver tripwires → no LLM, no writes, `watermark_advanced=False`, watermark held at seed value. PASS
- Break path 4 (boundary — empty/whitespace statement): delta pref with `statement="   "` + a contradicting candidate, flag ON → no crash, judge never fired (resolver skips no-statement rows), no spurious supersession. PASS

**Acceptance criteria**
- [x] PASS — `sweep_supersession` gated on `enable_supersession_judge` — `dream.py:796` `if dream_cfg.enable_supersession_judge:` guards `_supersession_sweep`.
- [x] PASS — Flag OFF (default): NO LLM/embedding client, no `resolve_supersessions` — adversarial probe A (independent tripwires) + `test_flag_off_default_constructs_no_llm`. `get_llm`/`get_search_embedding_model` constructed only inside `_supersession_sweep` after the dry_run + delta-present guards (`dream.py:649-650`).
- [x] PASS — Flag ON: resolver invoked over delta PREFERENCE/FACT; contradicting newer pref supersedes older (`superseded_by` + `valid_until`) — adversarial probe B + `test_flag_on_contradiction_supersedes`. Judge is the arbiter (no cosine pre-filter); reuses `resolve_supersessions` verbatim (no reimplementation).
- [x] PASS — Flag ON + dry_run: no writes, watermark not advanced — adversarial probe F + `test_flag_on_dry_run_skips_sweep_and_holds_watermark`. `_supersession_sweep` returns before constructing the LLM when `dry_run` (`dream.py:635-637`).
- [x] PASS — `dream_consolidation_all_users` enumerates active users + runs per-user — `test_fan_out_runs_per_active_user`, unit `TestActiveUserSelection` / `TestFanOut`.
- [x] PASS — `dream.enabled=False` ⇒ zero per-user dreams — `test_fan_out_disabled_runs_zero_dreams` (spy `assert_not_called`).
- [x] PASS — One user's failure isolated — `test_fan_out_isolates_one_user_failure` + unit `test_one_user_failure_is_isolated` (all users attempted, failure recorded in `stats.failures`).
- [x] PASS — `orchestrator.py` registers `dream-consolidation-etl` with `cron=app_config.dream.cron` + `tags=["dream"]` — verified via in-process `to_deployment`: name=`dream-consolidation-etl`, schedule `CronSchedule(cron='0 4 * * *')`, tags=`['dream']`, flow=`dream-consolidation-all-users`. (Verified at serve-config build time per project convention; no live scheduler started.)
- [x] PASS — `make memory-run-dream-consolidation` target exists + triggers the deployment — `Makefile` target calls `scripts/run_dream_consolidation.py` (no USER_ID), script streams logs via the standard poll-and-print pattern; SWE live-verified a real fan-out run.
- [x] PASS — format/lint/unit pass; `make memory-integration-tests-all` green — see Test summary.
- [ ] [HUMAN] Awaiting human verification — cron `0 4 * * *` (04:00 UTC daily) confirmation is an operator decision surfaced at the Step-3 gate.

**Regression (G)**
- `test_dream_consolidation.py` (#051 dream flow, requires_mongot + slow): 7 passed.
- `test_dream_adversarial_qa.py` (#051 adversarial): 4 passed.
- `test_preference_supersession.py` (reused resolver, inline extraction path): 4 passed — the dream wrapper feeds `resolve_supersessions` as-is and does NOT change inline extraction supersession behavior.

**Evidence**
```
$ make memory-unit-tests
============================ 1327 passed in 41.78s =============================

$ make memory-integration-tests-all
================== 251 passed, 1 skipped in 565.34s (0:09:25) ==================

$ uv run python -c "...to_deployment(name='dream-consolidation-etl', cron=app_config.dream.cron, tags=['dream'])"
name: dream-consolidation-etl
cron from config: '0 4 * * *'
tags: ['dream']
schedules: [DeploymentScheduleCreate(schedule=CronSchedule(cron='0 4 * * *', ...))]

$ uv run python _adversarial_probe.py   # throwaway DB, post-suite, isolated
[PASS] A flag-OFF zero-LLM (no LLM/embed/resolver constructed)
[PASS] A flag-OFF no superseded_by edge + no valid_until
[PASS] C incremental: old-vs-old NOT re-superseded (judge never fired)
[PASS] F flag-ON dry_run: no writes + watermark held
[PASS] BOUNDARY empty-statement delta pref: no crash, no spurious supersede
[PASS] B flag-ON judge supersedes via reused resolver: judge_calls=1 valid_until_set=True edge_new->old=True
```

**Other issues found**
- None blocking. Note (not a defect): `judge.py:162` uses PEP 758 `except TypeError, ValueError:` — confirmed valid Python 3.14 per CLAUDE.md, not a relic. Pre-existing, not part of #052.
- Note: `prefect deployment ls` spins up a *temporary* server (port 8832) and does NOT list the live-served deployments, so it cannot be used to confirm registration; the `to_deployment` config build (above) is the correct serve-time verification per the project convention.

**VERDICT: PASS**
