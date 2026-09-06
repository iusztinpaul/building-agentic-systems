---
id: 118-visualize-package-and-embedding-map-renderer
feature: embedding-clusters-viz
status: pending
---

# `tree/memory/visualize/` package (renderer move) + `tree/mcp/viz_app.py` split + embedding-map payload and template extension (fixed coordinates, palette legend, hull toggle, warning)

Tags: `memory`, `visualize`, `mcp`, `refactor`
Depends on: #116
Blocks: #119
Implements: ADR-007 — Decisions 2 (display), 7 (package layout); amends ADR-005 Decision 3 (module paths)

## Scope

**1. Renderer move (pure `git mv` + import rewrites, no behaviour change).**
`tree/memory/graph/visualize.py` → `tree/memory/visualize/graph.py` (+ `visualize/__init__.py`
docstring). Rewrite every import site (`tree/mcp/graph_tools.py`, `dashboard_app.py`, the MCP
app module, `scripts/query_graph.py`, tests, `apps/memory/README.md`, `.agents/skills/*`).
Tests move to `tests/unit/memory/visualize/test_graph.py`. Rule (enforced in
`test_package_layout.py`): `visualize/` never imports `tree.memory.graph*`, `tree.memory.pipeline`
or `tree.memory.clustering.store`; `graph/` may import `visualize/`; `rag/` never imports
`visualize/`; top-level set gains `"visualize"`; mirror test covers it.

