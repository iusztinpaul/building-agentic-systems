---
id: 137-reset-embeddings-command
status: pending
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

- [ ] Seed one user with: 3 embedded children (each with `cluster_id=2`, `viz={x,y,run_id}`), 1 parent (`embedding: []`), 1 `document` row, 2 embedded `person` nodes, 1 embedded `preference`; and a second user with 1 embedded child. `reset_embeddings(user_1)` returns `6`; all 6 rows have `embedding == []`; the 3 children have `cluster_id is None` and `viz is None`; the second user's child is byte-identical — `tests/unit/memory/rag/test_indexing.py::TestResetEmbeddings::test_resets_only_this_users_embedded_rows`.
- [ ] A second call returns `0` and performs no write (`updated_at` unchanged) — `::test_is_idempotent`.
- [ ] `dry_run=True` returns `6` and leaves every row untouched — `::test_dry_run_counts_without_writing`.
- [ ] Every row emptied by the reset matches `_backfill_filter(user_id)` afterwards (reset ⊆ backfill) — `::test_reset_rows_are_backfill_eligible`.
- [ ] `memory_clusters` rows are untouched — `::test_cluster_rows_are_left_alone`.
- [ ] `node_embedding_text` returns `"prefers dark mode"` for a stored `preference` row with `properties.statement="prefers dark mode"`, the `object` string for a `fact` row, the generic node-text for a `person`, and falls back to generic node-text for a preference with a blank statement — `::TestNodeEmbeddingText::test_preference_and_fact_use_statement_and_object`.
- [ ] For the same preference, `add_entity._embeddable_text(...)` and `node_embedding_text(stored_row)` return the identical string — `tests/unit/memory/graph/test_add_entity.py::test_inline_and_backfill_text_agree_for_preference_and_fact`.
- [ ] `scripts/reset_embeddings.py` calls `init_logger()` at module level, contains no `print(`, and its `_run` only resolves the user and calls `reset_embeddings` — `tests/unit/scripts/test_reset_embeddings_script.py` (CliRunner, `reset_embeddings` patched): without `--yes` → exit code 1 and `dry_run=True`; with `--yes` → exit 0 and `dry_run=False`.
- [ ] `make -n memory-reset-embeddings USER_IDENTIFIER=paul CONFIRM=yes` prints a command containing `--user-identifier "paul"` and `--yes`; without `CONFIRM` it contains no `--yes`.
- [ ] `grep -c "memory-reset-embeddings" apps/memory/README.md` ≥ 1.
- [ ] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green.

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
