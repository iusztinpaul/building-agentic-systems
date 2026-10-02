# ADR-011: Live d3-force Layout and Direct Manipulation in the Graph Renderer

- **Status:** Accepted — amends [005](005_single_graph_rendering_stack.md) Decision 1 (the layout library: graphology-layout-forceatlas2 → d3-force, one-shot → live) and Decision 2's import list; ADR-005's single-renderer and CDN decisions stand. Reaffirms [007](007_embedding_clusters_and_explicit_offline_phases.md) §2 (the Embedding map keeps its stored coordinates). — §7's capped, recency-ranked read and the `Documents` slider are reused by the rag **Memory structure** (task 173); the ranking helpers moved to `tree.memory.rag.structure`.
- **Date:** 2026-10-01
- **Deciders:** Paul (project owner)
- **Context references:**
  - `tasks/162-d3-force-live-layout-and-pin-on-drop.md`, `tasks/163-multi-select-group-drag-and-rigid-children.md`, `tasks/164-graph-controls-panel.md`, `tasks/165-full-graph-recent-documents-cap-and-slider.md`, `tasks/166-query-view-part-of-closure-and-child-count.md`, `tasks/168-query-view-relevance-documents-slider.md` (this feature's task plan)
  - `apps/memory/src/tree/memory/visualize/graph.py` (the renderer), `apps/memory/src/tree/memory/visualize/embeddings.py` (the map payload), `apps/memory/src/tree/mcp/viz_app.py` (the `ui://` variant + CSP)
  - Reference implementation: `pulse` (`/Users/pauliusztin/Documents/01-Projects/scrabble/scrabble/src/pulse/viz/static/graph.html`, `assets.py`, `render_html.py`) — ported, not shared code
  - `docs/glossary.md` — **Pinned node** (added), **Full graph** (added), **Graph payload**, **Graph renderer**, **Embedding map** (amended in this feature's grooming commit)

## Context

Every graph surface — `make memory-query-graph`, the `visualize_memory_graph` / `query_memory` /
`search_memory` MCP App iframe, the `.tree/graphs/` file fallback — draws through the ONE template
in `tree.memory.visualize.graph` (ADR-005). That template lays a graph out ONCE with
`forceAtlas2.assign(graph, {iterations: 300})` and then freezes: dragging rewrites one node's
`x`/`y` and nothing reacts, nodes are all size 6, chunks' parent/child roles show only in tooltips,
there is no selection and no way to tune the picture. The author's other project, `pulse`, has the
interaction model the human wants: d3-force owns positions live, drag holds a node with `fx`/`fy`
under `alphaTarget(0.3)` so neighbours follow, sliders reheat the simulation, and a vanilla-DOM panel
seeds from Python. The Embedding map (ADR-007 §2) is the one surface whose coordinates MEAN
something (UMAP) — it must not be simulated, yet it should not be a second, dumber viewer.

Facts verified during grooming (2026-10-01): `https://cdn.jsdelivr.net/npm/d3-force@3.0.0/+esm`
is a 200 ESM bundle that imports `d3-quadtree@3.0.1`, `d3-dispatch@3.0.1`, `d3-timer@3.0.1` from
jsdelivr itself and exports `forceSimulation/forceLink/forceManyBody/forceCenter` (and `forceX/forceY`, used since task 165); sigma 3.0.3
registers `arrow` and `line` edge programs by default and exposes `setCustomBBox`, `scaleSize`,
`doubleClickNode`, the captor's `mousemovebody`; sigma CORE does not ship `createNodeBorderProgram`
(that is `@sigma/node-border`); headless Chrome renders sigma's WebGL with
`--use-angle=swiftshader --enable-unsafe-swiftshader` but a d3 simulation does not settle inside a
`--virtual-time-budget`.

The full-graph read loads every node and edge of the tenant, embeddings included, and a
query view can leave a chunk floating when the last hop found its `next` edge but not its `part_of` one.
Both are payload problems: the picture is only as good as what Python puts in it.
A query view had no slider at all (165 scoped it out): a 70-node result with
several document stars could not be narrowed to the ones that matched.

## Decision

Eight related choices, one design:

1. **d3-force replaces graphology-layout-forceatlas2, and the layout is LIVE.** The renderer builds
   `forceSimulation(nodes)` with four forces — `forceCenter`, `forceManyBody` (slider × 30, pulse's
   `REPEL_SCALE`), `forceLink` (self-loops excluded), and a weak gravity pair `forceX(0)` + `forceY(0)`
   sharing one strength (`controls.forces.gravity`, 0.05; the `Gravity` slider, 0 disables it; added by
   orchestrator — human decision, task 165). Why gravity: `forceCenter` only translates the centroid, so
   repel alone lets a large or edge-less-heavy graph sprawl; at 0.05 the real full graph's extent drops from
   ~4500 to ~1700 graph units with no overlapping nodes, and edge-less entities stay near the stars. It does
   not keep a reveal inside a frozen viewport (measured: it contracts the pre-reveal layout, and so the
   frozen box, as much as the revealed one; the one auto-fit after a reveal, §4/§7, recovers that). The simulation ticks positions into graphology
   and lets sigma redraw; it cools to a stop on d3's default `alphaDecay`. Why d3-force over FA2's `supervisor`/
   worker: pulse's model is proven for exactly these gestures (hold with `fx`/`fy`, `alphaTarget`
   while dragging, `alpha(0.5)` reheat), it is ~8 KB, and it needs no WebWorker to feel live.
   Bias-to-least on the count of layout engines: FA2 is deleted, not kept as an option.

2. **Still pinned ESM from jsdelivr, now four imports: `graphology@0.26.0`, `sigma@3.0.3`,
   `d3-force@3.0.0` (+esm, its three deps resolved by the CDN), and the MCP ext-apps runtime.**
   ADR-005 Decision 2 stands (no vendoring, no SRI on generated `+esm` bundles, the CSP already
   allows jsdelivr). Upgrade trigger unchanged: an air-gapped deployment or a supply-chain policy.

3. **The Embedding map runs NO simulation but shares every interaction.** `layout: "fixed"` keeps
   ADR-007 §2's meaning (stored UMAP coordinates) and is now ALSO signalled by the payload's
   `controls` carrying no `forces`. Pin state, selection, group drag, rigid children, the overlay
   and the Display knobs are the same code on both layouts — the map simply never constructs a
   `forceSimulation`, so dragging moves only what the user holds.

4. **Direct manipulation semantics (the "pulse model", extended):** drag pulls unpinned neighbours
   live; a DROPPED node stays pinned (**Pinned node**, `fx != null`, dark centre dot); double-click
   unpins; Shift+click toggles selection, Shift+drag on the stage box-selects, Esc / stage click
   clears; dragging a selected node moves the whole selection keeping offsets; every dragged node
   carries its unpinned direct `part_of` children (one level, child → parent direction of the
   stored edge) by the same delta so a star keeps its shape, and those children resume settling on
   drop; user-pinned nodes are never carried. The first drag or marquee freezes the viewport with
   `setCustomBBox`, and it STAYS frozen after release so a dropped node lands where the cursor let
   go; only the zoom-fit button clears it (`setCustomBBox(null)` + `refresh()` + `animatedReset`). One
   exception (added by orchestrator — human decision, task 165): a `Documents` slider REVEAL arms ONE
   auto-fit through the same path when the reheated layout settles (at once when paused, and again at the
   settle after Resume — added by orchestrator, task 166), then re-freezes the camera on the new extent if a
   gesture had frozen it; hiding never fits, a drag, marquee or plain camera pan (pan added by orchestrator,
   task 166) before the settle cancels it, and only the camera moves (no position or pin changes). A press must
   move 3 px before it pins, reheats or freezes; only the left button starts a drag.

5. **One 2D overlay canvas for everything sigma cannot draw.** The hull canvas (`#hulls-layer`)
   becomes `#overlay` and draws, per `afterRender`, in order: cluster hulls, pin dots, selection
   rings, the marquee. Chosen over `@sigma/node-border` (a fifth CDN import for a border ring) and
   over per-node sigma programs: the canvas already exists, is `pointer-events: none`, and is
   redrawn in viewport space anyway. Upgrade trigger: rings/dots on thousands of nodes measurably
   lag — then move markers into a node program.

6. **Python decides, the JS obeys.** `to_graph_payload` resolves a per-node `size` by role
   (`document 10 > parent chunk 7 > entities 6 > child chunk 4`) and ships
   `controls: {forces, display}` defaults; `to_embedding_map_payload` ships per-node `size: 4` and
   `controls: {display}`. The panel's sliders are multipliers/switches over those values; the
   template contains no default of its own. Nothing is persisted (no localStorage/URL state) —
   a reload is "Reset to defaults" + "Unpin all". Upgrade trigger for persistence: the human asking
   for a layout they can come back to, which would be a `graphs://`-side concern, not a template one.

7. **The Full graph is capped and recency-ranked in Python; the browser hides, never re-fetches.**
   `fetch_full_graph` embeds the `query.full_graph_max_docs` (500) most-recent documents' subgraphs in four
   batched reads keyed on each row's `sources` provenance (documents → nodes → edges → unreached endpoints;
   `embedding` projected out) and stamps every row with `doc_rank`; `to_graph_payload` turns that into
   `docRank` + `controls.documents {shown: 100, total}`. The renderer's `Documents` slider sets `hidden` in
   Sigma and re-syncs `sim.nodes()` / `link.links()` so hidden nodes also leave the physics; pins survive
   hiding, selection does not; a reveal fits the camera once after it settles (§4). Ranking is Python-side on purpose: `properties.date` is an ISO string and
   `created_at` a BSON date, so Mongo would need `$dateFromString` and an in-memory sort anyway. Upgrade
   triggers: a tenant whose document rows alone scan slowly (~50k) → an aggregation; a measured slow
   `sources` scan → a `(user_id, sources)` index; a human asking to page beyond the cap → server-side paging.
   Query views carry the same slider ranked by SEARCH RELEVANCE (task 168):
   `query_memory` ranks the result's `document` rows by their best `hybrid_search` hit (RRF score,
   ties on `_id`; documents pulled in only by expansion rank last) and stamps `doc_rank` on node rows
   through each row's `sources`; `to_graph_payload(result, document_order=…)` adds
   `controls.documents.order` (`recency` | `relevance`) and shows every document by default on a
   query view (`shown = total`) — a query result is `top_k` seeds wide, so the slider narrows, it
   never caps. The rank is a viz concern: `search_memory`'s text and `deep_search_memory`'s files
   strip it, and `ranked_rows` is untouched. The hit order rather than a `_search_score` on rows
   because `expand_graph` re-hydrates every row — the seed list `query_memory` holds is the only
   relevance signal that survives.

