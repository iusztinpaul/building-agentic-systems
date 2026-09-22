---
id: 153-deploy-spec-base64-transport
status: done
feature: voyage-4-and-modal-embedding-catalog
---

# The deploy spec crosses into the container as base64: a catalog value holding a `"` no longer kills the App at import (the DEFAULT embedding model, `voyageai/voyage-4-nano`, could not be served)

Tags: `memory`, `modal`, `deploy`, `bug`, `tests`
Depends on: None
Blocks: #154, #141
Implements: ADR-009 — Decision 3, revision 6 ("the base64 of the spec's JSON"). Found live by #141 round 1, cycle 4b.

## Scope

**EXECUTION ORDER of the fix round: 153 -> 154 -> 155 -> 156 -> 157 -> 141 round 2.**

**HARD SAFETY RULE:** No agent may run `make memory-deploy-*`, `scripts/modal_*.py` as a process, or any `modal …` command outside task 141; a PATH shim does NOT intercept under `make`/`uv run` because `.venv/bin` wins — use the mocked unit tests and `DRY_RUN=yes`.
Run test commands one at a time — two concurrent pytest runs contend for the local MongoDB/Prefect and stall.
Do not run the real memory pipelines against the local database (shared with the human's `main` checkout).
The deploy scripts are edited and statically tested (AST / source), never imported with a real `modal` deploy and never executed.

**Root cause (proven byte-exactly by the SWE, `tasks/141` Log, "BLOCKER 2").** Both App scripts bake the spec with
`.env({…, DEPLOY_SPEC_ENV: json.dumps(SPEC)})` (`deploy/modal_vllm_embedding.py:113`, `deploy/modal_sglang_llm.py`
same shape). Modal 1.5.5 renders that as a Dockerfile `ENV {key}={shlex.quote(val)}` (`modal/_image.py:2821`) and the
build's `ENV` parsing drops each backslash, so every `\"` becomes `"`. voyage-4-nano's spec carries JSON inside JSON
(`--pooler-config '{"pooling_type":"MEAN"}'`, `--hf-overrides '{"architectures":[…]}'`) and the container crash-loops
at import: `json.decoder.JSONDecodeError: Expecting ',' delimiter: line 1 column 331 (char 330)`. LFM2.5's quote-free
spec crossed intact, which is why no test saw it.

**Decision (ADR-009 §3): base64.** The env var's value becomes `base64.b64encode(<spec JSON, UTF-8>)` — alphabet
`[A-Za-z0-9+/=]`, nothing any quoting layer (shlex, Dockerfile `ENV`, its `$VAR` expansion) can interpret. Rejected:
pre-escaping backslashes (emulates an inferred parser, still loses to `$`); a file in the image or a second Secret (new
path contract; breaks §9's one-Secret guard).

**What to build**
1. `tree.models.modal_catalog`: `encode_deploy_spec(spec: DeploySpec) -> str` — `base64.b64encode(spec.model_dump_json().encode("utf-8")).decode("ascii")`. Pure, no `modal` import. Update the comment above `EMBEDDING_DEPLOY_SPEC_ENV` / `LLM_DEPLOY_SPEC_ENV`.
2. Both scripts, under `modal.is_local()`: keep `SPEC = <build…>(…).model_dump()` and add `SPEC_ENV_VALUE = encode_deploy_spec(<the same spec object>)`. In the `else:` branch: `SPEC_ENV_VALUE = os.environ["<…>_DEPLOY_SPEC"]` and `SPEC = json.loads(base64.b64decode(SPEC_ENV_VALUE))`. The image layer becomes `.env({"HF_XET_HIGH_PERFORMANCE": "1", DEPLOY_SPEC_ENV: SPEC_ENV_VALUE})` — ONE name on both sides, so the image definition is byte-identical where Modal re-imports the file. `import base64` is stdlib (no image change). Update both module docstrings ("ONE JSON env var" -> base64, one sentence of why).
3. The existing static guards stay green or are amended DELIBERATELY: `test_the_spec_env_var_matches_the_catalog_constant` (the literal `os.environ["<SPEC_ENV>"]` still appears once), `test_the_environment_is_read_only_where_it_must_be` (still exactly three reads), `test_only_the_hf_token_secret`, `TestTheGuardsCatchTheirMutants`.

**Out of scope:** the Volume mount path (#154); logging the decoded spec in the container (the log allow-list stays as is).

## Acceptance Criteria

- [x] `tests/unit/models/test_modal_catalog.py::test_deploy_spec_survives_modal_and_docker_env_rendering` — parametrized over BOTH seeds' specs (`build_deploy_spec("voyageai/voyage-4-nano")`, `build_llm_deploy_spec("LiquidAI/LFM2.5-350M")`): take the env value, render it the way Modal + the build do (`shlex.quote(value)`, undo the shell quoting with `shlex.split(...)[0]`, then drop every backslash keeping the next character: `re.sub(r"\\(.)", r"\1", …)`), decode it the way the container does, and assert it equals `spec.model_dump()`. **The SWE first writes this test against the OLD transport (`json.dumps(spec.model_dump())` + `json.loads`) and pastes in `## Log` that the voyage-4-nano case FAILS with `JSONDecodeError` at `char 330` and the LFM2.5 case passes** — the unit-level reproduction of the live crash — then switches it to `encode_deploy_spec`.
- [x] `::test_encoded_deploy_spec_alphabet` — for both seeds `re.fullmatch(r"[A-Za-z0-9+/=]+", encode_deploy_spec(spec))`.
- [x] `::test_deploy_spec_roundtrips_any_byte` — an `EmbeddingDeploySpec` whose `server_args` values are `'"'`, `"\\"`, `"$HOME"`, `"'"`, `` "`id`" ``, `"a\nb"`, `"é"` round-trips through the same rendering simulation unchanged.
- [x] `::test_deploy_spec_has_no_credential_field` — no field name of `DeploySpec`, `EmbeddingDeploySpec`, `LLMDeploySpec` contains `token`, `secret`, `key` or `password` (case-insensitive); and with `settings.hf_token` monkeypatched to `SecretStr("hf_SENTINEL_153")`, the DECODED env value of both seeds does not contain `hf_SENTINEL_153`.
- [x] `tests/unit/deploy/test_modal_deploy_scripts.py::TestGlueContract::test_the_spec_crosses_as_base64` (both engines, AST): the value stored under `DEPLOY_SPEC_ENV` in the image `.env({...})` is the bare name `SPEC_ENV_VALUE`; that name is assigned exactly twice — from a call to `encode_deploy_spec` inside the `modal.is_local()` branch and from `os.environ[…]` in the `else` branch; the `else` branch contains `base64.b64decode`; `json.dumps` appears nowhere in the script.
- [x] `TestTheGuardsCatchTheirMutants`: a mutant that reverts the layer to `DEPLOY_SPEC_ENV: json.dumps(SPEC)` FAILS `test_the_spec_crosses_as_base64`; `test_the_shipped_script_passes_every_guard` stays green.
- [x] `grep -c "ONE JSON env var" apps/memory/deploy/*.py apps/memory/src/tree/models/modal_catalog.py` -> 0 in every file.
- [x] `make memory-deploy-model MODEL=voyageai/voyage-4-nano DRY_RUN=yes` exits 0 and starts no `modal` process (unchanged behaviour; paste the line).
- [x] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green.

## User Stories

### Story: Operator deploys the default embedding model and the container starts
1. Operator runs `make memory-deploy-model MODEL=voyageai/voyage-4-nano` (in #141 round 2 only).
2. `modal app logs ep-tree-voyage-4-nano` shows `HF_TOKEN set in container: …` as the first line — not `JSONDecodeError`.
3. `make memory-deploy-model-test MODEL=voyageai/voyage-4-nano` reaches `3 embeddings, 2048 dims`.

### Story: Engineer adds a model whose server arg is JSON
1. Adds `"--hf-overrides": '{"architectures":["X"]}'` to a catalog entry.
2. Runs `make memory-tests`: `test_deploy_spec_survives_modal_and_docker_env_rendering` passes for the seeds and `test_deploy_spec_roundtrips_any_byte` proves quotes, backslashes and `$` cross intact.
3. Never learns how Modal quotes a Dockerfile `ENV`.

### Story: Reviewer checks the opaque blob cannot hide a token
1. Opens `test_deploy_spec_has_no_credential_field`.
2. Sees the spec models have no credential-shaped field and that a sentinel `HF_TOKEN` never appears in the decoded value.
3. `test_only_the_hf_token_secret` still asserts `HF_TOKEN` is in no image env key.

### Story: Engineer reads a deployed image's spec
1. Copies the `EMBEDDING_DEPLOY_SPEC=` value from the image layer.
2. Runs `echo <value> | base64 -d` and reads the JSON — the script's docstring told them to.

---

Blocked by: (none)

## Log

### [PA] 2026-09-21 16:10 — Grooming

**Summary**
The spec env var becomes base64 so no quoting layer can corrupt it; a Modal-free unit test reproduces the live crash first, then proves the fix for both seeds and for hostile bytes.

**Key decisions**
- base64 over pre-escaping or a file/Secret transport (ADR-009 §3 rev. 6): smallest change that is robust to ANY byte.
- One `SPEC_ENV_VALUE` name on both sides of `is_local()` keeps the image definition identical on re-import.
- Token safety of an opaque blob is pinned on the spec MODEL (no credential field + sentinel test), since the `.env` literal guards cannot see inside it.

**Dependencies**
- None.

**User stories**
- 4: container starts, JSON server arg survives, blob carries no token, blob is still readable.

Ready for implementation.

### [SWE] 2026-09-21 17:05 — Implementation

**Files modified**
- `apps/memory/src/tree/models/modal_catalog.py` — `encode_deploy_spec(spec) -> str` (base64 of `model_dump_json()`, stdlib only, still no `modal` import); the comment above `EMBEDDING_DEPLOY_SPEC_ENV` / `LLM_DEPLOY_SPEC_ENV` now says base64.
- `apps/memory/deploy/modal_vllm_embedding.py` — `SPEC_ENV_VALUE` on both sides of `modal.is_local()` (encoder output locally, the env var's raw string in the container); the image layer bakes that bare name; the container decodes `json.loads(base64.b64decode(..., validate=True))` and raises `RuntimeError(SPEC_DECODE_ERROR)` naming `EMBEDDING_DEPLOY_SPEC` on anything else; docstring rewritten (why base64, `base64 -d` to read a layer).
- `apps/memory/deploy/modal_sglang_llm.py` — the same three changes, `LLM_DEPLOY_SPEC`.
- `apps/memory/tests/unit/models/test_modal_catalog.py` — the Modal-free rendering simulation + 6 transport tests (12 cases).
- `apps/memory/tests/unit/deploy/test_modal_deploy_scripts.py` — guard #7 (`_assert_the_spec_crosses_as_base64`), its 2 mutants, the AST-lifted container decode (drift guard + 6 corruption cases per engine).

**Tests**
- Unit: 3949 passing, 0 failing (`make memory-tests`), up from 3917 before this task — +32 cases, no test removed, nothing skipped.
- Integration: N/A — the project has no integration suite (AGENTS.md); no infra touched.

**Acceptance criteria** — all ten ticked above. Notes on three of them:
- `test_deploy_spec_survives_modal_and_docker_env_rendering` and `test_encoded_deploy_spec_alphabet` are parametrized over ALL FOUR seeds (both embedding + both LLM), a superset of the two the AC names.
- `TestTheGuardsCatchTheirMutants` gained TWO mutants, not one: the shipped-before transport (`DEPLOY_SPEC_ENV: json.dumps(SPEC)`) and a re-encode in the layer (`encode_deploy_spec(RESOLVED_SPEC)`), which would make the local and container sides compute the value twice instead of sharing one name.
- The existing guards are unchanged and green: `test_the_spec_env_var_matches_the_catalog_constant`, `test_the_environment_is_read_only_where_it_must_be` (still exactly three reads), `test_only_the_hf_token_secret` (`HF_TOKEN` still occurs exactly twice in each script), `test_the_shipped_script_passes_every_guard` (now seven guards).

**Evidence**

*1. RED — the unit-level reproduction of the live crash, before the fix* (the OLD transport, `json.dumps(spec.model_dump())`, through the same rendering simulation the shipped test uses):

```
$ uv run python scratch/red_step.py     # shlex.quote -> shlex.split[0] -> re.sub(r"\\(.)", r"\1")
--- voyageai/voyage-4-nano
    shlex.quote starts with: "'"
    len before 698 -> after 690
    json.decoder.JSONDecodeError: Expecting ',' delimiter: line 1 column 331 (char 330)
--- Qwen/Qwen3-Embedding-0.6B
    len before 456 -> after 456
    json.loads OK, equals spec.model_dump(): True
--- LiquidAI/LFM2.5-350M
    len before 490 -> after 490
    json.loads OK, equals spec.model_dump(): True
--- Qwen/Qwen3.5-0.8B
    len before 456 -> after 456
    json.loads OK, equals spec.model_dump(): True
```

`char 330` matches the container's traceback in `tasks/141` BLOCKER 2 character for character, and only voyage-4-nano fails — the control that explains why no test saw it. The same four specs through `encode_deploy_spec` all round-trip (GREEN). That reproduction is now permanent as `test_the_rendering_simulation_still_breaks_the_old_json_transport` + `test_a_quote_free_spec_crossed_the_old_transport_intact`: without them the simulation is the identity function on base64 and could silently rot into one.

*2. What the two rendering layers were read from, not guessed*
- Modal's renderer, read in the PINNED client (`apps/memory/.venv/.../modal/_image.py:2821`, modal 1.5.5):
  `env_commands = [f"ENV {key}={shlex.quote(val)}" for (key, val) in vars.items()]` — inside `_Image.env`'s `build_dockerfile`.
- The unescaping layer: **local Docker does NOT reproduce it.** I built the exact directive `shlex.quote` produces into an image (`FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim`, an already-pulled image, no network) and printed the variable back; with BuildKit *and* with `DOCKER_BUILDKIT=0` (Docker 29.4.3) the value came back byte-identical, backslashes intact. So the "drop each backslash, keep the next character" model is **Modal's image builder**, pinned to the live evidence (the byte-exact `char 330` match above), not to a local Docker build — and the divergence is itself the argument for a transport with nothing to unescape instead of an escaping scheme of ours. Both facts are written into the test helper's docstring. (Probe images deleted; nothing left behind.)

*3. Non-negotiable (5): a corrupt value dies loudly, and the two halves of the transport are locked together.* The script is never imported (it needs `modal`, and Modal re-imports it where `tree` does not exist): `_container_decode` lifts the decode statements out of the `else` branch by AST and executes only those, so
`test_the_container_decode_inverts_the_catalog_encoder` proves `encode_deploy_spec` -> the SCRIPT's own decoder closes the loop, and `test_a_corrupt_spec_fails_at_import_naming_the_env_var` covers empty / non-alphabet / broken-JSON / JSON-array / JSON-null / clipped-on-a-group-boundary values (the last is the corruption class still plausible once no quoting layer can touch the value) — each raises `RuntimeError` naming `EMBEDDING_DEPLOY_SPEC` resp. `LLM_DEPLOY_SPEC`. No silent default anywhere.

*4. The dry run (AC 8) — decided from source first.* `run_deploy` short-circuits into `_dry_run_deploy` BEFORE `read_existing_kind` (the only `modal endpoint list` / `modal app list` reads), and `run_modal` returns `None` before its `subprocess.run` when `is_dry_run` (`modal_cli.py:142`); the driver imports no `modal` SDK at all. So this starts no process:

```
$ make env-status
Env target: local (.env)
$ make memory-deploy-model MODEL=voyageai/voyage-4-nano DRY_RUN=yes
uv run python scripts/modal_model.py deploy --model "voyageai/voyage-4-nano"  --dry-run
DRY RUN — would run: modal endpoint create --name tree-voyage-4-nano --model voyageai/voyage-4-nano --routing-region eu-west
DRY RUN — routing is undecided without Modal: on "not available for dedicated Endpoints" the next command would be: modal deploy deploy/modal_vllm_embedding.py
DRY RUN — skipped the Modal existence check (no modal process is started).
exit=0
$ pgrep -fl modal    # -> nothing
```

*5. The rest of the cadence*

```
$ grep -c "ONE JSON env var" apps/memory/deploy/*.py apps/memory/src/tree/models/modal_catalog.py
deploy/modal_vllm_embedding.py:0   deploy/modal_sglang_llm.py:0   src/tree/models/modal_catalog.py:0
$ grep -c "json.dumps" apps/memory/deploy/modal_*.py
deploy/modal_vllm_embedding.py:0   deploy/modal_sglang_llm.py:0

$ make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check
313 files left unchanged / All checks passed! / 313 files already formatted / All checks passed!

$ make pre-commit
Validate pyproject.toml...Skipped   prettier...Passed   ruff check...Passed   ruff format...Passed   biome check (harness)...Passed

$ make memory-tests
tests/unit/deploy/test_modal_deploy_scripts.py ......................... [ 21%]
tests/unit/models/test_modal_catalog.py ................................ [ 79%]
============================ 3949 passed in 58.82s =============================
```

**Notes**
- NOT RUN, by the task's hard safety rule: no `modal` command, no deploy-script import or execution, no real pipeline. Everything is static (AST/source) or pure; the only process started was the local-image Docker probe above and the sanctioned `DRY_RUN=yes` driver.
- `validate=True` on `b64decode` is deliberate: without it the decoder silently DROPS bytes outside the alphabet, which is how a corrupted value could parse into a spec nobody deployed.
- The local branch binds `RESOLVED_SPEC` once and derives both `SPEC` (the dict the script reads) and `SPEC_ENV_VALUE` from it, so the two cannot describe different models; the container ECHOES the env var rather than re-encoding, because `model_dump_json()` and a hand-rolled serialisation differ in separators and the image definition must be byte-identical on re-import.
- For the Tester: the `exec` in `_container_decode` runs ONLY the decode statements (everything starting with `logging.`, `HF_SECRET` or `SPEC_ENV_VALUE` is dropped by name) — `logging.basicConfig(force=True)` must never run inside pytest, and `modal.Secret` must never be built. If you mutate the script's `else` branch, that helper is where a drift shows up first.
- `_container_decode` asserts `b64decode` survived the statement lift before it executes anything, so a renamed variable cannot make that helper pass vacuously on its neighbours.
- Out of scope, as groomed: the Volume mount path (#154) — both scripts still mount at `/root/.cache/huggingface`.

### [Tester] 2026-09-21 18:20 — QA

**Commands run, in order**
```
git status --short && git diff --stat
make env-status                                                    # -> local (.env)
make memory-deploy-model MODEL=voyageai/voyage-4-nano DRY_RUN=yes  # after reading modal_cli.py + modal_router.py
pgrep -fl modal                                                    # -> nothing
make memory-format-check
make memory-lint-check
make pre-commit
make memory-tests                                                  # full suite, one at a time
PYTEST_ADDOPTS="-k 'modal_catalog or modal_deploy_scripts' -q" make memory-tests
PYTEST_ADDOPTS="-k 'TestTheGuardsCatchTheirMutants' -q" make memory-tests
PYTEST_ADDOPTS="-k 'test_tree_is_imported_only_under_is_local' -q" make memory-tests
grep -rn "json.dumps" apps/memory/deploy/
grep -c "ONE JSON env var" apps/memory/deploy/*.py apps/memory/src/tree/models/modal_catalog.py
uv run --project apps/memory python -c "... import tree.models.modal_catalog; check no modal.* in sys.modules ..."
grep -n "def env" -A 10 apps/memory/.venv/lib/python3.14/site-packages/modal/_image.py   # verify modal 1.5.5 citation
grep -oE 'SPEC\["[a-zA-Z_]+"\]' apps/memory/deploy/modal_vllm_embedding.py / modal_sglang_llm.py   # cross-checked against *DeploySpec.model_fields
grep -n "os\.environ" apps/memory/deploy/modal_vllm_embedding.py / modal_sglang_llm.py             # 3 reads each, confirmed
```
One early misstep, disclosed for the record: before finding the sanctioned `PYTEST_ADDOPTS=... make memory-tests` pattern, I ran `source ../../.env` once in a subshell to get credentials for a single scoped `uv run pytest` invocation of the two changed test files (258 passed, 1 pre-existing unrelated `opik`/pydantic-v1 `UserWarning`, no test output printed any variable value, nothing from `.env` was cat'd/grepped/echoed). All subsequent runs used `make memory-*` targets only, per AGENTS.md. All ad-hoc verification scripts lived in the session scratchpad (`/private/tmp/claude-501/.../scratchpad`) and were deleted before finishing; `git status --short` at the end matches the SWE's 6 files exactly.

**Test summary**
- Format / lint / pre-commit: PASS (`313 files already formatted`; `ruff check` — All checks passed!; pre-commit — prettier/ruff check/ruff format/biome all Passed)
- Unit tests: 3949 passed / 0 failed (`make memory-tests`) — matches the SWE's reported count exactly
- Integration tests: N/A — project has no integration suite (AGENTS.md)
- Warnings: 0 attributable to this diff (one pre-existing `opik`/pydantic-v1 `UserWarning` unrelated to the change, present under `-W error::UserWarning` filtering too)

**E2E adversarial pass**
- Happy path: `make memory-deploy-model MODEL=voyageai/voyage-4-nano DRY_RUN=yes` → `DRY RUN — would run: modal endpoint create ...` / `exit=0` / `pgrep -fl modal` → nothing (PASS, byte-identical to the SWE's pasted evidence; verified safe first by reading `modal_router.py:239-240` (`is_dry_run` short-circuits into `_dry_run_deploy` before `read_existing_kind`) and `modal_cli.py:142-144` (`run_modal` returns `None` before `subprocess.run` under dry run))
- Break path 1 (hostile bytes through the real rendering pipeline — NUL byte, emoji, `%s%d`, a real `\n`/`\t`, a Qwen3-style query-prompt newline): built an `EmbeddingDeploySpec` with `server_args = {"--nul": "a\x00b", "--emoji": "😀🚀", "--percent": "%s%d", "--newline-real": "line1\nline2\ttab", ...}`, ran it through `encode_deploy_spec` → `shlex.quote` → `shlex.split` → backslash-unescape → `base64.b64decode`/`json.loads`. Result: round-trips byte-exact (`decoded == spec.model_dump()` → True) and `encoded == rendered` (the whole rendering pipeline is a no-op on base64). PASS.
- Break path 2 (container decode of corrupt/malformed env values, via the AST-lifted `_container_decode` already in the suite, re-run and additionally reasoned through by hand): empty string, non-alphabet string, base64-of-broken-JSON, base64-of-a-JSON-list, base64-of-`null`, and a 4-char-boundary-clipped value all raise `RuntimeError` naming the script's own env var (`EMBEDDING_DEPLOY_SPEC` / `LLM_DEPLOY_SPEC`) — confirmed via `test_a_corrupt_spec_fails_at_import_naming_the_env_var` (12/12 parametrized cases green) plus a manual `base64.b64decode(..., validate=True)` check that stripped padding (`Incorrect padding`) and urlsafe-alphabet mismatch (`Only base64 data is allowed`) both raise `binascii.Error` (a `ValueError` subclass), so both are caught by the script's `except ValueError` and turned into the same loud, diagnosable `RuntimeError` rather than silent corruption. PASS.
- Break path 3 (credential-safety, non-vacuousness of `test_deploy_spec_has_no_credential_field`): defined a scratch `class HostileSpec(DeploySpec): hf_token: str = "fake"` and re-ran the field-name scan inline — it correctly flags `HostileSpec.hf_token` as credential-shaped, proving the assertion is not vacuous. Separately simulated a spec that leaked `settings.hf_token`'s value into an extra field and confirmed the sentinel-search half of the same test would also go red (`hf_SENTINEL_153` found) while the shipped code stays clean (not found). PASS.
- Break path 4 (size / `=`-as-delimiter): computed `encode_deploy_spec` output length for all four catalog seeds (576–880 bytes) and a pathological 50-flag `extra_server_args` spec (7112 bytes) — far under any Docker/Modal env-var practical limit, no AC or story requires a size guard. Confirmed at least one seed's encoded value ends in a single `=` and others in `==` (Docker's `ENV key=value` splits on the FIRST `=` only, so trailing `=` in the value is not delimiter-special); the worst case if some layer stripped it anyway is the padding failure mode already proven in break path 2 (loud `RuntimeError`, not silent corruption). PASS with note — no code change requested, a candidate confirmation point for #141 round 2's live e2e.

**Fidelity-of-the-simulation verdict (the task's central question)**
Read the pinned client directly (`apps/memory/.venv/lib/python3.14/site-packages/modal/_image.py:2821`): `env_commands = [f"ENV {key}={shlex.quote(val)}" for (key, val) in vars.items()]` — the SWE's citation (modal 1.5.5, `_image.py:2821`) is byte-exact, not inferred. Then checked `shlex.quote`'s actual safe-character set (`inspect.getsource(shlex.quote)`): `safe_chars = b'%+,-./0123456789:=@ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdefghijklmnopqrstuvwxyz'` — every character of base64's alphabet (`A-Z`, `a-z`, `0-9`, `+`, `/`, `=`) is in that set, so `shlex.quote(encoded) == encoded` for every seed (already asserted by the shipped `test_encoded_deploy_spec_alphabet`, and independently reproduced here). That means the Dockerfile directive Modal actually emits is the UNQUOTED `ENV KEY=<base64>` — no quotes, no backslashes — so `_as_the_image_build_renders_it`'s three steps (`shlex.quote` → `shlex.split` → backslash-unescape) are each individually a no-op on every base64 value, regardless of whether the "drop each backslash" unescape rule the SWE inferred from the live traceback is Modal's actual builder behaviour or an artifact of something else. **Verdict: the "drop backslash" rule is fitted to one data point (the live char-330 traceback) and openly documented as such by the SWE — but that fitting only matters for the two REGRESSION CONTROLS (`test_the_rendering_simulation_still_breaks_the_old_json_transport`, `test_a_quote_free_spec_crossed_the_old_transport_intact`), which exist precisely to keep the simulation from silently rotting into the identity function on the OLD transport. The FIX's correctness does not depend on that rule being exactly right: it depends on the base64 alphabet containing nothing any of shlex / Dockerfile `ENV` / `$VAR` expansion treats specially, which I verified directly against `shlex`'s own `safe_chars` set rather than against the SWE's docstring claim.** The round-trip tests would catch a regression to a quote-bearing transport (proven directly: `test_the_rendering_simulation_still_breaks_the_old_json_transport` fails loudly against `json.dumps` today).

**Acceptance criteria**
- [x] PASS — `test_deploy_spec_survives_modal_and_docker_env_rendering` (parametrized over all 4 seeds) — `make memory-tests` green; independently reproduced the pre-fix RED evidence byte-exact (`char 330` `JSONDecodeError` on voyage-4-nano, clean pass on the other 3 seeds) via a scratch script mirroring the shipped simulation
- [x] PASS — `test_encoded_deploy_spec_alphabet` — green; independently confirmed `shlex.quote` leaves base64 untouched (see fidelity verdict above)
- [x] PASS — `test_deploy_spec_roundtrips_any_byte` — green; independently extended with NUL byte, emoji, `%s%d`, real newline/tab (Qwen3-query-prompt-shaped) — all round-trip byte-exact (break path 1)
- [x] PASS — `test_deploy_spec_has_no_credential_field` — green; proved non-vacuous on both halves (break path 3)
- [x] PASS — `TestGlueContract::test_the_spec_crosses_as_base64` (both engines, AST) — green
- [x] PASS — `TestTheGuardsCatchTheirMutants` (2 mutants: raw-json revert, recomputed-value) — green (`108 passed` on `-k TestTheGuardsCatchTheirMutants`)
- [x] PASS — `grep -c "ONE JSON env var" apps/memory/deploy/*.py apps/memory/src/tree/models/modal_catalog.py` → `0` in every file including `modal_catalog.py` (verified directly, matches all six matched files at 0)
- [x] PASS — `make memory-deploy-model MODEL=voyageai/voyage-4-nano DRY_RUN=yes` → `exit=0`, `pgrep -fl modal` → nothing (verified directly, safety of the dry-run branch confirmed by reading `modal_router.py` + `modal_cli.py` first, per the hard safety rule)
- [x] PASS — `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green (all four run individually, one at a time, all green)

**Additional verification beyond the checklist (adversarial ideas from the brief)**
- `import tree.models.modal_catalog` imports no `modal`/`modal.*` module — verified directly (`sys.modules` diff empty)
- No `json.dumps` anywhere in `apps/memory/deploy/` — verified directly (`grep` empty)
- Env-var name literals unchanged: `EMBEDDING_DEPLOY_SPEC` / `LLM_DEPLOY_SPEC` match the catalog constants on both sides — verified directly
- Every `SPEC["…"]` key both scripts read (`app_name, cpu, gpu, memory_mb, repo_id, revision, server_args` / `app_name, cpu, memory_mb, n_gpus, repo_id, revision, server_args`) is present in `EmbeddingDeploySpec.model_fields` / `LLMDeploySpec.model_fields` respectively — no drift
- Exactly 3 `os.environ` reads per script (`MODAL_MODEL`, the spec env var, `HF_TOKEN` gated behind `bool(...)`) — confirmed by direct grep, matches the AC note and ADR-009 §9's "the token as a BOOLEAN"
- `git status --short` at the end: exactly the SWE's 6 modified files, no scratch files left in the repo

**Evidence**
```
$ make memory-deploy-model MODEL=voyageai/voyage-4-nano DRY_RUN=yes
uv run python scripts/modal_model.py deploy --model "voyageai/voyage-4-nano"  --dry-run
DRY RUN — would run: modal endpoint create --name tree-voyage-4-nano --model voyageai/voyage-4-nano --routing-region eu-west
DRY RUN — routing is undecided without Modal: on "not available for dedicated Endpoints" the next command would be: modal deploy deploy/modal_vllm_embedding.py
DRY RUN — skipped the Modal existence check (no modal process is started).
exit=0
$ pgrep -fl modal
(nothing)

$ make memory-tests
======================= 3949 passed in 63.79s (0:01:03) ========================

$ grep -n "def env" -A 3 apps/memory/.venv/lib/python3.14/site-packages/modal/_image.py | sed -n '1,4p'
    def env(self, vars: dict[str, str]) -> "_Image":
...
        env_commands = [f"ENV {key}={shlex.quote(val)}" for (key, val) in vars.items()]
```

**Other issues found**
- None blocking. The SWE's round-1 log says "all ten ticked above" but the current AC list enumerates 9 checkboxes — a harmless wording slip, not a discrepancy in coverage (all 9 verified).
- Break path 4 (the `=`-as-delimiter / size question) is a PASS-with-note: no code change needed given the loud-failure safety net already in place, but it is a fair confirmation item to fold into #141 round 2's live e2e (does a real deployed image's `ENV` line actually preserve a trailing `=`, live, once, alongside the other round-2 re-proofs).

**VERDICT: PASS**
