---
id: 145-modal-catalog-rework-and-auto-router
status: pending
feature: voyage-4-and-modal-embedding-catalog
---

# Auto-routing with Modal as the oracle: drop `serving` / `base_model` from the YAML, add the LLM list, route endpoint-first then App-by-kind, stop path-blind — behind ONE `memory-deploy-model*` target family

Tags: `modal`, `config`, `deploy`, `cli`
Depends on: #143
Blocks: #146, #147, #148, #141
Implements: ADR-009 — Decision 2 (auto-routing: Modal is the oracle, endpoint first, App by kind) and Decision 3 (the **Modal catalog**: two YAML lists, kind from the list, one lookup)

## Scope

**EXECUTION ORDER of the rework: 143 -> 144 -> 145 -> 146 -> 147 -> 148 -> 149 -> 150 -> 141.**

**HARD SAFETY RULE:** No agent may run `make memory-deploy-*`, `scripts/modal_*.py` as a process, or any `modal …` command outside task 141; a PATH shim does NOT intercept under `make`/`uv run` because `.venv/bin` wins — use the mocked unit tests and `DRY_RUN=yes`.

**The human's decision (settled).** The YAML says WHICH Hugging Face model and that it goes to Modal — never
HOW. At deploy the driver first asks Modal whether the model can be a **Dedicated endpoint**; if not, it
deploys our App for the model's KIND (embedding -> vLLM script, llm -> SGLang script). Live evidence behind it
(2026-09-20): Modal's endpoint catalog holds 44 models, only 2 of them embedding models; "custom weights" =
same-architecture fine-tunes only. The Apps are the GENERAL path, not a fallback curiosity.

