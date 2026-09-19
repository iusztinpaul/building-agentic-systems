---
id: 139-modal-vllm-and-sglang-deploy-scripts
status: pending
feature: voyage-4-and-modal-embedding-catalog
---

# Two Modal deploy scripts (vLLM rewritten, SGLang new) on `@app.server` + proxy tokens, driven by `MODEL=<repo_id>`

Tags: `modal`, `deploy`, `scripts`, `infra`
Depends on: #138
Blocks: #140, #141
Implements: ADR-009 — Decision 2 (two engines), Decision 3 (one app per model) and Decision 4 (Proxy token auth, server side)

## Scope

**1. Dependency** — `apps/memory/pyproject.toml` `local-models` extra: `modal>=1.5.5` (today
`>=1.3.5`, lock pins 1.3.5; `@app.server` and `modal.Server` need the newer client); `uv lock`.
`autoinference-utils` is installed only INSIDE the Modal image — not a project dependency.

**2. Settings (add only)** — `src/tree/config/settings.py`: `modal_proxy_token_id: SecretStr = SecretStr("")`,
`modal_proxy_token_secret: SecretStr = SecretStr("")`; `.env.example` gains
`MODAL_PROXY_TOKEN_ID=wk-your-proxy-token-id` / `MODAL_PROXY_TOKEN_SECRET=ws-your-proxy-token-secret`
under the Modal heading with a one-line pointer to Modal → Settings → Proxy Auth Tokens;
`tests/unit/config/test_settings_credentials_only.py` field set gains both.
(`MODAL_EMBEDDING_API_KEY` is retired in #140, with the client that still reads it.)
Helper `modal_proxy_bearer() -> str` in `tree/models/modal_catalog.py`: `f"{id}.{secret}"`, raising
`ModelError("Modal proxy token is required. Set MODAL_PROXY_TOKEN_ID and MODAL_PROXY_TOKEN_SECRET.")`
when either is empty. (Verified: Modal accepts `Authorization: Bearer <id>.<secret>` —
modal.com/docs/guide/webhook-proxy-auth.)

**3. Scripts** — `deploy/modal_vllm_embedding.py` (rewritten) and `deploy/modal_sglang_embedding.py`
(new), SAME shape, GLUE ONLY:
- CONSTRAINT: Modal re-imports the script INSIDE the container, where `tree` is NOT installed.
  At module level: `if modal.is_local():` → `init_logger()`, `spec = build_deploy_spec(os.environ["EMBEDDING_MODEL"])`,
  `SPEC = spec.model_dump()`; `else:` → `SPEC = json.loads(os.environ["EMBEDDING_DEPLOY_SPEC"])` and
  `logging.basicConfig(level=logging.INFO)`. No top-level `import tree…` outside the local branch.
  The script refuses a catalog entry of the other engine (`ModelError` naming the right script).
- Image (template): `modal.Image.from_registry("nvidia/cuda:13.0.2-devel-ubuntu22.04", add_python="3.12").entrypoint([])`
  `.uv_pip_install(f"vllm=={engine_version}", f"autoinference-utils=={aiu_version}", "httpx", "huggingface-hub")`
  `.env({"HF_XET_HIGH_PERFORMANCE": "1", "EMBEDDING_DEPLOY_SPEC": spec_json})`. SGLang script installs
  `sglang` at the catalog version instead of vLLM — SWE must verify the install spec
  (`sglang[all]==0.5.20` vs plain) and BOTH engines' wheels against the CUDA 13.0.2 base image at
  e2e; if a wheel needs another CUDA tag, change the base image in that ONE script and log why.
- `app = modal.App(SPEC["app_name"])` — ONE app per model.
- `@app.server(image=…, gpu=SPEC["gpu"], cpu=SPEC["cpu"], memory=SPEC["memory_mb"], min_containers=0, scaledown_window=5 * MINUTES, port=8000, routing_region="eu-west", unauthenticated=False, exit_grace_period=25, startup_timeout=20 * MINUTES, target_concurrency=16, volumes={"/root/.cache/huggingface": modal.Volume.from_name("huggingface-cache", create_if_missing=True)})`
  on `class EmbeddingServer` (name == `EMBEDDING_SERVER_NAME`). No pre-baked
  `/flash-endpoint-model` volume.
- `@modal.enter()`: vLLM → `VLLMEndpoint(model=SPEC["repo_id"], worker_port=8000, extra_server_args=SPEC["server_args"], health_timeout=…, health_poll_interval=5.0)`;
  SGLang → `SGLangEndpoint(model_path=SPEC["repo_id"], …)` (NOTE `model_path=`, not `model=` —
  verified in the 0.2.6 source); `.start()`; then
  `validate_embeddings_endpoint(port=8000, payload={"model": SPEC["repo_id"], "input": [<two probe strings>], "encoding_format": "float"}, request_timeout=60.0)`.
  `@modal.exit()` → `.stop()`. The engine no longer takes `--api-key`; no `modal.Secret`.
- `@app.local_entrypoint()` smoke test (async): URL via the deployed server (SWE must verify the
  exact call on Modal 1.5.x with the `tech-docs` skill: `modal.Server.from_name(app_name, "EmbeddingServer").get_url()`
  and its async form); sends `Authorization: Bearer {modal_proxy_bearer()}` on `GET /health`
  (timeout 20 min — cold start) and `POST /v1/embeddings` with 2 inputs; asserts HTTP 200, 2 items,
  `len(embedding) == SPEC["native_dimensions"]`; then asserts the same `GET /health` WITHOUT the
  header answers 401. All output through `logger` — the template's `print` calls must not survive.
- Every function typed, including `-> None`.

**4. Driver glue + Make** — `scripts/modal_embedding_model.py` (`init_logger()`, `click` group
`deploy | test | stop`, each `--model`): resolves the entry (unknown id → the catalog error, exit 2),
builds the argv with ONE function in `tree/models/modal_catalog.py`
(`modal_cli_command(action, entry) -> list[str]`: `["modal", "deploy", <script>]` /
`["modal", "run", <script>]` / `["modal", "app", "stop", <app_name>]`) and runs it with
`EMBEDDING_MODEL=<repo_id>` in the child env. `apps/memory/Makefile` replaces the three old targets:
- `deploy-embedding-model: # Deploy ONE Embedding catalog model to Modal as its own app (ep-<model>); the script (vLLM or SGLang) is picked from the entry's engine. Requires MODEL=<repo_id>, e.g. MODEL=voyageai/voyage-4-nano.`
- `deploy-embedding-model-test: # Smoke-test a deployed model through proxy-token auth (health + /v1/embeddings, asserts native dimensions). Requires MODEL=<repo_id>.`
- `deploy-embedding-model-stop: # Stop the Modal app of one catalog model. Requires MODEL=<repo_id>.`
  Each prints a `USAGE:` line and exits 1 when `MODEL` is empty. The
  `modal secret create vllm-embedding-api-key …` bootstrap is deleted.

Write tests with `/squid-testing-python`; NO test touches the network or imports the deploy scripts
under a real Modal client.

## Out of scope
- The `ModalEmbeddingModel` client and retiring `MODAL_EMBEDDING_API_KEY` (#140): until #140 lands
  the old client points at the deleted `vllm-embedding-models` app — expected inside this feature
  branch; `provider: modal` is not the default.
- The real deployment (#141). Snapshot / pre-baked weight volumes. GPU autoscaling knobs in YAML.
- Vendoring SGLang PR #18436.

## Acceptance Criteria

- [ ] `uv.lock` resolves `modal` ≥ `1.5.5`; `uv --directory apps/memory run python -c "import modal; modal.Server; modal.App('x').server"` exits 0.
- [ ] `Settings` exposes `modal_proxy_token_id` / `modal_proxy_token_secret` (both `SecretStr`, default empty); the locked-down field-set test lists them — `tests/unit/config/test_settings_credentials_only.py`.
- [ ] `modal_proxy_bearer()` → `"wk-1.ws-2"` for id `wk-1` / secret `ws-2`; raises `ModelError` naming BOTH env vars when either is empty — `tests/unit/models/test_modal_catalog.py::TestProxyBearer`.
- [ ] `modal_cli_command("deploy", voyage_entry) == ["modal", "deploy", "deploy/modal_vllm_embedding.py"]`; `("test", qwen_entry)` → `["modal", "run", "deploy/modal_sglang_embedding.py"]`; `("stop", qwen_entry)` → `["modal", "app", "stop", "ep-qwen3-embedding-0-6b"]` — `::TestModalCliCommand`.
- [ ] Driver script: `deploy --model voyageai/voyage-4-nano` runs the vLLM argv with `EMBEDDING_MODEL=voyageai/voyage-4-nano` in the child env (`subprocess.run` patched); `--model BAAI/bge-m3` exits 2, runs nothing, and logs both catalog ids — `tests/unit/scripts/test_modal_embedding_model_script.py`.
- [ ] Static (AST/text) checks on BOTH deploy scripts — `tests/unit/deploy/test_modal_embedding_scripts.py`: no `print(` call; `init_logger()` is called; no module-level `import tree`/`from tree` outside an `if modal.is_local():` block; contains `unauthenticated=False`, `routing_region="eu-west"`, `scaledown_window=5 * MINUTES`, `target_concurrency=16`, `min_containers=0`, `exit_grace_period=25`; does not contain `MODAL_EMBEDDING_API_KEY`, `--api-key` or `Secret.from_name`; the vLLM script constructs `VLLMEndpoint(model=`, the SGLang script `SGLangEndpoint(model_path=`; every `def` has a return annotation.
- [ ] `make -n memory-deploy-embedding-model` (no MODEL) prints a `USAGE: make memory-deploy-embedding-model MODEL=<repo_id>` line and exits 1; same for `-test` and `-stop`. `grep -c "vllm-embedding-api-key" apps/memory/Makefile` → 0.
- [ ] `.env.example` contains `MODAL_PROXY_TOKEN_ID` and `MODAL_PROXY_TOKEN_SECRET`.
- [ ] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green.
- [ ] [HUMAN] none here — the live deploy is #141.

## User Stories

### Story: Operator deploys voyage-4-nano
1. `make memory-deploy-embedding-model MODEL=voyageai/voyage-4-nano`
2. The log shows `modal deploy deploy/modal_vllm_embedding.py` and Modal reports the app `ep-voyage-4-nano` with one server `EmbeddingServer`.
3. `make memory-deploy-embedding-model-test MODEL=voyageai/voyage-4-nano` logs `health 200`, `2 embeddings, 1024 dims`, `unauthenticated health → 401`, `Smoke test passed`.

### Story: Operator deploys an SGLang model
1. `make memory-deploy-embedding-model MODEL=Qwen/Qwen3-Embedding-0.6B`
2. The driver picks `deploy/modal_sglang_embedding.py`; the app is `ep-qwen3-embedding-0-6b`.

### Story: Operator mistypes MODEL
1. `make memory-deploy-embedding-model MODEL=qwen3`
2. `Unknown Modal embedding model 'qwen3'. Embedding catalog ids: Qwen/Qwen3-Embedding-0.6B, voyageai/voyage-4-nano. …`; exit 2; nothing is deployed.

### Story: Operator forgets MODEL
1. `make memory-deploy-embedding-model-stop`
2. `USAGE: make memory-deploy-embedding-model-stop MODEL=<repo_id>`; exit 1.

### Story: Someone without a proxy token hits the URL
1. `curl https://<workspace>--ep-voyage-4-nano-embeddingserver.modal.run/health`
2. `401 — modal-http: missing credentials for proxy authorization`.

---

Blocked by: #138

## Log

### [PA] 2026-09-19 17:43 — Grooming

**Summary**
Both engines deploy any catalog model as its own proxy-token-protected Modal app; the scripts are glue over the #138 helpers.

**Key decisions**
- A tiny driver script picks the engine script, because Make cannot read YAML; the argv builder lives in `src/tree/`.
- The resolved spec travels into the container as ONE JSON env var baked into the image — `tree` is not importable there.
- The smoke test also asserts the 401 path: "auth is on" is the thing a deploy can silently get wrong.
- Settings gains the two token fields here; the old key is removed in #140 together with its last reader.

**Dependencies**
- #138 — catalog entry, `build_deploy_spec`, `EMBEDDING_SERVER_NAME`.

**User stories**
- 5 stories: vLLM deploy + test, SGLang deploy, unknown model, missing MODEL, unauthenticated request.

Ready for implementation.
