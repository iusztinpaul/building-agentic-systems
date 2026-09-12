---
id: 105-memory-mode-config-and-memory-collection
feature: rag-graphrag-modes
status: done
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

- [x] `load_app_config(frozen_config_path).memory.mode == "graphrag"` and the frozen fixture + `configs/default.yaml` both carry a `memory:` section with `mode: graphrag`.
- [x] With `TREE_MEMORY__MODE=rag` in the environment, `load_app_config()` returns `memory.mode == "rag"`; with `TREE_MEMORY__MODE=hybrid` it raises a Pydantic `ValidationError` whose message contains both `'rag'` and `'graphrag'`.
- [x] `tree.entities.memory` exports `MemoryEntry`, `MEMORY_COLLECTION == "memory"`, `RAG_NODE_TYPES == frozenset({"document", "chunk"})`, `NodeType`, `EdgeType`, `ExtractorInfo`, `build_node_id`, `build_edge_id`; `MemoryEntry.Settings.name == "memory"`; `tree/entities/knowledge_graph.py` no longer exists and `grep -rn "KnowledgeGraphEntry\|entities.knowledge_graph" apps/memory/src apps/memory/tests apps/memory/scripts` returns nothing.
- [x] Every member of `RAG_NODE_TYPES` is a registered node type in `NODE_REGISTRY` and none of them is in `LLM_EXTRACTABLE_NODE_TYPES` (unit test).
- [x] `grep -rn '"knowledge_graph"' apps/memory/src apps/memory/scripts` returns nothing; `grep -rn "_KG_COLLECTION" apps/memory/src` returns nothing (every reader imports `MEMORY_COLLECTION`).
- [x] `build_nl_query_system_prompt()` contains `` `memory` `` as the collection name and `validate_pipeline` rejects a `$graphLookup` whose `from` is `"knowledge_graph"` with the message `... 'from' must be 'memory', got 'knowledge_graph'`.
- [x] `tree.db.ALL_DOCUMENT_MODELS` contains `MemoryEntry` and NOT any class named `KnowledgeGraphEntry`; `KnowledgeGraphMetaState` is still registered under `knowledge_graph_meta_state`.
- [x] A row inserted through `MemoryEntry(...).insert()` in the unit-test database lands in the `memory` collection (`await db.list_collection_names()` contains `"memory"` and not `"knowledge_graph"`).
- [x] `grep -rn "knowledge_graph" README.md apps/memory/README.md docs/notes/deployment-runbook.md docs/notes/conversations-storage-tradeoffs.md` returns only prose that explicitly describes the OLD name as superseded (or nothing).
- [x] `make memory-format-check && make memory-lint-check && make memory-tests` are green; the test count does not drop (renames, no deletions).

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

### [SWE] 2026-09-05 13:14 — Implementation

**Files modified**

Config (`memory.mode`):
- `apps/memory/src/tree/config/app_config.py` — new `MemoryConfig` (`mode: Literal["rag","graphrag"] = "graphrag"`), wired as `AppConfig.memory`; no new override mechanism (the existing `_apply_env_overrides` hatch reaches it).
- `apps/memory/configs/default.yaml` — `memory: mode: graphrag` + the comment block explaining both modes.
- `apps/memory/tests/unit/config/fixtures/frozen_config.yaml` — same section, values-only.
- `apps/memory/tests/unit/config/test_app_config.py` — `TestMemoryModeConfig` (7 tests).