**What this task rewrites (committed code from #138/#139/#140/#142 + #143):**
- `src/tree/config/app_config.py`: DELETE `ServingPath`, `ModalEmbeddingModelConfig.serving`, `.base_model`,
  `_check_endpoint_has_a_base_model`. ADD `ModelKind = Literal["embedding", "llm"]`, `ModalLLMModelConfig`,
  `ModalConfig.llm_models`. Everything else on an embedding entry STAYS explicit per model (`revision`,
  `native_dimensions`, `matryoshka_dimensions`, both prompts, `gpu`, `cpu`, `memory_mb`, `max_model_len`,
  `extra_server_args`).
- `src/tree/models/modal_catalog.py`: DELETE `resolve_serving`, `_SERVING_PATHS`, `fallback_script`,
  `_FALLBACK_SCRIPTS`; CHANGE `get_catalog_entry`, `modal_cli_command`, `hf_token_args`.
- NEW `src/tree/models/modal_router.py` (pure: text classification, list parsing, HF lineage, the decision).
- `scripts/modal_embedding_model.py` -> `git mv` to `scripts/modal_model.py`, rewritten around the router.
- `apps/memory/Makefile`: the three `deploy-embedding-model*` targets are REPLACED by `deploy-model`,
  `deploy-model-test`, `deploy-model-stop`.
- `configs/default.yaml`, `tests/unit/config/fixtures/frozen_config.yaml`, README's Modal section, and the
  tests of all of the above. The glossary term **Embedding catalog** is now **Modal catalog** (PA already
  updated `docs/glossary.md`): rename it in the 17 docstrings / messages under `src/`, `scripts/`, `deploy/`.

**A. Config.**
- `ModalLLMModelConfig` (`extra="forbid"`): `repo_id`, `revision` (same patterns as the embedding entry),
  `gpu: str = "A10"`, `n_gpus: int = 1` (`ge=1`; becomes SGLang's `tp` and the `:N` of the GPU string),
  `cpu: float = 4`, `memory_mb: int = 16384`, `max_model_len: int | None = None`,
  `extra_server_args: dict[str, str] = {}`; derived `endpoint_name` / `app_name` (`tree-<slug>` /
  `ep-tree-<slug>`, the 63-character rule from #143) and `kind`. The name derivation, the empty-slug check, the
  length check and the server-arg validator must exist ONCE for both classes (a shared base class or shared
  functions — SWE's call). Add `--tp`, `--tp-size`, `--tensor-parallel-size` to `_BUILDER_OWNED_SERVER_ARGS`.
- `kind` is a read-only property: `"embedding"` on `ModalEmbeddingModelConfig`, `"llm"` on
  `ModalLLMModelConfig` — it comes from WHICH LIST the entry lives in, never from guessing at the model.
- `ModalConfig._check_entries_are_unique` now spans BOTH lists: a `repo_id` or a derived `app_name` may appear
  once across `embedding_models` + `llm_models` (messages name both lists).
- `get_catalog_entry(model)` searches both lists and returns the entry (its `kind` tells the caller which);
  unknown id -> `ModelError("Unknown Modal model '<id>'. Modal catalog ids — embeddings: <sorted>; llms: <sorted>. Add an entry under modal.embedding_models or modal.llm_models in configs/default.yaml.")`.
  NEW `get_embedding_entry(model)` / `get_llm_entry(model)`: same lookup + a kind check
  (`ModelError("<id> is an LLM entry (modal.llm_models), not an embedding model.")` and the mirror) — the
  embedding client and `build_deploy_spec` switch to `get_embedding_entry`.
- YAML (`modal:` section): remove `serving:` and `base_model:` and their comments from both embedding seeds;
  add

      llm_models:
        # In Modal's endpoint catalog (2026-09-20) -> auto-routes to a Dedicated endpoint; the App fields keep their defaults.
        - repo_id: Qwen/Qwen3.5-0.8B
          revision: 2fc06364715b967f1860aea9cf38778875588b17 # HF sha of `main`, 2026-09-20
        # NOT in Modal's catalog (arch Lfm2ForCausalLM; base LiquidAI/LFM2.5-350M-Base is not either) -> SGLang App.
        - repo_id: LiquidAI/LFM2.5-350M
          revision: 9e6c6ccf47cd318696e137d381a7ded8fe4df09f # HF sha of `main`, 2026-09-20
          gpu: A10
          n_gpus: 1
          cpu: 4
          memory_mb: 16384
          max_model_len: 32768 # the card's context is 128000; 32K keeps the KV cache small on one A10

  Why `Qwen/Qwen3.5-0.8B` and not `google/gemma-3-1b-it` as the endpoint-routed LLM seed: both are in Modal's
  printed list, but gemma is `gated: manual` under the Gemma licence on the Hub, Qwen3.5-0.8B is ungated
  Apache-2.0 (HF API, read 2026-09-20). The LLM App script itself arrives in #147; until then an LLM that
  routes to the App exits 2 on the missing script (the #139 -> #142 pattern).

**B. The router — `src/tree/models/modal_router.py`** (no `subprocess`, no `modal` import; the process
boundary stays in #143's `modal_cli.py`).
- `classify_endpoint_refusal(output: str) -> Literal["not_in_catalog", "not_servable", "other"]` on STABLE
  SUBSTRINGS: `"is not available for dedicated Endpoints"` -> `not_in_catalog`;
  `"is not a servable checkpoint of base model"` -> `not_servable`; anything else -> `other`.
- `parse_endpoint_catalog(output: str) -> list[str]`: the repo ids of the bullet lines that follow
  `Models available for dedicated Endpoints:` (accept `-`, `*`, `•` bullets and surrounding whitespace / Rich
  markup; keep tokens matching the repo-id pattern). BEST-EFFORT: anything unparsable -> `[]`, never an
  exception — an empty list simply skips step 2.
- `hf_base_models(repo_id, token) -> list[str]`: `GET https://huggingface.co/api/models/<repo_id>` with
  `httpx` (a main dependency; a sync one-shot GET in a sync CLI driver), 10 s timeout, `Authorization: Bearer`
  only when `HF_TOKEN` is set (never logged). Reads `cardData.base_model`, which is a STRING on some cards and
  a LIST on others (verified 2026-09-20: `LiquidAI/LFM2.5-350M` -> `"LiquidAI/LFM2.5-350M-Base"`,
  `Qwen/Qwen3.5-0.8B` -> `["Qwen/Qwen3.5-0.8B-Base"]`, `voyageai/voyage-4-nano` -> none,
  `Octen/Octen-Embedding-0.6B` -> `"Qwen/Qwen3-Embedding-0.6B"`). Skip the card when
  `cardData.base_model_relation` is `adapter`, `merge` or `quantized` (not plain fine-tuned weights). Walk at
  most 2 hops (model -> base -> base's base). ANY failure (network, 404, 401, bad JSON) -> WARNING
  `Could not read the Hugging Face lineage of <repo_id> (<reason>) — skipping the custom-weights attempt.` and `[]`.
- **The algorithm** (`deploy`, no `SERVING=`):
  0. `existing_kind(entry)` (#143). `endpoint` live -> REFUSE, exit 3 (#143's message; an endpoint cannot be
     updated by `create`). `app` live -> skip to step 3 with reason `'<app_name>' is already live as an App —
     redeploying it (stop it first to re-route)`. `none` -> step 1. List unreadable -> #143's fail-closed rule.
  1. `modal endpoint create --name tree-<slug> --model <repo_id> --routing-region eu-west` (output captured,
     #143). Exit 0 -> DONE (endpoint).
  2. Refusal `not_in_catalog` -> `catalog = parse_endpoint_catalog(output)`; for the first ancestor from
     `hf_base_models` that is IN `catalog`: `modal endpoint create --name tree-<slug> --model <base>
     --custom-hf-repo <repo_id> --custom-hf-revision <revision> --routing-region eu-west` (+ the redacted
     `--custom-hf-token` pair of #139 when `HF_TOKEN` is set). Exit 0 -> DONE (endpoint, custom weights).
     Refusal `not_servable` (or `not_in_catalog`) -> step 3. No ancestor in the list -> step 3.
  3. Deploy the APP for the entry's KIND: `embedding` -> `modal deploy deploy/modal_vllm_embedding.py`;
     `llm` -> `modal deploy deploy/modal_sglang_llm.py` (#147; missing file -> exit 2 as today).
     `guard_deploy(entry, "app", force)` runs first.
  4. ANY OTHER failure at step 1 or 2 (`other`: auth, quota, network, a text we do not know) -> ERROR with
     Modal's own output, exit with Modal's code. NEVER fall back silently: an unknown failure is not evidence
     that the model is ineligible.
- ONE INFO line carries the decision and its reason — exact shapes (tests assert them):
  - `Routing Qwen/Qwen3-Embedding-0.6B: Modal accepted it → Dedicated endpoint tree-qwen3-embedding-0-6b`
  - `Routing voyageai/voyage-4-nano: not in Modal's endpoint catalog, no catalog base → vLLM App`
  - `Routing LiquidAI/LFM2.5-350M: not in Modal's endpoint catalog, no catalog base → SGLang App`
  - `Routing Octen/Octen-Embedding-0.6B: not in Modal's endpoint catalog, fine-tune of Qwen/Qwen3-Embedding-0.6B → Dedicated endpoint tree-octen-embedding-0-6b (custom weights)`
  - `Routing <id>: not in Modal's endpoint catalog, Qwen/Qwen3-Embedding-0.6B refused the weights (not a servable checkpoint) → vLLM App`
  - `Routing <id>: 'ep-tree-<slug>' is already live as an App → redeploying the vLLM App (stop it first to re-route)`
  - `Routing <id>: SERVING=app → vLLM App (no endpoint attempt)` / `Routing <id>: SERVING=endpoint → Dedicated endpoint only (no App fallback)`
- `SERVING=endpoint|app` survives ONLY as an ops escape hatch on `deploy-model` and `deploy-model-stop`
  (`--serving`), never in YAML. `SERVING=endpoint`: steps 0-2, and a refusal is a FAILURE (exit 1, Modal's
  text). `SERVING=app`: step 0 (an `endpoint` live -> REFUSE) then step 3. Any other value -> exit 2.
- `modal_cli_command(action, entry, target: Literal["endpoint", "app"], base_model: str | None = None)`; still
  token-free. `hf_token_args` appends the pair only for a custom-weights create (`base_model` given).
- **`stop` is path-blind**: `assert_owned_name` on BOTH names first; then `modal endpoint stop -y tree-<slug>`
  (captured); exit 0 -> done. Non-zero -> INFO `Not a Dedicated endpoint (<first line of Modal's output>) —
  stopping the App instead.` and `modal app stop -y ep-tree-<slug>`; its exit code is the command's.
  `SERVING=endpoint|app` runs only that one. No list call on stop (#143).
- **`DRY_RUN=yes`** cannot consult the oracle, so it logs the first command and what each verdict would lead to:
  `DRY RUN — would run: modal endpoint create --name tree-voyage-4-nano --model voyageai/voyage-4-nano --routing-region eu-west`
  then `DRY RUN — routing is undecided without Modal: on "not available for dedicated Endpoints" the next
  command would be: modal deploy deploy/modal_vllm_embedding.py`; `stop` logs both stop commands. Exit 0.
- `test` (`deploy-model-test`): embedding entry -> the existing `smoke_test`; LLM entry -> ERROR
  `No smoke test for LLM entries yet (tasks/147).`, exit 2.
- The env var the driver hands to a deploy script is renamed `EMBEDDING_MODEL` -> `MODAL_MODEL` (one line in
  each of the two existing scripts + their static tests).

**C. ONE target family — why.** `memory-deploy-model MODEL=<repo_id>` looks the id up in both lists. The
alternative (keep `memory-deploy-embedding-model*`, add `memory-deploy-llm*`) is six targets, a `--kind`
argument and a way to pass the wrong one; one lookup over two lists with a cross-list uniqueness rule is less
mechanism, and the feature is unreleased so the rename costs no user. EVERY reference to update (re-grep:
`grep -rn "deploy-embedding-model\|modal_embedding_model\|EMBEDDING_MODEL" . --exclude-dir=done`):
`apps/memory/Makefile` (3 targets + help text, `FORCE`/`DRY_RUN`/`SERVING` plumbing), `apps/memory/README.md`
(3 commands + prose), `configs/default.yaml` comments, `src/tree/models/modal_server.py`
(`resolve_server_url`'s "Run: make …" hint + module docstring), `deploy/modal_vllm_embedding.py` and
`deploy/modal_sglang_embedding.py` (docstring usage lines + the env var), `tests/unit/models/test_modal_server.py`,
`tests/unit/models/test_modal_embedding.py`, `tests/unit/scripts/test_modal_embedding_model_script.py` ->
`git mv` to `test_modal_model_script.py`, `tests/unit/deploy/test_modal_embedding_scripts.py`, #143's refusal
messages. NOT edited: `tasks/done/*`, `docs/` (PA), `.agents/skills/run-pipelines-e2e/SKILL.md` (#141).

**D. README** (`## Modal embedding deployment (optional)` -> `## Serving models on Modal (optional)`): DELETE
the three-rung ladder, "write it into the entry's `serving:`" and the "Fallback scripts" paragraph; say
instead, in this order: the YAML names the model, never the path; `deploy-model` asks Modal first and logs one
`Routing …` line; embeddings that Modal refuses go to the vLLM App, LLMs to the SGLang App; `SERVING=` is an
escape hatch; `-stop` needs no `SERVING=`. Per the project rule, REMOVE now-wrong sentences rather than
adding caveats.

**The orphan, on purpose.** After this task the driver never deploys `deploy/modal_sglang_embedding.py`
(embeddings go to vLLM). It and its static tests stay green and untouched until #147 turns the file into
`deploy/modal_sglang_llm.py`. Do not delete `build_deploy_spec`'s `engine` parameter here.

## Out of scope
- The scripts' content (#146 vLLM, #147 SGLang). The chat smoke test (#147). `ModalLLM` (#148).
- A "can you serve X" pre-flight API (Modal has none); caching Modal's list; a YAML allow-list of endpoint
  models (it would be config about the path — exactly what was removed).
- Proving the fine-tune -> custom-weights route LIVE: the human did not select that cycle; it is unit-tested
  only (against the exact live refusal texts), and #141 says so.
- Embedding models vLLM cannot serve and LLMs SGLang cannot serve.

## Acceptance Criteria

- [ ] `grep -rn "ServingPath\|resolve_serving\|base_model\b\|\.serving\b\|serving:" apps/memory/src apps/memory/scripts apps/memory/configs apps/memory/tests/unit/config/fixtures` -> no hit except `base_model` inside `modal_router.py` / `modal_catalog.modal_cli_command` (the RUNTIME base, never a config field) — list survivors in `## Log`. A YAML entry carrying `serving:` or `base_model:` fails to load (`extra="forbid"`): `test_retired_fields_are_rejected`.
- [ ] `test_seed_entries`: embeddings = `Qwen/Qwen3-Embedding-0.6B`, `voyageai/voyage-4-nano` (every remaining field unchanged from today); llms = `Qwen/Qwen3.5-0.8B` (rev `2fc06364715b967f1860aea9cf38778875588b17`, defaults) and `LiquidAI/LFM2.5-350M` (rev `9e6c6ccf47cd318696e137d381a7ded8fe4df09f`, `A10`, `n_gpus 1`, `max_model_len 32768`). `kind` is `embedding` / `llm` accordingly; `app_name`s are `ep-tree-qwen3-5-0-8b` and `ep-tree-lfm2-5-350m`.
- [ ] `test_uniqueness_spans_both_lists`: the same `repo_id` in both lists -> `ValidationError` naming both lists; two ids deriving one `app_name` across lists -> the app-name error. `test_n_gpus_must_be_positive`. `test_tp_flags_are_builder_owned`.
- [ ] `TestGetCatalogEntry`: finds ids in either list; the unknown-id message lists both groups sorted; `get_embedding_entry("LiquidAI/LFM2.5-350M")` and `get_llm_entry("voyageai/voyage-4-nano")` raise the kind errors.
- [ ] `TestClassifyEndpointRefusal`, fed the EXACT live texts from `MODAL_TEMPLATES_REFERENCE.md`: `'voyageai/voyage-4-nano' is not available for dedicated Endpoints.\nModels available for dedicated Endpoints:\n- Qwen/Qwen3-Embedding-0.6B\n…` -> `not_in_catalog`; `The custom model is not a servable checkpoint of base model 'Qwen/Qwen3-Embedding-0.6B': hidden_size …` -> `not_servable`; `Token missing`, `quota exceeded`, `Connection error`, `` -> `other`.
- [ ] `TestParseEndpointCatalog`: a 44-line bullet fixture -> 44 ids including `Qwen/Qwen3-Embedding-0.6B`, `Qwen/Qwen3.5-0.8B`, `google/gemma-3-1b-it`; `•` / `*` bullets and Rich markup parse; no header, garbage or empty text -> `[]` and no exception.
- [ ] `TestHfBaseModels` (`httpx.MockTransport`): string, list and missing `base_model`; `base_model_relation: adapter|merge|quantized` -> `[]`; 2-hop walk returns `[base, base_of_base]` and makes at most 2 requests; 404 / timeout / invalid JSON -> `[]` + the WARNING; the `Authorization` header is present iff a token is set and the token is in no log record.
- [ ] `TestRouter` (all `subprocess.run` mocked, `existing_kind` patched to `none` unless stated), asserting the argv sequence, the exit code and the exact `Routing …` line: (1) create exit 0 -> 1 call, endpoint line; (2) embedding + `not_in_catalog` + no lineage -> calls = [create, `modal deploy deploy/modal_vllm_embedding.py`], the vLLM line; (3) llm + same -> `deploy/modal_sglang_llm.py` (file faked present), the SGLang line; (4) lineage base IN the parsed list + second create exit 0 -> [create, create with `--model Qwen/Qwen3-Embedding-0.6B --custom-hf-repo Octen/Octen-Embedding-0.6B --custom-hf-revision <rev>`], custom-weights line, and with a fake token the logged argv shows `--custom-hf-token ***`; (5) same but the second create answers `not_servable` -> 3 calls, the "refused the weights" line; (6) lineage base NOT in the list -> no second create; (7) unparsable list -> no second create; (8) `other` (exit 1, `Token missing`) -> exactly 1 call, exit 1, Modal's text logged, NO `modal deploy`, no `Routing … App` line; (9) `existing_kind == "app"` -> no create at all, [`modal deploy …`], the already-live line; (10) `existing_kind == "endpoint"` -> exit 3, 0 deploy calls; (11) `SERVING=app` -> no create; (12) `SERVING=endpoint` + `not_in_catalog` -> exit 1, no deploy; (13) `SERVING=sglang` -> exit 2.
- [ ] `TestStop`: endpoint stop exit 0 -> 1 call; endpoint stop exit 1 -> the `Not a Dedicated endpoint (…)` INFO + `modal app stop -y ep-tree-voyage-4-nano`; both non-zero -> the app stop's exit code; `SERVING=app` -> only the app stop; an un-prefixed derived name -> exit 2 and 0 calls (#143 still holds).
- [ ] `TestDryRun` (extends #143's): `deploy --dry-run` logs the create argv AND the `routing is undecided without Modal` line naming the kind's script, 0 `subprocess.run` calls; `stop --dry-run` logs both stop commands.
- [ ] `make -n memory-deploy-model MODEL=x SERVING=app DRY_RUN=yes FORCE=yes` shows `scripts/modal_model.py deploy --model "x" --serving "app" --dry-run --force`; `make -n memory-deploy-embedding-model MODEL=x` fails with `No rule to make target`. `grep -rn "deploy-embedding-model\|modal_embedding_model\.py\|\"EMBEDDING_MODEL\"" apps/memory Makefile` -> 0 hits.
- [ ] `make memory-deploy-model MODEL=voyageai/voyage-4-nano DRY_RUN=yes`, `… MODEL=LiquidAI/LFM2.5-350M DRY_RUN=yes` and `make memory-deploy-model-stop MODEL=Qwen/Qwen3.5-0.8B DRY_RUN=yes` are run for real (they start no `modal`), exit 0, and their output is pasted in `## Log`.
- [ ] `grep -c "Embedding catalog" -r apps/memory/src apps/memory/scripts apps/memory/deploy` -> 0; `grep -c "three\|ladder\|fallback" ` in README's Modal section -> 0; `grep -c "Routing" apps/memory/README.md` >= 1.
- [ ] `test_router_does_not_import_modal_or_subprocess`; `test_catalog_does_not_import_modal` stays green.
- [ ] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green (LOCAL env).

## User Stories

### Story: Operator serves an embedding model Modal supports
1. Adds nothing — `Qwen/Qwen3-Embedding-0.6B` is a seed. Runs `make memory-deploy-model MODEL=Qwen/Qwen3-Embedding-0.6B`.
2. Log: `Running: modal endpoint create --name tree-qwen3-embedding-0-6b --model Qwen/Qwen3-Embedding-0.6B --routing-region eu-west`, then `Routing Qwen/Qwen3-Embedding-0.6B: Modal accepted it → Dedicated endpoint tree-qwen3-embedding-0-6b`.
3. No image was built; no `serving:` exists anywhere in the YAML.

### Story: Operator serves voyage-4-nano and never learns what a "serving path" is
1. `make memory-deploy-model MODEL=voyageai/voyage-4-nano`.
2. Modal answers `'voyageai/voyage-4-nano' is not available for dedicated Endpoints.`; the driver reads the Hub card (no `base_model`) and logs `Routing voyageai/voyage-4-nano: not in Modal's endpoint catalog, no catalog base → vLLM App`.
3. `Running: modal deploy deploy/modal_vllm_embedding.py` follows in the same command.

### Story: Operator serves a fine-tune of a catalog model
1. Adds `Octen/Octen-Embedding-0.6B` (rev, `native_dimensions: 1024`) to `modal.embedding_models` and deploys it.
2. Modal refuses the id; the Hub card names `Qwen/Qwen3-Embedding-0.6B`, which IS in the list Modal just printed.
3. The second command carries `--model Qwen/Qwen3-Embedding-0.6B --custom-hf-repo Octen/Octen-Embedding-0.6B --custom-hf-revision <sha>`; the log ends `→ Dedicated endpoint tree-octen-embedding-0-6b (custom weights)`.

### Story: Modal is down — nothing is deployed by accident
1. `modal endpoint create` exits 1 with `Connection error`.
2. ONE ERROR block with Modal's text, exit 1. No `Routing … App` line, no image build: an unknown failure is not a verdict.

### Story: Operator stops a model without remembering how it was served
1. `make memory-deploy-model-stop MODEL=voyageai/voyage-4-nano`.
2. `Running: modal endpoint stop -y tree-voyage-4-nano` fails; `Not a Dedicated endpoint (…) — stopping the App instead.`; `Running: modal app stop -y ep-tree-voyage-4-nano`; exit 0.

### Story: Operator forces the App for a model Modal would accept
1. `make memory-deploy-model MODEL=Qwen/Qwen3-Embedding-0.6B SERVING=app` (they want to pin vLLM 0.26.0 themselves).
2. `Routing Qwen/Qwen3-Embedding-0.6B: SERVING=app → vLLM App (no endpoint attempt)`; the YAML is unchanged and the client keeps working (same app name).

### Story: A typo in MODEL
1. `make memory-deploy-model MODEL=LiquidAI/LFM2.5-350m`.
2. Exit 2: `Unknown Modal model 'LiquidAI/LFM2.5-350m'. Modal catalog ids — embeddings: Qwen/Qwen3-Embedding-0.6B, voyageai/voyage-4-nano; llms: LiquidAI/LFM2.5-350M, Qwen/Qwen3.5-0.8B. …` — before anything is spent.

---

Blocked by: #143

## Log

### [PA] 2026-09-20 13:40 — Grooming (new task: third plan edit — auto-routing, D1/D2/D3/D6)

**Summary**
The Serving path stops being configuration. The YAML keeps the model and its per-model facts; the driver asks Modal (the only oracle), reads the fine-tune lineage from the Hub for the custom-weights attempt, and otherwise deploys the App for the entry's kind. An LLM list mirrors the embeddings list; one target family serves both.

**Key decisions**
- Classification on two stable substrings, unit-tested against today's live texts; list parsing is best-effort and degrades to "skip step 2". Unknown failures abort — never a silent fallback.
- A live App of ours is redeployed without an endpoint attempt ("stop first to re-route"); a live endpoint refuses. Both reuse #143's guard, which was specified on `target` for this reason.
- `stop` tries the endpoint, then the App (the human's rule); no list call.
- ONE `memory-deploy-model*` family over a second `-llm*` family: one lookup over two lists + a cross-list uniqueness rule beats six targets and a `--kind`. Every reference is enumerated.
- `httpx` for the one sync Hub GET in a sync CLI driver (the async `aiohttp` choice of #144 is about the poller inside `tree.models`' async clients).
- Seeds: `Qwen/Qwen3.5-0.8B` (ungated, Apache-2.0) over `google/gemma-3-1b-it` (gated: manual); `LiquidAI/LFM2.5-350M` as the custom LLM.
- Glossary: **Embedding catalog** -> **Modal catalog**; **Serving path** is now a runtime decision (`endpoint` | `app`).

**Verified at grooming (read-only HTTP, no `modal` command)**
- HF API `cardData.base_model` is a string OR a list; values for the four ids above; `google/gemma-3-1b-it` is `gated: manual`.

**Dependencies**
- #143 (`modal_cli.py`, `existing_kind`, `guard_deploy`, `assert_owned_name`, dry run, captured `endpoint create`).

**User stories**
- 7 stories: catalog model, refused model, fine-tune, Modal down, path-blind stop, forced App, typo.

**Open questions**
- None blocking. Whether Modal writes a refusal to stdout or stderr is NOT verified here (no `modal` process may be started): both streams are captured and concatenated, so the design holds either way, and #141 records which it was.

Ready for implementation.
