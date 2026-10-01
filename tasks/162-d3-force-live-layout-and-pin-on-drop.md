---
id: 162-d3-force-live-layout-and-pin-on-drop
status: pending
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

- [ ] `grep -rn "forceatlas2\|forceAtlas2\|_FA2_CDN" apps/memory/src apps/memory/tests` → 0 hits; both
      `_FILE_HTML_BASE` and `viz_app._GRAPH_HTML` contain exactly one
      `from "https://cdn.jsdelivr.net/npm/d3-force@3.0.0/+esm"` import naming `forceSimulation, forceLink, forceManyBody, forceCenter`.
- [ ] `to_graph_payload` nodes carry `size` by role: `document` 12, `chunk` with `subtype: "parent"` 8,
      `chunk` with `subtype: "child"` 4, every other type (incl. `unknown`, chunk without subtype) 6 (unit tests).
- [ ] `to_graph_payload` returns `controls == {"forces": {"centre": 0.2, "repel": 8.0, "link": 0.3, "linkDistance": 80}, "display": {"nodeSize": 1.0, "linkThickness": 1.0, "labelFade": 1, "arrows": True, "edgeLabels": True}}`
      (or the tuned values the SWE records in the Log — the test and the constants move together).
- [ ] `to_embedding_map_payload` has NO `nodeSize` key, every node has `size == 4`, and
      `controls == {"display": {...}}` with no `forces` key (unit tests).
- [ ] The template keeps `const isFixed = payload.layout === "fixed";` and wraps `forceSimulation(`
      in an `if (!isFixed) {` guard (string-pinned, both variants); no `Math.random()` start remains.
- [ ] The template contains `"doubleClickNode"`, `setCustomBBox(`, `alphaTarget(0.3)`, `alphaTarget(0)`,
      `alpha(0.5)`, `dataset.layout`, `dataset.sim`, `<canvas id="overlay">`, `function drawOverlay`;
      `hulls-layer` and `drawHulls` are gone (both variants).
- [ ] Headless smoke, Tester-run and pasted in the Log (graph): from a fresh
      `make memory-query-graph QUERY="<something in the local corpus>" ` file under `.tree/graphs/`,
      `"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --no-first-run --use-angle=swiftshader --enable-unsafe-swiftshader --virtual-time-budget=10000 --dump-dom "file://<abs path>.html" | grep -o '<body[^>]*>\|<span id="counts">[^<]*\|<div id="legend">.\{0,40\}'`
      prints `<body data-layout="live" data-sim="running">`, a non-"loading" counts line (`N nodes · M edges`)
      and a filled legend. (`--disable-gpu` MUST NOT be passed — it disables WebGL and `new Sigma(` throws.)
- [ ] Headless smoke (map): same command on a `make memory-visualize-embeddings HULLS=true` file prints
      `<body data-layout="fixed">` with no `data-sim`, and the counts line is the map summary.
- [ ] [HUMAN] On the graph file: nodes keep moving after load and come to rest within ~5 s; dragging a
      document node pulls its chunks along live; on release the node stays put with a dark centre dot;
      double-clicking it removes the dot and the node drifts back into the layout.
- [ ] [HUMAN] On the map file: no point moves on load; dragging a point moves ONLY that point, which
      then shows the pin dot; the hull outline follows it; double-click removes the dot (the point stays).
- [ ] `grep -rni "forceatlas" apps/memory/src apps/memory/README.md` → 0 hits.
- [ ] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green; test count ≥ today's.

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
