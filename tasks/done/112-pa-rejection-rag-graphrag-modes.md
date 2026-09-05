---
id: 112-pa-rejection-rag-graphrag-modes
feature: rag-graphrag-modes
status: done
---

# [PA rejection] Modular memory: vanilla RAG mode and GraphRAG mode over one `memory` collection

Tags: `rollup`, `pa-rejection`, `memory`, `rag`, `graph`, `docs`
Refs: `tasks/done/105-memory-mode-config-and-memory-collection.md` … `tasks/done/111-memory-graph-package-docs-and-e2e.md` (PR #41, branch `feat/rag-graphrag-modes`)
Implements: ADR-006 (no new decision — this task closes gaps between ADR-006 and what shipped)

## Scope

The seven tasks of `rag-graphrag-modes` PASSED automated QA but failed the user-perspective
acceptance review. The SWE fixes every issue below in ONE coordinated pass on the existing
branch, then hands back to the Tester (full pipeline re-runs from QA). No new architecture, no
new knobs, no new deployments — every fix is a small correction inside code this feature
already owns. Glossary terms are canonical (`docs/glossary.md`): **Memory mode**, **Parent
chunk**, **Child chunk**, **Clean step**, **Contextual header**, **Parent-document
retrieval**, `memory` collection, `parent_id`, `subtype: parent | child`, `chunk_index`.

Issues 1–5 are product defects (an ADR requirement not implemented, a broken user story, content
corruption, misleading agent-facing copy). Issues 6–8 are the Testers' follow-ups that are cheap
enough to fold into the same pass rather than open a second cycle.

## Acceptance Criteria

- [x] Issue 1: `build_nl_query_system_prompt()` contains the strings `subtype`, `parent_id` and `chunk_index` in the "Node document shape" section, states that `chunk` rows carry `subtype` `"parent"` or `"child"`, that `parent_id` is the `_id` of the parent chunk (for a child) or of the `document` row (for a parent), and that `chunk_index` is the 0-based sibling position; a unit test in `tests/unit/memory/graph/test_nl_query.py::TestSystemPrompt` asserts all three field names are present.
- [x] Issue 2: `clean-and-chunk` (task ①) takes the chunking config as an explicit task parameter so it is part of the `INPUTS` cache key; a unit test runs `clean_and_chunk_task` (or its `.fn`) on the same `Document` under `strategy=recursive` and `strategy=fixed_tokens` and asserts the two `ChunkedDocument` payloads differ AND that the task's computed cache key differs; the task's docstring no longer claims a cache-key property the code does not have.
- [x] Issue 3: `clean_text` leaves the CONTENT of fenced code blocks (``` ``` ``` and `~~~` fences, CommonMark unclosed-fence rule included) byte-identical apart from CRLF→LF and trailing-whitespace stripping: a fixture with a ```` ```python ```` block containing `#comment` and 4-space-indented lines comes out with `#comment` unchanged and indentation intact, while an `#Heading` line OUTSIDE the fence still becomes `# Heading`; `clean_text` stays idempotent on that fixture and `cleaning.py` still passes the stdlib-only AST test. Fence detection has ONE definition shared by `rag/cleaning.py` and `rag/chunking.py` (chunking imports it from cleaning — rag→rag, no new dependency).
- [x] Issue 4: `.agents/skills/tree-memory/SKILL.md` no longer says ingestion "Returns a JSON summary with node/edge counts" or asks the agent to "Confirm what was extracted (node/edge counts)"; it states the actual contract — `ingest_*` returns `{"status": <Prefect state>, "flow_run_id": ...}` immediately, memory is written out-of-band, and the agent confirms by a follow-up `search_memory` — in both modes.
- [x] Issue 5: `grep -n "knowledge graph" apps/memory/Makefile apps/memory/scripts/run_memory_pipeline.py` returns nothing for the `run-memory-pipeline` / `query-graph` help lines and the script docstring (they describe the `memory` collection / the mode-dependent output); `grep -n "six-task\|into the graph\|to build the graph" apps/memory/README.md` returns nothing. The `graphrag`-only modules (`graph_tools.py`, `graph_app.py`, `dashboard_app.py`, `_GRAPHRAG_INSTRUCTIONS`) keep "knowledge graph" — it is correct there.
- [x] Issue 6: `retrieve_parents(..., top_k=0)` and `top_k=-1` return `RetrievalResult(parents=[])` WITHOUT calling `hybrid_search` and WITHOUT logging "Vector search unavailable" / "Text search unavailable" (assert on caplog); rag `search_memory(query="   ", top_k=3)` returns `{"error": "invalid_input", "detail": ...}` without calling the embedding model.
- [x] Issue 7: `group_children_by_parent` sorts children by `(-score, child_id)` and parents by `(-best_score, parent_id)`; a unit test with two children of byte-identical score under one parent, fed in both input orders, returns the same `matched_children` order.
- [x] Issue 8: `fixed_tokens` never emits U+FFFD: splitting a text of 300 `🧠` characters with `parent.size=5, child.size=2` yields chunks whose concatenation contains no `�` and re-encodes to the original token sequence; the existing `TestFixedTokensStrategy` tests still pass. NO chunk may exceed its configured `size` in tokens (Tester QA 2026-09-05: violated — fixed in SWE round 2, see Log).
- [x] `make memory-format-check && make memory-lint-check && make memory-tests` green; test count ≥ 2400.
- [x] Tester re-runs full QA suite and PASSES (including one live rag-mode re-ingest of the SAME document after flipping `TREE_MEMORY__CHUNKING__STRATEGY=fixed_tokens`, showing the parent count / `heading_path` change — Issue 2's user story).
- [ ] PA re-runs acceptance review on the original tasks and ACCEPTS.

## Issues (detail)

### 1. `query_memory` cannot see the chunk hierarchy — `apps/memory/src/tree/memory/graph/nl_query.py:115-121`
- **What the user experiences (wrong):** In `graphrag`, a reader asks `query_memory("how many parent chunks does the coding-agent-loop article have?")`. The system prompt's "Node document shape" lists `_id, kind, type, name, properties, embedding` only — no `subtype`, `parent_id`, `chunk_index` — so the LLM cannot write `{"$match": {"type": "chunk", "subtype": "parent"}}` and either counts children+parents together (the e2e answer `total_chunk_nodes: 51` is exactly that) or invents field names.
- **What the spec / good UX implies (right):** ADR-006 § Consequences: "The `nl_query` prompt must describe `parent_id`/`chunk_index`/`subtype` so generated pipelines can walk the hierarchy." No task carried it (grooming omission — the PA's, not the SWE's), so it shipped unmet; documentation discipline requires the code to match the Accepted ADR.
- **Suggested fix:** Add three bullets to the node shape (`subtype`: optional string — for `chunk` rows `"parent" | "child"`; `parent_id`: the `_id` of the containing row — a child's parent chunk, a parent's `document`; `chunk_index`: 0-based position among siblings) and one sentence saying the hierarchy can be walked by `parent_id` in both directions without `$graphLookup`. Assert in `TestSystemPrompt`.

### 2. Changing the chunking config does not re-chunk — `apps/memory/src/tree/memory/pipeline.py:309-368`
- **What the user experiences (wrong):** #107 Story "Book reader switches back to the Chapter-4 fixed-window splitter": the reader sets `strategy: fixed_tokens`, drops `memory`, re-runs the pipeline on the same document — and gets the SAME recursive parents with heading paths, because `clean_and_chunk_task` reads the config via `_live_app_config()` inside the body while its `INPUTS` cache key is the `Document` alone (Tester #108 reproduced across two processes). The docstring at line 316-319 claims "the same (document, chunking config) always produces the same payload, which is what keeps this task's `INPUTS` cache a hit" — the config is not in the key.
- **What the spec / good UX implies (right):** Flipping a documented knob must take effect on the next run; Prefect `INPUTS` caching must be keyed on everything that determines the output (CLAUDE.md: pipelines idempotent, retried, checkpointed — a checkpoint that survives a config change is a stale checkpoint).
- **Suggested fix:** `_clean_and_chunk(document, chunking: ChunkingConfig, opik_trace_headers=None)`; the flow resolves `config.memory.chunking` once at entry (it already reads `memory.mode` there) and passes it in. A Pydantic model is hashable by Prefect's `INPUTS` policy. Keep `cache_expiration=30d`, `retries=1`. Update `_split_documents` and the task-① tests.

### 3. The Clean step corrupts code samples — `apps/memory/src/tree/memory/rag/cleaning.py:96-121, 151-154`
- **What the user experiences (wrong):** The corpus this repo ingests (`sources/*.yaml`: decodingai.com and other technical Substacks) is code-heavy. After `clean_text`, every Python comment `#comment` inside a ```` ```python ```` fence becomes `# comment` (Tester #106) and every indented code line collapses to one leading space (`collapse_whitespace`). That text is what `search_memory` returns as `content` for the agent to quote, what the CLI excerpt prints, and what the LLM extracts entities from in `graphrag`. A reader asking "show me the harness loop code" gets un-runnable Python.
- **What the spec / good UX implies (right):** ADR-006 Decision 7 / #106: normalise markdown and collapse *prose* whitespace; the Clean step must not alter what a document says. #107 already made the splitter fence-aware for exactly this reason (a fake heading corrupts the **Contextual header**); the cleaner must be consistent with it.
- **Suggested fix:** Move `_fenced_ranges` (chunking.py:365) into `cleaning.py` (stdlib-only, pure — it qualifies) and import it from chunking. In `normalize_markdown` and `collapse_whitespace`, skip lines inside fenced ranges (still apply CRLF→LF and `rstrip`, which are safe for code). Add `TestFencedCodeBlocks` to `test_cleaning.py`; keep the idempotence fixture loop covering the new fixture.

### 4. The `tree-memory` skill promises ingest counts that never arrive — `.agents/skills/tree-memory/SKILL.md:112, 118, 124, 128`
- **What the user experiences (wrong):** In both modes the agent is told `ingest_url` / `ingest_file` / `ingest_conversation` "Returns a JSON summary with node/edge counts" and to "Confirm what was extracted (node/edge counts)". The tools return `{"status": "scheduled", "flow_run_id": ...}` (async submit, `tree/online.py::dispatch_online_pipeline`), so the agent reports counts it does not have or tells the user the ingest "found 0 nodes". #110 Story 4 made the same wrong claim (`edges_written: 0`) — the story, not the code, was wrong.
- **What the spec / good UX implies (right):** The skill is the model-visible contract; it must describe the real return shape and the real way to confirm (a follow-up `search_memory` once the flow run completes).
- **Suggested fix:** Replace the four lines; under "After ingestion" say: report the `flow_run_id`, explain the write is out-of-band, offer to `search_memory` for the new title afterwards.

### 5. Help text and README still say "knowledge graph" where `rag` mode writes none
- `apps/memory/Makefile:173` — `run-memory-pipeline: # MEMORY pipeline (\`documents\` -> knowledge graph, …)`; `apps/memory/Makefile:187` — `query-graph: # Query and visualize the knowledge graph … omit for full graph` (in `rag` it prints ranked parents and refuses the full graph).
- `apps/memory/scripts/run_memory_pipeline.py:2` — "(``documents`` → knowledge graph)".
- `apps/memory/README.md:127` — "runs the six-task extraction body" (the body is now `clean-and-chunk`, `embed-children`, `load-rag-rows` + the graphrag stages); `:200` "extracts them into the graph"; `:209` "to build the graph".
- **What the user experiences (wrong):** `make help` — the first thing a reader runs — describes the Chapter-4 pipeline as building a knowledge graph.
- **Suggested fix:** Say "`memory` collection" / "memory rows (+ knowledge graph in `graphrag`)" and, for `query-graph`, "HTML graph in `graphrag`, ranked parent chunks as text in `rag`; omit QUERY for the full graph (graphrag only)". Leave every graphrag-only module's copy alone.

### 6. `top_k <= 0` and blank queries surface the wrong error — `rag/retrieval.py:76-91`, `mcp/tools.py:95-119`
- **What the user experiences (wrong):** `retrieve_parents(top_k=0)` passes `limit=0` to both search stages; both raise, both log `WARNING … search unavailable, falling back …` — an on-call reader thinks Atlas is down. Rag `search_memory(query="")` returns the raw Voyage `400 Input cannot contain empty strings` text, while every sibling tool returns `{"error": "invalid_input", …}`.
- **Suggested fix:** Early-return `RetrievalResult()` when `top_k <= 0` (log INFO); in the rag `search_memory`, `if not query.strip(): return json.dumps({"error": "invalid_input", "detail": "query must not be empty"})`.

### 7. RRF tie order is non-deterministic — `rag/retrieval.py::group_children_by_parent`
- **What the user experiences (wrong):** Two children with identical fused scores come back in a different order on repeated identical calls (Tester #109); the CLI/MCP output flips between runs.
- **Suggested fix:** Secondary sort keys `child_id` / `parent_id` (stable, already strings). Two lines.

### 8. `fixed_tokens` emits U+FFFD on multi-token characters — `rag/chunking.py:130-146`
- **What the user experiences (wrong):** With `strategy: fixed_tokens` (the documented "Chapter-4 first version"), a window boundary that falls inside a 3-token emoji decodes to `�` in the stored `content`; `recursive` already snaps to character boundaries via `_token_char_offsets`. Pre-existing in the old `chunk_document`, but this feature re-shipped and re-documented the strategy.
- **Suggested fix:** Compute windows over token indices, then map to character offsets with `_token_char_offsets` (already in the module) and slice the source string instead of decoding a raw token slice.

## Not in this rollup (recorded so they are not lost)
- #105 Story 2 names `make memory-check-db` as an entry point that should fail on `TREE_MEMORY__MODE=hybrid`; it never reads app config. Spec wording only — the AC (`load_app_config()`) is correct and satisfied. No code change.
- ADR-002 / ADR-005 prose still names pre-move module paths; ADR-006 Context references cite `tasks/105-…` rather than `tasks/done/105-…`. Accepted ADRs are historical records — leave them.
- `pipeline.py::_build_edge_op` assigns `extractor.model_dump()` bare inside a pipeline `$set` (pre-existing, fixed-schema, safe today).
- `title == H1` repeats the title in the **Contextual header** — format pinned by ADR-006 §4 and the glossary.
- Stray gitignored `apps/memory/.tree/graphs/how-does-memory-for-agents-work-20260905-163704.html` in the worktree — delete during cleanup, not a diff concern.

## User Stories

(Inherit from the original tasks #105–#111 — no new stories. Re-verify each one passes after the fix. #107's "Book reader switches back to the Chapter-4 fixed-window splitter" is the story Issue 2 restores; #110 Story 4's expected return value is superseded by Issue 4's corrected contract.)

---

Refs: `tasks/done/105-memory-mode-config-and-memory-collection.md`, `tasks/done/106-rag-cleaning-module.md`, `tasks/done/107-two-level-chunking-strategies.md`, `tasks/done/108-rag-load-stage-and-pipeline-mode-branching.md`, `tasks/done/109-parent-document-retrieval-and-query-branching.md`, `tasks/done/110-mcp-tool-gating-per-memory-mode.md`, `tasks/done/111-memory-graph-package-docs-and-e2e.md`

## Log

### [SWE] 2026-09-05 18:05 — Implementation

All 8 issues fixed in ONE pass on `feat/rag-graphrag-modes`. NOT committed — Tester first.

**Files modified**

- `apps/memory/src/tree/memory/graph/nl_query.py` — Issue 1: three bullets (`subtype`, `parent_id`, `chunk_index`) + a "walk it with `parent_id`, no `$graphLookup`" paragraph in the Node-document-shape section.
- `apps/memory/src/tree/memory/pipeline.py` — Issue 2: `_clean_and_chunk(document, chunking: ChunkingConfig, opik_trace_headers=None)`; `_split_documents(docs, chunking, ...)`; the flow passes `config.memory.chunking` (already resolved at entry next to `memory.mode`); docstrings corrected (they no longer claim a cache property the code lacked).
- `apps/memory/src/tree/memory/rag/cleaning.py` — Issue 3: owns the ONE fence definition (`fenced_line_flags` line view + `fenced_ranges` char view) and `_outside_fences`; `normalize_markdown`, `drop_boilerplate_lines`, `collapse_whitespace` and `_collapse_blank_runs` skip fenced code. Still stdlib-only (`collections.abc` added).
- `apps/memory/src/tree/memory/rag/chunking.py` — Issue 3: `_fenced_ranges` deleted, `fenced_ranges` imported from cleaning (rag→rag). Issue 8: `_fixed_window` maps token windows through `_token_char_offsets` and slices the source string instead of decoding a raw token slice.
- `apps/memory/src/tree/memory/rag/retrieval.py` — Issue 6: `top_k <= 0` early-returns `RetrievalResult()` (INFO, no search). Issue 7: `(-score, child_id)` / `(-best_score, str(parent_id))` sort keys.
- `apps/memory/src/tree/mcp/tools.py` — Issue 6: rag `search_memory` returns `{"error": "invalid_input", "detail": "query must not be empty"}` for a blank query, before the embedding model.
- `.agents/skills/tree-memory/SKILL.md` — Issue 4: the three `ingest_*` bullets and "After ingestion" now state the real async contract (`{"status", "flow_run_id"}`, out-of-band write, confirm with a later `search_memory`); "Presenting Results" no longer asks for counts.
- `apps/memory/Makefile`, `apps/memory/scripts/run_memory_pipeline.py`, `apps/memory/README.md` — Issue 5: mode-neutral wording.
- Tests: `tests/unit/memory/graph/test_nl_query.py`, `tests/unit/memory/test_pipeline.py`, `tests/unit/memory/rag/test_cleaning.py`, `tests/unit/memory/rag/test_chunking.py`, `tests/unit/memory/rag/test_retrieval.py`, `tests/unit/mcp/test_tools.py`.

**Tests**

- Unit: 2431 passing, 0 failing. Baseline on this branch before the task: 2400 (measured with `git stash push -- apps/memory`), so +31 tests, none removed or weakened.
- Integration: N/A — this repo has no integration suite; e2e is the real-pipeline run below.

**AC → test / grep mapping**

| AC | Verification |
|---|---|
| Issue 1 | `tests/unit/memory/graph/test_nl_query.py::TestSystemPrompt` (4 tests: each field name, both subtypes, the `$graphLookup`/0-based sentence) |
| Issue 2 | `tests/unit/memory/test_pipeline.py::TestChunkingConfigIsPartOfTheCacheKey` (payloads differ, cache keys differ, same config still hits, `chunking` is a declared non-excluded param) + the live Prefect run below |
| Issue 3 | `tests/unit/memory/rag/test_cleaning.py::TestFencedCodeBlocks` (8 tests) + `_FENCED_ARTICLE` and an unclosed-fence fixture added to the idempotence/determinism `_FIXTURES` loop + the untouched `TestModulePurity` AST test + `tests/unit/memory/rag/test_chunking.py::TestFencedCodeBlocks::test_fence_detection_has_one_definition_shared_with_the_cleaner` |
| Issue 4 | `grep -n "node/edge counts\|JSON summary\|node count, edge count" .agents/skills/tree-memory/SKILL.md` → empty |
| Issue 5 | `grep -n "knowledge graph" apps/memory/Makefile apps/memory/scripts/run_memory_pipeline.py` → empty; `grep -n "six-task\|into the graph\|to build the graph" apps/memory/README.md` → empty |
| Issue 6 | `tests/unit/memory/rag/test_retrieval.py::TestNonPositiveTopK` (top_k 0 and -1: empty, `hybrid_search` not called, no "unavailable" in caplog) + `tests/unit/mcp/test_tools.py::TestRagSearchMemory::test_blank_query_returns_the_standard_invalid_input_envelope` |
| Issue 7 | `tests/unit/memory/rag/test_retrieval.py::TestGroupChildrenByParent::test_tied_children_keep_a_deterministic_order_in_either_input_order` (+ the parent-level twin) |
| Issue 8 | `tests/unit/memory/rag/test_chunking.py::TestFixedTokensStrategy::test_multi_token_emoji_never_decodes_to_a_replacement_char` (+ the children twin); the 5 pre-existing `TestFixedTokensStrategy` tests still pass unchanged |

**Evidence**

```
$ make memory-format-check && make memory-lint-check
266 files already formatted
All checks passed!

$ make pre-commit
prettier / ruff check / ruff format / biome check (harness) .... Passed

$ make memory-tests
============================ 2431 passed in 21.40s =============================
```

Issue 2 — live cache check (Docker Mongo + local Prefect, scratch Document, task ① via Prefect so the real `INPUTS` cache is exercised):

```
run 1 (recursive, cold): prefect state = Completed, 16 parent(s)
  parent 0: heading_path=['Parent Document Retrieval'] ...
run 2 (recursive, repeat): prefect state = Cached, 16 parent(s)     # unchanged config still HITS
run 3 (fixed_tokens, after the flip): prefect state = Completed, 12 parent(s)
  parent 0: heading_path=[] head='# Parent Document Retrieval\n\nChildren carry ' ...
cleanup: deleted 1 scratch document(s) / 0 scratch document(s) remain
```

Issue 2 — the #107 user story, via the real pipeline (rag mode, one real code-heavy document, `memory` dropped between runs, `make memory-serve-workflows` run FROM THIS WORKTREE with the Dockerized `tree-prefect-worker` stopped so the branch code executed):

```
$ TREE_MEMORY__MODE=rag make memory-run-memory-pipeline MODE=online DOC_IDS=6a8ea976dfbcf322b44281c4 ...
  document 1 / parent 2 / child 27 rows, 0 edges, children with a 1024-dim vector: 27
  parent 0 heading_path=[]   parent 1 heading_path=["Q3 2023 Financial Performance Analysis"]

$ TREE_MEMORY__MODE=rag TREE_MEMORY__CHUNKING__STRATEGY=fixed_tokens make memory-run-memory-pipeline ... (same doc)
  parents: 2  children: 25   parents WITH a heading_path: 0
```

Issue 7 — two identical `make memory-query-graph QUERY="structured outputs with pydantic"` calls produced byte-identical ranked output (`diff` clean).

Issue 3 + 6 — against the real ingested article (24 506 chars) and live Mongo:

```
fenced blocks preserved byte-for-byte: 1, altered: 0
lines the fence-BLIND cleaner would have rewritten inside fences: 1
INFO:tree.memory.rag.retrieval:Skipping retrieval: top_k=0 asks for no parents (must be >= 1)
parents: []
```

Issue 8 — old vs new `_fixed_window` on 300 × `🧠` (3 cl100k tokens each), `size=5`:

```
OLD has U+FFFD: True
NEW has U+FFFD: False | roundtrip ok: True
```

**Notes / judgement calls**

- **Issue 3 scope.** The rollup's suggested fix names `normalize_markdown` and `collapse_whitespace`, but the AC says `clean_text` leaves fenced CONTENT byte-identical. Two further stages break that on real code: `_collapse_blank_runs` eats the PEP 8 double blank line between two `def`s, and `drop_boilerplate_lines` can delete a ≥20-char code line that repeats 3× (e.g. `        raise ValueError(message)`). Both are now fence-aware too — same shared mask, ~6 extra lines, tests for both. Flagging it as a deliberate widening of the diff, not scope creep.
- **Issue 3 mechanism.** The ONE definition is the line scanner `fenced_line_flags`; `fenced_ranges` (what chunking imports) is derived from it. Adjacent fenced blocks merge into one range — irrelevant, every caller only asks "is this offset fenced?". Behaviour outside fences is unchanged because `_outside_fences` applies the original regexes to the unfenced slices verbatim.
- **Issue 2 signature.** `chunking` is a required positional-or-keyword parameter (no `None` default that would fall back to a config read — that is the bug). `_split_documents` calls the task with `chunking=` as a KEYWORD so cache-key resolution is by name and existing task mocks that take `(doc, **kwargs)` keep working. Prefect hashes the `ChunkingConfig` via `INPUTS` → `hash_objects` → `JSONSerializer` (confirmed in `prefect/cache_policies.py` + `prefect/utilities/hashing.py`); a Pydantic model serialises deterministically, and the `test_the_same_config_keeps_the_cache_a_hit` test guards the flip side.
- **Issue 1 test class.** Added as a NEW `TestSystemPrompt` class (the AC's node id); the pre-existing `TestBuildSystemPrompt` is untouched.
- **Issue 8 boundary semantics.** Windows now snap to character boundaries, so a window can cover one token fewer than `size` when a multi-token character straddles the edge. That only ever SHRINKS a chunk, so the `<= size` guarantee downstream embedding batching relies on still holds; `test_no_parent_exceeds_the_parent_size` and the concatenation round-trip test both still pass.
- **Not touched (as the rollup instructs):** `graph_tools.py` / `graph_app.py` / `dashboard_app.py` / `_GRAPHRAG_INSTRUCTIONS` keep "knowledge graph"; ADR prose untouched; `_build_edge_op`, `make memory-check-db` and the `title == H1` header format left alone.
- **Cleanup done:** scratch `Document` deleted (0 remain, `documents` back at 303), `memory` collection dropped back to 0 rows, the stray `apps/memory/.tree/graphs/how-does-memory-for-agents-work-20260905-163704.html` removed, `tree-prefect-worker` restarted, all serve processes stopped.

### [Tester] 2026-09-05 20:40 — QA (round 1)

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check`, `make memory-lint-check`, `make pre-commit` all clean, 0 warnings)
- Unit tests: 2431 passed / 0 failed (`make memory-tests`, matches SWE's count, baseline 2400 confirmed by diff review)
- Integration tests: N/A — repo has no integration suite (per AGENTS.md); e2e verification done via direct live invocation of the changed code paths instead (below)
- Warnings: 0

**Diff review** — read every changed file in full (`nl_query.py`, `pipeline.py`, `cleaning.py`, `chunking.py`, `retrieval.py`, `mcp/tools.py`, `SKILL.md`, `Makefile`, `README.md`, `run_memory_pipeline.py`, all 6 changed test files). Issues 1, 2, 4, 5, 6, 7 match their AC text exactly, with meaningful tests (Issue 2's cache-key test calls the real `clean_and_chunk_task.cache_policy.compute_key`, not a proxy). Issue 3's widening to `_collapse_blank_runs` / `drop_boilerplate_lines` (judgement call a) is correct and necessary — confirmed live below. Issue 3's "adjacent fences merge" mechanism (judgement call b) is behaviourally inert — confirmed live below. Issue 8's `_fixed_window` change (judgement call c) is where adversarial testing found a real regression — see Break path 1.

**E2E adversarial pass**
- Happy path (Issue 1, prompt fields): `uv run python -c "from tree.memory.graph.nl_query import build_nl_query_system_prompt as f; p=f(); print('subtype' in p, 'parent_id' in p, 'chunk_index' in p)"` → `True True True` (PASS)
- Happy path (Issue 3, fence preservation): built a fixture combining a `#comment`, 4-space indentation, a `raise ValueError(message)` line repeated 3×, two consecutive blank lines, and `    trailing spaces  ` inside a ` ```python ` fence, plus `# Heading` outside → `clean_text()` output: fenced content byte-identical except the trailing-space `rstrip`, `# Heading` normalised outside the fence, `clean_text(clean_text(x)) == clean_text(x)` (idempotent). Also verified an UNCLOSED fence (runs to EOF, byte-identical, idempotent) and a `~~~` fence (byte-identical). (PASS)
- Break path 1 (Issue 8 — boundary: multi-token multi-byte characters): `split_document("🧠"*300, ChunkingConfig(strategy="fixed_tokens", parent={"size":5,"overlap":0}, child={"size":2,"overlap":0}))` → every one of 60 parents has content that re-encodes to **15 tokens**, not ≤5 (3× over budget); every child re-encodes to **6 tokens**, not ≤2. Repro:
  ```
  $ uv run python -c "... split_document('🧠'*300, cfg) ..."
  n parents 60
  parent token counts (first 10): [15, 15, 15, 15, 15, 15, 15, 15, 15, 15]
  max parent tokens: 15 declared size: 5
  max child tokens: 6 declared child size: 2
  ```
  Root cause: `_token_char_offsets` accumulates `consumed` from the PREVIOUS (already-forward-snapped) offset instead of the true cumulative token-byte count, so every token boundary that lands inside a multi-byte character compounds a full extra character onto the running total. Confirmed with mixed CJK text too (`parent.size=20` → max observed 31 tokens, some ~55% over budget) and confirmed ASCII-with-occasional-accents text does NOT trigger it (single-byte characters always land on valid boundaries, so the accumulator never desyncs) — that is why the pre-existing `TestFixedTokensStrategy` ASCII-only tests (`_text_of_tokens(250)`, `test_no_parent_exceeds_the_parent_size`) never caught it, and why the new `test_multi_token_emoji_never_decodes_to_a_replacement_char` doesn't either (it only asserts no `�` and a matching re-encoded token sequence — never asserts `<= size`). This directly contradicts the SWE's own log note: "That only ever SHRINKS a chunk, so the `<= size` guarantee downstream embedding batching relies on still holds" — it does not hold; it can more than TRIPLE a chunk's token count for multi-token-character-heavy content (emoji-heavy or CJK documents, which this system explicitly documents as in-scope, e.g. Substack articles). Expected: no chunk's re-encoded token count exceeds its configured `size`. Actual: up to 3× over. (FAIL)
- Break path 2 (Issue 6 — state edge: top_k=0 direct call, not just mocked): `await retrieve_parents(MagicMock(), "db", "q", AsyncMock(), "user1", top_k=0)` → `RetrievalResult(parents=[])`, log line `INFO:tree.memory.rag.retrieval:Skipping retrieval: top_k=0 asks for no parents (must be >= 1)`, embedding model never touched (`mock_calls == []`). Matches AC. (PASS)
- Break path 3 (Issue 3 — judgement call b, fence-merge boundary): confirmed `fenced_ranges` merges two fence blocks ONLY when there is truly nothing (not even a blank line) between the closing and next opening marker (`fenced_ranges("```\ncode1\n```\n```\ncode2\n```\n")` → `[(0, 42)]`, one merged range); when a heading + blank line sits between two blocks, `fenced_ranges` returns TWO separate ranges and `split_document` with a small `parent.size` correctly produces a NEW parent with `heading_path=("Real Heading Between Fences",)` between the two code blocks. The merge case is confirmed behaviourally inert (nothing can be misdetected inside a genuinely-adjacent pair). (PASS)
- Break path 4 (Issue 7 — concurrency/determinism: two separate OS processes, not just two calls in one process): ran the same tied-score `group_children_by_parent` input through `uv run python -c "..."` twice as two independent process invocations → identical output both times (`{'p1': ['c2', 'c3'], 'p2': ['c1', 'c4']}`, `['p1', 'p2']`). (PASS)

**Acceptance criteria**
- [x] PASS — Issue 1 (nl_query prompt fields) — `TestSystemPrompt` (4 tests) passes; live: `build_nl_query_system_prompt()` contains `subtype`, `parent_id`, `chunk_index`, both subtype values, `$graphLookup`, "0-based"
- [x] PASS — Issue 2 (chunking config in cache key) — `TestChunkingConfigIsPartOfTheCacheKey` (4 tests, using the REAL `cache_policy.compute_key`) passes; `pipeline.py:311` signature confirmed `_clean_and_chunk(document, chunking: ChunkingConfig, ...)`; `config.memory.chunking` resolved once at flow entry (`pipeline.py:1896`) and threaded through `_split_documents` → `clean_and_chunk_task(chunking=chunking, ...)` as a keyword; docstring no longer claims the config alone determines cache hits
- [x] PASS — Issue 3 (fence-safe cleaning) — `TestFencedCodeBlocks` (8 tests) + live adversarial fixture above (byte-identical fenced content, idempotent, unclosed fence and `~~~` fence both handled); `test_fence_detection_has_one_definition_shared_with_the_cleaner` asserts `chunking_module.fenced_ranges is cleaning.fenced_ranges` (identity, not just behavioural equivalence); `TestModulePurity` AST stdlib-only test still passes (74/74 `test_cleaning.py` tests green)
- [x] PASS — Issue 4 (SKILL.md async contract) — `grep -n "node/edge counts\|JSON summary\|node count, edge count" .agents/skills/tree-memory/SKILL.md` → empty; read the diff: all 4 lines replaced with the `{"status", "flow_run_id"}` contract in both modes
- [x] PASS — Issue 5 (mode-neutral help text) — `grep -n "knowledge graph" apps/memory/Makefile apps/memory/scripts/run_memory_pipeline.py` → empty; `grep -n "six-task\|into the graph\|to build the graph" apps/memory/README.md` → empty; graphrag-only modules untouched (spot-checked `graph_tools.py` still says "knowledge graph")
- [x] PASS — Issue 6 (top_k / blank query error handling) — `TestNonPositiveTopK` + `test_blank_query_returns_the_standard_invalid_input_envelope` (3 blank variants) pass; live direct call above confirms `top_k=0` never touches the embedding model and logs INFO, not WARNING
- [x] PASS — Issue 7 (RRF tie determinism) — `TestGroupChildrenByParent` tie tests pass; live two-process check above confirms determinism isn't an artifact of one interpreter's hash seed
- [ ] FAIL — Issue 8 (`fixed_tokens` never emits U+FFFD, chunks stay within `size`)
      Expected: no U+FFFD (confirmed, holds) AND no chunk's token count exceeds its configured `size` (the SWE's own stated invariant — "That only ever SHRINKS a chunk").
      Actual: U+FFFD is gone, but chunks can be up to 3× OVER the configured size for multi-token multi-byte characters (300×🧠, parent.size=5 → every parent is 15 tokens; child.size=2 → every child is 6 tokens; CJK text, parent.size=20 → up to 31 tokens observed).
      Fix: `_token_char_offsets` (chunking.py:460) must accumulate the TRUE cumulative consumed-byte count across tokens and only snap THAT value forward per-lookup, instead of carrying the previously-snapped (already-inflated) value as the base for the next token's addition — the current `consumed = min(consumed + len(token_bytes), byte_position)` compounds one full extra character's worth of overshoot per multi-byte token boundary. Add a regression test asserting `_ntok(chunk) <= size` for both the 300×🧠 fixture and a CJK fixture.
- [x] PASS — `make memory-format-check && make memory-lint-check && make memory-tests` green, 2431 ≥ 2400 — reproduced independently
- [ ] Tester full QA — see VERDICT below (blocked on Issue 8)
- [ ] PA acceptance review — not run; blocked on Tester PASS

**Evidence**
```
$ make memory-format-check && make memory-lint-check
266 files already formatted
All checks passed!

$ make pre-commit
prettier / ruff check / ruff format / biome check (harness) .... Passed

$ make memory-tests
============================ 2431 passed in 21.10s =============================

$ grep -n "node/edge counts\|JSON summary\|node count, edge count" .agents/skills/tree-memory/SKILL.md   # exit 1, empty
$ grep -n "knowledge graph" apps/memory/Makefile apps/memory/scripts/run_memory_pipeline.py               # exit 1, empty
$ grep -n "six-task\|into the graph\|to build the graph" apps/memory/README.md                             # exit 1, empty

# Issue 8 repro
n parents 60
parent token counts (first 10): [15, 15, 15, 15, 15, 15, 15, 15, 15, 15]
max parent tokens: 15 declared size: 5
max child tokens: 6 declared child size: 2
```

**Other issues found**
- None beyond Issue 8. Did not re-run the live Prefect cache proof (Issue 2) or start a Gemini `query_memory` NL call, since Issue 8 already blocks the round and those infra-heavy checks (stop/restart `tree-prefect-worker`, serve from worktree) are best spent once, after the fix — the SWE's own live evidence for Issue 2 (recursive cold Completed → repeat Cached → fixed_tokens Completed with a different parent count) is independently plausible from the code review (the `ChunkingConfig` is now a genuine task parameter, confirmed by the identity/parameter tests) and I have no reason to doubt it; will re-run it as part of round 2 regardless.
- `_token_char_offsets` (the root cause) is also used by the `recursive` strategy for token-count estimation during greedy splitting (chunking.py:293, 417) — same compounding bug could cause recursive chunks over CJK/emoji-heavy text to be systematically MIS-SIZED (likely undersized rather than oversized there, since recursive uses it as a stopping-condition estimate, not a slice boundary directly) — worth a follow-up ticket, not blocking this rollup since Issue 8 only asked about `fixed_tokens`, but the SWE should be aware the helper itself is the shared root cause.

**VERDICT: FAIL**

One issue: Issue 8's `fixed_tokens` fix removes the U+FFFD corruption but introduces a chunk-size-budget violation (up to 3× over `size`) for multi-token multi-byte characters (emoji, CJK) — exactly the corpus this fix's own test targets. Everything else (Issues 1–7, format/lint/pre-commit, full unit suite, fence-merge boundary behaviour, RRF cross-process determinism, top_k live behaviour) verified PASS with concrete evidence. Fix `_token_char_offsets`' offset accumulation, add a `<= size` regression assertion to the emoji/CJK tests, and resubmit for round 2 (which will include the live Prefect cache-flip re-proof and cleanup).

### [SWE] 2026-09-05 22:15 — Fixes (round 2, Issue 8 only)

Tester round 1 passed Issues 1-7 and failed Issue 8. Only `chunking.py` and its test file
changed this round; nothing else was touched. NOT committed.

**Files modified**

- `apps/memory/src/tree/memory/rag/chunking.py`
  - `_token_char_offsets` — root cause. `consumed` now tracks the TRUE cumulative
    token-byte count; the forward snap runs on a per-boundary copy (`boundary`) and is
    never written back into the accumulator. Before, each token boundary landing inside a
    multi-byte character added its bytes on top of the ALREADY-SNAPPED value, compounding
    a whole character of overshoot per multi-token character. Docstring now also warns
    callers that consecutive offsets may REPEAT (every token boundary inside one character
    maps to that character), which is the correct shape and what the callers must handle.
  - `_fixed_window` — the emitted window is shrunk one token at a time until the sliced
    text RE-ENCODES to `<= size`, exactly as `_token_pieces` already does at the recursive
    ladder's last level. Needed on top of the helper fix because the end boundary snaps
    FORWARD onto a character: with correct offsets a 5-token window over 3-token emoji
    still covers 2 emoji = 6 tokens until it is shrunk.
  - `_fixed_window` stride — kept the Chapter-4 stride (`size - overlap` from the NOMINAL
    end `start + size`); only a window that was actually SHRUNK strides from its real end,
    because striding past it would skip the tokens it gave up. See the self-inflicted
    regression under Notes.
- `apps/memory/tests/unit/memory/rag/test_chunking.py` — 10 new tests (no test weakened,
  none removed, the round-1 emoji tests kept verbatim and extended with new sibling tests).

**Tests**

- Unit: 2441 passing, 0 failing (`make memory-tests`). Round 1 was 2431, so +10.
- Integration: N/A — no integration suite in this repo.

**AC → test mapping (Issue 8)**

| Requirement | Test |
|---|---|
| root cause pinned at the helper | `TestMultiTokenCharacterBudget::test_token_offsets_track_the_true_cumulative_byte_count` (golden offsets for `"🧠"*5`) |
| emoji parents `<= size` | `TestFixedTokensStrategy::test_multi_token_emoji_parents_stay_within_the_parent_size` |
| emoji children `<= size` | `TestFixedTokensStrategy::test_multi_token_emoji_children_stay_within_the_child_size` |
| mixed CJK parents + children `<= size` | `TestFixedTokensStrategy::test_mixed_cjk_parents_and_children_stay_within_their_sizes` |
| both strategies, emoji + CJK | `TestMultiTokenCharacterBudget::test_no_chunk_exceeds_its_size_on_multi_token_characters` (4 cases: emoji/cjk x fixed_tokens/recursive) |
| no U+FFFD (round 1, unchanged) | `test_multi_token_emoji_never_decodes_to_a_replacement_char`, `test_multi_token_emoji_children_carry_no_replacement_char` |
| stride regression guards | `test_overlap_stops_at_the_last_window_instead_of_repeating_the_tail`, `test_a_document_shorter_than_the_window_is_one_parent_even_with_overlap` |

`_assert_within_budget` is the shared assertion: every chunk re-encodes to `<= size`
tokens, and the ONE allowed exception is a chunk that is a SINGLE character costing more
than `size` on its own (one 🧠 is 3 tokens under `child.size=2`) — splitting that is
precisely what produced U+FFFD, which the same AC forbids. The helper asserts every
over-budget chunk IS that case, so the carve-out cannot mask a real violation.

**Evidence — new tests RED against the round-1 shipped code, GREEN after the fix**

Round-1 `_fixed_window` + `_token_char_offsets` restored in place, full test file run:

```
$ uv run pytest tests/unit/memory/rag/test_chunking.py -q     # round-1 code
FAILED ...::TestFixedTokensStrategy::test_multi_token_emoji_parents_stay_within_the_parent_size
FAILED ...::TestFixedTokensStrategy::test_multi_token_emoji_children_stay_within_the_child_size
FAILED ...::TestFixedTokensStrategy::test_mixed_cjk_parents_and_children_stay_within_their_sizes
FAILED ...::TestMultiTokenCharacterBudget::test_no_chunk_exceeds_its_size_on_multi_token_characters[emoji-fixed_tokens]
FAILED ...::TestMultiTokenCharacterBudget::test_no_chunk_exceeds_its_size_on_multi_token_characters[cjk-fixed_tokens]
FAILED ...::TestMultiTokenCharacterBudget::test_token_offsets_track_the_true_cumulative_byte_count
6 failed, 128 passed in 1.12s

$ uv run pytest tests/unit/memory/rag/test_chunking.py -q     # fixed code
134 passed in 1.02s
```

Isolation check — with the `_fixed_window` shrink in place but the helper REVERTED, only
`test_token_offsets_track_the_true_cumulative_byte_count` fails (1 failed, 131 passed):
the shrink alone hides the symptom, so the golden-offset test is what guards the root
cause. Both changes are load-bearing and both are tested.

The Tester's exact repro, before and after:

```
# round 1 (shipped)                          # round 2 (fixed)
offsets("🧠"*5) = [0,1,2,3,4,5,5,5,...]      offsets("🧠"*5) = [0,1,1,1,2,2,2,3,3,3,4,4,4,5,5,5]
n parents 60                                 n parents 300
max parent tokens: 15  declared 5            max parent tokens: 3   declared 5
max child tokens:   6  declared 2            max child tokens:  3   declared 2  (one 🧠, indivisible)
CJK max parent tokens: 26 declared 20        CJK max parent tokens: 20 declared 20
CJK max child tokens:  10 declared  8        CJK max child tokens:   8 declared  8
roundtrip ok: True                           roundtrip ok: True, U+FFFD: False
```

**Evidence — QA loop**

```
$ make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check
266 files left unchanged / All checks passed! / 266 files already formatted / All checks passed!

$ make pre-commit
prettier / ruff check / ruff format / biome check (harness) .... Passed

$ make memory-tests
============================ 2441 passed in 21.11s =============================
```

**Evidence — end-to-end on the REAL corpus (local Mongo, 303 documents)**

Round-1 code vs round-2 code, same 4 real articles + one emoji/CJK article, through
`clean_text` -> `split_document`, counting chunks whose re-encoded token count exceeds
`parent.size`:

```
config            document                          round 1                    round 2
default 4096/256  From 1 Bloated Context Window...   maxTok=4100 over=1         maxTok=4096 over=0
default 4096/256  Context Engineering for Coding...  maxTok=4098 over=1         maxTok=4096 over=0
default 4096/256  The Bare-Bones Coding Agent Loop   maxTok=4133 over=2         maxTok=4096 over=0
default 4096/256  Stop Converting Documents to...    maxTok=4096 over=0         maxTok=4096 over=0
stress  20/8      From 1 Bloated Context Window...   maxTok=  26 over=98        maxTok=  20 over=0
stress  20/8      The Bare-Bones Coding Agent Loop   maxTok=  48 over=229       maxTok=  20 over=0
stress  20/8      multibyte article (emoji+CJK)      maxTok=  28 over=47        maxTok=  20 over=0
```

The budget was breached on the ENGLISH corpus at the shipped 4096 default too (4100 /
4133 tokens), not only on emoji/CJK — the Tester's repro was the loud case of a defect
that was already live. Parent counts at the shipped default are unchanged (2/2/3/3).

Through the real pipeline task body (`clean_and_chunk_task.fn`) on a real `Document`:

```
document: Structured Outputs: The Silent Hero of Production AI | chars: 24506
  recursive    parents=2 children=27 maxParentTok=3861 (<=4096: True) maxChildTok=256 (<=256: True) U+FFFD=False heading_paths=1
  fixed_tokens parents=2 children=27 maxParentTok=4096 (<=4096: True) maxChildTok=256 (<=256: True) U+FFFD=False heading_paths=0
```

Same 2 parents / 27 children as the round-1 live pipeline run, so the fix does not move
the shipped-config output; no DB writes, no cleanup needed.

**Recursive path — checked, as the Tester asked**

The recursive strategy shares `_token_char_offsets` (`_token_pieces`, `_overlap_start`)
but was NEVER over budget, before or after: both call sites measure candidate spans on
the RE-ENCODED text (`_span_tokens`) and shrink until they fit, so the inflated offsets
only moved candidate cut points, they never leaked into the size guarantee. Measured on
the same inputs (CJK article and a separator-free CJK blob, `parent.size=20`):

```
cjk-article            recursive OLD: parents= 28 max=20 mean=19.1 | NEW: parents= 28 max=20 mean=19.1
cjk-no-separator-blob  recursive OLD: parents= 48 max=20 mean=19.2 | NEW: parents= 48 max=20 mean=19.2
emoji "🧠"*300         recursive OLD: parents=300 max= 3           | NEW: parents=300 max= 3
```

Identical counts and identical packing; individual cut positions shift on the CJK article
(the cuts now land on the true token boundary instead of a rounded-up one), which is the
point of the fix. The 4 `TestMultiTokenCharacterBudget` recursive cases passed against
the round-1 code as well — they are kept as guards, not as regressions. So: no separate
follow-up ticket is needed for the recursive greedy merge; the Tester's "worth a
follow-up" note is closed by this measurement.

**Notes**

- **A regression I introduced and caught in the e2e step, not the unit tests.** My first
  version advanced the window from the SHRUNK stop unconditionally. For a document
  shorter than the window with overlap on (the shipped 4096/512 default), the last window
  never terminated the loop: a 1207-token document came out as 511 near-identical parents.
  The pre-existing `test_overlap_yields_more_parents_than_no_overlap` still passed, since
  it only asserts "more than without overlap". Fixed by striding from the NOMINAL end
  (`start + size`) unless the window was actually shrunk, and pinned by two new tests
  (`test_overlap_stops_at_the_last_window_instead_of_repeating_the_tail` asserts exactly 4
  distinct parents for 250 tokens at 100/20;
  `test_a_document_shorter_than_the_window_is_one_parent_even_with_overlap`). Both of
  those pass against the round-1 code too — they guard MY change, and they are why the
  Step-7 e2e ritual exists.
- **The `<= size` guarantee, stated precisely:** every chunk re-encodes to `<= size`
  tokens, EXCEPT a lone character that alone costs more than `size` (a 3-token 🧠 under
  `child.size=2`), which is emitted whole. That exception is forced by the same AC — the
  alternative is splitting the character and storing U+FFFD. The tests assert the
  exception applies only to single-character chunks.
- **Cost:** the shrink loop re-encodes a window at most once per token it gives up. On the
  real corpus at the shipped 4096/256 config it fires 0-2 times per document (bounded by
  the number of tokens a straddling character costs, ~3); on the pathological
  emoji/`size=5` fixture it fires twice per window. `make memory-tests` runtime is
  unchanged (21.1s vs 21.4s round 1).
- **Round-1 claim corrected:** my round-1 note "that only ever SHRINKS a chunk" was wrong,
  as the Tester showed. The invariant is now enforced by the code (the shrink loop) rather
  than asserted in prose, and tested on both strategies for emoji, CJK and the real corpus.
- **Not touched this round:** every Issue 1-7 file. `git status` shows the same file set as
  round 1 — no new files, no deletions, no cleanup pending (the task-body e2e only read
  from Mongo; it did log one Opik trace to `tree-memory`, no DB writes).

### [Tester] 2026-09-05 22:50 — QA (round 2)

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check`, `make memory-lint-check`, `make pre-commit` all clean, 0 warnings)
- Unit tests: 2441 passed / 0 failed (matches SWE's count, +10 over round 1's 2431, none removed/weakened — read the full diff of `test_chunking.py`)
- Integration tests: N/A — no integration suite (per AGENTS.md); e2e via direct live invocation instead (below)
- Warnings: 0

**E2E adversarial pass — Issue 8 (`fixed_tokens` token budget)**
- Happy path: `split_document("🧠"*300, parent.size=5, child.size=2)` → 300 parents, `max_p_tok=3<=5`, `max_c_tok=3` (only the single-🧠 carve-out, `len(chunk)==1`), no `�`, roundtrip OK. (PASS)
- Break path 1 (boundary: emoji + overlap>0): `parent.size=5, overlap=2` on 300×🧠 → 300 parents, `<=5` tokens each (carve-out only for the 1-char case), no `�`. (PASS)
- Break path 2 (mixed content: ASCII words interleaved with 🧠 runs, `parent.size=8, child.size=3`) → all parents/children `<=` their size (carve-out only where `len==1`), no `�`. (PASS)
- Break path 3 (mixed CJK, `parent.size=20, child.size=8`) → 28 parents/82 children, all `<=` size, no `�`, roundtrip OK (both `fixed_tokens` and `recursive`). (PASS)
- Break path 4 (ZWJ grapheme cluster `👨‍👩‍👧`, `parent.size=5, child.size=2`): the family emoji (5 code points, 13 cl100k tokens) gets torn across THREE parents (`'👨‍'`, `'👩‍'`, `'👧'`, each `<=5` tokens) and at the child level into FIVE 1-codepoint fragments (`👨`, ZWJ alone, `👩`, ZWJ alone, `👧`; the base-emoji fragments are 3 tokens > child.size=2, allowed only because `len(chunk)==1`). No `�`, no crash, token-level roundtrip holds. **The carve-out's "one character" is a Python code point, not a grapheme cluster** — confirmed by reading `_assert_within_budget`'s `len(chunk) == 1` and the module's own `_token_char_offsets` docstring, which snaps to `text[i]` (one code point) throughout, matching every other "character" boundary in this module (recursive's `_trim`, `_token_pieces`, etc. all treat a code point as the unit). Judgement: ACCEPTABLE — Issue 8's AC is about the token-size budget and U+FFFD, not grapheme integrity; a lone orphan ZWJ or split family-emoji is a pre-existing, codebase-wide code-point-granularity property (not a regression from this fix), and the actual corpus (technical Substack articles) essentially never contains ZWJ sequences. Flagged below as a follow-up, not a blocker.
- Stride probe (250 tokens, `size=100, overlap=20`): exactly 4 parents, sizes `[100,100,90,10]`, all distinct, last one ends with the final 20 chars (matches `test_overlap_stops_at_the_last_window_instead_of_repeating_the_tail`). (PASS)
- Stride probe (50-token doc, `size=100, overlap=20`, doc shorter than the window): exactly 1 parent = the whole text. (PASS)
- Stride probe (emoji, `size=5, overlap=2`, windows forced to shrink): with a MONOTONOUS single-emoji text, consecutive parent CONTENT looked identical (299/300 "same") — a false alarm from using an input where every character is literally the same glyph; re-ran with 10 DISTINCT multi-token emoji cycled — 300 parents, 0 consecutive duplicates, completes in 5ms. Consecutive parents genuinely advance; no near-duplicate explosion (the round-1-fixed bug: 1207 tokens → 511 near-identical tails). (PASS)
- Regression check: `fixed_tokens` on a 250-token plain-ASCII text is **byte-identical** to a restored round-1/Chapter-4 `_fixed_window` (imported the pre-round-2 module standalone and diffed parent AND child content lists — identical at both default and custom `size=100/overlap=20` configs). (PASS)
- Recursive strategy on the same emoji/CJK inputs: `<= size` holds throughout (verified directly and via the SWE's own new `TestMultiTokenCharacterBudget` parametrization); recursive's `_flush`/`_trim` step intentionally drops leading/trailing whitespace at chunk boundaries (pre-existing in the base commit, confirmed by diffing against `git show 8ac4c6e:.../chunking.py` — `_trim` is untouched), so recursive's own round-trip is not byte-exact on CJK text with spaces between ASCII runs (3 single-space characters dropped across ~28 chunks) — this is a PRE-EXISTING, non-regression, spec-consistent (`_flush`'s own docstring documents the trim) behavior unrelated to Issue 8, not re-broken or introduced by this round.
- Read all 10 new tests (`git diff` on `test_chunking.py`): `_assert_within_budget` re-encodes each chunk and compares its TOKEN COUNT to `size` (not just absence of `�`), and asserts every over-budget chunk is `len==1`; `test_token_offsets_track_the_true_cumulative_byte_count` pins the exact golden `_token_char_offsets("🧠"*5)` array; the SWE's own red/green table (round-1 code fails 6/134, round-2 code passes 134/134) reproduced independently.

**E2E adversarial pass — Issue 2 (chunking-config cache key, live Prefect proof)**
- Happy path, attempt 1 (reused document `6a9c60b5e26bba36f976aba9` from round-1-style testing): `Completed` → `Cached` (repeat, correct) → **`Cached` again after the flip to `fixed_tokens` + full process restart** — looked like a regression at first (contradicts the AC). Root-caused via `Inputs.compute_key` monkeypatching (class-level), `hash_objects`/JSON-serializer inspection, and a control experiment with a brand-new scratch document (same content) that behaved correctly: the anomaly was **test-session self-contamination**, not a code defect — I had ALREADY computed `clean_and_chunk_task` for (this exact document, this exact default `fixed_tokens` config) several times earlier in my OWN adversarial debugging (isolated Python probes using `ChunkingConfig(strategy=...)` with default sizes), warming BOTH cache slots for that specific document before ever running the "official" flip-and-restart check on it. Confirmed by: (a) a brand-new document with the SAME real article content correctly showed `Completed` for BOTH strategies in one script; (b) the SAME reused document with a never-before-tried `parent.size=4090` correctly showed `Completed` then `Cached` on repeat; (c) `cache_policy.compute_key()` on the real `Document` object genuinely returns different hashes per strategy (confirmed via direct JSON-dump length diff and MD5 comparison).
- Clean re-run with a GENUINELY FRESH document (ingested a local `.md` file — `cache_flip_notes.md`, new `Document _id=6a9c66f1beed88516f30f1de`, timestamp-verified never touched before):
  1. `TREE_MEMORY__MODE=rag make memory-serve-workflows` (docker `tree-prefect-worker` stopped first), default `recursive` — `make memory-run-memory-pipeline MODE=online DOC_IDS=6a9c66f1beed88516f30f1de` → task state **`Completed`** (fresh), log `clean_and_chunk: doc_id=... n_parents=1 n_children=1`; Mongo parent 0: `heading_path=["Cache Flip Test Note"]`.
  2. Killed the served process, restarted with `TREE_MEMORY__MODE=rag TREE_MEMORY__CHUNKING__STRATEGY=fixed_tokens make memory-serve-workflows`, re-ran task ① on the SAME doc id → task state **`Completed`** (NOT Cached), log line confirms the task body genuinely re-executed; Mongo parent 0: `heading_path=[]` (changed, as expected — `fixed_tokens` is structure-blind) and content ends with a preserved trailing `\n` (recursive's trims it) — a real content diff, not a coincidence.
  3. Repeat run 2's exact call (same `fixed_tokens` config, same doc) → **`Cached`**, correctly hitting the now-warm key — the flip-side of the AC also holds.
  This is Issue 2's #107 user story, reproduced live end-to-end: **PASS**.
- Cleanup: deleted both scratch `Document`s and the 17 "latent" substack sub-resources (images/cross-links) the first ingestion created; `memory` collection back to 0 rows; `documents` back to 303 (baseline). Killed both served-workflow processes; restarted `tree-prefect-worker` (confirmed `Up`, deployments re-registered). No `.tree/graphs` artefacts, `git status --short` shows only the expected pre-existing diff (no stray scratch files).

**Acceptance criteria**
- [x] PASS — Issue 8: `fixed_tokens` never emits U+FFFD AND no chunk exceeds its configured `size` (except a single indivisible character) — round-1 FAIL is fixed; verified via the SWE's 10 new tests, the red/green isolation proof, AND my own independent adversarial repros above (emoji+overlap, mixed ASCII/emoji, CJK, ZWJ, stride/shrink edge cases, ASCII-identical-to-round-1 regression check). Grapheme-cluster-vs-code-point carve-out semantics documented and judged acceptable (see Break path 4).
- [x] PASS — Live cache-flip proof (Issue 2 / #107 user story) — reproduced cleanly with a fresh document + full process restart: `Completed` (recursive) → flip + restart → `Completed` (fixed_tokens, NOT Cached) with a differing `heading_path`/content → repeat hits `Cached`. See detailed trace above.
- [x] PASS — Issues 1-7 spot-checked still pass: `build_nl_query_system_prompt()` still contains `subtype`/`parent_id`/`chunk_index`; all 4 Issue-4/5 greps still empty; `TestNonPositiveTopK` + `TestGroupChildrenByParent` (7 tests) + `test_blank_query_returns_the_standard_invalid_input_envelope` (3 tests) all green.
- [x] PASS — `make memory-format-check && make memory-lint-check && make memory-tests` green, 2441 ≥ 2400 — reproduced independently.
- [x] Tester full QA — PASS (this entry).
- [ ] PA acceptance review — not run; out of Tester scope, hands back to the orchestrator/PA.

**Evidence**
```
$ make memory-format-check && make memory-lint-check
266 files already formatted / All checks passed!

$ make pre-commit
prettier / ruff check / ruff format / biome check (harness) .... Passed

$ make memory-tests
============================ 2441 passed in 21.21s =============================

# Issue 8 — golden offsets, budget bound
offsets("🧠"*5) == [0,1,1,1,2,2,2,3,3,3,4,4,4,5,5,5]
300x🧠 parent.size=5 child.size=2: max_p_tok=3<=5 max_c_tok=3 (carve-out, len==1) no FFFD roundtrip=True
CJK parent.size=20 child.size=8: 28 parents/82 children, all <= size, no FFFD, roundtrip=True (both strategies)
ASCII 250-token fixed_tokens vs restored round-1 _fixed_window: byte-identical parent AND child lists

# Issue 2 — live proof (fresh doc 6a9c66f1beed88516f30f1de)
run1 recursive:      Completed   heading_path=["Cache Flip Test Note"]
run2 fixed_tokens:   Completed   heading_path=[]              <-- NOT Cached, content changed
run3 fixed_tokens repeat: Cached                               <-- unchanged config still hits
cleanup: documents=303 (baseline), memory=0, tree-prefect-worker Up, no stray files
```

**Other issues found**
- ZWJ / grapheme-cluster emoji sequences (e.g. `👨‍👩‍👧`) get torn into individual code-point fragments by `_token_char_offsets`'s "character" = code point, not grapheme cluster (see Break path 4). Not a spec violation, not a regression, negligible real-world impact on this corpus (technical Substack articles) — worth a follow-up ticket if the system ever ingests emoji-heavy chat/social content, not blocking this rollup.
- `_run_memory_extraction_body`'s "one trailing index run" (`ensure-kg-indexes` / `embed-kg-nodes`) executes unconditionally after every memory pipeline run regardless of `memory.mode`, including `rag` — confirmed harmless (no-op, nothing to index) and explicitly documented as mode-independent in the Makefile help text I already verified for Issue 5 ("no modes"), not a new finding, just confirming it's not a graphrag-leak.

**VERDICT: PASS**

Issue 8 is fixed and holds under adversarial testing across emoji, CJK, ZWJ, overlap, and stride edge cases, with no regression on the ASCII/Chapter-4 baseline. Issue 2's live cache-flip proof — the one item deferred from round 1 — now passes cleanly end-to-end with a fresh document and a full served-workflow restart (my first attempt on a reused, self-contaminated document was a false alarm from my own test methodology, root-caused and ruled out before concluding). Issues 1, 3, 4, 5, 6, 7 re-verified still green. Full suite green (2441 ≥ 2400), format/lint/pre-commit clean, environment cleaned up. Ready for orchestrator commit and PA re-review.
