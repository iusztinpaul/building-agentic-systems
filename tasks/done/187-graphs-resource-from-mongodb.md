---
id: 187-graphs-resource-from-mongodb
status: done
feature: request-scoped-users
---

# `graphs://<name>.html.gz` reads the **Graph file** from MongoDB, scoped to the **Request user** and the TTL: the file branch stores the gzip row under an unguessable name, states the expiry, writes no server file on HTTP; stdio keeps the local file + browser; one reworded download contract

Tags: `mcp`, `viz`, `resources`, `horizon`
Depends on: 185 (`GraphFile`), 186 (`resolve_request_user`)
Blocks: 189 (docs, live Horizon round trip)
Implements: ADR-014 §4 (amends ADR-013 §2 / ADR-005 Decision 4)

## Problem

`_graph_tool_result` writes `.tree/graphs/<slug>-<stamp>.html` during `tools/call` and `graph_file` reads
it during a later `resources/read` — the ONLY cross-request dependency in the visualization surface, and the
one Horizon does not honour (no instance affinity, ephemeral `/tmp`). The tool text also hands a remote
client a server path it can never open. Task 185 landed the store; this task moves the bytes into it, scopes
the read by the request user, and keeps the local stdio convenience.

## Scope

**Human decision (final):** MongoDB is the source of truth and the `graphs://` link is always returned.
Name = `secrets.token_urlsafe(16) + ".html.gz"` (flat, no user / tool in the URI; knowing the link is the
capability, the TTL bounds its life). `graphs://{name}` reads `{name, user_id: request user}`; not found /
other user / expired → ONE "not found or expired" message. Tool text states the link expires after N
minutes. Keep the gzip blob + `DOWNLOAD_CONTRACT` shape. On HTTP no disk write and no server path in the
text. ADDITIONALLY, only when `ctx.transport == "stdio"`, keep the best-effort `.tree/graphs/` write +
`webbrowser.open` and include the local path.

1. **Renderer split** (`tree/memory/visualize/graph.py`): extract `render_graph_html(payload) -> str` (the
   `__DATA__` splice with the `</` guard) out of `_render_graph_file`, which now calls it and keeps writing
   `<slug>-<stamp>.html` for the CLI and the stdio convenience. Dependency direction memory ← mcp holds.
