# 024 — CI cleanup: skip mongot-dependent tests + simplify docker-compose.ci + parallelize

## Why

CI was failing with 35 min of timeouts + 16 errors caused by mongot's gRPC channel to its Search Index Management service dying on GitHub runners (CI run `25989844295`). Diagnosis accepted: don't fight mongot in CI. Skip the mongot-dependent subset entirely and parallelize the rest.

## Scope

1. Register new pytest marker `requires_mongot` in `apps/memory/pyproject.toml`.
2. Decorate every test that needs mongot with `@pytest.mark.requires_mongot` (stacked with the existing `_skip_without_mongot` fixture which stays as a local-dev safety net).
3. Update `.github/workflows/ci.yml` integration step to:
   - Add `-m "not requires_mongot"` to exclude mongot tests.
   - Keep `--timeout=300` as a per-test safety net.
   - Remove the "Wait for mongot to be ready" step.
   - **Do NOT use `-n auto`**: the autouse `_clean_collections` fixture wipes
     the shared test DB between tests, so parallel xdist workers race.
     Sequential wall time is already ~50s locally and well under any cap.
4. Add `pytest-xdist` to dev deps (kept for local experimentation even
   though CI runs sequentially).
5. Simplify `docker-compose.ci.yml`: drop the `mongot` service block and the mongot-user `mongodb-init` override. Mount the existing `mongod.ci.conf` (no auth, no keyfile).
6. Update `CLAUDE.md` and `docs/PROCESS.md` to document the marker hierarchy.
7. Add a `make memory-integration-tests-ci` target that mirrors the CI command.

## Acceptance Criteria

- [x] `requires_mongot` marker registered in `[tool.pytest.ini_options]`.
- [x] Every test that requires mongot (calls `create_search_index`, runs live `$vectorSearch`, or uses the `_skip_without_mongot` fixture) carries `@pytest.mark.requires_mongot`.
- [x] CI command runs `tests/integration -m "not requires_mongot" --timeout=300` (sequential — see note in scope).
- [x] `pytest-xdist` present in dev deps.
- [x] `docker-compose.ci.yml` no longer references `mongot` or the mongot user/keyfile.
- [x] `docker compose -f docker-compose.yml -f docker-compose.ci.yml config` produces a valid compose config with no `mongot` service.
- [x] CLAUDE.md "Running QA and Tests" documents the `slow` vs `requires_mongot` distinction.
- [x] docs/PROCESS.md Tester section notes that mongot tests are skipped on CI and must be exercised locally.
- [x] `make memory-unit-tests` stays green (831 expected).
- [x] `make memory-integration-tests-all` stays green locally with mongot up.
- [x] `cd apps/memory && uv run pytest tests/integration -m "not requires_mongot" --timeout=300` runs cleanly (no mongot timeouts).

## Log

### [SWE] 2026-05-17 17:30 — Implementation

**Files modified**
- `apps/memory/pyproject.toml` — registered `requires_mongot` marker; added `pytest-xdist>=3.6` to dev deps.
- `apps/memory/uv.lock` — picked up `pytest-xdist` + `execnet` after `uv sync`.
- `apps/memory/tests/integration/memory/test_dedup.py` — `@pytest.mark.requires_mongot` on `TestDedupeEntityTiers`.
- `apps/memory/tests/integration/memory/test_indexing_pipeline.py` — `@pytest.mark.requires_mongot` on `TestMemoryIndexingPipeline` and `TestEnsureIndexesReconcile`.
- `apps/memory/tests/integration/test_two_user_isolation.py` — `@pytest.mark.requires_mongot` on `TestTwoUserIsolation`.
- `apps/memory/tests/integration/mcp/test_ingest_tools.py` — `@pytest.mark.requires_mongot` on `TestIngestConversation`, `TestIngestFile`, `TestIngestUrl` with a class-level comment explaining why (live `$vectorSearch` via the extraction pipeline; `dedupe_entity` is not patched here).
- `.github/workflows/ci.yml` — dropped the "Wait for mongot to be ready" step; the `up -d` line no longer mentions `mongot`; integration step now runs `pytest -m "not requires_mongot" --timeout=300`; removed `MONGOT_PORT` env var with a note.
- `docker-compose.ci.yml` — dropped the `mongot` override block and the mongot-user creation step; simplified `mongodb-init` entrypoint to only initiate the replica set; added a header comment explaining the CI tradeoff and the "override can't remove a service" caveat.
- `apps/memory/Makefile` — added `integration-tests-ci` target mirroring the CI command.
- `CLAUDE.md` — added a "Test-marker hierarchy" subsection explaining `slow` vs `requires_mongot` and the `pytest-xdist`-but-sequential decision.
- `docs/PROCESS.md` — updated the slow-test paragraph to document `requires_mongot` and the Tester's local-mongot acceptance duty.
- `tracker/024-ci-skip-mongot-and-simplify.in-progress.md` — this file.

