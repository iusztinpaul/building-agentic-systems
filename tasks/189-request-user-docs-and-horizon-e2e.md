---
id: 189-request-user-docs-and-horizon-e2e
status: pending
feature: request-scoped-users
---

# Docs and client guidance for the **Request user** and the Mongo-backed **Graph download** (runbook, tutorial, skill, README, `.env.example`), ADR Status-line notes, and the live Horizon checks (verified actor email, forged header, API key, cross-instance download, inline render, `client_supports_extension` ×5)

Tags: `docs`, `adr`, `horizon`, `mcp`
Depends on: 186, 187, 188
Blocks: —
Implements: ADR-014 §6 (the e2e record) and its "Amendments to apply"

## Problem

The runbook and tutorial tell operators to set `TREE_USER_IDENTIFIER` on Horizon ("boot fails loudly
otherwise"); nothing tells them that Horizon authentication must stay enabled, that giving someone access
is an org invite plus a sign-up with their Horizon email, or that a service-account key carries no email;
the skill and tutorial tell clients to share a server path; ADR-005 / ADR-013 Status lines predate ADR-014.
And four facts the plan could not verify locally need a live Horizon pass: the exact `horizon-actor-email`
value our account receives, that a forged client header loses to the gateway's, that a personal API key
resolves to the same user, and that a `graphs://` read works across instances.

## Scope

**Docs (no code):**
1. `docs/notes/deployment-runbook.md`: step 2 bullet (line 36) → "Every MCP request names its user. On
   Horizon the gateway attaches the authenticated actor's email (`horizon-actor-email`) and the server
   looks it up as `User.identifier` (case-insensitively), so **giving someone access = invite them to the
   Horizon org + `make memory-signup USER_IDENTIFIER=<their Horizon account email>`**. An email with no
   `User` row answers `configuration_error` naming `make memory-signup`; a service-account key carries no
   email and answers `configuration_error`. The server no longer pins a user at boot."; step 4: the env
   list (line 75) DROPS `TREE_USER_IDENTIFIER` and says so ("not `TREE_USER_IDENTIFIER` — ignored on
   HTTP"); `TREE_WORKING_DIR`'s reason → deep-search session files (graph downloads live in Mongo); add the
   optional `TREE_MCP__GRAPH_FILE_TTL_SECONDS` (default 300); the Auth paragraph → "**Horizon
   Authentication must stay enabled (Access mode never "Disabled")**: with it disabled the gateway neither
   verifies nor strips `horizon-*` headers, so anyone could name any user (ADR-014 §3). Users authenticate
   with Horizon OAuth (`/mcp` in Claude Code, `auth="oauth"` in Python) or a personal API key
   (`Authorization: Bearer fmcp_…`) — both resolve to the same actor email."
2. `tutorials/4_x_x_deploying_mcp_server.md`: line 10 (step 2 — also fix its `make memory-signup
   IDENTIFIER=` to the target's real `USER_IDENTIFIER=`, and say the identifier is the Horizon account
   email) and the env table row (line 20) as above; "Auth and Clients" gains the auth-must-stay-enabled
   sentence and the invite + signup rule; "Downloading visualizations" bullet rewritten — no server path;
   the link is a Mongo-backed download that expires after about 5 minutes (`mcp.graph_file_ttl_seconds`),
   readable from any instance; keep the decode one-liner and the cap sentence; replace the local
   measurement with the Horizon numbers from check (d) below.
3. `.agents/skills/tree-memory/SKILL.md` line 68 → "A visual answer carrying a `graphs://…html.gz` link
   could not render inline: on `tree-memory-local` share the local path it also gives; on `tree-memory`
   (cloud) read the linked blob within its expiry (about 5 minutes), base64-decode + gunzip it to a local
   `.html` (`base64 -d < blob.b64 | gunzip > map.html`) and open it; an expired link → re-run the visual
   tool once." Errors section: one line — "`configuration_error` naming `horizon-actor-email` or
   `TREE_USER_IDENTIFIER` means the request carries no user (Horizon auth disabled, a service-account key,
   an unsigned-up email, or an empty local env); relay the message, do not retry."
4. `apps/memory/README.md` "MCP server" section (lines 424–430): `make memory-serve-mcp` needs no
   `USER_ID`; one sentence on the per-transport rule (stdio reads `TREE_USER_IDENTIFIER` from `.env`; over
   HTTP the identity is Horizon's `horizon-actor-email` — set it on the client only against a local
   `TRANSPORT=streamable-http` server). `.env.example` line 1 comment: "the signed-up user for the LOCAL
   stdio MCP server (read from the process env); on Horizon the gateway's `horizon-actor-email` is the
   identity and this variable is ignored".
   `.agents/skills/run-pipelines-e2e/SKILL.md` line 59: `uv run fastmcp call http://… --auth none` has NO
   header flag (verified), so after task 186 every call on that line answers `configuration_error`; replace
   it with a 4-line `fastmcp.Client(StreamableHttpTransport("http://127.0.0.1:8000/mcp", headers=
   {"horizon-actor-email": os.environ["TREE_USER_IDENTIFIER"]}))` snippet (keep the "same path / warning
   line / no-run message as the CLI" check and the 8-vs-14 tool count), and note that the stdio entry
   needs no header.
5. **ADR Status-line notes** (the only Accepted-ADR edit allowed; body text untouched), exactly the two
   ADR-014's "Amendments to apply" lists: ADR-005 (Decision 4: the file branch stores a **Graph file** in
   MongoDB and writes a server file on stdio only; the inline payload rides in the `content` block alone)
   and ADR-013 (§2: gzip at write time into `graph_files`, read scoped to the **Request user**, expiring).
   ADR-001 line 227 (`--user-id`, body text) and ADR-007 §7 are NOT edited — ADR-014's Context references
   carry the trail.

**[HUMAN] Live Horizon checks (post-merge — Horizon redeploys from `main`; the orchestrator schedules
them; evidence in the Log, Traffic-Log rows cited):**
- (a) **Boot without `TREE_USER_IDENTIFIER`:** remove it from Horizon's env, redeploy; the server boots
  (server logs show "MCP server ready" without a user).
- (b) **Verified identity:** with Horizon authentication enabled, Claude Code (`/mcp` OAuth) →
  `search_memory` on `tree-memory` answers OUR user's memory. Record the EXACT `horizon-actor-email` value
  the server received (which email, its case — add one temporary DEBUG log line or read it off the
  unknown-identifier message by calling once before signing up) and `horizon-actor-type`. A
  client-forged `horizon-actor-email: someone-else@example.com` on the same authenticated request still
  resolves to OUR user (the gateway's value wins — stripped and re-added). A personal API key
  (`Authorization: Bearer fmcp_…`, `fastmcp.Client(url, auth="<key>")`) resolves to the SAME user as the
  OAuth session. An email with no `User` row → the signup message naming that exact email.
- (c) **Auth disabled is loud, then re-enabled:** flip the server's Access mode to "Disabled" for one
  call → every tool answers the missing-header `configuration_error` (no actor context is attached);
  re-enable it and confirm (b) again. (Skip the flip if the owner prefers not to expose the endpoint even
  briefly — say so in the Log.)
- (d) **Cross-instance download:** `visualize_memory_embeddings(as_html_file=true)` → read the
  `graphs://` link in a later request → decode → renders (`--dump-dom` shows `<body data-layout="fixed"`);
  record blob size and elapsed for the tutorial; after the TTL the read answers the real "not found or
  expired" message (the Traffic Log shows it unmasked — before, it showed "Error reading resource").
- (e) **Inline render on Horizon (task 188's cloud leg):** Claude Desktop on `tree-memory` renders the
  graph view and (graphrag only) the dashboard inline.
- (f) **`client_supports_extension` on stateless Horizon:** from Claude Desktop call
  `visualize_memory_embeddings` ~5× in one conversation; count inline views vs file fallbacks. Any file
  fallback → file a follow-up task (session capabilities not shared across instances); none → recorded.

## Acceptance criteria

- [ ] Runbook, tutorial, skill, README and `.env.example` say: over HTTP the identity is Horizon's
      `horizon-actor-email` (auth must stay enabled; access = org invite + signup with the Horizon email;
      service accounts answer `configuration_error`), stdio reads `TREE_USER_IDENTIFIER`, Horizon's env
      does NOT set `TREE_USER_IDENTIFIER`, downloads expire after about 5 minutes and are readable from any
      instance; no doc tells a cloud client to use a server path; `grep -rn "boot fails loudly\|REFUSES
      to boot\|serve_mcp.py --user-id\|serve-mcp USER_ID\|fastmcp call http\|X-Tree-User" docs/notes
      tutorials .agents apps/memory/README.md .env.example` is empty (`docs/adrs` excluded — ADR-001 line
      227 is Accepted body text; the pipeline scripts' `--user-id` in the README is unrelated and stays).
- [ ] Status-line notes applied to ADR-005 and ADR-013 exactly as ADR-014's "Amendments to apply" lists
      them; no other ADR touched; no body text changed.
- [ ] `run-pipelines-e2e` skill's MCP line uses a header-carrying `fastmcp.Client` snippet (or stdio) and
      the Tester can run it green against a local HTTP server.
- [ ] `make pre-commit` green (prettier on json/yaml; markdown wrapped by hand to the files' width).
- [ ] [HUMAN] (a), (b), (d), (e) pass with evidence in the Log — (b) records the exact email value and
      case received, the forged-header outcome and the API-key outcome; (c) done or explicitly skipped;
      (f) recorded with the inline/file count, and a follow-up task filed if any call fell back to the
      file path.
- [ ] Tutorial measurement sentence carries the Horizon numbers from (d) (no placeholder reaches `main`).

## User Stories

### Story: An operator gives a colleague access
1. The operator invites `dana@example.com` to the Horizon org and runs `make memory-signup
   USER_IDENTIFIER=dana@example.com NAME="Dana"` — exactly as the runbook's step 2 says.
2. Dana signs in with `/mcp` in Claude Code; `search_memory` answers her (empty) memory; nothing in her
   `.mcp.json` names a user.

### Story: An operator deploys from the runbook
1. The operator follows steps 1–4; step 4 lists `MCP_SKIP_INDEX_BOOTSTRAP`, `TREE_WORKING_DIR` and the
   credentials — no `TREE_USER_IDENTIFIER` — and the Auth paragraph says to keep Horizon Authentication
   enabled.
2. Their own OAuth session and their personal API key both resolve to the same signed-up user.

### Story: The skill handles an expired link
1. The model reads a `graphs://` link eleven minutes after it was issued and gets "not found or expired".
2. Following the skill line, it re-runs the visual tool once and reads the new link.

### Story: The reviewer finds the design in one place
1. A reader opens ADR-005; its Status line points at 014 for the Mongo-backed download and the single
   payload copy; ADR-013's Status note says the gzip now happens at write time into `graph_files`.

### Story: The Tester runs the e2e skill against a local HTTP server
1. Following `run-pipelines-e2e`, the Tester serves `make memory-serve-mcp TRANSPORT=streamable-http` and
   runs the skill's `fastmcp.Client` snippet with `horizon-actor-email` from `.env`'s
   `TREE_USER_IDENTIFIER`.
2. `visualize_memory_embeddings` answers the same warning line / no-run message as the CLI — no
   `configuration_error`.

---

Blocked by: 186, 187, 188

## Log

### [PA] 2026-10-05 16:40 — Grooming (rev 2, 2026-10-05: `horizon-actor-email` replaces `X-Tree-User`)

**Summary**
Make every operator- and client-facing text say what the code now does (identity = Horizon's authenticated
actor; access = invite + signup with the Horizon email; auth stays enabled), apply the Status-line notes
ADR-014 prescribes, and run the Horizon checks the plan could not verify locally — including the exact
email value and case the gateway delivers, the forged-header and API-key outcomes.

**Key decisions**
- The Horizon checks are `[HUMAN]` / post-merge by necessity (Horizon deploys from `main`), as task 181's
  were; the task is done when the evidence — or the follow-up task for (f) — is in the Log.
- The auth-disabled check (c) is optional: it briefly exposes the endpoint; the Log says whether it ran.
- `TREE_WORKING_DIR` stays on Horizon for deep-search files; only its stated reason changes.

**Dependencies**
- 186, 187, 188 — the texts describe their behaviour; (b)–(f) exercise them.

**User stories**
- 5 stories: giving access, runbook deploy (OAuth and API key), expired link via the skill, the reviewer's
  trail, the Tester's e2e run.

Ready for implementation.
