---
id: 119-embedding-map-mcp-tool-cli-docs-e2e
feature: embedding-clusters-viz
status: pending
---

# `visualize_memory_embeddings` MCP tool (both modes) + `make memory-visualize-embeddings` CLI + docs/skills + live e2e

Tags: `mcp`, `scripts`, `docs`, `e2e`
Depends on: #117, #118
Blocks: —
Implements: ADR-007 — Decision 8 (surfaces + warning contract)

## Scope

**1. MCP tool** — in `tree/mcp/tools.py` (registered in BOTH modes; READS only, never computes):
```python
@mcp.tool(app=AppConfig(resource_uri=GRAPH_VIEW_URI))
@track(tags=TAGS_RETRIEVAL_MCP, name="visualize_memory_embeddings", create_duplicate_root_span=False)
async def visualize_memory_embeddings(ctx: Context, hulls: bool = False, as_html_file: bool = False) -> str | ToolResult
```
Docstring (model-facing): "Show the memory's embedding space as a 2D map: every child chunk is a
point, coloured by its cluster from the latest clustering run, with an LLM-written label per
cluster. Use when the user wants to *see* what topics the memory holds or how it is organised.
`hulls=true` outlines each cluster. If no clustering run exists, this returns a message telling
the operator which command to run; if the map is stale, the answer starts with a warning line."
Body: `_set_retrieval_thread(ctx, "visualize_memory_embeddings")` → `load_embedding_map(lc["client"],
lc["database"], lc["user_id"])`; `None` → return `NO_CLUSTERING_RUN_MESSAGE` (plain `str`); else
`payload = to_embedding_map_payload(map, hulls=hulls)`; `summary = payload["summary"]`, prefixed by
`payload["warning"] + "\n"` when present; `return _graph_tool_result(ctx, payload, summary,
query="embedding-map", as_html_file=as_html_file)` (dual delivery identical to the graph).
`tools.py` imports `GRAPH_VIEW_URI, _graph_tool_result` from `tree.mcp.viz_app` (this registers
the `ui://` + `graphs://` resources in rag mode too). Rag mode must still NOT import
`tree.mcp.graph_tools` / `dashboard_app`, nor `umap`/`sklearn`. `mcp.instructions` (both texts)
mention the tool in one sentence. `viz_app.graph_view`'s docstring → "Interactive Sigma.js viewer
for knowledge graphs and embedding maps (read-only)".

**2. CLI (glue only)** — `scripts/visualize_embeddings.py` (`init_logger()`, `user_options`,
`--hulls/--no-hulls`, `--output/-o`, `--no-open`): connects (`init_mongodb`), resolves the user,
`load_embedding_map`; `None` → `click.echo(NO_CLUSTERING_RUN_MESSAGE)` + exit 1; else prints the
warning line FIRST when present, then `render_embedding_map_file(payload, output)` and best-effort
`webbrowser.open` unless `--no-open`, prints `Wrote <path>` and the summary line.
`apps/memory/Makefile`: `visualize-embeddings` target (`HULLS=true`, `OUTPUT=…`, `USER_ID` /
`USER_IDENTIFIER`).

**3. Docs & skills:**
- `apps/memory/README.md`: "Embedding map" subsection under Query CLI (command, what it shows,
  the warning contract, the no-run message), "Tools exposed" tables → both modes 7 / graphrag 14
  with `visualize_memory_embeddings` described, Layout block (`clustering/`, `visualize/`,
  `mcp/viz_app.py`), "Memory clustering" section cross-link.
- Repo-root `README.md` pipeline comment gains `→ (optional) cluster child embeddings → embedding map`.
- `.agents/skills/tree-memory/SKILL.md`: tool table (both modes) + "Visualization" section: use
  `visualize_memory_embeddings` for "what topics are in my memory / show me a map" in EITHER
  mode; graph visualisation stays graphrag-only; relay the warning line verbatim and the
  `make memory-run-clustering-pipeline` hint.
- `.agents/skills/run-pipelines-e2e/SKILL.md`: step 2 adds `make memory-run-clustering-pipeline`
  (after indexing; note the ~40 s cold numba import on a fresh env, and
  `TREE_MEMORY__CLUSTERING__HDBSCAN__MIN_CLUSTER_SIZE=5` for a small local corpus); step 3 adds
  `make memory-visualize-embeddings` + the MCP tool, and the stale-warning check.
- `docs/notes/prefect-execution-topologies.md`: the four phases of `offline-pipeline`.
- Verify code identifiers match the glossary (**Memory cluster**, **Embedding map**,
  **Clustering run**, **Offline phase**): `MemoryCluster`, `memory_clusters`, `EmbeddingMap`,
  `run_id`, `run_clustering`.

**4. Live e2e (per `.agents/skills/run-pipelines-e2e/SKILL.md`, local env, "Paul Iusztin" user,
served FROM THIS WORKTREE, evidence in `## Log`)** — in ONE mode (rag; clustering is
mode-orthogonal) plus a graphrag smoke of the tool count:
1. Ingest enough for ≥ 2 clusters (e.g. `make memory-run-pipeline SOURCE_FILE=sources/listen.yaml`
   or 4–5 `MODE=online` articles) — or export `TREE_MEMORY__CLUSTERING__HDBSCAN__MIN_CLUSTER_SIZE=5`
   in the serving shell; record which.
2. `make memory-run-clustering-pipeline` → paste the summary log line and `mongosh` output:
   `memory_clusters` rows (`label`, `size`, `keywords`) and one child row's `cluster_id`/`viz`.
