---
id: 104-query-memory-graph-app
feature: single-graph-renderer
status: pending
---

# `query_memory` + `search_memory` (`visualize=True`) become dual MCP App ∥ HTML-file graph tools

Tags: `mcp`, `memory`
Depends on: #103
Blocks: —
Implements: ADR-005 (`single-graph-renderer`)

## Scope

`visualize_memory_graph` already implements the dual delivery: inline MCP App iframe when
`ctx.client_supports_extension(UI_EXTENSION_ID)` (payload in a `content` block annotated
`audience=["user"]`), else self-contained HTML under `.tree/graphs/` + best-effort
`webbrowser.open` + a `graphs://<name>` `ResourceLink`. Give `query_memory(visualize=True)` AND
`search_memory(visualize=True)` the SAME dual behaviour — from a visualization standpoint all
THREE graph tools behave identically; both paths, chosen by client capability, NOT one or the
other.

**1. Extract the shared helper in `graph_app.py`:**
`_graph_tool_result(ctx: Context, payload: dict[str, list[dict[str, Any]]], summary: str, *,
query: str = "", as_html_file: bool = False) -> ToolResult` — it owns the capability check and BOTH
branches currently inlined in `visualize_memory_graph` (UI branch: model-visible summary text block
+ `audience=["user"]` payload JSON block + `structured_content`; fallback branch:
`_render_graph_file` + try/except `webbrowser.open` + path text + `ResourceLink`). Refactor
`visualize_memory_graph` to call it — its observable behaviour must be UNCHANGED (the existing
channel tests at `test_graph_app.py:369–:465` keep passing without edits to their assertions).
After this task the helper serves THREE tools; the capability check exists in exactly one place.

**2. Investigation step (MANDATORY, do not assume):** verify via the FastMCP docs (context7:
`/jlowin/fastmcp`, MCP Apps / `AppConfig` / `resource_uri`) AND a live `make memory-serve-mcp` boot
whether MULTIPLE tools may declare `app=AppConfig(resource_uri=GRAPH_VIEW_URI)` sharing ONE `ui://`
resource — with three tools now, not two.
- **Branch A (shared URI works):** `query_memory` and `search_memory` both gain
  `@mcp.tool(app=AppConfig(resource_uri=GRAPH_VIEW_URI))`.
- **Branch B (it does not):** each gains its own resource_uri (e.g.
  `ui://tree-memory/query-graph.html`, `ui://tree-memory/search-graph.html`) whose resource
  functions serve the SAME `_GRAPH_HTML` string and the same `ResourceCSP` (unpkg + jsdelivr) —
  no template duplication.
Record which branch was taken and the evidence in the `## Log`.

**3. One shared seam in `tools.py` — do NOT duplicate the branch logic per tool.**
`query_memory` (`output = _serialize(results)` … `output += _visualize(results)`) and
`search_memory` (`docs = result.nodes + result.edges` truncated to `max_results`,
`output = _serialize(docs)` … `output += _visualize(docs)`) share the identical shape:
serialized docs + optional graph. Replace `_visualize` with ONE module-level helper — suggested
`_dual_graph_result(ctx: Context, docs: list[dict[str, Any]], serialized: str, query: str)
-> str | ToolResult` — that:
- keeps `_visualize`'s kind-split (`d.get("kind") == "node"` / `"edge"`); when no docs carry a
  `kind` field, returns the plain `str` of `serialized` + today's "Visualization skipped" note
  (never a `ToolResult`);
