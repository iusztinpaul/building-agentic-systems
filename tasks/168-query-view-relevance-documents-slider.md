---
id: 168-query-view-relevance-documents-slider
status: pending
feature: dynamic-graph-viz
---

# Query views get the Documents slider, ranked by search relevance

Tags: `viz`, `memory`, `mcp`, `docs`
Depends on: 165, 166, 167
Blocks: —
Implements: ADR-011 (§7, amended by this grooming round)

## Scope

Task 165 gave the **Full graph** a `Documents` slider (recency rank, 100 of N shown) and scoped
query views out. The human wants the same slider on the normal query view (`make
memory-query-graph QUERY=…`, `visualize_memory_graph(query=…)`, `search_memory(visualize=True)`),
ranked by **search relevance**: every document of the result shows by default (a query result is
`top_k` seeds wide, so there is nothing to cap) and sliding LEFT keeps only the best-matching
documents' stars. The Full graph keeps 165's recency ranking. Python ranks, stamps the SAME
`doc_rank` key 165 uses, and the renderer's existing `applyDocumentLimit` / auto-fit / `Reset`
code is reused untouched — the only JS change is reading `controls.documents.order` for the row
label. The **Embedding map** and the NL `query_memory` tool are untouched (no rank → no slider).
`rag` mode has no graph surface (text only) and stays that way.

Why the hit order and not `_search_score`: `expand_graph` re-hydrates every row, so no score
survives on `QueryResult` rows; the seed list `query_memory` already holds is the one relevance
signal. Why `doc_rank` is stripped from model text: it is a viz concern; `search_memory`'s JSON and
`deep_search_memory`'s files must stay byte-identical to 166.

Grooming-round doc edits this task OWNS (apply them in this task's commit): ADR-011 §7 amendment,
Context paragraph + references, diagram labels `EX` / `DOCS`, Consequences bullets; glossary
**Graph payload**, **Graph renderer**, **Full graph** rows; README paragraph; Makefile comment.
Exact text: see `## Log` → `[PA] Grooming`.

### Retrieval (`retrieval.py::query_memory` — ONE pure helper, no extra read)
After `expand_graph` returns (both the hop path and `max_hops == 0`), rank the result's documents
from the `hits` `hybrid_search` returned (best RRF score first; hit rows are full Mongo rows with
`sources`):

1. **Candidates** = the `type == "document"` rows IN THE RESULT (a star that is drawn). A hit maps
   to a candidate when `set(hit.doc.get("sources") or []) & set(candidate.get("sources") or [])`
   is non-empty — a child seed (replaced by its parent in the result), a parent, an entity (its
   `sources` are the documents it was extracted from) and a document seed all map the same way.
   Use EVERY element of a document row's `sources`, not `[0]` (167 found rows with a latent stub
   id beside the real one).
2. **Order**: candidates with a hit by their best hit score descending, ties on `str(_id)`
   ascending; candidates no hit maps to (pulled in by expansion / the `part_of` closure) AFTER
   all of them, by `str(_id)`. `doc_rank` = 1-based position. Every document star in the view is
   ranked, so `total` = the stars drawn and the slider at 1 leaves exactly one star.
3. **Stamp** `doc_rank` on a COPY of every NODE row that maps to a ranked document:
   `provenance_rank = {source: rank}` over each ranked document's `sources` (keep the smaller rank
   on overlap) and reuse 165's `_rank_by_provenance(row, provenance_rank)` — an entity shared by
   two documents ranks with the more relevant one; the document row gets its own rank. A node with
   no ranked provenance (`sources: []`, a row owned only by a document that is not in the result,
   an `unknown` endpoint) gets NO stamp — unranked, always shown, as in 165. Edges are NOT
   stamped: the payload reads no edge rank and the renderer draws an edge iff both endpoints are
   visible.
4. No hits / no document row in the result → nothing stamped (no slider). `QueryResult`'s shape,
   `expand_graph`, `_seed_ids`, the `$graphLookup` pipeline, the closure and `ranked_rows` are
   unchanged. One DEBUG line: `"Relevance rank: %d document(s), %d of %d node rows ranked"`.

### Model-facing text — unchanged (`mcp/graph_tools.py::_serialize`, `mcp/deep_search.py` ~l.231)
Both `cleaned = {k: v … if k != "embedding"}` comprehensions also drop `doc_rank` (one shared
tuple of viz-only keys or two literals — SWE decides; a comment names task 168). `ranked_rows`
and the `max_results` truncation are untouched, so `search_memory`'s output and the deep-search
index/files are byte-identical to 166 for the same rows. (`child_count` already reaches the model
today; leave it.)

