---
id: 162-d3-force-live-layout-and-pin-on-drop
status: done
feature: dynamic-graph-viz
---

# Live d3-force layout with pin-on-drop drag and role-based node sizes

Tags: `viz`, `memory`, `mcp`, `docs`
Depends on: None
Blocks: 163, 164
Implements: ADR-011 (amends ADR-005 §1/§2 — the layout library; the single renderer and the CDN decision stand)

## Scope

The **Graph renderer** (`apps/memory/src/tree/memory/visualize/graph.py`) today runs ONE
`forceAtlas2.assign(graph, {iterations: 300})` pass (l.498–501) and then stops; dragging
(l.615–627) just rewrites `x`/`y` of one node and nothing reacts. Replace that with a LIVE
d3-force simulation modelled on `pulse` (`/Users/pauliusztin/Documents/01-Projects/scrabble/scrabble/src/pulse/viz/static/graph.html`
l.75–112, 197–241): d3 owns the positions and ticks them into graphology, sigma redraws;
a dragged node is held with `fx`/`fy` while `alphaTarget(0.3)` keeps the sim hot so its
unpinned neighbours follow live; on drop the node STAYS pinned (keeps `fx`/`fy`) and shows a
pin marker; double-clicking a pinned node unpins it. The **Embedding map** (`layout: "fixed"`)
constructs NO simulation — its UMAP coordinates stay where they are (ADR-007 §2) — but shares
the drag / pin / marker code. Every surface (file HTML, `ui://` iframe, file fallback) gets
this through the ONE template; `tree.mcp.viz_app` only changes its import line.

### Browser deps (`graph.py` l.79–84, `viz_app.py` l.252, `_resolve_static`)
- Delete `_FA2_CDN` and the `forceAtlas2` import from BOTH templates. Add
  `_D3_FORCE_CDN = "https://cdn.jsdelivr.net/npm/d3-force@3.0.0/+esm"` and the import
  `import { forceSimulation, forceLink, forceManyBody, forceCenter } from "__D3_FORCE_CDN__";`
  (`_resolve_static` splices `__D3_FORCE_CDN__`). VERIFIED 2026-10-01: that URL is a 200 ESM
  bundle that itself imports `d3-quadtree@3.0.1`, `d3-dispatch@3.0.1`, `d3-timer@3.0.1` from
  jsdelivr and exports those four names — ONE import line, no UMD load-order dance (pulse needs
  four `<script>` tags only because it loads UMD). No SRI: jsdelivr `+esm` bundles are generated
  (the bundle header says "Do NOT use SRI"), same as today's three imports. The CSP
  `resource_domains` in `viz_app.py::graph_view` already lists `https://cdn.jsdelivr.net` —
  no change. Rewrite the comment at l.79–81 (it explains why FA2 forced ESM).

### Payload contract (Python decides, JS obeys — pulse's split)
- `to_graph_payload`: every node gains `"size": _node_size(node_type, subtype)` with
  `_NODE_SIZES = {"document": 12, "chunk:parent": 8, "chunk:child": 4}` and `_DEFAULT_NODE_SIZE = 6`
  for entities / `unknown` / a chunk with no subtype. `subtype` comes off the row (it is already
  in `_NODE_META_FIELDS`). Sizes are CONFIRMABLE (see plan) — one dict.
- `to_graph_payload` also emits `"controls": {"forces": dict(_DEFAULT_FORCES), "display": dict(_DEFAULT_DISPLAY)}`
  with `_DEFAULT_FORCES = {"centre": 0.2, "repel": 8.0, "link": 0.3, "linkDistance": 80}` and
  `_DEFAULT_DISPLAY = {"nodeSize": 1.0, "linkThickness": 1.0, "labelFade": 1, "arrows": True, "edgeLabels": True}`.
  `display` values are MULTIPLIERS / switches over what Python resolved (`nodeSize 1.0` = draw
  `node.size` as is), exactly pulse's `_FORCES` / `_DISPLAY`. Task 164 builds sliders over them;
  THIS task reads `forces` to seed the sim and `display` in the reducers (`size: data.size * display.nodeSize`,
  edge `size: 1.2 * display.linkThickness`, `type: display.arrows ? "arrow" : "line"` — sigma 3.0.3
  registers both `arrow` and `line` by default — `renderEdgeLabels: display.edgeLabels`,
  `labelRenderedSizeThreshold: display.labelFade`). Defaults are confirmable; the SWE tunes
  `forces` on the e2e full graph until a ~300-node graph settles in < 5 s without a hairball and
  document→chunk stars read as stars, then records the final numbers in the Log.
- `to_embedding_map_payload` (`embeddings.py`): drop the payload-wide `nodeSize` key; every
  point carries `"size": 4` (same number, now per node — ONE sizing mechanism); emit
  `"controls": {"display": dict(_DEFAULT_DISPLAY)}` — NO `forces`, which is how the template
  knows there is nothing to simulate (together with `layout: "fixed"`). Import the default from
  `graph.py` (memory ← visualize, same package).
- The JS treats `payload.controls` as required for the simulation branch; a `layout: "fixed"`
  payload without `controls` still renders (display falls back to `{nodeSize: 1, linkThickness: 1,
  labelFade: 1, arrows: true, edgeLabels: true}` inline — keep the existing hand-made test payload
  in `test_graph.py::test_fixed_layout_payload_keeps_its_stored_coordinates` valid).

