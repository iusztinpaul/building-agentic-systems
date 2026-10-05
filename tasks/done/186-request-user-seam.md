---
id: 186-request-user-seam
status: done
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

- [x] `tree.mcp.request_user.resolve_request_user(ctx)` returns the `User._id` for `transport == "stdio"`
      + env, and for any other transport + `horizon-actor-email`; it raises `RequestUserError` with
      `MISSING_ENV_MESSAGE` / `MISSING_HEADER_MESSAGE` / the unknown-identifier message otherwise; the
      header is ignored on stdio and the env on HTTP (tests for both cross cases).
- [x] `MISSING_HEADER_MESSAGE` names Horizon authentication (Access mode not "Disabled"), the
      actor-needs-an-email rule and the local client recipe; the unknown message names the identifier,
      `make memory-signup` and that the identifier must equal the Horizon account email.
- [x] The identifier → `User` lookup is case-insensitive on both transports (anchored, escaped `$regex`
      with `i`); existing uppercase rows need no migration; `scripts/signup.py` stores new identifiers
      stripped + lowercased and refuses a case-insensitive collision (tests).
- [x] The five pinning names are gone from `tree.mcp.server`; the lifespan context carries no `user_id`;
      `ensure_indexes` accepts no user (`user_id: … | None = None`) and `pipeline.py` still passes one.
- [x] All 14 tools pass the resolved id to their business-logic call, and every one answers
      `{"error_type": "configuration_error", "retryable": false, "message": …}` when the seam raises
      (parametrised test); `_set_retrieval_thread` tags Opik with the resolved id.
- [x] `scripts/serve_mcp.py` has only `--transport`; `make memory-serve-mcp` runs without `USER_ID`; the
      `serve-mcp` target no longer mentions `USER_ID` / `--user-id` (the pipeline scripts' `USER_FLAGS`
      keep theirs).
- [x] `get_cloud_client()` sends no `horizon-*` header and keeps `auth="oauth"` (test); its docstring
      gives the local-HTTP header recipe.
- [x] `.mcp.json` and `.pi/mcp.json` are byte-identical to before this task; `X-Tree-User` appears nowhere
      in the repo.
- [x] Source guard: no lifespan `user_id` read under `src/tree/mcp`.
- [x] Live (local, HTTP with `TREE_USER_IDENTIFIER` set server-side): header present → results; missing →
      the header message; unknown → the signup message; `scrape_web` included. Live (stdio): env present →
      results; empty → the env message. Evidence in the Log.
- [x] `make memory-format-check`, `make memory-lint-check`, `make pre-commit`, `make memory-tests` green.

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

### [SWE] 2026-10-05 20:38 — Implementation

