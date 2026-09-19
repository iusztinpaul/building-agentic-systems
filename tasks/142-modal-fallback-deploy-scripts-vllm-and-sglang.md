---
id: 142-modal-fallback-deploy-scripts-vllm-and-sglang
status: pending
feature: voyage-4-and-modal-embedding-catalog
---

# Fallback Serving paths: two Modal deploy scripts (vLLM, SGLang) on `@app.server` + proxy tokens, for models a Dedicated endpoint cannot serve

Tags: `modal`, `deploy`, `infra`
Depends on: #138, #139
Blocks: #141
Implements: ADR-009 — Decision 2 (the `sglang` / `vllm` fallback Serving paths), Decision 3 (deploy spec crossing into the container) and Decision 9 (optional `HF_TOKEN`, fallback side: a locally built Modal Secret)

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
  `SPEC = spec.model_dump()`, `HF_SECRET = modal.Secret.from_dict(hf_token_env())`;
  `else:` -> `SPEC = json.loads(os.environ["EMBEDDING_DEPLOY_SPEC"])`,
  `HF_SECRET = modal.Secret.from_dict({})` and `logging.basicConfig(level=logging.INFO)`.
  No top-level `import tree…` outside the local branch.
- Hugging Face token (OPTIONAL, ADR-009 §9): new pure helper in `tree/models/modal_catalog.py`,
  `hf_token_env() -> dict[str, str]` -> `{"HF_TOKEN": <settings.hf_token value>}` when the setting
  (#139) is non-empty, else `{}`. The server gets `secrets=[HF_SECRET]` ALWAYS (one element; an empty
  Secret when there is no token) — this is exactly the local/remote shape Modal's secrets guide
  documents for "send Secrets from your local development machine"
  (`if modal.is_local(): Secret.from_dict({...}) else: Secret.from_dict({})`, modal.com/docs/guide/secrets.md),
  so the list has the same length on both sides and nothing is conditional remotely.
  The token is NEVER put in the image `.env(...)` next to `EMBEDDING_DEPLOY_SPEC` (image layers are
  cached and inspectable) and never logged. SWE must verify with the `tech-docs` skill on the pinned
  client: (a) `@app.server(...)` accepts `secrets=`; (b) after a deploy with a token, the container
  really sees `HF_TOKEN` although the in-container re-import builds `Secret.from_dict({})`;
  (c) `VLLMEndpoint` / `SGLangEndpoint` start the engine with the parent process env, so
  `huggingface_hub` inside the engine reads `HF_TOKEN` (read the `autoinference-utils` source; if it
  builds a clean env, pass the variable through its documented env parameter and log the finding).
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
- `@app.server(image=…, gpu=SPEC["gpu"], cpu=SPEC["cpu"], memory=SPEC["memory_mb"], min_containers=0, scaledown_window=5 * MINUTES, port=8000, routing_region="eu-west", unauthenticated=False, exit_grace_period=25, startup_timeout=20 * MINUTES, target_concurrency=16, secrets=[HF_SECRET], volumes={"/root/.cache/huggingface": modal.Volume.from_name("huggingface-cache", create_if_missing=True)})`
  on `class Server` (name == `EMBEDDING_SERVER_NAME`, the class name in Modal's generated `serve.py`).
  No pre-baked `/flash-endpoint-model` volume.
- `@modal.enter()`: FIRST line logs the boolean only — `logger.info("HF_TOKEN set in container: %s", bool(os.environ.get("HF_TOKEN")))`
  (the one observable proof the Secret arrived; the value is never logged); then
  vLLM -> `VLLMEndpoint(model=SPEC["repo_id"], worker_port=8000, extra_server_args=SPEC["server_args"], health_timeout=…, health_poll_interval=5.0)`;
  SGLang -> `SGLangEndpoint(model_path=SPEC["repo_id"], …)` (NOTE `model_path=`, not `model=` —
  verified in the 0.2.6 source); `.start()`; then
  `validate_embeddings_endpoint(port=8000, payload={"model": SPEC["repo_id"], "input": [<two probe strings>], "encoding_format": "float"}, request_timeout=60.0)`.
  `@modal.exit()` -> `.stop()`. No engine `--api-key`; no `Secret.from_name` (nothing to bootstrap in
  the workspace); the ONLY Modal Secret is `HF_SECRET`, whose only possible key is `HF_TOKEN`.
- All output through `logger`; every function typed, including `-> None`.

README "Modal embedding deployment": ONE paragraph "Fallback scripts" naming both files, when to use
them (the ladder from #140's section) and that they are the adapted Source-view `serve.py`; ONE
sentence on the token — `HF_TOKEN` in `.env` is optional and only for private or gated repos: a
Dedicated endpoint gets it as `--custom-hf-token` (custom weights only, shown as `***` in logs), a
fallback script as a Modal Secret built at deploy time, so a gated BASE model is served through a
fallback script; ONE troubleshooting line — "`401` / `403` / `GatedRepoError` from `huggingface.co`
in `modal app logs ep-<endpoint_name>` -> accept the model's licence on its Hub page, set `HF_TOKEN`
in `.env`, deploy again"; the project-tree comment
`deploy/  # Modal deployments (vLLM embedding)` -> `# Modal fallback deploy scripts (vLLM, SGLang), Prefect, Atlas`.

Write tests with `/squid-testing-python`; NO test touches the network or imports the deploy scripts
under a real Modal client.

## Out of scope
- The driver, Make targets, `modal_cli_command`, `hf_token_args` / `redact_argv`, the `hf_token`
  setting, the missing-token hint, smoke test (#139 — already route `vllm` / `sglang` to these paths
  and already log the hint when a deploy or test fails without a token). The real deployment (#141).
- A named, persistent Modal Secret (`Secret.from_name`) and any bootstrap target for it; a
  pre-flight Hub call; a per-model token.
- Snapshot / pre-baked weight volumes. GPU autoscaling knobs in YAML. Vendoring SGLang PR #18436.
- A shared base module for the two scripts (ADR-009: two boring copies until a third engine arrives).

## Acceptance Criteria

- [ ] Static (AST/text) checks on BOTH scripts — `tests/unit/deploy/test_modal_embedding_scripts.py`: no `print(` call; `init_logger()` is called; no module-level `import tree`/`from tree` outside an `if modal.is_local():` block; a class named `Server` decorated with `app.server`; contains `unauthenticated=False`, `routing_region="eu-west"`, `scaledown_window=5 * MINUTES`, `target_concurrency=16`, `min_containers=0`, `exit_grace_period=25`; does NOT contain `local_entrypoint`, `MODAL_EMBEDDING_API_KEY`, `--api-key` or `Secret.from_name`; the vLLM script contains `build_deploy_spec(os.environ["EMBEDDING_MODEL"], "vllm")` and constructs `VLLMEndpoint(model=`, the SGLang script `build_deploy_spec(os.environ["EMBEDDING_MODEL"], "sglang")` and `SGLangEndpoint(model_path=`; every `def` has a return annotation.
- [ ] Static, secrets — same file, `::test_only_the_hf_token_secret` (AST, both scripts): there are exactly 2 `Secret.from_dict(...)` calls, one whose single argument is the call `hf_token_env()` (inside the `if modal.is_local():` branch) and one whose single argument is the empty dict literal `{}` (inside the `else:` branch); the `app.server(...)` decorator has the keyword `secrets=[HF_SECRET]`; the dict literal passed to the image's `.env(...)` call has no key `HF_TOKEN`; the string `HF_TOKEN` occurs in the script only inside `os.environ.get("HF_TOKEN")` wrapped by `bool(...)`.
- [ ] `hf_token_env()` returns `{"HF_TOKEN": "hf_secret123"}` with `settings.hf_token` patched to `hf_secret123` and `{}` when it is empty — `tests/unit/models/test_modal_catalog.py::TestHfTokenEnv`; `python -c "import sys, tree.models.modal_catalog; assert 'modal' not in sys.modules"` still exits 0.
- [ ] Both files exist where `modal_cli_command` points: `::test_script_paths_match_cli_command` asserts `modal_cli_command("deploy", e, "vllm")[-1]` and `(…, "sglang")[-1]` are existing files relative to `apps/memory`.
- [ ] Driver, real paths: `deploy --model voyageai/voyage-4-nano` (catalog `serving: vllm`) runs `["modal", "deploy", "deploy/modal_vllm_embedding.py"]` with `EMBEDDING_MODEL=voyageai/voyage-4-nano`; `deploy --model Qwen/Qwen3-Embedding-0.6B --serving sglang` runs the SGLang script with `EMBEDDING_MODEL=Qwen/Qwen3-Embedding-0.6B`; with `settings.hf_token` patched to `hf_secret123` both argvs are UNCHANGED (no `--custom-hf-token` on a fallback path) and `hf_secret123` occurs nowhere in the captured log — `tests/unit/scripts/test_modal_embedding_model_script.py::TestFallbackScripts` (`subprocess.run` patched).
- [ ] `uv --directory apps/memory run python -c "import ast,sys; [ast.parse(open(p).read()) for p in ('deploy/modal_vllm_embedding.py','deploy/modal_sglang_embedding.py')]"` exits 0.
- [ ] README Modal section names `deploy/modal_vllm_embedding.py` and `deploy/modal_sglang_embedding.py` and the words `Source view`, and contains `HF_TOKEN`, `gated` and `GatedRepoError`; `grep -c "vLLM embedding)" apps/memory/README.md` -> 0.
- [ ] `## Log` answers the three "SWE must verify" items of the token bullet — (a) `secrets=` on `@app.server`, (b) local-vs-container `Secret.from_dict`, (c) engine subprocess env — each with its source (doc URL or file:line in `autoinference-utils`).
- [ ] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green.
- [ ] [HUMAN] none here — the live deploy of both scripts is #141.

## User Stories

### Story: Operator serves a model the managed recipe cannot
1. `voyageai/voyage-4-nano` needs `--hf-overrides` for `VoyageQwen3BidirectionalEmbedModel`; its entry says `serving: vllm`.
2. `make memory-deploy-embedding-model MODEL=voyageai/voyage-4-nano` -> the log shows `modal deploy deploy/modal_vllm_embedding.py`; Modal reports the app `ep-voyage-4-nano` with one server `Server`.
3. `make memory-deploy-embedding-model-test MODEL=voyageai/voyage-4-nano` -> `3 embeddings, 1024 dims`, `unauthenticated health -> 401`, `Smoke test passed`.

### Story: Operator serves a GATED model through a fallback script
1. Accepts the licence of `google/embeddinggemma-300m` on its Hub page, adds `HF_TOKEN=hf_…` to `.env` and an entry with `serving: sglang` to the Embedding catalog.
2. `make memory-deploy-embedding-model MODEL=google/embeddinggemma-300m` -> the log shows `modal deploy deploy/modal_sglang_embedding.py` and no token anywhere.
3. `modal app logs ep-embeddinggemma-300m` shows `HF_TOKEN set in container: True`; the weights download; `…-test` ends with `Smoke test passed`.

### Story: Operator deploys a gated model without a token
1. Same entry, no `HF_TOKEN` in `.env`. The deploy succeeds (weights are fetched at container start), `…-test` never gets `health 200`.
2. The driver's last line is `If google/embeddinggemma-300m is a private or gated Hugging Face repo, set HF_TOKEN in .env (read-scope token: https://huggingface.co/settings/tokens) and deploy again.`; `modal app logs` shows `HF_TOKEN set in container: False` followed by the Hub's `GatedRepoError` — the README troubleshooting line names exactly this.

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

### [PA] 2026-09-19 18:44 — Re-grooming (HF token)

**What changed and why**
- The human brought the optional Hugging Face token into scope. On a fallback path the CONTAINER downloads the weights, so it must see `HF_TOKEN`: delivered as `secrets=[modal.Secret.from_dict(hf_token_env())]`, built under `modal.is_local()`, with `Secret.from_dict({})` in the container — the exact shape Modal's secrets guide documents for sending a local secret. The list always has one element (an empty Secret without a token), so there is no conditional shape to get wrong on the remote re-import.
- NOT in the image `.env(...)` next to `EMBEDDING_DEPLOY_SPEC`: image layers are cached and inspectable. NOT `Secret.from_name`: a named secret needs a bootstrap step, which ADR-009 §4 just removed.
- The approved static criterion "no `modal.Secret`" was narrowed precisely: `Secret.from_name`, `MODAL_EMBEDDING_API_KEY` and `--api-key` stay forbidden; exactly two `Secret.from_dict` calls are allowed (`hf_token_env()` locally, `{}` remotely) and `HF_TOKEN` may not appear in the image env.
- One boolean log line at `@modal.enter` (`HF_TOKEN set in container: True|False`) is the only live evidence that the Secret arrived — it makes the gated-repo failure diagnosable and gives #141 something to assert without ever printing the value.
- `hf_token_env()` lives in `modal_catalog` (3 lines, unit-tested) so the two scripts stay glue.
- README gains the token sentence and the troubleshooting line here (after #140 rewrote the section).

**Dependencies**
- #139 additionally provides `Settings.hf_token` and the driver's missing-token hint.

**User stories**
- 6 stories: the previous 4 + gated model with a token + gated model without one.

Ready for implementation.