Rename (`knowledge_graph` → `memory`):
- `apps/memory/src/tree/entities/knowledge_graph.py` → `apps/memory/src/tree/entities/memory.py` (`git mv`) — `KnowledgeGraphEntry` → `MemoryEntry`, `Settings.name = MEMORY_COLLECTION`, new module docstring; adds `MEMORY_COLLECTION = "memory"` and `RAG_NODE_TYPES = frozenset({"document","chunk"})`, both with docstrings. No compat shim.
- 13 modules dropped their private `_KG_COLLECTION = "knowledge_graph"` copy and now import `MEMORY_COLLECTION`: `entities/users.py`, `memory/extraction/{core,pipeline,sharding,dedup,add_entity,preference_supersession}.py`, `memory/indexing/core.py`, `memory/query/{core,nl_query,kgquery}.py`, `memory/review/core.py`, `memory/consolidation/dream.py`. (The task listed 12; `extraction/preference_supersession.py` was a 13th copy.)
- `apps/memory/src/tree/db.py`, `entities/__init__.py` — register/export `MemoryEntry`; `KnowledgeGraphMetaState` untouched.
- 24 further src modules — import-site + prose updates only (`tree.entities.memory`, ``memory`` collection).
- `apps/memory/tests/unit/entities/test_knowledge_graph.py` → `test_memory.py` (`git mv`) + `TestMemoryCollectionName` / `TestRagNodeTypes` (12 tests).
- `apps/memory/tests/unit/memory/query/test_nl_query.py` — pipelines read `MEMORY_COLLECTION`; prompt assertion is now `` `memory` `` + "no `knowledge_graph`"; new `test_graphlookup_old_collection_name_raises`.
- `apps/memory/tests/unit/memory/extraction/test_pipeline.py` — sentinel reads `MEMORY_COLLECTION`.
- `apps/memory/tests/unit/entities/snapshots/ontology_schema.json` — regenerated (one docstring cross-reference; EOF style preserved).
- Docs: `README.md` (2), `apps/memory/README.md` (3), `docs/notes/deployment-runbook.md` (1), `docs/notes/conversations-storage-tradeoffs.md` (4). `apps/memory/scripts/check_db.py` verified — it lists collections dynamically, no hardcoded name.

**Tests**
- Unit: 1960 passing, 0 failing (baseline before this task: 1940 — +20, no deletions).
- Integration: N/A — this repo has no integration suite by design (AGENTS.md); e2e done by running the real entry points, below.

**Acceptance criteria**
- [x] frozen fixture + `configs/default.yaml` carry `memory: mode: graphrag` — `tests/unit/config/test_app_config.py::TestMemoryModeConfig::test_memory_mode_is_graphrag_in_frozen_config` and `::test_memory_mode_is_graphrag_in_default_yaml`
- [x] `TREE_MEMORY__MODE=rag` → `rag`; `hybrid` → `ValidationError` naming both values — `::test_memory_mode_env_override_selects_rag`, `::test_unknown_memory_mode_raises_naming_both_allowed_values`
- [x] `tree.entities.memory` exports + `Settings.name` + old module gone + grep empty — `tests/unit/entities/test_memory.py::TestMemoryCollectionName::{test_memory_collection_constant,test_entry_settings_name_is_the_shared_constant}` and the greps in Evidence
- [x] every `RAG_NODE_TYPES` member registered, none LLM-extractable — `::TestRagNodeTypes::{test_every_rag_node_type_is_registered,test_no_rag_node_type_is_llm_extractable}`
- [x] no `"knowledge_graph"` literal / `_KG_COLLECTION` in `src` — greps in Evidence
- [x] prompt says `` `memory` ``; validator rejects the old name with the exact message — `tests/unit/memory/query/test_nl_query.py::{test_contains_collection_name,test_graphlookup_old_collection_name_raises}`
- [x] `ALL_DOCUMENT_MODELS` has `MemoryEntry`, nothing targets `knowledge_graph`, meta-state unchanged — `::TestMemoryCollectionName::{test_memory_entry_is_registered_with_beanie,test_no_registered_model_still_targets_the_old_collection,test_dream_watermark_collection_is_deliberately_unchanged}`
- [x] an inserted row lands in `memory` — `::TestMemoryCollectionName::test_inserted_row_lands_in_the_memory_collection`
- [x] docs grep clean
- [x] format-check + lint-check + tests green, count did not drop

**Evidence**

```
$ make memory-tests
============================ 1960 passed in 16.42s =============================
   (baseline on this branch before the task: 1940 passed)

$ make memory-format-check && make memory-lint-check
248 files already formatted
All checks passed!

$ make pre-commit
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed
```

Acceptance greps (all from the repo root, all empty):