### Payload (`visualize/graph.py::to_graph_payload(result, *, document_order: Literal["recency", "relevance"] = "recency")`)
- `docRank` on a node iff the row carries `doc_rank` (unchanged).
- When at least one node is ranked: `controls["documents"] = {"shown", "total", "order"}` with
  `order = document_order`; `shown` = 165's `min(full_graph_shown_docs, full_graph_max_docs, total)`
  for `"recency"`, and `= total` for `"relevance"`. No ranked node → no `documents` key (so the
  map and NL-query payloads are as before; 165 payloads simply gain `"order": "recency"`).
- Callers pass the order explicitly (Python decides): `visualize_memory_graph` → `"relevance"` on
  the query branch, `"recency"` on the full graph; `_dual_graph_result` → `"relevance"` (its rows
  come from `search_memory` or an NL pipeline — the latter has no rank, so no control);
  `visualize_query_result(result, …, query=…)` → `"relevance" if query else "recency"`;
  `dashboard_app._fetch_payload` → the same rule (its own template ignores `docRank`; the default
  must still be right). Pin each with a test. No new config key.
- Summaries: `visualize_memory_graph`'s model-visible line keeps 165's full-graph wording and reads
  `f"Knowledge graph for {label}: {shown} of {total} most-relevant documents shown by default, {n} nodes, {m} edges"`
  on a query with a `documents` control (adjective by `order`: `most-recent` / `most-relevant`);
  unchanged (`"{n} nodes, {m} edges"`) with no control. `_render_graph_file`'s log line is already
  order-neutral — unchanged. `_dual_graph_result`'s `"Graph of these results: …"` tail is unchanged.
  Update `visualize_memory_graph`'s docstring: with a `query` the view's `Documents` slider ranks by
  relevance and shows every document by default.

### Renderer (`_RENDER_JS`; both variants token-identical)
- The gate stays `if (documents) {`; the section stays `panelSection("Documents")`; the ROW LABEL
  becomes `ORDER_LABEL[documents.order]` with `const ORDER_LABEL = { recency: "Most recent", relevance: "Most relevant" };`
  declared beside `const documents = …` — the one JS change. Readout `"{n} of {total}"`, header
  `"{n} of {total} documents · N nodes · M edges"`, `data-docs="n/total"`, `applyDocumentLimit`,
  seeding, auto-fit-on-reveal (165/166), `Reset to defaults` → `shown` (= `total` on a query view),
  gesture guards: all unchanged and now exercised on query views too. No literal `100`.

### CLI / Makefile
No new flag. `--max-docs` stays full-graph-only (help text unchanged). The `query-graph` Makefile
comment gains "; a QUERY view's slider ranks documents by relevance".

### Tests (`/squid-testing-python`)
- `tests/unit/memory/graph/test_retrieval.py` (`TestQueryMemoryComposition`, real fake collection,
  `hybrid_search` patched with `HybridSearchResult(hits=…)`; rows sharing explicit ObjectId `sources`):
  - two documents A, B with parents and `part_of` edges; hits: child of B (score 0.9), child of A
    (0.5) → `doc_rank` B=1, A=2 on the document rows AND their chunks; an entity with
    `sources=[A, B]` carries 1; a document C pulled in only by expansion (no hit) ranks 3; a node
    with `sources: []` carries no `doc_rank`; edges carry none.
  - tie: equal scores → `str(_id)` order; a hit whose `sources` matches no drawn document ranks
    nothing; a document row with a stub id beside the real one still maps (every `sources` element).
  - `max_hops=0` path ranks too; no hits → nothing stamped; rows are copies; `expand_graph` is
    awaited with the same seed ids as before (existing tests untouched and green); no extra `find`.
- `tests/unit/mcp/test_graph_tools.py`: `search_memory` on a result whose rows carry `doc_rank` →
  JSON text has no `doc_rank` and equals the pre-stamp serialization; `search_memory(visualize=True)`
  → `to_graph_payload` called with `document_order="relevance"`, payload ships
  `controls.documents.order == "relevance"`; `visualize_memory_graph(query="q")` on a ranked result
  → summary `"… 3 of 3 most-relevant documents shown by default, …"`; the full-graph summary test
  stays green; an unranked query result (NL rows) → no `documents` control, summary unchanged.