**2. MCP-layer split (pure move).** `tree/mcp/graph_app.py` → `tree/mcp/viz_app.py`, the
MODE-NEUTRAL MCP App layer: `GRAPH_VIEW_URI` (unchanged value `ui://tree-memory/graph.html`),
`_EXT_APPS_CDN`, `_graph_tool_result`, the `graph_file` (`graphs://{name}`) and `graph_view`
(`ui://`) resources, the iframe template. The `visualize_memory_graph` TOOL moves verbatim into
`tree/mcp/graph_tools.py` (graphrag-only, next to `query_memory`), which drops its
`from tree.mcp import graph_app` side-effect import. `tree/mcp/graph_app.py` no longer exists.
`test_tool_gating.py::_GRAPH_MODULES` becomes `["tree.mcp.graph_tools", "tree.mcp.dashboard_app"]`;
`tests/unit/mcp/test_graph_app.py` → `test_viz_app.py` (helper + resources) and the four
`visualize_*` tool tests move into `test_graph_tools.py`. In THIS task rag mode still imports
nothing from `viz_app` (the both-modes tool arrives in #119).

**3. Template extension in `visualize/graph.py`** (ONE template, shared by the file and iframe
variants through `_resolve_static`; the graph path renders byte-for-byte as before when the new
payload keys are absent):
- **Fixed coordinates:** in `_RENDER_JS`, when `payload.layout === "fixed"` every node uses its
  `x`/`y` and ForceAtlas2 is skipped; otherwise today's random start + FA2.
- **Legend:** when `payload.legend` is an array, render its rows (`swatch`, `label`, `size` right-
  aligned, e.g. `Agent memory design · 84`) in the given order instead of the per-type legend.
- **Warning banner:** new `<div id="warning" hidden>` in `#header`; shown with `payload.warning`
  text when present (amber background `#fff3bf`, border `#f08c00`).
- **Hull toggle + overlay:** new `<label id="hulls-toggle" hidden><input type="checkbox"
  id="hulls"> Cluster hulls</label>` in `#header` and a `<canvas id="hulls-layer">` absolutely
  positioned over `#sigma-container` (`pointer-events: none`, `z-index: 1`). Shown only when
  `payload.legend` exists and `payload.hulls` is a boolean (initial checked state = its value).
  Drawing: for each `cluster_id >= 0` with ≥ 3 points, the convex hull (Andrew monotone chain,
  in graph coordinates) of its nodes, mapped through `renderer.graphToViewport`, filled with the
  cluster colour at 12 % alpha + 1.5 px stroke at 60 %; redrawn on `afterRender` and `resize`,
  cleared when unchecked. Noise (`-1`) never gets a hull.
- Tooltip unchanged in mechanism (`name` + `meta` rows); labels: nodes with `label: ""` draw no
  canvas text (already supported by the reducer).
- Node size: `payload.nodeSize` (number) overrides the default 6 (the map uses 4).

**4. `tree/memory/visualize/embeddings.py`** — the **Embedding map** payload builder:
- `CLUSTER_PALETTE: tuple[str, ...]` — 20 distinct tableau-style hexes (`#1f77b4 #aec7e8 #ff7f0e
  #ffbb78 #2ca02c #98df8a #d62728 #ff9896 #9467bd #c5b0d5 #8c564b #c49c94 #e377c2 #f7b6d2
  #bcbd22 #dbdb8d #17becf #9edae5 #393b79 #e7ba52`), `NOISE_COLOUR = "#9e9e9e"`,
  `UNCLUSTERED_COLOUR = "#d5d8de"`; `cluster_colour(cluster_id) -> str` (`-1` → noise; else
  palette by cluster rank order, `% 20`).
- `NO_CLUSTERING_RUN_MESSAGE = "No clustering run found for this user — run make
  memory-run-clustering-pipeline to build the embedding map."`
- `unclustered_warning(embedding_map) -> str | None` → `f"{unclustered} of {total_children}
  chunks have no cluster assignment (or a stale one) — run make memory-run-clustering-pipeline"`
  when `unclustered > 0`, else `None`.
- `to_embedding_map_payload(embedding_map: EmbeddingMap, *, hulls: bool = False) -> dict`:
  `nodes` = one per `MapPoint` (`id=chunk_id`, `type="chunk"`, `name=<title or "(untitled)">`,
  `label=""`, `x`, `y`, `cluster_id`, `color=cluster_colour(...)`, `meta={"cluster": <label or
  "noise">, "document": title, "heading_path": " > ".join(...), "snippet": ...}`), `edges=[]`,
  `layout="fixed"`, `nodeSize=4`, `hulls=hulls`, `legend` = one row per cluster (label + size,
  size desc) + `{"label": "noise", "size": n, "color": NOISE_COLOUR}` when n > 0 +
  `{"label": "unclustered / stale (not shown)", "size": unclustered, "color":
  UNCLUSTERED_COLOUR}` when unclustered > 0, `warning=unclustered_warning(...)`,
  `summary=f"Embedding map: {len(points)} chunks in {k} clusters (+{noise} noise)"`.
- `render_embedding_map_file(payload, output=None) -> Path` → `_render_graph_file(payload,
  query="embedding-map", output=output)` (file `.tree/graphs/embedding-map-<UTC-stamp>.html`).
- No Mongo, no MCP imports (dependency direction memory ← mcp holds).

## Acceptance Criteria

- [ ] `find apps/memory/src/tree/memory -maxdepth 1` lists exactly `__init__.py, pipeline.py, embedding_text.py, types.py, rag/, graph/, clustering/, visualize/`; `tree/memory/graph/visualize.py` and `tree/mcp/graph_app.py` do not exist; `grep -rn "tree.memory.graph.visualize\|tree.mcp.graph_app" apps/memory/src apps/memory/tests apps/memory/scripts .agents apps/memory/README.md docs/glossary.md` returns nothing (ADR prose excluded, as in #111).
- [ ] `git log --follow --oneline apps/memory/src/tree/memory/visualize/graph.py` and `.../tree/mcp/viz_app.py` show history before this task (moves, not copies).
- [ ] Every test in the former `test_visualize.py` / `test_graph_app.py` passes from its new location with only import-path edits; `test_tool_gating.py` passes with `_GRAPH_MODULES == ["tree.mcp.graph_tools", "tree.mcp.dashboard_app"]` and rag still imports neither; graphrag still registers 13 tools and `visualize_memory_graph` still declares the shared `ui://` resource.
- [ ] Layout guards: `visualize/` importing `tree.memory.graph` and `rag/` importing `tree.memory.visualize` both turn the layout test red (demonstrated in the log); the mirror test lists `visualize/graph.py` and `visualize/embeddings.py`.
- [ ] `to_graph_payload` output is unchanged (existing tests) and rendering it produces HTML WITHOUT the strings `"layout":"fixed"`, `hulls-toggle` shown, or a warning: `_render_graph_file(graph_payload)` HTML contains `forceAtlas2.assign` in a branch guarded by `layout !== "fixed"`.
- [ ] `to_embedding_map_payload` on an `EmbeddingMap` with clusters `{0: 30 pts, 1: 12 pts}`, 5 noise points, `unclustered=3`, `total_children=50`: 47 nodes, `edges == []`, `layout == "fixed"`, `nodeSize == 4`; cluster-0 nodes share `CLUSTER_PALETTE[0]`, cluster-1 `CLUSTER_PALETTE[1]`, noise nodes `NOISE_COLOUR`; every node has numeric `x`/`y`, `label == ""`, `meta` keys `{cluster, document, heading_path, snippet}`; `legend == [cluster0 (30), cluster1 (12), noise (5), unclustered (3)]` with the two greys; `warning == "3 of 50 chunks have no cluster assignment (or a stale one) — run make memory-run-clustering-pipeline"`; with `unclustered=0` `warning is None` and no unclustered legend row; `hulls` mirrors the argument; `summary == "Embedding map: 47 chunks in 2 clusters (+5 noise)"`.
- [ ] 25 clusters → cluster 20 reuses `CLUSTER_PALETTE[0]` (wrap), never `NOISE_COLOUR`.
- [ ] `render_embedding_map_file(payload, tmp_path/"m.html")` writes HTML containing `"layout": "fixed"` (JSON-embedded), the hull-drawing function (`convexHull`), `hulls-toggle`, `graphToViewport`, `afterRender`, the warning text, and every cluster label; `</` inside a label is escaped (existing test pattern); default path matches `embedding-map-\d{8}-\d{6}\.html` under `GRAPHS_DIR`.
- [ ] The iframe variant (`viz_app._GRAPH_HTML`) contains the same extension tokens (shared template — asserted by string containment on both variants).
- [ ] `grep -n "^from tree.mcp\|^import tree.mcp" apps/memory/src/tree/memory/visualize/*.py` is empty; `grep -n "pymongo\|beanie" apps/memory/src/tree/memory/visualize/*.py` is empty.
- [ ] [HUMAN] Visual check of a rendered file built from a synthetic `EmbeddingMap` (3 clusters × 40 points + 10 noise, `hulls=True`): points sit where their coordinates say (no force layout drift), three coloured hulls are visible, toggling the checkbox hides/shows them, zooming keeps hulls aligned with the points, hovering a point shows cluster label / document / heading path / snippet, the legend lists the clusters with sizes, and the amber warning line appears when `unclustered > 0`. Screenshot path in the log.
- [ ] `make memory-format-check && make memory-lint-check && make memory-tests` green; test count ≥ the post-#117 count.

## User Stories

### Story: Reader finds the renderer in a neutral home
1. Reader opens `tree/memory/visualize/` and sees `graph.py` (the Sigma stack + templates) and `embeddings.py` (the map payload) — no import from `graph/`.
2. Reader opens `tree/memory/graph/` and finds no rendering code; `graph/retrieval.py` and `mcp/graph_tools.py` import the renderer from `visualize/`.

### Story: Book reader opens the embedding map HTML
1. The reader opens `.tree/graphs/embedding-map-20260906-101500.html` (built in #119).
2. Each dot is a child chunk placed by its stored coordinates and coloured by cluster; the legend on the right reads `Agent memory design · 84`, `Prompt engineering · 41`, …, `noise · 63`.
3. Ticking "Cluster hulls" draws a translucent convex hull around each cluster; zooming with the wheel keeps hulls glued to their points; hovering a dot shows `Agent memory design`, the document title, `Memory > Retrieval`, and the first 160 characters of the chunk.

### Story: Stale map warns the reader
1. The map is rendered after two new documents were ingested since the last clustering run.
2. An amber banner at the top reads `37 of 449 chunks have no cluster assignment (or a stale one) — run make memory-run-clustering-pipeline`, and the legend ends with `unclustered / stale (not shown) · 37`.

### Story: Graph rendering is untouched
1. Operator runs `make memory-query-graph` in graphrag.
2. The graph HTML looks and behaves exactly as before: force layout, per-type legend, no hull toggle, no banner.

## Out of scope

- The MCP tool and the CLI (#119); reading from Mongo (store is #117).
- 3D, a second JS library, vendoring the CDN bundles (ADR-005 stands).
- Hulls for noise or for clusters with fewer than 3 points; concave hulls.

---

Blocked by: #116

## Log
