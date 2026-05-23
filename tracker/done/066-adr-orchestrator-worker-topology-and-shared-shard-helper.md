# ADR-002 §3 amendment (orchestrator/worker topology) + shared shard-partitioning helper

Status: pending
Tags: `docs`, `infra`, `refactor`
Depends on: None
Blocks: #067, #068, #069

## Scope

This is the scaffolding task for the orchestrator/worker deployment split. It does
TWO things, both prerequisites for the memory (#067) and data (#068) splits, and it
changes NO deployment topology (memory extraction keeps working exactly as it does
on the current branch tip after this task).

### 1. Amend ADR-002 §3

Edit `docs/adrs/002_pipeline_concurrency_and_voyage_rate_limiting.md`. Add a new
amendment block to §3 (after the existing `Amendment (#061 …)` block) that records
the orchestrator/worker two-deployment topology for BOTH pipelines. The amendment
must state, in prose consistent with the existing ADR voice:

- **What changed:** the fan-out is now realized as TWO SEPARATE NAMED deployments per
  pipeline — an `…-orchestrator` (operator entrypoint) and an `…-worker` (internal
  dispatch target) — REPLACING #061's single-deployment + recursive-self-dispatch
  design. #061's in-flow `num_shards` recursive self-dispatch is hereby SUPERSEDED by
  this two-deployment topology.
- **Why:** the owner wants the orchestrator-vs-worker boundary to be explicit and
  visible in the Prefect UI (parent shows as `…-orchestrator`, children as
  `…-worker`). This is NOT the #061 "two operator entrypoints" problem — operators
  still run exactly ONE entrypoint per pipeline (the orchestrator); the worker is the
  orchestrator's internal `run_deployment` target, not a second operator command.
- **Memory topology (target):**
  - Worker flow `memory-extract-etl-worker` `(user_id, document_ids=None)` — the
    actual six-task extraction body. NO `num_shards`, NO orchestrator branch, NO
    indexing trigger. Registered as deployment `memory-extract-etl-worker`.
  - Orchestrator flow `memory-extract-etl-orchestrator`
    `(user_id, document_ids=None, num_shards=1)` — resolve pending docs (when
    `document_ids is None`), partition into `min(num_shards, N)` balanced shards,
    dispatch ONE `memory-extract-etl-worker` run per shard via `run_deployment` under
    `asyncio.gather(return_exceptions=True)`, then ONE trailing `memory-indexing-etl`
    run. Dispatches to the WORKER deployment (NO recursion). Registered as deployment
    `memory-extract-etl-orchestrator`.
  - `memory-indexing-etl` is UNCHANGED (name + behavior); the orchestrator still
    triggers it exactly once after the gather settles.
- **Data topology (target):**
  - Worker flow `data-etl-worker` `(user_id, sources: list[...])` — ingest a SUBSET
    (shard) of the configured sources, reusing the existing per-source-type batch
    logic. Registered as deployment `data-etl-worker`.
  - Orchestrator flow `data-etl-orchestrator` `(user_id, num_shards=1)` — read the
    configured `sources:` list, partition into N balanced shards, dispatch one
    `data-etl-worker` per shard via `run_deployment` under
    `asyncio.gather(return_exceptions=True)`. NO trailing step (the data pipeline
    only produces `documents`; no index). Registered as deployment
    `data-etl-orchestrator`.
- **`num_shards=1` semantics CHANGE (record explicitly):** on the memory orchestrator,
  `num_shards=1` now dispatches 1 worker run + 1 index run — it is NO LONGER a
  byte-identical in-process "plain" extraction run. A bare extraction (no index) is
  available by triggering `memory-extract-etl-worker` directly. This is an accepted
  consequence of making the worker a real deployment.
- **What is UNCHANGED (so Status stays `Accepted`, not `Superseded`):** the
  document-shard axis (memory), balanced contiguous partitioning, the
  `asyncio.gather(return_exceptions=True)` failure-isolation, the single-trailing-index
  rule (memory only), the cross-flow `voyage-embeddings` GCL (§1), and the
  `serve(limit=runner_global_limit)` admission control (§4). The data fan-out axis
  shifts from in-process per-type to a source-shard fan-out across worker deployments,
  but the partitioning math and failure-isolation are the same primitive.
- **Stale deployments note:** after #067/#068 rename the deployments, the server-side
  definitions for `memory-extraction-etl` and `data-pipeline-etl` (and the long-gone
  `memory-extraction-fanout-etl`) become orphaned and must be deleted with
  `prefect deployment delete <name>` on each environment. Record this as an ops note
  in the amendment.

Keep `Status: Accepted` and update the `Date:` line is NOT required (the ADR records
multiple dated amendments inline; add the amendment with its own `Amendment (#066 …)`
heading). Do NOT touch §1, §2, §4, §5, §6, or the Consequences section except to add
the amendment prose under §3.

### 2. Generalize the shared shard-partitioning helper

The pure helpers `_partition_into_shards(items, num_shards)` and
`_resolve_num_shards(num_shards)` currently live in
`apps/memory/src/tree/memory/extraction/sharding.py` and are typed for
`list[str]` document ids. Both #067 (memory, shards `list[str]` doc ids) and #068
(data, shards `list[SourceEntry]`) need the SAME balanced-contiguous partitioning
math. To avoid #068 duplicating it:

- Make `_partition_into_shards` generic over the element type (it already only relies
  on `len()` and slicing — a `list[T] -> list[list[T]]` signature, or keep `list[str]`
  if the SWE prefers and have the data orchestrator partition a `list[str]` of source
  indices/uris; the SWE picks the cleanest typing). `_resolve_num_shards` is already
  type-agnostic (`int -> int`).
- The helper's home module may stay `extraction/sharding.py` for this task (memory
  still owns it), OR the SWE may extract the two pure functions into a small
  shared module the data orchestrator can import without pulling in extraction code
  — e.g. `tree/data/sharding.py` mirroring or a neutral location. The SWE decides;
  the ONLY hard requirement is that #068 can reuse the identical partitioning math
  without copy-pasting it. If a module moves, update the existing imports in
  `extraction/pipeline.py` and the existing unit test import in
  `tests/unit/memory/extraction/test_fanout.py`, and update the
  `check-kgquery-discipline` allowlist if the moved file is on it.
