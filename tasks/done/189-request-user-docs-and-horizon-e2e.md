---
id: 189-request-user-docs-and-horizon-e2e
status: done
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

- [x] Runbook, tutorial, skill, README and `.env.example` say: over HTTP the identity is Horizon's
      `horizon-actor-email` (auth must stay enabled; access = org invite + signup with the Horizon email;
      service accounts answer `configuration_error`), stdio reads `TREE_USER_IDENTIFIER`, Horizon's env
      does NOT set `TREE_USER_IDENTIFIER`, downloads expire after about 5 minutes and are readable from any
      instance; no doc tells a cloud client to use a server path; `grep -rn
      --exclude-dir=fastmcp-client-cli "boot fails loudly\|REFUSES to boot\|serve_mcp.py
      --user-id\|serve-mcp USER_ID\|fastmcp call http\|X-Tree-User" docs/notes tutorials .agents
      apps/memory/README.md .env.example` is empty (`docs/adrs` excluded — ADR-001 line 227 is Accepted
      body text; the pipeline scripts' `--user-id` in the README is unrelated and stays;
      `.agents/skills/fastmcp-client-cli` excluded — a vendored, generic fastmcp-CLI skill whose
      `fastmcp call http://server/mcp tool --auth none` is correct CLI usage, not a Tree recipe).
- [x] Status-line notes applied to ADR-005 and ADR-013 exactly as ADR-014's "Amendments to apply" lists
      them; no other ADR touched; no body text changed.
- [x] `run-pipelines-e2e` skill's MCP line uses a header-carrying `fastmcp.Client` snippet (or stdio) and
      the Tester can run it green against a local HTTP server.
