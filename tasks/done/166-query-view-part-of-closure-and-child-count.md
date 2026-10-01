---
id: 166-query-view-part-of-closure-and-child-count
status: done
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

- [x] Every chunk row in an `expand_graph` result has its `part_of` edge to its parent and that parent
      row present; a pulled-in child's parent AND that parent's document arrive in the same closure,
      using exactly two extra batched `find`s, `user_id` first, none when the result has no chunks (unit tests).
- [x] `child_count` is stamped on a parent chunk only when it has children and none of them is in the
      result; equals the number of its `part_of` in-edges (unit tests).
- [x] Existing rows keep their order; closure rows are appended; no duplicate edges; `_seed_ids`,
      `hybrid_search` usage and the `$graphLookup` pipeline are unchanged (existing tests untouched and green).
- [x] `to_graph_payload` emits `childCount` only for stamped rows; both template variants render
      `child chunks` / `(not shown)` in the hover card (unit tests).
- [x] Headless smoke (Tester, Log): a query render boots as in 162, and the embedded payload has 0 chunk
      nodes without a `part_of` out-edge to a present node; `#parent-1` carries `childCount: 23`.
- [ ] [HUMAN] On `QUERY="langgraph agent memory"`: no parent chunk floats detached from a document star;
      hovering a parent whose children are not drawn shows `child chunks  N (not shown)`; hovering one
      whose children are drawn shows no such row. [HUMAN] pending — human visual check; CDP evidence in Tester Logs
- [x] ADR-011 Decision 8 present.
- [x] Pause → reveal → Resume: a reveal while paused fits at once (as in 165) AND stays armed, so the settle
      after Resume fits once more (unless a gesture cancelled it); CDP synthetic-12: freeze, Pause, 5 → 12,
      Resume, settle → 0 visible nodes off screen (unit pins + CDP). (added by orchestrator)
- [x] A plain camera pan (unshifted stage drag past the 3 px threshold) during the settle cancels the armed
      auto-fit like a drag/marquee, and the camera keeps the pan; CDP synthetic-12: reveal, pan during the
      settle → no fit, camera unchanged through the settle (unit pins + CDP). (added by orchestrator)
- [x] Ranking first under ANY `max_results`: closure rows carry a transient `_closure_added`; `ranked_rows` orders hop nodes, hop edges, closure nodes, closure edges and strips the marker for `search_memory` and `deep_search_memory`; it never reaches serialized rows, files or the payload; star fixture at max_results 3/6/8/10 is byte-identical to closure-off (unit tests). (added by orchestrator after QA)
- [ ] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green;
      test count ≥ today's; `.tree/graphs/*.html` cleaned up. (QA green per Tester Log; deferred: orchestrator cleans after the human pass)

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

### [SWE] 2026-10-02 00:57 — Implementation

**Files modified**
- `apps/memory/src/tree/memory/graph/retrieval.py`:
  - New `_attach_part_of_closure(collection, user_id, nodes, edges)`. `expand_graph` calls it on both return paths (the hop path and the `max_hops == 0` path).
  - It makes two batched finds, each led by `user_id`:
    1. An edge read on `part_of` with `$or` [source `$in` chunk ids ∪ lookahead parents, target `$in` parent ids].
    2. A node read on the missing UP targets, un-projected.
  - Rows are appended, never reordered. `child_count` is stamped on a COPY of the parent row.
  - A result with no chunks issues no read. One DEBUG line: `part_of closure: +N node(s), +M edge(s), K parent(s) with hidden children`.
  - The module docstring now says the chain to the document is always complete.
  - The INFO "Graph expansion" line now counts what the caller receives, closure included.
- `apps/memory/src/tree/memory/visualize/graph.py`:
  - `to_graph_payload`: a `child_count` row adds `childCount`. Rows without it are byte-identical to before.
  - `_RENDER_JS` `enterNode` adds `card["child chunks"] = n.childCount + " (not shown)"` before `metaRows(card)`.
  - Orchestrator extra 1: in `applyDocumentLimit`, a reveal does `if (sim) fitOnSettle = true; if (!sim || paused) autoFit();`. A paused reveal therefore fits at once AND stays armed for the settle after Resume.
  - Orchestrator extra 2: `let pan` records an unshifted stage/edge press (`startMarquee`). `mousemovebody` checks it BEFORE the press/marquee early return. Past `DRAG_THRESHOLD` it sets `fitOnSettle = false`. Sigma's pan is never prevented. `buttons === 0` and `mouseup` clear it.
