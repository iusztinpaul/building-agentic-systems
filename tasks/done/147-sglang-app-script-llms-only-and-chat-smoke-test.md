---
id: 147-sglang-app-script-llms-only-and-chat-smoke-test
status: done
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

- [x] `ls apps/memory/deploy/` shows `modal_sglang_llm.py` and `modal_vllm_embedding.py` and NO `modal_sglang_embedding.py`; `grep -rn "modal_sglang_embedding\|--is-embedding\|is_embedding" apps/memory/src apps/memory/deploy apps/memory/scripts apps/memory/README.md apps/memory/configs` -> only the builder-owned-keys entry in `app_config.py`.
- [x] `test_engine_pins`: `engines["sglang"].version == "v0.5.18"` (or the `-cu130` variant, with the reason in `## Log`) in YAML, code default and `frozen_config.yaml`.
- [x] `TestBuildLlmDeploySpec`: for `LiquidAI/LFM2.5-350M` -> `app_name == "ep-tree-lfm2-5-350m"`, `gpu == "A10"`, `n_gpus == 1`, `server_args` EXACTLY `{"--served-model-name": "LiquidAI/LFM2.5-350M", "--revision": "9e6c6ccf47cd318696e137d381a7ded8fe4df09f", "--trust-remote-code": "", "--mem-fraction-static": "0.85", "--context-length": "32768"}`; an entry with `extra_server_args: {"--reasoning-parser": "qwen3", "--mem-fraction-static": "0.75"}` yields both (the override wins); no key starts with `--speculative`, `--mamba`, `--enable-multimodal`; `build_llm_deploy_spec("voyageai/voyage-4-nano")` raises the kind error; the dumped spec contains no `HF_TOKEN` and no token value.
- [x] Static tests for `deploy/modal_sglang_llm.py` (AST, mocked `modal`): image is `from_registry` of an f-string starting `lmsysorg/sglang:`; NO `add_python`, NO `.entrypoint(`, NO `"sglang=="` pip spec; `SGLangEndpoint(model_path=…, tp=…, health_poll_interval=5.0)` and no `speculative_model_path`; `warmup_chat_completions(… successful_requests=2, request_timeout=60.0)`; the warm-up payload equals Modal's `city_facts` payload (`strict is True`, `additionalProperties is False`, `max_tokens == 64`, `temperature == 0`); `gpu=` is built from `gpu` AND `n_gpus`; every `TestGlueContract` guard (incl. #146's literal-`False` and no-stdout guards, the `is_local()` import rule, the first-line token boolean, typed defs, `secrets=[HF_SECRET]` only) passes for it.
- [x] `test_script_paths_match_cli_command`: kind `embedding` -> `deploy/modal_vllm_embedding.py`, kind `llm` -> `deploy/modal_sglang_llm.py`, both files exist.
- [x] `TestChatSmokeTest` (aiohttp recorder + patched `poll_health`): happy path logs the six lines in order and returns a report with `city`/`population`; content `not json` -> `ModelError` containing `not valid JSON`; JSON missing `population` or with an extra key -> `ModelError` naming the key; `population: "many"` -> `ModelError`; HTTP 400 on the completion -> `ExtractionError` with `status_code == 400`; unauthenticated health 200 -> `ModelError` containing `the server is public`; the request body sent is the strict `city_facts` schema with `max_tokens == 256`; the Proxy token is in no log record.
- [x] `TestTestCommandDispatch`: `test --model Qwen/Qwen3-Embedding-0.6B` awaits `smoke_test`, `test --model LiquidAI/LFM2.5-350M` awaits `chat_smoke_test`; both exit 1 on `ModelError`.
- [x] `make memory-deploy-model MODEL=LiquidAI/LFM2.5-350M SERVING=app DRY_RUN=yes` exits 0 and logs `DRY RUN — would run: modal deploy deploy/modal_sglang_llm.py` (pasted in `## Log`).
- [x] `## Log` answers every "SWE must verify" with its source: docker tag for the GPU, `uv_pip_install` on that image, `speculative_model_path` optional, LFM2 support at the tag (or the LOUD replacement), `warmup_chat_completions`' signature in 0.2.6. No `modal` process was started.
- [x] `grep -c "deploy/modal_sglang_llm.py\|LiquidAI/LFM2.5-350M" apps/memory/README.md` >= 1.
- [x] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green (LOCAL env).

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

### [SWE] 2026-09-20 17:05 — Implementation

