---
id: 187-graphs-resource-from-mongodb
status: pending
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

- [ ] `render_graph_html(payload)` exists in `tree.memory.visualize.graph`; `_render_graph_file` uses it
      and still writes `<slug>-<stamp>.html` for the CLI.
- [ ] The file branch inserts one `GraphFile` `{user_id = request user, name = <22-char token>.html.gz,
      html_gz = gzip(html), created_at}` and links `graphs://<name>` (`application/gzip`); the text contains
      `DOWNLOAD_CONTRACT` and "expires in about N minutes" with N from `mcp.graph_file_ttl_seconds`.
- [ ] On a non-stdio transport the file branch writes no file, opens no browser and names no server path;
      on stdio it ALSO writes `.tree/graphs/<slug>-<stamp>.html`, opens it best-effort and names the path;
      every file-branch answer (HTTP, stdio opened, stdio not opened) carries the link AND the expiry
      sentence.
- [ ] A failed insert answers `storage_unavailable` (retryable).
- [ ] `graphs://{name}` resolves the request user, reads `{name, user_id}` and answers the stored gzip as an
      `application/gzip` blob; unknown name, another user's row and an expired row answer ONE
      `ResourceError` "not found or expired" message; an invalid name shape (incl. plain `.html`) answers
      `ResourceError` "Invalid graph file name"; a seam failure answers `ResourceError` with the seam's
      message; the resource reads no filesystem path.
- [ ] `DOWNLOAD_CONTRACT` is reworded (no "path on your machine" preamble) and present verbatim (modulo
      wrapping) in the five tool docstrings and the resource docstring; the old sentence is gone from
      `src/tree/mcp`.
- [ ] Live (local, HTTP): generate → read → decode renders; a second user cannot read it; it expires;
      `.tree/graphs/` untouched. Live (stdio): path + browser + link. Evidence in the Log.
- [ ] `make memory-format-check`, `make memory-lint-check`, `make pre-commit`, `make memory-tests` green.

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
