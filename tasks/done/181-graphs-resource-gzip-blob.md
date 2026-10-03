---
id: 181-graphs-resource-gzip-blob
status: done
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

- [x] `graphs://{name}` serves `bytes` = gzip of the rendered `.html` with `mimeType: application/gzip`,
      only for names ending in `.html.gz` that resolve to a flat file under `GRAPHS_DIR`; traversal, other
      suffixes and plain `.html` names raise `ValueError`; a missing file raises `FileNotFoundError`.
- [x] Reading the resource through a FastMCP client yields `BlobResourceContents` whose base64 `blob`,
      decoded and gunzipped, equals the file byte-for-byte.
- [x] The file-branch `ToolResult` links `graphs://<stem>.html.gz` (name, uri, `application/gzip`) and its
      text contains `DOWNLOAD_CONTRACT`; the iframe branch is unchanged.
- [x] `DOWNLOAD_CONTRACT` appears verbatim (modulo wrapping) in the five tool docstrings and the resource
      docstring; "save its text" appears nowhere in `src/tree/mcp`, the skill, or the tutorial.
- [x] Tutorial bullet and skill line rewritten; glossary **Graph download** row added; ADR-005/007
      Status-line notes applied.
- [x] `make memory-format-check`, `make memory-lint-check`, `make pre-commit`, `make memory-tests` green.
- [x] Live (local): map and structure files round-trip through the resource and render; evidence in the Log.
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

### [SWE] 2026-10-03 18:37 — Implementation

**Files modified**
- `apps/memory/src/tree/mcp/viz_app.py`: `GZIP_MIME` and `DOWNLOAD_CONTRACT` constants (the second is a test anchor, same pattern as `ERROR_CONTRACT`). `graph_file` serves only `<name>.html.gz`. It strips `.gz`, keeps the flat-file guard, and returns `ResourceResult([ResourceContent(gzip.compress(html), mime_type="application/gzip")])`. Compression happens on read; no second file is written. In the file branch the link is now `graphs://<name>.html.gz` / `application/gzip` with the new description, and the not-opened closing line ends with `DOWNLOAD_CONTRACT`. The module, `_graph_tool_result` and `graph_file` docstrings are rewritten. The iframe branch is untouched.
- `apps/memory/src/tree/mcp/tools.py`: `visualize_memory_structure` (rag) docstring now carries the contract. `visualize_memory_embeddings` gains the file/resource paragraph plus the contract.
- `apps/memory/src/tree/mcp/graph_tools.py`: docstrings of `visualize_memory_structure`, `query_memory` and `search_memory` carry the contract.
- `apps/memory/tests/unit/mcp/test_viz_app.py`: gzip read test (no `.gz` written to disk). Rejection cases: `x.html`, `../x.html.gz`, `../../../etc/passwd.gz`, `x.txt.gz`, `sub/dir.html.gz`, `.gz`, `x.html.gz.gz`. Missing file raises `FileNotFoundError`. The registered template's mime is gzip. An in-process `fastmcp.Client` reads the real registered template (added to a bare server, so no Mongo lifespan runs) and gets `BlobResourceContents` with `application/gzip` and a blob that round-trips byte for byte. Also covered: link uri/name/mime and the text ending in the contract; the browser-opened case gets no download text; the `graph_file` docstring contract; the contract's own shell one-liner run through `subprocess`; a source guard that no file under `src/tree/mcp` contains "save its text".
- `apps/memory/tests/unit/mcp/test_tools.py`, `test_graph_tools.py`: link regexes, names and mime now expect `.html.gz` / gzip; the text contains the contract; contract-in-docstring tests cover the five tools.
- `docs/glossary.md`: **Embedding map** Notes and **Graph renderer** Notes now point at the **Graph download** (`graphs://…html.gz`). In the **Graph download** row the one-liner is corrected to `base64 -d < blob.b64` (see Notes).
- `docs/adrs/005_…md`: Status note for Decision 4 added. `docs/adrs/007_…md`: Status merged with the existing 179/180 note into "amended by 013 §2–§3 (tasks 179–181)", plus the §7 gzip-blob sentence.
- `tutorials/4_x_x_deploying_mcp_server.md`: "Downloading visualizations" bullet rewritten with the local measurements. `.agents/skills/tree-memory/SKILL.md:68` rewritten.

**Tests**
- Unit: 4851 passing, 0 failing (`make memory-tests`, env-status local).
- Integration: N/A (the project has no integration suite).
- `make memory-format-fix`, `memory-lint-fix`, `memory-format-check`, `memory-lint-check` and `make pre-commit`: all pass.