- This task does NOT change `_fan_out_extraction`, `_resolve_pending_document_ids`,
  `FanOutStats`, the `memory_extraction` flow, or any deployment registration. Those
  are #067's job. Memory extraction behavior is byte-identical after this task.

## Acceptance Criteria

- [x] `docs/adrs/002_pipeline_concurrency_and_voyage_rate_limiting.md` §3 contains a
      new `Amendment (#066 …)` block describing the orchestrator/worker two-deployment
      topology for BOTH pipelines, naming all four target deployments
      (`memory-extract-etl-worker`, `memory-extract-etl-orchestrator`,
      `data-etl-worker`, `data-etl-orchestrator`).
- [x] The amendment explicitly states #061's recursive-self-dispatch single-deployment
      design is SUPERSEDED by this two-deployment topology, and that the ADR Status
      stays `Accepted` (topology refinement of the same fan-out decision).
- [x] The amendment records the `num_shards=1` semantics change (memory orchestrator
      now dispatches 1 worker + 1 index, not a byte-identical in-process run).
- [x] The amendment records that operators still run exactly ONE entrypoint per
      pipeline (the orchestrator), and that the worker is the orchestrator's internal
      dispatch target — not the rejected #061 "two operator entrypoints" shape.
- [x] The amendment includes the stale-deployment cleanup ops note
      (`prefect deployment delete memory-extraction-etl`,
      `prefect deployment delete data-pipeline-etl`,
      `prefect deployment delete memory-extraction-fanout-etl`).
