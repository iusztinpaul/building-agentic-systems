---
id: 140-modal-client-catalog-and-proxy-token-auth
status: done
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

- [x] `grep -rIn "MODAL_EMBEDDING_API_KEY\|modal_embedding_api_key\|vllm-embedding-api-key" . --exclude-dir=.git --exclude-dir=.venv --exclude-dir=node_modules --exclude-dir=done --exclude=".env*"` -> 0 matches outside `tasks/`, `docs/adrs/009_*` and `docs/glossary.md:39` — the **Proxy token** entry *documents* the retirement ("replaces the engine-level `--api-key` / `MODAL_EMBEDDING_API_KEY` … (all retired)"); PA-owned and read-only for SWE, so flagged rather than edited (see the SWE log).
- [x] `ModalEmbeddingModel(proxy_token="", model="voyageai/voyage-4-nano")` raises `ModelError` naming both env vars; `model="BAAI/bge-m3"` raises the catalog error listing both ids — `tests/unit/models/test_modal_embedding.py::TestInit`.
- [x] First `embed()` awaits `resolve_server_url` once with the catalog entry, builds `AsyncOpenAI` with `base_url` ending in exactly one `/v1`, and a second `embed()` resolves nothing again; a `ModelError` from `resolve_server_url` propagates unchanged (message contains `make memory-deploy-embedding-model MODEL=voyageai/voyage-4-nano`) — `::TestUrlResolution`.
- [x] The SAME assertions hold for the `endpoint` seed and the `vllm` seed — `::TestUrlResolution::test_serving_path_is_invisible_to_the_client` (parametrised over both `repo_id`s; `grep -c "\.serving" apps/memory/src/tree/models/modal_embedding.py` -> 0).
- [x] `AsyncOpenAI` is built with `api_key="wk-1.ws-2"`; `wait_until_healthy` is awaited with bearer `wk-1.ws-2` and `health_timeout`; its `ModelError` (401) and `ExtractionError` (503) propagate unchanged — `::TestProxyAuth`.
- [x] With `served_model_id` answering `"served/other-name"`, `embeddings.create` is called with `model="served/other-name"` while Opik usage is recorded under `voyageai/voyage-4-nano` — `::TestServedModel`.
- [x] Guard: voyage-4-nano with `dimensions=None` or `2048` -> `.dimensions == 2048`; `dimensions=1024` -> `.dimensions == 1024`; `dimensions=512` -> `.dimensions == 512`; `dimensions=768` -> `ModelError` containing `cannot produce 768-d`, `native 2048` and `[256, 512, 1024, 2048]`; Qwen3 with `dimensions=512` -> `ModelError` containing `cannot produce 512-d` and `native 1024`; Qwen3 with `1024` or `None` -> `.dimensions == 1024` — `::TestMatryoshkaGuard`.
- [x] Wire + truncation (mocked `embeddings.create` answering 2048-d vectors for voyage-4-nano): in ALL of the cases above `embeddings.create` is called WITHOUT a `dimensions` kwarg (today's `kwargs["dimensions"] = self._dimensions` line is deleted: `grep -c 'kwargs\["dimensions"\]' apps/memory/src/tree/models/modal_embedding.py` -> 0); with `dimensions=1024` every returned vector has length 1024, L2 norm `1.0 ± 1e-6` and equals `truncate_embedding(raw, 1024)`; with `dimensions=None` the 2048-d vectors are returned unchanged — `::TestClientSideTruncation`.
- [x] Response-length assertion: voyage-4-nano (`dimensions=1024`) with a mocked response of 1024-d vectors raises `ExtractionError` containing `returned 1024-d vectors` and `native_dimensions 2048`, and returns nothing; Qwen3 with a mocked 2048-d response raises `ExtractionError` containing `returned 2048-d vectors` and `native_dimensions 1024`; a batch where only the LAST item has the wrong length also raises — `::TestResponseLength` (3 tests).
- [x] Role: voyage-4-nano `embed(["cats"], input_type="query")` sends input `["Represent the query for retrieving supporting documents: cats"]`; `"document"` -> `["Represent the document for retrieval: cats"]`; `None` -> `["cats"]`; Qwen3 `"document"` -> `["cats"]`, `"query"` -> the text prefixed with the catalog `query_prompt` — `::TestRolePrompts`.
- [x] `_build_embedding_model(EmbeddingConfig(provider="modal", model="voyageai/voyage-4-nano", dimensions=512))` constructs the client with `dimensions=512` and the bearer from settings; importing `tree.models.get_model` does not import `modal` (`'modal' not in sys.modules`, subprocess) — `tests/unit/models/test_get_model.py::TestModalBranch`.
- [x] `Settings` field set has no `modal_embedding_api_key` — `tests/unit/config/test_settings_credentials_only.py`.
- [x] `ci.yml` and `cd.yml` each mock the two proxy-token vars and neither mentions the old key.
- [x] README env table lists `MODAL_PROXY_TOKEN_ID` and `MODAL_PROXY_TOKEN_SECRET`; the Modal section shows the three `MODEL=` commands, names `SERVING=endpoint|sglang|vllm` and the ladder order endpoint -> sglang -> vllm, and contains no `generate-secret-key` step.
- [x] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green.

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

### [SWE] 2026-09-19 23:20 — Implementation

**Files modified**
- `apps/memory/src/tree/models/modal_embedding.py` — rewritten: catalog-driven client, proxy-token auth, Matryoshka guard, client-side truncation, response-width assertions, role prompts. Deleted `_DEFAULT_APP_NAME` / `_DEFAULT_FUNCTION_NAME` / `_MODEL_NATIVE_DIMENSIONS` / `_resolve_web_url` / `_health_check`.
- `apps/memory/src/tree/models/get_model.py` — modal branch now passes `proxy_token=modal_proxy_bearer()` and `cfg.dimensions`; lazy import + cold-boot note kept.
- `apps/memory/src/tree/config/settings.py` — `modal_embedding_api_key` removed.
- `apps/memory/src/tree/models/modal_catalog.py` — carry-over (a): `truncate_embedding` rejects `dimensions <= 0`.
- `apps/memory/src/tree/config/app_config.py` — carry-over (b): `revision` gets `_REVISION_PATTERN = ^[\w.\-/]+$`.
- `apps/memory/scripts/modal_embedding_model.py` — carry-over (c): a missing `modal` executable is ONE log line naming the extra + exit 2, never the argv.
- `.env.example`, `.github/workflows/ci.yml`, `.github/workflows/cd.yml`, root `Makefile`, root `README.md`, `apps/memory/README.md` — `MODAL_EMBEDDING_API_KEY` retired; the two proxy-token vars, the three `MODEL=` commands, the `SERVING=` ladder and the dashboard-only note documented.
- Tests: `tests/unit/models/test_modal_embedding.py` (rewritten), `test_get_model.py`, `test_dimensions.py`, `test_modal_catalog.py`, `test_observability_instrumentation.py`, `tests/unit/config/test_settings_credentials_only.py`, `tests/unit/config/test_app_config.py`, `tests/unit/scripts/test_modal_embedding_model_script.py`.

**Tests**
- Unit: 3359 passing, 0 failing (`make memory-tests`, local env). Modal-related subset: 327 passing.
- Integration: N/A — the repo has no integration suite (AGENTS.md); no live Modal call was made (task 141 owns those).

**Acceptance criteria**
- [x] repo-wide grep — 0 matches outside `tasks/` and `docs/adrs/009_*` EXCEPT `docs/glossary.md:39`, which *documents the retirement* ("replaces the engine-level `--api-key` / `MODAL_EMBEDDING_API_KEY` … (all retired)"). The glossary is PA-owned and read-only for SWE, and the content is correct — flagged rather than edited.
- [x] Construction failures — `::TestInit` (empty token names both env vars; `BAAI/bge-m3` lists both catalog ids; construction awaits nothing).
- [x] URL resolution, one `/v1`, no second resolve, `ModelError` passthrough — `::TestUrlResolution`.
- [x] Serving path invisible — `::TestUrlResolution::test_serving_path_is_invisible_to_the_client` (both seeds) + `grep -c "\.serving" …/modal_embedding.py` -> 0.
- [x] Proxy auth — `::TestProxyAuth` (api_key, bearer + timeout on `wait_until_healthy`, 401 `ModelError` / 503 `ExtractionError` unchanged).
- [x] Discovered served id vs Opik repo id — `::TestServedModel`.
- [x] Matryoshka guard — `::TestMatryoshkaGuard` (all 6 accepted widths + both refusals; substring asserts, not `match=` regexes, so `[256, 512, 1024, 2048]` is checked literally).
- [x] No `dimensions` on the wire + truncation — `::TestClientSideTruncation`; `grep -c 'kwargs\["dimensions"\]'` -> 0.
- [x] Response-width assertion — `::TestResponseLength` (3 tests: too narrow, too wide, last-item-only).
- [x] Role prompts — `::TestRolePrompts`.
- [x] Factory + lazy import — `tests/unit/models/test_get_model.py::TestModalBranch` (dimensions=512 and the bearer; `'modal' not in sys.modules` in a subprocess).
- [x] `Settings` surface — `tests/unit/config/test_settings_credentials_only.py`.
- [x] `ci.yml` / `cd.yml` mock the two proxy vars only.
- [x] README env rows + rewritten Modal section.
- [x] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green.

**Evidence**

```
$ make memory-tests
3359 passed in 46.55s

$ make memory-format-check && make memory-lint-check && make pre-commit
300 files already formatted / All checks passed! / prettier, ruff check, ruff format, biome: Passed

$ grep -c "\.serving" apps/memory/src/tree/models/modal_embedding.py      -> 0
$ grep -c 'kwargs\["dimensions"\]' apps/memory/src/tree/models/modal_embedding.py -> 0
```

Secret-safety proof — the Modal test files run with the settings-RELOAD file FIRST (it re-imports `tree.config.settings`, so a test patching the wrong binding would read the real `.env` token):

```
$ PYTEST_ADDOPTS="tests/unit/config/test_settings_credentials_only.py tests/unit/models/test_modal_embedding.py \
    tests/unit/models/test_modal_catalog.py tests/unit/models/test_modal_server.py tests/unit/models/test_get_model.py \
    tests/unit/models/test_dimensions.py tests/unit/models/test_observability_instrumentation.py \
    tests/unit/scripts/test_modal_embedding_model_script.py tests/unit/config/test_app_config.py -v" make memory-tests
first test executed: tests/unit/config/test_settings_credentials_only.py::…::test_settings_does_not_expose_dedup
327 passed in 7.04s
secret-shaped scan of the captured output (41 567 chars): wk-…: 0, ws-…: 0, hf_…: 0, AIza…: 0, pa-…: 0, sk-…: 0
```

End-to-end (step 7) — the REAL client, the real factory branch, the real `wait_until_healthy` / `served_model_id` over aiohttp and a real `AsyncOpenAI` request against a LOCAL stub server on 127.0.0.1:8931 that demands `Authorization: Bearer wk-1.ws-2` and answers 2048-d vectors. Only `resolve_server_url` (the one Modal-SDK call) was patched to the local URL. No Modal endpoint was created, woken or stopped.

```
GET /health 200 ; GET /v1/models 200
ModalEmbeddingModel ready: app=ep-voyage-4-nano server=Server served_model=served/voyage-custom native=2048 dimensions=1024 (truncated client-side) url=http://127.0.0.1:8931/v1
POST /v1/embeddings body keys = ['encoding_format', 'input', 'model']      <- no `dimensions`
  model='served/voyage-custom'                                            <- the DISCOVERED id
  input='Represent the query for retrieving supporting documents: how does sharding work?'
  input='Represent the document for retrieval: Sharding splits a collection across shards by a shard key.'
query vector: len=1024 norm=1.000000 ; doc vectors: len=[1024, 1024]
wrong-catalog guard (entry forced to native_dimensions: 1024):
  ExtractionError: voyageai/voyage-4-nano returned 2048-d vectors but its Embedding catalog entry says
  native_dimensions 1024 — fix modal.embedding_models (…). No vector was returned.
```

(The cosine numbers from that run are meaningless — the stub returns synthetic sinusoids, not model output. Ranking quality is #141's live smoke test.)

**Notes**
- FOR THE HUMAN: `.env` / `.env.prod` still carry the stale `MODAL_EMBEDDING_API_KEY=` line. Agents do not edit those files; `extra="ignore"` makes it harmless, but delete it by hand. #141 also notes the orphaned `vllm-embedding-api-key` Modal secret.
- `docs/glossary.md:39` intentionally still names the retired key (see the first AC above) — PA-owned, correct as written.
- Carry-overs from #139's QA, each with its own unit test: (a) `truncate_embedding` rejects `dimensions <= 0` (`test_modal_catalog.py::TestTruncateEmbedding::test_a_non_positive_width_is_refused`, parametrised 0 / -1 / -1024); (b) `revision` charset (`test_app_config.py` rejection cases `revision-with-whitespace`, `revision-with-shell-punctuation`, plus an acceptance case for sha / branch / tag / `refs/pr/3`); (c) missing `modal` CLI (`test_modal_embedding_model_script.py::TestHfToken::test_a_missing_modal_cli_says_how_to_install_it_without_the_argv` — asserts the install line, exit 2, and that the fake HF token and any second unredacted argv copy are absent).
- Ordering inside `_ensure_initialised` is resolve -> health -> `/v1/models` -> build client -> ONE `ready` log line. Story 2 narrates "logs … warms /health"; the log line carries `served_model=`, which cannot be known before the server is healthy, so the single line is emitted last. No AC pins the order.
- A response-width assertion runs on EVERY vector of EVERY batch (not a sample). At the memory's batch sizes that is O(n) length checks — negligible next to the HTTP round trip; if a future batch path ever hot-loops this, the check is one function (`_checked_vectors`).
- `served_model_id` failure is best-effort by design (#139): it falls back to the catalog `repo_id` with a WARNING, so a recipe that hides `/v1/models` still embeds.
- Sources consulted (tech-docs): Modal "Proxy Tokens" guide (`https://modal.com/docs/guide/webhook-proxy-auth`, fetched 2026-09-19) — Endpoints/Servers require auth by default; the two halves joined by `.` in `Authorization: Bearer` are "the same scheme the OpenAI API uses, so the combined value can be used as the API key in any OpenAI-compatible client". openai-python README (`raw.githubusercontent.com/openai/openai-python/main/README.md`) — `AsyncOpenAI(base_url=…, api_key=…)` against a non-OpenAI OpenAI-compatible server. context7 MCP tools were not exposed in this session, so both facts came from the primary docs by `curl`.
- NOT RUN — no live Modal call of any kind (`modal endpoint create/stop`, `modal deploy`, `make memory-deploy-embedding-model*`, or any request to the human's real `/health`, `/v1/models`, `/v1/embeddings`): #141 owns every live call, and waking a Dedicated endpoint bills a GPU.

### [Tester] 2026-09-20 00:00 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check`, `make memory-lint-check`, `make pre-commit` all green)
- Unit tests: 3359 passed / 0 failed (`make memory-tests`, local env)
- Modal-related subset (settings-reload file first, per SWE's ordering): 327 passed / 0 failed, run twice — once via bare `uv run pytest` (no `.env`) and once via `PYTEST_ADDOPTS="<files> -v" make memory-tests` (real `.env` exported, the stronger form). Both green.
- Warnings: 0

All 13 non-`[HUMAN]` acceptance criteria verified directly (evidence below) and confirmed PASS. The FAIL below comes from the e2e adversarial pass, not from an unmet AC — no checkbox needs to move.

**AC-1 ruling (as requested)**
The task's own AC-1 text (line 87) already lists `docs/glossary.md:39` in its exemption set alongside `tasks/` and `docs/adrs/009_*`. Re-ran the literal grep: the only match outside those three locations is `docs/glossary.md:39`, and it *documents* the retirement in the same way the ADR does. No live reader or writer of `MODAL_EMBEDDING_API_KEY` / `modal_embedding_api_key` / `vllm-embedding-api-key` remains anywhere in code, config, CI or docs outside that already-exempted set. AC-1 is satisfied in both letter and intent; no edit needed, not a FAIL.

**E2E adversarial pass**
Stood up a local `aiohttp.web` stub on `127.0.0.1` imitating `/health`, `/v1/models`, `/v1/embeddings` with proxy-token checking; patched only `tree.models.modal_embedding.resolve_server_url` to point at it (`wait_until_healthy` / `served_model_id` ran for real over loopback aiohttp, `AsyncOpenAI` ran for real against the stub). `OPIK_TRACK_DISABLE=true` set before import to keep telemetry local. No Modal CLI/SDK call, no request to any live endpoint.

- Happy path: construct `ModalEmbeddingModel(proxy_token=<fake>, model="voyageai/voyage-4-nano", dimensions=1024)`, `await model.embed(["cats"], input_type="query")` against the stub (native 2048-d response) → resolved once, healthy, discovered `served/custom-name`, returned one 1024-d unit vector equal to `truncate_embedding(raw, 1024)`, request body had no `dimensions` key. PASS.
- Break path 1 (wrong-width safety, 10 sub-cases): stub answering 1024-d for voyage-4-nano (catalog says 2048) → `ExtractionError` naming both widths, nothing returned; one bad item at end of a 3-item batch → same; `dimensions=1024` truncation → exactly 1024-d, L2 norm 1.0 ± 1e-6, equals `truncate_embedding` byte-for-byte; `dimensions=None`/`2048` → native vector returned unchanged; Qwen3 `dimensions=512` → `ModelError` at construction; `dimensions` in `{0, -1, -2048}` → all rejected at construction; `dimensions` key absent from every wire request across `{None, 1024, 2048}` × `{None, query, document}` → confirmed absent in every captured body. All PASS.
- Break path 2 (auth): 401 from `/health` → `ModelError` naming `MODAL_PROXY_TOKEN_ID` and `MODAL_PROXY_TOKEN_SECRET` (exact type, not `ExtractionError`); 503 → `ExtractionError`; fake bearer never appeared in the exception message, in `repr(model)`, or in captured logs at DEBUG across `openai`/`httpx`/`aiohttp`/the two `tree.models.*` loggers. All PASS.
- Break path 3 (`/v1/models` discovery): empty `data: []` → falls back to `repo_id`, no crash; multiple models listed → first one used on the wire; non-JSON body → falls back without crashing; 404 → falls back. All PASS.
- Break path 4 (role prompts): voyage query/document prefixes match the catalog bytes exactly; Qwen3 query prompt ends `Query:` with no added space and contains a real `\n`; `None` role leaves text unchanged; prompt not re-applied across two sequential calls on the same instance. PASS — **except** `embed([])`, see FAIL below.
- Break path 5 (concurrency): `asyncio.gather(model.embed(["a"]), model.embed(["b"]), model.embed(["c"]))` on one fresh instance → **FAIL**, see below. Separately: a first `embed()` that fails at the health check (503) correctly leaves `_client` unset — a retried call re-runs the health check (`health_calls` went 1 → 2, not skipped). PASS on that half.
- Cold boot: `import tree.models.get_model` and `import tree.mcp.server` in fresh interpreters both leave `'modal' not in sys.modules`. PASS.
- Retirement grep, CI/CD YAML validity, README wording: re-verified directly, see Acceptance Criteria evidence.
- #139 carry-overs spot-checked directly (not just via the unit tests): `truncate_embedding` rejects `0`/`-1`/`-1024`; `ModalEmbeddingModelConfig(revision=...)` rejects `"main branch"` and `"main;rm -rf /"`, accepts `"main"` / `"refs/pr/3"` / `"v1.2.0"` / a 40-char sha; `TestHfToken::test_a_missing_modal_cli_says_how_to_install_it_without_the_argv` passes (exit 2, no argv leak). All PASS.

**FAIL 1 — `_ensure_initialised` races under concurrent first `embed()` calls**

`apps/memory/src/tree/models/modal_embedding.py:154-165`:
```python
if self._client is not None:
    return
url = await resolve_server_url(self._entry)
await wait_until_healthy(url, self._proxy_token, self._health_timeout)
...
self._client = AsyncOpenAI(base_url=f"{url}/v1", api_key=self._proxy_token)
```
This is a classic check-then-act race with no `asyncio.Lock`. Under `asyncio.gather(model.embed(["a"]), model.embed(["b"]), model.embed(["c"]))` on one fresh instance against the stub, every one of the 3 coroutines runs past the `if self._client is not None` check before any of them awaits far enough to set `self._client` — so all 3 independently resolve the URL, run their own `wait_until_healthy`, and discover the served model id. Observed: `health_calls == 3`, `models_calls == 3` (expected 1 each). The docstring's claim "Called once on the first `embed()`; later calls are no-ops" is false under concurrency.

Why this matters beyond a nit: `wait_until_healthy` is a long-held request (up to `health_timeout`, default 300s) specifically designed to hold the connection while a scaled-to-zero GPU container wakes. N concurrent first-`embed()` calls on a shared model instance means N concurrent wake-holding requests against a billed endpoint, plus N `AsyncOpenAI` clients built and immediately orphaned (unclosed httpx connection pools — only the last one survives as `self._client`). `dispatch_concurrency` in the batching path defaults to 1 (sequential today, confirmed at `apps/memory/src/tree/memory/embedding_text.py:128` / `configs/default.yaml:86`), so this doesn't fire from `embed_in_batches` today — but it is directly reachable the moment two callers share one model instance and call `embed()` concurrently (concurrent MCP tool calls against a cached/shared model, or `dispatch_concurrency` raised above 1 later), and no AC or existing test covers it (`test_a_second_embed_resolves_nothing_again` is sequential, not concurrent).

Fix shape: guard `_ensure_initialised` with an `asyncio.Lock()` created in `__init__` (safe under asyncio on the pinned Python — no loop binding issue), and re-check `self._client is not None` *inside* the lock before doing the work, so a failed initialisation still leaves `_client` unset for the next caller to retry (preserves the already-correct 503-then-retry behaviour verified above).

**FAIL 2 — `embed([])` raises instead of returning `[]`, and the unit test asserting otherwise is unfaithful to the real dependency**

`test_modal_embedding.py::TestEmbedFailure::test_an_empty_batch_returns_an_empty_list` asserts `await model.embed([]) == []` and passes — but only because the test's `_Embeddings.create` fake ignores the real `openai` SDK's response-parsing contract. Against the REAL `AsyncOpenAI` client (stub server, no mocking of `embeddings.create`), `model.embed([])` sends `input=[]`, the server legitimately answers `{"data": []}`, and the OpenAI SDK's own post-parser (`openai/resources/embeddings.py:238-241`, hit because the client never sets `encoding_format` so the SDK's default base64 post-parser runs) raises `ValueError("No embedding data received")` on an empty `data` list — which `modal_embedding.embed()` wraps into `ExtractionError("Embedding call failed: No embedding data received")` instead of returning `[]`.

This is a real contract inconsistency, not just an edge case: the sibling implementation `VoyageTextEmbeddingModel.embed` (`apps/memory/src/tree/models/voyage_embedding.py:228-229`) explicitly guards `if not texts: return []` at the top of `embed()` with a comment explaining why (an empty call must not occupy a rate-limit slot). `ModalEmbeddingModel.embed` has no equivalent guard, so the two implementations of the same `BaseEmbeddingModel.embed` contract disagree on empty input — Modal's silently regresses to an exception the moment a caller (today none does, but nothing prevents one) passes `[]`.

Fix shape (either, or both): (a) minimal — `if not texts: return []` at the top of `ModalEmbeddingModel.embed`, matching Voyage; (b) also pass `encoding_format="float"` explicitly on the `embeddings.create` call — this both disables the SDK's empty-data-raising post-parser as a side effect AND closes a parity gap this QA pass surfaced: `tree.models.modal_server._embed` (the shared smoke-test path, #139) sends `"encoding_format": "float"` on the wire, while the client here lets the SDK default to `"encoding_format": "base64"` (confirmed in the DEBUG request log: `'encoding_format': 'base64'`). Story 3 claims the caller receives "the same [vectors] the smoke test checked under `sanity@1024`" — today the two paths exercise different response encodings on the wire, which is at minimum worth a decision (same encoding on both paths, or an explicit note that base64 vs float round-trips identically). Either way, `test_an_empty_batch_returns_an_empty_list` needs its fake tightened (or a real-client integration point added) so it cannot pass against a behavior the real dependency does not have.

**Acceptance criteria**
- [x] PASS — repo-wide grep 0 matches outside `tasks/`, `docs/adrs/009_*`, `docs/glossary.md:39` — re-ran `grep -rIn "MODAL_EMBEDDING_API_KEY\|modal_embedding_api_key\|vllm-embedding-api-key" . --exclude-dir=.git --exclude-dir=.venv --exclude-dir=node_modules --exclude-dir=done --exclude=".env*"`; only matches outside those paths are the already-exempted `docs/glossary.md:39`.
- [x] PASS — empty token / unknown model construction failures — `tests/unit/models/test_modal_embedding.py::TestInit` (3 tests) pass; independently reproduced both outside pytest.
- [x] PASS — URL resolution once, one `/v1`, `ModelError` passthrough — `::TestUrlResolution` (3 tests) pass; independently reproduced against the stub (resolved once across 2 sequential `embed()` calls).
- [x] PASS — serving path invisible for both seeds — `::TestUrlResolution::test_serving_path_is_invisible_to_the_client` (parametrised, 2 cases) pass; `grep -c "\.serving" apps/memory/src/tree/models/modal_embedding.py` → `0`.
- [x] PASS — proxy auth (`api_key`, bearer+timeout on `wait_until_healthy`, 401/503 typed correctly) — `::TestProxyAuth` (4 tests) pass; independently reproduced against the stub (401 → `ModelError` naming both env vars, 503 → `ExtractionError`, fake token absent from logs/repr).
- [x] PASS — discovered served id on the wire, `repo_id` on Opik — `::TestServedModel` passes; independently reproduced (`served/custom-name` on wire, `voyageai/voyage-4-nano` implied by the catalog id used in usage recording per the unit test).
- [x] PASS — Matryoshka guard, all 6 widths + 2 refusals, literal substrings — `::TestMatryoshkaGuard` (8 parametrised cases) pass; independently reproduced Qwen3@512 and voyage@768.
- [x] PASS — no `dimensions` kwarg ever, truncation correctness — `::TestClientSideTruncation` (6 cases) pass; `grep -c 'kwargs\["dimensions"\]' apps/memory/src/tree/models/modal_embedding.py` → `0`; independently reproduced: 0/0/0 `dimensions` keys across 9 role×size combinations against the real stub.
- [x] PASS — response-width assertion, 3 cases incl. last-item-only — `::TestResponseLength` (3 tests) pass; independently reproduced (narrow, and mixed-batch-last-item-wrong) against the real stub.
- [x] PASS — role prompts — `::TestRolePrompts` (4 tests) pass; independently reproduced exact bytes for voyage query/document and the Qwen3 `\nQuery:` prompt.
- [x] PASS — factory passes `dimensions` + bearer, lazy import — `tests/unit/models/test_get_model.py::TestModalBranch` (3 tests) pass; independently reproduced the `'modal' not in sys.modules` check for both `tree.models.get_model` and `tree.mcp.server` in fresh interpreters.
- [x] PASS — `Settings` field set has no `modal_embedding_api_key` — `tests/unit/config/test_settings_credentials_only.py` passes (run both with and without real `.env` loaded).
- [x] PASS — `ci.yml` / `cd.yml` mock the two proxy vars, neither mentions the old key, both parse as valid YAML — re-verified via `python3 -c "yaml.safe_load(...)"` on both files and a direct diff read.
- [x] PASS — README env table + Modal section — re-read `apps/memory/README.md` and root `README.md` diffs directly: lists `MODAL_PROXY_TOKEN_ID`/`MODAL_PROXY_TOKEN_SECRET`, three `MODEL=` commands, `SERVING=endpoint|sglang|vllm` ladder in the stated order, and `grep -n "generate-secret-key"` on both READMEs returns nothing.
- [x] PASS — `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green — reran independently: 3359 passed, 0 failed; format/lint/pre-commit all green.

**Evidence**
```
$ make memory-tests
============================ 3359 passed in 45.02s =============================

$ grep -c "\.serving" apps/memory/src/tree/models/modal_embedding.py
0
$ grep -c 'kwargs\["dimensions"\]' apps/memory/src/tree/models/modal_embedding.py
0

$ PYTEST_ADDOPTS="tests/unit/config/test_settings_credentials_only.py tests/unit/models/test_modal_embedding.py \
    tests/unit/models/test_modal_catalog.py tests/unit/models/test_modal_server.py tests/unit/models/test_get_model.py \
    tests/unit/models/test_dimensions.py tests/unit/models/test_observability_instrumentation.py \
    tests/unit/scripts/test_modal_embedding_model_script.py tests/unit/config/test_app_config.py -v" make memory-tests
327 passed in 7.06s
secret-shaped scan of the captured output (41567 chars, real .env exported by make): wk-: 0, ws-: 0, hf_: 0, AIza: 0, sk-: 0, pa-: 0

$ uv run python -c "import sys, tree.mcp.server; assert 'modal' not in sys.modules"
OK
$ uv run python -c "import sys, tree.models.get_model; assert 'modal' not in sys.modules"
OK

adversarial stub run (OPIK_TRACK_DISABLE=true, no live Modal call):
PASS: 29 checks (wrong-width safety x10, auth x5, /v1/models discovery x4, role prompts x5, cold-init/retry x2, concurrency-success x1, ...)
FAIL: 3 checks
  - 4e-empty-texts-list-behavior :: embed([]) RAISED instead of returning []: ExtractionError: Embedding call failed: No embedding data received
  - 5a-concurrent-first-embeds-single-health-call :: health_calls=3 (expected 1)
  - 5a-concurrent-first-embeds-single-models-call :: models_calls=3 (expected 1)
```

**Other issues found**
- None beyond the two FAILs above; no secrets observed anywhere (real-`.env` run included), no security regressions, no `print()` in touched files, all touched signatures typed.

**VERDICT: FAIL**

Both failures are from the e2e adversarial pass (not from an unmet AC — every AC checkbox above is independently verified PASS). Two fixes needed before re-review: (1) an `asyncio.Lock` around `_ensure_initialised` so concurrent first `embed()` calls do not each independently wake/resolve/discover; (2) an empty-input guard in `ModalEmbeddingModel.embed` (matching `VoyageTextEmbeddingModel`'s `if not texts: return []`) plus a tightened or real-client-backed version of `test_an_empty_batch_returns_an_empty_list` so it cannot pass against behavior the real `openai` SDK does not exhibit.

### [SWE] 2026-09-20 00:34 — Fixes (QA round 1)

**Files modified**
- `apps/memory/src/tree/models/modal_embedding.py` — (1) `asyncio.Lock` created in `__init__`, `_ensure_initialised` double-checked (`self._client is not None` before AND inside the lock); the served id and the client are now assigned from locals only after every step succeeded, so a failed init leaves nothing half-built. (2) `if not texts: return []` at the TOP of `embed()`, above the initialisation — an empty batch never wakes the endpoint. (3) `encoding_format="float"` sent explicitly on `embeddings.create` (orchestrator decision). Docstrings updated: `_ensure_initialised` no longer claims "later calls are no-ops" (it names the lock and the double-check), `embed` documents the empty-batch short circuit.
- `apps/memory/tests/unit/models/test_modal_embedding.py` — new `wire` fixture: the REAL `AsyncOpenAI` (with `max_retries=0`) over an in-process `httpx.MockTransport`, so the SDK's own body/response handling is exercised instead of bypassed. New `suspending_server` fixture: the three `modal_server` helpers as counting doubles that `await asyncio.sleep(0)` — an `AsyncMock` never suspends, so a `gather` over it runs each coroutine to completion in turn and the race cannot appear at all. New `TestWireContract` (3 tests) and `TestConcurrentInitialisation` (3 tests); `TestEmbedFailure::test_an_empty_batch_returns_an_empty_list` deleted and replaced by `TestWireContract::test_an_empty_batch_returns_early_without_waking_the_endpoint`; `TestClientSideTruncation::test_no_dimensions_parameter_is_ever_sent` now also pins `encoding_format == "float"`.

**Tests**
- Unit: 3364 passing, 0 failing, 0 warnings (`make memory-tests`, local env; was 3359 — +6 new, −1 replaced).
- Integration: N/A — the repo has no integration suite.
- Red-first proof: the new tests were run against the UNFIXED source before the edit and reported exactly the two defects (`resolve_calls/health_calls/models_calls == 3`, `embed([])` raising, base64 on the wire). 8 failed / 36 passed pre-fix; 44 passed post-fix.

**Fixes against the Tester's three items**
- FAIL 1 (race) — fixed by the lock; regression tests `TestConcurrentInitialisation::test_three_concurrent_first_embeds_initialise_exactly_once` (3 concurrent first `embed()` → exactly 1 resolve / 1 health / 1 `/v1/models`, all three calls return correct-width vectors, 3 embeddings requests), `::test_a_failed_initialisation_is_retried_in_full` (health 503 → no wire request; the retry re-runs resolve → health → `/v1/models` and succeeds), `::test_every_concurrent_waiter_sees_the_initialisation_failure` (all 3 waiters get the UNWRAPPED `ExtractionError`, zero embeddings requests — nobody falls through with an unbuilt client).
- FAIL 2 (`embed([])`) — fixed by the Voyage-parity guard; the replacement test asserts the stronger property: `resolve_server_url` / `wait_until_healthy` / `served_model_id` are never awaited and the transport sees zero requests. The guard is deliberately independent of `encoding_format`: it short-circuits before the SDK is reached at all, so it cannot regress if the SDK's post-parser behaviour changes.
- Item 3 (`encoding_format`) — applied. `TestWireContract::test_the_body_carries_exactly_model_input_and_float_encoding` pins the parsed JSON body: `set(body) == {"model", "input", "encoding_format"}`, `encoding_format == "float"`, no `dimensions`. A second wire test asserts the Proxy token reaches the wire as `Authorization: Bearer wk-1.ws-2` (stronger than the old constructor-kwarg check).

**Acceptance criteria**
- Unchanged — all 13 non-`[HUMAN]` criteria stay `[x]`; the Tester confirmed no checkbox moves (both FAILs came from the adversarial pass, not from an unmet AC). AC-15 (`make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green) re-verified after the fixes.

**Evidence**

```
$ PYTEST_ADDOPTS="tests/unit/models/test_modal_embedding.py -q" make memory-tests   # BEFORE the source fix
8 failed, 36 passed in 1.30s
  ::TestConcurrentInitialisation::test_three_concurrent_first_embeds_initialise_exactly_once
     assert 3 == 1  (resolve_calls)          <- the race, reproduced in a unit test
  ::TestWireContract::test_an_empty_batch_returns_early_without_waking_the_endpoint
  ::TestWireContract::test_the_body_carries_exactly_model_input_and_float_encoding
  ::TestClientSideTruncation::test_no_dimensions_parameter_is_ever_sent[None|2048|1024|512]

$ PYTEST_ADDOPTS="tests/unit/models/test_modal_embedding.py -q" make memory-tests   # AFTER
44 passed in 0.86s

$ make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check
300 files left unchanged / All checks passed! / 300 files already formatted / All checks passed!

$ make pre-commit
prettier / ruff check / ruff format / biome check (harness): Passed

$ make memory-tests
============================ 3364 passed in 50.39s =============================
```

End-to-end (step 7) — the same shape of harness the Tester used to find the defects: a REAL `aiohttp.web` stub on `127.0.0.1:8932` demanding `Authorization: Bearer <fake>`, the REAL `wait_until_healthy` / `served_model_id` over loopback and the REAL `AsyncOpenAI`; only `resolve_server_url` patched. `OPIK_TRACK_DISABLE=true`. No Modal call, no request to any live endpoint, fake token only.

```
GET /health 200 ; GET /v1/models 200        <- ONE of each, for THREE concurrent first embed() calls
ModalEmbeddingModel ready: app=ep-voyage-4-nano server=Server served_model=served/voyage-custom
  native=2048 dimensions=1024 (truncated client-side) url=http://127.0.0.1:8932/v1
POST /v1/embeddings 200  x3
concurrency: health_calls=1 models_calls=1 embeddings_calls=3 widths=[1024, 1024, 1024] norms=[1.0, 1.0, 1.0]
POST body #1 = {"input": ["<text>"], "model": "served/voyage-custom", "encoding_format": "float"}
body keys = ['encoding_format', 'input', 'model'] ; encoding_format='float' ; dimensions present = False
inputs seen = [['Represent the document for retrieval: b'],
               ['Represent the query for retrieving supporting documents: a'], ['c']]
empty batch -> [] ; stub calls unchanged: True
```

**Notes**
- Why the old unit tests could not have caught the race: `AsyncMock` resolves without ever yielding to the event loop, so `asyncio.gather` runs each `embed()` to completion in turn — the check-then-act window never opens. The new `_SuspendingServer` doubles `await asyncio.sleep(0)` inside the critical section, which is the smallest thing that reschedules; that single line is what makes the test red against the unfixed source.
- The failure path deliberately does NOT cache the failure: waiters 2 and 3 each serially re-run the full initialisation and each fail (health_calls == 3 in the failure case). That preserves the already-verified-PASS "503 then retry re-runs the health check" behaviour — a provisioning endpoint must stay retryable.
- `asyncio.Lock()` is created in `__init__`, which may run outside a loop (`get_model._build_embedding_model` is synchronous). That is safe because the lock binds lazily, on the first `acquire()`, not at construction — 3.10 removed the `loop` *parameter*, not the binding. The instance is therefore bound to whichever loop first initialises it, exactly the lifetime `self._client` and its httpx connection pool already assume, so the lock adds no new failure class. Checked the reuse scenario the Tester's FAIL-1 text raises ("a cached/shared model"): `get_embedding_model()` carries no `lru_cache` or module-level singleton — every call site (`memory/pipeline.py:2230/2243`, `mcp/server.py:167`, `scripts/query_graph.py`) builds a fresh instance, so no instance crosses two `asyncio.run()` loops today.
- The lock is per instance, not per process: two separately built clients still wake the endpoint separately. That is the existing contract (`get_model` builds one client per model config) and out of scope here.
- The `ready` log line now renders inside the lock (no `await` in it, so it delays no waiter) — this keeps it emitted exactly once, with the served id it could only know after `/v1/models`.
- No behaviour changed for the Tester's 29 passing checks: the wire still carries no `dimensions`, both width assertions still run on every vector, prompts and Opik usage recording are untouched.
- NOT RUN — still no live Modal call of any kind, and no request to the human's real `/health`, `/v1/models` or `/v1/embeddings` (#141 owns those; waking a Dedicated endpoint bills a GPU).
- Not committed — waiting on the Tester's re-review.
- SUPERSEDING NOTE (added at commit time, per the Tester's round-2 nit): "every call site builds a fresh instance" above is imprecise — `mcp/server.py:167` builds ONE `ModalEmbeddingModel` in `app_lifespan` at startup and shares it across every tool call, which is safe because the lock binds to the single event loop `mcp.run()` owns for the process lifetime, and is exactly the concurrent-sharing scenario the lock serialises.

### [Tester] 2026-09-20 01:20 — QA round 2 (re-review)

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check`, `make memory-lint-check`, `make pre-commit` all green, reran after every mutation-restore cycle)
- Unit tests: 3364 passed / 0 failed (`make memory-tests`, local env — `make env-status` confirmed `local` before and throughout)
- Modal-related subset (settings-reload file first): 332 passed / 0 failed; secret-shaped scan of the captured pytest output (`wk-`, `ws-`, `hf_`, `AIza`, `sk-`, `pa-`) → 0 for every pattern
- Warnings: 0

**Files re-reviewed**: `apps/memory/src/tree/models/modal_embedding.py` (full re-read), `apps/memory/tests/unit/models/test_modal_embedding.py` (full re-read, 787 lines, new `wire`/`suspending_server` fixtures + `TestWireContract`/`TestConcurrentInitialisation`). `git diff --stat` matches the SWE's round-2 file list exactly; no unrelated files touched.

**E2E adversarial pass (independent harness, not the SWE's)**
Built my own `aiohttp.web` stub on `127.0.0.1` (separate from the SWE's) exercising `/health`, `/v1/models`, `/v1/embeddings`; only `tree.models.modal_embedding.resolve_server_url` patched — `wait_until_healthy`, `served_model_id` and `AsyncOpenAI` all ran for real over loopback. `OPIK_TRACK_DISABLE=true`. No Modal CLI/SDK call, no request to any live endpoint, fake token only (`wk-FAKE1.ws-FAKE2`). 63/63 independent checks passed, 0 failed:

- Happy path: `ModalEmbeddingModel(proxy_token=<fake>, model="voyageai/voyage-4-nano", dimensions=1024)`, `await model.embed(["cats"], input_type="query")` against the stub (2048-d native response) → resolved once, healthy, 1024-d unit vector matching `truncate_embedding`. PASS.
- Break path 1 (concurrency, FAIL-1 regression target): `asyncio.gather` of 3 AND 10 concurrent first `embed()` calls on one fresh instance → `health_calls == 1`, `models_calls == 1`, `embeddings_calls == n` in both cases, every result the correct width and unit norm. PASS.
- Break path 2 (failure-then-retry): health answering 503 → `ExtractionError` (not wrapped/retyped), zero embeddings calls; second call after health recovers → succeeds, `health_calls` went 1→2, `models_calls` stayed at 1 (first successful discovery). PASS.
- Break path 3 (concurrent failure, FAIL-1 hazard question): 3 concurrent first `embed()` calls against a permanently-503 stub → all 3 waiters get `ExtractionError` (never `AttributeError`/bare exception), zero waiters reach `/v1/embeddings`, instance recovers cleanly on the next call once health is restored. Separately measured with 5 concurrent waiters: `health_calls == 5` — confirms health calls scale 1:1 with concurrent caller count (bounded by real concurrency, not unbounded/retry-looped). PASS.
- Break path 4 (cancellation): two concurrent first `embed()` calls, `task.cancel()` on one mid-init (during the health-check delay) → the cancelled task raises `CancelledError` cleanly, the other task completes successfully (1024-d vector), the lock is left unlocked afterward, and a subsequent `embed()` on the same instance succeeds — no deadlock, no poisoned instance. PASS.
- Break path 5 (`embed([])`, FAIL-2 regression target): on a FRESH instance → returns `[]` with zero resolve/health/models/embeddings calls; on an ALREADY-INITIALISED instance (warmed with one prior `embed(["warm"])`) → returns `[]` with the call counters unchanged (no extra calls of any kind). PASS on both.
- Break path 6 (wire contract): 9 role×dimension combinations (`{None, query, document}` × `{None, 1024, 2048}`) → every captured request body has `set(body.keys()) == {"model", "input", "encoding_format"}`, `encoding_format == "float"`, no `dimensions` key. Confirms the SDK is exercised for real (not a recorder) and correctly parses the float-encoded JSON response in every case. PASS.
- Break path 7 (wrong-width / truncation math): catalog says 2048-d for voyage-4-nano, stub answers 1024-d → `ExtractionError` naming both `1024` and `2048`, nothing returned. Truncation result byte-for-byte equal to `truncate_embedding(raw, 1024)`, L2 norm `1.0 ± 1e-6` (confirming slice + renormalise, not a different algorithm). PASS.

**Design-decision ruling requested by the orchestrator — failed init not cached under concurrency**
RULED ACCEPTABLE, not a FAIL. Verified directly (see break paths 3 above and the 5-waiter measurement): each concurrent waiter re-runs the full init serially under the lock and gets the correct typed `ExtractionError`; the number of extra health calls is bounded by the number of concurrently-waiting callers (5 waiters → 5 calls, not unbounded, not retried in a loop). This is a proportional, bounded cost that only fires when callers are already concurrently hammering a genuinely-unhealthy endpoint — a rare and self-limiting situation — in exchange for preserving the already-required "503 then retry re-runs the health check" behaviour (a provisioning endpoint must stay retryable, confirmed by `TestConcurrentInitialisation::test_a_failed_initialisation_is_retried_in_full`). No AC requires shared-failure caching, and there is no unbounded-request hazard (e.g. no retry loop, no exponential fan-out). Sharing one cached failure across waiters would also be a legitimate design, but the chosen one violates nothing and is defensible as-is.

**Mutation sanity on the two round-1 fixes**
1. Removed the `asyncio.Lock` (reverted `_ensure_initialised` to plain check-then-act, keeping everything else) → `TestConcurrentInitialisation::test_three_concurrent_first_embeds_initialise_exactly_once` failed as expected (`assert 3 == 1`, `resolve_calls`). Restored the file.
2. Removed the `if not texts: return []` guard at the top of `embed()` → `TestWireContract::test_an_empty_batch_returns_early_without_waking_the_endpoint` failed as expected (embed(`[]`) returned a non-empty vector instead of `[]`). Restored the file.
3. After each restore, verified byte-for-byte recovery: `shasum apps/memory/src/tree/models/modal_embedding.py` reproduced the pre-mutation hash (`979854739e088a99d888e8a2bccffdc97925f646`) both times, and `git diff apps/memory/src/tree/models/modal_embedding.py` was byte-identical to the pre-mutation diff both times. Re-ran `tests/unit/models/test_modal_embedding.py` after each restore (44 passed) and the full suite once more at the end (3364 passed).

Process note for the record: my first restore attempt used `git checkout -- <file>`, which — because this task's changes are still uncommitted — reverted the file all the way to the pre-task `HEAD` baseline (the original `MODAL_EMBEDDING_API_KEY`/vLLM-only client), not to the SWE's round-2 fixed state. I caught this immediately from the tool's own diff-preview, restored the exact round-2 content from my own prior full `Read` of the file, and verified the hash/diff match above. No SWE work was lost; flagging this only so future Tester mutation passes restore via `Edit`(reverse of the exact mutation) rather than `git checkout` on files that were never committed.

**Regression / carry-over spot checks**
- `import tree.models.get_model` and `import tree.mcp.server` in fresh interpreters both leave `'modal' not in sys.modules`. PASS.
- `asyncio.Lock()` constructed in `__init__` outside a running loop, then used inside `asyncio.run(...)` on the same instance → no "attached to a different loop" error (confirmed: the lock binds lazily on first `acquire()`, not at construction). PASS.
- Grepped every call site of `ModalEmbeddingModel(...)` / `get_embedding_model` / `get_search_embedding_model` / `get_resolution_embedding_model`: `_build_embedding_model` in `get_model.py` has no `lru_cache` / module-level singleton, so `memory/pipeline.py:2230/2243`, `scripts/query_graph.py` and `dream.py:667` each construct a brand-new `ModalEmbeddingModel` (and therefore a brand-new, unshared lock) on every call — no instance can cross two separate `asyncio.run()` loops via those paths. One correction to the SWE's note: `mcp/server.py:167` does NOT build "a fresh instance per call" — it builds ONE `ModalEmbeddingModel` at `app_lifespan` startup and shares that single instance across every subsequent MCP tool call for the server's lifetime. This is not a hazard, though: the lifespan (and everything after it, including all tool-call handling) runs inside the ONE event loop `mcp.run()` owns for the whole process lifetime — the lock is bound to that loop once and every concurrent tool call that touches it stays on it, which is exactly the "concurrent MCP tool calls sharing one model instance" scenario the lock exists to serialize. Not a FAIL; noting only because the SWE's phrasing ("every call site builds a fresh instance") is imprecise for this one call site.
- `grep -rIn "MODAL_EMBEDDING_API_KEY\|modal_embedding_api_key\|vllm-embedding-api-key" . --exclude-dir=.git --exclude-dir=.venv --exclude-dir=node_modules --exclude-dir=done --exclude=".env*"` → 0 matches outside `tasks/`, `docs/adrs/009_*`, `docs/glossary.md:39` (re-verified).
- `grep -c "\.serving"` and `grep -c 'kwargs\["dimensions"\]'` on `modal_embedding.py` → `0`, `0`.
- No `print()` in the touched file; all touched signatures typed; `git status --short` shows only the 20 files the SWE's own log lists — no unrelated changes.

**Acceptance criteria**
All 13 non-`[HUMAN]` criteria were already verified PASS in round 1 (no checkbox needed to move then, none now — both round-1 FAILs came from the adversarial pass, not from an unmet AC). Re-verified directly this round:
- [x] PASS — repo-wide grep clean outside the exempted paths (re-run above).
- [x] PASS — `TestInit` (3 tests) + independent reproduction.
- [x] PASS — `TestUrlResolution` (3 tests) + independent reproduction (resolved once across a fresh-then-warmed-then-warmed-again instance and across 3/10-way concurrency).
- [x] PASS — `test_serving_path_is_invisible_to_the_client` + `grep -c "\.serving"` → 0.
- [x] PASS — `TestProxyAuth` (4 tests) + independent reproduction (401 → `ModelError`, 503 → `ExtractionError`, both unwrapped even under concurrent failure).
- [x] PASS — `TestServedModel` + independent reproduction.
- [x] PASS — `TestMatryoshkaGuard` (8 cases).
- [x] PASS — `TestClientSideTruncation` (6 cases, now also pinning `encoding_format == "float"`) + independent reproduction across 9 role×size combinations on the real SDK.
- [x] PASS — `TestResponseLength` (3 cases) + independent reproduction.
- [x] PASS — `TestRolePrompts` (4 cases).
- [x] PASS — `test_get_model.py::TestModalBranch` (3 tests) + independent `'modal' not in sys.modules` checks.
- [x] PASS — `test_settings_credentials_only.py`.
- [x] PASS — `ci.yml` / `cd.yml` mock the two proxy vars only.
- [x] PASS — README env table + Modal section.
- [x] PASS — `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green (3364 passed, 0 warnings).

**New regression checks specific to round 2**
- [x] PASS — `TestConcurrentInitialisation` (3 tests) + independent harness (3-way, 10-way, 5-way-failure concurrency).
- [x] PASS — `TestWireContract` (3 tests, replacing the unfaithful `test_an_empty_batch_returns_an_empty_list`) + independent harness against a real stub (fresh instance and already-initialised instance).
- [x] PASS — mutation sanity: both round-1 fixes are load-bearing (removing either fails its regression test); file restored byte-for-byte after each mutation.
- [x] PASS — cancellation mid-init does not deadlock the lock or poison the instance (not explicitly required by any AC, but directly requested by the orchestrator's scope and directly relevant to the lock's correctness).

**Evidence**
```
$ make memory-tests
============================ 3364 passed in 48.25s =============================

$ make memory-format-check && make memory-lint-check && make pre-commit
300 files already formatted / All checks passed! / prettier, ruff check, ruff format, biome: Passed

$ PYTEST_ADDOPTS="tests/unit/config/test_settings_credentials_only.py tests/unit/models/test_modal_embedding.py \
    tests/unit/models/test_modal_catalog.py tests/unit/models/test_modal_server.py tests/unit/models/test_get_model.py \
    tests/unit/models/test_dimensions.py tests/unit/models/test_observability_instrumentation.py \
    tests/unit/scripts/test_modal_embedding_model_script.py tests/unit/config/test_app_config.py -v" make memory-tests
332 passed in 7.09s
secret-shaped scan: wk-: 0, ws-: 0, hf_: 0, AIza: 0, sk-: 0, pa-: 0

independent adversarial harness (own script, own stub, resolve_server_url patched only):
PASS: 63
FAIL: 0

mutation 1 (remove asyncio.Lock):
  assert 3 == 1  (resolve_calls) — TestConcurrentInitialisation FAILS as expected
mutation 2 (remove `if not texts: return []`):
  embed([]) returned a non-empty vector — TestWireContract FAILS as expected
both mutations reverted; shasum apps/memory/src/tree/models/modal_embedding.py ==
  979854739e088a99d888e8a2bccffdc97925f646 (matches pre-mutation state exactly)

$ uv run python -c "import sys, tree.mcp.server; assert 'modal' not in sys.modules"; OK
$ uv run python -c "import sys, tree.models.get_model; assert 'modal' not in sys.modules"; OK

5 concurrent waiters against a permanently-failing health check:
  health_calls == 5 (bounded by caller concurrency, not unbounded)
  result types: ['ExtractionError'] * 5
```

**Other issues found**
- None that block. One documentation nit for the SWE's own notes (not the code, not an AC): the claim "every call site builds a fresh instance" at `mcp/server.py:167` is imprecise — that call site builds ONE shared instance at lifespan startup, not a fresh one per tool call. The lock is still safe there for the reason given above (one continuous event loop for the server's process lifetime), so this is a wording correction only, not a code or test change.

**VERDICT: PASS**

Both round-1 FAILs are fixed and independently verified from scratch (own stub, own harness, not a re-run of the SWE's evidence): the `asyncio.Lock` correctly serializes concurrent first-`embed()` initialisation (1 health + 1 models call for 3-way and 10-way concurrency), a failed initialisation is retried in full and never leaves a half-built client, concurrent failure gives every waiter a proper typed `ExtractionError` with a bounded (not unbounded) number of extra health calls, cancellation of one waiter does not deadlock the lock or poison the instance, and `embed([])` returns `[]` with zero network calls on both a fresh and an already-initialised instance. The empty-batch fix's `encoding_format="float"` side effect is verified on the wire across 9 role×dimension combinations. Both fixes are confirmed load-bearing via mutation testing, with the source restored byte-for-byte afterward. Full suite green (3364 passed, 0 warnings), format/lint/pre-commit green, no secrets observed, no unrelated files touched. Ready to commit.
