---
id: 173-memory-visualize-structure
status: done
feature: mode-reset-and-structure-viz
---

# `make memory-visualize-structure` — the mode-aware **Memory structure** view (and the end of `memory-query-graph`)

Tags: `memory`, `visualize`, `cli`, `mcp`, `rag`, `graphrag`, `docs`
Depends on: 172 (`make memory-reset-mode` — this task's local verification switches modes with it)
Blocks: —
Implements: ADR-005 (ONE **Graph renderer**; this task extends its reach to `rag`), ADR-006 §5 (the tool
set IS the mode — amended: one more both-modes tool), ADR-011 §7 (the capped, recency-ranked no-query
read, reused by the rag view). Status-line amendments for ADR-005/006/011 are drafted below, to be
applied in the implementation commit. Branch `feat/mode-reset-and-structure-viz`, worktree
`building-agentic-systems-structure-viz`.

## Scope

**Human decisions (final):**

- The visualization commands become exactly two: `make memory-visualize-structure` (new) and
  `make memory-visualize-embeddings` (existing — behaviour unchanged, not touched).
- `memory-visualize-structure` renders the memory STRUCTURE according to `memory.mode`:
  - `rag` — the document → **Parent chunk** → **Child chunk** tree (rows linked by `parent_id`; no edge
    rows exist in rag).
  - `graphrag` — the knowledge graph exactly as `make memory-query-graph` renders it today (documents,
    chunks, entities, edges).
- Same single renderer (ADR-005: `apps/memory/src/tree/memory/visualize/graph.py`, with ADR-011's live
  d3-force layout, controls panel, and the `Documents` slider — most-recent default 100 of a 500 cap on
  the no-query view, relevance-ranked on a `QUERY` view).
- Optional `QUERY=` narrows the view to the search results in both modes: `rag` = the retrieved parents
  + their documents + their children; `graphrag` = today's query view.

**PA decisions (recommended, apply unless the human objects):**

1. **`make memory-query-graph` is removed, not aliased.** Its HTML role moves to
   `make memory-visualize-structure`; its text role (ranked parent chunks, which it printed in `rag` with
   a `QUERY`) becomes `make memory-search QUERY="…"` (`scripts/search_memory.py`), MODE-NEUTRAL: it runs
   **Parent-document retrieval** (`retrieve_parents`) and prints the same blocks in both modes
   (children are identical in both, ADR-006 §3). Why a separate text command rather than folding it into
   the viz command: a text answer and a picture are different jobs — ADR-007 §8 made the same split for
   the map ("a separate command rather than a flag on `query_graph.py`") — the e2e skill and operators
   keep a browser-free read-back, and `memory-search` matches the MCP tool it mirrors (`search_memory`).
   Why no alias: the repo keeps no compat shims (ADR-006 §1), and a target named `query-graph` that draws
   no graph in `rag` is the surprise this task removes. `tests/unit/scripts/test_query_graph.py` splits
   into `test_visualize_structure.py` (graph half, + rag cases) and `test_search_memory.py` (text half).
2. **MCP follows the same rule with the established two-functions-one-name pattern.**
   `visualize_memory_graph` is renamed `visualize_memory_structure` and registered in BOTH modes:
   the graphrag function stays in `graph_tools.py` with today's signature `(ctx, query="", top_k=15,
   max_hops=2, as_html_file=False, max_docs=None)`; a rag function lives in `tools.py`, registered
   `if MEMORY_MODE == "rag"`, with signature `(ctx, query="", top_k=10, as_html_file=False,
   max_docs=None)` — no `max_hops`, exactly as `search_memory` already splits (ADR-006 §5: no parameter
   the mode cannot honour). Tool counts become 8 (`rag`) / 14 (`graphrag`); the graph-only set is six.
   `tools.py` keeps importing no graph code (`test_tool_gating.py`'s `_GRAPH_MODULES` guard).
   Why rename: the codebase already refuses to call a map a "graph" (`_payload_noun`); a rag tree is not
   a knowledge graph either, and one name per surface in both modes is what the CLI now has.
3. **rag `search_memory` keeps its `(query, top_k)` signature — no `visualize` parameter.** The rag
   picture of a query is `visualize_memory_structure(query=…)`; adding `visualize` to rag
   `search_memory` would be a second way to draw the same thing and would re-open the ADR-006 §5 split
   for no new capability. graphrag's `search_memory(visualize=True)` / `query_memory(visualize=True)`
   are untouched (they ship the rows they already hold).
4. **Edges for the rag tree are SYNTHESISED from `parent_id` at read time and never persisted.** The
   **Graph payload** is `{nodes, edges}` and the renderer's rigid-children drag reads `part_of` edges
   (ADR-011 §4), so the rag reader emits `part_of` edge dicts (child → parent, parent → document — the
   same direction and the same `build_edge_id` ids a graphrag run would write) and the template changes
   by ZERO bytes. The `memory` collection stays edge-free in rag (ADR-006 §3 holds; the "rag never
   writes anything else" test is untouched).
5. **The rag reader lives in `tree/memory/rag/structure.py`** (Mongo reads of rag rows are rag's;
   `visualize/` renders what it is handed and reads no Mongo — layout guard 3). Its return type is a new
   `MemoryStructure(BaseModel)` in `rag/types.py` (`nodes: list[dict]`, `edges: list[dict]`), and
   `tree.memory.types.QueryResult` becomes `class QueryResult(MemoryStructure)` (same name, same fields,
   every caller untouched) — `rag/` may not import `tree.memory.types`, which pulls
   `graph.resolution.types`. The recency helpers `fetch_full_graph` uses (`_as_utc`,
   `_document_recency`, `_UNDATED`, `_rank_by_provenance`, `_NO_EMBEDDING`) MOVE to `rag/structure.py`
   as public names and `graph/retrieval.py` imports them (graph → rag is the allowed direction) — one
   ranking, two readers.

### What the user sees

| | `rag` | `graphrag` |
|---|---|---|
| `make memory-visualize-structure` | `.tree/graphs/structure-<stamp>.html`: document stars (size 10) → parent chunks (7) → child chunks (4), `part_of` edges, `Documents` slider `Most recent` showing `min(100, total)`; header `D of D documents · N nodes · M edges` | unchanged from today's `make memory-query-graph` (file slug `graph-…` stays) |
| `… QUERY="…"` | the retrieved parents (`top_k`, default `query.top_k`), their documents and ALL children of those parents; slider `Most relevant`, every document shown by default (ranked by best parent score, ties on `_id`) | unchanged from today's `make memory-query-graph QUERY=` |
| no rows at all | `Memory is empty for this user — run make memory-run-pipeline first.` exit 1, no file | same message, exit 1 (today's "No data found" log becomes this line) |
| `QUERY` with `nothing_found` | `No results for "<query>" — nothing to draw.` exit 1, no file | today's behaviour (empty graph → exit 1) with the same line |
| both search legs down | `Search unavailable: <message> — retryable`, exit 1 (today's line) | same |
| `make memory-search QUERY="…"` | ranked parent blocks (score, title — heading path, 300-char excerpt, matched-children count), degraded caveat first, `No results.` on empty | identical |
| MCP `visualize_memory_structure` | inline MCP App iframe or `.tree/graphs/` file + `graphs://` link (the ONE dual path, ADR-005 §4); summary `Memory structure (rag: document → parent chunk → child chunk) for your full memory: 3 of 3 most-recent documents shown by default, 121 nodes, 118 edges` | today's `Knowledge graph for …: …` summary |

### Implementation (by symbol)

