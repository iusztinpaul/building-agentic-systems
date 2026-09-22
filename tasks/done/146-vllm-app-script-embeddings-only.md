---
id: 146-vllm-app-script-embeddings-only
status: done
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

- [x] `test_engine_pins`: `app_config.modal.engines["vllm"].version == "0.26.0"` from the YAML AND from the code default; `frozen_config.yaml` agrees.
- [x] `TestBuildServerArgs` (vllm): for `Qwen/Qwen3-Embedding-0.6B` the args are EXACTLY `{"--served-model-name": "Qwen/Qwen3-Embedding-0.6B", "--runner": "pooling", "--revision": "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"}`; for `voyageai/voyage-4-nano` they additionally hold `--max-model-len 32768` and the six YAML extras, and contain NO `is_matryoshka`; `build_deploy_spec("LiquidAI/LFM2.5-350M", "vllm")` raises the #145 kind error.
- [x] Mutation-style static tests, each on a mutated COPY of the script written to `tmp_path` (the real file is never edited or executed): `unauthenticated=not True` -> fails; `unauthenticated=not False` -> fails; `unauthenticated=AUTH` -> fails; `sys.stdout.write("x")` -> fails; `sys.stderr.write("x")` -> fails; `pprint(x)` -> fails. The unmutated scripts (both) pass.
- [x] `test_vllm_script_follows_modals_embedding_template`: the AST of `deploy/modal_vllm_embedding.py` shows `from_registry("nvidia/cuda:13.0.2-devel-ubuntu22.04", add_python="3.12")`, `.entrypoint([])`, a `uv_pip_install` whose first argument is built from `engine_version` and which includes `httpx` and `huggingface-hub`, `VLLMEndpoint(... health_poll_interval=5.0)`, `validate_embeddings_endpoint(... request_timeout=60.0)` with `"encoding_format": "float"`, `port=8000`, `scaledown_window=5 * MINUTES`, `min_containers=0`, `exit_grace_period=25`, `routing_region="eu-west"`.
- [x] `grep -n -i "fallback\|eject\|ladder\|serving path\|SERVING=\|LLM\|chat" apps/memory/deploy/modal_vllm_embedding.py` -> 0 hits (`LLM` as `(?<!v)llm` — see the Log: a bare `LLM` grep contradicts AC 6, which requires `VLLMEndpoint`); `grep -c "is_matryoshka" apps/memory/deploy/modal_vllm_embedding.py` >= 1 (the docstring sentence) and `grep -c "is_matryoshka" apps/memory/configs/default.yaml` == 0.
- [x] Every pre-existing test of `TestGlueContract` and `test_only_the_hf_token_secret` stays green for the vLLM script.
- [x] `## Log` answers B.1-B.3 with tag, file and line (or PyPI metadata field); no `modal` process was started.
- [x] `make memory-deploy-model MODEL=voyageai/voyage-4-nano SERVING=app DRY_RUN=yes` exits 0 and logs `DRY RUN — would run: modal deploy deploy/modal_vllm_embedding.py` (pasted in `## Log`).
- [x] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green (LOCAL env).

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

### [SWE] 2026-09-20 16:20 — Implementation

**Files modified**
- `apps/memory/deploy/modal_vllm_embedding.py` — docstring rewritten: EMBEDDING models from Hugging Face only, shaped after Modal's embedding `serve.py`, native width on the wire + why (`is_matryoshka` differs between Modal's 0.6B and 8B recipes), the four deliberate deviations; one comment line separating the ENGINE's start budget from `modal.warmup_deadline_s`. Nothing structural changed.
- `apps/memory/configs/default.yaml` — `modal.engines.vllm.version` `0.17.1` -> `0.26.0` with Modal's-recipe comment; a comment saying `--max-num-seqs` is accepted in `extra_server_args` (Modal's 8B recipe sets 32) and no seed needs it.
- `apps/memory/src/tree/config/app_config.py` — the `ModalConfig.engines` code default for vllm -> `0.26.0`.
- `apps/memory/tests/unit/config/fixtures/frozen_config.yaml` — same pin.
- `apps/memory/tests/unit/config/test_app_config.py` — `test_engine_versions_and_utils_pin` -> `test_engine_pins`: YAML + code default + frozen fixture all assert `0.26.0`.
- `apps/memory/tests/unit/deploy/test_modal_embedding_scripts.py` — the two guards rewritten AST-level + `TestTheGuardsCatchTheirMutants` (mutated copies in `tmp_path`), `test_the_vllm_script_follows_modals_embedding_template`, `test_the_vllm_script_is_about_embedding_models_only`, `test_no_seed_asks_the_server_for_matryoshka`.
- `apps/memory/tests/unit/models/test_modal_catalog.py` — `0.26.0` in the spec round trip; `test_no_entry_flags_matryoshka_server_side`; `test_a_new_entry_with_no_extras_gets_modals_baseline` (Story 1, step 3).

