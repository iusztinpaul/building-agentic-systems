# Vector-space-swap migration runbook (voyage-multimodal-3 → voyage-3.5)

Status: pending
Tags: `docs`, `migration`, `ops`
Depends on: #048
Blocks: #053

## Scope

Document — **not script** — the operator migration triggered by Task #048's
default flip. Switching from `voyage-multimodal-3` to `voyage-3.5` keeps the
**same** dimensionality (1024) but produces vectors in a **DIFFERENT** vector
space. Therefore:

- Existing persisted node `embedding` values are **stale** (comparable
  dimension, incomparable geometry) — semantic search / dedup degrade silently.
- The dim-guard `assert_settings_match_live_vector_index`
  (`apps/memory/src/tree/memory/indexing/core.py`) will **NOT** catch this,
  because `numDimensions` is unchanged. There is no automatic signal — the
  runbook IS the signal.
- The fix is operator-driven re-extraction via the existing
  `RESET_ONTOLOGY=1` migration
  (`make memory-migrate-multi-tenancy USER_IDENTIFIER=<email> RESET_ONTOLOGY=1`,
  see `apps/memory/scripts/migrate_multi_tenancy.py` and the `apps/memory/Makefile`
  target), which wipes-and-rebuilds the KG so every node is re-embedded under
  the new model.

### IMPORTANT discrepancy this task must resolve

The feature spec assumes "the CLAUDE.md `[!CAUTION]` vector-space-change runbook
already documents this exact swap class." In **this worktree's** `CLAUDE.md`
(305 lines) that runbook section does NOT exist — it was authored on a different
branch (tracker #036, the `voyage-3` dim runbook) and is not present here.
`grep -n "vector space\|RESET_ONTOLOGY\|Embedding dimension mismatch" CLAUDE.md`
returns nothing. So this task **authors** the runbook section in
`CLAUDE.md`, it does not merely reference an existing one. (See the plan's
Open Questions — operator may instead point us at the canonical CLAUDE.md.)

### What to write

Add a discoverable sub-section to `apps/memory/.../CLAUDE.md` (repo-root
`CLAUDE.md`), under or near `## Configuration` / the migration area, titled with
a `[!CAUTION]` admonition, e.g. **"[!CAUTION] Vector-space change (same
dimension, different model) — re-extraction required."** The section must:

- State the swap class explicitly: same `dimensions`, different model ⇒ stale
  vectors ⇒ the dim-guard will NOT fire.
- Name the trigger: any change to `models.search_embedding.model` (or
  `resolution_embedding.model`) that keeps `dimensions` constant — with the
  `voyage-multimodal-3 → voyage-3.5` swap as the worked example.
