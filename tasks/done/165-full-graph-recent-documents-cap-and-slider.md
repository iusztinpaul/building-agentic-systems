---
id: 165-full-graph-recent-documents-cap-and-slider
status: done
feature: dynamic-graph-viz
---

# Full graph: the 500 most-recent documents embedded, a Documents slider shows 100 by default

Tags: `viz`, `memory`, `mcp`, `config`, `docs`
Depends on: 164
Blocks: —
Implements: ADR-011 (§7, added by this grooming round)

## Scope

Today `fetch_full_graph` (`apps/memory/src/tree/memory/graph/retrieval.py` l.213) loads EVERY
node and edge the user owns — embeddings included — and the browser draws all of it. This task
makes the **Full graph** a recency-ranked, capped view: Python embeds the `max_docs` most-recent
**Document**s' subgraphs (default 500) with a per-node recency rank, and the **Graph renderer**
shows the first `shown` (default 100) through a `Documents` slider at the TOP of task 164's
Controls panel (top-left). Hidden nodes are hidden in sigma AND removed from the d3 simulation; the
browser never re-fetches. Query views and the **Embedding map** are untouched (no slider: gated on
the payload). Python decides, the JS obeys (ADR-011 §6).

Grooming-round doc edits this task OWNS (apply them in this task's commit): `docs/glossary.md` —
new **Full graph** row; **Graph payload** row gains `docRank` and `controls.documents`; **Graph
renderer** row gains the `Documents` slider sentence. ADR-011 — Decision 7 (below), Context
paragraph, diagram nodes `FG` / `DOCS`, Consequences bullets "The full graph is no longer
everything" and "Payload contract grows again". Exact text: see `## Log` → `[PA] Grooming`.

### Config (`apps/memory/src/tree/config/app_config.py::QueryConfig`, `configs/default.yaml`, the frozen fixture)
- Two flat keys under `query:` — `full_graph_max_docs: 500` (`int`, `ge=1`; how many documents a
  full-graph read embeds) and `full_graph_shown_docs: 100` (`int`, `ge=1`; how many the slider
  shows on load). A `model_validator` rejects `shown > max`. Add both to `configs/default.yaml`
  (two comment lines: "most-recent first; `properties.date`, else `created_at`") and to
  `tests/unit/config/fixtures/frozen_config.yaml`. Escape hatch works as is
  (`TREE_QUERY__FULL_GRAPH_MAX_DOCS=50`); nothing in `.env.example`.

### Retrieval (`retrieval.py::fetch_full_graph(client, database, user_id, *, max_docs: int | None = None) -> QueryResult`)
`max_docs` defaults to `app_config.query.full_graph_max_docs`. FOUR batched finds, a constant count
whatever `max_docs` is — never one per document — every filter `user_id` first, every node find
projected `{"embedding": 0}` (the payload never reads vectors; today's read hauls every child
embedding across the wire):

1. **Rank the documents.** `find({"user_id": user_id, "kind": "node", "type": "document"}, {"embedding": 0})`
   (served by `user_kind_type`). Recency of a document row = `properties.date` parsed with
   `datetime.fromisoformat` (a naive result gets `tzinfo=UTC`; `None`/unparsable → skip), else
   `created_at` (a naive one gets `tzinfo=UTC`), else `datetime.min` in UTC (ranks last). Sort
   descending, tie-break `_id` ascending (deterministic), keep the first `max_docs`. `doc_rank` =
   1-based position. Why Python and not a `$sort`: `properties.date` is an ISO string and
   `created_at` a BSON date, so Mongo would need `$dateFromString` + `$ifNull` and an in-memory sort
   anyway (no index on either); the document rows are the smallest set in the collection and the
   fallback is unit-testable in Python. Upgrade trigger: a tenant whose document rows alone take
   measurably long to scan (~50k documents) → move the ranking into an aggregation.
   `provenance_rank = {doc["sources"][0]: rank}` for every kept row that has `sources`; a document
   row without `sources` is still ranked and embedded alone (`logger.debug`).
2. **Their subgraph nodes.** `find({"user_id": user_id, "kind": "node", "sources": {"$in": list(provenance_rank)}}, {"embedding": 0})`
   — document rows (dedupe by `_id` against step 1), both chunk levels, and every entity / fact /
   preference extracted from those documents, in one query. Node `doc_rank` =
   `min(provenance_rank[s] for s in row["sources"] if s in provenance_rank)` — an entity shared
   across documents ranks with its MOST RECENT one.
3. **Their edges.** `find({"user_id": user_id, "kind": "edge", "sources": {"$in": list(provenance_rank)}})`
   (Added by orchestrator after QA: the filter is `{"user_id", "kind": "edge", "$or": [{"sources": {"$in": …}}, {"sources": []}]}`
   — a no-provenance edge such as `same_as` is kept only when both endpoints are already included, never
   hydrates an endpoint, and ranks as the min of its endpoints' ranks.)
   (`part_of`, `next`, `referenced`, `mentions`, `related_to`, `same_as`, … of those documents).
   Edge `doc_rank` by the same `min` rule.
4. **Endpoints not yet loaded.** Collect `source_node_id`/`target_node_id` of step-3 edges that are
   not in the node set (e.g. a hand-added entity with `sources: []`, a `referenced` target) and
   hydrate them with ONE `find({"user_id": user_id, "kind": "node", "_id": {"$in": missing}}, {"embedding": 0})`
   (added by orchestrator after QA: a hydrated row whose `sources` is non-empty with NO kept provenance —
   owned only by excluded documents — is skipped, and the both-endpoints rule then drops its edges);
   each gets `doc_rank` = the smallest `doc_rank` among the edges that reference it. Then drop any
   edge whose endpoint still does not exist — the result carries ONLY edges with both endpoints
   included (so `to_graph_payload` materialises no `unknown` nodes on the Full graph).

`doc_rank: int` is stamped as a top-level key on every returned node and edge row (precedent:
`hybrid_search` stamps `_search_score`); `QueryResult` itself is unchanged (ADR-008 §3). Nodes with
`sources: []` and no edge to an included node are NOT in the Full graph (they belong to no
document's subgraph — see Out of scope). Log one INFO line:
`"Full graph: embedded %d of %d documents (most recent first) → %d nodes, %d edges"`. Docstring
rewrite: it is no longer "the ENTIRE graph".

### Payload (`visualize/graph.py::to_graph_payload`)
- A node whose row carries `doc_rank` gets `"docRank": <int>`; otherwise the key is ABSENT (query
  payloads stay byte-identical to today). Edges carry no rank — the browser draws an edge iff both
  endpoints are visible.
- When at least one node carries `docRank`, `controls` gains
  `"documents": {"shown": min(app_config.query.full_graph_shown_docs, total), "total": total}` with
  `total = max(docRank)`. No ranked node → no `documents` key (this is the slider gate; the map's
  `to_embedding_map_payload` never emits it). `graph.py` imports `app_config` for the one default —
  Python decides the number, the template contains no `100`.
- `_render_graph_file`'s log line and `visualize_memory_graph`'s summary read, for a payload with
  `controls.documents`: `"{shown} of {total} documents shown by default, {n} nodes, {m} edges"`.

### CLI (`scripts/query_graph.py`, `apps/memory/Makefile`)
- `--max-docs` (`click.IntRange(min=1)`, default `app_config.query.full_graph_max_docs`,
  `show_default=True`, help: "Most-recent documents embedded in the full graph (no --query); the
  browser's Documents slider reveals up to this many. Ignored with --query."). Forwarded as
  `fetch_full_graph(..., max_docs=max_docs)`; never passed to `query_memory`. Makefile `query-graph`
  gains `$(if $(MAX_DOCS),--max-docs "$(MAX_DOCS)",)` and its comment mentions `MAX_DOCS=N`.
- The script's `Result:` log line stays; the retrieval INFO line above carries the document counts.

### MCP (`mcp/graph_tools.py::visualize_memory_graph`)
- New arg `max_docs: int | None = None` — "With no `query`: how many most-recent documents to embed
  (default from config, 500); the inline view shows the 100 most recent and a slider reveals the
  rest. Ignored with a `query`." `max_docs is not None and max_docs < 1` →
  `tool_error("invalid_input", "max_docs must be ≥ 1", retryable=False)` BEFORE any read. Forward
  only on the no-query branch. The summary string (model-visible) becomes
  `f"Knowledge graph for your full memory: {shown} of {total} most-recent documents shown by default, {n} nodes, {m} edges"`
  when `controls.documents` exists, unchanged otherwise. `dashboard_app._fetch_payload` inherits the
  config cap through the default (no new arg; its own template ignores `docRank`).

### Renderer (`_RENDER_JS`; both variants token-identical except ext-apps + height)
- **Section.** When `payload.controls.documents` exists, build a `Documents` section FIRST in
  `#panel-body` (above 164's Forces) with ONE `rangeRow`: label `Documents`, `min 1`, `max total`,
  `step 1`, value `shown`, readout `"{n} of {total}"`. Absent on query views and the map (gate
  pinned by tests). Changing it calls `applyDocumentLimit(n)`.
- **`applyDocumentLimit(n)`** — ONE function, readable in isolation:
  1. visible(node) = `node.docRank == null || node.docRank <= n` (an unranked node is always shown).
  2. `graph.setNodeAttribute(id, "hidden", !visible)` for every node. Sigma 3 skips an edge whose
     extremity is hidden — verify on 3.0.3; if it does not, set `hidden` on those edges in the same pass.
  3. Prune: delete hidden ids from `state.selection`; if `state.hovered` is hidden, clear it and hide
     the tooltip. Hidden nodes KEEP `fx`/`fy` — a pin survives being hidden and shows again on reveal
     (the overlay already skips hidden nodes, so no dot is drawn meanwhile).
  4. Live layout: `sim.nodes(visibleSimNodes)` and `sim.force("link").links(visibleLinks)` where
     `visibleLinks` is rebuilt from `drawnEdges` with both endpoints visible (self-loops still
     excluded) — d3 throws on a link to a node it does not own, which is why both are re-synced
     together; then `reheat()` (164's `sim.alpha(0.5).restart()`) unless paused — while paused the
     hide/show applies and the sim stays stopped (`dataset.sim` stays `"paused"`). Map: step 4 is a no-op.
  5. Header counts → `"{n} of {total} documents · {visible nodes} nodes · {visible edges} edges"`;
     `document.body.dataset.docs = n + "/" + total` (headless evidence, like `dataset.sim`);
     `renderer.refresh()`.
  Called once on boot with `shown` (so the first paint and the first simulation already exclude the
  hidden nodes — hidden nodes never enter the initial `forceSimulation(...)` node array).
- **Gesture guards:** `dragSetFor` never carries a hidden child; the marquee never selects a hidden
  node (both: check `graph.getNodeAttribute(id, "hidden")`). `Unpin all` clears `fx/fy` on hidden
  nodes too (one rule, no exceptions). `Reset to defaults` (164) returns the slider to `shown` and
  calls `applyDocumentLimit(shown)` as part of its single reheat.
- Headless marker already in place: `data-layout="live"`; new `data-docs="S/T"`.

### README (`apps/memory/README.md`, the `make memory-query-graph` section ~l.340–375)
- Two sentences after the full-graph command: the Full graph embeds the `MAX_DOCS` (default 500)
  most-recent documents and shows the 100 most recent; drag the `Documents` slider to reveal more
  (hidden nodes leave the simulation; pins survive). Mention `MAX_DOCS=N` and the two YAML keys.

### Tests (`/squid-testing-python`)
- **Fake extensions** (`tests/unit/memory/conftest.py`): `matches` treats `$in` against a LIST
  field as "any element in the list" (Mongo array semantics — `sources`); `FakeMemoryCollection.find(query, projection=None)`
  records `find_projections` alongside `find_filters`; `make_document_row` accepts `created_at`
  and `sources` overrides (default `sources=[PydanticObjectId()]` so provenance exists).
- `tests/unit/memory/graph/test_retrieval.py` (`fetch_full_graph`):
  - cap + order: 7 documents with mixed `properties.date` strings → `max_docs=3` keeps the three
    most recent, `doc_rank` 1..3, most recent first; equal dates tie-break on `_id`.
  - fallback: a row with `properties.date: None` ranks by `created_at`; a naive `created_at` is
    treated as UTC and does not raise; a row with neither ranks last.
  - subgraph: parents, children, entities of kept documents are returned; rows of a document beyond
    the cap are not; a shared entity (sources = docs ranked 1 and 3) carries `doc_rank == 1`.
  - edges: only edges with both endpoints present survive; a hand-added entity (`sources: []`)
    reachable by an included edge is hydrated with the edge's rank; an unreachable `sources: []`
    node is absent.
  - query shape: exactly 4 `find_filters` for 7 documents AND for 1 document (no N+1); every filter's
    first key is `user_id`; every node filter's projection is `{"embedding": 0}`; the `sources` `$in`
    lists are exactly the kept provenance ids; `max_docs=None` reads `app_config.query.full_graph_max_docs`.
- `tests/unit/config/test_app_config.py`: both keys load from the frozen YAML (500 / 100); defaults
  when absent; `full_graph_shown_docs > full_graph_max_docs` raises; `full_graph_max_docs: 0` raises.
- `tests/unit/memory/visualize/test_graph.py`: `docRank` present iff the row has `doc_rank`;
  `controls.documents == {"shown": min(cfg, total), "total": total}` only when a rank exists
  (patch `app_config.query.full_graph_shown_docs`); `shown` clamps to `total` when fewer documents
  exist; a query-style payload has no `documents` key and no `docRank`. Template tokens (both
  variants, `test_viz_app.py` list too): `Documents`, `controls.documents`,
  `function applyDocumentLimit`, `sim.nodes(`, `.links(`, `"hidden"`, `dataset.docs`,
  `" of "` + `" documents · "`, and that the section is gated on `controls.documents`; `100` does
  not appear as a literal in the panel code.
- `tests/unit/memory/visualize/test_embeddings.py`: the map payload has no `controls.documents`.
- `tests/unit/scripts/test_query_graph.py`: `--max-docs 7 --no-open` → `fetch_full_graph` awaited with
  `max_docs=7`; `--max-docs 0` → exit 2 (click range error); `--query q --max-docs 7` never reaches
  `fetch_full_graph`; the default equals the config value.
- `tests/unit/mcp/test_graph_tools.py`: `max_docs=7` forwarded on the no-query branch; `max_docs=0`
  → `invalid_input`, `retryable: false`, no read; with a query `max_docs` is not forwarded; the
  summary carries "of … most-recent documents" only on the full graph.

### Verification (Tester; headless smoke + CDP evidence as in 162/163, never prod)
- Full graph file (`make memory-query-graph MAX_DOCS=500` or the 162 read-only wrapper): 162's
  `--dump-dom | grep` prints `<body data-layout="live" data-sim="running" data-docs="4/4">` (local
  corpus: 4 documents), `<span id="counts">4 of 4 documents · N nodes · M edges`, and the dump contains
  the `Documents` label above `Centre force`.
- CDP (real-time headless Chrome, like 162's gesture pre-check): set the slider to `1`, dispatch
  `input` → counts read `1 of 4 documents · n' nodes · m' edges` with `n' < N`,
  `graph.filterNodes((_, a) => a.hidden).length === N - n'`, `sim.nodes().length === n'`,
  `data-docs="1/4"`, `data-sim="running"` again; set it back to `4` → counts restore and no node is
  lost; pin a document at `4`, slide to `1` (it hides, no dot), slide back → it is still pinned at the
  same spot; select three entities, slide to `1` → rings gone, `state.selection.size === 0`.
- `--query` file and the map file: dumps contain NO `Documents` label and no `data-docs`.
- A synthetic 12-document payload (hand-built `QueryResult` rows through `to_graph_payload` +
  `_render_graph_file`, scratchpad only) with `full_graph_shown_docs` patched to 5 → boot shows
  `5 of 12 documents` and the hidden nodes are absent from `sim.nodes()` on the FIRST tick.

## Out of scope
- Nodes with `sources: []` that no included edge reaches (hand-added entities nobody linked to a
  document): not part of any document's subgraph, so not in the Full graph. Revisit if a human asks
  for an "orphans" toggle.
- A `sources` index, `$dateFromString` ranking, server-side pagination / re-fetch on slider change
  (the browser hides what it already has), filtering by date range, a slider on query views or the
  map, the dashboard's own template (inherits the cap, gets no slider), persisting the slider value
  (ADR-011 §6: a reload is Reset).

## Acceptance Criteria

> `[HUMAN]` criteria are checked by the human in a real browser (Claude in Chrome cannot drive the WebGL stage). The SWE/Tester supply CDP evidence and do not block on them.

- [x] `fetch_full_graph` returns the `max_docs` most-recent documents' subgraphs: documents ranked by
      `properties.date` (ISO string, tz-aware), else `created_at`, else last; `doc_rank` 1-based on every
      node AND edge row; a shared entity carries the rank of its most-recent document; exactly `max_docs`
      document rows and nothing owned only by excluded documents; no-provenance edges kept between included
      nodes (added by orchestrator after QA) (unit tests + `real_check.py`).
- [x] Exactly four `find` calls regardless of document count, every filter `user_id` first, every node
      read projected `{"embedding": 0}`; only edges with both endpoints included are returned (unit tests).
- [x] `query.full_graph_max_docs` (500) and `query.full_graph_shown_docs` (100) load from YAML, both `ge=1`,
      shown clamped at use to `min(shown, max, total)` (changed by orchestrator: no `shown ≤ max` validator —
      a config error must never take down every entry point); `--max-docs` / `MAX_DOCS` / `max_docs` override the cap per call; `< 1` is
      refused (click exit 2 / `invalid_input` envelope); the cap is never forwarded on a query (unit tests).
- [x] `to_graph_payload` emits `docRank` only for ranked rows and `controls.documents {shown, total}` only
      when a rank exists; the map payload and query payloads carry neither (unit tests).
- [x] Both template variants contain the `Documents` slider gated on `controls.documents`,
      `function applyDocumentLimit`, `sim.nodes(` / `.links(` re-sync, `dataset.docs`, and no literal `100` (unit tests).
- [x] Headless smoke (Tester, Log): full-graph dump prints `data-docs="4/4"`, `4 of 4 documents · N nodes · M edges`
      and the `Documents` label; `--query` and map dumps contain neither.
- [x] CDP (Tester, Log): slider to 1 hides nodes in sigma AND drops them from `sim.nodes()`/links, reheats,
      updates counts and `data-docs`; back to 4 restores all; a pinned hidden node is still pinned on reveal;
      hidden selected nodes leave the selection; while paused the slider applies without restarting the sim.
- [x] (added by orchestrator after QA) Zoom-fit recovers every visible node after a frozen-camera reveal:
      the fit handler calls `renderer.refresh()` between `setCustomBBox(null)` and `camera.animatedReset()` (a
      settled simulation no longer ticks, so Sigma kept a stale extent); CDP freeze → reveal → settle → Fit = 0
      visible nodes off screen on `synthetic-12-documents` and the `langgraph` query view (unit pin +
      `fitAfterReveal`).
- [x] (added by orchestrator — human decision) A weak gravity pair `forceX(0)` + `forceY(0)` (one strength,
      `controls.forces.gravity` = 0.05 from Python, live layout only) and a `Gravity` slider (0–0.5, step 0.01,
      after Centre force, reheats, Reset restores); both variants carry `forceX(` / `forceY(` / `"Gravity"`;
      ADR-011 §1 four forces. Tuning target "0 stars / connected nodes off screen after a frozen-camera reveal"
      NOT met by any gravity value — see the SWE gravity Log entry (Fit recovers every node).
- [x] (added by orchestrator — human decision) Auto-fit on reveal: a `Documents` reveal (n > previous) arms ONE
      fit through the Fit path when the layout settles (at once when paused), re-freezing the camera if it was
      frozen; hiding never fits; a drag/marquee before the settle cancels it; only the camera moves (unit pins +
      CDP: frozen reveal 5→12 synthetic and 1→4 real → 0 visible nodes off screen; hide → camera unchanged;
      drag during the settle → no auto-fit). Gravity default stays 0.05.
- [ ] [HUMAN] On a graph with more documents than the default (patched `full_graph_shown_docs=2` on the local
      corpus, or a 12-document synthetic file): load shows `2 of 4 documents`; dragging the slider right adds
      whole document stars that settle in; dragging left removes them and the rest re-settles; nothing is
      drawn for hidden nodes (no stray edges, dots or rings); `Reset to defaults` returns to `2 of 4`.
      [HUMAN] pending — human visual check; CDP evidence in Tester Logs
- [x] README updated (`grep -c "Documents" apps/memory/README.md` ≥ 1, mentions `MAX_DOCS`); glossary
      **Full graph** row present; ADR-011 Decision 7 present.
- [ ] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green;
      test count ≥ today's; `.tree/graphs/*.html` cleaned up.
      Format/lint/pre-commit/tests green (Tester PASS); `.tree/graphs` cleanup deferred: human is viewing the files; orchestrator cleans after the feature

## User Stories

### Story: Operator opens a big memory and gets the recent hundred
1. `make memory-query-graph` on a tenant with 342 documents.
2. The log reads `Full graph: embedded 342 of 342 documents (most recent first) → 9120 nodes, 11874 edges`;
   the page header reads `100 of 342 documents · 2710 nodes · 3480 edges`.
3. The Controls panel's first section is `Documents` with a slider at `100 of 342`; the live layout settles
   on the 100 most-recent stars only.

### Story: Operator digs further back in time
1. Operator drags the `Documents` slider from 100 to 250: the readout follows, older document stars appear,
   the simulation reheats and re-settles; the header now reads `250 of 342 documents · …`.
2. Operator drags it to 342: everything embedded is visible. Operator clicks `Reset to defaults`: back to `100 of 342`.

### Story: Operator caps the embed itself
1. `make memory-query-graph MAX_DOCS=50` → log `embedded 50 of 342 documents`; the slider's max is 50 and
   the header reads `50 of 50 documents · …` (shown clamps to total).
2. `make memory-query-graph MAX_DOCS=0` → click exits 2 with "Invalid value for '--max-docs'".

### Story: Operator's pins and selection survive hiding sanely
1. Operator drags the newest document aside (it pins), Shift+clicks two of its entities (rings).
2. Operator slides to a value that hides that document: the star, its dot and the rings vanish;
   `Esc` has nothing to clear.
3. Operator slides back: the document reappears exactly where it was pinned, dot included; the two
   entities are not re-selected.

### Story: Assistant asks for the full memory inline
1. In a UI-capable client the user says "show my whole memory graph"; the model calls
   `visualize_memory_graph()`.
2. The text reads `Knowledge graph for your full memory: 100 of 342 most-recent documents shown by default, 2710 nodes, 3480 edges`;
   the iframe shows the slider at the top of the panel.
3. The user asks for "only the last 20 documents"; the model calls `visualize_memory_graph(max_docs=20)`
   → `20 of 20 documents`. `max_docs=0` answers an `invalid_input` envelope without touching Mongo.

### Story: A query view and the map are unchanged
1. `make memory-query-graph QUERY="langgraph agent memory"`: no `Documents` section, header `78 nodes · 75 edges`.
2. `make memory-visualize-embeddings HULLS=true`: panel shows Display + Unpin all only, as in 164.

---

Blocked by: 164

## Log

### [PA] 2026-10-01 — Grooming

Human decisions: full graph only (query views unchanged); embed-then-reveal (no live fetch);
500-document embed cap; most recent first; `Documents` slider at the top of the existing top-left panel.

**Doc edits owned by this task (apply verbatim):**

`docs/glossary.md`
- **Graph payload**, Definition — after "a curated `meta` subset": "; a node may also carry `docRank`
  (its **Full graph** recency rank) and a parent chunk `childCount` (children not in the result, task 166)."
  Notes — append: "The **Full graph** payload additionally carries `controls.documents: {shown, total}` —
  the gate for the renderer's `Documents` slider; query payloads and the **Embedding map** never carry it."
- **Graph renderer**, Notes — after "reads its defaults from the payload.": "On the **Full graph** the
  panel's first section is a `Documents` slider (`shown` of `total`): hidden nodes are hidden in Sigma AND
  removed from the d3 simulation (pins survive hiding, selection does not); it never re-fetches."
- New row before **Graph payload**:
  `| **Full graph** | The no-query view of visualize_memory_graph / make memory-query-graph (fetch_full_graph): the query.full_graph_max_docs (500) most-recent document rows — ranked by properties.date, else created_at, tz-aware — with their chunks, the entities extracted from them (sources) and every edge whose two endpoints are included; every row stamped doc_rank (shared entities rank with their most-recent document). | Four batched reads keyed on sources, never one per document; --max-docs / MAX_DOCS / max_docs override the cap per call. The Graph renderer shows the first query.full_graph_shown_docs (100) and reveals the rest with the Documents slider — hiding happens in the browser, no second read. Rows with no provenance that no included edge reaches are not part of it. Distinct from a query view (seeds + max_hops expansion) — before dynamic-graph-viz this view loaded everything, embeddings included. |`
  (format the inline code spans with backticks like the neighbouring rows).

`docs/adrs/011_live_force_layout_and_direct_manipulation.md`
- Context references: add tasks 165 and 166; glossary line gains "**Full graph** (added)".
- Context: append "The full-graph read loads every node and edge of the tenant, embeddings included, and a
  query view can leave a chunk floating when the last hop found its `next` edge but not its `part_of` one.
  Both are payload problems: the picture is only as good as what Python puts in it."
- Decision intro "Six related choices" → "Eight related choices" (task 166 adds §8). Append §7:
  "7. **The Full graph is capped and recency-ranked in Python; the browser hides, never re-fetches.**
  `fetch_full_graph` embeds the `query.full_graph_max_docs` (500) most-recent documents' subgraphs in four
  batched reads keyed on each row's `sources` provenance (documents → nodes → edges → unreached endpoints;
  `embedding` projected out) and stamps every row with `doc_rank`; `to_graph_payload` turns that into
  `docRank` + `controls.documents {shown: 100, total}`. The renderer's `Documents` slider sets `hidden` in
  Sigma and re-syncs `sim.nodes()` / `link.links()` so hidden nodes also leave the physics; pins survive
  hiding, selection does not. Ranking is Python-side on purpose: `properties.date` is an ISO string and
  `created_at` a BSON date, so Mongo would need `$dateFromString` and an in-memory sort anyway. Upgrade
  triggers: a tenant whose document rows alone scan slowly (~50k) → an aggregation; a measured slow
  `sources` scan → a `(user_id, sources)` index; a human asking to page beyond the cap → server-side paging."
- Diagram: in the `py` subgraph add `FG["fetch_full_graph<br/>4 reads keyed on sources · doc_rank"] --> GP`;
  in `js` add `DOCS["Documents slider<br/>hidden in Sigma · sim.nodes()/links re-synced"]` with
  `PANEL --> DOCS`, `DOCS --> SIM`, `DOCS --> G`; class them `pyNode` / `jsNode`.
- Consequences — append: "- **The full graph is no longer "everything".** Older documents beyond the cap are
  not embedded and rows with no provenance that nothing links to are absent; the summary line and header say
  so (`100 of 342 documents`). The dashboard's own template inherits the cap without a slider." and
  "- **Payload contract grows again, all surfaces at once:** `docRank`, `controls.documents`, `childCount`;
  tests pin them in both template variants."

Ready for implementation (after 164).

### [SWE] 2026-10-01 17:10 — Implementation

**Files modified**
- `apps/memory/src/tree/memory/graph/retrieval.py`:
  - `fetch_full_graph(..., *, max_docs=None)` does four batched reads: documents, then nodes by `sources`, then edges by `sources`, then unreached endpoints by `_id`.
  - Every filter leads with `user_id`, and every node read projects `{"embedding": 0}`.
  - Ranking is done in Python: `_as_utc` / `_document_recency` read `properties.date`, else `created_at`, else `datetime.min` in UTC. Order is newest first; ties go to `_id` ascending.
  - `_rank_by_provenance` implements the min rule.
  - Every returned row is a COPY stamped `doc_rank`.
  - One INFO line, and the docstring is rewritten.
- `apps/memory/src/tree/config/app_config.py` (`QueryConfig`): adds `full_graph_max_docs = Field(500, ge=1)` and `full_graph_shown_docs = Field(100, ge=1)`. The `_check_full_graph_caps` validator rejects shown > max.
- `apps/memory/configs/default.yaml` and `tests/unit/config/fixtures/frozen_config.yaml`: the two keys plus a two-line comment.
- `apps/memory/src/tree/memory/visualize/graph.py`, Python side:
  - `to_graph_payload` adds `docRank` only when the row carries `doc_rank`.
  - It adds `controls.documents = {shown: min(cfg, total), total: max(docRank)}` only when a node is ranked.
  - The `_render_graph_file` log line is `"{shown} of {total} documents shown by default, N nodes, M edges"`.
  - Module docstring updated.
- `apps/memory/src/tree/memory/visualize/graph.py`, `_RENDER_JS`:
  - `documents`, `docLimit` and `isVisible(id)` (an unranked node is always shown).
  - `visibleSimNodes()` and `visibleLinks()` (fresh `{source, target}` ids, self-loops excluded). The boot sim is `forceSimulation(visibleSimNodes())` with `forceLink(visibleLinks())`, and the tick writes back only `sim.nodes()`.
  - `graph.addNode` sets `hidden: !shown` and parks hidden nodes at the origin (see Notes).
  - `applyDocumentLimit(n)`:
    1. Prunes the selection and the hover / edge hover, hiding the tooltip.
    2. Calls `seedRevealed()` (see Notes), then `sim.nodes(...)` BEFORE `.links(...)`.
    3. Runs one `graph.updateEachNodeAttributes` pass (`hidden`, plus the d3 position if shown, or the origin if hidden).
    4. Calls `reheat()`, then `showDocumentCounts()`, then `renderer.refresh()`.
  - `showDocumentCounts()` sets the header `"{n} of {total} documents · N nodes · M edges"` and `dataset.docs = "n/total"`.
  - The `Documents` section is built FIRST, gated on `if (documents) {`.
  - `rangeRow` gains an optional `format` (the readout shows `"n of total"`).
  - `dragSetFor` never carries a node whose `"hidden"` attribute is set.
  - `seedRevealed()`: a node revealed for the FIRST time is seeded per document as a small spiral around a point one link distance outside the current layout (golden angle by `docRank`, deterministic).
  - `render()` deletes `body.dataset.docs` before rebuilding, so the iframe's second render (a query result after a Full graph) carries no stale marker.
- `apps/memory/scripts/query_graph.py`: `--max-docs` (`click.IntRange(min=1)`, default from config, `show_default`), forwarded only to `fetch_full_graph`; the usage docstring is updated. `apps/memory/Makefile`: `$(if $(MAX_DOCS),--max-docs "$(MAX_DOCS)",)`, and the help comment mentions `MAX_DOCS=N`.
- `apps/memory/src/tree/mcp/graph_tools.py` (`visualize_memory_graph`):
  - New argument `max_docs: int | None = None`. A value below 1 returns `invalid_input` / `retryable: false` before any read.
  - It is forwarded only on the no-query branch.
  - On the full graph the summary reads `"… {shown} of {total} most-recent documents shown by default, N nodes, M edges"`.
- `apps/memory/README.md`: the full-graph command, `MAX_DOCS=50`, and the cap/slider paragraph (both YAML keys).
- `docs/glossary.md` and `docs/adrs/011_…md`: the PA's text applied VERBATIM, including "Eight related choices" (166 adds §8 and must NOT bump it again), §7, the Context paragraph, the diagram nodes `FG` / `DOCS`, and both Consequences bullets.
- Tests:
  - `tests/unit/memory/conftest.py`: `$in` over a list field (ObjectId ≠ hex string, as in Mongo); `find(query, projection)` records `find_projections` and applies 0-projections; `make_document_row(created_at=, sources=)`.
  - `test_retrieval.py`: `TestFetchFullGraph`, 13 tests.
  - `test_app_config.py`: `TestFullGraphCaps`, 7 tests.
  - `test_graph.py`: 25 tests in a new section; the 162 guard token and the 164 readout pin were updated. The last two (`seedRevealed`, re-render marker) were written alongside their fix, after the advisor review, not red-first.
  - `test_viz_app.py`: 10 both-variant tokens; the guard token was updated.
  - `test_embeddings.py`: the map has no `documents` / `docRank`.
  - `test_query_graph.py`: 4 tests.
  - `test_graph_tools.py`: 6 tests.

**Tests**
- Unit: **4346 passed**, 0 failed (164 baseline 4268), via `make memory-tests`. The new tests were red first: `TypeError` on the missing `max_docs` kwarg / `AttributeError` on the missing config key, then `AssertionError` / `ValueError: substring not found` for the template, and click "no such option". None came from imports or fixture typos.
- Integration: N/A (no suite in this repo).

**Acceptance criteria**
- [x] Ranking, cap, and `doc_rank` on nodes AND edges, with the shared-entity min rule:
  - `test_retrieval.py::TestFetchFullGraph::test_keeps_the_most_recent_documents_ranked_from_one`, `::test_ranks_all_documents_when_the_cap_exceeds_them`, `::test_a_missing_date_falls_back_to_created_at_then_ranks_last`, `::test_returns_the_subgraph_of_kept_documents_only`
  - `::test_a_shared_entity_ranks_with_its_most_recent_document`, `::test_an_entity_whose_provenance_is_a_hex_string_still_belongs`, `::test_edges_carry_a_rank_and_need_both_endpoints`
- [x] Four finds for 1 AND 7 documents; `user_id` first; node reads projected; `$in` = the kept ids; config default; both-endpoints rule:
  - `::test_reads_in_four_batched_finds_whatever_the_document_count[1|7]`, `::test_no_returned_node_carries_an_embedding`, `::test_max_docs_defaults_to_the_configured_cap`, `::test_edges_carry_a_rank_and_need_both_endpoints`
- [x] Config, the per-call overrides, the `< 1` refusals, and no forwarding on a query:
  - `test_app_config.py::TestFullGraphCaps::*`
  - `test_query_graph.py::TestGraphragModeUnchanged::test_max_docs_*`
  - `test_graph_tools.py::test_visualize_forwards_max_docs_to_the_full_graph_read`, `::test_visualize_refuses_max_docs_below_one_before_any_read[0|-3]`, `::test_visualize_never_forwards_max_docs_with_a_query`
- [x] `docRank` / `controls.documents` only when ranked; none on the map or query payloads:
  - `test_graph.py::test_a_ranked_row_carries_doc_rank_and_an_unranked_one_does_not`, `::test_a_full_graph_payload_ships_the_documents_control`, `::test_shown_clamps_to_the_documents_that_exist`, `::test_a_query_payload_has_neither_doc_rank_nor_a_documents_control`
  - `test_embeddings.py::test_payload_ships_display_controls_but_no_forces`
- [x] Template, both variants: tokens, gate, re-sync, `dataset.docs`, no literal `100`:
  - `test_viz_app.py::test_both_variants_carry_the_embedding_map_extensions[*]`
  - `test_graph.py::test_template_carries_the_documents_slider[*]`, `::test_the_documents_section_is_gated_and_built_before_forces`, `::test_the_panel_code_contains_no_shown_default_of_its_own`, `::test_apply_document_limit_hides_prunes_resyncs_then_counts`
- [ ] Headless smoke: the Tester owns this. SWE pre-run is in Evidence (all green).
- [ ] CDP: the Tester owns this. SWE pre-run is in Evidence (all green).
- [ ] [HUMAN]: left for the human. Files and CDP pre-check are below.
- [x] README (`grep -c Documents` = 1, `MAX_DOCS` = 2 hits), glossary **Full graph** row, ADR-011 §7.
- [ ] Format / lint / pre-commit / tests are green, 4346 ≥ 4268. **`.tree/graphs/*.html` cleanup is DEFERRED**: the six files below are the [HUMAN] files; delete them after the human pass. The directory holds only these six.

**Evidence**
```
$ make env-status                                        -> Env target: local (.env)
$ make memory-format-check && make memory-lint-check && make pre-commit
  317 files already formatted / All checks passed! / prettier, ruff check, ruff format, biome: Passed
$ make memory-tests                                      -> 4346 passed in 72.10s
$ node --check on the module <script> of BOTH variants   -> iframe SYNTAX OK, file SYNTAX OK

# Read-only mongosh replica of the four reads (predicts the numbers before rendering):
docs 4 nodes 170 edges fetched 232 missing endpoints 1 kept edges 231     (all user nodes 170, edges 236)

# Files (162's read-only wrapper run_readonly.py; the make target still hits the shared-Mongo index conflict):
$ query_graph.py --no-open
  Full graph: embedded 4 of 4 documents (most recent first) → 170 nodes, 231 edges
  Wrote self-contained graph HTML (4 of 4 documents shown by default, 170 nodes, 231 edges) to …/graph-20261001-170002.html
$ TREE_QUERY__FULL_GRAPH_SHOWN_DOCS=2 query_graph.py --no-open -o .tree/graphs/full-graph-shown2.html
  Wrote self-contained graph HTML (2 of 4 documents shown by default, 170 nodes, 231 edges)
$ query_graph.py --max-docs 2 -o .tree/graphs/full-graph-max2.html
  Full graph: embedded 2 of 4 documents (most recent first) → 157 nodes, 229 edges   (header: 2 of 2 — shown clamps)
$ query_graph.py --query "langgraph agent memory" --max-docs 2   -> 78 nodes, 75 edges (cap ignored)
$ make memory-query-graph MAX_DOCS=0   -> Error: Invalid value for '--max-docs': 0 is not in the range x>=1. ; exit 2
$ synthetic_full_graph.py 5 "" 12   (real to_graph_payload + _render_graph_file, 12 docs × (1 doc + 3 parents + 12 children + 3 persons) + 3 shared orgs)
  Wrote self-contained graph HTML (5 of 12 documents shown by default, 231 nodes, 360 edges) … controls.documents={'shown': 5, 'total': 12}

# headless --dump-dom smoke (162's command; DOM markers, not script text)
graph-…170002        <body data-layout="live" data-docs="4/4" data-sim="running"> | 4 of 4 documents · 170 nodes · 231 edges | sections [Documents, Forces, Display, Actions], readout "4 of 4"
full-graph-shown2    data-docs="2/4" | 2 of 4 documents · 137 nodes · 192 edges | readout "2 of 4"
full-graph-max2      data-docs="2/2" | 2 of 2 documents · 157 nodes · 229 edges
synthetic-12-…170019 data-docs="5/12" | 5 of 12 documents · 98 nodes · 150 edges | readout "5 of 12"
langgraph-…170009    <body data-layout="live" data-sim="running"> (no data-docs) | 78 nodes · 75 edges | sections [Forces, Display, Actions]; >Documents< 0
embedding-map-…170012 <body data-layout="fixed"> | 86 chunks in 3 clusters (+6 noise) | sections [Display, Actions]; >Documents< 0, data-docs 0

# MCP tool function called directly against local Mongo (no server lifespan -> no index work):
{}            -> 'Knowledge graph for your full memory: 4 of 4 most-recent documents shown by default, 170 nodes, 231 edges (interactive graph view).'
{max_docs: 2} -> '… 2 of 2 most-recent documents shown by default, 157 nodes, 229 edges …'
{max_docs: 0} -> {"error_type":"invalid_input","retryable":false,"message":"max_docs must be ≥ 1"}

# CDP (real-time headless Chrome 1400x900, swiftshader; copies with window.__t + a first-tick probe; Runtime.exceptionThrown [] in EVERY run)
slider (graph-…170002): first tick sim.nodes 170 / links 231 / hidden 0
  -> 1 (input): "1 of 4 documents · 55 nodes · 78 edges", data-docs 1/4, hidden 115 = 170-55, sim.nodes 55, links 78,
     hidden-in-sim 0, links touching a hidden node 0, non-finite positions 0, data-sim "running", alpha 0.5 (reheated)
  -> 4: "4 of 4 documents · 170 nodes · 231 edges", hidden 0, sim.nodes 170, links 231, graph.order 170 (nothing lost)
  real keyboard on the focused slider: Home -> 1 of 4 (55 nodes), End -> 4 of 4 (170)
pinSurvives: drag doc "how-does-memory…" (docRank 2) +120/+60 -> pinned, dot (31,36,48,255)
  -> 1: hidden, fx/fy kept, graph pos parked [0,0], not in sim.nodes, overlay at old spot (0,0,0,0)
  -> 4: shown, fx/fy identical, graph x/y == fx/fy, back in sim, dot (31,36,48,255) at the same viewport spot (drift 0 px)
selectionPruned: Shift+click 3 facts (docRank > 1) -> 3 rings (234,88,12,255), 297 orange overlay px; hover one -> tooltip shown
  -> 1: selection 0, hovered null, tooltip hidden, 0 orange overlay px; Esc -> []; -> 4: selection stays [] (not re-selected)
pausedAndReset: Pause -> data-sim "paused"; -> 1: 55 nodes, sim.nodes 55, data-sim "paused", max d3 node move over 1.2 s = 0;
  -> 3: 143 nodes, still paused; Resume -> "running"; -> 2 then Reset to defaults -> "4 of 4" (shown of this file)
resetFromMax (full-graph-shown2): boot "2 of 4" -> 4 "4 of 4 · 170 · 231" -> Reset to defaults -> "2 of 4 documents · 137 nodes · 192 edges", readout "2 of 4", sim.nodes 137
marqueeSkipsHidden: at 1, Shift-box 840x530 px around the graph origin (where the 115 hidden nodes are parked) -> 48 selected, 0 hidden
synthetic (5 of 12): FIRST tick sim.nodes 98, links 150, hidden 133 (= 231-98), hidden-in-sim 0
  -> 12: 231 nodes / 360 edges, sim 231 / 360; -> 3: 60 nodes / 90 edges, sim 60; Reset -> "5 of 12 documents · 98 nodes · 150 edges"
queryView (langgraph-…170009): sections [Forces, Display, Actions], no data-docs, 0 ranked, 0 hidden, sim.nodes 78 = order
rerender (iframe case, window.__render): Full graph (data-docs 4/4, sections [Documents, …]) then a 2-node query payload
  -> data-docs absent, counts "2 nodes · 1 edges", sections [Forces, Display, Actions], sim.nodes 2
revealSettle (wait in real time for data-sim="settled", ~12-14 s on swiftshader): synthetic 5 -> 12 settles in 14.4 s with
  every revealed document a whole star outside the old ones (synth-settled-at-max.png); real corpus 2 -> 4 settles in 13.8 s,
  stars separate (shown2-settled-at-max.png). BEFORE seedRevealed the same 5 -> 12 left docs 6-12 interleaved at the centre.
scale (synthetic 500 docs, shown 100 = 1903 of 9503 nodes, 3000 of 15000 edges): first tick at 301 ms with sim.nodes 1903,
  hidden 7600; one synchronous apply 100 -> 500 = 27.5 ms, 500 -> 100 = 25.0 ms; a drag-like burst of 20 inputs: mean 23.5 ms,
  max 41.2 ms -> no coalescing needed. (Frame rate with all 9503 nodes live on swiftshader: 1.5 fps — the simulation + software
  WebGL at that size, not the slider; Pause is the existing escape hatch, ADR-011 Consequences.)
Screenshots (scratchpad/165 and 165/final): full-at-total.png, full-at-1.png, full-pin-revealed.png, synthetic-at-5.png, synthetic-at-3.png,
synth-settled-at-max.png, shown2-settled-at-max.png. No stray edges, dots or rings for hidden nodes in any of them.
The battery above was re-run on the FINAL template (files 1700xx) — logs in scratchpad/165/final/*.log — same numbers.
```

**Notes**
- **Spec deviation (data-shape bug, found by a read-only probe):** entity rows store `sources` as the hex STRING of the document id, while document, chunk and edge rows store the ObjectId.
  - Cause: `add_entity.py` takes `source_id: str` and `$setUnion`s it as is. Locally all 54 `sources` VALUES across the 50 entity rows are `string`, all 120 on document/chunk rows (and every edge's) are `objectId`.
  - Mongo's `$in` never matches one against the other, so the literal `{"sources": {"$in": list(provenance_rank)}}` would have dropped EVERY entity from step 2. Entities would then have arrived only through step 4, ranked by edges, which breaks the shared-entity rule.
  - The fix is read-side: the step-2 and step-3 `$in` lists hold each kept id in BOTH forms, and `provenance_rank` is keyed on `str(id)`. The four-find test pins "ids plus their string forms". The regression test (`test_an_entity_whose_provenance_is_a_hex_string_still_belongs`) was red before the fix.
  - **Suggested follow-up task:** make `add_entity` write `PydanticObjectId(source_id)` and migrate existing rows. After that, the dual-form `$in` can go. (`sharding.py`'s "already ingested" set also mixes the two types but still works, because chunk rows carry the ObjectId — not a bug today.)
- **Step 4 always runs** (an empty `$in` is one cheap round trip), so the count is exactly 4 whatever the data. Locally the 4th read hydrates 0 rows: the 1 missing endpoint does not exist and its edge is dropped.
- **Why the real full graph is 170 / 231, not 171 / 236 as in 162-164:**
  - 4 `same_as` edges carry no `sources`, so step 3 never reads them.
  - 1 edge points at a node id that does not exist. It is dropped, so its `unknown` endpoint no longer materialises.
  - This matches the mongosh replica above. It is the specified behaviour, not a regression.
- **Boot does not call `applyDocumentLimit`** (the spec says "called once on boot"). Its `reheat()` is `alpha(0.5)`, which would cut the first layout's alpha from d3's 1 to 0.5. Instead, the boot builds the graph and the simulation from the SAME `isVisible` predicate and calls the shared `showDocumentCounts()`. The intent is proven over CDP: the first tick holds 98 sim nodes with 133 hidden.
- **Beyond spec: hidden nodes are parked at the graph origin in Sigma.** Sigma 3.0.3's extent function iterates EVERY node with no hidden check, so the auto-fit would keep framing invisible stars. Its constructor also throws on a non-finite x/y, and a never-shown node has none. The true position stays on the d3 node (`x`/`fx`) and is restored on reveal; `pinSurvives` shows the pin returns to the exact spot with 0 px drift. The origin lies inside the visible extent because forceCenter keeps the centroid there.
- **Sigma `hidden` semantics, verified on the 3.0.3 bundle** (jsdelivr `+esm`, the same file the template loads). The edge program's `process` zeroes an edge when `s.hidden||a.hidden||o.hidden` (the edge or either extremity), and edge labels are skipped the same way. So no edge needs its own `hidden` flag. A hidden node is also not pickable, so it cannot be hovered, clicked or pressed.
- **d3-force re-sync (d3-force@3.0.0 source):** `sim.nodes(arr)` re-runs `initializeNodes` and every force's `initialize`. A node with NaN `x` would get a phyllotaxis spot by its index in the new array — near the centre, a document's nodes scattered around 360° through the stars already there. Measured: after a full settle, synthetic documents 6-12 stayed interleaved. So `seedRevealed()` (beyond spec, advisor-reviewed) places each first-time-revealed document as its own small spiral one link distance outside the layout before `sim.nodes()` runs; after it, every revealed document settles as a whole star (Evidence, revealSettle). Re-revealed nodes keep their last d3 position.
  - `forceLink.initialize` resolves only non-object endpoints, so a reused link would keep a hidden node object. That is why the links are rebuilt from ids every time.
- **Config escape hatch — a spec contradiction for the PA:** the validator refuses `shown > max`, so the spec's own example `TREE_QUERY__FULL_GRAPH_MAX_DOCS=50` ALONE fails `app_config` at IMPORT time (shown is still 100), which takes down every entry point (CLI, MCP server, workers). Lower `TREE_QUERY__FULL_GRAPH_SHOWN_DOCS` with it (pinned by `test_the_escape_hatch_lowers_both_caps`). The per-call `--max-docs` / `max_docs` are not affected: `shown` clamps to the total in the payload.
- `rangeRow`'s new optional `format` argument is the only change to 164's helpers. The 164 pin `readout.textContent = v.toFixed(decimals);` became `format = (v) => v.toFixed(decimals)` + `readout.textContent = format(v);`.
- **Not done:** the MCP stdio server was not booted. Its lifespan runs `ensure_indexes` on the shared Mongo, and it opens a browser. Instead I called the registered tool function directly with a mock ctx and a real read-only `AsyncMongoClient` (Evidence). The dashboard's own template is untouched; it inherits the cap through `fetch_full_graph`'s default.
- **[HUMAN] files** (under `apps/memory/.tree/graphs/`; the directory holds only these six, all rendered from the FINAL template):
  - `full-graph-shown2.html`: real corpus with `full_graph_shown_docs=2`. Loads at `2 of 4`. **This is the [HUMAN] AC's file.**
  - `synthetic-12-documents-20261001-170019.html`: 12 synthetic documents, shown 5, so the slider reveals 7 more stars.
  - `graph-20261001-170002.html`: real full graph, defaults (4 of 4).
  - `full-graph-max2.html`: `--max-docs 2` (2 of 2).
  - `langgraph-agent-memory-20261001-170009.html`: query view (no slider).
  - `embedding-map-20261001-170012.html`: synthetic map, hulls on (no slider).
  - Scale file (scratchpad, not for the human): `<scratchpad>/165/synthetic-500.html` (500 docs, shown 100).
- **CDP harness for the Tester:** `<scratchpad>/165/`.
  1. `python3 instrument.py <rendered.html> <copy.instr.html>` adds `window.__t` (+ `nodeById`) and a `window.__firstTick` probe.
  2. Start Chrome as in 163/164 (`--remote-debugging-port=9333 --window-size=1400,900`).
  3. Run `node cdp.mjs file://<copy> <slider|pinSurvives|selectionPruned|pausedAndReset|marqueeSkipsHidden|synthetic|resetFromMax|queryView|rerender|revealSettle|scale>`.
  - Logs and screenshots land in the cwd.
  - Synthetic generator: `uv run python <scratchpad>/165/synthetic_full_graph.py <shown> [out.html|""] [n_docs=12]` from `apps/memory` (with `.env` sourced).
  - MCP direct call: `<scratchpad>/165/mcp_direct.py`.
  - `<scratchpad>` = `/private/tmp/claude-501/-Users-pauliusztin-Documents-01-Projects-AI-Engineer-Handbook-building-agentic-systems-building-agentic-systems/944c4516-3945-43f4-8111-e0c95524323d/scratchpad`.
- No Mongo index was touched; every read was a `find` / `countDocuments` / `aggregate` (read-only).

### [SWE] 2026-10-01 17:20 — Addendum: clamp at use, no cross-key validator (orchestrator fix for deviation 5)

- `QueryConfig._check_full_graph_caps` is removed. Both keys stay `Field(..., ge=1)`. A comment explains why there is no cross-key check: a config error must never take down every entry point.
- `to_graph_payload` now computes `shown = min(full_graph_shown_docs, full_graph_max_docs, total)`. This is the only place in `src/` that reads the shown default (grep).
- The AC line is updated and marked "changed by orchestrator". The glossary, the ADR and the README never mentioned the validator, so no doc edit was needed.
- Test changes:
  - `test_showing_more_than_is_embedded_is_refused` became `test_showing_more_than_is_embedded_loads` (no raise).
  - New `test_the_escape_hatch_lowers_the_cap_alone`: `MAX_DOCS=50` only gives 50 / 100 loaded.
  - New `test_graph.py::test_shown_clamps_to_the_configured_embed_cap`: shown 100, max 50, total 60 gives `{shown: 50, total: 60}`.
- `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` is green: **4348 passed**.
- E2E: `TREE_QUERY__FULL_GRAPH_MAX_DOCS=2` ALONE now loads. The log reads "Full graph: embedded 2 of 4 documents … → 157 nodes, 229 edges", and the file reads "2 of 2 documents shown by default". Before this fix, that setting failed `app_config` at import.
- The [HUMAN] files under `.tree/graphs/` do not need re-rendering. Their payloads are unchanged: shown is already ≤ max in each one.

### [Tester] 2026-10-01 20:45 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check && make memory-lint-check && make pre-commit`, exit 0, env target local).
- Unit tests: `make memory-tests` run TWICE: 4348 passed / 0 failed (54.2 s, then 61.5 s).
- Integration tests: N/A (no suite in this repo).
- Warnings: 0 (no warnings summary in either log).

**E2E adversarial pass** (Chrome 154 headless on port 9444, own profile; files rendered into the scratchpad, `.tree/graphs` untouched; Mongo read-only via 162's `skip_indexes=True` wrapper plus `find`/`aggregate`)
- Happy path: `run_readonly.py scripts/query_graph.py --no-open` → `Full graph: embedded 4 of 4 documents (most recent first) → 170 nodes, 231 edges`; dump-dom: `<body data-layout="live" data-docs="4/4" data-sim="running">`, `<span id="counts">4 of 4 documents · 170 nodes · 231 edges`, `>Documents<` before `>Centre force<`. `--query` file: no `data-docs`, no `Documents`, no `docRank`. Map file: `data-layout="fixed"`, no `Documents`. PASS.
- Real-Mongo provenance (deviation 1): independent pymongo script (`qa165/real_check.py`) recomputes ranks from raw rows. Local entities are 100% hex-string `sources`, chunks/docs/edges ObjectId. For `max_docs` 1/2/3/4/500, every entity of an included doc is returned (16/16, 40/40, 43/43, 50/50) with the exact min rank (wrongrank = 0, 4 finds each). Entities of excluded docs returned: 0 at 2 and 3. At 1: 2 returned (see Other issues #1). PASS with note.
- Dropped rows (deviation 2): full graph 170/231 vs 171/236. Dropped edges are exactly 4 `same_as` with `sources: []` + 1 `related_to` (sources 3 docs) whose source node does not exist; no node dropped (170 of 170). Confirmed. PASS.
- Retrieval fuzz with my own fake (`qa165/adv_retrieval.py`): shared entity (sources ranked 1 and 3) stays visible with rank 1 at `max_docs=2` (hex and ObjectId forms), `only3` absent; garbage dates (`"not-a-date"`, int, `""`, None, no `properties`, BSON datetime, date-only, `Z`) never raise and rank by `created_at` then last, ties by `_id`; a document with no `sources` (and one with the key absent) is ranked and embedded alone; dangling / no-provenance edges dropped; self-loop kept; always 4 finds; no `unknown` node; two docs sharing `sources[0]` both rank. PASS. Note: `max_docs=-1` at library level silently drops only the oldest doc (Python negative slice); entry points guard < 1 (note only).
- CLI/MCP/config: `MAX_DOCS=0|-3|abc` → click "Invalid value for '--max-docs'", make error, no read. `--max-docs 99999999999999999999` OK. `TREE_QUERY__FULL_GRAPH_MAX_DOCS=2` ALONE → embeds 2, header `2 of 2 documents shown by default` (clamp). `TREE_QUERY__FULL_GRAPH_SHOWN_DOCS=2` → `2 of 4`, 170 nodes. `--query ... --max-docs 2` → 78/75, cap ignored. MCP tool called directly: `{}` → "4 of 4 most-recent documents shown by default, 170 nodes, 231 edges"; `{max_docs:2}` → "2 of 2 …157/229"; `{max_docs:0}`, `-5`, and `{max_docs:0, query:"x"}` → `invalid_input`, `retryable:false`, no read. PASS.
- SWE's CDP battery re-run independently on a fresh render (slider, pinSurvives, selectionPruned, pausedAndReset, marqueeSkipsHidden): slider 1 → `1 of 4 documents · 55 nodes · 78 edges`, hidden 115 = 170-55, sim.nodes 55, links 78, `data-docs 1/4`, running, alpha 0.5; back to 4 restores 170/231; Home/End keys work; pin survives hide/reveal with 0 px drift and dot at the same spot; selection/hover pruned, no re-select; paused slider applies with 0 node movement and `data-sim="paused"`; marquee over parked origin selects 0 hidden. Synthetic 12 docs/shown 5: first tick sim.nodes 98, hidden 133, hiddenInSim 0; →12 (231/360), →3 (60/90). Runtime exceptions `[]` in every run. PASS.
- Break path (rapid, state edge): 300 synchronous flips 1↔max in one task, then 60 CDP-paced flips incl. random values, then 20 real Home/End key bursts, then min, max. Invariants after each (script `INV`): hidden attr == (docRank > limit) for every node, hidden-in-sim 0, sim.nodes == visible, links == visible non-loop edges, no bad link, no non-finite, counts text matches; ends `settled`. PASS.
- Break path (slider during an active drag): press a rank-2 document star, slider → 1 mid-drag (hides held doc + carried chunks), keep dragging, slider → 4 mid-drag, release. No exception, invariants hold, one node pinned, alphaTarget 0, doc lands at the pointer. PASS (minor note: while hidden, the 4 drag members still write their graph x/y, so they are not parked at the origin until the next slider apply; Fit afterwards still frames the visible 55 correctly, 0.51/0.92 of the viewport).
- Break path (hide a node mid-drag, release while hidden): members 4 → 1 after release (carried released, held doc pinned), alphaTarget 0; reveal: pinned doc exactly at its drop spot. PASS.
- Break path (slider mid-marquee): Shift+marquee started, slider → 1 mid-box, release: selection 47, 0 hidden; reveal ok. PASS.
- Break path (Unpin all with a hidden pinned node, then reveal): fx cleared on the hidden node (pinned count 0), still hidden; reveal → fx null, finite position, invariants hold. PASS.
- Break path (Reset to defaults after slider moves while paused, `shown=2` file): Pause, 4→1→3, Reset → `2 of 4`, readout `2 of 4`, `data-sim="paused"`, alpha unchanged (0.245), 0 node movement, buttons `Resume/Unpin all/Reset to defaults`. PASS.
- Break path (marquee over the origin parking spot): at slider 1, 24x24 px box at the origin: 9 visible nodes within 25 px, 3 selected, 0 hidden; plain click and hover at origin hit nothing hidden. PASS.
- Break path (zoom-fit with hidden nodes): slider 1 + Fit → 55 visible, 0 offscreen, bbox 0.51 x 0.92 of the viewport (visible only); drag (freezes viewport) then slider 4 → 13 nodes offscreen until Fit (inherent to 163's frozen camera, see notes), Fit → 0 offscreen; back to 1 + Fit → 0.51 x 0.92. PASS.
- Break path (fresh render of a query view after a Full graph, iframe path via `__render` in the same page; `_RENDER_JS` is one constant shared by both variants): Full (`4/4`, sections Documents/Forces/Display/Actions, 8 sliders) → query payload: `data-docs` absent, no Documents section, 7 sliders, 2 sim nodes → ranked payload (shown 1 of 2): `1 of 2 documents`, slider present → unranked: marker gone → empty: marker gone, "No graph data returned." PASS.
- Break path (real mouse on the Documents slider): press on thumb, drag left with vertical wander, drag right, release → `4/3/2/1/…/4` seen live, invariants hold, camera unchanged, selection empty, wheel over the slider does not zoom the stage. PASS.
- 163/164 regressions on both a no-hidden file and the `shown=2` file (33 hidden nodes): landing/group drag (rigidMaxDev 1.3 px), select, unselectedDoc, userPin, marquee (camera frozen, selected == nodes in box), unpin (double-click), rightButton, actions/collapse/escMarquee/lostMouseup/resetKeepsPins, sliderExtremes, pauseOrdering, rapidPause, collapseWhileRunning, panelPassThrough, escFocus, resetWhileDragging: all values as in their original QA, exceptions `[]`. One red on `fastClicks.dragTrailingClickAsSecond` in the `shown=2` file was a harness artefact (it picks a hidden entity from the full node list); re-run on the no-hidden file passes (`pinned:true, selectionUnchanged:true`).

**Acceptance criteria**
- [ ] FAIL — `fetch_full_graph` returns the `max_docs` most-recent documents' subgraphs (ranking, `doc_rank`, shared-entity min rule themselves PASS: `TestFetchFullGraph` 13 tests, real-Mongo recompute wrongrank=0, my fuzz).
      Expected: with `max_docs=k`, exactly k document rows and nothing owned only by excluded documents.
      Actual (real local corpus, `real_check.py`): `max_docs=1` returns 2 document rows (`ai-agent-memory` rank 1 and `context-engineering`, date-rank 4, stamped `doc_rank 1`) + 22 extra nodes (20 chunks, that document star, 2 entities); `=2` returns 3 document rows; `=3` returns 4. Consequently the CLI log "embedded 1 of 4", the header "1 of 1 documents shown by default" and the MCP summary "2 of 2 most-recent documents" are false (3 stars drawn for 2), and `--max-docs 1` draws 77 nodes / 2 stars while the slider at 1 on a full embed draws 55 nodes / 1 star (77 - 22 = 55: the slider cut is clean, the embed cap is not).
      Cause: `retrieval.py` step 4 hydrates every endpoint of an included edge with the edge's rank. The edges `…parent-0 -[part_of]-> context-engineering doc`, `child-N -[part_of|next]-> …` and `doc -[mentions]-> person:self` carry `sources: [ai_src (rank 1), ctx_src]`, so step 3 keeps them and step 4 pulls in the nodes owned by the excluded document (`sources: [ctx_src]` only). The spec's step-4 examples are a hand-added `sources: []` entity and a `referenced` target, not rows owned by an excluded document.
      Fix: after the step-4 find, skip a hydrated row whose `sources` is non-empty and has no element in `provenance_rank` (compare with `str(s)`: entity sources are hex strings); the existing both-endpoints filter then drops those edges. Still exactly 4 finds. Add a regression test: doc A newest, doc B older, B's chunk edge carries `[A_src, B_src]`; with `max_docs=1` no B node and no B edge are returned, a `sources: []` entity reached by A's edge is still hydrated, `len(find_filters) == 4`. Re-run `real_check.py` at k=1..3: expect `extra=0` and exactly k document rows; then re-render `full-graph-max2.html` (currently 3 stars under "2 of 2") — do not delete the [HUMAN] files before the human pass.
- [x] PASS — four finds, `user_id` first, `{"embedding": 0}`, both-endpoints edges — `test_reads_in_four_batched_finds_whatever_the_document_count[1|7]` passes; `calls 4` in every adversarial case; real run: dropped edges all lack an endpoint or provenance.
- [x] PASS — config keys/CLI/MCP — `TestFullGraphCaps` (7), `test_query_graph.py::*max_docs*`, `test_graph_tools.py::*max_docs*` pass; e2e: env max alone clamps, `--max-docs 0` exit 2, MCP `invalid_input`.
- [x] PASS — payload `docRank` / `controls.documents` gating — `test_graph.py` payload tests, `test_embeddings.py`; e2e: query/map files contain 0 `docRank`, full file 170, `"documents": {"shown": 4, "total": 4}`.
- [x] PASS — both template variants: tokens, gate, re-sync, `dataset.docs`, no literal `100` — `test_viz_app.py` + `test_graph.py` pass; regex over the rendered JS finds no standalone `100`.
- [x] PASS — Headless smoke — see happy path.
- [x] PASS — CDP — see the battery and break paths.
- [ ] [HUMAN] — awaiting human verification (skipped as instructed).
- [x] PASS — README / glossary **Full graph** / ADR-011 §7 — present in `git diff --stat` and contain the named terms (`Documents`, `MAX_DOCS`, "Full graph", "Eight related choices", §7).
- [ ] OPEN — format/lint/pre-commit/tests green and 4348 ≥ 4268 (PASS); `.tree/graphs/*.html` cleanup is deliberately NOT done (the human is viewing the six files; orchestrator forbade deleting them). Delete after the human pass.

**Evidence**
```
$ make memory-tests (x2)         -> 4348 passed in 54.23s / 4348 passed in 61.49s
$ real_check.py (read-only)      -> max_docs=1: nodes 77 edges 115 | =2: 157/229 | =3: 163/231 | =4,500: 170/231
                                    entities should-include/returned: 16/16, 40/40, 43/43, 50/50; wrongrank 0
                                    dropped (500): 4x same_as sources=[], 1x related_to (source node missing)
$ INV after every slider op      -> wrongHidden 0, simMismatch 0, badLinks 0, nonFinite 0, countsMatch true, exceptions []
```

**Other issues found** (none blocks)
1. PROMOTED TO FAIL (see AC1 above). Facts only: the local `part_of`/`next` edges of the context-engineering document carry `sources: [ai_src, ctx_src]` (57 `part_of` + 52 `next` + 2 `mentions` edges are `ai+ctx`); I did not establish why.
2. `fetch_full_graph(max_docs=-1)` (library level) keeps all but the oldest document; `0` returns empty. Entry points refuse < 1, so unreachable today; a one-line guard would make the function safe on its own.
3. Mid-drag slider change leaves the drag members' graph x/y off the origin while hidden (cosmetic, self-heals on the next apply; Fit still correct).
4. After any drag/marquee (viewport frozen, 163), revealing documents places new stars off-screen until Fit is pressed (13 of 170 nodes in my run). Consistent with ADR-011 §4 but worth a [HUMAN] eyeball; not in this task's scope.
5. `TREE_QUERY__FULL_GRAPH_MAX_DOCS=0` fails at import with a pydantic error "input_value=False, input_type=bool" (the env-override parser turns `0` into a boolean). A clear error at boot, pre-existing mechanism, only mentioned for completeness.
6. MCP server not booted (its lifespan runs `ensure_indexes` on the shared Mongo); the registered tool function was called directly, and the iframe variant was exercised through the shared `_RENDER_JS` (`__render`) rather than the ext-apps bridge.

**VERDICT: FAIL** (1 issue for the SWE: AC1, step-4 hydration leaks excluded-document rows; everything else PASS)

**Note for the PA (not blocking):** the 4 `same_as` edges with `sources: []` are dropped even though both endpoints are on the graph (step 3 reads by `sources`); a visible change from 164's 171/236.

### [SWE] 2026-10-01 21:05 — Fixes for QA FAIL (AC1) + two orchestrator additions

**1. AC1 blocker: step 4 leaked rows owned only by excluded documents**
- Fix (`retrieval.py`): after the step-4 find, a hydrated row is skipped when its `sources` is non-empty and none of it is in `provenance_rank`. The check is `str(s)`-safe, because entity sources are hex strings. The both-endpoints rule then drops the leaking edges. Still exactly 4 finds.
- Regression test, red first: `test_an_edge_shared_with_an_excluded_document_never_pulls_it_in`.
  - Setup: doc A is newer, doc B older; B's `part_of` and `mentions` edges carry `[A_src, B_src]`.
  - With `max_docs=1`: no B node and no B edge are returned, a `sources: []` entity reached by an A edge is still hydrated, and `len(find_filters) == 4`.
- The 162-era test `test_edges_carry_a_rank_and_need_both_endpoints` had pinned the leak itself (`e-beyond`, owned only by the excluded `d3`, was hydrated through an edge). It now expects `e-beyond` and its edge to be absent.
- The Tester's `real_check.py` on the local corpus, after the fix:
  ```
  max_docs=1: nodes 55 edges 79  missing=0 extra=0 wrongrank=0 | doc rows: [ai-agent-memory]
  max_docs=2: nodes 137 edges 195 missing=0 extra=0 wrongrank=0 | doc rows: 2
  max_docs=3: nodes 143 edges 197 missing=0 extra=0 wrongrank=0 | doc rows: 3
  max_docs=4/500: nodes 170 edges 235 extra=0 | dropped edges: only the related_to whose source node does not exist
  ```
  - Excluded-doc entities returned: 0 at every k.
  - The "edge rank mismatch" counts the script prints (1/3/3/4) are the `same_as` edges. The script expects them to have no rank; they now get one under fix 2.
- The embed cap and the slider now agree exactly:
  - `--max-docs k` gives 55/79, 137/195 and 143/197 for k = 1, 2, 3.
  - The slider at k on the full embed gives the same numbers (CDP `slider`, `pausedAndReset` re-runs).
- `full-graph-max2.html` was overwritten in place. It now reads `2 of 2 documents · 137 nodes · 195 edges` and holds 2 document rows; before, it drew 3 stars.

**2. No-provenance edges between included nodes are kept (added by orchestrator)**
- How it works:
  - Step 3's filter is now `{"user_id", "kind": "edge", "$or": [{"sources": {"$in": …}}, {"sources": []}]}`.
  - A no-provenance edge is kept only when both endpoints are in the node set, and it ranks as the min of its endpoints' ranks.
  - It never feeds step-4 hydration.
- Test changes:
  - New `test_a_no_provenance_edge_joins_included_nodes_only`. It covers a kept edge between included nodes, an edge to an excluded document's entity, an edge to an orphan, and an edge to a missing node; it also asserts 4 finds.
  - The four-find test now pins the `$or` shape.
  - The fake collection gained `$or`.
- Real corpus: the full graph is back to **235 edges** (231 + 4 `same_as`); the dangling `related_to` stays dropped.
- Docs: the task's Scope steps 3 and 4 and the AC1 wording are updated and marked "added by orchestrator after QA". The glossary **Full graph** Notes gained one sentence on both rules.

**3. Revealed stars land on screen with a frozen camera (added by orchestrator)**
- `seedRevealed()` now has two modes:
  - If `renderer.getCustomBBox()` is set (a gesture froze the camera), revealed documents are seeded on a ring halfway to the edge of the CURRENT viewport's graph-space bounds, one small spiral per document, angle set by `docRank`. A re-revealed node whose last position is off screen is re-seeded there too.
  - Otherwise it uses the outside-the-layout ring, as before.
- In both modes: Pinned nodes keep their spot, the camera stays frozen (no `setCustomBBox` in the function), and nothing is random.
- Tests: `test_with_a_frozen_camera_revealed_stars_land_inside_the_viewport`, plus the updated seeding test.
- CDP (`frozenReveal`: real drag to freeze, then reveal, then wait in real time for `data-sim="settled"`):
  ```
  full-graph-shown2 (2 -> 4, 33 never-placed nodes): offscreen at reveal 0, after settle (15.9 s) 0; camera unchanged, still frozen
  graph-…170002     (1 -> 4, 115 re-revealed nodes): offscreen at reveal 0, after settle (15.6 s) 7-10
  ```
  - **The 1→4 residue is physics, not seeding.** All 10 off-screen nodes are degree-0 entities: facts, objects and organizations with no edges. Two of them are rank-1 nodes that were never hidden.
  - Repel pushes isolated nodes out to an equilibrium radius that grows with the node count. A viewport frozen around 55 nodes cannot hold the 170-node equilibrium.
  - Every document star and every connected node stays on screen (screenshot `scratchpad/165/fix/full-frozen-reveal.png`).
  - Reaching literally 0 would need either a containment force, which changes ADR-011 §1's three forces, or giving up "keep the camera frozen". **That is a PA/orchestrator decision; I did not pick one.**
- With the camera auto-fitting (no gesture), the synthetic 5→12 reveal still settles as whole stars outside the old ones (`revealSettle` re-run).

**Re-run (final tree)**
```
$ make memory-format-check && make memory-lint-check && make pre-commit   -> clean (317 files, ruff, prettier, biome Passed)
$ make memory-tests                                                      -> 4351 passed in 59.38s
$ node --check both variants                                             -> iframe SYNTAX OK, file SYNTAX OK
dump-dom (all six [HUMAN] files re-rendered IN PLACE, same names):
  graph-20261001-170002          data-docs="4/4"  4 of 4 documents · 170 nodes · 235 edges
  full-graph-shown2              data-docs="2/4"  2 of 4 documents · 137 nodes · 195 edges   (4 doc rows embedded)
  full-graph-max2                data-docs="2/2"  2 of 2 documents · 137 nodes · 195 edges   (2 doc rows)
  synthetic-12-documents-…170019 data-docs="5/12" 5 of 12 documents · 98 nodes · 150 edges
  langgraph-agent-memory-…170009 no data-docs     78 nodes · 75 edges, >Documents< 0
  embedding-map-…170012          data-layout="fixed", >Documents< 0
CDP regressions on the new files: slider (1 -> 55/79, back 170/235, Home/End), pausedAndReset (0 px while paused,
  Reset -> 4 of 4), synthetic (first tick 98 sim nodes / 133 hidden), revealSettle: all as before, exceptions [] in every run.
```
Logs: `scratchpad/165/fix/*.log`. No Mongo index was touched; all reads were read-only.

### [Tester] 2026-10-01 22:00 — QA (re-review after the 21:05 fix round)

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check && make memory-lint-check && make pre-commit`, exit 0, env target local).
- Unit tests: `make memory-tests` run TWICE: 4351 passed / 0 failed (55.7 s, then 55.3 s).
- Integration tests: N/A (no suite in this repo).
- Warnings: 0 (no pytest warnings summary; only the pre-existing opik/pydantic import UserWarning line).
- Mutation: with the step-4 guard removed (`if False:`), `test_an_edge_shared_with_an_excluded_document_never_pulls_it_in` AND the rewritten `test_edges_carry_a_rank_and_need_both_endpoints` both go red (2 failed / 22 passed, no `-x`); `retrieval.py` restored byte-identical (`cmp`).
- The changed 162-era test is correct, not weakened: `e-beyond` is owned ONLY by the excluded `d3`, so hydrating it through `to-beyond` is exactly the leak. The test now also asserts `d3` absent, and edge ranks `{part1:1, part2:2, cross:1}` are unchanged.

**Real-Mongo checks** (read-only pymongo + `find` only; `scratchpad/qa165/real_check.py`, `same_as_check.py`, `finds.py`)
- `real_check.py` at k = 1/2/3/4/500: nodes/edges 55/79, 137/195, 143/197, 170/235, 170/235; `missing=0 extra=0 wrongrank=0` every k; document rows returned exactly k (`ai-agent-memory`; +`how-does-memory…`; +`example.com`; all 4); entities of excluded documents returned: 0 at every k; dangling edges 0. At 500 the only dropped edge is the `related_to` whose source node does not exist. The script's "edge rank mismatch 1/3/3/4" counts are the `same_as` edges (the script still expects no rank for them).
- Slider == embed: slider at k on the full embed gives 55/79, 137/195, 143/197, 170/235 (CDP `slider`, `pausedAndReset`, `rapid`), identical to `--max-docs k`.
- `same_as` rule (`same_as_check.py`): 4 no-provenance `same_as` edges in the DB (endpoint ranks 4/2, 1/2, 1/1, 1/2); returned at k=1/2/3/4: 1/3/3/4 = exactly the ones with BOTH endpoints rank <= k, each ranked `min(endpoint ranks)`, both endpoints in the node set.
- 4 finds per call (pymongo CommandListener): documents(`user_id,kind,type`) -> nodes(`user_id,kind,sources`) -> edges(`user_id,kind,$or`) -> endpoints(`user_id,kind,_id`); projections `{"embedding": 0}` on the 3 node finds. The `getMore` commands in the trace are cursor continuations of those four finds, not extra queries.

**E2E adversarial pass** (Chrome 154 headless :9444, own profile; copies of the six `.tree/graphs` files instrumented in `scratchpad/qa165b/f`; the human's files untouched)
- Happy path / smoke (`smoke.mjs` over the six re-rendered files, exceptions `[]` each):
  `graph-…170002` `data-docs=4/4`, `4 of 4 documents · 170 nodes · 235 edges`, Documents section present, 4 of 4 stars visible; `full-graph-shown2` `2/4`, `2 of 4 documents · 137 nodes · 195 edges`, 2 of 4 stars visible; `full-graph-max2` `2/2`, `2 of 2 documents · 137 nodes · 195 edges`, **2 stars** (file grep: `"documents": {"shown": 2, "total": 2}`, 2 `"type": "document"` rows); `synthetic-12` `5/12`, `98 nodes · 150 edges`; `langgraph-agent-memory` no `data-docs`, no Documents section, `78 nodes · 75 edges`; `embedding-map` `data-layout=fixed`, no Documents. PASS. (`--dump-dom` hung on the live simulation; CDP used instead.)
- Slider/gesture battery on the new no-hidden file, all invariants (hidden attr == docRank > limit, hiddenInSim 0, sim.nodes == visible, links == visible non-loop edges, no bad link/non-finite, counts text matches): slider (1 -> 55/79; back 170/235; Home/End), pinSurvives (0 px drift), selectionPruned, pausedAndReset (0 px while paused; Reset -> 4 of 4), marqueeSkipsHidden (0 hidden selected), rapid (300 sync + paced + key bursts), dragThenSlider, dragHideReleaseHidden, marqueeThenSlider, unpinHidden, marqueeOrigin, realSlider (real mouse), dragHideThenFit, rerenderFull (Full -> query -> Full -> empty in one page), synthetic (first tick 98 sim nodes / 133 hidden / 0 hiddenInSim; 12 -> 231/360; 3 -> 60/90; Reset -> 5 of 12), revealSettle, resetPausedShown2 (paused Reset keeps `2 of 4`, 0 px), queryView (no Documents). Runtime exceptions `[]` in every run. PASS.
- 163 regressions on the new files (landing rigidMaxDev 0.1 px, select, group, unselectedDoc, userPin, marquee, unpin, rightButton, map) and 164 regressions (display, actions, collapse, **fastClicks on the no-hidden file** all values `ratioAfter 1`, `pinned:true, selectionUnchanged:true`, escMarquee, lostMouseup, resetKeepsPins, mapPanel; adv: pauseOrdering, rapidPause, resetWhileDragging, panelPassThrough, escFocus, collapseWhileRunning, mapAdv): all as in their original QA, exceptions `[]`. PASS.
- Harness notes, NOT product findings: 164 `adv-rerender` never ran (it calls `__render(__DATA)`; my 165 instrumentation does not define `__DATA`; `rerenderFull` above is the re-render coverage). 164 `sliderExtremes` is flaky: in 3 of 10 runs `isolatedEntities()` returns no candidate (id `undefined`), and in 2 completed runs the click on a 1.2 px target selected 0; it was clean in the rest and there are no hidden nodes in that file.

**Break path: frozen camera + reveal (orchestrator item 3) — FAIL**
- Setup (`frozenReveal`): slider to its default, real drag of a document star (this freezes the camera, `getCustomBBox() != null`), slider to `total`, wait for `data-sim="settled"` (5 s), count visible nodes outside the stage, repeated twice (reproduces identically).
  - `graph-…170002` (1 -> 4): at reveal 0 off; after settle 7 off, all edge-less (docsOff 0, connectedOff 0). Within the accepted decision. Second hide/reveal cycle: 14 off, of which 2 connected (see note 3).
  - `full-graph-shown2` (2 -> 4): after settle 6 off, all edge-less; cycle 2: 11 off, all edge-less. Within the accepted decision.
  - **`synthetic-12` (5 -> 12): at reveal 0 off; after settle 20 off with `docsOff 1, connectedOff 20, isolatedOff 0` (star `doc-03` and its chunks); cycle 2: `docsOff 2`, `connectedOff 51`.** Same numbers on both runs. Screenshot `scratchpad/qa165b/frozenRevealFit-before-synthetic-12-documents-20261001-170019.png` shows doc-03 and doc-05 missing from the stage.
- Cause (measured, not guessed): seeding works (0 off at reveal; at 0 ms of cycle 2 the 21 off are stars of ranks <= 5 that were NEVER hidden and were already pushed out in cycle 1). It is the equilibrium: repel pushes a 98 -> 231 node layout wider than the viewport the camera froze around 98 nodes. The orchestrator accepted this only for edge-less isolated entities; here document stars and connected clusters leave the screen.

**Orchestrator premise check: "Fit recovers them" is false once the simulation has settled**
- `fitSettle` / `fitStale` (real file 1 -> 4 and synthetic 5 -> 12, settled, then the Fit button): still 10 off (real) / 38 off (synthetic), `bboxFracH 1.15`, and a second Fit click changes nothing. A manual `renderer.refresh()` right after brings both to 0 off (`bboxFracH 0.92`). Cause: the handler at `graph.py:794` is `renderer.setCustomBBox(null); camera.animatedReset();` and with no ticks running nothing re-indexes Sigma's extent, so `getBBox()` is stale.
- Pre-existing, not a 165 regression: `fitStale163` reproduces it on the `langgraph-agent-memory` QUERY view (drag to freeze, Link distance to max, settle, Fit: 4 off, `refresh()`: 0) and `git diff` shows the `zoom-fit` line unchanged by this task. The 20:45 QA saw Fit give 0 off only because the sim was still running when Fit was pressed; the new seeding settles it sooner.
- Reset to defaults does recover (it re-hides back to 5).

**Acceptance criteria**
- [x] PASS — `fetch_full_graph` AC1 (incl. "added by orchestrator after QA" wording): exactly k document rows, nothing owned only by excluded documents, no-provenance edges kept only between included nodes — `real_check.py` k=1..4/500, `same_as_check.py`, `TestFetchFullGraph` (incl. the two mutation-checked tests), 4 finds via CommandListener.
- [x] PASS — four finds / `user_id` first / `{"embedding": 0}` / both-endpoints edges; config keys / CLI / MCP; payload gating; template tokens and no literal `100`; headless smoke; CDP; README / glossary **Full graph** / ADR-011 §7 (re-checked in `git diff --stat`; the glossary Notes carry both orchestrator rules).
- [ ] [HUMAN] — awaiting human verification (skipped as instructed).
- [ ] OPEN — format/lint/pre-commit/tests green and 4351 >= 4268 (PASS); `.tree/graphs/*.html` cleanup deliberately NOT done (human is viewing them).

**Evidence**
```
$ make memory-tests (x2)   -> 4351 passed in 55.66s / 4351 passed in 55.32s
$ real_check.py            -> k=1: 55/79, k=2: 137/195, k=3: 143/197, k=4/500: 170/235; extra=0 missing=0 wrongrank=0
$ frozenReveal synthetic   -> afterSettle off 20 (docsOff 1, connectedOff 20); cycle2 off 51 (docsOff 2)   [x2 runs]
$ fitStale (settled)       -> Fit: 10 off; refresh(): 0 off     fitStale163 (query view): Fit 4 off; refresh() 0 off
```

**Other issues found**
1. FAIL item (for the SWE / PA): a frozen-camera reveal of a large set (synthetic 5 -> 12) leaves a document star and 20 connected nodes off the stage after settle; Fit does not recover it (item 2). Smallest change that makes the orchestrator's accepted recovery true: call `renderer.refresh()` in the `zoom-fit` handler (`graph.py:794`) between `setCustomBBox(null)` and `animatedReset()`, plus a CDP/test pin (settle, Fit, assert 0 off). Whether stars may leave the stage at all on reveal (containment force vs unfreezing the camera on a reveal vs accepting it with a working Fit) is the PA/orchestrator decision the SWE already flagged; with a working Fit the user needs one click.
2. Note: in cycle 2 on the 4-document file two CONNECTED nodes stay off (`object:ai agent memory`, `object:memory in agent systems`: connected only to each other, one is hidden at slider 1 so it is edge-less while hidden and drifts). Same cause as the accepted isolated drift; the orchestrator may or may not count them as "isolated".
3. Note: `fetch_full_graph(max_docs=-1)` at library level (carried from the 20:45 QA) still keeps all but the oldest document; entry points refuse < 1.
4. Note: a document row beyond the cap whose own `sources` happen to contain a kept provenance id would be returned by step 2 (and stamped with a kept rank). Not reachable with current writers (a document's `sources[0]` is its own id), unit-untested.
5. Carried, unchanged: MCP server not booted (its lifespan would run `ensure_indexes` on the shared Mongo); registered tool function was exercised directly in the 20:45 QA and the touched code path (`fetch_full_graph`) is unchanged since.

**VERDICT: FAIL** (1 issue: frozen-camera reveal of a large set leaves document stars / connected nodes off screen and the Fit button cannot recover them after settle; everything else PASS, AC1 now PASS)

### [SWE] 2026-10-01 21:40 — Fix: zoom-fit fits a stale extent once the sim has settled (orchestrator, after re-QA)

**The bug (from 163).** The fit handler ran `renderer.setCustomBBox(null); camera.animatedReset();`. Once the simulation has settled, nothing ticks, so Sigma never re-indexes the extent. Fit then animated to the stale frozen box and left nodes off screen.

**The fix (`graph.py`).** The handler is now `{ renderer.setCustomBBox(null); renderer.refresh(); camera.animatedReset(); }`, with a two-line comment. `setCustomBBox(null)` still appears exactly once.

**Tests**
- `test_graph.py::test_the_viewport_stays_frozen_after_a_drop_until_fit` now pins the new line.
- New `test_fit_re_indexes_the_extent_before_resetting_the_camera` pins the order `setCustomBBox(null)` < `refresh()` < `animatedReset()`.
- `test_viz_app.py` gains a both-variant token.
- All three were red before the one-line fix and green after.

**Checks**
- `make memory-format-check && make memory-lint-check && make pre-commit`: clean.
- `make memory-tests`: **4354 passed**.
- `node --check` passes on both variants.
- The AC line was added and marked "added by orchestrator after QA".

**CDP `fitAfterReveal`.** The run:
1. Waits for the layout to settle.
2. Freezes the camera with a real drag.
3. Reveals to max (query view: no slider).
4. Waits for `data-sim="settled"` again.
5. Clicks the real ⊡ button.
6. Waits 1.2 s and counts VISIBLE nodes off screen.
```
synthetic-12-documents (5 -> 12): before Fit 46 of 231 off screen -> after Fit 0 of 231, customBBox null
langgraph query view:             before Fit  2 of 78            -> after Fit 0 of 78
graph-…170002 (1 -> 4 is not needed; drag then 4 of 4): before Fit 11 of 170 -> after Fit 0 of 170
CONTROL, same synthetic page with the refresh() removed: 46 -> 61 off screen after Fit (the bug)
exceptions [] in every run
```
Logs and screenshots are in `scratchpad/165/fit/`.

**Files.** All six `.tree/graphs` files were re-rendered IN PLACE (same names), and each carries the fixed handler:
- `graph-20261001-170002` (4/4, 170/235)
- `full-graph-shown2` (2 of 4, 137/195 shown)
- `full-graph-max2` (2/2, 137/195)
- `synthetic-12-documents-20261001-170019` (5/12)
- `langgraph-agent-memory-20261001-170009` (78/75)
- `embedding-map-20261001-170012`

### [SWE] 2026-10-01 22:30 — Gravity force (added by orchestrator — human decision)

**Implementation**
- `graph.py`:
  - `_DEFAULT_FORCES["gravity"] = 0.05`, so the payload stays the single source of defaults.
  - One `import { …, forceX, forceY }` line in both variants. I verified `forceX` and `forceY` against the `d3-force@3.0.0/+esm` export list.
  - Inside the `if (!isFixed)` branch only: `.force("gravityX", forceX(0).strength(forces.gravity))` and `.force("gravityY", forceY(0).strength(forces.gravity))`. The map never builds them.
  - Panel row `Gravity`, placed after `Centre force`: range 0–0.5, step 0.01. Its apply sets BOTH strengths and then calls `reheat()`. Because it is a `rangeRow`, Reset restores it.
- Tests:
  - The controls defaults dict.
  - Both-variant tokens `"Gravity"`, `forceX(` and `forceY(`.
  - The forces-gated `reheat(); }` count goes from 4 to 5.
  - The per-slider apply parametrization.
  - The payload-default reference `forces.gravity, 2,`.
  - New `test_gravity_is_one_forcex_forcey_pair_on_the_live_layout_only`.
  - New `test_the_gravity_row_sits_after_centre_force_and_resets_like_any_row`.
  - `0.05` cannot serve as a "no literal" probe, because it is also Node size's step; the test comment says so.
- Docs:
  - ADR-011 Context: the export list now includes `forceX/forceY`.
  - ADR-011 §1: three forces become four, with the MEASURED rationale (see below).
  - ADR-011 diagram: the `SIM` label gains gravity.
  - README: the panel sentence names gravity.
  - Glossary: no edit. It does not enumerate the forces.
- The AC line was added and marked "added by orchestrator — human decision".

**Tuning: the target was NOT met, by any value, and higher is worse**

Method: real-time CDP, same as the re-QA, on instrumented copies with `controls.forces.gravity` patched.
- Freeze via a real drag, reveal to max, wait for `data-sim="settled"`, then count VISIBLE nodes off screen by kind.
- For the real full graph: slide to 1 first, then freeze, then 1 → 4.

| gravity | synthetic 5→12 off-screen (doc stars / connected / edge-less) | real 1→4 off-screen (doc / connected / edge-less) | default extent, real (w×h) | default extent, synthetic at 5 |
|---|---|---|---|---|
| 0 (= 164 forces) | 2 / 46–47 / 0 | 0 / 0 / 10 | 4511 × 4716 | 2304 × 2428 |
| **0.05 (shipped)** | 2 / 49–50 / 0 | 0 / 21–22 / 7 | **1669 × 1745** | **1191 × 1182** |
| 0.1 | 3 / 50 / 0 | 0 / 39 / 10 | 1186 × 1232 | 901 × 884 |

Overlapping node pairs (< 6 graph units) are 0 at every value.

**Why gravity cannot meet the target (measured, not argued).** Gravity contracts the PRE-reveal layout, and with it the frozen box, as much as it contracts the revealed one. The ratio of post-reveal extent to the frozen box stays at about 1.6–1.8:
```
synthetic g=0:    frozen box 2304×2428 -> after reveal 4063×3786  ratio 1.76
synthetic g=0.05: frozen box 1191×1182 -> after reveal 1957×1866  ratio 1.64
real      g=0:    frozen box 3696×3710 -> after reveal 4984×4757  ratio 1.35  (edge-less sprawl at 1 doc made the box big)
real      g=0.05: frozen box  949× 973 -> after reveal 1719×1663  ratio 1.81
```
- I changed the rationale I was handed ("keeps reveals inside a frozen viewport") to what the data shows. The comments in `_DEFAULT_FORCES` and the JS, and ADR §1, say gravity gives a compact layout and keeps edge-less nodes near the stars. They also state that gravity does not keep a reveal inside a frozen viewport, and that Fit recovers it.
- Options for literally 0 off screen, NOT implemented because each would be a new ADR-011 §1 decision:
  - a containment force toward the frozen viewport;
  - gravity scaled by node count;
  - re-fitting the camera on reveal (drops "keep the camera frozen").
- **Question for the human / PA:** keep the default at 0.05, or ship 0? The data rules out higher.
  - For 0.05: the default view is about 2.7× more compact and readable, with edge-less entities pulled in. Screenshots: `scratchpad/165/tune/tune-full-g0.05-extent.png` vs `tune-full-g0-extent.png`, and `tune-synth-g0.05-extent.png` vs `tune-synth-g0-extent.png`.
  - Against 0.05: in the frozen 1→4 reveal on the real graph, 21–22 connected nodes end off screen, against 0 at g=0.

**Verification (final tree, gravity 0.05)**
```
$ make memory-format-check && make memory-lint-check && make pre-commit   -> clean
$ make memory-tests                                                      -> 4364 passed in 59.24s
$ node --check both variants                                             -> SYNTAX OK x2
dump-dom, six files re-rendered IN PLACE: >Gravity< = 1 in each of the 5 graph files, 0 in the map; counts unchanged
  (4 of 4 · 170 · 235 | 2 of 4 · 137 · 195 | 2 of 2 · 137 · 195 | 5 of 12 · 98 · 150 | 78 · 75 | map 86 chunks)
CDP gravityRow: boot readout "0.05", forceX/forceY strength 0.05/0.05, range 0..0.5 step 0.01;
  -> 0.3: both strengths 0.3, reheated (alpha 0.489, running); Reset to defaults -> "0.05", both 0.05
CDP fitAfterReveal with gravity on: synthetic 49 off -> Fit -> 0 of 231; real full 0 -> 0 of 170; query view 0 -> 0 of 78
exceptions [] in every run
```
Logs and screenshots: `scratchpad/165/tune/` and `scratchpad/165/grav/`.

### [SWE] 2026-10-02 00:20 — Auto-fit on reveal; Gravity stays 0.05 (added by orchestrator — human decision)

**Implementation (`graph.py`, `_RENDER_JS`)**
- `fitView()` is now the ONE place that calls `setCustomBBox(null)`: `setCustomBBox(null)`, then `refresh()`, then `return camera.animatedReset()` (sigma 3 returns a Promise when no callback is given). The Fit button calls it.
- `autoFit()`:
  - Remembers `wasFrozen = getCustomBBox() != null`.
  - Runs `fitView().then(() => { if (wasFrozen) freezeViewport(); })`, so a camera a gesture had frozen is frozen again on the new extent and later drags still never refit.
  - Writes no position or pin.
- `applyDocumentLimit(n)`: `const revealing = n > docLimit` is read before `docLimit = n`. At the end, `if (revealing) { if (sim && !paused) fitOnSettle = true; else autoFit(); }`, so a paused (or simulation-less) reveal fits at once. Hiding never arms a fit.
- `sim.on("end")`: `if (fitOnSettle) { fitOnSettle = false; autoFit(); }`, so the fit runs once.
- Gesture cancel: in `mousemovebody`, the first threshold-crossing move of ANY drag or marquee sets `fitOnSettle = false`.
- Gravity comments: the `_DEFAULT_FORCES` comment now says gravity is for compactness and that the reveal auto-fit (not gravity) handles reveals in a frozen view.

**Docs**
- ADR-011 §4: the Fit path now names `refresh()`, and the exception is recorded: one auto-fit after a Documents reveal, with re-freeze, no fit on hide, drag/marquee cancels, camera-only.
- ADR-011 §7: one clause, "a reveal fits the camera once after it settles (§4)".
- ADR-011 §1: the gravity rationale now points at the reveal auto-fit as what recovers reveals.
- README: the slider sentence mentions the one fit after a reveal.
- AC line added, marked "added by orchestrator — human decision".

**Tests** (`test_graph.py` + `test_viz_app.py`)
- 4 new pins:
  - `test_a_reveal_arms_one_auto_fit_and_hiding_never_fits`
  - `test_the_armed_fit_runs_once_when_the_layout_settles`
  - `test_the_auto_fit_moves_only_the_camera_and_re_freezes_it`
  - `test_a_drag_or_marquee_cancels_an_armed_auto_fit`
- The two Fit pins now target `fitView()`; `setCustomBBox(null)` still appears exactly once.
- Both-variant tokens: `function fitView()`, `function autoFit()`, and the end-handler line.

**Verification (final tree)**
```
$ make memory-format-check && make memory-lint-check && make pre-commit   -> clean
$ make memory-tests                                                      -> 4372 passed in 57.23s
$ node --check both variants                                             -> SYNTAX OK x2
six .tree/graphs files re-rendered IN PLACE; dump-dom unchanged (4/4·170·235 | 2/4·137·195 | 2/2·137·195 |
  5/12·98·150 | 78·75 | map), >Gravity< 1 per graph file / 0 on the map, `function autoFit()` in every file
CDP (real drag to freeze, real-time settle, 1.5 s for the fit animation; exceptions [] in every run):
  autoFitReveal synthetic 5->12: after settle+fit 0 of 231 visible off screen; camera re-frozen (new bbox);
    node positions + pins identical before/after the fit; then hide 12->5: camera AND bbox unchanged;
    then a new drag: camera and bbox unchanged (no refit)
  autoFitReveal real full graph 1->4: 0 of 170 off screen; re-frozen; positions untouched; hide 4->1: unchanged;
    later drag: unchanged
  dragCancelsAutoFit synthetic: reveal 5->12, drag 0.8 s later during the settle -> after settle+1.5 s the
    camera and bbox are unchanged (no auto-fit; 53 of 231 off screen, as expected without a fit)
  pausedRevealFits synthetic: Pause, reveal 5->12 -> fits at once: 0 of 231 off screen, data-sim stays "paused", re-frozen
```
Logs and screenshots: `scratchpad/165/autofit/`.

### [Tester] 2026-10-02 01:10 — QA (third pass: Fit fix, Gravity, auto-fit on reveal)

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check && make memory-lint-check && make pre-commit`, exit 0; env target local).
- Unit tests: `make memory-tests` run TWICE: 4372 passed / 0 failed (67.5 s, then 56.1 s). 4372 >= 4268 (the pre-task count).
- Integration tests: N/A (no suite in this repo).
- Warnings: 0 (the only "warning" text is the pre-existing opik/pydantic-v1 import UserWarning line).

**Real-Mongo checks** (read-only pymongo + `find` only; `qa165/real_check.py`, `same_as_check.py`, `finds.py`; `retrieval.py` unchanged since the last pass)
- k = 1/2/3/4/500: nodes/edges 55/79, 137/195, 143/197, 170/235, 170/235; `missing=0 extra=0 wrongrank=0` at every k; document rows returned = k (1, 2, 3, 4, 4); excluded-document entities returned 0; dangling edges 0. The "edge rank mismatch" counts (1/3/3/4) are the `same_as` edges the script still expects unranked.
- `same_as`: 4 no-provenance edges in the DB; returned 1/3/3/4 at k=1..4 = exactly those with both endpoints rank <= k, endpoints in the node set (all True).
- Finds: 4 logical finds per call (documents `user_id,kind,type` -> nodes `user_id,kind,sources` -> edges `user_id,kind,$or` -> endpoints `user_id,kind,_id`), node reads projected `{"embedding": 0}`; the extra `getMore` entries are cursor continuations.

**E2E adversarial pass** (Chrome 154 headless on :9444, own profile; INSTRUMENTED COPIES of the six `.tree/graphs` files in `scratchpad/qa165c/f`; the human's files and port 8765 untouched; real CDP mouse/keyboard input; scenarios in `scratchpad/qa165c/scenarios.mjs`)
- Headless smoke, six files, exceptions `[]` each: `graph-…170002` `4/4`, `4 of 4 documents · 170 nodes · 235 edges`, Documents section; `full-graph-shown2` `2/4`, `137/195`; `full-graph-max2` `2/2`, `137/195`; `synthetic-12` `5/12`, `98 nodes · 150 edges`; `langgraph-agent-memory` no `data-docs`, no Documents, `78 nodes · 75 edges`; `embedding-map` `fixed`, no Documents, no sim. PASS. (The `noData:true` in the smoke output is the harness regex matching the template's own JS text, not a rendered message.)
- Frozen reveal -> auto-fit (`autoFitReveal`; real drag freezes the camera, reveal, wait `data-sim="settled"` (4.5 s), +1.8 s for the animation), counting visible nodes off the stage:
  - synthetic 5 -> 12 (231 nodes): 0 off at the reveal (seeding), after the fit 0 off, `fits` (calls of `setCustomBBox(null)`) = 1, frozen box re-set to the new extent `[-552,649,-586,622] -> [-927,1016,-923,939]`. PASS.
  - real full 1 -> 4 (170 nodes): after the fit 0 off, 1 fit, box `[-444,508,-500,468] -> [-883,829,-873,862]`. PASS. real 2 -> 4 (`full-graph-shown2`): 0 off, 1 fit. PASS.
  - hide 12 -> 5 / 4 -> 1 / 4 -> 2 afterwards: camera box byte-identical, no fit (`hideCamUnchanged` true x3). PASS.
  - second reveal: one more fit (fits 2); a later real drag of a document: box unchanged, no refit. PASS (after that drag the 4-doc file shows 1 edge-less node off screen, the accepted drag case).
- Drag during the settle (`dragCancels`, drag 0.6 s after the reveal): fits 0, box identical before and after the settle, `camUnchanged` true (synthetic 50 off incl. 2 document stars; real 29 off, 21 of them connected, because the user's gesture cancelled the fit and the frozen box stayed small). Behaves as specified; Fit recovers.
- Paused reveal (`pausedReveal`): Pause, reveal -> immediate fit (fits 1), `data-sim="paused"` before and after, 0 off, box re-frozen (synthetic and real). PASS.
- Fit after settle (`fitAfterSettle`): real 1 -> 4: 0 off before and after the Fit click (box null after); synthetic 5 -> 12: 0 / 0; query view `langgraph` (frozen by a drag, Link distance to max, settled): **6 off before Fit -> 0 off after Fit** (the old stale-bbox bug fixed). PASS.
- Break path 1 (rapid toggling while settling, `toggleStorm`: 40 alternating reveal/hide at 5-60 ms, ends on reveal): exactly 1 fit (no fit storm), 0 off, no non-finite coordinates, `sim` settled; second storm ending on hide: 1 further fit, ends sane (0 off). PASS (see note 3).
- Break path 2 (reveal then immediately Pause, `revealThenPause`): no fit while paused (fits 0, 0 off because seeding places the new stars in view), Resume -> settles -> exactly 1 fit -> 0 off. PASS (note 1).
- Break path 3 (reveal then immediately Reset to defaults, `revealThenReset`): Reset hides back to `5 of 12` / `4 of 4`, 1 fit after settle, 0 off, no exceptions. PASS (note 3).
- Break path 4 (Gravity extremes on synthetic, real full graph and the query view): 0 -> readout `0.00`, both `gravityX/gravityY` strengths 0; 0.5 -> `0.50`, strengths 0.5, layout extent 417 / 539 / 318 graph units; settles in 4.5 s; 0 non-finite coordinates, 0 off after Fit, screenshot `gravity-0.5-graph-….png` is dense but readable (labels legible); Reset -> `0.05`. PASS.
- Break path 5 (map): no `Gravity` / `Documents` rows (labels `Node size, Link thickness, Label fade`), `sim` null, no `data-docs`, layout fixed; nothing can arm a fit. PASS.
- Break path 6 (Shift-marquee while a fit is armed, with and without Esc mid-gesture, `marqueeArmed`): fits 0 in both, boxes unchanged. PASS. Plain Esc and a sub-threshold Shift-click while armed do NOT cancel the fit (fits 1, 0 off) (`escOnly`). PASS. A plain unshifted pan is NOT a canceller: run-verified, note 2.
- 163 regressions on the new files (landing rigidMaxDev 0.1 px, select, group, unselectedDoc, userPin, unpin, rightButton, map): all as before, exceptions `[]`. `marquee` reports `matchesNodesInBox: false` by exactly one node swapped at the box edge (the harness' expected set is float-compared; the two nodes are 0.32 px and 0.01 px from the box edge); deterministic, in code this task does not touch, not a regression.
- 164 regressions (display, actions, collapse, fastClicks (all ratioAfter 1), escMarquee, lostMouseup, resetKeepsPins, mapPanel; adv: pauseOrdering, rapidPause, collapseWhileRunning, panelPassThrough, escFocus, resetWhileDragging, mapAdv): same values as the original QA, exceptions `[]`.

**Acceptance criteria** (every non-`[HUMAN]` line re-verified)
- [x] PASS — `fetch_full_graph` ranking / cap / doc_rank / no-provenance edges — `real_check.py` k=1..4/500, `same_as_check.py`, `TestFetchFullGraph` in the 4372 green.
- [x] PASS — four finds / `user_id` first / `{"embedding": 0}` / both-endpoints edges — `finds.py`, unit tests.
- [x] PASS — config keys, `--max-docs` / `MAX_DOCS` / `max_docs`, `< 1` refused, clamp at use — unit tests green.
- [x] PASS — `docRank` / `controls.documents` gating — unit tests; the smoke above (no `data-docs` on the query and map files).
- [x] PASS — template tokens (`applyDocumentLimit`, `sim.nodes(`, `dataset.docs`, no literal 100) — unit tests; file grep.
- [x] PASS — headless smoke on the six files (above).
- [x] PASS — CDP slider / hide / reheat / pin / selection / paused (the 21:05-round battery was passed last time; this tree re-ran `autoFitReveal`, `pausedReveal`, `toggleStorm`, `revealThenReset`, 163/164 sets, all green).
- [x] PASS — zoom-fit re-indexes before reset: Fit after settle 0 off (real, synthetic), query view 6 -> 0 off.
- [x] PASS — Gravity: `forceX(`/`forceY(`/`"Gravity"` in all six files (`grep`), readout and both strengths follow the slider, 0 and 0.5 extremes finite, Reset restores 0.05, absent on the map; ADR-011 §1 names it.
- [x] PASS — auto-fit on reveal: armed on `n > prev` only, runs once at the settle (immediately when paused), re-freezes the box, hiding never fits, a drag/marquee cancels, only the camera moves (box changes, `fits` counters above).
- [ ] [HUMAN] — awaiting human verification (skipped as instructed).
- [x] PASS — README (`Documents` count 1, `MAX_DOCS` present, now also gravity and the one fit after a reveal), glossary **Full graph**, ADR-011 §7.
- [ ] OPEN — format/lint/pre-commit/tests green (PASS, 4372); `.tree/graphs/*.html` cleanup deliberately NOT done (human is viewing them).

**Evidence**
```
$ make memory-tests (x2)  -> 4372 passed in 67.47s / 4372 passed in 56.05s
$ autoFitReveal synthetic 5->12 : afterAutoFit off 0, fits 1, bbox [-552,649,-586,622] -> [-927,1016,-923,939]; hide: bbox unchanged
$ autoFitReveal real 1->4       : afterAutoFit off 0, fits 1, bbox [-444,508,-500,468] -> [-883,829,-873,862]; hide: bbox unchanged
$ dragCancels                   : fits 0, bbox unchanged through the settle
$ pausedReveal                  : fits 1, sim "paused", off 0
$ fitAfterSettle langgraph      : beforeFit off 6 -> afterFit off 0
$ toggleStorm (40 toggles)      : fitCalls 1, final off 0
$ dragCancelsThenFit            : after settle off 50 (synthetic) / 29 (real) -> Fit click -> 0 / 0
$ pausedResumeThenFit           : after Resume+settle off 47 / 17 -> Fit click -> 0 / 0
$ revealThenHide GAP 400/1500   : off right after hide 25/29 (synthetic), 7/11 (real) -> 0 after settle, fits 1
$ panDuringSettle               : camera [0.33,0.6275] after pan -> [0.5,0.5] after the fit (pan lost), 0 off
```

**Other issues found (none blocks; design questions / behaviour for the orchestrator)**
1. **Pause -> reveal -> Resume drifts off screen (main design question).** Pause THEN reveal fits at once on the SEEDED layout (spec: "immediate if paused", verified); after Resume the layout expands past the re-frozen box and nothing re-arms a fit (`pausedResumeThenFit`: synthetic 47 off incl. 2 document stars, real 17 off after the settle; the Fit button recovers both: 47 -> 0, 17 -> 0). It reintroduces the drift the auto-fit exists to fix, e.g. by re-arming the fit on Resume. Conversely, reveal then Pause leaves the fit armed (no fit while paused, 0 off thanks to seeding) and it fires once after Resume's settle.
2. **A plain stage pan during the settle does not cancel the armed fit; the pan is lost (run-verified, `panDuringSettle` synthetic).** An unshifted stage drag moved the camera to `[0.33, 0.6275]`; the fit at the settle then reset it to `[0.5, 0.5]` (box `[-552,649,-586,622] -> [-941,1016,-923,940]`, 0 off). Spec-compliant (only drag/marquee cancel), but design question: should a pan also cancel?
3. **A reveal followed by a hide before the settle still fits once at the settle (measured, `revealThenHide`; behaviour, not a bug).** Freeze, reveal, hide back after GAP ms, wait for the settle: GAP 400 ms: synthetic 5 -> 12 -> 5: 25 off right after the hide, 0 off after the settle, `fits` 1, box `[-552,649,-586,622] -> [-554,627,-610,649]`; real 1 -> 4 -> 1: 7 off right after the hide, 0 after, `fits` 1, box `[-444,507,-500,468] -> [-425,480,-496,493]`. GAP 1500 ms: synthetic 29 off -> 0, real 11 off -> 0, `fits` 1 each. The reveal pushed the remaining nodes outward before the hide, so the one armed fit is what brings them back; disarming on a hide would leave 25-29 nodes off screen. A plain hide with no unsettled reveal never moves the camera (box identical, x3), which is what "hiding never fits" asserts. The same applies to Reset to defaults after a reveal (`revealThenReset`: one fit after the settle, 0 off).
4. Carried: `fetch_full_graph(max_docs=-1)` at library level keeps all but the oldest (entry points refuse < 1); a document row beyond the cap whose own `sources` contain a kept provenance id would be returned (not reachable with current writers); MCP server not booted (lifespan would `ensure_indexes` on the shared Mongo).

**VERDICT: PASS**