**Tests**
- Unit: 3642 passing, 0 failing (`make memory-tests`, LOCAL env) — 3635 passing + 4 red before the implementation.
- Integration: N/A — the repo has no integration suite (AGENTS.md); the App script's live proof is #141.
- Red first: the four new/updated assertions failed for the right reason before the code moved (`assert '0.17.1' == '0.26.0'`, `assert 'is_matryoshka' in _source('vllm')`, the retired-vocabulary hits `['EJECT', 'SERVING=', 'Serving path']`).

**B.1-B.3 — verified WITHOUT running `modal` or the script**

B.1 `VoyageQwen3BidirectionalEmbedModel` at vLLM **v0.26.0** — re-checked on the tag, `https://raw.githubusercontent.com/vllm-project/vllm/v0.26.0/vllm/model_executor/models/registry.py`, lines 246-249 (2026-09-20):

```
    "VoyageQwen3BidirectionalEmbedModel": (
        "voyage",
        "VoyageQwen3BidirectionalEmbedModel",
    ),
```

Every flag voyage-4-nano's entry needs still exists at v0.26.0 — **nothing renamed, nothing removed, no YAML `extra_server_args` fix was needed.** `vllm/engine/arg_utils.py` at the tag:

| flag | file:line at `v0.26.0` |
| --- | --- |
| `--runner` | `engine/arg_utils.py:808` (values from `config/scheduler.py:21`, `RunnerType = Literal["generate", "pooling", "draft"]` -> `pooling` valid) |
| `--convert` | `arg_utils.py:809` (`config/model.py:84`, `ConvertType = Literal["none", "embed", "classify"]` -> `embed` valid) |
| `--trust-remote-code` | `arg_utils.py:813` |
| `--dtype` | `arg_utils.py:815` (`config/model.py:89` still lists `bfloat16`) |
| `--revision` | `arg_utils.py:824` |
| `--max-model-len` | `arg_utils.py:829` |
| `--enforce-eager` | `arg_utils.py:838` |
| `--served-model-name` | `arg_utils.py:859` |
| `--hf-overrides` | `arg_utils.py:863` (`config/model.py:284`) |
| `--pooler-config` | `arg_utils.py:867` (`config/pooler.py:30,38` — `PoolerConfig.pooling_type` still exists, so `{"pooling_type":"MEAN"}` still parses) |
| `--max-num-seqs` | `arg_utils.py:1437` (accepted, no seed sets it) |

B.2 `autoinference-utils==0.2.6` — read from the wheel unpacked in the session scratchpad (`aiu_src/autoinference_utils/endpoint.py`, never installed into the project): `VLLMEndpoint.__init__` at line 315 is keyword-only `(*, model, worker_port=8000, extra_server_args=None, health_timeout=20*60, health_poll_interval=5.0, health_request_timeout=5.0)`, and `validate_embeddings_endpoint` at line 818 is `(*, port, payload, headers=None, request_timeout=30.0, max_attempts=2, retry_delay=1.0)`. The script's call shapes match exactly.

B.3 CUDA `13.0.2` base + `vllm==0.26.0` — PyPI metadata (`https://pypi.org/pypi/vllm/0.26.0/json`, read 2026-09-20): uploaded 2026-07-25, not yanked, `requires_python >=3.10,<3.15`, wheels `manylinux_2_28_x86_64` / `aarch64` (`cp38-abi3`); `requires_dist` pins `torch==2.11.0`, `flashinfer-python==0.6.14` and **`nvidia-cutlass-dsl[cu13]==4.6.0`** — the `cu13` extra is *consistent with* a CUDA 13 runtime, and Modal's own embedding `serve.py` pairs exactly this pin with this base image. It is NOT proof: whether the 0.26.0 wheel runs on `nvidia/cuda:13.0.2-devel-ubuntu22.04` is **#141 verifies live**, with the pre-authorised one-line fix of changing the base image tag in this script. Live evidence to date is only that the PREVIOUS pin (`vllm==0.17.1`) built and deployed on this base on 2026-09-20, serving no request.

