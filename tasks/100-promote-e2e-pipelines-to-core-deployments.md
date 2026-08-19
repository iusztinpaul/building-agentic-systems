---
id: 100-promote-e2e-pipelines-to-core-deployments
feature: free-tier-deployments
status: done
---

# New core 5: drop the coordinator deployments, promote `online-pipeline`/`offline-pipeline`, move the nightly cron

Tags: `infra`, `data`, `memory`
Depends on: #099
Blocks: #101, #102
Implements: ADR-002 amendment (`free-tier-deployments`)

## Scope

The topology switch in `apps/memory/src/tree/orchestrator.py`. Both execution models (local serve +
cloud managed) read `_DEPLOYMENT_SPECS`, so this one edit retargets both; `deploy/prefect_pipelines.py`
and `deploy/prefect_pipelines_setup.py` are name-agnostic (they consume `deployment_full_names()` /
`deploy_cloud_pipelines()`) — docstring-only updates there.

**`_DEPLOYMENT_SPECS`:**

- REMOVE the `data-etl-coordinator` and `memory-extract-etl-coordinator` specs (and the now-unused
  `data_etl_coordinator` / `memory_extract_etl_coordinator` imports). The flows themselves are
  untouched — they run as inline subflows of `offline_pipeline` and still `run_deployment` their
  workers + the trailing `memory-indexing-etl`.
- PROMOTE `online-pipeline` and `offline-pipeline` to core: drop their `optional=True`.
- MOVE the schedule onto the `offline-pipeline` spec: `cron=_SCHEDULED_INGEST_CRON` (unchanged
  `"0 3 * * *"`), `schedule_parameters={"source_files": ["sources/listen.yaml"]}`. Flow defaults
  leave `run_data`/`run_extraction` True and `user_id` None, so the nightly run now ingests the
  listen feeds AND extracts AND indexes across all active users — deliberately fixing the gap where
  nightly documents sat PENDING forever. No new Prefect API surface: the existing
  `_DeploymentSpec.schedules()` → `Cron(cron, parameters=…)` machinery moves between spec instances
  verbatim.
- `dream-consolidation-all-users` stays the ONLY `optional=True` spec;
  `app_config.prefect.deploy_optional` keeps gating it.
- Resulting core 5 (in spec order): `data-etl-worker`, `memory-extract-etl-worker`,
  `memory-indexing-etl`, `online-pipeline`, `offline-pipeline`.

