---
id: 155-modal-request-timeout-not-cold
status: done
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

- [x] `tests/unit/config/…`: `ModalConfig().request_timeout_s == 300.0`; `request_timeout_s: 0` is a `ValidationError` (`ge=1`); `TREE_MODAL__REQUEST_TIMEOUT_S=45` yields `45.0`; `frozen_config.yaml` contains `request_timeout_s: 300`.
- [x] `tests/unit/models/test_modal_llm.py::test_the_client_is_bounded_and_never_retries_silently` and the twin in `test_modal_embedding.py`: after a warm with `AsyncOpenAI` patched, it was constructed with `timeout=300.0` and `max_retries=0`; with `app_config.modal.request_timeout_s` patched to `45.0`, `timeout=45.0`.
- [x] `tests/unit/models/test_modal_warmup.py::TestWarmGate::test_a_timeout_is_not_cold`: `fn` raises `openai.APITimeoutError(request=…)` -> the SAME exception propagates, `warm` ran exactly ONCE (the initial warm), `fn` ran exactly ONCE, `gate.warmed is True`, and no record contains `Cold again`.
- [x] `::test_a_timeout_on_the_retry_is_not_reported_as_still_cold`: first `fn` call raises `InternalServerError` (503), second raises `APITimeoutError` -> `APITimeoutError` propagates (not `… still answered … after one re-warm`).
- [x] `::test_a_refused_connection_is_still_cold`: a plain `openai.APIConnectionError(request=…)` still triggers ONE re-warm and ONE retry (regression pin — only the timeout subclass is carved out).
- [x] `test_modal_llm.py::test_a_timeout_is_an_extraction_error_naming_the_knob`: `generate_json` raises `ExtractionError` whose message contains `timed out after 300s`, `modal.request_timeout_s` and `TREE_MODAL__REQUEST_TIMEOUT_S`, `status_code is None`, `__cause__` is the `APITimeoutError`; the completion mock was awaited exactly ONCE. Twin for `embed` in `test_modal_embedding.py`.
- [x] `tests/unit/models/test_modal_server.py::test_the_chat_post_is_bounded`: the POST is made with `ClientTimeout(total=300.0)`; a raised `asyncio.TimeoutError` becomes `ExtractionError` containing `timed out after 300s`.
- [x] No message, log line or exception produced here contains the Proxy token (the existing token-leak tests of both clients stay green).
- [x] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green.

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

### [SWE] 2026-09-21 17:17 — Implementation

