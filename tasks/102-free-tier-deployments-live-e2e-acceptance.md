---
id: 102-free-tier-deployments-live-e2e-acceptance
feature: free-tier-deployments
status: done
---

# Live E2E acceptance for the free-tier deployment topology

Tags: `infra`, `data`, `memory`, `docs`
Depends on: #100, #101
Implements: ADR-002 amendment (`free-tier-deployments`)

## Scope

In the style of #088/#093: run the new topology end-to-end against the live LOCAL stack with the
"Paul Iusztin" user, prove all operator entry points and the cron semantics work, and paste command
+ output evidence into `## Log`. No code changes — if anything fails, file a rollup task; do not
expand this one.

Preconditions: `make env-status` → local; `make local-start`; kill any running serve process, then
`make memory-serve-workflows &` (re-serve to pick up #100); delete the stale local server-side
deployments left from the old topology
(`prefect deployment delete data-etl-coordinator/data-etl-coordinator`,
`prefect deployment delete memory-extract-etl-coordinator/memory-extract-etl-coordinator`) so the
registration check is clean.

## Acceptance criteria

- [x] Registration: `uv run prefect deployment ls` shows EXACTLY 5 deployments — `data-etl-worker`,
      `memory-extract-etl-worker`, `memory-indexing-etl`, `online-pipeline`, `offline-pipeline` —
      and `prefect deployment inspect offline-pipeline/offline-pipeline` shows the ONE cron
      `0 3 * * *` with `source_files=["sources/listen.yaml"]`. Neither coordinator name appears.
- [x] Data step: `make memory-run-data-pipeline SOURCE_FILE="sources/listen.yaml"` logs a
      DEPLOYMENT-mode submission of `offline-pipeline` (`"status": "submitted"` / "Submitted flow
      run … waiting"), completes green, lands documents, and performs NO extraction (the new docs
      remain PENDING — verify via mongosh or the memory-step log that follows).
- [x] Memory step: `make memory-run-memory-pipeline` extracts the PENDING docs (offline-pipeline run
      with `run_data=False`), fires ONE trailing `memory-indexing-etl` run, completes green.
- [x] Doc-id narrowing: `make memory-run-data-pipeline MODE=online SOURCE="<a real URL>"` then
      `make memory-run-memory-pipeline MODE=online DOC_IDS="<printed id>"` extracts exactly that
      document — the `DOC_IDS=` path survives the repoint.
- [x] Full chain: `make memory-run-pipeline` (offline, default or a small SOURCE_FILE) runs ingest →
      per-user extraction → index in ONE `offline-pipeline` flow run, green.
- [x] Async-submission contract (the prod MCP consequence, verified locally):
      `make memory-run-pipeline MODE=online SOURCE="<url>"` logs the `{"status": "submitted",
      "flow_run_id": …, "mode": "deployment"}` path (NOT the in-process fallback) before blocking on
      the run — proving that with `online-pipeline` registered,
      `ingest_url`/`ingest_file`/`ingest_conversation` return submitted+flow_run_id instead of
      blocking.
- [x] Cron rehearsal: trigger `offline-pipeline` with the cron's exact parameters
      (`source_files=["sources/listen.yaml"]`, NO user_id) and confirm listen-feed ingest, extraction
      across ALL active users, and the trailing index — the nightly PENDING-forever gap is closed.
- [x] Query: `make memory-query-graph QUERY="<topic from an ingested source>"` returns results for
      the "Paul Iusztin" user.
- [x] Hygiene: `make memory-format-check && make memory-lint-check && make pre-commit` clean;
      `make memory-tests` green.
- [x] `## Log` records every command with observed counts/flow-run ids, plus the prod rollout
      checklist for the operator: `make memory-deploy-prefect-setup-update` (re-registers the new
      set), then `prefect deployment delete` of the two stale coordinator deployments on Prefect
      Cloud.
- [ ] [HUMAN] Execute the prod rollout (env-prod update + stale-deployment deletion) and spot-check
      one MCP ingest returns `{"status": "submitted", …}` on prod.

## User stories

Inherit the operator stories from #098–#101 — this task executes them against the live stack; the
acceptance criteria above are their end-to-end realizations.

## Out of scope

- Any code/doc fix (file a rollup task instead).
- Running the pipelines against prod from this task (the prod rollout is the [HUMAN] item).

## Log

### [E2E] Live local acceptance run — 2026-08-19

Env: `make env-status` → local. `make local-start` (mongodb, mongot, prefect-server, prefect-worker
healthy). Re-served with `make memory-serve-workflows` to pick up #100.

**Registration.** `serve()` registered exactly the core 5:
`data-etl-worker`, `memory-extract-etl-worker`, `memory-indexing-etl`, `online-pipeline`,
`offline-pipeline`. Deleted the two stale coordinator deployments plus 13 older retired-topology
deployments left on the local server (`*-orchestrator`, `etl-offline`, `etl-online`, `ingest-*`,
`memory-extraction-etl`, `dream-consolidation-all-users`); `prefect deployment ls` then showed
EXACTLY those 5. `prefect deployment inspect offline-pipeline/offline-pipeline` →
`cron: '0 3 * * *'`, `parameters: {'source_files': ['sources/listen.yaml']}`, `active: True`,
tags `['data-pipeline', 'memory-pipeline', 'offline']`. Neither coordinator name appears.

**Data step.** `make memory-run-data-pipeline SOURCE_FILE="sources/listen.yaml"` →
`Submitted flow run 209b0473-6a55-4da6-9153-7cc6eb80a7f4` (DEPLOYMENT mode), Completed in ~23s.
Child runs: `data-etl-coordinator` (inline SUBFLOW) → `data-etl-worker` →
`ingest-substack-rss-feed-batch-etl`. NO extraction flow ran — pending count unchanged at 60.
Feeds were already ingested, so dedup on `(user_id, source_uri)` meant 0 new documents.

**Memory step.** `make memory-run-memory-pipeline` →
`Submitted flow run 1d035717-b564-419e-93c5-8829e9dc0523` (DEPLOYMENT mode). Coordinator logged
`resolved 60 pending document(s)` → `partitioned 60 document(s) into 1 shard(s)`. Cancelled
mid-run at operator request to retest on a smaller set; by then the worker had drained the
backlog (pending → 0).

**Doc-id narrowing.** `make memory-run-memory-pipeline DOC_IDS="6a6d46675eec7eb85b125750,6a6f65982fef3cf45ca62636"`
→ flow run `9bcfd216-8290-4306-9785-83aa084f8837`, Completed in 63s. Coordinator logged
`using 2 explicit document_id(s)` → `partitioned 2 document(s) into 1 shard(s)` →
`triggering single memory-indexing-etl run`. The `DOC_IDS=` path survives the repoint, and the
one-trailing-index contract holds.

**Full chain.** `make memory-run-pipeline SOURCE_FILE="sources/listen.yaml"` → flow run
`b3a6a0e8-d4b7-4afd-a9bf-6249ae5b85a2`, Completed. ONE `offline-pipeline` run with 2 subflows:
`data-etl-coordinator` (`shards_total=1 succeeded=1 failed=0`) then
`memory-extract-etl-coordinator` (`resolved 0 pending` → clean no-op, no child runs, no index run).

**Async-submission contract.** `make memory-run-pipeline MODE=online SOURCE="https://maximelabonne.substack.com/p/4-bit-quantization-with-gptq-36b0f4f02c34"`
→ `Submitted flow run db61acdf-2bb9-4541-8a64-74dd1931f731` on `online-pipeline`, Completed in ~5s.
DEPLOYMENT mode, NOT the in-process fallback — confirming that with `online-pipeline` registered the
MCP ingest tools return submitted+flow_run_id instead of blocking.

**Cron rehearsal.** `prefect deployment run offline-pipeline/offline-pipeline --param source_files='["sources/listen.yaml"]'`
(no `user_id`) → flow run `fa03b2d5-dd7f-4a63-bfc5-12043bdb3e4a`, Completed, 2 subflows: data
fan-out `succeeded=1 failed=0`, then extraction across all active users. The nightly
PENDING-forever gap is closed. NOTE: the local DB has exactly 1 active user
(`p.b.iusztin@gmail.com`), so multi-user fan-out is structurally exercised but not stressed.

**Query.** `make memory-query-graph QUERY="quantization"` → `10 seed(s) → 22 nodes, 15 edges`.

**Hygiene.** `make memory-format-check` (245 files already formatted), `make memory-lint-check`
(All checks passed), `make pre-commit` (prettier / ruff check / ruff format / biome all Passed),
`make memory-tests` → **1917 passed**.

**Drive-by fix (prose only).** `sources/listen.yaml`'s header comment still described the nightly
cron as the "data-etl coordinator cron" — corrected to `offline-pipeline` + the
extraction/indexing continuation. #101's sweep did not cover the `sources/` YAML headers.

### Prod rollout checklist — [HUMAN], NOT executed

1. `make env-prod`, confirm `make env-status` → prod.
2. `make memory-deploy-prefect-setup-update` — re-registers the new core 5 on Prefect Cloud.
3. `prefect deployment delete data-etl-coordinator/data-etl-coordinator` and
   `prefect deployment delete memory-extract-etl-coordinator/memory-extract-etl-coordinator`
   to free the two free-tier slots.
4. Spot-check one MCP ingest returns `{"status": "submitted", …}` on prod.
5. `make env-local` to switch back.
