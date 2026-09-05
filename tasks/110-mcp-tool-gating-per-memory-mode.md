---
id: 110-mcp-tool-gating-per-memory-mode
feature: rag-graphrag-modes
status: pending
---

# MCP tool registration gated by **Memory mode**; mode-specific `search_memory`

Tags: `mcp`, `memory`
Depends on: #109
Blocks: #111
Implements: ADR-006 — Decision 5

## Scope

The FastMCP server registers exactly the tool set that makes sense for `app_config.memory.mode`
(read once at import in `tree/mcp/server.py`).

**Registered in BOTH modes** (`tree/mcp/tools.py`): `search_memory`, `ingest_url`,
`ingest_file`, `ingest_conversation`, `search_web`, `scrape_web`.
**graphrag ONLY** (new module `tree/mcp/graph_tools.py` + the existing `graph_app.py`,
`dashboard_app.py`, imported by `server.py` only when mode is graphrag): `query_memory`,
`deep_search_memory`, `visualize_memory_graph`, `memory_dashboard`, `review_list_pending`,
`review_confirm`, `review_reject`.

**`search_memory` has a mode-specific signature** (one name, two functions; the module
registers the one matching the mode — never both):
- rag: `search_memory(query: str, ctx, top_k: int = 10) -> str` — calls
  `tree.memory.rag.retrieval.retrieve_parents`, returns `RetrievalResult.model_dump_json(indent=2)`
  (parents with `document`, `heading_path`, `content`, `score`, `matched_children`). No
  `max_hops`, no `visualize`, no `max_results` (`top_k` IS the result cap). Docstring: "Hybrid
  (vector + text) search over child chunks, returning the distinct parent chunks with their
  document metadata."
- graphrag: today's `search_memory(query, ctx, top_k=10, max_hops=1, max_results=10,
  visualize=False) -> str | ToolResult`, unchanged behaviour on the moved
  `tree.memory.graph.retrieval.query_memory` (seeds now resolve to parents — #109).

`FastMCP(instructions=...)` in `server.py` becomes mode-aware: the rag text mentions only the
six rag tools; the graphrag text is today's. `_ingest` helpers and the URL router are untouched
(ingestion already routes through `online_pipeline` → mode-branching worker, #108).
No dead parameters, no "ignored + logged" branches.

## Acceptance Criteria

- [ ] Subprocess test (pattern of `test_server_startup.py::test_entrypoint_loaded_by_path_registers_all_tools`) with `TREE_MEMORY__MODE=rag`: `sorted(await mcp.get_tools())` == `["ingest_conversation", "ingest_file", "ingest_url", "scrape_web", "search_memory", "search_web"]`.
- [ ] Same test with `TREE_MEMORY__MODE=graphrag`: the set additionally contains exactly `deep_search_memory`, `memory_dashboard`, `query_memory`, `review_confirm`, `review_list_pending`, `review_reject`, `visualize_memory_graph` (13 tools total, today's set).
- [ ] rag `search_memory` tool schema (`(await mcp.get_tool("search_memory")).parameters`) has properties `{query, top_k}` only — no `max_hops`, `visualize`, `max_results`; graphrag schema has all five parameters as today.
- [ ] rag `search_memory(query="parent chunk", top_k=3)` with `retrieve_parents` mocked to two parents returns a JSON string whose top-level `parents` array has 2 entries each with keys `parent_id, chunk_index, heading_path, content, score, document, matched_children` and no `embedding` key anywhere; with zero parents it returns `{"parents": []}`.
- [ ] graphrag `search_memory` existing tests (`test_tools.py` visualize branches) pass with the import path updated; `_dual_graph_result` behaviour unchanged.
- [ ] `mcp.instructions` in rag mode does not mention `query_memory`, `deep_search_memory` or "knowledge graph"; in graphrag mode it is today's text.
- [ ] Importing `tree.mcp.server` in rag mode does NOT import `tree.mcp.graph_tools`, `tree.mcp.graph_app`, `tree.mcp.dashboard_app` (`sys.modules` assertion in the subprocess test).
- [ ] `apps/memory/README.md` "Tools exposed" table is split into "both modes" and "graphrag only" with `search_memory`'s two signatures documented.
- [ ] `make memory-format-check && make memory-lint-check && make memory-tests` green.

## User Stories

### Story: Claude Code connects to a rag-mode server
1. Operator runs `TREE_MEMORY__MODE=rag make memory-serve-mcp USER_IDENTIFIER=paul TRANSPORT=streamable-http`.
2. The client's tool list shows six tools; `search_memory` advertises `query` and `top_k` only.
3. Calling `search_memory(query="how does memory for agents work", top_k=3)` returns JSON with three parents, each carrying `document.title` and `matched_children`.

### Story: The agent tries a graph-only tool against a rag server
1. The harness calls `query_memory(...)` on the rag-mode server.
2. The MCP client receives the standard "unknown tool" error — the tool was never registered, so there is no half-working path.

### Story: Same server in graphrag mode
1. Operator restarts without the override.
2. Thirteen tools are listed; `search_memory(query="…", max_hops=1, visualize=True)` returns the serialized results plus the graph (iframe or file), exactly as before this feature.

### Story: Ingestion behaves per mode without new parameters
1. In rag mode the agent calls `ingest_url("https://www.decodingai.com/p/agentic-harness-engineering")`.
2. The returned summary reports rows written and `edges_written: 0`; in graphrag the same call reports nodes AND edges.

## Out of scope

- Per-request mode switching (mode is fixed at server boot, like `user_id`).
- Changing `visualize_memory_graph`, `memory_dashboard`, review tools' behaviour.
- Harness (`apps/harness`) changes.

---

Blocked by: #109

## Log
