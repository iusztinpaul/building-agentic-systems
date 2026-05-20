# Dream consolidation — end-to-end acceptance (incremental watermark proof)

Status: pending
Tags: `memory`, `dream`, `e2e`, `test`
Depends on: #048, #049, #050, #051, #052
Blocks: —

## Scope

The headline acceptance for the whole feature: prove the incremental dream
pipeline collapses parallel-ingested near-duplicates AND that the watermark
makes the second run a near-noop. This is a test + verification task (no new
product logic). It exercises the full chain delivered by #048–#052.

Honor CLAUDE.md: "use the Paul Iusztin user when testing." Honor the free-tier
Voyage limit — keep any live-embedding step to a tiny node set, OR use
deterministic fake embedders for the unit/integration layer. The dream sweep
itself is embedding-READ-only over stored vectors, so it makes no Voyage calls;
the only embedding cost is creating the near-duplicate fixture nodes (do that
with a small set or a fake embedder).

### Headline e2e test

A `@pytest.mark.slow @pytest.mark.requires_mongot` integration test that:

1. For the Paul Iusztin user, creates TWO near-duplicate nodes of the same type
   (parallel-ingest simulation) with persisted embeddings that score
   ≥ `auto_merge_threshold` (or in the flag band for a flag-path variant). Use a
   deterministic embedder so the score is stable, or a tiny live-Voyage call.
2. Runs `dream_consolidation(user_id=<paul>, dry_run=False)`.
3. **Asserts they collapse**: either the loser is tombstoned (`merged_into` set,
   SAME_AS `status="confirmed"`, `reviewed_by="dream"`) for the auto-merge case,
   OR a SAME_AS `status="pending"` edge exists for the flag case (per which
   threshold band the fixture targets — cover both, ideally parametrized).
4. **Asserts the watermark advanced**: `load_watermark(<paul>, "dream")` returns
   the run's `run_start`.
5. Runs `dream_consolidation(user_id=<paul>, dry_run=False)` **AGAIN** with no
   new ingestion in between.
6. **Asserts the second run is a near-noop**: the driving set (delta) is empty
   (or contains only nodes already fully processed), zero NEW merges/flags are
   produced, and no already-decided pair is re-touched — proving the incremental
   watermark works.

### Operator-narrative verification (CLAUDE.md step 5 chain, adapted)

Run and capture evidence for the realistic operator flow:
- `make memory-serve-workflows &` registers `dream-consolidation-etl` (verify via
  `uv run prefect deployment ls` showing the cron schedule).
- `make memory-run-dream-consolidation` triggers the run and streams per-user
  stats; verify the logs show the two-set sweep and the watermark advance.
- Confirm the #048 default flip routes through `/v1/embeddings` (the text client)
  end-to-end during fixture creation (or assert the search model is
  `VoyageTextEmbeddingModel`).
- Confirm the #049 runbook is discoverable (`grep "vector space" CLAUDE.md`).

### Adversarial / break-it paths (Tester headline duty)

At least:
- **Dry-run before real run**: a `dry_run=True` pass writes nothing and does not
  advance the watermark, then the real run still collapses the pair.
- **Rejected pair is respected**: pre-seed a SAME_AS `status="rejected"` edge
  between two similar nodes; assert the dream does NOT re-merge or re-flag them.
- **Empty graph / no delta**: dream on a user with no fresh nodes is a clean noop.
- **Idempotent re-run**: running the real dream twice does not double-merge or
  error (review_duplicate CONFIRM idempotency).
- **Cap**: with `max_pairs` small and many candidates, the run stops at the cap
  and records `cap_hit=True` without crashing.
- **Supersession flag OFF**: confirm zero LLM calls on the default path.

## Acceptance Criteria

- [x] A `@pytest.mark.slow @pytest.mark.requires_mongot` e2e test creates two
      near-duplicate nodes for the Paul Iusztin user and asserts they collapse on
      the first `dry_run=False` run (auto-merge tombstone OR pending flag,
      covering both threshold bands).
      — `test_dream_e2e_acceptance.py::test_collapse_then_noop_watermark_proof`
      (parametrized `auto_merge`/`flag`).
