---
id: 111-memory-graph-package-docs-and-e2e
feature: rag-graphrag-modes
status: pending
---

# `tree/memory/graph/` package move, docs/skills wiring, and live e2e verification in BOTH modes

Tags: `memory`, `graph`, `refactor`, `docs`, `e2e`
Depends on: #108, #109, #110
Blocks: —
Implements: ADR-006 — Decision 8 (final layout)

## Scope

**1. Package move — final layout** (pure moves + import rewrites, no behaviour change;
`git mv` so history follows):
```
tree/memory/
  pipeline.py            # the 3 Prefect flows (worker, coordinator, indexing) — from #108
  embedding_text.py      # shared batching + entity node-text (unchanged)
  types.py               # shared transit types (ChunkedDocument, RawExtraction, …)
  rag/                   # cleaning, chunking, embedding, load, search, retrieval, indexing, types
  graph/
    extraction.py        # from extraction/core.py (LLM prompt, _parse_extraction, structural EDGE builder)
    add_entity.py, dedup.py, validation.py, judge.py, first_person_resolver.py,
    preference_supersession.py, sharding.py      # from extraction/
    resolution/          # unchanged package, moved
    review/              # moved
    consolidation/       # moved (dream.py, meta_state.py)
    retrieval.py         # from #109 (expand_graph + query_memory composition + fetch_full_graph)
    nl_query.py, kgquery.py, visualize.py   # from query/
```
Delete the now-empty `extraction/`, `indexing/`, `query/` packages. `orchestrator.py`
entrypoints for `dream_consolidation_all_users` move accordingly (deployment names unchanged,
count 5). Tests mirror the new module layout (`tests/unit/memory/rag/…`,
`tests/unit/memory/graph/…`). Dependency rule, asserted by a unit test that walks the AST of
every module under `tree/memory/rag/`: NO import from `tree.memory.graph` (rag never depends
on graph; graph may import rag). A second unit test asserts the RAG loader
(`tree/memory/rag/load.py`) can only emit node types in `RAG_NODE_TYPES` and no edge rows.

**2. Docs & skills**:
- `apps/memory/README.md`: "What this app contains" and "Layout" reflect `rag/` vs `graph/`;
  a "Memory modes" section explains the switch (`memory.mode`, `TREE_MEMORY__MODE`), what
  each mode writes (rows vs rows+edges+entities) and retrieves (parents vs parents+graph);
  "Memory extraction"/"indexing" sections use the new stage names; `default.yaml sections`
  lists `memory.mode` and `memory.chunking.*` and drops `extraction.chunk_*`.
- Repo-root `README.md` pipeline comment (`documents → … → memory collection`).
- `.agents/skills/run-pipelines-e2e/SKILL.md`: add step 0 "pick the mode
  (`TREE_MEMORY__MODE=rag|graphrag`, default graphrag) and drop the `memory` collection when
  switching — both modes start from scratch"; note the rag `make memory-query-graph
  QUERY=…` text output.
- `.agents/skills/tree-memory/SKILL.md`: tool table split by mode; `search_memory`
  parameters per mode; Knowledge Graph Reference gains `chunk` subtypes `parent`/`child` and
  `parent_id`; remove the stale `todo`/`experienced` edge rows (they are `related_to`
  semantics since #029).
- `docs/glossary.md` and `docs/adrs/006_rag_graphrag_memory_modes.md` were written in the
  grooming commit — verify code identifiers match the glossary terms (`MemoryEntry`,
  `RAG_NODE_TYPES`, `parent_id`, `subtype: parent|child`, `retrieve_parents`, `clean_text`).

**3. Live e2e in BOTH modes** (per `.agents/skills/run-pipelines-e2e/SKILL.md`, local env,
"Paul Iusztin" user), evidence pasted into `## Log`:
- rag: drop `memory` → `make memory-run-pipeline MODE=online SOURCE=<one Substack URL>` →
  `mongosh` counts (document / parent / child / edge=0) → `make memory-query-graph QUERY=…`
  text output → `TREE_MEMORY__MODE=rag make memory-serve-mcp …` + `search_memory` call.
- graphrag: drop `memory` → same run → counts incl. edges + entity types → `make
  memory-query-graph QUERY=…` HTML → `make memory-serve-mcp` + `search_memory(visualize=True)`
  and `query_memory`.

