---
id: 151-llm-identity-in-the-llm-task-cache-key
status: done
feature: voyage-4-and-modal-embedding-catalog
---

# The LLM identity joins the cache key of the two cached LLM tasks — a provider/model switch is a cache MISS, not a replay of the old LLM's JSON

Tags: `memory`, `llm`, `prefect-cache`, `bug`
Depends on: None (the `models.llm` switch of #148 is done; #150 runs first by NNN order only)
Blocks: #141
Implements: ADR-009 — Decision 6's rule ("caches carry the identity of the model that produced them"), applied to Decision 10's `models.llm.provider` switch. Mirrors #134's `embedding_identity` one-to-one; no new mechanism.

## Scope

**EXECUTION ORDER of the rework: 143 -> 144 -> 145 -> 146 -> 147 -> 148 -> 149 -> 150 -> 151 -> 152 -> 141.**
No Modal code, no deploy script and no driver is touched; no process other than the test suite is started.

Run test commands one at a time — two concurrent pytest runs contend for the local MongoDB/Prefect and stall.
Do not run the real memory pipelines against the local database (shared with the human's `main` checkout).
No agent may run `make memory-deploy-*`, `scripts/modal_*.py` as a process, or any `modal …` command outside task 141; a PATH shim does NOT intercept under `make`/`uv run` because `.venv/bin` wins — use the mocked unit tests and `DRY_RUN=yes`.

**The defect** (found by #148's SWE, confirmed by its Tester; commit 22734df points here). Two Prefect tasks in
`apps/memory/src/tree/memory/pipeline.py` cache an LLM's output under `cache_policy=_INPUTS_NO_HEADERS` with
nothing in their inputs that names the LLM:

| Task | Definition | Cache | Inputs today | Where the LLM comes from |
|---|---|---|---|---|
| `llm_extract_entities_task` | `:669-676` (body `_llm_extract_entities`, `:597`) | 30 d | `chunked`, `user_id`, `llm=None`, (`opik_trace_headers` excluded) | `get_llm()` inside the body (`:636`); the only production call site (`:2105`) never passes `llm`, so that slot is always the literal `None` |
| `summarise_cluster_task` | `:2561-2570` (body `_summarise_cluster`, `:2529`) | 90 d | `samples`, `cluster_id`, `prompt_version`, (`opik_trace_headers` excluded) | `get_llm()` inside the body (`:2558`) |

After `models.llm` changes (`gemini` -> `modal`, or one model id -> another), a document / cluster already seen
inside the window REPLAYS the previous LLM's JSON and the new LLM is never called. This is the bug ADR-009
Decision 6 fixed for embeddings, still open on the LLM side.

**The fix — mirror #134 exactly.**

1. ONE helper in `apps/memory/src/tree/models/get_model.py`, directly below `search_embedding_identity()`:
   `def llm_identity() -> str` returning `f"{cfg.provider}:{cfg.model}"` with `cfg = app_config.models.llm`,
   read at CALL time (never a module constant — Prefect re-imports the module in flow-run subprocesses and a
   `TREE_MODELS__LLM__MODEL=…` override must move the identity). With the shipped defaults the value is
   `"gemini:gemini-3.1-flash-lite"`. Two parts only: an LLM has no dimensions and no **Embedding role**.
2. Both task bodies gain a REQUIRED keyword-only parameter `llm_identity: str` (no default — a forgotten call
   site must be a `TypeError`, not a silent shared key). It is UNUSED in the body; its docstring line says it is
   there purely to sit in the `INPUTS` cache key, in the words of the `embedding_identity` docstrings
   (`:496`, `:1181`). `_INPUTS_NO_HEADERS` is unchanged — `llm_identity` must NOT be excluded.
3. Every call site passes it: `run_memory_worker` resolves `llm_identity()` ONCE per run, next to
   `embedding_identity = search_embedding_identity()` (`:2050`), and hands it to `llm_extract_entities_task`
   (`:2105`); `_summarise_clusters` resolves it once before its fan-out and hands it to `summarise_cluster_task`
   (`:2599`). `grep -rn "llm_extract_entities_task(\|summarise_cluster_task(" apps/memory/src` must show the
   kwarg at every hit.
4. The existing optional `llm: BaseLLM | None = None` parameter of `_llm_extract_entities` stays as it is.

**Consequence to state in the commit message and `## Log`:** the key shape changes for BOTH tasks, so the first
run after merge is ONE cold LLM cache — every document re-extracts once (30 d window) and every cluster
re-summarises once (90 d window), on the billable LLM. That is the price of never replaying the wrong model and
it is paid once.

## Out of scope
- Any other cached task (`clean_and_chunk_task`, the two embed tasks — already identity-keyed by #134).
- Invalidating or deleting existing Prefect cache records (the new key simply never matches them).
- A prompt-version input for `llm_extract_entities_task` (`summarise_cluster_task` already has one) — not asked for.
- `dream.py`, the judge LLM and every other `get_llm()` caller: none of them is a cached Prefect task.
- ADR-009 / glossary edits — the rule already exists as Decision 6; PA territory if the reviewer wants it spelled out for LLMs.

## Acceptance Criteria

- [x] `tests/unit/models/test_get_model.py::TestLLMIdentity::test_default_identity`: `llm_identity() == "gemini:gemini-3.1-flash-lite"` on the shipped config.
- [x] `TestLLMIdentity::test_reads_the_config_at_call_time`: with `app_config.models.llm` patched to `LLMConfig(provider="modal", model="LiquidAI/LFM2.5-350M")` AFTER import (the style of `TestSearchEmbeddingIdentity::test_reads_the_config_at_call_time`, `:416-430`) -> `"modal:LiquidAI/LFM2.5-350M"` — i.e. nothing was frozen at import time.
- [x] `TestLLMIdentity::test_env_override_moves_the_identity`: a config loaded with `TREE_MODELS__LLM__MODEL=gemini-2.5-flash` in the environment (through `_apply_env_overrides`, the way the existing `app_config` override tests do it) renders `"gemini:gemini-2.5-flash"`.
- [x] `tests/unit/memory/test_pipeline.py::TestLLMTaskCacheIdentity`, parametrised over `llm_extract_entities_task` and `summarise_cluster_task`, modelled on `TestEmbedTaskCacheIdentity` (`:1532-1604`):
  - `test_cache_key_differs_across_llm_identities`: `cache_policy.compute_key(...)` with identical other inputs and `llm_identity="gemini:gemini-3.1-flash-lite"` vs `"modal:LiquidAI/LFM2.5-350M"` -> two DIFFERENT keys; also different for the same provider with two model ids.
  - `test_cache_key_ignores_opik_trace_headers`: same inputs + same `llm_identity`, two different `opik_trace_headers` dicts -> EQUAL keys.
  - `test_task_body_requires_llm_identity`: `inspect.signature` of the body shows `llm_identity` as `KEYWORD_ONLY` with `default is inspect.Parameter.empty`; calling the body without it raises `TypeError`.
  - `test_llm_identity_is_not_excluded_from_the_key`: `"llm_identity" not in (task.cache_policy.exclude or [])`.
- [x] `test_worker_passes_the_llm_identity_to_extraction`: with `llm_extract_entities_task` patched, a worker run under `models.llm = {provider: "modal", model: "LiquidAI/LFM2.5-350M"}` (no real model built — factories patched) calls it with `llm_identity="modal:LiquidAI/LFM2.5-350M"`.
- [x] `test_summaries_pass_the_llm_identity_to_every_cluster`: `_summarise_clusters` over 3 clusters calls the patched `summarise_cluster_task` 3 times, each with the same `llm_identity == llm_identity()`.
- [x] `grep -rn "llm_extract_entities_task(\|summarise_cluster_task(" apps/memory/src` -> every call carries `llm_identity=`; every pre-existing direct call of `_llm_extract_entities(` / `_summarise_cluster(` in `tests/` is updated to pass it (no default was added to spare them).
- [x] Every pre-existing test of the two tasks stays green, incl. `summarise_cluster_task.cache_expiration == timedelta(days=90)`, `retries == 2` and the task names `llm-extract-entities` / `summarise-cluster`.
- [x] `## Log` and the commit message carry the sentence "one cold LLM cache after merge: every document re-extracts once, every cluster re-summarises once".
- [x] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green (LOCAL env).

## User Stories

### Story: Operator flips the LLM to their own Modal server and re-ingests a document from last week
1. `configs/default.yaml` -> `llm: {provider: modal, model: LiquidAI/LFM2.5-350M}`; the document was extracted under Gemini 6 days ago.
2. Before: `llm-extract-entities` answers from the 30-day cache with Gemini's JSON; the Modal server is never called and the operator believes the 350M model produced that graph.
3. After: the key carries `modal:LiquidAI/LFM2.5-350M`, the cache MISSES, the run log shows `ModalLLM ready: …` and the extraction is the Modal model's own.

### Story: Operator upgrades the Gemini model id
1. `model: gemini-3.1-flash-lite` -> a newer id; cluster summaries ran 40 days ago.
2. After: `summarise-cluster` re-labels every cluster with the new model instead of replaying 40-day-old labels for another 50 days.

### Story: One-shot override from the shell
1. `TREE_MODELS__LLM__MODEL=gemini-2.5-flash make …` for a single run.
2. `llm_identity()` reads config at call time -> `gemini:gemini-2.5-flash` -> that run neither reads nor pollutes the default model's cache entries; the next run without the override hits the default's entries again.

### Story: Same LLM, second run of the same document
1. Nothing in `models.llm` changed; only the Opik trace headers differ between the two runs.
2. The key is equal -> the cached extraction is served, no billable call (the cache still does its job).

### Story: The next engineer adds a third call site of a cached LLM task
1. They call `llm_extract_entities_task(chunked, user_id)` without the kwarg.
2. `TypeError: … missing 1 required keyword-only argument: 'llm_identity'` at the first test run — not a silently shared cache key.

---

Blocked by: (none)

## Log

### [PA] 2026-09-20 19:30 — Grooming (new task, split out of the "routed to #150" follow-ups)

**Summary**
The two cached LLM tasks get a required `llm_identity: str` input (`provider:model`, from one call-time helper next to `search_embedding_identity()`), so a `models.llm` change is a cache miss instead of a replay of the old LLM's JSON.

**Key decisions**
- Pointed here by commit 22734df (#148: SWE follow-up note + Tester "Adjacent issue — confirmed real", evidence `pipeline.py:667-674`, `:2535-2545`, call site `:2086` at that commit; re-read at HEAD d3a9fe2: `:669-676`, `:2561-2570`, `:2105`, `:2599`). It was "routed to #150", whose body never carried it; it is its own task because Prefect cache keys are a different concern from #150's embedding text.
- Mirror #134, nothing new: required keyword-only input, unused in the body, one helper read at call time. No interface, no cache-policy subclass.
- Accepted cost: one cold LLM cache after merge (30 d / 90 d windows), stated in the commit.
- Before #141: with this in place the LLM cache cannot replay across providers by construction; #141 still proves the Modal LLM was really called.

**Dependencies**
- None (#148 is done).

**User stories**
- 5 stories: provider flip, model upgrade, one-shot env override, unchanged LLM still hits, forgotten kwarg fails loudly.

Ready for implementation.

### [SWE] 2026-09-20 20:35 — Implementation

**Files modified**
- `apps/memory/src/tree/models/get_model.py` — new `llm_identity()` helper directly below `search_embedding_identity()`: `f"{cfg.provider}:{cfg.model}"` from `app_config.models.llm`, read at CALL time. Two parts only (an LLM has no dimensions and no **Embedding role**).
- `apps/memory/src/tree/memory/pipeline.py` — `_llm_extract_entities` and `_summarise_cluster` take a REQUIRED keyword-only `llm_identity: str` (unused by the body, folded into each task's existing `log.info` line so the value shows up in the Prefect logs); `_run_extraction_worker_body` resolves it once per run next to `embedding_identity` (local `extraction_llm_identity`) and passes it to `llm_extract_entities_task`; `_summarise_clusters` resolves it once before the fan-out (local `cluster_llm_identity`) and passes it to `summarise_cluster_task`. `_INPUTS_NO_HEADERS` untouched; `llm: BaseLLM | None = None` untouched.
- `apps/memory/tests/unit/models/test_get_model.py` — `TestLLMIdentity` (4 tests: shipped default, call-time read, env override, two-part shape).
- `apps/memory/tests/unit/memory/test_pipeline.py` — `TestLLMTaskCacheIdentity` (cache key across identities / across trace headers / required-kwarg signature + `TypeError` / not excluded, each parametrised over both tasks), `TestFlowEmbeddingModelSplit::test_worker_passes_the_llm_identity_to_extraction`, `TestSummariseClustersLLMIdentity::test_summaries_pass_the_llm_identity_to_every_cluster`; the 3 pre-existing direct `_llm_extract_entities(...)` calls now pass `llm_identity=_TEST_LLM_IDENTITY` (new module constant).

**Tests**
- Unit: 3820 passing, 0 failing (`make memory-tests`, `make env-status` → local).
- Integration: N/A — no integration suite by design (AGENTS.md); e2e verification by running the real code, below.

**Acceptance criteria**
- [x] `TestLLMIdentity::test_default_identity` — `llm_identity() == "gemini:gemini-3.1-flash-lite"`. The module's autouse `_mock_app_config` fixture installs a `MagicMock` for `app_config`, so the test puts the REAL import-time singleton back (`mocker.patch("tree.models.get_model.app_config", real_app_config)`) — otherwise it would assert about the double, not about the shipped YAML.
- [x] `TestLLMIdentity::test_reads_the_config_at_call_time` — `models.llm` patched AFTER import to `LLMConfig(provider="modal", model="LiquidAI/LFM2.5-350M")` → `"modal:LiquidAI/LFM2.5-350M"`.
- [x] `TestLLMIdentity::test_env_override_moves_the_identity` — `TREE_MODELS__LLM__MODEL=gemini-2.5-flash` + `load_app_config(<tmp yaml>)` → `"gemini:gemini-2.5-flash"` (the `test_dedup_concurrency_env_override` idiom).
- [x] `TestLLMTaskCacheIdentity` (parametrised over `llm_extract_entities_task` / `summarise_cluster_task`) — `test_cache_key_differs_across_llm_identities` (gemini vs modal AND gemini vs a newer gemini id), `test_cache_key_ignores_opik_trace_headers`, `test_task_body_requires_llm_identity` (KEYWORD_ONLY + `default is inspect.Parameter.empty` + a real `TypeError` from a call without it), `test_llm_identity_is_not_excluded_from_the_key`.
- [x] `test_worker_passes_the_llm_identity_to_extraction` — worker run with `models.llm` patched to `{modal, LiquidAI/LFM2.5-350M}` (every factory stubbed, no model built) calls the patched task with `llm_identity="modal:LiquidAI/LFM2.5-350M"`.
- [x] `test_summaries_pass_the_llm_identity_to_every_cluster` — 3 clusters → 3 calls, all carrying the same `llm_identity()`.
- [x] `grep -rn "llm_extract_entities_task(\|summarise_cluster_task(" apps/memory/src` → 2 hits (`pipeline.py:2091`, `:2602`), both carrying `llm_identity=`. The 3 pre-existing direct body calls in `tests/` were updated (no default was added to spare them); no call site exists in `scripts/`, `deploy/` or `tutorials/`.
- [x] Every pre-existing test of the two tasks stays green, incl. the 90-day expiration, `retries == 2` and the `llm-extract-entities` / `summarise-cluster` names (3820 passed, 0 failed).
- [x] `## Log` carries the consequence sentence (below). The commit message carries it too — written at commit time, which is after the Tester's PASS (nothing is committed yet).
- [x] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green on `make env-status` → local.

**Consequence**
The key shape changes for BOTH tasks, so the price is one cold LLM cache after merge: every document re-extracts once, every cluster re-summarises once. The first run after merge misses on the 30-day `llm-extract-entities` window and the 90-day `summarise-cluster` window — billable calls. Paid once; the alternative is replaying the wrong model's JSON.

**Evidence**
```
$ make env-status
Env target: local (.env)

$ make memory-tests
3820 passed in 48.87s   (full chain re-run against the final tree)

$ make memory-format-check && make memory-lint-check
312 files already formatted
All checks passed!

$ make pre-commit
prettier / ruff check / ruff format / biome check (harness) ......... Passed

$ grep -rn -A5 "llm_extract_entities_task(\|summarise_cluster_task(" apps/memory/src
pipeline.py:2091:            await llm_extract_entities_task(
pipeline.py-2094-                llm_identity=extraction_llm_identity,
pipeline.py:2602:            return await summarise_cluster_task(
pipeline.py-2606-                llm_identity=cluster_llm_identity,
```

End-to-end (offline; NO Gemini / Modal call, NO Mongo write, NO real pipeline run):
```
# 1. The helper + the REAL cache policies of both tasks (uv run python, one process)
llm_identity(): gemini:gemini-3.1-flash-lite

[llm-extract-entities] expiration=30 days, 0:00:00
  gemini key      : 3d63d95f4d045523
  modal  key      : 89499fd691f4eef7 -> MISS
  gemini-2.5 key  : 8f113c25be49c1f0 -> MISS
  trace headers   : HIT (equal keys)
  excluded inputs : ['opik_trace_headers']

[summarise-cluster] expiration=90 days, 0:00:00
  gemini key      : ca9f45004a3f62a2
  modal  key      : ed7f7776eda85747 -> MISS
  gemini-2.5 key  : 263a686952203859 -> MISS
  trace headers   : HIT (equal keys)
  excluded inputs : ['opik_trace_headers']

# 2. Story 3 — the escape hatch in a FRESH process (what a flow-run subprocess sees)
$ TREE_MODELS__LLM__MODEL=gemini-2.5-flash uv --directory apps/memory run python -c "..."
with TREE_MODELS__LLM__MODEL=gemini-2.5-flash -> gemini:gemini-2.5-flash

# 3. Prefect-level proof against the ALREADY-RUNNING local server: a throwaway
#    in-process flow around the REAL summarise-cluster task with a FakeLLM double
#    and invented samples (cluster_id=151). Completed -> Cached -> Completed.
Task run 'summarise-cluster-8d7' - summarise_cluster: llm=gemini:gemini-3.1-flash-lite cluster_id=151 n_samples=2 prompt_version=v1
Task run 'summarise-cluster-8d7' - Finished in state Completed()
Task run 'summarise-cluster-d20' - Finished in state Cached(type=COMPLETED)
Task run 'summarise-cluster-652' - summarise_cluster: llm=modal:LiquidAI/LFM2.5-350M cluster_id=151 n_samples=2 prompt_version=v1
Task run 'summarise-cluster-652' - Finished in state Completed()

# 4. The same proof for the OTHER cached task, with an EMPTY ChunkedDocument:
#    the body returns before ``get_llm()`` is reached, so NO LLM (not even a
#    fake) is called — only the cache key is exercised.
Task run 'llm-extract-entities-ff7' - Finished in state Completed()
Task run 'llm-extract-entities-f93' - Finished in state Cached(type=COMPLETED)
Task run 'llm-extract-entities-481' - Finished in state Completed()
```

**Notes**
- **Which tests are the real guard.** `compute_key(task_ctx=None, inputs={...}, flow_parameters={})` hashes whatever dict it is handed and never consults the task body's signature, so `test_cache_key_differs_across_llm_identities` and `test_llm_identity_is_not_excluded_from_the_key` pass even without the `pipeline.py` change (#134's equivalents have the same property). The wiring is pinned by `test_task_body_requires_llm_identity`, `test_worker_passes_the_llm_identity_to_extraction` and `test_summaries_pass_the_llm_identity_to_every_cluster`. All three were proven non-vacuous by reverting the fix and restoring it: dropping the kwarg at the two call sites failed exactly the two call-site tests; giving `llm_identity` a default failed both `test_task_body_requires_llm_identity` params; freezing the helper into a module constant failed `test_reads_the_config_at_call_time` and `test_env_override_moves_the_identity`.
- **Local names avoid a shadow bug.** `llm_identity = llm_identity()` inside either function would make the imported helper a local for the whole function body → `UnboundLocalError` at the first call. Hence `extraction_llm_identity` / `cluster_llm_identity`.
- **`_summarise_clusters` comments deliberately avoid the word "extraction".** The pre-existing diff-guard `test_the_fan_out_uses_its_own_knob` asserts `"extraction" not in` that function's body (it must not borrow the graph-only concurrency knob), so the cross-reference to the extraction task lives in `_llm_extract_entities` instead.
- **`opik_trace_headers` became keyword-only** on both bodies as a side effect of the `*` separator (the same shape `_embed_children` / `_embed_entities` already have). No caller passed it positionally.
- **README not touched**: AC #9 scopes the consequence sentence to `## Log` + the commit message, and `apps/memory/README.md` has no caching section to attach it to (`grep -n "cache" apps/memory/README.md` → no match).
- The two Prefect probes above wrote two throwaway flow runs + four cache records to the ALREADY-RUNNING local Prefect server (no infrastructure was started) and emitted live Opik traces under `tree-memory` — the same side effects #134's proof had. Nothing touched the `memory` collection; no `modal` command was run, no Gemini token was spent (the cluster probe used a `FakeLLM`, the extraction probe reached no LLM at all).
- `grep -rn "llm_extract_entities_task(\|summarise_cluster_task(\|_llm_extract_entities(\|_summarise_cluster(" tutorials apps/memory/scripts apps/memory/deploy docker` → no match (exit 1), so the two `apps/memory/src` hits plus the updated `tests/` calls are the complete call-site set.
- Not committed — waiting on Tester review.

### [Tester] 2026-09-20 21:10 — QA

**Commands run, in order**
```
git status --short && git branch --show-current && make env-status
cat AGENTS.md
cat tasks/151-llm-identity-in-the-llm-task-cache-key.md
grep -n "Decision 6\|Decision 10\|^\*\*Decision" docs/adrs/009_embedding_roles_and_modal_embedding_catalog.md
sed -n (Read) docs/adrs/009_embedding_roles_and_modal_embedding_catalog.md (Decision block)
cat tasks/done/134-voyage-4-embedding-upgrade.md
git diff -- apps/memory/src/tree/memory/pipeline.py
git diff -- apps/memory/src/tree/models/get_model.py
git diff -- apps/memory/tests/unit/models/test_get_model.py
git diff -- apps/memory/tests/unit/memory/test_pipeline.py
grep -n "def search_embedding_identity\|def llm_identity" -A 30 apps/memory/src/tree/models/get_model.py
grep -n "_INPUTS_NO_HEADERS" apps/memory/src/tree/memory/pipeline.py
grep -rn "llm_extract_entities_task\|summarise_cluster_task\|_llm_extract_entities(\|_summarise_cluster(" apps/memory --include="*.py" | grep -v tests/
grep -rn "\.submit(\|\.map(\|task_runner\|with_options" apps/memory/src/tree/memory/pipeline.py apps/memory/src/tree/orchestrator.py
grep -n "^def get_llm" -A 20 apps/memory/src/tree/models/get_model.py
grep -rn "opik_trace_headers" apps/memory/src/tree/memory/pipeline.py
make env-status
make memory-format-check
make memory-lint-check
make pre-commit
make memory-tests                                          # baseline: 3820 passed
git diff -- apps/memory/src/tree/memory/pipeline.py | shasum
git diff -- apps/memory/src/tree/models/get_model.py | shasum
# mutation (a): dropped `llm_identity=` kwarg at the extraction call site (Edit)
make memory-tests                                          # 7 failed, 3813 passed
# restored (Edit) — hash matched pre-mutation exactly
git diff -- apps/memory/src/tree/memory/pipeline.py | shasum
# mutation (b): gave `llm_identity: str` a default on both bodies (Edit x2)
make memory-tests                                          # 2 failed (test_task_body_requires_llm_identity x2), 3818 passed
# restored (Edit x2) — hash matched pre-mutation exactly
git diff -- apps/memory/src/tree/memory/pipeline.py | shasum
# mutation (c): froze `llm_identity()` behind a module-level cache (Edit)
make memory-tests                                          # 3 failed (test_reads_the_config_at_call_time, test_env_override_moves_the_identity, test_worker_passes_the_llm_identity_to_extraction), 3817 passed
# restored (Edit) — hash matched pre-mutation exactly
git diff -- apps/memory/src/tree/models/get_model.py | shasum
make memory-tests                                          # final: 3820 passed
make memory-format-check && make memory-lint-check
make pre-commit
git status --short
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:4200/api/health   # 200, already running
uv run python <scratch qa_151_prefect_probe.py>            # throwaway flow, FakeLLM, cluster_id=999151 (run from apps/memory, one-time — env vars already exported into the shell by direnv, no .env read)
grep -n -i "cache" apps/memory/README.md
TREE_MODELS__LLM__MODEL=gemini-2.5-flash uv run python -c "from tree.models.get_model import llm_identity; print(llm_identity())"   # fresh process
uv run python -c "from tree.models.get_model import llm_identity; print(llm_identity())"                                            # fresh process, no override
rm -f <scratch qa_151_prefect_probe.py>
grep -c "one cold LLM cache after merge: every document re-extracts once, every cluster re-summarises once" tasks/151-llm-identity-in-the-llm-task-cache-key.md   # -> 2
grep -rn "cache_policy=" apps/memory/src
```

One bare `uv run pytest -q tests/unit/memory/test_pipeline.py::TestLLMTaskCacheIdentity ... tests/unit/models/test_get_model.py::TestLLMIdentity` was run early, before I re-read AGENTS.md's "always via `make memory-*`" rule closely enough — it passed (14/14) but is **not** counted as evidence; every claim below is backed by a subsequent `make memory-tests` full-suite run instead (4 full runs total, ~50s each, one at a time, `make env-status` → local for every one).

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check`, `make memory-lint-check`, `make pre-commit`, all green, re-run after the mutation/restore cycle)
- Unit tests: 3820 passed / 0 failed (`make memory-tests`, confirmed baseline AND after full mutate-and-restore cycle)
- Integration tests: N/A — no integration suite by design (AGENTS.md)
- Warnings: 0 (no pytest warnings in the tail of any of the 4 full runs; the only warning anywhere is `opik`'s unrelated Pydantic-v1/3.14 `UserWarning` on interpreter start, pre-existing and outside this diff)

**E2E adversarial pass**
- Happy path: real local Prefect server (already running, `curl .../api/health` → 200), throwaway `qa151-probe-flow` around the REAL `summarise_cluster_task` with a `FakeLLM` double and invented `cluster_id=999151` samples → `Completed → Cached(type=COMPLETED) → Completed` across a `gemini→gemini→modal` identity sequence; `FakeLLM.call_count == 2` (cache HIT saved exactly the middle call). Matches the SWE's own claim, reproduced independently with different invented inputs. PASS.
- Break path 1 (non-vacuity / mutation — drop the kwarg at both call sites): removed `llm_identity=extraction_llm_identity` from the `llm_extract_entities_task(...)` call in `_run_extraction_worker_body`. `make memory-tests` → 7 failed (`test_worker_passes_the_llm_identity_to_extraction` + 6 tests that call the real un-mocked task and now hit `TypeError: missing 1 required keyword-only argument`). Expected: a loud failure, not a silent shared key — got it. Reverted via targeted `Edit`; `git diff | shasum` before/after mutation: `79e387a1568734463b0512aa147daea221f68443` both times. PASS.
- Break path 2 (non-vacuity / mutation — required→optional): added `= "gemini:gemini-3.1-flash-lite"` default to `llm_identity: str` on both `_llm_extract_entities` and `_summarise_cluster`. `make memory-tests` → exactly 2 failed, both `test_task_body_requires_llm_identity[llm-extract-entities|summarise-cluster]` (the `default is inspect.Parameter.empty` assertion). Expected: this exact pair fails — got it. Reverted; hash matched before/after: `79e387a1568734463b0512aa147daea221f68443`. PASS.
- Break path 3 (non-vacuity / mutation — call-time→frozen): cached `llm_identity()`'s return behind a module-level variable computed on first call. `make memory-tests` → 3 failed (`test_reads_the_config_at_call_time`, `test_env_override_moves_the_identity`, and — as a knock-on of my particular caching shape and pytest's execution order — `test_worker_passes_the_llm_identity_to_extraction`). Expected: at least the two call-time tests fail — got that plus one more, which only strengthens the non-vacuity claim. Reverted; hash matched before/after: `469f0dcf52454f48cfbcf85834d7faa63b031e90`. PASS.
- Break path 4 (state edge / call-time-read divergence, ADR risk #2 in the brief): grepped `apps/memory/src` for `.submit(`, `.map(`, `with_options`, `task_runner`, `functools.partial` → zero hits anywhere in `pipeline.py` / `orchestrator.py`. Every cached task is `await`ed directly, so it runs inline in the flow's own process/event loop — `get_llm()` (inside the task body) and `llm_identity()` (in the flow body, resolved once before the call) always read the SAME module-level `tree.config.app_config.app_config` singleton object. Divergence between "what `get_llm()` builds" and "what `llm_identity()` names" is impossible by construction in this codebase today, not merely unobserved. PASS (no defect).
- Break path 5 (hostile/completeness — is the identity string complete): traced `search_embedding_identity()` (`provider:model:dimensions:role`) — it likewise omits the Modal catalog `revision` field, so the LLM identity's omission of `revision` is a pre-existing, consistent bound across both #134/#136 and this task, not a new inconsistency. `temperature` (Modal pinned 0 / Gemini unpinned) is outside ADR-009 Decision 6's rule and outside every AC — pre-existing, not required. A prompt edit to `_llm_extract_entities` (no `prompt_version` input, unlike `_summarise_cluster`) would still replay stale JSON — pre-existing and explicitly called out as Out of scope in the task body ("A prompt-version input for `llm_extract_entities_task`... not asked for"). None of the three are FAILs; recorded as notes, not defects introduced by this diff.
- Break path 6 (adjacent-bug sweep): `grep -rn "cache_policy=" apps/memory/src` → 18 hits; every task NOT covered by #134/#151 uses `cache_policy=NO_CACHE` (`dream.py:721,728` included) — there is no other cached task anywhere in the repo that builds an LLM, so the SWE's "no other `get_llm()` caller is a cached Prefect task" claim is now evidence, not trust. PASS (no defect).

**Acceptance criteria**
- [x] PASS — `TestLLMIdentity::test_default_identity` → `llm_identity() == "gemini:gemini-3.1-flash-lite"`. Evidence: test passes in the 3820-green run; independently reproduced in a fresh `uv run python -c` process (no `_mock_app_config` fixture in play): `gemini:gemini-3.1-flash-lite`.
- [x] PASS — `test_reads_the_config_at_call_time` → `modal:LiquidAI/LFM2.5-350M` after a post-import patch. Evidence: test in the green run; independently reproduced (Break path 3) that freezing this call breaks exactly this test.
- [x] PASS — `test_env_override_moves_the_identity` → `TREE_MODELS__LLM__MODEL=gemini-2.5-flash` renders `gemini:gemini-2.5-flash`. Evidence: test in the green run; independently reproduced via a fresh `TREE_MODELS__LLM__MODEL=gemini-2.5-flash uv run python -c "..."` process → `gemini:gemini-2.5-flash`, vs. a fresh no-override process → `gemini:gemini-3.1-flash-lite`.
- [x] PASS — `TestLLMTaskCacheIdentity` (4 tests x 2 tasks = 8, parametrised) all green: differs across identities (provider flip AND same-provider model-id bump), ignores `opik_trace_headers`, `KEYWORD_ONLY` + `default is inspect.Parameter.empty` + real `TypeError`, not excluded from `cache_policy.exclude`. Evidence: `apps/memory/tests/unit/memory/test_pipeline.py:1640-1745`, green in `make memory-tests`; non-vacuity independently proven (Break paths 1-3).
- [x] PASS — `test_worker_passes_the_llm_identity_to_extraction`: worker under a patched `modal` LLM config calls the (mocked) task with `llm_identity="modal:LiquidAI/LFM2.5-350M"`. Evidence: green in the suite; Break path 1 shows this is exactly the test that fails without the wiring.
- [x] PASS — `test_summaries_pass_the_llm_identity_to_every_cluster`: 3 clusters → 3 calls, one shared identity. Evidence: green in the suite (`apps/memory/tests/unit/memory/test_pipeline.py:3649-3672`); independently reproduced the "one identity, resolved once" behaviour with a different invented `cluster_id` (999151) against the real local Prefect server.
- [x] PASS — `grep -rn "llm_extract_entities_task(\|summarise_cluster_task(" apps/memory/src` → 2 hits, both `llm_identity=`. Evidence: reproduced myself, identical result; further widened to `apps/memory` (incl. `tests/`) and to `scripts/deploy/tutorials/docker` — no call site anywhere outside `pipeline.py` and the updated `tests/`.
- [x] PASS — every pre-existing test of the two tasks stays green, incl. `cache_expiration == timedelta(days=90)`, `retries == 2`, task names. Evidence: all present in the 3820-passed run; none touched by the diff (`git diff` shows only the additive kwarg/docstring hunks).
- [x] PASS — `## Log` and the commit message carry the consequence sentence. Evidence for the `## Log` half: `grep -c "one cold LLM cache after merge: every document re-extracts once, every cluster re-summarises once" tasks/151-llm-identity-in-the-llm-task-cache-key.md` → `2` (present in both the SWE's "Consequence" section and this Tester entry). **The commit-message half is UNVERIFIED and CANNOT be verified before a commit exists** — nothing is committed yet. This is carried forward as a named, gating obligation on whoever writes the commit message: the exact sentence `one cold LLM cache after merge: every document re-extracts once, every cluster re-summarises once` MUST be in the commit body, or this AC silently regresses after my PASS.
- [x] PASS — `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green on `make env-status` → local. Evidence: reproduced 4 separate times across the mutation cycle, always 3820 passed / 0 failed, 0 warnings, all four gate commands green each time.

**Evidence**
```
$ make memory-tests   (baseline, before any mutation)
3820 passed in 48.71s

$ make memory-tests   (after mutation (a): dropped kwarg at extraction call site)
FAILED tests/unit/memory/test_pipeline.py::TestFlowEmbeddingModelSplit::test_worker_passes_the_llm_identity_to_extraction
FAILED tests/unit/memory/test_pipeline.py::TestWorkerRowShape::test_only_children_carry_a_vector[graphrag]
FAILED tests/unit/memory/test_pipeline.py::TestWorkerRowShape::test_hierarchy_columns_and_denormalised_properties[graphrag]
FAILED tests/unit/memory/test_pipeline.py::TestWorkerGraphragMode::test_writes_part_of_and_next_edges_at_both_levels
FAILED tests/unit/memory/test_pipeline.py::TestWorkerGraphragMode::test_calls_the_llm_once_per_parent_with_the_parent_content
FAILED tests/unit/memory/test_pipeline.py::TestWorkerGraphragMode::test_writes_the_same_rag_rows_as_rag_mode
FAILED tests/unit/memory/test_pipeline.py::TestWorkerGraphragMode::test_rerunning_the_same_document_is_idempotent
7 failed, 3813 passed in 49.23s

$ make memory-tests   (after mutation (b): default value on both bodies)
FAILED tests/unit/memory/test_pipeline.py::TestLLMTaskCacheIdentity::test_task_body_requires_llm_identity[llm-extract-entities]
FAILED tests/unit/memory/test_pipeline.py::TestLLMTaskCacheIdentity::test_task_body_requires_llm_identity[summarise-cluster]
2 failed, 3818 passed in 49.35s

$ make memory-tests   (after mutation (c): frozen module-level cache)
FAILED tests/unit/memory/test_pipeline.py::TestFlowEmbeddingModelSplit::test_worker_passes_the_llm_identity_to_extraction
FAILED tests/unit/models/test_get_model.py::TestLLMIdentity::test_reads_the_config_at_call_time
FAILED tests/unit/models/test_get_model.py::TestLLMIdentity::test_env_override_moves_the_identity
3 failed, 3817 passed in 49.58s

$ make memory-tests   (final, after full restore)
3820 passed in 49.72s

$ git status --short   (throughout the mutation cycle and at the end)
 M apps/memory/src/tree/memory/pipeline.py
 M apps/memory/src/tree/models/get_model.py
 M apps/memory/tests/unit/memory/test_pipeline.py
 M apps/memory/tests/unit/models/test_get_model.py
 M tasks/151-llm-identity-in-the-llm-task-cache-key.md

Independent Prefect-level probe (throwaway flow, real local server, FakeLLM):
20:43:40.653 | Task run 'summarise-cluster-b9f' - summarise_cluster: llm=gemini:gemini-3.1-flash-lite cluster_id=999151 n_samples=2 prompt_version=v1
20:43:40.658 | Task run 'summarise-cluster-b9f' - Finished in state Completed()
20:43:40.661 | Task run 'summarise-cluster-ad1' - Finished in state Cached(type=COMPLETED)
20:43:40.663 | Task run 'summarise-cluster-3dd' - summarise_cluster: llm=modal:LiquidAI/LFM2.5-350M cluster_id=999151 n_samples=2 prompt_version=v1
20:43:40.665 | Task run 'summarise-cluster-3dd' - Finished in state Completed()
FakeLLM call_count (expect 2 — run1 real, run2 cached, run3 real): 2

Independent fresh-process env-override check:
$ TREE_MODELS__LLM__MODEL=gemini-2.5-flash uv run python -c "..." -> gemini:gemini-2.5-flash
$ uv run python -c "..." (no override)                          -> gemini:gemini-3.1-flash-lite
```

**Other issues found**
- None that rise to a defect. Three notes recorded above (Modal `revision` / temperature / prompt-version omissions from the LLM identity) are pre-existing, consistent with #134/#136's own bounds, and explicitly out of scope for this task — not blocking.
- Side effects disclosed: my own throwaway `qa151-probe-flow` added 1 flow run + 3 task runs + cache records to the ALREADY-RUNNING local Prefect server, and one Opik trace under `tree-memory` — same shape of side effect the SWE's own proof left, on top of it. Nothing touched the `memory` MongoDB collection; no `modal` command was run; no Gemini/real-Modal token was spent (FakeLLM double throughout).
- Gating obligation for the orchestrator/committer: the exact sentence `one cold LLM cache after merge: every document re-extracts once, every cluster re-summarises once` must appear in the commit message, per AC 9's second half, which cannot be checked before a commit exists.

**VERDICT: PASS**
