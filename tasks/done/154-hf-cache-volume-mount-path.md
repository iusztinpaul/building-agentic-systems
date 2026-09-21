---
id: 154-hf-cache-volume-mount-path
status: done
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

- [x] `tests/unit/deploy/test_modal_deploy_scripts.py::TestGlueContract::test_the_weights_volume_mounts_outside_the_home_cache` (both engines, AST): the `volumes=` keyword of the `@app.server(...)` decorator has exactly ONE key, it is the name `HF_CACHE_DIR`, `HF_CACHE_DIR == "/cache/huggingface"`, and its value is `modal.Volume.from_name("huggingface-cache", create_if_missing=True)`.
- [x] `::test_hf_home_points_at_the_volume` (both engines, AST): the image `.env({...})` carries `HF_HOME` -> `HF_CACHE_DIR` and `HF_HUB_CACHE` -> `f"{HF_CACHE_DIR}/hub"`.
- [x] `::test_both_scripts_share_one_cache_path` (not parametrized): the `HF_CACHE_DIR` literal of the two scripts is equal, and the string `/root/.cache` occurs in neither script's source.
- [x] `TestTheGuardsCatchTheirMutants`: a mutant whose `volumes=` key is `"/root/.cache/huggingface"` FAILS `test_the_weights_volume_mounts_outside_the_home_cache`; `test_the_shipped_script_passes_every_guard` stays green for both engines.
- [x] `test_only_the_hf_token_secret` stays green unchanged (`HF_TOKEN` is still in no image env key; `HF_HOME` / `HF_HUB_CACHE` are not credentials).
- [x] `grep -rn "/root/.cache/huggingface" apps/memory/deploy apps/memory/README.md` -> no hit.
- [x] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green.

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

### [SWE] 2026-09-21 17:40 — Implementation

**Files modified**
- `apps/memory/deploy/modal_sglang_llm.py` — `HF_CACHE_DIR = "/cache/huggingface"` (with the why-not-`/root/.cache` comment), the mount key, and `HF_HOME` / `HF_HUB_CACHE` in the image `.env({...})`.
- `apps/memory/deploy/modal_vllm_embedding.py` — the same three edits, identical text: one Volume, one layout, one path.
- `apps/memory/tests/unit/deploy/test_modal_deploy_scripts.py` — guards #8 and #9 (`_assert_the_weights_volume_mounts_outside_the_home_cache`, `_assert_hf_home_points_at_the_volume`), their two tests, `test_both_scripts_share_one_cache_path` (three-way: script vs script vs ADR-009 §3), three mutants, the image-env key allow-list, and the `_SPEC_ENV_LAYER` anchor (the `.env({...})` dict is now one entry per line, so the anchor loses its `}`).

**Tests**
- Unit: 3960 passing, 0 failing (`make memory-tests`); the deploy file alone 173 passing (was 160).
- Integration: N/A — this repo has no integration suite (AGENTS.md); the live proof is #141 round 2.

