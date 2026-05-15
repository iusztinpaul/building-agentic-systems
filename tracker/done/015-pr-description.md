# Resolution & dedup port

## Summary

Ports the resolution + dedup subsystem onto the existing single-collection
knowledge graph: every newly-extracted entity now passes through a four-stage
resolver chain (alias → exact → fuzzy → semantic), a vector-search-based
dedup pass against the live graph, and a write-side orchestrator that
applies one of three documented merge strategies. The extraction Prefect
flow is rewritten around the six-task pipeline described in
`tracker/012-...` (chunk + structural → LLM extract → resolve → embed →
dedup → apply writes), with the resolver and dedup config constructed
ONCE at flow entry. The MCP ingest path and the unit-test seams stay
backwards-compatible.

The PR also ships the human-review API + CLI for flagged duplicates,
extends the data model with the five fields and the `EdgeType.SAME_AS`
needed to record resolution/dedup state, derives the Atlas Search
`vector_index` dimensions from the configured embedding model, and adds
an end-to-end smoke that walks the seed → extract → index → review →
mongosh-soft-join cycle once per merge strategy.

## Modules added / removed

**Added**

- `tree.memory.resolution` — alias, exact, fuzzy, semantic, composite
  resolvers + shared types. `CompositeResolver` is type-strict by default
  and bounded by an LRU embedding cache.
- `tree.memory.extraction.dedup` — `dedupe_entity(...)` runs Atlas
  `$vectorSearch` + an optional fuzzy stage and returns one of
  `{none, merged, flagged}` per entity, with a `reject-pair` `$lookup`
  filter so previously-rejected pairs cannot resurface.
- `tree.memory.extraction.add_entity` — write-side orchestrator that
  unifies the extraction-pipeline and human-review write surfaces; one
  aggregation pipeline per merge strategy
  (`keep_primary`, `merge_properties`, `keep_aliases`).
- `tree.memory.review` — `find_pending_duplicates(...)` +
  `review_duplicate(...)` + CLI at `scripts/review_duplicates.py`.

**Removed**

- `normalize_nodes` and its four helpers from `extraction/core.py` —
  replaced by the resolver chain + dedup writer.

## Data-model changes

`KnowledgeGraphEntry` (single `knowledge_graph` collection) grows five
node-side fields, one new edge variant, and three new indexes:

| Field | Type | Purpose |
| --- | --- | --- |
| `canonical_name` | `str \| None` | Soft-join target. Multiple `_id`s may share a value. |
| `aliases` | `list[str]` | Surface forms collapsed via merge (cap 50). |
| `confidence` | `float` | Resolver confidence at the time of latest write. |
| `merged_into` | `str \| None` | Tombstone pointer set on human-review confirm. |
| `merged_at` | `datetime \| None` | Tz-aware UTC timestamp of the merge. |

`EdgeType.SAME_AS` is a new edge variant; review state lives in
`properties.status ∈ {pending, confirmed, rejected}`.

