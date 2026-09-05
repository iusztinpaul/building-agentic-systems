---
id: 109-parent-document-retrieval-and-query-branching
feature: rag-graphrag-modes
status: done
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

- [x] `hybrid_search(..., node_filter={"type":"chunk","subtype":"child"})` issues a `$vectorSearch` whose `filter` is exactly `{"user_id": uid, "kind": "node", "type": "chunk", "subtype": "child"}` and a `$text` `$match` carrying the same keys (assert on the captured aggregate pipelines).
- [x] With `node_filter={}` (graphrag seeds) the `$text` `$match` excludes parents (a seeded parent row with matching content is absent from the results) and the `$vectorSearch` filter is `{"user_id", "kind": "node"}` as today.
- [x] `retrieve_parents` with fused hits `[c0(p1, .9), c1(p1, .8), c2(p2, .7)]` returns `[p1, p2]` with `p1.score == .9`, `p1.matched_children == [c0, c1]` (score-desc), `p2.matched_children == [c2]`; each result's `document.title/source_uri/date` come from the document row referenced by the parent's `parent_id`.
- [x] `retrieve_parents(top_k=2)` requests `limit=8` children and returns at most 2 parents even when 8 distinct parents match.
- [x] A hit whose `parent_id` points to no row is dropped and a WARNING containing the child `_id` is logged; the remaining parents are still returned.
- [x] `retrieve_parents` on an empty collection returns `RetrievalResult(parents=[])`; the CLI prints `No results.` and exits 0.
- [x] graphrag `query_memory` with child seeds `c0→p1`, entity seed `e1`, `max_hops=0` returns `nodes` containing `p1` and `e1` and NOT `c0`; with `max_hops=1` `expand_graph` is called with `seed_ids == {p1, e1}` (order-insensitive).
- [x] `expand_graph` unit behaviour is unchanged (existing `$graphLookup` pipeline assertions pass from the new module path); `tree/memory/query/core.py` no longer exists and `from tree.memory.rag.search import _rrf_fuse` satisfies the existing `TestRRFFuse`.
- [x] `scripts/query_graph.py` with `TREE_MEMORY__MODE=rag` and `--query` prints one block per parent containing the document title, ` > `-joined heading path and score, writes no file under `.tree/graphs/`; without `--query` it prints the unavailable message and exits 1.
- [x] `make memory-format-check && make memory-lint-check && make memory-tests` green.

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

### [SWE] 2026-09-05 16:05 — Implementation

**Files modified**

