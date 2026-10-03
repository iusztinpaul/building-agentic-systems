---
id: 181-graphs-resource-gzip-blob
status: pending
feature: horizon-mcp-fixes
---

# `graphs://<name>.html.gz` — every rendered graph file downloads as a gzip MCP blob resource (one code path), and every tool tells the client how to decode it; live Horizon round trip

Tags: `mcp`, `viz`, `resources`, `horizon`, `docs`
Depends on: 179, 180 (the live size test needs the lean, capped payload; the code does not)
Blocks: —
Implements: ADR-013 §2 (amends ADR-005 Decision 4 / ADR-007 §7's `graphs://` text download)

## Problem

The embedding-map HTML (~11.4 MB) cannot be read through the `graphs://{name}` text resource on Horizon:
JSON-RPC -32603 after ~440 ms, no server traceback; the same file reads fine from the local server, and
0.06–0.7 MB structure views read fine on Horizon. gzip alone takes the data to ~2.4 MB (~3.3 MB once
base64-encoded as a blob). Code: `graph_file` + `_graph_tool_result` in
`apps/memory/src/tree/mcp/viz_app.py`; the tool docstrings in `tools.py` and `graph_tools.py` tell the
client to "read the resource and save its text".

## Scope

**Human decision (final):** the `graphs://` download becomes an MCP BLOB resource for ALL graph files —
gzip-compressed HTML, `mimeType: application/gzip` (the protocol base64-encodes blobs natively), URI
`graphs://<name>.html.gz`, ONE code path; the tool result text, tool docstrings and resource docstring give
the decode one-liner and stop saying "save its text"; keep the path-traversal guard. If a live Horizon
download still fails after tasks 179/180 + this, drop the cap to 100 (config change, evidence recorded).

1. **Resource (`viz_app.py`):** `@mcp.resource("graphs://{name}", mime_type="application/gzip") def
   graph_file(name: str) -> bytes` — `name` MUST end with `.html.gz`; strip the `.gz`, resolve under
   `GRAPHS_DIR`, keep today's guard (`path.parent == base`, suffix `.html`, `is_file()` else
   `FileNotFoundError`); return `gzip.compress(path.read_bytes())` (default level). FastMCP 3.2 turns
   `bytes` into `BlobResourceContents` (base64) with the declared mime type — verified in
   `fastmcp/resources/base.py`. Gzip on READ, no second file on disk. A plain `.html` name is rejected
   (`ValueError`) so there is exactly one way to download.
2. **Tool result (`_graph_tool_result` file branch):** the `ResourceLink` becomes `uri=f"graphs://{path.name}.gz"`,
   `name=f"{path.name}.gz"`, `mimeType="application/gzip"`, `description=f"gzip-compressed self-contained
   interactive {noun} — base64-decode the blob, gunzip, open the .html"`. The closing sentence for the
   remote case becomes the ONE decode contract, a module constant `DOWNLOAD_CONTRACT` (test anchor, same
   pattern as `ERROR_CONTRACT`): "If that path is not on your machine (remote server, e.g. Prefect Horizon),
   read the linked `graphs://…html.gz` resource — an `application/gzip` blob: write its base64 `blob` to a
   file and run `base64 -d blob.b64 | gunzip > <name>.html` (Python: `gzip.decompress(base64.b64decode(blob))`),
   then open the `.html` in a browser." The inline-iframe branch is untouched.
3. **Docstrings** (every "save its text" site — grep `save its text`): `viz_app.py` module docstring +
   `_graph_tool_result` + `graph_file`; `tools.py` `visualize_memory_structure` (and add the file/resource
   sentence to `visualize_memory_embeddings`, which has none today); `graph_tools.py` L166–168, 251–253,
   308–310. Each contains `DOWNLOAD_CONTRACT` verbatim modulo wrapping (asserted like `ERROR_CONTRACT`).
4. **Docs:** `tutorials/4_x_x_deploying_mcp_server.md` "Two Remote-Only Behaviours" → "Downloading
   visualizations" bullet rewritten (draft):
   > **Downloading visualizations.** With `as_html_file=true`, the HTML is written to the server's
   > `/tmp/.tree/graphs/`, which you can't reach. The result carries a `graphs://<name>.html.gz` resource
   > link: an `application/gzip` blob (base64 in the JSON-RPC frame). Read it, decode it —
   > `base64 -d blob.b64 | gunzip > map.html` — and open the file; it loads sigma, graphology and d3-force
   > from jsdelivr, so it needs internet to render. Whole-memory views are capped at the 250 most-recent
   > documents (`query.full_graph_max_docs`): the embedding map plots their chunks and says "N of M chunks"
   > in its header. Measured on 2026-10-0x: a <N>-chunk map is <X> MB as HTML, <Y> MB gzipped, and reads
   > from Horizon in <t> ms. (Before: an 11 MB text resource failed with JSON-RPC -32603.)
   `.agents/skills/tree-memory/SKILL.md:68` → "…on `tree-memory` (cloud) read the linked
   `graphs://…html.gz` blob, base64-decode + gunzip it to a local `.html` and open it."; `apps/memory/README.md`
   layout tree comment (`graphs://`) if it says text; `docs/glossary.md` new **Graph download** row +
   **Graph renderer** / **Embedding map** row edits (Part 2); ADR-005 / ADR-007 Status-line notes.
5. **Tests** (`tests/unit/mcp/test_viz_app.py`, `test_tools.py`, `test_graph_tools.py`): `graph_file("x.html.gz")`
   → `bytes` whose `gzip.decompress` equals the file; `x.html` → `ValueError`; `../x.html.gz` and
   `x.txt.gz` → `ValueError`; missing → `FileNotFoundError`; the registered resource template's mime type
   is `application/gzip`; reading through an in-process `fastmcp.Client(mcp)` yields `BlobResourceContents`
   with `mimeType == "application/gzip"` and a base64 `blob` that round-trips; the file branch's
   `ResourceLink` has the `.html.gz` uri/name and gzip mime; every listed docstring contains
   `DOWNLOAD_CONTRACT`; the string "save its text" appears nowhere under `src/tree/mcp` (source guard).