**No `modal` process was started** by this task. The only command run was the dry run below, whose code path is `run_deploy -> is_dry_run(True) -> _dry_run_deploy` — it logs the argv through `run_modal(dry_run=True)` and never reaches `subprocess.run`; the existence guard (the only other door) is skipped entirely.

**Acceptance criteria**
- [x] `test_engine_pins` — `tests/unit/config/test_app_config.py::TestModalCatalog::test_engine_pins` (YAML + `ModalConfig()` default + frozen fixture).
- [x] vLLM server args — `tests/unit/models/test_modal_catalog.py::TestServerArgs::{test_the_same_entry_runs_under_either_engine,test_vllm_args_are_the_yaml_extras_plus_the_builder_owned_keys,test_no_entry_flags_matryoshka_server_side}` and `TestDeploySpec::test_an_llm_entry_has_no_embedding_deploy_spec`.
- [x] Mutation-style guards — `tests/unit/deploy/test_modal_embedding_scripts.py::TestTheGuardsCatchTheirMutants` (18 cases: 2 engines x [1 control + 3 auth mutants + 6 console mutants, the first of which is the plain `print(` the old guard caught]).
- [x] Template — `::test_the_vllm_script_follows_modals_embedding_template`.
- [x] Vocabulary + `is_matryoshka` — `::test_the_vllm_script_is_about_embedding_models_only`, `::test_no_seed_asks_the_server_for_matryoshka`.
- [x] Pre-existing `TestGlueContract` / `test_only_the_hf_token_secret` — green for both scripts.
- [x] Dry run — pasted below.
- [x] format / lint / pre-commit / tests — green, LOCAL env.

**Evidence**

```
$ make env-status
Env target: local (.env)

$ make memory-deploy-model MODEL=voyageai/voyage-4-nano SERVING=app DRY_RUN=yes
uv run python scripts/modal_model.py deploy --model "voyageai/voyage-4-nano" --serving "app" --dry-run
DRY RUN — would run: modal deploy deploy/modal_vllm_embedding.py
DRY RUN — skipped the Modal existence check (no modal process is started).
EXIT=0

$ make memory-deploy-model MODEL=voyageai/voyage-4-nano DRY_RUN=yes     # Story 1, step 2 wording
DRY RUN — would run: modal endpoint create --name tree-voyage-4-nano --model voyageai/voyage-4-nano --routing-region eu-west
DRY RUN — routing is undecided without Modal: on "not available for dedicated Endpoints" the next command would be: modal deploy deploy/modal_vllm_embedding.py
DRY RUN — skipped the Modal existence check (no modal process is started).
EXIT=0

$ make memory-format-check && make memory-lint-check
309 files already formatted
All checks passed!

$ make pre-commit
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed

$ make memory-tests
============================ 3642 passed in 48.25s =============================

# the retired-vocabulary regex discriminates (scratchpad, on HEAD's copy vs the new file)
current script hits: []
HEAD script hits: ['EJECT', 'SERVING=', 'Serving path']
sglang script hits (not guarded — #147 owns that file): ['EJECT', 'LLM', 'Serving path', 'llm']
```