### Renderer JS (`_RENDER_JS`; may be split into adjacent constants spliced by `_resolve_static` — the
two variants must stay token-identical except ext-apps + height, as `test_the_iframe_variant_only_adds_the_ext_apps_runtime` pins)
- Build `simNodes = [{id, x, y, fx: null, fy: null}]` for EVERY node on BOTH layouts (pin state
  lives on the d3 node object: `fx`/`fy` numbers = pinned, `null` = free — one source of truth,
  no separate Set). On the map, seed from the stored `x`/`y` and build NO simulation.
- Live layout only when `!isFixed` (keep the literal `const isFixed = payload.layout === "fixed";`
  and an `if (!isFixed) {` guard around the simulation — the existing tests pin the guard shape;
  update them to pin `forceSimulation(` instead of `forceAtlas2`). Let d3 seed positions (leave
  `x`/`y` undefined so `forceSimulation` places them on its phyllotaxis spiral; copy them into
  graphology before `new Sigma(`) — delete the `Math.random()` starts. Forces exactly as pulse:
  `forceCenter(0,0).strength(forces.centre)`, `forceManyBody().strength(-forces.repel * REPEL_SCALE)`
  with `REPEL_SCALE = 30`, `forceLink(links).id(n => n.id).strength(forces.link).distance(forces.linkDistance)`
  where `links` excludes self-loops (zero-length link = division by zero). `tick` → `graph.mergeNodeAttributes(id, {x, y})`
  (sigma refreshes itself on attribute changes). Keep `alphaDecay` default so the sim cools and
  stops on its own.
- Drag (replace l.615–627, follow pulse l.197–241 but PIN on release): `downNode` → freeze the
  viewport `if (!renderer.getCustomBBox()) renderer.setCustomBBox(renderer.getBBox())`, set
  `fx/fy = x/y`, and when a sim exists `sim.alphaTarget(0.3).restart()`; `mousemovebody` (the
  captor event already used today) → move the node (`fx/fy` AND graphology `x/y`), then
  `preventSigmaDefault()` + `original.preventDefault()/stopPropagation()` so sigma does not pan;
  captor `mouseup` → KEEP `fx`/`fy` (the node is now a **Pinned node**), `sim.alphaTarget(0)`,
  `renderer.setCustomBBox(null)`. Track `wasDragged` so a drag's trailing `clickNode` does not
  change selection (pulse l.379–383).
