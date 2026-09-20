---
id: 144-modal-warmup-poller-and-warm-gate
status: pending
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

- [ ] `tests/unit/models/test_modal_warmup.py::TestPollHealth` (all with injected `fetch`/`sleep`/`clock`, a fake clock that advances by the slept amount; the whole file runs in < 1 s):
  - `[503, 503, 200]` -> returns; `sleep` was called with `[5.0, 7.5]`; the first `fetch` happened before any sleep.
  - 8 x `503` then `200` -> sleeps are `[5.0, 7.5, 11.25, 15.0, 15.0, 15.0, 15.0, 15.0]` (cap at 15).
  - `fetch` raising `aiohttp.ClientConnectionError` then `asyncio.TimeoutError` then `200` -> returns (both are "booting").
  - `401`, `403`, `404`, `302`, `418` -> `ModelError` after exactly 1 `fetch`, message contains the URL and `giving up after 1 attempt`; 401/403 also contain `MODAL_PROXY_TOKEN_ID`.
  - `fetch` raising `aiohttp.TooManyRedirects`-class / `aiohttp.ClientPayloadError` -> `ModelError` after 1 attempt.
  - always `503` with `deadline_s=600` -> `ExtractionError`, `status_code == 503`, message contains `gave up after` and `last result: HTTP 503`; total slept time <= 600; the last sleep is clipped (never sleeps past the deadline).
  - `deadline_s=1` with `503` -> exactly 1 fetch, then the deadline error.
  - caplog: one `Still cold (HTTP 503) at <url> — ` line PER cold poll, one `Warm: <url> answered HTTP 200 after` line; the string `Bearer` and the fake token appear in NO log record.
- [ ] `TestWarmGate` (fake `warm` + fake call factories, no SDK call): 8 concurrent `ensure_warm()` -> `warm` awaited once; a raising `warm` leaves `warmed is False` and the next `ensure_warm()` awaits it again; `call` with `fn` raising `openai.InternalServerError` once -> `warm` awaited twice in total, `fn` twice, result returned, exactly ONE `Cold again` WARNING even with 8 concurrent callers; `fn` cold twice -> `ExtractionError` with `still answered` and `status_code == 503`; re-warm failing -> the prefixed error; `openai.RateLimitError` (429) and `openai.BadRequestError` -> propagate unchanged, `warm` awaited once, no WARNING; `openai.APITimeoutError` IS cold.
- [ ] `test_gate_survives_a_second_event_loop`: the same `WarmGate` used under two consecutive `asyncio.run(...)` calls, each with 2 contending callers, raises nothing and awaits `warm` once PER loop.
- [ ] `TestConcurrentInitialisation` (retargeted) green with the 1/1/1/1 counts above; `grep -n "_init_lock\|_ensure_initialised\|health_timeout\|wait_until_healthy" apps/memory/src apps/memory/scripts apps/memory/deploy -r` -> 0 hits in `src/` and `scripts/` (the two deploy scripts keep their own in-container `HEALTH_TIMEOUT` until #146/#147).
- [ ] `test_embed_rewarms_once_when_cold_again`: a warmed `ModalEmbeddingModel` whose `embeddings.create` raises `openai.InternalServerError(503)` once -> `poll_health` called twice in total, the vectors are returned, one WARNING. `test_embed_wraps_non_cold_errors`: a `BadRequestError` -> `ExtractionError("Embedding call failed: …")`, `poll_health` called once. `test_empty_batch_never_warms`: `embed([])` -> `[]`, `poll_health` not called.
- [ ] `test_model_exposes_ensure_warm_and_warm_key`: `warm_key == "ep-tree-qwen3-embedding-0-6b"`; `ensure_warm()` twice -> one poll.
- [ ] `TestSmokeTest`: `smoke_test` calls `poll_health` with `deadline_s == app_config.modal.warmup_deadline_s` when none is passed and logs `health 200 after`; a poll `ModelError` (401) and a poll `ExtractionError` (deadline) both reach the driver's `test` command as exit 1 with the poll's message.
- [ ] `test_modal_warmup_deadline_default_and_override`: default `600.0`; `TREE_MODAL__WARMUP_DEADLINE_S=45` -> `45.0`; `0` -> `ValidationError` (`ge=1`). `frozen_config.yaml` carries the key.
- [ ] `test_modal_warmup_does_not_import_modal`: importing `tree.models.modal_warmup` in a subprocess leaves `"modal" not in sys.modules`.
- [ ] `grep -c "TREE_MODAL__WARMUP_DEADLINE_S" apps/memory/README.md` >= 1.
- [ ] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green (LOCAL env).

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