1. **`tree/memory/rag/types.py`** — `class MemoryStructure(BaseModel)`: `nodes: list[dict[str, Any]]`,
   `edges: list[dict[str, Any]]` (both `Field(default_factory=list)`); docstring: the row bag every
   structural view hands the **Graph renderer**; in `rag` the edges are synthesised, never stored.
   **`tree/memory/types.py`** — `class QueryResult(MemoryStructure)` keeps its docstring (+ "the graphrag
   query view; a `MemoryStructure` with provenance").
2. **`tree/memory/rag/structure.py`** (new; mirror test required by `test_package_layout.py` guard 5):
   - moved helpers, public: `as_utc`, `document_recency`, `UNDATED`, `rank_by_provenance`,
     `NO_EMBEDDING` — behaviour unchanged; `graph/retrieval.py` imports them and keeps `fetch_full_graph`
     as is (SWE may also have `fetch_full_graph` call `rank_recent_documents` below for its step 1 — same
     reads, fewer copies; not required).
   - `rank_recent_documents(rows, max_docs) -> list[dict]` (pure): the `fetch_full_graph` step-1 sort —
     newest first by `properties.date` else `created_at`, ties on `str(_id)`, cut to `max_docs`.
   - `synthesize_part_of_edges(nodes, user_id) -> list[dict]` (pure): for every node row with a
     `parent_id` whose parent is in `nodes`, one dict `{"_id": build_edge_id(child, EdgeType.PART_OF,
     parent), "kind": "edge", "type": "part_of", "source_node_id": child, "target_node_id": parent,
     "user_id": user_id, "sources": row.get("sources", []), "doc_rank": row.get("doc_rank")}`; a
     `parent_id` pointing outside `nodes` yields no edge (no dangling endpoints — `to_graph_payload`
     would otherwise draw an `unknown` node); deterministic order (input order).
   - `async fetch_rag_structure(client, database, user_id, *, max_docs=None) -> MemoryStructure`: the
     no-query view. Two reads, both `user_id`-scoped, `NO_EMBEDDING` projection: ① every `document`
     row → `rank_recent_documents` (default `app_config.query.full_graph_max_docs`), `doc_rank` stamped
     1-based, provenance map from `sources[0]` exactly as `fetch_full_graph` step 1; ② every node row
     with `sources ∈ provenance` (parents + children), `doc_rank` via `rank_by_provenance`; edges =
     `synthesize_part_of_edges`. Log `Memory structure (rag): embedded %d of %d documents (most recent
     first) → %d nodes, %d edges`.
   - `async fetch_retrieval_structure(client, database, user_id, retrieval: RetrievalResult) -> MemoryStructure`:
     the query view. From `retrieval.parents` (already ranked best first): documents ranked by their
     best parent's position (first occurrence wins; ties impossible — the list is ordered), `doc_rank`
     1-based. Three reads, all `user_id`-scoped, no embedding: `document` rows by `_id ∈
     {p.document.document_id}`, parent rows by `_id ∈ {p.parent_id}`, child rows by `{"kind": "node",
     "type": "chunk", "subtype": "child", "parent_id": {"$in": parent_ids}}` (served by
     `user_kind_type_subtype`'s prefix, filtered on `parent_id` — fine at personal scale; upgrade trigger
     recorded in the docstring: a measured slow child scan → a `(user_id, parent_id)` index, ADR-012's
     rule). Every row stamped with its document's rank (`rank_by_provenance`); edges synthesised. An
     empty `retrieval.parents` → `MemoryStructure()` (the callers turn that into the no-results line).
   Module docstring: why edges are synthesised (decision 4), why the module is rag's (decision 5), and
   that it never imports `graph/`.
3. **`tree/memory/visualize/graph.py`** — `to_graph_payload(result: MemoryStructure, …)` and
   `visualize_query_result(result: MemoryStructure, …)` (annotation only; `QueryResult` still passes).
   `_default_graph_path("")` slug: the no-query file is named from the `query` argument the caller passes
   — the CLI/tool pass `"structure"` in rag and keep `"graph"` in graphrag so files read
   `structure-<stamp>.html` / `graph-<stamp>.html`. No template edit (`_GRAPH_STYLE`, `_BODY_MARKUP`,
   `_RENDER_JS`, `_FILE_HTML_TEMPLATE` byte-identical — assert in the tests).
4. **`scripts/visualize_structure.py`** (new, replaces `scripts/query_graph.py` — `git mv` + rewrite):
   `init_logger()`; `@user_options`, `--query/-q`, `--top-k/-k` (default `app_config.query.top_k`),
   `--max-hops/-h` (graphrag only; "Ignored in rag mode (no edges)" — today's wording), `--max-docs`
   (`IntRange(min=1)`, default `query.full_graph_max_docs`, ignored with `--query`), `--output/-o`,
   `--no-open`. Reads `app_config.memory.mode` ONCE. `rag`: no query → `fetch_rag_structure`; query →
   `retrieve_parents(top_k)` → `nothing_found` → the no-results line + exit 1, else
   `fetch_retrieval_structure`. `graphrag`: today's `query_memory` / `fetch_full_graph` branch verbatim.
   Shared tail: empty structure → the empty-memory line + exit 1; else
   `visualize_query_result(result, output, open_browser=not no_open, query=query or ("structure" if
   rag else "graph"))`, then `click.echo(f"Wrote {path}")`. `SearchUnavailableError` → today's one line +
   exit 1. Module docstring = the table above in prose. `RAG_FULL_GRAPH_UNAVAILABLE` is deleted; the two
   messages `EMPTY_MEMORY_MESSAGE` / `NO_RESULTS_MESSAGE` (the latter a `str.format` template taking the
   query) are shared by this script and the rag MCP tool, so they live in `tree/memory/rag/structure.py`
   the way `NO_CLUSTERING_RUN_MESSAGE` lives in `visualize/embeddings.py`; both callers import them.
5. **`scripts/search_memory.py`** (new, from the text half of `query_graph.py`): `init_logger()`;
   `@user_options`, `--query/-q` (required), `--top-k/-k`; `retrieve_parents` → `_print_parents`
   (`_format_parent_block`, `_EXCERPT_CHARS = 300`, `DEGRADED_SEARCH_CAVEAT`, `SEARCH_UNAVAILABLE_LINE`
   move here unchanged). No mode branch at all — document in the docstring that this is Chapter 4's
   retrieval and works identically in graphrag.
6. **`apps/memory/Makefile`** — delete `query-graph`; add under `# --- Querying ---`:
   ```make
   visualize-structure: # Render the MEMORY STRUCTURE of one user as an interactive HTML file under `.tree/graphs/` and open it, following memory.mode: in `rag` the document → parent chunk → child chunk tree (edges drawn from parent_id), in `graphrag` the knowledge graph. No QUERY: the 500 most-recent documents, 100 shown (MAX_DOCS=N embeds fewer). QUERY="…" narrows to the search results (rag: the retrieved parents + their documents and children; graphrag: the expanded subgraph); the Documents slider then ranks by relevance. OUTPUT=<path> pins the file. Defaults to the current user; USER_ID=<oid> / USER_IDENTIFIER=<handle> override.
   	uv run python scripts/visualize_structure.py $(USER_FLAGS) $(if $(QUERY),--query "$(QUERY)",) $(if $(TOP_K),--top-k "$(TOP_K)",) $(if $(MAX_DOCS),--max-docs "$(MAX_DOCS)",) $(if $(OUTPUT),--output "$(OUTPUT)",)

   search: # Search one user's memory and print the ranked parent chunks as TEXT (score, title, heading path, 300-char excerpt, matched children) — Parent-document retrieval, identical in both modes. Requires QUERY="…"; TOP_K=N caps the parents. Defaults to the current user; USER_ID / USER_IDENTIFIER override.
   	@if [ -z "$(QUERY)" ]; then echo 'USAGE: make memory-search QUERY="your query" [TOP_K=10]'; exit 1; fi
   	uv run python scripts/search_memory.py $(USER_FLAGS) --query "$(QUERY)" $(if $(TOP_K),--top-k "$(TOP_K)",)
   ```
   `visualize-embeddings` stays exactly as is.
7. **`tree/mcp/tools.py`** — `visualize_memory_structure` (rag; `@track(name="visualize_memory_structure")`,
   `_set_retrieval_thread`, `AppConfig(resource_uri=GRAPH_VIEW_URI)`), registered with
   `if MEMORY_MODE == "rag": mcp.tool(visualize_memory_structure)` next to the rag `search_memory`.
   Body: `max_docs < 1` → `tool_error("invalid_input", "max_docs must be ≥ 1", retryable=False)`; with
   `query`: `retrieve_parents(top_k)` inside the `_retrieval_error` guard → `nothing_found` → plain `str`
   `NO_RESULTS_MESSAGE` (no payload, like `NO_CLUSTERING_RUN_MESSAGE`); then `fetch_retrieval_structure`;
   without: `fetch_rag_structure(max_docs)`; empty → plain `str` `EMPTY_MEMORY_MESSAGE`. Then
   `to_graph_payload(result, document_order="relevance" if query else "recency")`, the summary from the
   table (reuse `_ORDER_ADJECTIVE` — move it to `viz_app.py` or duplicate the two-entry dict; SWE's
   call), `_graph_tool_result(ctx, payload, summary, query=query or "structure", as_html_file=…)`.
   Docstring: contains `ERROR_CONTRACT` verbatim; tells the model this is the way to SEE the memory's
   structure in rag (documents and their chunks), that `query` narrows to the passages that matched, and
   that there are no entities or relations in this mode. `EMPTY_MEMORY_MESSAGE` / `NO_RESULTS_MESSAGE`
   are imported from `tree.memory.rag.structure` (item 4) so the CLI and the tool print the same words.
8. **`tree/mcp/graph_tools.py`** — rename the function and the `@track` name to
   `visualize_memory_structure`; body unchanged except the summary prefix stays `Knowledge graph for …`
   and the no-query file slug stays `graph`. Docstring: "In graphrag this is the knowledge graph …".
   `_dual_graph_result` and the graphrag `search_memory` / `query_memory` untouched.
9. **`tree/mcp/server.py`** — both instruction strings name `visualize_memory_structure` (rag: "to show
   how the memory is organised — documents and the chunks they split into — optionally narrowed to a
   query"; graphrag: today's graph sentence, renamed). The rag comment "names ONLY the seven tools" →
   eight.
10. **Reference sweep** (every hit of `memory-query-graph`, `query_graph`, `visualize_memory_graph`
    outside `docs/adrs/` and `tasks/`): `README.md:170` (two lines: `make memory-visualize-structure`
    "(document → chunk tree in rag, knowledge graph in graphrag)" and `make memory-search QUERY=…`),
    `apps/memory/README.md` (lines 13, 28 — the mode table row becomes `make
    memory-visualize-structure` with the two renderings; the "Query CLI" section becomes "Structure view"
    + "Search CLI"; the tool table moves `visualize_memory_structure` into the both-modes table with
    "*Both modes (8 tools):*" and "*`graphrag` only (6 more, 14 total):*"; line 823 scripts list),
    `.agents/skills/tree-memory/SKILL.md` (row `| \`visualize_memory_structure\` | ✅ | ✅ | …`; "the
    seven graph tools" → six; the "exception that proves the rule" sentence now names both viz tools),
    `.agents/skills/run-pipelines-e2e/SKILL.md` step 3 (`make memory-search QUERY="test query"` for the
    text read-back in both modes; `make memory-visualize-structure` for the HTML in both modes — delete
    the "a missing HTML file in `rag` is the expected outcome" paragraph; keep the headless `--dump-dom`
    smoke sentence), `tree/mcp/dashboard_app.py` docstrings (lines 9, 92), `tree/config/constants.py:53`,
    `tree/mcp/client.py:17` (`call_tool("query_graph")` is stale already → `search_memory`),
    `docs/glossary.md` (**Full graph**, **Graph payload**, **Graph renderer**, **Memory mode** notes —
    wording below). `docs/notes/prefect-execution-topologies.md:272` only names the map command; no change.

### Documentation (PA-authored wording — the SWE applies verbatim in the implementation commit)

- **Glossary** (`docs/glossary.md`):
  - New row: `| **Memory structure** | The structural view of one user's \`memory\` collection that \`make memory-visualize-structure\` / the \`visualize_memory_structure\` MCP tool draw through the **Graph renderer**, following the **Memory mode**: in \`rag\` the \`document\` → **Parent chunk** → **Child chunk** tree, whose \`part_of\` edges are SYNTHESISED from each chunk row's \`parent_id\` at read time (\`tree.memory.rag.structure\`, a \`MemoryStructure\`) and never stored; in \`graphrag\` the knowledge graph (the **Full graph** without a query, the expanded query view with one). | One renderer, one controls panel, one \`Documents\` slider in both modes (no query: 100 of the 500 most-recent documents; \`QUERY=\`: every matched document, ranked by relevance). The rag query view = the retrieved parents + their documents + ALL their children. Text, not pictures: \`make memory-search\` (both modes). |`
  - **Full graph** Notes: append "In `rag` the no-query **Memory structure** is the same read without entities or stored edges (`fetch_rag_structure`), with the same cap and slider."
  - **Graph payload** Definition: "built by `to_graph_payload` … from a `MemoryStructure` (a `QueryResult` in graphrag)".
  - **Graph renderer** Notes: "`visualize_memory_graph`" → "`visualize_memory_structure`"; add "draws the rag **Memory structure** too (task 173)".
  - **Memory mode** Notes: "the seven graph tools" → "the six graph tools; `visualize_memory_structure` and `visualize_memory_embeddings` exist in both modes".
- **ADR-005** Status line, append: "— extended to `rag` by task 173 (`tasks/173-memory-visualize-structure.md`): the rag **Memory structure** (document → parent → child tree) draws through the same renderer from `part_of` edges synthesised out of `parent_id` by `tree.memory.rag.structure`, never persisted; `make memory-query-graph` → `make memory-visualize-structure` (+ `make memory-search` for text); `visualize_memory_graph` → `visualize_memory_structure`, registered in both modes. Body paths and names below read as history." No body edit.
- **ADR-006** Status line, append: "— §5 tool sets amended by task 173: `visualize_memory_structure` registers in both modes (`rag` 8 tools, `graphrag` 14; six graph-only); rag `search_memory` keeps `(query, top_k)`."
- **ADR-011** Status line, append: "— §7's capped, recency-ranked read and the `Documents` slider are reused by the rag **Memory structure** (task 173); the ranking helpers moved to `tree.memory.rag.structure`."
- No new ADR: every choice here applies ADR-005/006/011; nothing contradicts them.

### Tests (`/squid-testing-python`)

Every test that depends on the mode pins it with `monkeypatch.setattr(app_config.memory, "mode", …)`.

- `tests/unit/memory/rag/test_structure.py` (NEW — required by layout guard 5):
  - `TestSynthesizePartOfEdges` (pure): child→parent and parent→document edges with `build_edge_id`
    ids, `type == "part_of"`, `source_node_id` = the child; a `parent_id` outside the rows → no edge;
    rows without `parent_id` (documents) → no edge; `doc_rank`/`sources` copied; input order kept; a
    second call is byte-identical (deterministic).
  - `TestRankRecentDocuments`: ISO `properties.date` beats `created_at`, undated last, ties on
    `str(_id)`, cut to `max_docs`, naive datetimes never produced (the moved `as_utc`).
  - `TestFetchRagStructure` with the fake collection the file's neighbour `test_retrieval.py` uses
    (`make_collection` / `_client`): two reads only (count `find` calls), both carry `user_id` and project
    `embedding: 0`; nodes = documents + parents + children of the kept documents; `doc_rank` 1-based
    newest first; `max_docs` cap honoured and defaulted from `app_config.query.full_graph_max_docs`;
    edges == number of chunk rows; empty collection → `MemoryStructure()`.
  - `TestFetchRetrievalStructure`: documents ranked by first parent position; ALL children of each
    retrieved parent are read (not only `matched_children`) through the `subtype: child` + `parent_id
    $in` filter; `user_id` in every filter; empty `parents` → empty structure with no read.
  - `test_the_module_never_imports_the_graph_layer` is already enforced by `test_package_layout.py`;
    add nothing, keep it green.
- `tests/unit/memory/graph/test_retrieval.py`: the recency tests import the helpers from
  `tree.memory.rag.structure`; `fetch_full_graph` assertions unchanged.
- `tests/unit/memory/rag/test_types.py`: `MemoryStructure` defaults; `tests/unit/memory/test_pipeline.py`
  or wherever `QueryResult` is constructed — `isinstance(QueryResult(), MemoryStructure)`.
- `tests/unit/memory/visualize/test_graph.py`: `to_graph_payload` on a rag `MemoryStructure` — node
  sizes 10/7/4 by role, `part_of` edges present, `docRank` → `controls.documents {shown: min(100,
  total), total, order: "recency"}`, `order: "relevance"` with `shown == total` for a query view; a
  test pinning that the four template constants are unchanged (hash or token set — the SWE picks the
  cheapest form that would go red on a JS edit).
- `tests/unit/scripts/test_visualize_structure.py` (NEW; the graph half of `test_query_graph.py` ported:
  `test_query_expands_the_graph_and_renders_it`, `test_without_a_query_it_loads_the_full_graph`, the
  three `max_docs` cases, `test_an_empty_graph_exits_one`, `test_one_retryable_line_and_exit_one`) plus
  rag cases: no query → `fetch_rag_structure` called with `max_docs`, rendered with `query="structure"`
  and `document_order` recency; query → `retrieve_parents(top_k)` then `fetch_retrieval_structure`,
  rendered with the query (relevance); `nothing_found` → `NO_RESULTS_MESSAGE`, exit 1, renderer never
  called; empty memory → `EMPTY_MEMORY_MESSAGE`, exit 1; `--max-docs` never reaches a query in either
  mode; `graph` symbols never called in rag (`fetch_full_graph` / `query_memory` spies untouched).
- `tests/unit/scripts/test_search_memory.py` (the text half of `test_query_graph.py`, renamed):
  one block per parent, 300-char excerpt, writes no file, `No results.`, degraded caveat first (also on
  empty), no caveat on hybrid, missing `--query` → Click usage error (exit 2); parametrised over both
  modes: `retrieve_parents` is the only reader called.
- `tests/unit/mcp/test_tools.py` — `TestRagVisualizeMemoryStructure` (the `TestVisualizeMemoryEmbeddings`
  style, boundaries patched on the module): UI-capable client gets the payload inline with the summary
  text; non-UI client gets the file + `graphs://` link; `as_html_file=True` forces the file; `query` →
  `retrieve_parents(top_k=…)` and a relevance-ordered payload; `nothing_found` → the plain message, no
  `_graph_tool_result` call; empty memory → the plain message; `max_docs=0` → `invalid_input`
  envelope; `SearchUnavailableError` → `search_unavailable` retryable envelope; the docstring contains
  `ERROR_CONTRACT`; the function's signature has no `max_hops`.
- `tests/unit/mcp/test_graph_tools.py` — rename references; summaries unchanged.
- `tests/unit/mcp/test_tool_gating.py` — `_SHARED_TOOLS` += `visualize_memory_structure` (8),
  `_GRAPH_ONLY_TOOLS` −`visualize_memory_graph` (6); test names "seven" → "six"; `tools.py` still
  imports no `_GRAPH_MODULES` in rag; the rag server's `visualize_memory_structure` schema has no
  `max_hops`, the graphrag one has.
- `tests/unit/mcp/test_error_envelope.py` — the enumerated tool list gains the rag
  `visualize_memory_structure`.
- `tests/unit/test_embedding_map_docs.py` — needles `*Both modes (8 tools):*` and `6 more, 14 total`.
- `tests/unit/test_structure_view_docs.py` (NEW, same grep-level pattern): `apps/memory/README.md`,
  `README.md`, both skills contain `make memory-visualize-structure`, `make memory-search` and
  `visualize_memory_structure`; the `tree-memory` skill row for `visualize_memory_structure` has two ✅;
  `git grep --untracked -n -e "memory-query-graph" -e "query_graph" -e "visualize_memory_graph" --
  apps/memory .agents docs/glossary.md README.md` is empty (ADRs and `tasks/` keep the history —
  spell the needles as concatenations, as that file does for `graph_app`).
- `make memory-tests`, `make memory-format-check`, `make memory-lint-check`, `make pre-commit` green.

### Local verification (LOCAL ONLY — `make env-status` → `local`; STOP if it prints prod)

Starts in `rag` with the 3-article subset
`/private/tmp/claude-501/-Users-pauliusztin-Documents-01-Projects-AI-Engineer-Handbook-building-agentic-systems-building-agentic-systems/944c4516-3945-43f4-8111-e0c95524323d/scratchpad/169/light3.yaml`
loaded (172 left it so). Mode switches use `make memory-reset-mode CONFIRM=yes` (172). Serve workflows
FROM THIS WORKTREE; `counts.js` + `mongo.sh` from `…/scratchpad/169/` (point `mongo.sh`'s `cd` at this
worktree). Headless smoke as ADR-011's verification stance: `chrome --headless --disable-gpu
--use-angle=swiftshader --enable-unsafe-swiftshader --virtual-time-budget=4000 --dump-dom <file>` and
grep the `<body …>` markers. Paste every output in `## Log`. MUST END IN `rag`.

1. **rag, no query.** `make memory-visualize-structure` → `Wrote .tree/graphs/structure-<stamp>.html`;
   dump-dom shows `<body data-layout="live" data-sim="running" data-docs="3/3">`; the header counts
   equal `counts.js`: nodes = `rows:` (every rag row), edges = parents + children (every chunk row);
   legend rows `document`, `chunk` only. `[HUMAN]` open it: three stars, each document → its parents →
   their children; drag a document and its parents follow rigidly; drop pins it; the `Documents` slider
   reads `Most recent`, dragging it to 1 hides two stars and re-fits once revealed.
2. **rag, query.** `make memory-visualize-structure QUERY="how does memory for ai agents work"` → file
   `how-does-memory-…html`; `data-docs="D/D"` with `D ≤ 3`; the number of parent nodes equals the parent
   count `make memory-search QUERY="how does memory for ai agents work"` prints; every parent shows ALL
   its children (compare with `db.memory.countDocuments({parent_id: "<pid>"})` for one parent).
   `[HUMAN]` the slider reads `Most relevant`; the best-matching document is rank 1 (slider at 1 keeps
   only it).
3. **rag, edges and errors.** `make memory-search QUERY="zzzq wamble frobnitz"` → `No results.`;
   `make memory-visualize-structure QUERY="zzzq wamble frobnitz"` → `No results for "zzzq wamble
   frobnitz" — nothing to draw.`, exit 1, no new file. `make memory-query-graph` → make fails with "No
   rule to make target" (exit 2). `counts.js` still prints `edges: 0` (nothing was persisted by drawing).
4. **rag MCP.** `make memory-serve-mcp TRANSPORT=streamable-http` (rag) → `uv run fastmcp call
   http://127.0.0.1:8000/mcp --auth none visualize_memory_structure` answers the file path + `graphs://`
   link with the `Memory structure (rag: …)` summary; `… visualize_memory_structure query="how does
   memory for ai agents work"` likewise; the tool list has 8 tools, no `visualize_memory_graph`;
   `search_memory`'s schema has no `visualize`. Stop the server.
5. **Switch to graphrag.** In the serving shell `export TREE_MEMORY__MODE=graphrag`;
   `TREE_MEMORY__MODE=graphrag make memory-reset-mode CONFIRM=yes` (prints `memory.mode (configured):
   graphrag`); re-serve; `make memory-run-pipeline SOURCE_FILE=<light3.yaml>` (Gemini extraction over 3
   articles — the 169/170 cost; chunking + child embeddings replay from cache). `counts.js`: entity rows,
   `person self`, `edges: > 0`.
6. **graphrag parity with today's `memory-query-graph`.** `TREE_MEMORY__MODE=graphrag make
   memory-visualize-structure` → `graph-<stamp>.html`, `data-docs="3/3"`, entities and `part_of`/`next`/
   `mentions` edges, summary adjective `most-recent`; `… QUERY="how does memory for ai agents work"` →
   relevance slider, `part_of` closure complete (no floating chunk); `make memory-search QUERY=…` prints
   parents in graphrag too. MCP in graphrag: 14 tools, `visualize_memory_structure` present with
   `max_hops`, `visualize_memory_graph` absent; `search_memory visualize=true` still answers rows + graph.
   `[HUMAN]` the graphrag page looks exactly like before this task (same palette, panel, slider).
7. **Back to rag (final state).** `unset TREE_MEMORY__MODE`; `make memory-reset-mode CONFIRM=yes`
   (`configured: rag`); re-serve; `make memory-run-pipeline SOURCE_FILE=<light3.yaml>`; `counts.js` →
   rag shape (`edges: 0`, no `person` rows); `make memory-visualize-structure` works as in step 1.
8. **Clean up.** Stop the serve and MCP processes; delete `.tree/graphs/*.html`; `make env-status` →
   local; `git status` shows only this task's files.

### Out of scope (intentional)

- `next` edges between sibling chunks in the rag view (reading order) — the human asked for the
  `parent_id` tree; `chunk_index` is on the rows if a later task wants them.
- Highlighting the `matched_children` of a rag query view (a `meta.score` or a ring) — the view shows
  every child of a retrieved parent; which ones matched is in `make memory-search`'s output. Upgrade
  trigger: a human asking to see the match inside the parent.
- A `visualize` parameter on rag `search_memory` (decision 3).
- Any change to `make memory-visualize-embeddings`, the map payload, or the template JS/CSS.
- A `(user_id, parent_id)` index — recorded as an upgrade trigger, not built (ADR-012).
- Persisting rag `part_of` edges — contradicts ADR-006 §3.

## Acceptance Criteria

- [x] `make memory-visualize-structure` exists and `make memory-query-graph` does not (no alias); the
      memory Makefile lists exactly two `visualize-*` targets.
- [x] In `rag` with no `QUERY`, the command writes `.tree/graphs/structure-<stamp>.html` whose payload
      holds every `document`, parent and child row of the `max_docs` most-recent documents, one `part_of`
      edge per chunk row (child → parent, parent → document, `build_edge_id` ids), `docRank` by recency
      and `controls.documents {shown: min(100, total), total, order: "recency"}`.
- [x] In `rag` with `QUERY`, the payload holds the `top_k` retrieved parents, their documents and ALL
      children of those parents, ranked by relevance with `shown == total`.
- [x] In `graphrag` the command's output (file, payload, summary, slider) is identical to today's
      `make memory-query-graph` for the same arguments.
- [x] The `memory` collection gains no edge row in `rag` from any render (synthesised only).
- [x] The renderer template (`_GRAPH_STYLE`, `_BODY_MARKUP`, `_RENDER_JS`, `_FILE_HTML_TEMPLATE`) is
      byte-identical to `main`; `visualize/` still reads no Mongo; `rag/` still imports no `graph/`
      (layout test green with the new `rag/structure.py` + its mirror test).
- [x] Empty memory → `Memory is empty for this user — run make memory-run-pipeline first.`, exit 1, no
      file; `nothing_found` on a query → `No results for "<query>" — nothing to draw.`, exit 1, no file;
      both legs down → the existing `Search unavailable: … — retryable` line, exit 1 — in both modes.
- [x] `make memory-search QUERY="…"` prints the ranked parent blocks (score, title — heading path,
      300-char excerpt, matched-children count; degraded caveat first; `No results.` on empty) in BOTH
      modes and writes no file; without `QUERY` it prints the usage line and exits 1.
- [x] MCP: `visualize_memory_structure` is registered in both modes — rag signature `(query, top_k,
      as_html_file, max_docs)`, graphrag signature adds `max_hops`; `visualize_memory_graph` is gone;
      rag serves 8 tools, graphrag 14; rag `search_memory` still has no `visualize` parameter; the rag
      tool delivers through `_graph_tool_result`, answers the two plain-string messages for empty /
      no-results, and the `{error_type, retryable, message}` envelope on failure.
- [x] `tests/unit/scripts/test_query_graph.py` is replaced by `test_visualize_structure.py` +
      `test_search_memory.py` with every ported case still asserted.
- [x] Every reference in code, skills, READMEs and the glossary is updated (docs test: no
      `memory-query-graph` / `query_graph` / `visualize_memory_graph` outside `docs/adrs/` and `tasks/`);
      the glossary gains **Memory structure** verbatim; ADR-005/006/011 carry the Status-line notes above.
- [x] `make memory-tests`, format, lint and `make pre-commit` green; local verification pasted in the Log,
      ending in `rag`.
- [ ] [HUMAN] In the rag HTML: documents → parents → children read as three-level stars; dragging a
      document carries its parents rigidly; drop pins; the `Documents` slider reads `Most recent` and
      hides whole stars; a `QUERY` view's slider reads `Most relevant` and keeps the best document at 1.
- [ ] [HUMAN] In the graphrag HTML nothing looks different from before this task.

## User Stories

### Story: A Chapter-4 reader sees how their documents were chunked
1. Reader runs `make memory-visualize-structure` on a `rag` deployment with 3 articles.
2. The terminal prints `Wrote .tree/graphs/structure-20261002-181500.html` and the browser opens it.
3. The header reads `3 of 3 documents · 121 nodes · 118 edges`; the legend shows `document` and `chunk`.
4. Reader hovers a mid-size node: the card says `chunk`, `subtype parent`; hovers a small one: `subtype
   child`; drags a document star and its parents and children move with it.

### Story: A reader checks what a question retrieves
1. Reader runs `make memory-search QUERY="how does memory for ai agents work"` and reads 4 ranked parent
   blocks with scores and excerpts.
2. Reader runs `make memory-visualize-structure QUERY="how does memory for ai agents work"`.
3. The page shows the same 4 parents under their 2 documents with every child of those parents; the
   slider reads `Most relevant`; moving it to 1 leaves only the best-matching document's subtree.

### Story: A graphrag operator keeps their workflow
1. Operator used to run `make memory-query-graph QUERY="Paul Iusztin"`.
2. They run `make memory-visualize-structure QUERY="Paul Iusztin"` instead and get the same expanded
   subgraph (entities, `mentions`, complete `part_of` chains, relevance slider) in the same file layout.
3. `make memory-visualize-structure` with no query gives the same Full graph as before, 100 of up to 500
   documents shown.

### Story: An MCP client on a rag server asks to see the memory
1. The model on a rag server (8 tools) calls `visualize_memory_structure` with no arguments.
2. A UI-capable client renders the tree inline; a plain client gets `.tree/graphs/structure-….html` and a
   `graphs://` link, with the summary `Memory structure (rag: document → parent chunk → child chunk) for
   your full memory: 3 of 3 most-recent documents shown by default, 121 nodes, 118 edges`.
3. The model calls it with `query="context engineering"` and gets the narrowed tree; with a nonsense
   query it gets `No results for "zzzq wamble frobnitz" — nothing to draw.` as plain text.

### Story: A verifier reads the memory back without a browser
1. After a `rag` pipeline run, the verifier follows e2e step 3: `make memory-search QUERY="test query"`.
2. The ranked parents print; no HTML file is expected or written.
3. They run `make memory-visualize-structure` only when they want the picture, in either mode.

### Story: A model on a rag server does not look for a graph
1. The model reads the server instructions: `search_memory` for passages, `visualize_memory_structure`
   to see documents and their chunks, `visualize_memory_embeddings` for topics.
2. It never calls `visualize_memory_graph` (unknown) or `search_memory(visualize=True)` (no such
   parameter in rag) — the tool schemas say so.

---

Blocked by: 172

## Log

### [PA] 2026-10-02 18:05 — Grooming

**Summary**
The visualization surface becomes exactly two commands: `make memory-visualize-structure` (new — the
document → parent → child tree in `rag`, today's knowledge graph in `graphrag`, optional `QUERY=` in
both, one renderer) and `make memory-visualize-embeddings` (unchanged). `make memory-query-graph` is
removed: its HTML role moves to the structure command, its text role to a mode-neutral
`make memory-search QUERY=…`. The MCP tool `visualize_memory_graph` becomes `visualize_memory_structure`
in both modes.

**Key decisions**
- Rag edges are synthesised from `parent_id` at read time (`tree.memory.rag.structure`), never stored —
  zero template change, ADR-006 §3 intact.
- The rag reader is rag's (`rag/structure.py`, new `MemoryStructure` type; `QueryResult` subclasses it);
  the recency/provenance helpers move out of `graph/retrieval.py` so one ranking serves both readers.
- MCP uses the existing two-functions-one-name pattern: rag `(query, top_k, as_html_file, max_docs)` in
  `tools.py`, graphrag keeps `max_hops` in `graph_tools.py`; 8 / 14 tools. rag `search_memory` gains no
  `visualize` parameter.
- `memory-query-graph` is deleted without alias; text search survives as `make memory-search`.
- ADR-005/006/011 get Status-line notes (drafted above); no new ADR; glossary gains **Memory structure**.

**Dependencies**
- 172 — the local verification switches rag → graphrag → rag with `make memory-reset-mode`.

**User stories**
- 6 stories: the rag tree, the rag query view, graphrag parity, the rag MCP client, the browser-free
  verifier, the model that does not look for a graph.

**Open questions** (defaults recommended; do not block implementation)
- Keep rag `search_memory` without `visualize` (recommended, decision 3), or add it for symmetry with
  graphrag? Adding it is ~30 lines on top of this task if the human wants it.
- `make memory-search` as the text command's name (recommended) vs dropping the text CLI and relying on
  the MCP `search_memory` only.
- Doc edits (glossary, ADR Status lines) are specified in-task for the SWE to apply verbatim, since PA
  was asked not to write doc files in this round.

Ready for implementation.

### [SWE] 2026-10-02 18:58 — Implementation

**Files modified**
- `apps/memory/src/tree/memory/rag/structure.py` (new) — the rag **Memory structure** reader: `fetch_rag_structure` (no query, 2 reads), `fetch_retrieval_structure` (query, 3 reads, ALL children), `synthesize_part_of_edges` (child→parent, parent→document, `build_edge_id` ids, never stored), `rank_recent_documents`, and the moved recency helpers (`as_utc`, `document_recency`, `UNDATED`, `rank_by_provenance`, `NO_EMBEDDING`). Also `EMPTY_MEMORY_MESSAGE` / `NO_RESULTS_MESSAGE`, shared by the CLI and the rag MCP tool.
- `apps/memory/src/tree/memory/rag/types.py` — `MemoryStructure(nodes, edges)`; `tree/memory/types.py` — `QueryResult(MemoryStructure)`.
- `apps/memory/src/tree/memory/graph/retrieval.py` — imports the helpers from `rag.structure`; `fetch_full_graph` step 1 uses `rank_recent_documents` (same reads, same output).
- `apps/memory/src/tree/memory/visualize/graph.py` — annotations → `MemoryStructure`; `visualize_query_result` gains `document_order` (default: the old derivation from `query`). Template constants untouched (pinned by hash).
- `apps/memory/scripts/visualize_structure.py` (git mv from `query_graph.py`, rewritten) and `apps/memory/scripts/search_memory.py` (new, mode-neutral text search).
- `apps/memory/Makefile` — `query-graph` deleted; `visualize-structure` + `search` added (exactly two `visualize-*` targets).
- `apps/memory/src/tree/mcp/tools.py` — rag `visualize_memory_structure` `(ctx, query, top_k, as_html_file, max_docs)`, registered only in rag with the shared `ui://` app. `graph_tools.py` — the graphrag function renamed; `_ORDER_ADJECTIVE` moved to `viz_app.py` (both tools import it). `server.py` — both instruction texts name `visualize_memory_structure`; comments say eight / six.
- Reference sweep: `viz_app.py`, `dashboard_app.py`, `constants.py`, `client.py`, `scripts/visualize_embeddings.py`, `README.md`, `apps/memory/README.md` ("Structure view" + "Search CLI", tool tables 8 / 6 more, 14 total), both skills, `docs/glossary.md`, ADR-005/006/011 Status lines.
- Tests: `tests/unit/memory/rag/test_structure.py` (new), `tests/unit/scripts/test_visualize_structure.py` (git mv + rewrite), `tests/unit/scripts/test_search_memory.py` (new), `tests/unit/test_structure_view_docs.py` (new), and edits to `test_tools.py`, `test_tool_gating.py`, `test_error_envelope.py`, `test_graph_tools.py`, `test_viz_app.py`, `test_types.py`, `visualize/test_graph.py` and `test_embedding_map_docs.py`.

**Tests**
- Unit: 4647 passed, 0 failed (`make memory-tests`). Integration: N/A (no suite, per CLAUDE.md).
- `make memory-format-check`, `make memory-lint-check` and `make pre-commit` are all green.

**Acceptance criteria**
- [x] Two `visualize-*` targets, no `query-graph`: verified by `make memory-query-graph` → `No rule to make target` (log below) and `grep -c '^visualize-' Makefile` = 2.
- [x] rag no-query payload: `test_structure.py::TestFetchRagStructure`, `test_graph.py::test_a_rag_tree_*`, `test_visualize_structure.py::TestRagMode::test_no_query_draws_the_recent_tree_slugged_structure`, plus a live run (83 nodes / 80 edges = every row / every chunk row).
- [x] rag query payload: `TestFetchRetrievalStructure`, `TestRagMode::test_query_retrieves_parents_then_reads_their_tree`, plus a live run (child counts per parent = `countDocuments({parent_id})`).
- [x] graphrag parity: the payloads from `main`'s `make query-graph` and this branch's `make memory-visualize-structure` are dict-equal for no-query (103/157) and for the query view (70/68).
- [x] No edge row persisted in rag: `counts.js` shows `edges: 0` after every render.
- [x] Template byte-identical: `test_graph.py::test_the_template_constants_are_byte_identical_to_main` (sha256 of main's four constants); `test_package_layout.py` is green with `rag/structure.py` and its mirror test.
- [x] Empty / no-results / both search legs down, in both modes: `TestRagMode`, `TestGraphragMode`, `TestSearchUnavailable`, plus the live no-results run.
- [x] `make memory-search`: `test_search_memory.py` (parametrised over both modes), plus live runs in both modes and the usage line.
- [x] MCP: `test_tools.py::TestRagVisualizeMemoryStructure`, `test_tool_gating.py::TestStructureToolSignaturePerMode` + `TestRegisteredToolSet`, `test_error_envelope.py`, plus live `fastmcp list` / `fastmcp call` in both modes.
- [x] `test_query_graph.py` was split into `test_visualize_structure.py` + `test_search_memory.py`, with every ported case.
- [x] Reference sweep: `test_structure_view_docs.py::test_the_retired_names_are_gone_outside_the_history`. The glossary gains **Memory structure** verbatim and the ADR Status notes were applied.
- [x] Green QA, and the local verification below ends in `rag`.
- [ ] [HUMAN] rag HTML look and feel (see the paths under Notes).
- [ ] [HUMAN] graphrag HTML unchanged.

**Evidence** (LOCAL only. `make env-status` → `Env target: local (.env)`, host `localhost`, db `tree`)
```
# Start state (rag): rows: 83 edges: 0 embedded: 75 (75 child, 5 parent, 3 document)

# 1. rag, no query
$ BROWSER=true make memory-visualize-structure
Memory structure (rag): embedded 3 of 3 documents (most recent first) → 83 nodes, 80 edges
Wrote .../apps/memory/.tree/graphs/structure-20261002-154217.html
headless: <body data-layout="live" data-docs="3/3" data-sim="running">
          <span id="counts">3 of 3 documents · 83 nodes · 80 edges</span>
          legend: chunk, document · panel: Documents / Most recent / 3 of 3 · console errors: 0

# 2. rag, query
$ make memory-search QUERY="how does memory for ai agents work"
[0.032] How Does Memory for AI Agents Work?  matched children: 20
[0.032] How Does Memory for AI Agents Work?  matched children: 4
[0.027] Context Engineering: 2025's #1 Skill in AI  matched children: 9
[0.016] You're Not Building Agents ... — 3. OBSERVATION ...  matched children: 2
[0.014] You're Not Building Agents ...  matched children: 5
$ BROWSER=true make memory-visualize-structure QUERY="how does memory for ai agents work"
Memory structure (rag query): 5 parents under 3 documents → 83 nodes, 80 edges
headless: data-docs="3/3" · counts 3 of 3 documents · 83 nodes · 80 edges · panel: Most relevant · console errors: 0
(The 3-article corpus has only 5 parents, so top_k=10 retrieves all of them. Narrowing check with --top-k 2:
 "2 parents under 1 documents → 27 nodes, 26 edges"; p0 shows 20 children and p1 shows 4, equal to
 db.memory.countDocuments({parent_id: …}) = 20 / 4. The context-engineering parent shows all 18 children,
 not only the 9 that matched.)

# 3. rag, errors
$ make memory-search QUERY="zzzq wamble frobnitz"                 → No results.
$ make memory-visualize-structure QUERY="zzzq wamble frobnitz"    → No results for "zzzq wamble frobnitz" — nothing to draw.  (script exit 1, no new file)
$ make memory-query-graph                                          → make[1]: *** No rule to make target `query-graph'.  Stop.  (exit 2)
$ make memory-search                                               → USAGE: make memory-search QUERY="your query" [TOP_K=10]  (exit 1)
counts.js → rows: 83 edges: 0

# 4. rag MCP (port 8765: 8000 is taken by an unrelated docker container; USER_ID=6abf89ef81b8cfb94d2032a2)
fastmcp list → 8 tools: ingest_conversation ingest_file ingest_url scrape_web search_memory search_web visualize_memory_embeddings visualize_memory_structure
  search_memory ['query','top_k'] · visualize_memory_structure ['as_html_file','max_docs','query','top_k']
call visualize_memory_structure → "Memory structure (rag: document → parent chunk → child chunk) for your full memory: 3 of 3 most-recent documents shown by default, 83 nodes, 80 edges. … graphs://structure-20261002-154846.html"
call … query="how does memory for ai agents work" → "… for 'how does memory for ai agents work': 3 of 3 most-relevant documents shown by default, 83 nodes, 80 edges …"
call … query="zzzq wamble frobnitz" → No results for "zzzq wamble frobnitz" — nothing to draw.
call … max_docs=0 → {"error_type":"invalid_input","retryable":false,"message":"max_docs must be ≥ 1"}

# 5. → graphrag
$ TREE_MEMORY__MODE=graphrag make memory-reset-mode CONFIRM=yes → memory.mode (configured): graphrag · Dropped memory (83 rows)
serve (this worktree, TREE_MEMORY__MODE=graphrag, PREFECT_LOGGING_ROOT_LEVEL=INFO); make memory-run-pipeline SOURCE_FILE=light3.yaml → Completed
counts.js → rows: 261 edges: 158 (part_of 80, next 72, mentions 3, related_to 3); person:self present

# 6. graphrag parity
$ TREE_MEMORY__MODE=graphrag make memory-visualize-structure → Full graph: 3 of 3 documents → 103 nodes, 157 edges → graph-20261002-155028.html
  headless: data-docs="3/3" · 103 nodes · 157 edges · legend chunk document event fact object organization person · Most recent · console errors 0
… QUERY="how does memory for ai agents work" → 70 nodes, 68 edges · Most relevant · 0 chunks without a complete part_of chain to a document
Parity with main's query_graph.py (same DB, same args): payloads dict-equal for both (103/157 recency, 70/68 relevance).
make memory-search (graphrag) → the same 5 parent blocks as in rag.
MCP graphrag → 14 tools, visualize_memory_structure ['as_html_file','max_docs','max_hops','query','top_k'], the retired tool absent;
  summary "Knowledge graph for your full memory: 3 of 3 most-recent documents shown by default, 103 nodes, 157 edges";
  search_memory visualize=true → rows + a graphs:// file.

# 7. back to rag (final state)
$ make memory-reset-mode CONFIRM=yes → configured: rag · Dropped memory (261 rows); re-served (no TREE_MEMORY__MODE); pipeline → Completed
counts.js → rows: 83 edges: 0 embedded: 75 (75 child, 5 parent, 3 document); indexes back to the rag set
$ make memory-visualize-structure → 83 nodes, 80 edges → structure-20261002-155500.html · headless: data-docs="3/3", Most recent, console errors 0

# 8. Cleanup: the workflow serve and both MCP servers are stopped; env-status is local; stray HTML deleted.
```

**Notes**
- HTML kept for the [HUMAN] checks, under `apps/memory/.tree/graphs/`:
  - rag, no query: `structure-20261002-155500.html`
  - rag, query: `how-does-memory-for-ai-agents-work-20261002-154517.html`
  - graphrag, no query: `graph-20261002-155028.html`
  - graphrag, query: `how-does-memory-for-ai-agents-work-20261002-155032.html`
- `visualize_query_result` gained a `document_order` kwarg. Without it, the rag no-query slug `"structure"` would have flipped the slider to `relevance`. The graphrag no-query view now passes `query="graph"` with an explicit `recency`, so the file name and ranking are the same as before.
- In the graphrag no-query view, an empty structure now prints `EMPTY_MEMORY_MESSAGE` to stdout instead of the old `logger.error`; this is the line the task specifies. Exit code 1 is unchanged.
- In graphrag, `QUERY="zzzq wamble frobnitz"` still draws 2 entity nodes (`react`, `ztron`). `main`'s `query_graph.py` returns the same 2 nodes, so this is today's graphrag seed behaviour and not something this task changed. The no-results line only appears in graphrag when the expanded result is empty.
- rag MCP tool: a whitespace-only `query` is stripped and treated as no query (the no-query view), never sent to the embedder (`test_tools.py::TestRagVisualizeMemoryStructure::test_a_whitespace_query_is_the_no_query_view`). The graphrag twin is unchanged (body verbatim).
- Glossary: applied the PA rows verbatim. Two minimal renames the PA list did not name: the **Full graph** Definition now reads "graphrag no-query view of `visualize_memory_structure` / `make memory-visualize-structure`", and the "graphrag-only surfaces" sentence in **Graph payload** / **Graph renderer** drops the old tool and adds "`visualize_memory_structure` is registered in both modes". The **Memory mode** Notes had no "seven graph tools" phrase, so the sentence was appended instead.
- The graphrag instructions had no graph-viz sentence to rename, so one was added for `visualize_memory_structure`.
- Headless Chrome: one `--dump-dom` run hung after dumping (killed by `timeout 90`, exit 124); the DOM was complete and showed 0 console errors — a Chrome exit flake, not a page error.

### [Tester] 2026-10-02 16:20 — QA

**Test summary** (LOCAL only: `make env-status` → `Env target: local (.env)`, host localhost, db `tree`)
- Format / lint / pre-commit: PASS (`make memory-format-check` 0, `make memory-lint-check` 0, `make pre-commit` 0)
- Unit tests: 4647 passed / 0 failed (`make memory-tests`, 59s). Integration: N/A (no suite, per CLAUDE.md)
- Warnings: 0 pytest warnings (only the import-time Python 3.14 pydantic.v1 `UserWarning` from opik, pre-existing)

**E2E adversarial pass**
- Happy path rag: `BROWSER=true make memory-visualize-structure` → `Memory structure (rag): embedded 3 of 3 documents → 83 nodes, 80 edges`, `structure-<stamp>.html`; payload re-parsed from the file: 3 document (size 10) + 5 parent (7) + 75 child (4), 80 `part_of` edges, 0 dangling endpoints, every child → parent → document, `controls.documents {shown 3, total 3, order recency}`, docRank 1/2/3 (PASS). `edges: 0` in Mongo after every render (PASS).
- Tenancy (scratch DB `qa173_scratch`, dropped after): users A and B with identically named docs. `fetch_rag_structure` A → 12 nodes / 6 edges, B → 5 / 4, unknown user → 0 / 0; zero rows of another user in any result. `fetch_retrieval_structure` called as B with A's parent ids → 0 nodes (PASS).
- Orphans / gaps (same scratch DB): child whose `parent_id` points nowhere → drawn as a floating node, no edge, no `unknown` endpoint; chunk whose document row is missing → not drawn; document with no chunks → lone star; naive `created_at` / undated docs rank without a naive-datetime error; a retrieval whose document row is missing → child with `doc_rank None`, payload builds, no crash (PASS).
- Hostile: document name `evil</script><img src=x onerror=alert(1)>` → serialised as `<\/script>`, one `<script>` tag in the file, Chrome loads it with no JS error (PASS). CLI query `'; db.dropDatabase(); // $(touch /tmp/pwn173) <script>…` → ran as a plain query, `/tmp/pwn173` not created (PASS). Shell-meta MCP query likewise (PASS).
- Bounds CLI: `--max-docs 0` / `-1` → Click usage error exit 2; `--max-docs 1` → 1 of 1 documents, 27 nodes; `--max-docs 99999999999999999999` → 3 of 3 (PASS). `-k 2` → 2 parents, 1 document, 27 nodes. `-k 0` / `-k -3` → `No results for "…" — nothing to draw.` exit 1 (PASS with note 1).
- Query edge cases: nonsense `zzzq wamble frobnitz` → `No results for "zzzq wamble frobnitz" — nothing to draw.` exit 1, no file; `-q ""` → the no-query view; `-q "   "` → `No results for "   "` exit 1; Unicode `記憶 🤖 агент` → renders; 140 KB query → renders, slug capped (PASS).
- Empty memory (after `reset-mode`, user kept): rag no query → `Memory is empty for this user — run make memory-run-pipeline first.`; rag with `QUERY=x` → `No results for "x" — nothing to draw.`; graphrag mode no query → the same empty line; no file written (PASS).
- Commands: `make memory-query-graph` → `No rule to make target 'query-graph'` exit 2; `grep -c '^visualize-' apps/memory/Makefile` = 2 (`visualize-structure`, `visualize-embeddings`); `make memory-search` → usage line exit 1; `make memory-search QUERY=… TOP_K=2` prints ranked blocks, 300-char excerpts, `matched children: N`, writes no file; nonsense → `No results.` (PASS).
- rag MCP (port 8765, user 6abf…2a2): 8 tools; `visualize_memory_structure` schema `query, top_k, as_html_file, max_docs`; `max_hops=2` → unexpected-keyword error; `search_memory visualize=true` → unexpected-keyword error; `visualize_memory_graph` → not found; no args → `Memory structure (rag: document → parent chunk → child chunk) for your full memory: 3 of 3 most-recent documents shown by default, 83 nodes, 80 edges` + file + link; query → `most-relevant`; nonsense → `No results for "zzzq wamble frobnitz" — nothing to draw.`; `max_docs=0` / `-5` → `{"error_type":"invalid_input","retryable":false,"message":"max_docs must be ≥ 1"}`; `max_docs=abc` → validation error; whitespace `query="   "` → the no-query view (PASS).
- graphrag parity vs main (DB rebuilt in graphrag via `reset-mode` + `memory-run-pipeline` of light3.yaml; main = `git archive HEAD` snapshot of `scripts/query_graph.py` + `src`, run with `PYTHONPATH`; HEAD has no diff vs `main` on `scripts/query_graph.py`, `visualize/`, `graph/`): for no-query (106/158), `-q "how does memory for ai agents work"` (71/69), `-q "Paul Iusztin" -k 3 --max-hops 1` (30/29), nonsense query (3/2) and `--max-docs 1` (38/52) the produced HTML files are BYTE-IDENTICAL (`html identical: True`, payload equal) (PASS). Default file names `graph-<stamp>.html` / `<query-slug>-<stamp>.html` (PASS). graphrag MCP: 14 tools, `visualize_memory_structure` has `max_hops`, `visualize_memory_graph` absent, summary `Knowledge graph for your full memory: 3 of 3 most-recent documents shown by default, 106 nodes, 158 edges`, `max_docs=0` → invalid_input, `search_memory visualize=true` still returns rows + graph (PASS). `make memory-search` in graphrag prints the same 5 parent blocks as rag.
- Headless Chrome `--dump-dom` + `--enable-logging=stderr` on all four kept files: bodies `data-layout="live" data-docs="3/3"` (rag no-query `data-sim="running"`, graph no-query `settled`), counts `83 nodes · 80 edges` (rag), `103 nodes · 157 edges` (graphrag no-query), `70/68` (graphrag query); zero `Uncaught`/JS errors, the only CONSOLE lines are WebGL `GPU stall due to ReadPixels` info messages (SwiftShader) (PASS).
- Stale-name sweep: `grep -rIn -e query-graph -e query_graph -e visualize_memory_graph` over the whole worktree (harness, `.claude`, `.agents`, READMEs, src, tests) outside `docs/adrs/` and `tasks/` → only the concatenated needles in `tests/unit/test_structure_view_docs.py` (PASS).

**Acceptance criteria**
- [x] PASS — `visualize-structure` exists, `query-graph` does not, exactly two `visualize-*` targets — evidence above (`No rule to make target`, grep count 2)
- [x] PASS — rag no-query payload — re-parsed from the HTML file (counts, sizes 10/7/4, 80 `part_of` edges, docRank, controls) + `test_structure.py::TestFetchRagStructure`
- [x] PASS — rag query payload — `-k 2` run: 2 parents, 27 nodes (ALL children, 20 + 4), `shown == total`, order relevance + `TestFetchRetrievalStructure`
- [x] PASS — graphrag identical to main — byte-identical HTML on 5 argument sets
- [x] PASS — no edge row in rag — `edges: 0` after every render, scratch DB `kind: edge` count 0
- [x] PASS — template byte-identical, layout test green — `test_graph.py::test_the_template_constants_are_byte_identical_to_main` in the 4647 passing; the `visualize/graph.py` diff touches no template constant
- [x] PASS — empty / no-results / unavailable lines — empty (rag + graphrag) and no-results (rag) run live; unavailable covered by `test_visualize_structure.py::TestSearchUnavailable` (cannot take both legs down locally). See note 2 for graphrag nonsense
- [x] PASS — `memory-search` in both modes, usage line — live
- [x] PASS — MCP both modes, tool counts 8 / 14, signatures, messages, envelope — live
- [x] PASS — `test_query_graph.py` split — `git status` shows rename to `test_visualize_structure.py` + new `test_search_memory.py`, all green
- [x] PASS — reference sweep, glossary row, ADR-005/006/011 Status notes — grep + `git diff docs/adrs`
- [x] PASS — format / lint / pre-commit / tests green; log ends in `rag` (verified by me: DB ends `rows: 83 edges: 0 embedded: 75`, 75 child / 5 parent / 3 document, indexes `_id_, text_index, user_kind_type_subtype, user_type_name`, vector_index)
- [ ] [HUMAN] rag HTML look and feel — awaiting human verification
- [ ] [HUMAN] graphrag HTML unchanged — awaiting human verification (byte-identical HTML to main's output is strong evidence)

**Other issues found** (none blocks)
1. `--top-k 0` / `-k -3` (CLI) and `top_k=0/-1` (rag MCP) print `No results for "<q>" — nothing to draw.` although retrieval was skipped (`top_k … must be >= 1` is only in the log). Misleading, harmless; suggest `click.IntRange(min=1)` on `--top-k` (same as `--max-docs`).
2. graphrag with a nonsense query still draws the 2 junk seed entities (3 nodes / 2 edges), exactly as main does (parity-confirmed); the AC sentence "`nothing_found` … in both modes" is therefore only literally true for rag. The spec table says graphrag keeps today's behaviour, so I treat it as in-spec.
3. Whitespace query: rag MCP strips it to the no-query view; graphrag MCP does NOT (`query="   "` → `Knowledge graph for '   ': 17 nodes, 4 edges`, junk), and the CLI treats `"   "` as a real query in both modes (rag → `No results for "   "`). The graphrag twin is unchanged from main by design; cosmetic inconsistency across surfaces.
4. Pre-existing: unknown / malformed `USER_ID` gives a raw `ValueError` traceback from `resolve_user_id` (not new in this task).
5. Side effect of my own testing, restored: running the CLI with `TREE_MEMORY__MODE=graphrag` against the just-dropped collection re-created the three graphrag-only indexes (`active_user`, `user_kind_source_node`, `user_kind_target_node`) on an empty `memory`; I dropped them, so the final index set is the rag one.
6. The MCP tool itself calls `webbrowser.open` ("Opened it in your browser.") for non-UI clients, so my first MCP server started WITHOUT `BROWSER=true` may have opened 3 tabs on the host (structure, query, whitespace files) before I restarted the server with `BROWSER=true`. Pre-existing tool behaviour, not a defect of this task.
7. Parent-document retrieval of a retrieved parent whose document row is missing yields a view with a child at `doc_rank None` and no `documents` slider — only reachable with corrupt data.

**Cleanup**: workflow serve, MCP servers stopped; all my HTML deleted (`qa173-*`, extra stamps); the SWE's four HTML files kept; scratch DB `qa173_scratch` dropped.

**VERDICT: PASS**

**Tester addendum (same QA round, 2026-10-02 16:30)**
- ALL-children evidence, discriminating form: `make memory-search QUERY="how does memory for ai agents work" TOP_K=2` printed `matched children: 3` for BOTH parents, yet the `-k 2` structure view drew 27 nodes = 1 document + 2 parents + 24 children; `db.memory.countDocuments({parent_id: <pid>})` for that document's two parents = 20 and 4 (all five parents: 18, 14, 19, 20, 4 = 75 children). The scratch `fetch_retrieval_structure` call with a `RetrievedParent` whose `matched_children` was empty still returned both its children.
- Ported-test AC: old `test_query_graph.py` had 17 cases; all survive. Text half → `test_search_memory.py` (one block per parent, 300-char excerpt, no file, `No results.`, degraded caveat first + on empty, no caveat on hybrid, missing `--query` exit 2, both-legs-down line; plus `test_top_k_is_forwarded`, `test_parent_document_retrieval_is_the_only_reader`). Graph half → `test_visualize_structure.py` (`test_query_expands_the_graph_and_renders_it`, `test_without_a_query_it_loads_the_full_graph`, both `max_docs` forwarded/defaults cases, `test_max_docs_below_one_is_a_usage_error` and `test_max_docs_never_reaches_a_query` now in `TestBothModes`, `test_an_empty_graph_exits_one`, `test_one_retryable_line_and_exit_one`). The two old rag refusal cases (`test_without_a_query_it_refuses…`, `test_the_refusal_message_names_the_mode…`) are intentionally retired: rag with no query now draws the tree.
- code-review plugin: `.claude/settings.json` enables `code-review@claude-plugins-official`, but this Tester session has no way to invoke a plugin/skill (no Skill tool), so it was NOT run. It is advisory; I substituted a manual read of the `structure.py`, `visualize_structure.py`, `tools.py`, `graph.py` diffs (no defects found beyond the notes above). The orchestrator may want to run it.
- INSTRUCTION BREACH (mine): my first rag MCP server was started without `BROWSER=true`; three tool calls returned "Opened it in your browser." so up to three `file://` tabs (structure / query / whitespace HTML) may be open on the host, pointing at files I have since deleted. Everything after that restart used `BROWSER=true`.
