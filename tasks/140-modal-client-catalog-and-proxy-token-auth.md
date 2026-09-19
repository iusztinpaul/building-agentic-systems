---
id: 140-modal-client-catalog-and-proxy-token-auth
status: pending
feature: voyage-4-and-modal-embedding-catalog
---

# `ModalEmbeddingModel`: resolve from the Embedding catalog on any Serving path, proxy-token auth, Matryoshka guard + client-side truncation + response-length assertion, client-side prompts; retire `MODAL_EMBEDDING_API_KEY`

Tags: `models`, `modal`, `config`, `ci`
Depends on: #135, #138, #139
Blocks: #141
Implements: ADR-009 — Decision 3 (catalog lookup, one URL lookup for all Serving paths), Decision 4 (Proxy token auth, client side), Decision 5 (Modal role mapping)

## Scope

**1. Client** — `src/tree/models/modal_embedding.py`:
- Constructor `ModalEmbeddingModel(proxy_token: str, model: str, dimensions: int | None = None, health_timeout: float = 300.0)`.
  Delete `_DEFAULT_APP_NAME`, `_DEFAULT_FUNCTION_NAME`, the `app_name` / `function_name` params and
  the `_MODEL_NATIVE_DIMENSIONS` table (it cites a `deploy/embedding_models.py` that never existed).
  `entry = get_catalog_entry(model)` at construction — an unknown model fails THERE with the catalog error.
