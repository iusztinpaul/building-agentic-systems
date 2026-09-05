---
id: 111-memory-graph-package-docs-and-e2e
feature: rag-graphrag-modes
status: done
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

- [x] `find apps/memory/src/tree/memory -maxdepth 1` lists exactly `__init__.py, pipeline.py, embedding_text.py, types.py, rag/, graph/`; `extraction/`, `indexing/`, `query/` are gone.
- [x] `grep -rn "tree.memory.extraction\|tree.memory.indexing\|tree.memory.query" apps/memory/src apps/memory/tests apps/memory/scripts apps/memory/deploy .agents docs README.md apps/memory/README.md` returns nothing — SATISFIED per orchestrator ruling: the grep is clean everywhere except 6 hits, all inside ACCEPTED ADRs (`docs/adrs/005` ×4, `docs/adrs/002` ×1, `docs/adrs/006` ×1). Tester re-ran the grep and read all 6 hits in context: each is Context/Decision prose describing the state at the time that ADR was written (ADR-005 predates the move; ADR-006's own Context paragraph explicitly narrates the "today the code is graph-only" pre-state; ADR-002's Decision item is quoted verbatim from before #111). None reads as a live "go look here" pointer. `docs/glossary.md` (the current vocabulary) was fixed and is clean.
- [x] AST test: no module under `tree/memory/rag/` imports `tree.memory.graph` or `tree.memory.pipeline`; `tree/memory/rag/cleaning.py` still imports stdlib only (#106 invariant).
- [x] Unit test: the RAG loader's op builders reject any node type outside `RAG_NODE_TYPES` and never produce a `kind: edge` op.
- [x] `orchestrator._DEPLOYMENT_SPECS` has exactly 5 entries; every `entrypoint` path exists on disk and names a `@flow` (parametrised test over the specs, importing each module and checking `hasattr(module, fn_name)`).
- [x] `tests/unit/memory/` mirrors `src/tree/memory/` one-to-one (a test walks both trees: every `rag/*.py`/`graph/*.py` module has a `test_<name>.py`, except `__init__.py`/`types.py`).
- [x] `apps/memory/README.md` contains a `## Memory modes` heading, the strings `memory.mode`, `TREE_MEMORY__MODE`, `rag/` and `graph/` in Layout, and no `extraction.chunk_size`.
- [x] `.agents/skills/tree-memory/SKILL.md` contains no `todo` / `experienced` edge rows and documents `search_memory` for both modes; `.agents/skills/run-pipelines-e2e/SKILL.md` mentions `TREE_MEMORY__MODE`.
- [x] `make memory-format-check && make memory-lint-check && make memory-tests` green; test count ≥ the count after #110.
- [x] [HUMAN] rag-mode e2e evidence in `## Log`: `mongosh` output showing `edge` count 0, ≥1 `document`, ≥1 `chunk/parent` with `embedding: []`, ≥1 `chunk/child` with a 1024-length embedding; `make memory-query-graph QUERY=…` text output; an MCP `search_memory` response with `parents[0].document.title`.
- [x] [HUMAN] graphrag-mode e2e evidence in `## Log`: `mongosh` counts by `type` incl. `part_of`, `next`, ≥1 entity type; `make memory-query-graph QUERY=…` writes `.tree/graphs/<slug>-<stamp>.html`; MCP `query_memory` + `search_memory(visualize=True)` responses.

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

### [SWE] 2026-09-05 20:20 — Implementation

**Files modified**

Package move (pure `git mv` + import rewrites, NO behaviour change):
- `apps/memory/src/tree/memory/extraction/core.py` → `graph/extraction.py`; `add_entity.py`, `dedup.py`, `validation.py`, `judge.py`, `first_person_resolver.py`, `preference_supersession.py`, `sharding.py` → `graph/`; `extraction/` deleted.
- `apps/memory/src/tree/memory/query/{kgquery,nl_query,visualize}.py` → `graph/`; `query/__init__.py` (a `KGQuery` re-export nothing imported) deleted with the package.
- `apps/memory/src/tree/memory/{resolution,review,consolidation}/` → `graph/{resolution,review,consolidation}/`.
- `apps/memory/src/tree/memory/indexing/` — stale empty dir (only `__pycache__` after #108) removed.
- `apps/memory/src/tree/memory/graph/__init__.py` — docstring now describes the finished layout and points at the guard test.
- Import rewrites across 61 files (`tree.memory.extraction.core` → `tree.memory.graph.extraction`; `tree.memory.{extraction,query}` → `tree.memory.graph`; `tree.memory.{resolution,review,consolidation}` → `tree.memory.graph.…`), incl. `orchestrator.py` (the `dream-consolidation-all-users` entrypoint path), `scripts/query_graph.py`, `scripts/review_duplicates.py`, `mcp/{graph_tools,graph_app,dashboard_app}.py`, `entities/ontology.py` (+ its `tests/unit/entities/snapshots/ontology_schema.json` mirror), `config/app_config.py`, `memory/{pipeline,types}.py`, `sharding.py`.
- Tests moved to mirror: `tests/unit/memory/{extraction,query,resolution,review,consolidation}/` → `tests/unit/memory/graph/…`; `tests/unit/test_sharding.py` → `tests/unit/memory/graph/test_sharding.py`; `test_core.py` → `test_extraction.py`; `test_dedup_config.py` → `test_dedup.py` (so the mirror guard resolves it).

New tests:
- `apps/memory/tests/unit/memory/test_package_layout.py` (NEW) — the three structural guards: top level of `tree/memory/` is exactly the 6 entries + retired packages gone; AST walk asserting no `rag/` module imports `tree.memory.graph` or `tree.memory.pipeline`; every `rag/`+`graph/` module (recursive, minus `__init__.py`/`types.py`) has a mirroring `test_<name>.py`.
- `apps/memory/tests/unit/memory/graph/consolidation/test_meta_state.py` (NEW) — the mirror guard surfaced a real gap: `load_watermark` / `record_dream_run` had ZERO tests. Covers epoch-on-missing, no write on a miss, tenant-scoped `_id`, `last_run_at == run_start` (not `now()`), upsert, naive-datetime rejection.
- `apps/memory/tests/unit/memory/graph/resolution/test_base.py` (NEW) — same reason for `resolution/base.py`: `_no_match` envelope, `resolve_batch` ordering, and the `list(candidate_names)` materialisation that keeps a generator of candidates usable for the 2nd input.
- `apps/memory/tests/unit/memory/rag/test_load.py` — `TestRagNodeTypeGuard` strengthened: parametrised over EVERY `NodeType` outside `RAG_NODE_TYPES`, an edge-type-as-node-type probe, and "no op of a full hierarchy is an edge row" (`kind == "node"`, no `source_node_id`/`target_node_id`).
- `apps/memory/tests/unit/test_orchestrator.py` — `TestEveryEntrypointResolves`: parametrised over `_DEPLOYMENT_SPECS` (file exists / names a `prefect.Flow` / is the SAME object as `spec.flow`) + the count-is-5 pin; the existing entrypoint test also pins the new dream path.

Docs & skills:
- `apps/memory/README.md` — intro + "What this app contains" rewritten around `rag/` vs `graph/`; NEW `## Memory modes` section (switch, `TREE_MEMORY__MODE`, per-mode table of stages / writes / embeddings / retrieval / tools / CLI output, drop-and-reingest note); "Memory extraction" → "Memory pipeline" with the real stage names (`clean-and-chunk`, `embed-children`, `load-rag-rows`, then the graphrag stages); "Memory indexing" uses `embed-kg-nodes` / `ensure-kg-indexes` and the real filter paths; "Query CLI" documents the rag text output; Layout block redrawn (`pipeline.py`, `embedding_text.py`, `types.py`, `rag/`, `graph/`); Testing section points at the new layout guard.
- Repo-root `README.md` — Memory bullet and the step-6 pipeline comments now read `documents → clean → chunk → embed → memory collection (+ LLM nodes + edges in graphrag)`, with the query line noting HTML vs text.
- `.agents/skills/run-pipelines-e2e/SKILL.md` — new step 0 (pick the mode, drop `memory` when switching), an **IMPORTANT** callout that the Dockerized Prefect worker executes the MAIN checkout so a worktree/feature branch MUST be served with `make memory-serve-workflows` from that worktree, a step-3 verify section (mongosh counts + the rag text output vs the graphrag HTML) and a step-4 cleanup.
- `.agents/skills/tree-memory/SKILL.md` — tool table split by mode (6 vs 13) with a "don't work around a missing tool" rule; `search_memory` parameters + result shape per mode; `chunk` documented with subtypes `parent`/`child` + `parent_id`/`chunk_index`; stale `todo` / `experienced` edge rows replaced by the `related_to` + `semantic_type` umbrella (#029) and the missing `same_as` / `superseded_by` / POLE+O node types added.
- `apps/memory/src/tree/mcp/tools.py` — `ingest_url` / `ingest_file` / `ingest_conversation` / `search_web` docstrings no longer say "knowledge graph" unconditionally (flagged by the #110 Tester); they say "into memory" and note the graphrag extras.
- `docs/glossary.md` — ONE mechanical fix: the two `tree.memory.query.visualize` code pointers → `tree.memory.graph.visualize`. No term added, no definition changed.
- `docs/notes/slm-extraction-finetuning-spec.md`, `tutorials/2_4_1_serving_locally.md` — module paths follow the move.

**Tests**
- Unit: 2400 passing, 0 failing (`make memory-tests`) — up from 2311 after #110.
- Integration: N/A — no integration suite in this repo (unit + real-run e2e per `AGENTS.md`).

**Acceptance criteria**
- [x] Top level is exactly the 6 entries; `extraction/`, `indexing/`, `query/` gone — `tests/unit/memory/test_package_layout.py::TestTopLevelLayout::{test_top_level_holds_exactly_the_two_layers_and_three_modules,test_the_retired_packages_are_gone}` + the `find` in Evidence.
- [ ] **PARTIAL** — the grep is clean across `apps/memory/src`, `apps/memory/tests`, `apps/memory/scripts`, `apps/memory/deploy`, `.agents`, `README.md`, `apps/memory/README.md` and `docs/glossary.md`. 6 hits remain, all inside ACCEPTED ADRs: `docs/adrs/005` (4), `docs/adrs/002` (1), `docs/adrs/006` (1). I did NOT rewrite them — see Notes; PA call.
- [x] AST test — `…test_package_layout.py::TestRagNeverDependsOnGraph::{test_no_rag_module_imports_the_graph_layer,test_no_rag_module_imports_the_flow_module}` (parametrised over all 9 `rag/` modules); the stdlib-only half stays where the module is tested: `tests/unit/memory/rag/test_cleaning.py::TestModulePurity::test_cleaning_module_imports_only_stdlib`.
- [x] RAG loader guard — `tests/unit/memory/rag/test_load.py::TestRagNodeTypeGuard::{test_a_non_rag_node_type_raises,test_every_non_rag_node_type_of_the_ontology_is_rejected,test_the_edge_kind_is_rejected_like_any_other_foreign_type,test_no_op_of_a_full_hierarchy_is_an_edge_row}`.
- [x] `_DEPLOYMENT_SPECS` = 5, every entrypoint resolves — `tests/unit/test_orchestrator.py::TestEveryEntrypointResolves::*` (4 tests × 5 specs) + `test_memory_worker_entrypoint_points_at_the_one_pipeline_module`.
- [x] Tests mirror the modules — `…test_package_layout.py::TestTestsMirrorTheModules::test_module_has_a_mirroring_test_module` (23 params).
- [x] README strings — `## Memory modes` ×1, `TREE_MEMORY__MODE` ×3, `memory.mode` ×5, `rag/` + `graph/` in Layout, `extraction.chunk_size` ×0.
- [x] Skills — `tree-memory/SKILL.md` has no `todo`/`experienced` edge row and a per-mode `search_memory` table; `run-pipelines-e2e/SKILL.md` mentions `TREE_MEMORY__MODE` ×3.
- [x] `make memory-format-check && make memory-lint-check && make memory-tests` green; 2400 ≥ 2311.
- [x] [HUMAN] rag-mode e2e — Evidence below.
- [x] [HUMAN] graphrag-mode e2e — Evidence below.

**Evidence**

```
$ find apps/memory/src/tree/memory -maxdepth 1 -not -name __pycache__ | sort
apps/memory/src/tree/memory
apps/memory/src/tree/memory/__init__.py
apps/memory/src/tree/memory/embedding_text.py
apps/memory/src/tree/memory/graph
apps/memory/src/tree/memory/pipeline.py
apps/memory/src/tree/memory/rag
apps/memory/src/tree/memory/types.py

$ make memory-format-check && make memory-lint-check
266 files already formatted
All checks passed!

$ make pre-commit
prettier / ruff check / ruff format / biome check ....................Passed

$ make memory-tests
============================ 2400 passed in 22.23s =============================
```

RED CHECK — the three new structural guards fail for the right reason when the
invariant is broken (each break reverted immediately afterwards; suite back to
2400 green):

```
# (a) append `from tree.memory.graph import kgquery` to rag/chunking.py
# (b) revert the dream entrypoint to the pre-move path in orchestrator.py
# (c) move tests/unit/memory/rag/test_search.py away
$ uv run pytest tests/unit/memory/test_package_layout.py tests/unit/test_orchestrator.py -q
FAILED …test_package_layout.py::TestRagNeverDependsOnGraph::test_no_rag_module_imports_the_graph_layer[rag/chunking.py]
FAILED …test_package_layout.py::TestTestsMirrorTheModules::test_module_has_a_mirroring_test_module[rag/search.py]
FAILED …test_orchestrator.py::test_memory_worker_entrypoint_points_at_the_one_pipeline_module
FAILED …test_orchestrator.py::TestEveryEntrypointResolves::test_the_entrypoint_file_exists[dream-consolidation-all-users]
FAILED …test_orchestrator.py::TestEveryEntrypointResolves::test_the_entrypoint_names_a_flow_in_that_module[dream-consolidation-all-users]
FAILED …test_orchestrator.py::TestEveryEntrypointResolves::test_the_entrypoint_names_the_same_flow_the_spec_holds[dream-consolidation-all-users]
6 failed, 71 passed
```

LIVE E2E — local env (`make env-status` → local), Docker Mongo/mongot/Prefect,
user `paul@example.com` = `6a8ea9579a7aeb13175955c8`. The Dockerized
`tree-prefect-worker` was STOPPED for the whole run and the deployments served
FROM THIS WORKTREE, so every flow run executed this branch's code (restarted
afterwards).

**Deployment topology (User Story 2)** — identical in both modes:

```
$ TREE_MEMORY__MODE=rag make memory-serve-workflows
Your deployments are being served and polling for scheduled runs!
┌─────────────────────────────────────────────────────────────┐
│ data-etl-worker/data-etl-worker                             │
│ memory-extract-etl-worker/memory-extract-etl-worker         │
│ online-pipeline/online-pipeline                             │
│ offline-pipeline/offline-pipeline                           │
│ dream-consolidation-all-users/dream-consolidation-all-users │
└─────────────────────────────────────────────────────────────┘
```

**① rag mode**

```
$ mongosh … --eval 'db.getSiblingDB("tree").memory.drop()'      → true, count 0
$ TREE_MEMORY__MODE=rag make memory-run-pipeline MODE=online \
    SOURCE="https://www.decodingai.com/p/subagents-are-context-engineering" \
    USER_IDENTIFIER=paul@example.com
Submitted flow run 9c89f8a5-8bee-4457-b98e-ed701c19f809 … Finished in state Completed()
Done. Flow completed successfully.

$ mongosh … (group by kind/type/subtype)
TOTAL: 29
  node / chunk    / child   26
  node / chunk    / parent   2
  node / document / null     1
edges: 0                                   ← rag writes NO edges
documents: 1
parent _id: …:chunk:https://www.decodingai.com/p/subagents-are-context-engineering#parent-0
   | parent_id: …:document:https://…/subagents-are-context-engineering
   | chunk_index: 0 | embedding len: 0
   | title: From 1 Bloated Context Window to 6 Scoped Subagents
child  _id: …#parent-0#child-0
   | parent_id: …#parent-0 | chunk_index: 0 | embedding len: 1024
children with 1024-d vector: 26
parents with empty embedding: 2
document row embedding empty: 1

$ TREE_MEMORY__MODE=rag make memory-query-graph \
    QUERY="how do subagents help with context engineering" USER_IDENTIFIER=paul@example.com
INFO:tree.memory.rag.retrieval:Parent-document retrieval: 26 child hit(s) -> 2 parent(s), returning 2
[0.033] From 1 Bloated Context Window to 6 Scoped Subagents
    Every AI application that wraps an agent is a harness!
    In LangChain's Terminal-Bench experiment, changing only the harness (with the same model) …
    matched children: 17

[0.031] From 1 Bloated Context Window to 6 Scoped Subagents — mode: default
    ## mode: default
    You are the explore agent — a read-only subagent spawned to investigate one scoped question …
    matched children: 9
# TEXT output, NO file written: `.tree/graphs/` unchanged by this command.

$ TREE_MEMORY__MODE=rag make memory-serve-mcp USER_ID=6a8ea9579a7aeb13175955c8 TRANSPORT=streamable-http
$ uv run fastmcp list http://127.0.0.1:8000/mcp --auth none --json
n tools: 6 → ingest_conversation, ingest_file, ingest_url, scrape_web, search_memory, search_web
search_memory params: ['query', 'top_k']

$ uv run fastmcp call … search_memory query="how do subagents help with context engineering" top_k=2
n parents: 2
{'score': 0.03252, 'title': 'From 1 Bloated Context Window to 6 Scoped Subagents',
 'uri': 'https://www.decodingai.com/p/subagents-are-context-engineering',
 'matched_children': 6, 'content_chars': 12553,
 'keys': ['chunk_index','content','document','heading_path','matched_children','parent_id','score']}
{'score': 0.03128, 'title': 'From 1 Bloated Context Window to 6 Scoped Subagents', …}
embedding leaked: False
```

**② graphrag mode** (collection dropped again; a different fresh Substack URL,
since a duplicate `source_uri` makes `online-pipeline` short-circuit)

```
$ mongosh … --eval 'db.getSiblingDB("tree").memory.drop()'      → true, count 0
$ make memory-serve-workflows                                   # no mode override = graphrag
$ make memory-run-pipeline MODE=online \
    SOURCE="https://www.decodingai.com/p/the-coding-agent-loop" USER_IDENTIFIER=paul@example.com
Submitted flow run 3f3742a6-d04d-4652-81d8-e004e5b366b9 … Finished in state Completed()

$ mongosh … (group by kind/type/subtype)
TOTAL: 173
  nodes:  chunk/child 48 · chunk/parent 3 · document 1
          event/communication 1 · event/transaction 6 · fact 1
          object/software 6 · object/document 1 · object/project 2
          organization/company 5
  edges:  part_of 51 · next 47 · related_to 1
entity nodes with 1024-d embedding: 22
children with 1024-d embedding: 48
# part_of = 48 children→parent + 3 parents→document; next = 45 sibling children + 2 sibling parents.

$ BROWSER=/usr/bin/true make memory-query-graph \
    QUERY="what is the coding agent loop" USER_IDENTIFIER=paul@example.com
INFO:__main__:Result: 32 nodes, 30 edges
INFO:tree.memory.graph.visualize:Wrote self-contained graph HTML (32 nodes, 30 edges) to
  …/apps/memory/.tree/graphs/what-is-the-coding-agent-loop-20260905-170429.html

$ make memory-serve-mcp USER_ID=6a8ea9579a7aeb13175955c8 TRANSPORT=streamable-http
$ uv run fastmcp list … --json
n tools: 13 → deep_search_memory, ingest_conversation, ingest_file, ingest_url, memory_dashboard,
   query_memory, review_confirm, review_list_pending, review_reject, scrape_web, search_memory,
   search_web, visualize_memory_graph
search_memory params: ['max_hops', 'max_results', 'query', 'top_k', 'visualize']

$ uv run fastmcp call … query_memory query="how many chunk nodes are stored?"
[{"total_chunk_nodes": 51}]

$ uv run fastmcp call … search_memory query="what is the coding agent loop" top_k=3 max_hops=1 visualize=true
rows: 10 · kinds {node} · types {chunk}   ← the default mcp.max_results=10 truncates to the 10 seeds
resource link: graphs://what-is-the-coding-agent-loop-20260905-170512.html   (file written)

$ uv run fastmcp call … search_memory query="what is the coding agent loop" top_k=3 max_hops=1 max_results=60
rows: 52 · kinds {node: 27, edge: 25} · types {chunk 25, part_of 24, document 1, object 1, next 1}
```

Cleanup: both serve processes and both MCP servers stopped, the two
`.tree/graphs/what-is-the-coding-agent-loop-*.html` files removed,
`tree-prefect-worker` restarted. `git status` shows only source/test/doc changes.

**Notes**
- **AC 2 is deliberately partial.** The grep still matches three ACCEPTED ADRs — `005` (§Context references + the Decision line "the renderer lives in the memory domain (`tree.memory.query.visualize`)" + the mermaid label), `002` (§3's "stay in `tree.memory.extraction.sharding`") and `006` (its Context paragraph describing the PRE-ADR state, which is factually correct as-is). I am read-only on `docs/adrs/` and the project's own convention (ADR-006 §1 → "ADR-001's Status line GAINS a supersession note" rather than editing ADR-001's decision) is to annotate ADRs, not rewrite them. `docs/glossary.md` I did fix, since it is the canonical CURRENT vocabulary and a stale code pointer there is a broken link. If PA wants the grep fully clean, the mechanical fix is one `sed` over `docs/adrs/{002,005}` — say the word.
- The mirror guard is checked **src → test** only (every module needs a test), not the reverse: `tests/unit/memory/graph/{test_find_facts,test_preference_queries}.py` and `rag/{test_indexing_mongot_filter_paths,test_indexing_settings_vector_index_check}.py` are extra test modules for one source module each, which a bidirectional check would wrongly reject.
- Writing `test_meta_state.py` and `test_base.py` was NOT planned scope — the recursive mirror guard surfaced two modules that had never had a test. I chose to close the gap rather than weaken the guard to the AC's literal top-level `graph/*.py` glob.
- `graph/sharding.py` still re-exports the pure helpers from `tree.sharding` and keeps its function-scope `memory_indexing` import (the #108 cycle break) — moved verbatim, unchanged.
- `tests/unit/memory/rag/test_search.py`'s docstring pointer to the pre-ADR-006 `tests/unit/memory/query/test_core.py` was retargeted to `tests/unit/memory/graph/test_retrieval.py` (the module that inherited those assertions).
- `mcp.max_results` (default 10) truncates a graphrag `search_memory` response to the seeds before any edge appears. Pre-existing config behaviour, not introduced here, but it makes the default response look edge-free — recorded so it is not read as a regression of the expansion.

### [Tester] 2026-09-05 20:35 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check`, `make memory-lint-check`, `make pre-commit` all clean)
- Unit tests: 2400 passed / 0 failed (`make memory-tests`), ≥ 2311 after #110
- Integration tests: N/A — repo has no integration suite (unit + real-run e2e per `AGENTS.md`)
- Warnings: 0

**E2E adversarial pass**
- Happy path (rag, independent live run, worktree-served): dropped `memory`, `TREE_MEMORY__MODE=rag make memory-run-pipeline MODE=online SOURCE="https://www.decodingai.com/p/context-engineering-for-coding-agents" USER_IDENTIFIER=paul@example.com` → `Finished in state Completed()`; `mongosh` group-by shows 27 `chunk/child`, 3 `chunk/parent`, 1 `document`, edge count 0; a sampled parent has `embedding: []`, a sampled child has `embedding.length == 1024` → PASS
- Break path 1 (stale-state / duplicate source): first attempt reused the SWE's exact rag-mode URL (`subagents-are-context-engineering`); `online_pipeline` correctly detected the duplicate `documents` row from the SWE's earlier run (dropping `memory` does not drop `documents`) and short-circuited (`document is None` → no memory rows written, flow still reports `Completed()`). This is correct, documented dedup behaviour (the SWE's own note), not a defect — re-ran with a genuinely fresh URL and got real output → PASS (behaviour matches spec, not silent data corruption)
- Break path 2 (module reachability / import cycles): `uv run python -c` walked every submodule under `tree.memory.graph` and `tree.memory.rag` via `pkgutil.walk_packages` and imported each — all succeeded, no `ImportError`/cycle. Also bare-imported `tree.mcp.{tools,graph_tools,graph_app,dashboard_app}`, `tree.orchestrator`, `tree.entities.ontology`, `tree.config.app_config`, `tree.sharding` — all OK → PASS
- Break path 3 (MCP surface, hostile-ish/edge inputs on the live server): called `search_memory` with `top_k=2` over the freshly-ingested rag-mode data via `fastmcp call` — got `{"parents": [...]}` with exactly 2 entries, `parents[0].document.title` present, no `embedding` field leaked in the payload → PASS
- Break path 4 (git-mv history preservation, staged-but-uncommitted): `git add -A` (working tree only, not committed) then `git diff --cached -M --stat` — every moved file (e.g. `extraction/core.py => graph/extraction.py`) shows as a `{old => new}` rename with a small line-delta, confirming git's similarity heuristic will preserve `git log --follow` history once committed → PASS

**Acceptance criteria**
- [x] PASS — top level of `tree/memory/` is exactly `__init__.py, pipeline.py, embedding_text.py, types.py, rag/, graph/`; `extraction/`, `indexing/`, `query/` gone — `find apps/memory/src/tree/memory -maxdepth 1` re-run by Tester, matches exactly; `tests/unit/memory/test_package_layout.py::TestTopLevelLayout` (2 tests) pass
- [x] PASS (with note) — the grep for `tree.memory.extraction|indexing|query` is clean everywhere except 6 hits, all inside ACCEPTED ADRs. Tester re-ran the grep independently and read each hit in context: `docs/adrs/005_single_graph_rendering_stack.md` (×4, Context/Decision/diagram prose written when ADR-005 was accepted, before the #111 move), `docs/adrs/002_pipeline_concurrency_and_voyage_rate_limiting.md` (×1, a quoted Decision line from before #111), `docs/adrs/006_rag_graphrag_memory_modes.md` (×1, its own Context paragraph explicitly narrating "today the code is graph-only" — the pre-ADR-006 state, by design). None is a live "see X" pointer; ADR-006's own Context references confirm ADR-002 and ADR-005 are meant to stay "unchanged" (only their surfaces move). Per the project convention (only a Status-line supersession note may be added to an Accepted ADR), this is the correct outcome, not a partial pass. — SATISFIED
- [x] PASS — AST test: no `rag/` module imports `tree.memory.graph` or `tree.memory.pipeline`; `rag/cleaning.py` stdlib-only — `tests/unit/memory/test_package_layout.py::TestRagNeverDependsOnGraph::*` (18 params) + `tests/unit/memory/rag/test_cleaning.py::TestModulePurity` all pass; read the test bodies, they walk the AST correctly (`ast.Import`/`ast.ImportFrom`)
- [x] PASS — RAG loader op builders reject any node type outside `RAG_NODE_TYPES`, never emit `kind: edge` — `tests/unit/memory/rag/test_load.py::TestRagNodeTypeGuard::*` (read in full: parametrised over every non-RAG `NodeType`, an edge-type-as-node-type probe, and a "no op is `kind: edge`" assertion over a full parent+children hierarchy)
- [x] PASS — `orchestrator._DEPLOYMENT_SPECS` has exactly 5 entries, every entrypoint resolves to a `@flow` — `tests/unit/test_orchestrator.py::TestEveryEntrypointResolves::*` (4 tests × 5 specs, read in full: file-exists, `isinstance(attribute, prefect.Flow)`, `attribute is spec.flow`) + `test_the_topology_is_exactly_the_free_tier_five`; independently confirmed live via `make memory-serve-workflows` — Prefect registered exactly the 5 expected deployment names
- [x] PASS — `tests/unit/memory/` mirrors `src/tree/memory/` — `test_package_layout.py::TestTestsMirrorTheModules::test_module_has_a_mirroring_test_module` (parametrised, all pass); full file has 50 sub-tests, all green (`uv run pytest tests/unit/memory/test_package_layout.py -q` → `50 passed`)
- [x] PASS — README strings — `grep -c` confirms `## Memory modes` heading present, `memory.mode` ×4, `TREE_MEMORY__MODE` ×3, `rag/`+`graph/` present in the Layout block, `extraction.chunk_size` ×0 (and `extraction.chunk_` generally ×0)
- [x] PASS — `tree-memory/SKILL.md` has no standalone `todo`/`experienced` edge row (only `experienced_by` as a `semantic_type` value under the `related_to` umbrella, exactly as the spec requires) and documents `search_memory` for both modes in a side-by-side table; `run-pipelines-e2e/SKILL.md` mentions `TREE_MEMORY__MODE` ×3
- [x] PASS — `make memory-format-check && make memory-lint-check && make memory-tests` green, 2400 ≥ 2311 — re-run independently by Tester, same result
- [x] PASS — [HUMAN] rag-mode e2e — SWE's pasted evidence checked literally against every bullet (edge 0, 1 document, 2 parents `embedding: []`, 26 children @ 1024-d, CLI text output, MCP `search_memory` with `parents[0].document.title`) — all present. Tester ALSO ran an independent live rag-mode e2e (see Happy path above) with matching results (edge 0, parents `embedding: []`, children @ 1024-d, CLI text output, MCP `parents[0].document.title` present)
- [x] PASS — [HUMAN] graphrag-mode e2e — SWE's pasted evidence checked literally: counts by type incl. `part_of` (51), `next` (47), ≥1 entity type (event/fact/object/organization present), `.tree/graphs/what-is-the-coding-agent-loop-20260905-170429.html` written, `query_memory` response (`[{"total_chunk_nodes": 51}]`) and `search_memory(visualize=True)` response (resource link + row/kind/type breakdown) both present — all bullets literally satisfied

**Evidence**
```
$ find apps/memory/src/tree/memory -maxdepth 1 -not -name __pycache__ | sort
apps/memory/src/tree/memory
apps/memory/src/tree/memory/__init__.py
apps/memory/src/tree/memory/embedding_text.py
apps/memory/src/tree/memory/graph
apps/memory/src/tree/memory/pipeline.py
apps/memory/src/tree/memory/rag
apps/memory/src/tree/memory/types.py

$ grep -rn "tree.memory.extraction\|tree.memory.indexing\|tree.memory.query" apps/memory/src apps/memory/tests apps/memory/scripts apps/memory/deploy .agents docs README.md apps/memory/README.md
docs/adrs/005_single_graph_rendering_stack.md:8,14,50,82 (Context/Decision/diagram, pre-#111 state)
docs/adrs/002_pipeline_concurrency_and_voyage_rate_limiting.md:194 (Decision, pre-#111 state)
docs/adrs/006_rag_graphrag_memory_modes.md:20 (Context, explicitly the pre-ADR-006 state)

$ make memory-tests
============================ 2400 passed in 22.47s =============================

$ TREE_MEMORY__MODE=rag make memory-run-pipeline MODE=online SOURCE="https://www.decodingai.com/p/context-engineering-for-coding-agents" USER_IDENTIFIER=paul@example.com
Finished in state Completed()

$ mongosh ... (group by kind/type/subtype)
chunk/child: 27, chunk/parent: 3, document: 1, edge count: 0
parent embedding: [] | child embedding length: 1024

$ TREE_MEMORY__MODE=rag make memory-query-graph QUERY="what is context engineering for coding agents" ...
[0.033] Context Engineering for Coding Agents — ... matched children: 4   (TEXT output, no HTML file)

$ uv run fastmcp call http://127.0.0.1:8000/mcp --auth none search_memory query="..." top_k=2
n parents: 2, parents[0].document.title = "Context Engineering for Coding Agents", no embedding leaked

$ uv run python -c "import pkgutil; ... import every tree.memory.graph.* and tree.memory.rag.* submodule"
All graph/ and rag/ submodules imported successfully
```

**Other issues found**
- Stray untracked `.tree/graphs/how-does-memory-for-agents-work-20260905-163704.html` found in the worktree (gitignored, does not affect the diff or commit) — left in place since it predates this QA session and isn't part of this task's changes; PA/SWE may want to sweep it in a later cleanup pass.
- None of the AC-relevant code paths showed dead code, copy-paste smells, or missing logging beyond what's already noted in the SWE's own Notes (mirror guard is src→test only, `mcp.max_results` truncation is pre-existing).

**Cleanup performed by Tester**
- Stopped the Docker `tree-prefect-worker`, served workflows from the worktree in `rag` mode, ran one online-pipeline ingest, one CLI query, one MCP `search_memory` call, then: dropped the `memory` collection, killed the worktree-served orchestrator + MCP server processes, restarted `tree-prefect-worker` (confirmed `Up` afterwards), left `git status` with only the SWE's original source/test/doc changes (now fully staged via `git add -A`, nothing committed).

**VERDICT: PASS**