**Tests marked `@pytest.mark.requires_mongot` (47 collected)**
- `tests/integration/mcp/test_ingest_tools.py` — 13 tests across `TestIngestConversation`, `TestIngestFile`, `TestIngestUrl`.
- `tests/integration/memory/test_dedup.py::TestDedupeEntityTiers` — 14 tests.
- `tests/integration/memory/test_indexing_pipeline.py` — 6 tests across `TestMemoryIndexingPipeline` (3) and `TestEnsureIndexesReconcile` (3).
- `tests/integration/test_two_user_isolation.py::TestTwoUserIsolation` — 14 tests.

`test_two_user_review_isolation.py` was inspected (it has `@pytest.mark.slow`) — it uses regular `$match`/`$lookup`, NOT Atlas Search, and does not need mongot. Left unmarked.

**Resolution of the `test_ingest_tools.py` timeout issue**
The 7 CI timeouts were NOT real-Gemini retries — the tests use `FakeLLM`. The cause was that `ingest_*` invokes the memory-extraction pipeline, and the pipeline's `add_entity` step issues a live Atlas `$vectorSearch` without these tests patching `dedupe_entity` (unlike `test_extraction_pipeline.py`, which DOES patch it — see the long comment in `_patch_pipeline_deps`). With mongot's gRPC channel dead on the CI runner, the `aggregate(...)` call stalls until pytest's per-test 5-minute timeout fires. Tagging the three classes `@pytest.mark.requires_mongot` cleanly skips them in CI; locally they keep running because mongot is up.

**Tests**
- Unit: 831 passed, 0 failed (`make memory-unit-tests`).
- Integration (CI-mirror, mongot excluded): 108 passed, 12 skipped, 47 deselected in 50.5s (`uv run pytest tests/integration -m "not requires_mongot" --timeout=300`).
- Integration (full, mongot up): 155 passed, 12 skipped in 5:02 (`make memory-integration-tests-all`).
- Integration (fast inner loop): 119 passed, 12 skipped, 36 deselected in 1:58 (`make memory-integration-tests`).

