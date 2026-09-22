---
id: 145-modal-catalog-rework-and-auto-router
status: done
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

- [x] `grep -rn "ServingPath\|resolve_serving\|base_model\b\|\.serving\b\|serving:" apps/memory/src apps/memory/scripts apps/memory/configs apps/memory/tests/unit/config/fixtures` -> no hit except `base_model` inside `modal_router.py` / `modal_catalog.modal_cli_command` (the RUNTIME base, never a config field) — list survivors in `## Log`. A YAML entry carrying `serving:` or `base_model:` fails to load (`extra="forbid"`): `test_retired_fields_are_rejected`.
- [x] `test_seed_entries`: embeddings = `Qwen/Qwen3-Embedding-0.6B`, `voyageai/voyage-4-nano` (every remaining field unchanged from today); llms = `Qwen/Qwen3.5-0.8B` (rev `2fc06364715b967f1860aea9cf38778875588b17`, defaults) and `LiquidAI/LFM2.5-350M` (rev `9e6c6ccf47cd318696e137d381a7ded8fe4df09f`, `A10`, `n_gpus 1`, `max_model_len 32768`). `kind` is `embedding` / `llm` accordingly; `app_name`s are `ep-tree-qwen3-5-0-8b` and `ep-tree-lfm2-5-350m`.
- [x] `test_uniqueness_spans_both_lists`: the same `repo_id` in both lists -> `ValidationError` naming both lists; two ids deriving one `app_name` across lists -> the app-name error. `test_n_gpus_must_be_positive`. `test_tp_flags_are_builder_owned`.
- [x] `TestGetCatalogEntry`: finds ids in either list; the unknown-id message lists both groups sorted; `get_embedding_entry("LiquidAI/LFM2.5-350M")` and `get_llm_entry("voyageai/voyage-4-nano")` raise the kind errors.
- [x] `TestClassifyEndpointRefusal`, fed the EXACT live texts from `MODAL_TEMPLATES_REFERENCE.md`: `'voyageai/voyage-4-nano' is not available for dedicated Endpoints.\nModels available for dedicated Endpoints:\n- Qwen/Qwen3-Embedding-0.6B\n…` -> `not_in_catalog`; `The custom model is not a servable checkpoint of base model 'Qwen/Qwen3-Embedding-0.6B': hidden_size …` -> `not_servable`; `Token missing`, `quota exceeded`, `Connection error`, `` -> `other`.
- [x] `TestParseEndpointCatalog`: a 44-line bullet fixture -> 44 ids including `Qwen/Qwen3-Embedding-0.6B`, `Qwen/Qwen3.5-0.8B`, `google/gemma-3-1b-it`; `•` / `*` bullets and Rich markup parse; no header, garbage or empty text -> `[]` and no exception.
- [x] `TestHfBaseModels` (`httpx.MockTransport`): string, list and missing `base_model`; `base_model_relation: adapter|merge|quantized` -> `[]`; 2-hop walk returns `[base, base_of_base]` and makes at most 2 requests; 404 / timeout / invalid JSON -> `[]` + the WARNING; the `Authorization` header is present iff a token is set and the token is in no log record.
- [x] `TestRouter` (all `subprocess.run` mocked, `existing_kind` patched to `none` unless stated), asserting the argv sequence, the exit code and the exact `Routing …` line: (1) create exit 0 -> 1 call, endpoint line; (2) embedding + `not_in_catalog` + no lineage -> calls = [create, `modal deploy deploy/modal_vllm_embedding.py`], the vLLM line; (3) llm + same -> `deploy/modal_sglang_llm.py` (file faked present), the SGLang line; (4) lineage base IN the parsed list + second create exit 0 -> [create, create with `--model Qwen/Qwen3-Embedding-0.6B --custom-hf-repo Octen/Octen-Embedding-0.6B --custom-hf-revision <rev>`], custom-weights line, and with a fake token the logged argv shows `--custom-hf-token ***`; (5) same but the second create answers `not_servable` -> 3 calls, the "refused the weights" line; (6) lineage base NOT in the list -> no second create; (7) unparsable list -> no second create; (8) `other` (exit 1, `Token missing`) -> exactly 1 call, exit 1, Modal's text logged, NO `modal deploy`, no `Routing … App` line; (9) `existing_kind == "app"` -> no create at all, [`modal deploy …`], the already-live line; (10) `existing_kind == "endpoint"` -> exit 3, 0 deploy calls; (11) `SERVING=app` -> no create; (12) `SERVING=endpoint` + `not_in_catalog` -> exit 1, no deploy; (13) `SERVING=sglang` -> exit 2.
- [x] `TestStop`: endpoint stop exit 0 -> 1 call; endpoint stop exit 1 -> the `Not a Dedicated endpoint (…)` INFO + `modal app stop -y ep-tree-voyage-4-nano`; both non-zero -> the app stop's exit code; `SERVING=app` -> only the app stop; an un-prefixed derived name -> exit 2 and 0 calls (#143 still holds).
- [x] `TestDryRun` (extends #143's): `deploy --dry-run` logs the create argv AND the `routing is undecided without Modal` line naming the kind's script, 0 `subprocess.run` calls; `stop --dry-run` logs both stop commands.
- [x] `make -n memory-deploy-model MODEL=x SERVING=app DRY_RUN=yes FORCE=yes` shows `scripts/modal_model.py deploy --model "x" --serving "app" --dry-run --force`; `make -n memory-deploy-embedding-model MODEL=x` fails with `No rule to make target`. `grep -rn "deploy-embedding-model\|modal_embedding_model\.py\|\"EMBEDDING_MODEL\"" apps/memory Makefile` -> 0 hits.
- [x] `make memory-deploy-model MODEL=voyageai/voyage-4-nano DRY_RUN=yes`, `… MODEL=LiquidAI/LFM2.5-350M DRY_RUN=yes` and `make memory-deploy-model-stop MODEL=Qwen/Qwen3.5-0.8B DRY_RUN=yes` are run for real (they start no `modal`), exit 0, and their output is pasted in `## Log`.
- [x] `grep -c "Embedding catalog" -r apps/memory/src apps/memory/scripts apps/memory/deploy` -> 0; `grep -c "three\|ladder\|fallback" ` in README's Modal section -> 0; `grep -c "Routing" apps/memory/README.md` >= 1.
- [x] `test_router_does_not_import_modal_or_subprocess`; `test_catalog_does_not_import_modal` stays green.
- [x] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green (LOCAL env).

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

### [SWE] 2026-09-20 18:20 — Implementation

**Files modified**
- `apps/memory/src/tree/config/app_config.py` — `ServingPath`, `serving`, `base_model` and `_check_endpoint_has_a_base_model` DELETED; new `ModelKind`, shared base `ModalModelConfig` (repo_id/revision/App fields + the ONE name derivation, empty-slug check, 63-char check and server-arg validator), `ModalEmbeddingModelConfig` / `ModalLLMModelConfig` subclasses with `kind` as a `ClassVar`, `ModalConfig.llm_models`, cross-list uniqueness, `--tp`/`--tp-size`/`--tensor-parallel-size` builder-owned.
- `apps/memory/src/tree/models/modal_catalog.py` — `resolve_serving`, `_SERVING_PATHS`, `fallback_script`, `_FALLBACK_SCRIPTS` DELETED; `get_catalog_entry` spans both lists, new `get_embedding_entry` / `get_llm_entry` / `app_script(kind)`, `modal_cli_command(action, entry, target, base_model=None)`, `hf_token_args(base_model, token)`; `build_deploy_spec` uses `get_embedding_entry`.
- `apps/memory/src/tree/models/modal_router.py` — NEW. `classify_endpoint_refusal`, `parse_endpoint_catalog`, `hf_base_models` (httpx, ≤2 hops), `run_deploy` (the 0-4 algorithm + the `Routing …` line), `run_stop` (path-blind), `hint_if_gated`. No `subprocess`, no `modal` import.
- `apps/memory/src/tree/models/modal_cli.py` — annotations widened to `ModalModelConfig`; new `ModalResult` type alias (so the router needs no `subprocess` import) and `read_existing_kind(entry, force)`; `guard_deploy(..., kind=None)` accepts a pre-read kind (the router lists the workspace ONCE); the refusal now points at `make memory-deploy-model-stop MODEL=<id>` with no `SERVING=`.
- `apps/memory/scripts/modal_embedding_model.py` → `git mv` `apps/memory/scripts/modal_model.py` — rewritten as glue over the router; `test` exits 2 on an LLM entry.
- `apps/memory/Makefile` — `deploy-embedding-model*` (3) → `deploy-model`, `deploy-model-test`, `deploy-model-stop`.
- `apps/memory/configs/default.yaml` — `serving:` / `base_model:` removed from both embedding seeds; `llm_models` added (`Qwen/Qwen3.5-0.8B`, `LiquidAI/LFM2.5-350M`).
- `apps/memory/deploy/modal_vllm_embedding.py`, `apps/memory/deploy/modal_sglang_embedding.py` — `EMBEDDING_MODEL` → `MODAL_MODEL`, docstring usage lines (the SGLang script is the deliberate orphan until #147).
- `apps/memory/src/tree/models/modal_server.py`, `modal_embedding.py` — **Embedding catalog** → **Modal catalog**, `get_embedding_entry`, `make memory-deploy-model` hint.
- `apps/memory/README.md` — `## Modal embedding deployment` → `## Serving models on Modal (optional)`: the ladder, `serving:` and the "Fallback scripts" paragraph deleted; the routing + `Routing …` lines, `SERVING=` as an escape hatch, path-blind `-stop`.
- `apps/memory/pyproject.toml` + `uv.lock` — `aiohttp>=3.10` declared in the BASE dependencies (carry-over from #144's QA; `tree.models.voyage_embedding` imports it at module level on the default path, so it is not a `local-models` extra). `uv lock` → "Resolved 247 packages", 2 lines added to `uv.lock`.
- `apps/memory/tests/unit/models/test_modal_router.py` — NEW (34 tests): the live boxed/wrapped refusal texts, the 44-line list fixture, `httpx.MockTransport` lineage tests, the import check.
- `apps/memory/tests/unit/scripts/test_modal_embedding_model_script.py` → `git mv` `test_modal_model_script.py` — rewritten (86 tests): `TestRouter` (13 cases), `TestDeployGuard` matrix on `endpoint|app`, `TestStop`, `TestDryRun`, token/hint/capture suites.
- `apps/memory/tests/unit/conftest.py` — the shared `run` fixture gained `state.results`, a QUEUE for the up-to-three commands a routed deploy runs (empty falls back to `state.result`, so every #143/#144 test is untouched).
- `apps/memory/tests/unit/config/test_app_config.py`, `tests/unit/config/fixtures/frozen_config.yaml`, `tests/unit/models/test_modal_catalog.py`, `test_modal_cli.py`, `test_modal_server.py`, `test_modal_embedding.py`, `test_dimensions.py`, `tests/unit/deploy/test_modal_embedding_scripts.py` — updated for the new shape.

**Tests**
- Unit: 3616 passing, 0 failing — `make memory-tests` (LOCAL env).
- Integration: N/A — this repo has no integration suite (AGENTS.md); the deploy path is proven by the mocked driver tests + the three real `DRY_RUN=yes` runs below.

**Acceptance criteria**
- [x] Retired fields gone — survivors of the AC grep listed under Notes; `test_retired_fields_are_rejected` covers both `serving:` and `base_model:`.
- [x] `test_seed_entries` — both lists, `kind`, `ep-tree-qwen3-5-0-8b` / `ep-tree-lfm2-5-350m`.
- [x] `test_uniqueness_spans_both_lists`, `test_n_gpus_must_be_positive`, `test_tp_flags_are_builder_owned`.
- [x] `TestGetCatalogEntry` — both lists, the two-group unknown-id message, both kind errors.
- [x] `TestClassifyEndpointRefusal` — the live texts, boxed AND wrapped; auth/quota/network/empty → `other`.
- [x] `TestParseEndpointCatalog` — 44 ids, `•`/`*` bullets, Rich markup, garbage → `[]`.
- [x] `TestHfBaseModels` — string/list/missing, skipped relations, 2-hop walk (2 requests), 404/timeout/invalid JSON → `[]` + WARNING, Bearer iff a token is set, never logged.
- [x] `TestRouter` — all 13 cases (see Notes for the two deliberate substitutions).
- [x] `TestStop` — endpoint-only, fall-through with the `Not a Dedicated endpoint (…)` INFO, the app stop's code, `SERVING=app`, un-prefixed name → exit 2 / 0 calls.
- [x] `TestDryRun` — the create argv + the undecided line per kind, `stop` logs both, 0 `subprocess.run` calls, and no Hub call either.
- [x] `make -n` checks (pasted below); `grep -rn "deploy-embedding-model\|modal_embedding_model\.py\|\"EMBEDDING_MODEL\"" apps/memory Makefile` → 0 hits.
- [x] The three real `DRY_RUN=yes` runs — output pasted below, exit 0, no `modal` process.
- [x] `grep -c "Embedding catalog" -r apps/memory/src apps/memory/scripts apps/memory/deploy` → 0; README Modal section: `three`/`ladder`/`fallback` → 0, `Routing` → 3.
- [x] `test_router_does_not_import_modal_or_subprocess`; `test_catalog_does_not_import_modal` still green.
- [x] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green (LOCAL env).

**Evidence**
```
$ make memory-tests
============================ 3616 passed in 49.25s =============================

$ make memory-format-check && make memory-lint-check && make pre-commit
309 files already formatted
All checks passed!
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed

$ make -n memory-deploy-model MODEL=x SERVING=app DRY_RUN=yes FORCE=yes
uv run python scripts/modal_model.py deploy --model "x" --serving "app" --dry-run --force
$ make -n memory-deploy-embedding-model MODEL=x
make[1]: *** No rule to make target `deploy-embedding-model'.  Stop.

$ make memory-deploy-model MODEL=voyageai/voyage-4-nano DRY_RUN=yes
DRY RUN — would run: modal endpoint create --name tree-voyage-4-nano --model voyageai/voyage-4-nano --routing-region eu-west
DRY RUN — routing is undecided without Modal: on "not available for dedicated Endpoints" the next command would be: modal deploy deploy/modal_vllm_embedding.py
DRY RUN — skipped the Modal existence check (no modal process is started).
EXIT=0

$ make memory-deploy-model MODEL=LiquidAI/LFM2.5-350M DRY_RUN=yes
DRY RUN — would run: modal endpoint create --name tree-lfm2-5-350m --model LiquidAI/LFM2.5-350M --routing-region eu-west
DRY RUN — routing is undecided without Modal: on "not available for dedicated Endpoints" the next command would be: modal deploy deploy/modal_sglang_llm.py
DRY RUN — skipped the Modal existence check (no modal process is started).
EXIT=0

$ make memory-deploy-model-stop MODEL=Qwen/Qwen3.5-0.8B DRY_RUN=yes
DRY RUN — would run: modal endpoint stop -y tree-qwen3-5-0-8b
DRY RUN — would run: modal app stop -y ep-tree-qwen3-5-0-8b
EXIT=0
```

**Notes**
- NO `modal` command was run by this task (the HARD SAFETY RULE). The three `DRY_RUN=yes` runs above start no process — proven first by `TestDryRun` (`run.assert_not_called()`) and by `test_a_dry_run_reads_neither_modal_nor_the_hub`, which also proves a dry run makes no Hugging Face request.
- **AC-1 grep survivors** (all allowed — the RUNTIME base or the `SERVING=` parameter, never a config field): `modal_catalog.py` `modal_cli_command(..., base_model=None)` + `hf_token_args(base_model, token)` and their docstrings; `modal_router.py` `_card_base_model` / `cardData.base_model` / `base_model=base` and the `serving: str = ""` parameters of `run_deploy` / `run_stop`; `scripts/modal_model.py` `def deploy(model, serving, …)` / `def stop(...)` (the Click options); plus two docstring sentences in `app_config.py` and `modal_catalog.py` that state the fields no longer exist.
- **`MODAL_TEMPLATES_REFERENCE.md` does not exist in this repo** (searched `docs/`, `tasks/`, the whole tree). The refusal texts in `tests/unit/models/test_modal_router.py` are the ones recorded in `tasks/145` itself and in ADR-009's Context, re-boxed/wrapped by hand to prove the normalisation; the 44-id list fixture uses the six ids ADR-009 / the glossary / this task actually record (`Qwen/Qwen3-Embedding-0.6B`, `-8B`, `Qwen/Qwen3.5-0.8B`, `Qwen/Qwen3.6-35B-A3B-FP8`, `google/gemma-3-1b-it`, `openai/gpt-oss-120b`) and pads the rest to 44 with ids marked synthetic in a comment. #141 can paste the real list over it.
- **Two deliberate substitutions in `TestRouter`**: (a) the custom-weights cases use `voyageai/voyage-4-nano` with a FAKED Hub card naming `Qwen/Qwen3-Embedding-0.6B` instead of `Octen/Octen-Embedding-0.6B` — the seed catalog holds no fine-tune, and these tests go through the CLI, which only resolves catalog ids; the argv shape and the `Routing …` line are asserted verbatim. (b) The SGLang App route is exercised with both App scripts faked into a `tmp_path` (the real `deploy/modal_sglang_llm.py` arrives in #147); running from a directory without it is a separate test asserting exit 2.
- **HF lineage, verified read-only against the public API on 2026-09-20** (no token, no writes): `LiquidAI/LFM2.5-350M` → `cardData.base_model == "LiquidAI/LFM2.5-350M-Base"` (string), `Qwen/Qwen3.5-0.8B` → `["Qwen/Qwen3.5-0.8B-Base"]` (list), `voyageai/voyage-4-nano` → absent, `Octen/Octen-Embedding-0.6B` → `"Qwen/Qwen3-Embedding-0.6B"`; the `sha` of `main` matched the revisions this task pins for both LLM seeds. Every unit test mocks the HTTP.
- **Trade-off, named:** a lineage hop that fails mid-walk returns `[]` (not the ancestors read so far), so the WARNING stays the one pinned sentence and there is one code path instead of two. The cost is one App deploy instead of one endpoint in the rare "hop 1 ok, hop 2 404" case — the safe direction. Upgrade: return the partial walk and split the message if a real fine-tune chain ever hits it.
- **Small change to #143's `guard_deploy`** (additive, default unchanged): a `kind: ExistingKind | None = None` parameter plus `read_existing_kind(entry, force)`, so the router reads `modal endpoint list --json` + `modal app list --json` ONCE and hands the reading to the guard. Without it a routed deploy would list the workspace twice (4 read-only calls); ADR-009 promises two. Pinned by `test_the_workspace_is_listed_once_per_deploy` and `test_a_pre_read_kind_costs_no_second_lookup`.
- **One `Routing …` shape beyond the six in the task**, for a verdict the task did not enumerate: `not_servable` on the FIRST create (no custom weights in that argv, so Modal should never answer it) → `Routing <id>: Modal refused the weights (not a servable checkpoint) → vLLM App`. It routes to the App rather than aborting because `not_servable` is one of the two "ineligible" verdicts (ADR-009 §2), not an unknown failure. The step-2 phrase is parameterised (`not a servable checkpoint` / `not in the catalog either`) so the pinned example matches verbatim.
- **`SERVING=endpoint` includes step 2** (per Scope: "steps 0-2"): a refusal is only a failure after the custom-weights attempt, and it never falls back to the App. `SERVING=endpoint` with a live App of ours is refused by the guard (exit 3) rather than silently redeployed — the auto route is the only one that redeploys a live App.
- `EMBEDDING_DEPLOY_SPEC` and `build_deploy_spec(..., engine)` are untouched, as the task requires; `deploy/modal_sglang_embedding.py` stays green and orphaned for #147.
- `aiohttp` is now declared in the base dependencies, NOT in `local-models`: `voyage_embedding.py` imports it at module level on the default (non-Modal) path, while `modal_warmup` / `modal_server` are imported lazily.
- `httpx` was already a base dependency, so the Hub GET added no new one.
- Adjacent thing noticed, NOT changed (no task): `modal_server.resolve_server_url(entry: ModalEmbeddingModelConfig)` still has the embedding-only annotation; #148's `ModalLLM` will need it widened to `ModalModelConfig` (one line, no behaviour change).

### [SWE] 2026-09-20 18:55 — Amendment (self-review, before the Tester)

Three gaps found re-reading Scope against the implementation; all three fixed, `make memory-tests` 3617 passing, `make memory-format-check && make memory-lint-check && make pre-commit` green.

1. **The sixth pinned `Routing …` shape was missing.** `SERVING=endpoint` logged no decision line at all (and the first version of `test_serving_endpoint_turns_a_refusal_into_a_failure` pinned that absence). Now `Routing <id>: SERVING=endpoint → Dedicated endpoint only (no App fallback)` is logged UP FRONT, symmetrically with `SERVING=app`, and it REPLACES the derived line: the two endpoint-success lines (`Modal accepted it`, `fine-tune of <base>`) are suppressed under that override, so a command still carries exactly ONE `Routing` line. Pinned by `test_serving_endpoint_turns_a_refusal_into_a_failure` (refusal) and the new `test_serving_endpoint_logs_one_line_on_success_too`.
2. **The `modal command failed (exit N): <redacted argv>` summary had regressed** to the two create paths only — a failing `modal deploy` (App) or `modal app stop` returned its code silently, which #139/#142 did not. Restored for EVERY non-zero command via one `_report_failure(argv, result)` (`_abort` = it + the gated hint). Deliberate exception: the endpoint stop that a path-blind stop then falls through from gets only the `Not a Dedicated endpoint (…)` INFO — it is how the stop LEARNS the route, not a failure of the command. Pinned by the extended `test_a_failing_app_deploy_propagates_modals_exit_code` and `test_the_app_stops_exit_code_is_the_commands` (exactly ONE summary line, naming the app stop).
3. Removed a dead `run.state.results = [...]` assignment (overwritten two lines later) in `test_a_failing_app_deploy_propagates_modals_exit_code`.

The three `DRY_RUN=yes` runs were re-run after the change and are unchanged (`make memory-deploy-model MODEL=voyageai/voyage-4-nano DRY_RUN=yes` → same three lines, exit 0).

### [Tester] 2026-09-20 19:40 — QA

**Safety-rail read before anything else.** Read `src/tree/models/modal_router.py`, `src/tree/models/modal_cli.py`, `scripts/modal_model.py` in full and grepped `subprocess\|os\.system\|Popen\|os\.exec` over `src` + `scripts`: `modal_cli.py` is the ONLY file with a real `subprocess.run` call; `run_modal` checks `is_dry_run` and returns `None` BEFORE that call; `run_deploy`/`run_stop` both check `is_dry_run` before `read_existing_kind`/any `list --json` call; the Hub GET (`hf_base_models`) is reachable only from `_catalog_base`, itself only reachable after a live (non-dry-run) step-1 refusal. All three conditions (a)-(c) held, so I ran the permitted `DRY_RUN=yes` commands below and nothing else `modal`-shaped.

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check`, `make memory-lint-check`, `make pre-commit` — all green)
- Unit tests: 3617 passed / 0 failed (`make memory-tests`)
- Integration tests: N/A (no integration suite per AGENTS.md)
- Warnings: 0

**Commands run (exact)**
```
make env-status                                                        # -> Env target: local (.env)
make memory-format-check
make memory-lint-check
make pre-commit
make memory-tests                                                      # 3617 passed in 46.10s
cd apps/memory && uv run pytest tests/unit/config/test_settings_credentials_only.py -q   # 9 passed (also ran first, alphabetically, inside make memory-tests)
make -n memory-deploy-model MODEL=x SERVING=app DRY_RUN=yes FORCE=yes   # -> uv run python scripts/modal_model.py deploy --model "x" --serving "app" --dry-run --force
make -n memory-deploy-embedding-model MODEL=x                          # -> make[1]: *** No rule to make target `deploy-embedding-model'. exit 2
make memory-deploy-model MODEL=voyageai/voyage-4-nano DRY_RUN=yes      # -> 3 DRY RUN lines, exit 0 (see below)
make memory-deploy-model MODEL=LiquidAI/LFM2.5-350M DRY_RUN=yes        # -> 3 DRY RUN lines, exit 0 (see below)
make memory-deploy-model-stop MODEL=Qwen/Qwen3.5-0.8B DRY_RUN=yes      # -> 2 DRY RUN lines, exit 0 (see below)
grep -rn "ServingPath\|resolve_serving\|fallback_script\|base_model\b.*catalog\|serving:" apps/memory/src apps/memory/configs apps/memory/scripts apps/memory/README.md
grep -rn "deploy-embedding-model\|modal_embedding_model\|EMBEDDING_MODEL" apps/memory --exclude-dir=.venv --exclude-dir=.git
grep -rc "Embedding catalog" apps/memory/src apps/memory/scripts apps/memory/deploy
grep -n "## Serving models on Modal" -A 100 apps/memory/README.md | grep -ci "three\|ladder\|fallback"   # -> 0
grep -c "Routing" apps/memory/README.md                                # -> 3
uv run python -c "import sys; import tree.models.modal_router, tree.models.modal_catalog, tree.models.get_model; print('modal' in sys.modules)"   # -> False
```
(plus scratch scripts under the session scratchpad, never inside the repo — see below.)

**E2E adversarial pass**
- Happy path: `make memory-deploy-model MODEL=Qwen/Qwen3-Embedding-0.6B DRY_RUN=yes` (via the `TestRouter`/`TestDryRun` suite, since a real, non-dry run is out of scope for QA per the ABSOLUTE RULES) → logs `DRY RUN — would run: modal endpoint create --name tree-qwen3-embedding-0-6b --model Qwen/Qwen3-Embedding-0.6B --routing-region eu-west`, exit 0 (PASS)
- Break path 1 (real ground-truth fixture bytes fed to `classify_endpoint_refusal`/`parse_endpoint_catalog` via a bare `uv run python` scratch script): the orchestrator-captured `real_not_servable_boxed.txt` (phrase split across two lines by the box) → `not_servable`; `real_not_in_catalog_boxed.txt` (44 real ids incl. both Qwen3-Embedding ids, `openai/gpt-oss-120b`, `Qwen/Qwen3.5-0.8B`) → `not_in_catalog` + all 44 ids parsed correctly. Repeated with Windows CRLF line endings (both still correct), with ANSI colour escape codes wrapped around the whole box (still correct), and with a synthetic narrower box that wraps a repo id itself across two lines (degrades to `[]` for that entry only, per the best-effort design — not a crash). PASS
- Break path 2 (stdout vs stderr — the SWE's flagged unverified point): built `FakeResult`/merged-text harness mirroring `_log_modal_output`'s exact `f"{stdout or ''}{stderr or ''}"` expression and fed the real fixture bytes on stdout-only, stderr-only, and a worst-case no-separator glue (stdout ending mid-word with no trailing newline, immediately followed by stderr's box or a bare bullet list with no box at all). All four variants classified and parsed identically (`not_in_catalog`/`not_servable`, 44 ids). The design reads BOTH streams (merged, unconditionally) and is structurally insensitive to which stream Modal actually uses, because `classify_endpoint_refusal` flattens all whitespace before matching and `parse_endpoint_catalog` scans line-by-line with a substring `in` check on the header rather than an equality check — so a glued prefix on the header line does not break header detection. PASS, with a hardening NOTE below (not blocking).
- Break path 3 (lineage walk edges, via a scratch `httpx.MockTransport` harness): a cycle A→B→A terminates cleanly at 2 hops (`["acme/b", "acme/a"]`, no infinite loop); a 10-node chain returns exactly 2 ancestors and makes exactly 2 requests (hop cap respected); a mid-walk failure (hop 1 OK, hop 2 404s) returns `[]` — confirmed byte-for-byte against the SWE's documented trade-off note ("a lineage hop that fails mid-walk returns `[]`, not the ancestors read so far"). PASS
- Break path 4 (parser robustness / perf): `parse_endpoint_catalog` on a 100,000-line synthetic bullet list parses all 100,000 ids in 0.05s (no pathological behaviour); control characters (`\x00\x01\x02`) and a 100KB single-line string both classify to a sane verdict (`other` / `not_in_catalog`) without raising. PASS

**Acceptance criteria**
- [x] PASS — AC-1 (retired fields grep + `test_retired_fields_are_rejected`) — grep survivors match exactly what the SWE listed (router/catalog's `base_model` RUNTIME parameter, the `SERVING=` CLI option, two docstring sentences); `test_retired_fields_are_rejected` passes in `make memory-tests`.
- [x] PASS — `test_seed_entries` — read `configs/default.yaml`'s `modal:` block: both embedding seeds have `serving:`/`base_model:` removed, `llm_models` added with `Qwen/Qwen3.5-0.8B` (rev `2fc06364715b967f1860aea9cf38778875588b17`) and `LiquidAI/LFM2.5-350M` (rev `9e6c6ccf47cd318696e137d381a7ded8fe4df09f`, `A10`, `n_gpus: 1`, `max_model_len: 32768`); test asserts `kind`/`app_name` and passes.
- [x] PASS — `test_uniqueness_spans_both_lists`, `test_n_gpus_must_be_positive`, `test_tp_flags_are_builder_owned` — read `app_config.py`'s `_check_entries_are_unique` (spans both lists) and `_BUILDER_OWNED_SERVER_ARGS` (includes `--tp`/`--tp-size`/`--tensor-parallel-size`); tests pass.
- [x] PASS — `TestGetCatalogEntry` — read `modal_catalog.get_catalog_entry`/`get_embedding_entry`/`get_llm_entry`: one lookup over both lists, sorted-both-groups unknown-id message, kind-check errors; tests pass.
- [x] PASS — `TestClassifyEndpointRefusal` — verified independently against the REAL orchestrator-captured fixture bytes (not just the SWE's hand-built ones), including CRLF and ANSI-wrapped variants (see Break path 1); both fixture files verdict correctly; the four `other` cases (auth/quota/network/empty) pass in the suite.
- [x] PASS — `TestParseEndpointCatalog` — verified against the real 44-id fixture independently (see Break path 1); bullets (`-`/`*`/`•`) and Rich markup parse per the suite.
- [x] PASS — `TestHfBaseModels` — verified independently via a scratch `httpx.MockTransport` harness: string/list/missing base_model, skipped relations, 2-hop cap, cycle-safety, mid-walk-failure semantics, Bearer-iff-token, all match; suite passes too.
- [x] PASS — `TestRouter` (13 cases) — read all 13 in `tests/unit/scripts/test_modal_model_script.py::TestRouter`; argv sequences, exit codes and `Routing …` lines match ADR-009 §2 and the task's Scope verbatim, including the SWE's disclosed extra `not_servable`-on-first-create shape (which is ADR-sanctioned: ADR-009 §2 step 3 says "not a servable checkpoint... -> deploy the APP", with no carve-out for which create attempt produced it). Suite passes.
- [x] PASS — `TestStop` — read and passes; path-blind fall-through, `SERVING=` variants, un-prefixed-name exit 2 all present.
- [x] PASS — `TestDryRun` — read and passes; additionally re-verified live via the three real `DRY_RUN=yes` invocations below, byte-identical to the SWE's pasted evidence.
- [x] PASS — `make -n` pair + grep — reran both `make -n` commands and the `deploy-embedding-model\|modal_embedding_model\|EMBEDDING_MODEL` grep myself: identical output to the SWE's, 0 hits.
- [x] PASS — the three real `DRY_RUN=yes` runs — reran all three myself (see Evidence below); output byte-identical to the SWE's pasted log, exit 0, confirmed by static read that no `modal` process starts and no Hub request is made under `--dry-run`/`DRY_RUN=yes`.
- [x] PASS — `grep -c "Embedding catalog"` → 0 across every file in `src`/`scripts`/`deploy` (reran, confirmed); README Modal section `three`/`ladder`/`fallback` → 0, `Routing` → 3 (reran, confirmed).
- [x] PASS — `test_router_does_not_import_modal_or_subprocess` passes; independently confirmed `modal` never enters `sys.modules` after importing `modal_router`, `modal_catalog` and `get_model`.
- [x] PASS — `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` — reran all four, all green, 3617 passed (LOCAL env, `make env-status` confirmed `local` throughout).

**Evidence**
```
$ make memory-tests
============================ 3617 passed in 46.10s =============================

$ make memory-deploy-model MODEL=voyageai/voyage-4-nano DRY_RUN=yes
DRY RUN — would run: modal endpoint create --name tree-voyage-4-nano --model voyageai/voyage-4-nano --routing-region eu-west
DRY RUN — routing is undecided without Modal: on "not available for dedicated Endpoints" the next command would be: modal deploy deploy/modal_vllm_embedding.py
DRY RUN — skipped the Modal existence check (no modal process is started).
EXIT=0

$ make memory-deploy-model MODEL=LiquidAI/LFM2.5-350M DRY_RUN=yes
DRY RUN — would run: modal endpoint create --name tree-lfm2-5-350m --model LiquidAI/LFM2.5-350M --routing-region eu-west
DRY RUN — routing is undecided without Modal: on "not available for dedicated Endpoints" the next command would be: modal deploy deploy/modal_sglang_llm.py
DRY RUN — skipped the Modal existence check (no modal process is started).
EXIT=0

$ make memory-deploy-model-stop MODEL=Qwen/Qwen3.5-0.8B DRY_RUN=yes
DRY RUN — would run: modal endpoint stop -y tree-qwen3-5-0-8b
DRY RUN — would run: modal app stop -y ep-tree-qwen3-5-0-8b
EXIT=0

$ git status --short   # confirms no scratch file landed in the repo, before and after QA
 M apps/memory/Makefile
 ... (unchanged from the SWE's diff — see git diff --stat)
```

**Other issues found**
- **Hardening note, non-blocking:** `_log_modal_output` merges `stdout`/`stderr` with no separator (`f"{stdout or ''}{stderr or ''}"`). Empirically this never corrupts a real verdict or the id list (verified against the real fixtures and worst-case synthetic glue — see Break path 2), because both parsers operate on flattened/substring-matched text rather than exact line equality. Still, a `"\n".join(filter(None, [stdout, stderr]))` would be more robust against a future Modal output shape that isn't box-bordered. Suggest as a follow-up, not required for this task.
- **Recommendation (non-blocking, per the task's own instruction):** vendor the two real orchestrator-captured fixture files (`real_not_servable_boxed.txt`, `real_not_in_catalog_boxed.txt`) into `tests/unit/models/test_modal_router.py` as a fixture, since they passed cleanly against the current classifier/parser and give the suite provenance beyond the hand-built/padded 44-id list (`FULL_CATALOG_REFUSAL` currently pads 38 of 44 ids with `acme/filler-model-N`). Does not block this task; #141 already owns "paste the real list over it" per the SWE's Notes.
- No security regressions found: token redaction (`redact_argv`, `redact_text`) verified structurally sound and covered by tests; no secrets read or logged during this QA pass (`.env`/`.env.prod` never opened).
- SWE-disclosed point (3), `not_servable` on the first create → App: matches ADR-009 §2 step 3 ("...or there is no ancestor in its list -> deploy the APP for the entry's kind"); ADR-sanctioned, not a deviation.
- SWE-disclosed point (4), mid-walk lineage failure → `[]`: independently reproduced and matches the documented trade-off exactly (see Break path 3).
- SWE-disclosed point (5), `SERVING=endpoint` + a live App: covered by `TestDeployGuard::test_the_matrix_without_force`'s `serving == "endpoint" and existing == "app"` case (refuses, exit 3) — read and confirmed present, matches the task's Scope text.
- SWE-disclosed point (6), `resolve_server_url`'s embedding-only annotation: correctly out of scope for #145, deferred to #148 as noted.

**VERDICT: PASS**
