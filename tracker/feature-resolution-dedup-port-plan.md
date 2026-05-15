# Feature Plan: Resolution + Dedup Port (Neo4j → MongoDB)

## Summary

Port the Neo4j agent-memory resolution + deduplication algorithm to Tree on MongoDB. The single-pass `SequenceMatcher` normalization in `tree.memory.extraction.core.normalize_nodes` is replaced by a four-stage resolver chain (Alias → Exact → Fuzzy via RapidFuzz → Semantic via embeddings) and a three-tier vector-search dedup (auto-merge ≥0.95 / flag 0.85–0.95 / none <0.85). On the medium-confidence flag tier, a new `SAME_AS` edge is emitted with `status="pending"` so a human can confirm or reject the pair later. Three merge strategies (`KEEP_PRIMARY`, `MERGE_PROPERTIES`, `KEEP_ALIASES`) are dispatched from a new `add_entity()` orchestrator. The monolithic extraction flow is rewritten as six cache-aware Prefect tasks (chunk + structural / LLM extract / resolve / embed / dedupe / write) so expensive LLM and embedding calls are cached on `INPUTS` and re-runs only redo the cheap stages. A new human-review API (`find_pending_duplicates` / `review_duplicate` / `get_same_as_cluster`) ships in this PR with matching MCP tools and a CLI, and reject decisions feed back into write-path dedup as a `SAME_AS{status:"rejected"}` filter so the same pair isn't re-flagged. The `_id = "type:_normalize(original_name)"` contract is preserved; `canonical_name` is a separate soft-join property that multiple physical nodes may share.

## Tasks (in order)

1. **#007** — Data-model extension on `KnowledgeGraphEntry` (`canonical_name`, `aliases`, `confidence`, `merged_into`, `merged_at`) + `EdgeType.SAME_AS` — `tracker/007-knowledge-graph-data-model-extension.groomed.md`. Foundation. No dependencies.
2. **#008** — Resolution types + Alias/Exact/Fuzzy resolvers + `rapidfuzz>=3` dep — `tracker/008-resolution-alias-exact-fuzzy.groomed.md`. Depends on #007.
3. **#009** — Semantic resolver (bounded LRU cache) + `CompositeResolver` chain — `tracker/009-resolution-semantic-composite.groomed.md`. Depends on #008.
4. **#010** — Dedup config + `dedupe_entity()` with `$vectorSearch`, RapidFuzz boost, and `SAME_AS{status:"rejected"}` candidate filter — `tracker/010-dedup-vector-search.groomed.md`. Depends on #007.
5. **#011** — `add_entity()` orchestrator + three merge strategies (`KEEP_PRIMARY`, `MERGE_PROPERTIES`, `KEEP_ALIASES`) — `tracker/011-add-entity-orchestrator.groomed.md`. Depends on #007, #009, #010.
6. **#012** — Six-task Prefect extraction pipeline (chunk+structural / LLM-extract / resolve / embed.map / dedupe / apply-writes), config keys, and removal of legacy `normalize_nodes` — `tracker/012-extraction-pipeline-six-tasks.groomed.md`. Depends on #007, #009, #010, #011.
7. **#013** — Indexing updates: `BaseEmbeddingModel.dimensions`, vector-index dimension reconcile, `canonical_name` index, alias-aware text index, and `merged_into` filter — `tracker/013-indexing-dimensions-and-indexes.groomed.md`. Depends on #007. Ordered after #012 to keep the pipeline change contained.
8. **#014** — Human-review API (`find_pending_duplicates`, `review_duplicate`, `get_same_as_cluster`) + 3 MCP tools + CLI — `tracker/014-human-review-api.groomed.md`. Depends on #007, #010, #011.
9. **#015** — End-to-end verification, integration tests, three-strategy smokes, soft-join assertion, and PR — `tracker/015-e2e-verification-and-pr.groomed.md`. Depends on #007–#014.

## Dependencies (graph)

```
007 ──┬─► 008 ──► 009 ──┐
      │                  ├─► 011 ──► 012 ──► 013 ──┐
      ├─► 010 ───────────┘    │                     ├─► 014 ──► 015
      │                       └─────────────────────┘
      └─► (all downstream)
```

## Out of scope (intentional)

- **Transitive `SAME_AS*1..3` propagation on confirm.** Confirming a pair only merges that pair; multi-hop cluster collapse is a follow-up.
- **Background entity enrichment** (Wikipedia / Diffbot lookups). The resolver chain is purely intra-graph.
- **`cluster_id` on `ResolvedEntity`.** Cluster identity is read-time only (via `get_same_as_cluster`, 1-hop), not stored.
- **Scheduled / cron consolidation flow.** Human-review API is on-demand only; no scheduled sweep.
- **Promoting `transcript_languages`, `gemini_model`, or other prior YouTube knobs.** Unchanged in this PR.
- **Replacing `embed_nodes` indexing fallback.** It stays as a backfill for nodes with empty embeddings (e.g. CHUNK content embeddings); the new pipeline embeds entity nodes inline in task ④.

## Documentation updates (this grooming round)

This project does not maintain `docs/adr/` or `docs/glossary.md`, so no ADR or glossary edits accompany this PR. The architectural rationale, resolver-chain order, dedup tier thresholds, `_id` vs `canonical_name` contract, six-task pipeline shape, and merge-strategy semantics are all captured in the reference notes shipped with the source algorithm:

- `/Users/pauliusztin/Documents/01-Projects/test-neo4j-agent-memory/agent-memory/notes/RESOLUTION_MODULE.md` (technical reference; §14 "Orchestration and durability")
- `/Users/pauliusztin/Documents/01-Projects/test-neo4j-agent-memory/agent-memory/notes/RESOLUTION_DEDUP_ALGORITHM.md` (high-level explainer; §10 "Running it in production")
- `/Users/pauliusztin/Documents/01-Projects/test-neo4j-agent-memory/agent-memory/notes/ARCHITECTURE_DEEP_DIVE.md`

Each groomed task references the relevant section. If the project later adopts `docs/adr/`, the decisions in this PR (chain order, tier thresholds, six-task pipeline shape, `_id` contract, three merge strategies, reject-pair filter) are the natural first batch of ADRs to back-fill.

## Open questions

None. All architecture decisions are settled in the feature spec (see "Architecture decisions" §1–§10). The grooming round did not surface new ambiguity.
