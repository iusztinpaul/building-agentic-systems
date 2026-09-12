---
id: 126-tool-error-envelope
feature: mcp-tool-contracts
status: done
---

# One Tool error envelope `{error_type, retryable, message}` across every tool in both Memory modes

Tags: `mcp`, `rag`, `graphrag`
Depends on: #122, #124
Blocks: #128, #129
Implements: ADR-008 — Decision 2 (error envelope)

## Scope

Today `mcp/tools.py` answers `{"error": <code>, "detail": …}` on some paths, retrieval exceptions
escape to FastMCP as protocol-level errors, and `graph_tools.py` has its own two codes. One shape,
one helper, every tool.

- `tree/mcp/tools.py`: `class ToolErrorEnvelope(BaseModel)` (`error_type: str`, `retryable: bool`,
  `message: str`) and `def tool_error(error_type: str, message: str, *, retryable: bool) -> str`
  returning `model_dump_json()`. Docstring defines `retryable`: `true` = the SAME call may succeed
  later (transient infra: network, provider, Prefect, Mongo); `false` = change the input or stop
  (validation, configuration, unsupported). Drop `error` / `detail` outright — no alias.
- **Reach:** every early return and `except` in `tools.py` AND `graph_tools.py` (graph tools import
  the helper from `tree.mcp.tools`). Mapping, 1:1 with today's codes:
  `invalid_input` false (absorbs `empty_input`), `unsupported_url` false, `configuration_error`
  false, `file_error` false, `invalid_state` false (review tools), `fetch_failed` true,
  `network_error` true, `http_error` — true iff status ≥ 500 or 429, else false.
- **Retrieval boundary** — rag `search_memory`, graphrag `search_memory`, `query_memory`,
  `deep_search_memory`, `visualize_memory_graph` (query path): wrap the retrieval call;
  `except (SearchUnavailableError, pymongo.errors.PyMongoError, <embedding-provider error>)` →
  `tool_error("search_unavailable", …, retryable=True)`; `except Exception  # noqa: BLE001` →
  `tool_error("internal_error", …, retryable=False)`; both `logger.exception(...)`. The
  embedding-provider exception type is whatever `tree/models/` raises for Voyage failures — the
  SWE reads `tree/models/voyage*.py` and names it explicitly (fallback: `httpx.HTTPError`).
  Tools returning `ToolResult` keep `str | ToolResult`.
- **Dispatch boundary** — in `_ingest` (shared by the three ingest tools):
  `except (httpx.ConnectError, httpx.TimeoutException, prefect.exceptions.PrefectHTTPStatusError)`
  → `pipeline_unavailable`, retryable true, message "Prefect API unreachable — try again";
  `except prefect.exceptions.ObjectNotFound` → `configuration_error`, retryable false, message names
  `online-pipeline/online-pipeline` and `make memory-serve-workflows`. No in-process fallback.
- Tool docstrings: replace every `{"error": …}` mention with the envelope; one shared sentence
  ("Errors answer `{error_type, retryable, message}` — retry only when `retryable` is true").
- Regression guard: `tests/unit/mcp/test_error_envelope.py::test_no_legacy_error_keys` parses
  `tools.py` and `graph_tools.py` source and asserts neither contains the literals `"error":` or
  `"detail":` (the nested `search_web` ingest block uses `"error"` without a colon-quote pattern
  at top level — verify the assertion is scoped to `json.dumps({...})` calls or relax to
  `'"detail"'` only; SWE decides).

## Out of scope
- The nested `ingest` block inside a successful `search_web` answer (`triggered/urls/error`) —
  a sub-result, not the envelope. `deep_search_memory`'s plain "No results found." string.
