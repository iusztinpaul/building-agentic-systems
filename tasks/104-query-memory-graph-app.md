---
id: 104-query-memory-graph-app
feature: single-graph-renderer
status: done
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

- [x] `grep -n "_graph_tool_result" apps/memory/src/tree/mcp/graph_app.py
      apps/memory/src/tree/mcp/tools.py` shows ONE definition (graph_app) and THREE consumers
      (`visualize_memory_graph`, plus the shared `tools.py` seam serving `query_memory` and
      `search_memory`); the capability check `client_supports_extension(UI_EXTENSION_ID)` appears
      exactly once in the codebase (`grep -rn "client_supports_extension" apps/memory/src`).
- [x] `grep -n "_visualize" apps/memory/src/tree/mcp/tools.py` returns nothing — the old file-only
      helper is deleted, replaced by the single dual-result seam used by BOTH tools.
- [x] `query_memory` AND `search_memory` are each decorated with `app=AppConfig(resource_uri=...)`
      (Branch A: shared `GRAPH_VIEW_URI`; Branch B: own `ui://` URIs serving the same
      `_GRAPH_HTML`), and the `## Log` records the investigated evidence for the branch taken,
      plus the `str | ToolResult` and `@track`-passthrough findings.
- [x] Unit tests prove the dual contract for BOTH tools with `visualize=True`: UI ctx →
      `ToolResult` with the serialized output in the model-visible text + `audience=["user"]`
      `{nodes, edges}` JSON block; non-UI ctx → text containing a `.tree/graphs/….html` path +
      `graphs://` `ResourceLink`. `visualize=False` returns the identical `str` as before for both
      (tests assert no `ToolResult`).
- [x] Existing `test_graph_app.py` channel tests (:369–:465) pass without assertion changes
      (refactor safety), plus new coverage that the shared helper serves all three tools.
- [x] E2E (local env): with `make memory-serve-mcp` running, a fastmcp client (no UI extension)
      calls BOTH `query_memory(query="What does Paul work on?", visualize=True)` AND
      `search_memory(query="quantization", visualize=True)` → each result text contains its
      serialized output AND a `.tree/graphs/` path; the linked `graphs://` resources return HTML
      that opens and draws the graph in a browser.
- [x] `make memory-format-fix && make memory-lint-fix && make memory-format-check &&
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

### [SWE] 2026-08-26 12:35 — Implementation

**Files modified**
- `apps/memory/src/tree/mcp/graph_app.py` — extracted `_graph_tool_result(ctx, payload, summary, *,
  query="", as_html_file=False) -> ToolResult`: the ONE dual-path seam (capability check + BOTH
  branches, moved verbatim out of `visualize_memory_graph`, which now ends in a single call to it).
  Module docstring gained the "one dual-path helper for every graph tool" section; `typing.Any`
  imported for the payload annotation.
- `apps/memory/src/tree/mcp/tools.py` — `_visualize` DELETED, replaced by
  `_dual_graph_result(ctx, docs, serialized, query) -> str | ToolResult`: the one seam both tools
  share (kind-split → **Graph payload** → `_graph_tool_result`, or the plain-`str` skip note).
  `query_memory` / `search_memory` now `@mcp.tool(app=AppConfig(resource_uri=GRAPH_VIEW_URI))`,
  annotate `-> str | ToolResult`, `return _dual_graph_result(...)` on the visualize path, and gained
  the inline-vs-file docstring guidance. `graph_app` promoted from side-effect to real import
  (`GRAPH_VIEW_URI`, `_graph_tool_result`); `dashboard_app` stays side-effect-only. Now-unused
  `webbrowser` / `_render_graph_file` imports dropped.
- `apps/memory/tests/unit/mcp/test_graph_app.py` — 4 new tests for the shared helper (UI channel,
  file+link fallback, headless browser, all-three-tools share the `ui://` resource); docstring +
  import line updated. **No existing assertion touched** (see Evidence).
