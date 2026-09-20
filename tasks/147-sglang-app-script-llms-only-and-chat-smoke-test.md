---
id: 147-sglang-app-script-llms-only-and-chat-smoke-test
status: pending
feature: voyage-4-and-modal-embedding-catalog
---

# The SGLang App script serves LLMs only, following Modal's own LLM template (official `lmsysorg/sglang` image) — plus a chat smoke test behind `memory-deploy-model-test`

Tags: `modal`, `deploy`, `sglang`, `llm`
Depends on: #144, #145, #146
Blocks: #148, #141
Implements: ADR-009 — Decision 2 (App by kind: SGLang = LLMs) and Decision 10 (LLMs on Modal: deploy half)

## Scope

**EXECUTION ORDER of the rework: 143 -> 144 -> 145 -> 146 -> 147 -> 148 -> 149 -> 150 -> 141.**

**HARD SAFETY RULE:** No agent may run `make memory-deploy-*`, `scripts/modal_*.py` as a process, or any `modal …` command outside task 141; a PATH shim does NOT intercept under `make`/`uv run` because `.venv/bin` wins — use the mocked unit tests and `DRY_RUN=yes`.
A deploy script is NEVER executed here — only parsed (`ast`) and imported under a mocked `modal`.

**The human's decision (settled).** "For custom LLMs like LiquidAI/LFM2.5-350M (larger or smaller) always go
with SGLang — optimise the SGLang script for LLMs from Hugging Face, not embedding models." Read
`MODAL_TEMPLATES_REFERENCE.md`, section "LLMs -> SGLang": the facts of the two `serve.py` files Modal generates
for `openai/gpt-oss-120b` and `Qwen/Qwen3.6-35B-A3B-FP8`.