- `docs/adrs/011_…md`: the PA's text applied verbatim:
  - Decision 8.
  - Diagram node `EX`, also added to the `pyNode` class line so it renders styled.
  - Consequences bullet "Two more reads per query view".
  - Plus the §4 clause amended for the two orchestrator extras, marked "added by orchestrator, task 166". "Eight related choices" is unchanged.
- `apps/memory/README.md`: the slider sentence now says a box select or pan also cancels the fit, and that while paused it fits at once and again after Resume. One clause on the query view's `child chunks  N (not shown)` hover row. The glossary does not describe the auto-fit, so it needs no edit.
- `apps/memory/tests/unit/memory/conftest.py`: the fake now evaluates `$graphLookup` (breadth-first, `restrictSearchWithMatch`) and a `$setUnion` `$project`. The change is purely additive (other stages are still ignored as before). `$or` already existed.
- `apps/memory/tests/unit/memory/graph/test_retrieval.py`: `TestPartOfClosure`, 10 tests, plus the `chunk_star_rows` fixture.
- `apps/memory/tests/unit/memory/visualize/test_graph.py`, 10 new tests:
  - Paused re-arm, pan press, pan cancel, release ends the pan.
  - `childCount` gating (×2), hover-card order, 3 template tokens.
  - Two 165 pins updated deliberately: the reveal-arm strings, and `test_a_plain_stage_drag_still_pans` (which now asserts that the pan branch sits before `preventSigmaDefault`).
- `apps/memory/tests/unit/mcp/test_viz_app.py`: 5 both-variant tokens.
- `apps/memory/tests/unit/mcp/test_graph_tools.py`: `test_search_memory_truncation_keeps_the_ranked_rows_ahead_of_the_closure`.

**Tests**
- Unit: **4403 passed**, 0 failed (`make memory-tests`; 165 baseline 4372).
- Red first, for the right reasons:
  - Retrieval: 7 assertion failures (the fake hop gave 6 nodes / 5 edges, with no closure).
  - Template: 20 assertion / substring failures.
- The `search_memory` truncation test pins existing behaviour, so it was green from the start.
- Integration: N/A (no suite).

**Acceptance criteria**
- [x] Closure, lookahead, 2 reads, `user_id` first, none without chunks — `TestPartOfClosure::test_every_chunk_hangs_off_its_parent_and_document`, `::test_a_pulled_in_childs_parent_and_document_come_in_two_reads`, `::test_closure_reads_are_batched_and_tenant_scoped_first`, `::test_a_result_without_chunks_issues_no_extra_read`, `::test_the_zero_hop_path_also_closes_the_chain`
- [x] `child_count` rule — `::test_no_parent_with_a_drawn_child_or_no_child_is_stamped`, `::test_a_parent_whose_children_are_all_absent_reports_their_count`, `::test_stamps_a_copy_never_the_row_read`
- [x] Order / append / no duplicates / pipeline unchanged — `::test_existing_rows_keep_their_order_and_closure_rows_follow`; `TestExpandGraph` and `TestQueryMemoryComposition` are untouched and green; `test_graph_tools.py::test_search_memory_truncation_keeps_the_ranked_rows_ahead_of_the_closure`
- [x] `childCount` payload + hover row, both variants — `test_graph.py::test_only_a_stamped_parent_carries_child_count`, `::test_an_unstamped_payload_is_unchanged_by_the_child_count`, `::test_the_hover_card_ends_with_the_hidden_child_count`, `::test_template_carries_the_child_count_row[*]`; `test_viz_app.py` tokens
- [ ] Headless smoke: the Tester owns this. SWE pre-run is in Evidence (all green).
- [ ] [HUMAN]: left for the human. Files are below.
- [x] ADR-011 Decision 8.
- [x] (added by orchestrator) Pause → reveal → Resume — `test_graph.py::test_a_reveal_arms_one_auto_fit_and_hiding_never_fits`, `::test_a_paused_reveal_fits_now_and_again_at_the_settle_after_resume`; CDP `pausedResumeThenFit`
- [x] (added by orchestrator) Pan cancels — `::test_a_plain_stage_press_is_remembered_as_a_pan`, `::test_a_plain_pan_cancels_an_armed_auto_fit_and_keeps_its_camera`, `::test_a_release_ends_the_pan`, `::test_a_plain_stage_drag_still_pans`; CDP `panDuringSettleInertia`
- [ ] Format / lint / pre-commit / tests are green (4403 ≥ 4372). **`.tree/graphs/*.html` cleanup is NOT done**: the orchestrator forbade deleting them, because the human views them.