**Docstrings/comments in `orchestrator.py`:** `_SCHEDULED_INGEST_CRON` comment (names
`data-etl-coordinator`); the module docstring's `prefect deployment run memory-extract-etl-coordinator/…`
example (targets a deployment that will no longer exist); the `_DeploymentSpec` docstring ("the data
coordinator's nightly cron…"); the comment block above `DEPLOYMENT_GROUPS`. Per settled decision, ADD
one line to `DEPLOYMENT_GROUPS` / `_active_deployment_specs` naming the overlap — `online-pipeline`
and `offline-pipeline` carry BOTH identity tags, so they fall into BOTH the `data` and `memory`
groups; a `down --groups <either>` deletes them, and the next `up` restores them. NO code change to
the groups mechanism.

**`configs/default.yaml` (~line 94):** the `prefect.deploy_optional` comment describes "the 5
always-on core ones" and the online in-process fallback note — update the wording (the optional set
is now ONLY dream; the fallback note stays true for environments where registration hasn't happened).
Key and default value unchanged.

**Deploy-script docstrings:** `deploy/prefect_pipelines_setup.py` module docstring ("the 5
deployments") — still 5, but recheck the wording/group description against the new contents; same for
any stale phrasing in `deploy/prefect_pipelines.py`. No code change.

**Tests** (per `/squid-testing-python`):

- `tests/unit/test_orchestrator.py`:
  - Registered-set test (~:101): the FULL set equals the new core 5; `data-etl-coordinator` and
    `memory-extract-etl-coordinator` explicitly asserted ABSENT (they join the retired-names list).
  - `test_deploy_optional_disabled_by_default`: 5 names; `online-pipeline` and `offline-pipeline`
    now asserted PRESENT; dream absent.
  - `test_deploy_optional_enabled_registers_optional`: 6 names (5 + dream).
  - Scheduled-deployment test (~:149/:176): `offline-pipeline` is the ONLY scheduled deployment,
    with `(_SCHEDULED_INGEST_CRON, {"source_files": ["sources/listen.yaml"]})`.
  - Groups test (~:255): the groups NO LONGER PARTITION — rewrite to assert
    `names(("data",)) == {"data-etl-worker", "online-pipeline", "offline-pipeline"}`,
    `names(("memory",)) == {"memory-extract-etl-worker", "memory-indexing-etl", "online-pipeline",
    "offline-pipeline"}`, the union is the full set, and the intersection is EXACTLY the two e2e
    pipelines (the documented overlap). Keep the unknown-group `ValueError` and
    `deployment_full_names` scoping assertions.
  - Cloud-deploy test still expects 5 `deploy()` calls — unchanged count.
  - Update the test-module docstring (it narrates the #069 topology).
- `tests/unit/test_observability_tags.py` (:88–:90): drop the two coordinator rows from the
  `_DEPLOYMENT_SPECS` tag assertions; add `tags_by_name["online-pipeline"] == TAGS_ONLINE_PIPELINE`
  and `tags_by_name["offline-pipeline"] == TAGS_OFFLINE_PIPELINE`.

**Accepted consequence (no code change, verified in #102):** with `online-pipeline` registered on
prod, the MCP `ingest_url` / `ingest_file` / `ingest_conversation` tools return
`{"status": "submitted", "flow_run_id": …}` instead of blocking for a `document_id` — intended async
behavior, not a bug.

**Ops note (executed locally in #102; prod is a [HUMAN] step there):** the server-side definitions
for the two dropped names become orphaned per environment and must be deleted
(`prefect deployment delete data-etl-coordinator/data-etl-coordinator` and
`prefect deployment delete memory-extract-etl-coordinator/memory-extract-etl-coordinator`) — same
playbook as ADR-002's #066 stale-deployment note.

**Known pre-existing gap (do NOT fix):** `web_pipeline.py:165` dispatches
`ingest-web-url-batch-etl`, which is not in `_DEPLOYMENT_SPECS` at all — out of scope, noted for a
future task.

## Acceptance criteria

- [x] `_DEPLOYMENT_SPECS` contains exactly 6 specs: the new core 5 plus
      `dream-consolidation-all-users` (`optional=True`); the two coordinator specs and their
      orchestrator imports are gone.
- [x] The `offline-pipeline` spec carries the ONE cron (`"0 3 * * *"`) with
      `schedule_parameters={"source_files": ["sources/listen.yaml"]}`; no other core spec has a
      schedule.
- [x] All updated tests in `tests/unit/test_orchestrator.py` and
      `tests/unit/test_observability_tags.py` pass, including the rewritten groups-overlap
      assertions and the ABSENT assertions for the two dropped deployment names.
- [x] `grep -n "data-etl-coordinator\|memory-extract-etl-coordinator" apps/memory/src/tree/orchestrator.py`
      returns nothing.
- [x] The `deploy_optional` comment in `configs/default.yaml`, the `orchestrator.py`
      docstrings/comments (module example, cron comment, `_DeploymentSpec`, groups overlap line),
      and the deploy-script docstrings no longer describe the old topology.
- [x] `make memory-tests` green; format/lint/pre-commit clean.

## User stories

### Story: Developer serves the new topology locally
1. Developer runs `make local-start`, then `make memory-serve-workflows`.
2. `uv run prefect deployment ls` (from `apps/memory/`) lists exactly 5 deployments:
   `data-etl-worker`, `memory-extract-etl-worker`, `memory-indexing-etl`, `online-pipeline`,
   `offline-pipeline`.
3. Inspecting `offline-pipeline/offline-pipeline` shows the single `0 3 * * *` schedule with
   `source_files=["sources/listen.yaml"]`.

### Story: The nightly cron closes the PENDING gap
1. At 03:00 UTC, Prefect fires `offline-pipeline` with `source_files=["sources/listen.yaml"]` and no
   `user_id`.
2. The run ingests the polled listen feeds fanned across all active users, then runs one extraction
   coordinator per active user (each firing its trailing index run).
3. The next morning, yesterday's feed items are queryable in the graph — no documents left PENDING
   by the schedule.

### Story: Operator on a paid plan enables the dream deployment
1. Operator sets `TREE_PREFECT__DEPLOY_OPTIONAL=true` and re-serves.
2. Six deployments register: the core 5 plus `dream-consolidation-all-users` with its own cron —
   the only optional spec left.

## Out of scope

- Prose docs (#101) and live E2E verification (#102).
- The `ingest-web-url-batch-etl` registration gap (`web_pipeline.py:165`) — pre-existing, unrelated.
- Deleting `DEPLOYMENT_GROUPS` or changing the `--groups` selector code.
- Any change to dream consolidation.

## Log

### [SWE] 2026-08-19 — Implementation

**Files modified**
- `apps/memory/src/tree/orchestrator.py` — dropped the `data-etl-coordinator` /
  `memory-extract-etl-coordinator` specs + their imports; promoted `online-pipeline` /
  `offline-pipeline` out of `optional=True`; moved `_SCHEDULED_INGEST_CRON` (+
  `source_files=["sources/listen.yaml"]`) onto `offline-pipeline`; refreshed the module
  docstring example, cron comment, `_DeploymentSpec` / `_active_deployment_specs` docstrings and
  the `DEPLOYMENT_GROUPS` comment (documents the data∩memory overlap).
- `apps/memory/configs/default.yaml` — `prefect.deploy_optional` comment now names the new core 5
  and says dream is the ONLY optional deployment. Key + default unchanged.
- `apps/memory/deploy/prefect_pipelines_setup.py`, `apps/memory/deploy/prefect_pipelines.py` —
  docstrings only: the 5 core names and the non-partitioning `--groups`/`GROUPS` semantics.
- `apps/memory/tests/unit/test_orchestrator.py` — module docstring; registered-set = core 5 with
  both coordinator names asserted ABSENT; `deploy_optional` off = 5 (online/offline PRESENT), on =
  6; schedule test renamed to `test_serve_deployments_schedules_only_the_offline_pipeline`; groups
  test rewritten for the overlap (union = full set, intersection = the two e2e pipelines).
- `apps/memory/tests/unit/test_observability_tags.py` — dropped the two coordinator tag rows, added
  `online-pipeline` / `offline-pipeline` == `TAGS_ONLINE_PIPELINE` / `TAGS_OFFLINE_PIPELINE`.

**Evidence**
```
$ grep -n "coordinator" apps/memory/src/tree/orchestrator.py     # (no output)
$ uv run python -c "from tree.orchestrator import ..."
[('data-etl-worker', False, None, {}), ('memory-extract-etl-worker', False, None, {}),
 ('memory-indexing-etl', False, None, {}), ('online-pipeline', False, None, {}),
 ('offline-pipeline', False, '0 3 * * *', {'source_files': ['sources/listen.yaml']}),
 ('dream-consolidation-all-users', True, '0 4 * * *', {})]
groups data   -> ['data-etl-worker', 'offline-pipeline', 'online-pipeline']
groups memory -> ['memory-extract-etl-worker', 'memory-indexing-etl', 'offline-pipeline', 'online-pipeline']
$ make memory-format-fix && make memory-lint-fix && make memory-lint-check
All checks passed!
```

**Notes**
- `make memory-tests` NOT RUN — the orchestrating run explicitly deferred the suite (and
  pre-commit) to the verification pass; last acceptance box left unchecked for that reason.
- Not committed, per the same instruction; changes sit in the working tree on
  `feat/free-tier-deployments`.
- Out of scope, left alone: the `run-data-pipeline` help text in `apps/memory/Makefile:171` still
  says "data-etl-coordinator" (prose, #101), and the flow-level `*-coordinator` names in
  `data/offline_pipeline.py` / `memory/extraction/pipeline.py` (flows are untouched by design).

### [SWE] 2026-08-19 — Suite verification (no code change)

Ran the deferred suite pass. The rewritten `tests/unit/test_orchestrator.py` (9 passed) and
`tests/unit/test_observability_tags.py` (10 passed) pass as written — no fixes needed.

- `make memory-tests` (full suite, env target local): **1917 passed, 0 failed**.
- `make memory-format-check && make memory-lint-check && make pre-commit`: clean.