Source:
- `apps/memory/src/tree/memory/rag/search.py` — NEW. `hybrid_search(collection, query, embedding_model, user_id, *, limit, node_filter)` + `_vector_search` / `_text_search` / `_rrf_fuse` moved from `query/core.py`. `node_filter` is merged into BOTH the `$vectorSearch.filter` and the `$text` `$match`; the `$match` ALWAYS carries `"$nor": [{"type": "chunk", "subtype": "parent"}]` so the "a Parent chunk is never a seed" invariant lives in one place instead of in each caller's filter. Module docstring records the live Atlas filter semantics (see Notes).
- `apps/memory/src/tree/memory/rag/retrieval.py` — NEW. `retrieve_parents(...) -> RetrievalResult` (child-only hybrid search at `limit = top_k * _CHILD_HITS_PER_PARENT`, `_CHILD_HITS_PER_PARENT = 4`; group; ONE `$in` for parents, ONE for their documents; rank; truncate) + the shared `group_children_by_parent(hits)` helper.
- `apps/memory/src/tree/memory/rag/types.py` — added `ScoredHit`, `DocumentMeta`, `MatchedChild`, `RetrievedParent`, `RetrievalResult` (Pydantic, field-described) and extended the module docstring to cover the retrieval family.
- `apps/memory/src/tree/memory/graph/__init__.py`, `apps/memory/src/tree/memory/graph/retrieval.py` — NEW package (the rest lands in #111). `expand_graph` and `fetch_full_graph` moved unchanged; `query_memory` recomposed: `hybrid_search(node_filter={})` → `_seed_ids(hits)` (child hits collapsed to parent ids via the rag grouping helper, `∪` non-chunk seed ids) → `expand_graph`.
- `apps/memory/src/tree/memory/query/core.py` — DELETED. Importers retargeted: `mcp/tools.py`, `mcp/graph_app.py`, `mcp/dashboard_app.py`, `scripts/query_graph.py`. `memory/query/__init__.py` docstring points at the three new modules; `search_nodes` is gone (it had no external caller).
- `apps/memory/scripts/query_graph.py` — reads `memory.mode` once at start. `rag` + `--query`: prints one block per parent (`[score] title — heading > path`, 300-char indented excerpt, `matched children: N`) and returns without touching the renderer; empty result → `No results.`, exit 0. `rag` without `--query`: prints `RAG_FULL_GRAPH_UNAVAILABLE` and exits 1. `graphrag`: unchanged.

Tests:
- `apps/memory/tests/unit/memory/conftest.py` — NEW. `FakeMemoryCollection` (records aggregate pipelines / find filters AND applies them to rows: equality, `$in`, `$nor`, `$text`-as-substring, `$limit`; rows with an empty `embedding` are invisible to `$vectorSearch`, like the real index) + row-builder fixtures for document / parent / child / entity rows.
- `apps/memory/tests/unit/memory/rag/test_search.py` — NEW (14 tests) incl. `TestRRFFuse` moved verbatim from the deleted `tests/unit/memory/query/test_core.py`.
- `apps/memory/tests/unit/memory/rag/test_retrieval.py` — NEW (10 tests).
- `apps/memory/tests/unit/memory/graph/test_retrieval.py` (+ `__init__.py`) — NEW (10 tests).
- `apps/memory/tests/unit/scripts/test_query_graph.py` — NEW (9 tests, CliRunner).
- `apps/memory/tests/unit/memory/query/test_core.py` — DELETED (moved).

**Tests**
- Unit: 2282 passing, 0 failing (`make memory-tests`; was 2244 before this task, +38)
- Integration: N/A — no integration suite in this repo (CLAUDE.md); e2e was run against the local Docker stack instead.

**Acceptance criteria**
- [x] `hybrid_search(..., node_filter={"type":"chunk","subtype":"child"})` filter shapes — `test_search.py::TestChildOnlyFilter::test_vector_filter_is_user_kind_type_and_subtype` + `::test_text_match_carries_the_same_filter_keys`
- [x] `node_filter={}` excludes parents from the text stage, vector filter unchanged — `test_search.py::TestGraphSeedFilter::test_vector_filter_is_user_and_kind_only`, `::test_text_match_excludes_parent_rows`, `::test_a_matching_parent_row_is_not_returned` (behavioural: a parent row whose content matches is absent from the results)
- [x] Grouping `[c0(p1,.9), c1(p1,.8), c2(p2,.7)]` → `[p1, p2]`, document meta from the document row — `test_retrieval.py::TestRetrieveParents::test_returns_distinct_parents_with_their_matched_children`, `::test_document_metadata_comes_from_the_document_row` (the parent's denormalised title is deliberately stale in that test)
- [x] `top_k=2` requests `limit=8`, returns ≤ 2 parents out of 8 — `::test_requests_four_child_hits_per_requested_parent`, `::test_truncates_to_top_k_parents`
- [x] Orphan child dropped + WARNING naming the child `_id` — `::test_child_with_a_missing_parent_row_is_dropped_with_a_warning` (and `TestGroupChildrenByParent::test_hit_without_parent_id_is_dropped_with_a_warning` for the no-`parent_id` variant)
- [x] Empty collection → `RetrievalResult(parents=[])`; CLI prints `No results.` exit 0 — `::test_empty_collection_returns_no_parents`, `test_query_graph.py::TestRagMode::test_empty_result_prints_no_results_and_exits_zero`
- [x] graphrag composition: `max_hops=0` → `{p1, e1}` and NOT `c0`; `max_hops=1` → `expand_graph(seed_ids={p1,e1})` — `graph/test_retrieval.py::TestQueryMemoryComposition::test_child_seeds_are_returned_as_their_parents`, `::test_expansion_starts_from_parent_ids_union_entity_seeds`
- [x] `expand_graph` unchanged from the new path; `query/core.py` gone; `_rrf_fuse` importable from `rag.search` — `graph/test_retrieval.py::TestExpandGraph::*`, `test_search.py::TestRRFFuse`, `grep -rn "query.core|search_nodes" apps/memory/{src,tests,scripts,deploy}` → only prose references in docstrings
- [x] CLI rag mode: blocks with title / ` > ` heading path / score, no `.tree/graphs/` file; no query → message + exit 1 — `test_query_graph.py::TestRagMode::*` and the live CLI runs below
- [x] `make memory-format-check && make memory-lint-check && make memory-tests` green

**Evidence**

```
$ make memory-format-check && make memory-lint-check
264 files already formatted
All checks passed!

$ make pre-commit
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed

$ make memory-tests
============================ 2282 passed in 18.53s =============================
```

Live e2e against the Docker stack (real Voyage embeddings, real Gemini in graphrag,
`_run_extraction_worker_body` + `ensure_indexes` + retrieval in-process; chunking pinned small
via `TREE_MEMORY__CHUNKING__PARENT__SIZE=90 CHILD__SIZE=30 CHILD__OVERLAP=0` so one small
article yields 2 parents × 7 children; every row deleted afterwards):

```
# TREE_MEMORY__MODE=rag
Vector search index 'vector_index' already up-to-date (dimensions=1024,
    filters=['kind', 'merged_into', 'subtype', 'type', 'user_id'])
row counts: {'document': 1, 'parent': 2, 'child': 7, 'edges': 0}
Parent-document retrieval: 7 child hit(s) -> 2 parent(s), returning 2
[0.033] Memory for AI Agents … — Memory for AI Agents
    parent_id=…#parent-0 chunk_index=0
    matched children: [('child-1', 0.0328), ('child-2', 0.0323), ('child-3', 0.031),
                       ('child-4', 0.0308), ('child-0', 0.0305)]
[0.032] Memory for AI Agents … — Memory for AI Agents > Retrieval > Graph expansion
    parent_id=…#parent-1 chunk_index=1
    matched children: [('child-1', 0.0317), ('child-0', 0.0299)]
query_memory (rag): nodes=2 edges=0; node (type, subtype) pairs: [('chunk', 'parent')]
child nodes in result: []

# TREE_MEMORY__MODE=graphrag (same article)
llm_extract_entities: n_parents=2 n_entities_raw=9 n_edges_raw=1
apply_writes: nodes_written=9 edges_written=16
row counts: {'document': 1, 'parent': 2, 'child': 7, 'edges': 16}
Parent-document retrieval: 7 child hit(s) -> 2 parent(s), returning 2   # identical to rag
Graph expansion: 2 seed(s) → 9 nodes, 7 edges (1 hops)
node (type, subtype) pairs: [('chunk','child'), ('chunk','parent'), ('document',None),
                             ('object','software')]
```

Scores of `0.0328 ≈ 2/61` prove BOTH stages fired (vector + text) rather than one-stage fallback.

```
# CLI, rag mode, no query
$ TREE_MEMORY__MODE=rag make memory-query-graph
Full-graph visualization is unavailable in rag mode (memory.mode=rag): there are no edges.
Pass QUERY="..." for parent-document search or switch to graphrag.
make[1]: *** [query-graph] Error 1

# CLI, rag mode, with query (ingested user, rows deleted afterwards)
$ TREE_MEMORY__MODE=rag make memory-query-graph USER_ID=6a9c3c20… QUERY="what is parent document retrieval"
INFO:tree.memory.rag.retrieval:Parent-document retrieval: 7 child hit(s) -> 2 parent(s), returning 2
[0.033] Memory for AI Agents … — Memory for AI Agents
    # Memory for AI Agents
    …(300 chars)…
    matched children: 5

[0.032] Memory for AI Agents … — Memory for AI Agents > Retrieval > Graph expansion
    ### Graph expansion
    …
    matched children: 2
$ ls apps/memory/.tree/graphs | wc -l
       0

# CLI, graphrag mode, same user/query — unchanged behaviour
INFO:tree.memory.graph.retrieval:Graph expansion: 2 seed(s) → 2 nodes, 0 edges (1 hops)
INFO:tree.memory.query.visualize:Wrote self-contained graph HTML (2 nodes, 0 edges) to
    …/apps/memory/.tree/graphs/what-is-parent-document-retrieval-20260905-155910.html
```

**Notes**

- **MANDATORY Atlas check (tech-docs).** context7 was not reachable from this session (`mcp__context7__*` tools absent, `claude mcp list` shows no context7 entry), so I used the skill's documented fallback — the MongoDB `llms.txt` index → the markdown source of the two pages. Findings, now recorded in `rag/search.py`'s module docstring:
  - `https://www.mongodb.com/docs/vector-search/indexes/vector-search-type.md` — a `filter`-type index path can filter on **boolean, date, objectId, numeric, string and UUID values, including arrays of these types**. `null` is NOT in that list → `merged_into: null` stays a post-`$match` (unchanged) and `subtype: "child"` (a string, already a declared filter path since #108) is safe as a pre-filter.
  - `https://www.mongodb.com/docs/atlas/atlas-vector-search/vector-search-stage/` — the pre-filter supports **the `$eq` short form** (`{"subtype": "child"}` ≡ `{"subtype": {"$eq": "child"}}`) plus `$and` / `$in`; pre-filtering does not change `vectorSearchScore`. Confirmed live: the e2e's `$vectorSearch` with the four-key equality filter returned children only, and `retrieve_parents` never saw a parent row.
- **Judgement call — parent exclusion is unconditional in `_text_search`, not per-caller.** The `$nor` clause is added in every call, including `node_filter={"subtype": "child"}` where it is redundant. One invariant in one place beats two callers that must both remember it; the redundant clause costs nothing (the `subtype` equality already excludes parents).
- **Judgement call — `DocumentMeta` falls back to the parent's denormalised properties** (WARNING logged) when the `document` row is missing. The loader copies `title`/`source_type`/`source_uri`/`date` onto every chunk row, so a half-deleted document degrades to slightly stale metadata instead of dropping a valid hit. Only the missing-PARENT case drops hits (that is the AC).
- **Judgement call — the CLI's `_format_parent_block` lives in the script**, mirroring `scripts/review_duplicates.py::_format_pending_row`. It is presentation glue over a typed result, not logic; the retrieval layer returns `RetrievalResult` and knows nothing about terminals. If #110's MCP `search_memory` needs the same text (it returns JSON per ADR-006 §5), the helper moves then, not speculatively.
- **`_CHILD_HITS_PER_PARENT = 4` is a module constant**, per ADR-006's bias-to-least note; a measured recall gap is what would promote it to config.
- **Observed, pre-existing, NOT a regression — a nonsense query still returns parents.** `retrieve_parents(query="zzzqqq nonexistent term")` returned 2 parents in the e2e: ANN `$vectorSearch` has no score threshold, so it always yields its nearest neighbours. The old `search_nodes` behaved identically. `No results.` therefore means "no rows for this tenant", which is exactly what the AC's empty-collection test pins. A relevance cut-off would be a new decision (ADR territory), so I did not invent one.
- **Observed — in graphrag, child rows DO appear in `QueryResult.nodes`, as expansion neighbours.** They are never seeds (asserted at `max_hops=0`), but a 1-hop expansion from a parent traverses the child→parent `part_of` edges ADR-006 §3 mandates, so the children come back as neighbours. That matches the ADR ("chunk **seeds** are returned as Parent chunks"); flagging it because the rendered graph in graphrag now shows child nodes around each parent.
- **`_seed_ids` classifies on `type == "chunk" and subtype == "child"`**, not on `type == "chunk"`, so a hypothetical parent seed would seed itself rather than being silently rewritten to its document id. Defensive: parents cannot be seeds today.
- **Not covered by unit tests** (exercised by the e2e above): real mongot `$vectorSearch` pre-filter behaviour on `subtype`, and the vector/text index recreate path.

**What the Tester should probe**
1. Multi-tenancy: `retrieve_parents` / `query_memory` for user A must never surface user B's parent or document rows (both `$in` fetches carry `user_id`; try seeding two users with identical content).
2. The `$nor` parent-exclusion against a REAL Mongo text index (my fake matches substrings) — confirm a parent row with matching content is genuinely absent from `$text` results.
3. Idempotency/ordering: ties in RRF scores (two children with identical scores under one parent) and the resulting parent ordering.
4. `retrieve_parents(top_k=0)` / negative `top_k` — currently `limit=0` is passed straight to `$vectorSearch` (Mongo rejects `limit: 0`); the search layer catches and falls back. Decide whether that deserves a guard.
5. The MCP surfaces that now import from `tree.memory.graph.retrieval` (`tools.py`, `graph_app.py`, `dashboard_app.py`) still behave — `search_memory`/`query_memory`/`visualize_memory_graph` in graphrag return parents where they used to return children.

### [Tester] 2026-09-05 17:40 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check && make memory-lint-check` → all green; `make pre-commit` → all hooks Passed)
- Unit tests: 2282 passed / 0 failed (`make memory-tests`)
- Integration tests: N/A — no integration suite (per CLAUDE.md); verified via live Docker Mongo instead
- Warnings: 1 (`opik`'s pre-existing Pydantic-v1-on-3.14 `UserWarning`, unrelated to this diff — present before this task, not introduced by it)

**E2E adversarial pass** (against the running `tree-mongodb`/`tree-mongot` Docker stack, `env-status` confirmed `local`; rows inserted directly as `MemoryEntry` documents with `MockEmbeddingModel` — no live LLM/Voyage spend needed since these probes test plumbing, not relevance — and deleted afterward; final `db.memory.countDocuments({})` = 0)
- Happy path: CLI `TREE_MEMORY__MODE=rag ... query_graph.py --query "no such content anywhere zzzzz"` against the real empty collection → `No results.`, exit 0 (PASS); CLI `TREE_MEMORY__MODE=rag query_graph.py` (no query) → the exact `RAG_FULL_GRAPH_UNAVAILABLE` string, exit 1 (PASS, byte-for-byte diffed against the task's spec text).
- Break path 1 (cross-tenant isolation): seeded identical `"UNIQUEZEBRA"` content for two tenants (user A / user B) then called `retrieve_parents` and `query_memory` for each → user A's results never contained user B's parent/document/node ids and vice versa (PASS).
- Break path 2 (hostile/real infra: `$nor` parent-exclusion against a REAL `$text` index, not the unit fake): `hybrid_search(collection, "UNIQUEZEBRA", ..., node_filter={})` against real Mongo → the parent row (which also carries `"UNIQUEZEBRA"` in its content) never appeared in the fused hits; only its children did (PASS).
- Break path 3 (state edge: RRF ties): two children under one parent with byte-identical content and embedding vectors, queried twice in a row → `matched_children` order differed between the two calls (`['327d...', 'c386...']` vs `['c386...', '327d...']`). This is a genuine non-determinism, traced to the underlying Atlas-emulated ANN/text stages returning ties in a different order per call (not something `_rrf_fuse`/`group_children_by_parent` introduces — their sorts are stable given their input). No AC requires deterministic tie order and set membership was unaffected both times. Logged as a follow-up note, not a blocking FAIL.
- Break path 4 (boundary: `top_k=0` / `top_k=-1`): both degrade to `RetrievalResult(parents=[])` with no crash — `$vectorSearch`/`$text` reject `limit: 0`/`limit: -1`, both stages hit their `except Exception` branches. Acceptable per the AC's own "empty → `RetrievalResult(parents=[])`" precedent, but the resulting log lines read `WARNING: Vector search unavailable, falling back to text-only` / `WARNING: Text search unavailable, falling back to vector-only` — misleading for on-call debugging (implies an outage, not a caller-supplied `top_k<=0`). Noted as a quality follow-up, not blocking.
- Break path 5 (malformed data: a child whose `parent_id` points at a `document` row instead of a `parent` chunk): `retrieve_parents` did not crash — it fetched the document row as if it were the "parent", got empty `content`/`heading_path` (documents carry no `properties.content`), logged `Document row None missing for parent <doc_id>...` and fell back to the document's own denormalised properties for the title. No exception, no corrupted state; the returned block is just visibly empty-bodied, which is an acceptable degrade for a data-corruption scenario outside the task's Scope (PASS, with a minor smell noted).

**Acceptance criteria**
- [x] PASS — `hybrid_search` child-filter shapes exact — `test_search.py::TestChildOnlyFilter::*`; live: `stage["filter"] == {"user_id","kind","type","subtype"}` reproduced against real Mongo
- [x] PASS — `node_filter={}` excludes parents from text stage, vector filter unchanged — `test_search.py::TestGraphSeedFilter::*`; live break path 2 above confirms against a REAL text index
- [x] PASS — grouping `[c0(p1,.9), c1(p1,.8), c2(p2,.7)] → [p1, p2]`, document meta from document row — `test_retrieval.py::TestRetrieveParents::test_returns_distinct_parents_with_their_matched_children`, `::test_document_metadata_comes_from_the_document_row` (asserts against a deliberately stale parent-denormalised title)
- [x] PASS — `top_k=2` → `limit=8`, ≤2 parents out of 8 — `test_retrieval.py::TestRetrieveParents::test_requests_four_child_hits_per_requested_parent`, `::test_truncates_to_top_k_parents`
- [x] PASS — orphan child dropped + WARNING naming the child `_id` — `test_retrieval.py::TestRetrieveParents::test_child_with_a_missing_parent_row_is_dropped_with_a_warning`, `TestGroupChildrenByParent::test_hit_without_parent_id_is_dropped_with_a_warning`
- [x] PASS — empty collection → `RetrievalResult(parents=[])`; CLI `No results.` exit 0 — `test_retrieval.py::TestRetrieveParents::test_empty_collection_returns_no_parents`, `test_query_graph.py::TestRagMode::test_empty_result_prints_no_results_and_exits_zero`; reproduced live against the real empty collection above
- [x] PASS — graphrag composition: `max_hops=0` → `{p1,e1}` not `c0`; `max_hops=1` → `expand_graph(seed_ids={p1,e1})` — `graph/test_retrieval.py::TestQueryMemoryComposition::test_child_seeds_are_returned_as_their_parents`, `::test_expansion_starts_from_parent_ids_union_entity_seeds`; reproduced live in break path 1
- [x] PASS — `expand_graph` unchanged; `query/core.py` gone; `_rrf_fuse` importable from `rag.search` — `graph/test_retrieval.py::TestExpandGraph::*`, `test_search.py::TestRRFFuse`; `grep -rn "query\.core|search_nodes" apps/memory/{src,tests,scripts,deploy} .agents docs` → only prose docstring references, zero importers
- [x] PASS — CLI rag mode blocks (title / ` > ` heading path / score, no `.tree/graphs/` file); no query → message + exit 1 — `test_query_graph.py::TestRagMode::*`; both reproduced live (exact byte match on `RAG_FULL_GRAPH_UNAVAILABLE`, `.tree/graphs/` unchanged at 0 files)
- [x] PASS — `make memory-format-check && make memory-lint-check && make memory-tests` green — see Evidence

**Evidence**
```
$ make memory-format-check && make memory-lint-check
264 files already formatted
All checks passed!

$ make pre-commit
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed

$ make memory-tests
============================ 2282 passed in 18.04s =============================

$ grep -rn "query\.core|search_nodes" apps/memory/{src,tests,scripts,deploy} .agents docs
apps/memory/src/tree/memory/graph/retrieval.py:3:  ...pre-ADR-006 ``tree.memory.query.core``... (docstring prose)
apps/memory/src/tree/memory/rag/search.py:8:  ...pre-ADR-006 ``search_nodes`` did. (docstring prose)
# zero code importers

$ TREE_MEMORY__MODE=rag uv run python scripts/query_graph.py
Full-graph visualization is unavailable in rag mode (memory.mode=rag): there are no edges. Pass QUERY="..." for parent-document search or switch to graphrag.
$ echo $?
1

$ TREE_MEMORY__MODE=rag USER_IDENTIFIER=paul@example.com uv run python scripts/query_graph.py --query "no such content anywhere zzzzz"
...
No results.
$ echo $?
0
```

**Other issues found**
- RRF tie ordering is not stable across repeated identical calls when two children of one parent have byte-identical fused scores — traced to the underlying Atlas-emulated ANN/text search returning ties in varying order, not to `_rrf_fuse`/`group_children_by_parent`. No AC requires determinism here and set membership is unaffected; a secondary deterministic sort key (e.g. `child_id`) on `group_children_by_parent`'s per-parent sort would remove the flakiness cheaply if it's ever worth doing.
- `retrieve_parents(top_k<=0)` degrades to an empty result via BOTH `_vector_search` and `_text_search` hitting their generic `except Exception` branches, which log `"Vector search unavailable, falling back to text-only"` / `"Text search unavailable, falling back to vector-only"` — misleading during on-call debugging (reads like an infra outage, not a caller passing `top_k=0`). A `top_k <= 0` guard at the top of `retrieve_parents` returning `RetrievalResult()` directly (mirroring the empty-collection path) would be cheap and remove the misleading logs; not blocking since the observable behavior is already correct.
- A child row whose `parent_id` points at a `document` row (corrupted/malformed data) is silently treated as if the document were its parent chunk, producing a `RetrievedParent` with empty `content`/`heading_path`. No crash, WARNING logged, but the WARNING's phrasing ("parent %s is missing") doesn't name the row that turned out to be a document, which would slow down triage of this specific corruption. Minor, and outside the task's Scope (LLM/hand-written rows are the plausible corruption source, not anything this task's code writes).

**VERDICT: PASS**

### [PA] 2026-09-05 20:32 — Acceptance Review

**VERDICT: REJECT** (feature-level verdict for PR #41, `rag-graphrag-modes`)

Rollup Issues 1, 6 and 7 touch this task. Issue 1 (the one that matters): the `nl_query` system prompt still describes node rows as `_id, kind, type, name, properties, embedding` — no `subtype`, `parent_id`, `chunk_index` — although ADR-006 § Consequences says it "must" so `query_memory` can walk the hierarchy; a grooming omission (no task carried it), now assigned. Issue 6: `top_k <= 0` logs "search unavailable" and a blank rag `search_memory` query leaks the raw Voyage 400. Issue 7: tie order in `group_children_by_parent` is non-deterministic. Parent-document retrieval itself, the CLI rag output, `No results.` and the rag-unavailable message are exactly as specified.

Filed ONE rollup task for the whole feature: `tasks/112-pa-rejection-rag-graphrag-modes.md` (8 issues). Pipeline re-runs from the inner loop with the rollup task; on green, re-run acceptance on this task.
