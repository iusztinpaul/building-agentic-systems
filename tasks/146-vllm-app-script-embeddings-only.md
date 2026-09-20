---
id: 146-vllm-app-script-embeddings-only
status: pending
feature: voyage-4-and-modal-embedding-catalog
---

# The vLLM App script serves EMBEDDING models only, following Modal's own embedding template (vLLM 0.26.0) — and the static guards lose their two blind spots

Tags: `modal`, `deploy`, `vllm`, `safety`
Depends on: #145
Blocks: #147, #141
Implements: ADR-009 — Decision 2 (App by kind: vLLM = embeddings) and Decision 3 (dimensions: the client cannot rely on server-side `dimensions`)

## Scope

**EXECUTION ORDER of the rework: 143 -> 144 -> 145 -> 146 -> 147 -> 148 -> 149 -> 150 -> 141.**

**HARD SAFETY RULE:** No agent may run `make memory-deploy-*`, `scripts/modal_*.py` as a process, or any `modal …` command outside task 141; a PATH shim does NOT intercept under `make`/`uv run` because `.venv/bin` wins — use the mocked unit tests and `DRY_RUN=yes`.
A deploy script is NEVER executed here — not `python deploy/…`, not `modal deploy`. It is only parsed (`ast`)
and imported under a mocked `modal` by the static tests. Its live proof is #141.

**The human's decision (settled).** "For embeddings always go with the vLLM app — optimise the vLLM script for
EMBEDDING models from Hugging Face, not LLMs." Read `MODAL_TEMPLATES_REFERENCE.md` (scratchpad, section
"EMBEDDINGS -> vLLM"): it holds the exact facts of the two `serve.py` files Modal generates for
`Qwen/Qwen3-Embedding-0.6B` and `Qwen/Qwen3-Embedding-8B`. Our script follows THAT template.

