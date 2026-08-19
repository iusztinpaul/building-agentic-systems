---
id: 101-docs-sweep-free-tier-topology
feature: free-tier-deployments
status: done
---

# Docs sweep: README, Makefile help, topology notes, tutorial (text only)

Tags: `docs`
Depends on: #100
Blocks: #102
Implements: ADR-002 amendment (`free-tier-deployments`)

## Scope

Bring every operator-facing document in line with the new topology. Use the glossary's canonical
terms throughout (Coordinator, Worker, Deployment, Listen sources — the Coordinator and
Listen-sources rows were updated in this feature's grooming commit; docs must match them). ADR-002
and `docs/glossary.md` themselves were amended in the grooming commit — do NOT edit them here.

**`apps/memory/README.md`** (~lines 115–121, 162, 184, plus the memory-extraction intro):

- The core-5 bullet list: replace the coordinator entries — new list is the 5 from #100; state that
  the two coordinators remain FLOWS run as inline subflows of `offline-pipeline` (and manual
  single-step runs go through `make memory-run-data-pipeline` / `make memory-run-memory-pipeline`,
  which dispatch `offline-pipeline` with the other phase off).
- "Triggers `data-etl-coordinator`: …" (offline data section, ~162): rewrite — the target dispatches
  ONE `offline-pipeline` run with `run_extraction=False`; inside it the data coordinator (inline
  subflow) still groups by platform and fans out `data-etl-worker` runs. Fan-out description itself
  is unchanged.
- The nightly-cron paragraph (~184): the cron now fires `offline-pipeline` (same `0 3 * * *`, same
  `source_files=["sources/listen.yaml"]`, no `user_id`), and — widened semantics — ingests AND
  extracts AND indexes across all active users.
- Memory-extraction section intro ("both via `memory-extract-etl-coordinator`"): both modes go
  through `offline-pipeline` with `run_data=False`; the extraction coordinator runs inline and still
  shards + fires the trailing index run.

**`apps/memory/Makefile`** (~lines 171, 174 — help text only, no recipe changes):
`run-data-pipeline` help names `data-etl-coordinator`; `run-memory-pipeline` help describes the
coordinator deployment — reword both to the offline-pipeline-phase framing (keep them one line,
matching the `make help` format).

**`docs/notes/prefect-execution-topologies.md`** (~lines 74, 162, 179, 245, 282 and any other hits):

- The `deployment_id`-routing example ("loads that deployment's entrypoint (`data_etl_coordinator`
  vs `data_etl_worker`)") — retarget to a pair that are both still deployments (e.g.
  `offline_pipeline` vs `data_etl_worker`).
