---
id: 105-memory-mode-config-and-memory-collection
feature: rag-graphrag-modes
status: pending
---

# `memory.mode` config section + rename the `knowledge_graph` collection to `memory`

Tags: `config`, `entities`, `memory`, `docs`
Depends on: None
Blocks: #106, #107, #108, #109, #110, #111
Implements: ADR-006 (`rag-graphrag-modes`) — Decisions 1 and 5

## Scope

Two mechanical foundations every later task builds on. Nothing branches on the mode yet;
this task only makes the mode *readable* and gives the collection its mode-neutral name.

**1. `memory:` config section.** Add `MemoryConfig` to `AppConfig`
(`apps/memory/src/tree/config/app_config.py`) with exactly one field for now:
`mode: Literal["rag", "graphrag"] = "graphrag"`. Add the section to
`apps/memory/configs/default.yaml` (with a comment block explaining the two **Memory modes**:
`rag` = clean → chunk → embed → load → parent-document hybrid search; `graphrag` = the same
plus LLM entity extraction, resolution, dedup, structural/mentions edges and graph expansion
at retrieval) and to the frozen fixture `tests/unit/config/fixtures/frozen_config.yaml`.
`TREE_MEMORY__MODE=rag` overrides it through the existing `_apply_env_overrides` hatch — no
new mechanism. An invalid value (`TREE_MEMORY__MODE=hybrid`) fails `load_app_config()` with a
Pydantic `ValidationError` naming the two allowed values. Default is `graphrag` so an
unchanged checkout keeps today's behaviour.