```
$ grep -rn "KnowledgeGraphEntry\|entities.knowledge_graph" apps/memory/src apps/memory/tests apps/memory/scripts
$ echo $?
1
$ grep -rn '"knowledge_graph"' apps/memory/src apps/memory/scripts
$ grep -rn "_KG_COLLECTION" apps/memory/src
$ grep -rn "knowledge_graph" README.md apps/memory/README.md docs/notes/deployment-runbook.md docs/notes/conversations-storage-tradeoffs.md
$ ls apps/memory/src/tree/entities/knowledge_graph.py
ls: apps/memory/src/tree/entities/knowledge_graph.py: No such file or directory
```

End-to-end (env `local`, real Mongo via `make local-start`):

Story 1 — operator switches the mode without editing YAML:
```
$ TREE_MEMORY__MODE=rag uv --directory apps/memory run python -c "from tree.config.app_config import load_app_config; print(load_app_config().memory.mode)"
rag
$ uv --directory apps/memory run python -c "from tree.config.app_config import load_app_config; print(load_app_config().memory.mode)"
graphrag
```

Story 2 — mistyped mode is refused at boot (see Notes on the command the story names):
```
$ TREE_MEMORY__MODE=hybrid uv --directory apps/memory run python -c "import tree.mcp.server"
pydantic_core._pydantic_core.ValidationError: 1 validation error for AppConfig
memory.mode
  Input should be 'rag' or 'graphrag' [type=literal_error, input_value='hybrid', input_type=str]
```

Story 3 — a fresh database gets `memory`, never `knowledge_graph` (scratch DB `e2e_105_fresh`, dropped after):
```
MEMORY_COLLECTION = memory
collections on a fresh DB: ['documents', 'extraction_dropped_fields', 'extraction_rejections', 'knowledge_graph_meta_state', 'memory', 'users']
has 'memory': True
has 'knowledge_graph': False
```

Story 4 — the NL query translator targets the renamed collection:
```
PROMPT> ## Collection: `memory`
PROMPT> - For `$graphLookup`, the `from` field MUST be `"memory"`.
contains 'knowledge_graph': False
REJECTED> $graphLookup 'from' must be 'memory', got 'knowledge_graph'
```

Smoke-import of every renamed module (CLI, MCP server + tools, orchestrator, all three pipelines, query/review/consolidation, entities):
```
imported 15 modules OK
memory.mode = graphrag
MEMORY_COLLECTION = memory | Settings.name = memory
RAG_NODE_TYPES = ['chunk', 'document']
```

