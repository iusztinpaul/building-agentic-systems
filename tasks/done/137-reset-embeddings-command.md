---
id: 137-reset-embeddings-command
status: done
feature: voyage-4-and-modal-embedding-catalog
---

# `make memory-reset-embeddings`: the Embedding reset for one user, with backfill text parity for PREFERENCE / FACT

Tags: `memory`, `rag`, `scripts`
Depends on: #136
Blocks: #141
Implements: ADR-009 — Decision 7 (Embedding reset instead of model stamping)

## Scope

voyage-4 vectors are not comparable to persisted voyage-3.5 vectors, rows carry no model stamp, and
the backfill only embeds rows whose `embedding` is empty (`rag/indexing.py::_backfill_filter`,
`"embedding": {"$in": [[], None]}`). The migration is: empty the vectors, then let the existing
indexing phase re-embed.

**1. One async function** in `src/tree/memory/rag/indexing.py`:
`async def reset_embeddings(client: AsyncMongoClient, database: str, user_id: PydanticObjectId, *, dry_run: bool = False) -> int`
- Filter = `user_id` + `kind: "node"` + `embedding` non-empty (`{"$exists": True, "$nin": [[], None]}`)
  + THE SAME eligibility `$or` the backfill uses (child chunks ∪ `LLM_EXTRACTABLE_NODE_TYPES`).
  Extract that `$or` into one helper both filters share, so a row the reset empties is by
  construction a row the backfill refills.
