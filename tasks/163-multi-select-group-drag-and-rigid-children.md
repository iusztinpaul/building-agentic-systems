---
id: 163-multi-select-group-drag-and-rigid-children
status: pending
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
  where the translation left them.
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

- [ ] Template (both variants) contains `state.selection`, `shiftKey`, `"Escape"`, `"downStage"`,
      the `part_of` literal, and no `state.selected` (unit tests).
- [ ] `drawOverlay` draws selection rings and the marquee (template contains `#ea580c` ring stroke and
      the dashed marquee; unit test pins both tokens).
- [ ] Headless smoke (Tester, pasted in Log): the 162 command on a fresh graph file still prints
      `data-layout="live" data-sim="running"` and filled counts — the new code must not break boot.
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
- [ ] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green.

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
