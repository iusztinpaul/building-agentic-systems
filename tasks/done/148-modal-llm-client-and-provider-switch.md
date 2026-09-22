---
id: 148-modal-llm-client-and-provider-switch
status: done
feature: voyage-4-and-modal-embedding-catalog
---

# `ModalLLM(BaseLLM)`: a JSON-returning chat client for any Modal-served LLM, switched on with `models.llm: {provider: modal, model: <repo_id>}`

Tags: `modal`, `models`, `llm`, `config`
Depends on: #144, #145, #147
Blocks: #149, #141
Implements: ADR-009 — Decision 10 (LLMs on Modal: client half), Decision 3 (one lookup, path-blind) and Decision 11 (the client composes the `WarmGate`)

## Scope

**EXECUTION ORDER of the rework: 143 -> 144 -> 145 -> 146 -> 147 -> 148 -> 149 -> 150 -> 141.**

This task touches neither the driver nor the deploy scripts and starts no process. For the record, the rule of
the feature still applies: no `modal …` command and no `make memory-deploy-*` outside task 141.

**What this task adds / changes (committed code):**
- NEW `src/tree/models/modal_llm.py` + `tests/unit/models/test_modal_llm.py`.
- `src/tree/models/get_model.py::get_llm` — a `modal` branch with a LAZY import (the module note's rule: the
  MCP cold boot must never import `modal`).
- `src/tree/config/app_config.py::LLMConfig` — docstring only (`provider: gemini | modal`); Gemini stays the
  default in code and YAML. `configs/default.yaml` gains a COMMENTED example under `models.llm`.
- README: 4 lines in the Modal section ("Point the memory at it", LLM variant).
- NOT changed: `BaseLLM`, `GeminiLLM`, any caller of `generate_json`.

**A. The contract to match — `GeminiLLM` (`src/tree/models/gemini.py`).**
`async generate_json(prompt: str, *, system: str | None = None) -> dict[str, Any]`; JSON MODE, not a schema
(callers embed their schema in the prompt); every failure is an `ExtractionError` (`… API call failed: …`,
`… returned an empty response`, `… returned invalid JSON: <first 200 chars>`).