**Verification before coding** (tech-docs: context7 not needed, primary sources read directly; all fetched 2026-09-21)
- `huggingface_hub` `constants.py` (1.7.1, read in `apps/memory/.venv`): `HF_HOME` = env or `$XDG_CACHE_HOME|~/.cache` + `/huggingface`; `default_cache_path = HF_HOME/hub`; `HUGGINGFACE_HUB_CACHE` = env or `default_cache_path`; `HF_HUB_CACHE` = env or `HUGGINGFACE_HUB_CACHE`. So precedence is `HF_HUB_CACHE` > `HUGGINGFACE_HUB_CACHE` > `HF_HOME/hub` — exactly as ADR-009 §3 rev 6 states. (`TRANSFORMERS_CACHE` is gone in transformers 5.x, which both engines pin.)
- Both engines land on that same code: vLLM 0.26.0 `requirements/common.txt` pins `transformers >= 5.5.3`; SGLang v0.5.18 `python/pyproject.toml` pins `transformers==5.12.1`; transformers 5.12.1 `setup.py` pins `huggingface-hub>=1.5.0,<2.0`.
- `lmsysorg/sglang:v0.5.18` Dockerfile (`raw.githubusercontent.com/sgl-project/sglang/v0.5.18/docker/Dockerfile`): sets NO `HF_*`, `HUGGINGFACE_*`, `TRANSFORMERS_CACHE` or `XDG_CACHE_HOME` — nothing in the image contests our env. It is also the ROOT CAUSE, in two lines: the framework stage does `mkdir -p /root/.cache/huggingface` + `kernels download python`, and the runtime stage does `COPY --from=framework_final /root/.cache/huggingface /root/.cache/huggingface` ("Copy cache for kernels from kernels community"). Hence the non-empty path. No bare `/cache` occurs anywhere in that Dockerfile, and `nvidia/cuda:*-ubuntu22.04` has no `/cache` either — the new mount target is empty by construction.
- `autoinference-utils` 0.2.6 (wheel downloaded into the session scratchpad and unzipped — NOT installed into the project): `endpoint.py` names no cache path and sets no `HF_*`; the only env writes are `SGLANG_LOG_REQUEST_HEADERS` and the benchmark runner's `{**os.environ, AUTOINFERENCE_BENCH_*}`, so the engine child inherits our `HF_HOME` / `HF_HUB_CACHE` unchanged.
- Modal's own docs corroborate the fix verbatim (`modal.com/llms-full.txt`, fetched 2026-09-21): "Note that the SGLang image already has files under `/root/.cache/huggingface`, so we mount the Volume at `/cache` and point `HF_HOME` there." Their recipe uses `HF_CACHE_DIR` + `HF_HOME`; ours is `/cache/huggingface`, which additionally preserves the Volume's existing layout. The "non-empty path" rule itself is NOT in the prose docs — the evidence is the runner error in `tasks/141` (`scratchpad/e2e-141/4c-app2.log`, 10x `cannot mount volume on non-empty path: "/root/.cache/huggingface"`).