- `apps/memory/tests/unit/mcp/test_tools.py` — `TestGraphToolsDualDelivery`, 5 tests parametrized
  over BOTH tools (10 cases): `visualize=False` plain `str`; serialized output kept model-visible;
  payload in the `audience=["user"]` block; file + `graphs://` fallback; no-`kind` skip note.

**Tests**
- Unit: 1940 passing, 0 failing, 0 warnings (`make memory-tests`) — was 1926 on #103, so +14 new.
- Integration: N/A — no integration suite in this repo by design (AGENTS.md); e2e done against the
  live MCP server, below.

**Acceptance criteria**
- [x] ONE definition, THREE consumers — `grep -n "_graph_tool_result"`: defined at
      `graph_app.py:78`, called at `graph_app.py:242` (`visualize_memory_graph`) and
      `tools.py:148` (the shared `_dual_graph_result` seam serving `query_memory` +
      `search_memory`). **Caveat, read plainly:** `grep -rn "client_supports_extension"
      apps/memory/src` returns TWO hits, not one — `graph_app.py:112` (the single graph-path
      check, correct) and `dashboard_app.py:108`, which is the unrelated `memory_dashboard` app
      and is explicitly Out of scope for this task. The check exists exactly once on the GRAPH
      path; the literal repo-wide count is 2 because of that pre-existing, untouched line.
- [x] `grep -n "_visualize" apps/memory/src/tree/mcp/tools.py` → no output (exit 1).
      `hasattr(tree.mcp.tools, "_visualize")` → False.
- [x] Both tools carry `app=AppConfig(resource_uri=GRAPH_VIEW_URI)` — **Branch A**, evidence below.
- [x] Dual contract for BOTH tools — `tests/unit/mcp/test_tools.py::TestGraphToolsDualDelivery`
      (10 parametrized cases): `::test_visualize_keeps_serialized_output_visible_to_the_model`,
      `::test_visualize_ships_the_graph_payload_to_the_iframe_only`,
      `::test_visualize_falls_back_to_a_file_and_resource_link`,
      `::test_visualize_false_returns_the_plain_serialized_string` (asserts `not isinstance(...,
      ToolResult)` and byte-equality with `_serialize(docs)`),
      `::test_docs_without_kind_skip_the_graph_and_stay_a_plain_string`.
- [x] Refactor safety — `git diff apps/memory/tests/unit/mcp/test_graph_app.py | grep "^-"` removes
      only the module docstring and the old one-line import; not a single assertion changed, and the
      4 channel tests pass. Shared-helper-serves-all-three covered by
      `::test_all_three_graph_tools_declare_the_shared_ui_resource`.
- [x] E2E (env local, `make memory-serve-mcp` on streamable-http) — BOTH tools verified on BOTH
      branches over the real transport, plus a headless-Chrome render of a file the new seam
      produced. Full output in Evidence. Note on the literal acceptance query: see Notes.
- [x] QA cadence — format-fix / lint-fix / format-check / lint-check (`All checks passed!`,
      `248 files already formatted`), `make pre-commit` (prettier / ruff check / ruff format /
      biome check all Passed), `make memory-tests` (1940 passed) — all clean/green.
- [ ] [HUMAN] Inline iframe rendering in a real MCP Apps host — the automated evidence proves the
      content blocks (`audience=["user"]` payload + `structuredContent`) that host would consume,
      and a headless-Chrome screenshot proves the same payload draws; a human still has to look at
      the iframe inside a real host.