**Notes**
- **Story 2's example command does not exercise the config, by design.** `make memory-check-db` (`scripts/check_db.py`) imports only `tree.config.settings`; it is a connectivity probe and never calls `load_app_config()`, so `TREE_MEMORY__MODE=hybrid make memory-check-db` succeeds. Every entry point that DOES read app config (MCP server, all Prefect flows, the CLI) fails at import with the `ValidationError` above, because `app_config = load_app_config()` runs at module import. I did not add an unused `app_config` import to `check_db.py` purely to make that one example fail — that would be dead code in an entry-point script. Flagging for the PA in case the story's example should be re-worded to a config-reading entry point.
- **13 private copies, not 12.** The task listed 12 `_KG_COLLECTION` definitions; `memory/extraction/preference_supersession.py` had a 13th. All 13 are gone.
- **`RAG_NODE_TYPES` is defined and documented only.** Nothing consumes it yet — that is #108/#109/#111, per this task's Out of scope.
- **The local dev database still holds the old `knowledge_graph` collection** (143 docs, seen in `make memory-check-db`). Expected: ADR-006 ships no migration; both modes start from scratch and operators drop the old collection by hand. The "no `knowledge_graph`" assertion is therefore verified on a fresh database (Story 3) and in the unit-test database.
- **Snapshot regenerated, not hand-edited.** `tests/unit/entities/snapshots/ontology_schema.json` changed on exactly one line — the `fact` node type's docstring cross-reference to the renamed class. Regenerated via `get_ontology_schema()` (the file has no trailing newline upstream; that style is preserved so the diff is one line).
- **Two class names in `test_memory.py` were renamed for consistency** (`TestKnowledgeGraphSettingsIndexes` → `TestMemorySettingsIndexes`, `TestKnowledgeGraphCommonColumns` → `TestMemoryEntryCommonColumns`) so the acceptance grep is clean of the old name.
- **Environment setup done in this worktree** (not committed, all gitignored): copied `.env` / `.env.prod` from the main checkout, ran `uv sync --extra local-models` (the suite's model tests need it), and started the shared Docker infra. `make local-start` conflicts on the hardcoded container names already owned by the main checkout's compose project, so the existing `tree-mongodb` / `tree-mongot` / `tree-prefect-*` containers were started instead. `make env-status` → `local` throughout.

### [Tester] 2026-09-05 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check`, `make memory-lint-check`, `make pre-commit` all clean)
- Unit tests: 1960 passed / 0 failed (re-ran twice, stable; baseline before task 1940 — count did not drop)
- Integration tests: N/A — no integration suite by design (AGENTS.md); verified via real Mongo e2e instead
- Warnings: 0 (project `filterwarnings` in `apps/memory/pyproject.toml:67-70` pre-dates this task and is unrelated to this change)

**E2E adversarial pass**
- Happy path: `uv --directory apps/memory run python -c "from tree.config.app_config import load_app_config; print(load_app_config().memory.mode)"` → `graphrag` (default); with `TREE_MEMORY__MODE=rag` → `rag` (PASS)
- Break path 1 (malformed input: `TREE_MEMORY__MODE=hybrid`): `load_app_config()` → `pydantic_core._pydantic_core.ValidationError: ... memory.mode / Input should be 'rag' or 'graphrag'` — message contains both allowed values as required (PASS)
- Break path 2 (boundary inputs: uppercase `RAG`, empty string `""`): both rejected with the same `ValidationError` naming `'rag'`/`'graphrag'` — no silent case-folding or empty-string fallback (PASS)
- Break path 3 (hostile input to `validate_pipeline`): `$graphLookup {from: "knowledge_graph"}` → `PipelineValidationError: $graphLookup 'from' must be 'memory', got 'knowledge_graph'` (exact AC wording); `$graphLookup {from: "memory; DROP"}` similarly rejected; `$out` stage rejected (`not allowed`); empty pipeline rejected (`Pipeline is empty`) (PASS)
- Break path 4 (state edge — real Mongo insert, not the unit-test fixture DB): wrote a `MemoryEntry` via Beanie into a fresh scratch database (`e2e_105_qa_*`, dropped after) — `list_collection_names()` returned `['memory']` only, no `knowledge_graph` (PASS)
- Break path 5 (fan-out over every real entry point with `TREE_MEMORY__MODE=hybrid`, not just the one script in the story): `scripts/{run_pipeline,run_data_pipeline,run_memory_pipeline,run_indexing_pipeline,query_graph}.py` and `scripts/serve_mcp.py` (when actually invoked, not just imported — lazy-imports `tree.mcp.server` inside `main()`) all fail fast with the same `ValidationError` before doing any work. `scripts/{check_db,signup,scrape_web,search_web,review_duplicates,run_dream_consolidation}.py` do NOT fail, because none of them import anything that touches `app_config` — this is pre-existing lazy-singleton config architecture (`tree/config/app_config.py:470`), not a regression from this task; every module that reads ANY config field (not just `memory.mode`) has always failed the same way on ANY invalid config value. See Notes below on Story 2's wording.

**Acceptance criteria**
- [x] PASS — `load_app_config(frozen_config_path).memory.mode == "graphrag"`; fixture + `configs/default.yaml` carry `memory: mode: graphrag` — `tests/unit/config/test_app_config.py::TestMemoryModeConfig::{test_memory_mode_is_graphrag_in_frozen_config,test_memory_mode_is_graphrag_in_default_yaml}` pass; `git diff` on both YAML files confirmed
- [x] PASS — `TREE_MEMORY__MODE=rag` → `rag`; `TREE_MEMORY__MODE=hybrid` → `ValidationError` naming both `'rag'` and `'graphrag'` — reproduced live (see Break path 1); `::test_memory_mode_env_override_selects_rag`, `::test_unknown_memory_mode_raises_naming_both_allowed_values` pass
- [x] PASS — `tree.entities.memory` exports `MemoryEntry`, `MEMORY_COLLECTION == "memory"`, `RAG_NODE_TYPES == frozenset({"document","chunk"})`, `NodeType`, `EdgeType`, `ExtractorInfo`, `build_node_id`, `build_edge_id`; `MemoryEntry.Settings.name == "memory"`; module gone; grep clean — `grep -rn "KnowledgeGraphEntry\|entities.knowledge_graph" apps/memory/src apps/memory/tests apps/memory/scripts` → no matches (exit 1); `ls apps/memory/src/tree/entities/knowledge_graph.py` → No such file; `tests/unit/entities/test_memory.py::TestMemoryCollectionName::*` (6 tests) pass
- [x] PASS — every `RAG_NODE_TYPES` member registered in `NODE_REGISTRY`, none in `LLM_EXTRACTABLE_NODE_TYPES` — reproduced live via `python -c` (registered: `{document: True, chunk: True}`, llm-extractable overlap: `frozenset()`); `TestRagNodeTypes::{test_every_rag_node_type_is_registered,test_no_rag_node_type_is_llm_extractable}` (4 parametrized cases) pass
- [x] PASS — no `"knowledge_graph"` literal / `_KG_COLLECTION` in `src`/`scripts` — both greps return no matches (exit 1)
- [x] PASS — `build_nl_query_system_prompt()` names `` `memory` `` as `## Collection:`; `validate_pipeline` rejects `$graphLookup {from: "knowledge_graph"}` with `"'from' must be 'memory', got 'knowledge_graph'"` — reproduced live (see Break path 3); `test_nl_query.py::{test_contains_collection_name,test_graphlookup_old_collection_name_raises}` pass
- [x] PASS — `tree.db.ALL_DOCUMENT_MODELS` contains `MemoryEntry`, no `KnowledgeGraphEntry`; `KnowledgeGraphMetaState` still registered as `knowledge_graph_meta_state` — read `apps/memory/src/tree/db.py:9-17` directly; `TestMemoryCollectionName::{test_memory_entry_is_registered_with_beanie,test_no_registered_model_still_targets_the_old_collection,test_dream_watermark_collection_is_deliberately_unchanged}` pass
- [x] PASS — a row inserted via `MemoryEntry(...).insert()` lands in `memory` — reproduced live against a fresh real Mongo database, not just the test fixture DB (see Break path 4); `TestMemoryCollectionName::test_inserted_row_lands_in_the_memory_collection` also passes
- [x] PASS — docs grep clean — `grep -rn "knowledge_graph" README.md apps/memory/README.md docs/notes/deployment-runbook.md docs/notes/conversations-storage-tradeoffs.md` returns no matches; manually read every doc diff, all replacements are contextually correct (README.md, apps/memory/README.md, deployment-runbook.md, conversations-storage-tradeoffs.md)
- [x] PASS — `make memory-format-check && make memory-lint-check && make memory-tests` all green; test count 1960 (up from 1940 baseline, no deletions)

**Evidence**
```
$ make memory-tests
============================ 1960 passed in 15.05s =============================

$ TREE_MEMORY__MODE=hybrid uv --directory apps/memory run python -c "from tree.config.app_config import load_app_config; print(load_app_config().memory.mode)"
pydantic_core._pydantic_core.ValidationError: 1 validation error for AppConfig
memory.mode
  Input should be 'rag' or 'graphrag' [type=literal_error, input_value='hybrid', input_type=str]

$ grep -rn "KnowledgeGraphEntry\|entities.knowledge_graph" apps/memory/src apps/memory/tests apps/memory/scripts ; echo exit:$?
exit:1
$ grep -rn '"knowledge_graph"' apps/memory/src apps/memory/scripts ; echo exit:$?
exit:1
$ grep -rn "_KG_COLLECTION" apps/memory/src ; echo exit:$?
exit:1
$ grep -rn "knowledge_graph" README.md apps/memory/README.md docs/notes/deployment-runbook.md docs/notes/conversations-storage-tradeoffs.md ; echo exit:$?
exit:1

$ uv --directory apps/memory run python <e2e insert script>
collections: ['memory']
has memory: True
has knowledge_graph: False
```

**Other issues found**
- **SWE flag (1) — verdict: spec/story-wording gap, not a code defect.** User Story 2 says an operator "boots any entry point (e.g. `make memory-check-db`)" and expects a `ValidationError`. Verified: `check_db.py` never fails with `TREE_MEMORY__MODE=hybrid` (`make memory-check-db` runs to completion and lists collections, including the stale `knowledge_graph`). I extended the SWE's own check across every script in `apps/memory/scripts/`: 6 of 12 (`check_db`, `signup`, `scrape_web`, `search_web`, `review_duplicates`, `run_dream_consolidation`) don't fail because none of them transitively import `tree.config.app_config` — they only need `tree.config.settings` (env-only) or no config at all. This is consistent with the pre-existing lazy-singleton config pattern (`apps/memory/src/tree/config/app_config.py:470`) that predates this task: ANY invalid config field (not just `memory.mode`) has always only broken entry points that actually read `app_config`. Forcing every script to eagerly import `app_config` to satisfy the story's literal wording would add a dead import to scripts that have no other reason to touch memory config — exactly what CLAUDE.md tells us not to do. The binding Acceptance Criteria (checkbox 2) use `load_app_config()` directly, not `make memory-check-db`, and that criterion is fully satisfied. Recommend a follow-up doc fix: reword Story 2's example to a config-reading entry point (e.g. `make memory-run-memory-pipeline` or `uv run python scripts/query_graph.py`) rather than `make memory-check-db`. Not blocking.
- **SWE flag (2) (`RAG_NODE_TYPES` has no consumer yet) confirmed correct and in scope** — task's "Out of scope" section explicitly defers consumption to #108/#109/#111.
- **SWE flag (3) (stale local dev DB still has `knowledge_graph`, 143 docs) confirmed correct and by design** — ADR-006 ships no migration; verified the "no `knowledge_graph`" assertions are scoped to fresh databases (Story 3, and my independent scratch-DB insert), not the shared dev DB.
- No `print()` calls introduced (checked full `src` diff). No new function signatures were added (pure rename/constant-extraction), so no new typing gaps. `entities/__init__.py` exports updated correctly. `ontology_schema.json` snapshot diff is exactly the one expected docstring cross-reference line — verified by reading the diff and cross-checking the source docstring in `entities/ontology.py:628`.

**VERDICT: PASS**

### [PA] 2026-09-05 20:32 — Acceptance Review

**VERDICT: REJECT** (feature-level verdict for PR #41, `rag-graphrag-modes`)

Nothing in this task is defective from the user's POV: `TREE_MEMORY__MODE`, the `ValidationError` naming both values, `MemoryEntry` / `MEMORY_COLLECTION` / `RAG_NODE_TYPES`, and the `memory` collection all match ADR-006 §1/§5 and the glossary. Story 2's example entry point (`make memory-check-db`) never reads app config — a spec-wording slip recorded in the rollup's "Not in this rollup" list; no code change. Rollup Issue 5 (help-text copy that still says "knowledge graph" for the memory pipeline) touches the docs this task started renaming.

Filed ONE rollup task for the whole feature: `tasks/112-pa-rejection-rag-graphrag-modes.md` (8 issues). Pipeline re-runs from the inner loop with the rollup task; on green, re-run acceptance on this task.

### [PA] 2026-09-05 23:20 — Acceptance Review (round 2)

**VERDICT: ACCEPT** (feature-level verdict for PR #41, `rag-graphrag-modes`, HEAD `08cd632`)

Rollup Issue 5 landed: `make help` for `run-memory-pipeline` / `query-graph`, the `run_memory_pipeline.py` docstring and the README now describe the `memory` collection with the rag/graphrag split instead of "knowledge graph". Mode config, `MemoryEntry`, `MEMORY_COLLECTION` and the `ValidationError` naming both modes unchanged and still correct.

Rollup `tasks/done/112-pa-rejection-rag-graphrag-modes.md` implemented and Tester-PASSED (round 2). Hand off to the PR Reviewer.
