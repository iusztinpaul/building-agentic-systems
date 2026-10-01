---
id: 164-graph-controls-panel
status: pending
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

- [ ] Both variants contain the panel markup and every label/button string listed in Tests (unit tests).
- [ ] The Forces section, Pause/Resume and the force half of Reset are emitted only when
      `payload.controls.forces` exists (template gate pinned); Display + Unpin all are emitted for every payload.
- [ ] Forces callbacks call `reheat()`; Display callbacks call `renderer.refresh()` or `setSetting` and
      never `reheat()` (pin the two code paths with tokens, as pulse separates them).
- [ ] Headless smoke (Tester, Log): graph file prints `data-layout="live" data-sim="running"`, counts, and
      the dump contains `id="panel"` with `Centre force` and `Unpin all`; map file prints `data-layout="fixed"`,
      its dump contains `Node size` and `Unpin all` but NOT `Centre force` or `Pause`.
- [ ] [HUMAN] Moving `Link distance` from 80 to 300 visibly spreads the graph and it re-settles; moving
      `Node size` to 2 doubles node radii without any movement; unchecking `Arrows` turns heads into plain
      lines; unchecking `Edge labels` hides relationship labels; `Label fade` at 12 hides most labels until zoomed in.
- [ ] [HUMAN] `Pause` freezes all motion (dragging a node moves only it, neighbours stay); `Resume` restarts
      settling; `Unpin all` removes every pin dot and the graph re-settles; `Reset to defaults` returns every
      slider readout to its load value and leaves pins untouched.
- [ ] [HUMAN] The map shows only Display + Unpin all; `Unpin all` clears dots and no point moves.
- [ ] [HUMAN] Collapsing the panel leaves only the `Controls` button; the legend and zoom buttons are unobstructed in a 760 px-high iframe.
- [ ] README paragraph present; `grep -n "Unpin all" apps/memory/README.md` → 1 hit.
- [ ] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green;
      feature e2e per `.agents/skills/run-pipelines-e2e/SKILL.md` steps for `memory-query-graph` /
      `memory-visualize-embeddings` recorded in the Log; `.tree/graphs/*.html` cleaned up.

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
