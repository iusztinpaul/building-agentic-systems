---
status: done
feature: rag-graphrag-modes
---

# [PR review rollup] Modular memory — vanilla RAG and GraphRAG modes over one `memory` collection

Tags: `rollup`, `pr-review`
Refs: PR #41 (branch: `feat/rag-graphrag-modes`, HEAD `08cd632`)

## Scope

PR Reviewer found 1 Blocker and 8 Nits in the diff (`git diff main...HEAD`, 155 files, +12877/-2953). The SWE must fix the Blocker (and may fix Nits at their discretion) in a single coordinated pass, then hand back to the Tester. Pipeline re-runs from QA → PA acceptance → push → re-review.

The Blocker is small and mechanical; everything else reviewed is sound — see the review log on `tasks/done/112-pa-rejection-rag-graphrag-modes.md` for the evidence summary (multi-tenancy pins, idempotency, INPUTS cache keys, `$literal` replace, tool gating, ADR-006 vs code, glossary, 2441/2441 tests, format + lint clean).

## Acceptance Criteria

- [x] Blocker 1: `tree.memory.embedding_text.embed_node_texts` is deleted (or has a production caller). `grep -rn "embed_node_texts" apps/memory/src apps/memory/scripts` returns nothing; the four `embed_node_texts` tests in `apps/memory/tests/unit/memory/test_embedding_text.py` (`# embed_node_texts` section, ~L170 onwards) are retargeted to `embed_texts` (the batching / caps / positional-alignment claims are the same) or dropped where they duplicate `embed_texts` coverage; the module docstring of `test_embedding_text.py` no longer names `embed_node_texts`.
- [x] Tester re-runs full QA suite (`make memory-format-check && make memory-lint-check && make memory-tests`) and PASSES.
- [ ] PA re-runs acceptance review and ACCEPTS.
- [ ] PR Reviewer re-runs and reports `NO BLOCKERS`.

## Blockers (detail)

### 1. [Clean code] — `apps/memory/src/tree/memory/embedding_text.py:223-243`
- **What's wrong:** `embed_node_texts` has no production caller any more. On `main` its only caller was `tree.memory.indexing.core._embed_batch`; this diff rewired that caller to the new `embed_texts` seam (`rag/indexing.py:_embed_batch` now builds the texts itself via `node_embedding_text`) and left `embed_node_texts` behind as a 20-line wrapper kept alive only by `tests/unit/memory/test_embedding_text.py`.
- **Why it's a Blocker:** dead code the diff made unreachable (Severity Rule, dimension B: "functions ... that the diff makes unreachable").
- **Suggested fix:** delete `embed_node_texts`; move the batching/caps assertions in its tests onto `embed_texts` (which is what the backfill actually calls). If the SWE believes a second consumer is imminent, name it in the PR — otherwise `embed_texts` + `node_to_embedding_text` is the whole API.
- **Regression test (if applicable):** none — behaviour is unchanged; the existing `embed_texts` path is already exercised by `tests/unit/memory/rag/test_indexing.py::TestEmbedNodesIsBackfillOnly`.

## Nits (non-blocking; will be appended to PR description if pipeline advances)

### 1. [Clean code] — `apps/memory/src/tree/memory/graph/extraction.py:~520` (`upsert_graph_entries`)
- **Suggestion:** pre-existing dead code (zero production callers on `main` too), but this diff touched it (docstring + `MEMORY_COLLECTION`). Delete it in the same sweep as Blocker 1 so `graph/extraction.py` holds only what `pipeline.py` calls (`extract_entities`, `build_structural_entries`).

### 2. [Untested] — `apps/memory/src/tree/memory/rag/retrieval.py:208-236` (`_document_meta` fallback)
- **Suggestion:** the "document row missing → fall back to the parent's denormalised `properties`" branch has no test (`test_retrieval.py` covers a missing PARENT row, not a missing DOCUMENT row). Add one case asserting the WARNING and that `DocumentMeta.title/source_uri` come from the chunk row. Edge case the spec did not require, hence Nit.

### 3. [Standards] — `apps/memory/scripts/query_graph.py:50`
- **Suggestion:** `logging.basicConfig(level=logging.INFO)` instead of `init_logger()` from `tree.logging` (AGENTS.md "Entry-point scripts ... must call `init_logger()` at module level"). Pre-existing, but the script was rewritten in this PR — fix while there (`run_memory_pipeline.py` shows the pattern).

