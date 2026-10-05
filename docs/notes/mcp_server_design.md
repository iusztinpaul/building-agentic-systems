# MCP server design — stateless visuals and request-scoped users on Prefect Horizon

Design note on why the `tree-memory` visualization download broke once the FastMCP server moved
from a local stdio process to Prefect Horizon, and the three changes that follow from it: graph files
in MongoDB with a TTL, a single-copy UI payload, and a per-request user identity.

Grounding: `apps/memory/src/tree/mcp/{viz_app,tools,graph_tools,dashboard_app,server}.py`,
ADR-005, ADR-007, ADR-013 §2–§3, FastMCP 3.2.0 source (`fastmcp/server/context.py`,
`fastmcp/server/server.py`), and the docs quoted inline (Horizon, FastMCP, MCP 2025-11-25, MCP Apps
2026-01-26).

---

## The symptom

A Codex client called `visualize_memory_embeddings(as_html_file=true)` against Horizon, got back a
server path (`/tmp/.tree/graphs/embedding-map-20261005-140316.html`) plus a `graphs://` resource link,
and failed twice on `read_mcp_resource(uri="graphs://embedding-map-20261005-140316.html")` with
`Error reading resource '…'` — FastMCP's masked error for an exception raised inside a resource
template (`fastmcp/server/server.py:1337`, `mask_error_details` on).

## Why it happened — the design assumed a local, single-process, single-user server

Every assumption below holds for `make memory-mcp` over stdio and breaks on Horizon.

### 1. Large bytes travel through MCP messages

- The file branch hands the client a server-side file to pull back through `resources/read`. On
  `main` that resource is the whole HTML as `text/html`: 11.4 MB for a 17,306-chunk map
  (ADR-013 context).
- Horizon caps every MCP message: "Request and response size | 6 MB each | MCP requests and
  responses through the gateway … There is no streaming; to move a large file, upload it to external
  object storage out of band and pass a URL or reference" (https://docs.horizon.prefect.io/limits).
  The cap comes from AWS Lambda's synchronous invocation limit; base64 and JSON overhead eat into it.
- The branch fix (`83aed58`, `b56bc7c`, `b488a88`) already brings the download to a ~2.4 MB gzip
  (~3.3 MB as base64) by gzipping on read, dropping derivable node fields and capping the map at the
  250 most-recent documents. Size is the problem that is close to solved.
