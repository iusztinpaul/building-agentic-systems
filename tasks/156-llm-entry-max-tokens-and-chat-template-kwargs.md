---
id: 156-llm-entry-max-tokens-and-chat-template-kwargs
status: pending
feature: voyage-4-and-modal-embedding-catalog
---

# Thinking models answer: optional per-LLM-entry `max_tokens` and `chat_template_kwargs`, sent by `ModalLLM` AND by the chat smoke test from one helper; `Qwen/Qwen3.5-0.8B` is seeded with thinking off

Tags: `memory`, `modal`, `llm`, `config`, `tests`
Depends on: #155 (same client constructor / same `_chat` body; the timeout is what bounds a wrong knob)
Blocks: #141
Implements: ADR-009 — Decision 10, revision 6 ("Two optional per-entry request knobs, sent only when set"). Found live by #141 round 1, cycle 4d.

## Scope

**EXECUTION ORDER of the fix round: 153 -> 154 -> 155 -> 156 -> 157 -> 141 round 2.**

**HARD SAFETY RULE:** No agent may run `make memory-deploy-*`, `scripts/modal_*.py` as a process, or any `modal …` command outside task 141; a PATH shim does NOT intercept under `make`/`uv run` because `.venv/bin` wins — use the mocked unit tests and `DRY_RUN=yes`.
Run test commands one at a time — two concurrent pytest runs contend for the local MongoDB/Prefect and stall.
Do not run the real memory pipelines against the local database (shared with the human's `main` checkout).

**Root cause (`tasks/141` Log, cycle 4d).** `Qwen/Qwen3.5-0.8B` is a THINKING model. On its Dedicated endpoint the chat
smoke test (strict `city_facts` schema, `max_tokens=256`) got `the chat completion carried no content — the served model
answered with an empty message`: the whole budget went into reasoning. `ModalLLM.generate_json` sends NO `max_tokens`,
so the same loop ran unbounded (#155 bounds the wait; this task makes the model answer).

**Verified offline (SGLang `v0.5.18`, `python/sglang/srt/entrypoints/openai/protocol.py`, read 2026-09-21):**
`ChatCompletionRequest.chat_template_kwargs: Optional[Dict] = None` (:844) is accepted; `separate_reasoning: bool = True`
(:842) + a server started with `--reasoning-parser` puts the answer in `content` and the thinking in
`reasoning_content` (`serving_chat.py:700`, `:1809`) — which matches the live symptom (empty `content`, budget spent);
`"max_new_tokens": self.max_completion_tokens or self.max_tokens` (:1035); for Qwen3-family templates the key is
`enable_thinking` (:963-971). NOT provable offline: that Modal's managed recipe for this model honours the kwarg — that
is #141 round 2's first check, with the ladder written there.

**What to build**
1. `ModalLLMModelConfig` (`tree/config/app_config.py`, `extra="forbid"` stays): `max_tokens: int | None = Field(default=None, ge=1)` and `chat_template_kwargs: dict[str, Any] = Field(default_factory=dict)` (JSON object; keys non-empty strings). Descriptions say: request knobs, client-side, sent only when set, identical on both Serving paths, no redeploy.
2. `tree.models.modal_catalog.chat_request_knobs(entry: ModalLLMModelConfig) -> dict[str, Any]` — pure: `{}` by default; `{"max_tokens": n}` when set; `{"chat_template_kwargs": {...}}` (a copy) when non-empty. These are the TOP-LEVEL JSON body fields as they go on the wire.
3. `ModalLLM.generate_json`: passes `max_tokens=` only when the knob is set and `extra_body={"chat_template_kwargs": …}` only when non-empty (the OpenAI SDK merges `extra_body` into the top-level body, so the wire request equals the smoke test's). With NEITHER set, the `create(...)` kwargs are EXACTLY today's four (`model`, `messages`, `temperature`, `response_format`).
4. `modal_server._chat(url, bearer, served, entry)`: body = today's body `| chat_request_knobs(entry)`, with `max_tokens` = the entry's when set, else `CHAT_SMOKE_MAX_TOKENS` (256 stays the cost bound for an entry without the knob). One INFO line before the POST: `chat knobs: max_tokens=<n> chat_template_kwargs=<json>` (config, never a secret).
5. One-shot diagnosis of an empty answer — both places read `choices[0].finish_reason` and the LENGTH of `choices[0].message.reasoning_content` (never its text):
   - smoke test: `the chat completion carried no content (finish_reason=length, reasoning_content: 812 chars) — a thinking model spent its budget reasoning: set chat_template_kwargs: {enable_thinking: false} on the entry, or raise its max_tokens`; without `reasoning_content` the old sentence plus `(finish_reason=<x>)`.
   - client: the message still STARTS with `Modal LLM returned an empty response` (Gemini parity) and appends ` (finish_reason=length, reasoning_content: 812 chars)` when known.
6. Seeds in `configs/default.yaml` (+ `test_seed_entries`, `frozen_config.yaml`):
   - `Qwen/Qwen3.5-0.8B`: `max_tokens: 4096`, `chat_template_kwargs: {enable_thinking: false}` with the comment `# a THINKING model: with thinking on it spent 256 tokens reasoning and answered nothing (tasks/141 round 1)`.
   - `LiquidAI/LFM2.5-350M`: `max_tokens: 4096` (bounds a runaway constrained-JSON generation on a 32K context).
7. README, Modal LLM section: the two knobs in the entry example, one sentence each; REMOVE any sentence saying an LLM entry has only `n_gpus` extra.

**Out of scope (intentional)**
- The SGLang App's in-container warm-up payload (`WARMUP_PAYLOAD`, Modal's own, value for value; `test_the_sglang_warm_up_is_modals_strict_json_payload` unchanged) and `LLMDeploySpec`: the knobs are client-side, and no thinking model routes to the App today (ADR-009 "what would justify upgrading").
- `reasoning_effort`, `separate_reasoning`, streaming, per-call overrides on `generate_json`.
- The knobs in `llm_identity()` (#151's cache key): Gemini's sampling settings are not in it either; a knob change that must invalidate the 30-day extraction cache is a model-id-level event handled by hand.
- Any claim about extraction quality.

## Acceptance Criteria

- [ ] `tests/unit/config/test_app_config.py`: an LLM entry without the knobs has `max_tokens is None` and `chat_template_kwargs == {}`; `max_tokens: 0` is a `ValidationError`; `chat_template_kwargs: "x"` is a `ValidationError`; an EMBEDDING entry with `max_tokens:` is a `ValidationError` (`extra="forbid"`); `test_seed_entries` asserts Qwen3.5-0.8B = `4096` / `{"enable_thinking": False}` and LFM2.5-350M = `4096` / `{}`.
- [ ] `tests/unit/models/test_modal_catalog.py::TestChatRequestKnobs`: `{}` for a bare entry; `{"max_tokens": 4096}`; `{"chat_template_kwargs": {"enable_thinking": False}}`; both together; mutating the returned dict does not mutate the entry.
- [ ] `tests/unit/models/test_modal_llm.py::test_a_bare_entry_sends_exactly_todays_request`: `create` kwargs keys == `{"model", "messages", "temperature", "response_format"}`.
- [ ] `::test_the_seeded_thinking_model_sends_both_knobs`: for `Qwen/Qwen3.5-0.8B`, `create` got `max_tokens=4096` and `extra_body={"chat_template_kwargs": {"enable_thinking": False}}`, for the JSON-mode call AND the `schema=` call.
- [ ] `tests/unit/models/test_modal_server.py::test_the_smoke_test_sends_the_clients_knobs`: the POSTed JSON body for `Qwen/Qwen3.5-0.8B` has top-level `max_tokens == 4096` and `chat_template_kwargs == {"enable_thinking": False}`; for an entry without `max_tokens` it has `max_tokens == 256` and NO `chat_template_kwargs` key.
- [ ] `::test_wire_parity_between_client_and_smoke_test`: for each seed, `chat_request_knobs(entry)` is a subset of the smoke body, and equals `{max_tokens?} | extra_body` of the client call — one source, asserted.
- [ ] `::test_an_empty_answer_names_the_reasoning_budget`: a 200 with `content: null`, `finish_reason: "length"`, `reasoning_content` of 812 chars -> `ModelError` containing `finish_reason=length`, `reasoning_content: 812 chars` and `enable_thinking`, and NOT containing any of the reasoning text. Twin in `test_modal_llm.py`: `ExtractionError` that `startswith("Modal LLM returned an empty response")` and contains `finish_reason=length`.
- [ ] `frozen_config.yaml` carries both seeds' new fields; `grep -n "enable_thinking" apps/memory/configs/default.yaml` -> 1 hit.
- [ ] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green.

## User Stories

### Story: Operator serves a thinking model and gets JSON back
1. `configs/default.yaml` holds `Qwen/Qwen3.5-0.8B` with `chat_template_kwargs: {enable_thinking: false}` and `max_tokens: 4096`.
2. `make memory-deploy-model-test MODEL=Qwen/Qwen3.5-0.8B` (in #141 round 2 only) logs `chat knobs: max_tokens=4096 chat_template_kwargs={"enable_thinking": false}` and then `strict JSON schema honoured: city=Tokyo population=…`.
3. `ModalLLM(...).generate_json("Reply with JSON facts about Tokyo.", schema=…)` returns `{"city": …, "population": …}` — the client sent the same two fields.

### Story: The knob is wrong and the message says so
1. An engineer adds a new thinking model without `chat_template_kwargs`.
2. The smoke test fails with `the chat completion carried no content (finish_reason=length, reasoning_content: 812 chars) — a thinking model spent its budget reasoning: set chat_template_kwargs: {enable_thinking: false} on the entry, or raise its max_tokens`.
3. They add one YAML line and re-run `-test` — no redeploy, the knob is a request field.

### Story: A non-thinking model is untouched
1. `LiquidAI/LFM2.5-350M` has only `max_tokens: 4096`.
2. Its requests carry `max_tokens` and no `chat_template_kwargs` key at all.
3. An entry with neither knob sends byte-for-byte the request it sent before this task.

### Story: The smoke test tests what the memory will send
1. A reviewer opens `test_wire_parity_between_client_and_smoke_test`.
2. Both paths build their fields from `chat_request_knobs(entry)`; a knob added later cannot reach one and miss the other.

### Story: Engineer puts the knob on an embedding entry by mistake
1. Adds `max_tokens: 512` under `modal.embedding_models`.
2. The app refuses to boot with Pydantic's `Extra inputs are not permitted` naming the field.

---

Blocked by: #155

## Log

### [PA] 2026-09-21 16:10 — Grooming

**Summary**
Two optional, client-side request knobs per LLM entry, fed to the client and the smoke test by one pure helper; the thinking seed gets `enable_thinking: false`; an empty answer now diagnoses itself.

**Key decisions**
- Client-side request fields, not server flags: identical on both Serving paths (we control no flag of a managed recipe) and changeable without a redeploy — which is what lets #141 round 2 iterate inside ONE live cycle.
- `chat_template_kwargs` is accepted by SGLang v0.5.18 (protocol.py:844, verified); the managed recipe honouring it is the one thing left to #141, which gets an explicit ladder and a fallback.
- Smoke test keeps 256 as the default budget when the entry sets none (cost bound), otherwise uses the entry's — same knobs as the client.
- The App's in-container warm-up is NOT touched (Modal's payload; no thinking model routes there).

**Dependencies**
- #155 — same constructor and `_chat`; the timeout bounds a wrong knob.

**User stories**
- 5: thinking model answers, self-diagnosing failure, non-thinking untouched, parity, wrong list refused.

Ready for implementation.