**Evidence**
```
$ make env-status -> Env target: local (.env)
$ make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check && make pre-commit
  317 files already formatted / All checks passed! / prettier, ruff check, ruff format, biome: Passed
$ make memory-tests -> 4403 passed in 53.52s
$ node --check on both variants (165's check_syntax.py) -> iframe SYNTAX OK, file SYNTAX OK

# Payload check (scratchpad/166/check_payload.py over `const DATA = …`), same data, BEFORE = 8867a92 tree, AFTER = this tree
# (162's read-only wrapper run_readonly.py; no index touched, reads only)
before-h1  78 nodes 75 edges | floating chunks 1 (how-does-memory-for-ai-agents-work#parent-1) | childCount 0
after-h1   78 nodes 76 edges | floating chunks 0 | childCount on 1 node: [23] | parent-1: part_of up -> document, childCount 23
before-h2 105 nodes 166 edges | floating 0 | childCount 0
after-h2  105 nodes 166 edges | floating 0 | childCount 0 (parent-1's children are drawn at 2 hops -> no row)

# probe.py: SAME seeds per query, expand_graph with the closure OFF (patched to identity) vs ON
langgraph agent memory h1  OFF 78/75 floating 1 -> ON 78/76 floating 0 stamped [23]  prefix_kept True
langgraph agent memory h2  OFF 105/166 floating 0 -> ON 105/166 floating 0           prefix_kept True
voyage rate limit      h1  OFF 87/85 floating 2 -> ON 87/87 floating 0 stamped [10, 20] prefix_kept True
voyage rate limit      h2  OFF 111/185 floating 1 -> ON 111/186 floating 0 stamped [10] prefix_kept True
prefect deployments    h1/h2  59/58, 61/111 unchanged, floating 0                  prefix_kept True
memory for ai agents   h1  OFF 80/77 floating 1 -> ON 80/78 floating 0 stamped [21]  prefix_kept True
memory for ai agents   h2  OFF 105/167 floating 1 -> ON 105/168 floating 0          prefix_kept True
search_memory("voyage rate limit", max_results=5): same five rows OFF vs ON -> True
deep_search_memory("prefect deployments") (MEMORY_DIR patched to the scratchpad): index 1451 lines OFF and ON, 17 document entries each (3 hops is already complete)

# headless --dump-dom smoke (162's command)
langgraph-agent-memory-20261001-214857   <body data-layout="live" data-sim="running"> | 78 nodes · 76 edges | legend
langgraph-agent-memory-max-hops-2-166    <body data-layout="live" data-sim="running"> | 105 nodes · 166 edges | legend
synthetic-12-documents-20261001-214854   <body data-layout="live" data-docs="5/12" data-sim="running"> | 5 of 12 documents · 98 nodes · 150 edges

# CDP (headless Chrome 154 on :9555, own profile, swiftshader, 1400x900; instrumented copies; real mouse; exceptions [] in EVERY run)
childCountHover (query view): stamped = [how-does-memory-for-ai-agents-work#parent-1]
  hover it    -> tooltip rows: type chunk | subtype parent | created_at … | "child chunks  23 (not shown)"
  hover #parent-0 (children drawn) -> type | subtype | created_at   (no child row)
pausedResumeThenFit (synthetic 5->12): freeze, Pause, reveal, Resume, settle -> off 0, fits 2 (pause fit + settle fit), re-frozen
  CONTROL on the 165 file:                                               -> off 46, fits 1 (the drift the Tester reported)
panDuringSettleInertia (synthetic): reveal, plain stage pan 0.5 s later -> cam [0.2663,0.6785] from +250 ms (Sigma's pan inertia)
  through the settle to the end, fits 0, keptPan true
  CONTROL on the 165 file: same pan -> camFinal [0.5,0.5], fits 1 (pan lost)
165 regressions on the new synthetic file:
  autoFitReveal: off 0 after the fit, fits 1, re-frozen [-551,647,-587,615] -> [-960,1015,-921,941]; hide: camera unchanged; cycle 2 fits 2; later drag: no refit
  dragCancels: fits 0, camera + bbox unchanged through the settle
  pausedReveal: fits 1 at once, data-sim "paused", off 0
  revealThenPause: no fit while paused, after Resume + settle fits 1, off 0
  marqueeArmed: fits 0; escOnly (plain Esc / sub-threshold Shift-click while armed): fits 1, off 0
```

**Notes**
- **Spec deviation, test fixture.** The spec's fixture (seed `p1`, `max_hops=1`, "c3a reached via c1b→c3a") cannot happen under real `$graphLookup`. Each pass is direction-consistent: the incoming walk from `p1` follows `source_node_id`, so `c1b→c3a` is never walked.
  - I extended the fake to evaluate `$graphLookup` for real (so the hop is a behaviour, not a mock) and seeded `["p1", "c1b"]`.
  - `c3a` is then reached through `c1b`'s outgoing `next`, as in a real result where another seed's hop lands on a foreign child.
  - Everything else is as specified: the same rows, the same assertions, 3 finds = hydration + 2.