**B. `ModalLLM(BaseLLM)`.**
- `ModalLLM(proxy_token: str, model: str, warmup_deadline_s: float | None = None)`. Construction is offline
  and total, like `ModalEmbeddingModel`: empty token -> `ModelError("Modal proxy token is required. Set
  MODAL_PROXY_TOKEN_ID and MODAL_PROXY_TOKEN_SECRET.")`; `get_llm_entry(model)` (#145) -> unknown id or an
  embedding id fail HERE, before any network call.
- First use, through the SAME `WarmGate` design as the embedding client (#144) — composed, not inherited, and
  NOT a shared base class with `ModalEmbeddingModel` (two ~15-line `_warm` bodies are cheaper than a mixin):
  `_warm()` = `resolve_server_url(entry)` -> `poll_health(…, deadline_s)` -> `served_model_id(url, …)` ->
  `AsyncOpenAI(base_url=f"{url}/v1", api_key=proxy_token)` -> assign `_served_model` + `_client` LAST -> INFO
  `ModalLLM ready: app=<app_name> server=Server served_model=<id> url=<url>/v1`. Single-flight, all-or-nothing,
  per-loop lock, cold-again re-warm + one retry — all inherited from the gate, none re-implemented.
  `resolve_server_url` is path-blind: `modal.Server.from_name("ep-tree-<slug>", "Server")` resolves a
  Dedicated endpoint and our SGLang App alike (H1). `resolve_server_url` already accepts an LLM entry (#147 widened
  it for the chat smoke test) and its "Run: make memory-deploy-model MODEL=…" hint is kind-neutral since #145.
- Public `async ensure_warm()` and `warm_key` (= `entry.app_name`) — #149's pre-warm is duck-typed on them.
- `generate_json(prompt, *, system=None, schema: dict[str, Any] | None = None)`:
  messages = optional `{"role": "system", "content": system}` + `{"role": "user", "content": prompt}`;
  `response = await self._gate.call(lambda: self._client.chat.completions.create(model=self._served_model,
  messages=…, temperature=0, response_format=…))`.
  `response_format`: `{"type": "json_object"}` by default — `BaseLLM.generate_json` carries no schema, exactly
  like Gemini's `response_mime_type="application/json"`. With the OPTIONAL keyword `schema` (an extra,
  Liskov-safe parameter only this class has): `{"type": "json_schema", "json_schema": {"name": "response",
  "strict": True, "schema": schema}}` — used by #141's live check, available to future callers.
  **SWE must verify** (SGLang's OpenAI-compatible API docs via the `tech-docs` skill, and the OpenAI SDK's
  `response_format` types) that SGLang accepts `{"type": "json_object"}`; if it does not, send a permissive
  schema (`{"type": "object"}`, `strict: False`) instead and say so in `## Log`.
- Result handling, mirroring Gemini's messages with `Modal LLM` in place of `Gemini`:
  `choices[0].message.content` empty/`None` -> `ExtractionError("Modal LLM returned an empty response")`
  (a reasoning model that spent its budget thinking lands here — the message is the diagnosis);
  `json.loads` failure -> `ExtractionError("Modal LLM returned invalid JSON: <first 200 chars>")`; valid JSON
  that is not an object (a list, a string) -> `ExtractionError("Modal LLM returned JSON that is not an
  object: <type>")`. Errors from the gate (`ModelError` / `ExtractionError`) pass through; any other SDK
  exception -> `ExtractionError(f"Modal LLM call failed: {exc}", status_code=<exc.status_code if present>)`.
- Observability: `@track(type="llm", name="modal-generate-json")`; on success
  `update_current_span(provider="modal", model=<repo_id>, total_cost=0.0, usage={"prompt_tokens",
  "completion_tokens", "total_tokens"})` from `response.usage` when present (self-hosted: tokens yes, dollars
  no — the same rule as `_record_modal_usage`). `model` is the catalog `repo_id`, never the served id.
  Fully fail-open (`try/except`, DEBUG line) — telemetry never breaks a completion.
- Never logged: the Proxy token, the prompt, the completion (the ready line and the gate's lines only).

**C. The switch.** `get_llm(provider=None)`: `gemini` unchanged; `modal` ->

    from tree.models.modal_catalog import modal_proxy_bearer
    from tree.models.modal_llm import ModalLLM
    return ModalLLM(proxy_token=modal_proxy_bearer(), model=app_config.models.llm.model)

unknown provider -> the existing `ValueError`. YAML example (commented, under the live `gemini` block):

    # llm: {provider: modal, model: LiquidAI/LFM2.5-350M}   # any id from modal.llm_models; deploy it first:
    #                                                        # make memory-deploy-model MODEL=LiquidAI/LFM2.5-350M

Overridable for one run as `TREE_MODELS__LLM__PROVIDER=modal TREE_MODELS__LLM__MODEL=<repo_id>`.

## Out of scope
- A full extraction run on a Modal LLM, and any claim about extraction quality (a 350M model will not pass
  it). #141 proves PLUMBING: valid JSON back through the client.
- Changing `BaseLLM`, threading a schema through extraction, tool calling, streaming, multi-turn, `max_tokens`
  / thinking-mode knobs (ADR-009 "what would justify upgrading").
- The pipeline pre-warm (#149). Any live call (#141).

## Acceptance Criteria

- [x] `TestInit`: empty token -> `ModelError` naming both env vars; `model="nope/x"` -> the #145 unknown-id message listing both groups; `model="voyageai/voyage-4-nano"` -> `… is an embedding entry (modal.embedding_models), not an LLM.`; none of them touches the network (`modal.Server.from_name` not called).
- [x] `TestFirstUse` (patched `resolve_server_url`, `poll_health`, `served_model_id`, `AsyncOpenAI`): 8 concurrent first `generate_json` calls -> 1/1/1/1; `from_name` is asked for `("ep-tree-lfm2-5-350m", "Server")`; a failing poll leaves `_client is None` and the next call runs the whole sequence; the `ModalLLM ready:` line carries app, served id and url and NOT the token; `poll_health` receives `deadline_s == app_config.modal.warmup_deadline_s` unless the constructor got one.
- [x] `TestGenerateJson`: no `system` -> exactly one `user` message; with `system` -> `[system, user]` in that order; `model` sent is the DISCOVERED served id; `temperature == 0`; default `response_format == {"type": "json_object"}`; with `schema={…}` -> `{"type": "json_schema", "json_schema": {"name": "response", "strict": True, "schema": {…}}}`; content `'{"a": 1}'` -> `{"a": 1}`.
- [x] `TestFailures`: content `None` and `""` -> `ExtractionError("Modal LLM returned an empty response")`; `"<think>hmm"` -> `… returned invalid JSON: <think>hmm`; `"[1, 2]"` -> `… not an object: list`; `openai.BadRequestError` -> `ExtractionError` with `status_code == 400` and NO re-warm; `openai.RateLimitError` -> `status_code == 429`, no re-warm.
- [x] `TestColdAgain`: `chat.completions.create` raising `openai.InternalServerError(503)` once -> `poll_health` called twice in total, the dict is returned, ONE `Cold again: ep-tree-lfm2-5-350m …` WARNING; cold twice -> `ExtractionError`, `status_code == 503`.
- [x] `TestUsage`: `update_current_span` receives `provider="modal"`, `model="LiquidAI/LFM2.5-350M"`, `total_cost=0.0` and the three token counts; a response without `usage` records no `usage` key; `update_current_span` raising does not fail the call.
- [x] `test_model_exposes_ensure_warm_and_warm_key`: `warm_key == "ep-tree-lfm2-5-350m"`.
- [x] `TestGetLlm`: default -> `GeminiLLM`; `provider="modal"` with `models.llm.model = "LiquidAI/LFM2.5-350M"` and both token halves set -> `ModalLLM`; missing token half -> `ModelError`; `provider="x"` -> `ValueError`. `test_get_model_import_does_not_import_modal`: importing `tree.models.get_model` in a subprocess leaves `"modal"` and `"tree.models.modal_llm"` out of `sys.modules`.
- [x] `test_modal_llm_is_a_base_llm`: `issubclass(ModalLLM, BaseLLM)`; calling with only `(prompt, system=…)` works (the `BaseLLM` contract).
- [x] No test logs or asserts on a real token; the fake token appears in no captured log record.
- [x] `## Log` answers the `json_object` question with its source.
- [x] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green (LOCAL env).

## User Stories

### Story: Operator points the memory's LLM at their own Modal model
1. Deploys it: `make memory-deploy-model MODEL=LiquidAI/LFM2.5-350M`.
2. Sets `models.llm: {provider: modal, model: LiquidAI/LFM2.5-350M}` in `configs/default.yaml`.
3. The first call logs `Warming …`, `Warm: … after 84s`, `ModalLLM ready: app=ep-tree-lfm2-5-350m server=Server served_model=LiquidAI/LFM2.5-350M url=https://…/v1`; `generate_json` returns a dict.

### Story: The same code talks to a Dedicated endpoint
1. `models.llm.model: Qwen/Qwen3.5-0.8B` — deployed earlier, auto-routed to an endpoint.
2. Nothing else changes: same lookup `("ep-tree-qwen3-5-0-8b", "Server")`, same Proxy token; the served id is whatever `/v1/models` says.

### Story: A typo in the model id
1. `models.llm.model: LiquidAI/LFM2.5-350m`.
2. The process fails at construction with the catalog's message listing every embedding and LLM id — no GPU woke.

### Story: A small model answers with prose
1. The model returns `Sure! Here is the JSON…`.
2. `ExtractionError: Modal LLM returned invalid JSON: Sure! Here is the JSON…` — the same shape the pipeline already handles for Gemini.

### Story: One run on Modal without editing YAML
1. `TREE_MODELS__LLM__PROVIDER=modal TREE_MODELS__LLM__MODEL=Qwen/Qwen3.5-0.8B` on one command.
2. `get_llm()` builds a `ModalLLM`; the next command is back on Gemini.

### Story: Engineer checks what a run cost
1. Opens the Opik trace: a `modal-generate-json` span, `provider: modal`, `model: LiquidAI/LFM2.5-350M`, token counts, `total_cost: 0`.

---

Blocked by: #144, #145, #147

## Log

### [PA] 2026-09-20 13:40 — Grooming (new task: third plan edit — the client half of D6)

**Summary**
A `BaseLLM` over the OpenAI-compatible chat API of whatever Modal serves (endpoint or SGLang App), selected like the embedding blocks: `models.llm: {provider: modal, model: …}`. Gemini stays the default.

**Key decisions**
- JSON mode by default because `BaseLLM.generate_json` has no schema (Gemini parity); an optional `schema=` keyword gives strict JSON-schema output to callers that have one — #141 uses it.
- The gate from #144 is composed; no shared base class with the embedding client.
- Errors mirror Gemini's three messages, plus `status_code` so callers keep branching on numbers, not text.
- Usage recorded with `total_cost=0` under the catalog repo id.
- Lazy import in `get_llm` — the MCP boot path never imports `modal`.

**Dependencies**
- #144 (`WarmGate`, `poll_health`), #145 (`get_llm_entry`, `modal.llm_models`), #147 (something deployable to talk to; its chat smoke test is the raw-HTTP twin of this client).

**User stories**
- 6 stories: switch in YAML, endpoint vs App invisible, typo, prose instead of JSON, one-off override, cost in Opik.

**Open questions**
- None blocking.

Ready for implementation.

### [SWE] 2026-09-20 17:46 — Implementation

**Files modified**
- `apps/memory/src/tree/models/modal_llm.py` — NEW. `ModalLLM(BaseLLM)`: catalog lookup + **Proxy token** at construction, the composed `WarmGate` at first use, one OpenAI-compatible chat completion in JSON mode per `generate_json`.
- `apps/memory/src/tree/models/get_model.py` — `get_llm` gains the `modal` branch (LAZY import of `modal_catalog.modal_proxy_bearer` + `modal_llm.ModalLLM`); the module note now names both Modal clients.
- `apps/memory/src/tree/config/app_config.py` — `LLMConfig` docstring only (`provider: gemini | modal`); defaults untouched.
- `apps/memory/configs/default.yaml` — 4 COMMENTED lines under `models.llm`; the live block is still `gemini`.
- `apps/memory/README.md` — 7 lines under "Point the memory at it" (the LLM variant + the one-run env override).
- `apps/memory/tests/unit/models/test_modal_llm.py` — NEW, 63 tests over the 11 AC groups.
- `apps/memory/tests/unit/models/test_get_model.py` — 4 `get_llm` tests for the modal branch; the lazy-import subprocess probe now also asserts `tree.models.modal_llm` / `tree.models.modal_embedding` stay out of `sys.modules`.
- `apps/memory/tests/unit/mcp/test_tool_gating.py` — the EXISTING cached server-boot probe reports `modal_modules_imported`; one new test asserts `import tree.mcp.server` pulls neither `modal` nor either client, in both **Memory modes**. (Reused the cached subprocess rather than booting a third server — an MCP boot is the expensive probe in this suite.)

**Tests**
- Unit: 3771 passing, 0 failing, 0 warnings — `make memory-tests`, LOCAL env (`make env-status` → local). 69 of them are new (63 + 4 + 1 parametrized over 2 modes).
- Integration: N/A — the repo has no integration suite (AGENTS.md); e2e is the loopback drive below.

**The `json_object` question (the AC's explicit ask) — answered: JSON mode is supported, no fallback needed**
1. **SGLang**, `python/sglang/srt/entrypoints/openai/protocol.py` at tag `v0.5.18` (the exact docker tag `modal.engines.sglang.version` pins), fetched from `raw.githubusercontent.com` 2026-09-20:
   - `:229-231` — `class ResponseFormat(BaseModel): type: Literal["text", "json_object", "json_schema"]`.
   - `:1068-1069` — `elif self.response_format.type == "json_object": sampling_params["json_schema"] = '{"type": "object"}'`. So `json_object` is not merely *accepted*, it becomes REAL constrained decoding against an object schema — the same grammar machinery `json_schema` uses.
   - `:1058-1067` — `json_schema` + `strict` is compiled with `convert_json_schema_to_str`, which is the optional `schema=` path.
   This confirms #147's independent read of the same file. **No permissive-schema fallback was needed**, so the client sends `{"type": "json_object"}` exactly as the spec wrote it.
2. **OpenAI Python SDK**, the PINNED `openai==2.28.0` in this venv (read via `inspect.getsource`, 2026-09-20): `ResponseFormatJSONObject` = `{"type": Required[Literal["json_object"]]}`; `ResponseFormatJSONSchema` = `{"type": Required[Literal["json_schema"]], "json_schema": Required[JSONSchema]}` with `JSONSchema` = required `name` + optional `description` / `schema` / `strict`. Our two dicts are exactly those TypedDicts, and `AsyncCompletions.create` takes `response_format` as a plain keyword. (context7 was not reachable as a tool in this session; the pinned SDK source and the pinned engine's source are stronger primary sources than either doc site, and both are version-exact.)

**Acceptance criteria** (test names differ slightly from the AC's shorthand; the mapping is below)
- [x] `TestInit` — empty token / `nope/x` / a case-typo'd id / `voyageai/voyage-4-nano` all fail at construction, and `test_construction_never_looks_a_modal_server_up` asserts `modal.Server.from_name` is never called — `tests/unit/models/test_modal_llm.py::TestInit`
- [x] `TestFirstUse` — `test_concurrent_first_calls_initialise_exactly_once[3|8|10]` (1 resolve / 1 poll / 1 `/v1/models` / 1 client), `test_the_lookup_asks_for_the_app_and_the_server_class` (`("ep-tree-lfm2-5-350m", "Server")`, through the REAL `resolve_server_url`), `test_a_failing_poll_leaves_no_client_and_the_next_call_retries` (`_client is None`, full re-run), `test_the_ready_line_carries_the_app_the_served_id_and_the_url`, `test_the_deadline_defaults_to_the_configured_budget` / `…gets_the_bearer_header_and_the_deadline` — `::TestFirstUse`
- [x] `TestGenerateJson` — one `user` message / `[system, user]` in order / an empty `system` adds none / the DISCOVERED served id / `temperature == 0` / `{"type": "json_object"}` by default / the strict `json_schema` under `schema=` / `'{"a": 1}'` → `{"a": 1}` — `::TestGenerateJson`
- [x] `TestFailures` — `None` and `""` → empty response; `"<think>hmm"` → invalid JSON; `"[1, 2]"` / `'"nope"'` / `"7"` → `not an object: list|str|int`; `BadRequestError` → `status_code == 400` with `poll.await_count == 1`; `RateLimitError` → `429`, no re-warm — `::TestFailures`
- [x] `TestColdAgain` — one 503 → `poll_health` twice, the dict returned, exactly ONE `Cold again: ep-tree-lfm2-5-350m answered HTTP 503 — re-warming once` WARNING; cold twice → `ExtractionError`, `status_code == 503`; plus the 8-concurrent-cold burst over the REAL SDK sharing ONE re-warm — `::TestColdAgain`
- [x] `TestUsage` — `provider="modal"`, `model="LiquidAI/LFM2.5-350M"` (the repo id, while the served id was `served/other-name`), `total_cost=0.0`, the three counts; no `usage` key without a `usage` object; a raising recorder does not fail the call; nothing recorded for a failed call — `::TestUsage`
- [x] `test_model_exposes_ensure_warm_and_warm_key` — `warm_key == "ep-tree-lfm2-5-350m"`, twice-awaited `ensure_warm` polls once — `::TestWarmGateComposition`
- [x] `TestGetLlm` — default → `GeminiLLM`; `provider="modal"` + a catalog id + both token halves → `ModalLLM`; provider from config alone (the env-override story) → `ModalLLM`; a non-catalog id → `ModelError`; a missing token half → `ModelError`; `provider="x"` → `ValueError`. `test_importing_the_factory_does_not_import_modal` extended to `modal` + both clients — `tests/unit/models/test_get_model.py::TestGetLLM`, `::TestModalBranch`
- [x] `test_modal_llm_is_a_base_llm` — `issubclass(ModalLLM, BaseLLM)` and a `BaseLLM`-typed caller served by `(prompt, system=…)` alone — `::TestBaseContract`
- [x] No test logs or asserts on a real token — the fake pair `wk-1` / `ws-2` throughout; `::TestProxyTokenIsNeverLeaked` scans every DEBUG-level record of a full run, a failing run and `repr(model)` for the token
- [x] `## Log` answers the `json_object` question with its source — above
- [x] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green (LOCAL env)

**Evidence**

```
$ make memory-format-check && make memory-lint-check
311 files already formatted
All checks passed!

$ make pre-commit
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed

$ make env-status && make memory-tests
Env target: local (.env)
============================ 3771 passed in 47.57s =============================
```

End-to-end, the way an operator uses it — `TREE_MODELS__LLM__PROVIDER=modal TREE_MODELS__LLM__MODEL=LiquidAI/LFM2.5-350M` through the REAL `get_llm()` → catalog → `modal_proxy_bearer()` → `WarmGate` → `poll_health` → `AsyncOpenAI` → a real HTTP round trip. NO Modal, NO Gemini, no `*.modal.run`: an OpenAI-compatible stub listens on `127.0.0.1` and only `modal.Server.from_name` is replaced (it returns the loopback URL). The stub answers the first `/health` with 503, so the cold-start poll runs for real. Fake token `wk-e2e.ws-e2e`.

```
Warming http://127.0.0.1:58192/health — polling for up to 600s
Still cold (HTTP 503) at http://127.0.0.1:58192/health — 0s/600s
Warm: http://127.0.0.1:58192/health answered HTTP 200 after 5s
ModalLLM ready: app=ep-tree-lfm2-5-350m server=Server served_model=LiquidAI/LFM2.5-350M url=http://127.0.0.1:58192/v1

get_llm() -> ModalLLM, warm_key=ep-tree-lfm2-5-350m

--- 1. generate_json (JSON mode, cold server) ---
  -> {'nodes': [{'type': 'person', 'name': 'ada'}], 'edges': []}

--- 2. generate_json with a strict schema (the #141 shape) ---
  -> {'city': 'Tokyo', 'population': 13960000}

--- 3. an empty prompt must not touch the server ---
  -> ExtractionError: Modal LLM was given an empty prompt  (requests sent: 0)

--- what reached the wire ---
  Server.from_name('ep-tree-lfm2-5-350m', 'Server')
  /health polls: 2
  POST /v1/chat/completions  auth=Bearer wk-e2e.ws-e2e
    {"messages": [{"role": "system", "content": "Extract entities as JSON."}, {"role": "user", "content": "Ada Lovelace wrote the first algorithm."}], "model": "LiquidAI/LFM2.5-350M", "response_format": {"type": "json_object"}, "temperature": 0}
  POST /v1/chat/completions  auth=Bearer wk-e2e.ws-e2e
    {"messages": [{"role": "user", "content": "Reply with JSON facts about Tokyo."}], "model": "LiquidAI/LFM2.5-350M", "response_format": {"type": "json_schema", "json_schema": {"name": "response", "strict": true, "schema": {"type": "object", "properties": {"city": {"type": "string"}, "population": {"type": "integer"}}, "required": ["city", "population"], "additionalProperties": false}}}, "temperature": 0}
```

**Notes**
- **The empty-prompt decision (non-negotiable 3), and why it is not silence.** `generate_json("")` / `"   "` raises `ExtractionError("Modal LLM was given an empty prompt")` ABOVE the gate — no URL lookup, no poll, no request. `GeminiLLM` has no guard for this input (it would POST and turn the API's refusal into `ExtractionError("Gemini API call failed: …")`), so the TYPE every caller already handles is unchanged while the cost is not: the embedding client returns `[]` for an empty batch because `[]` is the correct answer for "embed zero texts", but there is no correct dict for "generate JSON from nothing" — `{}` would make an empty chunk look like a successful extraction of zero entities, which is the one failure mode that would be silent in the graph.
- **The deadlock is pinned, and it is real.** `WarmGate`'s `asyncio.Lock` is not re-entrant, so a `_warm` that went back through `gate.call` hangs forever behind the lock its own caller holds. `TestWarmGateComposition::test_the_warm_body_never_re_enters_its_own_gate` replaces `gate.call` with a double that raises immediately, so the regression fails in milliseconds with `_warm re-entered its own gate` rather than as a 300 s pytest-timeout. Verified by mutation: with `served_model_id` routed through `self._gate.call`, that test fails instantly AND `test_model_exposes_ensure_warm_and_warm_key` hangs until the timeout (10 s, forced) — i.e. the hazard is genuine and the pin is the fast detector. `_warm` touches the raw helpers only.
- **Nothing was added around the gate** (non-negotiable 2): no retry, no second deadline, no per-caller recovery. A failed warm stays one-per-waiter behind the lock exactly as #144 shipped it.
- **Every test drives the real SDK where the SDK decides something** (non-negotiable 5): `TestWireContract`, `TestFirstUse`'s concurrency, `TestColdAgain`'s 8-caller burst and the empty-prompt guard run the real `AsyncOpenAI` over `httpx.MockTransport` with `max_retries=0` (the default 2 retries would have inflated every request count and hidden the gate's ONE retry); the `_SuspendingServer` doubles `await asyncio.sleep(0)` so a check-then-act race can actually appear.
- **Settings are patched on the binding the code under test holds** (non-negotiable 7): `modal_catalog.settings` (the reused autouse fixture in `test_get_model.py`), never `tree.config.settings.settings`.
- **No `max_tokens`** — out of scope by ADR-009 §10. Consequence, pinned by `test_no_max_tokens_is_sent` and stated so the Tester reads it as intent: a reasoning model that spends its whole budget thinking returns an empty message and lands on `"Modal LLM returned an empty response"`, which is the intended diagnosis for that case, not a truncation bug.
- **`EMBEDDING_SERVER_NAME`** is imported and rendered as `server=Server` in the ready line. The name is misleading for an LLM, but it is the SAME constant `resolve_server_url` looks the app up with on both kinds; renaming it touches `modal_catalog`, `modal_server`, `modal_embedding` and three test files, which is a separate task, not "while I was in there".
- **`status_code` uses `isinstance(exc, openai.APIStatusError)`**, the same predicate `modal_warmup._cold_status` uses — not `getattr(exc, "status_code", None)`, so a stray attribute on a non-HTTP exception can never reach the field callers branch on (`test_a_transport_failure_has_no_status_code`).
- **The three result verdicts sit OUTSIDE the `try`** (empty / invalid JSON / not an object). Inside, they would be swallowed by `except ModelError: raise` and survive only by accident.
- **No `modal` command, no `make memory-deploy-*`, no `*.modal.run` / `*.modal.direct` request, no Gemini call, no `.env` read** anywhere in this task. Every HTTP interaction was an in-process `httpx.MockTransport` or the `127.0.0.1` stub above; every token was fake. `tasks/141`, `tasks/149`, `tasks/150`, `docs/adrs/009` and `docs/glossary.md` were read but not modified.
- NOT committed — the Tester goes first.

### [SWE] 2026-09-20 18:05 — Follow-ups from the self-review (no behaviour change)

**What changed**
- `apps/memory/src/tree/models/modal_llm.py` — module docstring only. "Never logged: … the prompt, the completion" was absolute and unholdable: with Opik configured, `@track` records the decorated call's inputs and output on the span, exactly as `track_genai_client` does for Gemini. The docstring now says what is true — the token is nowhere (log, exception, `repr`), the prompt/completion reach an Opik span and that is provider PARITY, not a leak — so `TestProxyTokenIsNeverLeaked`'s caplog-only scope matches the claim it defends.

**⚠ Adjacent issue found, NOT fixed here (out of scope: "NOT changed: any caller of `generate_json`") — and it is a caveat on #141**

The LLM Prefect tasks carry an `INPUTS` cache with **no LLM identity in the key** — the exact bug ADR-009 §6 fixed for embeddings, still open on the LLM side:

- `pipeline.py:666-674` — `llm_extract_entities_task = task(_llm_extract_entities, cache_policy=_INPUTS_NO_HEADERS, cache_expiration=timedelta(days=30))`. Its inputs are `chunked`, `user_id`, `llm=None`, `opik_trace_headers` (excluded). Nothing names the provider or the model.
- `pipeline.py:2539-2545` — `summarise_cluster_task`, same policy, 90 days.
- For contrast, the embedding tasks already take `embedding_identity` PURELY to be in the cache key (`pipeline.py:473`, `:1163`, and the docstrings at `:493` / `:1178` say so) via `search_embedding_identity()`.

**Consequence.** Flipping `models.llm` to `{provider: modal, model: …}` and re-running extraction on a document already extracted under Gemini inside the cache window REPLAYS Gemini's JSON and never calls the Modal server. For #141 — whose whole claim is "valid JSON back from a 350M model" — a cached replay would make it pass without the Modal LLM being asked once. **#141 must extract a document the cache has never seen** (a fresh `source_uri`), or run with the cache disabled, and should say which it did. The fix (an `llm_identity: str = f"{provider}:{model}"` task input, the twin of `search_embedding_identity`) is a separate task for the PA to file — it touches two callers of `generate_json`, which this task is explicitly forbidden to change.

**Two clarifications for the Tester (intended behaviour, not bugs)**
- **Usage is recorded for a 200 whose content will not parse.** `_record_modal_usage` runs BEFORE `_parsed_object`, so a prose completion still books its token counts. Deliberate: the tokens were genuinely spent and billed, so the Opik cost breakdown must see them; a run wasted on a chatty small model should look expensive, not free. `test_nothing_is_recorded_for_a_failed_call` covers only the case where the CALL failed (`BadRequestError`) and no tokens were spent — that asymmetry is the intent.
- **The new MCP gating test is not vacuous.** Verified that `import tree.mcp.server` really does reach the factory: in a fresh interpreter `tree.models.get_model` IS in `sys.modules` while `modal`, `tree.models.modal_embedding` and `tree.models.modal_llm` are all absent — so the assertion has something to catch, and a top-level import in either client would fail it.

**Re-verified after the docstring edit**

```
$ make memory-format-check && make memory-lint-check && make pre-commit
311 files already formatted
All checks passed!
prettier / ruff check / ruff format / biome check (harness) .............Passed

$ make memory-tests
============================ 3771 passed in 47.94s =============================
```

### [Tester] 2026-09-20 19:05 — QA

**Commands run, exactly**
```
make env-status                                            # -> Env target: local (.env)
make memory-format-check                                   # -> 311 files already formatted
make memory-lint-check                                     # -> All checks passed!
make pre-commit                                             # -> prettier / ruff check / ruff format / biome check ... Passed
cd apps/memory && set -a && source ../.env && set +a && \
  uv run pytest tests/unit/config/test_settings_credentials_only.py \
    tests/unit/models/test_modal_llm.py tests/unit/models/test_get_model.py \
    tests/unit/mcp/test_tool_gating.py -q --durations=15    # -> 143 passed in 11.49s
uv run pytest tests/unit/models/test_modal_llm.py --collect-only -q   # -> 63 tests collected
grep -noE "[A-Za-z0-9._-]{30,}" over the four Modal-related test files # -> only fake wk-1/ws-2/ws-e2e and identifiers, 0 secret-shaped strings
# Mutation check (re-entrancy pin), on the tracked working file, reverted immediately after:
Edit modal_llm.py: routed `served = await served_model_id(...)` through `self._gate.call(lambda: served_model_id(...))`
uv run pytest tests/unit/models/test_modal_llm.py::TestWarmGateComposition::test_the_warm_body_never_re_enters_its_own_gate -q
  -> FAILED in 1.09s: AssertionError: _warm re-entered its own gate   (pin is non-vacuous)
Edit modal_llm.py: reverted to the original `served = await served_model_id(...)` (verified byte-identical to pre-mutation content)
uv run python -c '... exercised tree.models.modal_llm._content on choices=[], tool_calls-with-no-content, reasoning_content+empty content ...'
  -> all three raise ExtractionError("Modal LLM returned an empty response"), no crash
curl raw.githubusercontent.com/sgl-project/sglang @ v0.5.18: protocol.py, sampling_params.py, scheduler.py (public read-only GET, no modal/gemini calls)
make memory-tests                                           # -> 3771 passed in 50.52s (0 failed, 0 warnings)
git status --short                                           # -> exactly 7 modified + 2 new files, nothing else
```

**E2E adversarial pass**
- Happy path: read `TestGenerateJson`/`TestWireContract` — real `AsyncOpenAI` over `httpx.MockTransport`, body carries `model` (discovered served id), `messages`, `temperature=0`, `response_format`; `'{"a": 1}'` back as `{"a": 1}`. Matches ADR-009 Decision 10. PASS.
- Break path 1 (re-entrancy / deadlock hazard): mutated `_warm` to route `served_model_id` through `self._gate.call` on the real file, ran the pin test alone → failed fast (`AssertionError: _warm re-entered its own gate`, 1.09s) instead of hanging to the 300s pytest-timeout. Confirms the regression test is non-vacuous. Reverted. PASS.
- Break path 2 (malformed/hostile completion shapes): in-process probes against `_content`/`_parsed_object` with `choices=[]`, a `tool_calls`-only message (no `content`), and `reasoning_content` + empty `content` → all three raise `ExtractionError("Modal LLM returned an empty response")`, no `IndexError`/`AttributeError`. Also covered by the suite: `None`/`""` content, `<think>...` prose, a 500-char completion (truncated to 200 chars in the message), a JSON array/string/int at top level (`not an object: list|str|int`), `choices` with `finish_reason: "length"` (falls into the same invalid-JSON path as any other truncated body). PASS.
- Break path 3 (concurrency / cold-start races): `TestFirstUse`/`TestColdAgain` — 3/8/10 concurrent first calls → exactly one resolve/poll/`/v1/models` call; a failing poll leaves `_client is None` and the next call re-runs the whole sequence; 8 concurrent calls each hitting a real 503 over the wire share exactly ONE re-warm and ONE warning (not 8); a 400/429 never triggers a re-warm. Verified by reading + exercising the tests (real `AsyncOpenAI`+`MockTransport`, suspending doubles that `await asyncio.sleep(0)` so a check-then-act race can actually surface). PASS.
- Break path 4 (empty/whitespace prompt — the task's explicit point 1): confirmed the ONLY production caller that could pass a blank string (`query_memory` → `execute_nl_query` → `nl_to_pipeline`) already guards upstream in `src/tree/mcp/graph_tools.py` (`if not query.strip(): return tool_error("invalid_input", ...)`) before the LLM is ever touched. `extract_entities`'s `parent.content` and `judge_contradiction`'s template-wrapped statements are never literally blank. Where a blank string COULD reach `generate_json`, Gemini has no guard and would turn the API's own rejection into `ExtractionError("Gemini API call failed: ...")` — same exception type, later and billed. `ExtractionError <: ModelError`, so it maps through `_RETRIEVAL_UNAVAILABLE` to `search_unavailable`/`retryable=True` exactly like every other LLM failure — no new envelope, no caller regression. PASS (drop-in safe).

**Points to rule on (from the brief)**
1. **Empty prompt raises early instead of returning `{}`.** Confirmed drop-in safe — see break path 4 above. Not a defect.
2. **Usage recorded for a 200 whose content won't parse; nothing recorded for a failed call.** Read `_record_modal_usage` — called before `_parsed_object(_content(response))`, deliberately (tokens were spent and billed). Exact parity with `modal_embedding.py::_record_modal_usage`. `test_nothing_is_recorded_for_a_failed_call` and `test_the_three_token_counts_are_recorded_under_the_repo_id` cover both halves of the asymmetry. Intentional, documented, tested — not a defect.
3. **No `max_tokens` sent.** Verified against the pinned SGLang tag `v0.5.18` on `raw.githubusercontent.com` (read-only GET, no `modal` command, no server touched): `protocol.py::ChatCompletionRequest.to_sampling_params` sends `"max_new_tokens": self.max_completion_tokens or self.max_tokens` **unconditionally** (not routed through `get_param`/`_DEFAULT_SAMPLING_PARAMS`), so with neither field set the engine receives an explicit `None` — NOT the `SamplingParams` dataclass's `128` default (`sampling_params.py:54`), which only applies to the low-level `/generate` API when the field is omitted entirely. `scheduler.py::init_req_max_new_tokens` treats `None` as effectively unbounded (`1 << 30`) and clamps it only to the request's remaining context budget (`max_req_len - input_len - 1`). **Correction to my own first read:** completions on this path are NOT truncated at a 128-token server default; they run to a real stop token or to the true context limit. The residual risk ADR-009 already names — a "thinking" model spending its output budget on reasoning before ever emitting JSON — is real but bounded by context length, not by a small fixed cap. Either way this is explicitly out of scope for #148 (the task's "Out of scope" section and ADR-009's "what would justify upgrading" both name `max_tokens`/`chat_template_kwargs` as a future knob) — not a defect against this task. Note for #141: expect failures (if any) to look like "empty response" from a reasoning model, or context exhaustion on a very long chunk with a small `max_model_len`, not systematic 128-token truncation.
4. **`EMBEDDING_SERVER_NAME` renders as `server=Server` for an LLM.** Confirmed cosmetic: it is the literal shared constant `resolve_server_url` uses to look up BOTH kinds (`modal_catalog.py:41`, `modal_server.py:147`); renaming it touches `modal_catalog`, `modal_server`, `modal_embedding` and three test files — correctly deferred, not a #148 defect.
5. **Parity with `GeminiLLM`.** Exception types: Gemini wraps every SDK exception into one untyped `ExtractionError` (status_code always `None`); `ModalLLM` additionally distinguishes 4xx/5xx via `_status_code` (mirrors `modal_warmup._cold_status`'s `isinstance(exc, openai.APIStatusError)` predicate exactly) — strictly additive, and confirmed no current LLM caller consumes `status_code` (`embedding_text.py:193`'s consumer is embeddings-only). Return shape: Gemini returns whatever `json.loads` yields verbatim (a list/str/int would silently violate `BaseLLM`'s `dict[str, Any]` contract); `ModalLLM` explicitly guards `isinstance(parsed, dict)` and raises — required by this task's AC, stricter than Gemini by design, not a regression. Markdown-fenced JSON: neither client strips fences; under `json_object` constrained decoding SGLang should never emit one (verified via `protocol.py:1068-1069`), and if a Dedicated-endpoint engine ignored `response_format` a fenced reply would fail exactly like Gemini's "invalid JSON" case — same failure shape. `system` placement: Gemini uses `system_instruction` (a config field), Modal uses a leading `{"role": "system"}` message — the correct shape for each API. **Temperature — the one parity gap with behavioral consequences:** Gemini's `GenerateContentConfig` sets no `temperature` (runs at the API default, not pinned), while `ModalLLM` explicitly sends `temperature=0`. Spec-mandated by this task's AC (`TestGenerateJson::test_the_temperature_is_zero`) and correct per ADR-009 Decision 10, but the operator-visible consequence is real: flipping `models.llm` from `gemini` to `modal` changes determinism for cached extraction, not just provider. Worth a callout for whoever documents the switch to an operator, not a #148 defect.

**Adjacent issue — confirmed real, not fixed here, correctly routed to #150/#141**
`llm_extract_entities_task` (`pipeline.py:667-674`) and `summarise_cluster_task` (`pipeline.py:2535-2545`) both use `cache_policy=_INPUTS_NO_HEADERS` with no LLM identity in their inputs. Read the call sites: `_llm_extract_entities` accepts an optional `llm: BaseLLM | None = None`, but its ONLY production caller (`pipeline.py:2086`, `await llm_extract_entities_task(chunked, user_id, opik_trace_headers=headers)`) never passes it — so the cache key's `llm` slot is always the literal `None`, regardless of `models.llm.provider`. `_summarise_cluster` doesn't even take an `llm` parameter; it calls `get_llm()` inside its own body, so the provider is entirely invisible to the cache key. Contrast with `embedding_identity` (`pipeline.py:473`, `:1163` — a real input purely to sit in the `INPUTS` cache key, per ADR-009 Decision 6). Confirmed: re-extracting a document already cached under `gemini` within the 30/90-day window after switching to `modal` replays the old JSON and never calls the Modal server. This does NOT fail #148 (out of scope: "NOT changed: any caller of `generate_json`"; the affected files are pipeline.py callers, untouched by this task's diff). Route to #150 for the fix (`llm_identity: str` input, the twin of `search_embedding_identity`); #141 must extract a document the cache has never seen or run cache-disabled, and must say which.

**Acceptance criteria**
- [x] PASS — `TestInit`: empty token → `ModelError` naming both env vars; unknown/typo'd/embedding-kind ids fail before any network call, `modal.Server.from_name` never called — `tests/unit/models/test_modal_llm.py::TestInit`, verified passing.
- [x] PASS — `TestFirstUse`: 8/3/10 concurrent first calls → 1/1/1/1; lookup asks for `("ep-tree-lfm2-5-350m", "Server")`; failing poll leaves `_client is None`; ready line carries app/served id/url, no token; deadline defaults to `app_config.modal.warmup_deadline_s` — `::TestFirstUse`, verified passing.
- [x] PASS — `TestGenerateJson`: message order, discovered served id, `temperature==0`, default `json_object`, `schema=` upgrades to strict `json_schema`, `'{"a": 1}'` → `{"a": 1}` — `::TestGenerateJson`, verified passing + independently confirmed on the wire via `TestWireContract`.
- [x] PASS — `TestFailures`: `None`/`""` → empty response; `<think>hmm` → invalid JSON; `[1,2]`/`"nope"`/`7` → not an object; `BadRequestError`→400 no re-warm; `RateLimitError`→429 no re-warm — `::TestFailures`, verified passing.
- [x] PASS — `TestColdAgain`: one 503 → 2 polls, dict returned, ONE `Cold again:` warning; two 503s → `ExtractionError` status 503 — `::TestColdAgain`, verified passing.
- [x] PASS — `TestUsage`: `provider="modal"`, repo-id `model`, `total_cost=0.0`, 3 token counts; no `usage` object → no `usage` key; raising recorder doesn't fail the call — `::TestUsage`, verified passing.
- [x] PASS — `test_model_exposes_ensure_warm_and_warm_key`: `warm_key == "ep-tree-lfm2-5-350m"` — verified passing.
- [x] PASS — `TestGetLlm`: default → `GeminiLLM`; `provider="modal"` + catalog id + token → `ModalLLM`; missing token half → `ModelError`; unknown provider → `ValueError`; subprocess import probe keeps `modal`/`tree.models.modal_llm` out of `sys.modules` — `tests/unit/models/test_get_model.py`, verified passing.
- [x] PASS — `test_modal_llm_is_a_base_llm`: `issubclass(ModalLLM, BaseLLM)` + two-argument contract call — verified passing.
- [x] PASS — No real token in any captured log/exception/`repr` — `TestProxyTokenIsNeverLeaked`, verified passing; independently confirmed 0 secret-shaped strings across all four Modal-related test files (test_settings_credentials_only.py scanned first).
- [x] PASS — `## Log` answers the `json_object` question with its source (SWE's 2026-09-20 17:46 entry, SGLang `protocol.py` + OpenAI SDK source, both version-pinned).
- [x] PASS — `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green, LOCAL env — see Evidence.

**Evidence**
```
$ make env-status
Env target: local (.env)

$ make memory-format-check
311 files already formatted

$ make memory-lint-check
All checks passed!

$ make pre-commit
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format...............................................................Passed
biome check (harness)....................................................Passed

$ uv run pytest tests/unit/config/test_settings_credentials_only.py tests/unit/models/test_modal_llm.py tests/unit/models/test_get_model.py tests/unit/mcp/test_tool_gating.py -q
143 passed in 11.49s

$ uv run pytest tests/unit/models/test_modal_llm.py --collect-only -q
63 tests collected in 0.25s

$ make memory-tests   # re-run clean, verbose, unbuffered, after an earlier contaminated attempt (see note)
============================ 3771 passed in 50.52s =============================
```

**Note on test-run wall time:** the first two `make memory-tests` attempts in this review stalled for 9-14 minutes around the 89-90% mark before I killed them; root-caused to MY OWN concurrent `uv run pytest` invocations (isolated file re-runs, the mutation check) hitting the same local Docker MongoDB/Prefect instance as the backgrounded full run, not to anything in the SWE's diff. A clean, uncontended re-run (`-vv --timeout=60 -p no:cacheprovider`, unbuffered) completed in 50.52s with the identical 3771-passed count the SWE reported — confirming this was pure resource contention on my part, not flakiness or a hang in the code under test. All sockets observed during the slow runs were `localhost:27017`/`localhost:4200` only — no external/forbidden network activity at any point.

**Other issues found**
- README's Modal-LLM paragraph is 9 added lines (incl. 2 blank), not the "7 lines" the task's narrative section mentions — cosmetic, not an AC, not blocking.
- `tests/unit/models/test_get_model.py::TestGetLLM::test_a_non_catalog_model_fails_before_any_gpu_wakes` takes an unused `mocker` fixture parameter — harmless (ruff/lint green), worth a follow-up cleanup only.

**VERDICT: PASS**
