---
id: 186-request-user-seam
status: pending
feature: request-scoped-users
---

# The **Request user** seam: `resolve_request_user(ctx)` replaces the boot-pinned user — HTTP reads ONLY Horizon's verified `horizon-actor-email` header, stdio ONLY `TREE_USER_IDENTIFIER`, every tool answers `configuration_error` when the user is absent or unknown; `serve_mcp.py` loses `--user-id` / `--identifier`

Tags: `mcp`, `auth`, `config`, `cli`
Depends on: —
Blocks: 187 (the resource and the file branch scope by the request user), 189 (docs)
Implements: ADR-014 §1–§3

## Problem

`server.py` resolves ONE user at boot (`--user-id` / `TREE_USER_IDENTIFIER`) and every tool reads
`ctx.lifespan_context["user_id"]` (19 reads across `tools.py`, `graph_tools.py`, `dashboard_app.py`, plus
`_set_retrieval_thread`'s Opik metadata). On Horizon every caller of the deployment shares one memory, and
graph files carry no owner. Horizon's gateway already attaches a VERIFIED identity to every authenticated
request (`horizon-actor-email`, after stripping any client-supplied `horizon-*` header); the server never
reads it. The user must be resolved per request, from ONE source per transport, through ONE seam.

## Scope

**Human decision (final):** `resolve_request_user(ctx) -> PydanticObjectId`, called per request by every
tool (and, in task 187, the `graphs://` resource). Identifier source — exactly ONE per transport, no
cross-fallback, always an error when absent: HTTP (any HTTP transport) → ONLY the `horizon-actor-email`
request header — on Horizon the gateway injects it after Horizon authentication succeeds and strips any
client-supplied `horizon-*` header (gateway.md "Actor context"); locally (`serve_mcp.py --transport http`)
there is no gateway, so the local CLIENT sets the header itself. A server-side `TREE_USER_IDENTIFIER` is
ignored on HTTP even if set. stdio → ONLY the process env `TREE_USER_IDENTIFIER`. Lookup
`User.find_one(User.identifier == identifier)` — a Horizon user's `User.identifier` is their Horizon
account email. Errors reuse the **Tool error envelope** `configuration_error`, `retryable: false`. The
lifespan no longer resolves or pins a user. No `X-Tree-User`, no client-config `headers` block.

1. **New module** `apps/memory/src/tree/mcp/request_user.py` (imports `fastmcp`, `beanie`, `tree.entities.users`
   only — nothing from `tree.mcp.tools`, so no cycle):
   - `ACTOR_EMAIL_HEADER = "horizon-actor-email"`, `USER_ENV_VAR = "TREE_USER_IDENTIFIER"`.
   - `class RequestUserError(Exception)` — the message IS the user-facing sentence.
   - Three message constants (test anchors, the `ERROR_CONTRACT` pattern — asserted verbatim):
     `MISSING_HEADER_MESSAGE` = `No horizon-actor-email header on this request. On Prefect Horizon the
     gateway adds it when Horizon authentication is enabled for this server (Access mode must not be
     "Disabled") and the caller is a user with an email (a service account may have none). Against a
     local HTTP server there is no gateway: set the header on your client, e.g.
     fastmcp.Client(StreamableHttpTransport(url, headers={"horizon-actor-email": "<your signed-up
     identifier>"})).`; `MISSING_ENV_MESSAGE` = `TREE_USER_IDENTIFIER is empty in this stdio server's
     environment. Set it in the "env" block of the tree-memory-local entry (.mcp.json), or in .env, which
     that entry loads with --env-file.`; `unknown_identifier_message(identifier)` = `No user with identifier
     '<identifier>' — run \`make memory-signup USER_IDENTIFIER=<identifier>\` (scripts/signup.py) first; on
     Horizon the signed-up identifier must equal your Horizon account email.`
   - `async def resolve_request_user(ctx: Context) -> PydanticObjectId`: `ctx.transport == "stdio"` →
     `os.environ.get(USER_ENV_VAR, "").strip()`; ANY other value (`"streamable-http"`, `"sse"`, and `None`,
     which FastMCP returns outside a server context) → `get_http_headers().get(ACTOR_EMAIL_HEADER, "")
     .strip()` (`get_http_headers` lowercases names and returns `{}` without an HTTP request — never
     raises). Empty → the transport's missing message; then the lookup is CASE-INSENSITIVE (Horizon's
     docs do not promise the email's case): `identifier = identifier.strip().lower()` and
     `User.find_one({"identifier": {"$regex": f"^{re.escape(identifier)}$", "$options": "i"}})` — an
     anchored, escaped regex (stdlib only; the `users` collection is tiny, so the index miss is free).
     `None` → the unknown message. No migration: an existing row stored with uppercase (e.g.
     `Paul@Example.com`) still matches. To keep the match unambiguous going forward, `scripts/signup.py`
     stores NEW identifiers stripped + lowercased and refuses one that collides case-insensitively with an
     existing row ("identifier 'X' already exists as 'x'"); existing rows are left as they are. One indexed
     read per request; no cache (upgrade trigger: a measured cost).
2. **`server.py`:** delete `_SERVER_USER_ID`, `set_server_user_id`, `get_server_user_id`,
   `_resolve_server_user_id`, `_resolve_user_id_from_env` and the module docstring's "Phase 1" paragraph
   (replace with two sentences pointing at `request_user.py`); the lifespan yields `{client, database, llm,
   embedding_model, thread_id}` — no `user_id`; its "ready" log line drops `user_id`;
   `ensure_indexes(client, database, embedding_model=…)` is called without a user.
3. **`tree/memory/rag/indexing.py`:** `ensure_indexes(..., user_id: PydanticObjectId | None = None)`; the
   log line says `triggered by tenant user_id=%s` when given, `triggered by server boot` when not (it is the
   only use — "indexes themselves are global to the collection"); `pipeline.py:2274` keeps passing it.
4. **Call sites (every `lc["user_id"]` / `lc.get("user_id")` read goes):** `tools.py` — `search_memory`,
   `visualize_memory_structure`, `visualize_memory_embeddings`, `ingest_url`, `ingest_file`,
   `ingest_conversation`, `search_web` (the user is resolved for EVERY call, with or without `ingest`),
   `scrape_web` (resolves the user too — "every tool", a misconfigured client fails on its first call);
   `graph_tools.py` — `visualize_memory_structure`, `query_memory`, `search_memory`, `deep_search_memory`,
   `review_list_pending`, `review_confirm`, `review_reject`; `dashboard_app.py` — `memory_dashboard`
   (gains envelope handling for this error; it returns `ToolResult | str` already). Each tool resolves the
   user where it first read `lc["user_id"]` today (after its cheap input guards, before tracing / retrieval),
   and a `RequestUserError` answers `tool_error("configuration_error", str(exc), retryable=False)` — the
   SWE picks the shape (a 4-line try/except per tool or one helper); the behaviour is the contract.
   `_set_retrieval_thread(ctx, tool, *, user_id)` takes the resolved id for its Opik metadata.
   `_graph_tool_result` stays synchronous and user-free in this task (187 adds `user_id`).
5. **Entrypoint + Make + cloud client:** `scripts/serve_mcp.py` loses `--user-id`, `--identifier`,
   `_resolve_and_pin`, the Mongo pre-connect and the "Phase 1" docstring — it is `init_logger()` +
   `main(transport)` → `mcp.run(...)`; its docstring states the per-transport rule in two lines.
   `apps/memory/Makefile` `serve-mcp` target ONLY: drop the `USER_ID`/`TREE_USER_IDENTIFIER` guard and
   `--user-id`; comment: "stdio reads TREE_USER_IDENTIFIER from .env (exported by this Makefile); over
   HTTP the gateway (Horizon) or the client (local) sends horizon-actor-email". The pipeline / visualize
   scripts' `USER_FLAGS` (`--user-id` / `--user-identifier`, Makefile line 164) are a different thing —
   the operator's per-run tenant — and stay. `tree/mcp/client.py` `get_cloud_client()` stays
   `Client(settings.tree_memory_cloud_url, auth="oauth")` and sends NO identity header: it targets Horizon,
   where the gateway injects (and would strip) it; its docstring says so and names the local-HTTP recipe
   (`fastmcp.Client(StreamableHttpTransport(url, headers={"horizon-actor-email": …}))`) for anyone
   pointing a client at `make memory-serve-mcp TRANSPORT=streamable-http`.
6. **Client config:** `.mcp.json` and `.pi/mcp.json` are UNCHANGED — the HTTP entries get their identity
   from the gateway, the stdio entries from `--env-file ../../.env`. The session-end hook
   (`tree.mcp.hooks`) spawns the stdio entry unchanged — no hook change.
7. **Tests** (`/squid-testing-python`): new `tests/unit/mcp/test_request_user.py` — stdio + env → id;
   stdio with a `horizon-actor-email` header present and env empty → `MISSING_ENV_MESSAGE` (the header
   is ignored); HTTP (`transport="streamable-http"`) + header → id; HTTP with env set and no header →
   `MISSING_HEADER_MESSAGE` (env ignored); whitespace-only header/env → missing; `transport=None` → the
   header path; header name case-insensitive (`Horizon-Actor-Email` arrives lowercased); the VALUE is
   matched case-insensitively (`Paul@Example.com` in the header finds the row `paul@example.com`, and a row
   stored as `Paul@Example.com` is found by `paul@example.com`; regex metacharacters in an identifier are
   escaped — `a+b@x.com` does not match `aab@x.com`); unknown identifier → message names the identifier,
   `make memory-signup` and the Horizon-email rule; `find_one` is awaited with the anchored `$regex`
   query. `tests/unit/scripts/test_signup.py` (extend): signup stores ` Paul@Example.com ` as
   `paul@example.com`; a second signup `PAUL@example.com` is refused naming the existing row; `whoami` /
   `set-current` still find a pre-existing uppercase row. `test_server_startup.py` — keep the path-load test; delete the pinning
   classes; assert `tree.mcp.server` has none of the five removed names and the lifespan context has no
   `user_id` key. `test_tools_user_id_pinning.py` → rename `test_tools_request_user.py`: for every tool the
   underlying call receives the RESOLVED id (seam patched to return a uid), and, parametrised over all 14
   tools (8 + 6 graphrag), a seam that raises answers an envelope `{"error_type": "configuration_error",
   "retryable": false, "message": <verbatim>}`. Every `_make_ctx` in `test_tools.py` / `test_graph_tools.py`
   / `test_viz_app.py` / `test_dashboard_app.py` / `test_search_web.py` drops `"user_id"` from
   `lifespan_context` (a `MagicMock().transport` is not `"stdio"`, so an unpatched test would hit the
   header path and fail — one conftest fixture that patches the seam keeps the suite dry; importing the
   module and calling `request_user.resolve_request_user(ctx)` at the call sites lets one patch cover
   `tools`, `graph_tools` and `dashboard_app`). `ensure_indexes` test: callable without `user_id`.
   `tests/unit/mcp/test_client.py` (new): `get_cloud_client()`'s transport carries NO `horizon-*` header
   and `auth == "oauth"`. Source guards (the "save its text" pattern): no `["user_id"]` / `.get("user_id")`
   read of `lifespan_context` under `src/tree/mcp`; `serve_mcp.py` contains neither `--user-id` nor
   `--identifier`; the Makefile's `serve-mcp` recipe contains neither `USER_ID` nor `--user-id`;
   `X-Tree-User` appears nowhere in the repo (`src`, `tests`, `.mcp.json`, `.pi/mcp.json`, docs).
8. **Live verification (LOCAL env):** (a) HTTP: `make memory-serve-mcp TRANSPORT=streamable-http` (the
   Makefile exports `.env`, so `TREE_USER_IDENTIFIER` IS set server-side — the test that it is ignored);
   a `fastmcp.Client(StreamableHttpTransport("http://127.0.0.1:8000/mcp", headers=
   {"horizon-actor-email": "<identifier>"}))` call to `search_memory` returns results; the same without
   the header → `configuration_error` whose message names `horizon-actor-email`, Horizon authentication
   and the local client recipe; with `horizon-actor-email: nobody@example.com` → the message names
   `nobody@example.com`, `make memory-signup` and the Horizon-email rule; `scrape_web` without the header
   → the same envelope. (b) stdio: from Claude Code (`tree-memory-local`) `search_memory` works; a scratch
   copy of the entry with `TREE_USER_IDENTIFIER=` empty → `configuration_error` naming
   `TREE_USER_IDENTIFIER` and the `env` block. Evidence in the Log.

## Acceptance criteria

- [ ] `tree.mcp.request_user.resolve_request_user(ctx)` returns the `User._id` for `transport == "stdio"`
      + env, and for any other transport + `horizon-actor-email`; it raises `RequestUserError` with
      `MISSING_ENV_MESSAGE` / `MISSING_HEADER_MESSAGE` / the unknown-identifier message otherwise; the
      header is ignored on stdio and the env on HTTP (tests for both cross cases).
- [ ] `MISSING_HEADER_MESSAGE` names Horizon authentication (Access mode not "Disabled"), the
      actor-needs-an-email rule and the local client recipe; the unknown message names the identifier,
      `make memory-signup` and that the identifier must equal the Horizon account email.
- [ ] The identifier → `User` lookup is case-insensitive on both transports (anchored, escaped `$regex`
      with `i`); existing uppercase rows need no migration; `scripts/signup.py` stores new identifiers
      stripped + lowercased and refuses a case-insensitive collision (tests).
- [ ] The five pinning names are gone from `tree.mcp.server`; the lifespan context carries no `user_id`;
      `ensure_indexes` accepts no user (`user_id: … | None = None`) and `pipeline.py` still passes one.
- [ ] All 14 tools pass the resolved id to their business-logic call, and every one answers
      `{"error_type": "configuration_error", "retryable": false, "message": …}` when the seam raises
      (parametrised test); `_set_retrieval_thread` tags Opik with the resolved id.
- [ ] `scripts/serve_mcp.py` has only `--transport`; `make memory-serve-mcp` runs without `USER_ID`; the
      `serve-mcp` target no longer mentions `USER_ID` / `--user-id` (the pipeline scripts' `USER_FLAGS`
      keep theirs).
- [ ] `get_cloud_client()` sends no `horizon-*` header and keeps `auth="oauth"` (test); its docstring
      gives the local-HTTP header recipe.
- [ ] `.mcp.json` and `.pi/mcp.json` are byte-identical to before this task; `X-Tree-User` appears nowhere
      in the repo.
- [ ] Source guard: no lifespan `user_id` read under `src/tree/mcp`.
- [ ] Live (local, HTTP with `TREE_USER_IDENTIFIER` set server-side): header present → results; missing →
      the header message; unknown → the signup message; `scrape_web` included. Live (stdio): env present →
      results; empty → the env message. Evidence in the Log.
- [ ] `make memory-format-check`, `make memory-lint-check`, `make pre-commit`, `make memory-tests` green.

## User Stories

### Story: Two people share one Horizon deployment and see their own memories
1. Alice and Bob are members of the Horizon org; each signed up with `make memory-signup
   USER_IDENTIFIER=<their Horizon account email>`.
2. Alice asks Claude Code "what do I have on Modal cold starts?"; the gateway attaches
   `horizon-actor-email: alice@example.com`; `search_memory` answers from Alice's rows only.
3. Bob asks the same; his request carries `bob@example.com` — his rows only. Neither client config names a
   user, and the server never pinned one.

### Story: A forged header does not work on Horizon
1. A client adds its own `horizon-actor-email: alice@example.com` to a request authenticated as Bob.
2. The gateway strips the client header and injects `bob@example.com`; the tool answers from Bob's memory.

### Story: The server is reached without Horizon authentication
1. A deployment's Access mode is set to "Disabled" (or a service account without an email calls it).
2. Every tool answers `{"error_type": "configuration_error", "retryable": false, "message": "No
   horizon-actor-email header on this request. On Prefect Horizon the gateway adds it when Horizon
   authentication is enabled … (a service account may have none) …"}`; the operator re-enables auth.

### Story: A Horizon user who never signed up
1. `carol@example.com` (an org member) calls `ingest_url`.
2. The answer is `configuration_error` with `No user with identifier 'carol@example.com' — run \`make
   memory-signup USER_IDENTIFIER=carol@example.com\` … must equal your Horizon account email.`; nothing was
   ingested.

### Story: The local stdio server keeps working from `.env`
1. The developer runs `make memory-serve-mcp` (no `USER_ID`), or Claude Code spawns `tree-memory-local`.
2. `.env`'s `TREE_USER_IDENTIFIER` reaches the process env through `--env-file`; `search_memory` works as
   before; the session-end hook still persists sessions.
3. With `TREE_USER_IDENTIFIER=` empty the tool answers `configuration_error` naming `TREE_USER_IDENTIFIER`
   and the client `env` block — never a boot failure, never a default user.

### Story: The developer tests a local HTTP server
1. `make memory-serve-mcp TRANSPORT=streamable-http` has `TREE_USER_IDENTIFIER` exported (ignored on HTTP).
2. A `fastmcp.Client` without the header gets the missing-header message — the env is not a fallback; the
   same client with `headers={"horizon-actor-email": "<identifier>"}` gets results.

---

Blocked by: (none)

## Log

### [PA] 2026-10-05 16:40 — Grooming (rev 2, 2026-10-05: `horizon-actor-email` replaces `X-Tree-User`)

**Summary**
One seam, `resolve_request_user(ctx)`, resolves the user on every call from the single source its transport
allows — Horizon's verified `horizon-actor-email` over HTTP, `TREE_USER_IDENTIFIER` over stdio; the
boot-pinned user, its two CLI flags and the lifespan `user_id` go away; every tool answers the existing
`configuration_error` envelope when the identifier is missing or unknown.

**Key decisions**
- `ctx.transport == "stdio"` → env; everything else (incl. `None`) → header. Deterministic, no fallback.
- The header is Horizon's own verified actor context (stripped from clients, injected after auth) — no
  client config carries an identity; locally the client sets it on its own machine (a claim, ADR-014 §3).
- `get_cloud_client()` sends no header: against Horizon the gateway owns it.
- `ensure_indexes` keeps its signature with `user_id` optional — its only use is a log line.
- "Every tool" is literal: `scrape_web` and `search_web` resolve the user too (plan open question 1).
- A Horizon user's `User.identifier` is their Horizon account email (plan open question 3), matched
  case-insensitively: Horizon's docs do not promise the email's case, and an anchored `$regex` with `i`
  needs no migration of existing rows; signup lowercases new rows so the match stays unambiguous.

**Dependencies**
- None.

**User stories**
- 6 stories: two users on one deployment, forged header, auth disabled / no email, unknown Horizon user,
  local stdio from `.env`, local HTTP with a client-set header.

Ready for implementation.