- The Managed-topology narrative ("The coordinator→worker fan-out is unchanged:
  `data_etl_coordinator` … `run_deployment`s one `data-etl-worker` per shard") — still true
  mechanically, but note the coordinator now executes as an inline subflow INSIDE an
  `offline-pipeline` run, not as its own deployment run.
- The free-tier cons paragraph ("**5 deployments** (hence `deploy_optional: false` → exactly the 5
  core; the dream deployment is gated off)") — update the parenthetical: the core 5 are now the two
  e2e pipelines + the two workers + indexing; dream remains the only gated deployment.
- The deployment enumeration (~245): replace with the new core 5 (+ optional dream).
- The 6-shard walkthrough (~282: "trigger data-etl-coordinator → 1 coordinator container") — step 1
  becomes triggering `offline-pipeline` (or the data-phase-only dispatch); the container holding the
  admission slot is the `offline-pipeline` run hosting the coordinator subflow. Slot math is
  unchanged (the e2e run costs the same single slot a lone coordinator did).

**`tutorials/2_4_deploying_the_database_and_pipelines.md`** (~lines 193, 236 — TEXT ONLY):

- Update the transcript/prose around "Deployed 2 pipeline(s) to tree-managed: data-etl-coordinator, …"
  and the Figure 2.17 caption text to the new names.
- Add an explicit note that the screenshots (Figure 2.17, Figure 2.24) and the quoted transcript were
  captured on the pre-`free-tier-deployments` topology (coordinator deployments) and now show a
  superseded layout; the current deployment names are the ones in the updated text. Do NOT re-capture
  images.

## Acceptance criteria

- [x] `grep -rn "data-etl-coordinator\|memory-extract-etl-coordinator" apps/memory/README.md apps/memory/Makefile docs/notes/prefect-execution-topologies.md tutorials/2_4_deploying_the_database_and_pipelines.md`
      — every remaining hit (if any) is either inside the explicit superseded-screenshot note or
      names the flow/subflow (not a Deployment); NO doc claims either coordinator is a registered
      deployment.
- [x] README's core list matches `_DEPLOYMENT_SPECS` exactly (5 core + optional dream) and its
      nightly-cron paragraph states the offline-pipeline ingest+extract+index semantics.
- [x] `make help` (repo root) shows the updated one-line help for `memory-run-data-pipeline` /
      `memory-run-memory-pipeline`; recipes are byte-identical (only `#` help text changed — verify
      via `git diff`).
- [x] The tutorial contains the superseded-screenshot note and no stale deployment names outside it;
      no image files changed in the diff.
- [x] Terminology matches `docs/glossary.md` (post-grooming rows): coordinators are Coordinators
      (flows/inline subflows), never "coordinator deployments".
- [ ] `make pre-commit` clean; `make memory-tests` still green (docs-only diff for code dirs —
      Makefile help text is comment-only).

## User stories

### Story: New contributor learns the topology from the README
1. Contributor opens `apps/memory/README.md` and reads the deployments section.
2. The list names exactly the 5 deployments they will see after `make memory-serve-workflows`, and
   explains where the coordinators went (inline subflows of `offline-pipeline`).
3. Running `uv run prefect deployment ls` matches the README one-to-one.

### Story: Tutorial reader isn't confused by old screenshots
1. Reader follows `tutorials/2_4_…` and reaches the "Deployed N pipeline(s)" output and Figure 2.17.
2. The text tells them the screenshot/transcript predate the `free-tier-deployments` topology change
   and lists the names they WILL see.
3. Their actual Prefect Cloud dashboard (5 deployments, no coordinators) matches the written
   expectation, so the mismatch with the image is expected and explained.

### Story: Operator discovers the group overlap before a teardown
1. Operator reads the `--groups` docs before `make memory-deploy-prefect-setup-down GROUPS=data`.
2. The docs state `online-pipeline`/`offline-pipeline` belong to BOTH groups, so a `down --groups
   data` deletes them too, and the next `up` restores them.
3. The operator proceeds (or scopes differently) with no surprise.

## Out of scope

- `docs/adrs/002_…` and `docs/glossary.md` — amended in the grooming commit.
- Re-capturing any screenshot.
- Any code or Makefile-recipe change.

## Log

### [SWE] 2026-08-19 — Docs sweep

**Files modified**
- `apps/memory/README.md` — core-5 list rewritten (workers + indexing + the two e2e pipelines),
  Coordinators-are-inline-subflows paragraph, the data∩memory group-overlap note, offline-data
  paragraph (`offline-pipeline` with `run_extraction=False`), nightly-cron paragraph
  (ingest + extract + index across all active users), memory-extraction intro (`run_data=False`).
- `apps/memory/Makefile` — help text ONLY for `run-data-pipeline` / `run-memory-pipeline`
  (offline-pipeline-phase framing); recipes byte-identical.
- `docs/notes/prefect-execution-topologies.md` — `deployment_id`-routing example retargeted to
  `offline_pipeline` vs `data_etl_worker`; Managed narrative notes the Coordinator now runs inline
  inside the `offline-pipeline` run; MCP async-ingest example points at `online-pipeline`;
  free-tier cons parenthetical; deployment enumeration = new core 5 (+ optional dream); 6-shard
  walkthrough step 1 + slot-math note; local-serve fan-out sentence.
- `tutorials/2_4_deploying_the_database_and_pipelines.md` — TEXT only: `Deployed 3 pipeline(s) …
  data-etl-worker, online-pipeline, offline-pipeline`, a `GROUPS=data` overlap paragraph, the
  superseded-screenshot note (Fig 2.17 + 2.24), and the Figure 2.17 caption.

**Acceptance criteria**
- [x] Grep over the four files — the ONLY remaining `*-etl-coordinator` hit is inside the
      tutorial's explicit superseded-screenshot note.
- [x] README list == `_active_deployment_specs()` (verified by running it: `data-etl-worker`,
      `memory-extract-etl-worker`, `memory-indexing-etl`, `online-pipeline`, `offline-pipeline`);
      nightly-cron paragraph states ingest+extract+index.
- [x] `make memory-help` shows the new one-liners; `git diff apps/memory/Makefile` touches only the
      two `#` comments.
- [x] Tutorial carries the superseded note; no image files in the diff.
- [x] Terminology follows `docs/glossary.md`: Coordinator / Worker / Deployment; coordinators are
      never called "coordinator deployments".
- [ ] `make pre-commit` clean (PASSED: prettier / ruff check / ruff format / biome all Passed);
      `make memory-tests` NOT RUN — the orchestrating run explicitly deferred the suite.

**Evidence**
```
$ uv run python -c "from tree.orchestrator import _active_deployment_specs; ..."
['data-etl-worker', 'online-pipeline', 'offline-pipeline']                      # groups=("data",)
['data-etl-worker', 'memory-extract-etl-worker', 'memory-indexing-etl', 'online-pipeline', 'offline-pipeline']
$ grep -rn "data-etl-coordinator\|memory-extract-etl-coordinator" apps/memory/README.md apps/memory/Makefile docs/notes/prefect-execution-topologies.md tutorials/2_4_deploying_the_database_and_pipelines.md
tutorials/2_4_deploying_the_database_and_pipelines.md:202:> **Note — the screenshots predate the current topology.** …
$ make pre-commit
prettier ... Passed | ruff check ... Passed | ruff format ... Passed | biome check (harness) ... Passed
```

**Notes**
- Docs-only diff; no Python touched, so no format/lint-fix pass was needed (pre-commit's ruff hooks
  still ran clean over the tree).
- The tutorial transcript is stylised (the real `up` echoes deployment UUIDs, not names) — kept the
  existing style, only the names/count changed (2 → 3 for `GROUPS=data`).
- Out of scope, flagged for follow-up: `scripts/run_data_pipeline.py` / `run_memory_pipeline.py`
  docstrings still call `offline-pipeline` / `online-pipeline` "the optional … deployment" in their
  in-process-fallback sentence — stale since #100 promoted both to core (code change, not prose).
- Not committed; changes sit in the working tree on `feat/free-tier-deployments`.