6. **Live verification, local** (LOCAL env): `make memory-serve-mcp TRANSPORT=streamable-http`, then a
   Python snippet with `fastmcp.Client("http://127.0.0.1:8000/mcp")`: call `visualize_memory_embeddings`
   with `as_html_file=true`, take the `resource_link` uri, `read_resource(uri)` → decode → write
   `map.html` → headless Chrome `--dump-dom` shows `<body data-layout="fixed"` and the header text; the same
   for `visualize_memory_structure(as_html_file=true)`. Record blob bytes and elapsed.
7. **[HUMAN] Live verification, Horizon** (post-merge — Horizon redeploys on push to `main`; the
   orchestrator schedules it): the same snippet with `tree.mcp.client.get_cloud_client()` against
   `tree-memory`: generate the map → read `graphs://embedding-map-<stamp>.html.gz` → decode → the HTML
   renders (headless `--dump-dom`). Evidence in the Log: blob size, elapsed, Horizon Traffic-Log status.
   **Fallback step:** if the read still fails (-32603 or a timeout), set `full_graph_max_docs: 100` in
   `default.yaml` (comment updated with the measurement), push, re-run, and record both measurements.
   **Placeholders:** the PR ships the tutorial sentence with the LOCAL measurements from step 6 (labelled
   "measured locally, 2026-10-0x"); the post-merge step replaces them with the Horizon numbers in a
   follow-up docs commit — no `<N>`-style placeholder reaches `main`.

## Acceptance criteria

- [ ] `graphs://{name}` serves `bytes` = gzip of the rendered `.html` with `mimeType: application/gzip`,
      only for names ending in `.html.gz` that resolve to a flat file under `GRAPHS_DIR`; traversal, other
      suffixes and plain `.html` names raise `ValueError`; a missing file raises `FileNotFoundError`.
- [ ] Reading the resource through a FastMCP client yields `BlobResourceContents` whose base64 `blob`,
      decoded and gunzipped, equals the file byte-for-byte.
- [ ] The file-branch `ToolResult` links `graphs://<stem>.html.gz` (name, uri, `application/gzip`) and its
      text contains `DOWNLOAD_CONTRACT`; the iframe branch is unchanged.
- [ ] `DOWNLOAD_CONTRACT` appears verbatim (modulo wrapping) in the five tool docstrings and the resource
      docstring; "save its text" appears nowhere in `src/tree/mcp`, the skill, or the tutorial.
- [ ] Tutorial bullet and skill line rewritten; glossary **Graph download** row added; ADR-005/007
      Status-line notes applied.
- [ ] `make memory-format-check`, `make memory-lint-check`, `make pre-commit`, `make memory-tests` green.
- [ ] Live (local): map and structure files round-trip through the resource and render; evidence in the Log.
- [ ] [HUMAN] Live (Horizon, post-merge): the embedding map round-trips (generate → read → decode → renders);
      evidence in the Log — or the cap is lowered to 100 with both measurements recorded.

## User Stories

### Story: The user downloads the map from the cloud server
1. In Claude Code against `tree-memory`, the user asks for "an HTML file of my memory map".
2. The model calls `visualize_memory_embeddings(as_html_file=true)`; the answer text ends with the decode contract and carries the link `graphs://embedding-map-20261003-181500.html.gz` (`application/gzip`).
3. The model reads the resource, writes the `blob` to `blob.b64`, runs `base64 -d blob.b64 | gunzip > embedding-map.html` and tells the user where the file is; the user opens it and sees the map.

### Story: The local user is not bothered with gzip
1. On `tree-memory-local` the same call answers the server path `.tree/graphs/embedding-map-….html` and "Opened it in your browser." — the user never touches the resource.

### Story: A client asks for the old text resource
1. A client reads `graphs://embedding-map-20261003-181500.html`.
2. The server answers an error `Invalid graph file name: … (expected <name>.html.gz)` — one download path.

### Story: The operator verifies Horizon after the deploy
1. After the merge, the operator runs the round-trip snippet against `tree-memory`.
2. Output: `blob 3.1 MB, read in 1.9 s, decoded 9.2 MB, <body data-layout="fixed">` and the Horizon Traffic Log shows the `resources/read` row green. The numbers go into the tutorial and the task Log.

---

Blocked by: 179, 180

## Log

### [PA] 2026-10-03 18:05 — Grooming

**Summary**
The `graphs://` download becomes a gzip blob (`<name>.html.gz`, `application/gzip`) for every rendered
graph file, compressed on read; the tool text, five docstrings, the skill and the tutorial carry one decode
contract; acceptance includes the live Horizon round trip with a cap-to-100 fallback.

**Key decisions**
- Gzip on read, no `.gz` file on disk, one resource template — least mechanism.
- The old `.html` name is rejected rather than served as text: two download paths would need two
  instructions.
- `DOWNLOAD_CONTRACT` is a test-anchored constant (the `ERROR_CONTRACT` pattern) so the five docstrings
  cannot drift.
- The Horizon check is `[HUMAN]`/post-merge by necessity (Horizon deploys from `main`); the task is done
  when the evidence — or the cap-to-100 fallback with evidence — is in the Log.

**Dependencies**
- 179, 180 — for the live size test, not for the code.

**User stories**
- 4 stories: cloud download, local user untouched, old text name rejected, the operator's Horizon check.

Ready for implementation.
