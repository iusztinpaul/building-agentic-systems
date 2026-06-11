# From-scratch deployment runbook

The verified order is **MongoDB Atlas → user sign-up → Prefect Cloud (+ one
indexing run) → FastMCP on Prefect Horizon**. Each step depends on the previous
one at *boot time*, not just logically — the dependencies are listed with each
step so the order is auditable, not folklore.

Prereqs: `.env.prod` filled in (see `.env.example`), `make env-prod` active,
`GITHUB_PAT` + Prefect Cloud + Atlas service-account credentials at hand.

## 1. MongoDB Atlas

```
make memory-atlas-up        # cluster + DB user + IP access list, waits for IDLE
```

Owns ONLY infra: the cluster, the database user, and network access
(including the Prefect/Horizon egress CIDRs via `ATLAS_ACCESS_CIDRS`). It does
NOT create app data or indexes.

* Collection (Beanie) indexes are self-healing: every `init_mongodb()` call
  (sign-up, pipelines, MCP boot) runs `init_beanie`, which ensures the declared
  indexes on all document models. No explicit step needed.
* Atlas Search indexes (`text_index`, `vector_index` on `knowledge_graph`) are
  NOT created here — see step 3. Mind the M0 cap ("maximum number of FTS
  indexes... for this instance size"): don't point test suites at this cluster.

## 2. User sign-up

```
make memory-signup IDENTIFIER=<email> NAME="<display name>"
```

Must run BEFORE anything that resolves a user:

* The MCP server REFUSES to boot when `TREE_USER_IDENTIFIER` matches no `User`
  row (no silent default-user creation — `scripts/signup.py` is the single
  creation path).
* Every pipeline deployment takes a required `user_id` parameter; the printed
  ObjectId is the value to pass.

## 3. Prefect Cloud (pipelines)

```
make memory-deploy-prefect-setup-up      # Managed work pool + Secret blocks/Variables + 5 deployments
make memory-run-memory-pipeline-indexing USER_ID=<oid>   # first indexing run
```

`up` is idempotent IaC (`deploy/prefect_pipelines_setup.py`); afterwards the CD
workflow (`.github/workflows/cd.yml`) keeps the deployment specs in sync on
every green push to `main` — flow code itself is branch-tracking (cloned from
`main` at run time), so merges go live without a re-deploy.

The first indexing run matters: it creates the Atlas Search indexes
(`ensure_indexes`). The cloud MCP server (step 4) boots with
`MCP_SKIP_INDEX_BOOTSTRAP=true` and only QUERIES the indexes — query tools fail
or return nothing until this run has happened.

## 4. FastMCP server on Prefect Horizon

Deployed via Horizon's GitHub integration (entrypoint
`apps/memory/src/tree/mcp/server.py:mcp`); pushes to `main` redeploy it.
Horizon env must set:

* `TREE_USER_IDENTIFIER` — the identifier from step 2 (boot fails loudly
  otherwise),
* `MCP_SKIP_INDEX_BOOTSTRAP=true` — index bootstrap (Atlas index create +
  mongot sync poll) would blow the 60s serverless readiness window,
* `TREE_WORKING_DIR=/tmp/.tree` — the install dir is read-only,
* the Mongo/API credentials (see `.env.example`).

Auth is platform-level (Horizon Authentication: OAuth + org membership) — the
endpoint 401s without a bearer token; clients use `auth="oauth"`
(`tree.mcp.client.get_cloud_client`) or Claude Code's native MCP OAuth.

## Teardown

Reverse order: Horizon server (UI) → `make memory-deploy-prefect-setup-down` →
`make memory-atlas-down`.
