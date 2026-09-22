---
id: 144-modal-warmup-poller-and-warm-gate
status: done
feature: voyage-4-and-modal-embedding-catalog
---

# Wait out a Modal cold start: ONE health poller, a `WarmGate` composed by the Modal client ("warm at use, not at t0"), one `modal.warmup_deadline_s` knob — and the smoke test on the same poller

Tags: `modal`, `models`, `config`, `robustness`
Depends on: #143
Blocks: #147, #148, #149, #141
Implements: ADR-009 — Decision 11 (warm at use, not at t0: closed three-way classification, single-flight gate, one deadline knob)

## Scope

**EXECUTION ORDER of the rework: 143 -> 144 -> 145 -> 146 -> 147 -> 148 -> 149 -> 150 -> 141.**

**HARD SAFETY RULE:** No agent may run `make memory-deploy-*`, `scripts/modal_*.py` as a process, or any `modal …` command outside task 141; a PATH shim does NOT intercept under `make`/`uv run` because `.venv/bin` wins — use the mocked unit tests and `DRY_RUN=yes`.
This task needs NO live call at all: every test injects `fetch`, `sleep` and `clock`.

**The bug (reproduced live 2026-09-20 on `ep-qwen3-embedding-4b`).** A scaled-to-zero Modal server answers
`GET /health` with **HTTP 503 in about 1 s** — it never holds the request — and the polling itself is what
boots the container. `modal_server.wait_until_healthy` is ONE long-held GET: it fails in ~1 s on every
scale-from-zero. So `make memory-deploy-embedding-model-test` right after a deploy fails, and so does the
first `ModalEmbeddingModel.embed()` after the idle window.

**Source of the design — port it, do not reinvent it.** READ (read-only, outside the repo, never write there):
`/Users/pauliusztin/Documents/01-Projects/scrabble/scrabble/src/pulse/warmup.py` (the whole design + its
reasoning), `…/pulse/embeddings/modal.py` and `…/pulse/models/modal.py` (how a client COMPOSES the gate).
Pulse's measurements, which this repo inherits: 503 in 0.25 s (embedding) / 0.74 s (LLM); 503 -> 200 at
t=113 s; slowest boot 199 s (a 35B LLM); an endpoint warmed at 09:34 answered 503 to the first ten real calls
at 09:40 = ten failed documents.

