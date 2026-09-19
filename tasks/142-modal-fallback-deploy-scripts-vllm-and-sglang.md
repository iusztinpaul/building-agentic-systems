---
id: 142-modal-fallback-deploy-scripts-vllm-and-sglang
status: pending
feature: voyage-4-and-modal-embedding-catalog
---

# Fallback Serving paths: two Modal deploy scripts (vLLM, SGLang) on `@app.server` + proxy tokens, for models a Dedicated endpoint cannot serve

Tags: `modal`, `deploy`, `infra`
Depends on: #138, #139
Blocks: #141
Implements: ADR-009 — Decision 2 (the `sglang` / `vllm` fallback Serving paths) and Decision 3 (deploy spec crossing into the container)

## Scope

**ORDER: numbered after #141 but MUST RUN BEFORE IT (execution order 138 -> 139 -> 140 -> 142 -> 141).
Independent of #140.**

These scripts are the EJECT PATH Modal itself documents ("Open the Source view to inspect its
generated serve.py. You can copy and adapt that code into your own Modal App when you need full
control" — modal.com/docs/guide/dedicated-endpoints.md): the same shape as Modal's generated
`serve.py` (`class Server`, `VLLMEndpoint` from `autoinference-utils`), parameterised by the
Embedding catalog. They exist for models the managed recipe cannot serve — custom architectures via
`--hf-overrides`, a pooler config, `--trust-remote-code`, a pinned engine version.

`deploy/modal_vllm_embedding.py` and `deploy/modal_sglang_embedding.py` (both NEW — #139 deleted the
old single-model script), SAME shape, GLUE ONLY, SERVER ONLY (no `local_entrypoint`: the smoke test
is #139's shared `smoke_test`, run by the driver):
- CONSTRAINT: Modal re-imports the script INSIDE the container, where `tree` is NOT installed.
  At module level: `if modal.is_local():` -> `init_logger()`,
  `spec = build_deploy_spec(os.environ["EMBEDDING_MODEL"], "vllm")` (the SGLang script passes
  `"sglang"` — the engine is the SCRIPT's, never `entry.serving`, so `SERVING=` overrides work),
  `SPEC = spec.model_dump()`; `else:` -> `SPEC = json.loads(os.environ["EMBEDDING_DEPLOY_SPEC"])` and
  `logging.basicConfig(level=logging.INFO)`. No top-level `import tree…` outside the local branch.
- Image (template): `modal.Image.from_registry("nvidia/cuda:13.0.2-devel-ubuntu22.04", add_python="3.12").entrypoint([])`
  `.uv_pip_install(f"vllm=={engine_version}", f"autoinference-utils=={aiu_version}", "httpx", "huggingface-hub")`
  `.env({"HF_XET_HIGH_PERFORMANCE": "1", "EMBEDDING_DEPLOY_SPEC": spec_json})`. The SGLang script installs
  `sglang` at the catalog version instead of vLLM — SWE must verify the install spec
  (`sglang[all]==0.5.20` vs plain) and BOTH engines' wheels against the CUDA 13.0.2 base image at
  e2e (#141); if a wheel needs another CUDA tag, change the base image in that ONE script and log why.
  `autoinference-utils` is installed only INSIDE the image — not a project dependency.
- `app = modal.App(SPEC["app_name"])` — ONE app per model, the SAME name a Dedicated endpoint of that
  model has (`ep-<endpoint_name>`): a model is served by exactly one Serving path at a time, so stop
  the endpoint before deploying a fallback for the same model.
- `@app.server(image=…, gpu=SPEC["gpu"], cpu=SPEC["cpu"], memory=SPEC["memory_mb"], min_containers=0, scaledown_window=5 * MINUTES, port=8000, routing_region="eu-west", unauthenticated=False, exit_grace_period=25, startup_timeout=20 * MINUTES, target_concurrency=16, volumes={"/root/.cache/huggingface": modal.Volume.from_name("huggingface-cache", create_if_missing=True)})`
  on `class Server` (name == `EMBEDDING_SERVER_NAME`, the class name in Modal's generated `serve.py`).
  No pre-baked `/flash-endpoint-model` volume.
- `@modal.enter()`: vLLM -> `VLLMEndpoint(model=SPEC["repo_id"], worker_port=8000, extra_server_args=SPEC["server_args"], health_timeout=…, health_poll_interval=5.0)`;
  SGLang -> `SGLangEndpoint(model_path=SPEC["repo_id"], …)` (NOTE `model_path=`, not `model=` —
  verified in the 0.2.6 source); `.start()`; then
  `validate_embeddings_endpoint(port=8000, payload={"model": SPEC["repo_id"], "input": [<two probe strings>], "encoding_format": "float"}, request_timeout=60.0)`.
  `@modal.exit()` -> `.stop()`. No engine `--api-key`; no `modal.Secret`.
- All output through `logger`; every function typed, including `-> None`.

README "Modal embedding deployment": ONE paragraph "Fallback scripts" naming both files, when to use
them (the ladder from #140's section) and that they are the adapted Source-view `serve.py`; the
project-tree comment `deploy/  # Modal deployments (vLLM embedding)` -> `# Modal fallback deploy scripts (vLLM, SGLang), Prefect, Atlas`.

Write tests with `/squid-testing-python`; NO test touches the network or imports the deploy scripts
under a real Modal client.

## Out of scope
- The driver, Make targets, `modal_cli_command`, smoke test (#139 — already route `vllm` / `sglang`
  to these paths). The real deployment (#141).
- Snapshot / pre-baked weight volumes. GPU autoscaling knobs in YAML. Vendoring SGLang PR #18436.
- A shared base module for the two scripts (ADR-009: two boring copies until a third engine arrives).

## Acceptance Criteria

- [ ] Static (AST/text) checks on BOTH scripts — `tests/unit/deploy/test_modal_embedding_scripts.py`: no `print(` call; `init_logger()` is called; no module-level `import tree`/`from tree` outside an `if modal.is_local():` block; a class named `Server` decorated with `app.server`; contains `unauthenticated=False`, `routing_region="eu-west"`, `scaledown_window=5 * MINUTES`, `target_concurrency=16`, `min_containers=0`, `exit_grace_period=25`; does NOT contain `local_entrypoint`, `MODAL_EMBEDDING_API_KEY`, `--api-key` or `Secret.from_name`; the vLLM script contains `build_deploy_spec(os.environ["EMBEDDING_MODEL"], "vllm")` and constructs `VLLMEndpoint(model=`, the SGLang script `build_deploy_spec(os.environ["EMBEDDING_MODEL"], "sglang")` and `SGLangEndpoint(model_path=`; every `def` has a return annotation.
- [ ] Both files exist where `modal_cli_command` points: `::test_script_paths_match_cli_command` asserts `modal_cli_command("deploy", e, "vllm")[-1]` and `(…, "sglang")[-1]` are existing files relative to `apps/memory`.
- [ ] Driver, real paths: `deploy --model voyageai/voyage-4-nano` (catalog `serving: vllm`) runs `["modal", "deploy", "deploy/modal_vllm_embedding.py"]` with `EMBEDDING_MODEL=voyageai/voyage-4-nano`; `deploy --model Qwen/Qwen3-Embedding-0.6B --serving sglang` runs the SGLang script with `EMBEDDING_MODEL=Qwen/Qwen3-Embedding-0.6B` — `tests/unit/scripts/test_modal_embedding_model_script.py::TestFallbackScripts` (`subprocess.run` patched).
- [ ] `uv --directory apps/memory run python -c "import ast,sys; [ast.parse(open(p).read()) for p in ('deploy/modal_vllm_embedding.py','deploy/modal_sglang_embedding.py')]"` exits 0.
- [ ] README Modal section names `deploy/modal_vllm_embedding.py` and `deploy/modal_sglang_embedding.py` and the words `Source view`; `grep -c "vLLM embedding)" apps/memory/README.md` -> 0.
- [ ] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green.
- [ ] [HUMAN] none here — the live deploy of both scripts is #141.

## User Stories

### Story: Operator serves a model the managed recipe cannot
1. `voyageai/voyage-4-nano` needs `--hf-overrides` for `VoyageQwen3BidirectionalEmbedModel`; its entry says `serving: vllm`.
2. `make memory-deploy-embedding-model MODEL=voyageai/voyage-4-nano` -> the log shows `modal deploy deploy/modal_vllm_embedding.py`; Modal reports the app `ep-voyage-4-nano` with one server `Server`.
3. `make memory-deploy-embedding-model-test MODEL=voyageai/voyage-4-nano` -> `3 embeddings, 1024 dims`, `unauthenticated health -> 401`, `Smoke test passed`.

### Story: Operator walks the ladder on a model whose endpoint failed
1. `make memory-deploy-embedding-model-stop MODEL=Qwen/Qwen3-Embedding-0.6B` (the endpoint), then `make memory-deploy-embedding-model MODEL=Qwen/Qwen3-Embedding-0.6B SERVING=sglang`.
2. The driver runs `deploy/modal_sglang_embedding.py`; the server starts with `--is-embedding` (builder-owned) on the default `A10` / 4 CPU / 16384 MB — the entry never listed them.
3. The operator writes `serving: sglang` into the YAML and stops with `SERVING=sglang` when done.

### Story: Someone without a proxy token hits a fallback server
1. `curl https://<workspace>--ep-voyage-4-nano-server.modal.run/health`
2. `401 — modal-http: missing credentials for proxy authorization`.

### Story: Engineer needs an engine flag
1. Adds `"--max-num-seqs": "64"` to the entry's `extra_server_args`, redeploys with the same command.
2. No script changed — the flag travelled in `EMBEDDING_DEPLOY_SPEC`.

---

Blocked by: #138, #139

## Log

### [PA] 2026-09-19 18:23 — Grooming (new task from the plan edit: Dedicated endpoints first)

**Summary**
The two engine scripts from the approved #139, re-scoped as FALLBACK Serving paths behind the Dedicated endpoint, with the driver/smoke test/Make targets left in #139.

**Key decisions**
- Split out of #139 so each task stays atomic: #139 ships the preferred zero-code path end to end; this task adds the eject path. Numbered 142 (next free) but runs before #141 — stated in both files.
- `class Server` and app `ep-<endpoint_name>`: identical to Modal's generated `serve.py`, so the client's single URL lookup works on all three paths.
- No `local_entrypoint`: one shared smoke test (#139) covers every path.
- Each script passes ITS engine to `build_deploy_spec`, so `SERVING=sglang` on an `endpoint` entry works without touching YAML; the engine baseline flag comes from the builder (#138).
- Kept both scripts by the owner's earlier decision even though no seed uses SGLang; #141 proves SGLang live through the override so it is not dead code.

**Dependencies**
- #138 — `build_deploy_spec(model, engine)`, `DEPLOY_SPEC_ENV`, `EMBEDDING_SERVER_NAME`. #139 — `modal>=1.5.5` (`@app.server`), the driver and `modal_cli_command` paths.

**User stories**
- 4 stories: vLLM fallback deploy + test, ladder to SGLang, unauthenticated request, extra engine flag.

Ready for implementation.
