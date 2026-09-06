---
id: 118-visualize-package-and-embedding-map-renderer
feature: embedding-clusters-viz
status: done
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

- [x] `find apps/memory/src/tree/memory -maxdepth 1` lists exactly `__init__.py, pipeline.py, embedding_text.py, types.py, rag/, graph/, clustering/, visualize/`; `tree/memory/graph/visualize.py` and `tree/mcp/graph_app.py` do not exist; `grep -rn "tree.memory.graph.visualize\|tree.mcp.graph_app" apps/memory/src apps/memory/tests apps/memory/scripts .agents apps/memory/README.md docs/glossary.md` returns nothing (ADR prose excluded, as in #111).
- [x] `git log --follow --oneline apps/memory/src/tree/memory/visualize/graph.py` and `.../tree/mcp/viz_app.py` show history before this task (moves, not copies).
- [x] Every test in the former `test_visualize.py` / `test_graph_app.py` passes from its new location with only import-path edits; `test_tool_gating.py` passes with `_GRAPH_MODULES == ["tree.mcp.graph_tools", "tree.mcp.dashboard_app"]` and rag still imports neither; graphrag still registers 13 tools and `visualize_memory_graph` still declares the shared `ui://` resource.
- [x] Layout guards: `visualize/` importing `tree.memory.graph` and `rag/` importing `tree.memory.visualize` both turn the layout test red (demonstrated in the log); the mirror test lists `visualize/graph.py` and `visualize/embeddings.py`.
- [x] `to_graph_payload` output is unchanged (existing tests) and rendering it produces HTML WITHOUT the strings `"layout":"fixed"`, `hulls-toggle` shown, or a warning: `_render_graph_file(graph_payload)` HTML contains `forceAtlas2.assign` in a branch guarded by `layout !== "fixed"`.
- [x] `to_embedding_map_payload` on an `EmbeddingMap` with clusters `{0: 30 pts, 1: 12 pts}`, 5 noise points, `unclustered=3`, `total_children=50`: 47 nodes, `edges == []`, `layout == "fixed"`, `nodeSize == 4`; cluster-0 nodes share `CLUSTER_PALETTE[0]`, cluster-1 `CLUSTER_PALETTE[1]`, noise nodes `NOISE_COLOUR`; every node has numeric `x`/`y`, `label == ""`, `meta` keys `{cluster, document, heading_path, snippet}`; `legend == [cluster0 (30), cluster1 (12), noise (5), unclustered (3)]` with the two greys; `warning == "3 of 50 chunks have no cluster assignment (or a stale one) — run make memory-run-clustering-pipeline"`; with `unclustered=0` `warning is None` and no unclustered legend row; `hulls` mirrors the argument; `summary == "Embedding map: 47 chunks in 2 clusters (+5 noise)"`.
- [x] 25 clusters → cluster 20 reuses `CLUSTER_PALETTE[0]` (wrap), never `NOISE_COLOUR`.
- [x] `render_embedding_map_file(payload, tmp_path/"m.html")` writes HTML containing `"layout": "fixed"` (JSON-embedded), the hull-drawing function (`convexHull`), `hulls-toggle`, `graphToViewport`, `afterRender`, the warning text, and every cluster label; `</` inside a label is escaped (existing test pattern); default path matches `embedding-map-\d{8}-\d{6}\.html` under `GRAPHS_DIR`.
- [x] The iframe variant (`viz_app._GRAPH_HTML`) contains the same extension tokens (shared template — asserted by string containment on both variants).
- [x] `grep -n "^from tree.mcp\|^import tree.mcp" apps/memory/src/tree/memory/visualize/*.py` is empty; `grep -n "pymongo\|beanie" apps/memory/src/tree/memory/visualize/*.py` is empty.
- [ ] [HUMAN] Visual check of a rendered file built from a synthetic `EmbeddingMap` (3 clusters × 40 points + 10 noise, `hulls=True`): points sit where their coordinates say (no force layout drift), three coloured hulls are visible, toggling the checkbox hides/shows them, zooming keeps hulls aligned with the points, hovering a point shows cluster label / document / heading path / snippet, the legend lists the clusters with sizes, and the amber warning line appears when `unclustered > 0`. Screenshot path in the log.
- [x] `make memory-format-check && make memory-lint-check && make memory-tests` green; test count ≥ the post-#117 count.

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

### [SWE] 2026-09-06 21:08 — Implementation

**Files modified**

*Moves (`git mv`, 100 % rename similarity — `git diff --cached -M --summary` below)*
- `apps/memory/src/tree/memory/graph/visualize.py` → `apps/memory/src/tree/memory/visualize/graph.py` — the Graph renderer, now in the neutral package; docstring pointers `tree.mcp.graph_app` → `tree.mcp.viz_app` + a paragraph on the four optional Embedding-map keys.
- `apps/memory/src/tree/mcp/graph_app.py` → `apps/memory/src/tree/mcp/viz_app.py` — the MODE-NEUTRAL MCP App layer: `GRAPH_VIEW_URI` (value unchanged), `_EXT_APPS_CDN`, `_graph_tool_result`, `graph_file`, `graph_view`, the iframe template. Docstring rewritten around "no tools of its own"; dropped the now-unused `fetch_full_graph` / `structured_query_memory` / `to_graph_payload` imports.
- `apps/memory/tests/unit/memory/graph/test_visualize.py` → `apps/memory/tests/unit/memory/visualize/test_graph.py`.
- `apps/memory/tests/unit/mcp/test_graph_app.py` → `apps/memory/tests/unit/mcp/test_viz_app.py`.

*New*
- `apps/memory/src/tree/memory/visualize/__init__.py` — package docstring (neutral: never `graph/`, `pipeline.py` or `clustering.store`).
- `apps/memory/src/tree/memory/visualize/embeddings.py` — `CLUSTER_PALETTE` (20 tableau hexes), `NOISE_COLOUR`, `UNCLUSTERED_COLOUR`, `NO_CLUSTERING_RUN_MESSAGE`, `cluster_colour`, `unclustered_warning`, `to_embedding_map_payload`, `render_embedding_map_file`. No Mongo, no MCP.
- `apps/memory/tests/unit/memory/visualize/{__init__.py,test_embeddings.py}` — 27 tests for the payload builder + the map file writer.

*Changed*
- `apps/memory/src/tree/memory/visualize/graph.py` — ONE template extended: `#warning` banner (amber `#fff3bf` / `#f08c00`), `#hulls-toggle` + `<canvas id="hulls-layer">` (pointer-events none, z-index 1), `#legend span.size` right-aligned; `render(payload)` now reads `layout`/`nodeSize`/`legend`/`hulls`/`warning`, skips ForceAtlas2 when `layout === "fixed"`, and draws per-cluster convex hulls (Andrew monotone chain in graph coords → `renderer.graphToViewport`, fill 12 % / stroke 60 % 1.5 px) redrawn on `afterRender` + `resize`. `_render_graph_file` payload type widened to `dict[str, Any]`.
- `apps/memory/src/tree/mcp/graph_tools.py` — `visualize_memory_graph` moved in VERBATIM (next to `query_memory`); imports `GRAPH_VIEW_URI` / `_graph_tool_result` from `tree.mcp.viz_app` and `fetch_full_graph` from `graph.retrieval`; docstring updated (only `dashboard_app` is a graphrag-only side-effect import now).
- `apps/memory/src/tree/mcp/{server.py,dashboard_app.py}`, `apps/memory/scripts/query_graph.py`, `apps/memory/README.md` — import paths / prose pointers rewritten; README module tree gains `clustering/` and `visualize/` and drops `visualize` from the `graph/` line.
- `apps/memory/tests/unit/memory/test_package_layout.py` — top-level set gains `"visualize"`; new `TestVisualizeNeverDependsOnGraphOrStorage` (no `graph/`, no `pipeline`, no `clustering.store`, no `tree.mcp`, no pymongo/beanie/motor; plus `rag/` never imports `visualize/`); mirror test covers `visualize/`.
- `apps/memory/tests/unit/mcp/test_tool_gating.py` — `_GRAPH_MODULES == ["tree.mcp.graph_tools", "tree.mcp.dashboard_app"]` (+ comment on why `viz_app` is deliberately absent).
- `apps/memory/tests/unit/mcp/test_viz_app.py` — helper + `graphs://` + `_GRAPH_HTML` tests kept; the four `visualize_*` tool tests and the shared-`ui://` test moved out; new `ui://`-registration test and the "both variants carry the map extensions" matrix (iframe vs file).
- `apps/memory/tests/unit/mcp/test_graph_tools.py` — received the five moved tests (patch targets retargeted to `tree.mcp.graph_tools.*`; the browser-open patch stays on `tree.mcp.viz_app`, where `_graph_tool_result` lives) + `_seed_result` / `_content_payload` helpers.

**Tests**
- Unit: 2831 passing, 0 failing (`make memory-tests`; post-#117 baseline was 2750 → +81).
- Integration: N/A — this repo has no integration suite; e2e ran the real CLI + real Mongo (below).

**Acceptance criteria**
- [x] Layout / no stale paths — `find apps/memory/src/tree/memory -maxdepth 1` → `__init__.py, pipeline.py, embedding_text.py, types.py, rag/, graph/, clustering/, visualize/`; `grep -rn "tree.memory.graph.visualize\|tree.mcp.graph_app" apps/memory/{src,tests,scripts} .agents apps/memory/README.md docs/glossary.md` → exit 1 (no hits). Also asserted by `tests/unit/memory/test_package_layout.py::TestTopLevelLayout::test_top_level_holds_exactly_the_layers_and_three_modules`.
- [x] `git mv`, not copies — `git diff --cached -M --summary` reports all four renames at 100 %. NOTE for the Tester: `git log --follow` is empty until the commit lands (the new paths have no committed history yet) — check it after the commit, or check the staged rename now.
- [x] Former tests pass from their new homes; gating list updated — `tests/unit/memory/visualize/test_graph.py` (32), `tests/unit/mcp/test_viz_app.py` (30), `tests/unit/mcp/test_graph_tools.py` (20) all green; `tests/unit/mcp/test_tool_gating.py` green with the two-module list; live probe: rag = 6 tools / imports none of the three modules, graphrag = 13 tools incl. `visualize_memory_graph` / imports `viz_app`, `graph_tools`, `dashboard_app`; `test_graph_tools.py::test_all_three_graph_tools_declare_the_shared_ui_resource` asserts the shared `ui://`.
- [x] Layout guards demonstrated red — see Evidence (3 FAILED, one per guard) then restored (146 passed).
- [x] Graph payload/HTML unchanged — `tests/unit/memory/visualize/test_graph.py::test_graph_payload_render_asks_for_no_fixed_layout`, `::test_graph_payload_render_leaves_the_hull_toggle_and_banner_hidden`, `::test_force_atlas_runs_only_outside_the_fixed_layout_branch` (FA2 inside `if (!isFixed)`, `isFixed = payload.layout === "fixed"`), `::test_template_carries_the_hull_overlay_machinery`; live: the rendered `real-graph.html` has 0 occurrences of `"layout": "fixed"` and ships `hulls-toggle" hidden`.
- [x] 47-node payload assertions — `tests/unit/memory/visualize/test_embeddings.py::test_payload_draws_one_fixed_node_per_point`, `::test_payload_colours_nodes_by_cluster_and_noise_grey`, `::test_payload_nodes_carry_coordinates_no_label_and_the_tooltip_meta`, `::test_payload_legend_lists_clusters_by_size_then_the_two_greys`, `::test_payload_carries_the_warning_when_chunks_are_unclustered`, `::test_payload_drops_the_warning_and_the_grey_row_when_nothing_is_stale`, `::test_payload_hulls_mirrors_the_argument`, `::test_payload_summary_counts_chunks_clusters_and_noise`.
- [x] Palette wraps at 20 — `::test_cluster_colour_wraps_the_palette_after_twenty_clusters` (cluster 20 → `CLUSTER_PALETTE[0]`, `NOISE_COLOUR` never in the 25).
- [x] Rendered-file tokens + escaping + default path — `::test_render_embedding_map_file_embeds_the_fixed_layout_and_hull_machinery`, `::test_render_embedding_map_file_carries_the_warning_and_every_cluster_label`, `::test_render_embedding_map_file_escapes_script_close_in_a_label`, `::test_render_embedding_map_file_defaults_to_a_stamped_file_in_graphs_dir` (`embedding-map-\d{8}-\d{6}\.html`).
- [x] Iframe variant shares the tokens — `tests/unit/mcp/test_viz_app.py::test_both_variants_carry_the_embedding_map_extensions` (9 tokens × 2 variants) + `::test_the_iframe_variant_only_adds_the_ext_apps_runtime`.
- [x] Import greps — both greps empty; also enforced by `test_package_layout.py::TestVisualizeNeverDependsOnGraphOrStorage::test_no_visualize_module_imports_the_mcp_layer` / `::test_no_visualize_module_imports_a_mongo_driver`.
- [ ] [HUMAN] Visual check — DONE in a real browser (headless Chrome + CDP-driven mouse/clicks), screenshots saved; awaiting human sign-off. Paths (gitignored, local to this worktree): `.tree/screenshots/task-118/synthetic-01-initial.png` (3 clusters × 40 + 10 noise at their exact coordinates, three hulls, legend `Agent memory design · 40 …`, amber banner `37 of 167 chunks …`), `-02-zoomed.png` (2 × zoom, hulls still glued), `-03-tooltip.png` / `-04-hulls-off.png` (real hover tooltip: `Cluster: Agent memory design`, `Document: Building Agentic Systems`, `Heading_path: Memory > Retrieval`, `Snippet: …`; toggle unticked → hulls cleared), `-05-hulls-on-again.png` (re-ticked → redrawn), plus the REAL local run `real-01-initial.png` … `real-04-hulls-off.png` (1448 chunks, 37 clusters, 142 noise) and `real-graph.png` (the untouched graph view). Source HTML kept beside them.
- [x] `make memory-format-check && make memory-lint-check && make memory-tests` green; 2831 ≥ 2750.

**Evidence**

```
$ make memory-tests
============================ 2831 passed in 51.06s =============================

$ make memory-format-check && make memory-lint-check
285 files already formatted
All checks passed!

$ make pre-commit
prettier / ruff check / ruff format / biome check (harness) ............ Passed

$ git diff --cached -M --summary | grep -i rename
 rename apps/memory/src/tree/mcp/{graph_app.py => viz_app.py} (100%)
 rename apps/memory/src/tree/memory/{graph/visualize.py => visualize/graph.py} (100%)
 rename apps/memory/tests/unit/mcp/{test_graph_app.py => test_viz_app.py} (100%)
 rename apps/memory/tests/unit/memory/{graph/test_visualize.py => visualize/test_graph.py} (100%)

$ find apps/memory/src/tree/memory -maxdepth 1 | sort        # (__pycache__ elided)
apps/memory/src/tree/memory/__init__.py
apps/memory/src/tree/memory/clustering
apps/memory/src/tree/memory/embedding_text.py
apps/memory/src/tree/memory/graph
apps/memory/src/tree/memory/pipeline.py
apps/memory/src/tree/memory/rag
apps/memory/src/tree/memory/types.py
apps/memory/src/tree/memory/visualize

$ grep -rn "tree.memory.graph.visualize\|tree.mcp.graph_app" apps/memory/src apps/memory/tests apps/memory/scripts .agents apps/memory/README.md docs/glossary.md
grep exit=1        # no hits

$ grep -n "^from tree.mcp\|^import tree.mcp" apps/memory/src/tree/memory/visualize/*.py
$ grep -n "pymongo\|beanie" apps/memory/src/tree/memory/visualize/*.py
                   # both empty

# Layout guards, deliberately broken (visualize/embeddings.py += graph.retrieval +
# clustering.store; rag/search.py += visualize.embeddings), then restored:
$ uv run pytest tests/unit/memory/test_package_layout.py -q
FAILED ...::TestVisualizeNeverDependsOnGraphOrStorage::test_no_visualize_module_imports_the_graph_layer[visualize/embeddings.py]
FAILED ...::TestVisualizeNeverDependsOnGraphOrStorage::test_no_visualize_module_imports_the_clustering_store[visualize/embeddings.py]
FAILED ...::TestVisualizeNeverDependsOnGraphOrStorage::test_no_rag_module_imports_the_visualize_layer[rag/search.py]
3 failed, 143 passed in 0.83s
$ # after restoring both files
146 passed in 0.75s

# Mode surfaces (fresh interpreter per mode)
rag:      6 tools; imported: []                    ; visualize_memory_graph registered: False
graphrag: 13 tools; imported: ['tree.mcp.viz_app', 'tree.mcp.graph_tools', 'tree.mcp.dashboard_app']
          visualize_memory_graph registered: True

# E2E 1 — synthetic Embedding map (3 clusters × 40 + 10 noise, hulls=True)
$ uv run --project apps/memory python /tmp/emb-viz/make_synthetic.py
summary: Embedding map: 130 chunks in 3 clusters (+10 noise)
warning: 37 of 167 chunks have no cluster assignment (or a stale one) — run make memory-run-clustering-pipeline
legend: [{'label': 'Agent memory design', 'size': 40, 'color': '#1f77b4'}, …,
         {'label': 'noise', 'size': 10, 'color': '#9e9e9e'},
         {'label': 'unclustered / stale (not shown)', 'size': 37, 'color': '#d5d8de'}]
Wrote self-contained graph HTML (130 nodes, 0 edges) to /tmp/emb-viz/synthetic-map.html
# driven in headless Chrome (CDP): zoom-in ×2, real mouse hover, toggle off/on
tooltip text: Cluster / Agent memory design · Document / Building Agentic Systems ·
              Heading_path / Memory > Retrieval · Snippet / Chunk 9 of Agent memory design: …
checkbox now: false      # hulls cleared, points untouched

# E2E 2 — the REAL local run, read-only via clustering.store.load_embedding_map
$ uv run --project apps/memory --env-file .env python /tmp/emb-viz/real_map.py
Resolved target user: id=6a8ea9579a7aeb13175955c8 identifier=paul@example.com
summary: Embedding map: 1448 chunks in 37 clusters (+142 noise)
warning: None
legend rows: 38
   {'label': 'Frontier Open-Weight Model Architectures', 'size': 100, 'color': '#c49c94'}
   {'label': 'Simulating Human Behavior and Populations', 'size': 96, 'color': '#d62728'}
   …
Wrote self-contained graph HTML (1448 nodes, 0 edges) to /tmp/emb-viz/real-map.html

# E2E 3 — the graph path is untouched (Story 4)
$ uv run python scripts/query_graph.py --user-identifier paul@example.com --query "agent memory" \
      --top-k 5 -o /tmp/emb-viz/real-graph.html --no-open
Graph expansion: 5 seed(s) → 32 nodes, 27 edges (1 hops)
Wrote self-contained graph HTML (32 nodes, 27 edges) to /tmp/emb-viz/real-graph.html
$ grep -c '"layout": "fixed"' /tmp/emb-viz/real-graph.html   → 0
$ grep -o 'hulls-toggle" hidden' /tmp/emb-viz/real-graph.html → hulls-toggle" hidden
# screenshot: force layout, per-type legend, no toggle, no banner.
```

**Notes**
- **Sigma.js 3.0.3 APIs confirmed from the shipped package** (context7 MCP is not exposed in this session; I read the pinned artefacts on jsdelivr instead — the same version the template loads): `graphToViewport(graphPoint, override?)` at `dist/declarations/src/sigma.d.ts:170`; `afterRender(): void` and `resize(): void` in `SigmaAdditionalEvents` (`types.d.ts`), with `resize` emitted from `Sigma#resize()` which Sigma's own `window resize` handler schedules — so `renderer.on("afterRender"|"resize", …)` is the supported pair; `applyNodeDefaults` THROWS `"All your nodes must have a number x and y"`, confirming a fixed layout must supply `x`/`y` (and that Sigma applies no layout of its own, so skipping FA2 leaves the stored coordinates exactly as they are); `if (!data.label && data.label !== "") data.label = null` — an empty-string label survives the reducer and draws no canvas text, which is what the map relies on.
- **`git log --follow` caveat** (see AC 2): with the work uncommitted, `--follow` on the new paths prints nothing. The staged renames are 100 %; run the AC's command after the commit.
- **Judgement calls**
  - `cluster_colour` indexes `CLUSTER_PALETTE[cluster_id % 20]`. ADR-007 §2 says "by cluster rank (`% 20`)" and the ACs pin cluster 0 → `[0]`, 1 → `[1]`, 20 → `[0]` — id-indexed is the only reading that satisfies both, and HDBSCAN already numbers clusters in a stable order.
  - The FA2 guard is `const isFixed = payload.layout === "fixed"` + `if (!isFixed) {`, not a literal `payload.layout !== "fixed"` in the `if` — one constant, three uses. The comment on the guard names `layout !== "fixed"` so a grep still finds it; the test asserts both halves.
  - The map's header still reads `1448 nodes · 0 edges` — the task's template list does not mention re-wording `#counts` for maps, so I left it alone. `payload.summary` is built (and asserted) for #119's model-visible text.
  - Hulls are computed ONCE per cluster in graph coordinates and only re-projected per frame — O(n log n) at load, O(hull vertices) per render. Dragging a node (still enabled) does not recompute its cluster's hull; on a fixed map that is a cosmetic edge case, not worth a per-frame rebuild over 1448 points.
  - The hull canvas is sized in device pixels (`devicePixelRatio` + `setTransform`) so 1.5 px strokes stay crisp on retina.
  - README's module tree also gained a `clustering/` line (it was missing since #116/#117) — one line, adjacent to the `visualize/` line this task required.
- **Not done here (by design):** no `visualize_memory_embeddings` MCP tool, no CLI, no Makefile target — those are #119; `rag` mode imports nothing from `viz_app` in this task (verified live above).

### [Tester] 2026-09-06 22:40 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check`, `make memory-lint-check`, `make pre-commit` all green)
- Unit tests: 2831 passed / 0 failed (`make memory-tests`; ≥ baseline 2750)
- Integration tests: N/A (repo has no integration suite, per AGENTS.md)
- Warnings: 0

**E2E adversarial pass**
- Happy path: rendered a synthetic `EmbeddingMap` (3 clusters × 40 + 10 noise, `hulls=True`) via `to_embedding_map_payload` + `render_embedding_map_file`, opened in headless Chrome (puppeteer-core, CDP) → points sit at stored coordinates, 3 coloured hulls glued to their clusters, amber banner `5 of 135 chunks have no cluster assignment…`, legend lists clusters + noise + unclustered, hull toggle checked. PASS
- Break path 1 (state edge: hull toggle off→on, zoom, resize): toggled `#hulls` off (canvas cleared to 0 non-transparent pixels) then on again (11372 px redrawn); zoomed in via mouse-wheel over the container — hull vertices stayed pixel-glued to the outer cluster points at every zoom level (visually verified: `tester-zoom-01-gentle-zoom.png`, `tester-zoom-02-more-zoom.png`); resized the browser viewport 1400×900 → 900×650 — hulls redrew (`renderer.on("resize", drawHulls)` fires) with no console errors. PASS
- Break path 2 (boundary: degenerate/tiny clusters): a cluster with exactly 2 points renders with no hull and no crash (`byCluster` filter requires ≥3 points before `convexHull` even runs); a 3-point COLLINEAR cluster renders with no hull and no crash (Andrew's monotone chain degenerates to <3 output points, filtered out by `h.hull.length >= 3`). Both fixtures loaded with 0 console errors. PASS
- Break path 3 (hostile input: `<script>` in a cluster label and a chunk title): built a payload with `label = "<script>alert('legend')</script>"` and `title = "<script>alert(1)</script>"`. Rendered HTML: the only unescaped `</script>` in the file is the real closing tag of the module `<script>`; every occurrence from the payload is `<\/script>` in the embedded JSON. In the live DOM the legend row and the tooltip both show the literal text via `esc()` (`&lt;script&gt;...`) — no `alert()` dialog fired, 0 console errors. PASS
- Break path 4 (scale: 5000 points / 15 clusters): loaded in 2997 ms end-to-end (`networkidle0` + settle), rendered "5000 nodes · 0 edges", 15 distinct palette colours + grey noise, 0 console errors. PASS
- Break path 5 (payload shape: `hulls` absent / `false` / `true`): absent → toggle `hidden=true`; `false` → toggle visible, checkbox unchecked; `true` → toggle visible, checkbox checked. Matches the "presence shows the toggle, value sets initial state" contract exactly. PASS
- Break path 6 (state edge: 0 clusters, only noise): `EmbeddingMap(clusters=[], points=8 noise)` → legend shows only `noise · 8`, no hulls (noise never gets one), summary `"Embedding map: 8 chunks in 0 clusters (+8 noise)"`. PASS
- Layout-guard sabotage (repo-level adversarial check, not payload-level): added `from tree.memory.graph.retrieval import fetch_full_graph` + `from tree.memory.clustering.store import load_embedding_map` to `visualize/embeddings.py`, and `from tree.memory.visualize.embeddings import cluster_colour` to `rag/search.py` → `pytest tests/unit/memory/test_package_layout.py` went red with exactly the 3 failures the SWE reported (`test_no_visualize_module_imports_the_graph_layer`, `test_no_visualize_module_imports_the_clustering_store`, `test_no_rag_module_imports_the_visualize_layer`); reverted from backup, re-ran → 146 passed. PASS

**Acceptance criteria**
- [x] PASS — layered rename, no stale paths — `find apps/memory/src/tree/memory -maxdepth 1` → `__init__.py, pipeline.py, embedding_text.py, types.py, rag/, graph/, clustering/, visualize/`; `apps/memory/src/tree/memory/graph/visualize.py` and `apps/memory/src/tree/mcp/graph_app.py` confirmed absent (`ls` → No such file); `grep -rn "tree.memory.graph.visualize\|tree.mcp.graph_app" apps/memory/src apps/memory/tests apps/memory/scripts .agents apps/memory/README.md docs/glossary.md` → no hits (exit 1)
- [x] PASS — `git mv`, not copies — `git diff --cached -M --summary` (staged) reports all four renames at 100%: `graph_app.py => viz_app.py`, `graph/visualize.py => visualize/graph.py`, `test_graph_app.py => test_viz_app.py`, `graph/test_visualize.py => visualize/test_graph.py`. (`git log --follow` will show history once committed, per the SWE's noted caveat — the rename itself is verified via the staged index now.)
- [x] PASS — moved tests pass; gating list updated — full `make memory-tests` green (2831); live-probed rag mode = 6 tools, `imported=[]`; graphrag mode = 13 tools incl. `visualize_memory_graph`, `imported=['tree.mcp.viz_app', 'tree.mcp.graph_tools', 'tree.mcp.dashboard_app']`; `tests/unit/mcp/test_tool_gating.py` 23/23 passed with `_GRAPH_MODULES == ["tree.mcp.graph_tools", "tree.mcp.dashboard_app"]`
- [x] PASS — layout guards go red under both sabotages — demonstrated myself (see E2E pass above); mirror test covers `visualize/graph.py` and `visualize/embeddings.py` (`TestTestsMirrorTheModules::test_module_has_a_mirroring_test_module`)
- [x] PASS — `to_graph_payload` output unchanged, rendered graph HTML has no fixed layout / hull toggle shown / warning — `tests/unit/memory/visualize/test_graph.py::test_graph_payload_render_asks_for_no_fixed_layout` etc. all green; confirmed `visualize_query_result`/`_render_graph_file` code path guards FA2 behind `if (!isFixed)` at `apps/memory/src/tree/memory/visualize/graph.py:475`; SWE's real 32-node graph render (`.tree/screenshots/task-118/real-graph.png`) shows force layout, per-type legend, no toggle, no banner — visually confirmed
- [x] PASS — 47-node payload assertions literal — read `tests/unit/memory/visualize/test_embeddings.py`; every literal from the AC (colours by `CLUSTER_PALETTE[0]`/`[1]`, `NOISE_COLOUR`, legend order + two greys, warning string, `hulls` mirrors argument, `summary` string) is asserted verbatim and passes
- [x] PASS — palette wraps at 20, `NOISE_COLOUR` never reused — `test_cluster_colour_wraps_the_palette_after_twenty_clusters` asserts `colours[20] == CLUSTER_PALETTE[0]` and `NOISE_COLOUR not in colours` across 25 ids
- [x] PASS — rendered-file token assertions — `test_render_embedding_map_file_embeds_the_fixed_layout_and_hull_machinery` / `_carries_the_warning_and_every_cluster_label` / `_escapes_script_close_in_a_label` / `_defaults_to_a_stamped_file_in_graphs_dir` all green; independently re-verified escaping and token presence on my own fixtures (see E2E break path 3)
- [x] PASS — iframe variant shares the tokens — `test_both_variants_carry_the_embedding_map_extensions` (9 tokens × 2 variants) + `test_the_iframe_variant_only_adds_the_ext_apps_runtime` green
- [x] PASS — no `tree.mcp` / pymongo / beanie imports in `visualize/` — both greps empty, confirmed myself; also enforced by `TestVisualizeNeverDependsOnGraphOrStorage::test_no_visualize_module_imports_the_mcp_layer` / `::test_no_visualize_module_imports_a_mongo_driver`
- [ ] [HUMAN] Visual check — awaiting human sign-off. SWE's screenshots reviewed (`.tree/screenshots/task-118/real-01-initial.png`, `real-graph.png` etc.) and match the spec; my own independent browser-driven screenshots saved under `.tree/screenshots/task-118-tester/` (gitignored, 19 files: happy path, hull toggle on/off/zoom/resize, hover tooltips on a cluster point and a noise point, two-point cluster, collinear cluster, XSS legend/tooltip, noise-only map, 5000-point map, hulls absent/false/true)
- [x] PASS — `make memory-format-check && make memory-lint-check && make memory-tests` green; 2831 ≥ 2750

**Judgement call — `cluster_colour` id-indexed vs rank-indexed (SWE call 1)**
Confirmed: `sklearn.cluster.HDBSCAN` (used in `clustering/core.py`) always returns dense, consecutive non-negative labels `0..k-1` for the clusters it finds (noise is `-1`), never sparse ids. Under that guarantee "indexed by cluster rank (`% 20`)" (ADR-007 §2 wording) and "indexed by `cluster_id % 20`" (the SWE's implementation) are the same function — there is no real-pipeline input where they diverge. A hand-built fixture COULD pass sparse ids (e.g. `{0, 5, 12}`) to `to_embedding_map_payload` since `MemoryClusterInfo.cluster_id` has no density constraint in `clustering/types.py`; in that hypothetical, id-indexing would not equal size/discovery-rank-indexing. This is out of scope for #118 (the store, #117, is what actually produces `EmbeddingMap`, and it reads HDBSCAN's dense labels verbatim) — flagging per the review brief, not blocking.

**Other issues found**
- Legend rows have no hover/tooltip interaction (confirmed by direct test: hovering the "unclustered / stale (not shown)" row produces no tooltip and no console error) — not required by any AC or User Story (the story only specifies hovering a *dot*), so not a defect; noting for completeness since the review brief asked me to probe it.
- `render_embedding_map_file(payload, output)` takes `output: Path | None` per its signature; passing a plain `str` raises an unhelpful `AttributeError: 'str' object has no attribute 'parent'` instead of a clear type error (found while building my own fixtures). Not a spec violation — the function is typed correctly and is an internal API only called with `Path`/`None` today — but worth a defensive `Path(output)` coercion if this is ever exposed as a public/CLI-facing seam in #119.
- `#counts` still reads "N nodes · 0 edges" on a map (SWE's own judgement call 3) — cosmetic, out of scope per the task's template list, `payload.summary` exists for #119 to surface properly.

**VERDICT: PASS**