2. **File branch** (`viz_app._graph_tool_result`) becomes `async` and takes `user_id: PydanticObjectId`
   (keyword) — `graph_tools._dual_graph_result` and all five callers (`tools.py` ×2, `graph_tools.py` ×3)
   `await` it. Inline branch: untouched here (task 188 drops `structured_content`). File branch, in order:
   `html = render_graph_html(payload)`; `name = secrets.token_urlsafe(16) + ".html.gz"`;
   `await GraphFile(user_id=user_id, name=name, html_gz=gzip.compress(html.encode("utf-8"))).insert()`
   (gzip at WRITE time — ADR-013 §2's "compressed on read" is amended; no `.gz` on disk still holds);
   `ttl_minutes = max(1, app_config.mcp.graph_file_ttl_seconds // 60)`. Text (HTTP): `"{summary}. Since
   {reason}, I rendered a self-contained interactive {noun} as a download. {DOWNLOAD_CONTRACT} The link
   expires in about {ttl_minutes} minutes."` plus the `ResourceLink` (`uri=f"graphs://{name}"`,
   `name=name`, `mimeType=GZIP_MIME`, description unchanged). stdio ONLY (`ctx.transport == "stdio"`):
   additionally `path = _render_graph_file(payload, query=query)` (keeps today's `<slug>-<stamp>.html`
   name; the URI name is the token — two names, stated in the docstring) and the best-effort
   `webbrowser.open`; the text then reads `"… I saved it to:\n{path}\n"` + `"Opened it in your browser."`
   or, when the open fails, the contract sentence — and in BOTH stdio cases the expiry sentence follows,
   because the link is always returned ("The graphs:// link expires in about N minutes."). A
   `PyMongoError` on the insert
   answers the `storage_unavailable` envelope (retryable, `STORAGE_UNAVAILABLE_MESSAGE`), logged with the
   traceback — the SWE picks how `viz_app` reaches `tool_error` without a module-level cycle (`tools.py`
   imports `viz_app`; a function-level import, or hoisting the envelope helpers into a small
   `tree.mcp.errors` re-exported by `tools.py`).
3. **Resource** `graph_file(name: str, ctx: Context) -> ResourceResult`, `async`: (a) name guard —
   `^[A-Za-z0-9_-]{22}\.html\.gz$` (22 chars = `token_urlsafe(16)`), a NUL or a plain `.html` name still
   raise the existing `Invalid graph file name: … (expected <name>.html.gz)` (task 181 Story 3 holds);
   (b) `user_id = await resolve_request_user(ctx)`; (c) `GraphFile.find_one(GraphFile.name == name,
   GraphFile.user_id == user_id)`; `None` → `Graph file '<name>' not found or expired (download links
   expire after about N minutes) — run the visualize tool again.`; (d) `ResourceResult([ResourceContent
   (row.html_gz, mime_type=GZIP_MIME)])`. EVERY failure (a–c, including the `RequestUserError` message
   verbatim) is raised as `fastmcp.exceptions.ResourceError`: `read_resource` re-raises `FastMCPError`
   subclasses unmasked but masks any other exception when `mask_error_details` is on — the only way the
   spec's messages reach a client on every host (this is the "Error reading resource" the design note saw).
   `GRAPHS_DIR` is no longer read by the resource (it stays for the stdio write).
4. **One download contract.** `DOWNLOAD_CONTRACT` no longer presupposes a path: `Read the linked
   \`graphs://…html.gz\` resource — an \`application/gzip\` blob: write its base64 \`blob\` to a file and
   run \`base64 -d < blob.b64 | gunzip > <name>.html\` (Python: \`gzip.decompress(base64.b64decode(blob))\`),
   then open the \`.html\` in a browser.` The five tool docstrings (`tools.py` `visualize_memory_structure`,
   `visualize_memory_embeddings`; `graph_tools.py` `visualize_memory_structure`, `query_memory`,
   `search_memory`) replace "the result carries the server-side path AND a `graphs://` link … If the path
   exists locally just share it. If that path is not on your machine …" with "the result carries a
   `graphs://<name>.html.gz` resource link that expires after a few minutes (and, on a local stdio server,
   the file path too) — do NOT re-author the HTML yourself. If a local path is given just share it.
   {DOWNLOAD_CONTRACT}"; `graph_file`'s and `_graph_tool_result`'s docstrings and the `viz_app` module
   docstring describe the Mongo-backed, user-scoped, expiring download. The verbatim-modulo-wrapping
   anchor tests cover all six.
5. **Tests** (`/squid-testing-python`; the `test_viz_app.py` file-branch tests become async with
   `GraphFile.insert` / `find_one` patched): HTTP (`ctx.transport = "streamable-http"`): the inserted row
   has the request `user_id`, a `name` matching the regex, `html_gz` that gunzips to `render_graph_html
   (payload)`; the link `uri`/`name` equal the row name with `application/gzip`; NO file under a tmp
   `GRAPHS_DIR`, `webbrowser.open` not called, no path in the text; the text contains `DOWNLOAD_CONTRACT`
   and `expires in about 5 minutes` (config 300). stdio: the row is inserted AND a `<slug>-<stamp>.html` is
   written AND `webbrowser.open` called; text carries the path, the link, "Opened it in your browser."
   (or the contract when the open fails) AND the expiry sentence. Insert `PyMongoError` →
   `storage_unavailable` envelope. Resource:
   a row for the request user → blob round-trips; unknown name → `ResourceError` "not found or expired";
   a row owned by ANOTHER user → the SAME message; `x.html`, `../x.html.gz`, `x.txt.gz`, 21-char token,
   NUL → `ResourceError` "Invalid graph file name"; seam raising → `ResourceError` carrying the seam's
   message; the in-process `fastmcp.Client(mcp)` read (existing test) still yields `BlobResourceContents`
   `application/gzip` with the seam and `find_one` patched. Contract anchors in the six docstrings; the
   shell one-liner test stays; source guards: `graph_file` references neither `GRAPHS_DIR` nor
   `Path`, and `If that path is not on your machine` appears nowhere under `src/tree/mcp`, the skill or the
   tutorial (task 189 updates those texts — this guard is scoped to `src/tree/mcp` here). `_dual_graph_result`
   and the five callers await the helper (existing tests adjust).
6. **Live verification (LOCAL env, `make memory-serve-mcp TRANSPORT=streamable-http`, a `fastmcp.Client`
   with `headers={"horizon-actor-email": "<identifier>"}` — locally the client sets it, there is no
   gateway):** `visualize_memory_embeddings(as_html_file=true)` → text with no path, the
   expiry sentence, link `graphs://<22 chars>.html.gz`; `read_resource(uri)` → blob decodes to HTML that
   renders (headless Chrome `--dump-dom` shows `<body data-layout="fixed"`); `mongosh`
   `db.graph_files.find({}, {name:1, user_id:1, created_at:1})` shows the row; `.tree/graphs/` gained NO
   file; a second signed-up user's header reading the same URI → "not found or expired"; after
   `TTL + ~60 s` the owner's read → "not found or expired" and the row is gone. stdio (Claude Code
   `tree-memory-local`): the same call answers the local path + "Opened it in your browser." + the link.
   Record blob size and elapsed. Evidence in the Log.

