---
id: 139-modal-deploy-driver-and-dedicated-endpoint
status: pending
feature: voyage-4-and-modal-embedding-catalog
---

# Deploy driver: Dedicated endpoint first (`modal endpoint create`), `SERVING=` override, one proxy-token smoke test for every Serving path, driven by `MODEL=<repo_id>`

Tags: `modal`, `deploy`, `scripts`, `infra`
Depends on: #138
Blocks: #140, #142, #141
Implements: ADR-009 — Decision 2 (Serving path ladder; the `endpoint` path), Decision 3 (one app per model, one URL lookup), Decision 4 (Proxy token auth, operator side) and Decision 9 (optional `HF_TOKEN`: the setting, the endpoint side, argv redaction)

## Scope

After this task `make memory-deploy-embedding-model MODEL=Qwen/Qwen3-Embedding-0.6B` creates a Modal
Dedicated endpoint — no deploy script of ours is involved. The `sglang` / `vllm` argv shapes are
built here too; their script FILES arrive in #142.

Verified on 2026-09-19 (modal.com/docs/cli/latest/endpoint.md, /docs/guide/dedicated-endpoints.md,
/docs/guide/endpoints.md): `modal endpoint create --model TEXT [--name TEXT] [--routing-region TEXT]
[--custom-hf-repo TEXT] [--custom-hf-revision TEXT] [--custom-hf-token TEXT] …`
(`--custom-hf-token`: "Hugging Face token for private --custom-hf-repo"; no env-var form is documented);
`modal endpoint list [--json]`; `modal endpoint stop [-y] ENDPOINT_IDENTIFIER`; scale to zero by
default; proxy tokens required by default (`Authorization: Bearer wk-<id>.ws-<secret>`); embedding
models answer the OpenAI-compatible Embeddings API.

**1. Dependency** — `apps/memory/pyproject.toml` `local-models` extra: `modal>=1.5.5` (today
`>=1.3.5`, lock pins 1.3.5; `modal endpoint`, `@app.server` and `modal.Server` need the newer
client — SWE must verify the minimum version that ships `modal endpoint` and raise the floor if it
is higher); `uv lock`.