- Unpin: `renderer.on("doubleClickNode", e)` → if the node is pinned, `fx = fy = null`,
  `e.preventSigmaDefault()` (suppress sigma's double-click zoom), reheat `sim.alpha(0.5).restart()`
  when a sim exists; a double-click on an UNPINNED node keeps sigma's default zoom.
- Overlay: rename `<canvas id="hulls-layer">` → `<canvas id="overlay">` (`#overlay` CSS unchanged:
  absolute, `pointer-events: none`, `z-index: 1`) and generalise `drawHulls` into ONE
  `drawOverlay()` that runs on `afterRender` + `resize` on BOTH layouts: hulls (map, when the
  checkbox is on) first, then a pin marker on every node with `fx != null` — a filled
  `#1f2430` disc of radius `0.35 * renderer.scaleSize(displayData.size)` at the node's
  `graphToViewport` position (skip nodes whose `getNodeDisplayData` is undefined/hidden). Tasks
  163 adds selection rings and the marquee to the same function.
- State markers for headless evidence and debugging: `document.body.dataset.layout = isFixed ? "fixed" : "live"`
  once rendered; on the live layout `document.body.dataset.sim = "running"` on the first tick and
  `"settled"` from the sim's `end` event (164 adds `"paused"`). Nothing else reads them.
- Unchanged: counts, search, legend, tooltips (hover + edge hover + `afterRender` repositioning),
  zoom buttons, hull convex-hull maths, warning banner, the `quietChunk` label rule.

### Docs / prose sweep (ForceAtlas2 is gone from every current-tense sentence)
- `graph.py` module docstring (l.3–5, 28–29, 36–41), `embeddings.py` docstring (l.6–8, 118–119),
  `apps/memory/README.md:357` ("fixed coordinates, ForceAtlas2 skipped" → "fixed coordinates, no
  simulation — drag, pin and the Display knobs still work"), `viz_app.py` docstring if it names the
  stack. `grep -rni "forceatlas" apps/memory/src apps/memory/README.md` → 0 hits. ADR-005/007 and
  the glossary's history lines are NOT touched by this task (grooming commit owns docs/).

### Tests (`/squid-testing-python`; unit suite only — see Verification)
- `tests/unit/memory/visualize/test_graph.py`: `test_render_graph_file_writes_self_contained_html`
  asserts `forceSimulation(` present and `forceAtlas2` ABSENT; `test_force_atlas_runs_only_outside_the_fixed_layout_branch`
  becomes `test_the_simulation_runs_only_outside_the_fixed_layout_branch` (guard literal
  `      if (!isFixed) {` precedes `forceSimulation(`); `test_graph_payload_render_asks_for_no_fixed_layout`
  keeps `'"nodeSize":' not in html` (the key is gone everywhere now) and adds `'"controls":' in html`;
  new payload tests: node `size` per role (document 12 / parent 8 / child 4 / person 6 / unknown 6),
  `controls.forces` + `controls.display` present with the module defaults; new template tests pin
  `"doubleClickNode"`, `setCustomBBox(`, `alphaTarget(0.3)`, `.fx =`, `dataset.layout`, `dataset.sim`,
  `<canvas id="overlay">`, `function drawOverlay`, and that `hulls-layer` is gone.
- `tests/unit/mcp/test_viz_app.py::test_both_variants_carry_the_embedding_map_extensions`: swap the
  `hulls-layer` / `drawHulls` tokens for `overlay` / `drawOverlay`; add `__D3_FORCE_CDN__`-resolved
  URL present and `forceatlas2` absent in BOTH variants.
- `tests/unit/memory/visualize/test_embeddings.py`: `payload["nodeSize"] == 4` → every node
  `size == 4` and `"nodeSize" not in payload`; `controls == {"display": …}` with no `forces`;
  the file test's `'"nodeSize": 4'` → `'"size": 4'`.

## Out of scope
- Multi-select, marquee, group drag, rigid `part_of` children (163). The control panel, Unpin
  all, Pause (164) — this task's only unpin gesture is the double-click.
- `forceCollide`, `@sigma/node-border`, SRI, vendoring, WebWorker layout, touch.

## Acceptance Criteria

> `[HUMAN]` criteria are executed by the orchestrator through Claude in Chrome (real browser, gestures + GIF), with results pasted into `## Log`; the human does a final glance. The SWE/Tester do not block on them.

- [x] `grep -rn "forceatlas2\|forceAtlas2\|_FA2_CDN" apps/memory/src apps/memory/tests` → 0 hits; both
      `_FILE_HTML_BASE` and `viz_app._GRAPH_HTML` contain exactly one
      `from "https://cdn.jsdelivr.net/npm/d3-force@3.0.0/+esm"` import naming `forceSimulation, forceLink, forceManyBody, forceCenter`.
- [x] `to_graph_payload` nodes carry `size` by role: `document` 12, `chunk` with `subtype: "parent"` 8,
      `chunk` with `subtype: "child"` 4, every other type (incl. `unknown`, chunk without subtype) 6 (unit tests).
- [x] `to_graph_payload` returns `controls == {"forces": {"centre": 0.2, "repel": 8.0, "link": 0.3, "linkDistance": 80}, "display": {"nodeSize": 1.0, "linkThickness": 1.0, "labelFade": 1, "arrows": True, "edgeLabels": True}}`
      (or the tuned values the SWE records in the Log — the test and the constants move together).
- [x] `to_embedding_map_payload` has NO `nodeSize` key, every node has `size == 4`, and
      `controls == {"display": {...}}` with no `forces` key (unit tests).
- [x] The template keeps `const isFixed = payload.layout === "fixed";` and wraps `forceSimulation(`
      in an `if (!isFixed) {` guard (string-pinned, both variants); no `Math.random()` start remains.
- [x] The template contains `"doubleClickNode"`, `setCustomBBox(`, `alphaTarget(0.3)`, `alphaTarget(0)`,
      `alpha(0.5)`, `dataset.layout`, `dataset.sim`, `<canvas id="overlay">`, `function drawOverlay`;
      `hulls-layer` and `drawHulls` are gone (both variants).
- [x] Headless smoke, Tester-run and pasted in the Log (graph): from a fresh
      `make memory-query-graph QUERY="<something in the local corpus>" ` file under `.tree/graphs/`,
      `"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --no-first-run --use-angle=swiftshader --enable-unsafe-swiftshader --virtual-time-budget=10000 --dump-dom "file://<abs path>.html" | grep -o '<body[^>]*>\|<span id="counts">[^<]*\|<div id="legend">.\{0,40\}'`
      prints `<body data-layout="live" data-sim="running">`, a non-"loading" counts line (`N nodes · M edges`)
      and a filled legend. (`--disable-gpu` MUST NOT be passed — it disables WebGL and `new Sigma(` throws.)
- [x] Headless smoke (map): same command on a `make memory-visualize-embeddings HULLS=true` file prints
      `<body data-layout="fixed">` with no `data-sim`, and the counts line is the map summary.
- [ ] [HUMAN] On the graph file: nodes keep moving after load and come to rest within ~5 s; dragging a
      document node pulls its chunks along live; on release the node stays put with a dark centre dot;
      double-clicking it removes the dot and the node drifts back into the layout.
- [ ] [HUMAN] On the map file: no point moves on load; dragging a point moves ONLY that point, which
      then shows the pin dot; the hull outline follows it; double-click removes the dot (the point stays).
  - [HUMAN] pending — orchestrator's Claude-in-Chrome run was inconclusive (extension drag pans the WebGL stage); Tester verified drag/pin/unpin via real CDP mouse events (overlay pixel evidence in Tester Log); human visual check outstanding.
- [x] `grep -rni "forceatlas" apps/memory/src apps/memory/README.md` → 0 hits.
- [x] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green; test count ≥ today's.

## User Stories

### Story: Operator opens the full graph and watches it settle
1. `make env-status` → local; `make memory-query-graph` (graphrag).
2. The browser opens `.tree/graphs/graph-<stamp>.html`; the header reads `N nodes · M edges`.
3. Nodes start on a spiral and spread out over ~2–5 s as the live forces act; document nodes are
   visibly larger than their parent chunks, which are larger than child chunks.
4. The layout comes to rest; `document.body.dataset.sim` reads `settled` in DevTools.

### Story: Operator drags a document and its star follows
1. Operator presses on a document node and drags it 300 px to the right.
2. While dragging, the view does not pan or rescale; the chunks linked to the document are pulled
   after it with a slight lag (the sim is hot).
3. On release the document stays exactly where dropped and shows a dark centre dot; its chunks keep
   settling around it.

### Story: Operator unpins a node
1. Operator double-clicks the pinned document from the previous story.
2. The pin dot disappears and the view does NOT zoom (sigma's double-click zoom is suppressed for this case).
3. The node is pulled back toward its neighbours and the graph re-settles.

### Story: Operator inspects the map without disturbing it
1. `make memory-visualize-embeddings HULLS=true` opens `embedding-map-<stamp>.html`.
2. Nothing moves on load; cluster hulls are drawn where they were.
3. Operator drags one point out of its hull: only that point moves; the hull outline stretches to
   include its new position; the point shows the pin dot. Double-click removes the dot.

### Story: Assistant shows the graph inline in an MCP App client
1. In a UI-capable client the user asks to visualize the graph; `visualize_memory_graph` returns the iframe.
2. The iframe behaves exactly like the file (live settle, drag-pin, double-click unpin) — same
   `_RENDER_JS`, the d3-force bundle loads from jsdelivr under the existing CSP.

---

Blocked by: (none)

## Log

### [SWE] 2026-10-01 14:09 — Implementation

**Files modified**
- `apps/memory/src/tree/memory/visualize/graph.py` — `_FA2_CDN` → `_D3_FORCE_CDN` (+ comment rewrite); `_NODE_SIZES` / `_DEFAULT_NODE_SIZE` / `_node_size()`; `_DEFAULT_FORCES` / `_DEFAULT_DISPLAY`; `to_graph_payload` emits per-node `size` + `controls {forces, display}` (copies), return type → `dict[str, Any]`; `_RENDER_JS` rewritten: d3 sim nodes (pin state = `fx`/`fy`) on BOTH layouts, `if (!isFixed) { sim = forceSimulation(simNodes)…` with centre / manyBody (`REPEL_SCALE = 30`) / link (self-loops excluded), tick → `mergeNodeAttributes`, `dataset.layout` / `dataset.sim` (`running` on tick, `settled` on `end`), display multipliers in the reducers, pin-on-drop drag (`setCustomBBox` freeze, `alphaTarget(0.3)` / `alphaTarget(0)`, `wasDragged` guard on `clickNode`), `doubleClickNode` unpin (`alpha(0.5)`, sigma zoom suppressed only for pinned nodes), `#hulls-layer`/`drawHulls` → `#overlay`/`drawOverlay` (hulls rebuilt from CURRENT graph x/y every draw, then pin dots); no `Math.random()`; module docstring sweep.
- `apps/memory/src/tree/memory/visualize/embeddings.py` — per-point `size: 4`, `controls: {"display": dict(_DEFAULT_DISPLAY)}`, `nodeSize` key gone; docstrings.
- `apps/memory/src/tree/mcp/viz_app.py` — import line only (d3-force).
- `apps/memory/src/tree/mcp/dashboard_app.py` — two stale `dict[str, list[dict[str, Any]]]` payload annotations → `dict[str, Any]` (the payload now carries `controls`).
- `apps/memory/src/tree/memory/visualize/__init__.py`, `apps/memory/README.md` — ForceAtlas2 prose sweep.
- `apps/memory/tests/unit/memory/visualize/test_graph.py` — role sizes (parametrized incl. `person` + `subtype: "parent"` → 6, endpoint-only → 6), `controls` defaults + copy-isolation, no payload-wide `nodeSize`, single d3 import, live-layout/pin tokens, guard test renamed `test_the_simulation_runs_only_outside_the_fixed_layout_branch`, overlay tokens, leftovers gone, self-loop filter, no-pin-on-plain-click, reducer multipliers.
- `apps/memory/tests/unit/memory/visualize/test_embeddings.py` — per-node size, `controls` without `forces`, copy isolation, file test `"size": 4` / `drawOverlay`.
- `apps/memory/tests/unit/mcp/test_viz_app.py` — both-variant tokens swapped to overlay/drawOverlay + live-layout tokens; new both-variant test: exactly one d3-force import naming the four forces, no "atlas", no `hulls-layer`/`drawHulls`/`Math.random()`.

**Tests**
- Unit: 4132 passed, 0 failed (baseline before this task: 4078) — `make memory-tests`.
- Integration: N/A (no integration suite in this repo).

**Acceptance criteria**
- [x] grep FA2 → 0 hits; one d3-force import in each variant — `test_viz_app.py::test_both_variants_import_d3_force_and_no_other_layout_engine`, `test_graph.py::test_the_file_variant_imports_d3_force_once_by_name`
- [x] role sizes — `test_graph.py::test_node_size_is_resolved_by_role`, `::test_dangling_endpoint_gets_the_default_size`
- [x] `controls` defaults (untuned — see Notes) — `test_graph.py::test_payload_ships_the_force_and_display_defaults`
- [x] map payload — `test_embeddings.py::test_payload_sizes_every_point_per_node`, `::test_payload_ships_display_controls_but_no_forces`
- [x] `isFixed` guard around `forceSimulation(`, no `Math.random()` — `test_graph.py::test_the_simulation_runs_only_outside_the_fixed_layout_branch`, both variants in `test_viz_app.py`
- [x] template tokens (both variants) — `test_viz_app.py::test_both_variants_carry_the_embedding_map_extensions`, `test_graph.py::test_template_carries_the_live_layout_and_pin_machinery`
- [ ] headless smoke (graph) — SWE pre-run: evidence below; Tester owns (repro recipe in Notes)
- [ ] headless smoke (map) — SWE pre-run on a SYNTHETIC map (no clustering run locally): evidence below; Tester owns
- [ ] [HUMAN] graph gestures — orchestrator (SWE pre-check over CDP in Evidence)
- [ ] [HUMAN] map gestures — orchestrator (SWE pre-check over CDP in Evidence)
- [x] `grep -rni "forceatlas" apps/memory/src apps/memory/README.md` → 0
- [x] format/lint/pre-commit/tests green, 4132 ≥ 4078

**Evidence**
```
$ make env-status
Env target: local (.env)
$ grep -rn "forceatlas2\|forceAtlas2\|_FA2_CDN" apps/memory/src apps/memory/tests | wc -l
       0
$ grep -rni "forceatlas" apps/memory/src apps/memory/README.md | wc -l
       0
$ make memory-format-check && make memory-lint-check && make pre-commit
317 files already formatted
All checks passed!
prettier....Passed  ruff check....Passed  ruff format....Passed  biome check (harness)....Passed
$ make memory-tests
============================ 4132 passed in 58.60s =============================

# full graph (read-only render; see Notes for why not `make memory-query-graph`)
Result: 170 nodes, 236 edges
Wrote self-contained graph HTML (171 nodes, 236 edges) to .../apps/memory/.tree/graphs/graph-20261001-135304.html
$ chrome --headless=new --no-first-run --use-angle=swiftshader --enable-unsafe-swiftshader --virtual-time-budget=10000 --dump-dom file://.../graph-20261001-135304.html | grep -o '<body[^>]*>\|<span id="counts">[^<]*\|<div id="legend">.\{0,40\}'
<body data-layout="live" data-sim="running">
<span id="counts">171 nodes · 236 edges
<div id="legend"><div class="title">Legend</div><div clas

# query graph — QUERY="langgraph agent memory"
Graph expansion: 8 seed(s) → 78 nodes, 75 edges (1 hops)
Wrote self-contained graph HTML (78 nodes, 75 edges) to .../apps/memory/.tree/graphs/langgraph-agent-memory-20261001-135339.html
$ chrome … --dump-dom file://.../langgraph-agent-memory-20261001-135339.html | grep -o …
<body data-layout="live" data-sim="running">
<span id="counts">78 nodes · 75 edges
<div id="legend"><div class="title">Legend</div><div clas

# map — 86 points, 3 clusters, 6 noise, 3 unclustered, hulls=True
Wrote self-contained embedding map HTML (86 points) to .../apps/memory/.tree/graphs/embedding-map-20261001-135410.html
$ chrome … --dump-dom file://.../embedding-map-20261001-135410.html | grep -o …
<body data-layout="fixed">
<span id="counts">86 chunks in 3 clusters (+6 noise)
<div id="legend"><div class="title">Legend</div><div clas

# role sizes on the real full graph: (type, subtype, size) counts
chunk/child/4: 109 · chunk/parent/8: 7 · document/None/12: 4 · object/document/6: 11 · fact/None/6: 12 · unknown/None/6: 1 · …

# gesture pre-check (NOT the [HUMAN] check): real MouseEvents into sigma's mouse canvas, real-time
# headless Chrome driven over CDP, on a scratchpad copy that only exposes renderer/graph/simById
graph (live), document "how-does-memory-for-ai-agents-work", 3 s after load, drag +160 px:
 pinnedAfterDrop=true bboxReleased=true movedX=926.87 staysPutAfterDrop(1.5 s)=true
 neighbourMoved(parent-0 chunk)=174.88 plainClickPins=false pinnedAfterDoubleClick=false
 zoomedOnUnpinDoubleClick=false driftAfterUnpin(1.5 s)=787.77
map (fixed), noise-0, drag +160 px:
 pinnedAfterDrop=true bboxReleased=true movedX=2.43 staysPutAfterDrop=true neighbourMoved=0
 otherMovedOnDrag=0 plainClickPins=false pinnedAfterDoubleClick=false zoomedOnUnpinDoubleClick=false
 driftAfterUnpin=0 sim=null

# settle time, real-time headless (swiftshader software WebGL), polling data-sim
graph-20261001-135304 (171 nodes): firstTick 104 ms, settled 16148 ms
langgraph-agent-memory-…   (78 nodes): firstTick 341 ms, settled 9593 ms
```

**Notes**
- **Forces: defaults kept** — `centre 0.2, repel 8.0, link 0.3, linkDistance 80` (`REPEL_SCALE = 30`). A 1400×900 headless screenshot of the 171-node full graph shows no hairball: documents (12) > parent chunks (8) > child chunks (4) read at a glance, child chunks form tight stars around their parents, and isolated entities sit on the periphery. The local corpus has 171 nodes, not ~300.
- **Settle time is frame-bound, not force-bound.** d3's default `alphaDecay` (kept, per spec) reaches `alphaMin` after ~300 ticks, one tick per rAF, so ~5 s at 60 fps. Headless swiftshader managed ~19 fps (300 ticks / 16 s); under `--virtual-time-budget`, rAF is starved (1 render per virtual second), which is why the dump shows `data-sim="running"` (as ADR-011 predicted). The ~5 s check belongs to the [HUMAN] run on a real GPU browser.
- **Spec contradictions resolved in the tests (advisor-reviewed):**
  1. `'"nodeSize":' not in html` cannot hold, because `controls.display.nodeSize` (the multiplier) is embedded in every graph file. The "no payload-wide `nodeSize`" check moved onto the dict (`test_graph_payload_has_no_payload_wide_node_size`); the HTML test now asserts `'"controls":' in html`.
  2. The AC grep scans `apps/memory/tests` for `forceAtlas2`, so the "absent" assertions are written as `"atlas" not in html.lower()`.
- **Deliberate choice (glossary-aligned, not a fork):** a press-and-release WITHOUT movement does not pin. The glossary says a node becomes pinned by being "DROPPED after a drag". `mouseup` restores `fx`/`fy = null` when `!wasDragged && !wasPinned`. A plain click on a node that is already pinned leaves it pinned.
- **No JS fallback for node `size`** (ADR-011 §6: Python decides). The only inline fallback is the display object the spec allows, used when a hand-made fixed payload has no `controls`.
- **Why the e2e did not use the make targets verbatim.** `make memory-query-graph` fails at startup in this worktree: `init_mongodb` → Beanie tries to create `user_source_uri_unique` on `{user_id, source_type, source_uri}`, but the shared local Mongo already has that index name on `{user_id, source_uri}`, created by the main checkout's `feat/document-unique-key` branch (`IndexKeySpecsConflict`, code 86). It is environmental, not caused by this task, and I did NOT touch the shared index. Instead I ran the real `scripts/query_graph.py` / `scripts/visualize_embeddings.py` through a scratchpad wrapper that swaps in `init_beanie(..., skip_indexes=True)`, so the run is read-only.
- **No clustering run exists locally**, so `visualize_embeddings.py --hulls` prints the "No clustering run found" message (exit 1). I did not run the clustering pipeline, which would write to the shared DB. The map file comes from the real `to_embedding_map_payload(hulls=True)` + `render_embedding_map_file` over a hand-built `EmbeddingMap` (3 clusters, noise, 3 unclustered → the warning banner shows).
- One chained `… && make pre-commit && make memory-tests` invocation exited non-zero once without my capturing the output. The three runs that followed were green (4132 passed each). It looks transient and unrelated, but the Tester should watch for it.
- **Pixel check after a drag (CDP screenshots, 1200×800).** Graph: the dragged document shows a clearly visible dark centre dot and stays where it was dropped. Map: the Topic 1 hull stretches to the dropped point. The map's pin dot, however, is barely visible: the spec formula `0.35 * scaleSize(size)` gives ~1.4 px radius on a size-4 point. The formula is unchanged as specified; flagging it for the [HUMAN] map check and for 164's Node-size knob.
- **[HUMAN] tip:** each document URL also exists as a smaller `object` entity sitting next to the document. Drag the LARGE dark-green node (size 12); grabbing the object would look like "its chunks didn't follow".
- **Worktree environment changes:** (1) copied `.env` from the main checkout (gitignored); (2) ran `uv sync --extra local-models` in the worktree venv, because the baseline `make memory-tests` failed collection without `modal` / `sentence_transformers`.
- **Repro recipe for the Tester** (the make targets hit the index conflict above). Run from `apps/memory` with `set -a; source ../../.env; set +a`:
  - `uv run python <run_readonly.py> scripts/query_graph.py --no-open` (full graph)
  - `… --query "langgraph agent memory" --no-open`
  - `uv run python <synthetic_map.py>` (map)
  - zsh trap: write `"${U}:chunk:…"`, not `"$U:chunk:…"`, because `:c` / `:f` are eaten as history modifiers.

  `run_readonly.py`:
  ```python
  import runpy
  import sys

  from beanie import init_beanie
  from pymongo import AsyncMongoClient

  import tree.db


  async def _init_mongodb_readonly(uri: str, database: str) -> AsyncMongoClient:
      client = AsyncMongoClient(uri, tz_aware=True)
      await init_beanie(
          database=client[database],
          document_models=tree.db.ALL_DOCUMENT_MODELS,
          skip_indexes=True,
      )
      return client


  tree.db.init_mongodb = _init_mongodb_readonly
  script, *args = sys.argv[1:]
  sys.argv = [script, *args]
  runpy.run_path(script, run_name="__main__")
  ```

  `synthetic_map.py`:
  ```python
  import logging
  import random

  from tree.logging import init_logger
  from tree.memory.clustering.types import EmbeddingMap, MapPoint, MemoryClusterInfo
  from tree.memory.visualize.embeddings import render_embedding_map_file, to_embedding_map_payload

  init_logger()
  rng = random.Random(7)
  centres = {0: (2.0, 3.0), 1: (8.0, 1.0), 2: (5.0, 8.0)}
  sizes = {0: 40, 1: 25, 2: 15}
  clusters = [
      MemoryClusterInfo(cluster_id=cid, label=f"Topic {cid}", summary="", keywords=["a", "b", "c"],
                        size=sizes[cid], sample_chunk_ids=[], centroid_x=cx, centroid_y=cy)
      for cid, (cx, cy) in centres.items()
  ]
  points = [
      MapPoint(chunk_id=f"c{cid}-{i}", x=rng.gauss(cx, 0.8), y=rng.gauss(cy, 0.8), cluster_id=cid,
               title=f"Doc {cid}", heading_path=["H1", "H2"], snippet="…")
      for cid, (cx, cy) in centres.items() for i in range(sizes[cid])
  ] + [
      MapPoint(chunk_id=f"noise-{i}", x=rng.uniform(0, 10), y=rng.uniform(0, 10), cluster_id=-1,
               title="Noise doc", heading_path=[], snippet="…")
      for i in range(6)
  ]
  emap = EmbeddingMap(run_id="synthetic", clusters=clusters, points=points,
                      total_children=len(points) + 3, unclustered=3)
  payload = to_embedding_map_payload(emap, hulls=True)
  logging.getLogger(__name__).info("%s — %s", render_embedding_map_file(payload), payload["summary"])
  ```
- Out of scope / not done: selection, marquee, group drag (163); panel, Pause, Unpin all (164).

### [Tester] 2026-10-01 17:20 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check && make memory-lint-check && make pre-commit`, exit 0)
- Unit tests: 4132 passed / 0 failed, TWICE back-to-back (`make memory-tests` runs 1 and 2: 75.4 s, 72.5 s) - no flakiness seen
- Integration tests: N/A (repo has none by design)
- Warnings: 0 (no warnings summary in either run)
- SWE note (d) (one unexplained non-zero chained run): the Tester re-ran the full chain `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` after writing this entry - result recorded in the last line of Evidence.

**E2E adversarial pass** (headless Chrome, swiftshader WebGL, NO `--disable-gpu`; real-time CDP with real `Input.dispatchMouseEvent`; scratch files outside the repo)
- Happy path graph (fresh render of the real local corpus via the SWE's read-only wrapper, see deviation below): `scripts/query_graph.py` -> `graph-20261001-141609.html`, 171 nodes / 236 edges. dump-dom: `<body data-layout="live" data-sim="running">`, `<span id="counts">171 nodes · 236 edges`, `<div id="legend"><div class="title">Legend</div><div clas` (PASS). Same for `--query "langgraph agent memory"`: `78 nodes · 75 edges`, live/running (PASS).
- Happy path map: `embedding-map-20261001-141622.html` -> `<body data-layout="fixed">` (no data-sim), counts `86 chunks in 3 clusters (+6 noise)`, filled legend (PASS).
- Break 1 (state: map NEVER constructs the simulation): copied the map and graph files, replacing the d3 import with a wrapper that bumps `body.dataset.simctor` per `forceSimulation()` call. Map -> `<body data-layout="fixed">` (no simctor); graph -> `<body data-simctor="1" data-layout="live" data-sim="running">` (exactly one ctor) (PASS).
- Break 2 (boundary payloads, each rendered to a file and loaded; no uncaught exceptions on any): zero edges (3 nodes) -> live/running `3 nodes · 0 edges`; single node -> live/running `1 nodes · 0 edges`; one node with only a self-loop -> live/running `1 nodes · 1 edges`; self-loop + parallel edges -> live/running `2 nodes · 3 edges`; empty payload -> `No graph data returned.` and no body dataset; map with 1 point + 1-point cluster hull -> fixed; map with 3 points -> fixed; hand-made fixed payload with NO `controls` -> `data-layout="fixed"`, `2 nodes · 0 edges` (all PASS). CDP `Runtime.exceptionThrown` list empty on every run.
- Break 3 (unknown types / missing subtype): payload with chunk no subtype, chunk parent, chunk child, chunk `subtype: "weird"`, novel type `zzz_novel`, person with `subtype: "parent"`, dangling edge endpoint -> sizes `[document 12, chunk 6, chunk 8, chunk 4, chunk 6, zzz_novel 6, person 6, unknown 6]` as specified; renders live/running, 8 nodes · 3 edges (PASS).
- Break 4 (hostile input): node id `</script><script>document.title='PWNED'</script>` and name `</script><img src=x onerror=...>`, edge type `</script>`. File has exactly one `</script` (the real closer; payload `</` is escaped to `<\/`). In page: `document.title === ""`, only 1 `<script>` in body (the module itself, which contains the escaped data), `PWNED` appears in exactly 1 script element and nowhere else. Hovering both nodes: tooltip shown, 0 `<img>` elements, hostile text displayed as literal text, title unchanged (PASS).
- Break 5 (gestures, real mouse events via CDP, per file; node picked via `graphToViewport`): press -> 10 moves -> release; then plain click; then double-click. Results (identical pattern on single_node, zero_edges, self_loop, chunk_no_subtype_unknown, and the map1 fixed file): after drop `pinned=true` and the `#overlay` canvas pixel at the node's viewport position is `31,36,48,255` (`#1f2430` dot); a plain click on the pinned node leaves it pinned; double-click -> `pinned=false`, overlay pixel at the old position `0,0,0,0`; no exceptions. The map point moved exactly the drag distance (100 px) with `sim` undefined throughout (PASS).
- Break 6 (settle): graph payloads of 1-8 nodes reach `data-sim="settled"` ~6.2-6.5 s after navigation (includes CDN load) in software-WebGL headless; the 171-node file was measured by the SWE (settle is frame-bound; real-GPU check belongs to the [HUMAN] run).

**Acceptance criteria**
- [x] PASS - grep FA2 -> 0 hits; one d3-force import naming the four forces in BOTH variants. Evidence: `grep -rn "forceatlas2\|forceAtlas2\|_FA2_CDN" apps/memory/src apps/memory/tests | wc -l` -> 0; scripted check over `_FILE_HTML_BASE` and `viz_app._GRAPH_HTML`: one `import { forceSimulation, forceLink, forceManyBody, forceCenter } from "https://cdn.jsdelivr.net/npm/d3-force@3.0.0/+esm";` each, no unresolved `__D3_FORCE_CDN__`; tests `test_both_variants_import_d3_force_and_no_other_layout_engine`, `test_the_file_variant_imports_d3_force_once_by_name`.
- [x] PASS - node `size` by role. Evidence: `test_node_size_is_resolved_by_role`, `test_dangling_endpoint_gets_the_default_size`; my payload run (Break 3) shows 12 / 8 / 4 / 6 incl. chunk without subtype and unknown types.
- [x] PASS - `controls` defaults on `to_graph_payload`. Evidence: `test_payload_ships_the_force_and_display_defaults`; `_DEFAULT_FORCES`/`_DEFAULT_DISPLAY` in graph.py match the AC literals (untuned values kept, SWE note).
- [x] PASS - map payload: no `nodeSize`, every node `size == 4`, `controls == {"display": ...}` no `forces`. Evidence: `test_payload_sizes_every_point_per_node`, `test_payload_ships_display_controls_but_no_forces`.
- [x] PASS - `isFixed` literal kept, `forceSimulation(` once, behind `      if (!isFixed) {`, no `Math.random()`. Evidence: scripted token counts on both variants (guard idx < forceSimulation idx, counts 1/1/0); `test_the_simulation_runs_only_outside_the_fixed_layout_branch`.
- [x] PASS - template tokens in both variants. Evidence (counts identical for file and iframe): `"doubleClickNode"` 1, `setCustomBBox(` 2, `alphaTarget(0.3)` 2, `alphaTarget(0)` 1, `alpha(0.5)` 1, `dataset.layout` 1, `dataset.sim` 3, `<canvas id="overlay">` 1, `function drawOverlay` 1; `hulls-layer` / `drawHulls` / `Math.random` / `atlas` 0. The iframe variant contains the file variant's render JS verbatim.
- [x] PASS - Headless smoke (graph). Evidence: command from the AC on `graph-20261001-141609.html` -> `<body data-layout="live" data-sim="running">`, `<span id="counts">171 nodes · 236 edges`, `<div id="legend"><div class="title">Legend</div><div clas`. DEVIATION (acknowledged by the orchestrator): file produced by `scripts/query_graph.py --no-open` through the SWE's read-only Beanie wrapper (scratchpad `run_readonly.py`, `skip_indexes=True`), not `make memory-query-graph`, because the shared local Mongo has a conflicting `user_source_uri_unique` index from another branch; I did NOT touch any Mongo index.
- [x] PASS - Headless smoke (map). Evidence: `embedding-map-20261001-141622.html` -> `<body data-layout="fixed">` (no data-sim), `<span id="counts">86 chunks in 3 clusters (+6 noise)`, filled legend. DEVIATION (acknowledged): the map is the real `to_embedding_map_payload(hulls=True)` + `render_embedding_map_file` over a SYNTHETIC `EmbeddingMap` (no local clustering run; `visualize_embeddings.py --hulls` prints "No clustering run found").
- [ ] [HUMAN] graph gestures - Awaiting human verification (orchestrator). Tester CDP pre-check: see Break 5.
- [ ] [HUMAN] map gestures - Awaiting human verification (orchestrator). Tester CDP pre-check: see Break 5 (map1).
- [x] PASS - `grep -rni "forceatlas" apps/memory/src apps/memory/README.md | wc -l` -> 0.
- [x] PASS - make memory-format-check && memory-lint-check && pre-commit && memory-tests green, 4132 >= 4078 (twice).

**Evidence**
```
$ make env-status                      -> Env target: local (.env)
$ make memory-format-check && make memory-lint-check && make pre-commit   (exit 0)
  317 files already formatted / All checks passed! / prettier, ruff check, ruff format, biome Passed
$ make memory-tests  (x2)              -> 4132 passed in 75.39s ; 4132 passed in 72.49s
$ make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests   (the SWE's chain, run AFTER this Log entry was written)
  exit 0, 4132 passed in 61.47s; pre-commit did not modify the task file (prettier Passed). Note (d) not reproduced.
```

**Other issues found** (non-blocking, none are regressions from this task)
- Pre-existing, unchanged line: the per-type legend branch interpolates the node `type` (`+ t +`) and colour into `innerHTML` WITHOUT `esc()`. A node type containing markup (types come from the ontology/DB, not user free text in practice) would inject. `git diff` shows the line is untouched by this task; spec marks the legend unchanged. Suggest a one-line follow-up task.
- Viewport jump on release: `setCustomBBox(null)` hands the viewport back to Sigma, which re-fits the extent, so the node's SCREEN position can differ from the cursor drop point (CDP: dragged 100 px, node ended 62 px / 98 px away on two graphs, 0 on a single-node graph, which re-centres). Its graph coordinates and pin are correct and this is exactly what the spec/pulse design prescribes; the [HUMAN] run should confirm it looks acceptable ("stays exactly where dropped" in the story is true in graph space).
- `downNode` calls `sim.alphaTarget(0.3).restart()` even for a click that never moves, so every click on a live-layout node reheats the whole layout for one cycle (it re-settles via `alphaTarget(0)` on mouseup). Spec-conformant; cosmetic.
- `drawOverlay` reassigns `canvas.width/height` on every `afterRender` (reallocates the canvas every simulation frame). The old `drawHulls` did the same, so it is not a new cost class, but it is now on the hot path of every tick; consider a size-changed guard in 163/164.
- Map pin dot radius ~1.4 px (`0.35 * scaleSize(4)`), already flagged by the SWE: hand to the [HUMAN] map check / task 164.
- Test assertions use `"atlas" not in html.lower()` (SWE note c): stricter than the AC and keeps the AC grep at 0; fine.
- Worktree environment: shared local Mongo conflict (SWE note a) means the make targets that init Beanie cannot run in this worktree; environmental, not caused by the diff.

**VERDICT: PASS**
