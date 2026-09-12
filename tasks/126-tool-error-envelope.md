---
id: 126-tool-error-envelope
feature: mcp-tool-contracts
status: pending
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

- [ ] `tool_error("invalid_input", "x", retryable=False)` → `{"error_type":"invalid_input","retryable":false,"message":"x"}` and nothing else — `tests/unit/mcp/test_error_envelope.py::test_helper_shape`.
- [ ] Every legacy code maps to the table above (parametrised over the tool + trigger) — `::TestMapping` (`ingest_url` bad scheme → `unsupported_url`/false; `ingest_conversation` blank → `invalid_input`/false; `search_web` `ingest_top_k=0` → `invalid_input`/false; `scrape_web` 6 URLs → `invalid_input`/false; `review_confirm` bad state → `invalid_state`/false; `ingest_url` `httpx.ConnectError` → `network_error`/true; HTTP 503 → `http_error`/true; HTTP 404 → `http_error`/false).
- [ ] rag `search_memory` with `retrieve_parents` raising `SearchUnavailableError` → `search_unavailable`/true; raising `RuntimeError` → `internal_error`/false; both logged at ERROR with traceback — `tests/unit/mcp/test_tools.py::TestSearchMemoryErrors`.
- [ ] graphrag `search_memory`, `query_memory`, `deep_search_memory`, `visualize_memory_graph(query=…)` return the same two envelopes under the same faults — `tests/unit/mcp/test_graph_tools.py::TestReaderErrors`.
- [ ] `_ingest` with `run_deployment` raising `httpx.ConnectError` → `pipeline_unavailable`/true; raising `ObjectNotFound` → `configuration_error`/false naming `make memory-serve-workflows` — `tests/unit/mcp/test_tools.py::TestDispatchErrors`.
- [ ] No `"detail":` literal remains in `tools.py` / `graph_tools.py` — `test_error_envelope.py::test_no_legacy_error_keys`.
- [ ] `grep -rn '"error":' apps/memory/src/tree/mcp/tools.py apps/memory/src/tree/mcp/graph_tools.py` shows only the `search_web` nested ingest block.
- [ ] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green.

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