## Acceptance criteria

- [x] `render_graph_html(payload)` exists in `tree.memory.visualize.graph`; `_render_graph_file` uses it
      and still writes `<slug>-<stamp>.html` for the CLI.
- [x] The file branch inserts one `GraphFile` `{user_id = request user, name = <22-char token>.html.gz,
      html_gz = gzip(html), created_at}` and links `graphs://<name>` (`application/gzip`); the text contains
      `DOWNLOAD_CONTRACT` and "expires in about N minutes" with N from `mcp.graph_file_ttl_seconds`.
- [x] On a non-stdio transport the file branch writes no file, opens no browser and names no server path;
      on stdio it ALSO writes `.tree/graphs/<slug>-<stamp>.html`, opens it best-effort and names the path;
      every file-branch answer (HTTP, stdio opened, stdio not opened) carries the link AND the expiry
      sentence.
- [x] A failed insert answers `storage_unavailable` (retryable).
- [x] `graphs://{name}` resolves the request user, reads `{name, user_id}` and answers the stored gzip as an
      `application/gzip` blob; unknown name, another user's row and an expired row answer ONE
      `ResourceError` "not found or expired" message; an invalid name shape (incl. plain `.html`) answers
      `ResourceError` "Invalid graph file name"; a seam failure answers `ResourceError` with the seam's
      message; the resource reads no filesystem path.
- [x] `DOWNLOAD_CONTRACT` is reworded (no "path on your machine" preamble) and present verbatim (modulo
      wrapping) in the five tool docstrings and the resource docstring; the old sentence is gone from
      `src/tree/mcp`.
- [x] Live (local, HTTP): generate → read → decode renders; a second user cannot read it; it expires;
      `.tree/graphs/` untouched. Live (stdio): path + browser + link. Evidence in the Log.
- [x] `make memory-format-check`, `make memory-lint-check`, `make pre-commit`, `make memory-tests` green.

## User Stories