- otherwise builds the **Graph payload** via `to_graph_payload(QueryResult(nodes=…, edges=…))` and
  returns `_graph_tool_result(ctx, payload, summary, query=query)` where the model-visible
  `summary` CONTAINS `serialized` plus a one-line graph note (e.g. "Interactive graph view:
  N nodes, M edges").
`_visualize` itself is DELETED in this task — after the rewire nothing calls it (its file-writing
duty lives in `_graph_tool_result`'s fallback branch). SWE may adjust the helper's exact
signature/name; the hard requirements are: one seam, no per-tool duplication of payload-build or
branch logic, and the kind-split/skip-note behaviour preserved.

**4. Rewire BOTH tools in `tools.py`** (same treatment each):
- Turn the registration side-effect import into a real one for graph_app (keep `dashboard_app` as
  the side-effect import): import `_graph_tool_result` (+ `GRAPH_VIEW_URI`/`AppConfig` per branch)
  from `tree.mcp.graph_app`. `graph_app.py` does NOT import `tools.py` — no cycle (verified).
- `visualize=False` (or empty results/docs): behaviour UNCHANGED — each returns the serialized-JSON
  `str` exactly as today.
- `visualize=True` with results: return `_dual_graph_result(ctx, docs, output, query)`.
- **Return-type contract (the behaviour change — spec it carefully):** both tools annotate
  `-> str | ToolResult`. The MODEL-visible text of any `ToolResult` must contain the SAME
  serialized output the tool produces today (`_serialize(results)` / `_serialize(docs)`) — these
  tools' contract is answering the query; the model must never lose the data. The full node/edge
  **Graph payload** rides ONLY in the `audience=["user"]` JSON block (and `structured_content`)
  for the iframe — the model never sees the dump. On the non-UI fallback branch the text block =
  serialized output + the `.tree/graphs/` path note, plus the `graphs://` `ResourceLink` —
  replacing #103's file-path-only note.
- Both docstrings gain the same guidance as `visualize_memory_graph` (inline view when supported;
  otherwise file + resource link; never hand-author HTML).

**5. Investigations that now cover BOTH tools (mandatory, findings in the `## Log`):**
- FastMCP accepts the `str | ToolResult` union return annotation on a tool.
- The Opik `@track` decorator (`tools.py:122` on `query_memory`, `:161` on `search_memory` —
  `visualize_memory_graph` is NOT tracked, so this is untested ground) passes a `ToolResult`
  through unharmed. If `track` chokes on `ToolResult`, fix the span-capture serialization or
  exclude the payload — do NOT drop tracing from either tool.

**6. Tests** (call the `/squid-testing-python` skill; proportionate to the new seam, no broad new
suite):
- `_graph_tool_result` unit tests: UI-capable ctx → summary block + `audience=["user"]` payload
  block + `structured_content`; non-UI ctx → file written + `ResourceLink` + no crash when
  `webbrowser.open` raises (mocked).
- `visualize_memory_graph` refactor safety: the existing :369–:465 tests pass unchanged.
- `query_memory` AND `search_memory` (parametrize where natural): (a) `visualize=False` returns
  the plain serialized `str` (unchanged); (b) `visualize=True` + UI ctx returns a `ToolResult`
  whose model-visible text contains the serialized output AND whose payload block parses to
  `{nodes, edges}`; (c) `visualize=True` + non-UI ctx returns text with the `.tree/graphs/` path +
  `graphs://` link; (d) docs without `kind` → "Visualization skipped" note as a plain `str`, no
  `ToolResult`.

## Acceptance criteria

- [ ] `grep -n "_graph_tool_result" apps/memory/src/tree/mcp/graph_app.py
      apps/memory/src/tree/mcp/tools.py` shows ONE definition (graph_app) and THREE consumers
      (`visualize_memory_graph`, plus the shared `tools.py` seam serving `query_memory` and
      `search_memory`); the capability check `client_supports_extension(UI_EXTENSION_ID)` appears
      exactly once in the codebase (`grep -rn "client_supports_extension" apps/memory/src`).
- [ ] `grep -n "_visualize" apps/memory/src/tree/mcp/tools.py` returns nothing — the old file-only
      helper is deleted, replaced by the single dual-result seam used by BOTH tools.
- [ ] `query_memory` AND `search_memory` are each decorated with `app=AppConfig(resource_uri=...)`
      (Branch A: shared `GRAPH_VIEW_URI`; Branch B: own `ui://` URIs serving the same
      `_GRAPH_HTML`), and the `## Log` records the investigated evidence for the branch taken,
      plus the `str | ToolResult` and `@track`-passthrough findings.
- [ ] Unit tests prove the dual contract for BOTH tools with `visualize=True`: UI ctx →
      `ToolResult` with the serialized output in the model-visible text + `audience=["user"]`
      `{nodes, edges}` JSON block; non-UI ctx → text containing a `.tree/graphs/….html` path +
      `graphs://` `ResourceLink`. `visualize=False` returns the identical `str` as before for both
      (tests assert no `ToolResult`).
- [ ] Existing `test_graph_app.py` channel tests (:369–:465) pass without assertion changes
      (refactor safety), plus new coverage that the shared helper serves all three tools.
- [ ] E2E (local env): with `make memory-serve-mcp` running, a fastmcp client (no UI extension)
      calls BOTH `query_memory(query="What does Paul work on?", visualize=True)` AND
      `search_memory(query="quantization", visualize=True)` → each result text contains its
      serialized output AND a `.tree/graphs/` path; the linked `graphs://` resources return HTML
      that opens and draws the graph in a browser.
- [ ] `make memory-format-fix && make memory-lint-fix && make memory-format-check &&
      make memory-lint-check`, `make pre-commit`, and `make memory-tests` all clean/green.
- [ ] [HUMAN] In an MCP Apps-capable host, `query_memory(..., visualize=True)` AND
      `search_memory(..., visualize=True)` each render the inline interactive graph iframe (visual
      judgment — automated tests only assert the content blocks), and plain calls
      (`visualize=False`) still read normally.

## User stories

### Story: User asks a question and sees the graph inline
1. In an MCP Apps-capable client, the user asks a question the agent answers via
   `query_memory(query="What does Paul work on?", visualize=True)`.
2. The model receives the serialized node/edge JSON and answers from it, exactly as before.
3. The host renders the "Tree: Your Rooted Memory" interactive graph inline (same view as
   `visualize_memory_graph`) — the user explores it (hover, search, zoom) without any file.

### Story: Semantic search draws the same inline graph
1. In the same MCP Apps-capable client, the agent calls
   `search_memory(query="quantization", visualize=True, top_k=10)`.
2. The model receives the serialized seed + expansion docs and answers from them, exactly as
   before.
3. The host renders the identical inline interactive graph view — from a visualization standpoint
   the user cannot tell which of the three graph tools produced it.

### Story: Terminal user gets a file instead
1. In the Claude Code terminal (no MCP App UIs), the agent calls
   `query_memory(query="MLOps", visualize=True)`.
2. The result text carries the serialized results, the server-side
   `.tree/graphs/mlops-<stamp>.html` path, and a `graphs://` resource link.
3. The path exists locally (stdio server), the user opens it, the graph draws. No browser is
   force-opened on a remote server — the open is best-effort and swallowed on failure.

### Story: Remote (Prefect Horizon) user downloads via the resource link
1. Against a REMOTE MCP server, the same calls return the path (unreachable) plus the `graphs://`
   link.
2. The client reads the resource, saves the text as a local `.html`, opens it — the graph draws.
3. The model never hand-authors HTML at any point.

### Story: Plain query stays plain
1. The agent calls `query_memory(query="How many documents were ingested?")` and
   `search_memory(query="MLOps")` (default `visualize=False`).
2. Each response is the serialized JSON string, no iframe payload, no file written under
   `.tree/graphs/`, no graph iframe rendered in the host.

## Out of scope

- Any change to the iframe UI/JS itself (`_GRAPH_HTML`), the `graphs://` resource, or
  `dashboard_app.py`.
- New graph features (click-to-expand, server round-trips) — the view stays read-only.

## Log