**Headline for the "which docker tag" question: the choice does not exist.** At v0.5.18 the plain tag IS the
cu130 build. `sgl-project/sglang` `.github/workflows/release-docker.yml` at tag `v0.5.18` (read 2026-09-20)
pins `tag_config: [{"cuda": "cu130", "tags": ["v0.5.18", "latest", "v0.5.18-cu130", "latest-cu130"]},
{"cuda": "cu129", "tags": ["v0.5.18-cu129", "latest-cu129"]}]` — so `lmsysorg/sglang:v0.5.18` and
`:v0.5.18-cu130` are the SAME image under two names, and Modal's two recipes (B200 "plain", H200 "-cu130")
differ only in spelling. We use the plain `v0.5.18`. It is CUDA 13.0.3 on Ubuntu 24.04 (`docker/Dockerfile`:
`ARG CUDA_VERSION=13.0.3`, `FROM nvidia/cuda:${CUDA_VERSION}-cudnn-devel-ubuntu24.04`), and Modal's hosts run
NVIDIA driver 580.95.05 with CUDA Driver API 13.0 (https://modal.com/docs/guide/cuda.md), so cu130 runs on
every Modal GPU — A10 included. The CUDA-12 variant would be `-cu129`, which nothing here asks for.

**Files modified**
- `apps/memory/deploy/modal_sglang_llm.py` — `git mv` of `modal_sglang_embedding.py`, REWRITTEN as the LLM App:
  official image, `SGLangEndpoint(tp=SPEC["n_gpus"])`, `warmup_chat_completions` with Modal's strict
  `city_facts` payload, `gpu=f"{gpu}:{n_gpus}"`, `LLM_DEPLOY_SPEC`. The embedding mode is gone.
- `apps/memory/deploy/modal_vllm_embedding.py` — `build_deploy_spec(...)` now takes one argument.
- `apps/memory/src/tree/models/modal_catalog.py` — `DeploySpec` base + `EmbeddingDeploySpec` / NEW
  `LLMDeploySpec`; NEW `build_llm_server_args` + `build_llm_deploy_spec`; `build_server_args` is vLLM-only;
  `FallbackEngine`, the `sglang` branch and the spec's `engine` FIELD are deleted (nothing read it; the
  script IS the engine, and `LLMDeploySpec` has no such field either); `EMBEDDING_DEPLOY_SPEC_ENV` +
  `LLM_DEPLOY_SPEC_ENV`; `_merge_by_flag` for per-model overrides.
- `apps/memory/src/tree/models/modal_server.py` — NEW `chat_smoke_test` + `ChatSmokeTestReport`, `_chat`,
  `_city_facts`, `CITY_FACTS_SCHEMA`; `resolve_server_url` widened to `ModalModelConfig` (it only reads
  `app_name` / `repo_id`).
- `apps/memory/src/tree/models/modal_router.py` — carry-over (c): the captured streams are joined with a
  NEWLINE (`"\n".join(filter(None, …))`), not concatenated.
- `apps/memory/scripts/modal_model.py` — `test` dispatches on `get_catalog_entry(model).kind`; the
  "No smoke test for LLM entries yet" exit 2 is gone.
- `apps/memory/src/tree/config/app_config.py` + `configs/default.yaml` + `tests/unit/config/fixtures/frozen_config.yaml`
  — `engines.sglang.version` `"0.5.20"` -> `"v0.5.18"` (a DOCKER TAG; the field description says so).
- `apps/memory/Makefile`, `apps/memory/README.md` — the `-test` target covers both kinds; a new README
  paragraph "Serving your own LLM" with the `LiquidAI/LFM2.5-350M` entry and the smoke-test transcript.
- `apps/memory/tests/unit/deploy/test_modal_deploy_scripts.py` — `git mv` of `test_modal_embedding_scripts.py`;
  the script table is now `{vllm: modal_vllm_embedding.py, sglang: modal_sglang_llm.py}`, per-engine
  expectations split (image / endpoint kwargs / warm-up / spec builder / spec env var / vocabulary), plus the
  hardened token-safety guards and their mutants (carry-over a).
- `apps/memory/tests/unit/models/test_modal_catalog.py` — `TestLlmServerArgs`, `TestBuildLlmDeploySpec`,
  `build_deploy_spec(model)` one-argument.
- `apps/memory/tests/unit/models/test_modal_server.py` — `TestChatSmokeTest` (17 rows).
- `apps/memory/tests/unit/models/test_modal_router.py` + NEW `tests/unit/models/fixtures/real_*_boxed.txt` —
  carry-over (b): the REAL CLI refusals, vendored; the synthetic 44-bullet padding is deleted.
- `apps/memory/tests/unit/scripts/test_modal_model_script.py` — `TestTestCommandDispatch`, the split-stream
  routing regression (carry-over c), stale `#147` comments removed.
- `apps/memory/tests/unit/config/test_app_config.py` — `test_engine_pins` covers the SGLang tag in all three
  places.

**Tests**
- Unit: 3702 passing, 0 failing (`make memory-tests`, LOCAL env) — up from 3605.
- Integration: N/A — this repo has no integration suite (AGENTS.md); no infrastructure changed.