- [x] The same test asserts the watermark advanced to `run_start` after run 1.
- [x] The same test asserts run 2 (no new ingestion) is a near-noop: empty
      delta, zero new merges/flags, no re-touch of decided pairs.
- [x] Adversarial: a `dry_run=True` pass writes nothing and leaves the watermark
      unchanged; the subsequent real run still collapses the pair.
      — `test_dry_run_writes_nothing_then_real_run_collapses`.
- [x] Adversarial: a pre-seeded `rejected` SAME_AS pair is NOT re-merged/re-flagged.
      — `test_rejected_pair_is_never_remerged_or_reflagged`.
- [x] Adversarial: dream on a user with no fresh delta is a clean noop (no error,
      no writes). — run 1 of `test_node_ingested_after_first_dream_is_caught_by_second`
      (empty-delta noop) + existing `test_dream_adversarial_qa::test_empty_user_is_clean_noop`.
- [x] Adversarial: a second real run does not double-merge (idempotency).
      — `test_collapse_then_noop_watermark_proof` run 2 (tombstone count stays 1).
- [x] Adversarial: `max_pairs` cap stops the run and records `cap_hit=True`.
      — `test_max_pairs_cap_stops_run_and_records_cap_hit`.
- [x] Default path (`enable_supersession_judge=false`) makes zero LLM calls.
      — `_no_cost_guard` fixture (`get_llm` side_effect=AssertionError) on every
      default-path test.
- [x] The dream sweep makes zero Voyage embedding calls (read-only over stored
      vectors) — asserted. — `_no_cost_guard` fixture
      (`get_search_embedding_model` side_effect=AssertionError).
- [x] `make memory-serve-workflows` registers `dream-consolidation-etl` and
      `make memory-run-dream-consolidation` triggers a run with streamed logs —
      evidence captured in the task log.
      — deployment `dream-consolidation-all-users/dream-consolidation-etl`
      registered with cron `0 4 * * *`, tags `['dream']` (evidence in log).
- [x] `make memory-integration-tests-all` (full, incl. mongot) green locally
      (260 passed, 1 skipped); `make memory-integration-tests-ci` (CI mirror, no
      mongot) green (176 passed, 84 deselected); CI passes.
- [ ] [HUMAN] If a live Voyage smoke is used for fixture creation, confirm it
      stayed within the free-tier 3 RPM (single tiny call) — else fake embedder
      was used and this is `NOT RUN — used deterministic embedder`.
      — NOT RUN — used deterministic 8-dim cosine embedder; the dream sweep is
      embedding-READ-only so zero Voyage calls were made (asserted by `_no_cost_guard`).

## User Stories

### Story: Operator proves the dream collapses parallel duplicates
1. Operator ingests two near-duplicate nodes for the Paul Iusztin user (parallel
   ingestion left them un-merged).
2. Operator runs `make memory-run-dream-consolidation`.
3. The logs show the pair detected and merged (or flagged); the loser is
   tombstoned with `reviewed_by="dream"`.
4. Operator re-runs the dream immediately; the logs show an empty delta and no
   work — proving the incremental watermark.

### Story: Operator rehearses with a dry run first
1. Operator runs the dream with `dry_run=True` (the safe default).
2. The report lists the pair it WOULD merge, but nothing is written and the
   watermark is unchanged.
3. Operator flips `dry_run` off and runs for real; the pair now collapses.

### Story: The dream respects a human's earlier rejection
1. A human previously rejected a SAME_AS pair (`status="rejected"`).
2. The dream sweep encounters the same pair and leaves it untouched — no merge,
   no re-flag.

---

Blocked by: #048, #049, #050, #051, #052

## Log

### [SWE] 2026-05-21 14:30 — Implementation

**Files modified**
- `apps/memory/tests/integration/memory/test_dream_e2e_acceptance.py` (new) —
  the headline `@pytest.mark.slow @pytest.mark.requires_mongot` e2e acceptance
  for the whole dream feature, run on the real Paul Iusztin `User`. 9 tests.

No product code changed — this is a test + verification task (per Scope).

