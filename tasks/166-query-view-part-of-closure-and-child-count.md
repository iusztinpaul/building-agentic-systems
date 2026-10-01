---
id: 166-query-view-part-of-closure-and-child-count
status: pending
feature: dynamic-graph-viz
---

# Query view: every pulled-in chunk keeps its `part_of` link and reports hidden children

Tags: `viz`, `memory`, `mcp`
Depends on: None (numbered after 165; run after 165 so the ADR/glossary edits stack cleanly)
Blocks: —
Implements: ADR-011 (§8, added by this grooming round)

## Scope

In a query view (`query_memory` → `expand_graph`, `retrieval.py` l.44–148) the `$graphLookup` walk
stops at `max_hops`: an edge found on the last hop gets its far endpoint hydrated, but that
endpoint's OWN edges are never walked. A **Parent chunk** reached through a `next` edge therefore
floats without its `part_of` edge to the document, and a **Child chunk** reached through `next`
floats without its parent. (Observed by the human: `…how-does-memory-for-ai-agents-work#parent-1`
drawn with only `next` edges in the `langgraph agent memory` query view, while it has 23 children
in Mongo.) This task closes the `part_of` chain for every chunk in the result and tells the operator
how many children a parent has when none of them are drawn. Nothing about seed search, ranking,
`rag` mode or the `QueryResult` contract changes.

Grooming-round doc edits this task OWNS (apply them in this task's commit): ADR-011 Decision 8,
diagram node `EX`, Consequences bullet "Two more reads per query view". Exact text: see `## Log` →
`[PA] Grooming`. (The glossary `childCount` clause is added by task 165.)

### Retrieval (`retrieval.py` — ONE helper, called by `expand_graph` on both its return paths)
`_attach_part_of_closure(collection, user_id, nodes, edges) -> tuple[nodes, edges]`, two batched
finds (never per chunk), every filter `user_id` first:

1. From the result's chunk rows compute `chunk_ids` (every `type == "chunk"` row) and the one-level
   lookahead `lookahead_parent_ids = {row["parent_id"] for child rows} - node_ids` (a child's parent
   that is not in the result yet — its `part_of` edge to the document must come in the same query).
   `parent_ids = {parent chunk rows in the result} ∪ lookahead_parent_ids`.
   ONE edge read:
   `find({"user_id": user_id, "kind": "edge", "type": "part_of", "$or": [{"source_node_id": {"$in": chunk_ids ∪ lookahead_parent_ids}}, {"target_node_id": {"$in": parent_ids}}]})`
   — the first branch is every chunk's (and lookahead parent's) edge UP (child→parent, parent→document);
   the second is every parent's edges DOWN from its children, used only for counting.
2. ONE node read hydrating the missing UP targets (lookahead parents and documents):
   `find({"user_id": user_id, "kind": "node", "_id": {"$in": missing_targets}})` (keep today's
   un-projected shape for parity with the hydration above it).
3. Append (never reorder existing rows — `search_memory` truncates `nodes + edges` to `max_results`,
   so ranking-first order must survive): the hydrated nodes, then every UP edge not already present
   whose source and target are now both in the node set. The DOWN edges from children absent from
   the result are NOT appended and their children are NOT hydrated — they only feed the count.
4. `child_count`: for every parent chunk row in the FINAL node set, count its incoming `part_of`
   edges from step 1; stamp `row["child_count"] = n` as a top-level key (precedent: `_search_score`)
   ONLY when none of its children is in the result (`n > 0` and no child row with `parent_id == parent`).
   A parent whose children are all drawn, or a parent with zero children, gets no stamp.
Return early (no reads) when the result has no chunk rows. Update the module docstring's
"the `part_of` / `next` edges around a parent are what the caller gets to read" to say the chain
to the document is always complete. Log at DEBUG how many edges/nodes the closure added.

### Payload (`visualize/graph.py::to_graph_payload`) and tooltip (`_RENDER_JS`)
- A node row with `child_count` → node key `"childCount": <int>`; absent otherwise (payloads
  without the stamp stay byte-identical).
- Tooltip (`enterNode`): reuse `metaRows` by adding `"child chunks": n.childCount + " (not shown)"`
  to `card` when `n.childCount != null` — one place, rendered as
  `child chunks  N (not shown)`.
- No new control, no layout change; the Full graph (165) never stamps `child_count` (its subgraphs
  are complete), so its tooltips are unchanged.

### Tests (`/squid-testing-python`)
- **Fake extension** (`tests/unit/memory/conftest.py::matches`): `$or` (any sub-query matches).
- `tests/unit/memory/graph/test_retrieval.py` (`expand_graph` with a collection of: `doc1`; parents
  `p1`,`p2`,`p3` (`parent_id: "doc1"`); children `c1a`,`c1b` of `p1`, `c3a` of `p3`; edges
  `p1→doc1`, `p2→doc1`, `p3→doc1`, `c1a→p1`, `c1b→p1`, `c3a→p3` (`part_of`), `p1→p2` (`next`),
  `c1b→c3a` (`next`); seed `p1`, `max_hops=1`):
  - today's hop yields `p2` (via `next`) and `c1a`,`c1b`,`doc1`; after the closure the result ALSO has
    `p2→doc1`; `c3a` (reached via `c1b→c3a`) gets `c3a→p3`, `p3` hydrated, and `p3→doc1` — in the
    SAME two reads (lookahead), not a third.
  - `child_count`: `p2` has no children → no stamp; `p3` (`c3a` present) → no stamp; add `c3b` of
    `p3` absent from the result → still no stamp (a child is shown); make `p2` have children `c2a`,
    `c2b` that are absent → `p2["child_count"] == 2`.
  - order: existing rows keep their order; appended rows come after; no duplicate edge `_id`.
  - query shape: exactly 2 extra `find_filters`, `user_id` first, `$or` with the two `$in` lists as
    specified; a result with no chunk rows issues no extra read; the `max_hops == 0` path also runs the closure.
  - unchanged: `_seed_ids`, the `hybrid_search` call and the `$graphLookup` pipeline tests all pass untouched.
