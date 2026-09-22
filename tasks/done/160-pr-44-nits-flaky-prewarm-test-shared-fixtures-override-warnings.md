---
id: 160-pr-44-nits-flaky-prewarm-test-shared-fixtures-override-warnings
status: done
feature: voyage-4-and-modal-embedding-catalog
---

# PR #44 nits the human selected: the order-dependent `test_prewarm.py` assertion, the duplicated Modal test fixtures, and the two dispatch scripts that still ignore shell overrides silently

Tags: `memory`, `tests`, `scripts`
Depends on: #159 (the five sibling scripts already warn)
Blocks: none — lands on PR #44 before merge

## Scope

Three of the 21 Nits the PR-Reviewer appended to PR #44. The human picked exactly these; the duplicated
code inside the two Modal CLIENTS (`modal_embedding.py` / `modal_llm.py`) is explicitly ACCEPTED and must
NOT be touched.

**HARD SAFETY RULE:** No agent may run `make memory-deploy-*`, `scripts/modal_*.py` as a process, or any
`modal …` command. Run test commands one at a time. Do not run the real memory pipelines against the
local database. Never open, cat, grep or `source` `.env` / `.env.prod`.

1. **Flaky assertion (Nit 1).** `tests/unit/models/test_prewarm.py` asserts `asyncio.all_tasks() == before`
   (lines ~118 and ~177). That compares EVERY task on the shared event loop, so a lingering task from another
   test module (a Mongo client keep-alive) fails it ~1 run in 6. Assert what the test means: no NEW,
   still-running task was left by `prewarm_models` — `{t for t in asyncio.all_tasks() if not t.done()} - before
   == set()`, through one small helper used by both sites. The intent comments stay.
2. **Duplicated Modal test fixtures (Nit 3).** The nine fixtures/helpers identical between
   `tests/unit/models/test_modal_embedding.py` and `test_modal_llm.py` (~250 lines), and `_endpoint_row` /
   `_app_row` / `hub` repeated across `test_modal_cli.py`, `test_modal_router.py` and
   `scripts/test_modal_model_script.py`, move to ONE `tests/unit/models/conftest.py` (or a small
   `tests/unit/models/modal_fixtures.py` imported by the scripts test if pytest's conftest scoping does not
   reach `tests/unit/scripts/`). Behaviour-neutral: every test keeps its node id and its assertions; the
   two unit-suite rails in `tests/unit/conftest.py` (`_modal_dry_run`, `_no_live_modal_sdk`) are untouched.
