---
id: 153-deploy-spec-base64-transport
status: pending
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

- [ ] `tests/unit/models/test_modal_catalog.py::test_deploy_spec_survives_modal_and_docker_env_rendering` — parametrized over BOTH seeds' specs (`build_deploy_spec("voyageai/voyage-4-nano")`, `build_llm_deploy_spec("LiquidAI/LFM2.5-350M")`): take the env value, render it the way Modal + the build do (`shlex.quote(value)`, undo the shell quoting with `shlex.split(...)[0]`, then drop every backslash keeping the next character: `re.sub(r"\\(.)", r"\1", …)`), decode it the way the container does, and assert it equals `spec.model_dump()`. **The SWE first writes this test against the OLD transport (`json.dumps(spec.model_dump())` + `json.loads`) and pastes in `## Log` that the voyage-4-nano case FAILS with `JSONDecodeError` at `char 330` and the LFM2.5 case passes** — the unit-level reproduction of the live crash — then switches it to `encode_deploy_spec`.
- [ ] `::test_encoded_deploy_spec_alphabet` — for both seeds `re.fullmatch(r"[A-Za-z0-9+/=]+", encode_deploy_spec(spec))`.
- [ ] `::test_deploy_spec_roundtrips_any_byte` — an `EmbeddingDeploySpec` whose `server_args` values are `'"'`, `"\\"`, `"$HOME"`, `"'"`, `` "`id`" ``, `"a\nb"`, `"é"` round-trips through the same rendering simulation unchanged.
- [ ] `::test_deploy_spec_has_no_credential_field` — no field name of `DeploySpec`, `EmbeddingDeploySpec`, `LLMDeploySpec` contains `token`, `secret`, `key` or `password` (case-insensitive); and with `settings.hf_token` monkeypatched to `SecretStr("hf_SENTINEL_153")`, the DECODED env value of both seeds does not contain `hf_SENTINEL_153`.
- [ ] `tests/unit/deploy/test_modal_deploy_scripts.py::TestGlueContract::test_the_spec_crosses_as_base64` (both engines, AST): the value stored under `DEPLOY_SPEC_ENV` in the image `.env({...})` is the bare name `SPEC_ENV_VALUE`; that name is assigned exactly twice — from a call to `encode_deploy_spec` inside the `modal.is_local()` branch and from `os.environ[…]` in the `else` branch; the `else` branch contains `base64.b64decode`; `json.dumps` appears nowhere in the script.
- [ ] `TestTheGuardsCatchTheirMutants`: a mutant that reverts the layer to `DEPLOY_SPEC_ENV: json.dumps(SPEC)` FAILS `test_the_spec_crosses_as_base64`; `test_the_shipped_script_passes_every_guard` stays green.
- [ ] `grep -c "ONE JSON env var" apps/memory/deploy/*.py apps/memory/src/tree/models/modal_catalog.py` -> 0 in every file.
- [ ] `make memory-deploy-model MODEL=voyageai/voyage-4-nano DRY_RUN=yes` exits 0 and starts no `modal` process (unchanged behaviour; paste the line).
- [ ] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green.

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
