# MongoDB Atlas setup

One-time setup for the remote (`production`) environment. Everything the Atlas
cluster needs afterwards is code — `apps/memory/deploy/atlas_cluster.py`, driven
by the `make memory-atlas-*` targets — but the credential that runs that code
has to be created by hand first. This page is the manual half.

The same `MDB_MCP_API_CLIENT_ID` / `MDB_MCP_API_CLIENT_SECRET` pair serves both
the IaC CLI and the MongoDB MCP server, so this setup unlocks both.

## 1. In the Atlas console (manual, one-time)

**a. Create the project.** `atlas_cluster.py` looks the project up by name
(`GET /groups/byName/{project}`) and never creates one. A project named `Tree`
(or whatever you pass via `--project`) must exist before `make memory-atlas-up`
will do anything but fail.

**b. Create a Service Account.** Organization → **Identity & Access** →
**Applications** → **Add new** → **Service Account**. Copy the Client ID and
Secret — the secret is shown exactly once.

**c. Grant it project roles.** Project Owner, or the three narrower roles the
script actually exercises: Cluster Manager (clusters), Database Access Admin
(the seed DB user), Network Access Manager (the IP access list).

**d. Add your IP to the service account's API Access List.** This is *not* the
same list as the project IP access list that `atlas-up` manages — it gates who
may call the Admin API with these credentials. Miss it and the OAuth token
exchange or the first API call fails with a 401.

## 2. Locally

Fill `.env.prod` (`cp .env.example .env.prod` on a fresh clone):

| Variable | Purpose |
| --- | --- |
| `MDB_MCP_API_CLIENT_ID` | Service-account client ID from step 1b |
| `MDB_MCP_API_CLIENT_SECRET` | Service-account secret from step 1b |
| `MONGO_INITDB_ROOT_USERNAME` | Seed DB user created on the cluster |
| `MONGO_INITDB_ROOT_PASSWORD` | Its password — generate with `make generate-password` |
| `MONGO_SCHEME=mongodb+srv` | Required for the app to reach Atlas (TLS implied) |
| `MONGO_HOST` | The cluster hostname — **unknown until step 4**, leave it for now |
| `ATLAS_ACCESS_CIDRS` | *Optional*, comma-separated extra CIDRs for the project access list |

Then `direnv allow` if `.envrc` has never been approved in this clone.

Use `make generate-password` for the DB password rather than an arbitrary
generator. `settings.py` builds the connection URI by plain f-string
interpolation with **no percent-encoding**, so a password containing
`@ : / ? # %` corrupts the URI. `secrets.token_urlsafe` emits only RFC 3986
unreserved characters, which survive that interpolation intact — whereas
`openssl rand -base64` emits `+` and `/` and will break it.

## 3. Switch to the prod env target, then run

```bash
make env-prod               # REQUIRED — see below
make memory-atlas-up        # cluster + DB user + IP access list, waits for IDLE
make memory-atlas-status    # state + connection string
```

`make env-prod` is not optional. The Atlas credentials live only in `.env.prod`;
on the `local` target make includes `.env`, which has none, and the script exits
with *"Set MDB_MCP_API_CLIENT_ID and MDB_MCP_API_CLIENT_SECRET"*.

The env target matters for a second reason: the seed DB user reuses
`MONGO_INITDB_ROOT_USERNAME` / `MONGO_INITDB_ROOT_PASSWORD`, and `.env` carries
the local dev pair (`tree` / `tree`). Running `atlas-up` against a local target
would seed dev credentials onto the remote cluster.

Non-default cluster shapes go through `ATLAS_ARGS`:

```bash
make memory-atlas-up ATLAS_ARGS="--cluster tree-staging --tier M10 --provider AWS --region US_EAST_1"
```

## 4. Copy the cluster hostname into `MONGO_HOST`

Atlas assigns the hostname at creation time — `tree.w5kc0am.mongodb.net`, where
`tree` is the cluster name and `w5kc0am` is a random identifier you don't
choose. It doesn't exist until `atlas-up` completes, which is why `MONGO_HOST`
can't be filled in ahead of step 3. Nothing writes it back to `.env.prod`; this
step is manual by design, so the script never mutates your secrets file.

`atlas-up` prints it as its last line, and `make memory-atlas-status` reprints it
at any time:

```
mongodb+srv://tree.w5kc0am.mongodb.net
```

**Strip the scheme before pasting.** The Atlas API returns `standardSrv` with the
`mongodb+srv://` prefix, but `MongoSettings.mongo_uri` prepends the scheme
itself. `MONGO_HOST` takes the bare host:

```bash
MONGO_SCHEME=mongodb+srv
MONGO_HOST=tree.w5kc0am.mongodb.net      # NOT mongodb+srv://tree.w5kc0am.mongodb.net
```

Pasting the full string yields `mongodb+srv://user:pass@mongodb+srv://tree…` and
a DNS failure that reads like a network problem. `MONGO_SCHEME=mongodb+srv` must
be set alongside it — otherwise the app builds the local-style URI with
`directConnection=true` against an SRV host and fails. `MONGO_PORT` is ignored in
SRV mode (SRV URIs forbid explicit ports).

Verify the whole chain — host, scheme, seed user, network access — in one shot:

```bash
make memory-check-db
```

The hostname is stable for the cluster's lifetime, so this is a once-per-
environment step. After an `atlas-down` + recreate, re-read it with
`memory-atlas-status`; the identifier can differ.

## Gotchas

**Missing seed-user variables are skipped silently.** `atlas_cluster.py` only
creates the DB user when *both* `MONGO_INITDB_ROOT_USERNAME` and
`MONGO_INITDB_ROOT_PASSWORD` are set — with no warning otherwise. The cluster
reaches IDLE and prints a connection string, and the missing user surfaces much
later as an auth failure. Check with `make memory-check-db`.

**Rotating the DB password is not idempotent.** `ensure_db_user` treats HTTP 409
as success, so changing `MONGO_INITDB_ROOT_PASSWORD` and re-running `atlas-up`
leaves the Atlas user on the old password — silently. Rotate in the Atlas UI,
then update `.env.prod`. (Locally, `MONGO_INITDB_ROOT_*` only apply on first init
of an empty data directory.)

**`0.0.0.0/0` is always on the project access list.** Hardcoded as
`HORIZON_EGRESS_CIDR` because Prefect Horizon's serverless runner has an
unpinnable egress IP. Anything in `ATLAS_ACCESS_CIDRS` is added on top; it never
narrows this. The database stays protected by SCRAM auth + TLS only.

**Don't point the test suite at Atlas.** M0 caps the number of Atlas Search
(FTS) indexes; index-creating tests fail with *"The maximum number of FTS
indexes has been reached for this instance size"*. Run tests on `make env-local`.

## Related

* `docs/notes/deployment-runbook.md` — the from-scratch deployment order this is step 1 of.
* README → *Managing the Atlas cluster as code (IaC)* — the day-to-day command reference.
* [MongoDB MCP server prerequisites](https://www.mongodb.com/docs/mcp-server/prerequisites/) — upstream docs for the service account.