- The MCP spec states no message size limit and no guidance for large binary resources
  (https://modelcontextprotocol.io/specification/2025-11-25/server/resources) — the limit is the
  host's, so the server must plan for the strictest host it deploys to.

### 2. A file written in one request is read in another

- The file branch writes `/tmp/.tree/graphs/<slug>-<stamp>.html` during `tools/call`, and
  `graph_file` (`viz_app.py:230`) reads it back during a LATER `resources/read`.
- Horizon gives no such guarantee: "Do not rely on files written during one request being available
  to another request", "No affinity guarantee | A later request may be handled by a different
  instance", local filesystem "Ephemeral"
  (https://docs.horizon.prefect.io/platform/compute-model, https://docs.horizon.prefect.io/limits).
- So the read can land on an instance whose `/tmp` never saw the file → `FileNotFoundError` →
  masked "Error reading resource". Gzip does not fix this; only moving the bytes off local disk does.
- This is the ONLY cross-request dependency in the visualization surface. The inline MCP App path is
  one request: the tool result carries the payload and the host pushes it into the iframe.

| Path | Requests that carry data | Breaks on |
|---|---|---|
| MCP App (`ui://tree-memory/graph.html`) | 1 — `tools/call` result | size only |
| File (`graphs://<name>.html.gz`) | 2 — `tools/call` writes, `resources/read` reads | size + instance affinity |

Tools on the file path: `visualize_memory_structure`, `visualize_memory_embeddings` (both modes),
`search_memory(visualize=True)` and `query_memory(visualize=True)` (graphrag), all through
`_graph_tool_result` (`viz_app.py:108`). `memory_dashboard` has no file path.

### 3. The UI payload is sent twice

- `_graph_tool_result` and `memory_dashboard` return the payload in a `content` JSON block marked
  `audience=["user"]` AND in `structured_content` (`viz_app.py:162`, `dashboard_app.py:122`).
- The `content` block is the channel that works: the custom iframe reads it via `ontoolresult`;
  `structured_content` was kept "for any host that forwards it too". Both copies count against the
  6 MB response cap, so the inline view hits the limit at roughly half the payload the file path does.

### 4. Which branch runs depends on per-session state

- The tool picks MCP App vs file with `ctx.client_supports_extension(UI_EXTENSION_ID)`, which reads
  the capabilities the client sent at `initialize` and FastMCP stored on the session
  (`fastmcp/server/context.py:579`: "Returns `False` when no session is available").
- Session state is in-memory per instance ("sessions are stored in memory on each server instance",
  https://gofastmcp.com/deployment/http). If `tools/call` reaches an instance that never saw the
  `initialize`, a UI-capable client (Claude Desktop) may be treated as text-only and pushed onto the
  fragile file path. **Unverified on Horizon** — one Claude Desktop call settles it.

### 5. One user is pinned at boot

- `server.py` resolved a CLI user flag or `TREE_USER_IDENTIFIER` ONCE at boot and every tool reads that
  `_SERVER_USER_ID` (`server.py:3`, `server.py:65`). Every caller of a deployment shares one memory.
- Locally that is one person, one process. On Horizon, with one shared bearer token, anyone holding
  the token reads, searches and ingests into the same user's memory, and graph files carry no owner.

---

## Idea 1 — graph files in MongoDB with a TTL

Fixes problem 2; keeps problem 1 solved by the existing gzip + cap.

- **Store:** a `graph_files` collection, one document per rendered file:
  `{user_id, name, html_gz: Binary, created_at}`. The ~2.4 MB gzip fits a plain document (16 MB
  BSON limit) — no GridFS.
- **Expire:** a TTL index on `created_at`, `expireAfterSeconds` configurable in YAML — 300 s while
  testing, 3600 s as the default. MongoDB's TTL monitor runs every ~60 s, so a file can outlive its
  TTL by about a minute; reads must still treat "not found" as expired.
- **Name:** flat and unguessable — `secrets.token_urlsafe(16)` + `.html.gz`, no user id or tool in
  the URI. Knowing the link is the capability; the TTL bounds its life.
- **Read:** `graphs://{name}` looks up `{name, user_id: <requesting user>}` and answers the stored
  gzip as the `application/gzip` blob it already returns. Another user's name → not found, same
  message as expired.
- **Drop:** `/tmp/.tree/graphs/` writes on the server path and the server-side path in the tool text
  (it is never reachable from a remote client). Local stdio keeps the best-effort `webbrowser.open`
  of a temp file only if we still want it — decide in planning.
- **Why not object storage + presigned URL:** a bucket is new infrastructure and a new credential;
  MongoDB is already the system of record and the bytes fit. Upgrade trigger: a gzip that approaches
  the 6 MB response cap → move to a bucket and return an `https://` `resource_link`, which the spec
  allows when "the client is able to fetch and load the resource directly from the web on its own".

## Idea 2 — send the UI payload once

Fixes problem 3.

- Drop `structured_content=payload` from `_graph_tool_result` and `memory_dashboard`; keep the
  `content` block with `audience=["user"]`, which the iframes already read first.
- Halves the inline response for every visual tool at no cost to the model (it only ever saw the
  summary).
- Risk: a host that forwards ONLY `structuredContent` to the iframe would now render empty. Verify on
  Claude Desktop before merging. Switching to `structuredContent`-only would match the MCP Apps spec
  ("optimized for UI rendering (not added to model context)") but contradicts what the iframe
  comment records about real hosts — don't flip it without a live test.

## Idea 3 — resolve the user on every request

Fixes problem 5 and gives Idea 1 its owner.

- **One seam:** `resolve_request_user(ctx) -> PydanticObjectId` replaces every read of the
  boot-pinned id (the `ContextVar` refactor `server.py:13` already anticipates). Visualization,
  search, query and ingest all scope by its result.
- **Input — a generic user identifier** (email or handle), looked up case-insensitively in
  `User.identifier`. Decided in planning (ADR-014): exactly one source per transport, no fallback.
  - HTTP: ONLY Horizon's `horizon-actor-email` header, which the gateway injects after authenticating
    an organization member (and strips when a client forges it). A local HTTP client sets it itself.
    An earlier draft used a custom client header; it was dropped because Horizon already supplies a
    verified identity.
  - stdio: ONLY `TREE_USER_IDENTIFIER` from `.env`.
  - Missing or unknown → `configuration_error`, never a default user.
- **Trust model:** verified on Horizon (the gateway's authenticated actor), a claim locally. Horizon
  authentication must stay enabled. See `mcp_server_auth.md`.
- **Long run:** public self-serve sign-up means our own OAuth (a FastMCP auth provider) with the
  seam reading the token's email instead of the header. Only the seam's body changes.

## Open checks before planning

1. Which exact `horizon-actor-email` value and case does Horizon send for our account?
2. Does a Claude Desktop call on Horizon get the inline view or the file? (problem 4)
3. Does the inline view render on Claude Desktop with `structured_content` removed? (Idea 2)
4. The real exception behind the masked "Error reading resource" in the Horizon server logs — expected
   `FileNotFoundError` (problem 2) or `ValueError` for a `.html` name on `main`.