**Every "SWE must verify", with its source (all read 2026-09-20)**
1. **Docker tag for the GPU** — see the headline above. `v0.5.18` == `v0.5.18-cu130`; Modal's driver is 580.95.05
   / CUDA 13.0. *#141 verifies live:* that `sgl-kernel` in that image ships sm_86 (A10, Ampere) binaries — CUDA 13
   supports Ampere, but the image's `TORCH_CUDA_ARCH_LIST` is not in the Dockerfile, so this is not provable statically.
2. **`uv_pip_install` on that image** — kept, no `.pip_install` fallback. Modal documents the requirement as
   "the image needs `python` and `pip` installed and available on `$PATH`"
   (https://modal.com/docs/guide/existing-images.md), and sglang's `docker/Dockerfile` installs `python3.12-full`,
   runs `get-pip.py` and symlinks `/usr/bin/python` -> `python3.12`; `.from_registry(...).uv_pip_install(...)` is
   the documented chain. Modal's own LLM `serve.py` calls `uv_pip_install("autoinference-utils==0.2.6")` on this
   exact image. Entry point: the image sets `CMD ["/bin/bash"]` and NO `ENTRYPOINT`, which is why Modal's recipe
   (and this file) drops `.entrypoint([])`. *#141 verifies live* that the layer builds.
3. **`speculative_model_path` optional in autoinference-utils 0.2.6** — YES:
   `SGLangEndpoint.__init__(..., speculative_model_path: Optional[str] = None, ...)` (`endpoint.py:130`), and
   `_build_cmd` only appends `--speculative-draft-model-path` `if self.speculative_model_path is not None`
   (`:238`). The parameter is left out entirely.
4. **LFM2 at that tag — the seed STAYS `LiquidAI/LFM2.5-350M`.** `python/sglang/srt/models/lfm2.py` exists at
   `v0.5.18` and ends in `EntryClass = [Lfm2ForCausalLM, Lfm2BidirectionalModel]` (the Hub `config.json`
   architecture is `Lfm2ForCausalLM`), and `docs/docs/supported-models/generative_models.mdx` at that tag lists
   "**LFM2** (230M, 350M, 1.2B) — hybrid … gated short convolutions with a small number of GQA blocks". The
   hybrid layers use the bundled `causal_conv1d` kernels from the mamba package (`lfm2.py:23-25`,
   `Lfm2ShortConv`); no server flag is documented for them, so none is set. `A10` + `max_model_len 32768` from
   #145 confirmed (the card's context is 128k; 32k keeps the KV cache small on one A10).
5. **`warmup_chat_completions`' signature (0.2.6)** — `def warmup_chat_completions(*, port: int, payload:
   Mapping[str, Any], headers: Mapping[str, str] | None = None, successful_requests: int = 3, request_timeout:
   float = 30.0, max_attempts_per_request: int = 2, retry_delay: float = 1.0) -> None` (`endpoint.py:788-797`);
   it POSTs to `http://127.0.0.1:{port}/v1/chat/completions` and raises unless each of `successful_requests`
   POSTs succeeds. Our call passes Modal's own `successful_requests=2, request_timeout=60.0`.