**Acceptance criteria**
- [x] Resource: gzip blob, `.html.gz` only, guards in place. Verified by `test_viz_app.py::test_graph_file_resource_serves_the_gzip_of_the_rendered_html`, `::test_graph_file_resource_rejects_unsafe_and_non_gzip_names`, `::test_graph_file_resource_missing_file_raises` and `::test_the_registered_graphs_template_declares_application_gzip`.
- [x] Client read gives `BlobResourceContents` that round-trips. Verified by `test_viz_app.py::test_a_client_reads_the_download_as_a_base64_gzip_blob` and by the live run below.
- [x] File-branch link and contract. Verified by `test_viz_app.py::test_the_file_branch_links_the_gzip_download_and_states_the_contract`, `test_tools.py` (map and structure) and `test_graph_tools.py` (fallback tests).
- [x] Contract in 5 tool docstrings and the resource docstring; "save its text" nowhere. Verified by `test_tools.py::…::test_the_docstring_states_the_download_contract`, `test_graph_tools.py::test_every_graph_tool_docstring_states_the_download_contract`, `test_viz_app.py::test_graph_file_docstring_states_the_download_contract` and `::test_no_mcp_source_still_says_save_its_text`; `git grep "save its text"` finds nothing in `apps/memory/src`, `.agents` or `tutorials`.
- [x] Docs: tutorial, skill, glossary and ADR-005/007 Status notes.
- [x] QA: all make targets green.
- [x] Live (local): evidence below.
- [ ] [HUMAN] Live (Horizon, post-merge): **PENDING.** Horizon deploys from `main`, so this cannot run before merge. Run the same snippet against `get_cloud_client()` / `tree-memory` after the merge, record blob size, elapsed time and the Traffic-Log row, then replace the tutorial's local numbers with the Horizon ones. If the read still fails, lower the cap to 100 per Scope §7.

**Evidence: local live round trip** (LOCAL env, streamable-http on `127.0.0.1:8765`, through a real `fastmcp.Client("http://127.0.0.1:8765/mcp")`)

The server was started through a scratchpad wrapper that stubs `webbrowser.open` to `False` (no tabs opened; the product code is unchanged), and was stopped by PID afterwards.
```
[visualize_memory_embeddings] tool 0.04s  link=graphs://embedding-map-20261003-183103.html.gz mime=application/gzip
[visualize_memory_embeddings] text tail: ...`blob` to a file and run `base64 -d < blob.b64 | gunzip > <name>.html` (Python: `gzip.decompress(base64.b64decode(blob))`), then open the `.html` in a browser.
[visualize_memory_embeddings] BlobResourceContents mimeType=application/gzip blob(base64)=0.07 MB gzip=0.05 MB html=0.24 MB read=10 ms
[visualize_memory_embeddings] shell decode identical: True; python decode identical: True
[visualize_memory_structure] tool 0.02s  link=graphs://structure-20261003-183103.html.gz mime=application/gzip
[visualize_memory_structure] BlobResourceContents mimeType=application/gzip blob(base64)=0.03 MB gzip=0.02 MB html=0.31 MB read=7 ms
[visualize_memory_structure] shell decode identical: True; python decode identical: True
[old .html name] McpError: Error reading resource 'graphs://nope-20261003-181500.html': Invalid graph file name: 'nope-20261003-181500.html' (expected <name>.html.gz)

exact bytes:  embedding map html=239861 gzip=48830 b64=65108 | structure html=307650 gzip=23479 b64=31308

headless Chrome --dump-dom over the DECODED files:
  embedding map: <body data-layout="fixed">  #counts "372 of 372 chunks (the 14 most-recent of 14 documents) in 15 clusters (+1 noise)"
  structure:     <body data-layout="live" data-docs="14/14" data-sim="running">  #counts "14 of 14 documents · 413 nodes · 399 edges"
```
"Identical" here means byte-identical: the decoded HTML was compared with the server-side file the tool text names.