- [x] `make pre-commit` green (prettier on json/yaml; markdown wrapped by hand to the files' width).
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

### [SWE] 2026-10-05 21:37 — Implementation

**Files modified** (docs only, no code or tests)
- `docs/notes/deployment-runbook.md`: step 2 bullet now covers per-request users, invite plus signup with the Horizon email, the unknown-email and service-account `configuration_error`, and no boot pin. Step 4's env list drops `TREE_USER_IDENTIFIER` ("not `TREE_USER_IDENTIFIER` — ignored on HTTP"). The `TREE_WORKING_DIR` reason is now deep-search session files. Optional `TREE_MCP__GRAPH_FILE_TTL_SECONDS` (default 300). The Auth paragraph is the "must stay enabled" text plus OAuth / personal API key. Wrapped to 80 columns.
- `tutorials/4_x_x_deploying_mcp_server.md`: step 2 (`IDENTIFIER=` → `USER_IDENTIFIER=`, the identifier is the Horizon account email, per-request lookup). The env table row is removed and a "Do not set `TREE_USER_IDENTIFIER`" sentence plus the optional TTL knob are added. "three" → "two" Horizon-specific values and rows. Sync step 1 now says "Copy `.env.prod` without its `TREE_USER_IDENTIFIER` line". `.env.example` line 1 is that variable, so pasting `.env.prod` as-is would set it on Horizon. "Auth and Clients" gains the API-key sentence, the auth-must-stay-enabled rule, the invite plus signup rule and the service-account line. "Downloading visualizations" now says: no server path, stored in MongoDB for the requesting user, readable from any instance, expires after about 5 minutes (`mcp.graph_file_ttl_seconds`), and "not found or expired" means re-run. The decode one-liner and the cap sentence are kept.
- `.agents/skills/tree-memory/SKILL.md`: line 68 uses the spec's text. The Errors section gets the spec's one line.
- `apps/memory/README.md` "MCP server": one paragraph. No `USER_ID`. Stdio reads `TREE_USER_IDENTIFIER` from `.env`; over HTTP the identity is `horizon-actor-email`, which the client sets only against a local `TRANSPORT=streamable-http` server.
- `.env.example`: a 2-line comment above `TREE_USER_IDENTIFIER` with the spec's wording.
- `.agents/skills/run-pipelines-e2e/SKILL.md` line 59: replaced with a runnable `fastmcp.Client(StreamableHttpTransport(..., headers={"horizon-actor-email": os.environ["TREE_USER_IDENTIFIER"]}))` heredoc. It runs through `uv --directory apps/memory run --env-file ../../.env`, the same form as `.mcp.json`, so `.env` is loaded and a bare `uv run` `KeyError` cannot happen. It has a `FASTMCP_PORT` hint for when 8000 is taken. It keeps the "same warning line / no-run message as the CLI" check and the 8 vs 14 tool counts. It says that over HTTP the answer names no path (a `graphs://<token>.html.gz` link plus "The link expires in about 5 minutes."), and that the stdio entry needs no header.
- `docs/adrs/005_single_graph_rendering_stack.md`, `docs/adrs/013_horizon_scale_mcp_surface.md`: ADR-014's two notes are appended verbatim, leading "— " included, to the END of each Status line. Diff is 1 line per file, no body text changed, no other ADR touched.

**Tests**
- Unit: N/A (no code or test changes).
- Integration: N/A (no integration suite).
- Live: the skill's snippet, extracted from the SKILL.md as written with only the port swapped, ran green against local HTTP servers in rag and graphrag mode (evidence below).

**Acceptance criteria**
- [ ] Docs say the identity and expiry rules, with no server path for cloud clients. Content is done in all five files. The AC grep still has ONE hit, outside the five target docs: `.agents/skills/fastmcp-client-cli/SKILL.md:81: fastmcp call http://server/mcp tool --auth none`. That file is the generic, vendored fastmcp-CLI skill, the line is correct generic CLI usage and not a Tree recipe, and the task does not list the file. I did not edit it. Tester/PA: accept the hit or narrow the grep (e.g. `--exclude-dir=fastmcp-client-cli`). Left unticked for that reason only.
- [x] ADR-005 / ADR-013 Status-line notes. `git diff --stat docs/adrs` shows 2 files, 1 line each.
- [x] `run-pipelines-e2e` header-carrying `fastmcp.Client` snippet, run green on local HTTP (below).
- [x] `make pre-commit` green.
- [ ] [HUMAN] Horizon checks (a)–(f). They need a deploy from `main` and an authenticated client. The ready-to-run checklist and client script are below and in the scratchpad.
- [ ] Tutorial measurement sentence: NOT changed. It still says "Measured locally on 2026-10-03: …", which is a real measurement and not a placeholder. It gets replaced with the (d) numbers after the live check. Template in checklist (d)4.

**Evidence**
```
$ make env-status
Env target: local (.env)

$ grep -rn "boot fails loudly\|REFUSES to boot\|serve_mcp.py --user-id\|serve-mcp USER_ID\|fastmcp call http\|X-Tree-User" docs/notes tutorials .agents apps/memory/README.md .env.example
.agents/skills/fastmcp-client-cli/SKILL.md:81:fastmcp call http://server/mcp tool --auth none     # vendored generic skill, see AC note

$ make pre-commit
prettier....Passed  ruff check....Passed  ruff format....Passed  biome check (harness)....Passed
$ make memory-tests          # no code changed; the 186 guard scans docs/ for X-Tree-User
======================= 5084 passed in 66.08s (0:01:06) ========================

# Port 8000 is held by kitaru-local-server-1, so FASTMCP_PORT=8765. The snippet was extracted from the
# SKILL.md with awk, and only 8000 -> 8765 was changed.
$ FASTMCP_PORT=8765 make memory-serve-mcp TRANSPORT=streamable-http
$ bash skill_snippet.sh
8 tools
Embedding map: 372 of 372 chunks (the 14 most-recent of 14 documents) in 15 clusters (+1 noise). Since this client does not render inline MCP App UIs, I rendered a self-contained interactive embedding map as a download. Read the linked `graphs://…html.gz` resource — … The link expires in about 5 minutes.
$ env -u TREE_USER_IDENTIFIER bash skill_snippet.sh        # direnv var removed: --env-file supplies it
8 tools
Embedding map: 372 of 372 chunks (the 14 most-recent of 14 documents) in 15 clusters (+1 noise). …
$ (header = bob@example.com, a user with no clustering run)
No clustering run found for this user — run make memory-run-clustering-pipeline to build the embedding map.
$ (the snippet without headers=, i.e. what the old `--auth none` recipe got)
{"error_type":"configuration_error","retryable":false,"message":"No horizon-actor-email header on this request. …"}

# CLI comparison (--no-open), same summary line and same no-run message:
$ uv run --env-file ../../.env python scripts/visualize_embeddings.py --hulls --no-open --output <scratch>/cli-map.html
Embedding map: 372 of 372 chunks (the 14 most-recent of 14 documents) in 15 clusters (+1 noise)
$ … --user-identifier bob@example.com
No clustering run found for this user — run make memory-run-clustering-pipeline to build the embedding map.

$ TREE_MEMORY__MODE=graphrag FASTMCP_PORT=8766 make memory-serve-mcp TRANSPORT=streamable-http
$ bash skill_snippet.sh (port 8766)
14 tools
Embedding map: 372 of 372 chunks … I rendered a self-contained interactive embedding map as a download. …

# Dry run of the Horizon client script against local HTTP (LOCAL_MCP_URL / LOCAL_ACTOR_EMAIL):
[oauth] search_memory('Prefect deployment') -> outcome=found parents=5 first_titles=[…]
[unknown] {"error_type":"configuration_error",…,"message":"No user with identifier 'nobody@example.com' — run `make memory-signup USER_IDENTIFIER=nobody@example.com` …"}
TOOL elapsed: 0.09s
LINK: graphs://gN3bssCL0J7MxrNCC67pWA.html.gz application/gzip
READ elapsed: 0.01s mime=application/gzip html=0.24 MB gzip=0.05 MB base64=0.07 MB (239861 / 48830 / 65108 B)   # re-run after switching the script to decimal MB
$ chrome --headless=new --use-angle=swiftshader --enable-unsafe-swiftshader --virtual-time-budget=15000 --dump-dom file://…/out/map.html | grep -o '<body[^>]*>'
<body data-layout="fixed">
```

**[HUMAN] Horizon checklist (post-merge; full copy with commands in the scratchpad `horizon/CHECKLIST.md`, client `horizon/horizon_checks.py`)**

Run from the repo root with `make env-prod`. `run() { uv --directory apps/memory run --env-file ../../.env.prod python horizon_checks.py "$@"; }`. Cite the Traffic-Log row for each call.
- (a) Delete `TREE_USER_IDENTIFIER` from Horizon env (Production) and redeploy. Server logs should show `MCP server ready (database=…, thread_id=…)` with no user and no traceback.
- (b) Exact email: use mongosh to rename your `users` row's `identifier` (copy it EXACTLY from `find`, so the restore matches) to `sentinel-189@invalid`, run `run unknown`, then rename it back. The answer names the email the gateway sent. The server lowercases it before naming it, so this read gives the value but not its raw case. This step also covers "unknown email → signup message naming it". For raw case plus `horizon-actor-type`, add one temporary `warning` log line of the `horizon-*` headers in `resolve_request_user` on a PR preview, read the server logs, then revert (exact line in CHECKLIST.md). For OAuth, run `/mcp` in Claude Code and `run oauth`, then record the fingerprint (outcome / parent count / first 3 titles). For the forged header, `run forged` (OAuth plus `horizon-actor-email: someone-else@example.com`) must give the SAME fingerprint. For the API key, `FMCP_API_KEY=fmcp_… run apikey` must also give the SAME fingerprint.
- (c) Optional. Set Access mode to Disabled, make one unauthenticated `Client(url)` call (expect the missing-header `configuration_error`), re-enable it immediately and re-run `run oauth`. If skipped, say so in the Log.
- (d) `run download`: the tool call and the read are in two separate client sessions. Write down TOOL / READ elapsed and the html / gzip / base64 MB, check `--dump-dom` shows `<body data-layout="fixed">`, and run `run download --expiry` (300 + 90 s), which should give "Graph file '…' not found or expired (download links expire after about 5 minutes) — run the visualize tool again." in place of "Error reading resource". The numbers then replace the tutorial's "Measured locally on 2026-10-03: …" sentence. Template is in CHECKLIST.md (d)4.
- (e) Claude Desktop on `tree-memory`: `visualize_memory_structure` and `visualize_memory_embeddings` render inline, and so does `memory_dashboard` on graphrag. "No graph data in tool result." means task 188's restore rule applies.
- (f) Call `visualize_memory_embeddings` 5× in one Desktop conversation and record inline X / file Y. If Y > 0, file a follow-up ("session capabilities not shared across Horizon instances").

**Notes**
- The skill's Errors line is the spec's verbatim text: "`configuration_error` naming `horizon-actor-email` or `TREE_USER_IDENTIFIER` … an unsigned-up email …". The unknown-identifier message (task 186) names neither string; it names `make memory-signup`. So "an unsigned-up email" is listed under a trigger it does not match. The general rule above it (`retryable: false` → do not retry) still gives the right behaviour. PA may want "naming `horizon-actor-email`, `TREE_USER_IDENTIFIER` or `make memory-signup`".
- Follow-up candidate (code, out of scope): the `apps/memory/src/tree/mcp/client.py` module docstring says "There is no static API key for client connections", which contradicts the personal-API-key sentence the runbook and tutorial now state (ADR-014).
- The tutorial's "Order of Operations" intro ("Each step depends on the previous one at boot time") and the runbook's opening paragraph still say sign-up is a boot dependency. It no longer is: it is a first-request dependency. Left alone because it is not in the spec's line list. One-word fix if the PA wants it.
- Local side effects: five `graph_files` rows from the runs above (the TTL removes them). No user rows changed. Servers on 8765/8766 are stopped.

### [SWE] 2026-10-05 21:55 — Addendum (coordinator fixes before QA)

**Files modified**
- `apps/memory/src/tree/mcp/client.py`: docstring only, no code change. In the module docstring, "There is no static API key for client connections." is replaced by: a personal Horizon API key works as a bearer token (`Client(url, auth="fmcp_…")`) and resolves to the same actor email as the OAuth session. The `get_cloud_client` docstring's "headless caller needs delegated authentication" paragraph now says that a headless caller uses a personal API key, and that a service-account key carries no email, so it gets `configuration_error`.
- `.agents/skills/tree-memory/SKILL.md`: the Errors line now separates two cases. A `configuration_error` naming `horizon-actor-email` or `TREE_USER_IDENTIFIER` means the request carries no user. One naming `make memory-signup` means the email is not signed up, and the operator runs `make memory-signup USER_IDENTIFIER=<their Horizon account email>`. Either way: relay the message, do not retry.
- `docs/notes/deployment-runbook.md` opening paragraph and the tutorial's "Order of Operations" intro now say that sign-up is a first-request dependency, not a boot one: the server resolves the user per request.
- This task file: the AC1 grep now has `--exclude-dir=fastmcp-client-cli`, and the AC says why. That folder is a vendored, generic fastmcp-CLI skill. Its `fastmcp call http://server/mcp tool --auth none` is correct CLI usage, not a Tree recipe. The narrowed grep is empty, so AC1 is ticked.

**Evidence**
```
$ make env-status
Env target: local (.env)
$ grep -rn --exclude-dir=fastmcp-client-cli "boot fails loudly\|REFUSES to boot\|serve_mcp.py --user-id\|serve-mcp USER_ID\|fastmcp call http\|X-Tree-User" docs/notes tutorials .agents apps/memory/README.md .env.example
(no output, exit 1)
$ make memory-format-check && make memory-lint-check
341 files already formatted
All checks passed!
$ make pre-commit
prettier....Passed  ruff check....Passed  ruff format....Passed  biome check (harness)....Passed
$ make memory-tests
======================= 5084 passed in 66.33s (0:01:06) ========================
```

**Notes**
- The 3 earlier Notes (skill Errors line, `client.py` docstring, boot-time wording) are resolved by this addendum.
- Still open: [HUMAN] Horizon checks (a)–(f), and the tutorial measurement sentence, which needs the numbers from (d).
- WARNING (not my change): after the runs above, something outside this task moved files in the working tree. `data/notes/mcp-facade-skill-dos-and-donts.md` and `data/notes/mcp_server_design.md` now show as deleted. `docs/notes/mcp-facade-skill-dos-and-donts.md`, `docs/notes/mcp_server_design.md` and `docs/notes/mcp_server_auth.md` are new and untracked. Two of them hit the AC1 grep: `docs/notes/mcp_server_auth.md:31` (`serve_mcp.py --user-id`) and `docs/notes/mcp_server_design.md:136` (`X-Tree-User`). They also fail the task-186 guard `test_tools_request_user.py::test_no_client_side_identity_header_name_exists_anywhere`, which scans `docs/`. I left them alone. With them excluded, the grep is empty and the suite is 5084/5084, as recorded above. Leave them out of the 189 commit, or have the owner move them back under `data/notes/`.

### [Tester] 2026-10-05 22:00 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check` 341 files, `make memory-lint-check`, `make pre-commit` all Passed)
- Unit tests: 5084 passed / 0 failed (`make memory-tests`, env target local, run after the coordinator's note edits)
- Integration tests: N/A (no suite by design)
- Warnings: 0 new

**E2E adversarial pass** (local `FASTMCP_PORT=8765 make memory-serve-mcp TRANSPORT=streamable-http`; 8000 is held by kitaru-local-server-1)
- Happy path: the run-pipelines-e2e snippet extracted from SKILL.md with awk, only `127.0.0.1:8000` -> `8765` changed → `8 tools` + the "Embedding map: 372 of 372 chunks … rendered … as a download … The link expires in about 5 minutes." line, no path, no `configuration_error` (PASS). Same with `env -u TREE_USER_IDENTIFIER` (`--env-file` supplies it) (PASS). `TREE_MEMORY__MODE=graphrag` on 8766 → `14 tools` + same map line (PASS).
- Break 1 (state: snippet exactly as written, port 8000): hits the unrelated container → `405 Method Not Allowed`. Expected on this machine; the SKILL text documents the `FASTMCP_PORT` + URL change (PASS, note below).
- Break 2 (malformed/hostile identity header, `search_memory`): no header, empty header, `horizon-actor-type` only (service-account shape) → `configuration_error`, `retryable:false`, message names `horizon-actor-email`; `nobody@example.com`, `{"$ne": null}`, `.*` → `configuration_error` "No user with identifier '…' — run `make memory-signup USER_IDENTIFIER=…`" (no injection/regex match, no stack trace); `PAUL.IUSZTIN@EXAMPLE.COM` and `Paul.Iusztin@Example.com` resolve to the signed-up lowercase user (case-insensitive) (PASS). Whitespace-only / padded header values are rejected client-side by httpx ("Illegal header value") before reaching the server.
- Break 3 (graph download scoping): owner read of the `graphs://<token>.html.gz` link → blob 65108 B; `bob@example.com` read → "Graph file '…' not found or expired (download links expire after about 5 minutes) — run the visualize tool again."; no-header read → the missing-header message; `graphs://x.html.gz` → "Invalid graph file name"; `graphs://../../etc/passwd` → "Unknown resource"; well-formed unknown token → "not found or expired" (PASS). Full 6-minute expiry not waited; the wording and the TTL minutes derive from `mcp.graph_file_ttl_seconds` (`viz_app.py:_ttl_minutes`).
- Break 4 (stdio transport): `TREE_USER_IDENTIFIER` set → answer names the LOCAL file `apps/memory/.tree/graphs/embedding-map-*.html`, "Opened it in your browser. The graphs:// link expires in about 5 minutes."; `TREE_USER_IDENTIFIER=""` → `configuration_error` naming `TREE_USER_IDENTIFIER` and the `.mcp.json` env block / `.env` (PASS; matches skill + README).
- Horizon scratchpad client: `horizon_checks.py` with `LOCAL_MCP_URL`/`LOCAL_ACTOR_EMAIL` → `oauth` (found, 5 parents), `unknown` (runs; prints the signed-up user's answer because the mixed-case email resolves), `download` (TOOL 0.09 s, READ 0.02 s, 0.24/0.05/0.07 MB, wrote blob.b64 + map.html) all work. `forged` is Horizon-only: it always builds an OAuth client against tree-memory.fastmcp.app and ignores `LOCAL_MCP_URL`, so locally it opened a real Horizon OAuth flow and failed with "Missing client_secret" (expected without the browser callback). A forged header cannot be tested without the gateway. `apikey` not run (needs a key).

**Acceptance criteria**
- [x] PASS — Docs say header identity / auth must stay enabled / invite + signup / service-account `configuration_error` / stdio reads `TREE_USER_IDENTIFIER` / Horizon env does not set it / ~5 min expiry / any instance / no server path for cloud clients. Evidence: git diff of the 5 docs read against `mcp/request_user.py` (`ACTOR_EMAIL_HEADER`, `normalize_identifier` case-insensitive lookup, `unknown_identifier_message` names `make memory-signup USER_IDENTIFIER=`, missing-header message names Access mode "Disabled" + service account) and `viz_app.py` ("The link expires in about N minutes", stdio-only local path, Mongo `GraphFile` scoped to `user_id`); every claim matches. `grep -rn --exclude-dir=fastmcp-client-cli "boot fails loudly\|REFUSES to boot\|serve_mcp.py --user-id\|serve-mcp USER_ID\|fastmcp call http\|X-Tree-User" docs/notes tutorials .agents apps/memory/README.md .env.example` → no output, exit 1 (empty), re-run after the coordinator's edits to the two `docs/notes/mcp_server_*` files.
- [x] PASS — ADR-005 / ADR-013 Status notes equal ADR-014 "Amendments to apply" verbatim (string-compared: note appended after a " " to the old Status line, `endswith` True for both); `git diff --stat docs/adrs` = those 2 files, 1 line each, no other ADR touched, no body text changed.
- [x] PASS — run-pipelines-e2e snippet is a header-carrying `fastmcp.Client` and ran green on local HTTP (rag 8 tools, graphrag 14 tools).
- [x] PASS — `make pre-commit` green.
- [ ] PENDING (by design) — [HUMAN] Horizon checks (a)–(f).
- [ ] PENDING (by design) — tutorial measurement sentence still "Measured locally on 2026-10-03: …" (a real measurement, not a placeholder); replace with check (d) numbers after the live run.

**Other issues found (non-blocking)**
- The skill snippet hardcodes `127.0.0.1:8000`; run as written on a machine where 8000 is taken it fails with an opaque 405. The prose above it tells the reader to swap the port, so this is acceptable; a `PORT=${FASTMCP_PORT:-8000}` in the heredoc would remove the manual edit.
- Scratchpad `horizon_checks.py forged` does not honor `LOCAL_MCP_URL` (it prints the local target, then opens a real Horizon OAuth flow). Scratch tool, not committed; fine for the live run, just do not dry-run `forged` locally.
- Stdio run opened a browser tab and wrote `apps/memory/.tree/graphs/embedding-map-20261005-184645.html` (gitignored; local test artifact).
- Out of scope, per the coordinator: the three `docs/notes/*.md` moved files and the `data/notes/*` deletions are not part of 189 and must be left out of its commit (or moved by the owner as one separate change).

**VERDICT: PASS** (all non-[HUMAN] criteria verified; the [HUMAN] Horizon AC and the tutorial-measurement AC remain open by design)

### [PA] 2026-10-05 22:10 — Acceptance Review

**VERDICT: ACCEPT**

Feature-level review of `request-scoped-users` (PR #46, tasks 185–189, ADR-014, glossary). Reviewed evidence from the Tester log entry; all machine-verifiable acceptance criteria verified from the user's POV. Reviewed runbook, tutorial, skill, README, .env.example and the two ADR Status-line notes against the code: every claim matches request_user.py / viz_app.py. The [HUMAN] Horizon checks (a)–(f) and the tutorial measurement sentence stay open by design. Hand off to the PR Reviewer.

### [PA] 2026-10-09 17:35 — Correction to the Horizon checklist, items (e) and (f)

**Corrections to the SWE entry above (not a rewrite; the original lines stand as written).**

- (e) says `"No graph data in tool result." means task 188's restore rule applies.` That mapping was
  wrong. Task 196 (`tasks/196-bug-mcp-apps-no-graph-data-on-horizon.md`) reproduced the empty iframe
  deterministically: on stateless streamable-http (what Horizon runs) every `tools/call` lands on a fresh
  session whose `client_params is None`, so `ctx.client_supports_extension` always answered `False` and
  `_graph_tool_result` took the file branch — which carries no payload in ANY channel. The host mounts the
  iframe from `tools/list`'s `_meta.ui.resourceUri` regardless of the per-call branch, so it rendered
  "No graph data in tool result." The restore rule (ADR-014 §5: flip to `structured_content`-only) does
  NOT apply and was never the cause; the capability gate is now three-state (`viz_app._ui_capability`).
- (f)'s anticipated follow-up ("session capabilities not shared across Horizon instances") is filed as
  `tasks/197-persist-mcp-client-capabilities-per-horizon-session.md`; its trigger was measured in 196
  (text-only clients receive the `audience=["user"]` block on Horizon unless they pass
  `as_html_file=true`).
- (e) and (f) remain un-run [HUMAN] checks; the post-merge inline check now lives on task 196's last AC.