**Files modified**
- `apps/memory/src/tree/config/app_config.py` — `ModalConfig.request_timeout_s: float = 300.0` (`ge=1`), the twin of `warmup_deadline_s`.
- `apps/memory/configs/default.yaml` — `modal.request_timeout_s: 300` + the four-line why (both clients, the smoke test, half the SDK's 600 s, the env override).
- `apps/memory/tests/unit/config/fixtures/frozen_config.yaml` — same key, one comment.
- `apps/memory/src/tree/models/modal_warmup.py` — `except openai.APITimeoutError: raise` BEFORE `except COLD_CALL_ERRORS` in both `WarmGate.call` and `WarmGate._retry`; `COLD_CALL_ERRORS` itself unchanged, its comment now says the subclass is carved out and why the poller classifies its own timeout the other way.
- `apps/memory/src/tree/models/modal_llm.py` — `_request_timeout_s` read at CONSTRUCTION; `AsyncOpenAI(…, timeout=…, max_retries=0)`; `APITimeoutError` -> `ExtractionError` naming `modal.request_timeout_s` / `TREE_MODAL__REQUEST_TIMEOUT_S`, `status_code=None`, `from exc`.
- `apps/memory/src/tree/models/modal_embedding.py` — the identical treatment (`import openai` added for the new handler).
- `apps/memory/src/tree/models/modal_server.py` — `_chat`'s POST gets `aiohttp.ClientTimeout(total=app_config.modal.request_timeout_s)`; a `TimeoutError` becomes `ExtractionError("POST …/v1/chat/completions timed out after 300s (modal.request_timeout_s)")` ahead of the broad wrapper, because `str(TimeoutError())` is empty.
- `apps/memory/README.md` — one paragraph next to `TREE_MODAL__WARMUP_DEADLINE_S` (waiting for a COLD server vs waiting for ONE answer) and the `modal` config bullet.
- `apps/memory/tests/unit/config/test_app_config.py` — `test_modal_request_timeout_default_and_override`.
- `apps/memory/tests/unit/models/test_modal_warmup.py` — `test_a_timeout_is_not_cold`, `test_a_timeout_on_the_retry_is_not_reported_as_still_cold`, `test_a_refused_connection_is_still_cold` (the old parametrized "a timeout is cold too" case is gone); the `_timed_out` / `COLD_CALL_ERRORS` docstrings now document the inheritance TRAP.
- `apps/memory/tests/unit/models/test_modal_llm.py`, `…/test_modal_embedding.py` — new `TestRequestTimeout` (6 tests each); the `wire` fixture no longer hard-codes `max_retries=0`, so the REAL `AsyncOpenAI` now carries the PRODUCTION timeout and retry budget and the request counts mean something.
- `apps/memory/tests/unit/models/test_modal_server.py` — `test_the_chat_post_is_bounded`, `test_a_hung_server_times_the_smoke_test_out_loudly`.

**Tests**
- Unit: 3978 passing, 0 failing (`make memory-tests`, local env). 19 new tests, all proven red against the pre-change code first.
- Integration: N/A — the repo has no integration suite (CLAUDE.md); e2e below.

**Acceptance criteria**
- [x] every criterion above — see the per-criterion test names in the list.

**Evidence**

Red first (before any production change — every new test failing for its own reason):

```
$ PYTEST_ADDOPTS="tests/unit/models tests/unit/config -q" make memory-tests
19 failed, 330 passed
E  Failed: DID NOT RAISE <class 'openai.APITimeoutError'>                     # test_a_timeout_is_not_cold
E  - Modal LLM call timed out after 300s (modal.request_timeout_s) — …
E  + ep-tree-lfm2-5-350m still answered APITimeoutError after one re-warm     # the bug, verbatim
E  - mpletions timed out after 300s (modal.request_timeout_s)
E  + mpletions failed:                                                        # str(TimeoutError()) is empty
```

Green after:

```
$ make memory-tests
3978 passed in 63.69s
$ make memory-format-check && make memory-lint-check && make pre-commit
313 files already formatted / All checks passed! / ruff+prettier+biome Passed
```

End-to-end, against a REAL hung HTTP server on 127.0.0.1 (accepts the POST, answers
never — the live failure reproduced without Modal), `TREE_MODAL__REQUEST_TIMEOUT_S=2`:

```
config: modal.request_timeout_s = 2.0
hung server: http://127.0.0.1:52571
ModalLLM ready: app=ep-tree-lfm2-5-350m server=Server served_model=LiquidAI/LFM2.5-350M url=http://127.0.0.1:52571/v1

ModalLLM.generate_json -> ExtractionError after 2.2s
  message: Modal LLM call timed out after 2s (modal.request_timeout_s) — the server is alive but slow: lower the entry's max_tokens or raise TREE_MODAL__REQUEST_TIMEOUT_S
  status_code: None
  __cause__: APITimeoutError

chat smoke test POST -> ExtractionError after 2.0s
  message: POST http://127.0.0.1:52571/v1/chat/completions timed out after 2s (modal.request_timeout_s)
```

2.2 s, not 6 s and not 12 s: ONE request, no SDK retry, no `Cold again`, no re-warm —
the wall clock is the part the unit suite (which RAISES `httpx.ReadTimeout`) cannot prove.

**Notes**
- **The node IDs in the Acceptance Criteria are approximate — the real ones** (paste these, the AC paths omit the class and name a `TestWarmGate` that never existed):
  - `tests/unit/config/test_app_config.py::TestModalCatalog::test_modal_request_timeout_default_and_override`
  - `tests/unit/models/test_modal_warmup.py::TestWarmGateCall::{test_a_timeout_is_not_cold,test_a_timeout_on_the_retry_is_not_reported_as_still_cold,test_a_refused_connection_is_still_cold}`
  - `tests/unit/models/test_modal_llm.py::TestRequestTimeout::…` and the twin `tests/unit/models/test_modal_embedding.py::TestRequestTimeout::…`
  - `tests/unit/models/test_modal_server.py::TestChatSmokeTest::{test_the_chat_post_is_bounded,test_a_hung_server_times_the_smoke_test_out_loudly}`
- AC 2 describes ONE test; it ships as three, because one test asserting three different constructions reads as three tests with a shared setup: `test_the_client_is_bounded_and_never_retries_silently` (300.0 + `max_retries=0`), `test_the_operator_can_widen_the_budget_for_one_process` (the `45.0` half — the knob is patched BEFORE construction, since that is when it is read), `test_the_real_sdk_client_carries_the_bound_and_no_retries` (the same two read off the REAL `AsyncOpenAI` object, not off the kwargs).
- The `connect-timeout` parametrisation asserts `len(wire.requests) == 1` for a failure that never puts bytes on a socket: the stub records the request and then raises. The property under test is "the SDK did not retry" (it would be 3 with the default budget), not "a connect attempt reached the server" — said in the test docstring too.
- `timeout=` is the plain float the AC and ADR name, so httpx applies it to the CONNECT phase too (the SDK's default was `connect=5.0`). Consequence: a SYN-blackholed host now fails after `request_timeout_s` instead of 5 s. Deliberate — Modal's edge answers a scaled-to-zero server in ~1 s, so a connect timeout is a network fault, and it gets the same one bound as every other failure. Raise it to an `httpx.Timeout(…, connect=5.0)` only if a live run shows connect hangs.
- A connect timeout therefore lands in the SAME class as a read timeout: `APITimeoutError` -> not cold -> `ExtractionError`. Both are covered by the parametrized wire tests.
- `max_retries=0` also removes the SDK's two silent retries #144's QA flagged. Nothing relied on them: both `wire` fixtures already forced `max_retries=0` locally, so no test changes meaning, and the gate owns the only retry. Visible consequence for the Tester: a 429 now reaches `ExtractionError(status_code=429)` on the FIRST response instead of after two silent retries + backoff — Story 5's intent, and Prefect's task retry decides from there.
- `modal_server._embed` (the EMBEDDING smoke test's POST) is deliberately NOT changed: the ADR, the task's item 5 and the AC all name only the chat POST, and three short texts under aiohttp's 300 s default are already bounded. Say the word and it is a one-line follow-up.
- `poll_health` still treats ITS timeout as "still booting" — unchanged and out of scope: a 10 s `/health` GET that never answered means the edge never answered, while a 300 s completion means the server took the request. The code now says so.
- The e2e script emitted one Opik trace (`modal-generate-json`, a failed span) to the `tree-memory` project — normal observability for any run of this code path, noted in case it shows up in the dashboard.
- NOT RUN (hard safety rule, task 141 only): `make memory-deploy-*`, any `modal …` command, any request to `*.modal.direct` / `*.modal.run`. The SDK and CLI rails (`tests/unit/test_modal_sdk_rail.py`, `tests/unit/models/test_modal_cli.py`) are untouched and green.
- Non-vacuity, beyond the red run: each behaviour line was reverted alone and restored (shasum-verified byte-identical, no `git checkout`). Moving the `except APITimeoutError` AFTER `except COLD_CALL_ERRORS` in `call` -> only `test_a_timeout_is_not_cold` fails; dropping it from `_retry` -> only `test_a_timeout_on_the_retry_is_not_reported_as_still_cold`; dropping `max_retries=0` -> 5 LLM tests; dropping the embedding timeout branch -> 3; dropping the POST's `timeout=` -> `test_the_chat_post_is_bounded`.
- Out of scope, untouched: per-entry `max_tokens` / `chat_template_kwargs` (#156 — `test_no_max_tokens_is_sent` still pins today's behaviour), the provisioning wait (#157).

### [Tester] 2026-09-21 18:40 — QA

**Commands run (exact, one at a time, `make env-status` → local throughout)**
```
make env-status
make memory-format-check
make memory-lint-check
make pre-commit
make memory-tests
PYTEST_ADDOPTS='tests/unit/test_cli.py -q' make memory-tests   # warning-source isolation
PYTEST_ADDOPTS='tests/unit/models/test_modal_warmup.py tests/unit/models/test_modal_llm.py tests/unit/models/test_modal_embedding.py tests/unit/models/test_modal_server.py tests/unit/config/test_app_config.py -q -k "TestRequestTimeout or TestWarmGateCall or TestModalCatalog or TestChatSmokeTest"' make memory-tests
PYTEST_ADDOPTS='tests/unit/models/test_modal_warmup.py -q' make memory-tests            # mutation 1 (except removed)
PYTEST_ADDOPTS='tests/unit/models/test_modal_llm.py -q' make memory-tests               # mutation 2 (max_retries=0 removed)
uv run pytest --collect-only -q <the 8 real node IDs the SWE listed>                    # from apps/memory/
uv run python -c "... import tree.models.get_model, tree.memory.pipeline, tree.mcp.server; assert no modal/modal_llm/modal_embedding in sys.modules"   # from apps/memory/
uv run python -c "... ModalConfig(request_timeout_s=0|-1|'abc') all ValidationError; 1e9 accepted"
TREE_MODAL__REQUEST_TIMEOUT_S=45 uv run python -c "print(app_config.modal.request_timeout_s)"   # fresh process, real loader
git diff -- apps/memory/src apps/memory/tests apps/memory/README.md apps/memory/configs | grep -Eoc "wk-...|ws-...|sk-...|hf_...|AIza...|MODAL_PROXY_TOKEN_(ID|SECRET)=..."
make memory-tests   # final re-run after all mutation reverts
```
Two scratch pytest files were written under `apps/memory/tests/unit/models/` for the e2e adversarial pass (real 127.0.0.1 aiohttp servers / a real `asyncio.wait_for` cancellation probe), run via `PYTEST_ADDOPTS`, then deleted; `git status --short` confirmed exactly the SWE's 14 files before and after. No `modal …` command, no `scripts/modal_model.py` process, no `make memory-deploy-model*` target, no request to `*.modal.direct`/`*.modal.run` was ever run.

**Test summary**
- Format / lint / pre-commit: PASS (`313 files already formatted`; `ruff check` all clear; `pre-commit` all hooks passed)
- Unit tests: 3978 passed / 0 failed (`61-73s` across three separate full runs, incl. the final post-mutation-revert run)
- Integration tests: N/A (repo has no integration suite per CLAUDE.md; e2e adversarial pass done instead, see below)
- Warnings: 1 (`opik/rest_api/core/pydantic_utilities.py:13: UserWarning: Core Pydantic V1 functionality isn't compatible with Python 3.14`) — pre-existing, environment-level (Python 3.14 vs opik's pydantic v1 shim), reproduced identically on an UNRELATED, untouched test file (`tests/unit/test_cli.py`), so NOT introduced by this diff. Zero warnings attributable to task 155's changes.

**E2E adversarial pass**
- Happy path: `python -c "await ModalLLM(...).generate_json('hi')"` against a real local aiohttp server that answers `/health`, `/v1/models`, `/v1/chat/completions` all 200 → dict back, exactly `TestRequestTimeout` and existing `TestChat*` suites already exercise this; confirmed via full suite green (PASS).
- Break path 1 (state edge — first call times out against a REAL 127.0.0.1 server, not `httpx.MockTransport`): wrote a scratch pytest starting an `aiohttp.web` server on `127.0.0.1` answering `/health`+`/v1/models` instantly and hanging forever on `POST /v1/chat/completions`; patched only `tree.models.modal_llm.resolve_server_url` and `app_config.modal.request_timeout_s=2.0`. `await model.generate_json("hi")` → `ExtractionError` after **2.14s wall-clock**, message `"Modal LLM call timed out after 2s (modal.request_timeout_s) — the server is alive but slow: lower the entry's max_tokens or raise TREE_MODAL__REQUEST_TIMEOUT_S"`, `status_code is None`, exactly 1 POST landed on the server, no `"Cold again"` / `"still booting"` in caplog. Expected per Story 1/ADR-009 §11: exactly this. PASS.
- Break path 2 (state edge — retry-after-real-503 then hangs, over the real wire): same real server, but the chat POST answers 503 once then hangs on the retry. Result: `ExtractionError` after 2.02s, message `"timed out after 2s"` (NOT `"still answered ... after one re-warm"`), exactly 2 requests landed (the cold call + the one retry, no more), `"Cold again"` DID appear in caplog (the one re-warm happened). Matches ADR-009 §11's "a timeout on the retry is not reported as still cold" exactly. PASS.
- Break path 3 (failure mode — outer `asyncio.wait_for` cancellation): scratch test wrapped `model.generate_json("hi")` in `asyncio.wait_for(..., timeout=0.3)` against a client whose `chat.completions.create()` never returns. `asyncio.TimeoutError` propagated UNCHANGED (not converted to `ExtractionError`, not swallowed) exactly as required by the adversarial checklist; the gate was left in a sound, non-poisoned state (`warmed is True` because `ensure_warm()` had already completed before the cancellation landed inside the actual call). PASS.
- Break path 4 (mutation/regression pin, real code paths not just tests): removed `except openai.APITimeoutError: raise` from `WarmGate.call` → exactly `test_a_timeout_is_not_cold` failed (41 others passed), confirming the SWE's non-vacuity claim; reverted, `shasum` before/after identical (`4329ed2c7677175c8dffb379e6566fe50cb9d4af`). Removed `max_retries=0` from `ModalLLM._warm`'s client construction → exactly 5 tests failed (`test_the_client_is_bounded_and_never_retries_silently`, `test_the_real_sdk_client_carries_the_bound_and_no_retries`, both timeout-naming-the-knob parametrisations, `test_a_cold_answer_still_costs_exactly_one_re_warm`), matching the SWE's claimed count; reverted, `shasum` identical (`ed1c1678ec4c6fe9de9330171fb323755a3ea580`). PASS.

**Acceptance criteria**
- [x] PASS — `ModalConfig().request_timeout_s == 300.0`; `0`/`-1`/`"abc"` → `ValidationError`; `TREE_MODAL__REQUEST_TIMEOUT_S=45` → `45.0` (verified independently in a fresh `uv run python` process, not just via the test); `frozen_config.yaml` contains `request_timeout_s: 300` — Evidence: `apps/memory/tests/unit/config/test_app_config.py::TestModalCatalog::test_modal_request_timeout_default_and_override` passes; `apps/memory/src/tree/config/app_config.py:1006-1013`.
- [x] PASS — both clients built with `timeout=300.0`/`max_retries=0` at construction, `45.0` when patched — Evidence: `test_modal_llm.py::TestRequestTimeout::{test_the_client_is_bounded_and_never_retries_silently,test_the_operator_can_widen_the_budget_for_one_process,test_the_real_sdk_client_carries_the_bound_and_no_retries}` and the embedding twins, all pass; independently confirmed the same off a REAL `AsyncOpenAI` object.
- [x] PASS — `test_a_timeout_is_not_cold`: exception propagates unchanged, `warm`/`fn` each ran once, `gate.warmed is True`, no `Cold again` — Evidence: test passes; mutation-proved (removing the carve-out fails exactly this test).
- [x] PASS — `test_a_timeout_on_the_retry_is_not_reported_as_still_cold` — Evidence: test passes; also reproduced over the REAL wire in break path 2 above.
- [x] PASS — `test_a_refused_connection_is_still_cold`: plain `APIConnectionError` still triggers one re-warm + one retry — Evidence: test passes (regression pin, `COLD_CALL_ERRORS` unchanged).
- [x] PASS — `generate_json`/`embed` timeout → `ExtractionError` naming `timed out after 300s`, `modal.request_timeout_s`, `TREE_MODAL__REQUEST_TIMEOUT_S`, `status_code is None`, `__cause__` is `APITimeoutError`, completion mock awaited exactly once — Evidence: `TestRequestTimeout::test_a_timeout_is_an_extraction_error_naming_the_knob[read-timeout|connect-timeout]` in both files pass; independently confirmed `status_code=None` means `_embed_chunk_resilient` (`apps/memory/src/tree/memory/embedding_text.py:195`, `if getattr(exc, "status_code", None) != 400: raise`) RE-RAISES rather than bisecting/skipping — a timeout can never be silently read as a poison input and never drops a vector; the surrounding Prefect task (`embed-children`/`llm-extract-entities`, `retries=2`, `apps/memory/src/tree/memory/pipeline.py:500,656`) is what decides the retry, exactly as the spec requires.
- [x] PASS — `test_the_chat_post_is_bounded`: `ClientTimeout(total=300.0)`; `asyncio.TimeoutError` → named `ExtractionError` — Evidence: test passes; `test_a_hung_server_times_the_smoke_test_out_loudly` also passes.
- [x] PASS — no Proxy token in any message/log/exception — Evidence: existing token-leak tests of both clients (unchanged, still green in the full 3978-test run); `git diff` secret-shaped scan returned 0 matches.
- [x] PASS — `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` all green — Evidence: see Test summary above (3978 passed, 0 failed, 1 pre-existing unrelated warning).

**Points explicitly ruled on**
1. `status_code=None` does not get bisected/skipped and does not poison the gate — confirmed by reading `_embed_chunk_resilient` and by tracing the Prefect task retry config (`retries=2` on `embed-children` and `llm-extract-entities`); nothing on the LLM side branches on `status_code` at all, so it always propagates to Prefect. Sound.
2. Plain-float `timeout=` also bounds CONNECT (was 5s, now `request_timeout_s`). Confirmed `WarmGate.call` always does `await self.ensure_warm()` before `await fn()`, so a blackholed host is normally caught by the 10s-per-GET poller first; the exposure the SWE names (a host that dies mid-run, after being warmed) is real but narrow and explicitly accepted, with a documented one-line follow-up (`httpx.Timeout(value, connect=5.0)`). Acceptable as shipped; recommend the follow-up only if a live run shows connect hangs, as the SWE's note already says.
3. `max_retries=0`: confirmed `embed-children`/`llm-extract-entities` Prefect tasks carry `retries=2` — a 429 or an overloaded-503 (treated as cold, one re-warm + one retry by the gate) both have a real backstop above the gate. Treating an OVERLOADED (not cold) 503 as cold is a known, documented, and pre-existing simplification (COLD_CALL_ERRORS unchanged; `pulse` made the same call) — out of this task's scope, correctly not touched.
4. Knob read at construction, not per call — confirmed `apps/memory/src/tree/mcp/server.py:169-170` (`app_lifespan`) builds `get_llm()`/`get_embedding_model()` exactly ONCE per process; env overrides are process-level so this is correct and consistent with `warmup_deadline_s`'s existing pattern.
5. `poll_health`'s own timeout ("still booting") vs the gate's call-time timeout ("not cold") — confirmed these are two independent mechanisms (aiohttp `ClientTimeout`/its own exception classification vs the OpenAI SDK's `APITimeoutError`), not a contradiction; the code and ADR text both explain the distinction (10s GET on the edge vs a 300s completion the server accepted). Sound.
6. AC node IDs — confirmed the SWE's corrected 8 node IDs all resolve and collect (`uv run pytest --collect-only` → 20 tests, matching AC 2's "3 tests" claim exactly: `test_the_client_is_bounded_and_never_retries_silently`, `test_the_operator_can_widen_the_budget_for_one_process`, `test_the_real_sdk_client_carries_the_bound_and_no_retries`).

**Other issues found (not blocking, PASS with note)**
- `modal_server._embed`'s POST is left on aiohttp's bare 300s default (confirmed: `aiohttp.client.DEFAULT_TIMEOUT == ClientTimeout(total=300, ...)`), not tied to `modal.request_timeout_s`. This is explicitly out of the task's stated scope (item 5 names only `_chat`) and the SWE flagged it themselves with a one-line follow-up offer — but it is a real latent inconsistency: if an operator raises `TREE_MODAL__REQUEST_TIMEOUT_S` to serve a slower model (Story 3, 900s), the embedding smoke test's POST silently stays capped at 300s while the chat smoke test's POST correctly follows the knob. Recommend a fast follow-up task to point `_embed`'s `aiohttp.ClientTimeout` at the same knob for consistency — not a blocker for this task.
- Confirmed via SDK source (`openai/_base_client.py:1010,1609`) that `httpx.TimeoutException` (the common base of `ReadTimeout`/`ConnectTimeout`/`WriteTimeout`/`PoolTimeout`) is what the SDK converts to `APITimeoutError` — so a `PoolTimeout` under concurrent callers sharing one client is handled identically to a read/connect timeout by this change. No gap found.
- Could not invoke the `code-review` plugin as a standalone gate from this session (no slash-command execution tool available to the Tester agent); compensated with an unusually thorough manual line-by-line diff review, two independent real-wire e2e reproductions, and two independent shasum-verified mutation checks.

**Evidence**
```
$ make memory-tests
======================= 3978 passed in 61.17s (0:01:01) ========================
$ make memory-format-check
313 files already formatted
$ make memory-lint-check
All checks passed!
$ make pre-commit
prettier / ruff check / ruff format / biome check (harness) — all Passed
```

**VERDICT: PASS**
