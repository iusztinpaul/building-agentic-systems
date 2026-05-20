# Shared node-text embedding function for dedup + indexing

Status: pending
Tags: `data`, `refactor`, `P1`
Depends on: #040
Blocks: #042

## Scope

After this feature, BOTH dedup (new-node creation) and indexing
(backfill) must produce the persisted node `embedding` from the SAME
"node → embeddable text → vector" logic, using the SAME search model.
Today that logic only exists for indexing as
`_node_to_text` (private, in
`apps/memory/src/tree/memory/indexing/core.py`) and dedup embeds the bare
NAME instead. This task extracts node-text embedding into ONE shared
function so the two call sites cannot drift.

This is a **refactor task**: it introduces the shared function and routes
indexing through it, with NO change to what indexing persists (it already
embeds node-text). Dedup is migrated onto it in #042.

### New home

Create `apps/memory/src/tree/memory/embedding_text.py` (a new module at
the `memory/` layer, since both `extraction/` and `indexing/` import it —
neither should import from the other). It exposes:

- `node_to_embedding_text(node: dict[str, Any]) -> str` — the single
  canonical "node dict → embeddable text" builder. Move the current body
  of `indexing.core._node_to_text` here verbatim (type + headline name +
  non-content properties + content last).
- `embed_node_texts(nodes, embedding_model) -> list[list[float]]` — embed
  a list of node dicts by mapping each through
  `node_to_embedding_text` and calling `embedding_model.embed(...)`.
  (Real-time request batching is added in #043; for now a single
  `.embed(texts)` call is fine since the model already accepts a list.)

### Special-case reconciliation (call this out, do not silently drop)