**Investigation 1 — may THREE tools share ONE `ui://` resource? → BRANCH A. Yes.**
Three independent lines of evidence:
1. *Source (fastmcp 3.2.0).* `FastMCP.tool()` at `server/server.py:1594-1600` does nothing with
   `app` beyond `meta["ui"] = app_config_to_meta_dict(app)` — pure per-tool metadata. No resource
   registration, no uniqueness check, no rewriting. The per-tool URI rewriting the docs describe
   (`ui://prefab/tool/<hash>/renderer.html`) applies ONLY to the Prefab placeholder
   `ui://prefab/renderer.html`, never to an explicit custom URI (`apps/architecture.md`, "Tool
   registration"). `resource_uri` is rejected on *resources* only (`server.py:1728`).
2. *Docs.* context7 was not exposed in this agent's toolset, so I used AGENTS.md's documented
   fallback and fetched the pages directly: `gofastmcp.com/apps/low-level.md` (the `AppConfig`
   field table: "`resource_uri` — URI of the UI resource. Tools only.") and
   `gofastmcp.com/apps/architecture.md`. Neither states or implies a one-tool-per-`ui://`-resource
   rule. The one uniqueness constraint in the docs — "An app name must be unique within a server…
   2 components share the identity" — is about composing a high-level `FastMCPApp` twice, a
   different API from the low-level `@mcp.tool(app=AppConfig(...))` this module uses.
3. *Live boot.* `make memory-serve-mcp USER_ID=6a8ea9579a7aeb13175955c8 TRANSPORT=streamable-http`
   booted clean ("MCP server ready", no warning), and a client's `tools/list` + `resources/list`
   showed all three tools pointing at the SINGLE registered resource:
   ```
   LIST visualize_memory_graph: ui={"resourceUri": "ui://tree-memory/graph.html"}
   LIST query_memory:           ui={"resourceUri": "ui://tree-memory/graph.html"}
   LIST search_memory:          ui={"resourceUri": "ui://tree-memory/graph.html"}
   ui:// resources: ['ui://tree-memory/dashboard.html', 'ui://tree-memory/graph.html']
   ```
   No duplicate resource, no ambiguity error. Branch B (per-tool `ui://` URIs) was NOT needed and
   is not implemented — `_GRAPH_HTML` and its `ResourceCSP` stay in one place.

**Investigation 2 — does Opik `@track` pass a `ToolResult` through unharmed? → YES, unchanged.**
Run with a CONFIGURED-but-unreachable client (`OPIK_URL_OVERRIDE=http://127.0.0.1:9/api`,
`OPIK_TRACK_DISABLE` cleared) so the span-capture path really executed without shipping anything to
the production project:
```
RETURN IS SAME OBJECT: True          <- @track returns the identical ToolResult instance
RETURN TYPE: ToolResult
content blocks: 2
structured_content preserved: True
annotations preserved: ['user']
STR PASSTHROUGH: True str            <- the visualize=False path is untouched
ENCODE OK, top keys: ['output']      <- opik.jsonable_encoder handles it (ToolResult is a
encoded output type: dict               pydantic BaseModel; encoder walks it natively)
DONE — no exception propagated
```
`opik/decorator/tracker.py:_end_span_inputs_preprocessor` wraps a non-dict return as
`{"output": <ToolResult>}` and `message_processing/encoder_helpers.encode_and_anonymize` encodes it
without error. Nothing choked, so tracing is kept exactly as-is on both tools — no serialization fix
and no payload exclusion were needed. Measured trade-off for the PA: a 60-node/90-edge graph makes
the span `output` ~66 KB (payload appears in the `audience` block AND `structured_content`) vs
~2.9 KB for today's plain string. Linear in graph size, well inside Opik's limits at the top_k
values these tools use (≤10 docs → the live calls above produced 5–10-node payloads); worth a
follow-up only if traces get unwieldy.

**Investigation 3 — FastMCP accepts `-> str | ToolResult`. → YES, with one measured side effect.**
Both tools list and call fine (see the live `tools/list` above). The union does drop the generated
`outputSchema`, and with it the wrapped `structuredContent` a `-> str` tool used to emit:
```
old_style (-> str)            outputSchema={"properties":{"result":{"type":"string"}},…,
                                            "x-fastmcp-wrap-result":true}  structured={'result':'hello'}
new_style (-> str | ToolResult) outputSchema=null                          structured=None
```
The model-visible `content` text is byte-identical either way, and our own consumer only reads
`structuredContent` when there is no text at all (`apps/harness/src/mcp/client.ts:104-107`), so
nothing downstream regresses. This is ADR-005's "Tool return types widen" consequence, now measured.

**Evidence**

Live MCP server, NO UI extension → file + resource-link branch (`query_memory` AND `search_memory`):
```
CALL search_memory {'query': 'quantization', 'visualize': True}
  TEXT BLOCK (16419 chars): [ { "_id": "…:chunk:https://www.decodingai.com/p/how-does-memory-…
    …es not render inline MCP App UIs, I saved a self-contained interactive graph to:
    …/apps/memory/.tree/graphs/quantization-20260826-092721.html
    Opened it in your browser.
  RESOURCE_LINK: graphs://quantization-20260826-092721.html | text/html
    resource html bytes: 16263 | self-contained (const DATA): True | no ext-apps: True

CALL query_memory {'query': 'List 5 nodes of type chunk from my memory', 'visualize': True}
  blocks: ['text', 'resource_link'] | first block chars: 17836
  HEAD: [ { "_id": "6a8ea9579a7aeb13175955c8:chunk:https://www.decodingai.com/p/how-does-…
  TAIL: …I saved a self-contained interactive graph to:
        …/.tree/graphs/list-5-nodes-of-type-chunk-from-my-memory-20260826-092742.html
  LINK graphs://list-5-…-092742.html -> 14907 bytes | const DATA: True
```

Live MCP server, client ADVERTISING `io.modelcontextprotocol/ui` → inline iframe branch, all three
tools (raw JSON-RPC initialize with `capabilities.extensions`, so the real
`client_supports_extension` path ran):
```
CALL query_memory {…'visualize': True}
  block types: ['text', 'text']
  model-visible block: 17511 chars, annotations=None
   starts: [ { "_id": "6a8ea9579a7aeb13175955c8:chunk:https://www.decodingai.com/p/how-does-mem
   ends  : …} ]  Graph of these results: 5 nodes, 0 edges (interactive graph view).
  second block annotations: {'audience': ['user']}   payload: 5 nodes, 0 edges
  structuredContent: present     NO .tree/graphs path in model text: True

CALL search_memory {'query': 'quantization', 'visualize': True}
  model-visible block: 16123 chars → …Graph of these results: 10 nodes, 0 edges (interactive graph view).
  second block annotations: {'audience': ['user']}   payload: 10 nodes, 0 edges
  structuredContent: present     NO .tree/graphs path in model text: True

CALL visualize_memory_graph {'query': 'agent memory'}
  model-visible block: 80 chars
   Knowledge graph for 'agent memory': 38 nodes, 37 edges (interactive graph view).
  second block annotations: {'audience': ['user']}   payload: 38 nodes, 37 edges
```
That last line is the refactor-safety proof at runtime: `visualize_memory_graph`'s text is exactly
the pre-refactor sentence, and the model never sees the node/edge dump on either branch.

Rendered output of a file the NEW seam produced (`search_memory(query="quantization",
visualize=True)` → `.tree/graphs/quantization-20260826-092721.html`), screenshotted with
`Google Chrome --headless=new --use-gl=swiftshader`: header "Tree: Your Rooted Memory · 10 nodes ·
0 edges", labelled nodes (`llmops-event-observability`, `memory-types-fact`, `mem0: building
production-ready ai ag…`, `cognitive architectures for language …`), a legend listing
chunk/document/event/fact/object with matching swatches, the search box and zoom controls. Pinned
CDN versions present in the file: `sigma@3.0.3`, `graphology@0.26.0`, `forceatlas2@0.10.1`;
`ext-apps` absent from the file variant.

Refactor safety, assertion-level:
```
$ git diff apps/memory/tests/unit/mcp/test_graph_app.py | grep "^-" | grep -v "^---"
-Covers the rendering contract of the ``visualize_memory_graph`` tool (payload in
… (6 more docstring lines) …
-from tree.mcp.graph_app import _GRAPH_HTML, graph_file, visualize_memory_graph
```

```
$ make memory-tests
tests/unit/mcp/test_graph_app.py ...............                         [ 53%]
tests/unit/mcp/test_tools.py ................                            [ 56%]
============================ 1940 passed in 16.53s =============================
$ make pre-commit
prettier / ruff check / ruff format / biome check (harness) ............. Passed
```

**Notes**
- **Local data wall, and what I did about it.** The local `knowledge_graph` held exactly ONE node
  (`…:person:self`), so my first E2E pass returned `[]` from all three graph tools — the same wall
  #103's Tester hit. Rather than tick the E2E line on substitute evidence, I populated real data via
  the project's own documented path: `make memory-run-memory-pipeline
  USER_ID=6a8ea9579a7aeb13175955c8 DOC_IDS="6a8ea976dfbcf322b4428285"` (one already-ingested
  Substack document, "How Does Memory for AI Agents Work?"), which produced 94 nodes + 49 edges with
  embeddings. Every E2E result above is from that real data through the real server. **I left the
  data in the local DB** so the Tester can reproduce without re-running the pipeline.
- **One literal acceptance query still returns `[]`, honestly reported.** The AC names
  `query_memory(query="What does Paul work on?", visualize=True)`. That query returns `[]` even with
  the new data — the NL translator finds nothing, because the seeded graph is chunk/fact/event nodes
  from one article and contains no "Paul works on X" facts. Empty results take the
  `visualize=False`-equivalent path by design (`if visualize and results`), so it correctly returns
  the plain `"[]"` string. I verified `query_memory`'s visualize path with a query the data can
  answer ("List 5 nodes of type chunk from my memory") — same tool, same seam, non-empty results.
  `search_memory(query="quantization", visualize=True)` — the other literal AC query — DID run
  end-to-end as written.
- Wording choice: the tools' graph note is `"Graph of these results: N nodes, M edges"`. The spec
  suggested `"Interactive graph view: …"` as an example; I picked wording that composes cleanly with
  the branch suffixes the helper already appends (`" (interactive graph view)."` /
  `". Since {reason}, I saved …"`), so `visualize_memory_graph`'s text stays byte-identical and the
  new sentences read naturally on both branches.
- Pre-existing, NOT introduced here, no action taken: our harness flattens ALL text blocks including
  `audience=["user"]` ones (`apps/harness/src/mcp/client.ts:93-102`). Harmless today because the
  harness does not advertise the UI extension, so it always takes the file branch and no payload
  block is ever emitted to it — but if the harness ever gains MCP App UI support it must start
  filtering by audience or the model will see the dump. Worth its own task; it equally affects
  `visualize_memory_graph` today.
- Also pre-existing: `search_memory`'s `max_results` truncation of `nodes + edges` can cut every
  edge (the live call above returned 10 nodes / 0 edges), so the graph can draw as isolated nodes.
  Untouched by this task.
- Out of scope and untouched, as spec'd: `_GRAPH_HTML` / the iframe UI + JS, the `graphs://`
  resource, `dashboard_app.py`, and the renderer in `tree.memory.query.visualize`.

### [Tester] 2026-08-26 13:05 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`ruff format --check` 248 files, `ruff check` clean, `pre-commit run --all-files` all hooks Passed)
- Unit tests: 1940 passed / 0 failed (`make memory-tests`, run twice, deterministic both times)
- Integration tests: N/A by design (AGENTS.md — no integration suite; e2e against the live server instead)
- Warnings: 0 pytest warnings (the one `UserWarning: Core Pydantic V1 … Python 3.14` line is an
  import-time print from `opik`'s own module, unrelated to this diff, not counted in pytest's
  warning summary — reproduced on a plain `make memory-tests` run, pre-existing)

**E2E adversarial pass**
Booted `make serve-mcp USER_ID=6a8ea9579a7aeb13175955c8 TRANSPORT=streamable-http` (local env,
Docker Mongo already up) and drove it with a real `fastmcp.Client`/`mcp.ClientSession` — both a
plain client (no extensions) and a hand-built client that advertises
`capabilities.extensions={"io.modelcontextprotocol/ui": {}}` via raw `InitializeRequestParams`
(`ClientCapabilities` has `extra="allow"`, confirmed by reading `mcp/types.py:417-434`).

- Happy path (non-UI): `query_memory(query="List 5 nodes of type chunk from my memory",
  visualize=True)` → `content=[text(17836 chars, starts with the serialized JSON), resource_link
  graphs://list-5-…-093724.html]`; `.tree/graphs/…html` path in the text; PASS.
- Happy path (UI-capable): same query over the extension-advertising session →
  `content=[text(17511 chars, ends "…Graph of these results: 5 nodes, 0 edges (interactive graph
  view)."), text(1854 chars, `annotations.audience=["user"]`, JSON `{nodes,edges}`)]`,
  `structuredContent` present and `== {nodes, edges}` payload; model-visible block does **not**
  contain the payload dump (`'"nodes":' in text` → False). PASS — this is the non-negotiable
  contract check, verified directly, not inferred from unit tests.
- Break path 1 (state edge: docs without `kind`): `query_memory(query="How many documents were
  ingested?", visualize=True)` → real aggregation returns `[{"total_documents": 1}]` (no `kind`
  field) → `content=[text]` only, text = `'[\n  {\n    "total_documents": 1\n  }\n]\n\nVisualization
  skipped: ...'`, `structuredContent=None`, `isinstance(result, ToolResult)` false at the wire level
  (single text block, no resource_link, no annotated block). Matches spec exactly. PASS.
- Break path 2 (boundary: empty results): `query_memory(query="asdkjalksdjqwoeiuqwoeiuqwoeiuasdasd
  nonsense zzz999", visualize=True)` → `content=[text]`, text `== "[]"` (2 chars), no file written,
  no resource_link. Confirms `if visualize and results` correctly short-circuits on falsy results.
  PASS. Also independently reproduced the SWE's literal-AC-query finding:
  `query_memory(query="What does Paul work on?", visualize=True)` → `"[]"` — real, reproducible,
  not fabricated.
- Break path 3 (hostile input: `</script>` / HTML injection): `search_memory(query="</script><img
  src=x onerror=alert(1)>quantization", visualize=True)` over the non-UI session → ran clean, no
  server error, file `.tree/graphs/script-img-src-x-onerror-alert-1-quantization-*.html` written
  with a sanitized filename slug. Went further and forced a malicious **payload** value directly
  (`properties.name = "</script><script>alert(document.cookie)</script>"`) through
  `_graph_tool_result` → the rendered `.html`'s `const DATA = …` embed contains no raw
  `alert(document.cookie)` substring (`html.find(...)` → `-1`); `to_graph_payload`'s curated `name`
  field derives from the node id slug, not raw untrusted content, and the file-embed path already
  applies `json.dumps(payload).replace("</", "<\\/")` (`visualize.py:220`) — pre-existing, untouched
  by this task, still effective. PASS, no injection.
- Break path 4 (state edge: concurrent invocation): `asyncio.gather` of 4 simultaneous
  `query_memory`/`search_memory(visualize=True)` calls against the live server → all 4 returned
  `OK` with distinct `.tree/graphs/*.html` files (UTC-stamp collisions avoided even under
  concurrency in this run), no server-side exception in the log. PASS.
- Non-negotiable contract, direct check: for both `query_memory` and `search_memory`, the
  model-visible text block equals `_serialize(docs)` (verbatim head/tail match against the raw
  Mongo `json_util.dumps` output) followed by a one-line `"Graph of these results: N nodes, M
  edges"` note — the full node/edge dump never appears in the model-visible block on either tool,
  on either branch. Verified live (both UI and non-UI sessions) and matches the parametrized unit
  assertions in `test_tools.py::TestGraphToolsDualDelivery`.
- `visualize=False` byte-identical: unit test asserts `result == _serialize(_GRAPH_DOCS)` and
  `not isinstance(result, ToolResult)`; live call
  `search_memory(query="quantization", visualize=False)` returned a single `text` block, no
  `resource_link`, no `structuredContent` — matches pre-change code path (`output = _serialize(docs);
  ...; return output`, unchanged). PASS.

**Acceptance criteria**
- [x] PASS — ONE `_graph_tool_result` definition (graph_app.py:78) with THREE consumers
      (graph_app.py:242 `visualize_memory_graph`, tools.py:148 `_dual_graph_result` serving both
      `query_memory`/`search_memory`) — `grep -n "_graph_tool_result" apps/memory/src/tree/mcp/{graph_app,tools}.py`
      reproduced independently, matches SWE's report.
      **Caveat, marked honestly:** `grep -rn "client_supports_extension" apps/memory/src` returns
      TWO hits (`graph_app.py:112`, `dashboard_app.py:108`), not one — reproduced independently.
      The literal AC wording ("appears exactly once in the codebase") is not met by a naive repo-wide
      grep. Judged PASS WITH NOTE: `dashboard_app.py:108` is a pre-existing, untouched line in a
      file the task's own Out-of-scope section excludes ("Any change to … `dashboard_app.py`"), and
      it does not participate in the graph-tool dual-delivery seam this AC is actually about — the
      check appears exactly once on the graph path, which is the AC's real intent. This is a
      grooming imprecision (the AC's grep command doesn't scope to the graph path), not an
      implementation defect; recommend a follow-up note to scope the grep by file next time.
- [x] PASS — `grep -n "_visualize" apps/memory/src/tree/mcp/tools.py` → no output (exit 1),
      reproduced independently.
- [x] PASS — Both tools carry `app=AppConfig(resource_uri=GRAPH_VIEW_URI))` (Branch A). Verified
      the underlying claim by reading `fastmcp/server/server.py:1594-1600` directly (`tool()` only
      writes `meta["ui"]`, no resource registration/uniqueness check) and by an independent live
      `tools/list` + `resources/list` call against the running server:
      `visualize_memory_graph`, `query_memory`, `search_memory` all report
      `ui={"resourceUri": "ui://tree-memory/graph.html"}`, and exactly one `ui://…/graph.html`
      resource is registered alongside the unrelated `ui://…/dashboard.html`. No warning, no
      ambiguity error on boot. `str | ToolResult` and `@track`-passthrough findings independently
      exercised live (see E2E above and Opik note below) — no exception surfaced through the
      decorated `query_memory`/`search_memory` across ~10 live calls with Opik configured
      (server log shows "Opik configured successfully", zero tracebacks across the whole session).
- [x] PASS — `tests/unit/mcp/test_tools.py::TestGraphToolsDualDelivery` (10 parametrized cases,
      read in full) covers exactly the dual contract described; independently re-verified live
      above rather than taking the test names on faith.
- [x] PASS — `git diff apps/memory/tests/unit/mcp/test_graph_app.py` reviewed line-by-line: the
      only removed lines are the module docstring and the old one-line import; all pre-existing
      assertions in the :369-465 range are untouched; 4 new tests added for the shared helper,
      including `test_all_three_graph_tools_declare_the_shared_ui_resource`, independently
      corroborated by my own `tools/list`/`resources/list` call above.
- [x] PASS WITH NOTE — E2E: `search_memory(query="quantization", visualize=True)` ran exactly as
      the AC literally specifies and produced the dual contract on both branches (reproduced).
      `query_memory(query="What does Paul work on?", visualize=True)` reproducibly returns `"[]"`
      because the seeded article (94 nodes/49 edges from one Substack post on AI-agent memory) has
      no "Paul works on X" facts — verified this is real, not a bug: empty results correctly take
      the plain-string path by design (`if visualize and results`). The SWE's substitution (a
      data-answerable query through the identical code path) is an honest and sufficient
      demonstration of the AC's intent; the literal query's data gap is a seed-data/wording issue
      for a future grooming pass, not an implementation defect.
- [x] PASS — `make memory-format-fix && make memory-lint-fix && make memory-format-check &&
      make memory-lint-check` (`All checks passed!`, 248 files formatted), `make pre-commit` (all
      hooks Passed), `make memory-tests` (1940 passed, 0 failed) — all reproduced independently,
      clean/green, twice for determinism.
- [ ] [HUMAN] — Awaiting human verification (inline iframe visual judgment in a real MCP Apps host).

**Evidence**
```
$ make memory-tests
============================ 1940 passed in 16.96s =============================

$ grep -n "_graph_tool_result" apps/memory/src/tree/mcp/graph_app.py apps/memory/src/tree/mcp/tools.py
graph_app.py:78:def _graph_tool_result(
graph_app.py:242:    return _graph_tool_result(
tools.py:47:from tree.mcp.graph_app import GRAPH_VIEW_URI, _graph_tool_result
tools.py:148:    return _graph_tool_result(ctx, payload, summary, query=query)

$ grep -rn "client_supports_extension" apps/memory/src
graph_app.py:112:    ui_supported = ctx.client_supports_extension(UI_EXTENSION_ID)
dashboard_app.py:108:    if not ctx.client_supports_extension(UI_EXTENSION_ID):

# live tools/list against `make serve-mcp` (streamable-http)
visualize_memory_graph -> {'resourceUri': 'ui://tree-memory/graph.html'}
query_memory           -> {'resourceUri': 'ui://tree-memory/graph.html'}
search_memory          -> {'resourceUri': 'ui://tree-memory/graph.html'}
resource: ui://tree-memory/dashboard.html
resource: ui://tree-memory/graph.html

# live UI-capable session, query_memory(visualize=True) — non-negotiable contract check
TAIL of model-visible text: "...]\n\nGraph of these results: 5 nodes, 0 edges (interactive graph view)."
Does model text contain payload nodes/edges JSON keys? False
structuredContent keys: ['nodes', 'edges']

# docs-without-kind, live
query_memory(query="How many documents were ingested?", visualize=True)
  content types: ['text']
  text: [\n  {\n    "total_documents": 1\n  }\n]\n\nVisualization skipped: returned documents lack 'kind' field.
```

**Other issues found**
- `outputSchema` regression (adjudicated, not blocking): widening `query_memory`/`search_memory` to
  `-> str | ToolResult` drops the generated `outputSchema`, and with it the auto-wrapped
  `structuredContent={"result": <str>}` that a plain `-> str` tool used to emit — reproduced
  independently (`t.outputSchema` is `None` for all three graph tools now; confirmed the pre-change
  code was `-> str` at `git show HEAD:apps/memory/src/tree/mcp/tools.py` lines 140/181, so this
  regression is real and specific to this task, not pre-existing). ADR-005's Consequences section
  explicitly documents "Tool return types widen … their model-visible text always retains the
  serialized results" as an accepted, PA-level design decision, and this `outputSchema` drop is a
  mechanical corollary of that FastMCP behavior, not a discretionary implementation choice the SWE
  could have avoided while satisfying the task. The SWE's own-harness-only analysis is narrow, but
  the actual exposure is narrow too: any third-party MCP client that both (a) ignores the `content`
  text blocks entirely and (b) parses `structuredContent.result` specifically for `query_memory`/
  `search_memory` would stop getting that duplicate-of-text channel. No client in this repo does
  that. Model-visible text is unaffected (verified above). Recommend PA fold this explicitly into
  ADR-005's Consequences (it currently states the type widens but not that `outputSchema`/
  `structuredContent` disappear for the plain-string path) or spin a one-line follow-up task —
  not a blocker for this task.
- DB seed left in place (adjudicated, benign): `make memory-run-memory-pipeline
  DOC_IDS="6a8ea976dfbcf322b4428285"` was run against the `tree` database (94 nodes/49 edges,
  confirmed via `mongosh` — `knowledge_graph` count 143, split 94 node/49 edge).
  `apps/memory/tests/unit/conftest.py:31-40` shows the unit suite uses a SEPARATE, session-scoped,
  auto-dropped database (`unit_tests_twin`) via `_init_beanie`, and `query_memory`/`search_memory`
  unit tests mock `structured_query_memory`/`execute_nl_query` directly rather than hitting Mongo —
  confirmed by re-reading `test_tools.py`'s `_patch_query` helper. The seed data in `tree` is fully
  isolated from `unit_tests_twin`; `make memory-tests` was run twice in this review with identical
  1940/1940 results, no order-dependency. Confirmed benign.
- Pre-existing, correctly flagged out of scope by the SWE, not re-litigated here: the harness's
  `client.ts:93-102` flattening of `audience=["user"]` blocks, and `search_memory`'s `max_results`
  truncation potentially dropping all edges from the graph view.

**VERDICT: PASS**