- Server `instructions` text. SKILL.md (#128).

## Acceptance Criteria

- [x] `tool_error("invalid_input", "x", retryable=False)` → `{"error_type":"invalid_input","retryable":false,"message":"x"}` and nothing else — `tests/unit/mcp/test_error_envelope.py::test_helper_shape`.
- [x] Every legacy code maps to the table above (parametrised over the tool + trigger) — `::TestMapping` (`ingest_url` bad scheme → `unsupported_url`/false; `ingest_conversation` blank → `invalid_input`/false; `search_web` `ingest_top_k=0` → `invalid_input`/false; `scrape_web` 6 URLs → `invalid_input`/false; `review_confirm` bad state → `invalid_state`/false; `ingest_url` `httpx.ConnectError` → `network_error`/true; HTTP 503 → `http_error`/true; HTTP 404 → `http_error`/false).
- [x] rag `search_memory` with `retrieve_parents` raising `SearchUnavailableError` → `search_unavailable`/true; raising `RuntimeError` → `internal_error`/false; both logged at ERROR with traceback — `tests/unit/mcp/test_tools.py::TestSearchMemoryErrors`.
- [x] graphrag `search_memory`, `query_memory`, `deep_search_memory`, `visualize_memory_graph(query=…)` return the same two envelopes under the same faults — `tests/unit/mcp/test_graph_tools.py::TestReaderErrors`.
- [x] `_ingest` with `run_deployment` raising `httpx.ConnectError` → `pipeline_unavailable`/true; raising `ObjectNotFound` → `configuration_error`/false naming `make memory-serve-workflows` — `tests/unit/mcp/test_tools.py::TestDispatchErrors`.
- [x] No `"detail":` literal remains in `tools.py` / `graph_tools.py` — `test_error_envelope.py::test_no_legacy_error_keys`.
- [x] `grep -rn '"error":' apps/memory/src/tree/mcp/tools.py apps/memory/src/tree/mcp/graph_tools.py` shows only the `search_web` nested ingest block.
- [x] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green.

## User Stories

### Story: Prefect is down when the user shares a link
1. Assistant calls `ingest_url(url="https://example.com/post")`; the Prefect API refuses connections.
2. Answer: `{"error_type": "pipeline_unavailable", "retryable": true, "message": "Prefect API unreachable — try again"}`.
3. The assistant tells the user ingestion is temporarily unavailable and offers to retry, rather than surfacing a stack trace.

### Story: Workflows were never served
1. Same call, but `run_deployment` raises `ObjectNotFound`.
2. Answer: `error_type: "configuration_error"`, `retryable: false`, message names `online-pipeline/online-pipeline` and `make memory-serve-workflows`.

### Story: Mongo is unreachable during a search (either mode)
1. `search_memory(query="prefect")` while Mongo is down.
2. Answer: `{"error_type": "search_unavailable", "retryable": true, "message": …}` — a JSON string, not an MCP protocol error; the server log has the traceback.

### Story: Model passes an empty query
1. `search_memory(query="   ")`.
2. Answer: `{"error_type": "invalid_input", "retryable": false, "message": "query must not be empty"}`; the assistant rephrases instead of retrying.

### Story: Bright Data returns 429 vs 404
1. `scrape_web` / `ingest_url` hit a 429 → `http_error`, `retryable: true`; a 404 → `http_error`, `retryable: false`.

---

Blocked by: #122, #124

## Log

### [PA] 2026-09-12 10:20 — Grooming

**Summary**
One envelope, one helper, every tool in both modes; retrieval and dispatch failures become JSON answers with a retry hint.

**Key decisions**
- `retryable` semantics are the whole point — the model reads it, not the code string.
- Unregistered deployment is a configuration error (false), unreachable API is transient (true).
- Graph tools import the helper from `tools.py` (already unconditional) — no third module.

**Dependencies**
- #122 — `_ingest` returns the receipt; the dispatch catch wraps that path.
- #124 — `SearchUnavailableError` exists to catch.

**User stories**
- 5 stories covering: Prefect down, deployment missing, Mongo down, blank query, 429 vs 404.

Ready for implementation.

### [SWE] 2026-09-12 16:45 — Implementation

**Files modified**
- `apps/memory/src/tree/mcp/tools.py` — `ToolErrorEnvelope` + `tool_error()` + `_http_retryable()` + `_retrieval_error()`; every early return / `except` converted; retrieval boundary on rag `search_memory`; dispatch boundary in `_ingest`; docstrings carry the shared `ERROR_CONTRACT` sentence.
- `apps/memory/src/tree/mcp/graph_tools.py` — imports `tool_error` / `_retrieval_error` from `tools.py`; the four readers (`search_memory`, `query_memory`, `deep_search_memory`, `visualize_memory_graph`) wrap their retrieval; `visualize_memory_graph` is now `-> str | ToolResult`; review tools' four legacy dicts converted.
- `apps/memory/scripts/query_graph.py` — one `SearchUnavailableError` catch around BOTH mode branches: prints `Search unavailable: <message> — retryable` and exits 1 instead of a raw traceback (Tester finding on #124).
- `apps/memory/tests/unit/mcp/test_error_envelope.py` — NEW: helper shape, the cross-tool mapping table, the AST regression guard, the docstring-contract guard.
- `apps/memory/tests/unit/mcp/test_tools.py` — `TestSearchMemoryErrors`, `TestDispatchErrors`; blank-query test asserts the envelope.
- `apps/memory/tests/unit/mcp/test_graph_tools.py` — `TestReaderErrors` (4 readers × 2 faults) + the full-graph path.
- `apps/memory/tests/unit/mcp/test_search_web.py` — error assertions converted to `error_type` / `retryable` / `message`; the two nested-`ingest` assertions left untouched (out of scope).
- `apps/memory/tests/unit/scripts/test_query_graph.py` — `TestSearchUnavailable` (rag + graphrag).

**Tests**
- Unit: 3041 passing, 0 failing (`make memory-tests`, env `local`), 0 warnings from this diff.
- Integration: N/A — no integration suite in this repo; e2e done over a real stdio MCP session (below).

**Acceptance criteria**
- [x] `test_helper_shape` — the three keys and nothing else.
- [x] `TestMapping` — 15 cases across `ingest_url` / `ingest_file` / `ingest_conversation` / `search_web` / `scrape_web` / `review_list_pending` / `review_confirm`. ONE documented deviation, see Notes: `network_error` is exercised on `search_web`, not `ingest_url`.
- [x] `tests/unit/mcp/test_tools.py::TestSearchMemoryErrors` — 4 unavailable types → `search_unavailable`/true, `RuntimeError` → `internal_error`/false, both with `exc_info` at ERROR (mutation-checked: swapping `logger.exception` for `logger.error` turns the test red).
- [x] `tests/unit/mcp/test_graph_tools.py::TestReaderErrors` — the same two envelopes on all four graphrag readers.
- [x] `tests/unit/mcp/test_tools.py::TestDispatchErrors` — `ConnectError` / timeout / `PrefectHTTPStatusError` → `pipeline_unavailable`/true; `ObjectNotFound` → `configuration_error`/false naming the deployment AND `make memory-serve-workflows`; all three ingest tools share the boundary. Story 2 live: NOT RUN — the `online-pipeline` deployment IS registered locally, so `ObjectNotFound` cannot be provoked without unregistering it; unit-tested instead.
- [x] `test_no_legacy_error_keys` — AST-scoped (see Notes).
- [x] `grep -rn '"error":'` → one hit, `tools.py:592`, inside `_build_ingest_block`.
- [x] format / lint / pre-commit / tests green.

**Evidence**

```
$ make memory-tests
============================ 3041 passed in 46.55s =============================

$ make memory-format-check && make memory-lint-check && make pre-commit
289 files already formatted
All checks passed!
ruff check / ruff format / prettier / biome check (harness) ............ Passed

$ grep -rn '"error":' apps/memory/src/tree/mcp/tools.py apps/memory/src/tree/mcp/graph_tools.py
apps/memory/src/tree/mcp/tools.py:592:            "error": str(exc),      # _build_ingest_block, nested search_web sub-result
$ grep -rn '"detail":' apps/memory/src/tree/mcp/tools.py apps/memory/src/tree/mcp/graph_tools.py
apps/memory/src/tree/mcp/tools.py:582:            "detail": "no urls to ingest",   # same block
```

E2E over REAL stdio MCP sessions (`scripts/serve_mcp.py --transport stdio` + `fastmcp.Client`,
user `paul@example.com`, local env):

```
--- rag mode, healthy infra
search_memory(query="   ")   -> {"error_type":"invalid_input","retryable":false,"message":"query must not be empty"}
ingest_url("ftp://example.com/post")
                             -> {"error_type":"unsupported_url","retryable":false,"message":"Unsupported URL scheme 'ftp': ..."}
scrape_web(6 urls)           -> {"error_type":"invalid_input","retryable":false,"message":"max 5 urls per call (got 6)"}

--- Story 1: PREFECT_API_URL=http://127.0.0.1:4999/api (dead port)
ingest_url("https://example.com/post-126")
                             -> {"error_type":"pipeline_unavailable","retryable":true,"message":"Prefect API unreachable — try again"}
   server log: full httpx.ConnectError traceback (logger.exception), no protocol error to the client

--- Story 3a: graphrag search_memory with a bogus VOYAGE_API_KEY (embedding provider arm)
search_memory("prefect")     -> {"error_type":"search_unavailable","retryable":true,
                                 "message":"Memory search is temporarily unavailable: Voyage text-embeddings API error 401: ..."}

--- Story 3b: graphrag search_memory with Mongo DOWN (docker stop tree-mongodb mid-session)
search_memory("prefect")     -> {"error_type":"search_unavailable","retryable":true,
                                 "message":"Memory search is temporarily unavailable: vector and text search are both unavailable"}
   then docker start tree-mongodb -> the SAME call answers 94 nodes / 90 edges.
   SIDE EFFECT worth knowing: stopping tree-mongodb takes tree-mongot down with it (it exited 1);
   it was restarted explicitly and the restored stack verified below.

--- infra left as found
$ docker ps --format '{{.Names}}\t{{.Status}}'   # tree-mongodb (healthy), tree-mongot, tree-prefect-server/worker all up
$ retrieve_parents("agentic harness")            -> search_mode=hybrid outcome=found parents=2

--- CLI (the #124 Tester finding), happy paths re-smoked after the restructure
$ uv run python scripts/query_graph.py --query "agentic harness" -k 2 --no-open        -> 8 nodes, 6 edges, exit 0
$ TREE_MEMORY__MODE=rag uv run python scripts/query_graph.py --query "agentic harness" -> ranked parent blocks, exit 0
```

**Notes**
- **`network_error` moved from `ingest_url` to `search_web` in `TestMapping`.** The Scope's dispatch
  boundary and User Story 1 both say `ingest_url` + a refused Prefect connection is
  `pipeline_unavailable`; since #122 the ONLY call `ingest_url` makes is `dispatch_online_pipeline`,
  so its own `httpx.ConnectError` clause is unreachable and the AC's parenthetical could not hold
  literally. `search_web` still calls Bright Data in-process, so the code is exercised there. The
  `http_error` 503/404 rule IS asserted on `ingest_url` as well (`PrefectHTTPStatusError` is an
  `httpx.HTTPStatusError` subclass, so the dispatch catch takes Prefect's and lets a plain one
  through) plus on `search_web`. `ingest_url`'s BrightData / httpx clauses are KEPT, not deleted.
- **`test_no_legacy_error_keys` is AST-scoped, not a text grep.** Both surviving literals
  (`"error"`, `"detail"`) live in `_build_ingest_block` — the nested `ingest` sub-result of a
  SUCCESSFUL `search_web` answer, explicitly out of scope. The guard parses the module, collects
  every dict literal carrying either key and allowlists that ONE function by name, so a text grep's
  two false positives (docstrings that quote the old shape while documenting the new one) don't
  fight the prose and any NEW legacy-key dict anywhere still fails. Consequence: the
  `"detail": "no urls to ingest"` literal at `tools.py:582` remains — renaming it would change an
  out-of-scope answer shape.
- **The embedding-provider exception is `tree.models.exceptions.ModelError`, the BASE**, not
  `ExtractionError`. Read of `tree/models/`: Voyage raises `ExtractionError` (a `ModelError`) for
  every API failure, but `ModalEmbeddingModel.embed()` lets a bare `ModelError` escape from
  `_ensure_initialised` → `_resolve_web_url` (endpoint not resolvable) — catching only
  `ExtractionError` would miss the local provider's most likely outage. `httpx.HTTPError` was NOT
  needed (both clients use `aiohttp` and wrap everything).
- **One `_retrieval_error` helper instead of a two-clause try/except copied five times.** Same
  behaviour as the Scope's literal form (tuple → `search_unavailable`/true, `Exception` →
  `internal_error`/false, both `logger.exception`), one mapping, so the codes cannot drift per tool.
  `asyncio.CancelledError` is a `BaseException` and is deliberately NOT caught.
- **`visualize_memory_graph` guards BOTH branches**, not only the query path: `fetch_full_graph`
  hits the same Mongo, and "the database is down" must not answer differently because the caller
  omitted a query. Its annotation went `ToolResult` → `str | ToolResult`;
  `test_tool_gating.py` / `test_server_startup.py` re-run green.
- `ERROR_CONTRACT` is a module constant asserted (normalised for wrapping) against the docstring of
  all 13 tools that can answer an envelope, so #128's skill rewrite has a fixed sentence to quote.
  It is a TEST ANCHOR, not the source of the prose — FastMCP reads `__doc__` at import, so editing
  the constant does not edit any docstring, it turns the test red (said so in its comment).
  `visualize_memory_embeddings` and the dashboard are deliberately excluded — they never answer one.
- **Gap CLOSED on coordinator instruction (second pass): one new code, `storage_unavailable`**
  (retryable true, message `memory store unreachable — try again`), built by
  `tools.storage_error(tool, exc)` — the same `logger.exception` + envelope shape as
  `_retrieval_error`. Applied at the two boundaries ADR-008 §2 did not name: (1) `_ingest` catches
  `PyMongoError` FIRST, around the whole `dispatch_online_pipeline` call, so the pre-flight
  `Document.find_one` is covered and the Prefect/httpx clauses below are untouched; (2) the three
  review tools wrap `_find_pending_duplicates` / `_review_duplicate`. Deliberately NOT
  `search_unavailable` — that code means the retrieval legs are down, and a model told "search is
  unavailable" right after an `ingest_url` call learns the wrong thing about the memory. Code lists
  in the `ToolErrorEnvelope` / `tool_error` docstrings and the `ERROR_CONTRACT` comment updated;
  docs/ untouched (the coordinator owns the ADR-008 §2 + glossary rows). No tool in either module
  can now raise through FastMCP on a Mongo failure. New tests:
  `test_tools.py::TestDispatchErrors::test_mongo_down_on_preflight_is_storage_unavailable`
  (patches `tree.online.Document.find_one`, asserts `run_deployment` was never awaited),
  `test_graph_tools.py::TestReviewToolErrors` (one per review tool) and a two-tool case in
  `test_error_envelope.py::TestMapping`.
- The AST guard also PINS the exempt count (2 in `tools.py`, 0 in `graph_tools.py`), so a third
  legacy-key dict inside `_build_ingest_block` fails too instead of riding the function's exemption.
- Prefect exception classes verified against the installed `prefect 3.6.19`:
  `ObjectNotFound` is a `PrefectException` (NOT a `PrefectHTTPStatusError` subclass), so clause
  order is free; the order chosen reads deployment-missing first.
- Not committed — Tester goes first.

**Addendum — `storage_unavailable` pass (second round)**

- `make memory-tests` → **3047 passed**; `make memory-format-check && make memory-lint-check && make pre-commit` green.
- Live stdio MCP session (graphrag, `docker stop tree-mongodb` mid-session) — all three codes now
  distinguish themselves on real infra:

```
ingest_conversation(...)     -> {"error_type":"storage_unavailable","retryable":true,"message":"memory store unreachable — try again"}
review_list_pending(limit=5) -> {"error_type":"storage_unavailable","retryable":true,"message":"memory store unreachable — try again"}
search_memory("prefect")     -> {"error_type":"search_unavailable","retryable":true,"message":"Memory search is temporarily unavailable: vector and text search are both unavailable"}

   docker start tree-mongodb + tree-mongot (mongot exits WITH mongodb — restart it explicitly)
review_list_pending(limit=5) -> the real pending SAME_AS queue
retrieve_parents("agentic harness") -> search_mode=hybrid outcome=found parents=2   # infra left as found
```

### [Tester] 2026-09-12 17:10 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check && make memory-lint-check`: `289 files already formatted`, `All checks passed!`; `make pre-commit`: all hooks Passed)
- Unit tests: 3047 passed / 0 failed (`make memory-tests`, env `local`)
- Integration tests: N/A — no integration suite in this repo (per AGENTS.md); e2e done over real stdio MCP sessions below
- Warnings: 0

**E2E adversarial pass**

1. Happy path: real stdio MCP session (`scripts/serve_mcp.py --transport stdio`, `fastmcp.Client`, user `paul@example.com`, graphrag mode, local env), `search_memory(query="prefect")` → real node/edge JSON (94 nodes / 90 edges style result). PASS.
2. Grep sweep (both modules, both patterns): `grep -rn "raise ToolError" tools.py graph_tools.py` → 0 hits; `grep -n "^\s*raise$"` → 0 hits; `grep -n 'json.dumps({"error"'` → 0 hits outside `_build_ingest_block` (`tools.py:592`, the documented out-of-scope `search_web` ingest sub-result). PASS.
3. Parametrised "inject a generic `RuntimeError` at the first awaited call" sweep across all 13 `_ENVELOPE_TOOLS`-listed tools (script, both modules): the 5 retrieval-boundary tools (rag `search_memory`, graphrag `search_memory`/`query_memory`/`deep_search_memory`/`visualize_memory_graph`) correctly answer `internal_error` envelopes. The other 8 (`ingest_url`, `ingest_file`, `search_web`, `scrape_web`, `ingest_conversation`, `review_list_pending`, `review_confirm`, `review_reject`) let the injected `RuntimeError` propagate as a raw exception — NOT an envelope. This matches the Scope section literally (the `except Exception # noqa: BLE001` catch-all is specified ONLY for the retrieval boundary; the dispatch boundary names exactly 3 exception types, review tools only convert `PyMongoError`/`ValueError`). Pre-task, `_ingest` had zero exception handling, so an unexpected bug already leaked before this diff — the diff strictly narrows what leaks, it does not regress it. **PASS WITH NOTE** — not a blocking issue for #126 as scoped, but worth a follow-up task (#128/#129) since the feature's headline goal ("every tool in both Memory modes") is not fully met for arbitrary bugs outside the enumerated exception types.
4. Retryable semantics: `TestMapping::test_http_error_is_retryable_only_on_429_or_5xx` (`test_error_envelope.py:236-256`) pins 429→true, 500→true, 503→true, 400→false, 404→false against `_http_retryable` (`status_code == 429 or status_code >= 500`, `tools.py`). PASS.
5. `storage_unavailable` vs `search_unavailable` vs `pipeline_unavailable` distinct on the same underlying outage — live stdio session, mongo+mongot stopped mid-session:
   - `ingest_conversation(...)` → `{"error_type":"storage_unavailable","retryable":true,"message":"memory store unreachable — try again"}` (pre-flight `Document.find_one` in `_ingest`, caught FIRST by `except PyMongoError`, `tools.py:381`)
   - `search_memory("prefect")` (graphrag) → `{"error_type":"search_unavailable","retryable":true,"message":"Memory search is temporarily unavailable: vector and text search are both unavailable"}` (retrieval boundary, `_RETRIEVAL_UNAVAILABLE` tuple includes `PyMongoError`)
   - separately, `PREFECT_API_URL` pointed at a dead port (mongo up) → `ingest_url(...)` → `{"error_type":"pipeline_unavailable","retryable":true,"message":"Prefect API unreachable — try again"}`, with the full `httpx.ConnectError` traceback in the server log (`logger.exception`), never surfaced to the client.
   All three codes on effectively the same class of infra fault (`ServerSelectionTimeoutError` / `ConnectError`), correctly distinguished by boundary. PASS.
6. Envelope shape validity: every envelope captured above (and every `_envelope()`-asserted test in `test_error_envelope.py`, `test_tools.py`, `test_graph_tools.py`) has exactly the 3 keys `{error_type, retryable, message}`, `retryable` renders as an unquoted JSON `true`/`false` (verified against the raw JSON text captured live, not just parsed Python). PASS.
7. Real stdio MCP e2e, additional break paths run live (not just re-reading the SWE's evidence):
   - Blank query, **rag mode** (`TREE_MEMORY__MODE=rag`): `search_memory(query="   ")` → `{"error_type":"invalid_input","retryable":false,"message":"query must not be empty"}`. PASS.
   - Blank query, **graphrag mode (the DEFAULT — `configs/default.yaml:22` `mode: graphrag`)**: `search_memory(query="   ")` → a full real result payload (94-node-style JSON), no envelope, no validation at all. **FAIL** — see Acceptance Criteria below.
   - `PREFECT_API_URL=http://127.0.0.1:4999/api` (dead port) → `ingest_url` → `pipeline_unavailable`/true. PASS.
   - `docker stop tree-mongodb tree-mongot` mid-session → `ingest_conversation` → `storage_unavailable`/true; `search_memory` → `search_unavailable`/true. PASS.
   - `docker start tree-mongodb tree-mongot`, waited for `tree-mongodb` healthy, then `search_memory` (graphrag) answered real results again; `TREE_MEMORY__MODE=rag uv run python scripts/query_graph.py --query "agentic harness" -k 2 --no-open` exited 0 and printed NO `DEGRADED_SEARCH_CAVEAT` line — `scripts/query_graph.py:99-100` only prints that caveat when `result.search_mode != "hybrid"`, so its absence proves `search_mode == "hybrid"` (both legs recovered). Infra left as found: `tree-mongodb`/`tree-mongot`/`tree-prefect-server`/`tree-prefect-worker` all up and healthy at the end, matching the state at session start.

**Acceptance criteria**
- [x] PASS — `tool_error(...)` helper shape — `test_error_envelope.py::test_helper_shape` passes; live: `tool_error("invalid_input","x",retryable=False)` → exactly `{"error_type":"invalid_input","retryable":false,"message":"x"}`.
- [x] PASS — legacy-code mapping table — `test_error_envelope.py::TestMapping` (all cases) passes; live-verified subset above.
- [x] PASS — rag `search_memory` `SearchUnavailableError`→`search_unavailable`/true, `RuntimeError`→`internal_error`/false, both logged at ERROR with `exc_info` — `test_tools.py::TestSearchMemoryErrors` (incl. `test_both_envelopes_log_the_traceback_at_error`, mutation-checked per SWE notes) passes; live-verified.
- [x] PASS — graphrag readers return the same two envelopes **under the two named retrieval faults** (`SearchUnavailableError` / `RuntimeError`) — `test_graph_tools.py::TestReaderErrors` passes for all 4 readers; live-verified on `search_memory`. Distinct from the blank-query gap below, which this AC does not cover (it names "faults", not input validation).
- [x] PASS — `_ingest` dispatch boundary (`ConnectError`/timeout/`PrefectHTTPStatusError`→`pipeline_unavailable`/true; `ObjectNotFound`→`configuration_error`/false naming the deployment + `make memory-serve-workflows`) — `test_tools.py::TestDispatchErrors` passes; live-verified the Prefect-unreachable case; `ObjectNotFound` unit-tested only (SWE note: cannot provoke live, deployment IS registered locally — reasonable).
- [x] PASS — no `"detail":` literal outside the exempt function — `test_error_envelope.py::test_no_legacy_error_keys`, AST-scoped, count-pinned (2 in `tools.py`, 0 in `graph_tools.py`) passes.
- [x] PASS — `grep -rn '"error":' tools.py graph_tools.py` → one hit, `tools.py:638` (in `_build_ingest_block`, the documented out-of-scope `search_web` ingest sub-result); `"detail":` → one hit, `tools.py:628`, same function.
- [x] PASS — `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green — re-ran independently, all four green, 3047 passed.
- [ ] FAIL — Story "Model passes an empty query" does not hold in **graphrag mode, the default** (`configs/default.yaml:22` → `mode: graphrag`).
      Expected (per User Story, no mode qualifier given): `search_memory(query="   ")` → `{"error_type":"invalid_input","retryable":false,"message":"query must not be empty"}`.
      Actual: real stdio session, default (graphrag) server → full real node/edge result payload, no validation, no envelope. The SAME call against `TREE_MEMORY__MODE=rag` correctly returns the envelope (`tools.py:266`, `if not query.strip(): return tool_error(...)`); `graph_tools.py`'s `search_memory` (`graph_tools.py:250`) has no such guard — it calls `_set_retrieval_thread` then `structured_query_memory` directly (`graph_tools.py:281-289`).
      This is a **pre-existing gap the envelope work surfaces, not a regression introduced by this diff** (`git show HEAD:apps/memory/src/tree/mcp/graph_tools.py` shows the same missing guard before this task's changes) — flagging so the fix stays scoped, not as a criticism of the diff's approach.
      Fix: add the same blank-query guard at the top of `graph_tools.py::search_memory` (mirroring `tools.py:266`), with a regression test next to `TestReaderErrors` in `test_graph_tools.py`. Worth a look, not a requirement: `query_memory` and `deep_search_memory` have the identical hole — the Story only names `search_memory`, so extending to those two is the SWE's call, not a blocking requirement here.

**Evidence**

```
$ make memory-tests
============================ 3047 passed in 57.25s =============================

$ make memory-format-check && make memory-lint-check
289 files already formatted
All checks passed!

$ make pre-commit
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed

$ grep -rn 'raise ToolError\|json.dumps({"error"' apps/memory/src/tree/mcp/tools.py apps/memory/src/tree/mcp/graph_tools.py
(no output outside _build_ingest_block)

Live stdio MCP session, default (graphrag) mode:
search_memory(query="   ")   -> [ ...94-node-style real result... ]   # FAIL: expected invalid_input envelope

Live stdio MCP session, TREE_MEMORY__MODE=rag:
search_memory(query="   ")   -> {"error_type":"invalid_input","retryable":false,"message":"query must not be empty"}

Live stdio MCP session, PREFECT_API_URL=http://127.0.0.1:4999/api:
ingest_url("https://example.com/qa-test-126") -> {"error_type":"pipeline_unavailable","retryable":true,"message":"Prefect API unreachable — try again"}

Live stdio MCP session, docker stop tree-mongodb tree-mongot mid-session:
ingest_conversation(...)     -> {"error_type":"storage_unavailable","retryable":true,"message":"memory store unreachable — try again"}
search_memory("prefect")     -> {"error_type":"search_unavailable","retryable":true,"message":"Memory search is temporarily unavailable: vector and text search are both unavailable"}
docker start tree-mongodb tree-mongot; waited for healthy
search_memory("prefect")     -> real results again

$ TREE_MEMORY__MODE=rag uv run python scripts/query_graph.py --query "agentic harness" -k 2 --no-open
(exit 0, no DEGRADED_SEARCH_CAVEAT line -> search_mode == "hybrid")

$ docker ps --format '{{.Names}}\t{{.Status}}' | grep tree   # infra left as found
tree-mongot            Up ... 
tree-prefect-worker    Up ...
tree-mongodb           Up ... (healthy)
tree-prefect-server    Up ... (healthy)
```

**Other issues found**
- 8 of the 13 envelope-answering tools still let an unexpected (non-enumerated) exception escape as a raw MCP protocol error rather than an `internal_error` envelope — see break path 3 above. Not a blocker for #126 (matches the Scope's literal per-boundary exception lists, and narrows a pre-existing leak rather than introducing one); worth a follow-up task before #128/#129 build on top, since a model calling any of `ingest_url`, `ingest_file`, `search_web`, `scrape_web`, `ingest_conversation`, `review_list_pending`, `review_confirm`, `review_reject` still cannot rely on "errors always answer the envelope."
- Style-only: `tools.py:395` uses `except httpx.ConnectError, httpx.TimeoutException, PrefectHTTPStatusError:` (no tuple parens). This is valid on this repo's `requires-python = ">=3.14"` per PEP 758, and I confirmed it catches correctly — but it's the only unparenthesised multi-exception `except` in either module; consider parenthesising for consistency with every other clause (ruff didn't flag it).
- Commit hygiene: `.agents/skills/tree-memory/SKILL.md` and `tasks/128-tree-memory-skill-search-loop.md` became dirty (unrelated #128 skill-search-loop content) partway through this QA session — they were NOT present in the initial `git status` and none of my QA scripts touched them (verified: the #126 diff --stat is byte-identical to the session's opening stat). Likely a concurrent agent working #128. Not a QA defect, but the orchestrator should confirm before staging — `git add -A` would sweep unrelated #128 work into #126's commit.
- ADR-008 §2 and `docs/glossary.md` doc edits verified: both add `storage_unavailable` (retryable true) consistently with the code (`tools.storage_error`); presence and topical match confirmed, no further content review performed (PR Reviewer's lane).

**VERDICT: FAIL**

One blocking issue: the "Model passes an empty query" User Story does not hold against the default (graphrag) server — `search_memory(query="   ")` returns real results instead of the `invalid_input` envelope. Everything else (all 8 checkbox ACs, retryable semantics, the three-way storage/search/pipeline distinction, envelope shape validity, the legacy-key regression guard, and the full local suite) verified green with live evidence. Fix is small and scoped (mirror the existing `tools.py:266` guard in `graph_tools.py::search_memory`, plus a regression test) — expect a fast re-review.


### [SWE] 2026-09-12 17:15 — QA fixes (round 2)

**What changed**

1. **BLOCKING — blank-query guard on the graphrag readers.** `graph_tools.search_memory`,
   `query_memory` and `deep_search_memory` now guard `query.strip()` BEFORE `_set_retrieval_thread`
   and any retrieval, answering the identical envelope as the rag tool. The message is no longer
   duplicated: `tools.BLANK_QUERY_MESSAGE = "query must not be empty"` is the ONE string both
   modules use, so the two modes cannot drift. `visualize_memory_graph` is deliberately NOT guarded
   — an empty `query` there MEANS "draw the whole memory graph", and a regression test pins that
   counter-case so the guard does not spread.
2. **Catch-all at every tool boundary.** New `tools.internal_error(tool, exc)` (`logger.exception`
   + `internal_error`/false) and a trailing `except Exception:  # noqa: BLE001` on the eight tools
   that leaked: `ingest_url`, `ingest_file`, `ingest_conversation`, `search_web`, `scrape_web`,
   `review_list_pending`, `review_confirm`, `review_reject`. Every specific clause above them is
   unchanged and still wins. `scrape_web` had no `try` at all, so its `asyncio.gather` fan-out is
   now wrapped (per-URL failures still come back inside `results`, untouched).
   `_retrieval_error` delegates its non-unavailable branch to the same helper — one place builds an
   `internal_error`, one place logs it.
3. **Style nit.** `tools.py` dispatch clause is parenthesised again:
   `except (httpx.ConnectError, httpx.TimeoutException, PrefectHTTPStatusError) as exc:`.
   Worth knowing for whoever re-runs the formatter: on Python 3.14 (PEP 758) an except tuple
   WITHOUT an `as` binding is legal unparenthesised, and `ruff format` strips the parens — which is
   how they were lost. Binding `as exc` (and logging it) makes the parens stable and matches the
   two sibling clauses.

**Tests**
- `tests/unit/mcp/test_graph_tools.py::test_blank_query_is_invalid_input` — 3 readers × 3 blank
  forms, asserting BOTH engines were never awaited; plus
  `test_visualize_memory_graph_still_draws_the_whole_graph_on_no_query`.
- `tests/unit/mcp/test_error_envelope.py::TestMapping::test_a_blank_query_is_invalid_input_in_both_modes`
  — the cross-mode case (rag + the three graphrag readers).
- `tests/unit/mcp/test_error_envelope.py::test_no_tool_raises_through_fastmcp` — THE contract guard:
  parametrised over all 13 envelope-answering tools, injecting `RuntimeError` at each tool's FIRST
  awaited call and asserting an `internal_error`/false envelope. `test_the_sweep_covers_every_envelope_answering_tool`
  pins that list against the docstring list, so a tool added to one and not the other fails.
- Unit: **3075 passing**, 0 failing (`make memory-tests`, env `local`). Format / lint / pre-commit green.

**Evidence**

```
$ make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests
289 files already formatted / All checks passed! / ruff+prettier+biome Passed
============================ 3075 passed in 46.63s =============================
```

Live stdio MCP session on the DEFAULT server (`mode: graphrag`, real Mongo, user `paul@example.com`):

```
search_memory(query="   ")        -> {"error_type":"invalid_input","retryable":false,"message":"query must not be empty"}
query_memory(query="  ")          -> {"error_type":"invalid_input","retryable":false,"message":"query must not be empty"}
deep_search_memory(query="\t ")   -> {"error_type":"invalid_input","retryable":false,"message":"query must not be empty"}
visualize_memory_graph()          -> "Knowledge graph for your full memory: 2638 nodes, 3705 edges" (no-query path INTACT)
```

**Notes**
- `.agents/skills/tree-memory/SKILL.md`, `tasks/128-*.md` and `docs/` were NOT touched (task 128 /
  the coordinator own them); the scratch HTML the live `visualize_memory_graph` wrote under
  `apps/memory/.tree/graphs/` (gitignored) was deleted.
- The catch-all is `retryable=False` on purpose: an unenumerated exception is a bug until someone
  reads the traceback, so the model must stop rather than loop. The specific retryable codes are
  all matched by earlier clauses.
- Still not committed — Tester goes again.

### [Tester] 2026-09-12 17:30 — QA (round 2)

**Test summary**
- Format / lint / pre-commit, scoped to the #126 diff (`graph_tools.py`, `tools.py`, `scripts/query_graph.py`, the five changed/added test files, both doc files): PASS (`ruff format --check` / `ruff check` on exactly those files: "8 files already formatted" / "All checks passed!"; `pre-commit run --files <the same list>`: ruff check/format Passed).
- Unit tests: 3075 passed / 0 failed on the first full run (`make memory-tests`, env `local`) — matches the SWE's evidence exactly.
- Integration tests: N/A — no integration suite in this repo (per AGENTS.md); e2e re-done live below.
- Warnings: 0 pytest-collected warnings (one pre-existing, diff-unrelated `UserWarning` from `opik`'s Pydantic-v1 shim on Python 3.14 prints at import time on every run, before and after this diff — not a warning this task introduces).
- **Environment hazard, not a #126 defect** — see "Other issues found": a concurrent agent was actively writing an UNRELATED feature (`tree.mcp.hooks` / ADR-008 §5 session-end hook) into this SAME working tree DURING this QA session, contradicting "no other agent is editing... now". Mid-session, a repo-wide `make memory-format-check` failed solely on that agent's two files (`src/tree/mcp/hooks.py`, `tests/unit/mcp/test_hooks.py`, both untracked, neither touched by #126) and, briefly earlier, `test_hooks.py` existed without its implementation module (a collection-error window). By the END of this session that agent had finished and formatted its own work: a full, unscoped, repo-wide re-run of AC #8's literal command chain is green (below), so AC #8 is satisfied AS WRITTEN, not by file-scoped substitution — the interim scoped runs are kept below only as the record of what was checked while the hazard was live.

**E2E adversarial pass**

1. Happy path, live stdio MCP session on the DEFAULT server (`scripts/serve_mcp.py --transport stdio`, real `fastmcp.Client` + `PythonStdioTransport(env=os.environ)`, identifier `paul@example.com`, mode `graphrag`, real Mongo/Prefect up and healthy): `list_tools()` → 14 tools registered; ran the four blank-query calls and the no-query visualize call below against REAL infra, not mocks. PASS.
2. **The original blocking check, re-run live against the DEFAULT (graphrag) server** — this is the exact repro from round 1's FAIL:
   - `search_memory(query="   ")` → `{"error_type":"invalid_input","retryable":false,"message":"query must not be empty"}`
   - `query_memory(query="\t ")` → `{"error_type":"invalid_input","retryable":false,"message":"query must not be empty"}`
   - `deep_search_memory(query="")` → `{"error_type":"invalid_input","retryable":false,"message":"query must not be empty"}`
   - `visualize_memory_graph()` (no query) → `"Knowledge graph for your full memory: 2638 nodes, 3705 edges. ..."` — still draws, no envelope, no regression on the deliberately-unguarded path.
   All four match the SWE's claim exactly, against the real server, not just the unit suite. PASS.
3. **`RuntimeError`-injection sweep, 13/13 tools.** `tests/unit/mcp/test_error_envelope.py::test_no_tool_raises_through_fastmcp` (all 13 parametrised cases) + `test_the_sweep_covers_every_envelope_answering_tool` re-run in isolation: `56 passed in 1.96s` for the whole file. Additionally ran ONE case live/out-of-suite (not just re-reading the SWE's test) by calling `tools.search_web` directly with `tree.mcp.tools.web_search` patched to `side_effect=RuntimeError("boom-live")`: answer was `{"error_type":"internal_error","retryable":false,"message":"search_web failed: boom-live"}`, and the server log carried a full traceback (`ERROR | tree.mcp.tools - search_web failed: boom-live` + the `RuntimeError` stack) via `logger.exception` inside `internal_error()` — confirmed the catch-all logs, not just answers. PASS. This closes the round-1 PASS-WITH-NOTE gap (8/13 tools used to leak raw exceptions; now 13/13 answer the envelope).
4. **Clause precedence — specific exceptions still win over the new catch-all**, live (not from the suite), patching the SAME names the code imports (not re-exported aliases, which would silently no-op the patch):
   - `ObjectNotFound` from `tree.mcp.tools.dispatch_online_pipeline` on `ingest_url` → `{"error_type":"configuration_error","retryable":false,"message":"The 'online-pipeline/online-pipeline' deployment is not registered ..."}` — NOT `internal_error`.
   - `ServerSelectionTimeoutError` (a `PyMongoError`) from `tree.online.Document.find_one` (the ingest pre-flight) on `ingest_conversation` → `{"error_type":"storage_unavailable","retryable":true,"message":"memory store unreachable — try again"}` — NOT `internal_error`, NOT `search_unavailable`.
   - `SearchUnavailableError` from `tree.mcp.tools.retrieve_parents` on rag `search_memory` → `{"error_type":"search_unavailable","retryable":true,"message":"Memory search is temporarily unavailable: both legs down"}` — NOT `internal_error`.
   All three specific clauses fire before the trailing `except Exception`. PASS.
5. `visualize_memory_graph()` no-query path re-confirmed live against real Mongo (2638 nodes / 3705 edges, see #2) — the counter-guard the SWE added did not spread here. PASS.
6. Full local suite re-run twice during this session (before and after the concurrent agent's files landed): 3075 passed, then 3098 passed — no failures either time, no #126 regression from the interleaving.

**Acceptance criteria**
- [x] PASS — all 8 checkboxes re-verified unchanged from round 1 (helper shape, legacy-code mapping, rag/graphrag retrieval boundary, dispatch boundary, no legacy keys, the two `grep` greps) — evidence above plus `tests/unit/mcp/test_error_envelope.py` full pass (56/56). The 8th checkbox (`make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green) verified LITERALLY, repo-wide, at the end of the session, after the concurrent-agent hazard below had resolved itself: `293 files already formatted` / `All checks passed!` / all four pre-commit hooks `Passed` / `3104 passed` (3075 from #126 + 29 from the finished concurrent work, 0 failures).
- [x] PASS — Story "Model passes an empty query" now holds in **graphrag, the default mode** — the round-1 blocking FAIL. Evidence: live session step 2 above, all three of `search_memory` / `query_memory` / `deep_search_memory` answer the `invalid_input` envelope on a blank query in graphrag; `visualize_memory_graph` intentionally excluded and pinned by `test_visualize_memory_graph_still_draws_the_whole_graph_on_no_query`.
- [x] PASS — "Other issues found" #1 from round 1 (8/13 tools leaking raw exceptions) closed — `test_no_tool_raises_through_fastmcp` (13/13) + live injection above.
- [x] PASS — Style nit from round 1 (unparenthesised `except httpx.ConnectError, httpx.TimeoutException, PrefectHTTPStatusError:`) fixed — `tools.py` now reads `except (httpx.ConnectError, httpx.TimeoutException, PrefectHTTPStatusError) as exc:`, confirmed at the `_ingest` dispatch boundary; `ruff format --check` on that file passes (PEP 758 parens are stable once an `as` binding is present).

**Evidence**

```
$ uv run ruff format --check src/tree/mcp/graph_tools.py src/tree/mcp/tools.py scripts/query_graph.py \
    tests/unit/mcp/test_graph_tools.py tests/unit/mcp/test_search_web.py tests/unit/mcp/test_tools.py \
    tests/unit/scripts/test_query_graph.py tests/unit/mcp/test_error_envelope.py
8 files already formatted

$ uv run ruff check <same files>
All checks passed!

$ uv run --project apps/memory pre-commit run --files <same files + docs/adrs/008_mcp_tool_contract.md docs/glossary.md tasks/126-tool-error-envelope.md>
ruff check ... Passed / ruff format ... Passed

$ make memory-tests   (first run, before the concurrent agent's files existed)
============================ 3075 passed in 47.22s =============================

$ make memory-tests   (second run, after tree.mcp.hooks / test_hooks.py appeared)
============================ 3098 passed in 54.55s =============================

$ uv run pytest tests/unit/mcp/test_error_envelope.py -v
56 passed in 1.96s

Live stdio MCP session, DEFAULT (graphrag) server, real Mongo/Prefect:
search_memory(query="   ")        -> {"error_type":"invalid_input","retryable":false,"message":"query must not be empty"}
query_memory(query="\t ")         -> {"error_type":"invalid_input","retryable":false,"message":"query must not be empty"}
deep_search_memory(query="")      -> {"error_type":"invalid_input","retryable":false,"message":"query must not be empty"}
visualize_memory_graph()          -> "Knowledge graph for your full memory: 2638 nodes, 3705 edges. ..."

Live in-process injection (not from the suite):
search_web w/ web_search=RuntimeError("boom-live")           -> {"error_type":"internal_error","retryable":false,"message":"search_web failed: boom-live"}
  server log: ERROR tree.mcp.tools - search_web failed: boom-live  + full RuntimeError traceback (logger.exception)
ingest_url w/ dispatch_online_pipeline=ObjectNotFound        -> {"error_type":"configuration_error","retryable":false,...}
ingest_conversation w/ Document.find_one=ServerSelectionTimeoutError -> {"error_type":"storage_unavailable","retryable":true,...}
rag search_memory w/ retrieve_parents=SearchUnavailableError -> {"error_type":"search_unavailable","retryable":true,...}
```

**Other issues found**
- **Concurrent-write hazard on the shared worktree (not a #126 defect, but blocking-adjacent for the orchestrator).** During this QA session, a different in-flight task (ADR-008 §5, session-end hook: `apps/memory/src/tree/mcp/hooks.py`, `apps/memory/tests/unit/mcp/test_hooks.py`, `apps/memory/tests/unit/mcp/fixtures/session_end_transcript.jsonl`, all untracked) was actively created and completed in this same checkout — none of these existed at session start (only `test_error_envelope.py` was untracked per the opening `git status`), and none are touched by #126's diff. At one point mid-session `test_hooks.py` existed without its `tree.mcp.hooks` implementation yet, which would fail a repo-wide `make memory-tests` collection — purely a timing artifact of the interleaving, gone by the time both files existed. **Recommendation: the orchestrator must stage #126's files by explicit name (as this report and the prior round's both flag), never `git add -A`,** or this unrelated WIP rides along into #126's commit.
- Scratch HTML written by the live `visualize_memory_graph()` no-query call (`apps/memory/.tree/graphs/graph-20260912-141222.html`, gitignored) was deleted after verification; infra (`tree-mongodb`, `tree-mongot`, `tree-prefect-server`, `tree-prefect-worker`) left running and healthy, unchanged from session start.
- Could not invoke the `code-review` plugin from this Tester session (no slash-command execution surface available here despite `code-review@claude-plugins-official: true` in `.claude/settings.json`); the manual checklist above is the substitute signal for this round.
- QA's live stdio session and the `search_web` injection each fired `@track`, emitting real traces into the live `tree-memory` Opik project (visible as `OPIK: Started logging traces to ...` in stdout). Unavoidable side effect of exercising the tool path for real rather than only through mocks; not cleanable, and distinct from `test_opik_no_pollution.py` (which guards the TEST suite's own Opik config, not manual QA sessions against a real server).
- Minor test-coverage gap, not blocking: the 13-case `test_no_tool_raises_through_fastmcp` sweep exercises `visualize_memory_graph` only on its query branch (`structured_query_memory`); the no-query branch (`fetch_full_graph`) shares the same `try`/`except _retrieval_error` so it is covered by construction and its happy path is live-verified above, but no test pins "full-graph read fails -> envelope" specifically. Worth a follow-up parametrize case, not a requirement here.

**VERDICT: PASS**

Both round-1 findings closed with live evidence, not just re-read test output: the blank-query guard now fires identically across `tools.py` (rag) and `graph_tools.py` (all three affected graphrag readers), verified against the real DEFAULT server; the `visualize_memory_graph` no-query path is deliberately and correctly left unguarded, with a regression test pinning it. The `internal_error` catch-all closes the 8-tool gap from the round-1 PASS-WITH-NOTE, verified both via the new 13-case parametrised suite and one live out-of-suite injection with traceback confirmed in the log. Specific exception clauses (`configuration_error`, `storage_unavailable`, `search_unavailable`) still take precedence over the catch-all on all three boundaries checked. Full suite green throughout (3075 -> 3098 -> 3104 as the unrelated concurrent WIP landed and finished — no #126 regression at any point); the literal AC #8 command chain is green repo-wide as of the end of this session. Ready to commit.
