---
id: 154-hf-cache-volume-mount-path
status: pending
feature: voyage-4-and-modal-embedding-catalog
---

# Both App scripts mount the shared `huggingface-cache` Volume at `/cache/huggingface` and point `HF_HOME` at it: the SGLang App can start (its image ships a non-empty `/root/.cache/huggingface`)

Tags: `memory`, `modal`, `deploy`, `bug`, `tests`
Depends on: #153 (same two files and the same `.env({...})` layer — sequential, not parallel)
Blocks: #141
Implements: ADR-009 — Decision 3, revision 6 (the mount path of the shared weights Volume). Found live by #141 round 1, cycle 4c.

## Scope

**EXECUTION ORDER of the fix round: 153 -> 154 -> 155 -> 156 -> 157 -> 141 round 2.**

**HARD SAFETY RULE:** No agent may run `make memory-deploy-*`, `scripts/modal_*.py` as a process, or any `modal …` command outside task 141; a PATH shim does NOT intercept under `make`/`uv run` because `.venv/bin` wins — use the mocked unit tests and `DRY_RUN=yes`.
Run test commands one at a time — two concurrent pytest runs contend for the local MongoDB/Prefect and stall.
Do not run the real memory pipelines against the local database (shared with the human's `main` checkout).
The deploy scripts are edited and statically tested (AST / source), never executed.

**Root cause (`tasks/141` Log, "BLOCKER 3").** Both scripts mount the Volume at `/root/.cache/huggingface`
(`deploy/modal_sglang_llm.py:164-166`, `deploy/modal_vllm_embedding.py:134-136`). `lmsysorg/sglang:v0.5.18` already has
content there, and Modal refuses: `Runner failed with exception: cannot mount volume on non-empty path:
"/root/.cache/huggingface"` -> `Function modal_sglang_llm.Server is crash-looping`. The CUDA base of the vLLM script has
nothing there, which is the only reason 4b did not hit it too. The image itself built and deployed fine.

**Decision (ADR-009 §3).** ONE path, both scripts: mount `huggingface-cache` at `/cache/huggingface` and add to the
image env `HF_HOME=/cache/huggingface` and `HF_HUB_CACHE=/cache/huggingface/hub`.
- `HF_HOME` is the root `huggingface_hub` derives every cache path from (`huggingface_hub/constants.py:132-141`), and vLLM, SGLang, `transformers` and `autoinference-utils` all download through `huggingface_hub`. It keeps the Volume's EXISTING layout (`hub/` at the Volume root, exactly as under the old mount), so weights already cached stay valid.
- `HF_HUB_CACHE` is set as well because it OUTRANKS `HF_HOME` and the legacy `HUGGINGFACE_HUB_CACHE` (`constants.py:145-153`): a base image that sets either cannot move the weights off the Volume. Whether `lmsysorg/sglang` sets one is not knowable offline; one dict entry removes the question.
- `/cache/...` does not exist in either base image, so the mount target is empty by construction.

**What to build**
1. In BOTH scripts a module constant `HF_CACHE_DIR = "/cache/huggingface"` (spelled out in each — `tree` is not importable in the container), used as the `volumes={HF_CACHE_DIR: modal.Volume.from_name("huggingface-cache", create_if_missing=True)}` key and in the image layer: `.env({"HF_XET_HIGH_PERFORMANCE": "1", "HF_HOME": HF_CACHE_DIR, "HF_HUB_CACHE": f"{HF_CACHE_DIR}/hub", DEPLOY_SPEC_ENV: SPEC_ENV_VALUE})`.
2. One comment line above the constant saying why it is not under `/root/.cache` (the verbatim Modal error).
3. The vLLM script changes identically even though its base image tolerated the old path: one Volume, one layout, one path.
4. `apps/memory/README.md`: if it names `/root/.cache/huggingface`, replace the path (do not add a caveat).

**Out of scope:** a second Volume; `VLLM_CACHE_ROOT` / compile caches; anything in the endpoint route (Modal owns its volume there).

## Acceptance Criteria

- [ ] `tests/unit/deploy/test_modal_deploy_scripts.py::TestGlueContract::test_the_weights_volume_mounts_outside_the_home_cache` (both engines, AST): the `volumes=` keyword of the `@app.server(...)` decorator has exactly ONE key, it is the name `HF_CACHE_DIR`, `HF_CACHE_DIR == "/cache/huggingface"`, and its value is `modal.Volume.from_name("huggingface-cache", create_if_missing=True)`.
- [ ] `::test_hf_home_points_at_the_volume` (both engines, AST): the image `.env({...})` carries `HF_HOME` -> `HF_CACHE_DIR` and `HF_HUB_CACHE` -> `f"{HF_CACHE_DIR}/hub"`.
- [ ] `::test_both_scripts_share_one_cache_path` (not parametrized): the `HF_CACHE_DIR` literal of the two scripts is equal, and the string `/root/.cache` occurs in neither script's source.
- [ ] `TestTheGuardsCatchTheirMutants`: a mutant whose `volumes=` key is `"/root/.cache/huggingface"` FAILS `test_the_weights_volume_mounts_outside_the_home_cache`; `test_the_shipped_script_passes_every_guard` stays green for both engines.
- [ ] `test_only_the_hf_token_secret` stays green unchanged (`HF_TOKEN` is still in no image env key; `HF_HOME` / `HF_HUB_CACHE` are not credentials).
- [ ] `grep -rn "/root/.cache/huggingface" apps/memory/deploy apps/memory/README.md` -> no hit.
- [ ] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green.

## User Stories

### Story: Operator deploys a small LLM Modal refuses as an endpoint
1. `make memory-deploy-model MODEL=LiquidAI/LFM2.5-350M` (in #141 round 2 only) -> `→ SGLang App`.
2. `modal app logs ep-tree-lfm2-5-350m` shows `HF_TOKEN set in container: …` and then `SGLang serving LiquidAI/LFM2.5-350M …` — no `cannot mount volume on non-empty path`.
3. `make memory-deploy-model-test MODEL=LiquidAI/LFM2.5-350M` prints the six chat smoke lines.

### Story: The second cold start does not download the weights again
1. After 4b has served once, the container scales to zero and boots again (the cold-again proof of #141).
2. `modal volume ls huggingface-cache hub` (read-only, #141 only) lists `models--voyageai--voyage-4-nano` — the weights are ON the Volume, not in the container's home.

### Story: Engineer bumps the SGLang image tag
1. Changes `modal.engines.sglang.version` in YAML.
2. Whatever the new image ships under `/root/.cache`, the mount target `/cache/huggingface` is still empty, and `HF_HUB_CACHE` still wins over any cache variable the image sets.

### Story: Reviewer asks why the vLLM script changed when it worked
1. Reads `test_both_scripts_share_one_cache_path`: one Volume, one layout.
2. Reads ADR-009 §3: the old path only worked because the CUDA base happened to have nothing there.

---

Blocked by: #153

## Log

### [PA] 2026-09-21 16:10 — Grooming

**Summary**
The shared weights Volume moves to `/cache/huggingface` with `HF_HOME` + `HF_HUB_CACHE` in both scripts, pinned by static tests.

**Key decisions**
- `HF_HOME` (keeps the Volume's `hub/` layout, honoured by everything that downloads through `huggingface_hub`) plus `HF_HUB_CACHE` (highest precedence — immune to a base image that sets a cache variable).
- Same path and env in the vLLM script for symmetry, although its base image tolerated the old path.
- Live proof (container starts, weights land on the Volume) is #141 round 2's, cycles 4b and 4c.

**Dependencies**
- #153 — edits the same `.env({...})` layer of the same two files.

**User stories**
- 4: LLM App starts, weights persist on the Volume, image bump stays safe, symmetry is explained.

Ready for implementation.
