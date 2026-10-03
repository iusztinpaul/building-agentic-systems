---
id: 178-prefect-rate-limiter-fail-open
status: pending
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

- [ ] One `acquire_voyage_slot` helper; `grep -rn "rate_limit(" apps/memory/src/tree` matches only the helper.
- [ ] A limiter raising `ConcurrencySlotAcquisitionError` (401 cause), `AcquireConcurrencySlotTimeoutError`,
      `PrefectHTTPStatusError` or `httpx.HTTPError` → ONE WARNING `Prefect rate limiter 'voyage-embeddings'
      unavailable: <cause> — embedding without throttle` and the Voyage POST proceeds; `embed` returns vectors.
- [ ] Any other limiter exception still fails the call as before.
- [ ] The acquire still happens once per real POST attempt, inside the 429 loop, never for empty input.
- [ ] Comments/README naming the limiter mention fail-open; ADR-002 Status-line note applied.
- [ ] `make memory-format-check`, `make memory-lint-check`, `make pre-commit`, `make memory-tests` green.
- [ ] Live (local, bogus cloud key): `search_memory` answers a result and the WARNING is in the server log;
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
