---
id: 140-modal-client-catalog-and-proxy-token-auth
status: pending
feature: voyage-4-and-modal-embedding-catalog
---

# `ModalEmbeddingModel`: resolve from the Embedding catalog, proxy-token auth, Matryoshka guard, client-side prompts; retire `MODAL_EMBEDDING_API_KEY`

Tags: `models`, `modal`, `config`, `ci`
Depends on: #135, #138, #139
Blocks: #141
Implements: ADR-009 — Decision 3 (catalog lookup), Decision 4 (Proxy token auth, client side), Decision 5 (Modal role mapping)

## Scope

**1. Client** — `src/tree/models/modal_embedding.py`:
- Constructor `ModalEmbeddingModel(proxy_token: str, model: str, dimensions: int | None = None, health_timeout: float = 300.0)`.
  Delete `_DEFAULT_APP_NAME`, `_DEFAULT_FUNCTION_NAME`, the `app_name` / `function_name` params and
  the `_MODEL_NATIVE_DIMENSIONS` table (it cites a `deploy/embedding_models.py` that never existed).
  `entry = get_catalog_entry(model)` at construction — an unknown model fails THERE with the catalog error.
- Empty `proxy_token` → `ModelError("Modal proxy token is required. Set MODAL_PROXY_TOKEN_ID and MODAL_PROXY_TOKEN_SECRET.")`.
- Matryoshka guard at construction: `dimensions in (None, entry.native_dimensions)` → native, and
  `dimensions=` is NOT sent on the wire; `dimensions in entry.matryoshka_dimensions` → sent as
  `dimensions=`; anything else → `ModelError("Qwen/Qwen3-Embedding-0.6B cannot produce 512-d vectors: native 1024, matryoshka_dimensions []. Set models.<block>.dimensions to 1024 or extend the Embedding catalog entry.")`.
  The `dimensions` property returns the effective size.
- URL: `modal.Server.from_name(entry.app_name, EMBEDDING_SERVER_NAME)` + `get_url()` (verified to
  exist on Modal "latest"; SWE must verify the ASYNC form — e.g. `.get_url.aio()` — with the
  `tech-docs` skill / context7 against Modal 1.5.x: modal.com/docs/sdk/py/latest/Server.md,
  /docs/guide/servers.md; never block the event loop). `/v1` suffix logic unchanged. Lookup failure →
  `ModelError("… Is the app deployed? Run: make memory-deploy-embedding-model MODEL=<repo_id>")`.
- Auth: `AsyncOpenAI(base_url=…, api_key=proxy_token)` (sends `Authorization: Bearer <id>.<secret>`);
  the aiohttp `GET /health` warm-up sends the SAME header. A 401 from `/health` raises
  `ModelError` (not retryable `ExtractionError`) naming the two env vars.
- Role: `embed(texts, input_type)` prepends `prompt_for(entry, input_type)` to each text client-side
  (OpenAI-compatible `/v1/embeddings` has no role parameter); `None` or an empty prompt → texts
  unchanged. Usage recording unchanged (`total_cost=0`).
**2. Factory** — `get_model.py` modal branch: `ModalEmbeddingModel(proxy_token=modal_proxy_bearer(), model=cfg.model, dimensions=cfg.dimensions)`
  (today `cfg.dimensions` is silently dropped). KEEP the lazy import and the cold-boot note.
**3. Retire `MODAL_EMBEDDING_API_KEY`** everywhere: `settings.py` field; `.env.example` line;
  `.github/workflows/ci.yml:34` and `cd.yml:64` mocks → replaced by `MODAL_PROXY_TOKEN_ID: mock-modal-proxy-token-id` /
  `MODAL_PROXY_TOKEN_SECRET: mock-modal-proxy-token-secret`; `tests/unit/config/test_settings_credentials_only.py`;
  `tests/unit/models/test_get_model.py`; root `Makefile:39` comment (`generate-secret-key` example →
  `e.g. MCP_AUTH_SECRET`-style neutral wording, no Modal mention); README env table row → the two
  new vars; README "Modal embedding deployment" section rewritten: catalog → `MODEL=` targets →
  proxy tokens → flipping `models.search_embedding` to `provider: modal, model: voyageai/voyage-4-nano`
  (and the stale "default embedding model is local sentence-transformers" sentence removed).
  `deploy/prefect_pipelines_setup.py` needs no change (it never forwarded the key — grep clean).

Write tests with `/squid-testing-python`; `modal.Server`, `AsyncOpenAI` and aiohttp all mocked.

## Out of scope
- A pipeline run with `provider: modal` (feature non-goal). Switching the default provider.
- Removing `MODAL_EMBEDDING_API_KEY` from the human's `.env` / `.env.prod` files (agents do not edit
  them; `extra="ignore"` makes the leftover line harmless) — noted in `## Log` for the human.
