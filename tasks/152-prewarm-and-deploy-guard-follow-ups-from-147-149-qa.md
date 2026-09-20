---
id: 152-prewarm-and-deploy-guard-follow-ups-from-147-149-qa
status: pending
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
- [ ] `test_a_sync_ensure_warm_does_not_leak_the_sibling`: models = (a fake whose `async ensure_warm` awaits `asyncio.sleep(600)`, FIRST; a fake whose `ensure_warm` is a plain `def` returning `None`, SECOND; distinct `warm_key`s). `before = asyncio.all_tasks()` -> `pytest.raises(TypeError, match="a coroutine was expected")` -> afterwards `asyncio.all_tasks() == before`, and the sleeping fake recorded a `CancelledError` if it had started (or never started) — it is NOT pending. The test FAILS on the code at d3a9fe2 (state that you saw it red in `## Log`).
- [ ] `test_first_failure_cancels_the_sibling`, `test_warms_concurrently`, `test_dedupes_on_warm_key`, `test_is_idempotent`, `test_noop_without_ensure_warm` stay green unmodified.
- [ ] `TestModalBackedModels::test_a_non_modal_provider_never_calls_its_factory`: parametrised over provider in `("sentence-transformers", "voyage", "gemini", "mock")` for the embedding blocks and `"gemini"` for `llm` — with `tree.models.get_model._build_embedding_model` / `get_llm` patched as tripwires, `modal_backed_models("llm", "resolution_embedding", "search_embedding") == []` and NO tripwire was called; `"sentence_transformers" not in sys.modules` growth is not required, the call count is.
- [ ] `TestModalBackedModels::test_a_modal_block_is_built_once`: only `search_embedding.provider == "modal"` -> exactly one factory call (the search one) and a one-element list holding its return value; all three `modal` -> three elements in the order asked.
- [ ] `TestModalBackedModels::test_reads_the_provider_at_call_time`: flipping `app_config.models.llm.provider` between two calls changes the result (no import-time freeze).

Seams — `apps/memory/tests/unit/memory/test_pipeline.py`:
- [ ] `test_worker_seam_builds_nothing_for_sentence_transformers`: worker run in `rag` AND in `graphrag` with `search_embedding.provider = "sentence-transformers"` (factories patched as tripwires, run stopped at the first stage like `_run_worker_for_prewarm`) -> zero factory calls before the first stage; `prewarm_models` awaited with no models.
- [ ] `test_worker_seam_builds_the_modal_blocks_only`: `llm = modal`, `search_embedding = modal`, `resolution_embedding = voyage`, `graphrag` -> `prewarm_models` receives exactly the two Modal instances; in `rag` with the same config -> the search instance only (the existing `test_rag_warms_the_search_embedding_model_only` intent, re-expressed under `provider: modal`).
- [ ] `test_worker_prewarm_is_a_noop_on_default_providers` stays green and additionally asserts zero factory calls at the seam.
- [ ] `test_summary_seam_builds_the_llm_only_under_modal`: `llm.provider = "gemini"` -> `get_llm` not called by the seam, summaries proceed; `= "modal"` -> built once, pre-warmed once before the fan-out (`test_the_llm_is_prewarmed_once_before_the_fan_out` re-expressed), a `ModelError` from the warm still yields ONE warning + fallback labels, and a construction error (missing **Proxy token**) still propagates.
- [ ] `tests/unit/memory/graph/consolidation/test_dream.py` is untouched and green.

Static guards — `apps/memory/tests/unit/deploy/test_modal_deploy_scripts.py`, every test parametrised over BOTH scripts (`_ENGINES`), every mutant built with `_mutate(...)` into `tmp_path`:
- [ ] `TestGlueContract::test_no_process_is_spawned`: `_process_spawns(_module(engine)) == []`; and the control row `test_the_shipped_script_passes_every_guard` asserts it too.
- [ ] `TestTheGuardsCatchTheirMutants::test_a_spawned_process_fails`, parametrised over these statements inserted before `_STOP_CALL`: `import subprocess; subprocess.run(["env"])`, `import subprocess as sp; sp.check_output("env")`, `from subprocess import run; run(["env"])`, `os.system("env")`, `os.popen("env").read()`, `os.execvp("env", ["env"])`, `os.spawnlp(os.P_WAIT, "env", "env")`, `from os import system; system("env")`, `import pty; pty.spawn("env")`, `import commands` -> `_process_spawns(mutant)` is non-empty and names the offender, for all 10 statements x 2 scripts.
- [ ] `TestTheGuardsCatchTheirMutants::test_any_spelling_of_a_logging_call_fails`, parametrised over: `logging.getLogger().info("spec is %s", SPEC)`, `logging.info("spec is %s", SPEC)`, `logging.getLogger(__name__).warning("spec is %s", SPEC)`, `log = logging.getLogger("x"); log.error("spec is %s", SPEC)`, `logger.log(logging.INFO, "spec is %s", SPEC)` -> `_log_offenders(mutant, engine)` is non-empty for all 5 x 2; and an allow-listed message sent through `logging.getLogger().info(<allow-listed literal>, <safe arg>)` is NOT an offender (the rule is the allow-list, not the spelling).
- [ ] `test_an_unreviewed_log_message_fails` and `test_a_logged_environment_fails` stay green with their exact expected strings.
- [ ] No test imports, executes or edits a file under `apps/memory/deploy/`; `git diff --stat -- apps/memory/deploy apps/memory/scripts Makefile` is empty.

Cosmetics:
- [ ] `grep -rn "EMBEDDING_SERVER_NAME" apps/ docs/` -> 0 hits; `grep -rn "MODAL_SERVER_NAME" apps/memory/src | wc -l` >= 9; `MODAL_SERVER_NAME == "Server"` is still asserted in `test_modal_catalog.py`.
- [ ] `test_a_non_catalog_model_fails_before_any_gpu_wakes` takes `self` only; no line of `get_model.py`'s module NOTE exceeds 88 columns.
- [ ] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green (LOCAL env).

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