### 4. [Documentation] — `apps/memory/README.md` "Memory modes" snippet
- **Suggestion:** `mongosh "$MONGO_URI" --eval 'db.memory.drop()'` — no `MONGO_URI` variable exists in `.env.example` or the Makefiles (the URI is assembled from `MONGO_SCHEME/HOST/PORT/...`), and without a database in the URI `db` is not `tree`. Use the form the `run-pipelines-e2e` skill already ships: `--eval 'db.getSiblingDB("tree").memory.drop()'` with the component env vars.

### 5. [Documentation] — `docs/notes/slm-extraction-finetuning-spec.md:210,327,339`
- **Suggestion:** still imports and calls `chunk_document` (deleted by #108) and describes "512-token / 64-overlap" chunks; the snippet no longer runs. Point it at `tree.memory.rag.cleaning.clean_text` + `tree.memory.rag.chunking.split_document` and "one LLM call per parent chunk".

### 6. [Documentation] — `apps/memory/README.md:14` ("Nodes use `_id = "type:name"`")
- **Suggestion:** ids have been `{user_id}:type:name` since #018 (`build_node_id`); the line was edited in this diff (collection rename) — correct the id format in the same line.

### 7. [Clean code] — `apps/memory/src/tree/memory/graph/dedup.py:358-361` (comment)
- **Suggestion:** the comment says `merged_into` "is not declared as a filter-path on the vector index (only ``kind`` and ``type`` are)" — `_VECTOR_INDEX_FILTER_PATHS` declares `user_id, kind, type, subtype, merged_into`. Pre-existing stale comment, touched by this diff's path rename; fix the sentence (or promote the post-`$match` to a pre-filter as the indexing docstring already suggests).

### 8. [Simplicity] — `apps/memory/src/tree/memory/rag/cleaning.py:261-280` (`_outside_fences`), PA nit #1
- **Suggestion:** classified Nit. The closing fence line keeps its own `\n` inside the fenced range, so a 3+ newline run right AFTER a closing fence collapses to 3 (two blank lines) instead of 2, and the docstring's "no run of spaces or newlines is ever split across the boundary" is false at the closing fence. Cosmetic, idempotent, no content change; either exclude the closing fence's trailing `\n` from the range or soften the docstring.

### Classified, no action

- **Grapheme-cluster note** (`_token_char_offsets` / `_assert_within_budget` treat "character" as a code point, so a ZWJ sequence can be torn across chunks): Nit-level follow-up only if emoji-heavy content is ever ingested; consistent with every other character boundary in the module. Already recorded under "Follow-ups" in the PR body — nothing to add.
- **PA nit #2 (#110 Story 4 expects `edges_written: 0` — historical story text):** not a code or doc-discipline finding; task logs are archives, the model-visible contract in `tree-memory/SKILL.md` is correct.

---

Refs: PR #41

## Log

### [SWE] 2026-09-05 19:55 — Implementation

Blocker 1 + all 8 Nits fixed in one pass, on `feat/rag-graphrag-modes` (no per-task branch).

**Files modified**

- `apps/memory/src/tree/memory/embedding_text.py` — **Blocker 1**: deleted the callerless `embed_node_texts` wrapper; module docstring now names the real API (`node_to_embedding_text` + `embed_texts`) and says why they are separate.
- `apps/memory/tests/unit/memory/test_embedding_text.py` — `TestEmbedNodeTexts` → `TestEmbedTexts`, driving `embed_texts` with `node_to_embedding_text`-built texts (same single-call / empty-input / 1000-input-cap claims), plus one new case pinning that the builder's control-char stripping is what reaches the model. Module docstring no longer names `embed_node_texts`.
- `apps/memory/src/tree/memory/graph/extraction.py` — **Nit 1**: deleted the zero-caller `upsert_graph_entries` and everything only it used (`_MAX_ALIASES`, `_MAX_SOURCES`, the "3. Persistence" banner, and the `datetime`/`PydanticObjectId`/`UpdateOne`/`MEMORY_COLLECTION`/`build_node_id`/`build_edge_id` imports). Module docstring: the module no longer writes to Mongo. The live caps live in `graph/add_entity.py` (its own `_MAX_ALIASES`/`_MAX_SOURCES`) and are untouched.
- `apps/memory/tests/unit/memory/graph/test_extraction.py` — dropped `TestUpsertGraphEntriesArrayCaps` (3 tests) and its two pipeline-walking helpers; sole subject was the deleted dead function.
- `apps/memory/tests/unit/memory/rag/test_retrieval.py` — **Nit 2**: new case for the `_document_meta` fallback (document row missing → parent still returned, metadata off the denormalised chunk row, WARNING naming both ids).
- `apps/memory/scripts/query_graph.py` — **Nit 3**: `logging.basicConfig` → `init_logger()` from `tree.logging` at module level (AGENTS.md entry-point rule).
- `apps/memory/README.md` — **Nit 4**: the Memory-modes drop snippet now uses the component env vars + `getSiblingDB("tree")`, byte-identical to the `run-pipelines-e2e` skill (no `$MONGO_URI` — it does not exist). **Nit 6**: node `_id` is `{user_id}:type:name` per `build_node_id`.
- `docs/notes/slm-extraction-finetuning-spec.md` — **Nit 5**: side note A and the dataset driver now use `clean_text` + `split_document` (recursive parent/child, cl100k_base 4096/0 and 256/32) with one LLM call per PARENT chunk; the snippet's imports resolve again.
- `apps/memory/src/tree/memory/graph/dedup.py` — **Nit 7**: the comment now states that `merged_into` IS one of the five `_VECTOR_INDEX_FILTER_PATHS` and that the exclusion stays a post-`$match` because "absent OR null" has no pre-filter form (`null` is not a filterable BSON type).
- `apps/memory/src/tree/memory/rag/cleaning.py` — **Nit 8**: behaviour fixed, not the docstring. `_outside_fences` hands the closing fence line's own `"\n"` back to the outside part, so a 3-newline run after a fence collapses to one blank line exactly like it does after a paragraph. Fenced content is untouched and every rewrite keeps ≥1 newline, so the fence line still ends where it did.
- `apps/memory/tests/unit/memory/rag/test_cleaning.py` — two regression cases for the boundary (collapse parity with plain prose; code byte-identical across the collapse) plus a `_FIXTURES` entry so idempotence/determinism cover it.

**Tests**

- Unit: 2444 passing, 0 failing — `make memory-tests`.
- Integration: N/A — this repo has no integration suite (AGENTS.md); e2e is the real-pipeline run below.
- Count arithmetic: 2441 (baseline) − 3 (`TestUpsertGraphEntriesArrayCaps`, dead-code-only) + 1 (`embed_texts` control-char case) + 1 (`_document_meta` fallback) + 2 (fence-boundary cases) + 2 (new `_FIXTURES` entry × idempotent/deterministic) = **2444**. No test was weakened; the only deletions are the 3 whose sole subject was the deleted `upsert_graph_entries`.

**Acceptance criteria**

- [x] Blocker 1 — `grep -rn "embed_node_texts" apps/memory/src apps/memory/scripts` → no matches (exit 1); repo-wide only the task archives mention it. Tests retargeted (`tests/unit/memory/test_embedding_text.py::TestEmbedTexts`), module docstring updated.
- [ ] Tester re-run — Tester's gate.
- [ ] PA acceptance — PA's gate.
- [ ] PR Reviewer re-run — Reviewer's gate.

**Evidence**

```
$ grep -rn "embed_node_texts\|upsert_graph_entries" apps docs README.md .agents
(no matches, exit 1)

$ make memory-format-check && make memory-lint-check
266 files already formatted
All checks passed!

$ make memory-tests
2444 passed in 22.93s

$ make pre-commit
prettier / ruff check / ruff format / biome check (harness) — Passed
```

E2E (`run-pipelines-e2e`, `rag` mode, local env, workflows served FROM this worktree — the
Dockerized `tree-prefect-worker` was stopped first because it ran main's code and wrote to
`knowledge_graph`; restarted afterwards). A markdown fixture whose fenced block is followed by a
3-newline run was ingested and re-queried:

```
$ TREE_MEMORY__MODE=rag make memory-run-memory-pipeline MODE=online DOC_IDS=6a9c71e6…
Done. Flow completed successfully.

$ mongosh … --eval 'db.getSiblingDB("tree").memory.aggregate([...])'
node/document           1 (embedding [])
node/chunk/parent       1 (embedding [])
node/chunk/child        1 (embedding 1024)
edges                   0                      # rag mode, as ADR-006 says

parent content: "…```python\ndef retrieve(query: str) -> list[str]:\n    #comment stays a comment\n
   if query:\n        return  [\"parent\"]\n\n\ndef other():\n    pass\n```\n\nThe three newlines…"
   -> ONE blank line after the closing fence (Nit 8 fix), while the fenced body keeps its two
      blank lines, its "#comment" and its 4-space indent byte-for-byte.

$ TREE_MEMORY__MODE=rag make memory-query-graph QUERY="parent document retrieval"
Retrieving parents for user_id=…: 'parent document retrieval' (top_k=10)   # init_logger works
Parent-document retrieval: 1 child hit(s) -> 1 parent(s), returning 1
[0.033] fence-boundary.md — Parent-document retrieval in Tree …  matched children: 1

# then, with the document row deleted, the same query exercises the Nit 2 branch for real:
Document row …:document:…fence-boundary.md missing for parent …#parent-0; falling back to the
denormalised chunk metadata
[0.033] fence-boundary.md — Parent-document retrieval in Tree …
```

**Notes**

- DB left as found: the fixture document and its 3 memory rows were deleted, plus the 3
  `knowledge_graph` rows the stale Docker worker wrote during the first (discarded) dispatch.
  `memory` 0 rows / `documents` 303 / `knowledge_graph` 195 — the pre-run state. Serve process
  stopped, `tree-prefect-worker` restarted, no `.tree/graphs` artefacts (rag writes none).
- `graph/add_entity.py`'s `_MAX_ALIASES` / `_MAX_SOURCES` `$slice` caps — the LIVE write path — have
  no unit test of their own; the caps tests deleted here only ever covered the dead
  `upsert_graph_entries` copy. Pre-existing gap, worth its own task; not fixed here to keep this
  rollup to the reviewed findings.
- Not committed — awaiting Tester PASS.

### [Tester] 2026-09-05 20:40 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check`, `make memory-lint-check`, `make pre-commit` all clean)
- Unit tests: 2444 passed / 0 failed (`make memory-tests`, 21.27s)
- Integration tests: N/A — repo has no integration suite (AGENTS.md); e2e verification below
- Warnings: 0

**E2E adversarial pass**

Happy path — re-ran the full local suite green (2444/2444) and confirmed the SWE's live `rag`-mode
run in the log above is coherent with the code: `_document_meta` fallback branch, `init_logger()`
at module level, README mongosh line, fence-boundary fix. I did not re-dispatch a live Prefect run
myself (the SWE's e2e evidence above is sufficient and re-running risks polluting the real `tree`
DB for no new signal); instead I drove the exact functions the Blocker/Nits touch directly, which
lets me hit shapes an e2e run with one fixture can't reach:

- Break path 1 (boundary/malformed: adversarial `_outside_fences`/`clean_text` fence shapes) —
  ran `clean_text` on: unclosed fence at EOF (with/without trailing newline, 3+ blank lines inside
  the code), adjacent fences separated by 1 blank line / 2 blank lines / touching with none,
  fence as the very last characters with/without a trailing newline, CRLF input, and
  fence→blank→heading→fence. For every case: (a) `fenced_ranges` before/after `clean_text` extract
  byte-identical fenced bodies, and (b) `clean_text(clean_text(x)) == clean_text(x)` (idempotent).
  The 2-blank-line-after-fence case collapses to 1 blank line, matching prose parity — PASS.
  Script: `/private/tmp/.../scratchpad/adversarial_fences.py` and `adversarial_fences2.py`.
- Break path 2 (state edge: does the fix leak into chunk boundaries anywhere else) — loaded the
  pre-fix `cleaning.py` (`git show HEAD:apps/memory/src/tree/memory/rag/cleaning.py`, HEAD=`08cd632`,
  the commit before these uncommitted changes) as an isolated module and diffed
  `split_document(clean_text(...), app_config.memory.chunking)` before vs. after on 3 real docs
  (`data/notes/system-architecture-future-ai-apps.md`, `docs/notes/prefect-execution-topologies.md`,
  `apps/memory/README.md`, all containing fenced code): 0 parent-boundary differences on any of the
  3 (none of them happen to have a 3-newline run right after a closing fence). To prove the fix is
  live and *localized*, I appended a fence-then-3-newlines block to README.md's content and re-ran
  the same before/after diff: exactly 1 of 2 parents differed (the one containing the injected
  fence), and the diff was exactly the two extra `\n` characters the fix collapses — every other
  parent byte-identical. PASS — the fix moves chunk boundaries ONLY where the newline collapse
  changed the input, not elsewhere in a document. Script: `.../scratchpad/split_diff.py`.
- Break path 3 (dependency-facing: doc/CLI surfaces this rollup rewrote) — (a) ran the README
  mongosh drop line verbatim against a scratch DB (`tester_scratch_qa_113`, not `tree`) on the
  local Docker Mongo: insert → count 1 → run the exact snippet (`db.getSiblingDB(...).drop()`) →
  count 0 → confirmed `tree.memory` untouched (0 rows, matches the SWE's pre-run baseline) → dropped
  the scratch DB. PASS. (b) Import-checked the `slm-extraction-finetuning-spec.md` snippet's
  `tree.*` imports (`app_config`, `get_ontology_schema`, `_SYSTEM_PROMPT`, `split_document`,
  `clean_text`) plus a stub `build_label.py`, then ran `split_document(clean_text(text), config)`
  end-to-end (minus the real LLM call) — all imports resolve, `_SYSTEM_PROMPT.format(...)` builds,
  `split_document` returns chunks with `.content`. PASS. (c) `uv run python scripts/query_graph.py
  --help` — clean `--help` output, `init_logger()` (module-level, before `--help` short-circuits)
  runs without error, no `print()` calls in the file (`grep -n "print(" scripts/query_graph.py` →
  empty). PASS.

**Acceptance criteria**
- [x] PASS — Blocker 1: `embed_node_texts` deleted, no production caller — `grep -rn
      "embed_node_texts" apps/memory/src apps/memory/scripts` → no matches (exit 1); repo-wide
      grep (excluding `tasks/`) also empty. `TestEmbedNodeTexts` → `TestEmbedTexts` in
      `apps/memory/tests/unit/memory/test_embedding_text.py`, retargeted onto `embed_texts` with
      the SAME single-call / empty-input / 1000-input-cap claims (read diff line-by-line — no
      claim was dropped, one new control-char case added), module docstring no longer names
      `embed_node_texts`.
- [x] PASS — Tester re-run of the full QA suite — `make memory-format-check && make
      memory-lint-check && make memory-tests` → format/lint clean, 2444 passed, 0 failed, 0
      warnings (this session, independent of the SWE's reported numbers).
- [ ] PA re-run — PA's gate, not mine.
- [ ] PR Reviewer re-run — Reviewer's gate, not mine.

**Nit verification (spot-checked, non-blocking but confirmed genuinely fixed)**
- Nit 1 — `upsert_graph_entries`, `_MAX_ALIASES`, `_MAX_SOURCES` deleted from `extraction.py`;
  `grep -rn "upsert_graph_entries"` empty outside `tasks/`; the 3 deleted tests
  (`TestUpsertGraphEntriesArrayCaps`) read as solely exercising the deleted function (confirmed by
  reading the removed diff hunk); `add_entity.py`'s own `_MAX_ALIASES`/`_MAX_SOURCES` untouched (no
  diff on that file).
- Nit 2 — new `test_missing_document_row_falls_back_to_the_chunk_metadata` genuinely exercises the
  fallback: `make_collection` in the test seeds only parent rows (no `make_document_row`), so
  `_fetch_nodes` for `document_ids` returns `{}` and `_document_meta` hits its `row is None` branch
  for real — not a false-positive from the default-denormalized fixture data.
- Nit 3 — `query_graph.py` uses `init_logger()` at module level; confirmed by direct read + `--help`
  run above.
- Nit 4/6 — README mongosh line matches `.agents/skills/run-pipelines-e2e/SKILL.md` byte-for-byte;
  `_id` format matches `build_node_id`'s docstring (`"{user_id}:{type}:{name}"`).
- Nit 5 — finetuning doc snippet imports resolve (verified above); `chunk_document` no longer
  referenced anywhere in the doc.
- Nit 7 — `dedup.py` comment now correctly states `merged_into` IS in
  `_VECTOR_INDEX_FILTER_PATHS` (confirmed the tuple has 5 entries including `merged_into`) and the
  post-`$match` reasoning (null not filterable) is accurate.
- Nit 8 — fixed at the behavior level (not just the docstring); regression tests added; verified
  independently via the adversarial fence-shape pass above.

**Evidence**
```
$ make memory-format-check
266 files already formatted

$ make memory-lint-check
All checks passed!

$ make pre-commit
prettier / ruff check / ruff format / biome check (harness) — Passed

$ make memory-tests
2444 passed in 21.27s

$ grep -rn "embed_node_texts\|upsert_graph_entries" apps/memory/src apps/memory/scripts
(no matches, exit 1)
```

**Other issues found**
- None new. The SWE's own follow-up note (`graph/add_entity.py`'s live `$slice` caps have no
  direct unit test) is a legitimate pre-existing gap, correctly scoped out of this rollup — worth
  its own task, not a blocker here.

**VERDICT: PASS**