### Story: Codex downloads the map from Horizon across instances
1. Against `tree-memory`, the user asks for "an HTML file of my memory map".
2. The model calls `visualize_memory_embeddings(as_html_file=true)`; the answer carries the link
   `graphs://Q2hhbmdlTWVQbGVhc2U.html.gz` (`application/gzip`), the decode one-liner and "The link expires
   in about 5 minutes." — no server path.
3. The model reads the resource (any instance serves it — the bytes are in Mongo), decodes it with
   `base64 -d < blob.b64 | gunzip > map.html`, and the user opens the map.

### Story: The link is read too late
1. Ten minutes later the model reads the same URI again.
2. The server answers `Error reading resource 'graphs://…': Graph file '…' not found or expired (download
   links expire after about 5 minutes) — run the visualize tool again.`
3. The model re-runs the tool and reads the fresh link.

### Story: Another user guesses a link
1. Bob reads Alice's `graphs://…html.gz` URI from his own Horizon account (the gateway attaches
   `horizon-actor-email: bob@example.com`).
2. The server answers the SAME "not found or expired" message — nothing distinguishes "not yours" from
   "gone".

### Story: The local user is not bothered with gzip
1. On `tree-memory-local` (stdio) the same call answers `.tree/graphs/embedding-map-20261005-171200.html`
   and "Opened it in your browser." — and still carries the `graphs://` link plus "expires in about 5
   minutes" in case the open failed.

### Story: A client asks for the old text name
1. A client reads `graphs://embedding-map-20261003-181500.html`.
2. The server answers `Invalid graph file name: … (expected <name>.html.gz)` — one download path, unchanged.

---

Blocked by: 185, 186

## Log

### [PA] 2026-10-05 16:40 — Grooming

**Summary**
The download's bytes move from the server's disk to a user-scoped `GraphFile` row with a TTL; the resource
reads `{name, user_id}` and raises `ResourceError` so its messages reach any client; HTTP answers a link
and an expiry only, stdio keeps the local file + browser.

**Key decisions**
- Gzip at write time into Mongo (one row per render) — the "compressed on read" of ADR-013 §2 is amended;
  the client-facing blob is unchanged.
- `ResourceError` for every resource failure: the only exception class FastMCP forwards unmasked.
- Two names on stdio (token URI, `<slug>-<stamp>.html` on disk): zero new file-naming code; the URI
  never exposes a user or a tool.
- "Not found", "not yours" and "expired" are one message on purpose.
- Supersedes the still-open `[HUMAN]` Horizon AC of task 181 — the Horizon round trip is re-verified in
  task 189 on the new path.

**Dependencies**
- 185 — the `GraphFile` model and its TTL index.
- 186 — `resolve_request_user`, the owner of the row and the reader's scope.

**User stories**
- 5 stories: cross-instance download, expired link, another user's link, local stdio, old text name.

Ready for implementation.

### [SWE] 2026-10-05 21:02 — Implementation

