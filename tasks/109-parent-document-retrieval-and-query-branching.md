---
id: 109-parent-document-retrieval-and-query-branching
feature: rag-graphrag-modes
status: pending
---

# **Parent-document retrieval** over **Child chunk**s, and mode-aware query composition (rag: parents; graphrag: parents + entity seeds → graph expansion)

Tags: `memory`, `rag`, `graph`, `query`
Depends on: #108
Blocks: #110, #111
Implements: ADR-006 — Decisions 2, 5

## Scope

**1. Hybrid seed search** (`tree/memory/rag/search.py`, moved from `query/core.py`):
`hybrid_search(collection, query, embedding_model, user_id, *, limit, node_filter: dict) ->
list[ScoredHit]` = today's `_vector_search` + `_text_search` + `_rrf_fuse`, with the caller's
`node_filter` merged into BOTH the `$vectorSearch.filter` (only equality on indexed filter
paths — `user_id`, `kind`, `type`, `subtype`) and the `$text` `$match`. Invariant in both
modes: a **Parent chunk** row is never a seed (parents carry no vector; the text stage
excludes `{"type":"chunk","subtype":"parent"}`). Before relying on the `subtype: "child"`
pre-filter, confirm the Atlas `$vectorSearch` filter semantics live via the `tech-docs` skill
(equality on indexed `filter` paths; `null` not filterable).

**2. Parent resolution** (`tree/memory/rag/retrieval.py`):
- Types (`tree/memory/rag/types.py`): `DocumentMeta(document_id, title, source_type,
  source_uri, date)`, `MatchedChild(child_id, chunk_index, content, score)`,
  `RetrievedParent(parent_id, chunk_index, heading_path, content, score, document:
  DocumentMeta, matched_children: list[MatchedChild])`, `RetrievalResult(parents:
  list[RetrievedParent])`.
- `retrieve_parents(client, database, query, embedding_model, user_id, *, top_k=None) ->
  RetrievalResult`: `hybrid_search` with `node_filter={"type":"chunk","subtype":"child"}` and
  `limit = top_k * _CHILD_HITS_PER_PARENT` (module constant `4` — several children of one
  parent collapse into one result; a measured recall gap is what would justify a knob), group
  hits by `parent_id` keeping the best RRF score and every matched child (sorted by score),
  fetch the parents with one `find({"_id": {"$in": ...}})`, fetch their documents by the
  parents' `parent_id` with one more `$in`, attach `DocumentMeta`, sort by score desc,
  truncate to `top_k`. A child whose parent row is missing is dropped with a WARNING log.
  Grouping is done in Python after RRF (fusion is already client-side; `$group`/`$lookup`
  would need the fused score server-side — rejected, ADR-006).

**3. Graph composition** (`tree/memory/graph/retrieval.py`, the graph half of today's
`query/core.py`): `expand_graph` moves here unchanged; `query_memory(...) -> QueryResult`
becomes: seeds = `hybrid_search(node_filter={} minus parents)` over children + entities +
documents → child seeds resolved to parents via `retrieve_parents`' grouping helper →
`seed_ids = distinct parent _ids ∪ non-chunk seed _ids` → `expand_graph(seed_ids, max_hops)`.
`QueryResult.nodes` therefore contains parent rows (never child rows) as chunk seeds.
`fetch_full_graph` moves here unchanged. Delete `tree/memory/query/core.py`.

**4. CLI** `scripts/query_graph.py` / `make memory-query-graph QUERY=...`: in rag mode print
the `RetrievalResult` as indented text (title — heading path — score — first 300 chars of
parent content) and exit 0 without writing HTML; with no `QUERY` in rag mode print
`Full-graph visualization is unavailable in rag mode (memory.mode=rag): there are no edges.
Pass QUERY="..." for parent-document search or switch to graphrag.` and exit 1. graphrag
behaviour unchanged.

## Acceptance Criteria

- [ ] `hybrid_search(..., node_filter={"type":"chunk","subtype":"child"})` issues a `$vectorSearch` whose `filter` is exactly `{"user_id": uid, "kind": "node", "type": "chunk", "subtype": "child"}` and a `$text` `$match` carrying the same keys (assert on the captured aggregate pipelines).
- [ ] With `node_filter={}` (graphrag seeds) the `$text` `$match` excludes parents (a seeded parent row with matching content is absent from the results) and the `$vectorSearch` filter is `{"user_id", "kind": "node"}` as today.
- [ ] `retrieve_parents` with fused hits `[c0(p1, .9), c1(p1, .8), c2(p2, .7)]` returns `[p1, p2]` with `p1.score == .9`, `p1.matched_children == [c0, c1]` (score-desc), `p2.matched_children == [c2]`; each result's `document.title/source_uri/date` come from the document row referenced by the parent's `parent_id`.
- [ ] `retrieve_parents(top_k=2)` requests `limit=8` children and returns at most 2 parents even when 8 distinct parents match.
- [ ] A hit whose `parent_id` points to no row is dropped and a WARNING containing the child `_id` is logged; the remaining parents are still returned.
- [ ] `retrieve_parents` on an empty collection returns `RetrievalResult(parents=[])`; the CLI prints `No results.` and exits 0.
- [ ] graphrag `query_memory` with child seeds `c0→p1`, entity seed `e1`, `max_hops=0` returns `nodes` containing `p1` and `e1` and NOT `c0`; with `max_hops=1` `expand_graph` is called with `seed_ids == {p1, e1}` (order-insensitive).
- [ ] `expand_graph` unit behaviour is unchanged (existing `$graphLookup` pipeline assertions pass from the new module path); `tree/memory/query/core.py` no longer exists and `from tree.memory.rag.search import _rrf_fuse` satisfies the existing `TestRRFFuse`.
- [ ] `scripts/query_graph.py` with `TREE_MEMORY__MODE=rag` and `--query` prints one block per parent containing the document title, ` > `-joined heading path and score, writes no file under `.tree/graphs/`; without `--query` it prints the unavailable message and exits 1.
- [ ] `make memory-format-check && make memory-lint-check && make memory-tests` green.

## User Stories

### Story: Reader searches the Chapter-4 memory from the terminal
1. With `TREE_MEMORY__MODE=rag` and the article ingested (#108 story), the reader runs `make memory-query-graph QUERY="what is a parent chunk"`.
2. The terminal lists up to 10 parents, best first: `[0.032] Memory for AI Agents — Parent-document retrieval` followed by the first 300 characters of the parent's content and `matched children: 2`.
3. No browser opens and no `.tree/graphs/*.html` file is written.

### Story: Reader asks for the whole graph in rag mode
1. Reader runs `make memory-query-graph` (no QUERY) with `TREE_MEMORY__MODE=rag`.
2. The command prints the "unavailable in rag mode … no edges" message and exits non-zero.

### Story: Same query in graphrag returns the parent plus its neighbourhood
1. Reader unsets the override (graphrag) and runs `make memory-query-graph QUERY="what is a parent chunk"`.
2. The rendered graph contains the matching parent chunk node(s), their `part_of` document, sibling parents via `next`, and any entity seeds with their `related_to`/`mentions` neighbours — and NO child chunk nodes as seeds.

### Story: Duplicate-heavy hit list still yields distinct parents
1. A query matches 8 children that all belong to 2 parents.
2. `retrieve_parents(top_k=5)` returns exactly 2 parents, each listing its matched children with scores.

## Out of scope

- MCP tool wiring and per-mode tool sets (#110).
- Reranking, `$rerank`, or a configurable child fan-out knob.
- Re-embedding or changing the RRF constant.

---

Blocked by: #108

## Log