`extraction/pipeline.py::_dispatch_entity_write` currently embeds
`properties.statement` for PREFERENCE nodes and `properties.object` for
FACT nodes (the #032 supersession contract), NOT the generic node-text.
That special-casing must be PRESERVED. So `node_to_embedding_text` is for
the GENERIC node-text path (used by indexing backfill and, after #042,
generic dedup). The preference/fact statement-embedding stays where it is
in `_dispatch_entity_write` and is OUT OF SCOPE for the shared function.
Document this boundary in the new module's docstring so a future reader
doesn't "unify" them and break supersession.

### Indexing migration (this task)

- `indexing/core.py::embed_nodes` / `_embed_batch` call
  `node_to_embedding_text` from the new module instead of the local
  `_node_to_text`.
- Remove the now-duplicate `_node_to_text` from `indexing/core.py` (or
  make it a thin re-export if other modules import it — grep first;
  prefer removing if unused outside the module).
- The search model used by indexing comes from
  `get_search_embedding_model()` (#040).

## Acceptance Criteria

- [x] New module `apps/memory/src/tree/memory/embedding_text.py` exists
      with `node_to_embedding_text(node)` and
      `embed_node_texts(nodes, embedding_model)`.
- [x] `node_to_embedding_text` produces byte-identical output to the
      pre-refactor `indexing.core._node_to_text` for the same input
      (regression test asserts this on at least 3 node shapes: name-only,
      name+properties, name+properties+content).
- [x] `indexing/core.py` no longer defines its own node-text builder; it
      imports `node_to_embedding_text` from the new module.
- [x] `grep -rn "_node_to_text" apps/memory/src` shows no surviving
      duplicate definition (only the new module's function, if named
      differently, plus any intentional re-export).
- [x] The preference/fact statement-embedding logic in
      `_dispatch_entity_write` is unchanged (diff shows no edits to that
      branch).
- [x] Integration test `test_indexing_pipeline.py` still passes — backfill
      embeds node-text via the shared function and writes vectors of
      `search_embedding.dimensions` length.
- [x] New-module docstring explicitly states the preference/fact
      statement-embedding path is intentionally separate.
- [x] `make memory-unit-tests` and `make memory-integration-tests` pass.
- [x] Format/lint/pre-commit clean.

## User Stories

### Story: Developer finds one place to change node-text embedding
1. Developer wants to add a node field (e.g. `subtype`) to the embeddable
   text.
2. They open `tree/memory/embedding_text.py` and edit
   `node_to_embedding_text` once.
3. Both indexing backfill and (after #042) dedup pick up the change — no
   second copy to keep in sync.

### Story: Indexing backfill produces the same vectors as before
1. Developer runs `make memory-serve-workflows &` then
   `make memory-run-memory-pipeline-indexing USER_ID=<oid>` against nodes
   lacking an `embedding`.
2. Each backfilled node's `embedding` is computed from its node-text via
   the shared function and the search model.
3. A node embedded before this refactor and re-embedded after produces an
   equivalent vector (same text in → same model → same vector).

### Story: Future maintainer does not break preference supersession
1. Maintainer reads `embedding_text.py`'s docstring.
2. They see the explicit note that PREFERENCE/FACT nodes embed
   `statement`/`object` elsewhere and must not be folded in.
3. They leave the supersession path alone.

---

Blocked by: #040

## Log

### [PM] 2026-05-20 — Grooming

**Summary**
Extracts `indexing.core._node_to_text` into a new
`tree/memory/embedding_text.py` (`node_to_embedding_text`) so dedup and
indexing share one node-text builder. Routes indexing through it now;
dedup follows in #042. Behavior-preserving for indexing.

**Key decisions**
- New module lives at the `memory/` layer so neither `extraction/` nor
  `indexing/` imports the other.
- Preference/fact statement-embedding (#032 contract) stays in
  `_dispatch_entity_write` and is explicitly OUT OF SCOPE for the shared
  generic builder — documented in the module docstring so it isn't
  "unified" later and breaks supersession.

**Dependencies**
- #040 — indexing now sources its model via `get_search_embedding_model()`.

**User stories**
- 3 stories: one place to change node-text; backfill produces same
  vectors; future maintainer doesn't break supersession.

Ready for implementation.

### [SWE] 2026-05-20 15:10 — Implementation

**Files modified**
- `apps/memory/src/tree/memory/embedding_text.py` (new) — shared
  `node_to_embedding_text(node)` (verbatim move of the old
  `indexing.core._node_to_text` body) + async
  `embed_node_texts(nodes, embedding_model)`. Module docstring documents
  that the PREFERENCE/FACT statement-embedding path is intentionally
  separate (#032 supersession contract) and must not be unified.
- `apps/memory/src/tree/memory/indexing/core.py` — removed local
  `_node_to_text`; imports `node_to_embedding_text` from the new module;
  `_embed_batch` now calls it. No other behavior changed.
- `apps/memory/tests/unit/memory/test_embedding_text.py` (new) — golden
  byte-identical regression on 3+ node shapes (name-only,
  name+properties, name+properties+content) plus headline fallback and
  missing-fields cases; `embed_node_texts` happy-path (single embed call,
  positionally aligned vectors) and empty-input no-call case.
- `apps/memory/tests/unit/memory/indexing/test_core.py` — dropped the
  `_node_to_text` import and the now-relocated `TestNodeToText` class
  (those assertions moved to the new module's test, upgraded to
  byte-identical golden literals).

**Tests**
- Unit: 1210 passing, 0 failing — `make memory-unit-tests` (output below).
- Integration (fast loop): 142 passing, 1 skipped, 70 deselected —
  `make memory-integration-tests`; `test_indexing_pipeline.py` 4 passing.

**Acceptance criteria**
- [x] New module exists with both functions — `embedding_text.py`.
- [x] Byte-identical regression on 3 shapes — `test_embedding_text.py::TestNodeToEmbeddingText`.
- [x] `indexing/core.py` imports the shared builder, defines none of its own.
- [x] `grep -rn "_node_to_text" apps/memory/src` — only docstring/comment
      references remain; zero surviving definitions or call sites.
- [x] `_dispatch_entity_write` unchanged — `extraction/pipeline.py` is NOT
      in `git diff` (preference/fact branch byte-for-byte untouched).
- [x] `test_indexing_pipeline.py` passes — backfill writes
      `search_embedding.dimensions` (1024) length vectors via shared fn.
- [x] Docstring states preference/fact path is intentionally separate.
- [x] Unit + fast integration tests pass.
- [x] Format/lint/pre-commit clean.

**Evidence**
```
$ grep -rn "_node_to_text" apps/memory/src apps/memory/tests
apps/memory/src/tree/memory/embedding_text.py:47:    ``indexing.core._node_to_text``): ... (docstring)
apps/memory/tests/unit/memory/test_embedding_text.py:5:  ... (docstring)
apps/memory/tests/unit/memory/test_embedding_text.py:44:  ... (docstring)
# zero surviving function definitions or call sites

$ make memory-unit-tests
... tests/unit/memory/test_embedding_text.py .......
... tests/unit/memory/indexing/test_core.py ............
============================ 1210 passed in 44.73s =============================

$ make memory-integration-tests
... tests/integration/memory/test_indexing_pipeline.py ....
========== 142 passed, 1 skipped, 70 deselected in 171.58s (0:02:51) ===========

$ uv run python -c "...exercise the new module..."
TEXT: 'person: Alice\nrole: engineer'
TEXT: 'chunk: Chunk 0\nsource_type: substack\nHello world'
embed_node_texts -> 2 vectors of dim 1024 (search_embedding.dimensions)
empty input -> []
```

**Notes**
- Behavior-preserving for indexing: the body of `node_to_embedding_text`
  is a verbatim copy of the old `_node_to_text` (same headline fallback
  order: `name` -> `canonical_name` -> `_id`; non-content props in dict
  order; `content` appended last). Regression test uses hard-coded golden
  literals, not recomputed-from-impl values, so a future layout change
  fails loudly.
- `embed_node_texts` is `async` because `BaseEmbeddingModel.embed` is
  async. The groomed spec wrote the signature without `async`; I made it
  async to match the real model contract. It issues a single
  `.embed(texts)` call (request batching deferred to #043 per scope).
- Preference/fact statement-embedding in `_dispatch_entity_write` left
  fully untouched — confirmed via `git diff --name-only` (no
  `extraction/pipeline.py` entry).
- Dedup is NOT yet migrated onto the shared function — that is #042's job,
  as the scope states. This task only routes indexing through it.
- Pre-existing display oddity: `core.py:339` reads
  `except (TypeError, ValueError):` — fine; the earlier `except TypeError,
  ValueError:` rendering was a viewer artifact, the module imports
  cleanly.

### [Tester] 2026-05-20 18:05 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check`, `make memory-lint-check`, `make pre-commit` all clean)
- Unit tests: 1210 passed / 0 failed (`make memory-unit-tests`, 43.07s) — `test_embedding_text.py` 7 passed
- Integration tests (full, mongot stack up): 211 passed / 1 failed / 1 skipped (`make memory-integration-tests-all`, 543.95s)
- Warnings: 0

**Integration failure classification — UNRELATED, not a #041 regression**
- `test_web_serp.py::TestLiveSerpSearch::test_empty_query_returns_empty_list` FAILED: a live Google SERP scrape returned tangentially-related YouTube ("Oddbods") near-matches for the nonsense query, defeating the test's empty-result contract. Re-run in isolation → SKIPPED (gated on a network/credential precondition), confirming it is an external-dependency live-network test, not deterministic. It lives in the `data/web` layer and touches no embedding/indexing code. #041 changes only `indexing/core.py` + the new `embedding_text.py`. Classified as a pre-existing flaky live-SERP test, orthogonal to this task. The #041-relevant integration test `test_indexing_pipeline.py` passed 6/6.

**E2E adversarial pass**
- Happy path (C — indexing embeds node-text end-to-end): `make memory-integration-tests-all` → `test_indexing_pipeline.py ......` (6 passed). Test asserts backfilled `node["embedding"]` length equals the model dimension (8 for the fake model) and the live `vector_index` converges to the configured `numDimensions`; in prod this is `search_embedding.dimensions=1024`. The embed→write path (`embed_nodes`→`_embed_batch`→`node_to_embedding_text`) is exercised; vector dim is driven by the model output, unchanged by the refactor. (PASS)
- Break path A (byte-identical text — behavior preservation): ran `node_to_embedding_text` vs an inline replica of the old `_node_to_text` over 6 shapes — (i) name+canonical+properties+content, (ii) `_id`-only fallback, (iii) empty properties, plus empty-dict, falsy-prop filtering, content-only. ALL byte-identical=True. Examples: `_id`-only → `'person: u:person:dave'`; empty-dict → `': '`; falsy-prop → `'note: N\nreal: kept'`. Golden unit tests assert exact equality (`text == "literal"`), not "contains" — confirmed by reading `test_embedding_text.py`. (PASS)
- Break path B (no surviving `_node_to_text`): `grep -rn "_node_to_text" apps/memory/src` → only one docstring reference in `embedding_text.py:47`; zero definitions/call sites. `indexing/core.py` removed the local def and imports `node_to_embedding_text`; `_embed_batch` routes through it and still writes vectors via `embedding_model.embed(...)` → `$set embedding` (dim = model dim = 1024 in prod). (PASS)
- Break path D (preference/fact embedding untouched): `extraction/pipeline.py` absent from both `git diff main..HEAD` and the uncommitted diff. The `_dispatch_entity_write` statement-embedding branch (lines 1102-1127) is intact: it embeds `properties.statement` (PREFERENCE) / `properties.object` (FACT) directly via `embedding_model.embed([statement_text])` and does NOT call the shared function. #032 supersession contract byte-for-byte unchanged. (PASS)
- Break path E (async signature sound): `embed_node_texts` is `async` to match `BaseEmbeddingModel.embed`. The sole caller delta is `_embed_batch`, which already `await`ed `embedding_model.embed(...)` and is unchanged in await structure. Runtime check: `await embed_node_texts([...], model)` → 2 positionally-aligned 1024-d vectors; `await embed_node_texts([], model)` → `[]` with no model call. No un-awaited-coroutine warnings in the 1210-passing unit run (0 warnings). (PASS)
- Boundary input (empty-dict / missing fields): `node_to_embedding_text({})` → `': '` (matches pre-refactor backward-compat behavior, asserted in `test_missing_fields_yields_separator_only`). No crash. (PASS)

**Acceptance criteria**
- [x] PASS — New module `embedding_text.py` with both functions — present; `node_to_embedding_text` + async `embed_node_texts`.
- [x] PASS — Byte-identical output on 3+ shapes — `test_embedding_text.py::TestNodeToEmbeddingText` (5 tests, exact-equality literals) + my 6-shape runtime diff, all identical.
- [x] PASS — `indexing/core.py` defines no node-text builder; imports the shared one — confirmed in diff (def removed, import added).
- [x] PASS — `grep -rn "_node_to_text" apps/memory/src` → docstring-only, zero defs/call sites.
- [x] PASS — `_dispatch_entity_write` preference/fact branch unchanged — `extraction/pipeline.py` not in diff; branch intact at core.py:1102-1127.
- [x] PASS — `test_indexing_pipeline.py` passes (6/6) — backfill writes model-dimension (1024 in prod) vectors via the shared fn.
- [x] PASS — Docstring states preference/fact path is intentionally separate — `embedding_text.py:16-27`.
- [x] PASS — `make memory-unit-tests` (1210) + `make memory-integration-tests` pass (the one full-suite failure is the unrelated live-SERP test, classified above).
- [x] PASS — Format/lint/pre-commit clean.

**Evidence**
```
$ make memory-unit-tests
tests/unit/memory/test_embedding_text.py .......
============================ 1210 passed in 43.07s =============================

$ make memory-integration-tests-all   # mongot stack up
tests/integration/memory/test_indexing_pipeline.py ......
============= 1 failed, 211 passed, 1 skipped in 543.95s (0:09:03) =============
# only failure: test_web_serp.py live Google SERP (unrelated to #041)

$ grep -rn "_node_to_text" apps/memory/src
apps/memory/src/tree/memory/embedding_text.py:47:    ``indexing.core._node_to_text``): ... (docstring only)

$ byte-identical runtime check (6 shapes) -> ALL BYTE-IDENTICAL: True
$ embed_node_texts -> nvecs=2 dim=1024 ; empty input -> []
```

**Other issues found**
- None blocking. The lone integration failure is a pre-existing flaky live-SERP test outside this task's surface (filed observation only; not introduced by #041). The SWE's note on `embed_node_texts` being `async` (spec wrote it sync) is a sound, necessary deviation — `BaseEmbeddingModel.embed` is async — and the only caller awaits it correctly.

**VERDICT: PASS**
