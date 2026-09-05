---
id: 108-rag-load-stage-and-pipeline-mode-branching
feature: rag-graphrag-modes
status: pending
---

# RAG load stage + one memory pipeline that runs the rag stages always and the graph stages only in `graphrag`

Tags: `memory`, `rag`, `graph`, `pipeline`, `indexing`
Depends on: #106, #107
Blocks: #109, #111
Implements: ADR-006 — Decisions 2, 3, 4, 8

## Scope

Rewrite the memory ingestion flow around the two-level hierarchy and the **Memory mode**.
Move the flows into `tree/memory/pipeline.py` while rewriting them (one touch, not two);
the remaining graph modules move in #111.

**1. Row hierarchy written in BOTH modes** (`tree/memory/rag/load.py`, pure op builders +
one `bulk_write(ordered=False)` upsert, following `_build_structural_node_op`). The loader
writes ONLY node types in `RAG_NODE_TYPES` (`tree.entities.memory`) and asserts it:
- `document` row: `_id = build_node_id(user_id, "document", source_uri)` (unchanged),
  `properties = {source_type, source_uri, title, date}`, `parent_id=None`, `chunk_index=None`,
  `embedding=[]`, `sources=[document_id]`.
- **Parent chunk** row: `name = f"{source_uri}#parent-{i}"`, `type="chunk"`, `subtype="parent"`,
  `parent_id = <document row _id>`, `chunk_index = i`, `properties = {source_type, source_uri,
  date, title, heading_path, content}`, `embedding=[]` (NEVER embedded).