- **ADR text vs UI, for the PA.** §8 was applied verbatim and says the hover card reads "N child chunks (not shown)". The spec's tooltip (implemented) renders `child chunks  23 (not shown)`, a key/value row via `metaRows`. A one-word ADR fix would align them; I did not change the PA's text.
- **Truncation caveat, for the PA.** `search_memory` truncates `result.nodes + result.edges`. Closure NODES are appended to `nodes`, so they sort ahead of the hop's EDGES in that flat list.
  - The first `max_results` rows are unchanged only while `max_results` ≤ the hop's node count. That holds for the stories (5 and 10 vs 59-111 nodes locally; the probe shows the same five rows).
  - With a large `max_results` on a tiny result, a hydrated document could displace a hop edge.
  - On the local corpus the closure has hydrated NO node so far (every document was already in the result), so today it only appends edges.
- **Hybrid search is non-deterministic run to run** ("voyage rate limit": 5 seeds, then 4). That is why the before/after probe fixes the seeds and calls `expand_graph`. The same applies to anyone comparing two `make memory-query-graph` runs.
- **Model-visible change.** `child_count` is a top-level row key. It therefore also appears in `search_memory`'s / `deep_search_memory`'s serialized rows for stamped parents. The deep-search YAML index has a fixed field list, so it is unchanged. "Same five rows" above compares `_id`s, not bytes. This is by design (the spec stamps it like `_search_score`).
- The [HUMAN] files themselves were re-checked with check_payload.py: `langgraph-agent-memory-20261001-214857.html` has 78/76, 0 floating, parent-1 → document, childCount 23. `…max-hops-2-166.html` has 105/166, 0 floating, no childCount.
- The closure always issues its node read (an empty `$in` when nothing is missing), so the count is exactly 2, as in 165's "step 4 always runs".
- Pan cancel: only an unshifted LEFT press on the stage or an edge that moves ≥ 3 px counts. A sub-threshold click and a wheel zoom do not cancel. A stale `pan` cannot outlive a lost mouseup, because the first button-less `mousemovebody` clears it.
- **[HUMAN] files** (under `apps/memory/.tree/graphs/`, rendered from the FINAL template; the six 165 files were left untouched):
  - `langgraph-agent-memory-20261001-214857.html`: the [HUMAN] AC's file. Hover `…how-does-memory-for-ai-agents-work#parent-1` to see `child chunks  23 (not shown)`; `#parent-0` (children drawn) has no such row.
  - `langgraph-agent-memory-max-hops-2-166.html`: the same query at `--max-hops 2`. parent-1's children are drawn and the row is gone (Story 2 step 3).
  - `synthetic-12-documents-20261001-214854.html`: for the two orchestrator extras.
    - Pause → slide to 12 → Resume: it ends framed.
    - Slide to 12, then pan the stage during the settle: the pan is kept.