- `tests/unit/memory/visualize/test_graph.py`: `childCount` emitted iff the row has `child_count`;
  template tokens (both variants, `test_viz_app.py` list): `childCount`, `child chunks`, `(not shown)`.
- `tests/unit/mcp/test_graph_tools.py`: `search_memory(max_results=3)` on a mocked result with
  appended closure rows still returns the first three rows (ranking survives truncation).

### Verification (Tester; never prod)
- `make memory-query-graph QUERY="langgraph agent memory"` (or 162's read-only wrapper): 162's
  headless smoke still prints `data-layout="live" data-sim="running"` and counts.
- Payload check over the generated file's `const DATA = …` JSON (scratchpad script): every
  `chunk` node has at least one outgoing `part_of` edge in `edges` whose target is also a node
  (`0 floating chunks`); every `document` node reached this way is present; count of nodes with
  `childCount` printed (≥ 0); `#parent-1` of how-does-memory-for-ai-agents-work now has its
  `part_of` edge to the document and `childCount == 23`. Same check on a `--max-hops 2` render.
- Compare node/edge counts before vs after on the same query (expected: a few more edges/nodes,
  never fewer).

## Out of scope
- Pulling in the absent children themselves (that is what `max_hops` is for), `next`-chain
  completion, the Full graph (165 builds complete subgraphs), `rag` mode, seed ranking,
  `execute_nl_query` results (arbitrary pipelines), `deep_search_memory`'s file index format.

## Acceptance Criteria

> `[HUMAN]` criteria are checked by the human in a real browser (Claude in Chrome cannot drive the WebGL stage). The SWE/Tester supply CDP evidence and do not block on them.

- [ ] Every chunk row in an `expand_graph` result has its `part_of` edge to its parent and that parent
      row present; a pulled-in child's parent AND that parent's document arrive in the same closure,
      using exactly two extra batched `find`s, `user_id` first, none when the result has no chunks (unit tests).
- [ ] `child_count` is stamped on a parent chunk only when it has children and none of them is in the
      result; equals the number of its `part_of` in-edges (unit tests).
- [ ] Existing rows keep their order; closure rows are appended; no duplicate edges; `_seed_ids`,
      `hybrid_search` usage and the `$graphLookup` pipeline are unchanged (existing tests untouched and green).
- [ ] `to_graph_payload` emits `childCount` only for stamped rows; both template variants render
      `child chunks` / `(not shown)` in the hover card (unit tests).
- [ ] Headless smoke (Tester, Log): a query render boots as in 162, and the embedded payload has 0 chunk
      nodes without a `part_of` out-edge to a present node; `#parent-1` carries `childCount: 23`.
- [ ] [HUMAN] On `QUERY="langgraph agent memory"`: no parent chunk floats detached from a document star;
      hovering a parent whose children are not drawn shows `child chunks  N (not shown)`; hovering one
      whose children are drawn shows no such row.
- [ ] ADR-011 Decision 8 present.
- [ ] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green;
      test count ≥ today's; `.tree/graphs/*.html` cleaned up.

## User Stories

### Story: Operator reads a query graph without orphans
1. `make memory-query-graph QUERY="langgraph agent memory"`.
2. Every chunk sits in a star: a parent reached via `next` hangs off its document, a child reached via
   `next` hangs off its parent, which hangs off its document.
3. The header count is a few nodes/edges higher than yesterday's render of the same query; nothing is missing.

### Story: Operator learns a parent has more to show
1. Operator hovers a parent chunk drawn without children.
2. The hover card ends with `child chunks  23 (not shown)`.
3. Operator re-runs with `--max-hops 2`: the children now appear and the row is gone from the card.

### Story: Assistant's search answer keeps its ranking
1. The model calls `search_memory("voyage rate limit", max_results=5, visualize=true)`.
2. The serialized text carries the same first five rows as before this task (seeds and their hop first);
   the graph beneath shows the chunks with their documents attached.

### Story: Deep search gets the documents too
1. `deep_search_memory("prefect deployments")` writes its per-node files.
2. The index now lists the document rows the pulled-in parents belong to; no other change in the YAML shape.

---

Blocked by: (none)

## Log

### [PA] 2026-10-01 — Grooming

Human decisions: approved both follow-ups (keep the document link for pulled-in chunks; show the
hidden child count in the tooltip).

**Doc edits owned by this task (apply verbatim):**

`docs/adrs/011_live_force_layout_and_direct_manipulation.md`
- Context references: add task 166 (if 165 has not already).
- Decision: append §8: "8. **Query views are `part_of`-complete.** After expansion, two batched reads
  attach every chunk's `part_of` edge and target (child → parent → document, one-level lookahead so both
  levels come in one pass) and stamp `child_count` on parents whose children were not pulled in; the
  payload carries `childCount` and the hover card reads "N child chunks (not shown)". Rows are appended,
  never reordered, so `search_memory`'s `max_results` truncation keeps ranking first. Nothing about seeds,
  `rag` mode or `QueryResult`'s shape changes." (Ensure the intro reads "Eight related choices".)
- Diagram: in the `py` subgraph add `EX["expand_graph + part_of closure<br/>2 reads · child_count"] --> GP` (class `pyNode`).
- Consequences — append: "- **Two more reads per query view, a few more rows.** Chunks never float;
  `search_memory` answers can be slightly larger before truncation."

Ready for implementation.
