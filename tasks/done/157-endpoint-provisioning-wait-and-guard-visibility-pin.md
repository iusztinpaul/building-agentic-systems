---
id: 157-endpoint-provisioning-wait-and-guard-visibility-pin
status: done
feature: voyage-4-and-modal-embedding-catalog
---

# What the live run taught us about `modal endpoint list`: `-test` waits while OUR endpoint is `provisioning`, `deploy` says the create is asynchronous, and the guard's endpoint-first short-circuit is pinned

Tags: `memory`, `modal`, `deploy`, `docs`, `tests`
Depends on: None (by NNN order it runs after #156; it touches none of its files)
Blocks: #141
Implements: ADR-009 — Decision 11, revision 6 ("A Dedicated endpoint is `provisioning` before it is `live`") and Consequences ("What the existence guard can and cannot see"). Found live by #141 round 1, cycles 4.0, 4a and 4d.

## Scope

**EXECUTION ORDER of the fix round: 153 -> 154 -> 155 -> 156 -> 157 -> 141 round 2.**

**HARD SAFETY RULE:** No agent may run `make memory-deploy-*`, `scripts/modal_*.py` as a process, or any `modal …` command outside task 141; a PATH shim does NOT intercept under `make`/`uv run` because `.venv/bin` wins — use the mocked unit tests and `DRY_RUN=yes`.
Run test commands one at a time — two concurrent pytest runs contend for the local MongoDB/Prefect and stall.
Do not run the real memory pipelines against the local database (shared with the human's `main` checkout).
Every test here mocks `run_modal` (the suite already sets `TREE_MODAL_DRY_RUN=1`); no test sleeps for real.

**Facts from the live run (`tasks/141` Log).**
- `modal endpoint create` is ASYNCHRONOUS: it returned in ~4 s with `status: provisioning`; `live` came after 2m15s (`Qwen/Qwen3-Embedding-0.6B`) and 9m25s (`Qwen/Qwen3.5-0.8B` — 565 s, 94 % of the 600 s warm-up budget). The SWE bridged it BY HAND, polling `modal endpoint list --json`, and only then ran `-test`; whether `-test` on a provisioning endpoint is sat out by the health poller (503) or fails at once in `resolve_server_url` was never observed. This task makes the answer irrelevant.
- `modal endpoint list --json` keys: `name, endpoint_id, status, created_at, created_by`; statuses seen: `provisioning`, `live`.
- `modal app list` shows NEITHER the `ep-*` app behind a Dedicated endpoint NOR long-stopped apps; `modal endpoint list` hides stopped endpoints. `existing_kind` (`modal_cli.py:181-209`) already reads the ENDPOINT list first and returns on a hit, and `TestExistingKind::test_both_lists_are_read_only_json_calls` already pins the argv order. PA analysis: **no collision class is open** — a hand-made name cannot carry `tree-`, a live or provisioning endpoint is always listed, and whatever is invisible is STOPPED (a deploy replaces it, never overwrites something serving). So this is a pin + a docstring, not a fix.
- Modal's refusals arrive on STDERR; `_log_modal_output` joins both streams since #147. Nothing to build — state it in the docstring of `_log_modal_output` if it is not already there.

**What to build**
1. `tree.models.modal_cli.endpoint_status(entry: ModalModelConfig) -> str | None` — ONE `run_modal(["modal", "endpoint", "list", "--json"], capture_output=True)`; the `status` of the row whose `name == entry.endpoint_name`, else `None`. A dry run (`None` result), a non-zero exit, or unparsable output -> `None` plus ONE WARNING `could not read the endpoint list (<detail>) — not waiting for provisioning`. This is a WAIT, not a guard: it fails OPEN (the smoke test gives the verdict), unlike `_list_rows`.
2. `tree.models.modal_cli.wait_until_live(entry, *, deadline_s: float = PROVISIONING_DEADLINE_S, sleep=time.sleep, clock=time.monotonic) -> None` with `PROVISIONING_DEADLINE_S = 1800.0` (a code constant with a comment: ~3x the slowest measured 565 s; NOT a YAML knob). Rules, closed:
   - status `None` (an App, a stopped endpoint, a dry run, an unreadable list) or `live` -> return at once, no log line for `None`.
   - `provisioning` -> INFO `Provisioning: <endpoint_name> is not live yet — <N>s/1800s`, sleep on the poller's schedule (5 s x 1.5 capped at 15 s — reuse `modal_warmup`'s constants, do not copy the numbers), read again; on `live` INFO `Live: <endpoint_name> after <N>s`.
   - any OTHER status -> ONE WARNING `<endpoint_name> has status '<x>' — not waiting` and return.
   - deadline spent -> `ModelError("<endpoint_name> is still provisioning after 1800s — check `modal endpoint list` and the Modal dashboard")` (the driver's existing `ModelError` branch turns it into exit 1).
3. `scripts/modal_model.py::test_command`: call `wait_until_live(entry)` before `asyncio.run(test(model))`, inside the existing `try` (glue only). Update its docstring: the sentence "No dry run either — it starts no CLI" is now wrong — REPLACE it with "one read-only `modal endpoint list --json` first (skipped under `DRY_RUN=yes`)".
4. `modal_router.run_deploy`: after a SUCCESSFUL endpoint create, ONE INFO line: `Endpoint <endpoint_name> is provisioning — Modal returns before it is live (minutes). make memory-deploy-model-test MODEL=<repo_id> waits for it.` Not after an App deploy, not in a dry run, not after a refusal.
5. `existing_kind` docstring: replace the sentence claiming the app list "catches everything else" with the live fact (it lists neither endpoint-backed `ep-*` apps nor long-stopped apps; the endpoint leg is what protects, hence first).
6. `apps/memory/README.md`: the paragraph #141 round 1 added ("A **Dedicated endpoint** has one wait the poller cannot cover … Watch that column, then run `-test`.") — REWRITE it: `create` returns while `provisioning` (keep the two measured times); `-test` waits for `live` by itself (up to 30 minutes) and then polls `/health`. Remove "Watch that column, then run `-test`" and the unobserved claim about `resolve_server_url`.
7. `.agents/skills/run-pipelines-e2e/SKILL.md`, "Models on Modal" step: one clause — `-test` waits out `provisioning`. Do not touch the verbatim `DRY_RUN=yes` closing sentence.

**Out of scope:** making `deploy` block until live; a YAML knob for the provisioning budget; waiting inside the clients (`ModalLLM` / `ModalEmbeddingModel` — a pipeline never runs seconds after a create); probing what Modal does on `endpoint create` over a STOPPED endpoint's name (recorded live by #141 round 2; any refusal already aborts as verdict `other`).

## Acceptance Criteria

- [x] `tests/unit/models/test_modal_cli.py::TestEndpointStatus`: returns `"provisioning"` / `"live"` for our row using a fixture that is the REAL key set (`name, endpoint_id, status, created_at, created_by`); `None` when only the operator's un-prefixed `qwen3-embedding-0-6b` is listed; `None` + exactly ONE WARNING containing `not waiting for provisioning` for a non-zero exit, for non-JSON output and for a dry run; the argv is exactly `["modal", "endpoint", "list", "--json"]` with `check=False`.
- [x] `::TestWaitUntilLive::test_no_row_returns_at_once` — one list call, `sleep` never called, no `Provisioning` record.
- [x] `::test_provisioning_then_live` — statuses `provisioning, provisioning, live` with a fake clock: `sleep` called with `5.0` then `7.5`; records `Provisioning: tree-qwen3-5-0-8b is not live yet — 0s/1800s`, a second `Provisioning:` line, then `Live: tree-qwen3-5-0-8b after <N>s`.
- [x] `::test_the_deadline_is_a_model_error` — always `provisioning`, fake clock past 1800 -> `ModelError` containing `still provisioning after 1800s`.
- [x] `::test_an_unknown_status_is_not_waited_on` — status `"failed"` -> returns, ONE WARNING containing `has status 'failed'`, `sleep` never called.
- [x] `::test_the_schedule_is_the_pollers` — `wait_until_live` imports `INITIAL_INTERVAL_S`, `BACKOFF_FACTOR`, `MAX_INTERVAL_S` from `tree.models.modal_warmup` (assert by patching `MAX_INTERVAL_S` to `6.0` and seeing a `6.0` sleep).
- [x] `TestExistingKind::test_a_live_endpoint_short_circuits_the_app_list` — with our endpoint row present AND an app-list fixture that would say `deployed` for `entry.app_name`, the result is `"endpoint"` and `run` was called exactly ONCE (the app list was never read). `test_both_lists_are_read_only_json_calls` stays green unchanged.
- [x] `::test_a_provisioning_endpoint_is_already_an_endpoint` — a row with `status: "provisioning"` makes `existing_kind` return `"endpoint"` (the guard refuses a second create during the 2-10 minutes before `live`).
- [x] `tests/unit/models/test_modal_router.py`: after a mocked successful endpoint create the log contains `is provisioning — Modal returns before it is live`; it is absent after an App deploy, a dry run and a refusal.
- [x] `tests/unit/scripts/` (wherever the driver's tests live): `test_command` calls `wait_until_live` BEFORE the smoke test (order asserted with a shared mock manager); a `ModelError` from it exits 1 and the smoke test never runs.
- [x] `grep -c "Watch that column" apps/memory/README.md` -> 0; `grep -c "it starts no CLI" apps/memory/scripts/modal_model.py` -> 0.
- [x] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green.

## User Stories

### Story: Operator tests an endpoint seconds after creating it
1. `make memory-deploy-model MODEL=Qwen/Qwen3.5-0.8B` ends with `Endpoint tree-qwen3-5-0-8b is provisioning — Modal returns before it is live (minutes). make memory-deploy-model-test MODEL=Qwen/Qwen3.5-0.8B waits for it.`
2. Operator runs `make memory-deploy-model-test MODEL=Qwen/Qwen3.5-0.8B` immediately.
3. Sees `Provisioning: tree-qwen3-5-0-8b is not live yet — 0s/1800s` … `Live: tree-qwen3-5-0-8b after 548s`, then the usual `Warming …` / `Warm: …` lines and the smoke lines. They never opened `modal endpoint list`.

### Story: Operator tests an App
1. `make memory-deploy-model-test MODEL=voyageai/voyage-4-nano` (served as our vLLM App).
2. No `Provisioning` line at all — the endpoint list has no `tree-voyage-4-nano` row — and the health poller starts at once, as before.

### Story: Modal's list is down during a test
1. `modal endpoint list --json` exits 1.
2. ONE warning `could not read the endpoint list (exit 1) — not waiting for provisioning`, and the smoke test runs and gives the verdict. (A `deploy` in the same situation still REFUSES, exit 3 — the guard fails closed; this wait fails open.)

### Story: An endpoint never becomes live
1. Modal leaves `tree-qwen3-5-0-8b` on `provisioning`.
2. After 30 minutes: `tree-qwen3-5-0-8b is still provisioning after 1800s — check `modal endpoint list` and the Modal dashboard`, exit 1.

### Story: Second deploy while the first is still provisioning
1. Operator repeats `make memory-deploy-model MODEL=Qwen/Qwen3.5-0.8B` one minute after the first.
2. `Refusing to deploy Qwen/Qwen3.5-0.8B via endpoint: 'tree-qwen3-5-0-8b' already exists on Modal as a Dedicated endpoint. …`, exit 3 — a `provisioning` row counts.

### Story: The next engineer wonders why the guard reads two lists
1. Reads `existing_kind`'s docstring: the app list cannot see an endpoint's app; the endpoint list cannot see a stopped endpoint; the endpoint leg is first and short-circuits.
2. Finds the same paragraph in ADR-009 Consequences.

---

Blocked by: (none)

## Log

### [PA] 2026-09-21 16:10 — Grooming

**Summary**
`-test` waits while our endpoint row says `provisioning` (fail-open, 1800 s constant), `deploy` announces the asynchronous create, and the guard's endpoint-first short-circuit and its treatment of a `provisioning` row are pinned by tests.

**Key decisions**
- F4 is NOT a hole (analysis in Scope and ADR-009 Consequences): pin + docstring only.
- The wait lives in the DRIVER's `test`, not in the clients and not in `deploy`: it is the only place a human (or #141) acts seconds after a create; it is robust whichever way a provisioning endpoint fails (503 or unresolvable), which round 1 never observed.
- Fail OPEN, unlike the guard: a wait that cannot look must not block a smoke test.
- A code constant, not a knob: 565 s measured against a 600 s warm-up budget is too tight to share `warmup_deadline_s`, and nobody has asked to tune it.

**Dependencies**
- None.

**User stories**
- 6: test right after create, App untouched, list down, never live, double deploy during provisioning, the two-list rationale.

Ready for implementation.

### [SWE] 2026-09-21 18:05 — Implementation

**Files modified**
- `apps/memory/src/tree/models/modal_cli.py` — `PROVISIONING_DEADLINE_S = 1800.0`, `endpoint_status` (one read-only list call, fails OPEN with ONE warning), `wait_until_live` (the poller's schedule, injected `sleep`/`clock`); `_read_rows` extracted so the guard's fail-CLOSED door and the wait's fail-OPEN read parse the same rows and share one `detail` vocabulary; `existing_kind` docstring rewritten to the live two-list facts; a paragraph in the module docstring saying the wait is NOT a rail.
- `apps/memory/src/tree/models/modal_router.py` — `_PROVISIONING_NOTICE`, logged at both SUCCESSFUL endpoint creates (plain and custom weights).
- `apps/memory/scripts/modal_model.py` — `test` calls `wait_until_live(entry)` inside the existing `try`, BEFORE `asyncio.run` (glue only); docstring sentence replaced.
- `apps/memory/README.md` — the `provisioning` paragraph rewritten (`-test` waits; no `resolve_server_url` claim, no "Watch that column").
- `.agents/skills/run-pipelines-e2e/SKILL.md` — one clause: `-test` waits `provisioning` out itself. The `DRY_RUN=yes` closing sentence untouched.
- `apps/memory/tests/unit/models/test_modal_cli.py` — `TestEndpointStatus` (7), `TestWaitUntilLive` (7), two `TestExistingKind` pins; `_endpoint_row` gained `status` (default `"live"`, the real status — it read `"running"`, which Modal never emits), plus `_clock` / `_wait_lines` helpers.
- `apps/memory/tests/unit/models/test_modal_router.py` — `TestProvisioningNotice` (4).
- `apps/memory/tests/unit/scripts/test_modal_model_script.py` — `TestTestCommandWaitsOutProvisioning` (3: order via `attach_mock`, the UNPATCHED chain through the CLI, `ModelError` -> exit 1 with no smoke test); `_endpoint_row` gained `status` here too (same `"running"` -> `"live"` correction).

**Tests**
- Unit: 4052 passing, 0 failing (baseline 4029; +23). Integration: N/A — the app has no integration suite.
- Red first: with the two functions stubbed, 11 of the new `test_modal_cli.py` tests FAILED on assertions (not imports); the router notice test failed on an empty notice list. The two driver tests ERRORED rather than failed — `mocker.patch.object(cli_module, "wait_until_live")` on an attribute the module did not import yet, which is the API-does-not-exist signal, not a broken harness. All green after the implementation.

**Acceptance criteria**
- [x] `TestEndpointStatus` — `test_it_returns_our_rows_status[provisioning|live]`, `test_a_workspace_without_our_row_has_no_status`, `test_a_list_it_cannot_read_is_one_warning[a failed list|output that is not JSON]`, `test_a_dry_run_cannot_look_either`, `test_it_is_one_read_only_list_call` (argv `["modal","endpoint","list","--json"]`, `check=False`, `capture_output=True`).
- [x] `::TestWaitUntilLive::test_no_row_returns_at_once`
- [x] `::test_provisioning_then_live` — sleeps `[5.0, 7.5]`; the three exact lines, `— 0s/1800s` first.
- [x] `::test_the_deadline_is_a_model_error` — plus `test_the_deadline_is_three_times_the_slowest_create` pinning the constant.
- [x] `::test_an_unknown_status_is_not_waited_on`
- [x] `::test_the_schedule_is_the_pollers[the cap]` (patch `tree.models.modal_warmup.MAX_INTERVAL_S` -> `6.0`, sleeps `[5.0, 6.0]`) and `[the first interval and the factor]` (patch `INITIAL_INTERVAL_S` -> `2.0`, `BACKOFF_FACTOR` -> `2.0`, sleeps `[2.0, 4.0]`) — all THREE constants pinned, because `modal_cli` reads them off the MODULE at call time, so the test proves reuse rather than a copy.
- [x] `TestExistingKind::test_a_live_endpoint_short_circuits_the_app_list`; `test_both_lists_are_read_only_json_calls` unchanged and green.
- [x] `::test_a_provisioning_endpoint_is_already_an_endpoint`
- [x] `tests/unit/models/test_modal_router.py::TestProvisioningNotice` — present after a successful create, absent after an App deploy, a dry run and a refusal.
- [x] `tests/unit/scripts/test_modal_model_script.py::TestTestCommandWaitsOutProvisioning::test_the_wait_runs_before_the_smoke_test` / `::test_a_spent_provisioning_budget_exits_one_and_never_smoke_tests`, plus `::test_a_live_endpoint_costs_one_list_call_and_no_wait` (the chain unpatched through the CLI).
- [x] `grep -c "Watch that column" apps/memory/README.md` -> 0; `grep -c "it starts no CLI" apps/memory/scripts/modal_model.py` -> 0.
- [x] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green (run one at a time).

**Evidence**
```
$ make memory-tests
======================= 4052 passed in 61.66s (0:01:01) ========================
$ make memory-format-check   -> 313 files already formatted
$ make memory-lint-check     -> All checks passed!
$ make pre-commit            -> prettier / ruff check / ruff format / biome all Passed
$ grep -c "Watch that column" apps/memory/README.md        -> 0
$ grep -c "it starts no CLI" apps/memory/scripts/modal_model.py -> 0
```

**Notes**
- **E2E: NOT RUN — forbidden by this task's HARD SAFETY RULE** (no `modal …`, no `scripts/modal_model.py` as a process, no `make memory-deploy-model*`, not even under `DRY_RUN=yes`). The closest exercise is the driver's own CliRunner tests, which invoke the real click command in-process with `subprocess.run` mocked. Live proof belongs to #141 round 2.
- **Judgement 1 — the `Live:` line only fires when we actually WAITED.** The task puts it inside the `provisioning` branch and makes `live` a "return at once" case, so a `-test` against an already-warm endpoint prints nothing here (it would otherwise print `Live: … after 0s` right before the poller's own `Warm: …`). No AC covers a first-read `live`; flagging it so the Tester can rule.
- **Judgement 2 — the dry-run WARNING is implemented exactly as the ACs pin it** (one message shape for a non-zero exit, non-JSON output and a dry run). My opinion for the record: under `DRY_RUN=yes` "could not read the endpoint list (dry run)" is expected, not anomalous, so INFO would read better than WARNING — but the AC says WARNING and the driver docstring's "skipped under DRY_RUN=yes" already sets the expectation. Tester's call.
- `_log_modal_output`'s docstring already carries the stderr fact ("Modal writes the refusal to one of them and nothing guarantees a trailing newline"), so scope item 0 needed no edit — left alone on purpose ("prefer removing over adding").
- `modal_cli` now imports `tree.models.modal_warmup` (for the poll schedule), which pulls `aiohttp`/`openai` into its import graph. Only `modal_router` and the deploy driver import `modal_cli`, so no boot path pays for it; `test_router_does_not_import_modal_or_subprocess` (fresh interpreter, `modal` not in `sys.modules`) still passes.
- Trade-off: `wait_until_live` polls with a linear read of the whole endpoint list per iteration — at most ~120 list calls over 1800 s, and the list is a handful of rows; no index or cache is warranted.

### [Tester] 2026-09-21 19:40 — QA

**Commands run (LOCAL env only, one at a time; no `modal …`, no `scripts/modal_model.py` as a process, no `make memory-deploy-model*`)**
```
make env-status                                                          -> Env target: local (.env)
make memory-format-check                                                 -> 313 files already formatted
make memory-lint-check                                                   -> All checks passed!
make pre-commit                                                          -> prettier / ruff check / ruff format / biome all Passed
make memory-tests                                                        -> 1 failed (test_prewarm.py::test_first_failure_cancels_the_sibling, asyncio-task-leak timing assertion; file untouched by this diff)
PYTEST_ADDOPTS="tests/unit/models/test_prewarm.py::test_first_failure_cancels_the_sibling -q" make memory-tests -> 1 passed (isolation)
make memory-tests (rerun)                                                -> 4052 passed in 54.74s
PYTEST_ADDOPTS="tests/unit/models/test_modal_cli.py::TestGuardDeploy -q" make memory-tests           -> ran against a targeted mutation (see below)
PYTEST_ADDOPTS="tests/unit/models/test_modal_cli.py -q" make memory-tests                            -> ran against the same mutation, full file
PYTEST_ADDOPTS="tests/unit/models/test_modal_cli.py::TestEndpointStatus tests/unit/models/test_modal_cli.py::TestWaitUntilLive -v" make memory-tests -> 14 passed
PYTEST_ADDOPTS="tests/unit/models/test_modal_cli.py::TestExistingKind -v" make memory-tests           -> 13 passed
PYTEST_ADDOPTS="tests/unit/models/test_modal_router.py::TestProvisioningNotice tests/unit/scripts/test_modal_model_script.py::TestTestCommandWaitsOutProvisioning -v" make memory-tests -> 7 passed
grep -c "Watch that column" apps/memory/README.md                        -> 0
grep -c "it starts no CLI" apps/memory/scripts/modal_model.py            -> 0
git diff | grep -oE "ep-[A-Za-z0-9]{22}" | wc -l                          -> 2 (see note below — both are the pre-existing alphabet placeholder, not new)
git diff | grep -icE "hf_[a-z0-9]{10,}|wk-[a-z0-9]+|ws-[a-z0-9]+|acme--|modal\.run" -> 0
grep -rn "wait_until_live" apps/memory/src apps/memory/scripts            -> one call site, scripts/modal_model.py:137, synchronous, before asyncio.run
uv run python -c "import tree.models.modal_cli; 'modal' in sys.modules"  -> False
uv run python -c "import tree.models.modal_router; 'modal' in sys.modules" -> False
uv run python -c "import tree.mcp.server; 'modal_cli'/'modal_router' in sys.modules" -> False, False
uv run python -c "import tree.orchestrator; 'modal_cli'/'modal_router'/'openai' in sys.modules" -> False, False, False (aiohttp True, pre-existing/unrelated)
shasum apps/memory/src/tree/models/modal_cli.py (before mutation)        -> f5385e360860d0b1fdb113b5714e34dd7fbbf337
[targeted Edit: `_list_rows` made to fail OPEN — `return rows` inserted before the raise]
PYTEST_ADDOPTS="tests/unit/models/test_modal_cli.py -q" make memory-tests -> 6 failed (guard tests caught the mutation)
[reverted the Edit]
shasum apps/memory/src/tree/models/modal_cli.py (after revert)           -> f5385e360860d0b1fdb113b5714e34dd7fbbf337 (identical)
uv run python <scratch: adversarial_wait.py>       -> 12 in-process adversarial cases against endpoint_status/wait_until_live, run_modal monkeypatched (no subprocess)
uv run python <scratch: adversarial_guard_edges.py> -> guard vs wait, same input, for "rows that are not dicts" / "None stdout" / "rows missing 'name'"
uv run python <scratch: qa_dryrun_check.py>         -> driver's `test` command under DRY_RUN=1: subprocess.run called == False, exit_code == 0
git status --short (final)                          -> exactly the SWE's 9 files
```
All scratch scripts lived under the session scratchpad (outside the repo) and were never committed; the mutation was a targeted `Edit` + revert proven by `shasum`, never a `git checkout`/`restore`.

**E2E adversarial pass**
- Happy path: `TestWaitUntilLive::test_provisioning_then_live` and the driver's `TestTestCommandWaitsOutProvisioning` (CliRunner, in-process) — `provisioning, provisioning, live` produces `Provisioning: … — 0s/1800s`, a second `Provisioning:` line, then `Live: … after 548s`; the driver calls `wait_until_live` before the smoke test. PASS.
- Break path 1 (state edge: list becomes unreadable **mid-wait**, `provisioning` then `exit 1`): scratch `adversarial_wait.py`, case 8 — expected: one WARNING, no further claim, smoke test's verdict stands. **Actual: `WARNING could not read the endpoint list (exit 1) — not waiting for provisioning` immediately followed by `INFO Live: tree-qwen3-5-0-8b after 0s`** — a false "Live" claim right after admitting it could not read the list. **FAIL.**
- Break path 2 (state edge: the row **disappears** mid-wait, `provisioning` then no row): scratch `adversarial_wait.py`, case 9 — expected: return cleanly, no confirmed-live claim (nothing said the row is live — it vanished). **Actual: `INFO Live: tree-qwen3-5-0-8b after 0s` with NO preceding warning at all** — worse than break path 1, nothing contradicts the false claim. **FAIL.**
- Break path 3 (malformed status): `""`, `"LIVE"` (case), `"Provisioning"` (case), missing `status` key, name collision (`tree-qwen3-5-0-8b-extra`, un-prefixed `qwen3-5-0-8b`), our name appearing twice, `deadline_s=0`, a clock jump past the deadline between reads — all PASS: exactly one warning where expected, exact-match name safety holds, no crash, no infinite loop, no oversleep past one interval past the deadline (scratch `adversarial_wait.py`, remaining cases).
- Break path 4 (guard mutation): `_list_rows` edited to fail OPEN (`return rows` before the raise) — 6 pre-existing guard tests failed as required (`test_a_failed_list_cannot_rule_out_an_overwrite`, `test_output_that_is_not_a_json_list_is_a_closed_door[×3]`, `test_a_dry_run_is_never_mistaken_for_an_empty_workspace`, `test_a_failed_list_under_force_is_only_a_warning`); reverted, `shasum` identical before/after. PASS (guard proven closed).
- Break path 5 (structural edges through both doors, item 4's remaining cases — `["a","b"]` not-dicts, `stdout=None`, rows missing `name`): scratch `adversarial_guard_edges.py` — "not dicts" and "`None` stdout" both refuse via `guard_deploy` (`ModalGuardError: could not list Modal endpoints (invalid JSON)`) and return `None` + one warning from `endpoint_status`, never raising — PASS. "Rows missing `name`" does **not** make `guard_deploy` refuse (the row simply fails to match our name and is treated as a non-collision, same as any foreign row) — this matching logic (`row.get("name") == entry.endpoint_name`) is byte-identical to `git show HEAD`, i.e. **pre-existing, unchanged by this diff, out of this task's scope** — noted, not a blocking regression of this task.
- Break path 6 (dry run, no spawn): scratch `qa_dryrun_check.py` — driver's `test` command invoked via `CliRunner` under `TREE_MODAL_DRY_RUN=1`, `subprocess.run` mocked and asserted never called: `subprocess.run called: False`, exit 0, smoke test still runs. PASS.

**Rulings on the SWE's open questions**
1. **`Live:` only when we waited — accept the "first-read-live logs nothing" half** (no AC covers it, and it avoids `Live: … after 0s` noise right before the poller's own `Warm:` line). **Reject the converse**: when `waited=True` and the final `status` is `None` (unreadable list or the row vanished mid-wait), the current `elif waited:` fires regardless, printing `Live: … after Ns` — a false, plausible-looking claim in exactly the situation where the code does not know the endpoint is live. This is the blocking defect (see below).
2. **Dry-run WARNING — keep, per AC 1**, which explicitly names the dry run as a WARNING-producing case alongside a non-zero exit and non-JSON output. The preceding `DRY RUN — would run: …` line already explains it in context (verified live in `qa_dryrun_check.py`'s output), so it never reads as an orphaned alarm. A downgrade to INFO would need a PA spec change, not a Tester ruling — leave as-is.
3. **Module-attribute reuse at call time — sound, and the only form that satisfies AC 6**, which pins reuse by patching `modal_warmup.MAX_INTERVAL_S` and asserting the sleep value follows — a `from … import MAX_INTERVAL_S` binding would not see that patch.
4. **The guard is proven byte-for-byte as closed as before.** Only additions in the test-file diff beyond the `_endpoint_row(status=...)` parameter (confirmed via `git diff` on the test files); `_read_rows` is semantically identical to the old `_list_rows` body in `git show HEAD` (same three `detail` strings, same JSON/list/dict validation, same fallthrough); mutating `_list_rows` to fail open broke 6 pre-existing guard tests; reverted with an identical `shasum`. The one non-refusal edge (rows missing `name`) is pre-existing, unchanged, out of scope — not a regression.
5. **`"running"` -> `"live"` fixture default — clean.** `grep -rn '"running"'` across both test files and the source returns nothing; `existing_kind`'s endpoint leg matches on `name` only and never reads `status` (confirmed by reading the function body); the app leg's `state`/`_DEAD_APP_STATES` check is untouched. The suite passing at 4052 (up from a hypothetical spurious-warning baseline under the old `"running"` default) is consistent with the fix being load-bearing, not cosmetic.

**Acceptance criteria** (all 12 node ids resolve and are green; see command list above)
- [x] PASS — `TestEndpointStatus` (7 tests) — real key set, `None` for an operator's un-prefixed row, `None` + ONE warning for non-zero exit / non-JSON / dry run, exact argv+`check=False`.
- [x] PASS — `TestWaitUntilLive::test_no_row_returns_at_once` — one list call, `sleep` never called, no `Provisioning` record.
- [x] PASS — `::test_provisioning_then_live` — sleeps `[5.0, 7.5]`, exact three lines.
- [x] PASS — `::test_the_deadline_is_a_model_error` — `ModelError` containing `still provisioning after 1800s`.
- [x] PASS — `::test_an_unknown_status_is_not_waited_on` — one warning containing `has status 'failed'`, no sleep.
- [x] PASS — `::test_the_schedule_is_the_pollers[×2]` — reuse proven by patching `modal_warmup` module attributes.
- [x] PASS — `TestExistingKind::test_a_live_endpoint_short_circuits_the_app_list` — `"endpoint"`, one call; `test_both_lists_are_read_only_json_calls` unchanged and green.
- [x] PASS — `::test_a_provisioning_endpoint_is_already_an_endpoint` — `"endpoint"` for a `provisioning` row.
- [x] PASS — `test_modal_router.py::TestProvisioningNotice` (4) — present after a successful create, absent after an App deploy / dry run / refusal.
- [x] PASS — `test_modal_model_script.py::TestTestCommandWaitsOutProvisioning` (3) — order via `attach_mock`, unpatched chain, `ModelError` -> exit 1, smoke test never runs.
- [x] PASS — `grep -c "Watch that column"` -> 0; `grep -c "it starts no CLI"` -> 0.
- [x] PASS — `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green — all four commands green, run one at a time (4052 passed; one unrelated pre-existing flake in `test_prewarm.py` reproduced once, passed in isolation, and cleared on a full rerun at 4052/4052 — file untouched by this diff). This AC is about the command set, which is genuinely green; the behavioral defect below was found by the e2e adversarial pass, which is a separate, blocking finding — see VERDICT.

**Evidence**
```
$ make memory-tests
============================ 4052 passed in 54.74s =============================

$ uv run python -u adversarial_wait.py   (case 8: list unreadable mid-wait)
INFO Provisioning: tree-qwen3-5-0-8b is not live yet — 0s/1800s
WARNING could not read the endpoint list (exit 1) — not waiting for provisioning
INFO Live: tree-qwen3-5-0-8b after 0s          <- false claim

$ uv run python -u adversarial_wait.py   (case 9: row disappears mid-wait)
INFO Provisioning: tree-qwen3-5-0-8b is not live yet — 0s/1800s
INFO Live: tree-qwen3-5-0-8b after 0s          <- false claim, no warning precedes it
```

**Other issues found**
- **Blocking — `wait_until_live` claims `Live:` after waiting even when the final read could not confirm `live`.** `apps/memory/src/tree/models/modal_cli.py:317-321`:
  ```python
  if status is not None and status != "live":
      logger.warning("%s has status %r — not waiting", entry.endpoint_name, status)
  elif waited:
      logger.info("Live: %s after %.0fs", entry.endpoint_name, clock() - start)
  ```
  When `waited=True` and the final `status` is `None` (the list became unreadable mid-wait, or our row vanished mid-wait — e.g. a `-stop` racing the wait), the `elif waited:` branch fires and prints a fabricated `Live: … after Ns`, contradicting Scope item 2 ("`provisioning` -> … read again; **on `live`** INFO `Live: …`") and Story 3 (one warning, then the smoke test's own verdict — not a warning immediately followed by a false success line). The row-disappears case is worse: no warning precedes it at all.
  Fix: `elif waited and status == "live":`. Regression tests to add to `TestWaitUntilLive`: (a) `provisioning` then an unreadable list (`list_returncode=1` or invalid JSON) — assert no record starts with `Live:` and the one warning is the "could not read" one; (b) `provisioning` then the row disappearing (empty rows) — assert no record starts with `Live:`.
- Pass-with-note — `guard_deploy`/`existing_kind` do not refuse when a row is present but lacks a `name` key (matches nobody's `name`, so it's treated as a non-collision like any foreign row). This matching logic (`row.get("name") == entry.endpoint_name`) is byte-identical to `git show HEAD:apps/memory/src/tree/models/modal_cli.py` — pre-existing, unchanged by this diff, out of task 157's scope (ADR-009 Consequences already treats the column contract as a fail-closed dependency for structural failures, not per-row field absence). Flagging for awareness only, not blocking.
- Pass-with-note — the custom-weights branch's `_PROVISIONING_NOTICE` call (`modal_router.py:320`) has no dedicated test; `TestProvisioningNotice` only covers the plain-create branch (`modal_router.py:295`) plus App/dry-run/refusal. AC 9 does not require it and the line is identical in both branches, but call it out explicitly rather than letting it ride on the SWE's summary.
- Pass-with-note — `git diff | grep -oE "ep-[A-Za-z0-9]{22}" | wc -l` returns `2`, not `0`. Investigated: both matches are the pre-existing `"ep-abcdefghijklmnopqrstuv"` alphabet placeholder already present at `HEAD` in both test files (one is a deleted/replaced line in `test_modal_cli.py`, the other is an untouched line in `test_modal_model_script.py`); the SWE's actually-new id, `"ep-FAKE0000000000000000"`, is 20 characters and deliberately does not match the 22-char pattern. No new secret or real-looking id was introduced.
- Pass-with-note — the `test_prewarm.py::test_first_failure_cancels_the_sibling` flake (asyncio-task-leak timing assertion against pymongo background tasks) reproduced once in the first full `make memory-tests` run, passed in isolation, and cleared on a full rerun (4052/4052). File is untouched by this diff; pre-existing flake, not a regression.

**VERDICT: FAIL**

One blocking issue: the false `Live:` log line after a wait that could not confirm liveness (`apps/memory/src/tree/models/modal_cli.py:317-321`). Everything else — all 12 ACs, the guard's fail-closed behavior (mutation-tested), import hygiene, docs, greps, and the rest of the adversarial pass — is green. Fix is a one-line condition change plus two regression tests in `TestWaitUntilLive`.

### [SWE] 2026-09-21 20:25 — Fix round 1

**The blocking issue, fixed**
`apps/memory/src/tree/models/modal_cli.py` — one condition, exactly as the Tester prescribed:
```python
    elif waited and status == "live":      # was: elif waited:
        logger.info("Live: %s after %.0fs", entry.endpoint_name, clock() - start)
```
`Live:` is now claimed only when the LAST read said `live`. Plus one docstring paragraph in `wait_until_live` saying so and why there is no line for the `None` cases. Nothing else in `src/` or `scripts/` changed.

**Ruling on the optional "row vanished" line: NOT added.**
`endpoint_status` collapses three different facts into `None` (no row of ours, an unreadable list, a dry run — AC 1 pins all three), so from inside `wait_until_live` a vanished row is indistinguishable from a list we could not read. The suggested wording is therefore out: a `<name> is no longer listed — not waiting` line would also fire one line after "could not read the endpoint list (exit 1)", asserting the row is gone when it may still be provisioning and we merely failed to look — the same fabricated-claim defect class this round is fixing, reworded. A neutrally-worded variant (`stopped waiting for <name> — status unconfirmed`) WOULD be truthful in both cases with no contract change, and I declined it on cost/benefit: in the unreadable case it is a second line immediately after a warning that already says the wait stopped, and in the vanished case the smoke test's verdict lands next anyway — it buys the operator nothing they do not already have, and CLAUDE.md says prefer removing over adding. (Distinguishing the two cases properly would need `endpoint_status` split into a `(status, detail)` private plus a public wrapper, leaving the AC-pinned public `endpoint_status` with no production caller — out of "nothing else changes".) The silence is pinned by an exact-list assertion (below), so re-adding a line has to be a decision, not a drift.

**Tests added** (`apps/memory/tests/unit/models/test_modal_cli.py::TestWaitUntilLive`, +4)
- `::test_a_list_that_goes_unreadable_mid_wait_claims_nothing[a failed list]` and `[output that is not JSON]` — `provisioning`, then `run.state.list_returncode = 1` / `list_stdout = "not json"` applied inside the injected `sleep`. Asserts the WHOLE record list is `["Provisioning: … — 0s/1800s", "could not read the endpoint list (exit 1|invalid JSON) — not waiting for provisioning"]` and `sleeps == [5.0]` — which pins "exactly ONE WARNING", "no `Live:`" and "no consolation line" in one assertion.
- `::test_a_row_that_vanishes_mid_wait_claims_nothing` — `provisioning`, then `run.state.endpoints = []`. Asserts the record list is exactly the one `Provisioning:` line.
- `::test_a_status_that_turns_unknown_mid_wait_is_one_warning_and_no_claim` — `provisioning` -> `failed` (case (c)): exactly `["Provisioning: …", "tree-qwen3-5-0-8b has status 'failed' — not waiting"]`.

**Red -> green evidence**
```
$ PYTEST_ADDOPTS="tests/unit/models/test_modal_cli.py::TestWaitUntilLive -q" make memory-tests   # BEFORE the fix
FAILED ...::test_a_list_that_goes_unreadable_mid_wait_claims_nothing[a failed list]
FAILED ...::test_a_list_that_goes_unreadable_mid_wait_claims_nothing[output that is not JSON]
FAILED ...::test_a_row_that_vanishes_mid_wait_claims_nothing
3 failed, 8 passed in 0.86s

# the failure text, i.e. the Tester's finding reproduced as an assertion:
E       AssertionError: assert ['Provisionin...-8b after 0s'] == ['Provisionin...t — 0s/1800s']
E         Left contains one more item: 'Live: tree-qwen3-5-0-8b after 0s'

$ PYTEST_ADDOPTS="tests/unit/models/test_modal_cli.py::TestWaitUntilLive -q" make memory-tests   # AFTER
11 passed in 0.72s
```
Honest note on the red step: only the three cases above were red. The `provisioning` -> `failed` test (c) was GREEN before the fix too — that path already hit the `status is not None and status != "live"` branch — so it is a pin for a previously untested path, not a reproduction of the bug.

**Full QA (LOCAL env, one command at a time)**
```
$ make env-status            -> Env target: local (.env)
$ make memory-format-check   -> 313 files already formatted
$ make memory-lint-check     -> All checks passed!
$ make pre-commit            -> prettier / ruff check / ruff format / biome check (harness) all Passed
$ make memory-tests          -> 4056 passed in 60.44s (0:01:00)
```
Totals: 4052 (Tester's baseline) + 4 new tests = **4056 passed, 0 failed**.

**Notes**
- The `test_prewarm.py::test_first_failure_cancels_the_sibling` flake did NOT reproduce in this round's full run (4056/4056 in one pass). Nothing new to record on it; left untouched as instructed.
- No doc change needed: `grep -rn "Live:" apps/memory/README.md .agents/skills/run-pipelines-e2e/SKILL.md scripts/modal_model.py` finds one hit, `README.md:719`, which illustrates the happy path (`Provisioning: …` … `Live: … after 548s`) and never promises the line unconditionally — still accurate after the fix.
- `git status --short` is the same 9 files as before; no new file, nothing committed.
- E2E: still NOT RUN — forbidden by this task's HARD SAFETY RULE (no `modal …`, no `scripts/modal_model.py` as a process, no `make memory-deploy-model*`). Live proof remains #141 round 2.

### [Tester] 2026-09-21 20:10 — QA round 2 (focused re-review of Fix round 1)

**Scope of this round.** Round 1 already proved everything except the one blocking defect (false `Live:` after a wait that could not confirm liveness). This round verifies only: the fix itself, its 4 new tests, that nothing else moved, and re-runs the full gate.

**Commands run (LOCAL env only, one at a time)**
```
make env-status                                                          -> Env target: local (.env)
git status --short / git diff --stat                                    -> same 9 files as round 1
for f in <9 files>; do stat -f '%Sm' "$f"; done                          -> only modal_cli.py (19:55:35), test_modal_cli.py (19:55:07)
                                                                             and the task file (19:59:10) postdate round 1's 19:40 review;
                                                                             the other 6 files predate it (untouched since round 1)
git diff -- apps/memory/src/tree/models/modal_cli.py | grep -n '^@@'    -> 6 hunks; read all 6 (old-file starts 24, 33, 79, 181, 209, 283)
shasum apps/memory/src/tree/models/modal_cli.py                          -> d99ac83b51b843cf0986ec8111dd1b5949b8a73d
[targeted Edit: `elif waited and status == "live":` -> `elif waited:`]
PYTEST_ADDOPTS="tests/unit/models/test_modal_cli.py::TestWaitUntilLive -v" make memory-tests -> 3 failed, 8 passed
[targeted Edit: reverted]
shasum apps/memory/src/tree/models/modal_cli.py                          -> d99ac83b51b843cf0986ec8111dd1b5949b8a73d (identical)
uv run python -u <scratch: round2_repro.py>                              -> in-process repro of round-1 case 8 (list unreadable mid-wait), case 9 (row vanishes mid-wait), and the happy path, `run_modal` monkeypatched, no subprocess, no sleep
PYTEST_ADDOPTS="tests/unit/models/test_modal_cli.py::TestEndpointStatus tests/unit/models/test_modal_cli.py::TestWaitUntilLive -v" make memory-tests -> 18 passed
PYTEST_ADDOPTS="tests/unit/models/test_modal_cli.py::TestExistingKind -v" make memory-tests             -> 13 passed
PYTEST_ADDOPTS="tests/unit/models/test_modal_router.py::TestProvisioningNotice tests/unit/scripts/test_modal_model_script.py::TestTestCommandWaitsOutProvisioning -v" make memory-tests -> 7 passed
grep -c "Watch that column" apps/memory/README.md                        -> 0
grep -c "it starts no CLI" apps/memory/scripts/modal_model.py            -> 0
grep -n "Live:" apps/memory/README.md                                    -> one hit, README.md:719, illustrative happy-path example, no unconditional claim
git diff | grep -icE "hf_[a-z0-9]{10,}|wk-[a-z0-9]+|ws-[a-z0-9]+|acme--|modal\.run" -> 1 (self-referential: round-1's own log text quoting this grep command)
git diff | grep -oE "ep-[A-Za-z0-9]{22}" | wc -l                          -> 3 (2 pre-existing placeholder hits, unchanged from round 1; 1 self-referential, round-1's log text quoting the same finding)
make memory-format-check                                                 -> 313 files already formatted
make memory-lint-check                                                   -> All checks passed!
make pre-commit                                                          -> prettier / ruff check / ruff format / biome (harness) all Passed
make memory-tests                                                        -> 4056 passed in 57.83s
for i in 1..10: PYTEST_ADDOPTS="tests/unit/models/test_prewarm.py::test_first_failure_cancels_the_sibling -q" make memory-tests -> 1 passed x10 (single runs, one at a time)
git status --short (final)                                               -> exactly the SWE's 9 files
```
All scratch scripts (`round2_repro.py`) lived under the session scratchpad, outside the repo, never committed. The mutation was a targeted `Edit` + revert proven identical by `shasum`, never `git checkout`/`restore`/`stash`.

**The fix, independently reproduced**
`round2_repro.py` calls `wait_until_live` in-process (`run_modal` monkeypatched, no subprocess, no real sleep) against the CURRENT (fixed) code:
```
=== CASE 8: list unreadable mid-wait (exit 1) ===
INFO Provisioning: tree-qwen3-5-0-8b is not live yet — 0s/1800s
WARNING could not read the endpoint list (exit 1) — not waiting for provisioning
                                                        (no Live: line — round 1's defect is gone)

=== CASE 9: row vanishes mid-wait ===
INFO Provisioning: tree-qwen3-5-0-8b is not live yet — 0s/1800s
                                                        (no Live: line, no warning — this is what the spec mandates, see ruling below)

=== HAPPY PATH: provisioning, provisioning, live ===
INFO Provisioning: tree-qwen3-5-0-8b is not live yet — 0s/1800s
INFO Provisioning: tree-qwen3-5-0-8b is not live yet — 0s/1800s
INFO Live: tree-qwen3-5-0-8b after 0s
```

**Mutation check**
Reverting the fix to `elif waited:` (the pre-fix condition) makes exactly the 3 reproducing tests fail — `test_a_list_that_goes_unreadable_mid_wait_claims_nothing[a failed list]`, `[output that is not JSON]`, `test_a_row_that_vanishes_mid_wait_claims_nothing` (3 failed, 8 passed). `test_a_status_that_turns_unknown_mid_wait_is_one_warning_and_no_claim` stays GREEN under the mutation, confirming the SWE's own honest note: that test pins a previously-untested path (mid-wait `provisioning` -> `failed`, which was already caught by the unaffected `if status is not None and status != "live"` branch, distinct from the first-read `failed` path covered by `test_an_unknown_status_is_not_waited_on`) — it is a worthwhile pin for a real code path, not a reproduction of the round-1 bug. Reverted; `shasum` identical before and after (`d99ac83b51b843cf0986ec8111dd1b5949b8a73d`).

**Rulings requested by the orchestrator**
1. **"Vanished row" consolation line — NOT a judgment call to bless, it is spec-mandated.** Scope item 2 (task file line 32): "status `None` (an App, a stopped endpoint, a dry run, an unreadable list) or `live` -> return at once, **no log line for `None`**." A vanished row collapses to `status = None` inside `endpoint_status` exactly like an unreadable list, a dry run, or no row at all — the spec already forbids a line there. The SWE's reasoning (a "no longer listed" line would fire in the unreadable-list case too and assert something not known) is airtight and consistent with the letter of the spec, not an extra judgment call. Round 1's phrasing ("the row-disappears case is worse: no warning precedes it at all") was true of the OLD, buggy code (which also wrongly printed `Live:` there); under the fix, silence there is correct per spec. Accept as-is.
2. **`test_a_status_that_turns_unknown_mid_wait_is_one_warning_and_no_claim` — keep as a pin, not a bug reproduction.** Confirmed by mutation (stays green under the reverted condition); it exercises a distinct code path (mid-wait status transition) from the pre-existing first-read case, and AC 5's wording ("an unknown status is not waited on") does not distinguish first-read from mid-wait, so the extra coverage is warranted.
3. **`existing_kind`/`guard_deploy`/`_read_rows`/`_list_rows` — unchanged since round 1, `guard_deploy` byte-identical to `HEAD`.** All 6 hunks in the `modal_cli.py` diff read and mapped: module docstring (new `wait_until_live` is explicitly NOT a rail), 3 new imports (`time`, `Callable`, `modal_warmup`) plus `PROVISIONING_DEADLINE_S` (Scope item 2), `existing_kind`'s docstring rewrite (Scope item 5), the new `endpoint_status`/`wait_until_live` functions (with the fix), and the `_list_rows` -> `_read_rows`/`_list_rows` split (feeds `endpoint_status` without duplicating the guard's JSON/list/dict validation). Old-file hunk boundaries are 24, 33, 79, 181, 209, 283 — `guard_deploy` sits at old lines ~215–282, between the `existing_kind`-tail hunk (209) and the next hunk (283), so no hunk touches it: it is byte-identical to `HEAD`, not merely unchanged since round 1. This closes the "diff since round 1" question precisely rather than by mtime inference alone (mtimes corroborate: only `modal_cli.py`, `test_modal_cli.py`, and the task file postdate round 1's 19:40 review).
4. **README `Live:` claim — verified directly, not taken on the SWE's word.** `README.md:719` illustrates the happy-path sequence (`Provisioning: … 120s/1800s` … `Live: … after 548s`) inside a sentence describing what `-test` does "by itself... and only then polls `/health`" — it never states the `Live:` line is unconditional or that it always follows a wait. Accurate before and after the round-2 fix; no doc update needed.

**Acceptance criteria** — unchanged from round 1 (all 12 node ids already `[x]`), re-verified green (see command list above); the 4 new `TestWaitUntilLive` tests are additional regression coverage for the fixed defect, not new ACs.

**Other issues found (carried forward from round 1, unaddressed by this round and still true — not blocking)**
- `modal_router.py:320`'s custom-weights branch call to `_PROVISIONING_NOTICE` still has no dedicated test (`TestProvisioningNotice` only covers the plain-create branch). AC 9 does not require it; flagging for awareness.
- `guard_deploy`/`existing_kind` still do not refuse a row present but missing a `name` key — byte-identical to `HEAD`, pre-existing, out of scope.
- The single `opik`/pydantic-v1 `UserWarning` at test-session start is a pre-existing dependency import warning (identical at `HEAD`), not a pytest-collected warning — the "0 warnings" bar holds.
- `test_prewarm.py::test_first_failure_cancels_the_sibling` did NOT flake this round: one clean full run at 4056/4056, plus 10/10 passes in isolation (single runs, one at a time). No new failure text to record; round 1's captured failure text stands as the only evidence for a follow-up.

**Evidence**
```
$ uv run python -u round2_repro.py
=== CASE 8: list unreadable mid-wait (exit 1) ===
INFO Provisioning: tree-qwen3-5-0-8b is not live yet — 0s/1800s
WARNING could not read the endpoint list (exit 1) — not waiting for provisioning

=== CASE 9: row vanishes mid-wait ===
INFO Provisioning: tree-qwen3-5-0-8b is not live yet — 0s/1800s

=== HAPPY PATH: provisioning, provisioning, live ===
INFO Provisioning: tree-qwen3-5-0-8b is not live yet — 0s/1800s
INFO Provisioning: tree-qwen3-5-0-8b is not live yet — 0s/1800s
INFO Live: tree-qwen3-5-0-8b after 0s

$ PYTEST_ADDOPTS="tests/unit/models/test_modal_cli.py::TestWaitUntilLive -v" make memory-tests   # mutated back to `elif waited:`
FAILED ...test_a_list_that_goes_unreadable_mid_wait_claims_nothing[a failed list]
FAILED ...test_a_list_that_goes_unreadable_mid_wait_claims_nothing[output that is not JSON]
FAILED ...test_a_row_that_vanishes_mid_wait_claims_nothing
3 failed, 8 passed in 0.74s

$ make memory-tests   # after revert
============================ 4056 passed in 57.83s =============================
```

**VERDICT: PASS**

The blocking defect from round 1 (false `Live:` after an unconfirmed wait) is fixed with the exact one-line condition change prescribed, independently reproduced against the source (not just the SWE's own tests), and mutation-tested (revert -> exactly the 3 reproducing tests fail, the 4th stays green as an unrelated, worthwhile pin). The declined "vanished row" consolation line is not a gap — the spec (Scope item 2) already mandates silence for any `None` status, and the SWE's reasoning matches the spec's letter. Nothing else in the diff moved since round 1 (confirmed by hunk-range analysis, not just mtimes): `existing_kind`, `guard_deploy`, `_read_rows`/`_list_rows` are unchanged, `guard_deploy` byte-identical to `HEAD`. All 12 original ACs plus the 4 new regression tests pass (4056/4056); format, lint, pre-commit all green; both greps investigated and explained as self-referential/pre-existing, not real hits; README's `Live:` reference verified accurate. `test_prewarm.py` flake did not reproduce this round (10/10 isolated, 1/1 clean full run) — not a reason to fail, evidence recorded for the standing follow-up.
