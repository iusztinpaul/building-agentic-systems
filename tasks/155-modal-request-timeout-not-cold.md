---
id: 155-modal-request-timeout-not-cold
status: pending
feature: voyage-4-and-modal-embedding-catalog
---

# A Modal call can no longer hang: one `modal.request_timeout_s` (300) on both clients and the chat smoke test, no silent SDK retries, and a timeout is NOT treated as a cold server

Tags: `memory`, `modal`, `llm`, `config`, `bug`, `tests`
Depends on: None (by NNN order it runs after #154; it touches none of its files)
Blocks: #156, #141
Implements: ADR-009 — Decision 11, revision 6 ("A timeout is not cold", "One request timeout, the gate owns the only retry"). Found live by #141 round 1, cycle 4d.

## Scope

**EXECUTION ORDER of the fix round: 153 -> 154 -> 155 -> 156 -> 157 -> 141 round 2.**

**HARD SAFETY RULE:** No agent may run `make memory-deploy-*`, `scripts/modal_*.py` as a process, or any `modal …` command outside task 141; a PATH shim does NOT intercept under `make`/`uv run` because `.venv/bin` wins — use the mocked unit tests and `DRY_RUN=yes`.
Run test commands one at a time — two concurrent pytest runs contend for the local MongoDB/Prefect and stall.
Do not run the real memory pipelines against the local database (shared with the human's `main` checkout).

**Root cause (`tasks/141` Log, cycle 4d).** `ModalLLM(...).generate_json(...)` against a live, slow server logged
`Retrying request to /chat/completions in 0.452048 seconds` and then did not return for 13 minutes (killed). Three
things stack:
1. `AsyncOpenAI(base_url=…, api_key=…)` (`modal_llm.py:260`, `modal_embedding.py:209`) inherits the SDK defaults `DEFAULT_TIMEOUT = httpx.Timeout(timeout=600, connect=5.0)` and `DEFAULT_MAX_RETRIES = 2` (`openai/_constants.py:9-10`) — up to 3 x 600 s per call, retried SILENTLY below the gate.
2. `COLD_CALL_ERRORS = (InternalServerError, APIConnectionError)` (`modal_warmup.py:63`) — and `APITimeoutError` IS an `APIConnectionError`. So a timeout flips the gate, the re-warm answers 200 at once (the server is alive), and the call is sent AGAIN: worst case 2 x 3 x 600 s = 60 minutes.
3. The chat smoke test's POST (`modal_server._chat`) has no explicit timeout (aiohttp's 300 s default, by accident).

**Decision (ADR-009 §11).** A cold Modal server answers 503 in about a second, so a request that timed out reached a
server that is ALIVE and slow — never cold. One knob bounds every call; the gate owns the only retry.

**What to build**
1. `ModalConfig.request_timeout_s: float = Field(default=300.0, ge=1.0, …)` in `tree/config/app_config.py`; `request_timeout_s: 300` under `modal:` in `configs/default.yaml` (one comment: per request, both clients; `TREE_MODAL__REQUEST_TIMEOUT_S`); `tests/unit/config/fixtures/frozen_config.yaml` regenerated the way earlier tasks did. Not added to `.env.example`.
2. Both clients build `AsyncOpenAI(base_url=…, api_key=…, timeout=app_config.modal.request_timeout_s, max_retries=0)`, read at CONSTRUCTION like `warmup_deadline_s` (keep the existing constructor-override pattern only if a test needs it — no new public parameter otherwise).
3. `WarmGate.call` and `WarmGate._retry`: `except openai.APITimeoutError: raise` placed BEFORE `except COLD_CALL_ERRORS` — no `Cold again` warning, no re-warm, no retry, `warmed` stays `True`. `COLD_CALL_ERRORS` itself is unchanged; update its comment ("incl. its `APITimeoutError` subclass" -> carved out, and why).
4. `ModalLLM.generate_json` and `ModalEmbeddingModel.embed`: an `APITimeoutError` becomes
   `ExtractionError("Modal LLM call timed out after 300s (modal.request_timeout_s) — the server is alive but slow: lower the entry's max_tokens or raise TREE_MODAL__REQUEST_TIMEOUT_S")`
   (embedding client: `Modal embedding call timed out after 300s (modal.request_timeout_s) — raise TREE_MODAL__REQUEST_TIMEOUT_S`), `status_code=None`, `from exc`. The number is the configured value formatted `%g`.
5. `modal_server._chat`: `session.post(…, timeout=aiohttp.ClientTimeout(total=app_config.modal.request_timeout_s))`; an `asyncio.TimeoutError` there becomes `ExtractionError("POST <url>/v1/chat/completions timed out after 300s (modal.request_timeout_s)")` (the existing broad `except` already wraps it — make the message say "timed out", since `str(TimeoutError())` is empty).
6. README: one sentence next to `TREE_MODAL__WARMUP_DEADLINE_S` naming `TREE_MODAL__REQUEST_TIMEOUT_S` and the difference (waiting for a COLD server vs waiting for ONE answer).

**Out of scope:** `max_tokens` / `chat_template_kwargs` (#156); a timeout on `poll_health` (it has `POLL_TIMEOUT_S`); streaming; a per-entry timeout.

## Acceptance Criteria

- [ ] `tests/unit/config/…`: `ModalConfig().request_timeout_s == 300.0`; `request_timeout_s: 0` is a `ValidationError` (`ge=1`); `TREE_MODAL__REQUEST_TIMEOUT_S=45` yields `45.0`; `frozen_config.yaml` contains `request_timeout_s: 300`.
- [ ] `tests/unit/models/test_modal_llm.py::test_the_client_is_bounded_and_never_retries_silently` and the twin in `test_modal_embedding.py`: after a warm with `AsyncOpenAI` patched, it was constructed with `timeout=300.0` and `max_retries=0`; with `app_config.modal.request_timeout_s` patched to `45.0`, `timeout=45.0`.
- [ ] `tests/unit/models/test_modal_warmup.py::TestWarmGate::test_a_timeout_is_not_cold`: `fn` raises `openai.APITimeoutError(request=…)` -> the SAME exception propagates, `warm` ran exactly ONCE (the initial warm), `fn` ran exactly ONCE, `gate.warmed is True`, and no record contains `Cold again`.
- [ ] `::test_a_timeout_on_the_retry_is_not_reported_as_still_cold`: first `fn` call raises `InternalServerError` (503), second raises `APITimeoutError` -> `APITimeoutError` propagates (not `… still answered … after one re-warm`).
- [ ] `::test_a_refused_connection_is_still_cold`: a plain `openai.APIConnectionError(request=…)` still triggers ONE re-warm and ONE retry (regression pin — only the timeout subclass is carved out).
- [ ] `test_modal_llm.py::test_a_timeout_is_an_extraction_error_naming_the_knob`: `generate_json` raises `ExtractionError` whose message contains `timed out after 300s`, `modal.request_timeout_s` and `TREE_MODAL__REQUEST_TIMEOUT_S`, `status_code is None`, `__cause__` is the `APITimeoutError`; the completion mock was awaited exactly ONCE. Twin for `embed` in `test_modal_embedding.py`.
- [ ] `tests/unit/models/test_modal_server.py::test_the_chat_post_is_bounded`: the POST is made with `ClientTimeout(total=300.0)`; a raised `asyncio.TimeoutError` becomes `ExtractionError` containing `timed out after 300s`.
- [ ] No message, log line or exception produced here contains the Proxy token (the existing token-leak tests of both clients stay green).
- [ ] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green.

## User Stories

### Story: A thinking model thinks forever and the caller gets an answer in five minutes
1. `ModalLLM(...).generate_json("Reply with JSON facts about Tokyo.")` against a server that never finishes.
2. After 300 s: `ExtractionError: Modal LLM call timed out after 300s (modal.request_timeout_s) — the server is alive but slow: lower the entry's max_tokens or raise TREE_MODAL__REQUEST_TIMEOUT_S`.
3. The log holds NO `Retrying request to /chat/completions` and NO `Cold again` line; the request was sent once.

### Story: The server really went cold mid-run
1. A call answers HTTP 503 after the container idled out.
2. ONE `Cold again: ep-tree-… answered HTTP 503 — re-warming once`, the poll series, `Warm again`, the result — exactly as before this task.

### Story: Operator serves a 35B model whose answers take seven minutes
1. Runs the pipeline with `TREE_MODAL__REQUEST_TIMEOUT_S=900` on the serving process.
2. The clients are built with `timeout=900.0`; nothing else changes.

### Story: The smoke test meets a hung server
1. `make memory-deploy-model-test MODEL=…` (in #141 only) against a server that accepts the POST and never answers.
2. After 300 s: `POST …/v1/chat/completions timed out after 300s (modal.request_timeout_s)`, exit 1 — not a silent wait.

### Story: A 429 is visible
1. The server answers 429.
2. The SDK no longer retries it twice in silence: `ExtractionError(status_code=429)` reaches the caller, and Prefect's task retry decides.

---

Blocked by: (none)

## Log

### [PA] 2026-09-21 16:10 — Grooming

**Summary**
One request-timeout knob for both Modal clients and the chat smoke test, `max_retries=0`, and `APITimeoutError` carved out of the cold class so a slow server fails once instead of looping.

**Key decisions**
- A timeout is never cold: a cold Modal server answers 503 in ~1 s (ADR-009 §11 measurements), so a timed-out call reached a living server; re-warming it only doubles the wait.
- 300 s default: half the SDK's 600 s, and the same order as the smoke test's accidental aiohttp default; one YAML knob + the existing `TREE_` env escape hatch, no per-entry timeout.
- `max_retries=0`: the gate owns the ONE retry; the SDK's two silent retries multiplied the hang by three and hid it (`Retrying request …` was the only trace).
- Both clients, not only the LLM one: the embedding client has the identical defaults.

**Dependencies**
- None.

**User stories**
- 5: bounded hang, real cold unchanged, raised budget, bounded smoke test, visible 429.

Ready for implementation.