- **Child chunk** row: `name = f"{source_uri}#parent-{i}#child-{j}"`, `subtype="child"`,
  `parent_id = <parent row _id>`, `chunk_index = j`, same `properties` shape (own `content`,
  parent's `heading_path`, document `title`), `embedding = <vector of child_embedding_text>`.
- Deterministic `_id`s → idempotent upserts; re-running a document rewrites the same rows.

**2. Flow topology** (`tree/memory/pipeline.py`; deployment/flow NAMES unchanged:
`memory-extract-etl-worker`, `memory-extract-etl-coordinator`, `memory-indexing-etl`;
`orchestrator._DEPLOYMENT_SPECS` entrypoint path updated; count stays 5):
- ① `clean_and_chunk_task` (per doc, `_INPUTS_NO_HEADERS`, `retries=1`): `clean_text` →
  `split_document(config)` → `ChunkedDocument` (now carrying `parents: list[ParentChunk]`,
  `title`; drop `chunk_texts/chunk_ids/structural`). The `chunk_id` provenance stamped on
  LLM emissions becomes the PARENT row `_id` (deterministic; replaces `uuid4()`).
- ② `embed_children_task` (`_INPUTS_NO_HEADERS` on the sorted unique text list, `retries=2`,
  `cache_expiration` 90 days like `embed_entities_task`): embeds `child_embedding_text(...)`
  for every child with `get_search_embedding_model()` via `embed_in_batches`.
- ③ `load_rag_rows_task` (`NO_CACHE`, `retries=3`): upserts document + parent + child rows.
- **rag mode stops here** (worker returns a `WriteSummary` with `nodes_written` = rows,
  `edges_written = 0`).
- **graphrag mode continues**: structural edges (`part_of` child→parent, `part_of`
  parent→document, `next` between sibling children AND between sibling parents, `referenced`
  document→document) are built from the hierarchy and upserted in `apply_writes` together with
  today's ⑥ path; LLM extraction (`llm_extract_entities_task`) runs over PARENT chunk contents
  (one call per parent — fewer, larger calls); `validate_raws` → first-person → supersession
  → resolve → embed entities → dedup → apply writes are unchanged in mechanism; `mentions`
  edges stay document→person as today. Mode read once at flow entry via `_live_app_config()`.
- `run_extraction_for_documents` (tests-only caller) is DELETED with its tests
  (`test_pipeline.py` blocks, `test_pipeline_user_id_propagation.py`) — its coverage moves
  to the worker body tests.
- Coordinator (`_coordinate_sharded_extraction`, `_fan_out_extraction`,
  `_resolve_pending_document_ids`) unchanged; `sources` arrays on every row keep the
  pending-doc resolution working in both modes.

**3. Indexing** (`tree/memory/rag/indexing.py`, moved from `indexing/core.py`; flow in
`pipeline.py`):
- `embed_nodes` backfill rule: rows with `kind: node`, empty embedding, AND
  (`type == "chunk" and subtype == "child"` OR `type in LLM_EXTRACTABLE_NODE_TYPES`). Children
  embed `child_embedding_text(title, heading_path, content)` from their own denormalised
  properties (no join); entities embed as today. Parents/documents are never selected.
- `_VECTOR_INDEX_FILTER_PATHS = ("user_id", "kind", "type", "subtype", "merged_into")` — the
  existing "missing filter path → recreate" logic makes the live index self-heal on the next
  indexing run. Text index fields unchanged.

**4. Delete** `chunk_document`, `extraction.chunk_size`, `extraction.chunk_overlap` (both YAML
files, `ExtractionConfig`, README `default.yaml sections` bullet) and `build_structural_entries`'
chunk-node branch (edges builder remains, hierarchy-aware).

Keep `_INPUTS_NO_HEADERS`, retries, Opik spans/tags, and `doc_concurrency`/`llm_concurrency`
semaphores exactly as today.

## Acceptance Criteria

- [ ] `ChunkedDocument` carries `parents: list[ParentChunk]` and `title`; task ① on a 3-parent document yields deterministic parent/child names `…#parent-0`, `…#parent-0#child-0`, … and identical output across two calls (`INPUTS` cache-safe).
- [ ] rag mode (`TREE_MEMORY__MODE=rag`), one document with 2 parents × 3 children: the worker writes exactly 1 document row + 2 parent rows + 6 child rows to `memory`; `count_documents({"kind": "edge"}) == 0`; every written row's `type` is in `RAG_NODE_TYPES`; the LLM mock is NOT called; `WriteSummary.edges_written == 0`.
- [ ] Same document in graphrag mode: additionally `part_of` edges = 6 (child→parent) + 2 (parent→document), `next` edges = 4 (children) + 1 (parents), and the LLM mock is called exactly 2 times (once per parent) with each parent's `content`; each extracted node's `chunk_id` equals the parent row `_id`.
- [ ] In both modes every child row has `embedding` of length `dimensions`, every parent and document row has `embedding == []`; child rows carry `parent_id == <parent _id>`, parent rows `parent_id == <document _id>`, `chunk_index` = sibling position, `subtype ∈ {parent, child}`, `properties.title` + `properties.heading_path` denormalised.
- [ ] The text embedded for a child equals `child_embedding_text(title, heading_path, content)` (assert on the spy model's captured inputs), NOT the raw content.
- [ ] Re-running the worker on the same document (either mode) leaves `count_documents({})` unchanged (idempotent upsert on deterministic ids) and refreshes `updated_at`.
- [ ] `embed_nodes` backfill: a fixture with one unembedded child, one unembedded parent, one unembedded document, one unembedded `person` → embeds exactly the child and the person; the child's text is the contextual text.
- [ ] `_VECTOR_INDEX_FILTER_PATHS[0] == "user_id"` and `"subtype" in _VECTOR_INDEX_FILTER_PATHS`; `_build_vector_index_definition(8)` declares `subtype` as a `filter` field; an existing index lacking `subtype` triggers the quiet recreate path (existing test pattern `test_missing_filter_paths_triggers_recreate_without_warning`).
- [ ] `grep -rn "chunk_document\|chunk_size\|chunk_overlap" apps/memory/src/tree/memory` returns nothing; chunk ids no longer use `uuid4`; `AppConfig().extraction` has no `chunk_size`/`chunk_overlap` and both YAML files dropped them.
- [ ] `run_extraction_for_documents` no longer exists; `orchestrator._DEPLOYMENT_SPECS` still has exactly 5 specs and the memory worker entrypoint is `apps/memory/src/tree/memory/pipeline.py:memory_extract_etl_worker`; `tree.offline`/`tree.online` import from `tree.memory.pipeline`.
- [ ] Coordinator fan-out tests (`test_fanout.py`) and `_resolve_pending_document_ids` tests pass unchanged apart from import paths.
- [ ] `make memory-format-check && make memory-lint-check && make memory-tests` green.

## User Stories

### Story: Reader runs the Chapter-4 pipeline (rag) on one article
1. Reader sets `TREE_MEMORY__MODE=rag`, runs `make memory-run-data-pipeline MODE=online SOURCE="https://www.decodingai.com/p/agentic-harness-engineering"`, then `make memory-run-memory-pipeline MODE=online DOC_IDS=<printed id>`.
2. The worker log shows `clean-and-chunk`, `embed-children`, `load-rag-rows` tasks and NO `llm-extract-entities` task.
3. `mongosh` shows in `memory`: 1 `document` row, N `chunk/parent` rows without embeddings, M `chunk/child` rows with 1024-dim embeddings, zero `kind: edge` rows.

### Story: Reader flips to Chapter-8 (graphrag) and sees what the graph layer adds
1. Reader drops the `memory` collection, unsets the override (default `graphrag`) and re-runs the same two commands.
2. The worker log now ALSO shows `llm-extract-entities` (once per parent), `resolve-entities`, `embed-entities`, `dedupe-entities`, `apply-writes`.
3. `memory` now contains `part_of`/`next` edges at both levels plus `person`/`organization`/… nodes and `mentions` edges.

### Story: Nightly offline run in rag mode indexes without touching an LLM
1. The `offline-pipeline` cron fires with `TREE_MEMORY__MODE=rag` in the worker environment.
2. Extraction shards run clean→chunk→embed→load; the trailing `memory-indexing-etl` backfills any child lacking a vector and ensures indexes (vector index now lists `subtype` as a filter path).
3. Gemini usage in Opik for the run is zero.

### Story: Operator re-runs a document after a crash
1. A worker run fails after `load-rag-rows` upserted 9 rows.
2. The operator re-triggers the same document ids.
3. Task ① and ② are `INPUTS` cache hits; ③ upserts the same 9 `_id`s; row count is unchanged.

## Out of scope

- Retrieval over the hierarchy (#109) and MCP behaviour (#110).
- Moving `resolution/`, `review/`, `consolidation/`, `nl_query`, `kgquery`, `visualize` into `memory/graph/` (#111).
- New Prefect deployments or renamed deployment names.

---

Blocked by: #106, #107

## Log