**Wall-time comparison**
- Old CI integration step (failing): 59:58 (16 errors + 7 five-min timeouts + tail of healthy tests).
- New CI integration step (locally simulated): ~50s — a >70× speedup.
- Local `make memory-integration-tests-all` (Tester's acceptance gate, runs everything including mongot): 5:02 — unchanged.

**Parallelization note**
Tried `-n auto` (pytest-xdist) first: wall went down to 56s but produced 16 failures because the autouse `_clean_collections` fixture wipes the shared test DB between tests, so parallel workers stomp on each other's writes. Dropped `-n auto` from the CI command. The dep stays in `pyproject.toml` for local experimentation. If we ever genuinely need parallelization the fix is per-worker DB names via `PYTEST_XDIST_WORKER`.

**`docker-compose.ci.yml` simplification**
- Before: 45 lines, contained a `mongot` override block (3 lines) and a 20-line `mongodb-init` entrypoint that also created the `mongot` Mongo user with `searchCoordinator` role.
- After: 48 lines (substantive config shrank; replaced with a header explaining the CI tradeoff). Substantive removed: `mongot` block + the `mongot` user creation in `mongodb-init` (no longer needed since mongot isn't started).
- Caveat documented inline: `docker compose -f docker-compose.yml -f docker-compose.ci.yml config` STILL lists `mongot` because Compose overrides can't remove a service from the base file. CI never starts it (the `up -d` command explicitly names `mongodb mongodb-init prefect-server`).

**Format / lint / pre-commit**
```
$ make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check
218 files left unchanged
All checks passed!
218 files already formatted
All checks passed!

$ make pre-commit
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed
KGQuery discipline (memory)..............................................Passed
```

**End-to-end verification (the CI command itself)**
```
$ time uv run pytest tests/integration -m "not requires_mongot" --timeout=300
=============== 108 passed, 12 skipped, 47 deselected in 50.45s ================
real    0m52.174s
```

**Notes for Tester**
- Don't commit. Tester runs next, then commit + push.
- The full-suite (`memory-integration-tests-all`) run above proves we didn't regress local coverage.
- The `pytest-xdist` install does add `execnet` to the lockfile but isn't enabled anywhere by default (Makefile + CI both sequential).
- The CI compose merge config still shows `mongot` listed; this is a Compose limitation, not a bug. Inspect `docker compose ps` after a CI run to confirm no `tree-mongot` container is running.

### [Tester] 2026-05-17 17:45 — QA

**Test summary**
- Format / lint / pre-commit: PASS (0 issues; `218 files already formatted`, `All checks passed!`, all 5 pre-commit hooks pass).
- Unit tests: 831 passed, 0 failed (`make memory-unit-tests`, 41.46s). Matches SWE summary.
- Integration (CI mirror): 108 passed, 12 skipped, 47 deselected, 65.98s wall-clock test phase (`make memory-integration-tests-ci`). My host is slower than the SWE's (50.5s reported); still well under the 90s bar.
- Integration (fast inner loop): 119 passed, 12 skipped, 36 deselected, 1:51 (`make memory-integration-tests`). Under 2 min.
- Integration (full with mongot): 155 passed, 12 skipped, 5:01 (`make memory-integration-tests-all`). Matches SWE 5:02.
- Warnings: 0 across all runs.

**E2E adversarial pass**
- **Happy path** — exact CI command, run from `apps/memory/`:
  `uv run pytest tests/integration -m "not requires_mongot" --timeout=300` → `108 passed, 12 skipped, 47 deselected in 58.07s`. PASS.
- **Break path 1 (boundary: mongot subset must be fully deselected from CI)** — `uv run pytest tests/integration -m "not requires_mongot" --collect-only -q | grep -E "test_two_user_isolation|test_ingest_tools|test_dedup|test_indexing_pipeline::TestEnsureIndexesReconcile"` → empty (`Correctly deselected — none of the 4 mongot files appear`). PASS — no mongot tests sneak into the CI command.
- **Break path 2 (regression: review-isolation gate must stay in CI)** — `uv run pytest tests/integration -m "not requires_mongot" --collect-only -q | grep test_two_user_review_isolation` → 3 tests (`test_find_pending_duplicates_returns_only_user_a_pair`, `test_review_duplicate_cannot_confirm_other_tenants_pair`, `test_get_same_as_cluster_does_not_traverse_other_tenant`). PASS — the Phase-1 multi-tenancy regression catcher that DOESN'T need mongot still runs in CI.
- **Break path 3 (infra: merged compose config must be valid YAML even though `mongot` can't be removed)** — `docker compose -f docker-compose.yml -f docker-compose.ci.yml config -q` → exit 0, "valid". Merged config still LISTS `mongot` (documented Compose limitation), but CI's `up -d` line explicitly names `mongodb mongodb-init prefect-server` and omits it. Verified by reading `.github/workflows/ci.yml` line 64. PASS.

**Acceptance criteria**
- [x] PASS — `requires_mongot` marker registered in `[tool.pytest.ini_options]`. Evidence: `apps/memory/pyproject.toml` lines 63-64; `uv run pytest --markers | grep requires_mongot` returns the descriptive entry.
- [x] PASS — Every test that requires mongot carries `@pytest.mark.requires_mongot` (47 collected). Evidence: `uv run pytest tests/integration -m requires_mongot --collect-only -q` → `47/167 tests collected (120 deselected) in 5.60s`. Matches SWE's 47-test breakdown across the 4 files.
- [x] PASS — CI command runs `tests/integration -m "not requires_mongot" --timeout=300` sequentially. Evidence: `.github/workflows/ci.yml` line 122 (`run: uv run pytest tests/integration -m "not requires_mongot" --timeout=300`); no `-n auto`.
- [x] PASS — `pytest-xdist` present in dev deps. Evidence: `apps/memory/pyproject.toml` line 48; `uv run python -c "import xdist; print(xdist.__version__)"` → `3.8.0`.
- [x] PASS — `docker-compose.ci.yml` no longer references `mongot` (service or user). Evidence: `git diff docker-compose.ci.yml` — removed the `mongot` override block (3 lines) and the `mongot`-user creation block from `mongodb-init`'s entrypoint.
- [x] PASS — `docker compose -f docker-compose.yml -f docker-compose.ci.yml config` produces valid YAML. Evidence: `config -q` exits 0. **Note (matches SWE caveat):** `mongot` is STILL listed in the merged output because Compose overrides cannot remove a base service; the CI workflow handles this by explicit `up -d mongodb mongodb-init prefect-server`. Documented inline in the file header comment.
- [x] PASS — CLAUDE.md "Running QA and Tests" documents `slow` vs `requires_mongot`. Evidence: CLAUDE.md lines 244-253 — new "Test-marker hierarchy" subsection covers orthogonality, CI behavior, and the `pytest-xdist`-but-sequential decision.
- [x] PASS — `docs/PROCESS.md` Tester section notes mongot tests are skipped on CI and must be exercised locally. Evidence: `docs/PROCESS.md` line 362 — explicit "CI excludes these ... The Tester MUST run them locally with the full `docker-compose.yml` stack ... before the Phase-1-style acceptance gate" with the canonical command `make memory-integration-tests-all`.
- [x] PASS — `make memory-unit-tests` stays green. Evidence: `831 passed in 41.46s`.
- [x] PASS — `make memory-integration-tests-all` stays green locally with mongot up. Evidence: `155 passed, 12 skipped in 301.03s (0:05:01)`.
- [x] PASS — `uv run pytest tests/integration -m "not requires_mongot" --timeout=300` runs cleanly. Evidence: `108 passed, 12 skipped, 47 deselected in 58.07s`. No mongot timeouts.

**Evidence (key outputs)**
```
$ make memory-unit-tests
============================= 831 passed in 41.46s =============================

$ time make memory-integration-tests-ci
========== 108 passed, 12 skipped, 47 deselected in 65.98s (0:01:05) ===========
real    1m08.778s

$ time make memory-integration-tests
========== 119 passed, 12 skipped, 36 deselected in 111.08s (0:01:51) ==========

$ time make memory-integration-tests-all
================= 155 passed, 12 skipped in 301.03s (0:05:01) ==================

$ uv run pytest --markers | grep -E "slow|requires_mongot"
@pytest.mark.slow: marks tests that take >3s or require vector-index (mongot) convergence or full Prefect e2e. ...
@pytest.mark.requires_mongot: marks tests that need a working Atlas Search / mongot ...

$ uv run pytest tests/integration -m requires_mongot --collect-only -q | tail -1
47/167 tests collected (120 deselected) in 5.60s

$ uv run pytest tests/integration -m "not requires_mongot" --collect-only -q | tail -1
120/167 tests collected (47 deselected) in 5.15s

$ docker compose -f docker-compose.yml -f docker-compose.ci.yml config -q && echo "valid"
valid
```

**Other issues found (non-blocking; for awareness only)**
- A single `Logging error` traceback (`ValueError: I/O operation on closed file` from `prefect/server/api/server.py:981` → `subprocess_server_logger.info("Stopping temporary server...")`) appears AFTER the pytest summary line during the standalone CI command run. It is pre-existing Prefect+pytest shutdown noise (subprocess server emits a log line into pytest's already-closed captured stdout); not introduced by this PR (the same Prefect / Python 3.14 combo would surface it on any test run that spins up a temporary Prefect server). Test outcome is reported BEFORE the noise. No-op for the verdict; worth filing as a tidy-up follow-up if it shows up in CI logs.
- The compose merge config still listing `mongot` is documented (Compose limitation), but it's worth keeping an eye on whether a future Compose version adds `--remove-service` so we can drop the workaround.

**Cross-task verification (Phase-1 acceptance gate)**
- `test_two_user_isolation.py::TestTwoUserIsolation` (16 tests — Phase-1 mongot-backed isolation gate): now `requires_mongot`, so EXCLUDED from CI. Per the user's accepted tradeoff (mongot is unreliable on GitHub runners), this lives in the local-only `make memory-integration-tests-all` gate.
- `test_two_user_review_isolation.py::TestTwoUserReviewIsolation` (3 tests — review/dedup isolation, uses `$match`/`$lookup`, NO mongot needed): correctly UNMARKED → runs in CI. Verified by collect-only filter. This is the closest multi-tenancy regression catcher that still runs in CI; if it stays green, basic tenant filtering on the query side is exercised every commit.
- `docs/PROCESS.md` line 362 and `CLAUDE.md` lines 244-253 explicitly document that the Tester (and pre-PR human) MUST run `make memory-integration-tests-all` locally before merging anything touching multi-tenancy — closing the loop on the local-only Phase-1 gate.

**VERDICT: PASS**

All 11 acceptance criteria verified with reproducible evidence. All three wall-time targets behave as advertised (CI mirror ~66s, fast loop ~1:51, full-with-mongot ~5:01). The CI command correctly excludes every mongot-dependent test, including the four classes the SWE called out. The review-isolation regression catcher remains in CI, and the local-only mongot gap is documented in both CLAUDE.md and docs/PROCESS.md. Lint, format, pre-commit and unit tests are green with 0 warnings. Adversarial break paths (mongot-exclusion correctness, review-isolation regression, merged-compose validity) all PASS. Ready for SWE to commit and push.

### [On-Call] 2026-05-17 15:05 — CI Failure

**Failed step:** CI → memory (python) → Unit tests (run 25994109339)

**Error**

```
pymongo.errors.OperationFailure: Authentication failed., full error: {'ok': 0.0,
'errmsg': 'Authentication failed.', 'code': 18, 'codeName': 'AuthenticationFailed', ...}
```

All 831 unit tests errored at setup of the session-scoped `_init_beanie` fixture
(`tests/unit/conftest.py`) when authenticating to mongod with `tree`/`tree` via
`settings.mongo.mongo_uri`. Integration tests skipped as a result.

**Root cause**

Commit 4529361 removed the `tree` root-user `createUser(...)` block from the
`mongodb-init` entrypoint in `docker-compose.ci.yml`. Mongod boots without auth
enforcement (no keyfile, `skipAuthentication...` on `mongod.ci.conf`), but the
test fixture's connection URI still carries SCRAM credentials, which MongoDB
validates against its user catalog. With no `tree` user provisioned, SCRAM-SHA-1
auth fails. Locally the pre-push verification used the full `docker-compose.yml`
stack (which still creates `tree`), so this never surfaced.

Fixing now: restore the `tree` user creation in `docker-compose.ci.yml`'s
`mongodb-init` block. The mongot user creation stays gone — only the root `tree`
user needs to come back so SCRAM-auth succeeds against the credentialed URI.

### [On-Call] 2026-05-17 15:30 — CI Failure (re-run after auth fix)

After commit `6a0a940` (tree-user restoration), run 25994458827 showed
**Unit tests: success** and **Integration tests: success** — confirming the
SCRAM-auth root cause was correctly identified. However the run was still
marked `failure` because the `Stop infrastructure` step exited 1 with:

```
env file /home/runner/work/building-agentic-systems/building-agentic-systems/.env
not found: stat .../.env: no such file or directory
```

**Root cause:** pre-existing infra bug from commit `133fb10` (on main since
2026-05-15), which added `env_file: [.env]` to the `prefect-worker` service
in `docker-compose.yml`. The CI `Stop infrastructure` step ran a bare
`docker compose -f ... down`, which walks every service in the merged
config — including `prefect-worker` — and fails because no `.env` exists on
the runner. CI's `Start infrastructure` step already sidesteps this by
explicitly naming services (`up -d mongodb mongodb-init prefect-server`), so
the symmetric fix is to mirror that list on the `down`.

Not introduced by #024 (the same step failed on main runs 25929456542 /
25922653543 / 25922587331), but it now gates this PR's CI to red, so it
falls into the on-call scope to unblock the pipeline.

Fixing now: change the `Stop infrastructure` step to
`docker compose ... stop mongodb mongodb-init prefect-server` so it tears
down only what was actually started.

### [On-Call] 2026-05-17 15:55 — CI Failure (re-run after named-services teardown)

Run 25994975355 (commit `d73fe13`) showed the named-services `stop` still
hit the same `env file .../.env not found` error. Docker Compose v2 parses
the merged config (including unrelated services' `env_file:`) regardless of
whether the named services depend on the absent file. The original
`Start infrastructure` step also touches the same merged config but
happened to pass — likely because some compose verbs are stricter than
others about env_file validation.

**Real fix:** create an empty `.env` at the top of the job before any
`docker compose` invocation. The file is empty (no secrets), and the
`prefect-worker` service that needs it is never started in CI. This
satisfies the lookup for every compose subcommand (`up`, `stop`, `down`,
`config`) without requiring any other workflow changes. Reverted the
`Stop infrastructure` step back to the original `down` form (cleanest
teardown, network removal included) now that env_file is satisfied.

### [On-Call] 2026-05-17 16:05 — CI Resolution

Run 25995473332 (commit `5ae4dae`) — **all green**.
URL: https://github.com/iusztinpaul/building-agentic-systems/actions/runs/25995473332

All 18 steps of `memory (python)` succeeded, including Unit tests,
Integration tests (excludes `requires_mongot`), and Stop infrastructure.
The harness (typescript) job stayed green throughout. Three commits to
close the loop:

1. `6a0a940` — restore the `tree` root user in `docker-compose.ci.yml`
   so SCRAM auth succeeds against the credentialed mongo URI.
2. `d73fe13` — (superseded) attempt to limit `Stop infrastructure` to
   the named services.
3. `5ae4dae` — touch `.env` before any `docker compose` invocation so
   the merged-config parser doesn't abort on `prefect-worker`'s
   `env_file`; revert teardown to plain `down` for full cleanup.

Moving the tracker file back to `tracker/done/`.