- Deleting the old `vllm-embedding-api-key` Modal secret in the workspace (#141 notes it).

## Acceptance Criteria

- [ ] `grep -rIn "MODAL_EMBEDDING_API_KEY\|modal_embedding_api_key\|vllm-embedding-api-key" . --exclude-dir=.git --exclude-dir=.venv --exclude-dir=node_modules --exclude-dir=done --exclude=".env*"` → 0 matches outside `tasks/` and `docs/adrs/009_*`.
- [ ] `ModalEmbeddingModel(proxy_token="", model="voyageai/voyage-4-nano")` raises `ModelError` naming both env vars; `model="BAAI/bge-m3"` raises the catalog error listing both ids — `tests/unit/models/test_modal_embedding.py::TestInit`.
- [ ] URL resolution calls `modal.Server.from_name("ep-voyage-4-nano", "EmbeddingServer")`; `/v1` is appended once; a failed lookup raises `ModelError` containing `make memory-deploy-embedding-model MODEL=voyageai/voyage-4-nano` — `::TestUrlResolution`.
- [ ] `AsyncOpenAI` is built with `api_key="wk-1.ws-2"`; the `/health` GET carries `Authorization: Bearer wk-1.ws-2`; a 401 health answer raises `ModelError` naming both env vars; a 503 raises `ExtractionError` — `::TestProxyAuth`.
- [ ] Dimensions: voyage-4-nano with `dimensions=1024` or `None` → `.dimensions == 1024` and NO `dimensions` kwarg in `embeddings.create`; `dimensions=512` → kwarg `dimensions=512`, `.dimensions == 512`; Qwen3 with `dimensions=512` → `ModelError` containing `cannot produce 512-d` and `native 1024` — `::TestMatryoshkaGuard`.
- [ ] Role: voyage-4-nano `embed(["cats"], input_type="query")` sends input `["Represent the query for retrieving supporting documents: cats"]`; `"document"` → `["Represent the document for retrieval: cats"]`; `None` → `["cats"]`; Qwen3 `"document"` → `["cats"]`, `"query"` → the text prefixed with the catalog `query_prompt` — `::TestRolePrompts`.
- [ ] `_build_embedding_model(EmbeddingConfig(provider="modal", model="voyageai/voyage-4-nano", dimensions=512))` constructs the client with `dimensions=512` and the bearer from settings; importing `tree.models.get_model` does not import `modal` (`'modal' not in sys.modules`, subprocess) — `tests/unit/models/test_get_model.py::TestModalBranch`.
- [ ] `Settings` field set has no `modal_embedding_api_key` — `tests/unit/config/test_settings_credentials_only.py`.
- [ ] `ci.yml` and `cd.yml` each mock the two proxy-token vars and neither mentions the old key.
- [ ] README env table lists `MODAL_PROXY_TOKEN_ID` and `MODAL_PROXY_TOKEN_SECRET`; the Modal section shows the three `MODEL=` commands and no `generate-secret-key` step.
- [ ] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green.

## User Stories

### Story: Operator points search at the Modal-hosted voyage-4-nano
1. After #139's deploy, sets `models.search_embedding: {provider: modal, model: voyageai/voyage-4-nano, dimensions: 1024}`.
2. First `embed()` logs `ModalEmbeddingModel ready: app=ep-voyage-4-nano server=EmbeddingServer url=https://…/v1`, warms `/health` with the bearer, and returns 1024-d vectors.
3. Because voyage-4-nano shares the 4-series space, the vectors are comparable to `voyage-4` API vectors.

### Story: Operator forgot the proxy token
1. `.env` lacks `MODAL_PROXY_TOKEN_SECRET`.
2. Building the model fails immediately: `Modal proxy token is required. Set MODAL_PROXY_TOKEN_ID and MODAL_PROXY_TOKEN_SECRET.` — no network call was made.

### Story: Operator asks a model for a size it cannot produce
1. `models.search_embedding: {provider: modal, model: Qwen/Qwen3-Embedding-0.6B, dimensions: 512}`.
2. Boot fails: `Qwen/Qwen3-Embedding-0.6B cannot produce 512-d vectors: native 1024, matryoshka_dimensions []. …`.

### Story: The app was stopped
1. `make memory-deploy-embedding-model-stop MODEL=voyageai/voyage-4-nano`, then a query runs.
2. `ModelError: Failed to resolve Modal server ep-voyage-4-nano/EmbeddingServer. Is the app deployed? Run: make memory-deploy-embedding-model MODEL=voyageai/voyage-4-nano`.

### Story: A question is embedded with the right prompt
1. `search_memory("how does sharding work?")` with `provider: modal`.
2. The request input is `"Represent the query for retrieving supporting documents: how does sharding work?"`; chunks were indexed with the document prompt.

---

Blocked by: #135, #138, #139

## Log

### [PA] 2026-09-19 17:43 — Grooming

**Summary**
The Modal client becomes catalog-driven and proxy-token-authenticated, finally honours `dimensions`, and maps the Embedding role to client-side prompts; the engine API key disappears from the repo.

**Key decisions**
- Unknown model / missing token / impossible dimension all fail at CONSTRUCTION, before any network call.
- `dimensions=` is sent only when it differs from native: vLLM/SGLang reject the parameter for models not flagged Matryoshka.
- A 401 on `/health` is a configuration error (`ModelError`), not a transient `ExtractionError` — retrying cannot fix a wrong token.
- The human removes the stale `.env` / `.env.prod` line by hand.

**Dependencies**
- #135 — `input_type` on the contract. #138 — catalog helpers. #139 — settings token fields, `modal_proxy_bearer`, `modal>=1.5.5`, the `EmbeddingServer` name.

**User stories**
- 5 stories: happy path, missing token, impossible dimension, stopped app, query prompt.

Ready for implementation.