3. **Ignored-override warnings (Nit 20).** `scripts/run_data_pipeline.py` (`_run_offline` AND `_run_online`)
   gets the same two `warn_ignored_config_overrides("TREE_MODELS__")` / `("TREE_MODAL__")` calls before
   dispatch as its five siblings (#159). `scripts/run_dream_consolidation.py` additionally warns for
   `TREE_DREAM__` (the dream flow reads `app_config.dream.*` — `dream.dry_run` — in the serving process).
   Glue only; `init_logger()` stays at module level. `warn_ignored_config_overrides` may take `*prefixes`
   (Nit 10) if that removes the six repeated pairs without changing any message.

**Out of scope:** everything else on the Nits list; the Modal clients; any README change beyond the one
sentence at `apps/memory/README.md` step 4 if "(the command warns when it sees one)" becomes true for all
six dispatchers (then it needs no change).

## Acceptance Criteria

- [x] `tests/unit/models/test_prewarm.py`: no `asyncio.all_tasks() == before` remains; the two sites assert
      "no new non-done task"; the module is green 10× in isolation AND green when a deliberately leaked
      background task from a fixture in another module is alive (add one test in the same module that
      spawns a never-finishing task, runs `prewarm_models` on a no-gate model, asserts the helper passes,
      then cancels the task).
- [x] `tests/unit/models/conftest.py` exists; `grep -c "def _endpoint_row\|def _app_row\|def hub" ` over
      `test_modal_embedding.py test_modal_llm.py test_modal_cli.py test_modal_router.py
      tests/unit/scripts/test_modal_model_script.py` → 0 each; the nine shared fixtures are defined ONCE;
      `make memory-tests` collects the SAME node ids as before (`--collect-only -q` diff against HEAD is
      empty apart from the new tests of this task).
      AMENDED (orchestrator ruling): "nine shared fixtures defined once" → "every byte-identical
      fixture/helper defined once; structurally different ones stay". Verified true under the amended
      reading — see Tester log.
- [x] `tests/unit/scripts/test_run_data_pipeline.py`: `TestRunDataPipelineIgnoredOverrides` with the warn
      test and the prefix-hermetic clean-shell test, for the offline path; and the `_run_online` path of
      BOTH `run_data_pipeline.py` and `run_pipeline.py` gets a warn test (the PR-Reviewer noted
      `_run_online` had none).
- [x] `tests/unit/scripts/test_run_dream_consolidation.py`: a `TREE_DREAM__DRY_RUN` override in the shell
      produces the warning naming that variable.
- [x] `TREE_MODAL_DRY_RUN` (single underscore, the suite rail) never trips the `TREE_MODAL__` warning
      (existing guarantee — a test pins it if none does).
- [x] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green.

## User Stories

### Story: CI stops failing on a test that was never wrong
1. A Mongo keep-alive task from another module is still alive when `test_prewarm.py` runs.
2. The assertion ignores it: it only fails when `prewarm_models` itself left a running task.

### Story: One place to fix a Modal fixture
1. An engineer changes the shape of a fake endpoint row.
2. They edit `tests/unit/models/conftest.py` once; five test modules pick it up.

### Story: The data pipeline says why the override did nothing
1. `TREE_MODELS__SEARCH_EMBEDDING__DIMENSIONS=2048 make memory-run-data-pipeline`.
2. The dispatcher logs `TREE_MODELS__SEARCH_EMBEDDING__DIMENSIONS set in THIS shell, but the flow reads its
   config where it RUNS … the override is IGNORED for this run.` — same as the other five scripts.

## Log

### [SWE] 2026-09-22 09:56 — Implementation

**Files modified**
- `apps/memory/tests/unit/models/test_prewarm.py` — `_running_tasks()` helper; the FOUR (not two)
  `asyncio.all_tasks() == before` sites now assert `_running_tasks() - before == set()`; two new tests
  for a foreign task that retires mid-call and one that never finishes.
- `apps/memory/tests/unit/models/modal_fixtures.py` (new) — the Modal doubles shared by more than one
  module: `endpoint_row` / `app_row`, the `hub` fixture, `SuspendingServer` + `patch_suspending_server`,
  `patch_server`.
- `apps/memory/tests/unit/models/conftest.py` (new) — re-exports `hub` to this package.
- `apps/memory/tests/unit/scripts/conftest.py` (new) — re-exports `hub` to the sibling package (a
  module-level import there collides with the `hub` test argument: ruff F811 × 8).
- `apps/memory/tests/unit/models/test_modal_cli.py`, `test_modal_router.py`, `test_modal_embedding.py`,
  `test_modal_llm.py`, `apps/memory/tests/unit/scripts/test_modal_model_script.py` — the moved
  definitions deleted, imports/thin fixtures in their place.
- `apps/memory/src/tree/cli.py` — `warn_ignored_config_overrides(*prefixes)`; `str.startswith` takes the
  tuple, so the message template is byte-unchanged.
- `apps/memory/scripts/run_data_pipeline.py` — warns in `_run_offline` AND `_run_online`, before
  `connect_and_resolve_user` like its siblings.
- `apps/memory/scripts/run_dream_consolidation.py` — additionally warns for `TREE_DREAM__`.
- `apps/memory/scripts/run_pipeline.py`, `run_memory_pipeline.py`, `run_indexing_pipeline.py`,
  `run_clustering_pipeline.py` — the repeated call pairs collapsed into one call each.
- `apps/memory/tests/unit/scripts/test_run_data_pipeline.py`, `test_run_dream_consolidation.py`,
  `test_run_pipeline.py`, `apps/memory/tests/unit/test_cli.py` — the nine new tests.

**Tests**
- Unit: 4074 passing, 0 failing (baseline 4065 + 9 new) — `make memory-tests`.
- Integration: N/A — this repo has no integration suite (AGENTS.md).
- `tests/unit/models/test_prewarm.py` green 10× in isolation, one run at a time.
- `--collect-only -q` diff vs HEAD: the 9 new node ids added, ZERO existing node ids changed or removed.

**Acceptance criteria**
- [x] `test_prewarm.py` — `grep -c "asyncio.all_tasks() == before"` → 0; verified by
      `tests/unit/models/test_prewarm.py::test_a_foreign_task_that_retires_mid_call_is_not_a_leak` and
      `::test_a_leaked_foreign_task_that_never_finishes_is_not_a_leak`.
- [ ] shared Modal fixtures — LEFT UNCHECKED on purpose. Conftest exists, the three greps are 0 each,
      the collect diff is empty apart from the new tests; but "the nine shared fixtures are defined
      ONCE" cannot be honestly claimed — the nine do not exist. See Notes for the per-fixture inventory.
- [x] `TestRunDataPipelineIgnoredOverrides` + both `_run_online` warn tests — verified by
      `tests/unit/scripts/test_run_data_pipeline.py::TestRunDataPipelineIgnoredOverrides::{test_offline_warns_when_a_model_override_is_set_in_this_shell,test_online_warns_when_a_model_override_is_set_in_this_shell,test_a_clean_shell_dispatches_without_a_warning}`
      and `tests/unit/scripts/test_run_pipeline.py::TestRunPipelineIgnoredOverrides::test_the_online_path_warns_too`.
- [x] `TREE_DREAM__DRY_RUN` — verified by
      `tests/unit/scripts/test_run_dream_consolidation.py::TestRunDreamConsolidationIgnoredOverrides::test_it_warns_when_a_dream_override_is_set_in_this_shell`.
- [x] `TREE_MODAL_DRY_RUN` never trips `TREE_MODAL__` — verified by
      `tests/unit/test_cli.py::TestWarnIgnoredConfigOverrides::test_the_suite_s_single_underscore_dry_run_rail_is_not_an_override`.
- [x] format-check / lint-check / pre-commit / tests green.

**Evidence**
```
$ PYTEST_ADDOPTS="tests/unit/models/test_prewarm.py -q -k foreign" make memory-tests   # RED, before the fix
>       assert asyncio.all_tasks() == before
E       AssertionError: assert {<Task pendin...k_wakeup()]>>} == {<Task finish...py:503]>, ...}
E         Extra items in the right set:
E         <Task finished name='Task-59' coro=<sleep() done ...> result=None>
1 failed, 16 deselected in 1.12s

$ PYTEST_ADDOPTS="tests/unit/test_cli.py tests/unit/scripts/... -q" make memory-tests  # RED, item 3
5 failed, 30 passed in 1.68s

$ make memory-tests
============================ 4074 passed in 55.15s =============================

$ diff <collect-only baseline> <collect-only after>
> tests/unit/models/test_prewarm.py::test_a_foreign_task_that_retires_mid_call_is_not_a_leak
> tests/unit/models/test_prewarm.py::test_a_leaked_foreign_task_that_never_finishes_is_not_a_leak
> tests/unit/scripts/test_run_data_pipeline.py::TestRunDataPipelineIgnoredOverrides::* (3)
> tests/unit/scripts/test_run_dream_consolidation.py::...::test_it_warns_when_a_dream_override_is_set_in_this_shell
> tests/unit/scripts/test_run_pipeline.py::...::test_the_online_path_warns_too
> tests/unit/test_cli.py::TestWarnIgnoredConfigOverrides::* (2)
(no removals, no renames)

$ TREE_MODELS__SEARCH_EMBEDDING__DIMENSIONS=2048 TREE_MODAL__REQUEST_TIMEOUT_S=600 \
  TREE_DREAM__DRY_RUN=true TREE_MODAL_DRY_RUN=1 uv --directory apps/memory run python -c "…"
TREE_MODAL__REQUEST_TIMEOUT_S, TREE_MODELS__SEARCH_EMBEDDING__DIMENSIONS set in THIS shell, but the flow
reads its config where it RUNS — … the override is IGNORED for this run.
TREE_DREAM__DRY_RUN, TREE_MODAL__REQUEST_TIMEOUT_S, TREE_MODELS__SEARCH_EMBEDDING__DIMENSIONS set in THIS
shell, but … IGNORED for this run.
(`TREE_MODAL_DRY_RUN` was set in the same shell and appears in neither line.)
```

**Notes**
- Item 1: the flake is NOT the done-filter. `asyncio.all_tasks()` already drops finished tasks, so
  `{t for t in … if not t.done()}` is close to a no-op; what fixes it is the SUBTRACTION. The old `==`
  broke on a foreign task LEAVING the set mid-call, which is exactly what the new red test reproduces.
  The task named two sites; there were four (`test_noop_without_ensure_warm`,
  `test_first_failure_cancels_the_sibling`, `test_a_sync_ensure_warm_does_not_leak_the_sibling`,
  `test_default_providers_have_no_gate_to_warm`) — all four converted.
- Item 2, the Nit's premise is wrong: the "nine identical fixtures/helpers, ~250 lines" between
  `test_modal_embedding.py` and `test_modal_llm.py` do not exist. Inventory:
  * byte-identical → MOVED: `_SuspendingServer` (35 lines × 2), `_endpoint_row` / `_app_row`
    (cli + script; the two fake ids differed, nothing asserts them — `test_the_guard_rows_match_the_pinned
    _clients_columns` asserts the KEY SET), `hub` (router + script; the router's is a strict superset,
    the script's uses only `cards`/`requests`).
  * identical modulo the module patched → MOVED behind a factory: `server` / `suspending_server`
    (`patch_server` / `patch_suspending_server`, 2-line fixtures left in each module).
  * differ structurally → LEFT in place, deliberately: `_OpenAI` + `_Embeddings` vs `_Completions`
    (`client.embeddings` vs `client.chat.completions`), `_WireStub` (embeddings response vs chat
    completion, `finish_reason` / `reasoning_content`), `_response`, `_model`, `_cold` / `_bad_request`
    (different URL path per client). One class serving both wire contracts is a `path=("chat",
    "completions")` indirection over 2600 lines of dependent assertions — worse than the duplication,
    and the same trade the human already accepted inside `modal_embedding.py` / `modal_llm.py`.
  * mechanism, as the task asked to state: doubles in `tests/unit/models/modal_fixtures.py` (same
    pattern as `tests/prefect_doubles.py`), fixtures re-exported through TWO conftests — the models one
    and a new `tests/unit/scripts/conftest.py`, because a conftest is invisible to a sibling directory
    and importing `hub` into `test_modal_model_script.py` directly trips ruff F811 on every test that
    takes `hub` as an argument. Neither conftest redefines anything from `tests/unit/conftest.py`,
    which is byte-unchanged (`git diff` empty).
- Item 3, the one observable change: a dispatcher now logs ONE warning line naming every matching
  variable instead of one line per prefix. The message template is byte-identical and every existing
  assertion is an `any(... for message in messages)`, so no caller test moved. `_run_online` of
  `run_pipeline.py` already warned; its test is new (the PR-Reviewer's point).
- `apps/memory/README.md` needs no change: "(the command warns when it sees one)" (line 739) is now
  true for all six dispatchers.
- E2E: no pipeline was run (task safety rule). The runtime surface was exercised in a real process —
  the helper above, plus `--help` on both touched scripts.

### [Tester] 2026-09-22 10:07 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check`, `make memory-lint-check` — "All checks
  passed!", `make pre-commit` — all hooks Passed, no ruff F811)
- Unit tests: 4074 passed / 0 failed (`make memory-tests`)
- Integration tests: N/A — no integration suite (AGENTS.md)
- Warnings: 0 pytest warnings in the full run

**E2E adversarial pass**
- Happy path: `make memory-tests` → `4074 passed in 52.58s` (PASS)
- Break path 1 (mutation — restore the flaky `==` at one site): edited
  `tests/unit/models/test_prewarm.py::test_a_foreign_task_that_retires_mid_call_is_not_a_leak`'s final
  assertion from `_running_tasks() - before == set()` back to `_running_tasks() == before`, ran
  `PYTEST_ADDOPTS="tests/unit/models/test_prewarm.py -q -k test_a_foreign_task_that_retires_mid_call_is_not_a_leak" make memory-tests`
  → `AssertionError: assert {...} == {...}` with `Extra items in the right set: <Task finished
  name='Task-59' ...>` — i.e. the SAME shape of failure the SWE's red-probe evidence shows, confirming
  the new test genuinely exercises the old bug. Reverted the edit; `shasum -a 256
  tests/unit/models/test_prewarm.py` before and after the mutation are byte-identical
  (`43ee95acef2474e153249e874c6b58633017ef0bba61f37d2b47e38c8c26b22f` both times). (PASS)
- Break path 2 (module run 10× in isolation, one run at a time): `PYTEST_ADDOPTS="tests/unit/models/test_prewarm.py -q" make memory-tests`
  run 10 separate times sequentially → `18 passed` every time, 0 flakes. (PASS)
- Break path 3 (does the fix still catch a REAL leak, not just tolerate foreign ones): throwaway
  scratch-only probe (`uv run python <scratchpad>/leak_probe.py`, never written into the repo) that
  monkeypatches a fake gate's `ensure_warm` to `asyncio.create_task` a never-finishing orphan and return
  without awaiting it — simulating a task `prewarm_models`'s own call graph leaves alive past the call.
  Output: `leaked task in after-before: True` /
  `PASS: helper's assertion correctly FAILS the real-leak case (after-before is non-empty)` — i.e. the
  subtraction-based helper is not a rubber stamp; a genuine leak still fails `_running_tasks() - before
  == set()`. (PASS)
- Break path 4 (collection parity — did any existing node id move/vanish): built a HEAD baseline with
  `git worktree add --detach <scratchpad>/head-baseline HEAD` (no `stash`/`checkout`/`reset` on the
  working tree), symlinked the existing `.venv` in (pyproject.toml/uv.lock unchanged per `git diff
  --stat`), ran `uv run pytest --collect-only -q tests/unit` in both the baseline worktree (4065
  collected) and the current tree (4074 collected), sorted and diffed the two node-id lists → the ONLY
  diff is 9 additions (the task's 9 new tests), zero removals, zero renames. Worktree removed afterward
  (`git worktree remove --force`); `git status` on the working tree is unchanged from before the check.
  (PASS)

**Acceptance criteria**
- [x] PASS — no `asyncio.all_tasks() == before` remains; helper subtracts `before`; 10×-isolation green;
      green with a leaked foreign task alive — Evidence: `grep -c "asyncio.all_tasks() == before"
      tests/unit/models/test_prewarm.py` → 0; `grep -c "_running_tasks() - before == set()"` → 6 (all
      four original sites + the two new tests); mutation + 10× isolation + real-leak probe above.
- [x] PASS — `tests/unit/models/conftest.py` exists; the three greps are 0 each over the five named
      modules; `tests/unit/conftest.py` byte-unchanged (`git diff --stat -- apps/memory/tests/unit/conftest.py`
      empty); AMENDED reading of "shared fixtures defined once" holds: byte-identical
      `_SuspendingServer`/`_endpoint_row`/`_app_row`/`hub` moved to
      `tests/unit/models/modal_fixtures.py`; identical-modulo-module `server`/`suspending_server` moved
      behind `patch_server`/`patch_suspending_server` factories; structurally different doubles
      (`_OpenAI`+`_Embeddings` vs `_Completions`, `_WireStub`, `_response`, `_cold`, `_bad_request`)
      verified still present and DISTINCT in both `test_modal_embedding.py` and `test_modal_llm.py`
      (`grep -n "^class \|^def "` on both files). `modal_embedding.py`/`modal_llm.py` confirmed untouched
      (`git diff --stat` empty for both). `--collect-only -q` diff vs HEAD (via `git worktree`, not
      `stash`) is empty apart from the 9 new node ids — see break path 4. No ruff F811 (`make
      memory-lint-check` → "All checks passed!").
- [x] PASS — `TestRunDataPipelineIgnoredOverrides` (offline warn + online warn + clean-shell) and
      `TestRunPipelineIgnoredOverrides::test_the_online_path_warns_too` present and passing —
      `tests/unit/scripts/test_run_data_pipeline.py`, `tests/unit/scripts/test_run_pipeline.py` (both in
      the green 4074).
- [x] PASS — `TestRunDreamConsolidationIgnoredOverrides::test_it_warns_when_a_dream_override_is_set_in_this_shell`
      sets `TREE_DREAM__DRY_RUN` and asserts the warning names it — passing.
- [x] PASS — `TestWarnIgnoredConfigOverrides::test_the_suite_s_single_underscore_dry_run_rail_is_not_an_override`
      pins `TREE_MODAL_DRY_RUN` never trips `TREE_MODAL__` — passing.
- [x] PASS — `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests`
      all green, 4074 passed, 0 warnings.

**Evidence**
```
$ PYTEST_ADDOPTS="tests/unit/models/test_prewarm.py -q -k test_a_foreign_task_that_retires_mid_call_is_not_a_leak" make memory-tests   # mutated back to ==
>       assert _running_tasks() == before
E       AssertionError: assert {<Task pendin...asks.py:503]>} == {<Task pendin...py:181]>, ...}
E         Extra items in the right set:
E         <Task finished name='Task-59' coro=<sleep() done ...> result=None>
1 failed, 17 deselected in 0.99s

$ shasum -a 256 tests/unit/models/test_prewarm.py   # before mutation, and again after revert
43ee95acef2474e153249e874c6b58633017ef0bba61f37d2b47e38c8c26b22f  tests/unit/models/test_prewarm.py
43ee95acef2474e153249e874c6b58633017ef0bba61f37d2b47e38c8c26b22f  tests/unit/models/test_prewarm.py

$ uv run python <scratchpad>/leak_probe.py
leaked task in after-before: True
PASS: helper's assertion correctly FAILS the real-leak case (after-before is non-empty)

$ diff baseline_nodeids.txt current_nodeids.txt
3612a3613,3614
> tests/unit/models/test_prewarm.py::test_a_foreign_task_that_retires_mid_call_is_not_a_leak
> tests/unit/models/test_prewarm.py::test_a_leaked_foreign_task_that_never_finishes_is_not_a_leak
3823a3826,3828
> tests/unit/scripts/test_run_data_pipeline.py::TestRunDataPipelineIgnoredOverrides::* (3)
3831a3837
> tests/unit/scripts/test_run_dream_consolidation.py::...::test_it_warns_when_a_dream_override_is_set_in_this_shell
3855a3862
> tests/unit/scripts/test_run_pipeline.py::...::test_the_online_path_warns_too
3876a3884,3885
> tests/unit/test_cli.py::TestWarnIgnoredConfigOverrides::* (2)
(no removals, no renames)

$ make memory-format-check
317 files already formatted

$ make memory-lint-check
All checks passed!

$ make pre-commit
prettier ... Passed
ruff check ... Passed
ruff format ... Passed
biome check (harness) ... Passed

$ make memory-tests
============================ 4074 passed in 52.58s =============================
```

**Other issues found**
- None blocking. `git diff | grep -inE "api[_-]?key|secret|token|password|bearer|AKIA|sk-[a-zA-Z0-9]"` →
  6 hits, all benign (`parse_uri_token` import, `bearer: str` parameter names, `SecretStr` import,
  `no_token` fixture name) — no secret VALUES in the diff.
- The SWE's log is candid that the task's literal premise ("nine identical fixtures, ~250 lines") does
  not hold and left that AC line unchecked with a full inventory; the orchestrator's amendment covers
  this, and I independently re-verified the inventory against the diffs (byte-identical vs
  identical-modulo-module vs structurally-different) rather than taking the SWE's classification on
  faith.

**VERDICT: PASS**
