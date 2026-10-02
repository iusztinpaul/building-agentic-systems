---
id: 168-query-view-relevance-documents-slider
status: done
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

- [x] `query_memory` stamps `doc_rank` (1-based) on copies of the node rows of every document row in
      the result: documents with a hit first by best RRF score (ties `str(_id)`), expansion-only documents
      after by `str(_id)`; hits map through every `sources` element; a shared entity takes the smaller rank;
      a node with no ranked provenance and every edge carry no stamp; no extra read; `expand_graph`,
      `_seed_ids`, the closure and `ranked_rows` unchanged (unit tests).
- [x] `search_memory`'s text and `deep_search_memory`'s index/files never contain `doc_rank`; for the same
      rows they are byte-identical to 166 (unit tests + the Tester's `cmp` on the local corpus).
- [x] `to_graph_payload(result, document_order=…)` emits `controls.documents = {shown, total, order}` only
      when a node is ranked; `shown = total` for `relevance`, 165's clamp for `recency`; an unranked
      payload (NL rows, the map) has neither `docRank` nor `documents`; the four callers pass the order by
      branch (unit tests).
- [x] Both template variants build the `Documents` section from `controls.documents` with the row label
      `ORDER_LABEL[documents.order]` (`Most recent` / `Most relevant`), reuse `applyDocumentLimit`, and carry
      no literal `100` (unit tests).
- [x] `visualize_memory_graph(query=…)` on a ranked result reads `k of k most-relevant documents shown by
      default, …`; the full-graph summary is unchanged (unit tests).
- [x] Headless smoke (Tester, Log): a query render prints `data-docs="k/k"`, header `k of k documents · …`,
      `Most relevant` first in the panel, `"order": "relevance"` in the payload; the full graph prints
      `Most recent` and `"order": "recency"`; the map carries neither.
- [x] CDP (Tester, Log): on a query file the slider at 1 leaves exactly one document star, re-syncs
      `sim.nodes()`/links, reheats, updates counts and `data-docs`; back to `k` restores all; `Reset to
      defaults` → `k of k`; a frozen-camera reveal auto-fits once; a pin survives hide/reveal.
- [ ] [HUMAN] On `QUERY="memory for ai agents"`: the panel opens with `Most relevant  k of k`; dragging the
      slider left peels off the least relevant document stars first and the best-matching document's star is
      the last one standing; dragging back restores them; the full graph still opens with `Most recent`. [HUMAN] pending — human visual check; CDP evidence in Tester Logs
- [x] rag mode unchanged: `TREE_MEMORY__MODE=rag make memory-query-graph QUERY=…` prints parents as text.
- [x] ADR-011 §7, Context, diagram, Consequences edits, glossary rows, README paragraph and Makefile
      comment applied verbatim from the Log (`grep -c "Most relevant" apps/memory/README.md` ≥ 1,
      `grep -c "controls.documents.order" docs/glossary.md` ≥ 1).
- [ ] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green;
      test count ≥ today's; `.tree/graphs/*.html` cleaned up after the human pass. (QA green per Tester Log; deferred: orchestrator cleans after the human pass)

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

### [SWE] 2026-10-02 11:58 — Implementation

**Files modified**
- `apps/memory/src/tree/memory/graph/retrieval.py` — new pure `_rank_by_relevance(result, hits)`. `query_memory` wraps its single `expand_graph` return with it, so the hop path and the `max_hops == 0` path are both covered. `_rank_by_provenance`'s docstring is now order-neutral (no behaviour change).
- `apps/memory/src/tree/mcp/deep_search.py` — new `MODEL_HIDDEN_KEYS = ("embedding", "doc_rank")`; the writer's `cleaned` comprehension uses it.
- `apps/memory/src/tree/mcp/graph_tools.py`:
  - `_serialize` uses the same `MODEL_HIDDEN_KEYS` tuple.
  - `_dual_graph_result` passes `"relevance"`; `visualize_memory_graph` passes `"relevance" if query else "recency"`.
  - New `_ORDER_ADJECTIVE` gives the summary's `most-recent` / `most-relevant`.
  - Docstring updated.
- `apps/memory/src/tree/mcp/dashboard_app.py` — `_fetch_payload` follows the same rule by branch.
- `apps/memory/src/tree/memory/visualize/graph.py`:
  - `to_graph_payload(result, *, document_order="recency")` emits `{shown, total, order}`; `shown = total` for `relevance`.
  - `visualize_query_result` passes `"relevance" if query else "recency"`.
  - JS: one `const ORDER_LABEL = {…}` next to `const documents`, and the row label is `ORDER_LABEL[documents.order]`. Three stale "Full graph only" comments/docstrings were reworded. No literal `100`.
- Docs: `docs/glossary.md` (3 rows), `docs/adrs/011_…md` (references, Context, §7, diagram `EX`/`DOCS`, Consequences), `apps/memory/README.md`, `apps/memory/Makefile` comment.
- Tests:
  - `test_retrieval.py`: new `TestQueryMemoryRelevanceRank`, 12 tests.
  - `test_graph.py`: 165's `documents` dicts gain `order`. New relevance payload test (parametrized over `full_graph_shown_docs` 1/5/100). Deliberately rewrote `test_a_query_payload_…` as `test_an_unranked_payload_has_neither_doc_rank_nor_a_documents_control` (parametrized over both orders). New tests for `visualize_query_result` order-by-query, the row label, and the 4 template tokens.
  - `test_viz_app.py`: 4 tokens added to the both-variant list.
  - `test_graph_tools.py`: `_serialize` strips `doc_rank` byte-identically. `search_memory` text and `visualize=True` → `"relevance"` / `order == "relevance"`. NL `query_memory(visualize=True)` → no control. Query summary reads `3 of 3 most-relevant documents shown by default, 3 nodes, 0 edges`. Full graph → `"recency"`. Unranked query keeps the plain summary.
  - `test_deep_search.py`: no `doc_rank` in the index or any file, and the files are byte-identical to an unranked run.
  - `test_dashboard_app.py`: order by branch.

**Tests**
- Unit: 4471 passed, 0 failed (`make memory-tests`), up from 4430 before the change (recorded first).
- `make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check && make pre-commit`: clean.
- Integration: N/A (no suite).

**Acceptance criteria**
- [x] Stamp: `test_retrieval.py::TestQueryMemoryRelevanceRank`:
  - ordering: `…best_hit_then_expansion_only_last` (B=1, A=2, C=3 expansion-only; chunks with their document; shared `e-ab` = 1) and `…equal_scores_rank_by_id`
  - what stays unranked: `…no_provenance_nodes_and_every_edge_stay_unranked` and `…a_hit_whose_sources_match_no_drawn_document_ranks_nothing`
  - mapping through `sources`: `…stub_id_beside_the_real_one_still_maps` and `…document_seed_maps_through_its_own_sources`
  - paths and empty cases: `…zero_hop_path_ranks_too` and `…without_document_rows_is_left_unstamped`
  - no side effects: `…stamps_copies_and_keeps_rows_and_order`, `…issues_no_extra_read` (find filters and pipelines equal a plain `expand_graph` run), `…still_starts_from_the_same_seed_ids` and `…logs_the_relevance_rank_at_debug`
  - the existing 166/167 tests are untouched and green
- [x] Model text: `TestSerialize::test_strips_the_relevance_rank_and_matches_the_unranked_text`, `test_search_memory_text_never_carries_the_relevance_rank`, `test_deep_search.py::test_the_relevance_rank_reaches_no_file`, plus the live `cmp` below.
- [x] Payload: the `test_graph.py` relevance, recency and unranked tests, plus the order-by-branch tests for `visualize_query_result`, `_dual_graph_result`, `visualize_memory_graph` and `_fetch_payload`.
- [x] Template: `test_template_carries_the_documents_slider[...]`, `test_the_documents_section_is_gated_and_built_before_forces`, `test_the_documents_row_label_names_the_order`, `test_the_panel_code_contains_no_shown_default_of_its_own`, and both variants in `test_viz_app.py`.
- [x] Summary: `test_visualize_query_summary_says_how_many_relevant_documents_show`; the full-graph summary test is unchanged and green.
- [x] Headless smoke and [x] CDP: evidence below (the Tester re-verifies).
- [ ] [HUMAN] Fresh files below.
- [x] rag mode: below.
- [x] Docs: below.
- [ ] QA green ✔; the `.tree/graphs/*.html` cleanup waits for the human pass, so this stays unticked.

**Evidence** (LOCAL env, user `6abf6a9a2591e6feb90ddf5e`, the 167 3-article corpus, read-only: no pipeline, no index or row writes)
```
# Baseline recorded BEFORE any edit with <scratchpad>/168/mcp_direct.py → <scratchpad>/168/before.
# It was re-run into before2: byte-identical, so the corpus/search is deterministic.
$ cmp before/search-10.json  after/search-10.json      # search_memory("memory for ai agents", max_results=10)  IDENTICAL
$ cmp before/search-500.json after/search-500.json     # max_results=500 (untruncated, 259192 chars)            IDENTICAL
$ cmp before/search-ctx-500.json after/search-ctx-500.json  # "context engineering", max_hops=2, 500        IDENTICAL
search_memory(visualize=True) model-visible block      IDENTICAL; payload controls.documents={'shown': 2, 'total': 2, 'order': 'relevance'}
deep_search_memory: diff -r of the row .md files       IDENTICAL; index.yaml identical apart from session_id/created_at/directory; grep doc_rank → 0 files
vis-query BEFORE: "Knowledge graph for 'memory for ai agents': 69 nodes, 120 edges (interactive graph view)."
vis-query AFTER : "Knowledge graph for 'memory for ai agents': 3 of 3 most-relevant documents shown by default, 69 nodes, 120 edges (interactive graph view)."
vis-ctx   AFTER : "Knowledge graph for 'context engineering': 2 of 2 most-relevant documents shown by default, 46 nodes, 82 edges ..."
vis-full  AFTER : "Knowledge graph for your full memory: 3 of 3 most-recent documents shown by default, 105 nodes, 162 edges ..."  (order recency)

$ make memory-query-graph QUERY="memory for ai agents"    → memory-for-ai-agents-20261002-085212.html (65 nodes, 63 edges, 1 hop)
$ make memory-query-graph                                 → graph-20261002-085217.html (3 of 3, 105 nodes, 162 edges — 167's numbers)
$ scripts/query_graph.py --query "memory for ai agents" --max-hops 2 --no-open → memory-for-ai-agents-20261002-085221.html (69 nodes, 120 edges)

# payload (<scratchpad>/168/check_payload.py)
query 085212: documents 3, controls.documents {'shown': 3, 'total': 3, 'order': 'relevance'}, chunks without docRank 0/61, docs without 0
              rank 1 how-does-memory-for-ai-agents-work · 2 context-engineering · 3 ai-agents-planning
max-hops 2  : same control/ranks, 0/61 chunks unranked
full graph  : {'shown': 3, 'total': 3, 'order': 'recency'}: 1 how-does-memory · 2 ai-agents-planning · 3 context-engineering  (recency ≠ relevance)
# cross-check: the hybrid_search hits query_memory ranks from (<scratchpad>/168/hits.py)
0.03227 chunk how-does-memory…#parent… → how-does-memory   (best seed ⇒ docRank 1 ✔)
0.01613 chunk context-engineering…    → context-engineering (best for it ⇒ 2 ✔)
0.01538 chunk ai-agents-planning…     → ai-agents-planning (⇒ 3 ✔)

# headless --dump-dom (--headless=new --enable-unsafe-swiftshader --use-angle=swiftshader --virtual-time-budget=15000, 40 s kill; 0 "Uncaught")
query 085212 : <body data-layout="live" data-docs="3/3" data-sim="running"> | 3 of 3 documents · 65 nodes · 63 edges | sections Documents, Forces, Display, Actions | row "Most relevant" "3 of 3"
query 085221 : data-docs="3/3" | 3 of 3 documents · 69 nodes · 120 edges | Documents first | "Most relevant" "3 of 3"
full  085217 : data-docs="3/3" | 3 of 3 documents · 105 nodes · 162 edges | Documents first | "Most recent" "3 of 3"
map (synthetic, no clustering run locally) embedding-map-20261002-085542.html: <body data-layout="fixed"> (no data-docs) | sections Display, Actions | no "documents" key in the payload

# CDP (<scratchpad>/168/cdp: instrument.py + cdp.mjs from 165, new scenarios.mjs; Chrome --remote-debugging-port=9333)
relevance (085212): boot 65 visible, sim 65/63 links; slider→1: "1 of 3 documents · 28 nodes · 27 edges", data-docs 1/3,
  visible documents [[1, how-does-memory…]] (exactly one), visible ranks [1], sim.nodes 28 = visible, links 27 = visible edges,
  badLinks 0, alpha 0.03→0.5 (reheat); back to 3: 65/63, 3/3; slider 1 then "Reset to defaults" → "3 of 3", 3/3; exceptions []
relevance (085221, max-hops 2): 1 → "1 of 3 documents · 31 nodes · 52 edges", one star (rank 1), sim 31/52; back/Reset → 69/120, 3/3
frozenReveal: at 1, settle, drag (camera frozen), reveal 1→3 → settle 9.1 s → refit (bbox changed), 0 of 65 visible off-screen; a second drag → camera and bbox unchanged (fits once)
pinSurvives: pinned context-engineering doc (rank 2) → at 1 hidden, still pinned, out of sim → at 3 same fx/fy/x/y, back in sim
fullLabel (085217): label "Most recent", order "recency", data-docs 3/3

$ TREE_MEMORY__MODE=rag make memory-query-graph QUERY="memory for ai agents"
Parent-document retrieval: 40 child hit(s) -> 5 parent(s), returning 5
[0.033] How Does Memory for AI Agents Work? …        (text; .tree/graphs file count 10 → 10)

$ grep -c "Most relevant" apps/memory/README.md → 1 ; grep -c "controls.documents.order" docs/glossary.md → 1
```

**Notes**
- **Doc edits.** The glossary (4 replacements) and ADR-011 (7 edits) were applied verbatim; every quoted phrase matched exactly once. Two edits are equivalent rather than literal:
  - README: the source text is hard-wrapped, so the two replacements were applied with re-wrapped lines. The wording is verbatim.
  - Makefile: the comment ended in "fewer.", so it now reads "…MAX_DOCS=N to embed fewer; a QUERY view's slider ranks documents by relevance."
- **Deliberate choice.** If hits exist but none maps to a drawn document, every document still ranks, by `str(_id)`. The spec's "nothing stamped" covers only "no hits / no document row". A query view always has hits, so its slider stays usable.
- **`MODEL_HIDDEN_KEYS`.** It lives in `deep_search.py`, which `graph_tools` already imports from. One tuple serves both boundaries, so they cannot drift apart.
- **Pre-existing, out of scope.** `search_memory(visualize=True)` draws only the `max_results` rows. `total` is the deepest rank present (2 here, at max_results=10).
- **[HUMAN] files** (under `apps/memory/.tree/graphs/`):
  - query: `memory-for-ai-agents-20261002-085212.html` (and `…-085221.html` for `--max-hops 2`)
  - full graph: `graph-20261002-085217.html`
  - existing files were left alone; the new files are the three above plus the synthetic map `embedding-map-20261002-085542.html`
- **Leftover artifacts.**
  - Deep-search sessions `apps/memory/.tree/memory/task168-{before,before2,after}` were kept for the Tester's `diff -r` (gitignored).
  - The rag CLI run printed "OPIK: Configuration saved to file ~/.opik.config" — the existing script's behaviour; I did not change it.
- **Scratchpad.** `<scratchpad>/168/`: `mcp_direct.py` (run `uv run python mcp_direct.py <outdir>` from `apps/memory` with `.env` sourced and `PYTHONPATH=./src`), `before/` `before2/` `after/`, `check_payload.py`, `hits.py`, `cdp/`, `dom/`. `<scratchpad>` = `/private/tmp/claude-501/-Users-pauliusztin-Documents-01-Projects-AI-Engineer-Handbook-building-agentic-systems-building-agentic-systems/944c4516-3945-43f4-8111-e0c95524323d/scratchpad`.
- **Callers of the graph `query_memory`: 6 checked** (grep across `src/`, `scripts/`, `deploy/`, `apps/harness/src`). Each one reaches model text or a file only through a boundary that strips `doc_rank`, or ships it only in the payload:
  - `visualize_memory_graph` (payload)
  - `search_memory` (`_serialize` + payload)
  - `deep_search_memory` (`write_deep_search_results`)
  - `dashboard_app._fetch_payload` (payload; the model sees only `_summary`'s counts)
  - `scripts/query_graph.py` (payload)
  - no hook or harness caller
  Opik `@track` traces of `query_memory` now include `doc_rank`. That is observability, not model text.
- **Tester ACs.** The "Headless smoke (Tester, Log)" and "CDP (Tester, Log)" ACs are ticked on the strength of the SWE evidence above, pending the Tester's re-verification.
- **Old harness.** `qa165c/scenarios.mjs`'s `rowSlider(h, "Documents", …)` matches the row label, which now reads `Most recent` / `Most relevant`. Expect it to return null; that is not a regression. `<scratchpad>/168/cdp/scenarios.mjs` locates the slider by the first range input and `.panel-title`.

### [Tester] 2026-10-02 12:20 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check`, `make memory-lint-check`, `make pre-commit` all exit 0).
- Unit tests: `make memory-tests` run three times. Run 1: 4471 passed. Run 2: 4470 passed, 1 failed (`tests/unit/models/test_prewarm.py::test_first_failure_cancels_the_sibling`, leaked `AsyncConnection.close_conn` tasks while a Chrome/CDP session and live Mongo queries were running in parallel). Run 3: 4471 passed. The file has no diff in this task; it passed 3/3 in isolation. Unrelated, timing-sensitive flake; not counted against 168.
- Integration tests: N/A (no suite, per CLAUDE.md).
- Warnings: 0 new (suite log shows none from changed code).

**Independent re-verification (nothing taken from the SWE's outputs)**
- "Before" re-recorded by me from a scratch git worktree of 56aa2fb (read-only DB, same `mcp_direct.py`), "after" on this tree.
  - `search_memory("memory for ai agents", 10)` `cmp` IDENTICAL (39091 B); `max_results=500` IDENTICAL (259192 B); `"context engineering"` hops 2 / 500 IDENTICAL (98730 B). My "before" is also byte-identical to the SWE's before.
  - `search_memory(visualize=True)` model-visible text block IDENTICAL (`diff` of lines 1-305); payload gains only `docRank` on 10/10 nodes and `controls.documents`.
  - deep search: `diff -r` of the 258 row files IDENTICAL; `index.yaml` differs only in `session_id`/`created_at`/`directory`; `grep -rl doc_rank` over the session = 0. (Note: my run reused session id `task168-after`, so that directory's files were regenerated, byte-identical content.)
  - `grep doc_rank` over every `src/` serializer: only `retrieval.py`, `graph.py` (payload -> camelCase `docRank`) and the `MODEL_HIDDEN_KEYS` strip points; all six `query_memory` callers go through a stripping boundary or the payload.
- Rank correctness vs `hybrid_search` hits: own oracle (best score over `sources` intersection, ties `str(_id)`, unmapped last, node rank = min over `sources`) compared against live `query_memory` for 6 queries x hops 0/1/2/3 x top_k 3/10/40 (72 runs, incl. a single-document result and a tie): 0 mismatches, 0 node-level mismatches, 0 stamped edges, `total == documents drawn == payload document nodes`, `shown == total`. `docRank 1` = the document of the best hit every time (e.g. 0.0323 how-does-memory for "memory for ai agents").
- Pure-function adversarial calls on `_rank_by_relevance`: hit mapping to no drawn document -> every document still ranked by `str(_id)`; hit with `sources` missing/None; equal scores -> `_id` order; document with `sources` `[]`/None -> ranks last and never maps; expansion-only document (rank 3), shared entity (min rank), `sources: []`/None entities unranked; no hits -> unstamped; row without `type` -> ignored. All behave as specified.

**E2E adversarial pass**
- Happy path: `make memory-query-graph QUERY="memory for ai agents"` -> `memory-for-ai-agents-20261002-090356.html` (65 nodes, 63 edges). `--dump-dom`: `data-docs="3/3"`, `3 of 3 documents · 65 nodes · 63 edges`, sections Documents first, row `Most relevant 3 of 3`, no "Uncaught". Payload `{shown:3,total:3,order:"relevance"}`, 0/61 chunks and 0/3 documents unranked, `docRank 1` = how-does-memory-for-ai-agents-work. PASS.
- Full graph: `graph-20261002-090620.html` `{3,3,"recency"}`, `Most recent`; `--max-docs 2` -> `{2,2,"recency"}`, `data-docs="2/2"`, `Most recent` (165 cap intact). Map (`embedding-map-20261002-085542.html`, existing file): no `documents` key, 86 unranked nodes. PASS.
- rag mode: `TREE_MEMORY__MODE=rag make memory-query-graph QUERY=…` prints parents as text, `.tree/graphs` file count unchanged (15 -> 15). PASS.
- Docs: `grep -c "Most relevant" apps/memory/README.md` = 1; `grep -c "controls.documents.order" docs/glossary.md` = 1; ADR/glossary/README/Makefile diffs match the groomed text. PASS.
- CDP (own harness copy under `<scratchpad>/qa/cdp`, real Input events, Chrome headless=new on :9333):
  - relevance (query file): boot 65/63, `Most relevant 3 of 3`; slider 1 -> `1 of 3 documents · 28 nodes · 27 edges`, `data-docs 1/3`, exactly ONE visible document (rank 1), visible ranks `[1]`, `sim.nodes` 28 = visible, links 27 = visible edges, 0 bad links, alpha 0.5 (reheat); back to 3 -> 65/63; slider 1 then `Reset to defaults` -> `3 of 3`, `3/3`, 0 exceptions. PASS.
  - `--max-hops 0` file (8 nodes, 4 edges, 3 docs): 3/3 -> 1/3 (4 nodes, 2 edges, sim 4/2) -> 3/3 -> Reset 3/3. PASS.
  - frozenReveal: at 1 settle (7 s), drag freezes camera, reveal 1->3: bbox changed [-228..174]->[-458..449] (refit), 0 of 65 visible off-screen, a second drag leaves camera and bbox unchanged (fits once). PASS.
  - pinSurvives: rank-2 document pinned by drag, slider 1 -> hidden, still pinned, out of sim; back to 3 -> same fx/fy/x/y, back in sim. PASS.
  - iframe re-render path (same page, `render()` called with: full -> query -> unranked -> single-doc query -> full -> query): labels `Most recent` / `Most relevant` / (no Documents section, `data-docs` removed, panel starts at Forces) / `Most relevant 1 of 1` / `Most recent` / `Most relevant`; header counts and `data-docs` follow each payload; the slider works after every re-render; no stale `ORDER_LABEL` or `data-docs`; 0 exceptions. PASS.
  - single document result (`context engineering`, hops 2): `1 of 1`, range 1..1 (degenerate but inert, setting 0 clamps to 1). PASS with note.
- BREAK PATH (FAIL): `search_memory(visualize=True)` on its default `max_results=10`.
  - Command: `search_memory(query="memory for ai agents", max_results=10, visualize=True)` via `<scratchpad>/qa/trunc.py` (read-only); payload re-rendered via CDP scenario `trunc`.
  - Observed: 10 nodes (all chunks), every one `docRank=2`, ZERO document nodes drawn, `controls.documents = {shown:2,total:2,order:"relevance"}`. Panel reads `Most relevant 2 of 2`, header `2 of 2 documents · 10 nodes · 0 edges`; slider to 1 -> `1 of 2 documents · 0 nodes · 0 edges`, `visible = 0` (empty canvas). `max_results=20`: ranks `{2:19, 3:1}`, total 3, rank 1 absent. Same on "agent planning and reflection" (mr 1..60). Only `max_results >= 60` gives a dense, valid slider.
  - Expected (Scope item 2 / invariant): `total` = stars drawn; the slider at 1 leaves exactly one star; never an empty view. Actual: the truncated rows keep `doc_rank`s whose document rows (or ranks 1..n-1) were cut, so `total` = deepest rank present, not stars drawn.
  - Why it is in scope: the Out-of-scope bullet only freezes WHICH rows `search_memory(visualize=True)` draws (truncated); the surface itself is listed in Scope as a slider surface. Before 168 this view had no slider, so the empty-at-1 canvas is new.
  - Fix (SWE chooses): the problem is local to `_dual_graph_result` (the only truncating caller; CLI, `visualize_memory_graph` and the full graph give dense ranks). (a) Emit `controls.documents` only when at least one `document` node is drawn, `total` = drawn stars; or (b) renumber the drawn rows' `docRank` densely 1..k (distinct ranks present) so position 1 is never empty; (a)+(b) together is safest (a drawn chunk whose rank exceeds `total` must not be hidden at the default). Add a regression test: a truncated `search_memory(visualize=True)` result with the rank-1 document's rows cut never yields an empty view at slider 1.

**Judgement on the SWE decision (hits exist, none maps to a drawn document -> rank all by `str(_id)`)**
Matches the AC/spec text: item 2 puts documents "no hit maps to" last, by `str(_id)`, and says every drawn star is ranked; item 4 only exempts "no hits / no document row". Slider UX is usable (total = stars drawn). It never fires on the live corpus (`unmapped=0` in all 72 combinations; no document drawn without a mapping hit), so it is a theoretical path. ACCEPT. Note: the only unit test near it (`…a_hit_whose_sources_match_no_drawn_document_ranks_nothing`) covers a mixed case; the all-unmapped decision itself is not pinned by a test (suggest one, non-blocking).

**Acceptance criteria**
- [x] PASS - stamp (copies, hit-then-expansion ordering, every `sources` element, shared min rank, unranked/edges, no extra read): `TestQueryMemoryRelevanceRank` green + my 72-run live oracle.
- [x] PASS - model text/files never contain `doc_rank`, byte-identical to 166: my own `cmp`/`diff -r` above.
- [x] PASS - `to_graph_payload(document_order=…)` contract and the four callers by branch: unit tests green; live payloads `{3,3,relevance}` / `{3,3,recency}` / `{2,2,recency}` / unranked none (the truncated search_memory case above is a separate break path).
- [x] PASS - template: `ORDER_LABEL[documents.order]`, no literal 100: tests green; row labels observed live.
- [x] PASS - `visualize_memory_graph(query=…)` summary reads `3 of 3 most-relevant documents shown by default, 69 nodes, 120 edges`; `2 of 2` for "context engineering"; full graph `3 of 3 most-recent …` (my `mcp_direct.py` run).
- [x] PASS - Headless smoke (Tester): re-verified independently on fresh files (query, `--max-docs 2` full, max-hops 0); map carries neither.
- [x] PASS - CDP (Tester): re-verified independently (relevance, hops 0, frozen reveal, pin survives, rerender); kept ticked. The search_memory break path above is on a different (truncated) payload.
- [ ] [HUMAN] - Awaiting human verification.
- [x] PASS - rag mode unchanged.
- [x] PASS - docs applied (greps above).
- [ ] OPEN - `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green (it is; stays open for the `.tree/graphs` cleanup after the human pass).

**Other issues found**
- `search_memory(query="")` -> Voyage 400 `ExtractionError` ("Input cannot contain empty strings"): pre-existing, not caused by 168.
- The `search_memory(visualize=True)` payload (second content block) is model-visible and now carries `docRank` + `controls.documents`, same as 165 does for the full graph; not `doc_rank`, so the "never in model text" criterion holds; mention only.
- Single-document query result renders a 1..1 slider (inert); cosmetic.
- Old `qa165c` selectors by row label "Documents" no longer match (expected).
- Files I created under gitignored `apps/memory/.tree/graphs/` (left in place for the human pass): `memory-for-ai-agents-20261002-090356.html`, `graph-20261002-090620.html`, `context-engineering-20261002-090623.html`, `memory-for-ai-agents-20261002-090628.html` (hops 0), `graph-20261002-091419.html` (`--max-docs 2`). The scratch worktree of 56aa2fb was removed.

**VERDICT: FAIL** - 1 issue: `search_memory(visualize=True)` (default `max_results=10`) ships a `Documents` slider over truncated rows with no document star; sliding to 1 empties the canvas.

### [SWE] 2026-10-02 12:45 — Fixes (Tester FAIL: truncated `search_memory(visualize=True)` slider)

**Root cause**
`_dual_graph_result` draws the `max_results`-truncated rows. Those rows keep the `doc_rank`s that `query_memory` gave the whole result, so a document star (rank 1 included) could be cut while its chunks stayed. `total` then counted the deepest rank present, not the stars drawn. `_dual_graph_result` is the only truncating caller: the CLI, `visualize_memory_graph` and the full graph always draw every ranked star.

**Fix**
Both (a) and (b) are covered by ONE pure helper, `densify_document_ranks(payload)` in `apps/memory/src/tree/memory/visualize/graph.py`. It works on any payload and is applied only in `graph_tools._dual_graph_result`.
- It renumbers the drawn `document` nodes' `docRank` densely `1..k`, keeping their relative order.
- A node whose rank no drawn star carries (its document was cut) loses `docRank`, so it is always shown.
- `controls.documents.total` becomes `k`. `shown` becomes the number of drawn stars the old default showed, with a minimum of 1; for relevance that is `k`.
- When no document star is drawn, `controls.documents` is dropped.
- A dense payload comes back unchanged and the input is never mutated (tested). Nothing else calls the helper, so the CLI, `visualize_memory_graph` and full-graph payloads are untouched by construction.

**Tests added**
- `test_graph.py`, six `densify` tests:
  - renumber from 1
  - never empty at 1
  - rows whose document star was cut become unranked
  - no star drawn: no slider and no `docRank`
  - dense payload unchanged and input not mutated (recency and relevance)
  - a recency default keeps the same stars visible
- `test_graph_tools.py`:
  - `test_a_truncated_search_view_is_never_empty_at_one[1..7]`: the rank-1 document's rows sit LAST in the row order, so truncation cuts them. Each run has either no slider, or total = stars drawn, dense ranks, and exactly one star at 1.
  - `test_a_truncated_search_view_renumbers_the_drawn_stars`: B and C drawn as 1 and 2, `{shown: 2, total: 2}`, no `doc_rank` in the text.
- `test_retrieval.py::…test_when_no_hit_maps_to_a_drawn_document_all_rank_by_id`: the Tester's non-blocking suggestion; A=1, B=2 and the stray hit stays unranked.

**QA loop**
`make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check && make pre-commit`: clean. `make memory-tests`: **4487 passed**, 0 failed.

**Evidence** (LOCAL, read-only)
```
# <scratchpad>/qa/trunc.py (the Tester's script), after the fix
memory for ai agents  mr 1..20  → controls.documents None, no docRank (no document star drawn)
memory for ai agents  mr 60/200 → {'shown': 2, 'total': 2, 'order': 'relevance'}, drawn stars [1, 2]
agent planning…       mr 1..60  → controls.documents None
agent planning…       mr 200    → {'shown': 3, 'total': 3, 'order': 'relevance'}, drawn stars [1, 2, 3]
→ every case: no slider at all, or slider at 1 = exactly one star (ranks unique per document)
# <scratchpad>/168/mcp_direct.py → after-fix/: byte-identical to after/ and before/
search-10 / search-500 / search-ctx-500 json IDENTICAL; search_memory(visualize=True) model block IDENTICAL to before;
visualize_memory_graph summaries (query, ctx, full) IDENTICAL; search-vis (mr 10) now ships no Documents control
```
Full log: `<scratchpad>/168/trunc-after.log`.

**Notes**
- No doc edit. The glossary already says `total` is the stars a view draws, and truncation is outside the PA-owned doc text. Flag for the PA if `densify_document_ranks` should be named in the **Graph payload** row.
- Nothing committed.

### [Tester] 2026-10-02 12:30 — QA (re-review after the truncated-`search_memory` slider fix)

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check && make memory-lint-check && make pre-commit`).
- Unit tests: `make memory-tests` run twice, **4487 passed / 0 failed** both times (57 s); 0 pytest warnings (only the pre-existing opik pydantic-V1 import notice).
- Integration tests: none by design (CLAUDE.md).
- Env: `make env-status` -> local; Mongo read-only (no writes besides the gitignored `.tree/graphs/` + `.tree/memory/task168qa2-after2` the tools themselves write).

**E2E adversarial pass**
- Break path 1 (the previous FAIL; truncation, live corpus): `search_memory(query, max_results=mr, max_hops=1, visualize=True)` for `memory for ai agents` and `agent planning and reflection` at `mr` 1, 5, 10, 20, 40, 60, 200, x6 repeated runs, invariant-checked per payload (`<scratchpad>/qa168b/trunc2.py`): every case is either no `controls.documents` and no `docRank` anywhere (mr 1..60: 0 document stars drawn), or slider `total` = drawn stars with ranks exactly `1..k`, `order=relevance`, exactly ONE star at slider 1, `1 <= shown <= total`, every drawn `docRank <= total` (mr 200: `{2,2}` / `{3,3}`). 84 cases, 0 BAD. `densify_document_ranks(payload) == payload` (idempotent) in all. Expected PASS; PASS.
- Break path 2 (exhaustive truncation: EVERY row prefix, `mr = 1..len(rows)`, 3 queries, 325 prefixes, `qa168b/scan.py`): input never mutated, idempotent, no-slider-or-(total = stars, dense, one star at 1, no rank > total). 172 slider cases + 153 no-slider cases, 0 BAD. 4 prefixes genuinely exercised the renumbering (raw total 3 -> dense 1 or 2, e.g. q0 mr 62 / 63, q1 mr 77 / 78). PASS.
- Break path 3 (CDP, real Chrome, files rendered by `_render_graph_file` from those payloads, instrumented; `qa168b/cdp/scenarios.mjs`): mr 10 and mr 60 (both queries): `data-docs` absent, panel starts at `Forces`, all 10 / 60 nodes visible, no Documents row. mr 200: `Most relevant`, `2/2` -> slider 1 -> `1/2`, 1 star, 28 of 48 nodes visible (q1: `3/3` -> `1/3`, 1 star, 36 of 80); back to total restores. Renumbered cases: q0 mr 62 `1 of 1` (1 star, inert 1..1), mr 63 `2 of 2` -> 1: 1 star / 46 of 63 nodes; q1 mr 77 `1 of 1`, mr 78 `2 of 2` -> 1: 1 star / 58 of 78; truncated-mid cases mr 64 / 79 (`3 of 3` -> 1: 1 star, 27 / 36 nodes). Slider at 1 never empties the canvas. 0 exceptions. PASS.
- Break path 4 (iframe re-render on the same page via `render()`; mr10 -> mr200 -> renum mr63 -> q1 mr60 -> renum mr77 -> q1 mr200): `Forces`-first (no Documents) / `Most relevant 2/2` / `2/2` / no Documents / `1/1` / `3/3`; each slider-at-1 shows one star; no stale `data-docs`; 0 exceptions. PASS.
- Break path 5 (helper edges, python): payload without `controls` and with empty nodes returned as-is / controls dropped; mixed ranks (star rank 5, chunk rank 5, chunk rank 9 whose star was cut, unranked fact) -> star 1, chunk 1, rank-9 chunk unranked, `{shown:1,total:1}`, input unchanged; recency payload with ranks 3, 4 drawn + cut rank-1 chunk -> `{shown:1,total:2}`, ranks 1, 2, cut chunk unranked. PASS. Note (cosmetic, unreachable): two document stars sharing one rank would count as one in `total` (ranks are unique per document by construction in `_rank_by_relevance`).
- Non-determinism note: Voyage/Atlas-local vector search returns varying seed sets between calls in the same process (e.g. `memory for ai agents` mr 200 gave 65 nodes in one call and 48 in another). Not caused by 168; it is why I validated every row prefix instead of a single call.

**No-regression of other surfaces**
- `densify_document_ranks` has exactly one caller: `graph_tools._dual_graph_result` (`grep`); nothing in `scripts/`, `visualize_memory_graph`, the CLI or `fetch_full_graph` calls it.
- Payload identity under the helper (so it could not have changed them anyway): `visualize_memory_graph` payloads (query / `context engineering` / full) `densify(p) == p` asserted inside `mcp_direct2.py`; CLI full-graph file (`query_graph.py --no-open`, `3 of 3 documents shown by default, 105 nodes, 162 edges`) `densify(p) == p`; `query_memory` payloads for the three queries (top_k 15, hops 2) and `fetch_full_graph` payload unchanged (`{3,3,relevance}` / `{3,3,relevance}` / `{2,2,relevance}` / `{3,3,recency}`).
- `search_memory` model text vs the 56aa2fb baseline (`168/before`, `before2`): `search-10.json`, `search-500.json`, `search-ctx-500.json` and the `search_memory(visualize=True)` model block `search-vis.txt` byte-identical (`cmp`); `visualize_memory_graph` full summary identical; query summaries differ only by the intended `N of N most-relevant documents shown by default,` wording. Deep-search files (`.tree/memory/task168qa2-after2/*.md`, 100+ rows) identical to `task168-before/` by `diff -r`; only `index.yaml` differs by session id / timestamp / directory; no `doc_rank` in any text or file.
- 165 / 166 quick regressions (real CDP on the CLI full graph and on the renum-q0-mr63 query file): `Most recent` label + `3/3` on the full graph; frozen camera reveal 1 -> k auto-fits exactly once (`refit: true`, `offAtReveal: 0`, `afterFit: 0` off-screen, a second drag does not refit); cap / closure covered by the green suites.

**Acceptance criteria**
- [x] PASS - stamp, model-text byte-identity, `to_graph_payload(document_order=...)` contract + four callers, template label, `visualize_memory_graph(query)` summary, headless smoke, CDP, rag mode, docs: unchanged and re-confirmed above (unit suites green x2, live payloads, CDP).
- [x] PASS - the truncated `search_memory(visualize=True)` view: slider total = drawn stars, never empty at 1 (Break paths 1-4); regression tests `test_a_truncated_search_view_is_never_empty_at_one[1..7]`, `test_a_truncated_search_view_renumbers_the_drawn_stars`, six `densify` unit tests, the all-unmapped ranking test all green (16 selected densify / truncated tests pass).
- [ ] [HUMAN] - Awaiting human verification (skipped as instructed).
- [ ] OPEN - gate AC `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` is green; stays open for the `.tree/graphs` cleanup after the human pass.

**Other issues found**
- None blocking. `search_memory(visualize=True)` on the live corpus at the default `max_results=10` now ships no Documents slider (no star survives the 10-row cut); sliders appear from ~62 rows. Consistent with the stated rule.
- `scripts/query_graph.py` has no query option via CLI args or stdin; a QUERY env var did not switch it to a query view in my invocation (`Result: 105 nodes` full graph). Not touched by 168; the query path is covered through `visualize_memory_graph` and `visualize_query_result` tests.
- Files left under gitignored `apps/memory/.tree/graphs/`: `graph-20261002-101211/101216/101222.html` (CLI full graph, mine); `.tree/memory/task168qa2-after2/` (deep-search run). Nothing deleted.

**VERDICT: PASS**