- [x] `_partition_into_shards` and `_resolve_num_shards` are reusable by the data
      orchestrator (#068) WITHOUT duplicating the partitioning math — verifiable by an
      import path the data module can use.
- [x] Memory extraction behavior is unchanged after this task: the existing memory
      fan-out unit + integration tests still pass without modification to their
      assertions (only import paths may change if a module moved).
- [x] `make memory-format-check && make memory-lint-check && make pre-commit` clean.
- [x] `make memory-unit-tests` passes (the existing
      `tests/unit/memory/extraction/test_fanout.py` partitioning tests are green).
- [x] `make memory-integration-tests` (fast inner loop) passes — no regression in the
      memory fan-out integration suite.

## User Stories

### Story: A future engineer reads ADR-002 to understand the deployment topology
1. Engineer opens `docs/adrs/002_pipeline_concurrency_and_voyage_rate_limiting.md`.
2. Engineer scrolls to §3 and reads the `Amendment (#066 …)` block.
3. The block tells them: each pipeline is now an orchestrator deployment + a worker
   deployment; operators run only the orchestrator; the worker is the internal
   dispatch target; memory triggers indexing once at the end; data has no trailing
   step.
4. Engineer understands why `num_shards=1` is no longer a plain in-process run, and
   sees the four deployment names and the stale-deployment cleanup commands.

### Story: The data orchestrator (built in #068) reuses the partitioning math
1. The #068 SWE needs to split the configured sources into N balanced shards.
2. They import the shared `_partition_into_shards` (and `_resolve_num_shards`) helper
   from its module.
3. Passing a list of N source items + `num_shards=2` returns two balanced, contiguous,
   disjoint shards whose in-order union reconstructs the input — identical math to the
   memory document-shard partitioning, with zero copy-paste.

### Story: The current memory pipeline keeps working through the scaffolding change
1. An operator on the post-#066 branch runs
   `make memory-run-memory-pipeline-extraction USER_ID=<oid> NUM_SHARDS=4`.
2. The run behaves exactly as it did before #066 (this task changed only docs + the
   helper's typing/home, not the flow or deployment).
3. The memory fan-out unit + integration tests pass unchanged.

---

Blocked by: (none)

## Log

### [PM] 2026-05-23 — Grooming

**Summary**
First task of the owner-approved orchestrator/worker re-architecture. Lands the
ADR-002 §3 amendment (recording the two-deployment topology for both pipelines,
superseding #061's recursive self-dispatch, recording the num_shards=1 semantics
change) and generalizes the pure shard-partitioning helper so #068 can reuse it
without duplication. Pure docs + helper typing — zero deployment-topology change, so
memory extraction stays byte-identical and #067/#068 build on a settled ADR.

**Key decisions**
- ADRs live in `docs/adrs/` (NOT `docs/adr/`); ADR-002 is the existing concurrency
  ADR. Amend §3 in place (the ADR already records dated inline amendments) — Status
  stays `Accepted` because the fan-out axis/partitioning/failure-isolation/trailing-
  index decisions are unchanged; only topology refines. This is the
  re-engagement/escalation cure per PROCESS.md — the cure for the topology change is
  grooming + an ADR amendment, authored here.
- The owner asked to SPEC the ADR amendment as the first task's deliverable (not
  pre-write it in the grooming commit), so #066 is an implementation task whose
  primary artifact is the ADR text.
