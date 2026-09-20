---
id: 148-modal-llm-client-and-provider-switch
status: pending
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

- [ ] `TestInit`: empty token -> `ModelError` naming both env vars; `model="nope/x"` -> the #145 unknown-id message listing both groups; `model="voyageai/voyage-4-nano"` -> `… is an embedding entry (modal.embedding_models), not an LLM.`; none of them touches the network (`modal.Server.from_name` not called).
- [ ] `TestFirstUse` (patched `resolve_server_url`, `poll_health`, `served_model_id`, `AsyncOpenAI`): 8 concurrent first `generate_json` calls -> 1/1/1/1; `from_name` is asked for `("ep-tree-lfm2-5-350m", "Server")`; a failing poll leaves `_client is None` and the next call runs the whole sequence; the `ModalLLM ready:` line carries app, served id and url and NOT the token; `poll_health` receives `deadline_s == app_config.modal.warmup_deadline_s` unless the constructor got one.
- [ ] `TestGenerateJson`: no `system` -> exactly one `user` message; with `system` -> `[system, user]` in that order; `model` sent is the DISCOVERED served id; `temperature == 0`; default `response_format == {"type": "json_object"}`; with `schema={…}` -> `{"type": "json_schema", "json_schema": {"name": "response", "strict": True, "schema": {…}}}`; content `'{"a": 1}'` -> `{"a": 1}`.
- [ ] `TestFailures`: content `None` and `""` -> `ExtractionError("Modal LLM returned an empty response")`; `"<think>hmm"` -> `… returned invalid JSON: <think>hmm`; `"[1, 2]"` -> `… not an object: list`; `openai.BadRequestError` -> `ExtractionError` with `status_code == 400` and NO re-warm; `openai.RateLimitError` -> `status_code == 429`, no re-warm.
- [ ] `TestColdAgain`: `chat.completions.create` raising `openai.InternalServerError(503)` once -> `poll_health` called twice in total, the dict is returned, ONE `Cold again: ep-tree-lfm2-5-350m …` WARNING; cold twice -> `ExtractionError`, `status_code == 503`.
- [ ] `TestUsage`: `update_current_span` receives `provider="modal"`, `model="LiquidAI/LFM2.5-350M"`, `total_cost=0.0` and the three token counts; a response without `usage` records no `usage` key; `update_current_span` raising does not fail the call.
- [ ] `test_model_exposes_ensure_warm_and_warm_key`: `warm_key == "ep-tree-lfm2-5-350m"`.
- [ ] `TestGetLlm`: default -> `GeminiLLM`; `provider="modal"` with `models.llm.model = "LiquidAI/LFM2.5-350M"` and both token halves set -> `ModalLLM`; missing token half -> `ModelError`; `provider="x"` -> `ValueError`. `test_get_model_import_does_not_import_modal`: importing `tree.models.get_model` in a subprocess leaves `"modal"` and `"tree.models.modal_llm"` out of `sys.modules`.
- [ ] `test_modal_llm_is_a_base_llm`: `issubclass(ModalLLM, BaseLLM)`; calling with only `(prompt, system=…)` works (the `BaseLLM` contract).
- [ ] No test logs or asserts on a real token; the fake token appears in no captured log record.
- [ ] `## Log` answers the `json_object` question with its source.
- [ ] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green (LOCAL env).

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
