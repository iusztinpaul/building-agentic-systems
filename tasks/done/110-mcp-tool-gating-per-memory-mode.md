---
id: 110-mcp-tool-gating-per-memory-mode
feature: rag-graphrag-modes
status: done
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

- [x] Subprocess test (pattern of `test_server_startup.py::test_entrypoint_loaded_by_path_registers_all_tools`) with `TREE_MEMORY__MODE=rag`: `sorted(await mcp.get_tools())` == `["ingest_conversation", "ingest_file", "ingest_url", "scrape_web", "search_memory", "search_web"]`.
- [x] Same test with `TREE_MEMORY__MODE=graphrag`: the set additionally contains exactly `deep_search_memory`, `memory_dashboard`, `query_memory`, `review_confirm`, `review_list_pending`, `review_reject`, `visualize_memory_graph` (13 tools total, today's set).
- [x] rag `search_memory` tool schema (`(await mcp.get_tool("search_memory")).parameters`) has properties `{query, top_k}` only — no `max_hops`, `visualize`, `max_results`; graphrag schema has all five parameters as today.
- [x] rag `search_memory(query="parent chunk", top_k=3)` with `retrieve_parents` mocked to two parents returns a JSON string whose top-level `parents` array has 2 entries each with keys `parent_id, chunk_index, heading_path, content, score, document, matched_children` and no `embedding` key anywhere; with zero parents it returns `{"parents": []}`.
- [x] graphrag `search_memory` existing tests (`test_tools.py` visualize branches) pass with the import path updated; `_dual_graph_result` behaviour unchanged.
- [x] `mcp.instructions` in rag mode does not mention `query_memory`, `deep_search_memory` or "knowledge graph"; in graphrag mode it is today's text.
- [x] Importing `tree.mcp.server` in rag mode does NOT import `tree.mcp.graph_tools`, `tree.mcp.graph_app`, `tree.mcp.dashboard_app` (`sys.modules` assertion in the subprocess test).
- [x] `apps/memory/README.md` "Tools exposed" table is split into "both modes" and "graphrag only" with `search_memory`'s two signatures documented.
- [x] `make memory-format-check && make memory-lint-check && make memory-tests` green.

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

### [SWE] 2026-09-05 19:45 — Implementation

**Files modified**
- `apps/memory/src/tree/mcp/server.py` — `MEMORY_MODE` (the ONE import-time read of `app_config.memory.mode`), mode-aware `instructions`, `graph_tools` imported only in graphrag.
- `apps/memory/src/tree/mcp/graph_tools.py` (new) — the seven graph-only tools + `_serialize` / `_dual_graph_result`, plus the `graph_app` / `dashboard_app` side-effect imports.
- `apps/memory/src/tree/mcp/tools.py` — now the six both-mode tools; new rag `search_memory(query, ctx, top_k)` on `retrieve_parents`, registered only when `MEMORY_MODE == "rag"`.
- `apps/memory/src/tree/mcp/graph_app.py` — docstring pointer `tree.mcp.tools` → `tree.mcp.graph_tools` (no code change).
- `apps/memory/README.md` — "Tools exposed" split into both-modes / graphrag-only + a table of the two `search_memory` signatures.
- `apps/memory/tests/unit/mcp/test_tool_gating.py` (new) — one subprocess per mode, asserting tool sets, schemas, `sys.modules` isolation and instructions.
- `apps/memory/tests/unit/mcp/test_graph_tools.py` (new) — the visualize / `_serialize` tests moved with their module.
- `apps/memory/tests/unit/mcp/test_tools.py` — rag `search_memory` JSON contract.
- `apps/memory/tests/unit/mcp/test_tools_user_id_pinning.py`, `apps/memory/tests/unit/test_observability_tags.py` — follow the moved module.

**Tests**
- Unit: 2311 passing, 0 failing — `make memory-tests`
- Integration: N/A — no integration suite in this repo (unit + real-run e2e).

**Acceptance criteria**
- [x] rag registers exactly six tools — `tests/unit/mcp/test_tool_gating.py::TestRegisteredToolSet::test_rag_mode_registers_only_the_six_shared_tools`
- [x] graphrag adds exactly the seven graph tools (13 total) — `…::test_graphrag_mode_adds_exactly_the_seven_graph_tools`, `…::test_graph_tools_are_unknown_to_a_rag_server`
- [x] `search_memory` schema per mode — `…::TestSearchMemorySignaturePerMode::{test_rag_advertises_query_and_top_k_only,test_graphrag_keeps_its_five_graph_expansion_parameters}`
- [x] rag `search_memory` JSON shape / empty result — `tests/unit/mcp/test_tools.py::TestRagSearchMemory::{test_returns_one_json_entry_per_retrieved_parent,test_never_leaks_an_embedding,test_no_hits_returns_an_empty_parents_array,test_top_k_is_the_result_cap_passed_to_retrieval,test_docstring_states_the_parent_document_contract}`
- [x] graphrag `search_memory` visualize behaviour unchanged — `tests/unit/mcp/test_graph_tools.py::TestGraphToolsDualDelivery` (moved verbatim, import path only)
- [x] mode-aware `instructions` — `…::TestModeAwareInstructions` (7 params + 4 tests)
- [x] rag never imports `graph_tools` / `graph_app` / `dashboard_app` — `…::test_rag_mode_never_imports_the_graph_modules`
- [x] README "Tools exposed" split with both signatures — `apps/memory/README.md`
- [x] format-check / lint-check / tests green

**Evidence**
```
$ make memory-format-check && make memory-lint-check
267 files already formatted
All checks passed!

$ make pre-commit
prettier / ruff check / ruff format / biome check ....................Passed

$ make memory-tests
============================ 2311 passed in 22.23s =============================

# red check — gate removed (import graph_tools unconditionally, single instructions text)
$ uv run pytest tests/unit/mcp/test_tool_gating.py -q
9 failed, 14 passed          # restored → 23 passed

# LIVE, local Docker Mongo (`memory` collection seeded by an in-process rag run of
# doc 6a8ea976dfbcf322b4428263: nodes_written=17 edges_written=0 → 1 document,
# 1 parent, 15 children)
$ TREE_MEMORY__MODE=rag make memory-serve-mcp USER_ID=6a8ea9579a7aeb13175955c8 TRANSPORT=streamable-http
$ uv run fastmcp list http://127.0.0.1:8000/mcp --auth none --json
n = 6 → ingest_conversation, ingest_file, ingest_url, scrape_web, search_memory, search_web
$ uv run fastmcp call … search_memory query="how does memory for agents work" top_k=3
is_error: False | embedding leaked: False | n parents: 1
{'score': 0.03279, 'title': 'The AI Agents Roadmap Nobody Is Teaching You',
 'uri': 'https://www.decodingai.com/p/ai-agents-foundations-course',
 'matched_children': 12, 'content_chars': 14167,
 'keys': ['chunk_index','content','document','heading_path','matched_children','parent_id','score']}
$ uv run fastmcp call … query_memory query="who is paul"
Error: Tool query_memory not found. Did you mean: search_memory?

$ make memory-serve-mcp USER_ID=… TRANSPORT=streamable-http      # graphrag (default)
n = 13 → deep_search_memory, ingest_conversation, ingest_file, ingest_url, memory_dashboard,
         query_memory, review_confirm, review_list_pending, review_reject, scrape_web,
         search_memory, search_web, visualize_memory_graph
search_memory params: ['max_hops','max_results','query','top_k','visualize']
$ uv run fastmcp call … search_memory query="how does memory for agents work" top_k=3 max_hops=1
is_error: False | rows: 1 | kinds: {'node'} | types: {'chunk'}   # parent seed, no edges in this corpus
```

**Notes**
- FastMCP 3.2 has no `mcp.get_tools()` (the AC's wording); the equivalents used are `await mcp.list_tools()` (→ names) and `await mcp.get_tool("search_memory")` → `.parameters` for the schema.
- The rag `search_memory` function is DEFINED unconditionally in `tools.py` and registered with an explicit `mcp.tool(search_memory)` under `if MEMORY_MODE == "rag"`, so it stays importable/unit-testable in a graphrag test process. In graphrag the name is registered only by `graph_tools`; the two are never both registered.
- Ingest-tool docstrings still say "knowledge graph" in rag mode (`_ingest` helpers were declared untouched, and #111 owns the docs pass). Only `mcp.instructions` is mode-aware here.
- The live rag corpus was produced by running `memory_extract_etl_worker` + `memory_indexing` IN-PROCESS with `TREE_MEMORY__MODE=rag`: the Dockerized Prefect worker runs the `main` checkout, not this worktree, so `make memory-run-memory-pipeline` there wrote the old `knowledge_graph` rows instead.

### [Tester] 2026-09-05 20:05 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`ruff format --check` 267 files, `ruff check` all passed, `pre-commit` all hooks passed)
- Unit tests: 2311 passed / 0 failed (run twice for stability — both runs 2311 passed, 0 flaky)
- Integration tests: N/A — no integration suite in this repo per `AGENTS.md`
- Warnings: 1 pre-existing, unrelated `UserWarning` from `opik`'s vendored pydantic-v1 shim on Python 3.14 (`opik/rest_api/core/pydantic_utilities.py:13`) — present regardless of this diff, not introduced by this task, not a test-suite warning

**E2E adversarial pass**
- Happy path rag: `TREE_MEMORY__MODE=rag uv run python scripts/serve_mcp.py --user-id 6a8ea9579a7aeb13175955c8 --transport streamable-http` then `uv run fastmcp list … --json` → exactly `["ingest_conversation","ingest_file","ingest_url","scrape_web","search_memory","search_web"]`, `search_memory` `inputSchema.properties` == `{query, top_k}`; `uv run fastmcp call … search_memory query="how does memory for agents work" top_k=3` → 1 parent, keys `{parent_id,chunk_index,heading_path,content,score,document,matched_children}`, no `embedding` (PASS)
- Happy path graphrag: same server without the mode override → 13 tools, `search_memory` params `{max_hops,max_results,query,top_k,visualize}`; `search_memory query=… top_k=3 max_hops=1 visualize=true` → `is_error:false`, plain-text serialized graph JSON plus a `resource_link` to a written `.tree/graphs/*.html` file — dual-delivery unchanged (PASS)
- Break path 1 (unknown tool / state edge — graph tool against rag server): `uv run fastmcp call … query_memory query="who is paul"` (rag-mode server) → `Error: Tool query_memory not found. Did you mean: search_memory?` — standard MCP unknown-tool error, no half-working path, matches User Story 2 (PASS)
- Break path 2 (malformed input — extra/unknown parameter on the narrowed rag schema): `uv run fastmcp call … search_memory query="paul" top_k=3 max_hops=2` (rag-mode server) → Pydantic `unexpected_keyword_argument` validation error — proves the rag schema genuinely lacks `max_hops` end-to-end, not just in the docstring (PASS)
- Break path 3 (boundary/hostile config — `TREE_MEMORY__MODE=junk` at import): `TREE_MEMORY__MODE=junk uv run python -c "import tree.mcp.server"` → `pydantic_core.ValidationError: Input should be 'rag' or 'graphrag'` at import time, process exits non-zero, no silent fallback (PASS)
- Break path 4 (state edge — duplicate tool-name registration): manually re-registered `tools.search_memory` on an already-booted graphrag `mcp` instance → FastMCP logged `WARNING Component already exists: tool:search_memory@` and silently overwrote rather than raising. This is NOT reachable on the real boot path (the `if MEMORY_MODE == "rag"` guard in `tools.py` and the `if MEMORY_MODE == "graphrag": import graph_tools` guard in `server.py` are both driven by the same single `MEMORY_MODE` read, so exactly one `search_memory` is ever registered per process) — recorded as a finding, not a failure, per the SWE's own probe question ("is the gate the only guard, and does FastMCP fail loudly on duplicate names?" — answer: no, FastMCP does not fail loudly; the mode-gate is a load-bearing single point of correctness with no independent safety net)

**Acceptance criteria**
- [x] PASS — rag registers exactly the six shared tools — live: `fastmcp list` → 6 names match exactly; test: `tests/unit/mcp/test_tool_gating.py::TestRegisteredToolSet::test_rag_mode_registers_only_the_six_shared_tools`
- [x] PASS — graphrag adds exactly the seven graph tools (13 total) — live: `fastmcp list` → 13 names match exactly; test: `…::test_graphrag_mode_adds_exactly_the_seven_graph_tools`, `…::test_graph_tools_are_unknown_to_a_rag_server`
- [x] PASS — rag `search_memory` schema `{query, top_k}` only, graphrag keeps all five — live `inputSchema.properties` confirmed both modes; test: `…::TestSearchMemorySignaturePerMode::{test_rag_advertises_query_and_top_k_only,test_graphrag_keeps_its_five_graph_expansion_parameters}`. Judged the `list_tools()`/`get_tool().parameters` substitution for the AC's literal `mcp.get_tools()` wording as acceptable: verified `dir(FastMCP)` on the installed `fastmcp==3.2.0` has no `get_tools` method (only `list_tools`, `get_tool`, `_list_tools`, `_get_tool`), so the substitution is the only available API, not a shortcut around the intent.
- [x] PASS — rag `search_memory` JSON shape (2 parents, exact key set, no `embedding`) and `{"parents": []}` on zero hits — `tests/unit/mcp/test_tools.py::TestRagSearchMemory::{test_returns_one_json_entry_per_retrieved_parent,test_never_leaks_an_embedding,test_no_hits_returns_an_empty_parents_array,test_top_k_is_the_result_cap_passed_to_retrieval}`; live call reproduced the same shape against real Mongo data
- [x] PASS — graphrag `search_memory` visualize behaviour unchanged — `tests/unit/mcp/test_graph_tools.py::TestGraphToolsDualDelivery` (moved verbatim); live `visualize=true` call returned the serialized text plus a `graphs://…html` resource link and wrote the file to `.tree/graphs/`
- [x] PASS — mode-aware `instructions` — read `_RAG_INSTRUCTIONS`/`_GRAPHRAG_INSTRUCTIONS` in `apps/memory/src/tree/mcp/server.py:230-249`: rag text names only the six shared tools, no `query_memory`/`deep_search_memory`/"knowledge graph"; test: `…::TestModeAwareInstructions` (7 params + 4 tests)
- [x] PASS — rag never imports `graph_tools`/`graph_app`/`dashboard_app` — live: `TREE_MEMORY__MODE=rag uv run python -c "import tree.mcp; …"` → `sys.modules` has none of the three; grep confirms `deep_search` (a fourth graph-only module the SWE flagged) is imported only from `graph_tools.py`, never from `tools.py`; test: `…::test_rag_mode_never_imports_the_graph_modules`
- [x] PASS — README "Tools exposed" split with both `search_memory` signatures — `apps/memory/README.md` diff has "Both modes (6 tools)" / "`graphrag` only (7 more, 13 total)" tables plus a "The two `search_memory` signatures" table with both parameter sets and return shapes
- [x] PASS — `make memory-format-check && make memory-lint-check && make memory-tests` green — see Evidence; re-ran the full suite twice, stable at 2311 passed both times

**Evidence**
```
$ make memory-format-check && make memory-lint-check
267 files already formatted
All checks passed!

$ make pre-commit
prettier / ruff check / ruff format / biome check ....................Passed

$ make memory-tests   (run 1)
============================ 2311 passed in 24.12s =============================
$ make memory-tests   (run 2)
============================ 2311 passed in 26.02s =============================

$ uv --directory apps/memory run python -c "from fastmcp import FastMCP; print([a for a in dir(FastMCP) if 'tool' in a.lower()])"
['_call_tool_mcp', '_get_tool', '_list_tools', '_list_tools_mcp', 'add_tool', ...,
 'call_tool', 'get_app_tool', 'get_tool', 'list_tools', 'remove_tool', ..., 'tool']
# no get_tools() — confirms the SWE's substitution is FastMCP 3.2.0's real API

$ TREE_MEMORY__MODE=rag <serve rag mode> && uv run fastmcp list http://127.0.0.1:8000/mcp --auth none --json
6 tools: ingest_conversation, ingest_file, ingest_url, scrape_web, search_memory, search_web
search_memory.inputSchema.properties: {query, top_k}

$ uv run fastmcp call http://127.0.0.1:8000/mcp --auth none search_memory query="how does memory for agents work" top_k=3
n parents: 1, keys: {parent_id,chunk_index,heading_path,content,score,document,matched_children}, has_embedding: False

$ uv run fastmcp call http://127.0.0.1:8000/mcp --auth none query_memory query="who is paul"
Error: Tool query_memory not found. Did you mean: search_memory?

$ <restart server without TREE_MEMORY__MODE override, i.e. graphrag> && uv run fastmcp list … --json
13 tools incl. deep_search_memory, memory_dashboard, query_memory, review_*, visualize_memory_graph
search_memory.inputSchema.properties: {max_hops, max_results, query, top_k, visualize}

$ uv run fastmcp call … search_memory query=… top_k=3 max_hops=1 visualize=true
is_error: False; content includes a resource_link to graphs://how-does-memory-for-agents-work-*.html
$ ls apps/memory/.tree/graphs/
how-does-memory-for-agents-work-20260905-163704.html

$ TREE_MEMORY__MODE=junk uv run python -c "import tree.mcp.server"
pydantic_core._pydantic_core.ValidationError: 1 validation error for AppConfig
memory.mode: Input should be 'rag' or 'graphrag' [type=literal_error, input_value='junk']
```

**Other issues found**
- Ingest-tool docstrings (`ingest_url`, `ingest_file`, `ingest_conversation` in `apps/memory/src/tree/mcp/tools.py`) still say "into the knowledge graph" and render that way live in rag mode (confirmed via `fastmcp list --json` against a rag-mode server). The task's own Scope explicitly declares `_ingest` helpers untouched, and no AC in this task requires mode-neutral ingest docstrings, so this does not block #110. However, I checked `tasks/111-memory-graph-package-docs-and-e2e.md` and its Scope/AC list covers `README.md`, `.agents/skills/*`, and the package move — it does **not** explicitly list the three ingest-tool docstrings either. Flagging so it doesn't fall through the cracks between #110 and #111; a one-line follow-up (or an explicit line item added to #111) would close it.
- FastMCP silently overwrites (warn-and-replace, not raise) on duplicate tool-name registration (see Break path 4). Not a live bug today — the single `MEMORY_MODE` read is the sole and sufficient guard on the real boot path — but worth knowing if either the `if MEMORY_MODE == "rag": mcp.tool(search_memory)` line in `tools.py` or the `if MEMORY_MODE == "graphrag": import tree.mcp.graph_tools` line in `server.py` is ever touched independently in a future change: a mismatch would silently ship a wrong `search_memory`, not crash loudly.
- `search_memory(query="", top_k=3)` in rag mode surfaces a raw Voyage API 400 error message ("Input cannot contain empty strings...") straight to the MCP client rather than a clean `{"error": "invalid_input", ...}` envelope, unlike every ingestion tool's input-validation pattern (`ingest_url`, `search_web`, `scrape_web`, `ingest_conversation` all pre-validate and return a structured error). This is `retrieve_parents`'s behaviour (owned by #109, not new code in #110) and there is no AC requiring rag `search_memory` to validate its input, so not blocking — but worth a follow-up given how the sibling tools already establish the structured-error convention.
- The code-review plugin (`code-review@claude-plugins-official`) is enabled in `.claude/settings.json` but is a slash command, not invocable as a subagent tool call in this session; substituted a manual review pass covering the same ground (full type annotations on every function/method in the three touched/added source files via an AST walk, no `print()` in library code — the one `print()` in `test_tool_gating.py` is inside the subprocess-probe string, not library code — no secrets, no raw-query injection surface, no unrelated files staged).

**VERDICT: PASS**

### [PA] 2026-09-05 20:32 — Acceptance Review

**VERDICT: REJECT** (feature-level verdict for PR #41, `rag-graphrag-modes`)

Tool gating is correct from the client's side: 6 vs 13 tools, the two `search_memory` signatures, mode-aware `instructions`, the unknown-tool error, the README tables. Rollup Issue 4 is adjacent: the `tree-memory` skill (rewritten in #111) still tells the agent the `ingest_*` tools "return node/edge counts" — they return `{"status", "flow_run_id"}` asynchronously. Story 4 of THIS task made the same wrong claim (`edges_written: 0`); the story is superseded by the corrected contract, the code was never wrong.

Filed ONE rollup task for the whole feature: `tasks/112-pa-rejection-rag-graphrag-modes.md` (8 issues). Pipeline re-runs from the inner loop with the rollup task; on green, re-run acceptance on this task.