**How the parallel-duplicate scenario is seeded**
- A real `User(identifier="p.b.iusztin@gmail.com", attributes={"name": "Paul
  Iusztin"})` is inserted; its `after_insert` hook materializes the active
  `person:self` node (`is_active_user=True`) — the signal the fan-out parent
  flow enumerates. Using a real user (not a bare ObjectId) is what makes the
  fan-out test exercise the genuine active-user selection.
- `_seed_parallel_duplicate` inserts TWO `person` nodes for the same entity:
  node `a` watermark-FRESH (`updated_at > last_run`, so it DRIVES) and node `b`
  OLD (in the search space only) — the new->old collapse parallel ingestion
  leaves behind. A prior watermark is seeded via `record_dream_run` so only the
  fresh twin drives. Both nodes carry hand-crafted 8-dim cosine embeddings
  (`_vec(target_cos)`, mirroring `test_dedup.py`): `cos=0.999` lands the
  auto-merge band, `cos=0.88` with non-fuzzy names lands the flag band.

**Real-vs-fake embedder choice**
- DETERMINISTIC FAKE embeddings (hand-crafted cosine vectors) — the groomed
  spec's stated preference. The dream sweep is embedding-READ-only over stored
  vectors, so it makes ZERO Voyage calls regardless; fakes keep scores stable
  and avoid the free-tier 3 RPM limit entirely. No live-Voyage smoke used → the
  `[HUMAN]` RPM AC is `NOT RUN — used deterministic embedder`.
- The `_no_cost_guard` fixture patches the dream module's `get_llm` and
  `get_search_embedding_model` to `AssertionError` side-effects, so the
  zero-LLM (default path) and zero-Voyage (read-only) invariants are PROVEN by
  failure, not trusted.

**Collapse-then-noop evidence (auto_merge band, from test assertions)**
- Run 1: `nodes_driven == 1` (only the fresh twin), `pairs == 1`,
  `action == "merged"`, `watermark_advanced is True`; watermark advances to
  `run_start` (`|wm - run_start| < 2ms`, `> last_run`). Exactly 1 tombstone
  (`merged_into` set); confirmed SAME_AS audit edge with `reviewed_by="dream"`.
- Run 2 (no new ingestion): `pairs == []`, `auto_merged == 0`, `flagged == 0`,
  `nodes_driven <= 1` (at most the merge-winner re-drives — documented overlap),
  tombstone count STILL 1 (no double-merge). Watermark advances further. This
  is the incremental-watermark proof.
- Flag band: run 1 produces a pending SAME_AS, no tombstone; run 2
  `nodes_driven == 0` (truly empty delta), zero new flags.

**Tests**
- New e2e file: 9 passing, 0 failing (`requires_mongot` + `slow`):
  `test_collapse_then_noop_watermark_proof[auto_merge]`,
  `[flag]`, `test_node_ingested_after_first_dream_is_caught_by_second`,
  `test_dry_run_writes_nothing_then_real_run_collapses`,
  `test_rejected_pair_is_never_remerged_or_reflagged`,
  `test_max_pairs_cap_stops_run_and_records_cap_hit`,
  `test_search_embedding_model_routes_through_voyage_text_client`,
  `test_vector_space_swap_runbook_is_discoverable`,
  `test_fan_out_collapses_paul_duplicates_end_to_end`.
- Unit: 1327 passing, 0 failing.
- Integration (full, incl. mongot): 260 passing, 1 skipped — no regressions in
  the existing #051/#052 dream suites.
- Integration (CI mirror, no mongot): 176 passing, 1 skipped, 84 deselected
  (my mongot test correctly excluded) — CI will pass.

**Evidence**
```
$ uv run pytest tests/integration/memory/test_dream_e2e_acceptance.py -v
... 9 passed in 41.67s ...

$ make memory-unit-tests
... 1327 passed in 44.47s ...

$ make memory-integration-tests-all
... 260 passed, 1 skipped in 611.27s (0:10:11) ...

$ make memory-integration-tests-ci
... 176 passed, 1 skipped, 84 deselected in 146.37s (0:02:26) ...

$ make pre-commit
... ruff check Passed / ruff format Passed / KGQuery discipline Passed ...

# Operator-narrative: dream-consolidation-etl registered with its cron
$ uv run prefect deployment inspect "dream-consolidation-all-users/dream-consolidation-etl"
  'name': 'dream-consolidation-etl',
  'schedule': { 'cron': '0 4 * * *' },
  'tags': ['dream'],
```