8. **Query views are `part_of`-complete.** After expansion, two batched reads
   attach every chunk's `part_of` edge and target (child → parent → document, one-level lookahead so both
   levels come in one pass) and stamp `child_count` on parents whose children were not pulled in; the
   payload carries `childCount` and the hover card reads `child chunks  N (not shown)`. Rows are appended,
   never reordered, each marked with a transient `_closure_added` key; `ranked_rows` hands the model hop
   nodes, hop edges, then the closure's nodes and edges, marker stripped, so `search_memory`'s `max_results`
   truncation (and `deep_search_memory`'s index) keeps every hop row ahead of the closure, and the marker
   never reaches a serialized row, a file or the payload (changed by orchestrator after QA). Nothing about
   seeds, `rag` mode or `QueryResult`'s shape changes.

**Verification stance (not a design choice, recorded so it is not re-litigated per task):** the
repo has no browser test suite and this feature does not add one. Unit tests pin the HTML/JS
contract (tokens in BOTH template variants, payload keys); the Tester runs a headless-Chrome
`--dump-dom` smoke that proves the page boots and the simulation starts
(`<body data-layout="live" data-sim="running">`, filled counts/legend); gestures and settling are
human checks recorded in the task Log.

## Diagram

```mermaid
flowchart TD
    subgraph py["Python — tree.memory.visualize (decides)"]
        GP["to_graph_payload<br/>nodes[].size by role · controls{forces, display}"]
        MP["to_embedding_map_payload<br/>layout: fixed · nodes[].size=4 · controls{display}"]
        TPL["ONE template<br/>_GRAPH_STYLE · _BODY_MARKUP · _RENDER_JS"]
        FG["fetch_full_graph<br/>4 reads keyed on sources · doc_rank"] --> GP
        EX["expand_graph + part_of closure<br/>2 reads · child_count · doc_rank by relevance"] --> GP
    end

    subgraph js["Browser — the Graph renderer (obeys)"]
        SIM["d3-force simulation<br/>centre · gravity (forceX/Y) · manyBody · link<br/>LIVE layout only"]
        G["graphology graph<br/>x/y ticked in"]
        SG["Sigma 3 (WebGL)<br/>reducers: size×nodeSize, arrow/line,<br/>highlighted selection"]
        OV["#overlay 2D canvas<br/>hulls · pin dots · selection rings · marquee"]
        PANEL["Controls panel (vanilla DOM)<br/>Forces → reheat · Display → refresh<br/>Pause · Unpin all · Reset"]
        DRAG["Drag set = selection ∪ unpinned part_of children<br/>fx/fy held · alphaTarget(0.3) · drop ⇒ Pinned node"]
        DOCS["Documents slider<br/>Most recent | Most relevant (controls.documents.order)<br/>hidden in Sigma · sim.nodes()/links re-synced"]
    end

    CDN["jsdelivr (pinned +esm)<br/>graphology 0.26.0 · sigma 3.0.3 · d3-force 3.0.0<br/>(d3-quadtree/dispatch/timer resolved by CDN)"]

    GP --> TPL
    MP --> TPL
    TPL -->|"file HTML · ui:// iframe · file fallback"| SG
    TPL --> SIM
    SIM -->|tick| G
    G --> SG
    SG -->|afterRender| OV
    PANEL -->|forces| SIM
    PANEL -->|display| SG
    DRAG --> SIM
    DRAG --> G
    PANEL --> DOCS
    DOCS --> SIM
    DOCS --> G
    MP -.->|"no forces ⇒ no simulation"| SIM
    CDN -.->|view time| SG
    CDN -.-> SIM

    classDef pyNode fill:#d3f9d8,stroke:#2f9e44,color:#000;
    classDef jsNode fill:#d0ebff,stroke:#1c7ed6,color:#000;
    classDef extNode fill:#fff3bf,stroke:#f08c00,color:#000;
    class GP,MP,TPL,FG,EX pyNode;
    class SIM,G,SG,OV,PANEL,DRAG,DOCS jsNode;
    class CDN extNode;
```

## Consequences

- **Layouts are no longer deterministic and the view "breathes".** FA2 gave the same picture per
  seed; a live sim settles slightly differently each load, and sigma's auto-rescale refits the
  camera as the bounding box changes while settling (pulse accepts the same). Accepted — the pin /
  Pause / Unpin-all gestures exist precisely so the operator, not the layout, has the last word.
- **CPU while settling.** `forceManyBody` is O(n log n) per tick; a 3 000-node full graph is fine on
  a laptop and the sim stops by itself (`alphaDecay`). `Pause` is the escape hatch for larger graphs;
  a WebWorker layout stays a non-goal until a measured need.
- **Contract changes, all surfaces at once.** Node `size` and `controls` enter the **Graph payload**
  and the map payload (`nodeSize` leaves); `#hulls-layer` is renamed `#overlay`; tests that pinned
  the FA2 guard string move to the `forceSimulation` guard. The `ui://` iframe and the file variant
  stay token-identical except for the ext-apps runtime and the body height.
- **Offline regression unchanged.** Four CDN imports instead of three; no network → blank canvas,
  as ADR-005 accepted.
- **Headless evidence is bounded.** A DOM dump proves boot and "simulation running", not settling or
  gestures; those stay human checks until a browser automation dependency earns its keep.
- **The Embedding map gets richer without losing meaning.** Coordinates are untouched unless the
  user drags; pin dots, selection and the Display knobs come for free from the shared code.
- ADR-005's text is unchanged except its Status line; its Decision 1/2 mentions of
  `graphology-layout-forceatlas2` read as history.
- **The full graph is no longer "everything".** Older documents beyond the cap are
  not embedded and rows with no provenance that nothing links to are absent; the summary line and header say
  so (`100 of 342 documents`). The dashboard's own template inherits the cap without a slider.
- **Payload contract grows again, all surfaces at once:** `docRank`, `controls.documents` (+ `order`, task 168), `childCount`;
  tests pin them in both template variants.
- **Two more reads per query view, a few more rows.** Chunks never float;
  `search_memory` answers can be slightly larger before truncation.
- **The slider means two things.** On the Full graph it
  reveals older documents (default 100 of N); on a query view it narrows to the most relevant
  (default all of N). The row label (`Most recent` / `Most relevant`) and the summary adjective
  (`most-recent` / `most-relevant`) say which; the header counts read the same.