**Acceptance criteria**
- [x] `TestGlueContract::test_the_weights_volume_mounts_outside_the_home_cache` — both engines, `_assert_the_weights_volume_mounts_outside_the_home_cache`.
- [x] `::test_hf_home_points_at_the_volume` — both engines, plus `sorted(_image_env_keys(module)) == ["HF_HOME", "HF_HUB_CACHE", "HF_XET_HIGH_PERFORMANCE"]`.
- [x] `test_both_scripts_share_one_cache_path` — one literal, `/root/.cache` in neither source, AND the same three strings in ADR-009 §3 (`` `/cache/huggingface` ``, `` `HF_HOME=/cache/huggingface` ``, `` `HF_HUB_CACHE=/cache/huggingface/hub` ``). The ADR is READ, never written; the test asserts the file exists first, so a moved ADR fails loudly instead of passing vacuously. Precedent for a test reaching repo-root docs: `tests/unit/test_embedding_map_docs.py`.
- [x] `TestTheGuardsCatchTheirMutants::test_a_volume_mounted_on_the_home_cache_fails` (the pre-#154 mount) and `::test_a_cache_env_that_leaves_the_volume_fails[home-cache|no-hub-cache]`; `test_the_shipped_script_passes_every_guard` now runs nine guards, green for both engines.
- [x] `test_only_the_hf_token_secret` untouched and green — the two new keys are literals in the allow-list of the NEW test, so nothing in the token guard was loosened (`_image_env_keys` still returns every key of every `.env({...})`, and `HF_TOKEN` is still absent; `source.count("HF_TOKEN") == 2` still holds).
- [x] `grep -rn "/root/.cache/huggingface" apps/memory/deploy apps/memory/README.md` -> exit 1, no hit. Nothing under `apps/memory/README.md`, `.agents/skills`, `src/` or `configs/` ever named the path, so no doc edit was needed.
- [x] `make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` all green.

**Evidence**
```
$ PYTEST_ADDOPTS="tests/unit/deploy/test_modal_deploy_scripts.py -q -p no:randomly" make memory-tests   # RED, before the scripts changed
13 failed, 160 passed in 1.49s
FAILED ...::TestGlueContract::test_the_weights_volume_mounts_outside_the_home_cache[sglang]
FAILED ...::TestGlueContract::test_hf_home_points_at_the_volume[vllm]
FAILED ...::test_both_scripts_share_one_cache_path
FAILED ...::TestTheGuardsCatchTheirMutants::test_a_volume_mounted_on_the_home_cache_fails[vllm]
  (assertion failures: `the script has no module-level HF_CACHE_DIR = ...` / the mutation anchor does not exist yet)

$ make memory-tests   # GREEN, after
3960 passed in 59.51s

$ make memory-format-check && make memory-lint-check && make pre-commit
313 files already formatted / All checks passed! / prettier, ruff check, ruff format, biome: Passed

$ grep -rn "/root/.cache/huggingface" apps/memory/deploy apps/memory/README.md; echo $?
1

$ python3 - <<'PY'   # static render of the two changed layers; no `modal`, no import of the scripts
...ast-only...
PY
modal_vllm_embedding.py: mount=/cache/huggingface  env(literal keys)={'HF_XET_HIGH_PERFORMANCE': '1', 'HF_HOME': '/cache/huggingface', 'HF_HUB_CACHE': '/cache/huggingface/hub'}
modal_sglang_llm.py:     mount=/cache/huggingface  env(literal keys)={'HF_XET_HIGH_PERFORMANCE': '1', 'HF_HOME': '/cache/huggingface', 'HF_HUB_CACHE': '/cache/huggingface/hub'}
```

**Notes**
- **The weights already on the Volume stay valid — confirmed, not assumed.** The old mount put the Volume's ROOT at `/root/.cache/huggingface`, which was also the DEFAULT `HF_HOME` (no `HF_*` was set), so `huggingface_hub` wrote its hub cache to `HF_HOME/hub` = `<volume root>/hub`. Mounting the same Volume at `/cache/huggingface` with `HF_HOME=/cache/huggingface` resolves the hub cache to `<volume root>/hub` again — the same directory, same `models--<org>--<name>` entries. `HF_HUB_CACHE=/cache/huggingface/hub` names that identical directory, so it PINS the layout rather than redirecting it. Nothing is re-downloaded and nothing is orphaned. #141 round 2's `modal volume ls huggingface-cache hub` is the live confirmation.
- **Consequence of moving `HF_HOME`, named deliberately:** the SGLang image's baked kernels-community cubins live in `/root/.cache/huggingface` (the `COPY` above). With the cache root moved, `kernels` will not find them and will fetch them into the Volume (or JIT-compile) on the first cold start. This is NOT a regression: mounting the Volume over that path — the old design — would have HIDDEN those cubins entirely, which is precisely what Modal's refusal prevents; and upstream already treats runtime JIT as a supported fallback (the Dockerfile's "no matching sgl-flash-attn3 cubin variant … kernels will be JIT-compiled at runtime" branch). Cost: a longer FIRST boot for the LLM App, then cached on the Volume. #141 round 2 cycle 4c's timing will observe it; `STARTUP_TIMEOUT` stays 20 min / `HEALTH_TIMEOUT` 18 min, which is what that budget is for.

  > SUPERSEDING NOTE (added at commit time, per the Tester's ruling 2): the mechanism in the paragraph above is wrong. `SGLANG_USE_SGL_FA3_KERNEL` defaults to `True` (`environ.py:1056`) and nothing in this repo overrides it, so SGLang loads its AOT-baked `sgl_kernel` extension and never touches the Hub kernel under the moved cache — for the shipped configuration this mount change is a no-op for kernel loading, and there is no first-boot download and no startup-budget cost. Even with the flag opted out, the lockfile check reads `SGLANG_CACHE_DIR` (`/root/.cache/sglang`), a directory this task's mount does not touch, so the call is `kernels.load_kernel()` — cache-only, never downloads. It raises on the cache miss, SGLang catches it, logs one warning and falls back to the same baked kernel on every cold start (permanent, not first-boot-only). No download, no JIT, no timeout risk. The original text above is left as written; this note supersedes it.

- **Test-harness change forced by the reformat:** with four entries the `.env({...})` dict is now one key per line, so the old anchor `"DEPLOY_SPEC_ENV: SPEC_ENV_VALUE}"` (trailing `}`) no longer occurs. `_SPEC_ENV_LAYER` and #153's two mutation strings dropped that brace; `test_a_spec_not_baked_as_the_shared_base64_value_fails` still fails on the `stored == [SPEC_ENV_VALUE]` assert (the one whose message carries the name the `pytest.raises(match=...)` looks for), both ids, both engines.
- **The vLLM script's comment names the SGLang image on purpose.** Both files carry the SAME comment ("the official SGLang image COPYs a kernels cache into it"), because that is where the live error came from and ADR-009 §3 mandates one path for both engines — the reviewer story in this task asks exactly that question. It is a deliberate cross-reference, not a copy-paste slip: `test_the_script_talks_about_its_own_kind_only` still passes, and `SGLang` is not in the vLLM script's retired vocabulary (`fallback|eject|ladder|serving path|SERVING=|(?<!v)llm|chat`).
- **One transient full-suite run, not reproducible.** The first `make memory-tests` after the ADR guard landed reported `2 failed, 3958 passed` — I had piped it through `tail -3`, so the names were lost. NINE consecutive full runs since (including the identical `make pre-commit && make memory-tests` chain, logs in the session scratchpad `full-run-*.log`) are `3960 passed`, and `grep "^FAILED"` finds nothing in any of them. Most plausible cause is contention on the shared LOCAL MongoDB/Prefect with another checkout, which AGENTS.md warns about; nothing in this diff touches a database. Flagged for the Tester to watch rather than silently dropped.
- **NOT RUN — the live path.** No `modal` command, no deploy script execution, no `make memory-deploy-*` (hard safety rule, #141 owns those). Step 7's "run it end-to-end" is therefore an AST-only render of the two layers (above) plus the full unit suite; the real container start is #141 round 2, cycles 4b/4c.
- Out of scope, untouched, as groomed: `VLLM_CACHE_ROOT` / compile caches, a second Volume, the endpoint route's own volume.

### [Tester] 2026-09-21 18:55 — QA

Commands run, in order, all one at a time, LOCAL env (`make env-status` → `local`): `git status --short`, `git diff --stat`, `git diff` (full), `PYTEST_ADDOPTS="tests/unit/deploy/test_modal_deploy_scripts.py -q" make memory-tests`, `make memory-format-check`, `make memory-lint-check`, `make pre-commit`, `make memory-tests` ×4 (each output captured in full to a scratchpad log, not `tail`), `PYTEST_ADDOPTS="tests/unit/deploy/test_modal_deploy_scripts.py -k test_a_spec_not_baked -v" make memory-tests`, `PYTEST_ADDOPTS="tests/unit/deploy/test_modal_deploy_scripts.py -k 'test_the_script_talks_about_its_own_kind_only or test_both_scripts_share_one_cache_path' -v" make memory-tests`, `PYTEST_ADDOPTS="tests/unit/deploy/test_modal_deploy_scripts.py -k test_only_the_hf_token_secret -v" make memory-tests`, several `grep -rn` checks, and a scratch-only Python harness (`uv run python <scratchpad>/adversarial_check.py`, never touching the repo) that imports the test module's own AST helpers to mutate 10 fresh COPIES of both scripts (never the shipped files) and re-runs the guards. No `modal …` command, no `scripts/modal_model.py` process, no `make memory-deploy-*`, no import/execution of the deploy scripts — only `ast.parse`. Public read-only `curl` GETs against `raw.githubusercontent.com` (sgl-project/sglang @ v0.5.18), `huggingface.co/api` (public model metadata) and PyPI-hosted GitHub source (`huggingface/kernels`) for the kernels investigation below. `.env`/`.env.prod` never opened.

I independently re-verified all 7 ACs from primary sources/commands rather than trusting the SWE's pre-checked boxes; every checkbox below reflects my own run, not an inherited state.

**Test summary**
- Format / lint / pre-commit: PASS (`ruff format --check`: 313 files already formatted; `ruff check`: All checks passed!; `pre-commit`: prettier/ruff check/ruff format/biome all Passed)
- Deploy-file-only unit tests: 173 passed / 0 failed
- Full unit suite: 4 consecutive runs, each **3960 passed / 0 failed**, `grep -c "^FAILED"` = 0 on every log
- Warnings: 1 pre-existing, unrelated (`pydantic.v1` / Python 3.14 `UserWarning` from `opik`'s vendored rest client, same on every run, not introduced by this diff)

**(1) Flakiness ruling — NON-BLOCKING, not attributable to this branch.** 4 consecutive full `make memory-tests` runs (full output captured, not `tail`'d): `3960 passed` every time, 0 lines matching `^FAILED` in any log. `tests/unit/deploy/test_modal_deploy_scripts.py` (this task's only test file) has no wall-clock read, no network call, no `os.getcwd()` dependency (paths are built from `pathlib.Path(__file__).resolve()`), and no shared mutable state across tests — every mutation happens on a fresh `tmp_path` copy, and the shipped scripts are only ever read. Nothing in this diff touches MongoDB/Prefect. I did open `tests/unit/test_modal_sdk_rail.py` (named in the brief) as the most plausible carrier of order/contention sensitivity: it is NOT part of this diff, but `test_the_conftest_imports_the_sdk_lazily` spawns a real `subprocess.run([sys.executable, "-c", ...])` (a fresh interpreter re-importing `tests.unit.conftest`), which is exactly the kind of test that gets measurably slower — not wrong, slower — under host contention (e.g. another checkout's suite or Mongo/Prefect running concurrently), matching AGENTS.md's documented warning. That file predates #154 (untouched by `git diff --stat`), so even if it is the transient's origin, it is **not introduced by this task** and the "blocking for the task that introduced it" rule does not attach to #154. I cannot prove a negative; I can report that 4/4 of my own runs were clean and name the one pre-existing candidate.

**(2) The kernels trade-off — verified from primary source, RESOLVED, non-blocking, and the SWE's stated mechanism is inaccurate.** Traced the exact runtime call site in `sgl-project/sglang` @ v0.5.18 (`python/sglang/kernels/ops/attention/flash_attention_v3.py::_load_fa3_kernels`) plus `python/sglang/srt/environ.py` and `huggingface/kernels` (`src/kernels/load.py`, `hf_hub.py`, `resolver.py`, `_snapshot_download.py:211-212` in this repo's own `.venv`):
  - `SGLANG_USE_SGL_FA3_KERNEL` defaults to `True` (`environ.py:1056`). When true — and nothing in this codebase (`configs/default.yaml`, `extra_server_args`, either deploy script) ever sets it `False` — SGLang loads `sgl_kernel.flash_attn`, an AOT extension **compiled directly into the Python wheel**, entirely independent of `HF_HOME`/`HF_HUB_CACHE`/the Volume. **The `kernels-community/sgl-flash-attn3` Hub kernel the Dockerfile bakes into `/root/.cache/huggingface` is never loaded by the default configuration this repo ships — task 154's mount-path change is a no-op for this concern as deployed today.**
  - Even in the hypothetical where an operator sets `SGLANG_USE_SGL_FA3_KERNEL=False`: the code checks `os.path.exists(SGLANG_CACHE_DIR/kernels.lock)` first. `SGLANG_CACHE_DIR` defaults to `/root/.cache/sglang` (`environ.py:991`) — a directory the Dockerfile bakes and `COPY`s separately from `/root/.cache/huggingface`, so it is **untouched** by this task's Volume mount and the lockfile always exists. That routes SGLang to `kernels.load_kernel()`, not `kernels.get_kernel()`. `load_kernel()`'s own docstring: "This function will never download anything and will fail when a kernel is not available locally" — confirmed in source: it always builds a `LockedHubCacheResolver`, whose `.resolve()` calls `snapshot_download(..., local_files_only=True)` (`resolver.py:366-378`), i.e. cache-only, no network fallback inside `kernels` itself. So the SWE's Log claim ("`kernels` will not find them and will fetch them into the Volume … on the first cold start … cost: a longer FIRST boot … then cached on the Volume") **does not match how `kernels`/SGLang actually resolve this** — it would not download at all, and nothing would ever get cached onto the Volume via this path. What actually happens: `load_kernel()` raises `FileNotFoundError` (cache miss under the moved `HF_HUB_CACHE`), SGLang's own `except Exception` in `_load_fa3_kernels` catches it, logs one `logger.warning`, and falls back to the **same** `sgl_kernel` AOT implementation used by default — no crash, no hang, no network traffic, but the fallback recurs on **every** cold start (permanent, not "first-boot-only"), silently downgrading to what SGLang's own comment calls "less efficient but more compatible."
  - Net verdict on the 20-minute `STARTUP_TIMEOUT` / 18-minute `HEALTH_TIMEOUT`: **confirmed safe, not merely estimated** — no download and no crash occur through this path for the shipped configuration, so there is nothing that could consume startup budget from this angle.
  - On the brief's proposed "cheap alternative" (leave `HF_HOME` default, set only `HF_HUB_CACHE`): **verified it does NOT work**, in this repo's own `.venv` (`huggingface_hub/_snapshot_download.py:211-212`: `if cache_dir is None: cache_dir = constants.HF_HUB_CACHE`). `kernels`'s own cache resolution (`hf_hub.py::_get_cache_dir`, `os.environ.get("KERNELS_CACHE", None)`) passes `cache_dir=None` straight through to `snapshot_download`, which then falls back to `HF_HUB_CACHE` regardless of `HF_HOME` — so redirecting only `HF_HUB_CACHE` to the Volume would *also* redirect `kernels` lookups there, same as today's shipped fix. The alternative that would actually decouple them is a fourth env key, `KERNELS_CACHE=/root/.cache/huggingface/hub` (the directory confirmed via the Dockerfile: `HF_HOME` is unset at build time, so `kernels download python` lands under the default `HF_HOME/hub`) — but this is new scope beyond ADR-009 §3's decision text and task 154's "What to build," only relevant if `SGLANG_USE_SGL_FA3_KERNEL=False` is ever adopted, and is **not required for this task**. Recommend as a follow-up note only if that flag is ever flipped.
  - This item is a **PASS with note**, not a FAIL: it does not breach the timeout (confirmed, not assumed), and the corrected mechanism should replace the SWE's Log narrative in a follow-up so nobody is surprised when `modal app logs` shows a warning instead of a multi-hundred-MB download.

**(3) Guard #7 (`_SPEC_ENV_LAYER` anchor) not weakened — confirmed.** `test_a_spec_not_baked_as_the_shared_base64_value_fails[raw-json-sglang/vllm]` and `[recomputed-sglang/vllm]` all 4 PASSED, each still raising `AssertionError` matching `_SPEC_VALUE_NAME` (`pytest.raises(match=_SPEC_VALUE_NAME)` succeeded) — i.e. the mutants (`DEPLOY_SPEC_ENV: json.dumps(SPEC)` / `DEPLOY_SPEC_ENV: encode_deploy_spec(RESOLVED_SPEC)`, no longer anchored by the trailing `}` that the ruff reformat removed) are still caught for the *same reason* as before #154: the image layer must bake the bare `SPEC_ENV_VALUE` name, not a re-serialised/recomputed one. The reformat only removed one character (`}`) from a text anchor used purely to locate the mutation site in `_mutate`; the AST-level assertion in `_assert_the_spec_crosses_as_base64` is untouched.

**E2E adversarial pass**
- Happy path: `PYTEST_ADDOPTS="tests/unit/deploy/test_modal_deploy_scripts.py -q" make memory-tests` → `173 passed in 1.03s` (PASS)
- Break path 1 (mutant: revert mount to `/root/.cache/huggingface`, both engines, on scratch copies): guard raises `AssertionError` naming `HF_CACHE_DIR` (PASS — caught)
- Break path 2 (mutant: mount key becomes the literal string `"/cache/huggingface"` instead of the `HF_CACHE_DIR` name, both engines): guard raises `AssertionError` ("not `'/cache/huggingface'`") (PASS — caught; the SWE's own mutant only tested the *old* path text, not a same-value literal — I extended coverage myself)
- Break path 3 (mutant: `HF_HUB_CACHE` redirected to `/root/.cache/huggingface/hub`, both engines): guard raises `AssertionError` naming `HF_HUB_CACHE` (PASS — caught)
- Break path 4 (mutant: `HF_HOME` entry dropped entirely, both engines): guard raises `AssertionError` (`baked.get('HF_HOME')` is `None`) (PASS — caught)
- Break path 5 (mutant: extra image-env key `HF_TOKEN` / `HF_HUB_ENABLE_HF_TRANSFER` baked in, both engines): `sorted(_image_env_keys(...))` mismatches the allow-list (PASS — caught; a credential leak would be rejected)
- Break path 6 (mutant: a SECOND `.env({"HF_TOKEN": …})` on a second Image object, both engines): `_image_env_dicts` walks the whole module and still catches the extra key (PASS — caught)
- Break path 7 (mutant: Volume name changed to `"huggingface-cache-v2"`, both engines): the per-engine hardcoded-literal comparison (`ast.unparse(volumes.values[0]) == _WEIGHTS_VOLUME`) fails (PASS — caught; confirms the shared Volume NAME is pinned across both scripts even though no test compares the two scripts' names directly — each engine's guard is pinned to the same hardcoded string)
- Break path 8 (mutant: `create_if_missing=True` dropped, both engines): same literal-comparison guard fails (PASS — caught)

All 8 of my own adversarial mutants (16 cases across both engines) were caught by the shipped guards; none were "not caught."

**Acceptance criteria**
- [x] PASS — `test_the_weights_volume_mounts_outside_the_home_cache` (both engines, AST) — ran directly: `TestGlueContract::test_the_weights_volume_mounts_outside_the_home_cache[sglang]` and `[vllm]` pass in the 173-item run; independently re-verified the single-key/name/path/Volume assertions via my own scratch harness.
- [x] PASS — `test_hf_home_points_at_the_volume` (both engines, AST) — passes in the 173-item run; independently re-verified `HF_HOME`/`HF_HUB_CACHE` targets and the sorted 3-key allow-list via my own mutants.
- [x] PASS — `test_both_scripts_share_one_cache_path` — ran directly (`-k` selection above), 1 passed; confirmed the ADR file (`docs/adrs/009_embedding_roles_and_modal_embedding_catalog.md:202-203`) carries all 3 literals verbatim.
- [x] PASS — `TestTheGuardsCatchTheirMutants` (mount-outside-home-cache mutant, `test_the_shipped_script_passes_every_guard`) — 173 passed includes `test_a_volume_mounted_on_the_home_cache_fails[sglang/vllm]` and the control row for both engines.
- [x] PASS — `test_only_the_hf_token_secret` — ran directly, 2 passed (sglang, vllm); diff confirms the function body itself is untouched by this PR (only new tests were appended after it).
- [x] PASS — `grep -rn "/root/.cache/huggingface" apps/memory/deploy apps/memory/README.md` → exit 1, no hit (ran directly). Also confirmed 0 hits for the bare `/root/.cache` anywhere under `apps/memory/deploy`.
- [x] PASS — `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` — ran each individually (one at a time), all green; `memory-tests` run 4× consecutively, 3960/3960/3960/3960 passed.

**Evidence**
```
$ PYTEST_ADDOPTS="tests/unit/deploy/test_modal_deploy_scripts.py -q" make memory-tests
173 passed in 1.03s

$ make memory-format-check
uv run ruff format --check src/ tests/ scripts/ deploy/
313 files already formatted

$ make memory-lint-check
uv run ruff check src/ tests/ scripts/ deploy/
All checks passed!

$ make pre-commit
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed

$ make memory-tests   # x4, full output captured, not tail'd
============================ 3960 passed in 55.69s =============================
============================ 3960 passed in 55.65s =============================
============================ 3960 passed in 59.32s =============================
============================ 3960 passed in 57.67s =============================
$ grep -c "^FAILED" full-run-{1,2,3,4}.log
0 0 0 0

$ grep -rn "/root/.cache/huggingface" apps/memory/deploy apps/memory/README.md; echo $?
1

$ git status --short
 M apps/memory/deploy/modal_sglang_llm.py
 M apps/memory/deploy/modal_vllm_embedding.py
 M apps/memory/tests/unit/deploy/test_modal_deploy_scripts.py
 M tasks/154-hf-cache-volume-mount-path.md
```

**Other issues found**
- The SWE's Log narrative on the kernels trade-off ("`kernels` will not find them and will fetch them into the Volume (or JIT-compile) on the first cold start … then cached on the Volume") does not match how `kernels`/SGLang actually resolve the FA3 cubin (see (2) above): no download, no JIT, and the fallback is silent and permanent, not one-time. It doesn't change the verdict (the shipped `SGLANG_USE_SGL_FA3_KERNEL` default never reaches that code path at all), but the narrative should be corrected in a follow-up so it isn't relied on later.
- 0 secret-shaped strings found in the diff (pattern scan for `sk-…`, `hf_…`, `AKIA…`, PEM headers — count only, per rule).
- No scratch files left in the repo; `git status --short` at the end matches exactly the SWE's 4 modified files (verified above).

**VERDICT: PASS**
