---
id: 151-llm-identity-in-the-llm-task-cache-key
status: pending
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

- [ ] `tests/unit/models/test_get_model.py::TestLLMIdentity::test_default_identity`: `llm_identity() == "gemini:gemini-3.1-flash-lite"` on the shipped config.
- [ ] `TestLLMIdentity::test_reads_the_config_at_call_time`: with `app_config.models.llm` patched to `LLMConfig(provider="modal", model="LiquidAI/LFM2.5-350M")` AFTER import (the style of `TestSearchEmbeddingIdentity::test_reads_the_config_at_call_time`, `:416-430`) -> `"modal:LiquidAI/LFM2.5-350M"` — i.e. nothing was frozen at import time.
- [ ] `TestLLMIdentity::test_env_override_moves_the_identity`: a config loaded with `TREE_MODELS__LLM__MODEL=gemini-2.5-flash` in the environment (through `_apply_env_overrides`, the way the existing `app_config` override tests do it) renders `"gemini:gemini-2.5-flash"`.
- [ ] `tests/unit/memory/test_pipeline.py::TestLLMTaskCacheIdentity`, parametrised over `llm_extract_entities_task` and `summarise_cluster_task`, modelled on `TestEmbedTaskCacheIdentity` (`:1532-1604`):
  - `test_cache_key_differs_across_llm_identities`: `cache_policy.compute_key(...)` with identical other inputs and `llm_identity="gemini:gemini-3.1-flash-lite"` vs `"modal:LiquidAI/LFM2.5-350M"` -> two DIFFERENT keys; also different for the same provider with two model ids.
  - `test_cache_key_ignores_opik_trace_headers`: same inputs + same `llm_identity`, two different `opik_trace_headers` dicts -> EQUAL keys.
  - `test_task_body_requires_llm_identity`: `inspect.signature` of the body shows `llm_identity` as `KEYWORD_ONLY` with `default is inspect.Parameter.empty`; calling the body without it raises `TypeError`.
  - `test_llm_identity_is_not_excluded_from_the_key`: `"llm_identity" not in (task.cache_policy.exclude or [])`.
- [ ] `test_worker_passes_the_llm_identity_to_extraction`: with `llm_extract_entities_task` patched, a worker run under `models.llm = {provider: "modal", model: "LiquidAI/LFM2.5-350M"}` (no real model built — factories patched) calls it with `llm_identity="modal:LiquidAI/LFM2.5-350M"`.
- [ ] `test_summaries_pass_the_llm_identity_to_every_cluster`: `_summarise_clusters` over 3 clusters calls the patched `summarise_cluster_task` 3 times, each with the same `llm_identity == llm_identity()`.
- [ ] `grep -rn "llm_extract_entities_task(\|summarise_cluster_task(" apps/memory/src` -> every call carries `llm_identity=`; every pre-existing direct call of `_llm_extract_entities(` / `_summarise_cluster(` in `tests/` is updated to pass it (no default was added to spare them).
- [ ] Every pre-existing test of the two tasks stays green, incl. `summarise_cluster_task.cache_expiration == timedelta(days=90)`, `retries == 2` and the task names `llm-extract-entities` / `summarise-cluster`.
- [ ] `## Log` and the commit message carry the sentence "one cold LLM cache after merge: every document re-extracts once, every cluster re-summarises once".
- [ ] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green (LOCAL env).

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
