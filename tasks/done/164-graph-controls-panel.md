---
id: 164-graph-controls-panel
status: done
feature: dynamic-graph-viz
---

# Graph controls panel: Forces, Display, Actions (and README)

Tags: `viz`, `memory`, `mcp`, `docs`
Depends on: 162, 163
Blocks: —
Implements: ADR-011

## Scope

Add a collapsible, vanilla-DOM control panel to the shared renderer, built with pulse's
`panelTitle` / `rangeRow` / `checkboxRow` helpers (`pulse/viz/static/graph.html` l.243–342), reading
its defaults from `payload.controls` (task 162). The panel is the user-facing half of what 162 wired
into the sim and the reducers; no new physics.

### Markup / CSS (`_BODY_MARKUP`, `_GRAPH_STYLE`)
- `<div id="panel"><button id="panel-toggle">Controls</button><div id="panel-body"></div></div>`
  positioned `absolute; top: 10px; left: 12px; z-index: 2; width: 190px`, same card style as
  `#legend` (white 0.92 bg, border, radius 10, 11 px font). Expanded on load; the toggle collapses
  `#panel-body` (`hidden`) leaving just the button. Sliders: label + live readout (`toFixed`) + `<input type=range>`,
  accent colour `--accent1`.

### Sections (built by JS from `payload.controls`; live layout = all three, `layout: "fixed"` = Display + Unpin all only)
- **Forces** (each change applies to the force and reheats `sim.alpha(0.5).restart()` unless paused):
  `Centre force` 0–1 step 0.01 → `centre.strength(v)`; `Repel force` 0–20 step 0.01 → `repel.strength(-v * REPEL_SCALE)`;
  `Link force` 0–1 step 0.01 → `link.strength(v)`; `Link distance` 0–500 step 1 → `link.distance(v)`.
- **Display** (refresh only, never reheat): `Node size` 0.2–3 step 0.05 → `display.nodeSize`;
  `Link thickness` 0.2–3 step 0.05 → `display.linkThickness`; `Label fade` 0–12 step 0.5 →
  `renderer.setSetting("labelRenderedSizeThreshold", v)`; `Arrows` checkbox → `display.arrows`
  (edge reducer `type: "arrow" | "line"`); `Edge labels` checkbox → `renderer.setSetting("renderEdgeLabels", on)`.
- **Actions**: `Pause` / `Resume` (one button whose label flips; `sim.stop()` / `sim.restart()`;
  `document.body.dataset.sim = "paused"` while paused; while paused a drag moves only the drag set
  and does NOT restart the sim, and dropping keeps it paused); `Unpin all` (`fx = fy = null` on every
  node, redraw overlay, reheat unless paused / no sim — shown on the map too); `Reset to defaults`
  (set every slider/checkbox back to `payload.controls`, apply each, reheat once; does NOT unpin, does
  NOT resume).
- Hide Forces + Pause/Resume + Reset's force part on the map (`!payload.controls.forces`). The
  existing `Cluster hulls` checkbox stays in the header (unchanged).

### README (`apps/memory/README.md`, the `make memory-query-graph` / Embedding map section ~l.340–375)
- One short "Interacting with a graph" paragraph: live layout; drag = pin (dot), double-click = unpin,
  Shift+click / Shift+drag = select, Esc = clear, Controls panel (Forces reheat, Display, Pause,
  Unpin all, Reset); the map has no simulation but the same selection/pin/Display. Keep it under 12 lines.

### Tests (`/squid-testing-python`)
- `test_graph.py` / `test_viz_app.py` token pins (both variants): `id="panel"`, `id="panel-toggle"`,
  `function rangeRow`, `function checkboxRow`, each label string (`Centre force`, `Repel force`,
  `Link force`, `Link distance`, `Node size`, `Link thickness`, `Label fade`, `Arrows`, `Edge labels`,
  `Pause`, `Resume`, `Unpin all`, `Reset to defaults`), `"labelRenderedSizeThreshold"` and
  `"renderEdgeLabels"` via `setSetting`, `dataset.sim = "paused"`, and that the Forces section is
  gated on `controls.forces`.
- A payload test: the slider defaults in the panel are READ from `payload.controls` (the template
  contains no numeric default for them — grep that e.g. `0.2`/`80` do not appear as literals in the
  panel code; pin by asserting `controls.forces.centre` etc. are referenced).