New indexes (built by the indexing pipeline's `ensure_indexes`):

- A non-unique, sparse classic index on `canonical_name` for soft-join lookups.
- A compound `(kind, embedding)` index for vector pruning.
- The Atlas Search `vector_index` exposes `merged_into` as a filter path so
  tombstones are excluded from `$vectorSearch` results.

## Config keys

The YAML schema (and corresponding env-var override surface) grows two
sections under `extraction`:

```yaml
extraction:
  resolution:
    fuzzy_threshold: 0.85
    semantic_threshold: 0.80
    type_strict: true
    max_candidates_per_type: 1000
    embedding_cache_max_size: 10000
  dedup:
    enabled: true
    auto_merge_threshold: 0.95
    flag_threshold: 0.85
    use_fuzzy_matching: true
    fuzzy_threshold: 0.90
    max_candidates: 10
    match_same_type_only: true
    merge_strategy: keep_primary
```

**Removed:** the old `extraction.similarity_threshold` (collapsed into
the more granular `dedup.auto_merge_threshold` / `flag_threshold`).

Every key under `extraction.resolution.*` and `extraction.dedup.*` can be
overridden at runtime via `TREE_EXTRACTION__<SECTION>__<KEY>` env vars.
The cross-key validator on `ExtractionConfig` rejects misconfigs (e.g.,
`auto_merge_threshold <= flag_threshold`, or
`resolution.type_strict != dedup.match_same_type_only`) at flow entry.

## Merge strategies

Three documented merge strategies are dispatched on
`DeduplicationConfig.merge_strategy` when `dedupe_entity` returns
`action="merged"` AND when the human-review CLI confirms a flagged pair:

- **`keep_primary`** — append the incoming surface form to `aliases`,
  union sources, discard incoming properties. Bumps `confidence` to
  `max(existing, incoming)`.
- **`merge_properties`** — `keep_primary` effects plus a per-key
  property merge: missing-on-canonical takes incoming, both strings →
  longer wins, both lists → set-union, type mismatch → primary wins.
- **`keep_aliases`** — alias append + source union only. Never touch
  `properties`.

Switch strategies via env var (the contract surface) or via the YAML:

```bash
TREE_EXTRACTION__DEDUP__MERGE_STRATEGY=keep_primary make memory-run-memory-pipeline-extraction
TREE_EXTRACTION__DEDUP__MERGE_STRATEGY=merge_properties make memory-run-memory-pipeline-extraction
TREE_EXTRACTION__DEDUP__MERGE_STRATEGY=keep_aliases make memory-run-memory-pipeline-extraction
```

The repo-level smoke walks all three:

```bash
make memory-smoke-resolution-dedup STRATEGY=keep_primary
make memory-smoke-resolution-dedup STRATEGY=merge_properties
make memory-smoke-resolution-dedup STRATEGY=keep_aliases
```

## The `_id` vs `canonical_name` soft join

The data-model contract introduced in #007 keeps `_id =
"type:_normalize(original_name)"` and treats `canonical_name` as a
**separate soft-join property**. Two physical rows may carry the same
`canonical_name` but distinct `_id`s — the soft join is asserted by the
smoke via:

```js
db.knowledge_graph.aggregate([
  {$match: {kind: "node", canonical_name: {$ne: null}}},
  {$group: {_id: "$canonical_name", ids: {$push: "$_id"}, n: {$sum: 1}}},
  {$match: {n: {$gt: 1}}}
])
```

This matters for retrieval — query callers that want every physical
node behind a single canonical (e.g., "all variants of Alice Smith")
join on `canonical_name`, not on `_id`. Conflating the two is a bug
guarded by a regression test in `tests/unit/entities/test_knowledge_graph.py`.

## Follow-ups (intentionally out of scope)

PR-reviewer-time decisions surfaced by the Testers during grooming:

- `KnowledgeGraphEntry.sources` ODM-typing gap (`list[PydanticObjectId]`
  vs `str`) — the extraction pipeline stores both forms; we keep the
  Pydantic union but should pick one in a follow-up.
- The review CLI subcommands surface `ValueError` tracebacks instead of
  a friendly error message on bad input.
- `FakeEmbeddingModel(dimensions=0)` evaluates to falsy default;
  callers that rely on it for tests should construct with an explicit
  positive int.
- Stale comment in `dedup.py:285-289` references a previous filter shape.
- `nl_query.py:116` docstring references the pre-merge field set.

Tester-flagged follow-ups surfaced during the #015 e2e verification (see
`tracker/015-e2e-verification-and-pr.groomed.md` for the evidence trail):

- **FLAG-path masking under default config.** With the shipped defaults
  `resolution.semantic_threshold=0.80 < dedup.flag_threshold=0.85`, any pair
  that would otherwise land in the dedup FLAG band already triggers
  resolver canonical substitution upstream → the entity is rewritten to
  the canonical surface form before it ever reaches dedup → vector search
  returns cos≈1.0 → the pair auto-merges instead of flagging. The dedup
  engine itself still correctly emits `flagged` when invoked directly
  (proven by `tests/integration/memory/test_dedup.py::test_three_tier_decision_flagged`
  at cos=0.88), so the masking is a config-interaction issue, not a dedup
  bug. **Recommendation (follow-up PR):** add a config cross-validator
  on `ExtractionConfig` that warns when `resolution.semantic_threshold
  ≤ dedup.flag_threshold` so operators understand the masking — OR
  document the invariant in `apps/memory/README.md` under "Resolution &
  dedup smoke". Tester evidence: `tracker/015-e2e-verification-and-pr.groomed.md`.

- **Prefect `INPUTS` cache contamination across processes.** Prefect's
  on-disk task-result cache at `~/.prefect/storage` retains task outputs
  across runs and across processes. Unit tests use
  `FakeEmbeddingModel(dim=8)`; their cached outputs survive into
  subsequent real-pipeline runs where the embedding model is the
  configured 384/768/1024-dim production model, which corrupts dedup's
  `$vectorSearch` (dimension mismatch → silent no-hits, or worse, hits
  against stale low-dim vectors). The repo-level smoke works around this
  with the `_purge_stale_prefect_cache` helper in
  `apps/memory/scripts/smoke_resolution_dedup.py`. **Recommendation
  (follow-up PR):** stamp the Prefect cache key with
  `embedding_model.dimensions` (or model id) so test and prod caches are
  namespaced and cannot cross-contaminate — OR skip the `INPUTS` cache
  entirely in test mode via a Prefect setting.

- **`ensure_indexes` `IndexOptionsConflict` on `text_index` upgrade.**
  Pre-#007 dev databases will hit MongoDB error code 85
  (`IndexOptionsConflict`) when `ensure_indexes` runs, because the
  existing `text_index` was created with a different weights map and no
  top-level `aliases` field. The smoke works around this with
  `_wipe_state`. **Recommendation (follow-up PR):** detect the conflict
  inside `ensure_indexes`, drop and recreate the index with the new
  shape, and emit a clear migration log line so operators know a
  destructive recreate occurred.

Carry-forward / future work (flagged but not built here):

- Transitive `SAME_AS*1..3` propagation across confirmed chains.
- Retroactive `MERGE_ENTITIES` for already-existing nodes that became
  duplicates after a config change.
- Background enrichment of canonical entities (description / web links).
- Scheduled consolidation flow that periodically auto-confirms long-pending
  flags above a high-confidence threshold.
- `cluster_id` field on nodes for fast transitive-cluster lookups.
- A web review UI on top of the existing CLI surface.
- Self-hosted Whisper as a third transcript-fetcher fallback (carried
  forward from the YouTube feature PR).

## Test plan

- [ ] `make memory-unit-tests` — green (725 tests).
- [ ] `make memory-integration-tests` — green within 15 minutes (136 tests).
- [ ] `make memory-smoke-resolution-dedup STRATEGY=keep_primary` — smoke OK.
- [ ] `make memory-smoke-resolution-dedup STRATEGY=merge_properties` — smoke OK.
- [ ] `make memory-smoke-resolution-dedup STRATEGY=keep_aliases` — smoke OK.
- [ ] CI green on the final commit.
- [ ] `code-review` plugin produces no Blockers.
