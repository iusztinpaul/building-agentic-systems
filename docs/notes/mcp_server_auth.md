# MCP server auth — who the user is, and who proves it

Design note on how the `tree-memory` MCP server learns which user a request belongs to, locally and on
Prefect Horizon, what is verified versus merely claimed, and how to move to a different auth source later
without touching the tools. Companion to `mcp_server_design.md` (why the visualization download broke) and
ADR-014 (the decision record).

Grounding: `apps/memory/src/tree/mcp/request_user.py`, `apps/memory/src/tree/entities/users.py`,
`apps/memory/scripts/{serve_mcp,signup}.py`, ADR-014, and Horizon's docs quoted inline
(https://docs.horizon.prefect.io — `gateway.md`, `platform/authentication.md`,
`platform/authorization.md`, `servers/hosted.md`, `members.md`, `connect-a-client.md`, `api-keys.md`).

---

## TL;DR

| Where the server runs | Transport | Where the identity comes from | Verified? |
|---|---|---|---|
| Prefect Horizon | HTTP | `horizon-actor-email` header, injected by Horizon's gateway after sign-in | **yes** — by Horizon |
| Local, `serve_mcp.py --transport http` | HTTP | `horizon-actor-email` header, set by the client itself | no — a claim on your own machine |
| Local, `tree-memory-local` | stdio | `TREE_USER_IDENTIFIER` process env (from `.env`) | no — a claim on your own machine |

Only the **injection** differs. Everything after `resolve_request_user(ctx)` is identical on every path:
look up `User` by identifier (case-insensitive), answer `configuration_error` if it is missing or unknown,
scope every tool and the `graphs://` download by the resolved `user_id`.

---

## Before: one user pinned at boot

- `serve_mcp.py`'s user flags or `TREE_USER_IDENTIFIER` were resolved ONCE at boot into
  `_SERVER_USER_ID`; every tool read it from the lifespan context.
- Fine for one person and one stdio process. On Horizon, with one shared credential, every caller read,
  searched and ingested into the same user's memory, and rendered graph files had no owner.

## Now: one seam, resolved per request

`tree.mcp.request_user.resolve_request_user(ctx) -> PydanticObjectId` is the ONLY place that knows where
identity comes from:

1. `ctx.transport == "stdio"` → read ONLY env `TREE_USER_IDENTIFIER`.
2. Any other transport → read ONLY the `horizon-actor-email` header (`get_http_headers()`, lowercased names).
3. No cross-fallback: the server's env is ignored on HTTP; headers are ignored on stdio.
4. Empty source → `configuration_error` (`retryable: false`) naming the fix.
5. `find_user_by_identifier` — anchored, escaped, case-insensitive `$regex` on `User.identifier` (regex
   injection like `.*` or `a|b` matches nothing). Unknown → `configuration_error` naming
   `make memory-signup` and "the identifier must equal your Horizon account email".
6. A Mongo outage during the lookup → `storage_unavailable` (`retryable: true`).

Every tool calls the seam first; `graphs://` calls it too and raises `ResourceError` with the same messages.
`serve_mcp.py` has only `--transport`; nothing user-related happens at boot.

---

## How Horizon authenticates (what the docs say)

**Getting an account.** "Organization admins can invite a person by email" (or Directory Sync from an
identity provider); "An invited person joins after accepting the invitation and completing sign-in"
(`members.md`). Enterprise SSO exists (`administration/sso.md`).

**Connecting a client.** "Clients that support authenticated MCP connections open a Horizon sign-in flow the
first time they connect. The user signs in, and the client holds the resulting credential"
(`connect-a-client.md`). Claude Code: add the server, run `/mcp`, sign in in the browser. Claude Desktop,
Cursor, Codex and ChatGPT support the same flow.

**Headless clients.** "Clients that cannot sign in interactively send a Horizon API key as a bearer token"
(`fmcp_…` in `Authorization: Bearer`). A personal key "acts as one user across every organization where
that user is a member"; a service-account key "acts as one service account" (`api-keys.md`).