**Files modified**
- `apps/memory/src/tree/mcp/request_user.py` (new) — the seam: `ACTOR_EMAIL_HEADER`, `USER_ENV_VAR`, the three message anchors, `RequestUserError`, `resolve_request_user(ctx)` (`ctx.transport == "stdio"` → env only; anything else incl. `None` → `get_http_headers()["horizon-actor-email"]` only). Imports `os`, `beanie`, `fastmcp`, `tree.entities.users` only.
- `apps/memory/src/tree/entities/users.py` — `normalize_identifier()` (strip + lower) and `find_user_by_identifier()` (anchored, `re.escape`d `$regex` with `i`); shared by the seam and `scripts/signup.py` so the query exists once.
- `apps/memory/src/tree/mcp/server.py` — the five pinning names, the "Phase 1" docstring paragraph and the `os`/`PydanticObjectId`/`User` imports are gone; lifespan yields `{client, database, llm, embedding_model, thread_id}`; "ready" log drops `user_id`; `ensure_indexes` called without a user.
- `apps/memory/src/tree/mcp/tools.py` — `REQUEST_USER_ERRORS` + `request_user_error(tool, exc)`; all 8 tools resolve via `request_user.resolve_request_user(ctx)` after their input guards (search_web/scrape_web: before the SERP call / scrape fan-out); `_set_retrieval_thread(ctx, tool, *, user_id)`.
- `apps/memory/src/tree/mcp/graph_tools.py` — the 7 graph tools resolve the same way.
- `apps/memory/src/tree/mcp/dashboard_app.py` — `memory_dashboard` resolves + answers the envelope; `_fetch_payload(..., *, user_id)`.
- `apps/memory/src/tree/mcp/client.py` — docstring: no identity header (gateway owns it) + the local-HTTP `StreamableHttpTransport(headers=…)` recipe. Code unchanged.
- `apps/memory/src/tree/memory/rag/indexing.py` — `ensure_indexes(..., user_id: PydanticObjectId | None = None)`; log says `triggered by tenant user_id=…` / `triggered by server boot`. `pipeline.py` unchanged (still passes it).
- `apps/memory/scripts/serve_mcp.py` — `init_logger()` + `main(--transport)` → `mcp.run(...)`; no `--user-id`/`--identifier`/`_resolve_and_pin`/Mongo pre-connect.
- `apps/memory/scripts/signup.py` — new identifiers stored stripped + lowercased; a case-only variant of an existing row is refused (`identifier 'X' already exists as 'x'.`); `set-current --user-identifier` uses the case-insensitive lookup.
- `apps/memory/Makefile` — `serve-mcp` target only: no `USER_ID` guard / `--user-id`; the requested comment sits above the target. `USER_FLAGS` untouched.
- Tests: `tests/unit/mcp/conftest.py` (new: `request_user_id` fixture + autouse seam patch), `tests/unit/mcp/test_request_user.py` (new, 19), `tests/unit/mcp/test_tools_request_user.py` (renamed from `test_tools_user_id_pinning.py`, rewritten, 114), `tests/unit/mcp/test_client.py` (new, 2), `tests/unit/scripts/test_serve_mcp.py` (new, 7), `tests/unit/scripts/test_signup.py` (new, 5), `tests/unit/mcp/test_server_startup.py` (pinning classes deleted, 8 new), `tests/unit/memory/rag/test_indexing.py` (+2); `_make_ctx` in `test_tools.py`, `test_graph_tools.py`, `test_viz_app.py`, `test_dashboard_app.py`, `test_search_web.py`, `test_error_envelope.py` drop `"user_id"` (the three that key fakes on a specific id override `request_user_id`).

**Tests**
- Unit: 5062 passing, 0 failing — `make memory-tests` (local env)
- Integration: N/A — no integration suite in this repo (AGENTS.md)

**Acceptance criteria**
- [x] Seam returns `User._id` for stdio+env and any-other-transport+header; raises with the three messages; cross cases — `test_request_user.py::test_stdio_ignores_the_header`, `::test_http_ignores_the_env_identifier`, `::test_no_transport_without_a_request_takes_the_header_path`, `::test_http_resolves_the_actor_email_header[streamable-http|sse|None]`
- [x] Message content — `test_request_user.py::test_missing_header_message_is_verbatim`, `::test_missing_env_message_is_verbatim`, `::test_unknown_identifier_message_is_verbatim`, `::test_an_unknown_identifier_names_signup_and_the_horizon_rule`
- [x] Case-insensitive lookup, no migration, escaping, anchoring; signup lowercases + refuses — `test_request_user.py::test_a_mixed_case_header_finds_a_lowercase_row`, `::test_a_lowercase_identifier_finds_a_legacy_uppercase_row`, `::test_regex_metacharacters_are_escaped`, `::test_the_match_is_anchored`, `::test_find_one_is_awaited_with_the_anchored_case_insensitive_regex`; `tests/unit/scripts/test_signup.py` (5)
- [x] Pinning names gone, no lifespan `user_id`, `ensure_indexes` optional user — `test_server_startup.py::test_server_module_no_longer_pins_a_user[*]`, `::test_lifespan_context_carries_no_user`, `::test_lifespan_ensures_indexes_without_a_user`; `test_indexing.py::TestEnsureIndexes::test_runs_without_a_user_and_logs_the_server_boot`
- [x] All tools pass the resolved id + envelope on seam failure; Opik tag — `test_tools_request_user.py::test_the_resolved_user_reaches_the_business_call[*]` (15), `::test_every_tool_resolves_the_request_user[*]` (16), `::test_a_failing_seam_answers_the_envelope_and_runs_nothing[*]` (16 tools × 4 failures), `::test_opik_threads_are_tagged_with_the_resolved_user[*]`
- [x] `serve_mcp.py` only `--transport`; make target needs no `USER_ID`; `USER_FLAGS` kept — `tests/unit/scripts/test_serve_mcp.py` (7) + live `make memory-serve-mcp` below
- [x] `get_cloud_client()` no `horizon-*` header, `auth="oauth"`, docstring recipe — `tests/unit/mcp/test_client.py` (2)
- [x] `.mcp.json` / `.pi/mcp.json` byte-identical (md5 `6e7b4001…` / `c31b0ec9…`, untouched in `git status`); `X-Tree-User` nowhere — `test_tools_request_user.py::test_no_client_side_identity_header_name_exists_anywhere` (scans `apps/memory/src`, `apps/memory/tests`, `docs/`, both mcp.json)
- [x] Source guard — `test_tools_request_user.py::test_no_tool_reads_a_user_from_the_lifespan_context[*]`
- [x] Live (HTTP + stdio) — evidence below
- [x] format-check, lint-check, pre-commit, memory-tests green