**2. Collection rename `knowledge_graph` → `memory`, entry class `KnowledgeGraphEntry` →
`MemoryEntry`.**
- Module `tree/entities/knowledge_graph.py` → `tree/entities/memory.py`. Class
  `KnowledgeGraphEntry` → `MemoryEntry`; `Settings.name = "memory"`. `NodeType`, `EdgeType`,
  `ExtractorInfo`, `build_node_id`, `build_edge_id` keep their names (they move with the
  module). NO backward-compat alias/shim for the old class or module name — every import site
  is updated (CLAUDE.md: remove, don't add).
- Export ONE constant `MEMORY_COLLECTION = "memory"` from `tree.entities.memory` and delete
  every per-module `_KG_COLLECTION = "knowledge_graph"` copy (today 12: `extraction/core.py`,
  `extraction/pipeline.py`, `extraction/sharding.py`, `extraction/dedup.py`,
  `extraction/add_entity.py`, `indexing/core.py`, `query/core.py`, `query/nl_query.py`,
  `query/kgquery.py`, `review/core.py`, `consolidation/dream.py`, `entities/users.py`) —
  import the shared constant instead. `$graphLookup`/`$lookup` `from:` values and the
  `nl_query` system prompt (`## Collection: ...`, the "`from` MUST be" rule and
  `validate_pipeline`'s `from` check) read the constant.
- Export ONE constant `RAG_NODE_TYPES: frozenset[str] = frozenset({"document", "chunk"})`
  from `tree.entities.memory` — the closed set of node types the RAG layer writes. It is the
  single, explicit place that says "these rows are the RAG rows; every other node type and
  every edge is graph". Later tasks consume it (#108 loader, #109 child-search filter, #111
  "rag never writes anything else" test); this task only defines and documents it.
- `KnowledgeGraphMetaState` / collection `knowledge_graph_meta_state` (dream watermark) are
  intentionally UNCHANGED — graph-only state whose name stays accurate (ADR-006).
- Update every test that imports the old module/class or asserts the string
  `"knowledge_graph"` (`test_nl_query.py`, `test_pipeline.py` sentinel, `test_kgquery.py`,
  `test_dream.py`, `test_ontology.py`, `test_users.py`, resolution tests, …).
- Docs in this task: `apps/memory/README.md` (3 mentions), repo-root `README.md` (2),
  `docs/notes/deployment-runbook.md` (1), `docs/notes/conversations-storage-tradeoffs.md` (4),
  `scripts/check_db.py` (lists collections — no hardcoded name expected; verify). The
  `.agents/skills/search` + `bright-data-best-practices` hits are Google SERP fields, NOT ours
  — leave them. ADR-001's supersession note and the glossary edits land in the grooming commit.

No data migration: both modes start from scratch (documents stay; the old collection is
simply no longer read — operators drop it by hand if they want).

## Acceptance Criteria

- [ ] `load_app_config(frozen_config_path).memory.mode == "graphrag"` and the frozen fixture + `configs/default.yaml` both carry a `memory:` section with `mode: graphrag`.
- [ ] With `TREE_MEMORY__MODE=rag` in the environment, `load_app_config()` returns `memory.mode == "rag"`; with `TREE_MEMORY__MODE=hybrid` it raises a Pydantic `ValidationError` whose message contains both `'rag'` and `'graphrag'`.
- [ ] `tree.entities.memory` exports `MemoryEntry`, `MEMORY_COLLECTION == "memory"`, `RAG_NODE_TYPES == frozenset({"document", "chunk"})`, `NodeType`, `EdgeType`, `ExtractorInfo`, `build_node_id`, `build_edge_id`; `MemoryEntry.Settings.name == "memory"`; `tree/entities/knowledge_graph.py` no longer exists and `grep -rn "KnowledgeGraphEntry\|entities.knowledge_graph" apps/memory/src apps/memory/tests apps/memory/scripts` returns nothing.
- [ ] Every member of `RAG_NODE_TYPES` is a registered node type in `NODE_REGISTRY` and none of them is in `LLM_EXTRACTABLE_NODE_TYPES` (unit test).
- [ ] `grep -rn '"knowledge_graph"' apps/memory/src apps/memory/scripts` returns nothing; `grep -rn "_KG_COLLECTION" apps/memory/src` returns nothing (every reader imports `MEMORY_COLLECTION`).
- [ ] `build_nl_query_system_prompt()` contains `` `memory` `` as the collection name and `validate_pipeline` rejects a `$graphLookup` whose `from` is `"knowledge_graph"` with the message `... 'from' must be 'memory', got 'knowledge_graph'`.
- [ ] `tree.db.ALL_DOCUMENT_MODELS` contains `MemoryEntry` and NOT any class named `KnowledgeGraphEntry`; `KnowledgeGraphMetaState` is still registered under `knowledge_graph_meta_state`.
- [ ] A row inserted through `MemoryEntry(...).insert()` in the unit-test database lands in the `memory` collection (`await db.list_collection_names()` contains `"memory"` and not `"knowledge_graph"`).
- [ ] `grep -rn "knowledge_graph" README.md apps/memory/README.md docs/notes/deployment-runbook.md docs/notes/conversations-storage-tradeoffs.md` returns only prose that explicitly describes the OLD name as superseded (or nothing).
- [ ] `make memory-format-check && make memory-lint-check && make memory-tests` are green; the test count does not drop (renames, no deletions).

## User Stories

### Story: Operator switches the memory mode without editing YAML
1. Operator runs `TREE_MEMORY__MODE=rag uv --directory apps/memory run python -c "from tree.config.app_config import load_app_config; print(load_app_config().memory.mode)"`.
2. The command prints `rag`.
3. Operator re-runs without the variable and sees `graphrag`.

### Story: Operator mistypes the mode and is told the allowed values
1. Operator sets `TREE_MEMORY__MODE=hybrid` and boots any entry point (e.g. `make memory-check-db`).
2. Boot fails immediately with a `ValidationError` for `memory.mode` listing `'rag'` and `'graphrag'`.

### Story: Developer inspects the database after this task
1. Developer runs `make memory-check-db`.
2. The collection listing shows `documents`, `memory`, `users`, … — no `knowledge_graph` (on a fresh DB).

### Story: The NL query translator targets the renamed collection
1. Developer calls `build_nl_query_system_prompt()`.
2. The prompt's `## Collection:` heading names `memory` and the `$graphLookup` rule says `from` MUST be `"memory"`.
3. `validate_pipeline([{"$graphLookup": {"from": "knowledge_graph", ...}}], ...)` raises `PipelineValidationError`.

### Story: Book reader finds where "RAG rows" are defined
1. Reader opens `tree/entities/memory.py`.
2. `RAG_NODE_TYPES = frozenset({"document", "chunk"})` with its docstring tells them exactly which row types Chapter 4 writes; everything else is Chapter 8.

## Out of scope

- Any behaviour branching on `memory.mode` (pipelines #108, retrieval #109, MCP #110).
- Migrating rows from `knowledge_graph` to `memory` (both modes start from scratch).
- Renaming `KnowledgeGraphMetaState` / `knowledge_graph_meta_state`.
- Renaming Prefect deployments.
- A Beanie class hierarchy (`RagEntry` / `KnowledgeGraphEntry` subclasses) — rejected in ADR-006: the rag/graph split is by MODE, not by row; every RAG row is also a graph node in graphrag.

---

Blocked by: (none)

## Log