**What this task rewrites (committed code from #142 + #145):**
- `deploy/modal_vllm_embedding.py` — the docstring (it still calls itself an "eject path" / "fallback" /
  "`vllm` Serving path" and tells the operator to "stop the endpoint before deploying this"), the engine pin,
  nothing structural: `@app.server` on `class Server`, `VLLMEndpoint`, `validate_embeddings_endpoint`, the
  `is_local()` split, the ephemeral `HF_SECRET`, the first-line token boolean all STAY.
- `configs/default.yaml` + `ModalConfig.engines` default + `frozen_config.yaml`: `modal.engines.vllm.version`
  `"0.17.1"` -> `"0.26.0"` (Modal's pin). Replace the YAML comment (`voyage-4-nano's own card pins
  vllm==0.16.0 … #141 owns the live bump`) with `# Modal's own embedding recipe pin (serve.py, 2026-09-20)`.
- `src/tree/models/modal_catalog.py::build_server_args` / `build_deploy_spec` — the `vllm` branch is the
  EMBEDDING builder: `--runner pooling` + `--served-model-name <repo_id>` + `--revision` + `--max-model-len`
  when set, then the entry's `extra_server_args`. (The `sglang` branch is left alone — #147 replaces it.)
- `tests/unit/deploy/test_modal_embedding_scripts.py` — the two new static guards below, for BOTH scripts.

**A. Follow Modal's embedding template — value by value.**
- Image: `modal.Image.from_registry("nvidia/cuda:13.0.2-devel-ubuntu22.04", add_python="3.12").entrypoint([])
  .uv_pip_install("vllm==<engines.vllm.version>", "autoinference-utils==<version>", "httpx", "huggingface-hub")
  .env({"HF_XET_HIGH_PERFORMANCE": "1", <the spec env var>})` — already the case; only the pin moves.
- `SERVER_ARGS = {"--served-model-name": …, "--runner": "pooling"} | extras` — Modal's exact baseline.
- `VLLMEndpoint(model=…, worker_port=8000, extra_server_args=…, health_timeout=…, health_poll_interval=5.0)`
  then `validate_embeddings_endpoint(port=8000, payload={"model", "input": [2 probes], "encoding_format":
  "float"}, request_timeout=60.0)` — already the case.
- Differences we KEEP on purpose, each one line in the docstring: weights by `repo_id` + `revision` from the
  shared `huggingface-cache` Volume (Modal's `MODEL_PATH` is a snapshot inside the endpoint's own volume, which
  we cannot reuse); `unauthenticated=False` spelled as a literal; stdlib logging instead of `print`; GPU / CPU /
  memory / `target_concurrency` from the catalog entry instead of module constants.
- Per-model extras stay in YAML `extra_server_args`. voyage-4-nano keeps `--convert embed`, `--pooler-config
  {"pooling_type":"MEAN"}`, `--hf-overrides {"architectures":["VoyageQwen3BidirectionalEmbedModel"]}`,
  `--dtype bfloat16`, `--enforce-eager`, `--trust-remote-code`; `--max-num-seqs` is accepted (Modal's 8B recipe
  sets `32`) — add a YAML comment saying so, no seed needs it.
- **Native wire width, client-side truncation stays (ADR-009 §3).** Do NOT add `is_matryoshka` to any seed's
  `--hf-overrides`: Modal's 8B recipe sets `{"is_matryoshka":true}` server-side but its 0.6B recipe does NOT —
  so server-side `dimensions` differs between two managed recipes of the same family, which is exactly why the
  client never sends it. Put that sentence in the script's docstring.
- Remove every LLM / generic pretence: the docstring says "embedding models from Hugging Face, nothing else";
  there is no chat warm-up, no `--tool-call-parser`, no "or an LLM" wording. The driver already refuses to send
  an LLM entry here (#145: kind -> script), and `build_deploy_spec` uses `get_embedding_entry`.
- `HEALTH_TIMEOUT` (18 min) / `STARTUP_TIMEOUT` (20 min) in the container stay: they are the ENGINE's start
  budget, unrelated to the client-side `modal.warmup_deadline_s` (#144). One comment line says so.

**B. SWE must verify, WITHOUT running `modal` or the script** (answers + sources in `## Log`):
1. `VoyageQwen3BidirectionalEmbedModel` is registered in vLLM **v0.26.0**. Grooming already found it
   (`vllm/model_executor/models/registry.py` at tag `v0.26.0`, 2 hits, read 2026-09-20) — re-check on the tag
   and paste the line. If a flag voyage needs was renamed by 0.26.0 (`--convert embed`, `--pooler-config`,
   `--runner pooling`, `--hf-overrides`) — read `vllm/engine/arg_utils.py` at the tag and the vLLM pooling-models
   docs via the `tech-docs` skill — fix the YAML value and say so. Only if voyage-4-nano provably cannot run on
   0.26.0 may the pin differ; name the blocker and the chosen tag.
2. `autoinference-utils==0.2.6` still takes `VLLMEndpoint(model=…)` and `validate_embeddings_endpoint(port=,
   payload=, request_timeout=)` (the wheel was unpacked in the scratchpad during #142: `aiu_src/`).
3. CUDA `13.0.2` base + the `vllm==0.26.0` wheel: read the wheel's CUDA requirement on PyPI. A mismatch is
   proven only live (#141) — record what the metadata says.

**C. The two static-guard blind spots (found by mutation during #142's QA).** Both guards live in
`tests/unit/deploy/test_modal_embedding_scripts.py` and apply to EVERY deploy script the file parametrises:
1. `test_the_decorator_pins_the_serving_contract` accepts `unauthenticated=not True` (an expression that
   evaluates to `False`… or `unauthenticated=not False`, which is PUBLIC). Require the keyword's AST node to be
   exactly `ast.Constant(value=False)`; anything else — `not True`, a name, a call — fails.
2. `test_nothing_is_printed` only looks for `print(`. Also reject `sys.stdout.write`, `sys.stderr.write`,
   `sys.stdout.writelines` and `pprint(`: walk `ast.Call` nodes and fail on a `print` / `pprint` name or an
   attribute chain ending in `stdout.write*` / `stderr.write*`.

## Out of scope
- The SGLang script (#147). Any LLM. A second engine for embeddings ("always vLLM": an embedding model vLLM
  cannot serve is out of scope for the feature).
- Server-side Matryoshka (`is_matryoshka`), per-entry engine versions, weight snapshots, `min_containers > 0`.
- Any live deploy (#141).

## Acceptance Criteria

- [ ] `test_engine_pins`: `app_config.modal.engines["vllm"].version == "0.26.0"` from the YAML AND from the code default; `frozen_config.yaml` agrees.
- [ ] `TestBuildServerArgs` (vllm): for `Qwen/Qwen3-Embedding-0.6B` the args are EXACTLY `{"--served-model-name": "Qwen/Qwen3-Embedding-0.6B", "--runner": "pooling", "--revision": "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"}`; for `voyageai/voyage-4-nano` they additionally hold `--max-model-len 32768` and the six YAML extras, and contain NO `is_matryoshka`; `build_deploy_spec("LiquidAI/LFM2.5-350M", "vllm")` raises the #145 kind error.
- [ ] Mutation-style static tests, each on a mutated COPY of the script written to `tmp_path` (the real file is never edited or executed): `unauthenticated=not True` -> fails; `unauthenticated=not False` -> fails; `unauthenticated=AUTH` -> fails; `sys.stdout.write("x")` -> fails; `sys.stderr.write("x")` -> fails; `pprint(x)` -> fails. The unmutated scripts (both) pass.
- [ ] `test_vllm_script_follows_modals_embedding_template`: the AST of `deploy/modal_vllm_embedding.py` shows `from_registry("nvidia/cuda:13.0.2-devel-ubuntu22.04", add_python="3.12")`, `.entrypoint([])`, a `uv_pip_install` whose first argument is built from `engine_version` and which includes `httpx` and `huggingface-hub`, `VLLMEndpoint(... health_poll_interval=5.0)`, `validate_embeddings_endpoint(... request_timeout=60.0)` with `"encoding_format": "float"`, `port=8000`, `scaledown_window=5 * MINUTES`, `min_containers=0`, `exit_grace_period=25`, `routing_region="eu-west"`.
- [ ] `grep -n -i "fallback\|eject\|ladder\|serving path\|SERVING=\|LLM\|chat" apps/memory/deploy/modal_vllm_embedding.py` -> 0 hits; `grep -c "is_matryoshka" apps/memory/deploy/modal_vllm_embedding.py` >= 1 (the docstring sentence) and `grep -c "is_matryoshka" apps/memory/configs/default.yaml` == 0.
- [ ] Every pre-existing test of `TestGlueContract` and `test_only_the_hf_token_secret` stays green for the vLLM script.
- [ ] `## Log` answers B.1-B.3 with tag, file and line (or PyPI metadata field); no `modal` process was started.
- [ ] `make memory-deploy-model MODEL=voyageai/voyage-4-nano SERVING=app DRY_RUN=yes` exits 0 and logs `DRY RUN — would run: modal deploy deploy/modal_vllm_embedding.py` (pasted in `## Log`).
- [ ] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green (LOCAL env).

## User Stories

### Story: Operator adds `BAAI/bge-m3`, which Modal's catalog does not have
1. Adds 5 YAML lines under `modal.embedding_models` (`repo_id`, `revision`, `native_dimensions: 1024`, `gpu`, `max_model_len: 8192`) — no extras.
2. `make memory-deploy-model MODEL=BAAI/bge-m3 DRY_RUN=yes` shows the endpoint attempt and `… the next command would be: modal deploy deploy/modal_vllm_embedding.py`.
3. The engine would start with exactly Modal's baseline: `--served-model-name BAAI/bge-m3 --runner pooling --revision <sha> --max-model-len 8192`.

### Story: Operator reads the script to see what it is for
1. Opens `deploy/modal_vllm_embedding.py`.
2. The first paragraph says: embedding models from Hugging Face, vLLM `--runner pooling`, shaped after the `serve.py` Modal generates for its own embedding endpoints; native width on the wire, truncation client-side, and why (`is_matryoshka` differs between Modal's 0.6B and 8B recipes).
3. Nothing in it mentions a ladder, a fallback or an LLM.

### Story: A contributor tries to make the server public "just for a test"
1. Changes `unauthenticated=False` to `unauthenticated=not REQUIRE_AUTH`.
2. `make memory-tests` fails in `test_the_decorator_pins_the_serving_contract`: the value must be the literal `False`.

### Story: A contributor debugs with `sys.stdout.write`
1. Adds `sys.stdout.write(str(SPEC))` to the script.
2. `test_nothing_is_printed` fails and names the call — the spec would otherwise have landed in `modal app logs`.

### Story: The engine pin is one YAML value
1. Operator bumps `modal.engines.vllm.version` to a newer tag.
2. `make memory-deploy-model MODEL=voyageai/voyage-4-nano` rebuilds the image with that pin; no script edit.

---

Blocked by: #145

## Log

### [PA] 2026-09-20 13:40 — Grooming (new task: third plan edit — D4 + D7c)

**Summary**
The vLLM script stops pretending to be a generic fallback: it is THE App for embedding models, shaped after Modal's own embedding `serve.py`, on Modal's vLLM pin. The static test file loses the two blind spots mutation testing found.

**Key decisions**
- Pin 0.17.1 -> 0.26.0 (Modal's). Grooming checked the v0.26.0 registry for `VoyageQwen3BidirectionalEmbedModel` (present); flag names remain SWE-must-verify, live proof in #141.
- No `is_matryoshka` override: Modal's own two recipes disagree on it, which is the evidence for client-side truncation.
- The script's structure is unchanged — this is a docstring, a pin and a builder branch, plus guards.
- Guards require the AST literal `False` and ban `stdout/stderr.write*` + `pprint`, verified on mutated copies in `tmp_path`.

**Dependencies**
- #145 (kind -> script mapping, `get_embedding_entry`, `MODAL_MODEL`, the renamed targets).

**User stories**
- 5 stories: a new non-catalog embedding model, reading the script, the public-server mutation, the stdout mutation, the pin.

**Open questions**
- None blocking.

Ready for implementation.