6. **SGLang accepts the payload** — `python/sglang/srt/entrypoints/openai/protocol.py` at `v0.5.18`:
   `ResponseFormat.type: Literal["text", "json_object", "json_schema"]` (`:230`) with
   `JsonSchemaResponseFormat.strict: Optional[StrictBool]` (`:226`). So `json_schema` + `strict: true` is the
   wire contract, and `json_object` (what #148's JSON mode needs) is supported too.
7. **No `modal` process was started anywhere in this task.** The only executions were `make memory-*` test /
   lint targets, two `DRY_RUN=yes` dry runs (which log and return before the single `subprocess.run` door), and
   an in-process stub server on `127.0.0.1`.

**Evidence**

```
$ make memory-tests
============================ 3702 passed in 48.27s =============================

$ make memory-format-check && make memory-lint-check && make pre-commit
309 files already formatted
All checks passed!
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed

$ make env-status
Env target: local (.env)

$ TREE_MODAL_DRY_RUN=1 make memory-deploy-model MODEL=LiquidAI/LFM2.5-350M SERVING=app DRY_RUN=yes ; echo exit=$?
uv run python scripts/modal_model.py deploy --model "LiquidAI/LFM2.5-350M" --serving "app" --dry-run
DRY RUN — would run: modal deploy deploy/modal_sglang_llm.py
DRY RUN — skipped the Modal existence check (no modal process is started).
exit=0

$ TREE_MODAL_DRY_RUN=1 make memory-deploy-model MODEL=LiquidAI/LFM2.5-350M DRY_RUN=yes   # the router's own path
DRY RUN — would run: modal endpoint create --name tree-lfm2-5-350m --model LiquidAI/LFM2.5-350M --routing-region eu-west
DRY RUN — routing is undecided without Modal: on "not available for dedicated Endpoints" the next command would be: modal deploy deploy/modal_sglang_llm.py
DRY RUN — skipped the Modal existence check (no modal process is started).

$ uv run python -c 'from tree.models.modal_catalog import build_llm_deploy_spec; print(build_llm_deploy_spec("LiquidAI/LFM2.5-350M").model_dump_json(indent=2))'
{
  "repo_id": "LiquidAI/LFM2.5-350M",
  "revision": "9e6c6ccf47cd318696e137d381a7ded8fe4df09f",
  "app_name": "ep-tree-lfm2-5-350m",
  "server_name": "Server",
  "gpu": "A10",
  "cpu": 4.0,
  "memory_mb": 16384,
  "engine_version": "v0.5.18",
  "autoinference_utils_version": "0.2.6",
  "server_args": {
    "--trust-remote-code": "",
    "--mem-fraction-static": "0.85",
    "--revision": "9e6c6ccf47cd318696e137d381a7ded8fe4df09f",
    "--served-model-name": "LiquidAI/LFM2.5-350M",
    "--context-length": "32768"
  },
  "n_gpus": 1
}
```

End-to-end (Step 7) for `chat_smoke_test`, against an in-process stub on `127.0.0.1` — real `aiohttp`, real
`poll_health`, NO Modal and no `*.modal.run` request (the script lives in the session scratchpad,
`e2e_chat_smoke.py`; `modal.Server` and the Proxy token are patched to a local URL and a fake pair):

```
Warming http://127.0.0.1:55595/health — polling for up to 600s
Warm: http://127.0.0.1:55595/health answered HTTP 200 after 0s
health 200 after 0.0s
served model id: LiquidAI/LFM2.5-350M
chat completion: {"city": "Tokyo", "population": 13960000}
strict JSON schema honoured: city=Tokyo population=13960000
unauthenticated health -> 401
Smoke test passed

requests the stub saw:
  GET /health authed=True
  GET /v1/models authed=True
  POST /v1/chat/completions authed=True
    body: {"model": "LiquidAI/LFM2.5-350M", "messages": [{"role": "user", "content": "Reply with JSON facts about Tokyo."}], "max_tokens": 256, "temperature": 0, "response_format": {"type": "json_schema", "json_schema": {"name": "city_facts", "schema": {"type": "object", "properties": {"city": {"type": "string"}, "population": {"type": "integer"}}, "required": ["city", "population"], "additionalProperties": false}, "strict": true}}}
  GET /health authed=False
```

**Carry-overs the orchestrator added (all three done)**
- **(a) Token-safety guard hardening**, `tests/unit/deploy/test_modal_deploy_scripts.py`, parametrised over BOTH
  scripts. New AST helpers: `_environ_reads` (an `os.environ` node is an offender unless it is
  `os.environ["MODAL_MODEL"]`, `os.environ["<spec env>"]` or the exact `bool(os.environ.get("HF_TOKEN"))`;
  `os.getenv` / `os.environ.copy` / `os.environb` are never allowed), `_log_offenders` (every `logger.*`
  message must be a literal from a per-engine allow-list, and no argument may mention
  `environ`/`getenv`/`Secret`/`HF_` except the token boolean), a widened `_console_writes` (adds `os.write*`,
  `sys.__stdout__`, and ALIAS tracking for `p = print` / `from sys import stdout`), and `_image_env_dicts`,
  which returns EVERY `.env({...})` layer. All four of #146's surviving mutants are regression rows in
  `TestTheGuardsCatchTheirMutants` (`logger.info("%s", os.environ)`, `os.write(1, b"leak")`, `p = print; p(SPEC)`,
  a second `.env({"HF_TOKEN": …})` on another Image) plus `sys.__stdout__.write`, the token VALUE logged, and an
  unreviewed log message. Each mutant is a COPY in `tmp_path`; the shipped scripts are never edited, imported or
  executed, and `test_the_shipped_script_passes_every_guard` is the control row.
- **(b) Vendored refusal fixtures** — `tests/unit/models/fixtures/real_not_in_catalog_boxed.txt` and
  `real_not_servable_boxed.txt` (checked before copying: public model ids and Modal's own prose only, no URL,
  workspace or credential). `classify_endpoint_refusal` -> `not_in_catalog` / `not_servable` on them, and
  `parse_endpoint_catalog` returns exactly 44 ids including `Qwen/Qwen3-Embedding-0.6B`,
  `Qwen/Qwen3-Embedding-8B`, `openai/gpt-oss-120b` and `Qwen/Qwen3.5-0.8B`, with a second test reading off
  "only 2 of the 44 are embedding models" — the number ADR-009's Context rests on. The synthetic 44-bullet
  padding is deleted. The two hand-written WRAPPED fixtures STAY and are now narrower than the terminal the
  real output was captured at, so each splits the marker sentence itself across lines: the vendored texts
  happen to carry both markers on one line, so only these prove the normalisation.
- **(c) `_log_modal_output`** joins with a newline. The regression test is discriminating: stdout ends
  `'…' is not available for dedicated` with NO trailing newline and stderr begins `Endpoints.`; concatenated
  that reads `dedicatedEndpoints.` -> verdict `other` -> the deploy ABORTS (exit 1), while the newline join
  routes to the vLLM App (exit 0). Verified both ways before keeping the test.

**Notes for the Tester / PA**
- `EmbeddingDeploySpec.engine` was DELETED along with `FallbackEngine` (grepped: nothing read it). The task
  said "`EmbeddingDeploySpec` stays"; keeping a one-value `engine` field after the engine stopped being a
  parameter would have been a field that can only ever say `"vllm"`, and `LLMDeploySpec`'s field list in the
  task has no `engine` either. Both specs now share a `DeploySpec` base.
- `build_llm_server_args` merges the per-model overrides by FLAG NAME, not by dict key
  (`_merge_by_flag`, 4 lines mirroring `autoinference-utils`' own `_merge_server_args`): `--flag=value` and
  `--flag value` set the same flag, so a plain `dict |` would have handed SGLang
  `--mem-fraction-static 0.85 --mem-fraction-static=0.75`. Pinned by
  `test_an_attached_override_replaces_the_default`.
- The catalog constant is still called `EMBEDDING_SERVER_NAME` although both kinds use it (it is `"Server"`,
  the class name on every Serving path). Renaming it touches ~10 call sites in code the task does not open;
  its docstring already says "on BOTH Serving paths". Flagging it rather than doing it.
- **Known bound of the new env guard, named rather than hidden:** `_environ_reads` matches the dotted name
  `os.environ`, so a MODULE ALIAS (`import os as o; o.environ`) is invisible to it. It is still caught on the
  path that matters — `_log_offenders` rejects any logger argument whose source mentions `environ`, alias or
  not — so the gap is only "an aliased `os.environ` handed to a NON-logger sink", and the scripts have no
  other sink (no `print`/`os.write`/stream write survives `_console_writes`, no `Secret.from_name`, no
  `subprocess`). Widening the helper to resolve import aliases is a bigger AST walk than the residue
  justifies; if a sink is ever added to a deploy script, this guard must grow with it.
- The `bool(os.environ.get('HF_TOKEN'))` exemption in both new guards is an EXACT `ast.unparse` match on
  purpose. It must never be relaxed into a substring check ("does the argument contain `HF_TOKEN`?") — that
  would re-admit `os.environ["HF_TOKEN"]`, which is the value, not the boolean.
- Adjacent, NOT fixed here (no task asks for it): `docs/adrs/009` §2 and the glossary both already describe
  this script, so no doc edit was needed — and both are read-only for me.

### [Tester] 2026-09-20 18:20 — QA

**Commands run (LOCAL env, in order)**
```
make env-status                                                      # -> Env target: local (.env)
make memory-format-check && make memory-lint-check
make pre-commit
make memory-tests                                                    # 3702 passed
make memory-deploy-model MODEL=LiquidAI/LFM2.5-350M SERVING=app DRY_RUN=yes
make memory-deploy-model MODEL=LiquidAI/LFM2.5-350M DRY_RUN=yes       # router-undecided branch
uv run pytest tests/unit/config/test_settings_credentials_only.py -q # 9 passed, settings-reload check first
uv run python -c "from tree.models.modal_catalog import build_llm_deploy_spec; ..."   # real loader, LiquidAI/LFM2.5-350M
uv run python -c "import tree.models.get_model; assert 'modal' not in sys.modules"    # OK
git diff --name-only | xargs grep -Eco "hf_[A-Za-z0-9]{20,}|wk-[A-Za-z0-9]{10,}|ws-[A-Za-z0-9]{10,}"  # 0 hits
grep -rn "modal_sglang_embedding" apps/memory --exclude-dir=.git      # 0 hits
grep -rn "modal_sglang_embedding\|--is-embedding\|is_embedding" apps/memory/src apps/memory/deploy apps/memory/scripts apps/memory/README.md apps/memory/configs
grep -Ino "p-b-iusztin|wk-...|ws-...|hf_...|https?://" apps/memory/tests/unit/models/fixtures/*.txt   # 0 hits
```
Plus, in the session scratchpad only (never in the repo, never executed as a Modal process):
- A standalone script that imports `tests/unit/deploy/test_modal_deploy_scripts.py`'s guard helper
  functions (`_console_writes`, `_environ_reads`, `_log_offenders`, `_image_env_dicts`,
  `_image_env_keys`, `_assert_proxy_auth_is_pinned`) and runs them against COPIES of the two shipped
  scripts mutated with ~20 of my own hostile snippets beyond the SWE's rows (see "Adversarial guard
  pass" below). No `modal` import, no `ast.parse`-then-`exec`, no process started.
- A real `aiohttp.web` stub on `127.0.0.1` (dynamic port), with `modal_server.resolve_server_url`
  patched to point at it — nothing else patched, so `chat_smoke_test` ran its REAL `poll_health`,
  `_chat`, `_city_facts` and HTTP calls end to end. No `*.modal.run` / `*.modal.direct` request.
- Read-only GETs (all matched the SWE's claims byte for byte): `raw.githubusercontent.com/sgl-project/
  sglang/v0.5.18/.github/workflows/release-docker.yml` (cu130 tag config), `.../docker/Dockerfile`
  (`CUDA_VERSION=13.0.3`), `.../python/sglang/srt/models/lfm2.py` (`EntryClass = [Lfm2ForCausalLM, ...]`),
  `.../python/sglang/srt/entrypoints/openai/protocol.py` (`ResponseFormat.type` incl. `json_schema`),
  `huggingface.co/LiquidAI/LFM2.5-350M/raw/main/config.json` (`architectures: ["Lfm2ForCausalLM"]`,
  `model_type: "lfm2"`), `modal.com/docs/guide/cuda.md` (driver 580.95.05 / CUDA Driver API 13.0), Docker
  Hub tags API (`v0.5.18` and `v0.5.18-cu130` both listed). `pip download autoinference-utils==0.2.6`
  into the scratchpad only, unzipped and read (`speculative_model_path: Optional[str] = None`,
  `_build_cmd` guards on `is not None`, `warmup_chat_completions(*, port, payload, headers=None,
  successful_requests=3, request_timeout=30.0, ...)`, and `_merge_server_args`/`_flag_name` — the SWE's
  `_merge_by_flag` is a verified line-for-line mirror).

**Test summary**
- Format / lint / pre-commit: PASS
- Unit tests: 3702 passed / 0 failed (matches the SWE's claim)
- Integration tests: N/A (no integration suite, per AGENTS.md)
- Warnings: 1 — `UserWarning: Core Pydantic V1 functionality isn't compatible with Python 3.14 or
  greater.` from `opik`'s vendored `pydantic.v1` shim. Pre-existing dependency/interpreter mismatch, not
  touched by this diff, reproduces on `main` code paths unrelated to Modal — not counted against this task.

**E2E adversarial pass**
- Happy path: `make memory-deploy-model MODEL=LiquidAI/LFM2.5-350M SERVING=app DRY_RUN=yes` →
  `DRY RUN — would run: modal deploy deploy/modal_sglang_llm.py`, exit 0 (PASS, byte-for-byte the
  SWE's pasted evidence).
- Break path 1 (state edge: router-undecided dry run, no `SERVING=`): `make memory-deploy-model
  MODEL=LiquidAI/LFM2.5-350M DRY_RUN=yes` → logs the `endpoint create` argv, then `DRY RUN — routing is
  undecided without Modal: on "not available for dedicated Endpoints" the next command would be: modal
  deploy deploy/modal_sglang_llm.py`, exit 0 (PASS).
- Break path 2 (malformed/boundary chat responses, real stub, real `poll_health`): 9 scenarios against
  a live `aiohttp` server — non-JSON content, missing key, extra key, `population: true`, `population:
  "9"`, empty `choices`, truncated JSON, HTTP 400, and a cold-503-then-200 (poller waited ~5s and
  returned `cold_start_seconds=5.00`). All 9 produced exactly the messages the spec/tests describe
  (`chat completion is not valid JSON: ...`, `missing ['population']`, `unexpected ['note']`, `types
  'population' as bool`, `types 'population' as str`, `carried no content`, `answered 400, expected
  200` with `status_code=400`); the happy path returned `city='Tokyo' population=13960000
  unauthenticated_status=401`. (PASS)
- Break path 3 (hostile: token-leak guard mutants beyond the SWE's rows, on scratch copies of both
  scripts): `logger.info(f"{os.environ}")`, `logger.info("x", extra={"e": os.environ})`, `env =
  os.environ; logger.info("%s", env)` (alias), `logger.info("%s", json.dumps(dict(os.environ)))`,
  `logger.info("%s", os.environ["HF_TOKEN"])` (the value, not the boolean), `logger.info("%s",
  bool(os.environ.get('HF_TOKEN')) or os.environ['HF_TOKEN'])`, `logging.getLogger().info(os.environ[
  "HF_TOKEN"])`, `unauthenticated=True` — ALL caught (by `_environ_reads` and/or `_log_offenders` and/or
  `_assert_proxy_auth_is_pinned`, on both engines). `.env(**{"HF_TOKEN": os.environ["HF_TOKEN"]})` and
  `.env(d)` where `d = {"HF_TOKEN": os.environ["HF_TOKEN"]}` on a second Image — NOT seen by
  `_image_env_dicts` (it only matches a literal single positional `{...}` argument), but the underlying
  `os.environ["HF_TOKEN"]` read is still caught by `_environ_reads` regardless of the sink shape, so no
  real leak path was found here. `subprocess.run(["env"])` and `os.system("env")` — NOT caught by any
  guard (`_console_writes`/`_environ_reads`/`_log_offenders` all silent) on either engine. (FAIL on this
  sub-path — see "Other issues found"; not blocking, see reasoning there.)

**Acceptance criteria**
- [x] PASS — no `modal_sglang_embedding.py`, no stray `--is-embedding`/`is_embedding` outside the
      builder-owned-keys entry — `grep -rn "modal_sglang_embedding\|--is-embedding\|is_embedding"
      apps/memory/src apps/memory/deploy apps/memory/scripts apps/memory/README.md apps/memory/configs`
      → only `src/tree/config/app_config.py:710` (`"--is-embedding"` in `_BUILDER_OWNED_SERVER_ARGS`).
      `ls apps/memory/deploy/` shows `modal_sglang_llm.py` + `modal_vllm_embedding.py` only.
- [x] PASS — `test_engine_pins` (`tests/unit/config/test_app_config.py:1159`) passes; confirmed by hand
      that YAML (`configs/default.yaml`), the code default (`app_config.py`
      `ModalConfig.engines`) and `tests/unit/config/fixtures/frozen_config.yaml:134` all say
      `"v0.5.18"`.
- [x] PASS — `TestBuildLlmDeploySpec` (`tests/unit/models/test_modal_catalog.py:386-464`) passes;
      re-ran `build_llm_deploy_spec("LiquidAI/LFM2.5-350M")` through the real loader — `app_name
      "ep-tree-lfm2-5-350m"`, `gpu "A10"`, `n_gpus 1`, `server_args` exactly the five keys the AC names;
      `test_an_entry_overrides_the_tuning_defaults` proves the override-wins case;
      `test_nothing_modal_tuned_per_model_leaks_in` proves no `--speculative`/`--mamba`/
      `--enable-multimodal`; `test_an_embedding_entry_is_refused` proves the kind error on
      `voyageai/voyage-4-nano`; `test_no_credential_is_ever_in_the_spec` proves no `HF_TOKEN` / token
      value in the dump.
- [x] PASS — static AST tests in `tests/unit/deploy/test_modal_deploy_scripts.py` pass:
      `test_the_sglang_script_follows_modals_llm_template` proves `from_registry` on an f-string
      starting `lmsysorg/sglang:`, no `add_python`/`.entrypoint(`/`"sglang=="`, `SGLangEndpoint(
      model_path=…, tp=…, health_poll_interval=5.0)` with no `speculative_model_path`,
      `warmup_chat_completions(successful_requests=2, request_timeout=60.0)`,
      `gpu=f"{SPEC['gpu']}:{SPEC['n_gpus']}"`; `test_the_sglang_warm_up_is_modals_strict_json_payload`
      proves the payload equals Modal's `city_facts` one; `TestGlueContract` (parametrised over
      `sglang`) passes every guard. Verified by reading `deploy/modal_sglang_llm.py` line-for-line
      against the test assertions.
- [x] PASS — `test_script_paths_match_cli_command` passes; confirmed `deploy/modal_vllm_embedding.py`
      and `deploy/modal_sglang_llm.py` both exist on disk.
- [x] PASS — `TestChatSmokeTest` (17 rows, `tests/unit/models/test_modal_server.py:562-819`) passes,
      and independently reproduced live against a real `aiohttp` stub (see E2E break path 2 above) with
      identical error text for every case the AC names.
- [x] PASS — `TestTestCommandDispatch` (`tests/unit/scripts/test_modal_model_script.py:1155-1219`)
      passes: embedding id awaits `smoke_test`, LLM id (App-routed and endpoint-routed) awaits
      `chat_smoke_test`, both exit 1 on `ModelError`/`ExtractionError`.
- [x] PASS — reproduced live: `make memory-deploy-model MODEL=LiquidAI/LFM2.5-350M SERVING=app
      DRY_RUN=yes` → exit 0, `DRY RUN — would run: modal deploy deploy/modal_sglang_llm.py` (pasted
      above). Confirmed by reading `modal_router.run_deploy` that the `is_dry_run` branch returns
      before `read_existing_kind` (the guard's list calls) and before any Hub GET.
- [x] PASS — `## Log`'s SWE entry answers all five "SWE must verify" items; independently re-verified
      all five against primary sources (GitHub raw, the Hub, the unpacked wheel, Modal's docs) — every
      figure quoted (cu130 tag aliasing, CUDA 13.0.3 base, `Lfm2ForCausalLM`/`lfm2` model_type,
      `speculative_model_path` optional, `warmup_chat_completions` signature, `ResponseFormat.type`
      accepting `json_schema`) matched byte for byte. No `modal` process was started by me either.
- [x] PASS — `grep -c "deploy/modal_sglang_llm.py\|LiquidAI/LFM2.5-350M" apps/memory/README.md` = 10
      (well over 1).
- [x] PASS — `make memory-format-check && make memory-lint-check && make pre-commit &&
      make memory-tests` all green, LOCAL env confirmed via `make env-status`.

**Disclosed points, ruled on**
- `EmbeddingDeploySpec.engine` / `FallbackEngine` deletion: grepped both deploy scripts for every
  `SPEC[...]` key they read against their respective `*DeploySpec` field list — every key read exists
  in the model, no key is read that the script doesn't emit via `model_dump()`, and `engine` is read
  nowhere (router, driver, deploy scripts, tests, container-side JSON). No stale-reader risk. PASS.
- `_merge_by_flag`: reproduced by hand — `--flag=value` overrides replace a plain `--flag` default
  (single resulting key), same-spelling overrides update in place, bare flags round-trip, and it is a
  verified line-for-line mirror of `autoinference-utils`' own `_merge_server_args`/`_flag_name`
  (confirmed in the unpacked 0.2.6 wheel). Builder-owned keys (`--tp`, `--tp-size`,
  `--tensor-parallel-size`, `--model-path`, `--served-model-name`, `--context-length`, `--host`,
  `--port`, `--model`) are all rejected at config load for `ModalLLMModelConfig`, reproduced live.
  PASS.
- The `import os as o; o.environ` alias exemption: verified `_log_offenders` still catches an aliased
  read because it substring-matches `"environ"` in the unparsed argument text regardless of the dotted
  prefix, so the SWE's stated residual ("an aliased read to a non-logger sink") is the accurate
  boundary — except the "no other sink" half of that reasoning is not actually enforced by any test
  (see "Other issues found").
- The exact-`ast.unparse` `bool(os.environ.get('HF_TOKEN'))` exemption: agreed, must stay exact. A
  substring relaxation would re-admit `os.environ["HF_TOKEN"]` itself.
- `EMBEDDING_SERVER_NAME` naming: PASS with note — cosmetic, correctly flagged rather than silently
  fixed outside scope.

**Other issues found**
- **Guard gap (not blocking, but should be a follow-up fix): `subprocess`/`os.system` env-dump is
  invisible to every guard.** `subprocess.run(["env"])` or `os.system("env")` added to either deploy
  script's `@modal.enter` body dumps the real container environment (including `HF_TOKEN`) to inherited
  stdout, which Modal captures as `modal app logs` — the exact harm class `_console_writes` exists to
  prevent — yet it passes `_console_writes`, `_environ_reads` and `_log_offenders` silently on both
  engines (verified on scratch mutants of both scripts). The SWE's own note asserts "the scripts have
  no other sink (... no `subprocess`)" as part of why the alias gap is safe to accept — that's true of
  today's file but is not enforced by any test, so a future regression would sail through CI. Suggested
  fix: extend `test_the_retired_auth_never_comes_back`'s substring-ban style (or `_console_writes`) to
  reject `subprocess`/`os.system`/`os.popen` in both scripts — a one-line, low-cost addition matching
  the project's own established pattern. Not blocking this PASS because: no AC asks for it, the shipped
  scripts contain neither call today, and the exploit requires code with no purpose but leaking the
  environment (not a plausible accidental regression, unlike the #146 mutants this suite already
  guards against).
- **Minor: `_log_offenders` only recognises calls whose dotted name starts with `logger.`.** A second
  logger binding (`log = logging.getLogger(__name__)`) or a direct `logging.getLogger().info(...)`
  bypasses the message allow-list. In practice this does not open a token-leak path today — any
  `os.environ["HF_TOKEN"]`-shaped read is still caught by the sink-agnostic `_environ_reads` regardless
  of which logger call carries it forward — but it does weaken the "every message is enumerated"
  invariant the guard's own docstring claims. Cheap follow-up: resolve logger aliases the same way
  `_console_aliases` already resolves print/stream aliases.
- 1 pre-existing `UserWarning` from `opik`'s `pydantic.v1` shim under Python 3.14, unrelated to this
  diff (see Test summary).

**VERDICT: PASS**