**Evidence**
```
$ make env-status
Env target: local (.env)

$ make memory-format-check && make memory-lint-check
All checks passed!
$ make pre-commit
prettier....Passed  ruff check....Passed  ruff format....Passed  biome check (harness)....Passed
$ make memory-tests
======================= 5062 passed in 61.68s (0:01:01) ========================

$ git grep -c init_mongodb -- apps/memory/scripts/serve_mcp.py apps/memory/src/tree/mcp/server.py
apps/memory/src/tree/mcp/server.py:2        # the import + the ONE lifespan call; serve_mcp.py: 0

# (a) HTTP — .env exports TREE_USER_IDENTIFIER=paul.iusztin@example.com server-side (ignored on HTTP).
# Port 8000 is held by an unrelated docker container (kitaru-local-server-1), so FASTMCP_PORT=8765.
$ FASTMCP_PORT=8765 make memory-serve-mcp TRANSPORT=streamable-http
Ensuring indexes on memory (triggered by server boot; indexes themselves are global to the collection)
MCP server ready (database=tree, thread_id=mcp-session-f328ee5a-…)
$ uv run python scratchpad/live_http.py http://127.0.0.1:8765/mcp paul.iusztin@example.com
tools: ['ingest_conversation', 'ingest_file', 'ingest_url', 'scrape_web', 'search_memory', 'search_web', 'visualize_memory_embeddings', 'visualize_memory_structure']
--- header present: search_memory headers={'horizon-actor-email': 'paul.iusztin@example.com'}
outcome=found search_mode=hybrid parents=3 first_doc=Stop Converting Documents to Text. You're Doing It Wrong.
--- header present, mixed case: search_memory headers={'Horizon-Actor-Email': 'PAUL.IUSZTIN@EXAMPLE.COM'}
outcome=found search_mode=hybrid parents=3 first_doc=Stop Converting Documents to Text. You're Doing It Wrong.
--- no header: search_memory headers={}
{"error_type": "configuration_error", "retryable": false, "message": "No horizon-actor-email header on this request. On Prefect Horizon the gateway adds it when Horizon authentication is enabled for this server (Access mode must not be \"Disabled\") and the caller is a user with an email (a service account may have none). Against a local HTTP server there is no gateway: set the header on your client, e.g. fastmcp.Client(StreamableHttpTransport(url, headers={\"horizon-actor-email\": \"<your signed-up identifier>\"}))."}
--- unknown user: search_memory headers={'horizon-actor-email': 'nobody@example.com'}
{"error_type": "configuration_error", "retryable": false, "message": "No user with identifier 'nobody@example.com' — run `make memory-signup USER_IDENTIFIER=nobody@example.com` (scripts/signup.py) first; on Horizon the signed-up identifier must equal your Horizon account email."}
--- no header: scrape_web headers={}
{"error_type": "configuration_error", "retryable": false, "message": "No horizon-actor-email header on this request. …"}   (same text as above)
--- header present: scrape_web → {"requested": 1, "succeeded": 1, "failed": 0, …}

# (b) stdio — the tree-memory-local entry's exact command/args/env, spawned via fastmcp StdioTransport
$ uv run python scratchpad/live_stdio.py <repo>
--- tree-memory-local as shipped: env={'ENV_FILE_PATH': '../../.env'}
outcome=found search_mode=hybrid parents=3
--- scratch copy, TREE_USER_IDENTIFIER empty: env={'ENV_FILE_PATH': '../../.env', 'TREE_USER_IDENTIFIER': ''}
{"error_type": "configuration_error", "retryable": false, "message": "TREE_USER_IDENTIFIER is empty in this stdio server's environment. Set it in the \"env\" block of the tree-memory-local entry (.mcp.json), or in .env, which that entry loads with --env-file."}

$ make memory-serve-mcp < /dev/null          # stdio, no USER_ID
uv run python scripts/serve_mcp.py
MCP server ready (database=tree, thread_id=mcp-session-e066a0f7-…)
```