3. `make memory-visualize-embeddings HULLS=true` → file path + screenshot; no warning line.
4. `make memory-serve-mcp TRANSPORT=streamable-http` → `uv run fastmcp call … visualize_memory_embeddings hulls=true` → paste the tool result (file path + `graphs://` resource link; the summary text).
5. Ingest ONE new document (`make memory-run-pipeline MODE=online SOURCE=<url>`), re-run steps 3
   and 4 → both outputs START with `N of M chunks have no cluster assignment (or a stale one) —
   run make memory-run-clustering-pipeline`; the legend shows the unclustered row.
6. Drop `memory_clusters` and clear the chunk fields (`db.memory.updateMany({}, {$unset:
   {cluster_id: "", viz: ""}})`) → CLI exits 1 with the no-run message; tool returns the same text.
7. graphrag: boot the MCP server without the override → `fastmcp list` shows 14 tools incl.
   `visualize_memory_embeddings`.

## Acceptance Criteria

- [ ] `test_tool_gating.py`: rag registers exactly the 7 tools `ingest_conversation, ingest_file, ingest_url, scrape_web, search_memory, search_web, visualize_memory_embeddings`; graphrag registers 14; rag's `sys.modules` contains `tree.mcp.viz_app` but none of `tree.mcp.graph_tools`, `tree.mcp.dashboard_app`, `umap`, `sklearn`; the tool's schema properties are exactly `{hulls, as_html_file}`.
- [ ] Both mode instruction texts mention `visualize_memory_embeddings`; the rag text still does not mention `query_memory` / `deep_search_memory`.
- [ ] Unit tests (`test_tools.py`): with `load_embedding_map` patched to `None` the tool returns `NO_CLUSTERING_RUN_MESSAGE` as a plain `str` and `_graph_tool_result` is NOT called; with a map (`unclustered=0`) and a UI-capable ctx the result is a `ToolResult` whose model-visible text starts with `Embedding map:` and whose `audience=["user"]` block JSON has `layout == "fixed"` and `hulls == True` when `hulls=True`; with `unclustered=2` the model-visible text STARTS with `2 of 12 chunks have no cluster assignment`; with a non-UI ctx the result carries a `graphs://embedding-map-…html` resource link; `as_html_file=True` forces the file branch.
- [ ] `tests/unit/scripts/test_visualize_embeddings.py` (pattern of `test_query_graph.py`): no run → prints `NO_CLUSTERING_RUN_MESSAGE`, exit code 1, no file written; a run with `unclustered=3` → the FIRST stdout line is the warning, `render_embedding_map_file` is called with `hulls` forwarded, `--output` honoured, `--no-open` skips `webbrowser.open`.
- [ ] `grep -n "visualize-embeddings" apps/memory/Makefile` shows the target with `HULLS`/`OUTPUT` wiring; `make memory-help` lists it.
- [ ] `apps/memory/README.md` contains `visualize_memory_embeddings`, the strings `7 tools` and `14 total` (or the tables' equivalents), an `Embedding map` heading, and `have no cluster assignment`; `.agents/skills/tree-memory/SKILL.md` lists the tool under both modes; `.agents/skills/run-pipelines-e2e/SKILL.md` mentions `run-clustering-pipeline` and `visualize-embeddings`.
- [ ] `grep -rn "graph_app" apps/memory .agents docs/glossary.md README.md` returns nothing (ADR prose excluded).
- [ ] `make memory-format-check && make memory-lint-check && make memory-tests` green; test count ≥ post-#118.
- [ ] [HUMAN] e2e evidence steps 1–4 in `## Log`: clustering summary line; `mongosh` `memory_clusters` rows with real labels; the CLI file path + screenshot with hulls; the MCP tool result text.
- [ ] [HUMAN] e2e evidence step 5: CLI and MCP outputs both beginning with the warning line after ingesting one new document; legend shows the unclustered count.
- [ ] [HUMAN] e2e evidence steps 6–7: the no-run message from both surfaces; graphrag `fastmcp list` → 14 tools.

## User Stories

### Story: Reader asks the assistant what their memory is about
1. In Claude Code (rag mode server), the user asks "show me a map of what's in my memory".
2. The `tree-memory` skill picks `visualize_memory_embeddings`; the client cannot render MCP Apps, so the answer is `Embedding map: 412 chunks in 7 clusters (+63 noise). Since this client does not render inline MCP App UIs, I saved a self-contained interactive graph to: …/.tree/graphs/embedding-map-20260906-101500.html …` plus a `graphs://` link.
3. Opening the file shows the coloured map with the legend `Agent memory design · 84`, …

### Story: Same question in a UI-capable client
1. The user asks the same in a client supporting MCP Apps, adding "outline the clusters".
2. The agent calls `visualize_memory_embeddings(hulls=true)`; the map renders inline in an iframe with hulls on; the model only sees the summary line.

### Story: The map is stale
1. Two days after clustering, the user has ingested more articles and asks for the map.
2. The tool answer starts with `37 of 449 chunks have no cluster assignment (or a stale one) — run make memory-run-clustering-pipeline`; the agent relays that line and offers to run the command.

### Story: Nobody has clustered yet
1. A fresh user calls the tool (or `make memory-visualize-embeddings`).
2. They get `No clustering run found for this user — run make memory-run-clustering-pipeline to build the embedding map.` — no empty canvas, and the CLI exits 1.

### Story: Operator renders from the terminal with hulls
1. Operator runs `make memory-visualize-embeddings HULLS=true USER_IDENTIFIER=paul`.
2. The terminal prints `Wrote …/embedding-map-<stamp>.html` then `Embedding map: 412 chunks in 7 clusters (+63 noise)`; the browser opens on the map with hulls drawn.

## Out of scope

- Computing clusters from the tool or CLI (they read only).
- Harness changes; a new deployment; run comparison.

---

Blocked by: #117, #118

## Log
