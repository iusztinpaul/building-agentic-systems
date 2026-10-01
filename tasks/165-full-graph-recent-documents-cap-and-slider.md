---
id: 165-full-graph-recent-documents-cap-and-slider
status: pending
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
   (`part_of`, `next`, `referenced`, `mentions`, `related_to`, `same_as`, … of those documents).
   Edge `doc_rank` by the same `min` rule.
4. **Endpoints not yet loaded.** Collect `source_node_id`/`target_node_id` of step-3 edges that are
   not in the node set (e.g. a hand-added entity with `sources: []`, a `referenced` target) and
   hydrate them with ONE `find({"user_id": user_id, "kind": "node", "_id": {"$in": missing}}, {"embedding": 0})`;
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

- [ ] `fetch_full_graph` returns the `max_docs` most-recent documents' subgraphs: documents ranked by
      `properties.date` (ISO string, tz-aware), else `created_at`, else last; `doc_rank` 1-based on every
      node AND edge row; a shared entity carries the rank of its most-recent document (unit tests).
- [ ] Exactly four `find` calls regardless of document count, every filter `user_id` first, every node
      read projected `{"embedding": 0}`; only edges with both endpoints included are returned (unit tests).
- [ ] `query.full_graph_max_docs` (500) and `query.full_graph_shown_docs` (100) load from YAML with
      `shown ≤ max` enforced; `--max-docs` / `MAX_DOCS` / `max_docs` override the cap per call; `< 1` is
      refused (click exit 2 / `invalid_input` envelope); the cap is never forwarded on a query (unit tests).
- [ ] `to_graph_payload` emits `docRank` only for ranked rows and `controls.documents {shown, total}` only
      when a rank exists; the map payload and query payloads carry neither (unit tests).
- [ ] Both template variants contain the `Documents` slider gated on `controls.documents`,
      `function applyDocumentLimit`, `sim.nodes(` / `.links(` re-sync, `dataset.docs`, and no literal `100` (unit tests).
- [ ] Headless smoke (Tester, Log): full-graph dump prints `data-docs="4/4"`, `4 of 4 documents · N nodes · M edges`
      and the `Documents` label; `--query` and map dumps contain neither.
- [ ] CDP (Tester, Log): slider to 1 hides nodes in sigma AND drops them from `sim.nodes()`/links, reheats,
      updates counts and `data-docs`; back to 4 restores all; a pinned hidden node is still pinned on reveal;
      hidden selected nodes leave the selection; while paused the slider applies without restarting the sim.
- [ ] [HUMAN] On a graph with more documents than the default (patched `full_graph_shown_docs=2` on the local
      corpus, or a 12-document synthetic file): load shows `2 of 4 documents`; dragging the slider right adds
      whole document stars that settle in; dragging left removes them and the rest re-settles; nothing is
      drawn for hidden nodes (no stray edges, dots or rings); `Reset to defaults` returns to `2 of 4`.
- [ ] README updated (`grep -c "Documents" apps/memory/README.md` ≥ 1, mentions `MAX_DOCS`); glossary
      **Full graph** row present; ADR-011 Decision 7 present.
- [ ] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green;
      test count ≥ today's; `.tree/graphs/*.html` cleaned up.

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
