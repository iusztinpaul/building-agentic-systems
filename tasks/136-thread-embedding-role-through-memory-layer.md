---
id: 136-thread-embedding-role-through-memory-layer
status: pending
feature: voyage-4-and-modal-embedding-catalog
---

# Embedding role at every call site: persisted = `document`, user question = `query`, resolution = `None`

Tags: `memory`, `rag`, `graph`
Depends on: #134, #135
Blocks: #137, #141
Implements: ADR-009 — Decision 5 (role rule) and Decision 6 (cache identity carries the role)

## Scope

The role of every vector is FORCED by the invariant at `src/tree/memory/pipeline.py:1565-1574`
("dedup vector == persisted vector, computed once"): the vector dedup compares is the vector that is
stored, so it must be a `document` vector; therefore every persisted vector is `document`.

- `src/tree/memory/embedding_text.py`: `embed_in_batches(texts, embedding_model, *, input_type=None, …)`,
  `_embed_chunk_resilient(embedding_model, chunk, input_type=None)` (the bisect recursion passes it
  on), `embed_texts(..., input_type=None)`. Each forwards `input_type=` to `.embed(...)`.
- `document` callers (persisted vectors):
  `pipeline.py::_embed_children`, `pipeline.py::_embed_entities`,
  `rag/indexing.py::_embed_batch` (the backfill), `graph/add_entity.py:250`
  (`_embed_chunk_resilient(embedding_model, [embeddable_text], input_type="document")`),
  `graph/preference_supersession.py:414`, and the online pipeline's inline indexing (it reaches
  `_embed_batch`).
- `query` callers (user questions): `memory/rag/search.py:150` (`_vector_search`) and
  `memory/graph/nl_query.py:443` (the `__EMBED__` placeholder).
- `None` (symmetric, never persisted): `graph/resolution/semantic.py` — BOTH `prewarm_cache`
  (`embed_in_batches(to_embed, self._embedding_model)`) and `_embed_cached`. Name-vs-name; its LRU
  therefore never mixes roles — add that sentence to the class docstring.
- `_CachedSingleEmbedding` keeps returning the task-④ vector whatever role it is asked for; its
  docstring states the cached vector IS a `document` vector.
- The `embedding_identity` helper from #134 gains the role: `"voyage:voyage-4:1024:document"` for
  both cached embed tasks, so a vector cached before this task (role-less) is never reused.
- Update the comment block at `pipeline.py:1565-1574` to name the role rule and point at ADR-009.

Write tests with `/squid-testing-python`; assert the role with the spy models, not by patching HTTP.

## Out of scope
- Re-embedding existing rows (#137) — after this task new rows are `document` vectors while old rows
  are role-less; #137 + #141 make the collection uniform (one re-embed total).
- A `query` role for dedup's candidate lookup: forbidden by the invariant above.

## Acceptance Criteria

- [ ] `embed_in_batches(texts, spy, input_type="document")` calls `spy.embed` with `input_type="document"` on EVERY request chunk (3 chunks under `max_inputs=2` for 5 texts → 3 calls, all `document`) — `tests/unit/memory/test_embedding_text.py::TestInputTypeThreading::test_every_chunk_carries_the_role`.
- [ ] A structured-400 bisect keeps the role on both halves and still returns the aligned `[]` placeholder for the poison input — `::test_bisect_preserves_the_role`.
- [ ] `_embed_children` and `_embed_entities` → `document` — `tests/unit/memory/test_pipeline.py::TestEmbeddingRoles`.
- [ ] Backfill `_embed_batch` → `document` — `tests/unit/memory/rag/test_indexing.py::TestEmbeddingRoles::test_backfill_embeds_as_document`.
- [ ] `add_entity` → `document`; with a `_CachedSingleEmbedding` injected, the persisted `embedding` equals the cached vector (invariant unchanged) — `tests/unit/memory/graph/test_add_entity.py::TestEmbeddingRole`.
- [ ] Preference supersession embeds the new statement as `document` — `tests/unit/memory/graph/test_preference_supersession.py::TestEmbeddingRole`.
- [ ] `hybrid_search` embeds the query as `query` — `tests/unit/memory/rag/test_search.py::TestEmbeddingRole::test_query_vector_uses_query_role`; the `__EMBED__` placeholder path does too — `tests/unit/memory/graph/test_nl_query.py::TestEmbeddingRole`.
- [ ] `SemanticResolver` calls `.embed` / `embed_in_batches` with `input_type=None` on both the prewarm and the single-name path — `tests/unit/memory/graph/resolution/test_semantic.py::TestEmbeddingRole`.
- [ ] The identity helper returns `"voyage:voyage-4:1024:document"` for the default config — `tests/unit/memory/test_pipeline.py::TestEmbedTaskCacheIdentity::test_identity_includes_role`.
- [ ] `grep -rn "\.embed(\[" apps/memory/src/tree/memory` shows an explicit `input_type=` on every hit except `resolution/semantic.py`.
- [ ] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green.

## User Stories

### Story: User asks a question after ingesting an article
1. `make memory-run-pipeline MODE=online SOURCE="https://example.substack.com/p/prefect"` — the Opik trace shows `voyage-text-embed` spans whose request carried `input_type: document`.
2. `make memory-query-graph QUERY="how does the coordinator shard documents?"` — the single embed span carries `input_type: query`.
3. The top parent quotes the sharding passage.

### Story: Extraction resolves "Ada Lovelace" against "A. Lovelace"
1. The semantic resolution stage embeds both names with NO role (symmetric name-vs-name).
2. Their cosine similarity is compared to `extraction.resolution.semantic_threshold` as before; neither vector is stored.

### Story: Dedup compares like with like
1. A new `organization` "Prefect Technologies" is extracted; task ④ embeds its node-text as `document`.
2. `add_entity` receives that vector via `_CachedSingleEmbedding`, `dedupe_entity` searches persisted (`document`) vectors with it, and the same vector is persisted on the non-merged path.

---

Blocked by: #134, #135

## Log

### [PA] 2026-09-19 17:43 — Grooming

**Summary**
Every call site states its role; the rule is derived from the dedup==persisted invariant, not chosen per site.

**Key decisions**
- Dedup's lookup vector is `document`, not `query`: it IS the persisted vector (computed once), and entity-vs-entity is a symmetric comparison inside document space.
- Resolution stays role-less: it is never persisted and compares names to names.
- The cache identity gains the role so role-less cached vectors can never be replayed.

**Dependencies**
- #134 — owns the `embedding_identity` helper this task extends.
- #135 — provides the `input_type` parameter.

**User stories**
- 3 stories: ingest-then-ask, role-less resolution, dedup in document space.

Ready for implementation.
