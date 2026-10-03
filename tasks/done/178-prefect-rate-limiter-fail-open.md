---
id: 178-prefect-rate-limiter-fail-open
status: done
feature: horizon-mcp-fixes
---

# The Voyage `rate_limit` acquire fails open: a Prefect limiter that cannot be reached logs one WARNING and the embedding call proceeds

Tags: `models`, `prefect`, `voyage`, `mcp`
Depends on: —
Blocks: —
Implements: ADR-013 §5 (amends ADR-002 §1's "acquire one shared `voyage-embeddings` slot per POST")

## Problem

Both Voyage clients call `await rate_limit(_VOYAGE_EMBED_LIMIT, occupy=1, strict=False)` before every
POST (`apps/memory/src/tree/models/voyage_multimodal_embedding.py` ~L223,
`apps/memory/src/tree/models/voyage_embedding.py` ~L261 — the only two call sites). Live on Horizon a
stale `PREFECT_API_KEY` made the acquire raise `ConcurrencySlotAcquisitionError("Unable to acquire
concurrency slots on ['voyage-embeddings']")` (Prefect wraps the underlying 401 as `__cause__`), the
clients' outer `except Exception` turned it into `ExtractionError("Voyage text-embeddings call failed:
Unable to acquire…")`, and `search_memory` answered `search_unavailable`. The limiter is a courtesy to
the shared free-tier key; Voyage's own 429 backoff still protects the quota.

## Scope

**Human decision (final):** FAIL OPEN + WARN — on auth / connection / HTTP errors from the limiter, log a
WARNING ("Prefect rate limiter unavailable: <cause> — embedding without throttle") and call Voyage anyway.

1. **One helper, two call sites.** New module `apps/memory/src/tree/models/throttle.py` (name is the
   SWE's; it must live under `tree.models`, which both clients already import from) exposing
   `VOYAGE_EMBED_LIMIT = "voyage-embeddings"` (moved; the two `_VOYAGE_EMBED_LIMIT` constants are deleted)
   and `async def acquire_voyage_slot() -> None`:
   ```python
   try:
       await rate_limit(VOYAGE_EMBED_LIMIT, occupy=1, strict=False)
   except (ConcurrencySlotAcquisitionError, AcquireConcurrencySlotTimeoutError,
           PrefectHTTPStatusError, httpx.HTTPError) as exc:
       cause = exc.__cause__ or exc
       logger.warning("Prefect rate limiter %r unavailable: %s — embedding without throttle",
                      VOYAGE_EMBED_LIMIT, cause)
   ```
   (`ConcurrencySlotAcquisitionError` / `AcquireConcurrencySlotTimeoutError` come from
   `prefect.concurrency.asyncio`; verified in prefect 3.6.19 that `aacquire_concurrency_slots` wraps EVERY
   exception in the former and timeouts in the latter.) Anything else propagates unchanged. The absent-limit
   path (`strict=False`, Prefect's own "do not exist - skipping" warning) is untouched.
2. Both clients replace their inline `rate_limit(...)` with `await acquire_voyage_slot()` — still INSIDE the
   429-backoff loop, still after the empty-input short-circuit (the ADR-002 §1 placement comment moves to
   the helper's docstring and the call sites keep a one-line pointer).
3. Comments elsewhere that claim the limiter is load-bearing — grep `voyage-embeddings` across
   `apps/memory/src/tree` (`memory/embedding_text.py`, `memory/graph/add_entity.py`, `config/default.yaml`
   `concurrency:` block, `README.md`) — gain the sentence "fail-open: an unreachable limiter warns and the
   call proceeds (task 178)". No other `rate_limit` / `concurrency(` call sites exist (grep confirmed).
4. **Docs:** ADR-002 §1 Status-line note (ADR-013 "Amendments"); `docs/glossary.md` has no limiter row —
   none added.
5. **Tests** (`tests/unit/models/test_voyage_embedding.py`, `…_multimodal_…`, new `test_throttle.py`):
   `rate_limit` patched to raise `ConcurrencySlotAcquisitionError("Unable to acquire concurrency slots on
   ['voyage-embeddings']")` with `__cause__ = PrefectHTTPStatusError…(401)` → `embed` returns the vectors
   (aiohttp patched) and caplog has exactly one WARNING containing `embedding without throttle` and `401`;
   `AcquireConcurrencySlotTimeoutError` and `httpx.ConnectError` variants; a `RuntimeError` from the
   limiter still surfaces as today's `ExtractionError`; the happy path acquires once per POST attempt and
   a 429 retry re-acquires (existing tests re-pointed at the helper).
6. **Live verification** (LOCAL env): the same bogus-key server as task 177
   (`PREFECT_API_KEY=pnu_bogus` against the cloud `PREFECT_API_URL`) → `search_memory(query="…")` answers
   `outcome: found` (or `nothing_found`), NOT `search_unavailable`; the server log shows the single WARNING.
   Then the normal local server: `search_memory` works and no WARNING appears. Paste both into the Log.

## Acceptance criteria

- [x] One `acquire_voyage_slot` helper; `grep -rn "rate_limit(" apps/memory/src/tree` matches only the helper.
- [x] A limiter raising `ConcurrencySlotAcquisitionError` (401 cause), `AcquireConcurrencySlotTimeoutError`,
      `PrefectHTTPStatusError` or `httpx.HTTPError` → ONE WARNING `Prefect rate limiter 'voyage-embeddings'
      unavailable: <cause> — embedding without throttle` and the Voyage POST proceeds; `embed` returns vectors.
- [x] Any other limiter exception still fails the call as before.
- [x] The acquire still happens once per real POST attempt, inside the 429 loop, never for empty input.
- [x] Comments/README naming the limiter mention fail-open; ADR-002 Status-line note applied.
- [x] `make memory-format-check`, `make memory-lint-check`, `make pre-commit`, `make memory-tests` green.
- [x] Live (local, bogus cloud key): `search_memory` answers a result and the WARNING is in the server log;
      evidence in the Log.

## User Stories

### Story: Horizon's Prefect key is stale but search still works
1. `PREFECT_API_KEY` on Horizon is expired.
2. The user asks "what did I save about Beanie?"; the model calls `search_memory(query="beanie odm")`.
3. The server logs `WARNING Prefect rate limiter 'voyage-embeddings' unavailable: Client error '401 Unauthorized' … — embedding without throttle`, embeds the query with Voyage and answers `{"parents": [...], "outcome": "found", "search_mode": "hybrid"}`.

### Story: Prefect Cloud is down during an offline backfill
1. The indexing phase runs on a worker whose Prefect API connection drops mid-batch (`httpx.ConnectError` from the limiter).
2. The batch logs one WARNING per failed acquire and keeps embedding; a Voyage 429 still sleeps through the backoff schedule as today.

### Story: The limiter simply does not exist (fresh local Prefect)
1. The operator never ran `prefect gcl create voyage-embeddings …`.
2. Behaviour is unchanged: Prefect's own "Concurrency limits ['voyage-embeddings'] do not exist - skipping acquisition" warning, no fail-open WARNING, the call proceeds.

---

Blocked by: (none)

## Log

### [PA] 2026-10-03 17:50 — Grooming

**Summary**
The Prefect global-concurrency acquire in front of every Voyage POST becomes advisory: when the limiter
cannot be reached (auth, connection, HTTP, timeout) the client warns once and embeds anyway.

**Key decisions**
- A single helper in `tree.models` replaces the two inline calls — the spec's "apply consistently if they
  share the helper" is made true by giving them one.
- Catch Prefect's WRAPPER classes (`ConcurrencySlotAcquisitionError`, `AcquireConcurrencySlotTimeoutError`),
  not the 401 itself — the 401 arrives as `__cause__`, which the WARNING prints.
- A non-infra exception from the limiter still fails the call: fail-open is for "cannot reach", not bugs.

**Dependencies**
- None (shares task 177's bogus-key verification recipe).

**User stories**
- 3 stories: stale key on Horizon, Prefect down mid-backfill, no limiter configured.

Ready for implementation.

### [SWE] 2026-10-03 19:53 — Implementation

**Files modified**
- `apps/memory/src/tree/models/throttle.py` (new) — `VOYAGE_EMBED_LIMIT` + `acquire_voyage_slot()`: the one `rate_limit(...)` call site; catches `ConcurrencySlotAcquisitionError` / `AcquireConcurrencySlotTimeoutError` / `PrefectHTTPStatusError` / `httpx.HTTPError`, logs ONE WARNING naming the `__cause__` (falls back to the wrapper's message when the cause is empty, e.g. a bare `TimeoutError()`), returns. The ADR-002 §1 placement rationale moved into its docstring.
- `apps/memory/src/tree/models/voyage_embedding.py`, `voyage_multimodal_embedding.py` — `_VOYAGE_EMBED_LIMIT` + `rate_limit` import deleted; the inline acquire becomes `await acquire_voyage_slot()` at the same spot (inside the 429 loop, after the empty-input return), with a one-line pointer comment.
- `apps/memory/src/tree/memory/embedding_text.py` (2 comments), `memory/graph/add_entity.py`, `config/default.yaml` (`concurrency:` block), `config/app_config.py` (2 comments that also name the GCL) — gained "fail-open: an unreachable limiter warns and the call proceeds (task 178)". No README names the limiter (grep), so none edited.
- `docs/adrs/002_pipeline_concurrency_and_voyage_rate_limiting.md` — Status-line note, verbatim from ADR-013 "Amendments to apply".
- `apps/memory/tests/unit/conftest.py` — the autouse `_noop_voyage_rate_limit` now patches the single target `tree.models.throttle.rate_limit`.
- `apps/memory/tests/unit/models/conftest.py` — new `limiter_401` fixture: `ConcurrencySlotAcquisitionError` with a `PrefectHTTPStatusError.from_httpx_error(...)` 401 as `__cause__` (httpx's real "Client error '401 Unauthorized' …" message).
- `apps/memory/tests/unit/models/test_throttle.py` (new) — happy path (one acquire, args, no WARNING); 401-cause; timeout; bare + wrapped `ConnectError`; `ReadTimeout`; bare `PrefectHTTPStatusError` → exactly one WARNING; `RuntimeError` propagates with no WARNING.
- `apps/memory/tests/unit/models/test_voyage_embedding.py`, `test_voyage_multimodal_embedding.py` — chokepoint tests re-pointed at `tree.models.throttle.rate_limit`; new `Test…RateLimiterFailsOpen`: 401 → vectors + POST sent + one WARNING containing `401`; timeout / `ConnectError` → vectors + one WARNING; `RuntimeError` → today's `ExtractionError("… call failed: limiter bug")`, no POST.
- `apps/memory/tests/unit/memory/graph/test_add_entity.py` — cache hit/miss spies re-pointed (the two per-client spies collapse into one).

**Tests**
- Unit: 4807 passing, 0 failing (`make memory-tests`, env-status local). The 12 new fail-open tests were red first (exception propagated → `ExtractionError`), green after the except block.
- Integration: N/A — no integration suite (AGENTS.md).

**Acceptance criteria**
- [x] One helper; `grep -rn "rate_limit(" apps/memory/src/tree` → only `src/tree/models/throttle.py:58` — verified by grep.
- [x] 401-cause / timeout / `PrefectHTTPStatusError` / `httpx.HTTPError` → one WARNING, POST proceeds, vectors returned — `test_throttle.py::TestAcquireVoyageSlot::*`, `test_voyage_embedding.py::TestVoyageTextRateLimiterFailsOpen::*`, `test_voyage_multimodal_embedding.py::TestVoyageMultimodalRateLimiterFailsOpen::*`.
- [x] Other limiter exceptions still fail — `test_any_other_limiter_exception_propagates`, `test_other_limiter_exception_still_fails_the_call` (both clients).
- [x] Once per POST attempt, inside the 429 loop, never for empty input — existing `Test…RateLimitChokepoint` (3 per client) re-pointed at the helper; `TestCachedDedupAcquiresNoRateLimitSlot` still green.
- [x] Comments mention fail-open; ADR-002 Status-line note applied.
- [x] format-check / lint-check / pre-commit / memory-tests green.
- [x] Live bogus-key + local evidence below.

**Evidence**
```
$ make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check
331 files already formatted
All checks passed!
$ make pre-commit
prettier....Passed  ruff check....Passed  ruff format....Passed  biome check (harness)....Passed
$ make env-status && make memory-tests
Env target: local (.env)
============================ 4807 passed in 55.85s =============================

# Live 1 — LOCAL env, Prefect Cloud workspace URL from .env.prod, PREFECT_API_KEY=pnu_bogus
#   (server launched as .venv/bin/python scripts/serve_mcp.py --transport streamable-http, FASTMCP_PORT=8765,
#    with .env sourced first and the two Prefect vars exported after it; stopped with kill <PID>)
$ uv run fastmcp call http://127.0.0.1:8765/mcp --auth none search_memory 'query=beanie odm'
{"result": "{\"parents\": [], \"outcome\": \"nothing_found\", \"search_mode\": \"hybrid\"}"}   # 1.46s wall
$ uv run fastmcp call http://127.0.0.1:8765/mcp --auth none search_memory 'query=structured outputs in production'
outcome: found | search_mode: hybrid | parents: 9 | top title: Structured Outputs: The Silent Hero of Production AI
# server log (account/workspace ids redacted):
19:52:28.581 | ERROR   | GlobalEventLoopThread | prefect._internal.concurrency - Service 'ConcurrencySlotAcquisitionService' failed to process item (1, 'rate_limit', None, None, <Future at 0x1179f4150 state=finished raised PrefectHTTPStatusError>)
Prefect rate limiter 'voyage-embeddings' unavailable: Client error '401 Unauthorized' for url 'https://api.prefect.cloud/api/accounts/<redacted>/workspaces/<redacted>/v2/concurrency_limits/increment'
Response: {'detail': 'Invalid authentication credentials'}
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/401 — embedding without throttle
vector leg: 40 candidate(s), 0 kept at min_vector_score=0.70 (top=0.606)
No child hits for query (outcome=nothing_found, search_mode=hybrid): beanie odm
# 3 search_memory calls → 3 fail-open WARNINGs, 3 "vector leg" lines (Voyage embedded every time)

# Live 2 — normal LOCAL server (local Prefect at 127.0.0.1:4200)
$ uv run fastmcp call http://127.0.0.1:8765/mcp --auth none search_memory 'query=structured outputs in production'
outcome: found | search_mode: hybrid | parents: 10 | top title: Structured Outputs: The Silent Hero of Production AI
fail-open warnings: 0   # and no Prefect concurrency ERROR line
vector leg: 40 candidate(s), 27 kept at min_vector_score=0.70 (top=0.784)
```

**Notes**
- The 401 fails fast (call answered in ~1.5s): prefect's `ConcurrencySlotAcquisitionService` retries only 423s.
- Prefect itself logs an ERROR line from `prefect._internal.concurrency` ("Service 'ConcurrencySlotAcquisitionService' failed to process item …") before our WARNING. That is Prefect's own logger, not ours; left alone (silencing a Prefect-internal logger is out of scope).
- Intent, not slop: prefect's `aacquire_concurrency_slots` wraps EVERY exception in `ConcurrencySlotAcquisitionError`, so a genuine bug inside Prefect's acquire path also fails open in production. The "RuntimeError still fails" guarantee holds for exceptions raised outside that wrapper (the tests patch `rate_limit` directly). This is what the spec chose ("catch the wrapper classes").
- `PrefectHTTPStatusError` is already an `httpx.HTTPError` subclass; kept by name in the catch tuple because ADR-013 §5 names it.
- One WARNING per failed acquire (Story 2) — on a long backfill with Prefect down this is one line per POST attempt; no rate-limiting of the warning itself was added (not asked for).
- Possible follow-up (not touched): `docs/notes/prefect-execution-topologies.md` L283–286 describes the `voyage-embeddings` GCL without the fail-open caveat.
- Story 3 (absent limit) is untouched code-wise: `strict=False` still returns normally and our except never fires; covered by the happy-path test asserting no fail-open WARNING.
- The WARNING is ONE log record but prints on three lines: `str(PrefectHTTPStatusError)` embeds `\nResponse: {...}\nFor more information check: …`. Kept deliberately (the spec's code prints the cause verbatim, and the `Response: {'detail': 'Invalid authentication credentials'}` body is what tells an operator it is a key problem, not a URL problem). A single-line `grep` for the full message will not match; grep `embedding without throttle` or `Prefect rate limiter`.
- For PA: the ADR-002 Status line now reads "…every other decision stands — §1's per-POST … FAILS OPEN …". That is ADR-013 L184's text appended verbatim; the join reads slightly oddly, left as written (SWE is read-only on ADR prose).

### [Tester] 2026-10-03 20:20 — QA

**Test summary**
- Format / lint / pre-commit: PASS (331 files formatted, ruff clean, prettier/ruff/biome hooks Passed)
- Unit tests: 4807 passed / 0 failed (`make memory-tests`, env-status local)
- Integration tests: N/A (no integration suite per AGENTS.md)
- Warnings: 0

**E2E adversarial pass** (in-process scripts with real Voyage calls + fake limiter HTTP server; live MCP server)
- Happy path, limiter absent (local Prefect, no GCL): text + multimodal `embed` → vectors (1024-d), no fail-open WARNING (PASS)
- Happy path, limiter present (`prefect gcl create voyage-embeddings --limit 1 --slot-decay-per-second 0.1`): text embed 1.2s, multimodal embed 12.7s (blocked on the shared slot) → acquire still happens and throttles; no WARNING (PASS). Limit deleted afterwards (`gcl ls` → none, as before).
- Break 1, hostile limiter responses (fake server, `PREFECT_API_KEY=pnu_bogus`): 401 → vectors + 1 WARNING per POST naming "401 Unauthorized … Invalid authentication credentials"; 500 → vectors + WARNING "Server error '500 …'"; 200 with non-JSON body → vectors + WARNING "Expecting value: line 1 column 1" (JSONDecodeError caught via Prefect's wrapper) (PASS)
- Break 2, unreachable limiter (`PREFECT_API_URL=http://127.0.0.1:9/api`, connection refused): vectors + WARNING "All connection attempts failed — embedding without throttle" (PASS). Cost: ~1.5-2.5s extra per POST.
- Break 3, hanging limiter (accepts the connection, never replies): fails open only after Prefect's own retry/timeout budget (api.request_timeout=60s, client.max_retries=5) — first WARNING appeared after ~6-8 min with the generic "Unable to acquire concurrency slots on ['voyage-embeddings']" detail (empty-cause fallback works). PASS on fail-open semantics, but see "Other issues".
- Break 4, 429 backoff with dead limiter (2 injected 429s then real Voyage): 3 POST attempts, 3 throttle WARNINGs (one per attempt), 2 "Voyage 429 … sleeping" WARNINGs, vectors returned (PASS).
- Break 5, 5 concurrent `embed` via `asyncio.gather` with dead limiter: all 5 return vectors, 5 WARNINGs, 8.2s total (PASS).
- Break 6, non-infra limiter error (`rate_limit` patched to `RuntimeError("limiter bug")`): `ExtractionError: Voyage text-embeddings call failed: limiter bug`, no POST (PASS).
- Break 7, empty input / unicode + NUL + 20k-char input with dead limiter: `embed([])` → `[]` with no WARNING (no slot); unicode/large → 2 vectors (PASS).
- Offline/indexing path: `embed_in_batches` (6 texts, `max_inputs=2`, `input_type="document"`) with dead limiter → 6 vectors, 3 WARNINGs (one per batch POST) (PASS).
- Live MCP: `scripts/serve_mcp.py --transport streamable-http` (FASTMCP_PORT=8766) with `PREFECT_API_URL` → fake 401 endpoint, `PREFECT_API_KEY=pnu_bogus`: `search_memory "structured outputs in production"` → `outcome: found`, 10 parents; `search_memory "beanie odm"` → `nothing_found` (not `search_unavailable`); server log shows 2 `embedding without throttle` WARNINGs (one per call), preceded each time by Prefect's own ERROR line. Server stopped by PID (the earlier SWE normal-server run covers the no-WARNING case; my local-Prefect runs above confirm no WARNING on the normal path).

**Acceptance criteria**
- [x] PASS — one `acquire_voyage_slot` helper; `grep -rn "rate_limit(" apps/memory/src/tree` → only `src/tree/models/throttle.py:58`; `concurrency(` has no call sites.
- [x] PASS — 401 / timeout / PrefectHTTPStatusError / httpx.HTTPError → ONE WARNING `Prefect rate limiter 'voyage-embeddings' unavailable: <cause> — embedding without throttle`, POST proceeds, vectors returned. Evidence: `test_throttle.py` + both `…RateLimiterFailsOpen` classes; live runs above (401, 500, ConnectError, ReadTimeout-after-wrapper).
- [x] PASS — other limiter exception still fails (Break 6; `test_any_other_limiter_exception_propagates`, `test_other_limiter_exception_still_fails_the_call` x2).
- [x] PASS — acquire once per POST attempt, inside the 429 loop, never for empty input (Break 4 and 7; `Test…RateLimitChokepoint`; `throttle` call is after the `if not texts` return in both clients, diff).
- [x] PASS — fail-open comments in embedding_text.py (x2), add_entity.py, default.yaml, app_config.py (x2); no README names the limiter (grep); ADR-002 Status-line note present, matching ADR-013 L184.
- [x] PASS — format-check, lint-check, pre-commit, memory-tests green.
- [x] PASS — live: bogus key against a fake 401 limiter → `search_memory` returns `found`; WARNING in server log.

**Evidence**
```
$ make memory-tests  ->  4807 passed in 55.84s
fake 401:  WARNING tree.models.throttle | Prefect rate limiter 'voyage-embeddings' unavailable: Client error '401 Unauthorized' for url 'http://127.0.0.1:<port>/api/csrf-token?client=…'
           Response: {'detail': 'Invalid authentication credentials'} … — embedding without throttle   -> TEXT OK 1 1024 / MM OK 1 1024
conn refused: … unavailable: All connection attempts failed — embedding without throttle
429-RETRY OK 1 posts: 3 6.8s   (3 throttle WARNINGs, 2 Voyage-429 sleeps)
CONCURRENT OK [1,1,1,1,1] 8.2s
RuntimeError -> ExtractionError: Voyage text-embeddings call failed: limiter bug
BATCHES OK 6 {1024}  (3 WARNINGs)
limit present: TEXT 1.2s, MM 12.7s (throttled by voyage-embeddings slot)
```

**Other issues found (non-blocking)**
- A limiter that hangs (blackholed Prefect Cloud, TCP accepted but no reply) delays every POST by minutes (Prefect request_timeout 60s x max_retries 5) before failing open. Pre-existing behavior (the call hung before too), not in the AC; consider a follow-up that passes `timeout_seconds` to `rate_limit` or bounds the acquire with `asyncio.wait_for`.
- Unreachable Prefect adds ~1.5-2.5s per POST (serialized by Prefect's acquisition service: 5 concurrent embeds took 8.2s) plus one WARNING per POST; no warning de-duplication (acknowledged by SWE).
- WARNING spans 3 lines for HTTP errors and the URL includes Prefect's `csrf-token?client=<uuid>` rather than the increment URL; Prefect's own ERROR line precedes ours. Cosmetic.
- Fail-open also covers genuine bugs inside Prefect's acquire path because Prefect wraps every exception (SWE noted; matches spec).
- `docs/notes/prefect-execution-topologies.md` L283-286 lacks the fail-open caveat (possible follow-up).
- Side effect: importing/running memory code with `.env` sourced makes Opik rewrite `~/.opik.config` ("Configuration saved"); unrelated to this task, outside the repo.

**VERDICT: PASS**

### [SWE] 2026-10-03 20:18 — Orchestrator follow-ups (bounded acquire, notes caveat)

**Files modified**
- `apps/memory/src/tree/models/throttle.py` — `acquire_voyage_slot()` wraps the acquire in `asyncio.wait_for(rate_limit(..., timeout_seconds=bound), timeout=bound)`; the `wait_for` timeout fails open with the same WARNING (`no reply within <bound> s`). The `except TimeoutError` follows the `_LIMITER_UNAVAILABLE` clause because prefect's `AcquireConcurrencySlotTimeoutError` subclasses `TimeoutError`.
- `apps/memory/src/tree/config/app_config.py`, `default.yaml` — new `concurrency.voyage_slot_acquire_timeout_seconds: 120`.
- `apps/memory/tests/unit/models/test_throttle.py` — hanging acquire fails open within a 0.05 s bound, a 0.1 s throttling wait inside a 2 s bound completes with no WARNING, default-bound check, call args include `timeout_seconds`.
- `test_voyage_embedding.py`, `test_voyage_multimodal_embedding.py`, `test_add_entity.py` — call-arg assertions accept `timeout_seconds=ANY`.
- `docs/notes/prefect-execution-topologies.md` — one-sentence fail-open caveat on the GCL bullet.

**Why both `wait_for` and `timeout_seconds`**: prefect 3.6 serializes acquires per limit in one background `ConcurrencySlotAcquisitionService`. Its `timeout_seconds` only starts when a request reaches the head of that queue, so it cannot bound the caller's wait. `wait_for` is the hard bound for the caller. The same `timeout_seconds` makes the service drop a hung request so the queue drains.

**Bound = 120 s**: `runner_global_limit` (6) × one slot's decay (60 / `voyage_rpm` = 20 s). Ordinary contention completes before the bound, so the fail-open WARNING still means the limiter is broken. A hung limiter now stalls a POST for 2 min instead of ~6–8 min. Trade-off: the bound also caps a legitimate wait, for example with more containers than the formula assumes. Past the bound the POST proceeds and Voyage's 429 backoff guards the quota (ADR-013 §5). Re-derive the value if `voyage_rpm` or `runner_global_limit` changes.

**Tests**
- Unit: 4810 passing, 0 failing (`make memory-tests`, env-status local). The new tests were red first (missing config field).
- format-check / lint-check / pre-commit: green.

**Evidence**
```
# fake limiter that accepts TCP and never replies, TREE_CONCURRENCY__VOYAGE_SLOT_ACQUIRE_TIMEOUT_SECONDS=3
WARNING tree.models.throttle: Prefect rate limiter 'voyage-embeddings' unavailable: no reply within 3.0 s — embedding without throttle
1st acquire returned after 3.0s
3 concurrent returned after 3.0s
# real local Prefect, gcl voyage-embeddings --limit 1 --slot-decay-per-second 0.25 (deleted afterwards; gcl ls → none)
bound = 120.0
slot 0 at 0.0s / slot 1 at 4.0s / slot 2 at 8.5s   (throttles, no WARNING)
$ make memory-tests  ->  4810 passed in 57.68s
```

**Notes**
- Cosmetic: when the bound abandons a request, prefect logs `ERROR ... Service 'ConcurrencySlotAcquisitionService' failed to process item (... state=cancelled)` once per abandoned request. This is not fatal. If the bound cuts a legitimate wait, the queued request may later use a slot that no caller consumes.