- Empty `proxy_token` -> `ModelError("Modal proxy token is required. Set MODAL_PROXY_TOKEN_ID and MODAL_PROXY_TOKEN_SECRET.")`.
- Matryoshka guard at construction: `dimensions in (None, entry.native_dimensions)` -> effective = native;
  `dimensions in entry.matryoshka_dimensions` -> effective = `dimensions`; anything else ->
  `ModelError("Qwen/Qwen3-Embedding-0.6B cannot produce 512-d vectors: native 1024, matryoshka_dimensions []. Set models.<block>.dimensions to 1024 or extend the Embedding catalog entry.")`.
  The `dimensions` property returns the effective size. NOTE the seeds: `voyageai/voyage-4-nano` is natively
  **2048**-d (#138), so the memory's default `dimensions: 1024` is a TRUNCATION for it; Qwen3 is natively 1024.
- Dimensions on the wire — DECISION (ADR-009 §3): the client NEVER sends an OpenAI `dimensions` parameter.
  It always requests the native width, then on EVERY response:
  (1) asserts every `len(embedding) == entry.native_dimensions`, else
  `ExtractionError("voyageai/voyage-4-nano returned 1024-d vectors but its Embedding catalog entry says native_dimensions 2048 — fix modal.embedding_models (or the Serving path dropped the model's projection head). No vector was returned.")`;
  (2) when effective != native, maps #139's `truncate_embedding(vector, effective)` over the batch (slice + L2-renormalise);
  (3) asserts every returned `len(vector) == self.dimensions` (same `ExtractionError` family) as the last line before returning.
  A wrong-width vector can therefore never reach the 1024-d mongot index — not with a wrong catalog, not with a
  listed size above native. Why not server-side `dimensions=` (even as the primary path with this as a fallback):
  vLLM answers 400 unless the model's HF config is flagged Matryoshka (voyage-4-nano's `config.json` is not;
  it would need `is_matryoshka` merged into the seed's single `--hf-overrides` JSON), a managed Dedicated-endpoint
  recipe exposes no such flag, and SGLang's behaviour is unproven — three per-path behaviours plus a detect-and-fall-back
  branch, against one 4-line pure function that is identical everywhere and already proven by #139's smoke test.
  Cost: 2048 floats per text on the wire for voyage-4-nano at 1024 (ADR-009 Consequences names the upgrade trigger).
- URL, health and served model id come from #139's `tree.models.modal_server` — DELETE the client's
  own `_resolve_web_url` / `_health_check`: first `embed()` awaits `resolve_server_url(entry)`
  (append `/v1` once for `AsyncOpenAI`), `wait_until_healthy(url, proxy_token, health_timeout)` and
  `served_model_id(url, proxy_token, default=entry.repo_id)`. The client NEVER branches on
  `entry.serving`: a Dedicated endpoint and both fallback scripts expose the same app name
  (`ep-<endpoint_name>`), the same class (`Server`) and the same Proxy token auth (Modal docs,
  endpoints guide: proxy tokens are required by default on Dedicated Endpoints, same
  `Authorization: Bearer wk-….ws-…` header). That is also why a `SERVING=` override at deploy time
  needs no client change.
- Auth: `AsyncOpenAI(base_url=…, api_key=proxy_token)` (sends `Authorization: Bearer <id>.<secret>`).
  A 401 from `/health` surfaces as the `ModelError` raised by `wait_until_healthy` (not a retryable
  `ExtractionError`).
- Request `model=` is the DISCOVERED served id (a managed recipe chooses it; our scripts set it to
  `repo_id`); Opik usage recording keeps `entry.repo_id` and `total_cost=0`.
- Role: `embed(texts, input_type)` prepends `prompt_for(entry, input_type)` to each text client-side
  (OpenAI-compatible `/v1/embeddings` has no role parameter); `None` or an empty prompt -> texts unchanged.
**2. Factory** — `get_model.py` modal branch: `ModalEmbeddingModel(proxy_token=modal_proxy_bearer(), model=cfg.model, dimensions=cfg.dimensions)`
  (today `cfg.dimensions` is silently dropped). KEEP the lazy import and the cold-boot note.
**3. Retire `MODAL_EMBEDDING_API_KEY`** everywhere: `settings.py` field; `.env.example` line;
  `.github/workflows/ci.yml:34` and `cd.yml:64` mocks -> replaced by `MODAL_PROXY_TOKEN_ID: mock-modal-proxy-token-id` /
  `MODAL_PROXY_TOKEN_SECRET: mock-modal-proxy-token-secret`; `tests/unit/config/test_settings_credentials_only.py`;
  `tests/unit/models/test_get_model.py`; root `Makefile:39` comment (`generate-secret-key` example ->
  neutral wording, no Modal mention); root `README.md:43` prerequisite line (-> "Modal account + a
  Proxy token — only if you want to serve an embedding model on Modal"); README env table row -> the
  two new vars; README "Modal embedding deployment" section rewritten as ONE story: the Embedding
  catalog -> the three Serving paths and their ladder (Dedicated endpoint first; if
  `modal endpoint create` has no compatible base model, the recipe fails or the smoke test fails ->
  `SERVING=sglang` if SGLang supports the architecture -> else `SERVING=vllm`; then write the path
  that worked into the YAML, because the client reads the YAML) -> the three `MODEL=` commands ->
  proxy tokens -> flipping `models.search_embedding` to `provider: modal, model: Qwen/Qwen3-Embedding-0.6B`
  -> "min/max containers of a Dedicated endpoint are dashboard-only; to redeploy, stop first".
  The stale "default embedding model is local sentence-transformers" sentence and the "on an A10G"
  claim are removed. `deploy/prefect_pipelines_setup.py` needs no change (it never forwarded the key — grep clean).

Write tests with `/squid-testing-python`; `tree.models.modal_server` functions and `AsyncOpenAI` mocked.

## Out of scope
- A pipeline run with `provider: modal` (feature non-goal). Switching the default provider.
- A second URL mechanism (`modal endpoint list --json`, a `url:` catalog field): only if #141 proves
  H1 false, and then inside `resolve_server_url`, not here.
- The README paragraph naming the two fallback script files and the `deploy/` tree comment (#142).
- Removing `MODAL_EMBEDDING_API_KEY` from the human's `.env` / `.env.prod` files (agents do not edit
  them; `extra="ignore"` makes the leftover line harmless) — noted in `## Log` for the human.
- Deleting the old `vllm-embedding-api-key` Modal secret in the workspace (#141 notes it).

## Acceptance Criteria

- [ ] `grep -rIn "MODAL_EMBEDDING_API_KEY\|modal_embedding_api_key\|vllm-embedding-api-key" . --exclude-dir=.git --exclude-dir=.venv --exclude-dir=node_modules --exclude-dir=done --exclude=".env*"` -> 0 matches outside `tasks/` and `docs/adrs/009_*`.
- [ ] `ModalEmbeddingModel(proxy_token="", model="voyageai/voyage-4-nano")` raises `ModelError` naming both env vars; `model="BAAI/bge-m3"` raises the catalog error listing both ids — `tests/unit/models/test_modal_embedding.py::TestInit`.
- [ ] First `embed()` awaits `resolve_server_url` once with the catalog entry, builds `AsyncOpenAI` with `base_url` ending in exactly one `/v1`, and a second `embed()` resolves nothing again; a `ModelError` from `resolve_server_url` propagates unchanged (message contains `make memory-deploy-embedding-model MODEL=voyageai/voyage-4-nano`) — `::TestUrlResolution`.
- [ ] The SAME assertions hold for the `endpoint` seed and the `vllm` seed — `::TestUrlResolution::test_serving_path_is_invisible_to_the_client` (parametrised over both `repo_id`s; `grep -c "\.serving" apps/memory/src/tree/models/modal_embedding.py` -> 0).
- [ ] `AsyncOpenAI` is built with `api_key="wk-1.ws-2"`; `wait_until_healthy` is awaited with bearer `wk-1.ws-2` and `health_timeout`; its `ModelError` (401) and `ExtractionError` (503) propagate unchanged — `::TestProxyAuth`.
- [ ] With `served_model_id` answering `"served/other-name"`, `embeddings.create` is called with `model="served/other-name"` while Opik usage is recorded under `voyageai/voyage-4-nano` — `::TestServedModel`.
- [ ] Guard: voyage-4-nano with `dimensions=None` or `2048` -> `.dimensions == 2048`; `dimensions=1024` -> `.dimensions == 1024`; `dimensions=512` -> `.dimensions == 512`; `dimensions=768` -> `ModelError` containing `cannot produce 768-d`, `native 2048` and `[256, 512, 1024, 2048]`; Qwen3 with `dimensions=512` -> `ModelError` containing `cannot produce 512-d` and `native 1024`; Qwen3 with `1024` or `None` -> `.dimensions == 1024` — `::TestMatryoshkaGuard`.
- [ ] Wire + truncation (mocked `embeddings.create` answering 2048-d vectors for voyage-4-nano): in ALL of the cases above `embeddings.create` is called WITHOUT a `dimensions` kwarg (today's `kwargs["dimensions"] = self._dimensions` line is deleted: `grep -c 'kwargs\["dimensions"\]' apps/memory/src/tree/models/modal_embedding.py` -> 0); with `dimensions=1024` every returned vector has length 1024, L2 norm `1.0 ± 1e-6` and equals `truncate_embedding(raw, 1024)`; with `dimensions=None` the 2048-d vectors are returned unchanged — `::TestClientSideTruncation`.
- [ ] Response-length assertion: voyage-4-nano (`dimensions=1024`) with a mocked response of 1024-d vectors raises `ExtractionError` containing `returned 1024-d vectors` and `native_dimensions 2048`, and returns nothing; Qwen3 with a mocked 2048-d response raises `ExtractionError` containing `returned 2048-d vectors` and `native_dimensions 1024`; a batch where only the LAST item has the wrong length also raises — `::TestResponseLength` (3 tests).
- [ ] Role: voyage-4-nano `embed(["cats"], input_type="query")` sends input `["Represent the query for retrieving supporting documents: cats"]`; `"document"` -> `["Represent the document for retrieval: cats"]`; `None` -> `["cats"]`; Qwen3 `"document"` -> `["cats"]`, `"query"` -> the text prefixed with the catalog `query_prompt` — `::TestRolePrompts`.
- [ ] `_build_embedding_model(EmbeddingConfig(provider="modal", model="voyageai/voyage-4-nano", dimensions=512))` constructs the client with `dimensions=512` and the bearer from settings; importing `tree.models.get_model` does not import `modal` (`'modal' not in sys.modules`, subprocess) — `tests/unit/models/test_get_model.py::TestModalBranch`.
- [ ] `Settings` field set has no `modal_embedding_api_key` — `tests/unit/config/test_settings_credentials_only.py`.
- [ ] `ci.yml` and `cd.yml` each mock the two proxy-token vars and neither mentions the old key.
- [ ] README env table lists `MODAL_PROXY_TOKEN_ID` and `MODAL_PROXY_TOKEN_SECRET`; the Modal section shows the three `MODEL=` commands, names `SERVING=endpoint|sglang|vllm` and the ladder order endpoint -> sglang -> vllm, and contains no `generate-secret-key` step.
- [ ] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green.

## User Stories

### Story: Operator points search at the Dedicated endpoint
1. After `make memory-deploy-embedding-model MODEL=Qwen/Qwen3-Embedding-0.6B`, sets `models.search_embedding: {provider: modal, model: Qwen/Qwen3-Embedding-0.6B, dimensions: 1024}`.
2. First `embed()` logs `ModalEmbeddingModel ready: app=ep-qwen3-embedding-0-6b server=Server served_model=Qwen/Qwen3-Embedding-0.6B native=1024 dimensions=1024 url=https://…/v1`, warms `/health` with the bearer, and returns 1024-d vectors.

### Story: Operator points search at voyage-4-nano and the 1024-d index stays safe
1. After `make memory-deploy-embedding-model MODEL=voyageai/voyage-4-nano`, sets `models.search_embedding: {provider: modal, model: voyageai/voyage-4-nano, dimensions: 1024}`.
2. First `embed()` logs `ModalEmbeddingModel ready: app=ep-voyage-4-nano server=Server served_model=voyageai/voyage-4-nano native=2048 dimensions=1024 (truncated client-side) url=https://…/v1`.
3. The server answers 2048 floats per text; the caller receives 1024-d unit vectors — the same ones `make memory-deploy-embedding-model-test` checked under `sanity@1024`.

### Story: The Embedding catalog is wrong about a model and nothing gets written
1. Someone edits the voyage-4-nano entry back to `native_dimensions: 1024` (it was derived from `hidden_size` once already).
2. The first `embed()` raises `ExtractionError: voyageai/voyage-4-nano returned 2048-d vectors but its Embedding catalog entry says native_dimensions 1024 — fix modal.embedding_models (…). No vector was returned.`
3. No row in `memory` carries a 2048-d vector.

### Story: Operator moved a model down the ladder and the client did not notice
1. Stops the Qwen3 endpoint, runs `make memory-deploy-embedding-model MODEL=Qwen/Qwen3-Embedding-0.6B SERVING=sglang`, writes `serving: sglang` into the YAML.
2. The next process start resolves the same `ep-qwen3-embedding-0-6b` / `Server` and embeds — no client setting changed.

### Story: Operator forgot the proxy token
1. `.env` lacks `MODAL_PROXY_TOKEN_SECRET`.
2. Building the model fails immediately: `Modal proxy token is required. Set MODAL_PROXY_TOKEN_ID and MODAL_PROXY_TOKEN_SECRET.` — no network call was made.

### Story: Operator asks a model for a size it cannot produce
1. `models.search_embedding: {provider: modal, model: Qwen/Qwen3-Embedding-0.6B, dimensions: 512}`.
2. Boot fails: `Qwen/Qwen3-Embedding-0.6B cannot produce 512-d vectors: native 1024, matryoshka_dimensions []. …`.

### Story: The model was stopped
1. `make memory-deploy-embedding-model-stop MODEL=voyageai/voyage-4-nano`, then a query runs.
2. `ModelError: Failed to resolve Modal server ep-voyage-4-nano/Server. Is the model deployed (…)? Run: make memory-deploy-embedding-model MODEL=voyageai/voyage-4-nano`.

### Story: A question is embedded with the right prompt
1. `search_memory("how does sharding work?")` with `provider: modal, model: voyageai/voyage-4-nano`.
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

### [PA] 2026-09-19 18:23 — Re-grooming (plan edit: Dedicated endpoints first)

**What changed and why**
- The client must work against a Modal Dedicated endpoint as well as our two fallback scripts. Design goal: the Serving path is INVISIBLE to the client — same app name (`ep-<endpoint_name>`), same class (`Server`, was `EmbeddingServer`), same Proxy token header (verified in Modal's endpoints guide: identical to ADR-009 §4). A test pins that the client never reads `entry.serving`.
- URL lookup, authenticated health and served-model discovery moved to #139's `tree.models.modal_server` (the shared smoke test needs the same three things); the client deletes its own copies and keeps only catalog lookup, the Matryoshka guard, prompts and the OpenAI call.
- NEW: `model=` on the wire is the id discovered from `/v1/models` — a managed recipe, not us, names the served model.
- README section now tells the three-path story and the ladder; the default example model is the endpoint seed (Qwen3). Auth, Matryoshka guard and client-side prompts are unchanged.
- If #141 proves H1 false, the fix lands in `resolve_server_url` (#139's module), not in this client.

**Dependencies**
- #135 — `input_type`. #138 — catalog helpers, `EMBEDDING_SERVER_NAME = "Server"`. #139 — token settings, `modal_proxy_bearer`, `modal>=1.5.5`, `modal_server` helpers.

**User stories**
- 6 stories: endpoint happy path, ladder move invisible to the client, missing token, impossible dimension, stopped model, query prompt.

Ready for implementation.

### [PA] 2026-09-19 22:11 — Re-grooming (voyage-4-nano is natively 2048-d)

**What changed and why**
- FACT CORRECTION (Tester, #138 QA): voyage-4-nano's native served width is 2048 (1024->2048 linear head), not 1024; #138's catalog now says so. The approved criterion "voyage with `dimensions=1024` sends NO kwarg and is native" was therefore false: 1024 is a truncation for this model, and it is the size the memory uses by default (`EmbeddingConfig.dimensions = 1024`, the mongot index width).
- DECISION — supersedes the first entry's "`dimensions=` is sent only when it differs from native": the client NEVER sends `dimensions`; it requests native width and truncates + L2-renormalises client-side with #139's `truncate_embedding`. Chosen over "server-side, client-side as fallback" because it is the least mechanism that cannot go wrong per path: vLLM 400s on `dimensions` unless the HF config is flagged Matryoshka (voyage-4-nano's is not — that would mean merging `is_matryoshka` into #138's seed `--hf-overrides`), a managed recipe has no such flag, SGLang is unproven. One pure function, identical on all three Serving paths, keeps the client path-blind and needs no change to #138's seed. Mathematically identical to server-side MRL truncation (slice, then L2-normalise).
- NEW guard, the lesson of #138: the client asserts on every response that the wire length equals `native_dimensions` and that what it returns equals the effective `dimensions`, raising `ExtractionError`. The catalog was wrong about a model once (`hidden_size` read instead of the projection head's `num_labels`); with this assertion a wrong entry fails loudly instead of writing 2048-d vectors against a 1024-d index.
- The `ready` log line now shows `native=` and `dimensions=`. #141 records the wire length with and without `dimensions` on every path — evidence for a future server-side upgrade, not a gate for this task.

**Dependencies**
- #139 additionally provides `truncate_embedding`.

**User stories**
- 8 stories: the previous 6 + voyage-4-nano truncated to the 1024-d index + a wrong catalog entry caught before any write.

Ready for implementation.