- `tests/unit/mcp/test_deep_search.py`: no `doc_rank` in the index or any written file.
- `tests/unit/mcp/test_dashboard_app.py` (or wherever `_fetch_payload` is pinned): order by branch.
- `tests/unit/memory/visualize/test_graph.py`: 165's `documents` dict assertions gain `order`;
  `document_order="relevance"` → `{shown: 12, total: 12, order: "relevance"}` whatever
  `full_graph_shown_docs` is; **consciously rewrite** `test_a_query_payload_has_neither_doc_rank_nor_a_documents_control`
  into `test_an_unranked_payload_has_neither_doc_rank_nor_a_documents_control`; `visualize_query_result`
  passes `"relevance"` iff `query`; template tokens `ORDER_LABEL`, `"Most recent"`, `"Most relevant"`,
  `ORDER_LABEL[documents.order]`; still no `100` in the panel code.
- `tests/unit/mcp/test_viz_app.py` both-variant token list: the four tokens above.
- `test_embeddings.py` and rag-mode `test_query_graph.py` tests: unchanged, green.

### Verification (Tester; headless smoke + CDP as in 165, LOCAL env only, never prod)
Shared local Mongo: 167 dropped and rebuilt the DB from a 3-article subset (memory-for-ai-agents,
context-engineering, ai-agents-planning) — use that corpus; do not rebuild it.
- Query file (`make memory-query-graph QUERY="memory for ai agents"`; also one `--max-hops 2`
  render): `--dump-dom` prints `data-docs="k/k"` with `k` = number of document nodes in `const DATA`,
  header `k of k documents · …`, `Documents` section FIRST with row label `Most relevant`; payload
  `"documents": {"shown": k, "total": k, "order": "relevance"}`, every chunk and document node carries
  `docRank`, and the `docRank == 1` document is the one whose chunks hold the best seed
  (cross-check against `search_memory`'s first rows for the same query).
- Full graph file: 167's numbers, row label `Most recent`, `"order": "recency"`. Map: no `Documents`.
- CDP on the query file: slider to 1 → exactly ONE visible document node, every visible node has
  `docRank == 1` or none, `sim.nodes().length` = visible count, `data-docs="1/k"`; back to `k`
  restores every node; `Reset to defaults` → `k of k`; a frozen-camera reveal 1 → k auto-fits once;
  a pinned document survives hide/reveal.
- MCP (tool functions called directly with a read-only client): `search_memory("memory for ai agents", max_results=10)`
  text byte-identical to the same call on the 167 tree (`cmp`); `visualize=True` payload carries
  `order == "relevance"`; `visualize_memory_graph(query=…)` summary reads `k of k most-relevant documents
  shown by default`; deep-search files contain no `doc_rank`.
- rag mode: `TREE_MEMORY__MODE=rag make memory-query-graph QUERY=…` prints parents as text, no file.

## Out of scope
- A relevance cap or a "shown by default" config key for query views.
- A slider on the NL `query_memory(visualize=True)` view, the **Embedding map**, or `rag` mode.
- Showing the score itself, re-ranking by anything but the seed hits, changing `search_memory`'s
  truncation or `ranked_rows`, the dashboard's own template, drawing `search_memory(visualize=True)`
  from the untruncated result (it draws the `max_results` rows, pre-existing).
- Edges stamped with `doc_rank` on query views.

## Acceptance Criteria

> `[HUMAN]` criteria are checked by the human in a real browser (Claude in Chrome cannot drive the WebGL stage). The SWE/Tester supply CDP evidence and do not block on them.

- [ ] `query_memory` stamps `doc_rank` (1-based) on copies of the node rows of every document row in
      the result: documents with a hit first by best RRF score (ties `str(_id)`), expansion-only documents
      after by `str(_id)`; hits map through every `sources` element; a shared entity takes the smaller rank;
      a node with no ranked provenance and every edge carry no stamp; no extra read; `expand_graph`,
      `_seed_ids`, the closure and `ranked_rows` unchanged (unit tests).
- [ ] `search_memory`'s text and `deep_search_memory`'s index/files never contain `doc_rank`; for the same
      rows they are byte-identical to 166 (unit tests + the Tester's `cmp` on the local corpus).
- [ ] `to_graph_payload(result, document_order=…)` emits `controls.documents = {shown, total, order}` only
      when a node is ranked; `shown = total` for `relevance`, 165's clamp for `recency`; an unranked
      payload (NL rows, the map) has neither `docRank` nor `documents`; the four callers pass the order by
      branch (unit tests).
- [ ] Both template variants build the `Documents` section from `controls.documents` with the row label
      `ORDER_LABEL[documents.order]` (`Most recent` / `Most relevant`), reuse `applyDocumentLimit`, and carry
      no literal `100` (unit tests).
- [ ] `visualize_memory_graph(query=…)` on a ranked result reads `k of k most-relevant documents shown by
      default, …`; the full-graph summary is unchanged (unit tests).
- [ ] Headless smoke (Tester, Log): a query render prints `data-docs="k/k"`, header `k of k documents · …`,
      `Most relevant` first in the panel, `"order": "relevance"` in the payload; the full graph prints
      `Most recent` and `"order": "recency"`; the map carries neither.
- [ ] CDP (Tester, Log): on a query file the slider at 1 leaves exactly one document star, re-syncs
      `sim.nodes()`/links, reheats, updates counts and `data-docs`; back to `k` restores all; `Reset to
      defaults` → `k of k`; a frozen-camera reveal auto-fits once; a pin survives hide/reveal.
- [ ] [HUMAN] On `QUERY="memory for ai agents"`: the panel opens with `Most relevant  k of k`; dragging the
      slider left peels off the least relevant document stars first and the best-matching document's star is
      the last one standing; dragging back restores them; the full graph still opens with `Most recent`.
- [ ] rag mode unchanged: `TREE_MEMORY__MODE=rag make memory-query-graph QUERY=…` prints parents as text.
- [ ] ADR-011 §7, Context, diagram, Consequences edits, glossary rows, README paragraph and Makefile
      comment applied verbatim from the Log (`grep -c "Most relevant" apps/memory/README.md` ≥ 1,
      `grep -c "controls.documents.order" docs/glossary.md` ≥ 1).
- [ ] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green;
      test count ≥ today's; `.tree/graphs/*.html` cleaned up after the human pass.

## User Stories

### Story: Operator narrows a query view to what matched
1. `make memory-query-graph QUERY="memory for ai agents"` on the 3-article corpus.
2. The header reads `3 of 3 documents · …`; the Controls panel's first section is `Documents` with a
   row `Most relevant` at `3 of 3`.
3. Operator drags the slider to 1: only the star of the article whose chunks matched best remains, the
   simulation reheats and the view fits once it settles; the header reads `1 of 3 documents · …`.
4. Operator drags back to 3: every star returns; `Reset to defaults` also returns to `3 of 3`.

### Story: Operator opens the Full graph and nothing moved
1. `make memory-query-graph` (no query): the row label reads `Most recent`; the slider behaves as in 165.

### Story: Assistant visualizes a search inline
1. The model calls `visualize_memory_graph(query="context engineering")`.
2. The text reads `Knowledge graph for 'context engineering': 2 of 2 most-relevant documents shown by
   default, …`; the iframe's panel starts with `Most relevant  2 of 2`.
3. The user drags the slider to 1: the less relevant article's star disappears.

### Story: The model's search answer is unchanged
1. The model calls `search_memory("voyage rate limit", max_results=5)`.
2. The serialized rows are exactly 166's: same order, same keys — no `doc_rank` anywhere.

### Story: Nothing to slide on an NL query or the map
1. `query_memory("count chunks per document", visualize=true)`: no `Documents` section.
2. `make memory-visualize-embeddings`: panel shows Display + Unpin all only.

---

Blocked by: 167

## Log

### [PA] 2026-10-02 11:35 — Grooming

**Summary**
The `Documents` slider from 165 reaches query views (CLI `QUERY=`, `visualize_memory_graph(query)`,
`search_memory(visualize=True)`), ranked by search relevance with every document shown by default;
the Full graph keeps recency. One stamp (`doc_rank`), one payload gate (`controls.documents`, now
with `order`), one renderer, no new config.

**Human decisions**
Slider on query views; relevance = each document's best search hit; default shows ALL documents in
the result; sliding down keeps the most relevant; Full graph unchanged (recency).

**Key decisions**
- Rank from the `hits` `query_memory` already holds, not from a `_search_score` on rows — none
  survives `expand_graph`'s re-hydration (the key exists only in `rag/search.py`).
- Document identity = the drawn `document` rows, matched to hits through `sources` (all elements).
  Documents pulled in only by expansion rank LAST (so the slider at 1 leaves exactly one star and
  `total` = stars drawn).
- `doc_rank` on node rows only (copies); edges unstamped.
- `to_graph_payload(result, *, document_order="recency")`; callers pass it per branch; 165 payloads
  gain `"order": "recency"`.
- `doc_rank` stripped at the two model-facing boundaries (`_serialize`, deep-search writer).
- Row label `Most recent` / `Most relevant` under the unchanged `Documents` heading; MCP summary
  adjective `most-recent` / `most-relevant`.
- rag mode: no graph surface exists (text only) — pinned unchanged, nothing built.

**Doc edits owned by this task (apply verbatim):**

`docs/glossary.md`
- **Graph payload**, Definition — replace "a node may also carry `docRank` (its **Full graph**
  recency rank)" with "a node may also carry `docRank` (its document rank: **Full graph** recency,
  or search relevance on a query view — task 168)".
  Notes — replace the sentence "The **Full graph** payload additionally carries
  `controls.documents: {shown, total}` — the gate for the renderer's `Documents` slider; query
  payloads and the **Embedding map** never carry it." with: "A ranked payload additionally carries
  `controls.documents: {shown, total, order}` — the gate for the renderer's `Documents` slider;
  `order` is `recency` on the **Full graph** (`shown` = `query.full_graph_shown_docs`, clamped) and
  `relevance` on a seed-based query view (`shown` = `total`: a query result is `top_k` wide, so the
  slider narrows it, never caps it). The **Embedding map** and a view whose rows carry no rank (an
  NL `query_memory` pipeline) never carry it."
- **Graph renderer**, Notes — replace "On the **Full graph** the panel's first section is a
  `Documents` slider (`shown` of `total`):" with "When the payload carries `controls.documents`
  (the **Full graph** and every seed-based query view) the panel's first section is a `Documents`
  slider — row label `Most recent` or `Most relevant` after `controls.documents.order` — (`shown`
  of `total`):" (rest of the sentence unchanged).
- **Full graph**, Notes — replace "Distinct from a query view (seeds + `max_hops` expansion)" with
  "Distinct from a query view (seeds + `max_hops` expansion, whose `Documents` slider ranks by
  search relevance instead — task 168)".