## Acceptance Criteria

- [ ] `find apps/memory/src/tree/memory -maxdepth 1` lists exactly `__init__.py, pipeline.py, embedding_text.py, types.py, rag/, graph/`; `extraction/`, `indexing/`, `query/` are gone.
- [ ] `grep -rn "tree.memory.extraction\|tree.memory.indexing\|tree.memory.query" apps/memory/src apps/memory/tests apps/memory/scripts apps/memory/deploy .agents docs README.md apps/memory/README.md` returns nothing.
- [ ] AST test: no module under `tree/memory/rag/` imports `tree.memory.graph` or `tree.memory.pipeline`; `tree/memory/rag/cleaning.py` still imports stdlib only (#106 invariant).
- [ ] Unit test: the RAG loader's op builders reject any node type outside `RAG_NODE_TYPES` and never produce a `kind: edge` op.
- [ ] `orchestrator._DEPLOYMENT_SPECS` has exactly 5 entries; every `entrypoint` path exists on disk and names a `@flow` (parametrised test over the specs, importing each module and checking `hasattr(module, fn_name)`).
- [ ] `tests/unit/memory/` mirrors `src/tree/memory/` one-to-one (a test walks both trees: every `rag/*.py`/`graph/*.py` module has a `test_<name>.py`, except `__init__.py`/`types.py`).
- [ ] `apps/memory/README.md` contains a `## Memory modes` heading, the strings `memory.mode`, `TREE_MEMORY__MODE`, `rag/` and `graph/` in Layout, and no `extraction.chunk_size`.
- [ ] `.agents/skills/tree-memory/SKILL.md` contains no `todo` / `experienced` edge rows and documents `search_memory` for both modes; `.agents/skills/run-pipelines-e2e/SKILL.md` mentions `TREE_MEMORY__MODE`.
- [ ] `make memory-format-check && make memory-lint-check && make memory-tests` green; test count ≥ the count after #110.
- [ ] [HUMAN] rag-mode e2e evidence in `## Log`: `mongosh` output showing `edge` count 0, ≥1 `document`, ≥1 `chunk/parent` with `embedding: []`, ≥1 `chunk/child` with a 1024-length embedding; `make memory-query-graph QUERY=…` text output; an MCP `search_memory` response with `parents[0].document.title`.
- [ ] [HUMAN] graphrag-mode e2e evidence in `## Log`: `mongosh` counts by `type` incl. `part_of`, `next`, ≥1 entity type; `make memory-query-graph QUERY=…` writes `.tree/graphs/<slug>-<stamp>.html`; MCP `query_memory` + `search_memory(visualize=True)` responses.

## User Stories

### Story: Reader navigates the codebase progressively
1. Reader opens `apps/memory/src/tree/memory/rag/` and reads `cleaning.py → chunking.py → embedding.py → load.py → search.py → retrieval.py → indexing.py` — the entire Chapter-4 system, with no reference to `graph/`.
2. Reader opens `pipeline.py` and sees one flow body: the rag stages, then `if mode == "graphrag":` the graph stages.
3. Reader opens `graph/` and finds only what Chapter 8 adds.

### Story: Operator verifies the deployment topology after the move
1. Operator runs `make memory-serve-workflows` locally.
2. Prefect registers exactly 5 deployments with the SAME names as before (`data-etl-worker`, `memory-extract-etl-worker`, `online-pipeline`, `offline-pipeline`, `dream-consolidation-all-users`).
3. `make memory-run-pipeline MODE=online SOURCE=…` completes in both modes.

### Story: Agent skill user asks Tree to search in rag mode
1. The user invokes `/tree-memory what does the article say about parent chunks`.
2. The skill picks `search_memory` (documented as available in both modes) and presents parents grouped by document title — it does not attempt `query_memory`.

### Story: Documentation reviewer checks glossary drift
1. Reviewer greps the code for `MemoryEntry`, `RAG_NODE_TYPES`, `parent_id`, `retrieve_parents`, `clean_text`.
2. Every identifier exists and matches the glossary term it implements.

## Out of scope

- Any behaviour change in the moved graph modules.
- Immutable-log storage / materialization.
- Harness changes.

---

Blocked by: #108, #109, #110

## Log