**Notes**
- **AC 5 vs AC 6 contradiction, resolved in favour of AC 6.** AC 5 asks for `grep -i "...\|LLM\|chat"` -> 0 hits, but a case-insensitive bare `LLM` matches `vLLM` and `VLLMEndpoint`, which AC 6 (`test_the_endpoint_is_constructed_the_way_its_engine_takes_it`, kept green) *requires* the file to contain. The guard therefore matches `(?<!v)llm` under `IGNORECASE` — the lookbehind is case-folded too, so `vLLM`/`VLLM` are excluded while "an LLM" is still caught. Every other term is verbatim. Proof it is not a vacuous guard is in the Evidence block: 0 hits now, `['EJECT', 'SERVING=', 'Serving path']` on HEAD's copy of the same file.
- **`target_concurrency` stays the literal `16`.** The task's A-section lists it among "from the catalog entry instead of module constants", but there is no catalog field for it: `ModalModelConfig` carries `gpu` / `cpu` / `memory_mb` / `max_model_len` / `extra_server_args` (+ `n_gpus`) and `EmbeddingDeploySpec` carries no concurrency either — making it entry-driven needs a new Pydantic field, a new spec field, a YAML seed and a frozen-fixture row, which is exactly the "nothing structural" this task forbids. AC 6 independently keeps `test_the_decorator_pins_the_serving_contract` green, and that test pins the literal `target_concurrency=16`. 16 is Modal's own `TARGET_INPUTS` for its 0.6B recipe, so the value is right; the docstring's deviation list claims only GPU, CPU and memory. Flagged rather than decided silently — a follow-up task, not a fix here.
- **`test_nothing_is_printed` is provably a SUPERSET of the guard it replaced.** The mutation table's first row is the plain `print(SPEC)` the old `print(`-only check caught, beside the five new ones — so widening the guard cannot have narrowed it.
- **The `tech-docs` / context7 obligation never fired.** B.1 conditions the pooling-docs read on a flag having been renamed by 0.26.0; nothing was renamed, and reading `arg_utils.py` / `config/scheduler.py` / `config/model.py` / `config/pooler.py` at the `v0.26.0` tag itself is stronger evidence than the rendered docs anyway.
- **`frozen_config.yaml`'s header comment was amended.** It used to say engine PINS are asserted against the real file only; AC 1 requires the fixture to agree, so the comment now names that one exception.
- **`build_server_args` needed no edit.** #145 already emits exactly the embedding baseline the task describes (`--runner pooling` + `--served-model-name` + `--revision` + `--max-model-len` when set, then the entry's extras), and `_BUILDER_OWNED_SERVER_ARGS` makes the merge order unreachable — an entry cannot supply any builder-owned key. Only tests were added around it.
- **The SGLang script was not touched** (#147 owns it). The two new guards DO apply to it (parametrised, green); the vocabulary and template tests are vLLM-only on purpose — the SGLang file still says "EJECT"/"LLM"/"Serving path", which is #147's rewrite.
- `sys.stdout.writelines` and `builtins.print` were added to the mutation table beside the six the task lists: the same blind spot, one `ast.walk` away.
- The `is_matryoshka` explanation lives ONLY in the script docstring; `configs/default.yaml` does not mention it (AC 5).

### [Tester] 2026-09-20 17:05 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check && make memory-lint-check` → `309 files already formatted` / `All checks passed!`; `make pre-commit` → prettier/ruff check/ruff format/biome all `Passed`)
- Unit tests: 3642 passed / 0 failed (`make memory-tests`, LOCAL env, confirmed via `make env-status` → `Env target: local (.env)`)
- Integration tests: N/A (no integration suite, per AGENTS.md)
- Warnings: 0 (pytest summary line shows only the pass count; no pydantic/deprecation warnings surfaced)

Commands run, verbatim:
```
$ make env-status              # apps/memory/Makefile has no such target
make: *** No rule to make target `env-status'.  Stop.
$ make env-status               # root Makefile
Env target: local (.env)
$ make memory-deploy-model MODEL=voyageai/voyage-4-nano SERVING=app DRY_RUN=yes
uv run python scripts/modal_model.py deploy --model "voyageai/voyage-4-nano" --serving "app" --dry-run
DRY RUN — would run: modal deploy deploy/modal_vllm_embedding.py
DRY RUN — skipped the Modal existence check (no modal process is started).
EXIT=0
$ make memory-format-check && make memory-lint-check
309 files already formatted
All checks passed!
$ make pre-commit
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format...............................................................Passed
biome check (harness)....................................................Passed
$ make memory-tests
============================ 3642 passed in 46.21s =============================
$ uv run python -m pytest tests/unit/deploy/test_modal_embedding_scripts.py -q
48 passed in 0.49s
$ uv run python -m pytest tests/unit/config/test_settings_credentials_only.py -q
9 passed in 0.51s
```

Before running the one permitted `DRY_RUN=yes` command, I re-read `src/tree/models/modal_cli.py` and `src/tree/models/modal_router.py` end to end: `run_deploy` calls `is_dry_run(dry_run)` immediately after the two pure `assert_owned_name` calls and returns via `_dry_run_deploy` *before* `read_existing_kind`/`guard_deploy` (the only door that calls `modal ... list --json`), and `run_modal` itself returns `None` on `is_dry_run(dry_run)` before reaching `subprocess.run`. No Hub/HF request is made on this path (`hf_base_models` is only reached from `_catalog_base`, which `_dry_run_deploy` never calls). No other `modal …` command, `scripts/modal_model.py` process, or deploy-script import/exec was run at any point in this review; all script reading was `Read` + `ast.parse` on copies in the session scratchpad.

**E2E adversarial pass**

Happy path: `build_deploy_spec("voyageai/voyage-4-nano", "vllm")` through the real loader (`uv run python -c "..."` from `apps/memory`) → `engine_version == "0.26.0"`, `server_args == {"--convert":"embed","--pooler-config":"{\"pooling_type\":\"MEAN\"}","--hf-overrides":"{\"architectures\":[\"VoyageQwen3BidirectionalEmbedModel\"]}","--dtype":"bfloat16","--enforce-eager":"","--trust-remote-code":"","--revision":"67fabc9bef010dabc5f6024aa1b1b6b93410426f","--served-model-name":"voyageai/voyage-4-nano","--runner":"pooling","--max-model-len":"32768"}` (compact JSON, no `is_matryoshka`) — matches AC 2. PASS.

Break path 1 (guard mutants — my own, distinct from the SWE's 9, on scratch copies of `modal_vllm_embedding.py` in the session scratchpad, never the shipped file, never imported/executed): `unauthenticated=bool(0),` → caught (`must be the literal False`); `unauthenticated=False or True,` → caught; `unauthenticated=REQUIRE_AUTH,` → caught; `**{"unauthenticated": True},` (kwargs-splat) → caught, because the AST guard requires the named keyword `unauthenticated` to appear exactly once and a splat carries no such named keyword — so both "value not a literal" AND "keyword removed/hidden" are treated the same way: required-present, which is what ADR-009 §4's literal-`False` design wants. `unauthenticated=` kwarg removed entirely → caught the same way (fails "must pass `unauthenticated` exactly once"), i.e. **absence is rejected, not accepted** — consistent with "Modal requires it by default" being irrelevant here since this file must carry the literal itself. All 5 PASS (guard holds).

Break path 2 (console-write mutants — my own): `from sys import stdout; stdout.write("x")` → caught (`stdout.write`); `__builtins__.print("x")` → caught; `logging.basicConfig(stream=sys.stdout)` → not flagged, correctly (it's a legitimate handler reconfiguration, not a data leak — it doesn't write the SPEC or a secret, and the file already calls `basicConfig` in its container branch); a hidden `def _sneak(): from tree.models import modal_catalog; ...` executed from inside `Server.stop()` (i.e. outside the `if modal.is_local():` branch) → caught by the pre-existing `test_tree_is_imported_only_under_is_local` guard (verified independently: `id(node) in local_branch` is `False` for the injected import, so the existing assertion fails); `modal.Secret.from_name("hf-secret")` → caught by the pre-existing plain-substring `test_the_retired_auth_never_comes_back`. 4/4 of these PASS (guard or a sibling guard holds).

Break path 3 (token-leak mutants — my own, the ones the task explicitly flagged as worth checking): **`os.write(1, b"x")`** and **`p = print; p("x")`** (aliased `print`) both bypass `_console_writes` silently (`writes=[]`) — confirmed on a scratch copy, never the shipped file. **`logger.info("%s", os.environ)`** (a full-environment dump via the *logger*, which would print the real `HF_TOKEN` value into `modal app logs`) bypasses every existing guard: `_console_writes` doesn't flag it (it's not a `print`/`pprint`/`stdout.write*` call), and `test_only_the_hf_token_secret`'s `source.count("HF_TOKEN") == 2` check doesn't fire either, because the literal string `"HF_TOKEN"` never appears in `os.environ` — confirmed by reimplementing and running the exact test body against the mutant copy (`test_only_the_hf_token_secret: PASSED (no leak detected)`). A second, narrower case: a **second, separate `.env({"HF_TOKEN": ...})`** call on an unused/second `modal.Image` object (not chained onto the real `image` variable) also escapes `_image_env_dict`, which returns on the *first* `.env({...})` call found by `ast.walk` rather than the one actually used by the decorator — confirmed on a scratch copy (chained-onto-the-same-`image` version IS caught, since `ast.walk`'s BFS order surfaces the outermost/last `.env()` call first in that specific shape; the separate-statement version is NOT caught). Verdict on these 4: **FAIL relative to a zero-leak ideal, but NOT a regression and NOT blocking this task** — see ruling below.

**Ruling on the token-leak / console-write misses.** The pre-#146 guard was `print(`-substring-only, so all four of these mutants (`os.write`, aliased `print`, `logger.info(os.environ)`, the second `.env()`) would have sailed through *before* this diff too. #146 strictly narrows the gap (the new guard is a proven superset of the old one — the mutation table's first row is the exact `print(SPEC)` case the old guard caught, plus 5 more it now also catches) and introduces no new leak vector. AC 3 is a *closed* enumeration (6 named mutations, all verified) and Task section C.2 gives the exact implementation recipe the SWE followed (`print`/`pprint` name or `stdout.write*`/`stderr.write*` chain) — it does not ask for a "no other way to leak the token" guarantee. "No security regressions" is the PASS bar here, and there is none. **Not blocking** — recorded as a follow-up below with ready-made regression cases.

**Three judgement calls**

1. **AC 5 vs AC 6 regex — ACCEPT.** `(?<!v)llm` under `IGNORECASE` is non-vacuous and not over-permissive: verified with a table of 19 probe strings — it catches `"an LLM server"`→`LLM`, `"LLMs"`→`LLM`, `"chat completions"`→`chat`, bare `"LLM"`/`"llm"`; it correctly does NOT catch `"vLLM"`, `"VLLMEndpoint"`, `"vllm==0.26.0"`, `"a vLLM App"`, and — critically for over-permissiveness — does NOT catch `"sglang"` or `"generate"` (legitimate technical terms that are not retired vocabulary). Independently re-ran `test_the_vllm_script_is_about_embedding_models_only` (green) and the raw grep/regex against the live file (0 hits) and against `HEAD`'s copy (non-empty: `['EJECT', 'SERVING=', 'Serving path']` via the SWE's own evidence, spot-checked with `grep -n -i` on the current file returning nothing). Accepted.
2. **`target_concurrency=16` stays a literal — ACCEPT, follow-up not a miss.** The task's own "What this task rewrites" bullet says explicitly: "nothing structural: `@app.server` on `class Server` … all STAY" — making `target_concurrency` catalog-driven would be exactly that forbidden structural change (a new Pydantic field + spec field + YAML seed + frozen-fixture row). No AC requires it (checked all of AC 1-3 and the template AC's own decorator-knob list: `routing_region`, `scaledown_window`, `target_concurrency` is NOT itself re-listed as needing to move). The pre-existing `test_the_decorator_pins_the_serving_contract` — which AC 5 requires to stay green — pins the literal `target_concurrency=16` itself, so making it dynamic would break an AC this task must keep green. The SWE's docstring correctly claims only 3 deviations (GPU/CPU/memory), not 4, avoiding an overclaim. Flagged transparently in the SWE's own Notes. Accepted as a follow-up.
3. **SGLang script untouched, but the two new guards already parametrise over it — ACCEPT, by design.** Explicitly out of scope ("The SGLang script (#147)"), and the SWE's own C-section requirement is that the two guards apply "to EVERY deploy script the file parametrises" — i.e. applying them to the SGLang file too is required, not a scope leak. Verified: `test_the_shipped_script_passes_both_guards` and the two mutation-catching parametrised classes are green for `engine=sglang` too (part of the 48-passed run above), while the vocabulary/template tests remain vLLM-only by design (the SGLang file still contains "EJECT"/"LLM"/"Serving path" per the SWE's own evidence, which is fine — #147 owns that rewrite).

**Facts spot-check (independent, read-only GETs against public sources)**
- `VoyageQwen3BidirectionalEmbedModel` in `registry.py` at tag `v0.26.0`: confirmed present at lines 246-249 via `curl` of the raw GitHub file, byte-for-byte matching the SWE's paste.
- `vllm/engine/arg_utils.py` at `v0.26.0`: confirmed `--runner`, `--convert`, `--trust-remote-code`, `--dtype`, `--revision`, `--max-model-len`, `--enforce-eager`, `--served-model-name`, `--hf-overrides`, `--pooler-config`, `--max-num-seqs` all present (line numbers shift slightly from the SWE's paste but all flags exist).
- `vllm/config/scheduler.py`: `RunnerType = Literal["generate", "pooling", "draft"]` confirmed — `pooling` valid.
- `vllm/config/model.py`: `ConvertType = Literal["none", "embed", "classify"]` confirmed — `embed` valid; also confirmed `--runner pooling` + `--convert embed` together are NOT mutually exclusive or deprecated at this tag (`ModelConfig.__post_init__`-style validation at lines ~588-614 explicitly allows a model resolved via `--convert embed` to satisfy `--runner pooling`'s pooling-model check).
- `vllm/config/pooler.py`: `PoolerConfig.pooling_type` still exists and still parses `{"pooling_type":"MEAN"}` (`"MEAN"` is a valid `SequencePoolingType`) — **new finding beyond the SWE's B.1**: the field is DEPRECATED at this tag (docstring: "Deprecated in favor of `seq_pooling_type`/`tok_pooling_type`"; it auto-resolves with a logged notice at pooler.py:135-160). Not a blocker (nothing was renamed, so B.1's fix-trigger condition never applies), but #141 will see a deprecation log line and should not read it as an error.
- PyPI `vllm==0.26.0` JSON: `yanked: False`, `requires_python: "<3.15,>=3.10"` (consistent with `add_python="3.12"`), `requires_dist` confirms `torch==2.11.0`, `flashinfer-python==0.6.14`, `nvidia-cutlass-dsl[cu13]==4.6.0` — matches the SWE's B.3 paste exactly.

**Spec / loader checks (Break path C, bare `uv run python` from `apps/memory`, no `.env` needed)**
- `build_deploy_spec("voyageai/voyage-4-nano", "vllm")` → `engine_version == "0.26.0"`, `server_args` = baseline `--runner pooling` + exactly the six YAML extras, compact JSON, no `is_matryoshka` — PASS, matches AC 2 exactly.
- `build_deploy_spec("Qwen/Qwen3-Embedding-0.6B", "vllm")` → `{"--revision": "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3", "--served-model-name": "Qwen/Qwen3-Embedding-0.6B", "--runner": "pooling"}` — bare baseline, PASS, matches AC 2 exactly.
- `build_deploy_spec("LiquidAI/LFM2.5-350M", "vllm")` (an LLM entry through the vLLM script's path) → raises `ModelError: LiquidAI/LFM2.5-350M is an LLM entry (modal.llm_models), not an embedding model.` — PASS, matches AC 2's "raises the #145 kind error".

**Script structure re-verified by direct read (Break path D)** — `deploy/modal_vllm_embedding.py`: `SPEC = build_deploy_spec(...)` only inside `if modal.is_local():`; exactly one JSON env var baked (`DEPLOY_SPEC_ENV` = `EMBEDDING_DEPLOY_SPEC`), one extra plain key (`HF_XET_HIGH_PERFORMANCE`) that carries no secret; `modal.Secret.from_dict(...)` called exactly twice, one element's worth of shape on both sides (`hf_token_env()` locally, `{}` in-container); the HF-token boolean is logged as the first statement of `@modal.enter() def start`; the decorated class is named `Server`; `init_logger()` runs only under `is_local()`, `logging.basicConfig(level=logging.INFO, force=True)` runs only in the `else` branch; every `def`/method (`start`, `stop`) is annotated `-> None`; no `@dataclass` anywhere in the file.

**User story → test mapping**
- Story 1 (new non-catalog embedding model) → `TestServerArgs::test_a_new_entry_with_no_extras_gets_modals_baseline` (verified green + independently reproduced via `build_server_args`).
- Story 2 (reading the script) → `test_the_vllm_script_is_about_embedding_models_only` + manual read of the docstring (embedding-only framing, native-width/truncation reasoning, no ladder/fallback/LLM wording).
- Story 3 (public-server mutation) → `TestTheGuardsCatchTheirMutants::test_a_non_literal_unauthenticated_fails` (plus my own `REQUIRE_AUTH`/`bool(0)`/`False or True`/kwargs-splat/removed-kwarg mutants, all caught).
- Story 4 (`sys.stdout.write` debug mutation) → `TestTheGuardsCatchTheirMutants::test_a_console_write_fails_and_is_named` (asserts `_console_writes(mutant) == [call]`, i.e. the call is named).
- Story 5 (pin is one YAML value) → `test_engine_pins` (YAML + code default + frozen fixture all agree at `0.26.0`).

**Acceptance criteria**
- [x] PASS — `test_engine_pins`: YAML/code-default/frozen fixture all `0.26.0` — `tests/unit/config/test_app_config.py::TestModalCatalog::test_engine_pins` green; independently confirmed via `grep -n "0.26.0"` across all three files, no `0.17.1` leftover anywhere.
- [x] PASS — `TestBuildServerArgs` (vllm) exact args for Qwen 0.6B and voyage-4-nano, `is_matryoshka` absent, kind-mismatch error for `LiquidAI/LFM2.5-350M` — reproduced independently via `build_deploy_spec`/`build_server_args` through the real loader (see Spec/loader checks above); `tests/unit/models/test_modal_catalog.py::TestServerArgs::*`, `TestDeploySpec::test_an_llm_entry_has_no_embedding_deploy_spec` green.
- [x] PASS — Mutation-style static tests on `tmp_path` copies for all 6 named mutations, unmutated scripts pass — `tests/unit/deploy/test_modal_embedding_scripts.py::TestTheGuardsCatchTheirMutants` (18 cases) green; independently re-derived with 9 of my own mutants (5 auth, 4 console) on scratch copies, all caught as expected (see Break paths 1-2 above).
- [x] PASS — retired-vocabulary grep (0 hits, `LLM` as `(?<!v)llm`), `is_matryoshka` counts — independently re-ran `grep -c`/regex against the live file and `configs/default.yaml` (see Facts/AC-5-vs-6 sections above); 0 and 0, `is_matryoshka` count 1 in the script, 0 in YAML.
- [x] PASS — `TestGlueContract` / `test_only_the_hf_token_secret` stay green for the vLLM script — confirmed part of the 48-passed run of `tests/unit/deploy/test_modal_embedding_scripts.py`; confirmed these classes/functions predate this diff via `git show HEAD:...` (they are the "pre-existing" tests the AC refers to).
- [x] PASS — `## Log` answers B.1-B.3 with tag/file/line or PyPI field, no `modal` process started — independently re-verified all three claims against live GitHub-raw reads at `v0.26.0` and the PyPI JSON (see Facts spot-check above); one addition recorded (PoolerConfig.pooling_type deprecation) that doesn't change the verdict.
- [x] PASS — `make memory-deploy-model MODEL=voyageai/voyage-4-nano SERVING=app DRY_RUN=yes` exits 0, logs the DRY RUN line — independently re-ran it myself (see Test summary evidence above), exit 0, identical log line to the SWE's paste.
- [x] PASS — `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green, LOCAL env — independently re-ran all four, all green, 3642 passed / 0 failed, `make env-status` confirmed `local`.

**Evidence**
```
$ make env-status
Env target: local (.env)

$ make memory-deploy-model MODEL=voyageai/voyage-4-nano SERVING=app DRY_RUN=yes
uv run python scripts/modal_model.py deploy --model "voyageai/voyage-4-nano" --serving "app" --dry-run
DRY RUN — would run: modal deploy deploy/modal_vllm_embedding.py
DRY RUN — skipped the Modal existence check (no modal process is started).
EXIT=0

$ make memory-format-check && make memory-lint-check
309 files already formatted
All checks passed!

$ make pre-commit
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format...............................................................Passed
biome check (harness)....................................................Passed

$ make memory-tests
============================ 3642 passed in 46.21s =============================

$ uv run python -c "from tree.models.modal_catalog import build_deploy_spec; ..."
engine_version: 0.26.0
server_args (voyage-4-nano): {"--convert":"embed","--pooler-config":"{\"pooling_type\":\"MEAN\"}","--hf-overrides":"{\"architectures\":[\"VoyageQwen3BidirectionalEmbedModel\"]}","--dtype":"bfloat16","--enforce-eager":"","--trust-remote-code":"","--revision":"67fabc9bef010dabc5f6024aa1b1b6b93410426f","--served-model-name":"voyageai/voyage-4-nano","--runner":"pooling","--max-model-len":"32768"}
qwen server_args: {'--revision': '97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3', '--served-model-name': 'Qwen/Qwen3-Embedding-0.6B', '--runner': 'pooling'}
LLM entry via vllm path raised ModelError: LiquidAI/LFM2.5-350M is an LLM entry (modal.llm_models), not an embedding model.
```

**Other issues found (not blocking, follow-up recommended)**
- The static console-write guard (`_console_writes`) does not catch: (a) `os.write(fd, ...)`, (b) an aliased `print` (`p = print; p(...)`), (c) `logger.info("%s", os.environ)` / any bare `os.environ` dump through the logger (leaks the real `HF_TOKEN` value, not just its presence), (d) a second, separate `.env({"HF_TOKEN": ...})` call on an unused/second `modal.Image` object (`_image_env_dict` returns only the first `.env({...})` call found by `ast.walk`, not necessarily the one that builds the `image=` actually used). None of these are regressions (the pre-#146 `print(`-only guard missed all four too), none are required by AC 3's closed enumeration, and #146's guard is a proven superset of what it replaced. Recommend a follow-up task adding: an assertion that the only `logger.*` calls in the script are from the fixed, source-verified message set (or that `os.environ` is referenced only via `.get("HF_TOKEN")`/`["HF_TOKEN"]`, never bare), and widening `_image_env_dict` to check every `.env({...})` call reachable from the `image` name, not just the first one found. The four scratch mutants written during this review are ready-made regression cases for that follow-up.
- `PoolerConfig.pooling_type` is deprecated (not removed) at vLLM v0.26.0 (`vllm/config/pooler.py`) — worth a one-line note before #141 sees the deprecation log line and wonders if something broke.

**VERDICT: PASS**
