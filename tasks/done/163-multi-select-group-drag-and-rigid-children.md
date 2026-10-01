---
id: 163-multi-select-group-drag-and-rigid-children
status: done
feature: dynamic-graph-viz
---

# Multi-select, group drag and rigid `part_of` children

Tags: `viz`, `memory`, `mcp`
Depends on: 162
Blocks: —
Implements: ADR-011

## Scope

Task 162 drags ONE node. This task introduces a **selection** and a **drag set** in the shared
renderer (`apps/memory/src/tree/memory/visualize/graph.py::_RENDER_JS`), on BOTH layouts:

### Selection (replaces `state.selected`, a single id, l.504/536–537 on main)
- `state.selection` is a `Set` of node ids. Plain `clickNode` → selection becomes `{node}` (today's
  "click highlights" behaviour, now expressed in the new model). Shift+`clickNode`
  (`e.event.original.shiftKey`) toggles the node in/out. Plain `clickStage` and `Escape`
  (`document.addEventListener("keydown")`) clear it. A drag's trailing click (162's `wasDragged`)
  never changes the selection.
- Marquee: `downStage` with Shift held starts a box at `e.event` viewport coords; captor
  `mousemovebody` updates its far corner and calls `preventSigmaDefault()` + `original.preventDefault()`
  so sigma does not pan (the proven pattern from 162's drag); captor `mouseup` ends it and sets the
  selection to every node whose `graphToViewport` position lies inside the box (plus the previous
  selection when Shift is still held — pick one and document it; default: REPLACE). Guard so the
  marquee's release is not treated as a plain stage click that would clear what it just selected
  (verify sigma 3.0.3's `draggedEventsTolerance: 3` already suppresses `clickStage` after a
  moving drag; otherwise swallow the next `clickStage`). Plain drag on empty stage still pans.
- Visuals in `drawOverlay()` (162's overlay): a 2 px `#ea580c` (`--accent1`) ring at
  `renderer.scaleSize(size) + 3` px around every selected node; the marquee as a `rgba(234,88,12,0.08)`
  fill with a 1 px `#ea580c` dashed border while active. Selected nodes also get `highlighted: true`
  + `zIndex: 2` in `nodeReducer` (today's single-select look, so their labels show).

### Drag set (generalises 162's drag)
- On `downNode`: drag set = `selection` if the pressed node is selected, else `{node}` (the
  selection is NOT changed by dragging an unselected node). Then EXPAND: for every node in the set,
  add its direct **unpinned** `part_of` children — the sources of its in-edges whose `relType === "part_of"`
  (child chunk → parent chunk, parent chunk → document; one level, no recursion) — unless already in
  the set. Nodes the user pinned (`fx != null` before this drag) are never added.
- During the drag every member moves by the SAME delta as the pointer (relative offsets preserved):
  set `fx/fy` and graphology `x/y` for all members (held with `fx/fy` so the hot sim cannot pull
  them apart mid-drag). The viewport is frozen (`setCustomBBox`) as in 162.
- On drop: the ORIGINAL set (the pressed node / selection) keeps `fx/fy` — they are **Pinned
  node**s with markers; the EXPANDED children get `fx = fy = null` so they keep settling under the
  forces; `alphaTarget(0)`; `setCustomBBox(null)`. On the map (no sim) the children simply stay
  where the translation left them — superseded by the orchestrator AC (camera stays frozen after
  release).
- Edge case: a child that is itself selected is part of the original set (pinned on drop), not a
  rigid passenger; a pinned child is left alone (not moved).

### Tests (`/squid-testing-python`)
- Template-contract tests in `tests/unit/memory/visualize/test_graph.py` pin: `state.selection`,
  `shiftKey`, `"Escape"`, `"downStage"`, `relType === "part_of"` (or the equivalent literal the SWE
  uses, one place), `inEdges`/`forEachInEdge`, `function drawOverlay` still present, the ring colour
  `#ea580c`, and that the old `state.selected` identifier is gone.
- `tests/unit/mcp/test_viz_app.py` parametrised token list gains `state.selection` and `"downStage"`
  so both variants carry the behaviour.
- A pure-JS unit test is NOT required (no JS test runner in the repo); keep the drag-set expansion
  in ONE small function (`dragSetFor(nodeId)`) so a reviewer can read it in isolation.

## Out of scope
- The control panel / Unpin all (164). Keyboard arrows, lasso, rubber-band on the map's hulls,
  touch.

## Acceptance Criteria

> `[HUMAN]` criteria are executed by the orchestrator through Claude in Chrome (real browser, gestures + GIF), with results pasted into `## Log`; the human does a final glance. The SWE/Tester do not block on them.

- [x] Template (both variants) contains `state.selection`, `shiftKey`, `"Escape"`, `"downStage"`,
      the `part_of` literal, and no `state.selected` (unit tests).
- [x] `drawOverlay` draws selection rings and the marquee (template contains `#ea580c` ring stroke and
      the dashed marquee; unit test pins both tokens).
- [x] Headless smoke (Tester, pasted in Log): the 162 command on a fresh graph file still prints
      `data-layout="live" data-sim="running"` and filled counts — the new code must not break boot.
- [x] (added by orchestrator) After a drag or marquee ends the camera does NOT re-fit: release never
      calls `setCustomBBox(null)`, so the custom bbox frozen by the first drag / marquee stays, and a
      dropped node's screen position stays within a few px of the release point (real CDP mouse
      events). Only the zoom fit button clears it (`setCustomBBox(null)` then `animatedReset()`).
      Supersedes the Scope drop bullet's `setCustomBBox(null)` and ADR-011 §4's "for the duration of
      any drag or marquee" (unit test pins `setCustomBBox(null)` once, on the fit line).
- [ ] [HUMAN] Shift+click three entity nodes: each gets an orange ring; Shift+click one again removes
      its ring; Esc clears all; a plain click on a node selects only it; a plain click on empty stage clears.
- [ ] [HUMAN] Shift+drag on empty stage draws a dashed orange box; on release every node inside it is
      ringed and the view did not pan; a plain drag on empty stage still pans.
- [ ] [HUMAN] With three nodes selected, dragging one moves all three keeping their relative layout;
      on release all three show pin dots and the rest of the graph keeps settling.
- [ ] [HUMAN] Dragging an UNSELECTED document node (no selection change) carries its parent chunks
      rigidly (the star keeps its shape) while entities linked by `mentions` are merely pulled by the
      forces; on release the document shows a pin dot, its chunks do not and keep settling.
- [ ] [HUMAN] A chunk the user pinned earlier (dot visible) does NOT move when its document is dragged.
- [ ] [HUMAN] Same gestures on the map: marquee selects points, group drag translates them, pin dots
      appear on the dragged points, no simulation starts.

[HUMAN] pending — human visual check; CDP gesture evidence in Tester Log.

- [x] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green.

## User Stories

### Story: Operator groups three people and parks them in a corner
1. On the full graph, operator Shift+clicks `Paul`, `Alice`, `Bob` — each gets an orange ring.
2. Operator drags `Alice` to the top-left corner: all three move together, offsets unchanged.
3. On release the three show pin dots and stay; their `mentions` neighbours drift after them.
4. Operator presses Esc: rings vanish, dots remain (selection ≠ pin).

### Story: Operator box-selects a cluster of chunks
1. Operator holds Shift and drags a box over a document's star.
2. A dashed orange rectangle follows the pointer; the canvas does not pan.
3. On release every node inside the box is ringed; the header/legend are unchanged.
4. Operator drags one ringed chunk 200 px down: the whole star moves as a unit and pins.

### Story: Operator drags a document and the star keeps its shape
1. Nothing selected. Operator drags a document node across the canvas.
2. Its parent chunks (direct `part_of` children) move rigidly with it — the star does not stretch;
   the child chunks (two levels down) are only pulled by forces.
3. On release the document is pinned (dot); the parent chunks keep settling (no dots).

### Story: Operator's earlier pin is respected
1. Operator drags one parent chunk away from its document and releases (it pins).
2. Operator then drags the document: every other parent chunk follows rigidly; the pinned one stays.

### Story: Operator rearranges the map
1. On `make memory-visualize-embeddings HULLS=true`, operator Shift+drags a box around a noise cluster.
2. The grey points inside are ringed; dragging one moves them all; the hulls of their clusters are redrawn.

---

Blocked by: 162

## Log

### [SWE] 2026-10-01 17:58 — Implementation

**Files modified**
- `apps/memory/src/tree/memory/visualize/graph.py`, `_RENDER_JS`:
  - `state.selected` → `state.selection` (a `Set`). `clickNode` selects one node; Shift toggles it. `clickStage` clears, but not with Shift. `keydown` `"Escape"` clears.
  - The marquee: `startMarquee` runs on `"downStage"` and `"downEdge"`; `selectInside(box)` REPLACES the selection.
  - `dragSetFor(nodeId)` returns `{held, carried}`, the ONE place with `relType === "part_of"`. The group drag moves by the pointer delta from the press anchor; carried children are released on drop.
  - `DRAG_THRESHOLD = 3` px: pinning, reheat and the viewport freeze happen only once the pointer moves that far (`startDrag()`).
  - `pointerMoved` guards the trailing click on both `clickNode` and `clickStage`.
  - The viewport stays frozen after the first gesture. `setCustomBBox(null)` lives only on the fit button.
  - `drawOverlay` draws hulls, then pin dots (skipping carried passengers mid-drag), then 2 px `#ea580c` rings at `scaleSize(size) + 3`, then the dashed marquee.
  - Selected nodes get `zIndex: 2` and `highlighted`, EXCEPT a chunk in a multi-selection (see Notes).
  - Only a LEFT-button press starts a drag or a marquee (regression, see Notes).
  - Module docstring updated.
- `apps/memory/src/tree/memory/visualize/graph.py`, `_NODE_SIZES`: **human-requested resize**, `document 12 → 10`, `chunk:parent 8 → 7` (child chunk 4 and the default 6 unchanged).
- `docs/glossary.md` (**Graph payload** row) and `docs/adrs/011_…md` (Decision 6 sizes line): `document 10 > parent chunk 7 > entities 6 > child chunk 4`. This is the human-requested resize, relayed by the orchestrator; no other ADR/glossary text was touched.
- `apps/memory/README.md`: the map sentence now mentions select and group drag.
- `apps/memory/tests/unit/memory/visualize/test_graph.py`:
  - role sizes re-pinned to 10/7;
  - the 162 test `test_a_drag_without_movement_does_not_pin` was replaced by `test_a_press_without_movement_neither_pins_reheats_nor_freezes`;
  - new "Selection, marquee and group drag" section, with a `_js_block()` slicer so assertions target ONE handler body.
- `apps/memory/tests/unit/mcp/test_viz_app.py`: both-variant token list gains `state.selection`, `shiftKey`, `"Escape"`, `"downStage"`, `relType === "part_of"`, `function dragSetFor(nodeId)`, `#ea580c`, `setLineDash([4, 3])`; new `test_both_variants_drop_the_single_selected_id`.

**Tests**
- Unit: 4175 passed, 0 failed (162 baseline 4132) — `make memory-tests`. The new tests were red first (28 failures with AssertionError / ValueError, none from import errors), then green. The right-button regression test was red (2 failures) before its fix.
- Integration: N/A (no integration suite in this repo).

**Acceptance criteria**
- [x] Template tokens in both variants, no `state.selected`:
  - `test_viz_app.py::test_both_variants_carry_the_embedding_map_extensions[*]`, `::test_both_variants_drop_the_single_selected_id`;
  - `test_graph.py::test_template_carries_the_selection_and_group_drag_machinery`, `::test_the_single_selected_id_is_gone`, `::test_rigid_children_are_the_unpinned_part_of_sources_in_one_place`.
- [x] Rings and marquee drawn by `drawOverlay`: `test_graph.py::test_the_overlay_draws_hulls_pins_rings_then_the_marquee`, plus the `#ea580c` / `rgba(234,88,12,0.08)` / `setLineDash([4, 3])` tokens.
- [ ] Headless smoke: the Tester owns this. The SWE pre-run below shows live/running and filled counts.
- [x] (orchestrator) Frozen viewport after drop: `test_graph.py::test_the_viewport_stays_frozen_after_a_drop_until_fit`. CDP: the drop lands 0.8 px from the release point (the 162 baseline on the same data: 31.9–37 px).
- [ ] [HUMAN] ×6: for the orchestrator. The SWE CDP pre-check is in Evidence.
- [x] format / lint / pre-commit / tests green.

**Evidence**
```
$ make env-status                                   -> Env target: local (.env)
$ make memory-format-check && make memory-lint-check && make pre-commit
  317 files already formatted / All checks passed! / prettier, ruff check, ruff format, biome: Passed
$ make memory-tests                                 -> 4175 passed in 65.28s
$ node --check on the module <script> of BOTH variants (file + ui:// iframe)   -> SYNTAX OK

# fresh files (162's read-only wrapper; see 162 Log for why not the make target)
Wrote self-contained graph HTML (171 nodes, 236 edges) to apps/memory/.tree/graphs/graph-20261001-145134.html
Wrote self-contained graph HTML (78 nodes, 75 edges) to apps/memory/.tree/graphs/langgraph-agent-memory-20261001-145138.html
Wrote self-contained embedding map HTML (86 points) to apps/memory/.tree/graphs/embedding-map-20261001-145141.html
role sizes in graph-20261001-145134: chunk/child/4: 109 · chunk/parent/7: 7 · document/10: 4

# headless --dump-dom smoke (162's command)
graph-20261001-145134          <body data-layout="live" data-sim="running">  171 nodes · 236 edges  legend filled
langgraph-agent-memory-…145138 <body data-layout="live" data-sim="running">  78 nodes · 75 edges    legend filled
embedding-map-20261001-145141  <body data-layout="fixed">                    86 chunks in 3 clusters (+6 noise)

# gesture pre-check (NOT the [HUMAN] check). Real CDP Input.dispatchMouseEvent / dispatchKeyEvent
# (Shift = modifiers 8, clickCount 1, buttons 1 on moves), headless Chrome 1400x900, swiftshader,
# on scratchpad COPIES that only add `window.__t = {renderer, graph, simById, state, sim}`.
# Exceptions (Runtime.exceptionThrown) were empty in every run.
landing (doc "how-does-memory-for-ai-agents-work", fresh load + 1.5 s, sim still running; pressed at the node centre, released +150/+90):
  customBBox null before any gesture=true; drop→release-point distance t0=0.8 px, t500ms=0.8, t2000ms=0.8
  customBBox frozen after drop=true; after a click on the fit button customBBox===null
  SAME gesture on the committed 162 template, same data: 31.9 / 34 / 37 px, bbox released
  mid-drag: doc moved 175 px, its 3 parent chunks rigid (offset deviation 0.9 px), passengers' pin-dot alpha 0
  mention neighbours moved 62/129/126 px mid-drag (pulled, not carried); after drop doc pinned (dot 31,36,48,255),
  kids pinned [false,false,false] and kept settling 5.7/3.4/3.0 px over 1.5 s
select (3 isolated entities):
  Shift+click ×3 → selection 3, ring pixel (234,87,11,228) on all 3; Shift+click #2 again → 2, ring gone
  Esc → 0, no rings; plain click e1 → [e1], not pinned; Shift+click empty stage → still 1; plain click empty → 0
  plain click on a SETTLED layout: data-sim stays "settled", alpha 0.001 → 0.001, node moved 0 px,
  not pinned, customBBox still null (no reheat, no pin, no freeze)
group (3 selected, drag #2 by -120/-100):
  members moved 157/155.9/156.3 px, offset deviation 1.3 px; all 3 pinned; selection unchanged;
  members stay put 0/0/0 px over 1.5 s while other nodes move up to 18.8 px (still settling, data-sim running);
  Esc → selection 0, still pinned [true,true,true]
unselectedDoc (e1 selected, drag the unselected doc): selection [e1] before and after; star rigid (0.7 px);
  doc pinned, kids not
userPin (drag parent chunk k0 away → pinned; then drag its doc +160/-40):
  k0 moved 0 px mid-drag and 0 px after, still pinned (dot visible); other kids rigid (0.6 px); doc moved 165 px
marquee (after settle; Shift+drag 180×144 px box around the doc star):
  camera [0.5,0.5,1] before = mid = after (no pan); fill pixel (230,89,13,20); dashed top border 76 on / 100 off px
  selection 61 = exactly the nodes whose viewport position is in the box; 61 rings; marquee gone after release
  plain drag on empty stage → camera [0.5,0.5,1] → [0.28754,0.60623,1] (pans), selection kept (61)
unpin (162 regression): drag → pinned; double-click → unpinned, camera unchanged
rightButton (regression; settled layout): right press+release on an entity, then 116 px of buttonless moves →
  node moved 0 px, not pinned; Shift + right press on the stage, then moves → no marquee pixel, customBBox null.
  BEFORE the fix (same gesture): the node followed the pointer 113 px and was pinned; a marquee was drawn
map (fixed; sim null, no data-sim): Shift+drag box around Topic 2 → 16 selected (all 15 Topic-2 points + 1 noise
  point inside the box), 16 rings, camera unchanged; drag one → every selected point moved exactly (100, 60),
  others 0 px, all pinned (dot 35,37,47,219), grabbed point 0 px from release, sim still null,
  hull redrawn around the moved points (screenshot); Esc → 0, still pinned
```

**Notes**
- **Trailing click (the spec's verification question).** Sigma 3.0.3's `draggedEventsTolerance` does NOT suppress the click after our gestures. In `MouseCaptor.handleMove`, `mousemovebody` is emitted first and the handler then returns on `sigmaDefaultPrevented` BEFORE `draggedEvents++`. So every drag or marquee whose default we prevent is followed by a sigma `click` on whatever sits under the release, node or stage. `pointerMoved` guards both handlers and is reset on every press (`downNode` / `downStage` / `downEdge`).
- **Frozen viewport (orchestrator requirement).** This supersedes the Scope drop bullet and ADR-011 §4's "for the duration of any drag or marquee". **PA should amend ADR-011 §4.** I did not edit that text. The freeze happens only when a gesture crosses the 3 px threshold, so a plain click never freezes the viewport. Consequence: after the first gesture, the auto-fit "breathing" of a still-settling layout stops, and nodes that settle outside the frozen extent need ⊡ (fit).
- **Reheat only on movement** (orchestrator, from 162 Tester note). `downNode` only records the press. Pinning, `alphaTarget(0.3)` and the freeze all wait for 3 px of travel. This also removed 162's `wasPinned` restore logic.
- **Deliberate narrowing: a chunk in a MULTI-selection gets no `highlighted` label box.** The spec says every selected node gets `highlighted: true`. A CDP screenshot of a 61-node marquee over a document star showed 61 overlapping white label boxes burying the star. Now every selected non-chunk node, and a lone selected chunk, keep the spec's look (label box, label shown); chunks in a group get only their ring plus `zIndex: 2`. Story 1's three people are unaffected. Pinned by `test_selected_nodes_are_highlighted_except_chunks_in_a_group`. The [HUMAN] run should confirm; reverting is one line.
- **Regression found and fixed: a right-click left the press open.** Sigma emits `downNode`/`downStage` for EVERY button, but its captor `mouseup` fires only after a LEFT press (`handleUp` returns when `!isMouseDown`, and only `button === 0` sets it). After a right-click, the next buttonless moves dragged the node, which then pinned on the next left release. 162's drag had the same hole. Both press handlers now start with `if (e.event.original.button !== 0) return;`; pinned by `test_only_a_left_button_press_starts_a_drag_or_a_marquee[*]` and verified over CDP (rightButton, above).
- **Choices the spec left open:**
  - Marquee = REPLACE.
  - Shift+click on empty stage keeps the selection, so a near-miss does not wipe a selection being built.
  - A Shift+press on an edge line also starts a marquee (`downEdge`), because with `enableEdgeEvents: true` sigma reports it as an edge press, not a stage press.
  - The drag delta is anchored at the press point rather than snapping the node centre to the cursor, so nothing jumps on the first move.
- **Human-requested resize:** document 12 → 10, parent chunk 8 → 7, in `_NODE_SIZES`, the role-size test, the glossary Graph payload row and the ADR-011 Decision 6 line.
- Known, pre-existing pattern: `render()` adds `window` / `document` listeners (resize since 162, now keydown) each time it is called. The iframe calls it once per tool result.
- The make targets that init Beanie still fail in this worktree (shared local Mongo has another branch's `user_source_uri_unique` index, see the 162 Log). The files above come from 162's read-only wrapper (`run_readonly.py`) and the synthetic map (`synthetic_map.py`). I did NOT touch any Mongo index.
- **[HUMAN] tips:**
  - Drag the LARGE dark-green document (size 10), not its smaller `object` twin.
  - Leave >300 ms between a drop and the next click on the same node, or sigma reads a double-click and unpins it.
- **Gesture harness for the Tester.** Directory: `/private/tmp/claude-501/-Users-pauliusztin-Documents-01-Projects-AI-Engineer-Handbook-building-agentic-systems-building-agentic-systems/944c4516-3945-43f4-8111-e0c95524323d/scratchpad/163/`.
  1. Instrument a copy: `python3 instrument.py <rendered.html> <copy.instr.html>`. It inserts ONE line after `      document.body.dataset.layout = isFixed ? "fixed" : "live";`:
     `      window.__t = { renderer, graph, simById, state, get sim() { return sim; } };`
  2. Start Chrome: `"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --no-first-run --use-angle=swiftshader --enable-unsafe-swiftshader --remote-debugging-port=9333 --window-size=1400,900 --user-data-dir=<tmp> about:blank &`
  3. Run a scenario: `node cdp.mjs file://<copy.instr.html> <landing|select|group|unselectedDoc|userPin|marquee|unpin|rightButton>` (graph) or `… map` (map). Node ≥ 22 (built-in WebSocket).
  4. The scenario sources are `scenarios.mjs` in the same directory. Fresh files come from `/private/tmp/claude-501/-Users-pauliusztin-Documents-01-Projects-AI-Engineer-Handbook-building-agentic-systems-building-agentic-systems/944c4516-3945-43f4-8111-e0c95524323d/scratchpad/run_readonly.py` and `/private/tmp/claude-501/-Users-pauliusztin-Documents-01-Projects-AI-Engineer-Handbook-building-agentic-systems-building-agentic-systems/944c4516-3945-43f4-8111-e0c95524323d/scratchpad/synthetic_map.py` (162 Log recipe).


### [Tester] 2026-10-01 18:40 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check && make memory-lint-check && make pre-commit`: 317 files formatted, ruff/prettier/biome Passed). Env target: local.
- Unit tests: `make memory-tests` run twice on the final tree, 4175 passed / 0 failed each (63.15 s, 58.21 s). Integration: N/A (no suite in repo).
- Warnings: 0.
- Note: a FIRST `make memory-tests` run hung at 13% (`tests/unit/data/web/test_web_scraper_api.py`, pytest ~5-6 GB RSS) while two headless Chrome / CDP sessions ran concurrently; I killed it (resource contention, unrelated to this diff) and re-ran with nothing else running, twice, clean.
- `code-review` plugin is enabled in `.claude/settings.json`; a subagent cannot invoke plugin skills from this toolset, so I did a manual read of the full `graph.py` diff instead (`dragSetFor`, `startDrag`, `mousemovebody`, `mouseup`, `selectInside`, `drawOverlay`): no defects found beyond the items below. The orchestrator may still run the plugin.

**Headless smoke (AC)**
```
$ (apps/memory) uv run python <run_readonly.py> scripts/query_graph.py --no-open     # 162's read-only wrapper; no Mongo index touched
Wrote self-contained graph HTML (171 nodes, 236 edges) to .../apps/memory/.tree/graphs/graph-20261001-145534.html
$ chrome --headless=new --no-first-run --use-angle=swiftshader --enable-unsafe-swiftshader --virtual-time-budget=5000 --dump-dom file://.../graph-20261001-145534.html | grep -o '<body[^>]*>\|<span id="counts">[^<]*\|id="legend"><div class="title">Legend'
<body data-layout="live" data-sim="running">
<span id="counts">171 nodes · 236 edges
id="legend"><div class="title">Legend
```

**E2E adversarial pass** (real CDP `Input.dispatchMouseEvent`/`dispatchKeyEvent`, headless Chrome 1400x900, on COPIES of fresh files with one `window.__t = {...}` line added by the SWE's `instrument.py`; my scenarios: `<scratchpad>/qa/adv.mjs`; zero `Runtime.exceptionThrown` in every run)
- Happy path, independently re-run (SWE scenarios `landing select group unselectedDoc userPin marquee unpin rightButton` on graph, `map` on the map): all reproduce the SWE numbers. Drop-to-release 0.9 px (t0/500/2000 ms), bbox null before first gesture, frozen after drop, null after fit click; Shift+click x3 rings (234,88,12), toggle off, Esc clear, plain click selects only it, stage click clears, plain click on settled layout: no reheat / pin / freeze; group drag members 157/156/156 px, offset dev 1.2 px, all pinned, selection unchanged; unselected-doc drag: star rigid 0.7 px, selection untouched, doc pinned, kids not; user-pinned kid moved 0 px; marquee 61 = exact in-box set, no camera change, plain stage drag still pans; map: 16 selected, every point moved exactly (100,60), others 0, sim null. (PASS)
- Break 1, nested drag set, no double-move (doc has 3 parent chunks with 21/23/10 child chunks). Deviation of each node's displacement from the pointer delta (140,80), mid-drag:
  - A: selection {doc, k0}, press doc: doc 2.9, k0 3.7, k1 3.1, k2 3.1 px; k0's 21 children 2-6 px (carried via k0); k1/k2's children 36-80 px (pulled only, no recursion). Pins after drop: doc, k0 true; k1, k2, all children false. (PASS)
  - B: selection {k0, one of its children g0.0}, press k0: k0 3.5, g0.0 3.3, other k0 children 2-5 px (carried); doc/k1/k2 NOT carried (100-155 px off = pulled only). Pins: k0, g0.0 true; rest false. Selected child is held+pinned, not a passenger. (PASS)
  - C: selection {k0}, press UNSELECTED doc: doc, k0, k1, k2 all carried (3-6 px dev), children of k0 not (no recursion); only doc pinned, k0 (selected) released; selection unchanged. Consistent with the spec's literal drag-set rule (selection only seeds the set when the pressed node is in it). (PASS, see note 3)
  - D: selection {doc, k0}, press the child k0: doc 2.6, k1/k2 2.7 (carried by doc), k0 children carried; doc and k0 pinned. No double move anywhere (max dev of a moved node 5.8 px, never ~160). (PASS)
- Break 2, Shift+click on a pinned node: dragged e1 (pinned), Shift+click it -> in selection, ring drawn, still pinned; Shift+click e2, drag e2: pinned e1 moved (-100,70) with e2 (-98.9,68.9), both pinned; Shift+click e1 again toggles it off and keeps the pin. (PASS; the one oddity is the <300 ms case below)
- Break 3, Esc mid-group-drag: Esc pressed with the pointer held: selection -> 0, rings vanish, drag continues, members moved 149.2/150.4/149.9 px (expected 150), all 3 pinned on release. (PASS) Esc mid-marquee: marquee stays drawn (pixel 230,89,13,20), release still selects 61. (PASS with note 2: Esc does not cancel an active marquee)
- Break 4, marquee release outside the canvas: Shift+drag from empty stage to (-40,-40) (off-window), release: selection 0 == expected 0, camera unchanged; to (1430,843) (off bottom-right): 166 == expected 166; release over the header bar: 2 selected, marquee cleared (not compared to an expected set); reverse-direction box (bottom-right -> top-left) around the doc: 61. No stuck marquee. (PASS)
- Break 5, right / middle buttons: with the button held, 8 moves, then release, then buttonless moves, on a node: moved 0 px, not pinned, no bbox freeze (both buttons); on stage plain and Shift: selection {e2} kept, camera unchanged, no marquee pixel, no bbox; then a normal left drag still moved the node exactly 80.6 px (expected 80.6) and pinned it. (PASS)
- Break 6, fit then drag: drag -> bbox frozen; click fit -> bbox null, camera [0.5,0.5,1]; drag e2 -> bbox frozen again, drop 0.3 px from release; group drag after fit 0 px; drag begun DURING the fit animation: drop 0.2 px. (PASS)
- Break 7, zoomed (wheel x3, ratio 0.20) Shift+marquee: 6 selected == 6 expected; drag after: drop 0 px, 6 pinned. (PASS)
- Break 8, map with hulls on (3 clusters, noise, warning banner): Shift+drag around Topic 1 -> 25 == 25, no noise inside; drag one by (-180,+120): grabbed point 0 px from release, 25 pinned, sim stays null, hull redrawn around the moved points (screenshot `qa/hulls-after-drag.png`), Esc -> selection 0, pins kept. (PASS)
- Break 9, fuzz: 40 seeded random ops (click, Shift+click, drag, Shift+drag, marquee, Esc, pan, short drag) on a live graph at >=350 ms spacing: no exception; afterwards buttonless moves over 3 nodes moved them 0.5/0.9/0.7 px (sim drift only), no marquee pixels left, selection holds only existing ids, Esc -> 0. (PASS)
- Break 10, rapid clicks (the orchestrator's "rapid click-drag-click"): see note 1. No corrupted or stuck state; the effects are Sigma's 300 ms double-click detection. Quick-nudge control: two consecutive drags of one node each 27 px, pinned both times, on a fresh load (the 162 baseline lands 24-37 px away). (PASS with note 1)
- Break 11, lost mouseup (synthetic: buttonless moves after a node press, no `mouseReleased`): the node keeps following the pointer (108 px; `pinned` true) and a Shift marquee keeps growing until the next left click. Pre-existing pattern for the drag half (162 had it; no `buttons` check on `mousemovebody`); the marquee half is new. Real browsers deliver the mouseup even off-window (Break 4 passes), so only focus loss / OS dialogs trigger it. (PASS with note 4)

**Acceptance criteria**
- [x] PASS — Template (both variants) contains `state.selection`, `shiftKey`, `"Escape"`, `"downStage"`, the `part_of` literal, no `state.selected` — `test_viz_app.py::test_both_variants_carry_the_embedding_map_extensions[iframe|file]`, `::test_both_variants_drop_the_single_selected_id[*]`, `test_graph.py::test_template_carries_the_selection_and_group_drag_machinery`, `::test_the_single_selected_id_is_gone`; all in the 4175 green.
- [x] PASS — `drawOverlay` draws rings and marquee — `#ea580c`, `rgba(234,88,12,0.08)`, `setLineDash([4, 3])` pinned by `test_the_overlay_draws_hulls_pins_rings_then_the_marquee` and the viz_app token list; CDP: ring pixel (234,88,12,255) on every selected node, marquee fill (230,89,13,20), dashed top border 76 on / 100 off.
- [x] PASS — Headless smoke (evidence above): `data-layout="live" data-sim="running"`, `171 nodes · 236 edges`, legend filled. Ticked in the spec.
- [x] PASS — (orchestrator) camera does not re-fit after a drag or marquee: `setCustomBBox(null)` appears once, on the fit button (`graph.py:743`; `test_the_viewport_stays_frozen_after_a_drop_until_fit`); CDP: bbox null before the first gesture, frozen after drag and after marquee, camera unchanged across a marquee, drop 0.9 px from the release point at t=0/0.5/2 s (162 baseline on the same data 32-37 px), refreeze after fit 0.3 px.
- [ ] [HUMAN] x6 — Awaiting human verification (not touched).
- [x] PASS — `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green (4175 passed x2).
- Doc ACs: the human-requested resize is present in `git diff --stat` (`docs/glossary.md` Graph payload row and ADR-011 Decision 6: `document 10 > parent chunk 7 > entities 6 > child chunk 4`); `_NODE_SIZES` and its test match; no other "document 12 / parent chunk 8" left outside `tasks/` (grep).

**Evidence**
```
$ make memory-tests   ->  ======================= 4175 passed in 63.15s (0:01:03) =======================
$ make memory-tests   ->  ============================ 4175 passed in 58.21s ===============================
$ make memory-format-check && make memory-lint-check && make pre-commit -> all Passed
Sigma event log, drag A then Shift+click pinned B (identical on the committed 162 template, so NOT a regression):
  gap 100 ms: new  [clickNode:A, doubleClickNode:B]  B unpinned, selection []     | 162: same events, B unpinned
  gap 450 ms: new  [clickNode:A, clickNode:B]        B pinned, selection {B}       | 162: same events
Shift+click 3 nodes, one per N ms (fresh load each):
  150 ms -> [clickNode, doubleClickNode]  selected 1, camera ratio 1 -> 0.4545 (Sigma default double-click zoom)
  250 ms -> [clickNode]                   selected 1, camera zoomed (the next target moved away)
  350 ms -> 3 clickNode                   selected 3, camera unchanged
```

**Other issues found** (none blocks; orchestrator decides)
1. Sigma treats ANY two clicks <300 ms apart as a double-click, regardless of target, and the drag's trailing `click` counts as the first. Consequences: (a) a click within ~300 ms of a drag release or of the previous click is delivered as `doubleClickNode` / `doubleClickStage`, so `clickNode` / `clickStage` never run: a Shift+click is silently dropped, a stage click does not clear; (b) on a PINNED node it unpins it (the 162 handler); (c) on an unpinned node or the stage Sigma's default double-click zoom fires (camera ratio 1 -> 0.45). Verified identical on the 162 baseline for the drag-then-click and unpin cases, so pre-existing, and the SWE tip already says "leave >300 ms". What 163 adds is the exposure: the headline Shift+click multi-select breaks for a click cadence of <=~250 ms. Suggested follow-up (small): in `doubleClickNode`/`doubleClickStage`, when the previous click was a different node or a drag's trailing click, `preventSigmaDefault()` and run the click logic; or disable Sigma's double-click zoom on the stage and keep only the unpin double-click.
2. Esc does not cancel an active marquee (box stays, release replaces the selection). Spec is silent. One-liner if wanted: `if (marquee) { marquee = null; drawOverlay(); }` in the keydown handler (and it should not clear the selection mid-gesture).
3. A selected child that is carried because its parent (not selected) is pressed is released, not pinned, and keeps its ring. Matches the spec text literally ("drag set = selection if the pressed node is selected, else {node}"); flagging only because the Edge-case bullet could be read the other way.
4. `mousemovebody` never checks `e.original.buttons`; a lost mouseup (focus loss, OS dialog) leaves a node drag or marquee glued to the pointer until the next left click. Pre-existing for the node drag; cheap guard: end the gesture when a move arrives with `buttons === 0`.
5. `tasks/165-full-graph-recent-documents-cap-and-slider.md` and `tasks/166-query-view-part-of-closure-and-child-count.md` are untracked and are not part of the SWE's diff: keep them out of the 163 commit.
6. The SWE-flagged items stand: ADR-011 §4 ("for the duration of any drag or marquee") is now stale and needs a PA amendment; chunks in a multi-selection get a ring only, no label box (deliberate deviation, [HUMAN] run should confirm).

**VERDICT: PASS**
