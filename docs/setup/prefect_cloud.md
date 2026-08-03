# Prefect Cloud setup

One-time setup for running the pipelines on a Prefect Cloud **Managed work pool** —
Prefect hosts the workers, so flow runs (including the async ingestion the MCP
server submits) execute with no self-hosted `serve` process.

Run this **after** `docs/setup/mongodb_atlas.md`. The setup script copies
`MONGO_HOST` and the rest of the runtime config out of your environment into
Prefect's config stores, so the Atlas cluster must already exist and `.env.prod`
must already point at it.

Two scripts own this, with deliberately asymmetric requirements:

| Script | Make target | Needs |
| --- | --- | --- |
| `deploy/prefect_pipelines_setup.py` | `memory-deploy-prefect-setup-*` | Everything — it seeds the secrets |
| `deploy/prefect_pipelines.py` | `memory-deploy-prefect` | Only `PREFECT_API_URL` + `PREFECT_API_KEY` |

The asymmetry is the point: the managed-run environment is a static mapping of
`{{ prefect.blocks.secret.* }}` / `{{ prefect.variables.* }}` references, never
raw values. The operator seeds those stores once; CI only re-applies the
references, so the CD path never handles an app secret.

## 1. Prefect Cloud account (manual, one-time)

Sign up, create a workspace, then create an API key under your profile →
**API keys**. That gives you two values for `.env.prod`:

```bash
PREFECT_API_URL=https://api.prefect.cloud/api/accounts/<account-id>/workspaces/<workspace-id>
PREFECT_API_KEY=pnu_...
```

Note the Cloud URL form — `.env.example` ships the local default
(`http://127.0.0.1:4200/api`), which points at the Docker Prefect server, not Cloud.

**Free tier allows one work pool per workspace.** `_ensure_work_pool` reads
before creating precisely because the create endpoint enforces that limit
*before* the duplicate check, returning a 403 (limit reached) rather than a
swallowable 409. If the workspace already has a pool from another project, `up`
cannot add `tree-managed` — delete the other pool or use a fresh workspace.

## 2. GitHub PAT (manual, one-time)

Prefect Managed **clones this private repo** at both deploy time and run time, so
it needs a read token → `GITHUB_PAT` in `.env.prod`. Either form works:

* a **classic** PAT with the `repo` scope, or
* a **fine-grained** PAT whose *Resource owner* is the repo owner, with this
  repository selected and **Contents: Read-only**.

`up` probes the token against the GitHub API before doing anything
(`_verify_pat_access`), because an unauthorized token otherwise surfaces as a
cryptic `git clone ... exit code 128` deep inside `from_source`.

The token is stored once as the `tree-github-pat` Secret block; the deployments
reference the block, so CD never needs the PAT either.

## 3. `.env.prod` runtime config

`up` seeds every entry of `RUNTIME_CONFIG` (`src/tree/orchestrator.py`) from your
environment into a Prefect Secret block (secrets) or Variable (non-secret config):

| Group | Variables |
| --- | --- |
| MongoDB | `MONGO_SCHEME`, `MONGO_HOST`, `MONGO_PORT`, `MONGO_INITDB_ROOT_USERNAME`, `MONGO_INITDB_ROOT_PASSWORD`, `MONGO_INITDB_DATABASE` |
| Models | `GOOGLE_API_KEY`, `VOYAGE_API_KEY` |
| Crawling | `BRIGHTDATA_API_KEY`, `BRIGHTDATA_UNLOCKER_ZONE`, `BRIGHTDATA_SERP_ZONE` |
| Observability | `OPIK_API_KEY`, `OPIK_WORKSPACE`, `OPIK_PROJECT_NAME` |

All of these must hold their **production** values before `up` runs — the Mongo
group in particular must be the Atlas cluster, not `localhost`.

Not needed by these scripts: `PREFECT_ACCOUNT_ID`, `PREFECT_WORKSPACE_ID`,
`PREFECT_API_URL_WORKER`, `PREFECT_HORIZON_API_KEY`. Those belong to the worker
container and the Horizon MCP deployment; leave them for those steps.

## 4. Provision

```bash
make env-prod                            # REQUIRED — creds live only in .env.prod
make memory-deploy-prefect-setup-up      # blocks + work pool + deployments (idempotent)
make memory-deploy-prefect-setup-status  # pool + each deployment's work-pool binding
```

`up` is idempotent, so re-running it after adding a missing key re-seeds the
stores. Scope any verb to one pipeline family with `GROUPS=data` or
`GROUPS=memory` (unset = all).

Day-to-day afterwards:

```bash
make memory-deploy-prefect-setup-update  # re-deploy code/spec only (pool + blocks exist)
make memory-deploy-prefect-setup-down    # delete deployments (+ pool when unscoped)
```

## 5. GitHub Actions (for the CD path)

`.github/workflows/cd.yml` runs `make memory-deploy-prefect` on every push to
`main` after CI passes, keeping the Cloud deployments in sync with the code. Add
two **repository secrets** (Settings → Secrets and variables → Actions):

* `PREFECT_API_URL`
* `PREFECT_API_KEY`

Nothing else. The workflow's other env values are deliberate mocks — importing
the flow modules constructs settings objects and clients, so the variables must
be *present*, but the deploy itself only talks to the Prefect API.

## Gotchas

**Empty env vars are seeded blank, with only a warning.** `_seed_config_stores`
logs `Env <VAR> is empty; seeding <store> blank` and carries on, so `up` reports
success and the gap surfaces much later as a managed run that can't
authenticate. Scan the `up` output for `WARNING` lines before trusting it.

**Never run `make memory-serve-workflows` against the Cloud workspace.** Local
serve and `up` register the same deployment names and clobber each other. A
clobbered deployment shows as `work_pool=<none — clobbered, re-run up>` in
`status`; re-run `up` or `...-update` to restore it. Local serve belongs against
the local Prefect server from `make local-start`.

**Deployments track the `main` branch by default.** `--git-ref` (or `GIT_REF`)
pins them to a branch or commit instead; branch-tracking means merges go live
without a re-deploy, which is usually what you want but does mean a bad merge
reaches the managed workers immediately.

**`down` does not remove blocks or variables.** It deletes the deployments and
(when unscoped) the work pool, then logs a reminder to delete `tree-github-pat`
and the `tree-*` blocks/variables in the Prefect UI by hand.

## Related

* `docs/setup/mongodb_atlas.md` — the prerequisite step; `MONGO_HOST` must be real before `up`.
* `docs/notes/deployment-runbook.md` — full from-scratch deployment order.
* `docs/notes/prefect-execution-topologies.md` — why Managed work pools over a self-hosted worker.