**What this task rewrites (committed code from #142/#145/#146):**
- `deploy/modal_sglang_embedding.py` -> `git mv deploy/modal_sglang_llm.py`, REWRITTEN (its embedding mode —
  `--is-embedding`, `validate_embeddings_endpoint`, the pip-installed `sglang` on a CUDA base — is REMOVED).
- `src/tree/models/modal_catalog.py`: `EmbeddingDeploySpec` stays for embeddings; NEW `LLMDeploySpec` +
  `build_llm_deploy_spec(model)`; `build_server_args`' `sglang` branch and `FallbackEngine` are DELETED
  (`build_deploy_spec(model)` loses its `engine` parameter — the vLLM script calls it with one argument);
  the kind -> script map gains `"llm": "deploy/modal_sglang_llm.py"`.
- `ModalConfig.engines`: `sglang.version` `"0.5.20"` (a PyPI version) -> `"v0.5.18"` (a DOCKER TAG of
  `lmsysorg/sglang`, Modal's pin); YAML comment + `frozen_config.yaml` follow.
- `src/tree/models/modal_server.py`: NEW `chat_smoke_test`; the driver's `test` command becomes kind-aware
  (replaces #145's "No smoke test for LLM entries yet" exit 2).
- `tests/unit/deploy/test_modal_embedding_scripts.py` -> `git mv test_modal_deploy_scripts.py`; its script
  table becomes `{"vllm": "deploy/modal_vllm_embedding.py", "sglang": "deploy/modal_sglang_llm.py"}`;
  per-engine expectations split where they differ (image, endpoint class, warm-up call, spec builder).
- README's Modal section: one paragraph + the LLM example. Every reference to the old file name
  (`grep -rn "modal_sglang_embedding" . --exclude-dir=done`): `modal_catalog.py`, the static test file,
  README, `configs/default.yaml` comments.

**A. `deploy/modal_sglang_llm.py` — follow Modal's LLM template, generic-safe flags only.**
- Image: `modal.Image.from_registry(f"lmsysorg/sglang:{SPEC['engine_version']}")
  .uv_pip_install(f"autoinference-utils=={…}").env({"HF_XET_HIGH_PERFORMANCE": "1", <spec env var>})` — the
  OFFICIAL SGLang image: no `add_python`, no `.entrypoint([])`, no pip-installed engine. **SWE must verify**
  (Docker Hub tag list + SGLang's install docs via the `tech-docs` skill; grooming saw the tags `v0.5.18` and
  `v0.5.18-cu130` on Docker Hub, 2026-09-20): which tag fits the seed's GPU. Modal's own recipes use the plain
  tag on B200 and `-cu130` on H200; for an `A10` start with the plain `v0.5.18` and say in `## Log` why. If
  `uv_pip_install` cannot find a Python in that image, record it and use `.pip_install` — a fact only #141
  can prove live; leave a `# proven live in tasks/141` marker on the line.
- `GPU = f"{gpu}:{n_gpus}"`; `SGLangEndpoint(model_path=<repo_id>, worker_port=8000, tp=<n_gpus>,
  extra_server_args=<server args>, health_timeout=…, health_poll_interval=5.0)` — NO
  `speculative_model_path` (leave the parameter out; **SWE must verify** in the unpacked wheel
  `aiu_src/` that it is optional in `autoinference-utils==0.2.6`).
- Server args the BUILDER owns: `--served-model-name <repo_id>`, `--revision <revision>`,
  `--trust-remote-code ""`, `--mem-fraction-static 0.85`, and `--context-length <max_model_len>` when set.
  `--mem-fraction-static` and `--trust-remote-code` may be overridden per model through `extra_server_args`
  (they are NOT builder-owned keys); `--tp*`, `--model-path`, `--served-model-name`, `--context-length`,
  `--is-embedding` are (#145). OPTIONAL per model via YAML `extra_server_args`: `--reasoning-parser`,
  `--tool-call-parser`. NO speculative decoding (`--speculative-*`), NO mamba flags, NO
  `--enable-multimodal`, NO Modal-specific image env (`SGLANG_ENABLE_JIT_DEEPGEMM` …) as defaults — those are
  Modal's MODEL-SPECIFIC tuning for a 120B and a 35B-MoE model; a generic script must not assume them.
- Warm-up: `warmup_chat_completions(port=8000, payload=WARMUP_PAYLOAD, successful_requests=2,
  request_timeout=60.0)` with Modal's STRICT JSON-schema payload, verbatim: model = the repo id, one user
  message `"Reply with JSON facts about Tokyo."`, `max_tokens: 64`, `temperature: 0`, `response_format` =
  `json_schema` named `city_facts` — object with `city: string`, `population: integer`, both required,
  `additionalProperties: false`, `strict: true`. A server that cannot do constrained JSON never goes healthy.
- Everything #142 established STAYS: the `modal.is_local()` split (`tree` only importable locally), the spec
  as ONE JSON env var baked into the image (rename the var `EMBEDDING_DEPLOY_SPEC` -> keep it for the vLLM
  script; the LLM script uses `LLM_DEPLOY_SPEC`), the ephemeral `HF_SECRET`, the first-line
  `HF_TOKEN set in container: %s` boolean, `unauthenticated=False` as a literal, `class Server`, app name
  `ep-tree-<slug>`, `port=8000`, `min_containers=0`, `scaledown_window=5 * MINUTES`, `exit_grace_period=25`,
  `routing_region="eu-west"`, the shared `huggingface-cache` Volume, stdlib logging, typed defs.
- `LLMDeploySpec`: `repo_id`, `revision`, `app_name`, `server_name`, `gpu`, `n_gpus`, `cpu`, `memory_mb`,
  `engine_version`, `autoinference_utils_version`, `server_args`. Configuration only — never a credential.

**B. The seed — `LiquidAI/LFM2.5-350M`.** Verified at grooming (read-only HTTP, 2026-09-20): architecture
`Lfm2ForCausalLM` (`config.json`), `max_position_embeddings: 128000`, bfloat16, ungated; SGLang registers
`Lfm2ForCausalLM` in `python/sglang/srt/models/lfm2.py` at tags `v0.5.18` AND `v0.5.20` (`EntryClass`).
**SWE must verify** on SGLang's supported-models docs (`tech-docs` skill) that LFM2 text generation is listed
for the chosen tag and whether the hybrid conv layers need a flag there; confirm `A10` + `max_model_len 32768`
from #145. **If SGLang cannot serve LFM2.5 at that tag: say so in CAPITALS at the top of `## Log`, and replace
the seed with `Qwen/Qwen2.5-0.5B-Instruct`** (the smallest widely used SGLang-supported instruct model;
architecture `Qwen2ForCausalLM`, ungated, Apache-2.0 — NOT in Modal's endpoint list, so it still routes to the
App): YAML + `test_seed_entries` + `frozen_config.yaml`, and write `PA: glossary + ADR-009 need the LLM seed
<old -> new>` in `## Log`.

**C. `chat_smoke_test(model, deadline_s=None) -> ChatSmokeTestReport`** in `modal_server.py`, path-blind like
the embedding one (an endpoint-routed `Qwen/Qwen3.5-0.8B` and the SGLang App answer the same API):
`get_llm_entry` -> `resolve_server_url` (WIDEN its parameter from `ModalEmbeddingModelConfig` to both entry
classes / their shared base — it only reads `app_name` and `repo_id`) -> `poll_health` (#144) -> `served_model_id` -> ONE
`POST /v1/chat/completions` with the SAME strict `city_facts` payload as the script's warm-up but
`max_tokens: 256` (a reasoning model may spend tokens before the JSON), through **Proxy token** auth ->
assert HTTP 200, `choices[0].message.content` parses as JSON, is an object with exactly the keys `city` (str)
and `population` (int) -> the unauthenticated `/health` answers 401. Log lines (INFO), in order:
`health 200 after <n>s`, `served model id: <id>`, `chat completion: {"city": …, "population": …}`,
`strict JSON schema honoured: city=<str> population=<int>`, `unauthenticated health -> 401`,
`Smoke test passed`. Failures are `ModelError` with the numbers / the first 200 characters of the content
(e.g. `chat completion is not valid JSON: '<think>…'`). The driver's `test` command dispatches on
`get_catalog_entry(model).kind`; its HF hint rule is #143's.

## Out of scope
- The `ModalLLM` client and `models.llm.provider` (#148). Extraction quality on a 350M model.
- Speculative decoding, draft models, mamba / multimodal flags, tool calling, streaming, multi-node.
- LLMs SGLang cannot serve ("always SGLang"). A vLLM LLM script. An SGLang embedding mode.
- Any live deploy (#141).

## Acceptance Criteria

- [ ] `ls apps/memory/deploy/` shows `modal_sglang_llm.py` and `modal_vllm_embedding.py` and NO `modal_sglang_embedding.py`; `grep -rn "modal_sglang_embedding\|--is-embedding\|is_embedding" apps/memory/src apps/memory/deploy apps/memory/scripts apps/memory/README.md apps/memory/configs` -> only the builder-owned-keys entry in `app_config.py`.
- [ ] `test_engine_pins`: `engines["sglang"].version == "v0.5.18"` (or the `-cu130` variant, with the reason in `## Log`) in YAML, code default and `frozen_config.yaml`.
- [ ] `TestBuildLlmDeploySpec`: for `LiquidAI/LFM2.5-350M` -> `app_name == "ep-tree-lfm2-5-350m"`, `gpu == "A10"`, `n_gpus == 1`, `server_args` EXACTLY `{"--served-model-name": "LiquidAI/LFM2.5-350M", "--revision": "9e6c6ccf47cd318696e137d381a7ded8fe4df09f", "--trust-remote-code": "", "--mem-fraction-static": "0.85", "--context-length": "32768"}`; an entry with `extra_server_args: {"--reasoning-parser": "qwen3", "--mem-fraction-static": "0.75"}` yields both (the override wins); no key starts with `--speculative`, `--mamba`, `--enable-multimodal`; `build_llm_deploy_spec("voyageai/voyage-4-nano")` raises the kind error; the dumped spec contains no `HF_TOKEN` and no token value.
- [ ] Static tests for `deploy/modal_sglang_llm.py` (AST, mocked `modal`): image is `from_registry` of an f-string starting `lmsysorg/sglang:`; NO `add_python`, NO `.entrypoint(`, NO `"sglang=="` pip spec; `SGLangEndpoint(model_path=…, tp=…, health_poll_interval=5.0)` and no `speculative_model_path`; `warmup_chat_completions(… successful_requests=2, request_timeout=60.0)`; the warm-up payload equals Modal's `city_facts` payload (`strict is True`, `additionalProperties is False`, `max_tokens == 64`, `temperature == 0`); `gpu=` is built from `gpu` AND `n_gpus`; every `TestGlueContract` guard (incl. #146's literal-`False` and no-stdout guards, the `is_local()` import rule, the first-line token boolean, typed defs, `secrets=[HF_SECRET]` only) passes for it.
- [ ] `test_script_paths_match_cli_command`: kind `embedding` -> `deploy/modal_vllm_embedding.py`, kind `llm` -> `deploy/modal_sglang_llm.py`, both files exist.
- [ ] `TestChatSmokeTest` (aiohttp recorder + patched `poll_health`): happy path logs the six lines in order and returns a report with `city`/`population`; content `not json` -> `ModelError` containing `not valid JSON`; JSON missing `population` or with an extra key -> `ModelError` naming the key; `population: "many"` -> `ModelError`; HTTP 400 on the completion -> `ExtractionError` with `status_code == 400`; unauthenticated health 200 -> `ModelError` containing `the server is public`; the request body sent is the strict `city_facts` schema with `max_tokens == 256`; the Proxy token is in no log record.
- [ ] `TestTestCommandDispatch`: `test --model Qwen/Qwen3-Embedding-0.6B` awaits `smoke_test`, `test --model LiquidAI/LFM2.5-350M` awaits `chat_smoke_test`; both exit 1 on `ModelError`.
- [ ] `make memory-deploy-model MODEL=LiquidAI/LFM2.5-350M SERVING=app DRY_RUN=yes` exits 0 and logs `DRY RUN — would run: modal deploy deploy/modal_sglang_llm.py` (pasted in `## Log`).
- [ ] `## Log` answers every "SWE must verify" with its source: docker tag for the GPU, `uv_pip_install` on that image, `speculative_model_path` optional, LFM2 support at the tag (or the LOUD replacement), `warmup_chat_completions`' signature in 0.2.6. No `modal` process was started.
- [ ] `grep -c "deploy/modal_sglang_llm.py\|LiquidAI/LFM2.5-350M" apps/memory/README.md` >= 1.
- [ ] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green (LOCAL env).

## User Stories

### Story: Operator serves a small custom LLM
1. `make memory-deploy-model MODEL=LiquidAI/LFM2.5-350M`.
2. Modal refuses it as an endpoint; `Routing LiquidAI/LFM2.5-350M: not in Modal's endpoint catalog, no catalog base → SGLang App`; `Running: modal deploy deploy/modal_sglang_llm.py`.
3. In `modal app logs ep-tree-lfm2-5-350m`: `HF_TOKEN set in container: False`, the SGLang launch line with `--tp 1 --context-length 32768 --mem-fraction-static 0.85`, two successful warm-up completions.

### Story: Operator smoke-tests an LLM, however it is served
1. `make memory-deploy-model-test MODEL=LiquidAI/LFM2.5-350M` (or `MODEL=Qwen/Qwen3.5-0.8B`, a Dedicated endpoint).
2. `Warming …`, `Still cold (HTTP 503) …`, `Warm: … after 96s`, `health 200 after 96.0s`, `served model id: …`, `chat completion: {"city": "Tokyo", "population": 13960000}`, `strict JSON schema honoured: city=Tokyo population=13960000`, `unauthenticated health -> 401`, `Smoke test passed`.

### Story: Operator adds a bigger reasoning model
1. Adds an `llm_models` entry with `gpu: H100`, `n_gpus: 2`, `extra_server_args: {"--reasoning-parser": "qwen3", "--tool-call-parser": "qwen3_coder"}`.
2. The spec carries GPU `H100:2`, `tp=2` and both parsers; nothing speculative was added for them.

### Story: A model that cannot do constrained JSON
1. The served model ignores `response_format`.
2. In the container the warm-up never gets 2 successes -> the server never reports healthy; from the laptop the smoke test says `chat completion is not valid JSON: 'Sure! Tokyo is…'`, exit 1.

### Story: Someone points the LLM script at an embedding model
1. `MODAL_MODEL=voyageai/voyage-4-nano` reaches `build_llm_deploy_spec`.
2. `ModelError: voyageai/voyage-4-nano is an embedding entry (modal.embedding_models), not an LLM.` — before any image is built. (Through the driver this cannot happen: the kind picks the script.)

---

Blocked by: #144, #145, #146

## Log

### [PA] 2026-09-20 13:40 — Grooming (new task: third plan edit — D5 + the deploy half of D6)

**Summary**
The SGLang script becomes THE App for LLMs: Modal's LLM template (official image, `SGLangEndpoint(tp=…)`, strict-JSON warm-up) with generic-safe flags, its embedding mode deleted, the file renamed for intent. A chat smoke test joins the embedding one behind the same `-test` target.

**Key decisions**
- Rename to `deploy/modal_sglang_llm.py` (and the static test file to `test_modal_deploy_scripts.py`): the old names would now lie. References enumerated.
- The engine "version" for SGLang is a docker tag (`v0.5.18`), still per ENGINE.
- Modal's speculative / mamba / multimodal flags are model-specific tuning — excluded by test, not by convention.
- `--mem-fraction-static 0.85` default, overridable per model; `--trust-remote-code` on by default (Modal sets it on both recipes; LFM2 and most new architectures need it).
- The smoke test's completion uses `max_tokens: 256` (reasoning models), the in-container warm-up keeps Modal's 64.
- Separate `LLMDeploySpec` / `LLM_DEPLOY_SPEC` rather than widening the embedding spec with optional fields.

**Verified at grooming (read-only HTTP)**
- LFM2.5-350M: `Lfm2ForCausalLM`, 128000 context, bfloat16, ungated, sha `9e6c6ccf…`. SGLang `lfm2.py` exists at `v0.5.18` and `v0.5.20`. Docker Hub has `lmsysorg/sglang:v0.5.18` and `:v0.5.18-cu130`.

**Dependencies**
- #144 (`poll_health`), #145 (LLM list, kind -> script, `MODAL_MODEL`, one target family), #146 (the static guards this script must pass).

**User stories**
- 5 stories: custom LLM deploy, path-blind chat smoke test, bigger reasoning model, no constrained JSON, wrong kind.

**Open questions**
- None blocking. Replacement seed if SGLang cannot serve LFM2.5 at the tag: `Qwen/Qwen2.5-0.5B-Instruct` (procedure in Scope B).

Ready for implementation.