- Give the exact recovery recipe: the `RESET_ONTOLOGY=1` migration command
  (with `DRY_RUN=1` first), and a note that plain re-indexing is INSUFFICIENT
  because `embed_unembedded_nodes` skips rows that already have an embedding
  (per the #036 note) — re-extraction is the canonical full refresh.
- Be greppable: include the literal phrases `vector space` and
  `RESET_ONTOLOGY` so an operator searching either lands here.
- Cross-reference the existing dim-mismatch runbook concept (the dim-guard
  catches dimension changes; THIS section covers the silent same-dim case the
  guard misses).

No code, no new Makefile target — the migration script and target already exist.

## Acceptance Criteria

- [x] `CLAUDE.md` gains a `[!CAUTION]`-flagged sub-section on the same-dimension
      vector-space swap.
- [x] The section states the dim-guard does NOT catch this swap class and
      explains why (dimension unchanged).
- [x] The section quotes the exact recovery command
      `make memory-migrate-multi-tenancy USER_IDENTIFIER=<email> RESET_ONTOLOGY=1`
      and recommends a `DRY_RUN=1` rehearsal first.
- [x] The section explains that re-indexing alone is insufficient
      (`embed_unembedded_nodes` skips already-embedded rows). *(Doc correctly
      uses the REAL function name `embed_nodes`; `embed_unembedded_nodes` does
      not exist in the codebase — accuracy-over-spec, verified correct.)*
- [x] `grep -n "RESET_ONTOLOGY" CLAUDE.md` and `grep -n "vector space" CLAUDE.md`
      each return at least the new section's line(s).
- [x] The `voyage-multimodal-3 → voyage-3.5` swap appears as the worked example.
- [x] `make pre-commit` passes (markdown lint, if any).

## User Stories

### Story: Operator pulls the branch and worries their search got worse
1. Operator deploys the #048 default flip; queries return subtly worse matches.
2. The dim-guard never fired (dimensions still 1024), so there was no crash.
3. Operator greps `CLAUDE.md` for `vector space` and lands on the new section.
4. The section tells them their persisted vectors are stale and exactly how to
   refresh: rehearse with `DRY_RUN=1`, then run the `RESET_ONTOLOGY=1` migration
   for the Paul Iusztin user.

### Story: Operator avoids the re-indexing trap
1. Operator's first instinct is to just re-run the indexing pipeline.
2. The runbook explicitly warns that `embed_unembedded_nodes` skips rows that
   already have an embedding, so re-indexing leaves stale vectors in place.
3. Operator instead runs the re-extraction migration and gets fresh vectors.

---

Blocked by: #048

## Log

### [SWE] 2026-05-21 — Implementation

**Files modified**
- `CLAUDE.md` — authored a fresh `> [!CAUTION]` vector-space-swap migration runbook under `## Configuration`, between the dimension-mismatch "Diagnosis tip" and the "macOS torch / TMPDIR shim" subsection (new lines 124-145). Documents the same-dimension-different-model hazard, why the dim-guard misses it, the `RESET_ONTOLOGY=1` recovery (with `DRY_RUN=1` rehearsal), the re-indexing trap, and the ~30-90s convergence window.

**Tests**
- Unit: 1301 passing, 0 failing — `make memory-unit-tests` (no-op pass; docs-only change touches no Python). Output below.
- Integration: N/A — docs-only, no infra/code changes.

**Acceptance criteria**
- [x] `CLAUDE.md` gains a `[!CAUTION]`-flagged sub-section on the same-dimension vector-space swap — `CLAUDE.md:124` (`> [!CAUTION]`).
- [x] States the dim-guard does NOT catch this swap class and explains why (dimension unchanged) — "Why nothing fires" paragraph, `CLAUDE.md:129`; names `assert_settings_match_live_vector_index` and the 1024 → 1024 invariant.
- [x] Quotes the exact recovery command `make memory-migrate-multi-tenancy USER_IDENTIFIER=<email> RESET_ONTOLOGY=1` and recommends a `DRY_RUN=1` rehearsal first — `CLAUDE.md:139` (DRY_RUN rehearsal) and `:143` (real run).
- [x] Explains re-indexing alone is insufficient — "Recovery — re-extract, do not just re-index" paragraph at `CLAUDE.md:135` describes `embed_nodes` backfill semantics (`embedding: {"$in": [[], None]}` skips already-embedded rows).
- [x] `grep -n "RESET_ONTOLOGY" CLAUDE.md` and `grep -n "vector space" CLAUDE.md` each return the new section's lines — verified (RESET_ONTOLOGY: 135/139/143; vector space: 127).
- [x] The `voyage-multimodal-3 → voyage-3.5` swap appears as the worked example — `CLAUDE.md:127`.
- [x] `make pre-commit` passes — prettier/ruff/biome/KGQuery all Passed; prettier left CLAUDE.md unchanged.

**Evidence**
```
$ grep -c "CAUTION\|vector-space\|Embedding dimension mismatch" CLAUDE.md   # pre-edit
0

$ make pre-commit
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed
KGQuery discipline (memory)..............................................Passed

$ make memory-unit-tests
============================ 1301 passed in 41.50s =============================

$ grep -n "RESET_ONTOLOGY" CLAUDE.md
135:... `RESET_ONTOLOGY=1` migration ...
139:... RESET_ONTOLOGY=1 DRY_RUN=1
143:... RESET_ONTOLOGY=1
$ grep -n "vector space" CLAUDE.md
127:... different vector space ...
```

**Notes**
- AC text references `embed_unembedded_nodes`; the actual function in `apps/memory/src/tree/memory/indexing/core.py` is named `embed_nodes` (its docstring documents the "backfill, not a re-embedder" skip semantics the AC describes). I referenced the real function name + its `embedding: {"$in": [[], None]}` filter so the runbook stays accurate against the codebase. Flagging for Tester awareness.
- Generalized the warning to the swap *class* (any same-dimension model change) with voyage-3.5 as the worked example — this deliberately addresses the #038 review nit that the prior block read as voyage-3-specific/historical.
- Section placed adjacent to the existing "Diagnosis tip" so the two cases (dim-guard-caught dimension change vs. silent same-dimension change) sit side by side; the runbook explicitly cross-references that contrast.
- This is the right (worktree-root) CLAUDE.md on `feat/dream-consolidation`. No production code touched. NOT COMMITTED — awaiting Tester PASS + PM ACCEPT per role.

### [Tester] 2026-05-21 14:50 — QA

**Test summary**
- Format / lint / pre-commit: PASS — `make pre-commit` all hooks Passed (prettier, ruff check, ruff format, biome, KGQuery discipline); prettier left CLAUDE.md unchanged (diff stat stable at 24 insertions, so the markdown is prettier-stable).
- Unit tests: 1301 passed / 0 failed (`make memory-unit-tests`, 41.21s) — no-op pass, docs-only change touches no Python.
- Integration tests: N/A — docs-only change; no infra/code path touched.
- Warnings: 0.

**E2E adversarial pass** (docs task → operator-discoverability + factual-accuracy break paths)
- Happy path: operator greps `CLAUDE.md` for `vector space` → lands on line 127 (the `[!CAUTION]` block), which explains stale vectors + the exact recovery. PASS.
- Break path 1 (discoverability — symptom terms a confused operator would actually type): grepped `semantic search` (1), `stale` (2), `re-extract` (3), `DRY_RUN` (2), `embed_nodes` (1), `convergence` (1) — every realistic search term lands inside the new block. PASS.
- Break path 2 (factual accuracy — phantom function/flag): confirmed `embed_unembedded_nodes` exists in NEITHER the codebase NOR the doc (`grep -rn` apps/memory/src = none; CLAUDE.md = none). Doc uses the REAL `embed_nodes` and its real filter `embedding: {"$in": [[], None]}`. All quoted flags `--reset-ontology` (script line 683) and `--dry-run` (line 667) are real argparse options with dedicated handlers `_run_reset_ontology` / `_print_reset_ontology_dry_run_plan`. PASS.
- Break path 3 (markdown well-formedness): verified the `> [!CAUTION]` GitHub admonition blockquote is unbroken — every line 124-146 is a `>`-continuation or the trailing blank; no bare line breaks the blockquote, so the admonition renders intact. PASS.

**Acceptance criteria**
- [x] PASS — `[!CAUTION]` sub-section on the same-dimension vector-space swap — `CLAUDE.md:124` (`> [!CAUTION]`).
- [x] PASS — states dim-guard does NOT catch this and why (dimension unchanged) — "Why nothing fires" para `CLAUDE.md:129`; cross-checked against `assert_settings_match_live_vector_index` (`indexing/core.py:437`), which compares `numDimensions` vs configured `dimensions` and returns `None` on match (lines 454-455). Doc's claim is accurate.
- [x] PASS — quotes exact recovery command + `DRY_RUN=1` rehearsal — `CLAUDE.md:139` (DRY_RUN) and `:143` (real run). Verified `migrate-multi-tenancy` is a real target (`apps/memory/Makefile:152`) mapping `RESET_ONTOLOGY=1`→`--reset-ontology`, `DRY_RUN=1`→`--dry-run`, `USER_IDENTIFIER`→`--identifier`. Exact match.
- [x] PASS — re-indexing-insufficient explanation — `CLAUDE.md:135` describes `embed_nodes` backfill + `embedding: {"$in": [[], None]}` filter. Verified against `indexing/core.py:54-99`: function IS `embed_nodes`, filter IS `{"$in": [[], None]}` (line 85), docstring confirms backfill/skip semantics (lines 60-66). SWE used the REAL name (`embed_unembedded_nodes` does not exist) — accuracy-over-spec, confirmed CORRECT. A doc quoting a phantom function would have been worse.
- [x] PASS — grep anchors present — `RESET_ONTOLOGY` at 135/139/143; `vector space` at 127.
- [x] PASS — `voyage-multimodal-3 → voyage-3.5` worked example — `CLAUDE.md:127`. Cross-checked `default.yaml`: `search_embedding.model: voyage-3.5`, `dimensions: 1024`, with inline comment "1024-d is unchanged from voyage-multimodal-3 (#048)". Same-dim claim is factually correct.
- [x] PASS — `make pre-commit` passes — see Test summary.

**Hazard correctness (load-bearing safety message)** — VERIFIED CORRECT. Doc states: same dimension (1024) ⇒ `assert_settings_match_live_vector_index` does NOT raise ⇒ silent failure (degraded `$vectorSearch`/dedup) ⇒ operator-driven re-extraction is the only signal. Matches the dim-guard code exactly. The `RESET_ONTOLOGY=1` path's behavior (drops `knowledge_graph`, re-creates `person:self`, ensures indexes, triggers `memory-extraction-etl` + `memory-indexing-etl`) matches `migrate_multi_tenancy.py` (`_run_reset_ontology`, docstring steps 46-60). No contradiction with the existing "Diagnosis tip" — doc explicitly contrasts the dim-change case (guard catches) vs the silent same-dim case (guard misses).

**Scope** — VERIFIED: `git diff --stat` touches ONLY `CLAUDE.md` (+24 lines). Tracker files are untracked, not part of the code diff. No production code modified.

**Evidence**
```
$ git diff --stat
 CLAUDE.md | 24 ++++++++++++++++++++++++

$ make pre-commit
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed
KGQuery discipline (memory)..............................................Passed

$ make memory-unit-tests
============================ 1301 passed in 41.21s =============================

$ grep -rn "embed_unembedded_nodes" apps/memory/src/   # phantom function
(no matches — confirmed absent from codebase; doc correctly uses embed_nodes)
```

**Other issues found**
- None. The SWE's accuracy-over-spec note (real `embed_nodes` vs spec's `embed_unembedded_nodes`) was the correct call and is fully verified.

**VERDICT: PASS**