- **CDP harness:** `scratchpad/166/cdp/` holds `cdp.mjs` on port 9555 and `scenarios.mjs` (qa165c's, plus `childCountHover` and `panDuringSettleInertia`), with logs per scenario (`control-*.log` = the 165 file). Payload checker: `scratchpad/166/check_payload.py`. Probe: `scratchpad/166/probe.py`. `<scratchpad>` = `/private/tmp/claude-501/-Users-pauliusztin-Documents-01-Projects-AI-Engineer-Handbook-building-agentic-systems-building-agentic-systems/944c4516-3945-43f4-8111-e0c95524323d/scratchpad`.
- No Mongo index or row was touched: `find` / `aggregate` / `countDocuments` only, through the read-only wrapper.

### [Tester] 2026-10-02 — QA

**Test summary**
- Format / lint / pre-commit: PASS (317 files formatted, ruff clean, prettier/ruff/biome pass)
- Unit tests (`make memory-tests`, run twice): 4403 passed / 0 failed both runs; 0 warnings. No integration suite (deleted deliberately).
- Env target: local.

**E2E adversarial pass** (read-only wrapper `run_readonly.py`; renders written to the scratchpad, never `.tree/graphs`)
- Happy path: `query_graph.py --query "langgraph agent memory" --max-hops 1|2` -> 78/76 and 105/166. Headless smoke on three renders (lg h1, lg h2, voyage h1): `data-layout="live" data-sim="running"`, counts 78/76, 105/166, 107/107, 0 page exceptions. PASS
- Payload check (own script): 4 queries x hops 1/2 = 8 renders -> 0 floating chunks, 0 dangling edges, 0 duplicate edges, no stamped parent with a drawn child. childCount appears on lg h1 [23], memory-for-ai-agents h1 [21], voyage h1/h2 [10]; none on the hops-2 lg render. `#parent-1` of how-does-memory-for-ai-agents-work: `part_of` to the document, `childCount 23`, and Mongo (pymongo read) has exactly 23 `part_of` in-edges. PASS
- Never fewer rows (closure OFF via patched helper vs ON, same seeds, 8 runs): rows only grow (e.g. voyage h1 85 -> 87 edges, lg h1 75 -> 76); first rows identical (prefix_kept True for every run). PASS
- Reads: fake-collection tests pin exactly 2 extra finds, `user_id` first, 0 when there are no chunks, also on the `max_hops == 0` path; my own run on the star fixture saw 3 finds total (hop hydration + 2). PASS
- rag mode: `grep expand_graph|_attach_part_of src/` -> no callers outside `retrieval.py`; diff touches nothing under `rag/`. PASS
- Break: dangling child (parent_id points at a missing row): result is `['c1']` / `['c1>ghost']`; the dangling edge is PRE-EXISTING (identical with the closure patched to identity), the closure adds nothing; payload drops it. PASS (note)
- Break: another user's `part_of` edge on the same ids -> never read (tenant filter). PASS
- Break: child row with no `parent_id` -> no crash, no lookahead. PASS. Second call on the same collection -> identical result (idempotent). PASS
- **Break: `search_memory` truncation (KEY SCRUTINY) -> FAIL, details below.**
- CDP (synthetic 12-doc Full graph, real input, headless Chrome 154):
  - 165 set, all as expected: autoFitReveal (1 fit, hide leaves the camera, cycle 2 fits again, a later drag no fit), dragCancels (0 fits, camera unchanged), pausedReveal (fits at once, stays paused), fitAfterSettle, toggleStorm (1 fit after 40 toggles), revealThenPause, revealThenReset, marqueeArmed (0 fits), escOnly (1 fit), revealThenHide, dragCancelsThenFit, pausedResumeThenFit (2 fits then the Fit click = 3), panDuringSettle / panDuringSettleInertia (0 fits, camera keeps the pan through inertia + settle, off=64 is the user's own pan).
  - A. Pause -> reveal 5->12 -> hide -> Resume: fit at the reveal (fits 1), no fit at hide, ONE fit at the settle after Resume (fits 2); camera [0.5,0.5,1], 0 nodes off screen, nonFinite 0. PASS (note: the armed fit survives the hide and fires once for the 98 visible nodes; sane, same as the unpaused revealThenHide)
  - B. Pause -> reveal -> pan -> Resume: reveal fit 1, pan cancels the armed fit, fits stays 1 after Resume, camera unchanged through the settle (the layout then expands, 64 off screen: the user owns the camera). PASS
  - C. Pause -> three reveals (7, 9, 12) -> Resume: 3 immediate fits + exactly ONE after Resume (4 total), 0 off screen. PASS
  - D. Reveal -> pan during settle (0 fits, camera keeps the pan) -> hide -> reveal again: re-armed, 1 fit, 0 off screen. PASS
  - E. Pause, reveal, Resume, Pause again 150 ms later, reveal, Resume: 3 fits, 0 off screen, no exception. PASS
  - Hover cards (real renders): stamped parents show `child chunks  23 (not shown)` (lg h1 `#parent-1`) and `child chunks  10 (not shown)` (voyage h1 `#parent-2`); parents with drawn children show NO such row. A zero-child parent has no instance in the real corpus; pinned by `test_no_parent_with_a_drawn_child_or_no_child_is_stamped` and the `count and ...` guard (never stamps 0). PASS
  - 163/164 regression set (nestedA-D, pinnedShift, escMidDrag/Marquee, marqueeOutside, lostMouseup, otherButtons, rapid, fitThenDrag, zoomedMarquee, fuzz, dbl, nudge): all 16 run clean, 0 exceptions. Four boolean diffs vs the old 164 baselines (different graph) were re-run old-JS vs new-JS on the SAME synthetic file with the three JS changes reverted: 0 diffs. PASS

**Acceptance criteria**
- [x] PASS - closure rows, 2 batched `find`s `user_id` first, none without chunks (`TestPartOfClosure::test_*`, 10 tests green; fixture seed `["p1","c1b"]` is legitimate: with seed `p1` alone `c3a` is never reached in one hop (I ran it), and the lookahead is still pinned: `p3` in the node read, 3 finds total; test_retrieval.py has 215 additions / 0 deletions, the existing `_seed_ids`, `hybrid_search` and `$graphLookup` pipeline tests are untouched)
- [x] PASS - `child_count` only when children exist and none drawn (tests above + real data: 23 matches Mongo)
- [x] PASS (literal) - existing rows keep their order within `nodes` and within `edges`, closure appended, no duplicate edges, pipeline unchanged. BUT see the FAIL below: the purpose stated in the spec/ADR/story does not hold.
- [x] PASS - `childCount` only for stamped rows; template tokens in both variants (`test_graph.py`, `test_viz_app.py`)
- [x] PASS - headless smoke + payload check (above)
- [ ] [HUMAN] - awaiting human verification
- [x] PASS - ADR-011 Decision 8 present (+ `EX` node + Consequences bullet)
- [x] PASS - paused reveal fits at once and stays armed (unit pins + CDP pausedReveal / A / C)
- [x] PASS - plain pan cancels the armed fit and keeps the camera (unit pins + CDP D / panDuringSettleInertia)
- [ ] FAIL - `.tree/graphs/*.html` cleaned up: `apps/memory/.tree/graphs/langgraph-agent-memory-max-hops-2-166.html` (00:49) and `langgraph-agent-memory-20261001-214857.html`, `synthetic-12-documents-20261001-214854.html` (00:48) are this task's leftovers (orchestrator said do not delete; SWE/orchestrator to remove before commit)
- [ ] FAIL - User story "Assistant's search answer keeps its ranking" / ADR-011 §8 sentence "so `search_memory`'s `max_results` truncation keeps ranking first" (see Failure 1)

**Failure 1: ranking-first only holds while `max_results` <= the original node count**
`search_memory` slices `result.nodes + result.edges`. The closure appends its nodes to `nodes`, so a closure node sits AHEAD of every original edge in that concatenation. Reproduced with the real `expand_graph` on the spec star fixture (6 hop nodes, 5 hop edges; closure adds node `p3` and 3 edges), through the real `search_memory`, closure OFF vs ON:
```
MR3  same=True   [doc1,p1,p2]
MR6  same=True   [doc1,p1,p2,c1a,c1b,c3a]
MR8  same=False  before=[doc1,p1,p2,c1a,c1b,c3a,p1>doc1,p1>p2]
                 after =[doc1,p1,p2,c1a,c1b,c3a,p3,p1>doc1]            <- p3 displaces the hop edge p1>p2
MR10 same=False  before=[..., p1>doc1,p1>p2,c1a>p1,c1b>p1]
                 after =[..., p3,p1>doc1,p1>p2,c1a>p1]                  <- c1b>p1 (an original row) lost to p3
```
The default `max_results` is 10, so any result with fewer than ~10 original nodes that gains a closure node changes its first rows. The SWE's `test_search_memory_truncation_keeps_the_ranked_rows_ahead_of_the_closure` uses `max_results=3` equal to the original node count, the one regime that cannot catch this. The real-data probe could not see it either (`max_results=5`, and the closure added NO node on any of the 8 real renders). Impact on real corpora is small (typical results have dozens of nodes), but the guarantee is stated unconditionally in ADR §8 and the story.
Fix (any one; the contract must not change and nothing may leak into `_serialize` or the payload):
1. `QueryResult` is not to change shape, so keep the closure counts out of it: have `search_memory` build `docs` as original nodes, then original edges, then closure rows. Simplest way without a new field: `_attach_part_of_closure` returns the closure rows through a second module-level helper used by `expand_graph`, and `structured_query_memory` returns the head order `nodes_orig + edges_orig + closure_nodes + closure_edges`? That would put edges inside `nodes`, which breaks `QueryResult` semantics, so prefer:
2. add two additive `int` fields to `QueryResult` (`appended_nodes: int = 0`, `appended_edges: int = 0`, excluded from serialization), and in `search_memory` order `nodes[:n-a] + edges[:e-b] + nodes[n-a:] + edges[e-b:]` before slicing; or
3. if the PA prefers no contract change at all: weaken ADR §8 + the story to "ranking-first holds while `max_results` is at most the original node count" and rename the SWE's test accordingly.
Regression test (required with 1 or 2): the star fixture, `search_memory(max_results=8)` and `(max_results=10)`, asserting the serialized ids equal the closure-off ids (my scratch test, `MR8`/`MR10` above).

**Other issues found**
- ADR §8 / Story wording: ADR says the card reads "N child chunks (not shown)" but the built row is `child chunks  N (not shown)` (fix at commit time, as the SWE noted).
- Pre-existing, not a regression: a hop edge whose far endpoint row does not exist (dangling `parent_id`) stays in `QueryResult.edges`; the payload filters it.
- Deep-search story is consistent (the 3-hop index is already part_of-complete on the probe query: 1451 lines, 17 document entries ON and OFF); no document rows were added, so that story is only partly exercised.
- Removed assertions in `test_graph.py` (`if (!e.event.original.shiftKey) return;`, `if (!press && !marquee) return;`, `if (sim && !paused) ... else autoFit();`) all have updated replacements for the new code; not weakened.

**VERDICT: FAIL**

### [SWE] 2026-10-02 01:40 — Fixes for QA FAIL (truncation ranking) — orchestrator decision

**Fix**
- `retrieval.py`:
  - Every row `_attach_part_of_closure` appends (hydrated nodes, added edges) is a copy marked `CLOSURE_ADDED = "_closure_added"`. Same precedent as `_search_score`.
  - New `ranked_rows(result)` does a stable sort of `nodes + edges` on that marker, giving hop nodes, hop edges, closure nodes, closure edges. It then strips the marker.
  - `QueryResult`'s contract is unchanged.
- `graph_tools.py::search_memory`: `docs = ranked_rows(result)` BEFORE the `max_results` slice. `_serialize` and the graph payload therefore both get unmarked rows.
- `deep_search.py::write_deep_search_results`: also uses `ranked_rows`, so the index order is the same and no file or index line carries the marker.
- `query_memory` (MCP) runs `execute_nl_query`, not `expand_graph`, so it is unaffected.
- `to_graph_payload` already ignores unknown keys (curated `meta` only); this is now pinned by a test.
- ADR-011 §8:
  - The hover text now reads `` `child chunks  N (not shown)` ``, matching the built card.
  - New sentence on the marker and `ranked_rows` ordering, marked "changed by orchestrator after QA".
- `.tree/graphs` files are deliberately kept: the human views them and the orchestrator cleans them up at the end. The cleanup AC stays unticked.

**Tests**
- 10 new tests, red first:
  - `test_search_memory_answers_the_same_rows_with_the_closure[8|10]` failed exactly as the Tester showed: `p3` displaced `p1>doc1`.
  - `test_search_memory_appends_the_closure_after_every_original_row` and `test_deep_search.py::test_the_index_lists_every_original_row_before_the_closure` failed with order `n1, n2, e1, e2`.
  - The retrieval tests failed on an ImportError for the new API.
- `tests/unit/memory/conftest.py::chunk_star_rows(user_id)`: the star fixture is now a plain builder. Both `test_retrieval.py` and `test_graph_tools.py` use it, the latter through the REAL `expand_graph` over `FakeMemoryCollection`.
- The new tests:
  - `test_graph_tools.py::test_search_memory_answers_the_same_rows_with_the_closure[3|6|8|10]`: byte-identical serialized answer with the closure on vs patched off.
  - `::test_search_memory_appends_the_closure_after_every_original_row` (max_results 20): the off ids are a prefix, then `p3`, then the 3 closure edges, with no marker.
  - `::test_search_memory_never_leaks_the_closure_marker_to_the_graph`: visualize=True; no marker in any content block or in `structured_content`.
  - `test_retrieval.py::TestPartOfClosure::test_only_the_rows_the_closure_appended_are_marked`, `::test_ranked_rows_put_every_hop_row_before_the_closure_unmarked`.
  - New `tests/unit/mcp/test_deep_search.py`: index order, and no marker in any of the 5 written files.
  - `test_graph.py::test_the_closure_marker_never_reaches_the_payload`.
- My earlier `test_search_memory_truncation_keeps_the_ranked_rows_ahead_of_the_closure` (`max_results=3`, which could not catch the bug) is replaced by the parametrized test.

**Evidence**
```
$ make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check && make pre-commit -> clean (318 files)
$ make memory-tests -> 4413 passed in 51.30s   (was 4403)
# probe_fix.py (scratchpad/166; read-only; fixed seeds; real search_memory / deep_search_memory functions)
search_memory max_results=5:    off 5,   on 5,   on[:len(off)] == off True, marker in output False
search_memory max_results=10:   off 10,  on 10,  True, False
search_memory max_results=100:  off 100, on 100, True, False
search_memory max_results=1000: off 172, on 174, True, False   (the 2 closure edges come last)
deep_search ("voyage rate limit", 1 hop): 175 files, marker in index False, in any file False
query render (langgraph, 1 hop): 78 nodes / 76 edges, 0 floating, parent-1 -> document, childCount 23; "_closure_added" in HTML: 0
```
- The template is unchanged in this round, so the [HUMAN] files and the CDP evidence still hold.

### [Tester] 2026-10-02 — Re-QA (ranking-first fix round)

**Test summary**
- Format / lint / pre-commit: PASS (318 files formatted, ruff clean, prettier/ruff/biome hooks Passed)
- `make memory-tests` run twice: 4413 passed / 0 failed both runs; no pytest warnings summary (the only "warning" text is Opik's import-time Pydantic-V1 UserWarning, environmental, pre-existing)
- Integration tests: none by design (CLAUDE.md)

**E2E adversarial pass**
- Happy path: independent star-fixture probe (`scratchpad/166/re/star_probe.py`, REAL `expand_graph` over `FakeMemoryCollection(chunk_star_rows)` seed `["p1","c1b"]`, 1 hop, through REAL `search_memory`), closure on vs `_attach_part_of_closure` patched to identity: `max_results` 1/3/6/8/10/11 -> byte-identical output strings; 12/14/20 -> first 11 ids identical, closure rows only after (full order: `doc1,p1,p2,c1a,c1b,c3a, p1>doc1,p1>p2,c1a>p1,c1b>p1,c1b>c3a, p3, p2>doc1,p3>doc1,c3a>p3`). PASS
- Break 1 (state: in-place mutation): collection rows deep-equal before/after `expand_graph`; no result row is a collection row object; `ranked_rows(result)` leaves `result.nodes/edges` untouched (marker still on `p3, p2>doc1, p3>doc1, c3a>p3`) and returns no marker. PASS
- Break 2 (marker leak, every serialization path): `search_memory` text (all `max_results`) 0 hits; `search_memory(visualize=True)` ToolResult content + structured_content 0; `visualize_memory_graph(as_html_file=True)` written HTML 0; `deep_search_memory` index + 16 files 0, index order = hop rows first then closure; 8 real-corpus HTML renders 0 (`grep -l _closure_added` = 0). PASS
- Break 3 (src audit of every consumer of a `QueryResult` from `expand_graph`/`query_memory`): `graph_tools.search_memory` -> `ranked_rows`; `deep_search.write_deep_search_results` -> `ranked_rows` (only `.nodes/.edges` use in that module); `graph_tools.visualize_memory_graph`, `dashboard_app._fetch_payload`, `scripts/query_graph.py` -> `to_graph_payload`, which reads named keys only (marker cannot pass; unit-pinned); `graph_tools.query_memory` (NL) never touches `expand_graph`; `deep_search_memory` only truth-tests `.nodes/.edges`. No other place concatenates/serializes nodes+edges. PASS
- Break 4 (real corpus, read-only wrapper, 4 queries x hops 1,2 = 8 renders): 0 floating chunks in all 8, 0 dangling edges, 0 dup edges, stamped parents never have a drawn child (langgraph h1 `[23]`, memory-for-ai-agents h1 `[21]`, voyage h1/h2 `[10]`). PASS
- Break 5 (CDP, fresh voyage-rate-limit 1-hop render, real mouse hover): stamped parent `how-does-memory-for-ai-agents-work#parent-2` card ends `child chunks  10 (not shown)`; a parent with drawn children (`context-engineering-2025s-1-skill#parent-0`) has no such row; layout `live`, 0 exceptions. PASS

**Acceptance criteria** (all non-[HUMAN] re-verified; evidence = the passes above + 4413-test suite)
- [x] PASS — closure completeness / 2 batched finds / none without chunks (unit tests green; real renders 0 floating)
- [x] PASS — `child_count` only when no child drawn (unit tests; real renders 0 stamped-with-drawn-child)
- [x] PASS — order preserved, no duplicate edges, existing tests untouched
- [x] PASS — `childCount` iff stamped; hover tokens in both variants (CDP card above)
- [x] PASS — headless smoke / 0 floating / childCount (8 real renders)
- [x] PASS — ADR-011 Decision 8 present (now documents ordering + `child chunks  N (not shown)`)
- [x] PASS — Pause/Resume re-fit and pan-cancel ACs (template unchanged this round; 4413 green)
- [x] PASS — Ranking first under ANY `max_results` (added by orchestrator after QA): star fixture byte-identical at 3/6/8/10 (+1,11), `_closure_added` never serialized
- [ ] [HUMAN] — awaiting human verification
- [ ] OPEN by orchestrator decision (NOT a blocker) — `.tree/graphs/*.html` cleanup (left for the human)

**Other issues found (notes, non-blocking)**
- `_closure_added` (and `child_count`) ride on `QueryResult` rows, so the `@track`-ed `expand_graph`/`query_memory` Opik spans will log the marker. Observability only, not model-facing; mention if span size/noise matters.
- `child_count` IS visible to the model in `search_memory` text / deep-search node files for stamped parents (like `_search_score`). Intended per spec; no leak of the marker.
- My own probe's first run wrote one stray `q-*.html` into `.tree/graphs` (unpatched dir); I removed that single file (mine). No other file there was touched.

**VERDICT: PASS**