**Every request through the gateway.** "Horizon authenticates the bearer credential and resolves it to a
user or service account" (`authorization.md`). "The actor must belong to the organization that owns the
server. An actor in another organization cannot use that organization's servers." Then: "the gateway
removes any client-supplied `horizon-*` identity headers and adds trusted actor context before invoking a
hosted server" (`gateway.md`):

| Header | Meaning |
|---|---|
| `horizon-actor` | Horizon ID of the user or service account |
| `horizon-actor-type` | `user` or `service_account` |
| `horizon-actor-email` | "The user's email address when the actor has one" |
| `horizon-user-role` | the actor's organization role |
| `horizon-server-roles` | the actor's resolved server roles |

**What the docs do NOT say.** Which providers the Horizon sign-in page offers (Google, GitHub, password),
whether Horizon verifies email ownership at sign-up, and whether the invite email must match the account
used to sign in. So "verified" here means **Horizon's authenticated organization member** — the server
trusts the gateway's actor, not a proof that the person owns the mailbox.

---

## Trust model and hard constraints

- **Horizon authentication must stay enabled.** "Requests to a hosted server with Horizon authentication
  disabled do not receive verified Horizon actor context", and the stripping of client-supplied `horizon-*`
  headers only happens "when Horizon authentication succeeds". With auth disabled, anyone could send
  `horizon-actor-email: someone@else.com` and the server would believe it.
- **Never expose the HTTP server without Horizon's gateway in front.** Locally the header is a claim, which
  is fine on your own machine and nowhere else.
- **Service accounts** may carry no email → `configuration_error`. Mapping `horizon-actor` ids to users is a
  follow-up, not built.
- **Identity is a string match.** The Horizon account email must equal `User.identifier`. `signup.py` stores
  new identifiers stripped + lowercased and refuses case-only duplicates; the lookup is case-insensitive so
  older mixed-case rows still match.

## Operating it

- **Give someone access:** invite them to the Horizon organization, then
  `make memory-signup USER_IDENTIFIER=<their Horizon account email>`. Both steps are required — the first
  gets them through the gateway, the second gives them a memory.
- **Horizon env:** do NOT set `TREE_USER_IDENTIFIER` (ignored on HTTP anyway).
- **Local stdio:** keep `TREE_USER_IDENTIFIER` in `.env`; `.mcp.json` / `.pi/mcp.json` need no change.
- **Local HTTP:** the client sets the header itself, e.g.
  `fastmcp.Client(StreamableHttpTransport(url, headers={"horizon-actor-email": "<you>"}))` (the
  `run-pipelines-e2e` skill has a runnable snippet).
- **Existing handle-style users** keep working on stdio; Horizon needs a signup with the Horizon email,
  which is a separate memory (no re-keying).

---

## Upgrade path: public self-serve sign-up

Horizon's gateway admits **organization members only** — there is no self-serve sign-up for outsiders while
Horizon auth is on. For a service where anyone can create an account:

1. Disable Horizon authentication on the server (Developer / Enterprise plans): "Horizon forwards without
   validating a Horizon bearer credential; the server owns authentication and authorization"
   (`servers/hosted.md`).
2. Add a FastMCP auth provider (Google, GitHub, WorkOS AuthKit, Auth0, …) so the server runs its own OAuth.
3. Change ONLY the HTTP branch of `resolve_request_user`: read the email from the verified token
   (`get_access_token()` claims) and **ignore `horizon-actor-email` entirely** — with gateway auth off it is
   neither injected nor stripped, so it would be forgeable.
4. Sign-up (creating the `User` row) moves from `make memory-signup` to the first authenticated request or
   a sign-up flow.

Tools, `graphs://` and every downstream query stay untouched — the seam is the only place identity lives.

Not a fit: Horizon's **external authentication** (`platform/external-authentication.md`) is for Horizon
passing credentials to downstream services, and requires Horizon authentication to stay enabled.

## Open live checks (task 189)

- The exact `horizon-actor-email` value and case our account receives, plus `horizon-actor-type`.
- A client-forged `horizon-actor-email` resolves to the gateway's value, not the forged one.
- A personal API key (`Authorization: Bearer fmcp_…`) resolves to the same user as the OAuth sign-in.
