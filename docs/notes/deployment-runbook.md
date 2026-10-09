# From-scratch deployment runbook

The verified order is **MongoDB Atlas → user sign-up → Prefect Cloud (+ one
indexing run) → FastMCP on Prefect Horizon**. Each step depends on the previous
one at *boot time* or, for the sign-up, at the *first request* (the server
resolves the user per request, not at boot) — the dependencies are listed with
each step so the order is auditable, not folklore.

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
* Atlas Search indexes (`vector_index`, `text_search_index` on `memory`) are
  NOT created here — see step 3. Mind the M0 cap ("maximum number of FTS
  indexes... for this instance size"): don't point test suites at this cluster.

## 2. User sign-up

```
make memory-signup USER_IDENTIFIER=<email> NAME="<display name>"
```

Must run BEFORE anything that resolves a user:

* Every MCP request names its user. On Horizon the gateway attaches the
  authenticated actor's email (`horizon-actor-email`) and the server looks it
  up as `User.identifier` (case-insensitively), so **giving someone access =
  invite them to the Horizon org + `make memory-signup USER_IDENTIFIER=<their
  Horizon account email>`**. An email with no `User` row answers
  `configuration_error` naming `make memory-signup`; a service-account key
  carries no email and answers `configuration_error`. The server no longer pins
  a user at boot (no silent default-user creation — `scripts/signup.py` is the
  single creation path).
* Every pipeline deployment takes a required `user_id` parameter; the printed
  ObjectId is the value to pass.

## 3. Prefect Cloud (pipelines)

```
make memory-deploy-prefect-setup-up GROUPS=data   # pool + blocks + the 3 data deployments
make memory-deploy-prefect-setup-up GROUPS=memory # add extraction + dream when you need them
make memory-run-indexing-pipeline USER_ID=<oid>   # first indexing run (dispatches offline-pipeline)
```

`GROUPS=data|memory` (comma-separated) scopes every verb — `up`, `update`,
`status`, `down` — to whole pipelines; unset means all 5 core deployments (`data` selects 3:
the data worker + both end-to-end pipelines; `memory` selects 4: the extraction worker + dream +
both end-to-end pipelines, which carry BOTH identity tags). Start with
`data` so nothing but ingestion is registered while you verify the cluster, then
re-run with `memory`. A group-scoped `down` keeps the work pool.

`up` is idempotent IaC (`deploy/prefect_pipelines_setup.py`); afterwards the CD
workflow (`.github/workflows/cd.yml`) keeps the deployment specs in sync on
every green push to `main` (a direct push or a merged PR), pinned to that commit:
each managed run clones exactly the SHA that passed CI, so a red push never runs.
`up` / `update` default to `--git-ref main` (branch-tracking) until the next green
push re-pins them. `make memory-deploy-prefect-setup-status` prints each
deployment's `ref=commit:<sha7>` and flags any ORPHAN deployment on the pool — CD
re-applies the current specs but never deletes a renamed or removed one. CD deploys
only the tip of `main`, so a green commit with a red commit landed on top stays
undeployed; `GIT_REF=<40-hex sha> make memory-deploy-prefect` pins a commit by hand
(that case, or a rollback).

The first indexing run matters: it creates the Atlas Search indexes
(`ensure_indexes`). It dispatches the `offline-pipeline` deployment with only the
indexing phase on, so it works right after `GROUPS=data` — that group already
registers `offline-pipeline`. The cloud MCP server (step 4) boots with
`MCP_SKIP_INDEX_BOOTSTRAP=true` and only QUERIES the indexes — query tools fail
or return nothing until this run has happened.
Re-run it after ANY deploy that changes a search-index definition (ADR-015 added
`text_search_index`): until the run, `search_memory` answers `search_mode: vector_only`.

## 4. FastMCP server on Prefect Horizon

Deployed via Horizon's GitHub integration (entrypoint
`apps/memory/src/tree/mcp/server.py:mcp`); pushes to `main` redeploy it.
Horizon env must set (not `TREE_USER_IDENTIFIER` — ignored on HTTP; the
request's `horizon-actor-email` names the user, step 2):

* `MCP_SKIP_INDEX_BOOTSTRAP=true` — index bootstrap (Atlas index create +
  mongot sync poll) would blow the 60s serverless readiness window,
* `TREE_WORKING_DIR=/tmp/.tree` — the install dir is read-only and deep-search
  session files go here (graph downloads live in Mongo),
* the Mongo/API credentials (see `.env.example`).

Optional: `TREE_MCP__GRAPH_FILE_TTL_SECONDS` — how long a `graphs://` download
link stays readable (default 300).

**Horizon Authentication must stay enabled (Access mode never "Disabled")**:
with it disabled the gateway neither verifies nor strips `horizon-*` headers,
so anyone could name any user (ADR-014 §3). Users authenticate with Horizon
OAuth (`/mcp` in Claude Code, `auth="oauth"` in Python via
`tree.mcp.client.get_cloud_client`) or a personal API key (`Authorization:
Bearer fmcp_…`) — both resolve to the same actor email. Without either, the
endpoint answers 401.

## Teardown

Reverse order: Horizon server (UI) → `make memory-deploy-prefect-setup-down` →
`make memory-atlas-down`.