- `update_many` → `{"$set": {"embedding": [], "updated_at": <UTC now>}}` on the matched rows, and on
  the child-chunk subset also `cluster_id: None`, `viz: None` (a second `update_many` is fine).
  Why: `load_embedding_map` reads a child as current when `viz.run_id` equals the latest run id;
  re-embedding does not touch `viz`, so without this the **Embedding map** would silently show
  old-space coordinates. Clearing the fields makes the EXISTING warning fire ("N of M chunks have
  no cluster assignment (or a stale one) — run make memory-run-clustering-pipeline").
  `memory_clusters` rows are left alone — the next **Clustering run** replaces them wholesale.
- `dry_run=True` counts (`count_documents`) and writes nothing. Returns the matched/modified count.
  Logs `Embedding reset: user=%s database=%s rows=%d (children=%d) dry_run=%s` via the logger.
- Idempotent: a second call matches 0 rows and writes nothing.

**2. Backfill text parity.** Today `node_embedding_text` sends every non-child row through the
generic `node_to_embedding_text`, while the inline path (`graph/add_entity.py::_embeddable_text`)
embeds PREFERENCE on `properties.statement` and FACT on `properties.object` / `object_`. After a
reset the backfill would re-embed every preference/fact on the WRONG text and break supersession's
statement-vs-statement comparison. Move the per-type choice into the neutral
`tree/memory/embedding_text.py` (e.g. `entity_embedding_text(node: dict) -> str`: statement /
object when present and non-blank, else `node_to_embedding_text(node)`); `node_embedding_text` and
`_embeddable_text` both call it (`rag/` must NOT import `graph/` — ADR-006). `_embeddable_text`
keeps stripping `aliases` / `confidence` before delegating.

**3. Glue** — `apps/memory/scripts/reset_embeddings.py`: module-level `init_logger()`, `click` +
`tree.cli.user_options`, `--yes`; `init_mongodb` → `resolve_user_id` → `reset_embeddings(...,
dry_run=not yes)`. Without `--yes` it logs the dry-run line plus
`Nothing written. Re-run with CONFIRM=yes to reset N row(s).` and exits 1. With `--yes` the last
log line is `Next: make memory-run-indexing-pipeline (then make memory-run-clustering-pipeline if you use the Embedding map).`
No logic in the script beyond that.

**4. Make** — `apps/memory/Makefile` (next to `run-indexing-pipeline`):
`reset-embeddings: # EMBEDDING RESET for one user: empties every persisted vector (child chunks + entity nodes) and clears children's cluster_id/viz so make memory-run-indexing-pipeline re-embeds them with the CURRENT models.search_embedding. Needed after changing the embedding model or the Embedding role — rows carry no model stamp. Dry run unless CONFIRM=yes. Forwards USER_ID/USER_IDENTIFIER.`
→ `uv run python scripts/reset_embeddings.py $(USER_FLAGS) $(if $(filter yes,$(CONFIRM)),--yes,)`.
The root `memory-%` passthrough already exposes it as `make memory-reset-embeddings`.

**5. README** — a short "Changing the embedding model" subsection under the pipeline commands:
the three-step sequence (reset → indexing → clustering), that it is per user (loop over users on a
multi-user env), and that search returns `text_only`/empty vector hits between step 1 and step 2.

Write tests with `/squid-testing-python` against the local MongoDB the unit suite already uses.

## Out of scope
- Stamping rows with a model id; detecting stale vectors automatically; an `--all-users` flag.
- Deleting `memory_clusters` rows; re-running clustering automatically.
- Dropping / rebuilding `vector_index` (dimensions unchanged at 1024).

## Acceptance Criteria

- [x] Seed one user with: 3 embedded children (each with `cluster_id=2`, `viz={x,y,run_id}`), 1 parent (`embedding: []`), 1 `document` row, 2 embedded `person` nodes, 1 embedded `preference`; and a second user with 1 embedded child. `reset_embeddings(user_1)` returns `6`; all 6 rows have `embedding == []`; the 3 children have `cluster_id is None` and `viz is None`; the second user's child is byte-identical — `tests/unit/memory/rag/test_indexing.py::TestResetEmbeddings::test_resets_only_this_users_embedded_rows`.
- [x] A second call returns `0` and performs no write (`updated_at` unchanged) — `::test_is_idempotent`.
- [x] `dry_run=True` returns `6` and leaves every row untouched — `::test_dry_run_counts_without_writing`.
- [x] Every row emptied by the reset matches `_backfill_filter(user_id)` afterwards (reset ⊆ backfill) — `::test_reset_rows_are_backfill_eligible`.
- [x] `memory_clusters` rows are untouched — `::test_cluster_rows_are_left_alone`.
- [x] `node_embedding_text` returns `"prefers dark mode"` for a stored `preference` row with `properties.statement="prefers dark mode"`, the `object` string for a `fact` row, the generic node-text for a `person`, and falls back to generic node-text for a preference with a blank statement — `::TestNodeEmbeddingText::test_preference_and_fact_use_statement_and_object`.
- [x] For the same preference, `add_entity._embeddable_text(...)` and `node_embedding_text(stored_row)` return the identical string — `tests/unit/memory/graph/test_add_entity.py::test_inline_and_backfill_text_agree_for_preference_and_fact`.
- [x] `scripts/reset_embeddings.py` calls `init_logger()` at module level, contains no `print(`, and its `_run` only resolves the user and calls `reset_embeddings` — `tests/unit/scripts/test_reset_embeddings_script.py` (CliRunner, `reset_embeddings` patched): without `--yes` → exit code 1 and `dry_run=True`; with `--yes` → exit 0 and `dry_run=False`.
- [x] `make -n memory-reset-embeddings USER_IDENTIFIER=paul CONFIRM=yes` prints a command containing `--user-identifier "paul"` and `--yes`; without `CONFIRM` it contains no `--yes`.
- [x] `grep -c "memory-reset-embeddings" apps/memory/README.md` ≥ 1.
- [x] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green.

## User Stories

### Story: Operator previews the reset
1. `make memory-reset-embeddings`
2. Log: `Embedding reset: user=66f… database=tree rows=1842 (children=1710) dry_run=True` then `Nothing written. Re-run with CONFIRM=yes to reset 1842 row(s).`; exit 1.
3. `mongosh` shows every child still has its 1024-length vector.

### Story: Operator migrates a user to voyage-4
1. `make memory-reset-embeddings CONFIRM=yes` → `rows=1842 … dry_run=False`, then the `Next: make memory-run-indexing-pipeline …` line.
2. `make memory-run-indexing-pipeline` → the run logs `Embedded 1842 nodes in memory`.
3. `make memory-query-graph QUERY="how does the coordinator shard documents?"` returns parents again.

### Story: Operator runs the reset twice by accident
1. Immediately re-runs `make memory-reset-embeddings CONFIRM=yes`.
2. Log: `rows=0`; nothing is written; exit 0.

### Story: Operator opens the Embedding map after re-embedding
1. `make memory-visualize-embeddings` — first output line: `1710 of 1710 chunks have no cluster assignment (or a stale one) — run make memory-run-clustering-pipeline`.
2. After `make memory-run-clustering-pipeline` the warning is gone.

### Story: A user's preferences survive the migration
1. The user has the preference "prefers dark mode" (embedded inline on its statement).
2. After reset + indexing the row's vector is again the embedding of `"prefers dark mode"`, not of `"preference: …\nstatement: …"`, so a later "prefers light mode" still supersedes it.

---

Blocked by: #136

## Log

### [PA] 2026-09-19 17:43 — Grooming

**Summary**
A per-user, idempotent Embedding reset that empties exactly the rows the backfill refills, plus the backfill-text fix without which the reset would corrupt preference/fact vectors.

**Key decisions**
- Reset filter reuses the backfill's eligibility clause: a vector is only emptied if the backfill is guaranteed to rebuild it.
- Clear `cluster_id`/`viz` instead of only documenting: it costs one `$set` and turns a silent stale map into the warning the surfaces already print. `memory_clusters` is left for the next run to replace.
- `CONFIRM=yes` guard (PA addition, not in the grilled spec): the command is destructive on whichever env is active, prod included; the unguarded form is a dry run.
- Text parity lives in `memory/embedding_text.py` because `rag/` may not import `graph/` (ADR-006).

**Dependencies**
- #136 — the backfill must already embed as `document`, so the e2e pays for ONE re-embed.

**User stories**
- 5 stories: dry run, migrate, double run, stale map warning, preference parity.

Ready for implementation.

### [SWE] 2026-09-19 20:52 — Implementation

**Files modified**
- `apps/memory/src/tree/memory/embedding_text.py` — new `entity_embedding_text(node)`: the ONE per-type text choice (preference → `properties.statement`, fact → `properties.object`/`object_`, else generic node-text). Module docstring rewritten — it used to say PREFERENCE/FACT "must NOT be routed through this generic path", which the new function makes false.
- `apps/memory/src/tree/memory/graph/add_entity.py` — `_embeddable_text` keeps the shape job (strip `aliases`/`confidence`, build the persisted row) and delegates the type choice.
- `apps/memory/src/tree/memory/pipeline.py` — `_entity_embeddable_text` (a THIRD copy of the same branch, not named in the spec) delegates too; see Notes.
- `apps/memory/src/tree/memory/rag/indexing.py` — `_embeddable_row_clause()` (the eligibility `$or` shared by backfill + reset), `_reset_filter()`, `reset_embeddings()`; `node_embedding_text` now routes non-child rows through `entity_embedding_text`.
- `apps/memory/scripts/reset_embeddings.py` — new; module-level `init_logger()`, `user_options` + `--yes`, `init_mongodb` → `resolve_user_id` → `reset_embeddings`.
- `apps/memory/Makefile` — `reset-embeddings` target (verbatim help text from the spec), next to `run-indexing-pipeline`.
- `apps/memory/README.md` — "Changing the embedding model" subsection under Memory indexing.
- `apps/memory/tests/unit/memory/rag/test_indexing.py` — `TestResetEmbeddings` (6 live-Mongo tests) + `TestNodeEmbeddingText::test_preference_and_fact_use_statement_and_object`.
- `apps/memory/tests/unit/memory/graph/test_add_entity.py` — inline-vs-backfill text parity (parameterized over preference / fact / legacy `object_`, plus the blank-statement fallback).
- `apps/memory/tests/unit/scripts/test_reset_embeddings_script.py` — new; CliRunner over the mocked boundaries.

**Tests**
- Unit: 3209 passing, 0 failing (3194 before this task → +15). `make memory-tests`.
- Integration: N/A — this repo has no integration suite by design (AGENTS.md); e2e is the real command, below.

**Acceptance criteria**
All 11 ticked. Every one is covered by a named test except the two shell-verified ones (`make -n`, `grep -c README`), whose output is in Evidence.

**Evidence**

Full suite, after the final revert of the deliberate red-check below:
```
$ make memory-tests
======================= 3209 passed in 305.34s (0:05:05) =======================
```

The reset tests are not vacuous — temporarily keying the second `update_many` on the
filter instead of on the captured ids (the naive ordering: the first update makes the rows
stop matching `_reset_filter`, so the child write silently matches zero rows) turns the
headline test red, then green again on revert:
```
$ PYTEST_ADDOPTS="tests/unit/memory/rag/test_indexing.py::TestResetEmbeddings -q" make memory-tests
>       assert all(row["cluster_id"] is None for row in children)
E       assert False
1 failed, 5 passed in 5.81s
```

Make wiring:
```
$ make -n memory-reset-embeddings USER_IDENTIFIER=paul CONFIRM=yes
uv run python scripts/reset_embeddings.py  --user-identifier "paul" --yes
$ make -n memory-reset-embeddings USER_IDENTIFIER=paul
uv run python scripts/reset_embeddings.py  --user-identifier "paul"
$ grep -c "memory-reset-embeddings" apps/memory/README.md
2
```

**(a) DRY RUN against the REAL user — writes nothing** (`make env-status` → local):
```
$ make memory-reset-embeddings
Resolved target user: id=6a8ea9579a7aeb13175955c8 identifier=paul@example.com
Embedding reset: user=6a8ea9579a7aeb13175955c8 database=tree rows=2423 (children=1797) dry_run=True
Nothing written. Re-run with CONFIRM=yes to reset 2423 row(s).
make: *** [memory-reset-embeddings] Error 2      <- exit 1, as specified
```
Reconciled read-only against the raw collection: that user has **2423** embedded rows in
total (node + edge) and `_reset_filter` matches **2423** — the reset covers every embedded
row they own and nothing else. Breakdown: 1797 child chunks, 626 entity nodes across
`object`/`person`/`organization`/`event`/`location` **plus 64 `fact` and 12 `preference`
rows** — i.e. 76 rows that the text-parity half of this task exists to protect. Re-running
the dry run after all the write tests below still reports 2423: the real user's data was
never written to.

**(b) FULL WRITE PATH against a THROWAWAY user I seeded and deleted**
(`reset-e2e-throwaway@example.invalid`, id `6aaecba03b47a326a881424d` — a distinct tenant,
never the real user):
```
before: child-0 embedding=[0.1,0.2,0.3] cluster_id=2 viz={'x':1.0,'y':2.0,'run_id':'run-1'}
        child-1 embedding=[0.1,0.2,0.3] cluster_id=2 viz={...}
        parent-0 embedding=[] ; person:self embedding=[] ; preference:dark-mode embedding=[0.4,0.5,0.6]

$ make memory-reset-embeddings CONFIRM=yes USER_IDENTIFIER=reset-e2e-throwaway@example.invalid
Embedding reset: user=6aaecba03b47a326a881424d database=tree rows=3 (children=2) dry_run=False
Next: make memory-run-indexing-pipeline (then make memory-run-clustering-pipeline if you use the Embedding map).

after:  child-0 embedding=[] cluster_id=None viz=None
        child-1 embedding=[] cluster_id=None viz=None
        parent-0 / person:self unchanged (vector-less by design, never matched)
        preference:dark-mode embedding=[]

$ make memory-reset-embeddings CONFIRM=yes USER_IDENTIFIER=reset-e2e-throwaway@example.invalid
Embedding reset: user=6aaecba03b47a326a881424d database=tree rows=0 (children=0) dry_run=False   <- idempotent, exit 0

teardown: deleted user=6aaecba03b47a326a881424d and 5 memory row(s); 0 users, 0 rows left
```

**Notes**
- **What I ran against whose data, explicitly:** the real user (`paul@example.com`,
  `6a8ea9...55c8`) saw ONLY the dry run — no `CONFIRM=yes`, no `--yes`, no `dry_run=False`
  — twice, with identical counts before and after. Every write went to the throwaway
  tenant above (seeded by me, deleted by me) or to the unit suite's `unit_tests_twin`
  database. The real reset + re-index is task 141's, not this one's.
- **Root cause, not symptom (a third copy of the branch).** The spec names two call sites
  for the preference/fact choice; `grep` found a THIRD, `memory/pipeline.py::_entity_embeddable_text`
  (the batched extraction path, byte-identical to `add_entity._embeddable_text`). Patching
  only the two named ones would have left the pre-computed extraction vector able to drift
  from the backfill's. All three now call `entity_embedding_text`.
- **Residual duplication I did NOT remove.** `add_entity._embeddable_text` and
  `pipeline._entity_embeddable_text` still each build the persisted-row dict (strip
  `aliases`/`confidence`) — ~8 identical lines. That duplication predates this task and is
  orthogonal to it (the drift risk was the type branch, which is now shared). Worth a
  follow-up task, not a "while I was in there" edit.
- **`find(..., {"_id": 1})` rather than `distinct("_id", ...)`** to collect the ids: a
  `distinct` result must fit in one 16 MB BSON document (~300k ids), a ceiling a large
  tenant could plausibly reach. Two projected id reads + two `update_many`s keyed on
  `_id` — and capturing the ids BEFORE either write is load-bearing (see the red-check
  above).
- **Both counts come from the same snapshot**, so a dry run reports the `(children=N)`
  split for free and a run with 0 matches returns before issuing any write — which is what
  makes "a second call performs no write" assertable on `updated_at` rather than just on
  the return value.
- **Why the reset is load-bearing, not cosmetic** (carried forward from #136's QA, and now
  in the README): on live voyage-4, cos(query-role, role-less) = 0.983 vs
  cos(document-role, role-less) = 0.774 for the same text. Legacy role-less rows are not
  comparable with the new `document` vectors.
- **Dry run with 0 rows still exits 1** ("…to reset 0 row(s)"), per the spec's literal
  "without `--yes` … exits 1". Flagging it as a deliberate reading, not an oversight: a
  uniform exit code keeps the guard unambiguous in scripts.
- **One behavioural delta on the backfill path, disclosed not fixed.**
  `node_to_embedding_text` ends with `strip_invalid_chars` (drops control chars + lone
  surrogates); the statement/object branch returns the raw `.strip()`ed text, exactly as
  the inline path always has. So a preference whose STATEMENT carries such characters is
  now sent to Voyage unsanitized by the backfill too, where before the backfill quietly
  rescued it via the sanitized generic node-text. This is forced by the parity requirement
  (the two sides must build the same bytes) and is what the spec asks for literally; the
  blast radius is bounded by the existing 400 bisect-and-skip, which leaves such a row
  unembedded for a later run rather than failing the batch. The real user has 12
  preferences + 64 facts.
  > **Superseded by [SWE] 2026-09-19 22:15 — FIXED, not disclosed.** The "parity forces it"
  > reasoning was wrong: parity is equality AMONG the three call sites, not a constraint on
  > the exact bytes. `entity_embedding_text` now sanitizes the statement/object branch too,
  > which moves all three together. No unsanitized text reaches Voyage on any path.
- Not run: `make memory-run-indexing-pipeline` after a reset. It needs served workflows and
  would re-embed 2423 real rows on the live Voyage API — that is task 141's e2e, on
  purpose.

### [Tester] 2026-09-19 21:40 — QA

**Environment / safety statement (verbatim-checkable)**
`make env-status` → `local` throughout. `.env` / `.env.prod` were never opened, catted or
grepped. The real user (`paul@example.com`, `_id=6a8ea9579a7aeb13175955c8`) saw **dry run
only**, twice (`make memory-reset-embeddings`, no `CONFIRM`, no `--yes`): both times
`rows=2423 (children=1797) dry_run=True`, identical before and after every other action in
this session — the real user's data was never written to. Every write in this session went
to a throwaway tenant I seeded and deleted myself:
`qa-tester-throwaway-137@example.invalid` (`_id=6aaece361d487fcae8f74894`) — write path run
twice (once real reset, once idempotent no-op), then 5 memory rows + the user document
deleted. (An earlier seeding attempt that failed on a Pydantic validation error left one
partial user `6aaece1318366361bf162b4b` with 2 rows; also cleaned up before re-seeding.) No
writes went to the unit-test database's fixtures beyond what `make memory-tests` itself
manages and tears down.

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check`, `make memory-lint-check`,
  `make pre-commit` all green)
- Unit tests: 3209 passed / 0 failed (`make memory-tests`)
- Integration tests: N/A by design (AGENTS.md — no integration suite; e2e substitutes)
- Warnings: 0 pytest warnings; only an unrelated Python-3.14/opik/pydantic-v1
  `UserWarning` at script startup, pre-existing and unrelated to this diff

**E2E adversarial pass**
- Happy path: `make memory-reset-embeddings` (real user, no CONFIRM) → `Embedding reset:
  user=6a8ea9579a7aeb13175955c8 database=tree rows=2423 (children=1797) dry_run=True` then
  `Nothing written. Re-run with CONFIRM=yes to reset 2423 row(s).`, `make[1]: ***
  [reset-embeddings] Error 1` (PASS — matches the "Operator previews the reset" story
  exactly)
- Break path 1 (state edge — legacy `embedding: null` / missing `embedding` field, inserted
  via raw pymongo to bypass the ODM's list-type validator): seeded
  `person:none-embedding` (`embedding: None`) and `person:missing-embedding` (no
  `embedding` key) on the throwaway tenant alongside one real embedded child. Dry run
  reported `rows=1 (children=1)` — both degenerate rows correctly excluded from the reset
  (they'd be picked up by the *backfill* instead, since `_backfill_filter`'s
  `$in: [[], None]` matches null/missing while `_reset_filter`'s `$exists + $nin` does
  not) — reset ⊆ backfill holds even for these edge values. PASS.
- Break path 2 (must-never-touch — `kind: "edge"` row carrying an `embedding` field):
  seeded `{"kind": "edge", "type": "person", "embedding": [0.9, 0.9, 0.9]}` on the
  throwaway tenant. After `--yes`, the edge row's `embedding` was still `[0.9, 0.9, 0.9]`,
  untouched — the `kind: "node"` clause excludes it unconditionally. PASS.
- Break path 3 (malformed input — unknown `--user-identifier`): `uv run python
  scripts/reset_embeddings.py --user-identifier
  "nonexistent-user-qa-adversarial@example.invalid"` → uncaught `ValueError: No user found
  for identifier=...`, traceback to stderr, exit code 1, **no Mongo write attempted**
  (fails before `reset_embeddings` is ever called). This is `tree.entities.sessions.
  resolve_user_id`'s pre-existing, documented contract ("Raises ValueError... the calling
  entrypoint owns how a CLI surfaces that"), identical to every sibling
  `run-*`/`query-*` script. Not a regression introduced by this task; noted, not a FAIL.
- Break path 4 (idempotency / double-run, on the throwaway tenant): `--yes` run 1 →
  `rows=1 (children=1) dry_run=False`; inspected via raw pymongo — `child-0` now
  `embedding=[]`, `cluster_id=None`, `viz=None`, `updated_at` bumped to a fresh tz-aware UTC
  timestamp (`2026-09-19 18:02:51.932000+00:00`); the two degenerate rows and the edge row
  untouched, `updated_at` still the seed value. `--yes` run 2 → `rows=0 (children=0)
  dry_run=False`, exit 0. PASS.

**Acceptance criteria**
- [x] PASS — `reset_embeddings(user_1)` returns 6; 6 rows emptied; 3 children lose
      `cluster_id`/`viz`; user 2's child byte-identical —
      `tests/unit/memory/rag/test_indexing.py::TestResetEmbeddings::test_resets_only_this_users_embedded_rows`
      passes; confirmed live against the throwaway tenant (see above).
- [x] PASS — second call returns 0, no write —
      `::test_is_idempotent` passes; confirmed live (throwaway tenant run 2: `rows=0`,
      `dry_run=False`, exit 0, no further mutation).
- [x] PASS — `dry_run=True` returns 6, no write — `::test_dry_run_counts_without_writing`
      passes; confirmed live against the real user twice (2423/1797 both times, unchanged).
- [x] PASS — reset ⊆ backfill — `::test_reset_rows_are_backfill_eligible` passes; also
      confirmed live via the null/missing-embedding break path above.
- [x] PASS — `memory_clusters` untouched — `::test_cluster_rows_are_left_alone` passes.
- [x] PASS — `node_embedding_text` statement/object/generic/blank-fallback behavior —
      `::TestNodeEmbeddingText::test_preference_and_fact_use_statement_and_object` passes;
      read `apps/memory/src/tree/memory/embedding_text.py:250-266`.
- [x] PASS — inline vs backfill text agree for preference/fact —
      `tests/unit/memory/graph/test_add_entity.py::test_inline_and_backfill_text_agree_for_preference_and_fact`
      (parameterized: statement, `object`, legacy `object_`) passes, plus
      `test_inline_and_backfill_agree_on_the_generic_fallback`.
- [x] PASS — script contract (init_logger, no print, `_run` glue-only, exit codes) —
      `tests/unit/scripts/test_reset_embeddings_script.py` (4 tests) passes; manually
      confirmed `grep -n "print("` empty and `init_logger()` at module level
      (`apps/memory/scripts/reset_embeddings.py:43`).
- [x] PASS — `make -n memory-reset-embeddings USER_IDENTIFIER=paul CONFIRM=yes` →
      `uv run python scripts/reset_embeddings.py  --user-identifier "paul" --yes`; without
      `CONFIRM` → no `--yes`; also confirmed `USER_ID=abc123 CONFIRM=yes` →
      `--user-id "abc123"  --yes`. Command output captured directly.
- [x] PASS — `grep -c "memory-reset-embeddings" apps/memory/README.md` → `2`.
- [x] PASS — `make memory-format-check && make memory-lint-check && make pre-commit &&
      make memory-tests` all green (see Test summary).

**Evidence**
```
$ make memory-tests
======================= 3209 passed in 44.91s =============================

$ make memory-reset-embeddings           # real user, dry run only
Resolved target user: id=6a8ea9579a7aeb13175955c8 identifier=paul@example.com
Embedding reset: user=6a8ea9579a7aeb13175955c8 database=tree rows=2423 (children=1797) dry_run=True
Nothing written. Re-run with CONFIRM=yes to reset 2423 row(s).
make[1]: *** [reset-embeddings] Error 1
make: *** [memory-reset-embeddings] Error 2
                                                # re-run at end of session: identical 2423/1797

$ uv run python scripts/reset_embeddings.py --user-identifier "qa-tester-throwaway-137@example.invalid" --yes
Embedding reset: user=6aaece361d487fcae8f74894 database=tree rows=1 (children=1) dry_run=False
Next: make memory-run-indexing-pipeline (then make memory-run-clustering-pipeline if you use the Embedding map).

$ uv run python scripts/reset_embeddings.py --user-identifier "qa-tester-throwaway-137@example.invalid" --yes
Embedding reset: user=6aaece361d487fcae8f74894 database=tree rows=0 (children=0) dry_run=False
```

**BLOCKING FINDING — disclosed sanitization gap is a real, cheap-to-fix regression, not
an acceptable trade-off**

The SWE's report explicitly flags and asks for a judgment call on this delta (see the
"One behavioural delta" note above). I traced it and it does not hold up:

- `entity_embedding_text` (`apps/memory/src/tree/memory/embedding_text.py:250-266`)
  returns `statement.strip()` / `obj.strip()` directly for PREFERENCE/FACT, but falls
  through to `node_to_embedding_text`, which ends in `strip_invalid_chars(...)`, for
  everything else. A preference/fact whose stored text carries a control character or a
  lone surrogate is now sent to Voyage unsanitized on **both** the inline write path
  (`add_entity._embeddable_text`, `pipeline._entity_embeddable_text`) **and** the indexing
  backfill (`rag.indexing.node_embedding_text`) — previously only the inline path had this
  gap; the backfill used to sanitize via the generic node-text.
- The SWE's justification is "parity forces it." I checked whether the exact unsanitized
  bytes are load-bearing for the `_CachedSingleEmbedding` cache-key lookup
  (`pipeline.py:1607` `embeddings.vectors.get(embeddable_text)`, populated from
  `embeddable_text_by_key` at `pipeline.py:1122` and `pipeline.py:2150`) — they are not.
  All three call sites (`add_entity._embeddable_text`, `pipeline._entity_embeddable_text`,
  `rag.indexing.node_embedding_text`) build the identical persisted-row shape and hand it
  to the *same* `entity_embedding_text` function. Parity is an equality constraint among
  those three call sites, not a constraint pinning the bytes to any particular
  (unsanitized) form. Wrapping the two `return` statements in `strip_invalid_chars(...)`
  moves all three callers together — the cache key and the embedded text stay
  byte-identical to each other, just additionally sanitized. Nothing breaks.
- The fix only changes what is sent to Voyage as embedding input; `properties.statement` /
  `properties.object` on the persisted row are untouched, so supersession's
  statement-vs-statement text comparison and any exact-match/display logic are unaffected.
- This is strictly better and cheap: two one-line changes in
  `apps/memory/src/tree/memory/embedding_text.py` (`strip_invalid_chars` is already
  imported in that module):
  - `return statement.strip()` → `return strip_invalid_chars(statement.strip())`
  - `return obj.strip()` → `return strip_invalid_chars(obj.strip())`
  This is also a net improvement on the inline path, which never sanitized before this
  task either — not merely a restoration of prior backfill behavior.
- Fix required for PASS: apply the two-line change above, and add one regression test
  (naturally alongside `test_inline_and_backfill_text_agree_for_preference_and_fact` in
  `tests/unit/memory/graph/test_add_entity.py`) asserting that a preference whose
  `statement` carries a control character or lone surrogate produces identical,
  invalid-char-free text from `_embeddable_text` and `node_embedding_text`.

**Other issues found (non-blocking, PASS with note)**
- Unknown `--user-identifier` surfaces a raw Python traceback + `ValueError` to the
  operator's terminal rather than a clean CLI error message. Pre-existing, unmodified
  behavior of `tree.entities.sessions.resolve_user_id`, shared verbatim by every sibling
  `run-*`/`query-*` script — not a regression introduced by this task. Worth a follow-up
  task across all scripts, not blocking this one.
- `reset_embeddings.py::_run` duplicates the two steps `tree.cli.connect_and_resolve_user`
  already bundles (`init_mongodb` + `resolve_user_id`). Justified: `reset_embeddings`
  needs the raw `AsyncMongoClient` for `update_many`, which the existing helper discards.
  Not a real duplication smell.
- Race window between id-collection (`find(..., {"_id": 1})`) and the two `update_many`
  calls: a row that gains a fresh embedding in that window is simply excluded from this
  reset run (its new vector, not the stale one, survives) — no corruption, no data loss,
  and this is an offline operator command, not a concurrent hot path. Note only.
- `$in` with all 2423 of the real user's ids is ~75 KB, nowhere near Mongo's 16 MB
  document cap; the `find`-projection-over-`distinct` choice is the right call for a
  larger tenant. Confirms the SWE's design note, not an issue.

**VERDICT: FAIL**
One blocking issue: the disclosed `strip_invalid_chars` sanitization gap in
`entity_embedding_text` (`apps/memory/src/tree/memory/embedding_text.py`) is a genuine,
cheap, strictly-better fix that the SWE chose not to make. All 11 spec acceptance
criteria pass as evidenced above (checkboxes left ticked); the FAIL is this
adversarially-found, concrete, actionable gap plus its regression test, both required
before the next PASS.

### [SWE] 2026-09-19 22:15 — Fixes (QA round 1)

Single blocking finding accepted in full — the Tester is right and my "parity forces it"
justification was wrong. Parity is equality AMONG the three call sites, not a pin on the
exact (unsanitized) bytes; sanitizing inside the ONE chooser moves all three together.

**Files modified**
- `apps/memory/src/tree/memory/embedding_text.py` — `entity_embedding_text` now runs the
  PREFERENCE/FACT branch through `strip_invalid_chars` like the generic path; docstring
  rewritten (the delta is fixed, not disclosed).
- `apps/memory/tests/unit/memory/graph/test_add_entity.py` — 3 regression tests + the
  missing `_entity_embeddable_text` / `strip_invalid_chars` imports.
- `tasks/137-reset-embeddings-command.md` — superseded marker on the old "behavioural
  delta" note; this entry.

**The fix (one branch, not two `return`s)**
```python
if node_type == NodeType.PREFERENCE:
    special = properties.get("statement")
elif node_type == NodeType.FACT:
    special = properties.get("object") or properties.get("object_")
else:
    special = None

if isinstance(special, str):
    cleaned = strip_invalid_chars(special).strip()
    if cleaned:
        return cleaned

return node_to_embedding_text(node)
```
Deliberate deviation from the literal `strip_invalid_chars(statement.strip())` the Tester
wrote, for the empty-after-sanitizing edge the Tester asked me to order correctly:
**sanitize first, then `.strip()`**. Strip-then-sanitize has a hole — `"\x00 \x00"`
survives `.strip()` intact, passes the non-blank guard, and then sanitizes down to `" "`,
so the row would be embedded on a blank string (breaking this function's own contract).
Sanitize-then-strip collapses it to `""` and falls through to the generic node-text, the
same way a blank or missing statement does. Same reason `"  \x00 hello  "` yields
`"hello"`, not `" hello"`. Restructured into one `special` variable so the fallback is
reached from a single place instead of duplicating the guard per type.

Persisted `properties.statement` / `properties.object` are untouched — only the embedding
INPUT is cleaned — so supersession's text comparison and any display path are unaffected.

**Tests (regression, red before the fix)**
- `test_preference_text_is_sanitized_identically_on_all_three_paths` (parameterized:
  control char `\x00`, lone surrogate `\ud800`) — asserts
  `add_entity._embeddable_text == pipeline._entity_embeddable_text ==
  rag.indexing.node_embedding_text`, that the result is `"prefers dark mode"`, and that it
  is invalid-char-free (`strip_invalid_chars(t) == t`).
- `test_all_invalid_chars_statement_falls_back_to_the_generic_text` — `statement="\x00
  \ud800"` sanitizes to blank on all three paths and equals `node_to_embedding_text(row)`.
- Note: the fallback case is written on PREFERENCE, not FACT, on purpose — FACT's
  `object or object_` means a truthy-but-invalid `object` shadows a legacy `object_`. That
  shadowing pre-dates this task (raw `"\x00"` was truthy and returned then too) and is out
  of scope here; flagging it rather than widening the diff.

**Tests**
- Unit: 3212 passing, 0 failing (`make memory-tests`) — 3209 before + 3 new
- Integration: N/A by design (no integration suite; e2e substitutes)

**Acceptance criteria** — unchanged, all 11 still ticked; this round touched no AC behavior
(the `node_embedding_text` statement/object/generic/blank-fallback AC is now strictly
stronger: statement/object texts are sanitized too).

**Evidence**
```
$ make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check
295 files left unchanged / All checks passed! / 295 files already formatted / All checks passed!

$ make pre-commit
prettier..Passed  ruff check..Passed  ruff format..Passed  biome check (harness)..Passed

$ make memory-tests
============================ 3212 passed in 45.07s =============================

# RED check — same suite with the fix line reverted to the pre-fix `special.strip()`:
FAILED tests/unit/memory/graph/test_add_entity.py::test_preference_text_is_sanitized_identically_on_all_three_paths[control-char]
FAILED tests/unit/memory/graph/test_add_entity.py::test_preference_text_is_sanitized_identically_on_all_three_paths[lone-surrogate]
FAILED tests/unit/memory/graph/test_add_entity.py::test_all_invalid_chars_statement_falls_back_to_the_generic_text
======================= 3 failed, 3209 passed in 44.35s ========================
#   e.g. AssertionError: assert '\x00 \ud800' == 'preference: malformed\nstatement:  '
# Fix line restored, suite back to 3212 passed.

$ make env-status
Env target: local (.env)
```

**Notes**
- Safety: `make env-status` → `local` throughout. `.env` / `.env.prod` never opened. NO
  reset was run in this round at all — not even a dry run — against any tenant; the fix is
  pure text construction with no CLI or runtime surface, so there was nothing new to
  exercise there.
- `make memory-run-indexing-pipeline` after a reset: still NOT RUN — unchanged from the
  first round (live Voyage re-embed of 2423 real rows; that is task 141's e2e).
- **Swept for sibling callers of the same bug class** (the "fix the root cause, not the
  path the test names" check). `grep -rn 'get("statement")\|get("object")\|get("object_")'
  apps/memory/src/tree/` → three hits: the two inside `entity_embedding_text` (fixed) and
  `graph/preference_supersession.py:213` (`_preference_statement`). That third one is NOT
  an `entity_embedding_text` caller — it is a FOURTH, independent embed input:
  `preference_supersession._maybe_supersede` (`:417`) does
  `embedding_model.embed([new_statement], input_type="document")` on the raw `.strip()`ed
  statement and writes that vector on the superseding row. Left unchanged, deliberately:
  (a) it is pre-existing — that path never sanitized, before or after this task; (b) it is
  out of task 137's scope (spec item 2 is backfill-vs-inline text parity) and touching it
  widens the diff into the supersession resolver; (c) its blast radius is bounded and
  self-healing — the write is explicitly "intentionally redundant with the later
  `apply_writes` upsert", so the standard extraction path overwrites it with
  `add_entity`'s now-sanitized vector, and its `except Exception` already degrades a
  Voyage 400 to an empty `embedding` column rather than failing the flow. Concrete
  follow-up for a new task, one line: embed `strip_invalid_chars(new_statement)` there
  (keeping `new_statement` itself raw for the judge prompt and the slug). Flagging, not
  fixing.
- Not committed, per the lifecycle. Ready for re-review.

### [Tester] 2026-09-19 23:05 — QA round 2 (re-review)

**Environment / safety statement**
`make env-status` → `local` throughout. `.env` / `.env.prod` never opened, catted
or grepped. No `--yes`, no `CONFIRM=yes`, no `dry_run=False` run this round on
any tenant — this round's fix is pure text construction with no CLI/runtime
surface change, so nothing new needed exercising there. One dry run against the
real user (`paul@example.com`) to reconfirm continuity: `rows=2423
(children=1797) dry_run=True`, exit 2 (make) / 1 (script) — **identical to
round 1's counts**, confirming the fix changed no persisted data. No writes to
any tenant this round; unit-suite writes are the suite's own fixtures.

**Scope of this round**
Re-read `[Tester] 2026-09-19 21:40` (FAIL) and `[SWE] 2026-09-19 22:15` (fix).
Only `apps/memory/src/tree/memory/embedding_text.py` and
`apps/memory/tests/unit/memory/graph/test_add_entity.py` changed since round 1
(confirmed via `git diff --stat`) — no other file in the diff was touched, so
round-1 evidence for untouched files stands and is re-confirmed live below
where the AC depends on a live command.

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check`,
  `make memory-lint-check`, `make pre-commit` all green)
- Unit tests: 3212 passed / 0 failed (`make memory-tests`)
- Integration tests: N/A by design (AGENTS.md — no integration suite)
- Warnings: 0 pytest warnings; only the pre-existing, unrelated
  Python-3.14/opik/pydantic-v1 `UserWarning` at script startup

**Blocking-finding verification (the FAIL item)**
- Reverted-vs-fixed red check reproduced independently rather than trusted:
  ```
  >>> strip_invalid_chars("\x00 \x00".strip())   # strip-then-sanitize (the literal fix I originally asked for)
  ' '
  ```
  `' '` is **truthy** — it would pass `if cleaned:` and the row would be
  embedded on a blank-ish string, violating `entity_embedding_text`'s own
  no-blank-embed contract. The SWE's sanitize-then-strip ordering avoids this:
  `strip_invalid_chars("\x00 \x00").strip() == ''`, correctly falling through
  to `node_to_embedding_text`. **The ordering deviation from my round-1
  literal prescription is correct, verified empirically, not accepted on the
  SWE's assertion alone.**
- Own inputs run directly against `entity_embedding_text` (not just the
  suite):
  - `"\x00 \x00"` (preference statement) → falls back to generic node-text
    (non-blank: `"preference: x\nstatement:  "`), never a blank embed.
  - `"  \x00 hello  "` → `"hello"` (matches the SWE's claim exactly).
  - `"prefiere modo oscuro 🌙 — café"` (non-ASCII: emoji, em-dash, accents) →
    byte-identical round-trip, unmangled. This is the one place the fix
    could have silently corrupted real data (the real user has 12
    preferences + 64 facts) since sanitization now runs on text it never
    touched before — confirmed the regex only targets control chars/lone
    surrogates, not legitimate Unicode.
  - FACT `object`-invalid-shadows-`object_`-valid (`{"object": "\x00",
    "object_": "Bucharest"}`) → `entity_embedding_text` returns
    `"fact: x\nobject: \nobject_: Bucharest"` (falls back to generic
    node-text, which still surfaces "Bucharest" in the text). Confirmed via
    `git diff` that `properties.get("object") or properties.get("object_")`
    pre-dates this task verbatim (present in the code this task's diff
    REMOVES from `add_entity.py`/`pipeline.py`) — not introduced or altered
    by this round's fix. **Judgment: non-blocking, pre-existing, out of
    scope for 137.** It is also not a regression in the diagnostic sense:
    pre-fix, `"\x00".strip()` was truthy too, so the old code returned raw
    `"\x00"` verbatim → a guaranteed Voyage 400 → bisect-and-skip → the row
    left unembedded. Post-fix the same input degrades to embeddable,
    non-blank generic text that still contains "Bucharest" — strictly better,
    not worse. Worth a follow-up task (`object_ or object` fallback order, or
    check both and prefer whichever sanitizes non-blank), not a block here.
- `_CachedSingleEmbedding` key-vs-embedded-text parity (scope item 2): traced
  `pipeline.py:1122` (`embeddable_text_by_key[key] = _entity_embeddable_text(...)`)
  and `pipeline.py:2150` (`embeddable_texts = sorted(set(resolved.embeddable_text_by_key.values()))`,
  fed to `embed_entities_task`) against `pipeline.py:1607-1614` (cache lookup
  `embeddings.vectors.get(embeddable_text)`). Both the cache key and the text
  actually sent to Voyage are the same string returned by
  `_entity_embeddable_text` → `entity_embedding_text` for the same node — one
  function, one call, no place where sanitizing splits "the text used as key"
  from "the text embedded." Confirmed by reading, not just by the SWE's
  narrative.
- **preference_supersession.py:417** (the SWE's disclosed 4th, out-of-scope
  path): confirmed via `git diff --stat` / `git log -1` that this file has
  zero changes in this task's diff — untouched before and after this round.
  Per the task's own instruction, a pre-existing gap in a file this task
  never touches cannot be a regression this task introduced. Sharper
  discriminator beyond "pre-existing": the reset → backfill sequence this
  task ships (`reset_embeddings` empties vectors, `embed_nodes` backfill
  refills them) never runs through `_maybe_supersede` — that embed fires
  during LIVE extraction of a new statement, not during the backfill. So
  task 137 does not increase exposure to this gap; it is orthogonal to the
  ADR-009 §7 reset flow entirely. Combined with its own `except Exception` →
  empty-embedding degrade and the immediately-following `apply_writes`
  upsert overwriting it with `add_entity`'s now-sanitized vector, blast
  radius is bounded. **Judgment: acceptable as a follow-up outside this
  task, non-blocking.** Concrete one-line follow-up already specified by the
  SWE (`strip_invalid_chars(new_statement)` at that call site) — good enough
  to hand off as-is.

**Regression check — all 11 ACs**
- [x] PASS — `reset_embeddings(user_1)` returns 6 / cluster_id+viz cleared /
      other user untouched — `TestResetEmbeddings::test_resets_only_this_users_embedded_rows`
      passes (re-run this round: PASS).
- [x] PASS — idempotent second call, 0 rows, no write —
      `::test_is_idempotent` passes.
- [x] PASS — `dry_run=True` returns 6, untouched — `::test_dry_run_counts_without_writing`
      passes; live-reconfirmed against the real user this round: `rows=2423
      (children=1797) dry_run=True`, identical to round 1.
- [x] PASS — reset ⊆ backfill — `::test_reset_rows_are_backfill_eligible` passes.
- [x] PASS — `memory_clusters` untouched — `::test_cluster_rows_are_left_alone` passes.
- [x] PASS — `node_embedding_text` statement/object/generic/blank-fallback,
      NOW sanitized too — `TestNodeEmbeddingText::test_preference_and_fact_use_statement_and_object`
      passes; strengthened by the 3 new regression tests, all passing.
- [x] PASS — inline vs backfill text agree for preference/fact —
      `test_inline_and_backfill_text_agree_for_preference_and_fact` (3 params)
      + `test_inline_and_backfill_agree_on_the_generic_fallback` pass; extended
      this round by `test_preference_text_is_sanitized_identically_on_all_three_paths`
      (2 params) and `test_all_invalid_chars_statement_falls_back_to_the_generic_text`,
      all passing, all independently re-run by me (see Evidence).
- [x] PASS — script contract — `tests/unit/scripts/test_reset_embeddings_script.py`
      (4 tests) passes, unchanged this round.
- [x] PASS — `make -n memory-reset-embeddings USER_IDENTIFIER=paul CONFIRM=yes`
      → `uv run python scripts/reset_embeddings.py  --user-identifier "paul" --yes`;
      without `CONFIRM` → no `--yes`. Re-run live this round from repo root
      (evidence below) rather than inherited from round 1.
- [x] PASS — `grep -c "memory-reset-embeddings" apps/memory/README.md` → `2`.
      Re-run live this round.
- [x] PASS — `make memory-format-check && make memory-lint-check && make
      pre-commit && make memory-tests` all green this round (3212 passed).

**Evidence**
```
$ make memory-format-check && make memory-lint-check
295 files already formatted / All checks passed!

$ make pre-commit
prettier..Passed  ruff check..Passed  ruff format..Passed  biome check (harness)..Passed

$ make memory-tests
============================ 3212 passed in 44.84s =============================

$ make -k 'TestResetEmbeddings or ...' -> 18/18 targeted tests re-run individually, all PASSED
(TestResetEmbeddings x6, TestNodeEmbeddingText x1, test_inline_and_backfill x2,
test_preference_text_is_sanitized... x2, test_all_invalid_chars... x1,
test_reset_embeddings_script x4)

$ make memory-reset-embeddings                      # real user, dry run, this round
Resolved target user: id=6a8ea9579a7aeb13175955c8 identifier=paul@example.com
Embedding reset: user=6a8ea9579a7aeb13175955c8 database=tree rows=2423 (children=1797) dry_run=True
Nothing written. Re-run with CONFIRM=yes to reset 2423 row(s).
make: *** [memory-reset-embeddings] Error 2          # identical to round 1 — no data changed

$ make -n memory-reset-embeddings USER_IDENTIFIER=paul CONFIRM=yes
uv run python scripts/reset_embeddings.py  --user-identifier "paul" --yes
$ make -n memory-reset-embeddings USER_IDENTIFIER=paul
uv run python scripts/reset_embeddings.py  --user-identifier "paul"
$ grep -c "memory-reset-embeddings" apps/memory/README.md
2

$ python3 -c 'from tree.memory.rag.cleaning import strip_invalid_chars; print(repr(strip_invalid_chars("\x00 \x00".strip())))'
' '     # truthy — confirms the ordering deviation was necessary, not stylistic
```

**Other issues found (non-blocking, PASS with note — carried forward + this round's)**
- FACT `object or object_` truthy-shadow (see above): pre-existing, made
  strictly better (not worse) by this round's fix, follow-up task material.
- `preference_supersession.py:417` unsanitized `new_statement` embed:
  pre-existing, untouched by this task's diff, orthogonal to the reset →
  backfill flow this task ships, bounded blast radius, follow-up task
  material (one-line fix already specified by the SWE).
- Carried forward from round 1, still non-blocking: raw traceback on unknown
  `--user-identifier` (pre-existing, shared by sibling scripts); id-collection
  race window (offline operator command, no concurrent hot path); `$in` size
  headroom confirmed fine.

**VERDICT: PASS**
The round-1 blocking finding is fixed, verified independently (not trusted on
the SWE's report) with my own adversarial inputs including the exact
"\x00 \x00" edge case, a non-ASCII/emoji round-trip check, and the FACT
truthy-shadow case. The ordering deviation (sanitize-then-strip vs the
literal strip-then-sanitize I originally asked for) is empirically correct —
reproduced the failure mode myself. The two disclosed out-of-scope items
(FACT shadow, `preference_supersession.py:417`) are both genuinely
pre-existing and out of this task's diff — neither is a regression
introduced here, so neither blocks. All 11 ACs re-verified green,
`_CachedSingleEmbedding` key/text parity confirmed by reading the three call
sites, full suite green (3212 passed), format/lint/pre-commit clean, real
user saw dry-run-only with unchanged counts across both rounds. Ready to
commit.