(If a phrase was reworded since grooming, apply the equivalent edit and note it in the SWE log.)

`docs/adrs/011_live_force_layout_and_direct_manipulation.md`
- Context references: add `tasks/168-query-view-relevance-documents-slider.md` to the task list.
- Context: append "A query view had no slider at all (165 scoped it out): a 70-node result with
  several document stars could not be narrowed to the ones that matched."
- Decision §7: append "Query views carry the same slider ranked by SEARCH RELEVANCE (task 168):
  `query_memory` ranks the result's `document` rows by their best `hybrid_search` hit (RRF score,
  ties on `_id`; documents pulled in only by expansion rank last) and stamps `doc_rank` on node rows
  through each row's `sources`; `to_graph_payload(result, document_order=…)` adds
  `controls.documents.order` (`recency` | `relevance`) and shows every document by default on a
  query view (`shown = total`) — a query result is `top_k` seeds wide, so the slider narrows, it
  never caps. The rank is a viz concern: `search_memory`'s text and `deep_search_memory`'s files
  strip it, and `ranked_rows` is untouched. The hit order rather than a `_search_score` on rows
  because `expand_graph` re-hydrates every row — the seed list `query_memory` holds is the only
  relevance signal that survives." (The intro stays "Eight related choices".)
- Diagram: `EX` label → `"expand_graph + part_of closure<br/>2 reads · child_count · doc_rank by relevance"`;
  `DOCS` label → `"Documents slider<br/>Most recent | Most relevant (controls.documents.order)<br/>hidden in Sigma · sim.nodes()/links re-synced"`.
