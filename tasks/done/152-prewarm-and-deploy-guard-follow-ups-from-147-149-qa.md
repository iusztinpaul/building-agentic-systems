---
id: 152-prewarm-and-deploy-guard-follow-ups-from-147-149-qa
status: done
feature: voyage-4-and-modal-embedding-catalog
---

# Small Modal hardening follow-ups from #147's and #149's QA: the Pre-warm never leaks a poll and never builds a model it cannot warm; the deploy-script guards also catch a spawned process and a differently-spelled logger

Tags: `memory`, `modal`, `prewarm`, `deploy`, `tests`, `bug`
Depends on: None (#147, #148, #149 are done; #150 and #151 run first by NNN order only)
Blocks: #141
Implements: ADR-009 — Decision 11 (**Pre-warm**: "fail-fast with an explicit cancel", "never wake / pay for what the run does not use") and Decision 9 (the `HF_TOKEN` must never reach `modal app logs`). No new decision — four Tester findings against guarantees those decisions already make.

## Scope

**EXECUTION ORDER of the rework: 143 -> 144 -> 145 -> 146 -> 147 -> 148 -> 149 -> 150 -> 151 -> 152 -> 141.**

**HARD SAFETY RULE:** No agent may run `make memory-deploy-*`, `scripts/modal_*.py` as a process, or any `modal …` command outside task 141; a PATH shim does NOT intercept under `make`/`uv run` because `.venv/bin` wins — use the mocked unit tests and `DRY_RUN=yes`.
Run test commands one at a time — two concurrent pytest runs contend for the local MongoDB/Prefect and stall.
Do not run the real memory pipelines against the local database (shared with the human's `main` checkout).
The two deploy scripts (`apps/memory/deploy/modal_*.py`) are only ever READ by this task — never edited, imported or executed; every mutant is a copy in `tmp_path` (the existing `_mutate` helper).

One task, in the shape of #150: four small Tester findings plus three cosmetics, all "the guarantee the docstring
already claims is not enforced". None changes behaviour on the default (Voyage + Gemini) path.

**1. `prewarm_models` leaks an already-scheduled poll when a LATER `ensure_warm` is not a coroutine function**
(#149 Tester, finding 1; commit d3a9fe2 points here). `apps/memory/src/tree/models/get_model.py:229`:
`tasks = [asyncio.create_task(warm()) for warm in warms.values()]` sits OUTSIDE the `try/finally`. If the second
model's `ensure_warm` is a plain `def` (`warm()` returns `None`), `create_task` raises
`TypeError: a coroutine was expected, got None` before the `try` is entered, so the first model's real poll is
never cancelled — against this function's own "FAIL-FAST WITH AN EXPLICIT CANCEL" docstring. Fix: build the list
INCREMENTALLY INSIDE the `try` (`tasks: list[asyncio.Task[None]] = []` before it, `tasks.append(create_task(…))`
inside), so the existing `finally` cancels and reaps whatever was scheduled. The `TypeError` propagates unchanged.
Do not add an `inspect.iscoroutinefunction` pre-check — the `finally` already covers it.

**2. The throw-away pre-warm seams construct a model just to discover it has nothing to warm** (#149 Tester,
finding 2). `pipeline.py:2028-2033` (extraction worker) and `:2616` (cluster summaries) call
`get_search_embedding_model()` / `get_resolution_embedding_model()` / `get_llm()` unconditionally and throw the
instance away. For `sentence-transformers` that is `SentenceTransformer(...)` — a torch weight load, seconds
plus memory — paid on every `rag` / `graphrag` run, even a fully cached one that previously built no model at
all. Fix: check the configured provider BEFORE calling the factory.
- ONE helper in `get_model.py`, next to `prewarm_models`:
  `modal_backed_models(*blocks: Literal["llm", "resolution_embedding", "search_embedding"]) -> list[object]` —
  for each block, reads `app_config.models.<block>.provider` at CALL time and calls that block's EXISTING
  factory (`get_llm` / `get_resolution_embedding_model` / `get_search_embedding_model`) only when it equals
  `"modal"`; every other provider contributes nothing and its factory is not called. No provider registry, no
  new config key.
- Worker seam: `rag` -> `await prewarm_models(*modal_backed_models("search_embedding"))`; otherwise
  `await prewarm_models(*modal_backed_models("llm", "resolution_embedding", "search_embedding"))`.
- Cluster-summary seam: `prewarm_models(*modal_backed_models("llm"))` with the construction still OUTSIDE the
  `try` (a missing **Proxy token** must keep failing the run loudly; only the WARM fails open — keep that
  comment).
- `prewarm_models` itself stays duck-typed and keeps its name and signature (many tests patch
  `tree.memory.pipeline.prewarm_models`); `prewarm_models()` with zero models is already a silent no-op.
- `dream.py:674-678` is NOT changed: it pre-warms the SAME instances the sweep then uses, so nothing is built
  "just to find out". Say so in `## Log`.

**3. Deploy-script static guard: a spawned process is invisible to every guard** (#147 Tester). In
`apps/memory/tests/unit/deploy/test_modal_deploy_scripts.py`, `subprocess.run(["env"])` / `os.system("env")`
inside `@modal.enter` dumps the container environment — the real `HF_TOKEN` — to the inherited stdout, i.e.
into `modal app logs`, and passes `_console_writes`, `_environ_reads` and `_log_offenders`. Add ONE AST guard
`_process_spawns(module) -> list[str]` in the style of `_console_writes`, reporting: any `import` /
`from … import` of `subprocess`, `pty` or `commands` (top-level or nested, aliased or not), and any call or
attribute reference whose dotted name is `os.system`, `os.popen`, or starts with `os.exec` / `os.spawn` /
`os.posix_spawn`, plus `from os import system|popen|exec*|spawn*|posix_spawn*`. Wire it into
`TestGlueContract` (parametrised over both scripts via `_ENGINES`) and into the control row
`test_the_shipped_script_passes_every_guard`.

**4. Deploy-script static guard: the log-message allow-list only sees calls spelled `logger.*`** (#147 Tester).
`_log_offenders` (`:439-468`) filters on `_dotted(node.func).startswith("logger.")`, so
`logging.getLogger().info(...)`, `logging.info(...)` and `log = logging.getLogger(__name__); log.info(...)`
bypass the allow-list. Fix, receiver-agnostic (no alias resolution needed): treat as a logging call ANY
`ast.Call` whose `func` is an `ast.Attribute` with `attr` in
`{"debug", "info", "warning", "warn", "error", "exception", "critical", "fatal", "log"}`, whatever the receiver;
for `.log(...)` the message is `args[1]`, otherwise `args[0]`. `logging.basicConfig(...)` /
`logging.getLogger(...)` are not emit methods and stay legal (both scripts use them, `:97-104`). If a shipped
script has a non-logging call with one of those attribute names, exempt that ONE dotted name explicitly with a
comment — do not narrow the rule.

**5. Cosmetics collected by the commit agents** (no behaviour change):
- `tests/unit/models/test_get_model.py::TestGetLLM::test_a_non_catalog_model_fails_before_any_gpu_wakes` (`:138`): drop the unused `mocker` parameter.
- `get_model.py:15`: re-wrap the one over-long comment line of the module NOTE to the file's width.
- Rename `EMBEDDING_SERVER_NAME` -> `MODAL_SERVER_NAME` (it names the `Server` class of LLM Apps too since #147/#148). Judged cheap and safe: value stays `"Server"`, 15 hits in 6 files (`modal_catalog.py`, `modal_server.py`, `modal_llm.py`, `modal_embedding.py`, `tests/unit/deploy/test_modal_deploy_scripts.py`, `tests/unit/models/test_modal_catalog.py`), 0 hits in `docs/` and in the deploy scripts (they spell the class `Server` literally; the test compares). No alias for the old name is kept.

## Out of scope
- Any edit to `apps/memory/deploy/modal_*.py`, `scripts/modal_*.py`, the Makefile or the driver.
- Gating the dream seam, the indexing backfill or the MCP lifespan (unchanged — see item 2 and the glossary's **Pre-warm** entry).
- A generic "provider has a warm-up" capability flag / registry — `== "modal"` is the whole rule until a second warmable provider exists.
- Resolving logger aliases by data-flow (item 4's receiver-agnostic rule makes it unnecessary).
- The LLM cache identity (#151). ADR-009 / glossary edits (none needed: the **Pre-warm** entry describes `prewarm_models`, which is unchanged).

## Acceptance Criteria

Pre-warm — `apps/memory/tests/unit/models/test_prewarm.py`:
- [x] `test_a_sync_ensure_warm_does_not_leak_the_sibling`: models = (a fake whose `async ensure_warm` awaits `asyncio.sleep(600)`, FIRST; a fake whose `ensure_warm` is a plain `def` returning `None`, SECOND; distinct `warm_key`s). `before = asyncio.all_tasks()` -> `pytest.raises(TypeError, match="a coroutine was expected")` -> afterwards `asyncio.all_tasks() == before`, and the sleeping fake recorded a `CancelledError` if it had started (or never started) — it is NOT pending. The test FAILS on the code at d3a9fe2 (state that you saw it red in `## Log`).
- [x] `test_first_failure_cancels_the_sibling`, `test_warms_concurrently`, `test_dedupes_on_warm_key`, `test_is_idempotent`, `test_noop_without_ensure_warm` stay green unmodified.
- [x] `TestModalBackedModels::test_a_non_modal_provider_never_calls_its_factory`: parametrised over provider in `("sentence-transformers", "voyage", "gemini", "mock")` for the embedding blocks and `"gemini"` for `llm` — with `tree.models.get_model._build_embedding_model` / `get_llm` patched as tripwires, `modal_backed_models("llm", "resolution_embedding", "search_embedding") == []` and NO tripwire was called; `"sentence_transformers" not in sys.modules` growth is not required, the call count is.
- [x] `TestModalBackedModels::test_a_modal_block_is_built_once`: only `search_embedding.provider == "modal"` -> exactly one factory call (the search one) and a one-element list holding its return value; all three `modal` -> three elements in the order asked.
- [x] `TestModalBackedModels::test_reads_the_provider_at_call_time`: flipping `app_config.models.llm.provider` between two calls changes the result (no import-time freeze).

Seams — `apps/memory/tests/unit/memory/test_pipeline.py`:
- [x] `test_worker_seam_builds_nothing_for_sentence_transformers`: worker run in `rag` AND in `graphrag` with `search_embedding.provider = "sentence-transformers"` (factories patched as tripwires, run stopped at the first stage like `_run_worker_for_prewarm`) -> zero factory calls before the first stage; `prewarm_models` awaited with no models.
- [x] `test_worker_seam_builds_the_modal_blocks_only`: `llm = modal`, `search_embedding = modal`, `resolution_embedding = voyage`, `graphrag` -> `prewarm_models` receives exactly the two Modal instances; in `rag` with the same config -> the search instance only (the existing `test_rag_warms_the_search_embedding_model_only` intent, re-expressed under `provider: modal`).
- [x] `test_worker_prewarm_is_a_noop_on_default_providers` stays green and additionally asserts zero factory calls at the seam.
- [x] `test_summary_seam_builds_the_llm_only_under_modal`: `llm.provider = "gemini"` -> `get_llm` not called by the seam, summaries proceed; `= "modal"` -> built once, pre-warmed once before the fan-out (`test_the_llm_is_prewarmed_once_before_the_fan_out` re-expressed), a `ModelError` from the warm still yields ONE warning + fallback labels, and a construction error (missing **Proxy token**) still propagates.
- [x] `tests/unit/memory/graph/consolidation/test_dream.py` is untouched and green.

Static guards — `apps/memory/tests/unit/deploy/test_modal_deploy_scripts.py`, every test parametrised over BOTH scripts (`_ENGINES`), every mutant built with `_mutate(...)` into `tmp_path`:
- [x] `TestGlueContract::test_no_process_is_spawned`: `_process_spawns(_module(engine)) == []`; and the control row `test_the_shipped_script_passes_every_guard` asserts it too.
- [x] `TestTheGuardsCatchTheirMutants::test_a_spawned_process_fails`, parametrised over these statements inserted before `_STOP_CALL`: `import subprocess; subprocess.run(["env"])`, `import subprocess as sp; sp.check_output("env")`, `from subprocess import run; run(["env"])`, `os.system("env")`, `os.popen("env").read()`, `os.execvp("env", ["env"])`, `os.spawnlp(os.P_WAIT, "env", "env")`, `from os import system; system("env")`, `import pty; pty.spawn("env")`, `import commands` -> `_process_spawns(mutant)` is non-empty and names the offender, for all 10 statements x 2 scripts.
- [x] `TestTheGuardsCatchTheirMutants::test_any_spelling_of_a_logging_call_fails`, parametrised over: `logging.getLogger().info("spec is %s", SPEC)`, `logging.info("spec is %s", SPEC)`, `logging.getLogger(__name__).warning("spec is %s", SPEC)`, `log = logging.getLogger("x"); log.error("spec is %s", SPEC)`, `logger.log(logging.INFO, "spec is %s", SPEC)` -> `_log_offenders(mutant, engine)` is non-empty for all 5 x 2; and an allow-listed message sent through `logging.getLogger().info(<allow-listed literal>, <safe arg>)` is NOT an offender (the rule is the allow-list, not the spelling).
- [x] `test_an_unreviewed_log_message_fails` and `test_a_logged_environment_fails` stay green with their exact expected strings.
- [x] No test imports, executes or edits a file under `apps/memory/deploy/`; `git diff --stat -- apps/memory/deploy apps/memory/scripts Makefile` is empty.

Cosmetics:
- [x] `grep -rn "EMBEDDING_SERVER_NAME" apps/ docs/` -> 0 hits; `grep -rn "MODAL_SERVER_NAME" apps/memory/src | wc -l` >= 9; `MODAL_SERVER_NAME == "Server"` is still asserted in `test_modal_catalog.py`.
- [x] `test_a_non_catalog_model_fails_before_any_gpu_wakes` takes `self` only; no line of `get_model.py`'s module NOTE exceeds 88 columns.
- [x] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green (LOCAL env).

## User Stories

### Story: Operator on the `sentence-transformers` provider re-runs a fully cached ingest
1. `search_embedding.provider: sentence-transformers`; every document of the run is already in the 90-day embed cache.
2. Before: the worker's pre-warm line loads the torch model (seconds, hundreds of MB) and throws it away. After: the seam reads `provider != modal`, builds nothing, and the run goes straight to the cached stages.

### Story: Operator on Modal for the LLM only
1. `llm: {provider: modal, model: LiquidAI/LFM2.5-350M}`, embeddings on Voyage; `graphrag` run over 12 documents.
2. The log shows `Pre-warming 1 Modal server(s): ep-tree-lfm2-5-350m` once before document 1; no Voyage client is built by the seam.

### Story: The next engineer writes a test double with a sync `ensure_warm`
1. They pass `(real_modal_embedding, MagicMock(ensure_warm=lambda: None))` to `prewarm_models`.
2. They get `TypeError: a coroutine was expected, got None` at once, and the real model's 600 s poll is cancelled and reaped inside the call — no "Task was destroyed but it is pending" later in the suite.

### Story: A debugging `os.system("env")` sneaks into a deploy script
1. Someone adds it inside `@modal.enter` of `modal_sglang_llm.py` to see what the container sees.
2. `make memory-tests` fails `test_no_process_is_spawned[sglang]` naming `os.system('env')` — before the real `HF_TOKEN` ever reaches `modal app logs`.

### Story: A second logger appears in a deploy script
1. Someone adds `logging.getLogger().info("spec is %s", SPEC)`.
2. `test_only_allow_listed_lines_are_logged` fails and prints that call: the message is not on the allow-list, however the logger was spelled.

### Story: The next engineer reads `modal_llm.py`
1. They see `modal.Server.from_name(app_name, MODAL_SERVER_NAME)` and no longer wonder why an LLM client resolves an "embedding" server.

---

Blocked by: (none)

## Log

### [PA] 2026-09-20 19:30 — Grooming (new task, split out of the "routed to #150" follow-ups)

**Summary**
Four Tester findings + three cosmetics: build the pre-warm task list inside the `try`; construct a model for the pre-warm only when its block's provider is `modal`; ban process spawning in the deploy scripts; apply the log allow-list to any logging call.

**Key decisions**
- Pointed here by commit d3a9fe2 (#149 Tester "Other issues found" 1 and 2: `get_model.py:229`, `pipeline.py:2029-2033`) and by #147's Tester log (guard gap `subprocess`/`os.system`; `_log_offenders` only matches `logger.`). All were "routed to #150", whose body never carried them.
- ONE task, not two: same shape and precedent as #150 (several small QA follow-ups of one feature, none with a design decision, each a few lines + its regression test); kept apart from #151 because a Prefect cache-key change (one cold billable cache) must be revertable on its own.
- Least mechanism: `provider == "modal"` check before the factory (no capability registry); receiver-agnostic emit-method match (no alias data-flow); incremental list inside the existing `try` (no pre-validation).
- Rename `EMBEDDING_SERVER_NAME` -> `MODAL_SERVER_NAME` INCLUDED: value unchanged, 15 hits / 6 files, 0 in docs and deploy scripts.
- `dream.py` seam deliberately unchanged (its instances are used, not thrown away).

**Dependencies**
- None (#147-#149 are done).

**User stories**
- 6 stories: cached sentence-transformers run, LLM-only Modal, sync test double, `os.system("env")`, second logger, the neutral constant name.

Ready for implementation.

### [SWE] 2026-09-20 21:15 — Implementation

**Files modified**
- `apps/memory/src/tree/models/get_model.py` — item 1: the pre-warm task list is built incrementally INSIDE the `try`; item 2: new `modal_backed_models(*blocks)` gate; cosmetic: module NOTE re-wrapped (longest line now 82 cols).
- `apps/memory/src/tree/memory/pipeline.py` — both seams go through `modal_backed_models` (`rag` -> `search_embedding`; else the three blocks; cluster summaries -> `llm`, construction still OUTSIDE the `try`).
- `apps/memory/src/tree/models/{modal_catalog,modal_server,modal_embedding,modal_llm}.py` — `EMBEDDING_SERVER_NAME` -> `MODAL_SERVER_NAME` (value unchanged, `"Server"`).
- `apps/memory/tests/unit/conftest.py` — new `modal_seam` fixture: per-block providers + the two factory TRIPWIRES (`tree.models.get_model.get_llm` / `_build_embedding_model`), shared by the helper's tests and the pipeline seam's (one copy, not two).
- `apps/memory/tests/unit/models/test_prewarm.py` — `test_a_sync_ensure_warm_does_not_leak_the_sibling` + `TestModalBackedModels` (4 tests, one parametrised x4).
- `apps/memory/tests/unit/memory/test_pipeline.py` — worker seam: `test_worker_seam_builds_nothing_for_sentence_transformers` (x2 modes), `test_worker_seam_builds_the_modal_blocks_only` (x2 modes, absorbs `test_rag_warms_the_search_embedding_model_only`); summary seam: `test_summary_seam_builds_the_llm_only_under_modal` (x2 providers) and `test_a_missing_proxy_token_fails_the_run`; the neighbouring tests re-expressed under `provider: modal` so they stay non-vacuous.
- `apps/memory/tests/unit/deploy/test_modal_deploy_scripts.py` — new `_process_spawns`, widened `_log_offenders`, `TestGlueContract::test_no_process_is_spawned`, the control row, and 3 new mutant rows (x2 scripts); module docstring updated to six guards.
- `apps/memory/tests/unit/models/test_get_model.py` — cosmetic: dropped the unused `mocker`.

**Tests**
- Unit: 3872 passing, 0 failing (`make memory-tests`, LOCAL env) — 3789 before, +83.
- Integration: N/A — the repo has no integration suite (AGENTS.md); no infra touched.

**Acceptance criteria**
- [x] `test_a_sync_ensure_warm_does_not_leak_the_sibling` — seen RED on the code at d3a9fe2: `AssertionError: Extra items in the left set: <Task pending name='Task-59' coro=<_SleepingFake.ensure_warm() ... test_prewarm.py:55>>`, i.e. the sibling's 600 s poll survived the `TypeError`. Green after moving the list inside the `try`. The fake-state clause is written `not sleeping.entered.is_set() or sleeping.cancelled is True` — with the fix there is no await between the two `create_task` calls, so the poll is cancelled at its entry point and never runs its body (the AC's "or never started"). `asyncio.all_tasks() == before` is the load-bearing assertion; no "Task was destroyed but it is pending" warning remains.
- [x] `test_first_failure_cancels_the_sibling`, `test_warms_concurrently`, `test_dedupes_on_warm_key`, `test_is_idempotent`, `test_noop_without_ensure_warm` — unmodified, green.
- [x] `TestModalBackedModels::test_a_non_modal_provider_never_calls_its_factory` (x4 providers), `::test_a_modal_block_is_built_once`, `::test_reads_the_provider_at_call_time`. The AC's second clause of `test_a_modal_block_is_built_once` ("all three `modal` -> three elements in the order asked") lives in the sibling `::test_every_modal_block_is_built_in_the_order_asked` — one behaviour per test (AAA), same coverage.
- [x] `test_worker_seam_builds_nothing_for_sentence_transformers` (`rag` and `graphrag`) — zero factory calls, `prewarm_models` awaited with `()`.
- [x] `test_worker_seam_builds_the_modal_blocks_only` — `graphrag` -> `("LLM", "SEARCH-EMBEDDING")`, `rag` -> `("SEARCH-EMBEDDING",)`, one embedding-factory call.
- [x] `test_worker_prewarm_is_a_noop_on_default_providers` — green, and now asserts `warmed == [()]` plus zero factory calls at the seam.
- [x] `test_summary_seam_builds_the_llm_only_under_modal` — `gemini` -> no `get_llm`, summaries still run; `modal` -> built once. The other clauses of that bullet are the siblings in the same class: `test_the_llm_is_prewarmed_once_before_the_fan_out` (re-expressed under `provider: modal`, order `["prewarm", "summarise", "summarise"]`), `test_cluster_summaries_fail_open_when_the_llm_is_dead` (ONE warning + fallback labels), `test_a_missing_proxy_token_fails_the_run` (construction error propagates, `prewarm_models` never called).
- [x] `tests/unit/memory/graph/consolidation/test_dream.py` untouched (`git diff --stat` empty) and green. `dream.py` is deliberately unchanged: it pre-warms the SAME `llm` / `embedding_model` instances the sweep then uses, so nothing is built "just to find out" — gating it would only make the sweep rebuild them.
- [x] `TestGlueContract::test_no_process_is_spawned` + the control row (`test_the_shipped_script_passes_every_guard` now asserts `_process_spawns` too).
- [x] `TestTheGuardsCatchTheirMutants::test_a_spawned_process_fails` — 10 statements x 2 scripts, each naming its offender; plus `test_an_imported_module_name_alone_fails` for `__import__("subprocess")`.
- [x] `test_any_spelling_of_a_logging_call_fails` — 5 spellings x 2 scripts; `test_an_allow_listed_message_passes_whatever_the_spelling` proves the rule is the allow-list, not the receiver (`logging.getLogger().info(<allow-listed>, <token boolean>)` -> no offender). `logging.basicConfig` / `logging.getLogger` are not emit methods and needed NO exemption — the control row is green on both shipped scripts with no dotted name exempted.
- [x] NO existing guard weakened: the widened `_log_offenders` is a SUPERSET, not a swap — the old `logger.*` prefix is kept as a floor beside the emit-method set, because a non-emit method on the module logger still emits (`logger.handle(record)` hands a `LogRecord` straight to the handlers). New row `test_a_record_pushed_past_the_allow_list_fails` x 2 scripts; with the floor removed it goes red on both, with it green, and `test_an_unreviewed_log_message_fails` still gets exactly one entry (one `if`, no duplicate).
- [x] `test_an_unreviewed_log_message_fails` and `test_a_logged_environment_fails` — unchanged expected strings, green.
- [x] No test imports, executes or edits anything under `apps/memory/deploy/`; `git diff --stat -- apps/memory/deploy apps/memory/scripts Makefile` is EMPTY. No `modal` / `make memory-deploy-*` command was run.
- [x] `grep -rn "EMBEDDING_SERVER_NAME" apps/ docs/` -> 0; `grep -rn "MODAL_SERVER_NAME" apps/memory/src | wc -l` -> 10; `MODAL_SERVER_NAME == "Server"` still asserted twice in `test_modal_catalog.py`.
- [x] `test_a_non_catalog_model_fails_before_any_gpu_wakes(self)`; no line of `get_model.py`'s module NOTE exceeds 88 columns (max 82).
- [x] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green (LOCAL env, one command at a time).

**Evidence**
```
$ PYTEST_ADDOPTS="tests/unit/models/test_prewarm.py -q -k sync_ensure_warm" make memory-tests   # BEFORE the fix
E       AssertionError: assert {<Task pendin...py:503]>, ...} == {<Task pendin...k_wakeup()]>>}
E         Extra items in the left set:
E         <Task pending name='Task-59' coro=<_SleepingFake.ensure_warm() running at .../test_prewarm.py:55>>
1 failed, 8 deselected in 0.91s

$ PYTEST_ADDOPTS="tests/unit/deploy/test_modal_deploy_scripts.py -q" make memory-tests          # _process_spawns stubbed to [], _log_offenders not yet widened
30 failed, 78 passed in 0.95s        # 22 spawn rows + 8 logger-spelling rows, both scripts

$ make memory-format-check && make memory-lint-check
312 files already formatted
All checks passed!

$ make pre-commit
prettier / ruff check / ruff format / biome check (harness) ......... Passed

$ PYTEST_ADDOPTS="tests/unit/deploy/... -q -k record_pushed" make memory-tests   # floor clause removed from _log_offenders
FAILED ...::test_a_record_pushed_past_the_allow_list_fails[sglang]
FAILED ...::test_a_record_pushed_past_the_allow_list_fails[vllm]
2 failed, 108 deselected in 0.63s

$ make memory-tests
============================ 3872 passed in 58.77s =============================
```

**Notes**
- `test_an_allow_listed_message_passes_whatever_the_spelling` is a NEGATIVE control (against over-strictness) and was already green before the widening — it is not in the 30-red count above; its red counterpart is `test_any_spelling_of_a_logging_call_fails[root-getLogger]`.
- Non-vacuity, proved by mutation (each mutation reverted immediately, nothing left behind): (a) relaxing the gate to `provider != "__never__"` fails 6 of the 7 `TestModalBackedModels` rows; (b) restoring the PRE-fix seams (`get_search_embedding_model()` / `get_llm()` called unconditionally) fails 7 seam tests, including both `sentence-transformers` rows and both `test_summary_seam_builds_the_llm_only_under_modal` rows; (c) the guard rows were written against a `_process_spawns` stub and the un-widened `_log_offenders` and went 30-red -> green.
- ONE fix outside the task's five items, forced by item 2 and worth the Tester's eye: `TestFlowEmbeddingModelSplit`'s `stubbed_graph_stages` fixture only patched `tree.memory.pipeline.get_llm`. Since the seam now resolves its factory in `tree.models.get_model`, `test_worker_passes_the_llm_identity_to_extraction` (which flips `models.llm` to `provider: modal`) built a REAL `ModalLLM` and the real pre-warm called `modal.Server.from_name` — a live Modal lookup from a unit test. The fixture now patches BOTH names with the same `MagicMock(spec=BaseLLM)`; comment explains why. This is the general hazard the `modal_seam` fixture exists to remove: flipping a provider to `modal` without patching the `get_model`-level factory reaches Modal.
- `prewarm_models` keeps its name, signature and duck typing (`prewarm_models()` with zero models is still a silent no-op, which is what every non-Modal run now hits). No `inspect.iscoroutinefunction` pre-check was added — the `finally` covers it.
- Lazy imports intact: nothing on the default boot path imports `modal` / `modal_embedding` / `modal_llm` — `test_prewarm_helper_does_not_import_modal` (fresh interpreter) and the MCP tool-gating probes are green in the full run.
- NOT RUN (by the task's HARD SAFETY RULE, reserved for #141): any `modal …` command, `make memory-deploy-*`, `scripts/modal_*.py` as a process, and any import/execution of `apps/memory/deploy/*`. The deploy scripts were only READ; every mutant is a copy in `tmp_path`.
- Uncommitted, as instructed — the Tester goes first.

### [Tester] 2026-09-20 22:05 — QA

**Commands run (exact, one at a time, LOCAL env, `make env-status` = local throughout)**
```
git status --short ; git diff --stat ; git diff --stat -- apps/memory/deploy apps/memory/scripts Makefile
shasum apps/memory/tests/unit/conftest.py                                      # 8156809330a1ab2e2f7247d8a3b571d712746cc5, BEFORE any edit
# Temporary tripwire experiment (HAZARD verification) — Edit conftest.py to add an
# autouse fixture patching modal.Server.from_name / modal.Function.from_name /
# modal.App.lookup to raise AssertionError("live Modal lookup from a unit test")
make memory-tests                                                              # 3872 passed in 387.10s, exit 0 — WITH the tripwire active
# Edit conftest.py to remove the tripwire fixture (targeted revert)
shasum apps/memory/tests/unit/conftest.py                                      # 8156809330a1ab2e2f7247d8a3b571d712746cc5, identical to BEFORE
git status --short                                                             # exactly the SWE's 13 files
make memory-format-check                                                       # 312 files already formatted
make memory-lint-check                                                         # All checks passed!
make pre-commit                                                                # prettier / ruff check / ruff format / biome check ... Passed
make memory-tests                                                              # 3872 passed in 455.94s, exit 0 — clean tree, post-revert (canonical evidence)
grep -rn "EMBEDDING_SERVER_NAME" apps/ docs/                                   # 0 hits
grep -rn "MODAL_SERVER_NAME" apps/memory/src | wc -l                           # 10
grep -rln "EMBEDDING_SERVER_NAME" . --exclude-dir=.git --exclude-dir=.venv --exclude-dir=node_modules   # 7 files, all tasks/*.md (done/152 logs only)
grep -n "MODAL_SERVER_NAME" apps/memory/tests/unit/models/test_modal_catalog.py                          # 2 hits incl. `== MODAL_SERVER_NAME == "Server"` (x2)
awk 'NR>=13 && NR<=20 {print length($0)}' apps/memory/src/tree/models/get_model.py                       # max 82
grep -n "def test_a_non_catalog_model_fails_before_any_gpu_wakes" apps/memory/tests/unit/models/test_get_model.py   # takes `self` only
uv run python -c "import sys, tree.models.get_model; print([m for m in ('modal','tree.models.modal_embedding','tree.models.modal_llm','torch') if m in sys.modules])"   # []
uv run python -c "import sys, tree.memory.pipeline; print(...)"               # []  (same 4-name check)
uv run python -c "import sys, tree.mcp.server; print(...)"                    # []  (same 4-name check)
git diff -- apps/memory/src apps/memory/tests | grep -Eic "api[_-]?key|secret|token|bearer|password"     # 23 — all variable names / "Proxy token" / "HF_TOKEN" comments and error-message literals, no secret values
git diff --stat -- apps/memory/src/tree/memory/graph/consolidation/dream.py apps/memory/tests/unit/memory/graph/consolidation/test_dream.py   # empty, both
PYTEST_ADDOPTS="tests/unit/models/test_prewarm.py -q -W error::RuntimeWarning:asyncio -W error::pytest.PytestUnraisableExceptionWarning" make memory-tests   # 16 passed, exit 0
uv run python <scratch C1 asyncio script, session scratchpad, not committed — see below>
uv run python <scratch C2 script: modal_backed_models("not_a_block"); sentence-transformers seam sys.modules check>
TREE_MODELS__LLM__PROVIDER=modal uv run python <scratch: app_config.models.llm.provider>   # "modal" — env override is live at call time in a fresh process
uv run python <scratch D script: 15 process-spawn / log-emission mutants against copies of _process_spawns / _log_offenders / _environ_reads, ast.parse only, no deploy-script file touched>
```

**Test summary**
- Format / lint / pre-commit: PASS
- Unit tests: 3872 passed / 0 failed (both the tripwire run and the clean post-revert run agree with each other and with the SWE's claimed count, +83 over #149's 3789)
- Warnings: 0 new (pre-existing, unrelated opik/pydantic `UserWarning` on every collection, confirmed by #149's Tester as untouched by this diff)

**HAZARD verification (top priority) — live Modal lookup from a unit test**
The specific hazard the SWE hit and fixed (`test_worker_passes_the_llm_identity_to_extraction` building a real `ModalLLM` because only `tree.memory.pipeline.get_llm` was patched) is gone: `stubbed_graph_stages` (`test_pipeline.py:2143`) now patches BOTH `tree.memory.pipeline.get_llm` and `tree.models.get_model.get_llm`, with a comment explaining why. Verified read.

Ran the prescribed experiment: added a TEMPORARY autouse fixture to `apps/memory/tests/unit/conftest.py` that patches `modal.Server.from_name`, `modal.Function.from_name` and `modal.App.lookup` to raise `AssertionError("live Modal lookup from a unit test")`, then ran `make memory-tests` ONCE (3872 passed, exit 0 — zero tests tripped the tripwire), then reverted the edit with a targeted `Edit` and proved byte-identical via `shasum` (`8156809330a1ab2e2f7247d8a3b571d712746cc5` before and after). `git status --short` after the revert shows exactly the SWE's 13 modified files — no scratch residue left in the repo.

**Judgment: should this tripwire be PERMANENT? YES — this is a FAIL-level gap, not a note.** `apps/memory/tests/unit/conftest.py:89`'s `_modal_dry_run` autouse fixture only sets `TREE_MODAL_DRY_RUN`, which guards the `modal_cli.subprocess.run` door used by the deploy CLI/router (`tree.models.modal_cli`, `tree.models.modal_router`). It does **not** guard the Modal **SDK** client-resolution path used by `ModalLLM`/`ModalEmbeddingModel` via `tree.models.modal_server:147` (`modal.Server.from_name(...)`). No other autouse fixture anywhere in `apps/memory/tests` covers this second, distinct door — confirmed by `grep -rln "modal" apps/memory/tests/*/conftest.py` (only `tests/unit/conftest.py`, and its only modal-related autouse fixture is the CLI dry-run one). Today the only defense is convention (every test that flips a provider to `modal` must remember to patch `tree.models.get_model.get_llm` / `_build_embedding_model`, or use the new `modal_seam` fixture) — exactly the convention the SWE's own fixture proves engineers forget. This mirrors #143's `TREE_MODAL_DRY_RUN` rail precedent (a suite-wide safety rail added after a live-CLI incident) and this diff explicitly documents having hit the SDK-side sibling of that same incident, but only patched the one fixture that tripped it rather than closing the door for every future test.
- **Fix, proven costless:** add a permanent autouse fixture (same shape as `_modal_dry_run`) to `apps/memory/tests/unit/conftest.py` that patches `modal.Server.from_name` / `modal.Function.from_name` / `modal.App.lookup` to raise unless a test opts out via an explicit marker/fixture (e.g. a `modal_sdk_allowed` fixture used nowhere today, or an `@pytest.mark.modal_sdk_ok` a future live-fixture-diagnostics test could use). This review already ran the exact proposed fixture against the whole suite: **3872 passed, exit 0, zero tests needed an opt-out** — the fix is a net-zero-risk addition, not a hypothetical one.

**Other guard-gap findings (D — adversarial mutants against `_process_spawns` / `_log_offenders` / `_environ_reads`, ast.parse only, no deploy script touched)**
`_process_spawns`'s docstring claims "every way `module` could start a CHILD PROCESS, as written." Confirmed it catches all 10 statements in AC bullet 110's own list (subprocess import/alias/from-import, `os.system`/`popen`/`exec*`/`spawn*`, `from os import system`, `pty`, `commands`) plus `__import__("subprocess")` — matches the AC's enumerated scope exactly. Beyond that enumerated scope, on scratch `ast.parse` snippets (not the shipped scripts):
- `__import__("os").system("env")` → `_process_spawns` returns `[]`. This is a ONE-TOKEN rewrite of the exact vulnerability item 3 exists to close (`os.system("env")` dumping the real `HF_TOKEN` to inherited stdout / `modal app logs`) — `os.environ`, `print`/`stdout.write` and every named `os.*` dotted call are all absent, so `_console_writes`/`_environ_reads`/`_log_offenders` stay silent too. `_dotted()` only resolves a chain rooted at an `ast.Name`; `__import__("os")` is a `Call`, so `.system` renders as bare `"system"`, which never matches the `"os."` prefix check.
- `importlib.import_module("subprocess")`, `asyncio.create_subprocess_exec`/`_shell`, `multiprocessing.Process` → all `[]`. Not in the AC's enumerated statement list.
- `getattr(logger, "info")("unreviewed message")` → `_log_offenders` returns `[]` (same root cause: `node.func` must be an `ast.Attribute`, and `getattr(...)` is a `Call`).
- `os.environ["HF_TOKEN"]` (direct VALUE read, not the boolean) and `raise RuntimeError(os.environ["HF_TOKEN"])` → both **correctly caught** by `_environ_reads` (any `os.environ` node not one of the three approved shapes is an offender) — confirms the task prompt's open question ("it should be caught") with evidence.
- `shutil.which` → correctly `[]` (spawns nothing, not a false negative).
- `warnings.warn(str(os.environ))` → incidentally caught by `_log_offenders` (its `attr` "warn" coincides with the receiver-agnostic emit-method set) — a safe-direction over-inclusion, not a gap.
- `socket`/`urllib` exfiltration, `eval`/`exec`/`compile` of a spawn string → out of scope for a process-spawn guard by construction (different attack class / general static-analysis limit); noted, not blocking on their own.
- **Verdict on this cluster: FAIL, but narrowly.** The `__import__("os").system(...)` and `getattr(logger, "info")(...)` cases are the two that matter: they are trivial rewrites of the *exact* leak this task's items 3 and 4 were written to close, they are not covered by the AC's enumerated mutant list (so nothing here contradicts an AC), and the guard docstrings ("every way", "receiver-agnostic... whatever it is called on") overclaim relative to what the code checks — which is precisely this task's own stated theme ("the guarantee the docstring already claims is not enforced"). Two acceptable resolutions, either is sufficient: (a) widen `_process_spawns`'s `ast.Attribute` branch to also flag `node.value` being a `Call` to `__import__` with a string-constant argument followed by an os-spawn attribute (and the analogous widening in `_log_offenders` for a `Call`-typed `node.func`, e.g. `getattr(x, "info")(...)`) — a few lines plus one mutant row each; or (b) narrow both docstrings to name the bound explicitly (the file already does this well for alias resolution in `_log_offenders`'s comment) and add a one-line "known bound, not closed" note. Not required to close before commit if the SWE picks (b), but must be a deliberate choice, not silence.

**C1 — `prewarm_models` further asyncio adversarial pass** (in-process scratch script, no Modal/network)
- Sync `ensure_warm` on the FIRST model (nothing scheduled yet, second model a real sleeping poll) → `TypeError: a coroutine was expected, got None`, zero leaked tasks — PASS.
- `ensure_warm()` call itself raises synchronously (not merely a bad return) → the exception propagates, zero leaked tasks, sibling never entered/cancelled (nothing was scheduled before the raise) — PASS.
- `ensure_warm` async but resolves immediately (already-finished once awaited) → zero leaked tasks — PASS.
- Caller cancels the outer `prewarm_models()` call mid-`gather` → `CancelledError` propagates, zero leaked tasks after — PASS.
- `PYTEST_ADDOPTS="tests/unit/models/test_prewarm.py -q -W error::RuntimeWarning:asyncio -W error::pytest.PytestUnraisableExceptionWarning" make memory-tests` → 16 passed, no unraisable-exception / asyncio RuntimeWarning escaped (a plain `-W error` collection-errors on an unrelated Python-3.14 `google-genai` stdlib deprecation warning, unrelated to this diff — used the narrower filter instead). `gc.collect()` after each scratch scenario produced no "Task was destroyed but it is pending" message. PASS on all 4 extra scenarios plus the file-level warning check.

**C2 — `modal_backed_models` further adversarial pass**
- Unknown block name (`modal_backed_models("not_a_block")`) → `AttributeError: 'ModelsConfig' object has no attribute 'not_a_block'` — clear, names the exact bad attribute — PASS.
- Provider casing (`"Modal"` vs `"modal"`): `models.*.provider` is a plain `str` (not a `Literal`/enum) in `app_config.py`, so `"Modal"` fails the `== "modal"` gate silently (pre-warm skips it) — but the same exact-match is used by `get_llm`/`_build_embedding_model` themselves, so the actual task's factory call still raises `ValueError: Unknown LLM provider: Modal` immediately afterward. Pre-existing case-sensitivity, not introduced or worsened by this task, and explicitly out of scope ("no capability registry, no new config key") — PASS with note, not blocking.
- `TREE_MODELS__LLM__PROVIDER=modal` env override in a fresh process → `app_config.models.llm.provider == "modal"` — confirmed live at call time — PASS.
- Same block asked twice (`modal_backed_models("llm", "llm")`) → built twice, factory called twice, two distinct objects (dedup is `prewarm_models`'s job via `warm_key`, not this helper's) — matches spec — PASS.
- Shipped default config (gemini/voyage/voyage): `modal_backed_models("search_embedding", "resolution_embedding", "llm")` → `[]`, zero factory calls, `torch` and `sentence_transformers` absent from `sys.modules` before and after — PASS.

**Acceptance criteria** (all 18 verified true; see commands above for evidence per line)
- [x] PASS — `test_a_sync_ensure_warm_does_not_leak_the_sibling` — code read at `get_model.py:294-320`: task list built incrementally inside the `try`, `finally` cancels + reaps unconditionally.
- [x] PASS — `test_first_failure_cancels_the_sibling`, `test_warms_concurrently`, `test_dedupes_on_warm_key`, `test_is_idempotent`, `test_noop_without_ensure_warm` — unmodified in the diff, green in the 3872-pass run.
- [x] PASS — `TestModalBackedModels::test_a_non_modal_provider_never_calls_its_factory` / `::test_a_modal_block_is_built_once` / `::test_reads_the_provider_at_call_time` — read + green; `::test_every_modal_block_is_built_in_the_order_asked` is the sibling covering the AC's "three elements in the order asked" clause — confirmed equivalent, non-vacuous coverage (one behaviour per test).
- [x] PASS — `test_worker_seam_builds_nothing_for_sentence_transformers` (x2 modes) — read `test_pipeline.py:2808-2836`, green.
- [x] PASS — `test_worker_seam_builds_the_modal_blocks_only` — read `test_pipeline.py:2838-2876`, green; `rag` warms search only, `graphrag` warms LLM + search.
- [x] PASS — `test_worker_prewarm_is_a_noop_on_default_providers` — read `test_pipeline.py:2938-2975`, asserts zero factory calls at the seam, green.
- [x] PASS — `test_summary_seam_builds_the_llm_only_under_modal` (+ siblings `test_the_llm_is_prewarmed_once_before_the_fan_out`, `test_a_missing_proxy_token_fails_the_run`, `test_cluster_summaries_fail_open_when_the_llm_is_dead`) — read `test_pipeline.py:3632-3752`, construction still outside the `try` in `pipeline.py:2624-2632`, green.
- [x] PASS — `tests/unit/memory/graph/consolidation/test_dream.py` untouched — `git diff --stat` empty for both the source and the test file.
- [x] PASS — `TestGlueContract::test_no_process_is_spawned` + control row — green; `_process_spawns` implementation read at `test_modal_deploy_scripts.py:449-479`.
- [x] PASS — `TestTheGuardsCatchTheirMutants::test_a_spawned_process_fails` (10 x 2) + `test_an_imported_module_name_alone_fails` — green.
- [x] PASS — `test_any_spelling_of_a_logging_call_fails` (5 x 2), `test_a_record_pushed_past_the_allow_list_fails`, `test_an_allow_listed_message_passes_whatever_the_spelling` — green; widened `_log_offenders` confirmed a superset via the floor-clause read.
- [x] PASS — `test_an_unreviewed_log_message_fails`, `test_a_logged_environment_fails` — unchanged expected strings, green.
- [x] PASS — no test imports/executes/edits `apps/memory/deploy/`; `git diff --stat -- apps/memory/deploy apps/memory/scripts Makefile` empty.
- [x] PASS — `grep -rn "EMBEDDING_SERVER_NAME" apps/ docs/` → 0; `grep -rn "MODAL_SERVER_NAME" apps/memory/src | wc -l` → 10; `== "Server"` still asserted (x2) in `test_modal_catalog.py`.
- [x] PASS — `test_a_non_catalog_model_fails_before_any_gpu_wakes(self)`; longest NOTE line 82 cols.
- [x] PASS — `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green, LOCAL env, one command at a time (ran twice: once under the temporary tripwire, once on the clean post-revert tree — both 3872 passed, exit 0).

**Other issues found**
1. **BLOCKING (see HAZARD verification above): no permanent autouse guard against a live Modal-SDK lookup (`modal.Server.from_name` / `modal.Function.from_name` / `modal.App.lookup`) from a unit test.** Only the CLI-subprocess door (`TREE_MODAL_DRY_RUN`) has a suite-wide rail; the SDK door relies on every test author remembering to patch `get_model`-level factories (or use the new `modal_seam` fixture). Proven costless: the proposed permanent fixture ran clean against the whole 3872-test suite.
2. **BLOCKING (see D findings above): `_process_spawns` / `_log_offenders` docstrings overclaim relative to what the AST walk checks.** `__import__("os").system("env")` and `getattr(logger, "info")(unreviewed)` are trivial rewrites of the exact vulnerabilities items 3/4 close, and pass every guard silently. Fix by widening the two functions a few lines each, or by narrowing the docstrings to name the bound explicitly (as already done for alias resolution) — SWE's choice, but not silent.
3. Not blocking: provider-casing (`"Modal"`) is a pre-existing, unaddressed-by-scope case-sensitivity quirk shared by every provider dispatch in `get_model.py`, not introduced by this task.
4. Not blocking: `socket`/`urllib` exfiltration and `eval`/`exec`/`compile`-of-a-string spawns are outside `_process_spawns`'s attack class by construction; flagged for awareness only.

**VERDICT: FAIL**

Two concrete, actionable fixes required before re-review (either close or explicitly, deliberately bound each):
1. Add a permanent autouse fixture to `apps/memory/tests/unit/conftest.py` patching `modal.Server.from_name` / `modal.Function.from_name` / `modal.App.lookup` to raise unless a test explicitly opts out — this review already validated it costs 0 test failures against the full suite.
2. Widen `_process_spawns` (catch `__import__(<module>).spawn_attr(...)`) and `_log_offenders` (catch `Call`-typed `.func`, e.g. `getattr(logger, "info")(...)`) to close the two demonstrated one-line bypasses of items 3/4's own stated guarantee — or narrow both docstrings to explicitly name this as an accepted bound, matching the existing alias-resolution precedent in the same file.

Everything else in this task — all 18 literal acceptance criteria, the four numbered Tester findings from #147/#149, the three cosmetics, the full regression suite, import hygiene, and the specific hazard this task's Log already names — is verified PASS with evidence above.

### [SWE] 2026-09-21 — Fixes (QA round 1)

**What this entry is.** A PRIOR SWE session started the fixes for the Tester's two blocking findings and ended abruptly before logging or reporting. Its edits were intact in the worktree. I did NOT start over: I reviewed what was there against the FAIL, completed the gaps, proved every new rule non-vacuous by mutation, and ran the full QA loop. Both findings are now CLOSED (not merely bounded).

**Reviewed and kept from the interrupted session**
- `apps/memory/tests/unit/conftest.py` — the permanent SDK rail: `MODAL_SDK_RAIL_MESSAGE` (`"live Modal SDK lookup from a unit test — patch resolve_server_url / use the modal_seam fixture"`), the marker `_tree_modal_sdk_rail` on the raiser, `_MODAL_SDK_DOORS`, the `modal_sdk_allowed` opt-out fixture and the autouse `_no_live_modal_sdk`.
- `apps/memory/tests/unit/test_modal_sdk_rail.py` (new file) — the rail fires, a test-level patch wins, the opt-out lifts it.
- `apps/memory/tests/unit/deploy/test_modal_deploy_scripts.py` — `_process_spawns` widened to dynamic imports + `eval`/`exec`/`compile` + `create_subprocess*` + `multiprocessing`, `_log_offenders` widened to a `Call`-typed `func` (`getattr(logger, "info")(…)`), KNOWN BOUNDS blocks on both, 18 spawn rows + 3 dynamic-dispatch rows x 2 scripts.
- `apps/memory/README.md` — the "TWO doors" safety paragraph in *Tests & QA*.

**What I added on top (this session)**
1. **`_install_modal_sdk_rail(mocker) -> list[str]`** extracted from the autouse fixture (`conftest.py`). The fixture keeps the opt-out early return, the helper owns the LAZY `import modal` + the patching, and now RETURNS the door names it closed — which makes the "no SDK installed" path testable without reaching into pytest internals.
2. **`test_the_rail_is_a_no_op_without_the_sdk`** — `mocker.patch.dict(sys.modules, {"modal": None})` (the in-repo idiom, cf. `tests/unit/memory/graph/resolution/test_composite.py:59`) -> the installer returns `[]` and patches nothing. `modal` is in the OPTIONAL `local-models` extra (`pyproject.toml:49-52`), so a box without it must get a no-op rail, never a collection error.
3. **`test_the_conftest_imports_the_sdk_lazily`** — fresh interpreter: `import sys, tests.unit.conftest; assert 'modal' not in sys.modules`. Same subprocess form as `test_prewarm_helper_does_not_import_modal`, and necessary because this very test file imports `modal` at module level (an in-process assertion would pass on a regression).
4. **`_DOORS` in the rail test now matches `_MODAL_SDK_DOORS`** — the interrupted file listed 3 doors under a comment claiming "same shape as `_MODAL_SDK_DOORS`", which has 4 (`Cls.from_name`). Since `_install_modal_sdk_rail` SKIPS an attribute the SDK does not have, an imaginary door would have been closed vacuously and silently. Verified against modal 1.5.5 by attribute inspection only (no lookup, no network): all four `(class, attr)` pairs exist. `test_every_sdk_door_carries_the_rail` now proves all four.
5. **`_process_spawns`: two more closures** (both sanctioned by the FAIL's option (a)) —
   - a dynamic primitive BOUND to a name (`imp = __import__; imp("os").system("env")` — the Tester's own aliasing example; `runner = eval; runner(…)`). A bare `ast.Name` in `_DYNAMIC_IMPORTS | _DYNAMIC_EXECUTION` that is not itself a callee is the offence, so no data-flow analysis is needed. The `called = {id(n.func) …}` set keeps a direct `__import__("os")` from being reported twice.
   - CALLING THE RESULT OF A CALL (`getattr(os, "system")("env")`, `g = getattr; g(os, "system")("env")`). `_log_offenders` already banned that shape, so the suite did catch it — but under the name `test_only_allow_listed_lines_are_logged`, which reads as a logging problem. A spawn must fail as a SPAWN.
6. **KNOWN BOUNDS updated to match the code** on both helpers: `_process_spawns` now names the remaining bound (a dynamic import bound through an ATTRIBUTE — `f = importlib.import_module`, `b = builtins; b.__import__(…)`) instead of the aliasing case I closed; `_log_offenders` names its own (`emit = logger.info; emit(…)`) as a deliberate, accepted bound — closing it is the data-flow walk the receiver-agnostic rule exists to avoid. `socket`/`urllib` exfiltration and `eval`-built strings stay named in both blocks.
7. **README**: one sentence added next to the DRY_RUN / PATH-shim paragraph (`README.md:653-656`, the deploy section, where the "a fake `modal` on `PATH` does NOT work" warning lives) pointing at the two autouse rails; the detailed TWO-doors paragraph stays in *Tests & QA* where the unit suite is described.

**Covered set of the SDK rail, justified** (`grep -rn "modal\.[A-Za-z_]" apps/memory/src`): the ONLY SDK lookup `apps/memory/src` performs is `modal.Server.from_name(entry.app_name, MODAL_SERVER_NAME)` at `tree/models/modal_server.py:147` (under the single `import modal` at `:24`); every other `modal.` hit is config (`app_config.models.modal.*` — checked file by file, incl. `modal_warmup.py:25,74` = `modal.warmup_deadline_s` in prose) or a docstring (`modal_catalog.py:107,462,472,475` = `modal.Secret` / `modal.Secret.from_dict`, described, never called from `src`). `Function.from_name` / `App.lookup` / `Cls.from_name` are the neighbouring lookup doors a future client would use, closed now so the rail is not re-litigated per client. NOT closed: `Volume.from_name` / `Secret.from_name|from_dict` / `Dict` / `Queue` — they return a LAZY handle (no lookup at the call) and appear only in `deploy/modal_*.py`, which the unit suite parses as text and never imports.

**Fresh-interpreter import probes: green, and structurally out of the rail's reach** — `test_prewarm.py::test_prewarm_helper_does_not_import_modal`, `test_get_model.py` (`'modal' not in sys.modules` after `import tree.models.get_model`), `test_modal_router.py:416`, `tests/unit/mcp/test_tool_gating.py`, `test_server_startup.py`, `test_clustering_dependencies.py` — 146 passed. They each run the probe in a SUBPROCESS, so an autouse fixture in the parent pytest process cannot affect them either way; the new `test_the_conftest_imports_the_sdk_lazily` is what actually pins the conftest's own import hygiene.

**Non-vacuity — every new rule seen RED by mutation, each mutation reverted and proved byte-identical**

`shasum apps/memory/tests/unit/conftest.py` = `5add1be3754f83ba640539a54b98e72685b56f11` before the five mutations and after the last revert (identical). Mutations were applied to the RAISER / MARKER / OPT-OUT / IMPORT GUARD only — never to the `mocker.patch.object` loop, because removing that loop would hand a test the REAL `modal.Server.from_name` (a live lookup, forbidden here):

| mutation | rows that went RED |
| --- | --- |
| raiser body `raise AssertionError(...)` -> `return None` | `test_a_live_lookup_raises_the_rails_assertion`, `test_the_client_path_fails_on_the_rail_not_on_the_network` (2 failed, 11 passed) |
| drop `setattr(_raise_live_modal_lookup, MODAL_SDK_RAIL_MARKER, True)` | `test_every_sdk_door_carries_the_rail[Server/Function/App/Cls]` (4 failed) |
| opt-out early `return` -> `pass` | `test_the_opt_out_hands_the_real_sdk_back[Server/Function/App/Cls]` (4 failed) |
| `try: import modal / except ImportError: return []` -> bare `import modal` | `test_the_rail_is_a_no_op_without_the_sdk` (1 failed) |
| add `import modal` at conftest module level | `test_the_conftest_imports_the_sdk_lazily` (1 failed) |

`shasum apps/memory/tests/unit/deploy/test_modal_deploy_scripts.py`, in order: `d3a03c518856c5ef593d2335b13c6b0abd911049` before the two guard mutations -> `d3a03c518856c5ef593d2335b13c6b0abd911049` after the last revert (byte-identical revert, proved) -> a different hash today, because `make memory-format-fix` ran AFTER the mutation round and re-wrapped one parametrise tuple plus a comment. A re-runner therefore gets the post-format hash, not `d3a03c5…`; the delta is formatter-only, which `make memory-format-check` (clean) and the unchanged test counts confirm:

| mutation | rows that went RED |
| --- | --- |
| binding rule disabled (`elif isinstance(node, ast.Name) and False`) | `test_a_spawned_process_fails[imp = __import__…]`, `[runner = eval…]` x sglang, vllm (4 failed, 42 passed) |
| `or isinstance(node.func, ast.Call)` removed from the Call branch | `test_a_spawned_process_fails[getattr(os, "system")("env")]`, `[g = getattr; g(os, "system")("env")]` x sglang, vllm (4 failed, 42 passed) |

**Evidence**
```
$ PYTEST_ADDOPTS="tests/unit/test_modal_sdk_rail.py -q" make memory-tests
13 passed in 1.02s                      # 9 before this session (+Cls marker, +Cls opt-out, +2 new tests)

$ PYTEST_ADDOPTS="tests/unit/deploy/test_modal_deploy_scripts.py -q" make memory-tests
142 passed in 0.85s                     # 134 before (+4 new mutant statements x 2 scripts)

$ make memory-format-fix                # 1 file reformatted, 312 left unchanged
$ make memory-lint-fix                  # All checks passed!
$ make memory-format-check              # 313 files already formatted
$ make memory-lint-check                # All checks passed!
$ make pre-commit                       # prettier / ruff check / ruff format / biome check (harness) ... Passed
$ make memory-tests
============================ 3917 passed in 58.40s =============================

$ git diff --stat -- apps/memory/deploy apps/memory/scripts Makefile
                                        # EMPTY — no shipped script or driver edited
```

**Notes**
- Test count: 3917, up from the 3905 the orchestrator verified on this tree (+12 = 4 rail rows + 8 guard rows). The old `3872` in the `[SWE] 2026-09-20 21:15` entry is the pre-QA-fix number.
- ACCEPTED CONSEQUENCE of the rail: the autouse fixture imports `modal` into the pytest process for every test, so `modal` is in `sys.modules` for the whole in-process suite. Every `'modal' not in sys.modules` assertion in this repo runs in a SUBPROCESS (checked: `test_prewarm.py:313`, `test_get_model.py:541`, `test_modal_router.py:416`, `test_tool_gating.py`), so none is weakened — but a FUTURE in-process lazy-import assertion would be, and should use the subprocess form like its neighbours.
- Rail bound, stated plainly: the rail no-ops without the `local-models` extra, but `tests/unit/test_modal_sdk_rail.py` itself imports `modal` at module level — as does `tree/models/modal_server.py:24`, so that file is uncollectable without the extra regardless. No `importorskip` was added; it would buy nothing.
- The `_log_offenders` `emit = logger.info` bound and the `f = importlib.import_module` bound are DELIBERATE, named in the KNOWN BOUNDS blocks, not silent.
- NOT RUN, by the HARD SAFETY RULE: any `modal …` / `uv run modal …` command (not even `--help`), `make memory-deploy-*`, `scripts/modal_*.py` as a process, any import/execution of `apps/memory/deploy/*`, any request to a `*.modal.run` / `*.modal.direct` URL. The SDK was only INSPECTED for attribute existence (`getattr(modal, "Cls")`), which performs no lookup. Every mutant is a copy in `tmp_path`. No `git checkout` / `restore` / `stash` / `clean` was run; every revert was a targeted `Edit` verified by `shasum`. The real pipelines were not run.
- Uncommitted, as instructed — back to the Tester.

### [Tester] 2026-09-21 — QA (round 2)

**Commands run (exact, one at a time, LOCAL env, `make env-status` = local throughout)**
```
git status --short ; git diff --stat ; git diff --stat -- apps/memory/deploy apps/memory/scripts Makefile
                                        # 14 modified + 1 untracked (test_modal_sdk_rail.py); deploy diff EMPTY

# --- (A) re-run the two round-1 blocking mutants against copies of BOTH scripts ---
uv run python <scratch: importlib.util.spec_from_file_location the test module,
  then _mutate(engine, tmp_path, _STOP_CALL, '__import__("os").system("env")')
  -> _process_spawns(mutant); and _mutate(..., 'getattr(logger, "info")("unreviewed message")')
  -> _log_offenders(mutant, engine); for engine in (sglang, vllm)>
  # [sglang] __import__("os").system("env")            -> ["__import__('os').system", "__import__('os')"]
  # [vllm]   __import__("os").system("env")            -> ["__import__('os').system", "__import__('os')"]
  # [sglang] getattr(logger,"info")("unreviewed msg")  -> ["getattr(logger, 'info')('unreviewed message')"]
  # [vllm]   getattr(logger,"info")("unreviewed msg")  -> ["getattr(logger, 'info')('unreviewed message')"]
  # both CAUGHT on both scripts — round-1 blocking findings CLOSED

# --- (B) the rail: no network before the AssertionError ---
uv run python -c "import tree.models.modal_server"    # read resolve_server_url:122-152 — modal.Server.from_name
                                                        # is a synchronous, un-awaited call BEFORE get_url.aio()
# temp test file apps/memory/tests/unit/test_scratch_socket_probe.py: patches socket.socket.connect
# to raise a DISTINCT _NetworkOpened error, then calls resolve_server_url(...) un-patched (rail active)
PYTEST_ADDOPTS="tests/unit/test_scratch_socket_probe.py -q" make memory-tests   # 1 passed — ModelError.__cause__
                                                        # is the rail's AssertionError, socket.connect never fired
rm apps/memory/tests/unit/test_scratch_socket_probe.py
git status --short                                     # back to 14 modified + 1 untracked — no residue

PYTEST_ADDOPTS="tests/unit/test_modal_sdk_rail.py -v" make memory-tests        # 13 passed (fires / test-patch
                                                        # wins / opt-out lifts it / no-op w/o SDK / lazy import /
                                                        # 4-of-4 doors)
grep -rln "modal" apps/memory/tests/**/conftest.py apps/memory/tests/conftest.py
                                                        # only tests/unit/conftest.py; the only session-scoped
                                                        # autouse fixture anywhere (_init_beanie) touches Mongo,
                                                        # not modal — no fixture can race the rail

# --- (C) shipped scripts, no deploy edit ---
git diff --stat -- apps/memory/deploy apps/memory/scripts Makefile             # EMPTY (already shown above)

# --- (D) non-vacuity: independently disable (i) the raiser, (ii) the binding rule ---
shasum apps/memory/tests/unit/conftest.py                                     # 5add1be3754f83ba640539a54b98e72685b56f11
Edit: _raise_live_modal_lookup body `raise AssertionError(...)` -> `return None`
PYTEST_ADDOPTS="tests/unit/test_modal_sdk_rail.py -q" make memory-tests        # 2 failed, 11 passed —
                                                        # test_a_live_lookup_raises_the_rails_assertion,
                                                        # test_the_client_path_fails_on_the_rail_not_on_the_network
Edit: revert to `raise AssertionError(MODAL_SDK_RAIL_MESSAGE)`
shasum apps/memory/tests/unit/conftest.py                                     # 5add1be3754f83ba640539a54b98e72685b56f11 — identical

shasum apps/memory/tests/unit/deploy/test_modal_deploy_scripts.py             # e02739f796b1548cbc70e97bb3bbaaff970e7440
Edit: `_process_spawns`'s bare-Name binding clause `... and id(node) not in called` -> `... and False`
PYTEST_ADDOPTS="tests/unit/deploy/test_modal_deploy_scripts.py -q" make memory-tests  # 4 failed, 138 passed —
                                                        # test_a_spawned_process_fails[imp = __import__…] x2 engines,
                                                        # [runner = eval…] x2 engines
Edit: revert `and False` removed
shasum apps/memory/tests/unit/deploy/test_modal_deploy_scripts.py             # e02739f796b1548cbc70e97bb3bbaaff970e7440 — identical
git status --short                                     # 14 modified + 1 untracked — no residue after both reverts

# --- (E) regression + import hygiene + secret scan + wall time ---
make memory-format-check                               # 313 files already formatted
make memory-lint-check                                 # All checks passed!
make pre-commit                                        # prettier / ruff check / ruff format / biome check ... Passed
make memory-tests                                      # 3917 passed in 56.29s (wall: 49.01s user, 6.10s sys, 61.06s total)
PYTEST_ADDOPTS="tests/unit/models/test_prewarm.py -v" make memory-tests        # 16 passed — test_a_sync_ensure_warm_does_not_leak_the_sibling,
                                                        # TestModalBackedModels (4+1+1 rows) all green, unchanged from round 1
uv run python -c "import sys, tree.models.get_model; print([m for m in ('modal','tree.models.modal_embedding','tree.models.modal_llm','torch') if m in sys.modules])"   # []
uv run python -c "import sys, tree.memory.pipeline; print(...)"               # []
uv run python -c "import sys, tree.mcp.server; print(...)"                    # []
grep -rn "EMBEDDING_SERVER_NAME" apps/                                        # 0 hits (exit 1)
git diff -- apps/memory/src apps/memory/tests | grep -Eic "api[_-]?key|secret|token|bearer|password"   # 28 (keyword hits — variable
                                                        # names / "Proxy token" / "HF_TOKEN" comments, no values)
git diff -- apps/memory/src apps/memory/tests | grep -Eic "sk-[a-zA-Z0-9]{10,}|AKIA[0-9A-Z]{16}|ghp_[a-zA-Z0-9]{30,}|[A-Za-z0-9+/]{40,}={0,2}"   # 0 — no secret-VALUE-shaped strings

# SWE-disclosed point (1): does `import modal` open a network connection?
uv run python -c "
import socket
def _boom(*a, **k): raise RuntimeError('NETWORK AT IMPORT')
socket.socket.connect = _boom
socket.create_connection = _boom
import modal
print('imported clean, no socket')
"                                                       # imported clean, no socket — import performs no network I/O

# SWE-disclosed point (3): adversarial candidates beyond the AC's list, both helpers, ast.parse only
uv run python <scratch: vars(os)["system"]("env"); os.__dict__["system"]("env");
  operator.attrgetter("system")(os)("env"); functools.partial(os.system,"env")();
  f = importlib.import_module; f("subprocess").run(["env"]); b = builtins; b.__import__("os").system("env");
  from os import system as s; s("env"); s = (lambda: os.system)(); s("env")
  against _process_spawns; and getattr(logger,"info")(...) [round-1, closed],
  logger.__getattribute__("info")(...), type(logger).info(logger,"x"), logging.Logger.info(logger,"x"),
  emit = logger.info; emit(...), s = (lambda: logger.info)(); s(...), type(logger).__dict__["info"](logger,"x")
  against _log_offenders>
```

**Round-1 blocking findings — verdict: CLOSED**
1. `__import__("os").system("env")` — re-mutated on both `sglang` and `vllm` copies (scratch, no deploy-script edit) → `_process_spawns` now reports `["__import__('os').system", "__import__('os')"]` on both. Traced to `_dynamically_imported()` (resolves the literal first argument of a `_DYNAMIC_IMPORTS` call) feeding the `ast.Attribute` branch's `imported == "os" and _spawns_from_os(node.attr)` clause. CLOSED.
2. `getattr(logger, "info")("unreviewed message")` — re-mutated on both scripts → `_log_offenders` now reports `["getattr(logger, 'info')('unreviewed message')"]` on both, via the new `isinstance(node.func, ast.Call)` branch ("calling the result of a call"). CLOSED.
Both are also permanent regression rows now (`test_a_spawned_process_fails[imp = __import__…]` / `[getattr(os,"system")("env")]` x2 engines, `test_a_spawned_process_fails[runner = eval…]`), proven non-vacuous by the (D) mutations below.

**(B) rail: fires before any network, composes with test-level patches, no fixture-ordering hole**
- `resolve_server_url` (`modal_server.py:146-150`) calls `modal.Server.from_name(...)` synchronously, *before* the `await server.get_url.aio()` — so a raiser on `from_name` fires before any coroutine that could touch a socket is even created. Verified empirically: a temporary test patched `socket.socket.connect` to raise a distinct `_NetworkOpened` error, then called `resolve_server_url(...)` with the rail active and un-patched; the result was `ModelError` whose `__cause__` was the rail's `AssertionError` ("live Modal SDK lookup…"), not `_NetworkOpened` — the socket door was never reached. Scratch file removed immediately after (`git status --short` clean at 14+1).
- `test_a_test_level_patch_still_wins` (pre-existing, re-run green) proves a test's own `mocker.patch("tree.models.modal_server.modal.Server")` replaces the whole class reference the module holds, so the rail's `from_name` patch on the *original* class object is bypassed for that test's duration and restored (by LIFO teardown) the instant the test ends.
- Fixture-ordering: `grep`-checked every `conftest.py` under `apps/memory/tests` — `modal` is referenced only in `tests/unit/conftest.py`. The only session/module-scoped fixture anywhere in the suite is `_init_beanie` (session-scoped, Mongo only). No fixture can perform a lookup before `_no_live_modal_sdk` (function-scoped autouse) installs the rail for a given test.

**(C) shipped scripts — no deploy edit**
`git diff --stat -- apps/memory/deploy apps/memory/scripts Makefile` — empty, both before and after this review's mutation rounds.

**(D) non-vacuity — both rules independently disabled, named tests red, reverted, shasum-proved identical**
- `conftest.py` raiser body `raise AssertionError(...)` → `return None`: `test_a_live_lookup_raises_the_rails_assertion` and `test_the_client_path_fails_on_the_rail_not_on_the_network` went red (2 failed, 11 passed) — matches the SWE's claimed table exactly. Reverted; `shasum` = `5add1be3754f83ba640539a54b98e72685b56f11` before and after.
- `test_modal_deploy_scripts.py` binding-rule clause (`and id(node) not in called`) → `and False`: `test_a_spawned_process_fails[imp = __import__…]` and `[runner = eval…]` on both engines went red (4 failed, 138 passed) — matches the SWE's claimed table exactly. Reverted; `shasum` = `e02739f796b1548cbc70e97bb3bbaaff970e7440` before and after.
Both reverts also confirmed by `git status --short` returning to exactly 14 modified + 1 untracked.

**(E) regression, import hygiene, secrets, wall time**
- `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` — all green, run one at a time: 313 files formatted, lint clean, pre-commit clean, **3917 passed in 56.29s** (49.01s user + 6.10s sys, 61.06s wall per `time`).
- `test_a_sync_ensure_warm_does_not_leak_the_sibling` and the full `TestModalBackedModels` class (5 rows) re-run directly: all green, unchanged from round 1 — C1/C2 pre-warm behaviour is intact.
- Import hygiene, fresh interpreter, 3 modules (`tree.models.get_model`, `tree.memory.pipeline`, `tree.mcp.server`): none of `modal` / `tree.models.modal_embedding` / `tree.models.modal_llm` / `torch` in `sys.modules` after import — `[]` all three times.
- `grep -rn "EMBEDDING_SERVER_NAME" apps/` → 0 hits (grep exit 1).
- Secret scan: 28 case-insensitive keyword hits (`token`/`secret`/`key`/`password`/`bearer`) in the diff — all identifiers/comments/error-message literals (`HF_TOKEN`, `Proxy token`, `MODAL_SDK_RAIL_MESSAGE`, `mocker.patch`, etc.); 0 hits for an actual secret-VALUE-shaped pattern (API-key prefixes, 40+-char base64/hex blobs). No live credential in the diff.
- Wall time judgment: the task's "≈50s" baseline is not attested anywhere in this task's own log; the SWE's own pre-rail number is 3872 tests in 58.77s. My clean 3917-test run (45 more tests, 4 extra `mocker.patch.object` calls per test from the new autouse fixture) came in at 56.29s — *faster* than the pre-rail baseline, well within normal machine-to-machine variance (the 455.94s figure in round 1's log is contention noise from a different run, not a regression signal). **No measurable slowdown attributable to the rail.**

**SWE-disclosed point (1) — ruled**
- In-process `sys.modules` growth: accepted consequence, explicitly named in the SWE's log, and every existing `'modal' not in sys.modules` assertion in the repo runs in a subprocess (confirmed unaffected).
- Import-time side effects: `import modal` with `socket.socket.connect` / `socket.create_connection` patched to raise still imports cleanly (`imported clean, no socket`) — no network call at import. (Config-file reads of `~/.modal.toml`, if any, are explicitly permitted by the task and not itself checked further — no network path exists to check.)
- Verdict: the rail's in-process import is safe and costless.

**SWE-disclosed point (2) — ruled**
A silent skip in `_install_modal_sdk_rail` for a door the installed SDK lacks is acceptable **because `test_modal_sdk_rail.py`'s `_DOORS` list is a hand-written duplicate of `_MODAL_SDK_DOORS`, not an import of it** (comment at `test_modal_sdk_rail.py:40-43` says so explicitly). If a future `modal` upgrade renames `Server.from_name`, the installer's `hasattr` check skips it silently — but `_door("Server", "from_name")` in the test (`getattr(getattr(modal, "Server"), "from_name")`) raises `AttributeError` immediately, and `test_every_sdk_door_carries_the_rail[Server-from_name]` errors loudly at collection/run time. The duplication IS the loudness mechanism, not a coincidence — proven by the SWE's own history here (the interrupted session's list had 3 doors; the completing session's 4-of-4 test caught the mismatch against `_MODAL_SDK_DOORS`, which is exactly this failure mode firing for real). Confirmed all 4 `(class, attribute)` pairs exist on the pinned modal 1.5.5 by attribute inspection only (`getattr`, no call). Sufficient.

**SWE-disclosed point (3) — the two documented KNOWN BOUNDS, tried plus more**
All candidates from the task prompt, run as `ast.parse` scratch mutants (no deploy script touched):

| candidate | `_process_spawns` | `_log_offenders` |
| --- | --- | --- |
| `vars(os)["system"]("env")` | `[]` — **UNNAMED GAP** | n/a |
| `os.__dict__["system"]("env")` | `[]` — **UNNAMED GAP** | n/a |
| `operator.attrgetter("system")(os)("env")` | caught (calling-the-result-of-a-call rule) | n/a |
| `functools.partial(os.system, "env")()` | caught (`os.system` is a bare Attribute reference regardless of wrapper) | n/a |
| `from os import system as s; s("env")` | caught (`from os import system`) | n/a |
| `s = (lambda: os.system)(); s("env")` | caught (`os.system` Attribute reference present) | n/a |
| `f = importlib.import_module; f("subprocess")` | **named bound** (docstring's own example, verbatim) | n/a |
| `b = builtins; b.__import__("os").system("env")` | **named bound** (docstring's own example, verbatim) | n/a |
| `getattr(logger, "info")("unreviewed message")` | n/a | caught (round-1 fix, re-verified above) |
| `logger.__getattribute__("info")("x")` | n/a | caught (Call-typed `.func`, same rule) |
| `type(logger).info(logger, "x")` | n/a | caught (receiver-agnostic: `.attr == "info"` matches regardless of receiver) |
| `logging.Logger.info(logger, "x")` | n/a | caught (same receiver-agnostic match) |
| `emit = logger.info; emit("x")` | n/a | **named bound** (docstring's own example, verbatim) |
| `s = (lambda: logger.info)(); s("x")` | n/a | `[]` — same root cause as the named `emit = logger.info` bound (an emit method bound to a name, called through it) — **covered**, not a new gap |
| `type(logger).__dict__["info"](logger, "x")` | n/a | `[]` — **UNNAMED GAP**, same subscript-shape as the spawn-side gap |

Ruling: `vars(...)`/`.__dict__[...]` subscript-based attribute access is an **UNNAMED gap on both helpers**, not covered by either KNOWN BOUNDS block (which name only: non-spawn exfiltration, attribute-bound dynamic import, eval-built strings, third-party imports for `_process_spawns`; eval-built messages, un-named-logger handlers, bound emit methods, non-log exfiltration for `_log_offenders`). Judged **exotic, not a trivial one-token rewrite** — unlike round 1's `__import__("os").system(...)` (a plausible IDE-completion / refactor artifact matching the task's own "debugging `os.system("env")` sneaks in" user story), `vars(os)["system"]`/`__dict__` subscripting requires deliberate evasion intent nobody produces by accident. Per the standard set for this review ("a trivial rewrite is blocking, an exotic one is a follow-up note") — **not blocking**, but it IS a genuine, demonstrated counterexample to `_process_spawns`'s docstring claim "Every way `module` could start a CHILD PROCESS, **or hide one**, as written" (no such absolute claim is made by `_log_offenders`, whose docstring already scopes itself to KNOWN BOUNDS). Recommend as a cheap, named follow-up (not required before commit): either (a) add `vars`/`__dict__`-subscript detection alongside the existing `_DYNAMIC_IMPORTS`/`_DYNAMIC_EXECUTION` handling (~a few lines, mirrors the existing pattern), or (b) add one more KNOWN BOUNDS clause naming subscript-based attribute access explicitly in both docstrings. Filed as "Other issues found," not a blocking finding — this review's mandate was closing the two specifically-identified round-1 blockers, both of which are closed.

**Acceptance criteria** — all 18 re-verified, all still PASS (unchanged from round 1's evidence; spot-checked `test_a_sync_ensure_warm_does_not_leak_the_sibling`, `TestModalBackedModels`, `test_no_process_is_spawned`, `test_the_shipped_script_passes_every_guard`, `MODAL_SERVER_NAME` rename, cosmetics — all green, all read).

**Other issues found**
1. Not blocking, follow-up recommended: `vars(os)["system"](...)` / `os.__dict__["system"](...)` / `type(logger).__dict__["info"](...)` bypass both deploy-script guards silently; not named in either KNOWN BOUNDS block. Exotic (deliberate-evasion-only), not a trivial rewrite of the task's own threat model — does not block this review, but should be a named follow-up (widen the two helpers a few lines, or extend KNOWN BOUNDS) before the guard's docstring claims "every way" without qualification.

**VERDICT: PASS**

Both round-1 blocking findings are independently re-verified closed (re-mutated from scratch, not just re-read) on both deploy scripts; the permanent SDK rail is proven to fire before any network access, to compose correctly with test-level patches, and to have no fixture-ordering hole; both new guard rules are proven non-vacuous by an independent (Tester-authored, not SWE-authored) mutation with byte-identical shasum-verified reverts; the shipped deploy scripts remain untouched (empty diff); the full regression suite (3917 tests), format/lint/pre-commit are green with no measurable wall-time regression; import hygiene and the secret scan are clean; all 18 acceptance criteria hold. One exotic, non-blocking gap (subscript-based attribute access bypassing both guards) is filed as a named follow-up, not a blocker, per the standard set for this review.