**Files modified**
- `apps/memory/src/tree/memory/visualize/graph.py` — new public `render_graph_html(payload) -> str` (the `__DATA__` splice + `</` guard); `_render_graph_file` calls it and still writes `<slug>-<stamp>.html` (CLI + stdio).
- `apps/memory/src/tree/mcp/viz_app.py` — `_graph_tool_result` is `async`, takes `*, user_id`, returns `ToolResult | str`. File branch: render → `secrets.token_urlsafe(16) + ".html.gz"` → `GraphFile(...).insert()` with gzip at write time → link + "The link expires in about N minute(s)." On `ctx.transport == "stdio"` it ALSO writes the local file, opens it best effort and names the path ("… The graphs:// link expires in about N minutes."). `PyMongoError` on insert → `storage_error(...)` → `storage_unavailable` (retryable), via a function-level `from tree.mcp.tools import storage_error` (no module-level cycle; ruff passes). `graph_file(name, ctx)` is `async`: regex guard `[A-Za-z0-9_-]{22}\.html\.gz` (fullmatch) → `request_user.resolve_request_user(ctx)` (through the module, so the conftest patch reaches it) → `GraphFile.find_one(name, user_id)` → blob. Every failure raises `ResourceError`: invalid name, seam `RequestUserError` (message verbatim), `PyMongoError` from the seam or `find_one` (`STORAGE_UNAVAILABLE_MESSAGE`, logged with the traceback), and one "not found or expired" message. `GRAPHS_DIR` is no longer imported here. `DOWNLOAD_CONTRACT` reworded to start with "Read the linked …"; module, helper and resource docstrings now describe the Mongo-backed, user-scoped, expiring download.
- `apps/memory/src/tree/mcp/tools.py` — both visualize tools `await _graph_tool_result(..., user_id=user_id)`; both docstrings reworded.
- `apps/memory/src/tree/mcp/graph_tools.py` — `_dual_graph_result` is `async` and takes `*, user_id`; the three callers await it; three docstrings reworded.
- `apps/memory/tests/unit/mcp/test_viz_app.py` — file-branch and resource sections rewritten (async, against the session's REAL Beanie test DB so the `{name, user_id}` scoping is proven by Mongo; `GraphFile.insert` / `find_one` patched only for the `PyMongoError` cases).
- `apps/memory/tests/unit/mcp/test_graph_tools.py`, `apps/memory/tests/unit/mcp/test_tools.py` — the ctx builders set `transport = "streamable-http"` explicitly. File-branch tests now assert a token link, a row owned by the request user, and no file in `GRAPHS_DIR`.
- `apps/memory/tests/unit/memory/visualize/test_graph.py` — `_render_graph_file` writes exactly `render_graph_html(payload)` (the existing `</script>` test still covers the guard).

**Tests**
- Unit: 5083 passed, 0 failed (`make memory-tests`, env target local).
- Integration: N/A (no integration suite; live checks below).

**Acceptance criteria**
- [x] `render_graph_html` exists; `_render_graph_file` uses it: `test_graph.py::test_render_graph_file_writes_exactly_render_graph_html`.
- [x] One `GraphFile` row `{request user, 22-char token .html.gz, gzip(html), created_at}`, linked `graphs://<name>` `application/gzip`, contract + expiry from config: `test_viz_app.py::test_the_http_file_branch_stores_a_graph_file_and_links_it`, `::test_the_expiry_sentence_follows_the_configured_ttl` (300 s → "5 minutes", 90 s / 30 s → "1 minute"), `::test_every_render_gets_its_own_token`.
- [x] HTTP: no file, no browser, no path. stdio: file + open + path. All three answers carry the link and the expiry: `::test_the_http_file_branch_…`, `::test_the_stdio_file_branch_also_saves_and_opens_the_local_file`, `::test_a_stdio_browser_open_that_fails_answers_the_contract[open-returns-false|open-raises]`.
- [x] Failed insert → `storage_unavailable` (retryable), with no file, no browser and no link on either transport: `::test_a_failed_insert_answers_storage_unavailable[streamable-http|stdio]`.
- [x] The resource:
  - The owner reads the blob: `::test_graph_file_resource_serves_the_request_users_row`.
  - Another user's row and an unknown name answer the identical message: `::test_another_users_row_reads_exactly_like_an_unknown_one`.
  - 10 invalid names (incl. plain `.html`, a slug+stamp `.html.gz`, 21 chars, NUL, a trailing newline, traversal) are refused before any users read: `::test_graph_file_resource_rejects_names_it_never_handed_out`.
  - The seam's message reaches the reader verbatim: `::test_a_seam_failure_reaches_the_reader_with_the_seams_message`.
  - An unreachable store answers the storage message: `::test_an_unreachable_store_answers_the_storage_message[seam|find_one]`.
  - No filesystem path is read: `::test_graph_file_reads_no_filesystem_path`.
  - The in-process client gets the blob: `::test_a_client_reads_the_download_as_a_base64_gzip_blob`.
  - The message survives `mask_error_details=True`: `::test_the_not_found_message_survives_error_masking`.
- [x] `DOWNLOAD_CONTRACT` reworded and anchored in all six docstrings: `test_tools.py::…::test_the_docstring_states_the_download_contract` ×2, `test_graph_tools.py::test_every_graph_tool_docstring_states_the_download_contract` ×3, `test_viz_app.py::test_graph_file_docstring_states_the_download_contract`. The old sentence is gone from `src/tree/mcp`: `::test_no_mcp_source_still_carries_a_retired_download_sentence[…]`. The shell one-liner test still runs the contract's own command.
- [x] Live HTTP + stdio — evidence below.
- [x] `make memory-format-check`, `make memory-lint-check`, `make pre-commit`, `make memory-tests` green.

**Evidence**
```
$ make env-status
Env target: local (.env)
$ make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check
341 files already formatted
All checks passed!
$ make pre-commit
prettier / ruff check / ruff format / biome check (harness) ........ Passed
$ make memory-tests
======================= 5083 passed in 63.20s (0:01:03) ========================

# Live HTTP: FASTMCP_PORT=8765 make memory-serve-mcp TRANSPORT=streamable-http
# fastmcp.Client(StreamableHttpTransport("http://127.0.0.1:8765/mcp", headers={"horizon-actor-email": ...}))
[paul] visualize_memory_embeddings(as_html_file=true)  TOOL elapsed: 0.07s
TEXT: Embedding map: 372 of 372 chunks (the 14 most-recent of 14 documents) in 15 clusters (+1 noise). Since you asked for an HTML file, I rendered a self-contained interactive embedding map as a download. Read the linked `graphs://…html.gz` resource — an `application/gzip` blob: write its base64 `blob` to a file and run `base64 -d < blob.b64 | gunzip > <name>.html` (Python: `gzip.decompress(base64.b64decode(blob))`), then open the `.html` in a browser. The link expires in about 5 minutes.
LINK: graphs://iqiw0HW4GCBnnCPcv6sJjw.html.gz application/gzip
[paul] read_resource  READ elapsed: 0.01s, mime=application/gzip, base64 blob=65108 B, gzip=48830 B, html=239861 B
$ base64 -d < blob.b64 | gunzip > map.html
$ chrome --headless=new --use-angle=swiftshader --enable-unsafe-swiftshader --virtual-time-budget=15000 --dump-dom file://…/map.html | grep -o '<body[^>]*>'
<body data-layout="fixed">        # (with --disable-gpu Sigma's WebGL init never runs, so the attribute is never set)
$ mongosh … db.graph_files.find({}, {name:1, user_id:1, created_at:1})
{ user_id: ObjectId('6ac0f976f5896c3a79957359') /* paul */, name: 'iqiw0HW4GCBnnCPcv6sJjw.html.gz', created_at: ISODate('2026-10-05T17:59:21.608Z') }
[bob@example.com — signed up locally with --no-set-current]  McpError | Graph file 'iqiw0HW4GCBnnCPcv6sJjw.html.gz' not found or expired (download links expire after about 5 minutes) — run the visualize tool again.
[no header]  McpError | No horizon-actor-email header on this request. On Prefect Horizon the gateway adds it … (MISSING_HEADER_MESSAGE verbatim)
[paul again]  READ OK: 65108 B base64
$ find apps/memory/.tree/graphs -newer <marker written before server boot> -type f | wc -l
0                                 # newest file there: structure-20261005-174459.html (20:44:59 local, before the 20:59:21 call)

# Expiry: TREE_MCP__GRAPH_FILE_TTL_SECONDS=60 FASTMCP_PORT=8765 make memory-serve-mcp TRANSPORT=streamable-http
boot log: graph_files created_at_ttl: expireAfterSeconds 300 → 60
TEXT: … The link expires in about 1 minute.   LINK: graphs://WmAQA2wG8lkYWuSm7bYnxQ.html.gz   generated at 18:00:42Z
row count for WmAQA2wG8lkYWuSm7bYnxQ.html.gz = 0 at 18:02:01Z      # TTL monitor removed it after ~79 s
[paul]  McpError | Graph file 'WmAQA2wG8lkYWuSm7bYnxQ.html.gz' not found or expired (download links expire after about 1 minute) — run the visualize tool again.

# stdio: StdioTransport with .mcp.json "tree-memory-local" command/args/env, cwd = repo root (TREE_USER_IDENTIFIER from .env)
TOOL elapsed: 0.44s
TEXT: Embedding map: … Since you asked for an HTML file, I saved a self-contained interactive embedding map to:
/Users/…/apps/memory/.tree/graphs/embedding-map-20261005-180222.html
Opened it in your browser. The graphs:// link expires in about 5 minutes.
LINK: graphs://29ac-pYNObTPGHFKmTpQYQ.html.gz application/gzip
READ: mime=application/gzip html=239861 B
$ mongosh … created_at_ttl expireAfterSeconds → 300   # the stdio boot reconciled the TTL back to the default
```

**Notes**
- The expiry sentence uses the singular for N = 1 ("about 1 minute"). The AC wording ("about N minutes") holds for every N ≠ 1, including the default 300 s → "about 5 minutes".
- The resource also maps `PyMongoError` (from the seam's users read or from `find_one`) to `ResourceError(STORAGE_UNAVAILABLE_MESSAGE)`, logged with the traceback. Without this, a Mongo outage would reach the client as the masked "Error reading resource".
- The name guard is stricter than "ends in `.html.gz`": a pre-ADR-014 slug+stamp name like `alice-x.html.gz` is now "Invalid graph file name", not "not found". Those names are never minted any more.
- The tool-level tests write real `GraphFile` rows to the session test DB (dropped at session end) instead of patching `insert` / `find_one`. A patched `find_one` cannot prove the `user_id` filter.
- The stdio check drove the `.mcp.json` `tree-memory-local` command through `fastmcp`'s `StdioTransport`, not through the Claude Code UI. The browser really opened locally.
- Local DB side effects: one added user `bob@example.com` (signed up with `--no-set-current`, so the current-user session is unchanged). The short-TTL boot changed the `graph_files` TTL index to 60 s; the stdio boot set it back to 300 s (verified).
- Outside `src/tree/mcp`, no copy of the old sentence exists (whitespace-normalised grep over `.agents`, `docs`, READMEs). `.agents/skills/tree-memory/SKILL.md:68` still says a visual answer carries "a file path plus a `graphs://…html.gz` link" and tells the reader to share the path on `tree-memory-local`. That still holds on stdio, but the line does not mention the expiry. Rewording it is task 189's scope and was not touched here.

### [Tester] 2026-10-05 21:15 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check`, `memory-lint-check`, `pre-commit`)
- Unit tests: 5083 passed / 0 failed (`make memory-tests`, env target local)
- Integration tests: N/A (none in this repo)
- Warnings: 0 new

**E2E adversarial pass** (HTTP server on FASTMCP_PORT=8765, `fastmcp.Client` with `horizon-actor-email`)
- Happy path: `visualize_memory_structure(as_html_file=true)` and `visualize_memory_embeddings(as_html_file=true)` → text has contract + "expires in about 5 minutes", no path; link `graphs://<22 chars>.html.gz`; owner read = `application/gzip`, structure 31308 B base64 / 307650 B html, embeddings 65108 B base64 / 239861 B html; both ~0.03 s (PASS)
- Cross-user: bob@example.com reads paul's token → "Graph file '…' not found or expired (download links expire after about 5 minutes) …" (PASS)
- No header / empty header → MISSING_HEADER message; unknown identifier → "No user with identifier …" seam message, as `McpError` text (PASS)
- Hostile names: `..%2f..%2fetc%2fpasswd.html.gz`, `x.html`, 21- and 23-char tokens, NUL-prefixed, unicode, 5000-char, old slug+stamp `.html.gz`, `.HTML.GZ` → all "Invalid graph file name … (expected <name>.html.gz)"; well-formed unknown 22-char → "not found or expired"; raw `../x.html.gz`, empty name and a space are rejected at URI-template level by FastMCP before the resource runs (clean errors) (PASS)
- Concurrent generation (paul x2 + bob x1 in `asyncio.gather`): 2 distinct tokens, bob (no clustering run) got the normal "No clustering run" text, no crash (PASS)
- Disk: `find apps/memory/.tree/graphs -newer <marker>` = 0 new files over all HTTP calls; viz_app has no `GRAPHS_DIR`/`Path`/`read_bytes`; only `path.resolve()` remains in the stdio branch (PASS)
- Expiry (own run, `TREE_MCP__GRAPH_FILE_TTL_SECONDS=60`): text "about 1 minute"; read OK at 18:10:37, not found at 18:12:17 with "(… after about 1 minute)"; TTL index restored 60 → 300, verified in mongosh (PASS)
- stdio (`.mcp.json` tree-memory-local via StdioTransport): path `.tree/graphs/embedding-map-20261005-180751.html` + "Opened it in your browser. The graphs:// link expires in about 5 minutes." + link; read OK (PASS)
- graphrag tools (`search_memory`/`query_memory` `visualize=True`) are not registered on the local rag server (rag mode); covered by `test_graph_tools.py` lines ~257 and ~706 (row owned by request user + link), in the green suite (PASS, unit only)

**Acceptance criteria**
- [x] PASS — `render_graph_html` split — `graph.py`; `test_graph.py::test_render_graph_file_writes_exactly_render_graph_html`
- [x] PASS — row + link + contract + expiry from config — live above and `test_viz_app.py` file-branch tests
- [x] PASS — HTTP no file/browser/path; stdio file+browser+path; all answers carry link + expiry — live HTTP/stdio + tests
- [x] PASS — insert failure → `storage_unavailable` — tests on both transports (code read: `viz_app.py` `except PyMongoError`)
- [x] PASS — resource scoping, one not-found message, invalid-name, seam message, no filesystem — live + tests
- [x] PASS — DOWNLOAD_CONTRACT reworded, six docstrings, old sentence absent from `src/tree/mcp` and `.agents`/`docs` (grep empty)
- [x] PASS — live HTTP + stdio — independently reproduced
- [x] PASS — format / lint / pre-commit / tests green

**Other issues found (non-blocking)**
- `pymongo.errors.DocumentTooLarge` subclasses `bson.errors.InvalidDocument`, NOT `PyMongoError`, so a Graph file whose gzip exceeds 16 MB would escape `except PyMongoError` in `viz_app._graph_tool_result` as a generic tool error. Today's blobs are 30-50 KB gzip (the ADR's 11 MB html is ~3 MB gzip), so not reachable in practice; consider a follow-up or a one-line note.
- stdio: `render_graph_html` runs twice (once for the row, once inside `_render_graph_file`); and an OSError from the local write after a successful insert fails the whole tool though the link is already stored. Same failure mode as before; cosmetic.
- Singular "about 1 minute" deviates from the literal "about N minutes" AC only for N=1; judged fine. Stricter name check (old slug names -> "Invalid graph file name") matches the spec's Story 5; PyMongoError->ResourceError mapping on the resource is sound. Real-DB tests are a good choice (patched `find_one` cannot prove user scoping).
- My first server restart attempt silently failed to bind (stale server on 8765) yet reconciled the TTL index first; harmless, but note the boot reconciles the index before binding.

**VERDICT: PASS**