- Consequences: in "**Payload contract grows again, all surfaces at once:**" replace
  "`docRank`, `controls.documents`, `childCount`" with "`docRank`, `controls.documents` (+ `order`,
  task 168), `childCount`". Append: "- **The slider means two things.** On the Full graph it
  reveals older documents (default 100 of N); on a query view it narrows to the most relevant
  (default all of N). The row label (`Most recent` / `Most relevant`) and the summary adjective
  (`most-recent` / `most-relevant`) say which; the header counts read the same."

`apps/memory/README.md` (the `make memory-query-graph` section)
- In the full-graph paragraph replace "Drag the `Documents` slider at the top of the Controls panel
  to reveal older ones;" with "Drag the `Documents` slider (`Most recent`) at the top of the
  Controls panel to reveal older ones;".
- Replace "A `QUERY` view has no slider; there, a parent chunk drawn without its children says how
  many it has on hover (`child chunks  N (not shown)`)." with "A `QUERY` view has the same slider
  ranked by search relevance (`Most relevant`): every document of the result shows by default
  (`3 of 3`); drag it left to keep only the best-matching documents' stars. There, a parent chunk
  drawn without its children says how many it has on hover (`child chunks  N (not shown)`)."
(If a phrase was reworded since grooming, apply the equivalent edit and note it.)

`apps/memory/Makefile` — `query-graph` comment: append "; a QUERY view's slider ranks documents by relevance".

Ready for implementation (after 167 lands).