**Notes**
- **Deviation 1 (FastMCP behaviour; the task's premise was wrong):** a resource *template* that returns bare `bytes` reaches the client as `application/octet-stream`, not as the decorator's `mime_type`. In FastMCP 3.2.0, `FunctionResourceTemplate._read` → `convert_result` → `ResourceResult(raw)` drops the template mime; only static `Resource`s forward it, which is the case `base.py` covers. The in-process client test caught this. The fix is that `graph_file` returns an explicit `ResourceResult([ResourceContent(bytes, mime_type=GZIP_MIME)])`, while the decorator keeps `mime_type=GZIP_MIME` for listings. The protocol contract is unchanged (blob, `application/gzip`). AC 1's "serves `bytes`" now means the gzip bytes inside that `ResourceResult`. ADR-013 §2's parenthetical ("FastMCP base64-encodes bytes into BlobResourceContents" with the decorator mime) is inaccurate for templates; flag for PA (ADRs are read-only for SWE).
- **Deviation 2 (decode one-liner):** the groomed `base64 -d blob.b64 | gunzip > <name>.html` FAILS on macOS: `/usr/bin/base64` (BSD) prints `base64: invalid argument blob.b64` (it accepts only `-i file` or stdin). The contract therefore reads `base64 -d < blob.b64 | gunzip > <name>.html`, which works on both BSD and GNU. It changed in the constant, all docstrings, the glossary **Graph download** row, the skill and the tutorial. Regression test `test_the_contracts_shell_one_liner_decodes_the_blob` runs the contract's own command: it was red with the old wording ("invalid argument") and is green now. **ADR-013 §2 line 62 still says `base64 -d blob.b64`; PA should fix it in a rollup.**
- With the local browser open succeeding, the closing line stays "Opened it in your browser." and carries no contract (Story 2; Scope §2 says the contract is "the closing sentence for the remote case"). This is pinned by `test_a_local_browser_open_needs_no_download_instructions`. AC 3's "text contains `DOWNLOAD_CONTRACT`" holds whenever the open fails, which is always the case on Horizon (headless).
- The local memory is small (14 documents, 372 chunks), so the tutorial's local numbers are small too. The Horizon follow-up should replace them with the real ~17k-point numbers.
- `apps/memory/README.md:840` says only "ui:// + graphs://" and does not mention text, so it is left unchanged.


### [Tester] 2026-10-03 21:55 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check && make memory-lint-check && make pre-commit`, exit 0)
- Unit tests: 4851 passed / 0 failed (`make memory-tests`, env-status local; no integration suite in this project)
- Warnings: 0

**E2E adversarial pass** (real `fastmcp.Client` over streamable-http against a real server on 127.0.0.1:8791, started via the SWE's `serve_no_browser.py` with `TREE_WORKING_DIR` pointed at a scratchpad dir so the user's `.tree/` and browser were untouched; stopped by PID)
- Happy path: `roundtrip.py` → both tools return `graphs://…html.gz` link (`application/gzip`), text ends with DOWNLOAD_CONTRACT, `BlobResourceContents mimeType=application/gzip`, shell + Python decode byte-identical to server file; template listed as `('graphs://{name}', 'application/gzip')`. PASS
- Traversal/hostile: `graphs://../secret.html.gz`, `%2e%2e/…`, `///etc/hosts.gz`, `sub/ok-1.html.gz` → "Unknown resource" (template doesn't match slashes); `..%2Fsecret.html.gz`, `%2e%2e%2fsecret.html.gz`, `%2Fetc%2Fhosts.gz`, `ok-1.html.gz%00`, `ok-1.html.gz#frag`, `OK-1.HTML.GZ` → "Invalid graph file name … (expected <name>.html.gz)". Symlink in graphs dir → /etc/hosts and → a file outside the dir: rejected (resolve() escapes base). Secret outside the dir never returned. PASS
- Suffix variants: `ok-1.html` (plain), `ok-1.html.gz.gz`, `.gz`, `.html.gz` → ValueError msg; on-disk literal `dotfile.html.gz` requested as `.html.gz` → FileNotFoundError msg, as `.html.gz.gz` → ValueError; missing → FileNotFoundError msg. PASS
- Names with spaces/unicode (percent-encoded `my%20graph%201.html.gz`, `café-日本.html.gz`, encoded form): served, decode identical. Raw space in URI is rejected client-side by fastmcp ("Provided resource URI is invalid") — fine. Empty (0 byte) file → valid gzip blob, round-trips. PASS
- Large input: 10.08 MB compressible HTML → blob 66 KB b64, 24 ms, identical; 12 MB incompressible random → blob 16.0 MB b64, 351 ms, identical. PASS
- Concurrency: 16 concurrent reads on one client (8 big + 8 small) all OK in 0.20 s; 4 parallel clients × 12 MB all OK. Server RSS plateaued ~860–930 MB over 45 sequential 12 MB reads (no unbounded growth). PASS
- One-liner on macOS (this machine, BSD `/usr/bin/base64`): `base64 -d < blob.b64 | gunzip > x.html` byte-identical for 10 MB, 12 MB and tiny blobs (single-line, no trailing newline). The groomed `base64 -d blob.b64` form exits 1 ("invalid argument") on macOS → the SWE's stdin form is justified. GNU (`gbase64` from coreutils): both stdin and file forms identical. PASS
- Inline `ui://` iframe branch: not edited (diff of viz_app.py touches only the file branch, docstrings and `graph_file`); covered by the green suite. PASS
- Graphrag-mode tools: `test_graph_tools.py` asserts `.html.gz` links + contract (unit level; covered in 4851). PASS

**Acceptance criteria**
- [x] PASS — resource serves gzip bytes, `.html.gz` only, flat file under GRAPHS_DIR; traversal/other suffix/plain `.html` → ValueError, missing → FileNotFoundError. Evidence: live adversarial table above; `test_viz_app.py::test_graph_file_resource_*`. (Bytes travel inside an explicit `ResourceResult` — orchestrator-accepted deviation.)
- [x] PASS — client read yields `BlobResourceContents`, base64 blob gunzips byte-identical. Evidence: live (above) + `test_a_client_reads_the_download_as_a_base64_gzip_blob`.
- [x] PASS — file-branch link `graphs://<stem>.html.gz`/name/`application/gzip`, text carries DOWNLOAD_CONTRACT (remote case); iframe branch unchanged. Note: when the local browser opens, text is "Opened it in your browser." with no contract (Story 2, Scope §2 "remote case"; pinned by test).
- [x] PASS — DOWNLOAD_CONTRACT in five tool docstrings + `graph_file`; `git grep "save its text"` hits only the two guard tests; none in src/skill/tutorial.
- [x] PASS — tutorial bullet (local measurements, no `<N>` placeholders), skill line, glossary Graph download row, ADR-005/007 Status notes present in diff.
- [x] PASS — format/lint/pre-commit/tests green.
- [x] PASS — local live round trip: reproduced independently (embedding map 239,861→gzip→identical; structure identical).
- [ ] [HUMAN] Horizon post-merge — honestly marked PENDING in SWE log and the box is unchecked; nothing faked. Not verifiable here.

**Other issues found (non-blocking)**
- `graphs://ok-1%00.html.gz` (NUL mid-name) surfaces the raw OS-level message "lstat: embedded null character in path" instead of the "Invalid graph file name" text. Still an error (no crash, no leak of data); pre-existing behaviour of `Path.resolve()`. Optional hardening: reject names containing `\x00` up front.
- Docstring wrapping in `tools.py` (both new paragraphs): "If the path exists locally just share it. If that path is not on your machine (remote server, e.g. Prefect" is a very long line — cosmetic only; verbatim-modulo-wrapping assertion still passes.
- Symlink inside the graphs dir pointing to another file in the same dir is served (alias.html); harmless, files are server-written.
- Claude Code's own `ReadMcpResourceTool` rendering of blob contents is not testable here; the Horizon/Claude Code read stays part of the HUMAN post-deploy step. ADR-013 §2 wording fix deferred to commit per orchestrator.

**VERDICT: PASS**

### [SWE] 2026-10-03 22:10 — Post-Tester fixes

**Files modified**
- `docs/adrs/013_horizon_scale_mcp_surface.md` §2: two corrections, made at the orchestrator's instruction. (1) The resource answers an explicit `ResourceResult([ResourceContent(bytes, mime_type="application/gzip")])`, because a FastMCP 3.2.0 template drops the decorator mime for a bare `bytes` return. (2) The one-liner is now the stdin form `base64 -d < blob.b64 | gunzip > <name>.html`. Nothing else in the ADR changed.
- `apps/memory/src/tree/mcp/viz_app.py`: `graph_file` rejects a name containing `\x00` up front, raising the guard's `Invalid graph file name … (expected <name>.html.gz)` instead of the OS-level "embedded null character".
- `apps/memory/tests/unit/mcp/test_viz_app.py`: `alice-x\x00.html.gz` added to the rejection parametrize. It failed with the guard removed ("lstat: embedded null character in path") and passes with it in place.
- `apps/memory/src/tree/mcp/tools.py` (2 sites) and `graph_tools.py` (3 sites): re-wrapped the 120-char docstring line the Tester flagged. The remaining >88-char lines in those files predate this task.

**Tests**
- Unit: 4852 passed, 0 failed (`make memory-tests`, env local).
- `make memory-format-check`, `memory-lint-check` and `pre-commit`: all pass.

**Notes**
- [HUMAN] Horizon AC stays pending post-merge.

### [PA] 2026-10-04 00:35 — Acceptance Review

**VERDICT: ACCEPT**

Reviewed from the user's POV in the feature-level acceptance of PR #45 (first pass REJECTed on
documentation discipline only — ADR-013 §1/§5/§7 did not record the mid-pipeline decisions; resolved by
rollup `tasks/done/184-pa-rejection-horizon-mcp-fixes.md`, commit 7fbe460). Evidence from the Tester
log entries above; every automated acceptance criterion verified against the user-visible surface
(tool text, docstrings, README, skill, tutorial, glossary). The `[HUMAN]` post-merge Horizon checks, where
present, stay open and are listed in the PR body. Hand off to the PR Reviewer.