**Notes**
- Beyond the spec (deliberate, small): a `PyMongoError` from the per-request `users` read answers `storage_unavailable` (retryable) via the existing `storage_error` — without it a Mongo outage at the lookup would escape as a protocol error (ADR-008 §2). Swept in the parametrised failure test.
- Seam shape: a 4-line `try: user_id = await request_user.resolve_request_user(ctx) / except REQUEST_USER_ERRORS as exc: return request_user_error(tool, exc)` per tool. Called through the module so one patch covers `tools`, `graph_tools`, `dashboard_app`.
- Signup collision rule: compare the stored identifier to the STRIPPED (not lowercased) input — equal → idempotent (keeps re-signing a legacy `Paul@Example.com` row idempotent); different → refused. Comparing against the lowercased input would make `PAUL@example.com` idempotent and contradict the spec's own test.
- The unknown-identifier message names the NORMALIZED (lowercased) identifier — the form `signup` would store.
- "14 tools" in the spec = a graphrag server's registered set; the tests parametrise over all 16 tool functions (8 + 7 + dashboard).
- Live checks ran in rag mode (the local config); the 7 graph tools + dashboard are covered by unit tests only. The new `dashboard_app → tree.mcp.tools` import edge was checked in graphrag: `TREE_MEMORY__MODE=graphrag uv run pytest tests/unit/mcp` → 660 passed, and loading `server.py` BY PATH (Horizon's way) registers 14 tools.
- For task 189: `.agents/skills/run-pipelines-e2e/SKILL.md:59`'s `fastmcp call http://127.0.0.1:8000/mcp --auth none <tool>` recipe now answers `configuration_error` on every call (no header) — 189 must give it the `horizon-actor-email` header recipe.
- The stdio check from inside a Claude Code session itself is NOT RUN (a subagent cannot drive Claude Code); `live_stdio.py` spawns the same `uv --directory apps/memory run --env-file ../../.env python scripts/serve_mcp.py` command with the entry's `env` block, which is what Claude Code does.
- Follow-up candidate: `visualize_memory_embeddings` and `memory_dashboard` now answer an envelope (from the seam only) but neither states `ERROR_CONTRACT` nor has a catch-all; `test_error_envelope.py`'s comment now says so. Widening the sweep would be its own task.

### [Tester] 2026-10-05 20:50 — QA

**Test summary**
- Format / lint / pre-commit: PASS (341 files formatted; ruff, prettier, biome pass)
- Unit tests: 5062 passed / 0 failed (`make memory-tests`, `make env-status` = local); graphrag subset `TREE_MEMORY__MODE=graphrag pytest tests/unit/mcp tests/unit/scripts` 892 passed
- Integration tests: N/A (none in this repo, AGENTS.md)
- Warnings: 0 new

**E2E adversarial pass** (own server `FASTMCP_PORT=8766 make memory-serve-mcp TRANSPORT=streamable-http`, `.env` exports TREE_USER_IDENTIFIER server-side; own scripts `qa_http.py`, `qa_http2.py`, `qa_stdio.py` in scratchpad; temp users inserted in local Mongo and removed afterwards)
- Happy path: header `paul.iusztin@example.com` → `search_memory` outcome=found parents=3; `Horizon-Actor-Email` header-name case → found (PASS)
- Env ignored on HTTP: no header, server env set → `configuration_error` MISSING_HEADER message, for search_memory, search_web, scrape_web, ingest_url, ingest_conversation, ingest_file, visualize_memory_structure, visualize_memory_embeddings (PASS)
- Regex injection: headers `.*`, `paul.*`, `a|b` alternation, `pa.l.iusztin@…`, `{"$ne": null}` → all "No user with identifier" (PASS); `qa.a+b@example.com` finds its own row, `qa.aab@example.com` → unknown (escaping works) (PASS)
- Case / whitespace: legacy row stored `QA.Legacy@Example.com` found by `qa.legacy@…` and `QA.LEGACY@example.COM` (PASS); empty header → missing-header message (PASS); whitespace/padded/non-ASCII header values are rejected by the HTTP client library before reaching the server (not reachable) — padded/upper env on stdio `"  PAUL.IUSZTIN@example.com "` → found (PASS)
- Isolation: user `qa.bob@example.com` (no data) searching the same query → `nothing_found` parents=0, while Paul → found 3; 20 concurrent requests alternating Paul/Bob → 0 mismatches; Paul's `visualize_memory_structure` / `_embeddings` return his 14 docs (PASS)
- Foreign headers: `X-Tree-User` and `horizon-actor-id` carrying Paul's email → missing-header error (not honored) (PASS)
- Unknown (carol) on 7 tools → unknown-identifier envelope, nothing ingested (PASS)
- stdio (tree-memory-local command via StdioTransport): padded/upper env → found; bob → nothing_found; `.*` → unknown envelope; whitespace env → MISSING_ENV message; legacy-upper row via lower env → resolves; scrape_web follows the same gate (PASS)
- 8k-character header → unknown envelope, no crash (PASS, see note)
- signup: `QA.BOB@example.com` refused ("already exists as 'qa.bob@example.com'"); `"  QA.New@Example.COM  "` stored `qa.new@example.com`; exact legacy re-run idempotent; `qa.legacy@…` refused naming `QA.Legacy@Example.com`; `set-current --user-identifier PAUL.IUSZTIN@example.com` finds the row (PASS)
- `serve_mcp.py --user-id x` → "No such option: --user-id"; `make memory-serve-mcp` runs without USER_ID (PASS)

**Acceptance criteria** — all 11 verified [x]
- [x] Seam both transports + cross cases — live above + `test_request_user.py`
- [x] Message content — live output matches the three anchors verbatim
- [x] Case-insensitive anchored escaped lookup, no migration, signup lowercases/refuses — live above + `test_request_user.py`, `test_signup.py`
- [x] Pinning names gone, lifespan has no user_id, `ensure_indexes` optional user — `test_server_startup.py`, `test_indexing.py`; `grep` of the repo (excl. tasks, tests) finds none of the removed names
- [x] All tools pass resolved id / envelope on seam failure — `test_tools_request_user.py`; live envelope seen on 8 rag tools
- [x] serve_mcp `--transport` only; Makefile target without USER_ID; USER_FLAGS untouched
- [x] `get_cloud_client()` — `test_client.py`
- [x] `.mcp.json` / `.pi/mcp.json` unchanged (`git diff` empty); `X-Tree-User` absent outside tasks/ and the guard test
- [x] Source guard — `test_no_tool_reads_a_user_from_the_lifespan_context`
- [x] Live HTTP + stdio — independently re-run
- [x] format-check, lint-check, pre-commit, memory-tests green

**Other issues found (non-blocking)**
- The unknown-identifier message echoes the full submitted identifier (8k chars in my test) and puts it in a `make memory-signup USER_IDENTIFIER=…` command unquoted; harmless over JSON, only cosmetic.
- `visualize_memory_embeddings` / `memory_dashboard` carry no `ERROR_CONTRACT` / catch-all (SWE already flagged as a follow-up).
- Judgement on SWE's PyMongoError → `storage_unavailable` (retryable): sound, consistent with ADR-008 §2; accepted.
- Graph-mode tools + dashboard not live-tested (rag mode locally); covered by unit tests incl. graphrag-mode run.
- Task 189 must update `.agents/skills/run-pipelines-e2e/SKILL.md` `fastmcp call … --auth none` recipe (now needs the header), as the SWE noted.

**VERDICT: PASS**

### [PA] 2026-10-05 22:10 — Acceptance Review

**VERDICT: ACCEPT**

Feature-level review of `request-scoped-users` (PR #46, tasks 185–189, ADR-014, glossary). Reviewed evidence from the Tester log entry; all machine-verifiable acceptance criteria verified from the user's POV. Reviewed the seam from the user's POV: one identifier source per transport (header over HTTP, env over stdio), everything after the identifier is read is shared, and each of the three failure messages names its fix. Hand off to the PR Reviewer.