- Helper generalization is grouped here (not in #068) so the partitioning math has a
  single home before two orchestrators consume it. The SWE picks the exact typing +
  module home; the hard requirement is reuse-without-duplication.
- No glossary in this project; "orchestrator"/"worker" are already ADR-002 vocabulary,
  so no new terms.

**Dependencies**
- None. This is the root scaffolding task.

**User stories**
- 3 stories covering: reading the amended ADR, reusing the helper from the data
  orchestrator, and confirming memory extraction is unchanged through the change.

Ready for implementation.

### [SWE] 2026-05-23 14:30 — Implementation

**Files modified**
- `docs/adrs/002_pipeline_concurrency_and_voyage_rate_limiting.md` — added the
  `Amendment (#066 …)` block to §3 (after the #061 amendment, before §4) recording
  the orchestrator/worker two-deployment topology for BOTH pipelines, superseding
  #061's recursive self-dispatch. Status stays `Accepted`.
- `apps/memory/src/tree/sharding.py` — NEW neutral, pipeline-agnostic module holding
  the two pure helpers `_partition_into_shards` (now GENERIC over the element type,
  `list[T] -> list[list[T]]`) and `_resolve_num_shards` (unchanged `int -> int`).
- `apps/memory/src/tree/memory/extraction/sharding.py` — removed the two pure-helper
  definitions; now imports + re-exports them from `tree.sharding` (via `__all__`).
  The memory-specific helpers (`_resolve_pending_document_ids`, `_fan_out_extraction`,
  `FanOutStats`) stay here. Module docstring updated to point at the new home.
- `apps/memory/tests/unit/test_sharding.py` — NEW unit test pinning the #068 reuse
  contract directly against `tree.sharding`: generic-element partitioning (non-`str`
  source items), object-identity preservation, and the clamp.

**Helper location/shape (the architectural choice this task asked the SWE to make)**
- Canonical home: `tree.sharding` (top-level `tree`, NOT under `memory/` or `data/`)
  so BOTH orchestrators import the IDENTICAL math with zero copy-paste and no
  cross-module memory↔data dependency.
- `_partition_into_shards` is generic over `T` (relies only on `len()` + slicing); the
  data orchestrator (#068) will shard a `list[SourceEntry]` with the same call.
- Memory keeps its existing import path: `tree.memory.extraction.sharding` re-exports
  both functions, so `extraction/pipeline.py`, the unit `test_fanout.py`, and the
  integration `test_extraction_fanout.py` imports are UNCHANGED. Verified the
  re-exported objects are `is`-identical to the canonical ones.
- `tree/sharding.py` has no `knowledge_graph` access → NOT added to the
  `check-kgquery-discipline` allowlist (the existing `extraction/sharding.py` entry
  stays — it still owns `_resolve_pending_document_ids`). `make pre-commit` (KGQuery
  discipline) passes.

**Confirmation memory still works (zero topology change)**
- No deployment, flow, or `num_shards` behavior changed. `memory-extraction-etl` /
  `memory_extraction(num_shards)` are byte-identical after this task.
- Import smoke: `tree.memory.extraction.pipeline` (flow present) and `tree.orchestrator`
  (deployment registration) both import cleanly; re-exported helpers are the same
  objects; `_partition_into_shards(range(6), 4)` → sizes `[2,2,1,1]`, `[]→[]`,
  `_resolve_num_shards(0/-3/4)` → `1/1/4`.

**Tests**
- Unit: 1431 passing, 0 failing (`make memory-unit-tests`), incl. NEW `test_sharding.py`
  (15) and existing `test_fanout.py` (via re-export, assertions unmodified).
- Integration (fast, isolated quiesced stack): 153 passed, 1 skipped, 0 failing
  (`make memory-integration-tests`).
- Integration (slow memory fan-out, run directly): `test_extraction_fanout.py` →
  9 passed. (A benign Prefect temp-server teardown logging artifact —
  `ValueError: I/O operation on closed file` in a logging callback AFTER the green
  run — is unrelated to the result; exit code 0, "9 passed".)

**Acceptance criteria** — all 10 checked above; each verified:
- ADR §3 `Amendment (#066 …)` present, names all four deployments, states SUPERSEDED +
  Status `Accepted`, records num_shards=1 change, the one-operator-entrypoint point,
  and the three `prefect deployment delete` ops-note commands — verified by `grep`.
- Helper reuse-without-duplication — verified: only ONE definition of each pure
  function exists (`tree/sharding.py`); importable via `from tree.sharding import …`.
- Memory unchanged — verified by unmodified unit + integration fan-out tests passing
  and the import smoke.
- format-check / lint-check / pre-commit / unit-tests / integration-tests — all clean.

**Evidence**
```
$ make memory-format-check && make memory-lint-check
267 files already formatted
All checks passed!

$ make pre-commit
KGQuery discipline (memory)..............................................Passed
(all hooks Passed)

$ make memory-unit-tests
============================ 1431 passed in 43.01s =============================

$ make memory-integration-tests   # fast, isolated quiesced stack
========== 153 passed, 1 skipped, 115 deselected in 214.81s (0:03:34) ==========

$ uv --directory apps/memory run pytest tests/integration/memory/test_extraction_fanout.py
============================== 9 passed in 9.25s ===============================

$ grep -rn "def _partition_into_shards\|def _resolve_num_shards" apps/memory/src apps/memory/tests
apps/memory/src/tree/sharding.py:26:def _resolve_num_shards(num_shards: int) -> int:
apps/memory/src/tree/sharding.py:41:def _partition_into_shards(items: list[T], num_shards: int) -> list[list[T]]:
```

**Notes**
- Did NOT run `make memory-integration-tests-all` (slow full suite) — the fast loop
  was the task's stated bar; the relevant slow fan-out file was run directly and is
  green. Tester runs `-all` at the acceptance gate per PROCESS.md.
- NOT COMMITTED — handing off to Tester first.

### [Tester] 2026-05-23 16:10 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`267 files already formatted`; `All checks passed!`;
  all pre-commit hooks Passed incl. `KGQuery discipline (memory)`)
- Unit tests: 1431 passed / 0 failed (incl. NEW `test_sharding.py` → 15)
- Integration tests (`make memory-integration-tests-all`, ACCEPTANCE GATE — slow +
  requires_mongot, quiesced + isolated stack): 268 passed / 1 skipped / 0 failed,
  exit 0, 633s. `test_extraction_fanout.py` → 9 passed (helper relocation did NOT
  break the memory fan-out suite). `test_web_serp` → 3 passed (no flake this run).
- Warnings: 0

**Diff scope (confined as specified)**
- `git diff --name-only` = only `apps/memory/src/tree/memory/extraction/sharding.py`
  + `docs/adrs/002_pipeline_concurrency_and_voyage_rate_limiting.md`; untracked:
  `apps/memory/src/tree/sharding.py`, `apps/memory/tests/unit/test_sharding.py`
  (+ tracker files). NO change to `pipeline.py`, `orchestrator.py`, deployments, or
  num_shards branch — `git diff --name-only | grep -E "pipeline.py|orchestrator.py"`
  → NONE.

**No topology change (core guarantee) — PASS**
- `import tree.orchestrator` imports cleanly (deployment registration intact).
- `memory_extraction` flow / `num_shards` branch / `memory-extraction-etl`
  registration unchanged (not in diff). `pipeline.py:1490` still resolves then
  `pipeline.py:1521` partitions — call site untouched.

**Helper relocation correct — PASS**
- Single-definition grep (`def _partition_into_shards|def _resolve_num_shards` over
  src+tests):
  ```
  apps/memory/src/tree/sharding.py:26:def _resolve_num_shards(num_shards: int) -> int:
  apps/memory/src/tree/sharding.py:41:def _partition_into_shards(items: list[T], num_shards: int) -> list[list[T]]:
  ```
  Each function defined EXACTLY ONCE (in `tree.sharding`). `extraction/sharding.py`
  imports + re-exports via `__all__`; `pipeline.py`, unit `test_fanout.py`, and
  integration `test_extraction_fanout.py` all still import through
  `tree.memory.extraction.sharding` — no stale paths.
- Object identity: `reexport._partition_into_shards is canon._partition_into_shards`
  and `reexport._resolve_num_shards is canon._resolve_num_shards` → both `True`
  (re-export, not duplicate).
- Partition probes: `(6,4)→[2,2,1,1]`, `(7,3)→[3,2,2]`, `[]→[]`, clamp
  `0→1 / -3→1 / 4→4`, generic non-`str` elements with object-identity preserved — all PASS.

**E2E adversarial pass (pure-helper surface)**
- Happy path: `_partition_into_shards(range(6),4)` → `[2,2,1,1]` (PASS).
- Break path 1 (boundary: empty list): `p([],0)`/`p([],4)` → `[]` (PASS, documented no-op).
- Break path 2 (malformed: num_shards=0 on non-empty, BYPASSING resolve):
  `p(['a'],0)` → `ZeroDivisionError`. This is the DOCUMENTED contract (resolve-first
  guard), NOT a regression — identical to pre-relocation math. The only real consumer
  (`_fan_out_extraction`) calls `_resolve_num_shards` first (`pipeline.py:1490`), so
  the error is unreachable in production; `test_fanout.py` clamp-then-partition test
  covers the composition. (PASS — behavior preserved.)
- Break path 3 (generic element identity): sharding arbitrary objects returns the
  SAME objects regrouped (`is`-identity) — the #068 reuse contract (PASS).

**Acceptance criteria** — all 10 verified PASS
- [x] ADR §3 `Amendment (#066 …)` block present, names all four deployments
      (`memory-extract-etl-worker` ×4, `memory-extract-etl-orchestrator` ×2,
      `data-etl-worker` ×3, `data-etl-orchestrator` ×2 — grep counts).
- [x] States #061 recursive-self-dispatch SUPERSEDED + Status stays `Accepted`
      (`- **Status:** Accepted` line unchanged).
- [x] Records num_shards=1 semantics change (1 worker + 1 index, not in-process).
- [x] Records one-operator-entrypoint / worker = internal dispatch target.
- [x] Stale-deployment ops note: `prefect deployment delete memory-extraction-etl /
      data-pipeline-etl / memory-extraction-fanout-etl` (lines 199-201).
- [x] Helper reusable by #068 without duplication — single definition in
      `tree.sharding`, importable `from tree.sharding import …`.
- [x] Memory extraction unchanged — fan-out unit + integration tests pass unmodified.
- [x] format-check / lint-check / pre-commit clean.
- [x] unit-tests pass (1431).
- [x] integration-tests pass — full `-all` gate green (268 passed, 0 failed).

**Other issues found**
- None. The direct `ZeroDivisionError` on `num_shards=0` is the documented
  resolve-first contract and matches pre-relocation behavior; not actionable.

**VERDICT: PASS**