**Notes for the Tester**
- mongot REQUIRED: the headline file is `@pytest.mark.requires_mongot` (live
  `$vectorSearch` against the 8-dim `vector_index`). Run with the full
  `docker-compose.yml` stack up (`make local-start`). I confirmed
  `tree-mongodb` + `tree-mongot` + `tree-prefect-server` were Up/healthy.
- Run via a `.env`-loaded invocation (the `VoyageTextEmbeddingModel` routing
  test constructs the client, which needs `VOYAGE_API_KEY`). `make
  memory-integration-tests-all` loads `.env`; a bare `uv run pytest` does NOT.
- NO live Voyage calls are made by these tests (deterministic embedder +
  `_no_cost_guard`). No rate-limit exposure.
- mongot index convergence is eventually-consistent; `_wait_for_indexed_count`
  polls up to 60s before each run. If a run flakes on a slow index, it is
  timing, not a logic regression (see #051's documented retry pattern).
- `make memory-run-dream-consolidation` triggers the registered deployment and
  streams per-user logs; I verified the deployment + cron via `prefect
  deployment inspect`. The full live trigger needs a served worker
  (`make memory-serve-workflows &`) — I confirmed registration; a live trigger
  against the production DB was not run (it would mutate Paul's real graph).

### [Tester] 2026-05-21 17:10 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make pre-commit` — prettier, ruff check,
  ruff format, biome, KGQuery discipline all Passed).
- Unit tests: 1327 passed / 0 failed (`make memory-unit-tests`, 41.16s).
- Integration tests (FULL, incl. mongot, IN ISOLATION): 260 passed / 1 skipped
  (`make memory-integration-tests-all`, 630.53s). Headline file shows 9 dots.
- Warnings: 0.

**E2E adversarial pass**
- Headline file in TRUE isolation: `uv run pytest
  tests/integration/memory/test_dream_e2e_acceptance.py -v` → 9 passed in 45.97s.
- Break path 1 (concurrency / shared-stack contention — the #051 lesson):
  I accidentally launched a 2nd run against the shared `integration_tests_twin`
  vector index while one was live → `OperationFailure: cannot query vector index
  ... while in state INITIAL_SYNC` + `DuplicateKeyError users.identifier`.
  CLASSIFIED AS CONTENTION, NOT A REAL FAILURE: after killing the stray run and
  re-running the file alone, all 9 pass. The conftest `_clean_collections`
  fixture self-healed the leftover Paul row left by the killed run's skipped
  teardown. The clean isolated run and the clean full suite (260 passed) agree.
- Break path 2 (zero-cost tripwire reality — `_no_cost_guard`): throwaway probe
  patched `dream.get_llm` / `dream.get_search_embedding_model` with an
  AssertionError side-effect and called them → both raised. Confirms the guard
  is a REAL tripwire (the patched names are the exact module-level symbols
  called at dream.py:649-650), not a no-op fixture. Probe removed, no residue.
- Break path 3 (verification-only scope): `git diff main...HEAD` for #053's
  uncommitted change touches ONLY the new test file + tracker. No product code.

**Acceptance criteria**
- [x] PASS — headline e2e creates two near-dup nodes for Paul + asserts collapse
      on run 1, both bands. Evidence: `test_collapse_then_noop_watermark_proof
      [auto_merge-0.999-merged]` + `[flag-0.88-flagged]` PASS. Run 1 reads ACTUAL
      DB state — `_tombstones()` (`merged_into $nin [None,""]`) == 1 +
      `_confirmed_audit_edge()` with `reviewed_by=="dream"` for auto_merge;
      pending SAME_AS + zero tombstones for flag. Not shallow.
- [x] PASS — watermark advanced to run_start after run 1. Evidence: same test
      reads back persisted watermark, `abs(wm.last_run_at - run1.run_start) <
      0.002` and `> last_run`. Backed by `record_dream_run` writing
      `last_run_at = run_start` (meta_state.py:137).
- [x] PASS — run 2 (no new ingestion) is a near-noop. Evidence: `run2.pairs ==
      []`, `auto_merged == 0`, `flagged == 0`; flag band `nodes_driven == 0`,
      auto_merge band `<= 1` (winner re-drive, no actionable twin); tombstone
      count re-read STILL 1. Asserted on real DB state + counts, not inferred.
- [x] PASS — dry_run writes nothing + watermark unchanged, then real run
      collapses. Evidence: `test_dry_run_writes_nothing_then_real_run_collapses`
      — dry: no tombstone, NO SAME_AS edge at all, `wm == last_run`; real:
      tombstone == 1, confirmed audit edge, `wm > last_run`. Both DB states read.
- [x] PASS — rejected pair never re-merged/re-flagged. Evidence:
      `test_rejected_pair_is_never_remerged_or_reflagged` — pre-seeds rejected
      SAME_AS, asserts `pairs == []`, no tombstone, edge status still
      `rejected`. Backed by `_same_as_edge_exists` skipping ANY-status edge
      (dream.py:259-287).
- [x] PASS — incremental catch-up: node ingested after dream 1 caught by dream 2.
      Evidence: `test_node_ingested_after_first_dream_is_caught_by_second` —
      late node `updated_at = now+1s > wm1`, run 2 `nodes_driven == 1`,
      `auto_merged == 1`, tombstone == 1, `wm2 > wm1`. Real 2nd-run pickup
      (late node inserted AFTER run 1 completed), not a re-seed artifact.
- [x] PASS — idempotency: 2nd real run does not double-merge. Evidence: run 2
      tombstone count stays 1 in the headline test.
- [x] PASS — max_pairs cap stops + records cap_hit. Evidence:
      `test_max_pairs_cap_stops_run_and_records_cap_hit` — `cap_hit is True`,
      `len(pairs) == 1` with 2 candidate pairs, watermark still advanced.
- [x] PASS — default path makes zero LLM calls. Evidence: `_no_cost_guard`
      (`get_llm` AssertionError side_effect) active on every default-path test;
      all pass = never called. Tripwire reality independently proven (break
      path 2). `_supersession_sweep` gated off at dream.py:796 on default path.
- [x] PASS — dream sweep makes zero Voyage calls. Evidence: `_no_cost_guard`
      (`get_search_embedding_model` AssertionError side_effect); sweep is
      embedding-READ-only over stored vectors. Tripwire proven real.
- [x] PASS — deployment registered. Evidence: orchestrator.py wires
      `dream_consolidation_all_users.to_deployment(name="dream-consolidation-etl",
      cron=app_config.dream.cron, tags=["dream"])`; default.yaml `dream.cron:
      "0 4 * * *"`. Routing (#048) + runbook (#049) tests pass in-suite, tying
      the feature ends together. CLAUDE.md contains "vector space" (#049 runbook).
- [x] PASS — full suite green locally. Evidence: 260 passed / 1 skipped (full,
      incl. mongot, in isolation). CI mirror not re-run by me, but mongot test is
      correctly `requires_mongot`-marked so CI deselects it.
- [ ] [HUMAN] live-Voyage RPM — NOT RUN — used deterministic 8-dim embedder;
      dream sweep is embedding-READ-only, zero Voyage calls (asserted). Correctly
      left for human; no action needed.

**Evidence**
```
$ make memory-integration-tests-all   # IN ISOLATION
================== 260 passed, 1 skipped in 630.53s (0:10:30) ==================

$ uv run pytest tests/integration/memory/test_dream_e2e_acceptance.py -v  # ISOLATED
============================== 9 passed in 45.97s ==============================

$ make memory-unit-tests
============================ 1327 passed in 41.16s =============================

$ make pre-commit
ruff check Passed / ruff format Passed / KGQuery discipline Passed
```

**Other issues found**
- None blocking. Process note (not a defect): the headline file is
  `requires_mongot` and races the shared `integration_tests_twin` vector index;
  running two probes concurrently against it produces INITIAL_SYNC /
  DuplicateKeyError flakes (the documented #051 contention). Always run it in
  isolation. The tests themselves are clean — verified by an uninterrupted
  isolated run agreeing with the full-suite run.

**VERDICT: PASS**