### Feature e2e (this is the last task — run the whole feature's verification here)
- `make memory-query-graph` (full graph) and `make memory-visualize-embeddings HULLS=true` locally;
  headless smoke on both files (162's command) now also greps `id="panel"` and the `Controls` button;
  `[HUMAN]` interaction pass below. Also one UI-less MCP call (`fastmcp call … visualize_memory_graph`
  with `as_html_file=true`) to prove the file fallback carries the panel. Never prod.

## Out of scope
- Persisting settings (localStorage / URL) — explicitly a non-goal; a reload is Reset.
- New forces (collide, radial), per-type force tuning, exporting positions.

## Acceptance Criteria

> `[HUMAN]` criteria are executed by the orchestrator through Claude in Chrome (real browser, gestures + GIF), with results pasted into `## Log`; the human does a final glance. The SWE/Tester do not block on them.

- [x] Both variants contain the panel markup and every label/button string listed in Tests (unit tests).
- [x] The Forces section, Pause/Resume and the force half of Reset are emitted only when
      `payload.controls.forces` exists (template gate pinned); Display + Unpin all are emitted for every payload.
- [x] Forces callbacks call `reheat()`; Display callbacks call `renderer.refresh()` or `setSetting` and
      never `reheat()` (pin the two code paths with tokens, as pulse separates them).
- [x] Headless smoke (Tester, Log): graph file prints `data-layout="live" data-sim="running"`, counts, and
      the dump contains `id="panel"` with `Centre force` and `Unpin all`; map file prints `data-layout="fixed"`,
      its dump contains `Node size` and `Unpin all` but NOT `Centre force` or `Pause`.
- [x] [HUMAN] Moving `Link distance` from 80 to 300 visibly spreads the graph and it re-settles; moving
      `Node size` to 2 doubles node radii without any movement; unchecking `Arrows` turns heads into plain
      lines; unchecking `Edge labels` hides relationship labels; `Label fade` at 12 hides most labels until zoomed in.
- [x] [HUMAN] `Pause` freezes all motion (dragging a node moves only it, neighbours stay); `Resume` restarts
      settling; `Unpin all` removes every pin dot and the graph re-settles; `Reset to defaults` returns every
      slider readout to its load value and leaves pins untouched.
- [x] [HUMAN] The map shows only Display + Unpin all; `Unpin all` clears dots and no point moves.
- [x] [HUMAN] Collapsing the panel leaves only the `Controls` button; the legend and zoom buttons are unobstructed in a 760 px-high iframe.
- [x] (added by orchestrator) Fast clicking: Sigma emits `doubleClickNode` / `doubleClickStage` for ANY two
      clicks <300 ms apart (a drag's trailing click counts). With Shift, `doubleClickNode` toggles the
      selection like a click (no unpin, no zoom); without Shift it unpins a PINNED node and, on an
      unpinned one, acts as a plain click (selects only that node, same code path as `clickNode`) —
      amended by orchestrator, supersedes "does nothing". A plain `doubleClickStage` acts as a plain stage
      click (clears the selection). Neither handler ever zooms (`preventSigmaDefault`) and both ignore a
      drag's trailing click. Unit-test token pins; CDP: Shift+click 3 nodes 150 ms apart → all 3 selected,
      camera ratio unchanged; two quick plain clicks on two unpinned nodes → only the second selected;
      quick stage click after a node click → selection cleared.
- [x] (added by orchestrator) Esc cancels an active marquee: no selection change, the box disappears, the
      viewport stays (unit-test token pins + CDP).
- [x] (added by orchestrator) Lost-mouseup guard: in the `mousemovebody` handler, a move with
      `e.original.buttons === 0` while a drag or marquee is active ends it as if released at the last
      held position (unit-test token pins + CDP).
- [x] README paragraph present; `grep -n "Unpin all" apps/memory/README.md` → 1 hit.
- [x] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green;
      feature e2e per `.agents/skills/run-pipelines-e2e/SKILL.md` steps for `memory-query-graph` /
      `memory-visualize-embeddings` recorded in the Log; `.tree/graphs/*.html` cleaned up (cleaned by
      orchestrator after human pass).

> The four [HUMAN] ACs above: [HUMAN] passed — human visual check 2026-10-01 (ticked by the Tester; independently re-driven over real CDP input, see the Tester log).

## User Stories

### Story: Operator untangles a dense graph
1. Full graph open; the panel shows Forces / Display / Actions at the top-left.
2. Operator drags `Repel force` from 8 to 15: nodes push apart and re-settle within a few seconds.
3. Operator drags `Link distance` to 200: stars widen; readout shows `200`.
4. Operator clicks `Reset to defaults`: readouts return to `8.00` / `80`, layout re-settles; pins untouched.

### Story: Operator freezes the graph to read it
1. Operator clicks `Pause`; the label becomes `Resume`; all motion stops; `data-sim` is `paused`.
2. Operator drags a document: only the document (and its rigid chunks) move; nothing else reacts.
3. Operator clicks `Resume`: the graph settles around the new position.

### Story: Operator cleans up after rearranging
1. After pinning six nodes by dragging, operator clicks `Unpin all`.
2. Every pin dot disappears and the graph re-settles into a balanced layout.

### Story: Operator declutters a big graph
1. Operator sets `Label fade` to 8: only large nodes keep labels at the fitted zoom; zooming in reveals more.
2. Operator unchecks `Edge labels` and `Arrows`: relationship text disappears; edges become plain lines.
3. Operator sets `Node size` to 0.5: the graph looks sparser; nothing moves.

### Story: Operator tunes the map's look without disturbing it
1. `make memory-visualize-embeddings HULLS=true`: the panel shows only Display and `Unpin all`.
2. `Node size` 2 enlarges every point in place; hulls stay; `Unpin all` clears any pin dots.

### Story: Assistant's file fallback has the same controls
1. In a UI-less client, `visualize_memory_graph(as_html_file=true)` writes `.tree/graphs/...html`.
2. Opened in a browser, the file shows the same panel, live layout and pins as the inline iframe.

---

Blocked by: 162, 163

## Log

### [SWE] 2026-10-01 18:50 — Implementation

**Files modified**
- `apps/memory/src/tree/memory/visualize/graph.py`:
  - `_GRAPH_STYLE`: `#panel` card at top 10 / left 12, 190 px wide, styled like the legend, with `max-height` and `overflow: auto`. Collapsed, the card hugs the button (`#panel:has(#panel-body[hidden]) { width: auto; }`). Rows, readouts and buttons are styled; slider and checkbox accent is `--accent1`.
  - `_BODY_MARKUP`: `<div id="panel"><button id="panel-toggle" type="button">Controls</button><div id="panel-body"></div></div>` inside `#stage`.
  - `_RENDER_JS`, panel:
    - `#panel-body` is cleared before the early return, so the iframe rebuilds it per tool result.
    - `panelSection(title)` APPENDS a titled block to `#panel-body`. Task 165 only has to build its `Documents` section before Forces.
    - `rangeRow` / `checkboxRow` / `actionButton` each register a reset closure.
    - Forces rows sit under `if (forces) {` and call `sim.force(name).strength/distance(v); reheat();`.
    - Display rows: `display.* = v; renderer.refresh()`, or `renderer.setSetting("labelRenderedSizeThreshold" | "renderEdgeLabels", …)`.
    - Actions: Pause/Resume (under `if (forces) {`), Unpin all, Reset to defaults.
  - `_RENDER_JS`, pause:
    - `let paused` plus ONE guarded `reheat()` (`if (sim && !paused) sim.alpha(0.5).restart();`). Sliders, unpin, Unpin all and Resume all go through it.
    - `startDrag` restarts only `if (sim && !paused)`.
  - `_RENDER_JS`, orchestrator extras:
    1. `toggleSelected()` is shared by `clickNode` and `doubleClickNode`. `doubleClickNode` calls `e.preventSigmaDefault()` first, then the `pointerMoved` guard, then Shift → toggle, else unpin only a pinned node. New handler: `doubleClickStage` → `preventSigmaDefault()`.
    2. Esc with an active marquee sets `marquee.cancelled = true`. A cancelled box is not drawn, its moves are still swallowed (no pan), and the release does not apply it.
    3. `mousemovebody`: `if (e.original.buttons === 0) { captor.handleUp(e.original); return; }`. This releases sigma's own stuck press and emits the existing `mouseup` exactly once.
  - Module docstring: one sentence on the panel.
- `apps/memory/README.md`: a 7-line "Interacting with a graph" paragraph after the Query CLI block. `grep -n "Unpin all"` → 1 hit (l.356).
- `apps/memory/tests/unit/memory/visualize/test_graph.py`: 20 new tests in two new sections (the extras; the Controls panel). The drawOverlay order test now indexes `if (marquee && !marquee.cancelled)`.
- `apps/memory/tests/unit/mcp/test_viz_app.py`: the both-variant token list gains every panel label/token and the three extras' guard lines.

**Tests**
- Unit: 4265 passed, 0 failed (163 baseline 4175), via `make memory-tests`. Before implementation the new tests were red: 89 failures, all `AssertionError` or `ValueError: substring not found`, none from imports. They went green after it.
- Integration: N/A (no suite in this repo).

**Acceptance criteria**
- [x] Panel markup and every label/button string in both variants:
  - `test_viz_app.py::test_both_variants_carry_the_embedding_map_extensions[iframe|file-*]`
  - `test_graph.py::test_template_carries_the_controls_panel[*]`
  - `::test_the_panel_markup_is_a_toggle_and_a_body_at_the_top_left`
- [x] Forces / Pause / force-Reset gated on `controls.forces`; Display and Unpin all always present:
  - `::test_the_forces_pause_and_force_reset_are_gated_on_controls_forces` (Reset's force half = the force rows' reset closures, which only exist inside `if (forces) {`)
  - CDP map: sections `["Display","Actions"]`, buttons `["Unpin all","Reset to defaults"]`.
- [x] Forces call `reheat()`, Display never does:
  - `::test_forces_callbacks_reheat_and_display_callbacks_only_refresh` (4× `reheat(); }` in the gated blocks, 0 `reheat` in Display, 3 `refresh` + 2 `setSetting`)
  - `::test_each_force_slider_drives_its_force[*]`
  - Defaults are read from the payload: `::test_the_slider_defaults_are_read_from_the_payload` (refs `forces.centre` … `display.edgeLabels`; `80` / `0.3` / `8.0` / `8,` absent from the panel code).
- [ ] Headless smoke: the Tester owns this. SWE pre-run is in Evidence. **Grep the DOM text markers `>Centre force<` / `>Pause<`, not the bare words.** `--dump-dom` also serializes the inline module script, which contains `"Centre force"` and `"Pause"` as JS strings, so a naive grep on the MAP dump always matches.
- [ ] [HUMAN] ×4: for the orchestrator. The SWE ran every one over real CDP input (Evidence).
- [x] (orchestrator) Fast clicking:
  - `::test_a_double_click_never_zooms`, `::test_a_shift_double_click_toggles_like_a_click`, `::test_a_plain_double_click_unpins_only_a_pinned_node`, `::test_a_drags_trailing_click_never_completes_a_double_click`
  - CDP: 3 Shift+clicks 150 ms apart → 3 selected, ratio 1 → 1.
- [x] (orchestrator) Esc cancels a marquee:
  - `::test_esc_cancels_an_active_marquee_and_keeps_the_selection`, `::test_a_cancelled_marquee_is_neither_drawn_nor_applied`
  - CDP evidence below.
- [x] (orchestrator) Lost mouseup:
  - `::test_a_lost_mouseup_ends_the_gesture_at_the_last_held_position`
  - CDP evidence below.
- [x] README paragraph, 1 `Unpin all` hit.
- [ ] Format / lint / pre-commit / tests are green (Evidence) and the feature e2e is recorded below. **`.tree/graphs/*.html` cleanup is deliberately DEFERRED**: the orchestrator asked for fresh files for the [HUMAN] pass. Delete them after it. This box stays open for that reason only.

**Evidence**
```
$ make env-status                                   -> Env target: local (.env)
$ make memory-format-check && make memory-lint-check && make pre-commit
  317 files already formatted / All checks passed! / prettier, ruff check, ruff format, biome: Passed
$ make memory-tests                                 -> 4265 passed in 59.66s
$ node --check on the module <script> of BOTH variants (file + ui:// iframe)   -> SYNTAX OK

# Feature e2e. The make targets still cannot init Beanie here: a READ-ONLY listIndexes shows the
# shared Mongo `documents.user_source_uri_unique` is {user_id, source_uri}, from the main checkout's
# branch. So the files come from 162's read-only wrapper (run_readonly.py) + synthetic_map.py.
# No Mongo index was touched.
Wrote self-contained graph HTML (171 nodes, 236 edges) to apps/memory/.tree/graphs/graph-20261001-154055.html
Wrote self-contained graph HTML (78 nodes, 75 edges)  to apps/memory/.tree/graphs/langgraph-agent-memory-20261001-154059.html
(visualize_embeddings.py --hulls via wrapper -> "No clustering run found for this user …", exit 1, as in 162)
synthetic map (real to_embedding_map_payload(hulls=True) + render_embedding_map_file) -> apps/memory/.tree/graphs/embedding-map-20261001-154102.html

# headless --dump-dom smoke (162's command), grepping DOM text markers
graph-…154055        <body data-layout="live" data-sim="running"> | 171 nodes · 236 edges | <div id="panel"> |
                     <button id="panel-toggle" type="button">Controls</button> | >Centre force< >Link distance< >Node size< >Pause< >Unpin all< >Reset to defaults< | legend filled
langgraph-…154059    same, 78 nodes · 75 edges
embedding-map-…154102 <body data-layout="fixed"> | 86 chunks in 3 clusters (+6 noise) | <div id="panel"> | Controls button |
                     >Node size< >Unpin all< >Reset to defaults< | legend filled ; >Centre force< count 0, >Pause< count 0

# UI-less MCP file fallback (runs on the stdio server through the same read-only wrapper)
$ fastmcp call --command "bash -c '… uv run python run_readonly.py scripts/serve_mcp.py --user-id 6abe3b76…'" \
    --target visualize_memory_graph --input-json '{"as_html_file": true}'
  Wrote self-contained graph HTML (171 nodes, 236 edges) to apps/memory/.tree/graphs/graph-20261001-154841.html
  + resource_link graphs://graph-20261001-154841.html ; exit 0
  dump-dom: <body data-layout="live" data-sim="running"> | 171 nodes · 236 edges | <div id="panel"> | Controls | >Centre force< >Pause< >Unpin all<
  The file's template is byte-identical to the CLI file's (DATA line stripped).

# Real CDP input (Input.dispatchMouseEvent / dispatchKeyEvent; sliders = a click on the track + arrow
# keys, which fires `input`), headless Chrome 1400x900, swiftshader, on COPIES of the files above with
# one `window.__t = {…, get paused()}` line. Runtime.exceptionThrown: [] in EVERY run.
[HUMAN] Display/Forces (graph-…154055, after settle 14.4 s):
  load readouts: Centre 0.20, Repel 8.00, Link 0.30, Distance 80, Node size 1.00, Link thickness 1.00, Label fade 1.0; Arrows/Edge labels checked
  Node size -> "2.00": every node's display size x2.000 (min = max ratio), max node move 0 px, data-sim stays "settled"
  Arrows off -> every edge's display type "line"; Edge labels off -> renderEdgeLabels false
  Label fade -> "12.0": labels drawn 36 -> 0 at the fitted zoom, 4 after 3 wheel zoom-ins; nodes moved 0, sim "settled"
  Link distance -> "300": data-sim "running" at once, link.distance() = 300, re-settled after 9.1 s; mean node-to-centroid
    distance 1395.6 -> 1713.0 graph units (screenshots: graph-loaded.png vs graph-linkdistance300.png, the stars visibly widen)
[HUMAN] Actions:
  Pause (while running): button -> "Resume", data-sim "paused", every node moved 0 over 1 s
  drag the doc while paused: doc + its 3 rigid parent chunks moved 748.2 graph units, all 167 other nodes 0;
    data-sim stays "paused" after the drop, doc pinned
  Resume: button -> "Pause", data-sim "running" within 300 ms, other nodes move up to 1023 units in 1.5 s, settled after 11.3 s
  Unpin all with 4 pins (dot alpha 255 x4): 0 pinned, dot alpha 0 x4, data-sim "running", re-settled after 11.6 s
  Reset to defaults (after Repel 15.00, Distance 200, Node size 2.00, Label fade 6.0, Arrows + Edge labels off, 1 pin):
    every readout back to its load value (8.00 / 80 / 1.00 / 1.0 …), both boxes checked; sim forces centre 0.2,
    repel -240, link 0.3, distance 80; edge type "arrow", renderEdgeLabels true, threshold 1; pin KEPT; data-sim "running"
  Reset while paused: data-sim stays "paused", button still "Resume", Repel back to 8.00
[HUMAN] Map (embedding-map-…154102): sections Display + Actions only; buttons Unpin all, Reset to defaults;
  Node size 2 -> sizes x2.000, max point move 0; 2 points pinned by drag (dot alpha 255) -> Unpin all -> 0 pinned,
  dot alpha 0, max point move 0, sim null, no data-sim
[HUMAN] Collapse at 1000x760 (Emulation.setDeviceMetricsOverride): expanded panel [12,57]-[202,499] (scrollHeight 440),
  no overlap with the legend [884,57]-[988,203] or zoom [956,640]-[988,748]; collapsed: body hidden, panel [12,57]-[93,91],
  innerText "Controls"; re-expands (graph-760-expanded.png / graph-760-collapsed.png)
(orchestrator) fast clicks (settled):
  Shift+click 3 nodes 150 ms apart -> selected 3, camera ratio 1 -> 1
  Shift+double-click one node (60 ms) -> selection unchanged (two toggles), ratio 1
  plain double-click on an unpinned node -> ratio 1, not pinned; double-click on the empty stage -> ratio 1
  plain double-click on a PINNED node -> unpinned, ratio 1 (162 behaviour kept)
  Shift-stage click, then within 40 ms a Shift-drag (its trailing click = 2nd click of the pair) -> node stays pinned, selection unchanged, ratio 1
(orchestrator) Esc mid-marquee (one node selected): fill (230,89,13,20) before Esc -> (0,0,0,0) after; 5 more held moves
  -> camera [0.5,0.5,1] unchanged; release -> selection unchanged ([ibm]), no marquee pixel; a later plain Esc clears
(orchestrator) lost mouseup: node press + 6 held moves, then 8 buttonless moves without a release -> node moved 0 px
  after the last held move (lands exactly at it), pinned, camera unchanged, captor.isMouseDown false.
  Shift-marquee + 6 held moves, then 8 buttonless moves -> selection == the nodes inside the LAST HELD box (1 == 1),
  no marquee pixel, camera unchanged, isMouseDown false; the next real drag lands 0 px from its release
163 regression re-run (the 163 scenarios, harness aim moved right of the panel) on graph-…154055 / map: all reproduce:
  landing drop 0.8 px, rigid 0.9 px; select 3 rings / toggle / Esc / click; group 156-157 px, offset dev 1 px;
  unselectedDoc rigid 0.6; userPin 0 px; marquee 61 = in-box set, camera still; unpin; rightButton 0 px; map 16 selected, (100,60)
```

**Notes**
- **Deliberate deviations (please review):**
  1. **Resume REHEATS**, using `sim.alpha(0.5).restart()` via `reheat()` instead of a bare `sim.restart()`. On a layout that cooled before or while paused, `restart()` alone ticks once and ends, so "Resume restarts settling" would not hold. Pinned by `test_pause_stops_the_simulation_and_resume_reheats_it`.
  2. **Forces use `sim.force("centre" | "repel" | "link")`** instead of named consts. This is the same d3 object and needs no extra variables.
  3. **"Reheat once" on Reset**: each force row's reset calls `reheat()`, so a Reset makes 4 synchronous `alpha(0.5).restart()` calls in one task. That is idempotent (same alpha, same re-armed timer), so it equals one reheat. I chose this over a suppress flag.
  4. **Collapsed card hugs the button** (`width: auto`) rather than staying 190 px wide.
  5. **Empty payload**: `#panel` is hidden, so "No graph data returned." shows no orphan card.
- **Residual fast-click quirks, per the orchestrator's rule (not bugs in it).**
  - (a) A plain click on a node within 300 ms of a click elsewhere, or of a drag's release, arrives as `doubleClickNode`. If that node is pinned, it unpins. If it is not pinned, the click is swallowed (no select).
  - (b) A plain click on the stage within 300 ms of another click does not clear the selection.
  - Both behave as before 164, except that the camera no longer zooms. Making a plain double-click on an unpinned node act as a click would be harmless for real double-clicks. I did not do it, because the rule says "does nothing".
- **Sigma internals used:** `captor.handleUp(e.original)`. It is a bound method of sigma 3.0.3's `MouseCaptor`; it clears `isMouseDown` and emits `mouseup`. Without it, sigma's stuck press pans the camera under the bare pointer once our gesture ends. CDP confirms `isMouseDown` is false afterwards.
- **MCP call side effects to know about:**
  - The stdio server's lifespan ran `ensure_indexes` on `memory`: "Text index ensured / Compound indexes ensured / vector index already up-to-date". `rag/indexing.py` is identical to the main checkout's HEAD (`git diff --stat HEAD d4e7110 -- …indexing.py` is empty), so `createIndex` with identical specs is a no-op. A read-only `getIndexes()` afterwards lists the same 11 `memory` indexes. Proof that nothing was created or rebuilt: a read-only `$indexStats` taken after the call (15:52 UTC) shows every `memory` and `documents` index with `accesses.since` between 10:52 and 10:54 UTC (mongod start 10:33 UTC), hours before the call at about 15:48 UTC.
  - `documents` (the conflict) was skipped by the wrapper.
  - The file branch's best-effort `webbrowser.open` opened `graph-20261001-154841.html` in the local browser.
  - `configure_opik` rewrote `~/.opik.config`, as any server boot does.
- **Harness changes** in `<scratchpad>/164/`: `cdp.mjs` (163's, with `emptySpot` searching from x = 230) and `re163/scenarios.mjs` (163's, with the `isolatedEntities` x-floor raised from 30 to 230). The panel now covers x 12–202 at the top-left, so an aim there would hit the panel instead of the stage. This is a harness fix, not a code change.
- **Gesture harness for the Tester:** `<scratchpad>/164/`:
  1. `python3 instrument.py <rendered.html> <copy.instr.html>` adds `get paused()` to 163's `window.__t`.
  2. Start Chrome as in 163 (`--remote-debugging-port=9333 --window-size=1400,900`).
  3. Run `node cdp.mjs file://<copy> <display|actions|collapse|fastClicks|escMarquee|lostMouseup|resetKeepsPins>` (graph) or `… mapPanel` (map).
  4. For the 163 scenarios, use `re163/cdp.mjs`.
  5. Screenshots land in the cwd.
  - `<scratchpad>` = `/private/tmp/claude-501/-Users-pauliusztin-Documents-01-Projects-AI-Engineer-Handbook-building-agentic-systems-building-agentic-systems/944c4516-3945-43f4-8111-e0c95524323d/scratchpad`.
- **Fresh files for the [HUMAN] pass** (under `apps/memory/.tree/graphs/`):
  - `graph-20261001-154055.html` (full graph, 171 nodes)
  - `langgraph-agent-memory-20261001-154059.html` (query view)
  - `embedding-map-20261001-154102.html` (synthetic map, hulls on)
  - `graph-20261001-154841.html` (the MCP fallback file)
  - Older 162/163 files there were left alone.
- **Stale ADR text** (the PA owns it, unchanged here): ADR-011 §4 still reads "for the duration of any drag or marquee" (163 note). §6/Decision text says "the template contains no default of its own"; the 162 fallback `display` literal for hand-made payloads without `controls` remains the only JS default.

### [SWE] 2026-10-01 18:57 — Addendum: double-click acts as a click (orchestrator amendment)

**Change.** This supersedes extra #1's "does nothing", and the AC line has been updated.
- A plain `doubleClickNode` on an UNPINNED node now acts as a plain click and selects only that node.
- A plain `doubleClickStage` acts as a plain stage click and clears the selection.
- A Shift double-click still toggles the node.
- A plain double-click on a PINNED node still unpins it.
- Both handlers call `preventSigmaDefault()` first, so there is no zoom.
- Both handlers keep the `pointerMoved` guard, so a drag's trailing click never completes a pair.

**Shared click path.** `clickNode`, `clickStage` and both double-click handlers now go through one helper per target: `clickOnNode(node, shiftKey)` and `clickOnStage(shiftKey)`. These replace `toggleSelected`.

**Tests.** In `test_graph.py`, these tests were rewritten or added:
- `test_a_double_click_never_zooms`, which now covers both handlers;
- `test_clicks_and_double_clicks_share_one_click_path`;
- `test_a_double_click_acts_as_a_click_unless_it_unpins`;
- `test_a_drags_trailing_click_never_completes_a_double_click[node|stage]`.

`test_viz_app.py` tokens: the stage handler opener `renderer.on("doubleClickStage", (e) => {` and `clickOnNode(e.node, shiftKey)`.

`make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` passes: **4268 passed**.

**CDP.** I re-ran `fastClicks` on the fresh `graph-20261001-155433.html` with real input. There were no exceptions.
```
Shift+click 3 nodes 150 ms apart              -> selected 3, ratio 1 -> 1
Shift+double-click one node                   -> selection unchanged, ratio 1
plain double-click, unpinned node             -> selection [that node], not pinned, ratio 1
two quick plain clicks, 2 unpinned nodes 150 ms -> selection [second node only], ratio 1
node click then stage click 150 ms later      -> selection 0, ratio 1
stage double-click                            -> selection 0, ratio 1
plain double-click, pinned node               -> unpinned, ratio 1
drag whose trailing click is the 2nd click    -> stays pinned, selection unchanged, ratio 1
```
I also re-ran the 163 regressions `select` and `unpin`. Both reproduce their earlier numbers.

**Corrections to the entry above.**
- The ADR-011 §4 "stale" note is withdrawn: §4 already reads "it STAYS frozen after release" (amended in the 163 commit). No ADR edit is needed.
- The "residual fast-click quirks" note no longer applies.
- The [HUMAN] files listed above were rendered before this change and have been deleted.

**Current [HUMAN] files** (under `apps/memory/.tree/graphs/`):
- `graph-20261001-155433.html`
- `langgraph-agent-memory-20261001-155448.html`
- `embedding-map-20261001-155451.html`

`graph-20261001-154841.html` (the MCP fallback proof) predates this amendment. It is the same template apart from the double-click handlers.

### [Tester] 2026-10-01 19:50 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check && make memory-lint-check && make pre-commit`: 317 files formatted, ruff clean, prettier / ruff / biome Passed). `make env-status` = local.
- Unit tests: `make memory-tests` run TWICE: 4268 passed / 0 failed (56.99 s, 57.65 s)
- Integration tests: N/A (no suite in this repo)
- Warnings: 0 failures; no warnings gate tripped. 0 `print(` in `graph.py`.
- Template check: I re-rendered the graph + map files from the CURRENT `graph.py` (SWE's DATA lines re-injected); the graph template is byte-identical to the SWE's `graph-20261001-155433.html` apart from the DATA line.

**E2E adversarial pass** (real CDP input, headless Chrome 154 on `file://` copies with `window.__t`; `Runtime.exceptionThrown` = [] in EVERY run below; harness + logs in `<scratchpad>/qa164/`)
- Happy path, headless smoke (162 command, grepping DOM markers): graph file -> `<body data-layout="live" data-sim="running">`, `171 nodes · 236 edges`, `id="panel"`, `<button id="panel-toggle" type="button">Controls</button>`, `>Centre force<` `>Link distance<` `>Node size<` `>Pause<` `>Unpin all<` `>Reset to defaults<` each x1. Map file -> `data-layout="fixed"`, `86 chunks in 3 clusters (+6 noise)`, `id="panel"`, `>Node size<` 1, `>Unpin all<` 1, `>Reset to defaults<` 1, `>Centre force<` 0, `>Pause<` 0 (PASS).
- Happy path, SWE scenarios independently re-run (display, actions, collapse, fastClicks, escMarquee, lostMouseup, resetKeepsPins, mapPanel): all reproduce the SWE numbers: Node size 2 -> x2.000, 0 px move; Arrows off -> all edges "line"; Label fade 12 -> labels 36 -> 0; Link distance 300 -> running at once, re-settles 7.6 s, spread 1395.6 -> 1713.0; Pause -> 0 px motion, paused drag moves doc + 3 rigid kids only (others 0), data-sim stays paused; Resume -> running; Unpin all (4 pins) -> 0 pinned, dots gone; Reset -> every readout/force/display back to default, pin kept, Reset while paused stays paused; 760 px collapse: no overlap, collapsed card = "Controls"; Shift+click x3 150 ms apart -> 3 selected, ratio 1 -> 1; two quick plain clicks -> only 2nd selected; node-click + stage-click 150 ms -> cleared; stage dblclick -> cleared, ratio 1; plain dblclick pinned -> unpinned; drag trailing click -> no unpin / no toggle; Esc mid-marquee -> fill (230,89,13,20) -> (0,0,0,0), camera [0.5,0.5,1] unchanged, selection unchanged, plain Esc then clears; lost mouseup: node lands 0 px from last held move, marquee == last held box, `isMouseDown` false, next drag 0 px; map: sections Display+Actions, Unpin all clears dots, 0 point movement, `sim` null (all PASS).
- Break 1 (boundary: slider extremes, `adv.mjs sliderExtremes`): Home/End on EVERY slider: Centre 0.00/1.00, Repel 0.00/20.00, Link force 0.00/1.00, Link distance 0/500, Node size 0.20/3.00, Link thickness 0.20/3.00, Label fade 0.0/12.0, each held 1.8 s: 171 nodes, 0 non-finite positions every time, no exception. Pathological combos (repel 0 + distance 0 + link 1 + centre 0; repel 20 + distance 500 + link 0 + centre 1): 0 NaN/Infinity after 3 s. Reset afterwards: readouts 8.00 / 80 / 0.20 / 0.30. Node size 0.20 (drawn size 1.2): a real click still selects a node (PASS).
- Break 2 (state: Pause -> drag -> Unpin all -> Resume ordering, `pauseOrdering`): Pause: paused true, data-sim paused. Drag while paused: node pinned, other nodes 0 px. `Unpin all` while paused: 0 pins, dot alpha 0, **nothing moves (0 px)**, still paused, alpha unchanged (0.456). Link distance slider while paused: force becomes 250, 0 px motion. Dblclick-unpin while paused: unpinned, 0 px, still paused. Resume: running, others move up to 654 units in 1.5 s, settles 12.3 s, 0 non-finite (PASS).
- Break 3 (state: rapid toggling, `rapidPause`): 21 real clicks 40 ms apart -> paused true / data-sim paused / label "Resume" / 0 px motion over 800 ms; 20 synchronous `.click()` -> still paused (even count) and consistent; 3 synchronous clicks -> running, nodes move (state, label, data-sim and timer always agree) (PASS).
- Break 4 (collapse/expand during a simulation, `collapseWhileRunning`): 6 toggles while the Link distance 300 re-settle runs: sim keeps running (alpha 0.35 -> 0.07 decaying), slider value/readout kept (300), re-settles 8.8 s; selection survives collapse and expand (PASS).
- Break 5 (panel pass-through, `panelPassThrough`): with one node selected, plain click on panel background / toggle row, double-click, plain drag, Shift+drag, wheel over the panel: selection unchanged and camera [0.5,0.5,1] every time (no stage clear, no pan, no marquee, no zoom). A slider-thumb drag that exits onto the stage and releases there: slider tracks it (0.30 -> 1.00), selection and camera unchanged. A node drag released over the panel: pinned, `isMouseDown` false. A Shift marquee released over the panel: selection applied (2), no marquee pixel, `isMouseDown` false. A click at a node drawn UNDER the panel does not select it (PASS).
- Break 6 (keyboard focus, `escFocus`): focus on a range input + Esc: the selection IS cleared (Esc is a document-level handler), focus stays on the slider, value unchanged. Esc mid-marquee with a slider focused: box cancelled, selection kept. ArrowRight on a focused slider: camera unchanged. Esc during a node drag: node still pins and lands 0.3 px from release (PASS; behaviour note below).
- Break 7 (Reset / Pause / Resume while dragging via keyboard on a focused button, `resetWhileDragging`): Reset (Enter) mid-drag: readouts 15.00 -> 8.00, node size 2.00 -> 1.00, drag continues, node pins and lands 1.9 px from release, `alphaTarget` 0, `isMouseDown` false. Pause mid-drag then release: stays paused, `alphaTarget` 0, node pinned. Resume mid-drag: running, `alphaTarget` 0 after release; re-settles; 0 non-finite (PASS).
- Break 8 (render() called again, as the iframe does per tool result; `rerender`): after render, `{nodes: [], edges: []}` -> panel hidden, body empty, "No graph data returned."; render again with data -> 9 rows, 3 titles, 3 buttons (not duplicated), `paused` false, data-sim running, new slider and Pause drive the NEW sim, Esc works (PASS).
- Break 9 (map, `mapAdv`): Node size min, Link thickness max, Label fade max, Unpin all, Reset on the map: 0 px point movement, readouts back to 1.00 / 1.00 / 1.0, `sim` null, no data-sim; drag + click + Esc still work (PASS).
- 163 regressions (`re163/` scenarios on the fresh graph + map): landing (drop 0.7 px at 0/0.5/2 s, rigid dev 0.9 px, viewport frozen after drop, kids keep settling), select/toggle/Esc/click, group drag (members +156 px, offset dev 1 px, stay put), unselectedDoc (rigid 0.7), userPin (0 px), marquee (61 = nodes in box, camera still, plain stage drag still pans), unpin (dblclick unpins, no zoom), rightButton (0 px), map (marquee 16 = cluster, group drag dx 100/dy 60, others 0) -> all reproduce (PASS).

**Acceptance criteria**
- [x] PASS — Both variants contain the panel markup and every label/button string — `test_viz_app.py::test_both_variants_carry_the_embedding_map_extensions[iframe|file-*]`, `test_graph.py::test_template_carries_the_controls_panel[*]` all inside the two 4268-passed runs; DOM-marker smoke above.
- [x] PASS — Forces / Pause / force-Reset gated on `controls.forces`; Display + Unpin all always — `graph.py` `if (forces) {` blocks; map CDP: sections ["Display","Actions"], buttons ["Unpin all","Reset to defaults"], no `>Centre force<` / `>Pause<` in the dump.
- [x] PASS — Forces callbacks `reheat()`; Display callbacks `refresh()` / `setSetting`, never `reheat()` — `graph.py` panel code (Display rows have no `reheat`); CDP: Node size / Label fade / Arrows leave data-sim "settled" and nodes at 0 px; Link distance flips to "running".
- [x] PASS — Headless smoke — see Happy path above (graph: live/running, `id="panel"`, `>Centre force<`, `>Unpin all<`; map: fixed, `>Node size<`, `>Unpin all<`, no `>Centre force<` / `>Pause<`).
- [x] [HUMAN] passed — human visual check 2026-10-01 (Link distance / Node size / Arrows / Edge labels / Label fade). Independently reproduced over CDP (display.log).
- [x] [HUMAN] passed — human visual check 2026-10-01 (Pause / Resume / Unpin all / Reset). Reproduced (actions.log, pauseOrdering).
- [x] [HUMAN] passed — human visual check 2026-10-01 (map: Display + Unpin all). Reproduced (mapPanel.log, mapAdv).
- [x] [HUMAN] passed — human visual check 2026-10-01 (collapse, 760 px iframe). Reproduced (collapse.log).
- [x] PASS — (orchestrator) Fast clicking: fastClicks.log (all 8 sub-cases) + the `test_clicks_and_double_clicks_share_one_click_path`, `test_a_double_click_acts_as_a_click_unless_it_unpins`, `test_a_double_click_never_zooms`, `test_a_drags_trailing_click_never_completes_a_double_click[node|stage]` pins.
- [x] PASS — (orchestrator) Esc cancels a marquee: escMarquee.log + escFocus (also with a focused slider).
- [x] PASS — (orchestrator) Lost-mouseup guard: lostMouseup.log (node + marquee; `isMouseDown` false; next drag 0 px).
- [x] PASS — README paragraph present; `grep -n "Unpin all" apps/memory/README.md` -> 1 hit (l.356).
- [ ] OPEN (not a defect) — Last AC: format / lint / pre-commit / `make memory-tests` x2 are green and the SWE recorded the feature e2e (read-only-wrapper workaround for the shared-Mongo index conflict, documented). The only unmet part is "`.tree/graphs/*.html` cleaned up", deliberately deferred by the SWE for the [HUMAN] pass. The human has now passed it; the orchestrator should delete the generated files (gitignored: `apps/memory/.tree/graphs/*.html`, 22 files from 162-164) and tick this box. I did NOT delete them (they include 162/163-era files I did not create). My scratch files are only under the session scratchpad.

**Evidence**
```
$ make memory-tests   (x2)   -> 4268 passed in 56.99s / 4268 passed in 57.65s
$ make memory-format-check && make memory-lint-check && make pre-commit -> 317 files already formatted / All checks passed! / prettier, ruff check, ruff format, biome: Passed
$ chrome --headless=new ... --dump-dom graph.html -> <body data-layout="live" data-sim="running"> | 171 nodes · 236 edges | id="panel" | >Centre force< >Pause< >Unpin all<
$ chrome ... --dump-dom map.html -> <body data-layout="fixed"> | 86 chunks in 3 clusters (+6 noise) | >Node size< >Unpin all< | >Centre force< 0, >Pause< 0
$ node cdp2.mjs graph.instr.html {sliderExtremes,pauseOrdering,rapidPause,collapseWhileRunning,panelPassThrough,escFocus,resetWhileDragging} ; graph.re.html rerender ; map.instr.html mapAdv -> exceptions: [] each
```

**Other issues found** (none blocking; PASS with notes)
- Esc with keyboard focus on a slider/button still clears the selection (document-level handler, focus remains). Consistent with "Esc = clear"; mention only so nobody expects Esc to blur the control first.
- A node dropped under the Controls card (x 12-202, y 57-499 on a 1000-px stage) cannot be grabbed again until the card is collapsed or the camera is moved (same as the legend; the card is opaque to the stage). Collapsing is the escape hatch; follow-up if it annoys.
- Pre-existing (162/163, not 164): a second `render()` does not stop the first render's d3 simulation or remove its `document` keydown listener; both only matter for the iframe's multi-result case and did not misbehave here (new panel/sim worked, no exceptions).
- `.tree/graphs/` cleanup still pending (see the last AC).

**VERDICT: PASS**
