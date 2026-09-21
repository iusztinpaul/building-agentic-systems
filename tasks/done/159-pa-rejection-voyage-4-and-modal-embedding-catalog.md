---
id: 159-pa-rejection-voyage-4-and-modal-embedding-catalog
status: done
feature: voyage-4-and-modal-embedding-catalog
---

# [PA rejection] voyage-4-and-modal-embedding-catalog — the README's "Serving models on Modal" journey: two silent traps when switching `models.*` to `provider: modal`, a split verdict list, no embedding-entry example, the Pre-warm is undocumented

Tags: `rollup`, `pa-rejection`, `memory`, `modal`, `docs`
Refs: `tasks/done/134-…158` (the feature's Tasks Plan), ADR-009 rev 7, PR #44

## Scope

The feature PASSED automated QA on every task (134-158) and the four auto-routed cycles are
proven live in `tasks/done/141`. The code-level operator experience is good and is NOT in
question: every failure the PA walked names the next step (unknown `MODEL` lists both catalog
groups and the file to edit; the guard refusal names `-stop` and `FORCE=yes`; a 401/403 poll
names the two `.env` variables; "not deployed" prints the deploy command; a timeout names
`modal.request_timeout_s` and the knob to turn; a wrong `native_dimensions` says
`expected N dims, got M`).

It failed the user-perspective review on ONE surface: `apps/memory/README.md` "Serving models
on Modal" (lines 550-750) — the only document an operator reads before switching `models.*`
to `provider: modal`. Two of its instructions lead to a run that SILENTLY does the wrong thing,
its routing-verdict list is split in two by an inserted section, and two of the human's
headline requirements ("easily deploy another HF model" for embeddings; the warm-up before a
pipeline run) cannot be found in it. The SWE fixes every issue below in a single coordinated
pass, then hands back to the Tester.

This is a docs pass plus ONE reuse of an existing helper. It changes no ADR-009 decision, no
glossary term and no Modal code path. This task implements ADR-009 (no new ADR).

**Removing over adding (CLAUDE.md).** The section is 201 lines and already carries incident
narrative an operator does not need to act. Pay for every added sentence by cutting one:
the section (from `## Serving models on Modal (optional)` up to `## Testing`) must be NO
LONGER after this task than before it (<= 201 lines). Candidates to cut or shrink — the
forensic paragraph of 1a (`with it on, Qwen/Qwen3.5-0.8B spent … tasks/141 round 2`): keep the
rule "every Modal chat request is JSON mode", drop the story, ADR-009 §10 holds it; the SGLang
image / speculative-decoding paragraph: keep "model-specific flags go in `extra_server_args`",
drop the rest; "which is how two accidental deploys once happened". *Good:* "`-test` prints
`expected 1024 dims, got 2048` — write the second number". *Bad:* a new "Background" subsection.

**HARD SAFETY RULE (unchanged from #141/#158):** no agent runs `make memory-deploy-model*`
(not even `DRY_RUN=yes` is needed here), `scripts/modal_model.py`, or any `modal …` command
for this task; never open, read or `source` `.env` / `.env.prod`; no pipeline run is required.

## Acceptance Criteria

- [x] Issue 1: in `apps/memory/README.md` the three routing verdicts — **Accepted**, **Refused**, **Any other failure** — are three CONSECUTIVE bullets of one list under step 1, with no heading or paragraph between them; "1a. Serving your own LLM" comes after the list. Check: between the line starting `- **Accepted**` and the line starting `- **Any other failure**` there is no line starting `**1a.`.
- [x] Issue 2: step "4. Point the memory at it" says that changing `models.search_embedding` to a Modal model requires the **Embedding reset** and links to `#changing-the-embedding-model` (the four commands are NOT repeated). It states the one exception in <= 1 sentence or not at all (`models.resolution_embedding` is transient and needs no reset). Check: `awk '/^\*\*4\. Point the memory/,/^## Testing/' apps/memory/README.md | grep -c "changing-the-embedding-model"` >= 1.
- [x] Issue 3 (docs): the same step says WHERE a `TREE_MODELS__…` / `TREE_MODAL__…` override must be set for a PIPELINE run — in the process that runs the flow (the shell running `make memory-serve-workflows` locally, restarted; the deployment's environment on Prefect Managed) — and that on a `make memory-run-*` command line it is a silent no-op. It keeps saying that the `make memory-deploy-model-test … TREE_MODAL__WARMUP_DEADLINE_S=1200` form DOES work (that command runs in-process). Reuse the wording of the clustering note (README "Small corpora", ~line 329) — link to it rather than restating it if that is shorter.
- [x] Issue 3 (code): `scripts/run_memory_pipeline.py`, `scripts/run_pipeline.py` and `scripts/run_indexing_pipeline.py` call the EXISTING `tree.cli.warn_ignored_config_overrides` for the prefixes `TREE_MODELS__` and `TREE_MODAL__` before dispatching, exactly as `scripts/run_clustering_pipeline.py:59` does for its prefix (add the same two calls there too). No new helper, no new message text. Unit tests (one per script, in the existing `tests/unit/scripts/test_run_*_pipeline.py` files; the helper itself is already covered by `tests/unit/test_cli.py` — do not re-test its message): with `TREE_MODELS__LLM__PROVIDER=modal` in the environment the WARNING names that variable and the run still dispatches; with no such variable nothing is logged.
- [x] Issue 4: the README shows ONE `embedding_models:` YAML entry for a model that is NOT a seed (placeholder repo id, e.g. `<org>/<embedding-model>`) with exactly the fields an operator must fill — `repo_id`, `revision`, `native_dimensions`, `matryoshka_dimensions`, `query_prompt`, `document_prompt` — and, in <= 3 sentences total, says where each non-obvious value comes from: `revision` = the commit sha on the model's Hub page; the prompts = the model card's (empty string when the card has none); `native_dimensions` = the width the server returns, NOT `hidden_size` — when unsure, deploy and run `make memory-deploy-model-test`: a wrong value fails with `expected <yours> dims, got <served>` (quote the real message from `tree.models.modal_server.smoke_test`). It points at the two seeds in `configs/default.yaml` for the App fields instead of listing them again.
- [x] Issue 5: the `llm_models:` YAML example in 1a matches the committed `LiquidAI/LFM2.5-350M` seed field for field (no `chat_template_kwargs` on it — that knob belongs to the thinking model `Qwen/Qwen3.5-0.8B`, and the prose already says so), and the sentence counting its lines is either correct or gone. Check: the example block contains no `enable_thinking`.
- [x] Issue 6: step 4 says, in <= 3 sentences, what a PIPELINE run does once a `models.*` block is `provider: modal`: every distinct Modal server is **Pre-warm**ed concurrently before document 1 (extraction worker, dream consolidation, cluster summaries), the log shows `Pre-warming N Modal server(s): …` then the `Still cold (HTTP 503) … — 38s/600s` / `Warm: … after 113s` lines, a server that never warms fails the run with ZERO documents attempted, and the MCP server is deliberately NOT pre-warmed — so the first `search_memory` after ~5 idle minutes waits for the boot. Uses the glossary terms **Pre-warm** and **Warm gate** verbatim. Check: `grep -c "Pre-warm" apps/memory/README.md` >= 1.
- [x] Issue 7: the three places that say a Proxy token fronts "an embedding model" say "a model" / "embedding models and LLMs": `README.md:43`, and the `MODAL_PROXY_TOKEN_ID` row of the env table (`apps/memory/README.md:92`). Step 2 (or the line above step 1) names the OTHER credential in one clause: the Modal CLI must be logged in once (`uv --directory apps/memory run modal setup`) before any `make memory-deploy-model*` — distinct from the Proxy token, as the glossary's **Proxy token** entry already says.
- [x] Issue 8: PR #44's body no longer says the Embedding reset has "`DRY_RUN` support" / "`DRY_RUN=yes` … on the embedding reset": the reset is a dry run BY DEFAULT and `CONFIRM=yes` applies it (`DRY_RUN=yes` belongs to the deploy driver only). Follow-up 1 reads "once per user". (`gh pr edit 44 --body …` — orchestrator or SWE; no code.)
- [x] The section from `## Serving models on Modal (optional)` to `## Testing` in `apps/memory/README.md` is <= 201 lines (`awk '/^## Serving models on Modal/,/^## Testing/' apps/memory/README.md | wc -l` <= 202 counting the closing heading).
- [x] No operator-facing doc regains a retired promise: `grep -rn "0\.75\|MODAL_EMBEDDING_API_KEY\|deploy-embedding-model\|serving:" README.md apps/memory/README.md .env.example .agents/skills/run-pipelines-e2e/SKILL.md` returns only the existing "There is no `serving:` field" sentence.
- [x] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green (env `local`).
- [x] Tester re-runs full QA suite and PASSES.
- [x] PA re-runs acceptance review on the feature and ACCEPTS.

## Issues (detail)

### 1. The routing-verdict list is cut in two — `apps/memory/README.md:575-637`
- **What the user experiences (wrong):** step 1 promises the driver's decision and starts a list: **Accepted** (575), **Refused** (577). Then "1a. Serving your own LLM" (589) runs for 45 lines — a YAML block, the JSON-mode story, the SGLang image — and the third verdict, **Any other failure** (634), appears glued to the end of the SGLang flags paragraph ("…so it does not." / "- **Any other failure**"), where it reads as a bullet about SGLang. The one verdict that tells the operator "the deploy ABORTS, this is not a fallback" is the one they will not find.
- **What good UX implies (right):** the three outcomes of ONE command are one list.
- **Suggested fix:** move the bullet (and its `SERVING=` paragraph if it reads better there) up under **Refused**; 1a follows.

### 2. Switching `models.search_embedding` to a Modal model silently corrupts retrieval — `apps/memory/README.md:732-739`
- **What the user experiences (wrong):** the operator follows step 4 to the letter — `{provider: modal, model: Qwen/Qwen3-Embedding-0.6B, dimensions: 1024}` — and everything "works": same 1024-d index, no error, no warning. Every stored vector is still voyage-4; every new question is embedded by Qwen. ADR-009 Consequences says it plainly: "scores are meaningless and `min_vector_score` may hide everything … Nothing detects a forgotten migration." The cure exists and is documented — 470 lines earlier, under "Changing the embedding model" (265) — but step 4, the instruction that CAUSES the need, never mentions it.
- **What the spec implies (right):** glossary **Embedding reset**: "The migration for ANY change that makes persisted vectors incomparable — a new embedding model". The step that changes the model must point at it.
- **Suggested fix:** one sentence + the anchor link in step 4. Do not repeat the four commands.

### 3. "For one run without editing the file" is a silent no-op for every pipeline run — `apps/memory/README.md:744-745`
- **What the user experiences (wrong):** the operator types `TREE_MODELS__LLM__PROVIDER=modal TREE_MODELS__LLM__MODEL=LiquidAI/LFM2.5-350M make memory-run-memory-pipeline`. The command only DISPATCHES; the flow runs in the `make memory-serve-workflows` process and "forwards no environment" (`tree.cli.warn_ignored_config_overrides`'s own docstring). The run extracts with Gemini, bills Gemini, and the operator believes they evaluated their Modal LLM. The same trap sits under the client's own advice `raise TREE_MODAL__REQUEST_TIMEOUT_S` (`modal_llm.py:393-398`, `modal_embedding.py:313-317`). The clustering docs already solved this exact problem (README ~329: "set it where the FLOW runs, not where you type `make` … a silent no-op (the command warns when it sees one)") — but only `run_clustering_pipeline.py` calls the warning helper, and only for `TREE_MEMORY__CLUSTERING__`.
- **What good UX implies (right):** the README says where the override goes; the dispatching command warns when it sees one it will ignore — the existing pattern, extended to the two prefixes this feature made operator-facing.
- **Suggested fix:** the docs sentence + `warn_ignored_config_overrides("TREE_MODELS__")` / `("TREE_MODAL__")` in the dispatch scripts. Bias to least: reuse the helper as is; do NOT forward environment through `run_deployment`, do NOT add a flag.

### 4. "Easily deploy another HF model" has no embedding example — `apps/memory/README.md:558-563`, `:75-76`
- **What the user experiences (wrong):** the human's requirement 2 is about EMBEDDING models, yet the only YAML example in the section is an LLM (1a). For an embedding model the README offers a field list in a parenthesis. The operator is left to guess the one field that was already guessed wrong once by this project (`native_dimensions` for voyage-4-nano was written as 1024 from `hidden_size`; it serves 2048 — ADR-009 "The catalog can be wrong about a model — it already was"). The smoke test would tell them the right number in one line (`expected 1024 dims, got 2048`), but nothing says "run it and read the number".
- **What good UX implies (right):** one copy-paste entry with placeholders, and the one-line recipe for the value nobody can read off a model card.
- **Suggested fix:** a 7-line YAML block in step 1 + <= 3 sentences; point at the seeds for App fields and `extra_server_args`.

### 5. The LLM example is not the seed it claims to be — `apps/memory/README.md:591-602`
- **What the user experiences (wrong):** "the entry is five YAML lines" above a seven-line block, which puts `chat_template_kwargs: { enable_thinking: false }` on `LiquidAI/LFM2.5-350M` — a non-thinking model whose committed seed (`configs/default.yaml`) has no such key — while the next paragraph explains the knob exists for the thinking model `Qwen/Qwen3.5-0.8B`. An operator copying the block sends a template kwarg their model's chat template may not define.
- **Suggested fix:** mirror the seed; mention `chat_template_kwargs` only in the sentence about Qwen.

### 6. The warm-up before a pipeline run (requirement 7) is not in the README — `apps/memory/README.md:706-721`
- **What the user experiences (wrong):** the README explains the poller only for `make memory-deploy-model-test`. An operator who flips `models.llm` to `modal` and starts a run sees it sit for 2-10 minutes before document 1 and has no page that says this is the **Pre-warm** — nor that a dead server now costs zero documents instead of N failed ones, nor that the MCP server is deliberately not pre-warmed, so their first `search_memory` after an idle period blocks for the boot (ADR-009 Consequences: "inside an MCP tool call for the query path, where nothing is pre-warmed on purpose"). `grep -ci "pre-warm" apps/memory/README.md` → 0; the glossary has the term, the operator docs do not.
- **Suggested fix:** <= 3 sentences in step 4, quoting the three log lines.

### 7. Docs still call the Proxy token an embedding-only credential; the CLI login is never mentioned — `README.md:43`, `apps/memory/README.md:92`, `:678-683`
- **What the user experiences (wrong):** "only if you want to serve an embedding model on Modal" / "the only auth in front of a Modal-hosted embedding model" — the feature's second half is LLMs behind the same token. Separately, a first-time operator who mints the Proxy token as told and runs `make memory-deploy-model` is refused by the Modal CLI: the README never says the CLI needs its own one-time login, a distinction the glossary already draws.
- **Suggested fix:** two word-level edits + one clause.

### 8. PR #44's body misdescribes the reset's safety switch — PR description
- **What the user experiences (wrong):** "`make memory-reset-embeddings` … (with `DRY_RUN` support)" and "`DRY_RUN=yes` on the deploy driver and on the embedding reset". The reset has no `DRY_RUN`: it is a dry run by default and `CONFIRM=yes` applies it. The post-merge follow-up is correct (`CONFIRM=yes`) but omits "once per user".
- **Suggested fix:** edit the PR body; no repo change.

## User Stories

(Inherit from the original tasks — no new stories. Re-verify the README-facing ones after the fix, on paper, with safe commands only:)

### Story: Operator adds a new Hugging Face embedding model by reading only the README
1. Operator opens `apps/memory/README.md` "Serving models on Modal", step 1.
2. Operator copies the `embedding_models:` example, fills `repo_id` and `revision` from the Hub page, the two prompts from the model card, and guesses `native_dimensions: 1024`.
3. Operator reads that `-test` reports the served width, and that a wrong value fails with `expected 1024 dims, got <served>`.
4. Operator reads all three routing verdicts in one list, including that an unknown failure aborts.

### Story: Operator switches the search embedding to a Modal model and does not lose retrieval
1. Operator reads step 4 and sets `models.search_embedding: {provider: modal, model: Qwen/Qwen3-Embedding-0.6B, dimensions: 1024}`.
2. The same step tells them the stored vectors are now incomparable and links to "Changing the embedding model".
3. Operator follows the link: dry run, `CONFIRM=yes`, indexing, clustering.

### Story: Operator tries a Modal LLM for one run with an env override
1. Operator runs `TREE_MODELS__LLM__PROVIDER=modal TREE_MODELS__LLM__MODEL=LiquidAI/LFM2.5-350M make memory-run-memory-pipeline`.
2. The command logs ONE WARNING naming both variables and saying the override is IGNORED for this run because the flow reads its config where it runs; the run still dispatches.
3. Operator reads step 4, exports the two variables in the shell running `make memory-serve-workflows`, restarts it, and re-runs.
4. The run log shows `Pre-warming 1 Modal server(s): ep-tree-lfm2-5-350m`, `Still cold (HTTP 503) …` lines, then `Warm: … after <N>s`, before document 1 — as step 4 said it would.

---

Refs: `tasks/done/134-…158`, ADR-009 rev 7, PR #44

## Log

### [PA] 2026-09-21 23:45 — Grooming (rollup from the feature acceptance review)

**Summary**
Docs-and-one-helper pass on the README's "Serving models on Modal" journey: two silent traps in step 4, a split verdict list, a missing embedding-entry example, an undocumented Pre-warm, three wording slips and a PR-body correction.

**Key decisions**
- Reuse `tree.cli.warn_ignored_config_overrides` as is for `TREE_MODELS__` / `TREE_MODAL__` — no environment forwarding, no new flag (bias to least).
- The README section may not grow: every added sentence is paid for by a cut (CLAUDE.md "prioritize removing").
- No ADR change, no glossary change: every term used (**Pre-warm**, **Warm gate**, **Embedding reset**, **Proxy token**, **Modal catalog**, **Serving path**) is already canonical.

**Dependencies**
- None.

**User stories**
- 3 stories covering: adding an embedding model from the README alone; switching the search embedding without losing retrieval; the env-override trap and the visible Pre-warm.

Ready for implementation.

### [SWE] 2026-09-22 00:40 — Implementation

**Files modified**
- `apps/memory/README.md` — "Serving models on Modal" rewritten in one pass (issues 1-7) + the `MODAL_PROXY_TOKEN_ID` env-table row.
- `README.md` — line 43: the Proxy token fronts "a model (an embedding model or an LLM)".
- `apps/memory/scripts/run_memory_pipeline.py`, `run_pipeline.py` (both `_run_offline` and `_run_online`), `run_indexing_pipeline.py`, `run_clustering_pipeline.py` — two `warn_ignored_config_overrides("TREE_MODELS__")` / `("TREE_MODAL__")` calls before dispatch, mirroring `run_clustering_pipeline.py:59`. No new helper, no new message text.
- `apps/memory/tests/unit/scripts/test_run_{memory,indexing,,clustering}_pipeline.py` — one warn test + one clean-shell test per script (clustering keeps its `TREE_MEMORY__CLUSTERING__` pair and gains the model-override one).
- PR #44 body (no repo change) — issue 8.

**Tests**
- Unit: 4063 passing, 0 failing (`make memory-tests`, env `local`); baseline was 4056, +7 new tests.
- Integration: N/A — the project has no integration suite (AGENTS.md).

**Acceptance criteria**
- [x] Issue 1 — `awk '/^- \*\*Accepted\*\*/,/^- \*\*Any other failure\*\*/' apps/memory/README.md | grep -c '^\*\*1a\.'` → `0`; the three verdicts are `README:573-586`, `**1a.` now at `:611`.
- [x] Issue 2 — `awk '/^\*\*4\. Point the memory/,/^## Testing/' … | grep -c "changing-the-embedding-model"` → `1`; the four reset commands are NOT repeated; the exception is one sentence ("`models.resolution_embedding` is transient and needs no reset").
- [x] Issue 3 (docs) — `README:736-741`, wording lifted from the clustering "Small corpora" note (restated, not linked: a cross-section link from Modal to clustering read worse and was not shorter). `TREE_MODAL__WARMUP_DEADLINE_S=1200` on `-test` still documented as working, now with the reason ("this target runs the code IN this process") at `:700-702`.
- [x] Issue 3 (code) — verified by `tests/unit/scripts/test_run_memory_pipeline.py::TestRunMemoryPipelineIgnoredOverrides::{test_it_warns_when_a_model_override_is_set_in_this_shell,test_a_clean_shell_dispatches_without_a_warning}`, and the same pair in `test_run_indexing_pipeline.py::TestRunIndexingPipelineIgnoredOverrides`, `test_run_pipeline.py::TestRunPipelineIgnoredOverrides`, plus `test_run_clustering_pipeline.py::TestRunClusteringPipeline::test_it_warns_when_a_model_override_is_set_in_this_shell`.
- [x] Issue 4 — `README:592-609`: a 7-line `embedding_models:` entry with `<org>/<embedding-model>` + 3 sentences; the `expected 1024 dims, got 2048 — the Modal catalog's native_dimensions is wrong for this Serving path …` message is quoted from `tree.models.modal_server` (`modal_server.py:258`).
- [x] Issue 5 — `README:615-625` mirrors the `LiquidAI/LFM2.5-350M` seed field for field (`repo_id`, `revision`, `gpu`, `n_gpus`, `cpu`, `memory_mb`, `max_model_len`, `max_tokens`); no `enable_thinking` in the block; the line-count sentence is gone ("the entry mirrors the committed seed").
- [x] Issue 6 — `README:743-749`: 2 sentences, **Pre-warm** and **Warm gate** verbatim, the three log lines quoted from `get_model.py:292` / `modal_warmup.py:165,173`. `grep -c "Pre-warm" apps/memory/README.md` → `2`.
- [x] Issue 7 — `README.md:43`, `apps/memory/README.md:92`; the Modal CLI login clause is in step 2 (`:670-673`).
- [x] Issue 8 — `gh pr edit 44 --body-file …`; three lines changed (the two `DRY_RUN` sentences + "once per user"), attribution line kept.
- [x] Section length — `awk '/^## Serving models on Modal/,/^## Testing/' apps/memory/README.md | wc -l` → **203 before, 202 after** (limit 202).
- [x] Retired promises — the grep returns only `apps/memory/README.md:561: There is no `serving:` field`.
- [x] `make memory-format-check`, `make memory-lint-check`, `make pre-commit`, `make memory-tests` — green, run one at a time, `make env-status` → local.

**Evidence**
```
$ make memory-tests
============================ 4063 passed in 52.71s =============================

$ awk '/^## Serving models on Modal/,/^## Testing/' apps/memory/README.md | wc -l
202          # was 203 on the parent commit

$ TREE_MODELS__LLM__PROVIDER=modal TREE_MODAL__REQUEST_TIMEOUT_S=600 uv --directory apps/memory run python -c "…; warn_ignored_config_overrides('TREE_MODELS__'); warn_ignored_config_overrides('TREE_MODAL__')"
TREE_MODELS__LLM__PROVIDER set in THIS shell, but the flow reads its config where it RUNS — the shell running `make memory-serve-workflows` locally, the deployment's environment on Prefect Managed. Dispatch forwards no environment, so the override is IGNORED for this run.
TREE_MODAL__REQUEST_TIMEOUT_S set in THIS shell, but the flow reads its config where it RUNS — …

$ make pre-commit
prettier / ruff check / ruff format / biome check (harness) .......... Passed
```

**Notes**
- RED first: the four warn tests failed on `assert any(... in message ...)` (assertion, not ImportError) before the calls were added; green after.
- `run_pipeline.py` gets the pair in BOTH `_run_offline` and `_run_online` (it has no single dispatch coroutine). The unit test covers the offline path; the online path is the same three lines.
- Every clean-shell test now deletes any `TREE_MODELS__` / `TREE_MODAL__` (and, for clustering, `TREE_MEMORY__CLUSTERING__`) name it finds in `os.environ` before asserting silence — `make` exports `.env`, so `caplog.records == []` would otherwise depend on an operator's local file.
- Cuts that paid for the additions (CLAUDE.md "prioritize removing"): the 1a JSON-mode forensic story (kept the rule, dropped `tasks/141` round 2), the SGLang image / speculative-decoding paragraph, "which is how two accidental deploys once happened", the `Routing … LiquidAI` sample line, and light compression of the HF_TOKEN / poller / provisioning paragraphs. Nothing actionable was dropped: the endpoint min/max-containers note moved into the provisioning paragraph.
- Section length, both readings: content lines (`## Serving models on Modal` → the last line before the blank that precedes `## Testing`) **201 → 200**; the AC's `awk … | wc -l`, which also counts that blank and the `## Testing` heading, **203 → 202**. Both caps met.
- Adjacent, NOT fixed (out of scope — AC 3 names exactly four scripts): `scripts/run_dream_consolidation.py` dispatches the same way and is a **Pre-warm** site, so the same `TREE_MODELS__` / `TREE_MODAL__` silent no-op applies there. Suggest a follow-up task rather than widening this one.
- NOT RUN (safety rule): no `modal …` command, no `make memory-deploy-model*`, no pipeline run. The dispatch change is proven by unit tests plus the direct-call transcript above.

**Extension — 2026-09-22 01:25 (closes the "Adjacent, NOT fixed" note above)**

- `apps/memory/scripts/run_dream_consolidation.py` — the same two `warn_ignored_config_overrides("TREE_MODELS__")` / `("TREE_MODAL__")` calls with the same comment, at the top of `_run()` before `async with get_client()` (the analogue of "before `connect_and_resolve_user`"); new `from tree.cli import warn_ignored_config_overrides`. Glue only; `init_logger()` stays at module level.
- `apps/memory/tests/unit/scripts/test_run_dream_consolidation.py` — NEW module (the script had none): `TestRunDreamConsolidationIgnoredOverrides::{test_it_warns_when_a_model_override_is_set_in_this_shell,test_a_clean_shell_dispatches_without_a_warning}`, mirroring `test_run_memory_pipeline.py`. Prefect's `get_client` is mocked with an already-final+completed run so the poll loop exits on its first pass.
- RED first: the warn test failed on `assert any(...)` (assertion, not ImportError); green after the two calls. The clean-shell test passed before the change, as expected — it asserts silence. `caplog.records == []` holds verbatim here even though this `_run` logs three INFO lines of its own, because `caplog.at_level(WARNING, logger="tree.cli")` also raises the capture handler's level.
- Unit: **4065 passing, 0 failing** (`make memory-tests`, env `local`; 4063 → +2). `make memory-format-check`, `make memory-lint-check`, `make pre-commit` green, run one at a time.
- Survey (closed; `grep -iln "deployment\|flow_run\|prefect" apps/memory/scripts/*.py` returns only these + the four already fixed — `hook_session_end.py`, `serve_mcp.py`, `scrape_web.py` do not dispatch, `visualize_embeddings.py` only says it needs no Prefect). Two dispatchers still have no warning, both **reported, NOT fixed**:
  - `run_data_pipeline.py` (`dispatch_offline_pipeline` / `dispatch_online_pipeline`) — would take the identical 2-line pattern (precedent: `run_pipeline.py` got it in both `_run_offline` and `_run_online`). Out of this task's AC 3, which names exactly four scripts, so left alone. **Strongest follow-up candidate:** the no-op is real, not theoretical — the data flow reads `app_config.models.search_embedding.dimensions` at boot for the live-index gate (`tree/data/offline_pipeline.py:237-243`), so `TREE_MODELS__SEARCH_EMBEDDING__DIMENSIONS=… make memory-run-data-pipeline` is silently ignored against a real gate.
  - `search_web.py` — fires the ingest deployment fire-and-forget through the domain helper `trigger_url_batch_ingest`, not `run_deployment` and not the shared dispatch helper; not the same pattern.
- Adjacent, NOT fixed: this script's behaviour is gated by `app_config.dream.dry_run`, so `TREE_DREAM__DRY_RUN=… make memory-run-dream-consolidation` is a silent no-op too — `run_clustering_pipeline.py` sets the precedent for a third, domain-specific prefix. Suggest a follow-up task; the instruction here was the same two prefixes.
- NOT RUN (safety rule): no `modal …`, no `make memory-deploy-model*`, no pipeline run; nothing committed.

### [Tester] 2026-09-22 02:10 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check` → "314 files already formatted"; `make memory-lint-check` → "All checks passed!"; `make pre-commit` → prettier/ruff check/ruff format/biome all "Passed")
- Unit tests: 4065 passed / 0 failed (`make memory-tests`, env `local`, single run, no flake observed on `tests/unit/models/test_prewarm.py`)
- Integration tests: N/A — no integration suite (AGENTS.md)
- Warnings: 0 pytest-collected warnings in the default run (the one `UserWarning` printed is `opik`'s pre-existing Python-3.14/pydantic-v1 import warning, emitted at collection before any test runs, unrelated to this diff and not a pytest warning in the summary line)

**E2E adversarial pass** (docs + one-helper reuse — no pipeline/Modal commands per the hard safety rule; verified via direct in-process calls with Prefect/Modal mocked)
- Happy path (operator reads only the README, step 1 → step 4, then tries the "one run" override): `TREE_MODELS__LLM__PROVIDER=modal make memory-run-memory-pipeline` semantics reproduced by unit test `TestRunMemoryPipelineIgnoredOverrides::test_it_warns_when_a_model_override_is_set_in_this_shell` → WARNING naming `TREE_MODELS__LLM__PROVIDER` and `make memory-serve-workflows`, run still dispatches (PASS)
- Break path 1 (mutation — remove the guard from one script): `Edit` deleted the two `warn_ignored_config_overrides` calls from `run_memory_pipeline.py`; `shasum` before `f174f242577dc7beff8364f31ea88252f519a2b2`, ran `PYTEST_ADDOPTS="tests/unit/scripts/ -k IgnoredOverrides -q" make memory-tests` (from `apps/memory`) → `1 failed, 7 passed` — exactly `TestRunMemoryPipelineIgnoredOverrides::test_it_warns_when_a_model_override_is_set_in_this_shell` failed, no other script's test broke; reverted with a targeted `Edit`, `shasum` after matched the pre-mutation hash exactly (PASS — mutation kills exactly the right test, revert proven byte-identical)
- Break path 2 (env-shadowing / suite-rail conflict — single-underscore `TREE_MODAL_DRY_RUN` vs the double-underscore `TREE_MODAL__` prefix, set globally by `apps/memory/tests/unit/conftest.py:148-150` on every unit test): `TREE_MODAL_DRY_RUN=1 uv --directory apps/memory run python -c "from tree.cli import warn_ignored_config_overrides; warn_ignored_config_overrides('TREE_MODAL__')"` → no warning logged (PASS — `str.startswith("TREE_MODAL__")` correctly does not match `TREE_MODAL_DRY_RUN`, so the CLI dry-run rail can never falsely trip the new docs-driven warning)
- Break path 3 (untested code path — `run_pipeline.py`'s `_run_online`, which the new unit tests do not cover, only `_run_offline` does): direct call `await m._run_online(None, None, "https://example.com", None)` with `connect_and_resolve_user` / `dispatch_online_pipeline` / `wait_for_dispatch` / `flush_opik` mocked and `TREE_MODELS__LLM__PROVIDER=modal` in the environment → WARNING logged naming `TREE_MODELS__LLM__PROVIDER` (PASS — the guard fires correctly even on the coroutine the unit suite left uncovered; flagged below as a coverage gap, not a defect)

**Acceptance criteria**
- [x] Issue 1 (verdict list contiguous) — `awk '/^- \*\*Accepted\*\*/,/^- \*\*Any other failure\*\*/' apps/memory/README.md | grep -c '^\*\*1a\.'` → `0`; `apps/memory/README.md:573,575,584` are the three consecutive bullets, `**1a.` at `:611`
- [x] Issue 2 (Embedding reset link in step 4) — `awk '/^\*\*4\. Point the memory/,/^## Testing/' apps/memory/README.md | grep -c "changing-the-embedding-model"` → `1`; anchor resolves (`#### Changing the embedding model` heading exists at `apps/memory/README.md:265`, slug matches); four reset commands not repeated; `models.resolution_embedding is transient and needs no reset` is one sentence
- [x] Issue 3 docs (one-run override truthfully described) — `apps/memory/README.md:736-741` says to set the override "where the FLOW runs, not where you type `make`", names the two places (`make memory-serve-workflows` shell / Prefect Managed deployment env), says a `make memory-run-*` command is a silent no-op that bills Gemini, and that the command warns; the `-test … TREE_MODAL__WARMUP_DEADLINE_S=1200` form is still documented as working, now with the reason ("this target runs the code IN this process") at `apps/memory/README.md:700-702`
- [x] Issue 3 code (dispatch scripts warn before dispatch) — all five scripts (`run_memory_pipeline.py`, `run_pipeline.py` both `_run_offline`/`_run_online`, `run_indexing_pipeline.py`, `run_clustering_pipeline.py`, `run_dream_consolidation.py`) call `warn_ignored_config_overrides("TREE_MODELS__")` / `("TREE_MODAL__")` before dispatch, reusing the existing helper verbatim, `init_logger()` unmoved. Verified: unit tests (4 warn tests + 4 clean-shell tests + clustering's own pair + dream's new pair, all green); mutation test above (kills exactly one test); direct call proving `TREE_MODAL_DRY_RUN` (single underscore, the suite's global rail) never trips the `TREE_MODAL__` warning; direct call proving the untested `_run_online` path also warns
- [x] Issue 4 (embedding YAML example + `native_dimensions` recipe) — `apps/memory/README.md:596-604`: 7-line `embedding_models:` entry with `<org>/<embedding-model>` placeholder and the six required fields (`repo_id`, `revision`, `native_dimensions`, `matryoshka_dimensions`, `query_prompt`, `document_prompt`); the quoted smoke-test message (`expected 1024 dims, got 2048 — the Modal catalog's native_dimensions is wrong for this Serving path …`) matches `apps/memory/src/tree/models/modal_server.py:258-259` verbatim; points at the two `configs/default.yaml` seeds for App fields instead of relisting them
- [x] Issue 5 (LLM example mirrors the seed) — `apps/memory/README.md:616-625` field-for-field matches the `LiquidAI/LFM2.5-350M` entry in `apps/memory/configs/default.yaml:281-288` (`repo_id`, `gpu: A10`, `n_gpus: 1`, `cpu: 4`, `memory_mb: 16384`, `max_model_len: 32768`, `max_tokens: 4096`); no `enable_thinking` / `chat_template_kwargs` on it; `grep -c enable_thinking apps/memory/README.md` inside the block → 0; `chat_template_kwargs` is mentioned only in the following paragraph, scoped to the `Qwen/Qwen3.5-0.8B` seed
- [x] Issue 6 (Pre-warm documented) — `apps/memory/README.md:743-749`, 2 sentences, uses **Pre-warm** and **Warm gate** verbatim (`grep -c "Pre-warm" apps/memory/README.md` → `2`), quotes `Pre-warming N Modal server(s): …` / `Still cold (HTTP 503) … — 38s/600s` / `Warm: … after 113s`, matching the literal format strings in `apps/memory/src/tree/models/get_model.py:292` and `apps/memory/src/tree/models/modal_warmup.py:165,173`; states the MCP server is deliberately not pre-warmed and the first `search_memory` after ~5 idle minutes waits out the boot
- [x] Issue 7 (Proxy token wording + CLI login) — `README.md:43` reads "a model (an embedding model or an LLM)"; `apps/memory/README.md:92` reads "Modal-hosted models (embedding models and LLMs)"; the one-time `uv --directory apps/memory run modal setup` login clause is in step 2 at `apps/memory/README.md:670-673`, distinguished from the Proxy token and `HF_TOKEN`
- [x] Issue 8 (PR #44 body) — `gh pr view 44 --json body -q .body` shows the reset described only as "a dry run BY DEFAULT (it only writes under `CONFIRM=yes`)"; no "DRY_RUN support" / "DRY_RUN=yes … on the embedding reset" text remains; `DRY_RUN=yes` is scoped to the deploy driver bullet only; follow-up 1 reads "`make memory-reset-embeddings CONFIRM=yes` (once per user)"; attribution line intact at the end of the body; 0 workspace names / server URLs / endpoint ids found (`grep -noE "ep-[a-z0-9-]+|tree-[a-z0-9.-]+|https://[a-z0-9.-]*modal[a-z0-9.-]*/…"` on the fetched body → no matches)
- [x] Section length cap — `awk '/^## Serving models on Modal/,/^## Testing/' apps/memory/README.md | wc -l` → `202` (<= 202)
- [x] No retired promise regained — `grep -rn "0\.75\|MODAL_EMBEDDING_API_KEY\|deploy-embedding-model\|serving:" README.md apps/memory/README.md .env.example .agents/skills/run-pipelines-e2e/SKILL.md` → only `apps/memory/README.md:561: … There is no \`serving:\` field …`
- [x] Gates green — `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` all green, env `local`, run one at a time (see Evidence)
- [x] Tester re-runs full QA suite and PASSES — this entry
- [ ] PA re-runs acceptance review on the feature and ACCEPTS — awaiting PA (not a Tester responsibility)

**"Prefer removing" check** — section length 202 lines (cap 202); read the whole section against the code: every quoted command (`make memory-deploy-model[-test|-stop]`, `DRY_RUN=yes`, `FORCE=yes`, `SERVING=endpoint|app`), YAML key (`native_dimensions`, `matryoshka_dimensions`, `query_prompt`, `document_prompt`, `n_gpus`, `cpu`, `memory_mb`, `max_model_len`, `max_tokens`, `chat_template_kwargs`, `extra_server_args`), config default (`modal.warmup_deadline_s: 600`, `modal.request_timeout_s: 300` — matches `apps/memory/src/tree/config/app_config.py:1064-1075`), and log-line format string was grepped against the actual source and found present verbatim; no stale promise found (`0.75`, `MODAL_EMBEDDING_API_KEY`, strict `json_schema` and `serving:` field are all absent except the one permitted "no `serving:` field" sentence).

**Evidence**
```
$ make memory-format-check
314 files already formatted

$ make memory-lint-check
All checks passed!

$ make pre-commit
prettier / ruff check / ruff format / biome check (harness) .......... Passed

$ make memory-tests
============================ 4065 passed in 54.21s =============================

$ awk '/^## Serving models on Modal/,/^## Testing/' apps/memory/README.md | wc -l
202

$ shasum apps/memory/scripts/run_memory_pipeline.py   # before mutation
f174f242577dc7beff8364f31ea88252f519a2b2  apps/memory/scripts/run_memory_pipeline.py
$ # (removed the two warn_ignored_config_overrides calls via Edit)
$ PYTEST_ADDOPTS="tests/unit/scripts/ -k IgnoredOverrides -q" make memory-tests
....F...   1 failed, 7 passed in 2.24s
FAILED tests/unit/scripts/test_run_memory_pipeline.py::TestRunMemoryPipelineIgnoredOverrides::test_it_warns_when_a_model_override_is_set_in_this_shell
$ # (reverted via Edit)
$ shasum apps/memory/scripts/run_memory_pipeline.py   # after revert
f174f242577dc7beff8364f31ea88252f519a2b2  apps/memory/scripts/run_memory_pipeline.py   # byte-identical

$ TREE_MODAL_DRY_RUN=1 uv --directory apps/memory run python -c "from tree.cli import warn_ignored_config_overrides; warn_ignored_config_overrides('TREE_MODAL__')"
(no warning logged — PASS)
```

**Other issues found**
- Coverage gap (not blocking): `run_pipeline.py`'s `_run_online` coroutine carries the same two guard calls as `_run_offline` but has no dedicated unit test exercising the warn path on that branch — confirmed correct by a direct manual call (see Break path 3), but a regression there would not be caught by `make memory-tests`. Worth a follow-up test, not a fix to this task.
- Judgment call on the two REPORTED-not-fixed gaps in the SWE's "Extension" note (`run_data_pipeline.py`'s silent no-op against the live-index gate at `tree/data/offline_pipeline.py:237-243`, and `TREE_DREAM__DRY_RUN` on `run_dream_consolidation.py`): AC 3 (code) names exactly the four scripts it names, and the SWE's own extension to a fifth (`run_dream_consolidation.py`) was already anticipated in this task's expected diff. I did not extend further — `run_data_pipeline.py` and the `TREE_DREAM__` prefix are a different (if kindred) trap on scripts/knobs this task's AC never named, so I'm treating both as accurately-reported follow-up nits for the PR-Reviewer / a new task, not a blocker for #159's own AC 3. Not overruling the SWE's lean.
- No print() calls added; no secrets or secret-shaped strings in the diff; new test fixtures without return-type annotations match the pre-existing untyped-fixture convention already used throughout `tests/unit/scripts/*.py`.

**VERDICT: PASS**