**What this task rewrites (committed code, from #139/#140):**
- DELETES `tree.models.modal_server.wait_until_healthy` and `TestWaitUntilHealthy`
  (`tests/unit/models/test_modal_server.py`).
- DELETES `ModalEmbeddingModel._init_lock`, `ModalEmbeddingModel._ensure_initialised` and the `health_timeout`
  constructor parameter (`src/tree/models/modal_embedding.py`); the gate SUBSUMES them (see B).
  `TestConcurrentInitialisation` (`tests/unit/models/test_modal_embedding.py`) is KEPT and retargeted.
- CHANGES `modal_server.smoke_test(model, health_timeout=1200.0)` -> `smoke_test(model, deadline_s=None)`.
- ADDS `src/tree/models/modal_warmup.py`, `tests/unit/models/test_modal_warmup.py`,
  `ModalConfig.warmup_deadline_s`, one YAML line, one `frozen_config.yaml` line.

**A. `src/tree/models/modal_warmup.py` — the ONE place that knows how to wait out a cold start.** Imports
`aiohttp`, `openai`, `tree.models.exceptions` — NOT `modal`, NOT `tree.config` (the deadline is a parameter).
- Constants: `POLL_TIMEOUT_S = 10.0` (per GET), `INITIAL_INTERVAL_S = 5.0`, `BACKOFF_FACTOR = 1.5`,
  `MAX_INTERVAL_S = 15.0` -> polls at t ~ 0, 5, 12.5, 23.75, 38.75, then every 15 s.
- `async def poll_health(url: str, headers: dict[str, str], *, deadline_s: float, sleep=asyncio.sleep,
  clock=time.monotonic, fetch=_get_status) -> float` — returns the elapsed seconds until the 200.
  `fetch: Callable[[str, dict[str, str], float], Awaitable[int]]` (url, headers, per-GET timeout -> status) is
  the test seam; the default does one `aiohttp` GET. The first GET fires IMMEDIATELY; the DEADLINE is enforced
  by the loop `clock`, never by the HTTP timeout; the last sleep is clipped to the time left.
- **Closed three-way classification** (a 4xx never becomes 200 by waiting):
  1. `200` -> warm, return.
  2. `500-599`, `aiohttp.ClientConnectionError`, `asyncio.TimeoutError` -> still booting, keep polling.
  3. ANYTHING ELSE — `401`/`403` (bad **Proxy token**), `404` (wrong URL), any other status, any other
     `aiohttp.ClientError` (redirect loop, payload/decoding error) -> fail after ONE attempt with
     `ModelError("Health poll of <url> returned HTTP 403 — not a cold start, giving up after 1 attempt.")`;
     for 401/403 append ` Check MODAL_PROXY_TOKEN_ID and MODAL_PROXY_TOKEN_SECRET in .env.`
  Deadline spent -> `ExtractionError("Health poll of <url> gave up after 600s (deadline 600s); last result: HTTP 503", status_code=503)`
  (`status_code=None` when the last result was a transport error).
- **No new exception class.** `ModelError` = configuration, waiting will not help; `ExtractionError` =
  transient. That is exactly the split #140 already had (401 -> `ModelError`, the rest -> `ExtractionError`)
  and no caller would branch on a third type.
- Logging (stdlib `logging`, never the headers or the token): `Warming <url> — polling for up to 600s`; ONE
  INFO line per cold poll `Still cold (HTTP 503) at <url> — 38s/600s` (a 2-10 min boot must never look hung);
  `Warm: <url> answered HTTP 200 after 113s`.
- `COLD_CALL_ERRORS = (openai.InternalServerError, openai.APIConnectionError)` — what a cold container looks
  like AT CALL TIME (the SDK raises the first for every status >= 500, the second for refused connections and
  timeouts). A 429 or any 4xx is NEVER cold.
- `class WarmGate` — composed, not inherited:
  `WarmGate(warm: Callable[[], Awaitable[None]], label: str)`; `warmed: bool` (a HINT with an expiry);
  `async ensure_warm()` — returns at once when `warmed`, else takes the lock, RE-CHECKS, awaits `warm()`, and
  sets `warmed = True` only AFTER it returned (a failed warm leaves the gate cold; the next call retries);
  `async call(fn: Callable[[], Awaitable[T]]) -> T` — `ensure_warm()`, then `fn()`; on a `COLD_CALL_ERRORS`
  exception: log ONE WARNING per cold period (`Cold again: <label> answered HTTP 503 — re-warming once`; the
  `warmed` flag gates the line, so 8 concurrent callers print it once), set `warmed = False`, `ensure_warm()`
  ONCE behind the same lock, retry `fn()` ONCE, log `Warm again: <label> answered after one re-warm`. A second
  cold answer raises `ExtractionError("<label> still answered HTTP 503 after one re-warm", status_code=503)`;
  a failed re-warm re-raises the poll's error with the prefix `re-warm of <label> after HTTP 503 failed: `.
  Every other exception from `fn()` propagates UNCHANGED.
- **The lock is per RUNNING EVENT LOOP** (pulse's `_loop_lock`): an `asyncio.Lock` binds to the loop of its
  first contended acquire and raises `bound to a different event loop` on the next loop. `_loop_lock()`
  compares `asyncio.get_running_loop()` with the remembered loop; on a change it makes a new lock AND resets
  `warmed = False` — so the client below is rebuilt on the new loop (its HTTP pool belongs to the old one).
  This repo does cross loops with one instance: scripts call `asyncio.run` more than once, Prefect may run a
  task on another loop, and the MCP server keeps one instance for the process lifetime.

**B. `ModalEmbeddingModel` composes the gate — what happens to #140's code.**
- Constructor: `ModalEmbeddingModel(proxy_token, model, dimensions=None, warmup_deadline_s: float | None = None)`;
  `None` -> `app_config.modal.warmup_deadline_s` read at CONSTRUCTION. Still offline and total.
- `self._gate = WarmGate(self._warm, label=self._entry.app_name)`.
- `async def _warm(self) -> None` — the single-flight body, ALL-OR-NOTHING exactly as `_ensure_initialised`
  was: `resolve_server_url(entry)` -> `poll_health(f"{url}/health", {"Authorization": f"Bearer {token}"}, deadline_s=…)`
  -> `served_model_id(url, …)` -> build `AsyncOpenAI(base_url=f"{url}/v1", api_key=token)` -> assign
  `_served_model` and `_client` LAST -> the existing `ModalEmbeddingModel ready: …` INFO line. It runs again,
  whole, on a cold-again re-warm and on a loop change (cost: one `get_url`, one `/v1/models` GET, one client
  object — once per cold period, not per call).
- NEW public `async def ensure_warm(self) -> None` (delegates to the gate — #149's pre-warm is duck-typed on
  it) and `warm_key: str` property = `entry.app_name` (#149 warms each distinct app once).
- `embed()`: the empty-batch guard stays FIRST (an empty call never wakes a GPU); then
  `response = await self._gate.call(lambda: self._client.embeddings.create(...))` — the lambda must read
  `self._client` at CALL time (a re-warm replaces it). `ModelError` / `ExtractionError` from the gate pass
  through unchanged; any other exception is still wrapped as `ExtractionError("Embedding call failed: …")`.
  Everything after the call (`_record_modal_usage`, `_checked_vectors`) is untouched.
- `TestConcurrentInitialisation` retargeted: 8 concurrent first `embed()` calls -> exactly 1 `resolve_server_url`,
  1 `poll_health`, 1 `served_model_id`, 1 `AsyncOpenAI`; a failing poll leaves `_client is None` and the next
  call runs the whole sequence again.

**C. The smoke test and the driver's `test` command use the SAME poller.**
`smoke_test(model, deadline_s: float | None = None)`: `None` -> `app_config.modal.warmup_deadline_s`;
`elapsed = await poll_health(...)`; the line `health 200 after %.1fs` and `SmokeTestReport.cold_start_seconds`
are fed by that return value. No new CLI flag: after a first-ever deploy that also downloads weights, the
operator raises the budget for one command with the escape hatch —
`make memory-deploy-embedding-model-test MODEL=… TREE_MODAL__WARMUP_DEADLINE_S=1200` (a make command-line
variable is exported to the recipe's environment). Add that one sentence to the README's step 3.

**D. Config.** `ModalConfig.warmup_deadline_s: float = Field(default=600.0, ge=1.0, description="Total budget
(seconds) polling ONE cold Modal server's /health before failing; per server.")`; `configs/default.yaml`
`modal.warmup_deadline_s: 600` with the comment `# ~3x the slowest boot measured (199 s); 300 s (client) and
1200 s (smoke test) before #144`; the same line in `tests/unit/config/fixtures/frozen_config.yaml`.
Overridable as `TREE_MODAL__WARMUP_DEADLINE_S`. NOT in `.env.example`.

**HTTP library: `aiohttp`, not `httpx` (pulse uses httpx).** The whole `tree.models` package and
`modal_server.py`'s other three calls already use `aiohttp`, and so does the existing test recorder;
choosing httpx would leave two HTTP libraries in one module or force porting three working functions and
~30 tests. Pulse's `transport=` seam becomes the injected `fetch` callable.

## Out of scope
- `ModalLLM` (#148 composes the same gate). The pipeline pre-warm (#149). Any live call (#141).
- A keep-warm ping, `min_containers > 0`, a per-process model cache (ADR-009 "what would justify upgrading").
- Retrying anything other than the two cold classes; a second re-warm.

## Acceptance Criteria

- [x] `tests/unit/models/test_modal_warmup.py::TestPollHealth` (all with injected `fetch`/`sleep`/`clock`, a fake clock that advances by the slept amount; the whole file runs in < 1 s):
  - `[503, 503, 200]` -> returns; `sleep` was called with `[5.0, 7.5]`; the first `fetch` happened before any sleep.
  - 8 x `503` then `200` -> sleeps are `[5.0, 7.5, 11.25, 15.0, 15.0, 15.0, 15.0, 15.0]` (cap at 15).
  - `fetch` raising `aiohttp.ClientConnectionError` then `asyncio.TimeoutError` then `200` -> returns (both are "booting").
  - `401`, `403`, `404`, `302`, `418` -> `ModelError` after exactly 1 `fetch`, message contains the URL and `giving up after 1 attempt`; 401/403 also contain `MODAL_PROXY_TOKEN_ID`.
  - `fetch` raising `aiohttp.TooManyRedirects`-class / `aiohttp.ClientPayloadError` -> `ModelError` after 1 attempt.
  - always `503` with `deadline_s=600` -> `ExtractionError`, `status_code == 503`, message contains `gave up after` and `last result: HTTP 503`; total slept time <= 600; the last sleep is clipped (never sleeps past the deadline).
  - `deadline_s=1` with `503` -> exactly 1 fetch, then the deadline error.
  - caplog: one `Still cold (HTTP 503) at <url> — ` line PER cold poll, one `Warm: <url> answered HTTP 200 after` line; the string `Bearer` and the fake token appear in NO log record.
- [x] `TestWarmGate` (fake `warm` + fake call factories, no SDK call): 8 concurrent `ensure_warm()` -> `warm` awaited once; a raising `warm` leaves `warmed is False` and the next `ensure_warm()` awaits it again; `call` with `fn` raising `openai.InternalServerError` once -> `warm` awaited twice in total, `fn` twice, result returned, exactly ONE `Cold again` WARNING even with 8 concurrent callers; `fn` cold twice -> `ExtractionError` with `still answered` and `status_code == 503`; re-warm failing -> the prefixed error; `openai.RateLimitError` (429) and `openai.BadRequestError` -> propagate unchanged, `warm` awaited once, no WARNING; `openai.APITimeoutError` IS cold.
- [x] `test_gate_survives_a_second_event_loop`: the same `WarmGate` used under two consecutive `asyncio.run(...)` calls, each with 2 contending callers, raises nothing and awaits `warm` once PER loop.
- [x] `TestConcurrentInitialisation` (retargeted) green with the 1/1/1/1 counts above; `grep -n "_init_lock\|_ensure_initialised\|health_timeout\|wait_until_healthy" apps/memory/src apps/memory/scripts apps/memory/deploy -r` -> 0 hits in `src/` and `scripts/` (the two deploy scripts keep their own in-container `HEALTH_TIMEOUT` until #146/#147).
- [x] `test_embed_rewarms_once_when_cold_again`: a warmed `ModalEmbeddingModel` whose `embeddings.create` raises `openai.InternalServerError(503)` once -> `poll_health` called twice in total, the vectors are returned, one WARNING. `test_embed_wraps_non_cold_errors`: a `BadRequestError` -> `ExtractionError("Embedding call failed: …")`, `poll_health` called once. `test_empty_batch_never_warms`: `embed([])` -> `[]`, `poll_health` not called.
- [x] `test_model_exposes_ensure_warm_and_warm_key`: `warm_key == "ep-tree-qwen3-embedding-0-6b"`; `ensure_warm()` twice -> one poll.
- [x] `TestSmokeTest`: `smoke_test` calls `poll_health` with `deadline_s == app_config.modal.warmup_deadline_s` when none is passed and logs `health 200 after`; a poll `ModelError` (401) and a poll `ExtractionError` (deadline) both reach the driver's `test` command as exit 1 with the poll's message.
- [x] `test_modal_warmup_deadline_default_and_override`: default `600.0`; `TREE_MODAL__WARMUP_DEADLINE_S=45` -> `45.0`; `0` -> `ValidationError` (`ge=1`). `frozen_config.yaml` carries the key.
- [x] `test_modal_warmup_does_not_import_modal`: importing `tree.models.modal_warmup` in a subprocess leaves `"modal" not in sys.modules`.
- [x] `grep -c "TREE_MODAL__WARMUP_DEADLINE_S" apps/memory/README.md` >= 1.
- [x] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green (LOCAL env).

## User Stories

### Story: Operator smoke-tests right after a deploy
1. `make memory-deploy-embedding-model-test MODEL=voyageai/voyage-4-nano` while the container is scaled to zero.
2. The log shows `Warming https://…/health — polling for up to 600s`, then `Still cold (HTTP 503) at … — 0s/600s`, `… — 5s/600s`, `… — 13s/600s`, …
3. After ~2 minutes: `Warm: … answered HTTP 200 after 113s`, then `health 200 after 113.0s` and the rest of the smoke test. Before this task the command failed after ~1 s with `Health check … answered 503`.

### Story: Operator has a wrong Proxy token
1. `MODAL_PROXY_TOKEN_SECRET` is stale; they run the smoke test.
2. ONE poll, then `Health poll of https://…/health returned HTTP 401 — not a cold start, giving up after 1 attempt. Check MODAL_PROXY_TOKEN_ID and MODAL_PROXY_TOKEN_SECRET in .env.`; exit 1 within seconds — not after 600 s.

### Story: The memory embeds after the endpoint scaled to zero mid-run
1. A long ingestion pauses for 6 minutes on a slow LLM stage; Modal scales the embedding server to zero.
2. The next `embed()` gets a 503 from the SDK. The log shows ONE `Cold again: ep-tree-voyage-4-nano answered HTTP 503 — re-warming once`, the `Still cold` lines, `Warm: …`, `Warm again: …`.
3. The call returns its vectors; no document failed.

### Story: Eight concurrent embeds hit a cold server
1. Eight coroutines call `embed()` on one fresh instance.
2. Exactly one URL lookup, one poll loop and one `/v1/models` call happen; all eight get vectors.

### Story: A server that never comes up
1. The engine crashes at start; `/health` answers 503 for ever.
2. After 600 s: `Health poll of … gave up after 600s (deadline 600s); last result: HTTP 503` as an `ExtractionError` — a transient failure the caller may retry.

### Story: Operator needs a longer budget once
1. First deploy of a model with 16 GB of weights.
2. `make memory-deploy-embedding-model-test MODEL=… TREE_MODAL__WARMUP_DEADLINE_S=1200` -> the first log line reads `polling for up to 1200s`. No file was edited.

---

Blocked by: #143

## Log

### [PA] 2026-09-20 13:40 — Grooming (new task: third plan edit, addendum D11)

**Summary**
A scaled-to-zero Modal server answers `/health` with 503 in ~1 s, so our single long-held GET fails on every cold start. This task ports the pulse warm-up design: one poller with a closed three-way classification, one `WarmGate` the client composes (warm at use, single-flight, one re-warm + one retry), one deadline knob, and the smoke test on the same poller.

**Key decisions**
- The gate SUBSUMES #140's double-checked `_init_lock`: URL resolution + poll + served-id discovery + client construction are ONE single-flight body, all-or-nothing; `_init_lock`, `_ensure_initialised`, `health_timeout` and `wait_until_healthy` are deleted, `TestConcurrentInitialisation` is kept and retargeted.
- The gate takes a `warm` coroutine instead of a base URL (pulse's shape) because here the URL itself is discovered at first use.
- A loop change resets the hint, so the `AsyncOpenAI` client is rebuilt on the new loop — one rule for the lock and the HTTP pool.
- No `WarmupError`: fail-fast -> `ModelError`, deadline / still-cold -> `ExtractionError(status_code=…)` — the split #140 already had.
- `aiohttp` over httpx: one HTTP library in `tree.models`; pulse's `transport=` seam becomes an injected `fetch`.
- 600 s replaces both 300 s (client) and 1200 s (smoke test); the escape hatch covers a slow first deploy.

**Dependencies**
- #143 (the autouse dry-run rail must exist before anyone touches driver-adjacent code again).

**User stories**
- 6 stories: smoke test waits, wrong token fails fast, cold again mid-run, concurrent first use, dead server, one-off longer budget.

**Open questions**
- None blocking. SWE must verify whether `AsyncOpenAI` tolerates a loop change (the design does not depend on the answer: the client is rebuilt either way).

Ready for implementation.

### [SWE] 2026-09-20 13:10 — Implementation

**Files modified**
- `apps/memory/src/tree/models/modal_warmup.py` — NEW: `poll_health` (first GET immediately, 5s x1.5 capped at 15s, deadline on the loop clock, closed three-way classification) + `WarmGate` (single-flight per RUNNING loop, `warmed` as a hint, one re-warm + one retry) + `COLD_CALL_ERRORS`. Imports `aiohttp` / `openai` / `tree.models.exceptions` only — no `modal`, no `tree.config`.
- `apps/memory/src/tree/models/modal_embedding.py` — composes the gate: `_warm()` is the ALL-OR-NOTHING body (resolve -> poll -> `/v1/models` -> `AsyncOpenAI`, assigned LAST); new public `ensure_warm()` and `warm_key`; `warmup_deadline_s` replaces `health_timeout`; `embed()` calls through `self._gate.call(lambda: …)` and re-raises `ModelError`/`ExtractionError` unchanged. `_init_lock` / `_ensure_initialised` deleted.
- `apps/memory/src/tree/models/modal_server.py` — `wait_until_healthy` DELETED; `smoke_test(model, deadline_s=None)` feeds `health 200 after %.1fs` and `cold_start_seconds` from the shared poller.
- `apps/memory/src/tree/config/app_config.py` — `ModalConfig.warmup_deadline_s: float = 600.0, ge=1.0`.
- `apps/memory/configs/default.yaml`, `apps/memory/tests/unit/config/fixtures/frozen_config.yaml` — one `warmup_deadline_s: 600` line each.
- `apps/memory/README.md` — step 3 gains the `TREE_MODAL__WARMUP_DEADLINE_S=1200` one-off budget sentence.
- `apps/memory/tests/unit/models/test_modal_warmup.py` — NEW (40 tests): the poller with injected `fetch`/`sleep`/`clock`, the gate with fake warm bodies and call factories, the two-event-loop test, two cancellation tests, the no-`modal`-import subprocess check.
- `apps/memory/tests/unit/models/test_modal_embedding.py` — `server`/`suspending_server` fixtures retargeted to `poll_health`; `TestConcurrentInitialisation` retargeted (3/8/10 concurrent first calls, failed poll retried in full, every waiter sees the failure, a cancelled caller leaves the instance usable); new `TestWarmAtUse` (re-warm once, non-cold wrapped, retry uses the re-warm's client, `ensure_warm`/`warm_key`, empty batch never warms).
- `apps/memory/tests/unit/models/test_modal_server.py` — `TestWaitUntilHealthy` deleted; new `TestSmokeTestWarmUp` (the poll's URL/header/budget, an explicit deadline, a 401 fail-fast, a spent budget).
- `apps/memory/tests/unit/config/test_app_config.py` — `test_modal_warmup_deadline_default_and_override`.
- `apps/memory/tests/unit/scripts/test_modal_embedding_model_script.py` — the poll's `ModelError` (401) and `ExtractionError` (deadline) both exit 1 with the poll's message; the 401 does NOT print the HF_TOKEN hint.
- `apps/memory/tests/unit/models/test_observability_instrumentation.py` — the Modal double now uses a REAL `WarmGate` over a no-op warm instead of stubbing the deleted `_ensure_initialised`.

**Tests**
- Unit: 3549 passing, 0 failing (`make memory-tests`, LOCAL env). `tests/unit/models/test_modal_warmup.py` alone: 40 passed in 0.90s — no real sleeping, a 600 s budget exercised on a fake clock.
- Integration: N/A — the repo has no integration suite (AGENTS.md); e2e is the local-server run below.

**Acceptance criteria**
- [x] `TestPollHealth` — all eight cases green, including `[503,503,200]` -> sleeps `[5.0, 7.5]` with the first fetch at t=0, the 15 s cap, the two booting transport errors, the five fail-fast statuses, `TooManyRedirects` / `ClientPayloadError`, the clipped last sleep, `deadline_s=1` -> one fetch, and the caplog/no-token-in-logs check.
- [x] `TestWarmGate…` — 8 concurrent `ensure_warm` -> one warm; a failed warm leaves it cold; one re-warm + one retry with exactly ONE WARNING under 8 concurrent callers; cold twice -> `ExtractionError(status_code=503)`; a failed re-warm keeps its type and gains the prefix; 429/400/401 and a `ValueError` pass through unchanged; `APITimeoutError` is cold.
- [x] `test_gate_survives_a_second_event_loop` — two `asyncio.run` bursts of 2, `warm` awaited once per loop.
- [x] `TestConcurrentInitialisation` retargeted (1/1/1/1 counts); `grep -n "_init_lock\|_ensure_initialised\|health_timeout\|wait_until_healthy" apps/memory/src apps/memory/scripts apps/memory/deploy -r` -> only the two `deploy/*.py` in-container `health_timeout=` lines (#146/#147 own those).
- [x] `test_embed_rewarms_once_when_cold_again`, `test_embed_wraps_non_cold_errors`, `test_empty_batch_never_warms`.
- [x] `test_model_exposes_ensure_warm_and_warm_key`.
- [x] `TestSmokeTestWarmUp` + the two driver-exit-1 tests.
- [x] `test_modal_warmup_deadline_default_and_override`; `frozen_config.yaml` carries the key.
- [x] `test_modal_warmup_does_not_import_modal`.
- [x] `grep -c "TREE_MODAL__WARMUP_DEADLINE_S" apps/memory/README.md` -> 1.
- [x] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green (LOCAL env).

**Evidence**
```
$ make memory-tests
3549 passed in 46.10s

$ make memory-tests PYTEST_ADDOPTS="tests/unit/models/test_modal_warmup.py -q"
40 passed in 0.90s

$ make memory-format-check && make memory-lint-check
307 files already formatted
All checks passed!

$ make pre-commit
prettier / ruff check / ruff format / biome check .......... Passed
```

End-to-end (NO Modal, NO network: a real `aiohttp` server on 127.0.0.1:8931 that answers 503 while "booting", plus the REAL `AsyncOpenAI` client and REAL sleeps):
```
=== 1. cold server: 503 for 8s, then 200 ===
Warming http://127.0.0.1:8931/health — polling for up to 60s
Still cold (HTTP 503) at http://127.0.0.1:8931/health — 0s/60s
Still cold (HTTP 503) at http://127.0.0.1:8931/health — 5s/60s
Warm: http://127.0.0.1:8931/health answered HTTP 200 after 13s
-> poll_health returned 12.5s after 12.5s wall; 3 health GETs, auth header present: True

=== 2. the client embeds through the gate ===
ModalEmbeddingModel ready: app=ep-tree-voyage-4-nano server=Server served_model=served/local-stub native=2048 dimensions=1024 (truncated client-side) url=http://127.0.0.1:8931/v1
-> warm_key=ep-tree-voyage-4-nano; embed() returned 1 vector of width 1024

=== 3. the server scales to zero mid-run ===
Cold again: ep-tree-voyage-4-nano answered HTTP 503 — re-warming once
Warming … / Still cold (HTTP 503) … 0s/600s / Warm: … answered HTTP 200 after 5s
ModalEmbeddingModel ready: … (rebuilt)
Warm again: ep-tree-voyage-4-nano answered after one re-warm
-> embed() recovered in 6.4s: 2 vectors of width 1024 — no failed document

=== 4. a 404 fails after ONE poll ===
-> ModelError after 0.00s: Health poll of http://127.0.0.1:8931/nope returned HTTP 404 — not a cold start, giving up after 1 attempt.

=== 5. a server that never comes up ===
-> ExtractionError(status_code=503) after 12.0s: Health poll of … gave up after 12s (deadline 12s); last result: HTTP 503
```

**Notes**
- NOT RUN — anything live: no `modal` command, no `make memory-deploy-*`, no request to `*.modal.run` / `*.modal.direct` (task 141 owns those). Every HTTP interaction above is in-process or 127.0.0.1.
- The OpenAI SDK's own retries fire BEFORE the gate sees a cold call (2 retries, ~1.4 s in the e2e log). That is the documented reason the gate exists — the SDK's seconds cannot cover a 1-4 minute boot — and it costs two extra 503s per cold period, not per call.
- `_loop_lock()` runs BEFORE the `warmed` fast path (pulse checks the flag first): the task requires a loop change to reset the hint, which a fast return would skip. Consequence: setting `gate.warmed = True` from outside a running loop never survives the first `ensure_warm` — test doubles must replace the warm body, not the flag (that is why `test_observability_instrumentation.py` now builds a real gate over an `AsyncMock`).
- A failed warm is per waiter, not shared: 8 concurrent first callers whose warm fails each pay their own attempt (the flag is only set on success). That is #140's behaviour and pulse's; worth a look if a 600 s deadline x N waiters ever shows up in a log.
- `aiohttp` is used by `tree.models` but is NOT declared in `apps/memory/pyproject.toml` (it arrives transitively). Pre-existing, out of this task's file list — flagging it for the PA.
- Running the client for real against the local server emitted one Opik trace (`modal-embed`) to the `tree-memory` project, since `@track` is live outside the test suite. Expected, harmless.
- Added after a review pass (3551 unit tests total, from 3549):
  - `TestWarmAtUse::test_eight_concurrent_cold_embeds_share_one_re_warm` — the ADR consequence's burst at the CLIENT level, over the REAL SDK (`max_retries=0`) against a wire stub that is cold until something polls it warm again: 8 vectors, `resolve`/`poll`/`/v1/models` awaited exactly TWICE (not nine times), exactly ONE `Cold again` WARNING. **Finding while writing it:** the winner's whole raise -> re-warm -> retry cycle completes before the other seven even send, so only 9 POSTs happen, not 16 — the request count is an interleaving detail and is asserted as `>= 9`; the deterministic "all eight answer cold" proof stays in the gate's own test, where the yields are controlled.
  - `test_a_cancelled_waiter_leaves_the_instance_usable` (was `…_first_caller_…`) now asserts `resolve_calls == 1` BEFORE cancelling, so the winner is provably inside the warm body and the cancelled task provably reached the lock — the previous version could have passed with the cancelled task never touching the gate (the `@track` decorator sits in `embed`'s path, so it drives `ensure_warm` directly instead).
  - `test_a_budget_of_zero_polls_nothing_and_says_so` — the ONE path that reports `last result: no response` (budget spent before the first GET). `modal.warmup_deadline_s` is `ge=1`, but `poll_health` takes any float, so the branch is real, not dead; the constant now carries that comment.

### [Tester] 2026-09-20 15:20 — QA

**Commands run (LOCAL env; `make env-status` confirmed `local` throughout)**
```
git status --short
git diff --stat
make memory-format-check && make memory-lint-check
make pre-commit
make memory-tests
make memory-tests PYTEST_ADDOPTS="tests/unit/config/test_settings_credentials_only.py tests/unit/models/test_modal_warmup.py tests/unit/models/test_modal_embedding.py tests/unit/models/test_modal_server.py tests/unit/scripts/test_modal_embedding_model_script.py -v"
make memory-tests PYTEST_ADDOPTS="tests/unit/models/test_modal_warmup.py -q"      # timing
make memory-tests PYTEST_ADDOPTS="tests/unit/models/test_modal_embedding.py -q"   # timing
make memory-tests PYTEST_ADDOPTS="tests/unit/models/test_modal_server.py -q"      # timing
grep -n "wait_until_healthy\|_init_lock\|_ensure_initialised\|health_timeout" apps/memory/src apps/memory/scripts apps/memory/tests -r
grep -n "\.warmed\s*=" apps/memory/tests apps/memory/src -r
grep -rln "^import aiohttp\|^    import aiohttp\| import aiohttp" apps/memory/src apps/memory/tests apps/memory/scripts
grep -n "aiohttp" apps/memory/pyproject.toml
grep -rn "max_retries" apps/memory/src apps/memory/tests
uv run python -c "import sys, tree.models.get_model; print('modal' in sys.modules)"                      # (apps/memory, via uv)
uv run python -c "import sys, tree.models.modal_warmup; print('modal' in sys.modules, 'tree.config' in sys.modules)"
uv run python -c "from tree.config.app_config import app_config; print(app_config.modal.warmup_deadline_s)"                      # default, real boot loader
TREE_MODAL__WARMUP_DEADLINE_S=45 uv run python -c "from tree.config.app_config import app_config; print(app_config.modal.warmup_deadline_s)"   # override, real boot loader
TREE_MODAL__WARMUP_DEADLINE_S=0  uv run python -c "from tree.config.app_config import app_config"        # ge=1 enforced at real boot
# Adversarial script (scratch dir, deleted before finishing; see "E2E adversarial pass" below)
uv run python <scratchpad>/adversarial_warmup.py
uv run python <scratchpad>/adversarial_warmup2.py
```
Read (not executed): `/Users/pauliusztin/Documents/01-Projects/scrabble/scrabble/src/pulse/warmup.py`, `.../pulse/embeddings/modal.py`, `apps/memory/src/tree/models/modal_warmup.py`, `modal_embedding.py`, `modal_server.py`, `exceptions.py`, `app_config.py` (env-override mechanism), and every changed/new test file in full.

**Test summary**
- Format / lint / pre-commit: PASS (`307 files already formatted`, `All checks passed!`, pre-commit `prettier / ruff check / ruff format / biome check` all Passed)
- Unit tests: 3551 passed / 0 failed (`make memory-tests`, 45.5s)
- Modal-related test files alone: `test_modal_warmup.py` 41 passed in 1.00s, `test_modal_embedding.py` 54 passed in 0.95s, `test_modal_server.py` 22 passed in 0.64s — no real sleeping in any of them.
- Secret-shaped scan: `test_settings_credentials_only.py` + all Modal test files run together first → 194 passed, 0 failures (no secret-shaped strings surfaced; the fake pair `wk-1`/`ws-2` is the only token literal in the suite).
- Warnings: 1 line total across the whole run — `opik/rest_api/core/pydantic_utilities.py:13: UserWarning: Core Pydantic V1 functionality isn't compatible with Python 3.14`. Confirmed pre-existing and unrelated to this diff: it fires on a bare `uv run python -c "import tree.models.get_model"` with none of this PR's code in the import path. Not counted against this task.

**E2E adversarial pass** (script run from `apps/memory` via `uv run python`, fake `sleep`/`clock`/`fetch` plus two real 127.0.0.1 servers; zero Modal/network calls)
- Happy path: `poll_health` on `[503, 503, 200]` with a fake clock → returns `elapsed=12.5`, `sleep==[5.0, 7.5]`, first fetch at t=0 (matches AC #1). PASS.
- Break path 1 (boundary: `deadline_s` exactly equal to the elapsed time after the sleep) → one fetch, one sleep clipped to exactly `5.0`, `ExtractionError` at `clock.now == 5.0`, no overshoot. PASS.
- Break path 2 (boundary: `deadline_s=3.0` < `INITIAL_INTERVAL_S=5.0`) → sleep clipped to `3.0`, one fetch, deadline message says `deadline 3s`. PASS.
- Break path 3 (malformed/hostile: HTTP 429, not in the SWE's `[401,403,404,302,418]` parametrize) → `ModelError` after exactly 1 fetch, zero sleeps — 429 is correctly fail-fast, never treated as booting. PASS (gap noted below: not literally in the parametrize list; behavior is correct by code inspection and by this run).
- Break path 4 (failure mode: a REAL local aiohttp server that sleeps 2s past a 0.3s per-GET timeout) → `TimeoutError` raised in 0.30s (bounded by the per-GET timeout, not the handler's sleep). PASS.
- Break path 5 (failure mode: connection refused, real socket, nothing listening on the port) → `aiohttp.ClientConnectionError` subtype (`ClientConnectorError`), which `poll_health` classifies as booting. PASS.
- Break path 6 (state edge: `asyncio.CancelledError` delivered while `poll_health` is inside its injected `sleep`) → propagates cleanly out of the coroutine, not swallowed, task ends in `CancelledError`. PASS.
- Break path 7 (state edge: 50 concurrent first `ensure_warm()` callers) → `warm.calls == 1`. PASS.
- Break path 8 (re-entrancy: the `warm` body itself calls `gate.call()` on the SAME gate while the lock is held for the first warm) → **DEADLOCKS** (`asyncio.wait_for(..., timeout=2.0)` times out; `asyncio.Lock` is not reentrant). Not a shipped-code path today (`ModalEmbeddingModel._warm` never touches `self._gate`) and not reachable from any current caller — see ruling below. Companion check: `fn()`-side reentrancy (an already-warmed `call()` invoking `call()` again) works fine, since the fast path needs no lock.
- Break path 9 (disclosed point 3, timed): 5 concurrent `ensure_warm()` callers against a `warm()` that always fails after a fixed delay → all 5 pay the FULL delay serially behind the lock (measured ~5× the single-warm delay, not 1×), confirming the "N × deadline" serial-failure cost is real, not theoretical.
- Config real-loader check (not just `load_app_config(path)` in isolation): booted the process-global `app_config` singleton three ways — no override → `600.0`; `TREE_MODAL__WARMUP_DEADLINE_S=45` → `45.0`; `=0` → `ValidationError` raised at import time (module-level `app_config = load_app_config()` in `app_config.py`), confirming `ge=1` is enforced on the real boot path, not just the unit test's direct `load_app_config()` call.
- Hygiene: `import tree.models.modal_warmup` → `'modal' not in sys.modules` AND `'tree.config' not in sys.modules` (the docstring's stronger claim, not only the SWE's own subprocess test which checks `modal` alone) — confirmed both hold.

**Rulings on the disclosed points**
1. **Burst assertion `>= 9` too loose?** Correct that `>= 9` alone would not catch a regression to 16 POSTs. But the metric that actually matters — how many times the server is BOOTED — is pinned exactly: `resolve_calls == 2`, `poll_calls == 2`, `models_calls == 2` (not `>=`), so a regression to "9 boots" (one per caller) is caught deterministically at the client level. The fully deterministic "all eight answer cold" proof lives in the gate's own `test_eight_concurrent_cold_callers_log_one_warning` (`sum(call.calls) == 16`, `warm.calls == 2`, exactly one WARNING) — verified passing. Acceptable as designed; not a gap.
2. **`_loop_lock()` before the fast path.** Confirmed correct and deliberate (verified `test_gate_survives_a_second_event_loop` passes, and independently confirmed no test double anywhere sets `.warmed = True` from outside a running loop — `grep -n "\.warmed\s*=" apps/memory/src apps/memory/tests` returns only the four assignments inside `modal_warmup.py` itself). `test_observability_instrumentation.py` was correctly updated to build a real `WarmGate` over an `AsyncMock` rather than poke the flag. No gap.
3. **Failed warm is per-waiter, not shared.** Confirmed real via a timed adversarial run (break path 9): N concurrent first callers against a truly dead server cost N × 600s of SERIAL waiting behind the lock before the last one fails — this is not bounded by anything in #144. It is, however, an exact match for pulse's own `WarmGate.ensure_warm()` (identical check → lock → re-check → warm shape) and #140's prior `_init_lock` behavior — not a regression introduced by this port. The MCP path (CLAUDE.md: one instance held for the process lifetime, no pre-warm gate in front of tool calls) is the real exposure, not #149's pipeline (which pre-warms once, ahead of any concurrent fan-out, converting the worst case into "one pre-warm failure blocks the pipeline before fan-out starts" rather than N serial 600s waits). The exposure is latency-only, not a leak or corruption: sessions are scoped per-GET (`async with aiohttp.ClientSession`), waiters only await a lock, and the first caller already times out client-side well before 600s in practice. Ruling: acceptable for #144 as a faithful port; **worth a one-line flag to whoever owns the MCP composition (#148/#149 territory) that N concurrent first-time MCP tool calls against a dead server serialize their failures**, not a #144 blocker.
4. **OpenAI SDK's own retries / `max_retries` not set.** Confirmed: `AsyncOpenAI(base_url=..., api_key=...)` in `modal_embedding.py` sets no `max_retries`, so the SDK default (2) applies in production; only the test's `wire` fixture pins `max_retries=0`, and its own comment says why ("a retried request would inflate the request count the wire tests assert on"). Pulse's own `embeddings/modal.py` and `models/modal.py` likewise never set `max_retries` — this is a faithful port, not a lost guarantee. SDK retries multiply wire *requests*, not *wakes*: the underlying container boot is still triggered once by whichever request lands first, and every AC-relevant assertion pins poll/warm counts (the thing that costs GPU-seconds), not wire-request counts. Worth writing down for whoever reads "9 POSTs" later: production traffic during a cold window is realistically ~3x that number per caller (1 + 2 SDK retries), so that number must never be read as a production traffic estimate.
5. **`aiohttp` undeclared in `apps/memory/pyproject.toml`.** Confirmed by `grep -n "aiohttp" apps/memory/pyproject.toml` (zero hits) and `uv.lock` (present transitively). `modal_warmup.py` is a new direct importer, and its own design intent (enforced by `test_modal_warmup_does_not_import_modal`, and independently reconfirmed here to also exclude `tree.config`) is to be usable without the `modal` SDK in the picture — a goal partially undermined if `aiohttp` only reaches the venv because `modal` pulls it in. Not on #144's file list (no `pyproject.toml` change was scoped or required by the ACs) — non-blocking for this task, forwarded to the PA to rule on declaring it directly.

**Acceptance criteria**
- [x] PASS — `TestPollHealth` (all 8+ cases: schedule/cap, transport-errors-as-booting, closed 4xx/401/403 classification incl. redirect/payload errors, deadline math, clipped last sleep, caplog with no token) — `apps/memory/tests/unit/models/test_modal_warmup.py::TestPollHealth` all green; independently reconfirmed the deadline-boundary, sub-first-interval, 429, real-timeout, real-connection-refused and cancellation cases with a standalone adversarial script (above).
- [x] PASS — `TestWarmGate…` (single-flight, failed-warm-cold, cold-again re-warm+retry with ONE warning under 8 concurrent, 4xx passthrough, `APITimeoutError` is cold) — `test_modal_warmup.py::TestWarmGateEnsureWarm`/`TestWarmGateCall` all green; independently reconfirmed 50-concurrent-callers and the serial-failure-cost timing.
- [x] PASS — `test_gate_survives_a_second_event_loop` — green; hint reset and one warm per loop confirmed by reading the assertion and passing test.
- [x] PASS — `TestConcurrentInitialisation` retargeted (1/1/1/1, 3/8/10 callers) — green; `grep` for `_init_lock|_ensure_initialised|health_timeout|wait_until_healthy` across `src/`, `scripts/`, `tests/` leaves only the deploy scripts' own `health_timeout` (owned by #146/#147, explicitly out of scope) and a docstring mention.
- [x] PASS — `test_embed_rewarms_once_when_cold_again`, `test_embed_wraps_non_cold_errors`, `test_empty_batch_never_warms` — all green in `test_modal_embedding.py::TestWarmAtUse`.
- [x] PASS — `test_model_exposes_ensure_warm_and_warm_key` — green.
- [x] PASS — `TestSmokeTest` (now `TestSmokeTestWarmUp` + the two script exit-1 tests) — `smoke_test` calls `poll_health` with `deadline_s == app_config.modal.warmup_deadline_s` when none passed, an explicit `deadline_s` wins, a 401 `ModelError` and a spent-budget `ExtractionError` both reach the driver as exit 1 with the poll's message and without the HF_TOKEN hint — all green.
- [x] PASS — `test_modal_warmup_deadline_default_and_override` — green; independently reconfirmed through the REAL process-global `app_config` boot path (not just `load_app_config(path)`): no override → `600.0`, `TREE_MODAL__WARMUP_DEADLINE_S=45` → `45.0`, `=0` → `ValidationError` raised at import time.
- [x] PASS — `test_modal_warmup_does_not_import_modal` — green; independently reconfirmed the stronger claim from the module's own docstring ("No `tree.config` import") also holds.
- [x] PASS — `grep -c "TREE_MODAL__WARMUP_DEADLINE_S" apps/memory/README.md` → 1 (README step 3 carries the one-off-budget sentence).
- [x] PASS — `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green (LOCAL env, 3551 passed, 0 failed).

**Evidence**
```
$ make memory-tests
============================ 3551 passed in 45.53s =============================

$ make memory-tests PYTEST_ADDOPTS="tests/unit/config/test_settings_credentials_only.py tests/unit/models/test_modal_warmup.py tests/unit/models/test_modal_embedding.py tests/unit/models/test_modal_server.py tests/unit/scripts/test_modal_embedding_model_script.py -v"
============================= 194 passed in 1.51s ==============================

$ uv run python -c "import sys, tree.models.modal_warmup; print('modal' in sys.modules, 'tree.config' in sys.modules)"
False False

$ TREE_MODAL__WARMUP_DEADLINE_S=0 uv run python -c "from tree.config.app_config import app_config"
pydantic_core._pydantic_core.ValidationError: 1 validation error for AppConfig
modal.warmup_deadline_s
  Input should be greater than or equal to 1 [type=greater_than_equal, ...]
```

**Other issues found**
- 429 is not in the `poll_health` fail-fast parametrize list (`[401, 403, 404, 302, 418]`); behavior is verified correct by both code inspection and an independent run, but the AC's own "429 must NOT be treated as booting" idea is only pinned at the SDK/gate layer (`test_a_4xx_passes_through_unchanged`), not at the raw-HTTP poller layer. Cheap to add; not blocking.
- `test_empty_batch_never_warms` only covers a FRESH (never-warmed) instance; the empty-batch guard is provably the first statement in `embed()` (reads `if not texts: return []` before anything gate-related), so a warmed instance is covered by inspection but not by an explicit second test. Not blocking.
- Found via adversarial testing, NOT a shipped-code defect today: `WarmGate.ensure_warm()`/`.call()` deadlock if the composed `warm` body itself calls `gate.call()` on the same gate while its own first-warm lock is held (`asyncio.Lock` is not reentrant). `ModalEmbeddingModel._warm()` never does this, so there is no live path to it in this PR. Flagging for whoever composes the gate next (#148 `ModalLLM`, and any future composer) to keep `warm` bodies from routing back through their own gate — a guard that raises a clear error instead of hanging would be a much friendlier failure mode than the silent 2-hour-MCP-hang this would otherwise be, but is out of scope for #144 to add given no AC requires reentrancy safety and no current caller triggers it.
- Pre-existing (unrelated to this diff, forwarded to PA per ruling 5): `aiohttp` is imported directly by 4 modules under `tree.models` (including the new `modal_warmup.py`) but is absent from `apps/memory/pyproject.toml`'s direct dependencies; it currently arrives only transitively via `modal`.
- The one `UserWarning` seen anywhere in the suite (`opik`/pydantic-v1/Python 3.14) is confirmed pre-existing and independent of this diff — reproduced on a bare `import tree.models.get_model`.

**VERDICT: PASS**