**2. Settings (add only)** — `src/tree/config/settings.py`: `modal_proxy_token_id: SecretStr = SecretStr("")`,
`modal_proxy_token_secret: SecretStr = SecretStr("")` and the OPTIONAL `hf_token: SecretStr = SecretStr("")`
(env `HF_TOKEN` — the name `huggingface_hub`, vLLM and SGLang read natively; empty = today's
behaviour for public repos; nothing ever requires it). `.env.example` gains
`MODAL_PROXY_TOKEN_ID=wk-your-proxy-token-id` / `MODAL_PROXY_TOKEN_SECRET=ws-your-proxy-token-secret`
under the Modal heading (heading text -> `# Modal (hosted embedding models)`) with a one-line pointer
to Modal -> Settings -> Proxy Auth Tokens, then the two lines
`# Optional — only for private or gated Hugging Face repos (read-scope token: https://huggingface.co/settings/tokens).`
and `# HF_TOKEN=hf_your-read-token` (commented out, like `# OPIK_WORKSPACE=`).
`apps/memory/README.md` env table gains ONE row after the Modal row: `HF_TOKEN`, required `no`,
default `—`, description `Hugging Face token — only to serve a private or gated repo on Modal`.
`tests/unit/config/test_settings_credentials_only.py` field set gains all three names.
`HF_TOKEN` is NOT added to the `ci.yml` / `cd.yml` mock env: optional secrets are not mocked there
today (`OPIK_API_KEY`, `BRIGHTDATA_API_KEY` are absent) and the field-set test compares names only.
(`MODAL_EMBEDDING_API_KEY` is retired in #140, with the client that still reads it.)
Helper `modal_proxy_bearer() -> str` in `tree/models/modal_catalog.py`: `f"{id}.{secret}"`, raising
`ModelError("Modal proxy token is required. Set MODAL_PROXY_TOKEN_ID and MODAL_PROXY_TOKEN_SECRET.")`
when either is empty.

**3. CLI argv — pure functions** in `tree/models/modal_catalog.py`:
`modal_cli_command(action: Literal["deploy", "stop"], entry, serving: ServingPath) -> list[str]`
(`MODAL_ROUTING_REGION = "eu-west"`; script paths are relative to `apps/memory`) — ALWAYS token-free,
so it is safe to log and to assert on:
- `deploy` + `endpoint` -> `["modal", "endpoint", "create", "--name", entry.endpoint_name, "--model", entry.base_model, "--routing-region", "eu-west"]`,
  with `"--custom-hf-repo", entry.repo_id, "--custom-hf-revision", entry.revision` inserted before
  `--routing-region` ONLY when `entry.repo_id != entry.base_model` (custom weights).
- `deploy` + `vllm` -> `["modal", "deploy", "deploy/modal_vllm_embedding.py"]`; + `sglang` -> `…/modal_sglang_embedding.py`.
- `stop` + `endpoint` -> `["modal", "endpoint", "stop", "-y", entry.endpoint_name]`;
  `stop` + `vllm`/`sglang` -> `["modal", "app", "stop", entry.app_name]`.
  SWE must verify with `modal endpoint stop --help` / `modal app stop --help` on the locked client:
  that `ENDPOINT_IDENTIFIER` accepts the NAME, and whether `modal app stop` needs a `-y`.

The Hugging Face token joins the argv in ONE place and leaves every log in ONE place:
- `hf_token_args(entry, serving: ServingPath, token: str) -> list[str]` -> `["--custom-hf-token", token]`
  ONLY when `token` is non-empty AND `serving == "endpoint"` AND `entry.repo_id != entry.base_model`;
  otherwise `[]`. Base-model-only endpoints never get it: Modal documents the flag as the token
  "for private --custom-hf-repo" (SWE must verify in `modal endpoint create --help` that the wording
  still says so; if a gated BASE model turns out to need it, record the source in `## Log` and drop
  the custom-weights condition). The fallback scripts receive the token differently (#142).
- `redact_argv(argv: list[str]) -> list[str]` -> a copy where the element AFTER every
  `--custom-hf-token` is `"***"`. EVERY argv the driver logs goes through it.
- `HF_TOKEN_HINT = "If {repo_id} is a private or gated Hugging Face repo, set HF_TOKEN in .env (read-scope token: https://huggingface.co/settings/tokens) and deploy again."`
- SWE must verify in `modal endpoint create --help` on the pinned client whether `--custom-hf-token`
  lists an environment variable (`[env var: …]`). If it does: pass the token through THAT variable in
  the child `env=` instead, delete `hf_token_args` / `redact_argv` and their tests, and write
  `PA: ADR-009 §9 needs argv -> env var <NAME>` in `## Log`. If it does not (the docs today): keep the argv form.

**3b. Client-side Matryoshka truncation — ONE pure helper** in `tree/models/modal_catalog.py`, used by the
smoke test below AND by the `ModalEmbeddingModel` client (#140), so the smoke test proves the exact path
the memory uses: `truncate_embedding(vector: list[float], dimensions: int) -> list[float]` — the first
`dimensions` components divided by their L2 norm (a zero vector is returned as sliced); `dimensions > len(vector)`
-> `ModelError("cannot truncate a 1024-d vector to 2048-d")`; the input is not mutated. Why client-side
(ADR-009 §3): NO request of ours ever carries an OpenAI `dimensions` parameter — vLLM answers 400 to it
unless the model's HF config is flagged Matryoshka (voyage-4-nano's is not) and a managed recipe exposes
no flag to change that, so client-side truncation is the one mechanism that behaves identically on all
three Serving paths.

**4. Shared server helpers + the ONE smoke test** — new `src/tree/models/modal_server.py`
(imports `modal`; never imported by `modal_catalog` or at MCP boot). Used by the driver now and by
the `ModalEmbeddingModel` client in #140 — one implementation of URL lookup and authenticated health:
- `async resolve_server_url(entry) -> str` — `modal.Server.from_name(entry.app_name, EMBEDDING_SERVER_NAME)`
  then its URL, WITHOUT blocking the event loop (SWE must verify the async form on the locked client
  with the `tech-docs` skill — e.g. `.get_url.aio()`; modal.com/docs/sdk/py/latest/Server.md).
  Returns the root URL (no `/v1`). Failure or empty URL ->
  `ModelError("Failed to resolve Modal server ep-x/Server. Is the model deployed (a Dedicated endpoint may still be provisioning — check `modal endpoint list`)? Run: make memory-deploy-embedding-model MODEL=<repo_id>")`.
  The SAME lookup serves all three Serving paths (H1: a Dedicated endpoint named `N` is the Modal app
  `ep-N` with a server class `Server`; the fallback scripts copy both names). #141 proves H1 live and
  owns the correction if it is false — do not build a second mechanism here.
- `async wait_until_healthy(url: str, bearer: str, timeout: float) -> float` — `GET {url}/health`
  with `Authorization: Bearer {bearer}`; returns elapsed seconds on 200; 401 ->
  `ModelError` naming `MODAL_PROXY_TOKEN_ID` and `MODAL_PROXY_TOKEN_SECRET` (a wrong token is not
  retryable); any other status / transport error -> `ExtractionError`.
- `async served_model_id(url: str, bearer: str, default: str) -> str` — `GET {url}/v1/models`,
  returns `data[0].id`; an empty list or a failing call -> `default`, logged at WARNING. Why: we do
  not control the name a managed recipe serves under (custom weights especially), and vLLM rejects an
  unknown `model`.
- `async smoke_test(model: str, health_timeout: float = 1200.0) -> SmokeTestReport` (Pydantic:
  `url, served_model, dimensions, truncated_dimensions: int | None, cold_start_seconds, cos_relevant, cos_unrelated, unauthenticated_status`):
  resolve -> `wait_until_healthy` -> `served_model_id` -> `POST /v1/embeddings`
  (`encoding_format: "float"`, NO `dimensions` key in the body) with 3 inputs — the query `"how do I reset my password?"` prefixed by
  `prompt_for(entry, "query")`, and the documents `"To reset your password, open Settings and choose Reset password."`
  and `"The Eiffel Tower is 330 metres tall."` prefixed by `prompt_for(entry, "document")` — then assert:
  HTTP 200; 3 items; every `len(embedding) == entry.native_dimensions` — the NATIVE width, i.e.
  **2048 for `voyageai/voyage-4-nano`** (its 1024->2048 linear head) and 1024 for `Qwen/Qwen3-Embedding-0.6B`;
  `cos(query, relevant) > cos(query, unrelated)`; and `GET /health` WITHOUT the header answers 401.
  THEN, only when the entry lists a Matryoshka size below native
  (`target = max(d for d in entry.matryoshka_dimensions if d < entry.native_dimensions)` -> 1024 for
  voyage-4-nano; none for Qwen3, whose list is empty -> the step is skipped and `truncated_dimensions is None`):
  `truncate_embedding` each of the 3 vectors to `target` (no second request) and assert every length
  `== target`, every L2 norm within `1.0 ± 1e-3`, and the SAME ordering `cos(query, relevant) > cos(query, unrelated)`
  at the truncated width — this is the vector the memory writes to the 1024-d mongot index, proven per Serving path.
  Each failed assertion raises `ModelError` with the numbers, e.g.
  `expected 2048 dims, got 1024 — the Embedding catalog's native_dimensions is wrong for this Serving path, or the served architecture dropped the model's projection head`,
  `truncated norm=0.412, expected 1.0 ± 0.001`, `sanity check failed: cos(query, relevant)=0.31 <= cos(query, unrelated)=0.35 — the served architecture or pooling is probably wrong`,
  `unauthenticated /health answered 200, expected 401 — the server is public`.
  Log lines (INFO, via `logger`): `health 200 after 143.2s`, `served model id: <id>`,
  `3 embeddings, 2048 dims` (voyage-4-nano; `1024 dims` for Qwen3), `sanity: cos(query, relevant)=0.71 > cos(query, unrelated)=0.22`,
  then for voyage-4-nano only `truncated 2048 -> 1024 dims client-side, norm=1.000` and
  `sanity@1024: cos(query, relevant)=0.70 > cos(query, unrelated)=0.21`,
  `unauthenticated health -> 401`, `Smoke test passed`.

**5. Driver glue + Make** — `scripts/modal_embedding_model.py` (`init_logger()` at module level,
`click` group `deploy | test | stop`; all take `--model`, `deploy` and `stop` also take
`--deployment-path`-free option `--serving`): resolves the entry (unknown id -> the catalog error,
exit 2) and `resolve_serving(entry, serving)` (bad value / missing `base_model` -> exit 2);
- `deploy` / `stop`: builds the argv with `modal_cli_command`; if the argv names a `deploy/*.py` that
  does not exist -> logs `Serving path 'vllm' needs deploy/modal_vllm_embedding.py, which does not exist.`
  and exits 2. Otherwise `run_argv = argv + hf_token_args(entry, serving, settings.hf_token.get_secret_value())`
  for `deploy` (`run_argv = argv` for `stop`); logs `Running: <shlex.join(redact_argv(run_argv))>`;
  `result = subprocess.run(run_argv, check=False, env={**os.environ, "EMBEDDING_MODEL": repo_id})`.
  A non-zero `result.returncode` -> logs `modal command failed (exit <n>): <the same redacted argv>`
  and exits `<n>`. `check=True` is FORBIDDEN in this script: `CalledProcessError` prints its full
  argv — the token. `run_argv` itself is never logged, formatted into a message or raised.
  When an override differs from the entry it logs
  `SERVING=sglang overrides the catalog's serving: endpoint for THIS command only — the ModalEmbeddingModel client keeps reading configs/default.yaml.`
  On an `endpoint` deploy it logs
  `Dedicated endpoint: Modal picks the GPU, engine and flags — this entry's gpu/cpu/memory_mb/max_model_len/extra_server_args are used only by SERVING=sglang|vllm.`
- `test`: `asyncio.run(smoke_test(model))`; a `ModelError` / `ExtractionError` is logged and exits 1.
  It takes no `--serving`: under H1 the lookup is identical on every path.
- Missing-token hint (the ONLY gated-repo handling — no pre-flight Hub call): when `settings.hf_token`
  is EMPTY and a `deploy` exits non-zero or `test` fails, the driver logs `HF_TOKEN_HINT` (WARNING,
  formatted with the `repo_id`) as its last line. Why both sites: a gated download fails inside the
  engine at container start, which surfaces as a failed create OR as a smoke test that never gets
  `health 200`. With a token set the hint is never logged.

`apps/memory/Makefile` replaces the three old targets (and deletes the
`modal secret create vllm-embedding-api-key …` bootstrap):
- `deploy-embedding-model: # Serve ONE Embedding catalog model on Modal as its own app (ep-<model>) via the entry's Serving path — endpoint (Modal Dedicated Endpoint, default) | sglang | vllm (our fallback scripts). Requires MODEL=<repo_id>, e.g. MODEL=Qwen/Qwen3-Embedding-0.6B. Optional SERVING=endpoint|sglang|vllm overrides the YAML for this command only (the client still reads the YAML).`
- `deploy-embedding-model-test: # Smoke-test a served model through proxy-token auth (health, /v1/embeddings, native dimensions, client-side Matryoshka truncation when the entry lists sizes, relevant-vs-unrelated sanity, 401 without a token). Requires MODEL=<repo_id>.`
- `deploy-embedding-model-stop: # Stop one catalog model on Modal. Requires MODEL=<repo_id>; pass the same SERVING= you deployed with.`
  Each prints a `USAGE:` line and exits 1 when `MODEL` is empty (same `@if [ -z … ]` guard as `search-web`).
  `HF_TOKEN` is NOT a Make variable: it reaches the driver through `Settings`, never through a recipe line (Make echoes recipes).

**6. Delete** `apps/memory/deploy/modal_vllm_embedding.py` — the single-model script with the
engine-level `--api-key`. Its replacement arrives in #142 as a fallback script.

Write tests with `/squid-testing-python`; NO test touches the network; `modal.Server` and aiohttp are mocked;
token tests PATCH `settings.hf_token` (never rely on the developer's real `HF_TOKEN`, which Make exports from `.env`).

## Out of scope
- The fallback script files, their `HF_TOKEN` Modal Secret and their static tests (#142). Until then
  `serving: vllm|sglang` deploys exit 2 with the "does not exist" message — expected inside this feature branch.
- The `ModalEmbeddingModel` client and retiring `MODAL_EMBEDDING_API_KEY` (#140): until #140 lands the
  old client points at a deleted app — expected inside this feature branch; `provider: modal` is not the default.
- Any live `modal` call (#141 — including the proof of H1, `--name` semantics, the stop identifier,
  `modal endpoint list --json` fields and the live redaction check).
- A pre-flight Hugging Face Hub call to detect a gated repo; a per-model token or a token field in the
  Embedding catalog (the token is one workspace credential — ADR-009 §9); a README Modal-section
  sentence about the token (#142, after #140 rewrites that section).
- `--custom-volume-*`, `--compute-region`, `--colocate-compute`; min/max/buffer
  containers (dashboard-only on a Dedicated endpoint). Re-deploying an existing endpoint name
  (the CLI's own error surfaces; README says "stop first").

## Acceptance Criteria

- [ ] `uv.lock` resolves `modal` >= `1.5.5`; `uv --directory apps/memory run python -c "import modal; modal.Server"` exits 0; `uv --directory apps/memory run modal endpoint create --help` exits 0 and its output contains `--custom-hf-repo`, `--custom-hf-revision`, `--custom-hf-token`, `--routing-region` and `--name` (output pasted in `## Log`, with one line stating whether `--custom-hf-token` lists an env var).
- [ ] `Settings` exposes `modal_proxy_token_id` / `modal_proxy_token_secret` / `hf_token` (all `SecretStr`, default empty); `Settings()` validates with none of them set; the locked-down field-set test lists all three — `tests/unit/config/test_settings_credentials_only.py`. `.env.example` contains `MODAL_PROXY_TOKEN_ID`, `MODAL_PROXY_TOKEN_SECRET`, `HF_TOKEN` and `https://huggingface.co/settings/tokens`; `grep -c "HF_TOKEN" apps/memory/README.md` >= 1; `grep -c "HF_TOKEN" .github/workflows/ci.yml .github/workflows/cd.yml` -> 0 in both.
- [ ] `modal_proxy_bearer()` -> `"wk-1.ws-2"` for id `wk-1` / secret `ws-2`; raises `ModelError` naming BOTH env vars when either is empty — `tests/unit/models/test_modal_catalog.py::TestProxyBearer`.
- [ ] `modal_cli_command("deploy", qwen_entry, "endpoint") == ["modal", "endpoint", "create", "--name", "qwen3-embedding-0-6b", "--model", "Qwen/Qwen3-Embedding-0.6B", "--routing-region", "eu-west"]` (no `--custom-hf-*`: `repo_id == base_model`) — `::TestModalCliCommand::test_endpoint_without_custom_weights`.
- [ ] `modal_cli_command("deploy", voyage_entry, "endpoint") == ["modal", "endpoint", "create", "--name", "voyage-4-nano", "--model", "Qwen/Qwen3-Embedding-0.6B", "--custom-hf-repo", "voyageai/voyage-4-nano", "--custom-hf-revision", "main", "--routing-region", "eu-west"]` — `::test_endpoint_with_custom_weights`; no argv returned by `modal_cli_command` ever contains `--custom-hf-token` or `--unauthenticated`, even with `settings.hf_token` patched to `hf_secret123` — `::test_never_public_never_token`.
- [ ] `hf_token_args(voyage_entry, "endpoint", "hf_secret123") == ["--custom-hf-token", "hf_secret123"]`; it returns `[]` for `(qwen_entry, "endpoint", "hf_secret123")` (no custom weights), `(voyage_entry, "vllm", "hf_secret123")`, `(voyage_entry, "sglang", "hf_secret123")` and `(voyage_entry, "endpoint", "")` — `::TestHfTokenArgs` (5 cases). `redact_argv(["modal", "endpoint", "create", "--custom-hf-token", "hf_secret123"]) == ["modal", "endpoint", "create", "--custom-hf-token", "***"]`, leaves an argv without the flag unchanged, and does not mutate its input — `::TestRedactArgv`.
- [ ] `("deploy", voyage_entry, "vllm")` -> `["modal", "deploy", "deploy/modal_vllm_embedding.py"]`; `("deploy", qwen_entry, "sglang")` -> `["modal", "deploy", "deploy/modal_sglang_embedding.py"]`; `("stop", qwen_entry, "endpoint")` -> `["modal", "endpoint", "stop", "-y", "qwen3-embedding-0-6b"]`; `("stop", qwen_entry, "sglang")` -> `["modal", "app", "stop", "ep-qwen3-embedding-0-6b"]` (plus `-y` only if verified necessary) — `::test_script_and_stop_commands`.
- [ ] `resolve_server_url(qwen_entry)` calls `modal.Server.from_name("ep-qwen3-embedding-0-6b", "Server")` and returns the URL without a trailing `/` or `/v1`; a raising lookup or an empty URL -> `ModelError` containing `make memory-deploy-embedding-model MODEL=Qwen/Qwen3-Embedding-0.6B` and `modal endpoint list` — `tests/unit/models/test_modal_server.py::TestResolveServerUrl`.
- [ ] `wait_until_healthy` sends `Authorization: Bearer wk-1.ws-2`; 200 -> a float >= 0; 401 -> `ModelError` naming both env vars; 503 -> `ExtractionError` — `::TestWaitUntilHealthy`. `served_model_id` returns `"Qwen/Qwen3-Embedding-0.6B"` for `{"data": [{"id": "Qwen/Qwen3-Embedding-0.6B"}]}` and the `default` (with one WARNING record) for `{"data": []}` and for a 500 — `::TestServedModelId`.
- [ ] `smoke_test("voyageai/voyage-4-nano")` with mocked HTTP: posts exactly 3 inputs, the first starting with `Represent the query for retrieving supporting documents: `, the other two with `Represent the document for retrieval: `, under the DISCOVERED model id; the POST body has NO `dimensions` key; with 2048-d mocked vectors it returns a report with `dimensions == 2048`, `truncated_dimensions == 1024`, `unauthenticated_status == 401` and logs `3 embeddings, 2048 dims` and `truncated 2048 -> 1024 dims client-side`. It raises `ModelError` containing `expected 2048 dims, got 1024` for 1024-d vectors (the catalog-was-wrong / head-dropped case); containing `sanity check failed` when the unrelated document scores >= the relevant one at native width, and containing `sanity@1024` when the ordering holds at 2048-d but flips in the first 1024 components; containing `expected 401` when the header-less `/health` answers 200. `smoke_test("Qwen/Qwen3-Embedding-0.6B")` with 1024-d vectors returns `dimensions == 1024`, `truncated_dimensions is None` and logs no `truncated` line — `::TestSmokeTest` (6 tests).
- [ ] `truncate_embedding([3.0, 4.0, 12.0], 2) == [0.6, 0.8]` (± 1e-9); a 2048-d unit vector truncated to 1024 has length 1024 and L2 norm `1.0 ± 1e-6`; `truncate_embedding(v, len(v))` returns `v` renormalised; `truncate_embedding([1.0] * 1024, 2048)` raises `ModelError` containing `cannot truncate a 1024-d vector to 2048-d`; the input list is not mutated — `tests/unit/models/test_modal_catalog.py::TestTruncateEmbedding`.
- [ ] Driver — `tests/unit/scripts/test_modal_embedding_model_script.py` (`subprocess.run` and `smoke_test` patched): `deploy --model Qwen/Qwen3-Embedding-0.6B` runs the endpoint argv with `EMBEDDING_MODEL=Qwen/Qwen3-Embedding-0.6B` in the child env and logs the `Dedicated endpoint: Modal picks the GPU` line; `deploy --model voyageai/voyage-4-nano --serving endpoint` runs the custom-weights argv and logs the `overrides the catalog's serving: vllm` line; `deploy --model voyageai/voyage-4-nano` (script absent in a tmp cwd) exits 2, runs nothing and logs `does not exist`; `--model BAAI/bge-m3` exits 2, runs nothing, logs both catalog ids; `--serving tgi` exits 2; `test --model …` awaits `smoke_test` once and exits 1 when it raises `ModelError`.
- [ ] Driver, token set (`settings.hf_token` patched to `hf_secret123`) — `::TestHfToken`: `deploy --model voyageai/voyage-4-nano --serving endpoint` calls `subprocess.run` with an argv whose last two items are `--custom-hf-token`, `hf_secret123`; the combined `caplog` text + click output contains `--custom-hf-token ***` and does NOT contain `hf_secret123`; `deploy --model Qwen/Qwen3-Embedding-0.6B` runs an argv WITHOUT `--custom-hf-token`; with `subprocess.run` returning `returncode=1` the command exits 1, logs `modal command failed (exit 1)`, no `CalledProcessError` is raised, `hf_secret123` occurs nowhere in the captured output, and the hint is NOT logged.
- [ ] Driver, token empty — `::TestHfTokenHint`: the custom-weights argv equals `modal_cli_command(...)` exactly; a `deploy` with `returncode=1` and a `test` whose `smoke_test` raises `ExtractionError` each log, as the last record, `If voyageai/voyage-4-nano is a private or gated Hugging Face repo, set HF_TOKEN in .env` (WARNING); a successful deploy logs no hint.
- [ ] `grep -c "check=True" apps/memory/scripts/modal_embedding_model.py` -> 0.
- [ ] `make memory-deploy-embedding-model` (no MODEL, run WITHOUT `-n`) prints `USAGE: make memory-deploy-embedding-model MODEL=<repo_id> [SERVING=endpoint|sglang|vllm]`, exits non-zero and runs no `modal` command; same for `-stop`; `-test` prints `USAGE: make memory-deploy-embedding-model-test MODEL=<repo_id>`. `grep -c "vllm-embedding-api-key\|MODAL_EMBEDDING_API_KEY\|HF_TOKEN" apps/memory/Makefile` -> 0.
- [ ] `apps/memory/deploy/modal_vllm_embedding.py` no longer exists; `grep -rn "print(" apps/memory/scripts/modal_embedding_model.py apps/memory/src/tree/models/modal_server.py` -> 0 matches; every new function has parameter and return annotations.
- [ ] `python -c "import sys, tree.models.modal_catalog; assert 'modal' not in sys.modules"` still exits 0 (the argv builders and the bearer live there; `modal` is imported only by `modal_server`).
- [ ] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green.
- [ ] [HUMAN] none here — every live `modal` call is #141. The task `## Log` must never contain a token value: paste logged (redacted) lines only.

## User Stories

### Story: Operator serves Qwen3-Embedding as a Dedicated endpoint
1. `make memory-deploy-embedding-model MODEL=Qwen/Qwen3-Embedding-0.6B`
2. The log shows `Running: modal endpoint create --name qwen3-embedding-0-6b --model Qwen/Qwen3-Embedding-0.6B --routing-region eu-west` and the line `Dedicated endpoint: Modal picks the GPU, engine and flags — …`.
3. `make memory-deploy-embedding-model-test MODEL=Qwen/Qwen3-Embedding-0.6B` logs `health 200 after …s`, `served model id: …`, `3 embeddings, 1024 dims`, `sanity: cos(query, relevant)=… > cos(query, unrelated)=…`, `unauthenticated health -> 401`, `Smoke test passed`.
4. `make memory-deploy-embedding-model-stop MODEL=Qwen/Qwen3-Embedding-0.6B` runs `modal endpoint stop -y qwen3-embedding-0-6b`.

### Story: Operator tries custom weights before falling back
1. `make memory-deploy-embedding-model MODEL=voyageai/voyage-4-nano SERVING=endpoint`
2. The log shows `… --model Qwen/Qwen3-Embedding-0.6B --custom-hf-repo voyageai/voyage-4-nano --custom-hf-revision main …` and `SERVING=endpoint overrides the catalog's serving: vllm for THIS command only — …`.
3. The `-test` target fails with `expected 2048 dims, got 1024 — …` (the base model's recipe has no 1024->2048 head), with `sanity check failed: …`, or Modal refuses the weights -> the operator stops it with `SERVING=endpoint` and moves down the ladder.

### Story: Operator serves a PRIVATE fine-tune as a Dedicated endpoint
1. Adds `HF_TOKEN=hf_…` (read scope) to `.env` and the entry `{repo_id: acme/my-embedder, revision: <sha>, base_model: Qwen/Qwen3-Embedding-0.6B, native_dimensions: 1024}` to the Embedding catalog.
2. `make memory-deploy-embedding-model MODEL=acme/my-embedder`
3. The log shows `Running: modal endpoint create --name my-embedder --model Qwen/Qwen3-Embedding-0.6B --custom-hf-repo acme/my-embedder --custom-hf-revision <sha> --routing-region eu-west --custom-hf-token ***` — the token value appears nowhere, including when the command fails.

### Story: Operator forgot the token for a gated repo
1. `HF_TOKEN` is not in `.env`; `make memory-deploy-embedding-model MODEL=acme/my-embedder`, then `…-test`.
2. The create fails (or the smoke test never gets `health 200`); the LAST log line is `If acme/my-embedder is a private or gated Hugging Face repo, set HF_TOKEN in .env (read-scope token: https://huggingface.co/settings/tokens) and deploy again.`

### Story: Operator walks down the ladder before the fallback scripts exist
1. `make memory-deploy-embedding-model MODEL=Qwen/Qwen3-Embedding-0.6B SERVING=sglang` (before #142).
2. `Serving path 'sglang' needs deploy/modal_sglang_embedding.py, which does not exist.`; exit 2; nothing is deployed.

### Story: The smoke test proves the 1024-d vector the memory will store
1. voyage-4-nano is live through any Serving path; `make memory-deploy-embedding-model-test MODEL=voyageai/voyage-4-nano`.
2. The log shows `3 embeddings, 2048 dims`, `sanity: …`, `truncated 2048 -> 1024 dims client-side, norm=1.000`, `sanity@1024: cos(query, relevant)=… > cos(query, unrelated)=…`, `unauthenticated health -> 401`, `Smoke test passed`.
3. Had the Embedding catalog still said `native_dimensions: 1024`, the run would end with `expected 1024 dims, got 2048 — …`; exit 1.

### Story: Operator mistypes MODEL or SERVING
1. `make memory-deploy-embedding-model MODEL=qwen3` -> `Unknown Modal embedding model 'qwen3'. Embedding catalog ids: Qwen/Qwen3-Embedding-0.6B, voyageai/voyage-4-nano. …`; exit 2.
2. `… MODEL=Qwen/Qwen3-Embedding-0.6B SERVING=tgi` -> `Unknown Serving path 'tgi'. Use one of: endpoint, sglang, vllm.`; exit 2.

### Story: Operator forgets MODEL
1. `make memory-deploy-embedding-model-stop`
2. `USAGE: make memory-deploy-embedding-model-stop MODEL=<repo_id> [SERVING=endpoint|sglang|vllm]`; non-zero exit.

### Story: The smoke test catches a public server
1. A model was created by hand with `--unauthenticated`.
2. `make memory-deploy-embedding-model-test MODEL=…` ends with `unauthenticated /health answered 200, expected 401 — the server is public`; exit 1.

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

### [PA] 2026-09-19 18:23 — Re-grooming (plan edit: Dedicated endpoints first)

**What changed and why**
- RENAMED from `139-modal-vllm-and-sglang-deploy-scripts`. The human made a Modal Dedicated Endpoint the preferred, zero-code Serving path; this task now delivers exactly that path plus the driver every path shares. The two scripts moved to the new #142 (fallbacks), which keeps both tasks atomic.
- `modal_cli_command` gained the `endpoint` argv (`modal endpoint create` / `modal endpoint stop -y`), with `--custom-hf-repo/--custom-hf-revision` only when `repo_id != base_model`; flags verified against Modal's CLI docs today.
- The smoke test left the deploy scripts (a Dedicated endpoint has no script of ours to host a `local_entrypoint`): ONE function in `tree/models/modal_server.py`, run directly by the driver — `-test` no longer calls `modal run`. It gained a relevant-vs-unrelated sanity assertion because custom weights under the wrong architecture can return well-shaped garbage.
- `resolve_server_url` / `wait_until_healthy` / `served_model_id` are built here and reused by the client in #140 — one URL mechanism (H1) and one authenticated health check. The served model id is DISCOVERED, since a managed recipe picks it.
- `SERVING=` override on the Make targets lets an operator execute the ladder without editing YAML; the YAML stays the truth the client reads (logged on every override).
- The old single-model script is deleted here (its Make targets go, and it carries the retired engine key); the approved `make -n` USAGE criterion was replaced — `-n` never executes the guard.
- `HF_TOKEN` / `--custom-hf-token` out of scope: public seeds, and a secret in a logged argv needs redaction.

**Dependencies**
- #138 — entry, `endpoint_name` / `app_name`, `resolve_serving`, `prompt_for`, `EMBEDDING_SERVER_NAME`.

**User stories**
- 6 stories: endpoint deploy/test/stop, custom-weights attempt, ladder before #142, bad MODEL/SERVING, missing MODEL, public server caught.

Ready for implementation.

### [PA] 2026-09-19 18:44 — Re-grooming (HF token)

**What changed and why**
- The human brought the Hugging Face token INTO scope as an optional credential ("if the deployment needs it"); the last bullet of the entry above is superseded. It is `Settings.hf_token` / `HF_TOKEN` (no HF variable existed in the repo; this is the name `huggingface_hub`, vLLM and SGLang read), default empty, never required, never YAML — one workspace credential, not a per-model field. Not mocked in CI: optional secrets are not mocked there today.
- Endpoint side: two 3-line pure functions instead of a structure. `modal_cli_command` stays token-free (the approved `test_never_public_never_token` criterion survives unchanged); `hf_token_args` adds `--custom-hf-token <token>` only for custom weights — Modal documents the flag as the token "for private --custom-hf-repo"; `redact_argv` is the single door every logged argv passes through.
- `subprocess.run(..., check=True)` became `check=False` + return-code handling: `CalledProcessError` embeds the argv in its message, which would print the token on the first failed create.
- Missing token is handled by ONE hint line at the two places a gated download can surface (failed deploy, failed smoke test), only when the token is empty. No pre-flight Hub call.
- Residual risk accepted in ADR-009 §9: the token is in the local process list while `modal endpoint create` runs. Modal documents no env-var form; the SWE checks `--help` on the pinned client and switches to it if one exists.
- The README env-table row lands here (with the setting); the README Modal-section sentence is #142's, because #140 rewrites that section in between.

**Dependencies**
- #138 — unchanged.

**User stories**
- 8 stories: the previous 6 + private fine-tune with a redacted log + forgotten token hint.

Ready for implementation.

### [PA] 2026-09-19 22:11 — Re-grooming (voyage-4-nano is natively 2048-d)

**What changed and why**
- FACT CORRECTION (Tester, #138 QA, primary sources): `voyageai/voyage-4-nano` serves 2048-d natively — a learned 1024->2048 linear head (`num_labels: 2048`, `linear.weight [2048, 1024]`) in the custom modeling code, reimplemented untruncated by vLLM. 1024 is a Matryoshka truncation. `Qwen/Qwen3-Embedding-0.6B` IS natively 1024. #138's catalog now says `native_dimensions: 2048` for voyage-4-nano.
- The smoke test's native-length assertion therefore expects 2048 for voyage-4-nano (1024 for Qwen3); the unit criterion flipped from `expected 1024 dims, got 2048` to `expected 2048 dims, got 1024`, and the log line is `3 embeddings, 2048 dims` for voyage.
- NEW `truncate_embedding` (pure, in `modal_catalog`) + a second smoke step for entries listing a Matryoshka size below native: truncate the SAME three vectors to 1024, assert length, L2 norm `1.0 ± 1e-3` and the sanity ordering at 1024-d. No second request and no `dimensions` parameter on the wire: ADR-009 §3 now makes truncation client-side on every Serving path (vLLM 400s on `dimensions` for a model not flagged Matryoshka; a managed recipe has no flag for it), so this step exercises exactly what #140's client does before a vector reaches the 1024-d mongot index.
- The custom-weights story gained the most likely failure: a Dedicated endpoint over base Qwen3 has no projection head, so it answers 1024-d where the catalog says 2048.

**Dependencies**
- #138 — unchanged (now with `native_dimensions: 2048` for voyage-4-nano).

**User stories**
- 9 stories: the previous 8 + the smoke test proving the truncated 1024-d vector.

Ready for implementation.
