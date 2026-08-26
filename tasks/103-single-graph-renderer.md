---
id: 103-single-graph-renderer
feature: single-graph-renderer
status: pending
---

# Single graph renderer: move the Sigma stack into `visualize.py`, delete pyvis/networkx

Tags: `memory`, `mcp`, `cli`
Depends on: None
Blocks: #104
Implements: ADR-005 (`single-graph-renderer`)

## Scope

The repo has TWO knowledge-graph renderers: the browser-side graphology 0.26.0 + Sigma 3.0.3 +
graphology-layout-forceatlas2 0.10.1 stack embedded in `src/tree/mcp/graph_app.py`, and the Python
networkx + pyvis one in `src/tree/memory/query/visualize.py`. Keep the first, delete the second,
and make `visualize.py` the single home of the **Graph renderer** (glossary). ADR-005 records the
design; its settled choices (CDN not vendored; renderer in the memory domain; one output
convention) are NOT up for debate here. Use the glossary terms **Graph payload** and
**Graph renderer** in docstrings/comments you touch.

**1. Move from `graph_app.py` into `src/tree/memory/query/visualize.py`** (verbatim behaviour, no
redesign): `to_graph_payload`, `_curated_meta`, `_NODE_META_FIELDS`, `_EDGE_META_FIELDS`,
`_NODE_COLOURS` (the light-theme `Colours`-based palette — it takes over the name from the deleted
pyvis dark palette; update its now-stale "so visualize.py's dark-theme palette … is left untouched"
comment), `_FALLBACK_COLOUR`, `_render_graph_file`, `_slugify`, `_default_graph_path`,
`_GRAPH_STYLE`, `_BODY_MARKUP`, `_RENDER_JS`, `_FILE_HTML_TEMPLATE`, `_resolve_static`,
`_FILE_HTML_BASE`, and the three graph-lib CDN constants `_GRAPHOLOGY_CDN` / `_SIGMA_CDN` /
`_FA2_CDN`. Bring the imports they need (`json`, `re`, `datetime`/`UTC`, `GRAPHS_DIR`, `Colours`).
`visualize.py` ends ~500 lines — expected; do NOT create a new module or package. Rewrite its
module docstring (it currently claims networkx + pyvis).

**`_resolve_static` placement (settled):** it moves to `visualize.py` and DROPS the
`__EXT_APPS_CDN__` replacement (the ext-apps iframe runtime is an MCP-layer concern).
`graph_app.py` builds its iframe variant as
`_GRAPH_HTML = _resolve_static(_GRAPH_HTML_TEMPLATE, "760px").replace("__EXT_APPS_CDN__", _EXT_APPS_CDN)`.
Net effect: `visualize.py` imports NOTHING from `tree.mcp` — the existing dependency direction
(memory ← mcp, established by graph_app.py importing `_extract_display_name`/`_truncate`) is
preserved, never inverted.

**2. `graph_app.py` keeps** the `visualize_memory_graph` tool, the `graph_view` / `graph_file`
resources, `GRAPH_VIEW_URI`, the `AppConfig`/`ResourceCSP` wiring, `_EXT_APPS_CDN`, and
`_GRAPH_HTML_TEMPLATE`/`_GRAPH_HTML` (iframe variant). Replace the moved definitions with imports
from `tree.memory.query.visualize`. Fix the three stale comments referencing "the pyvis renderer"
(current lines 74, 101, 144).

**3. Delete from `visualize.py`:** `build_networkx_graph`, `render_html`, the dark-theme
`_NODE_COLOURS`, `_PYVIS_OPTIONS`, and the networkx/pyvis imports.

**4. `visualize_query_result` is reimplemented over the moved renderer** and keeps a
call-compatible signature: `visualize_query_result(result, output=None, *, open_browser=True,
query="") -> Path` — `output: str | Path | None` now defaults to `None`, meaning
`_default_graph_path(query)` (i.e. `.tree/graphs/<query-slug>-<UTC-stamp>.html`; empty query →
`graph-<stamp>.html`). Internals: `to_graph_payload(result)` → `_render_graph_file(...)`;
`open_browser=True` becomes BEST-EFFORT (`webbrowser.open` wrapped in try/except, mirroring
`visualize_memory_graph`'s fallback branch — it must never raise on a headless host).

**5. Rewire `tools.py`:** drop the `build_networkx_graph, render_html` import (line 46).
`_visualize` gains the query text — `_visualize(docs, query=query)` at BOTH call sites
(`query_memory` AND `search_memory`) — and internally builds the `QueryResult` as today, then
`payload = to_graph_payload(result)` → `path = _render_graph_file(payload, query=query)` →
best-effort browser open (try/except) → returns the same style of note string with node/edge counts
(now `len(payload["nodes"])` / `len(payload["edges"])`) + the path. Behaviour change vs today: the
file lands under `.tree/graphs/`, and a headless/remote server no longer risks a browser-open error.
This keeps #103 shippable on its own: both tools stay on the (new) file path; #104 then promotes
both to the dual MCP App ∥ file delivery.

**6. Rewire `scripts/query_graph.py`:** `--output` default changes from `"knowledge_graph.html"`
to `None` (drop `show_default`; the help text states the default:
"Defaults to .tree/graphs/<query-slug>-<UTC-stamp>.html under the repo root"). Pass the query
through for the slug: `visualize_query_result(result, output, open_browser=not no_open,
query=query or "")`. Update the module docstring's usage examples if they mention the old output.
BEHAVIOUR CHANGE to preserve verbatim in the log: `make memory-query-graph` stops writing
`knowledge_graph.html` into `apps/memory/` and instead logs the `.tree/graphs/` path
(`.tree/` and `*.html` are already gitignored — `.gitignore:214,225`). The Makefile target itself
needs no change (it passes no `--output`).

**7. Drop the dependencies:** remove `pyvis>=0.3` and `networkx>=3.0` from
`apps/memory/pyproject.toml` (lines 24–25) and refresh `uv.lock` (`uv lock` from `apps/memory/`).
KNOWN FACT: `networkx` REMAINS in `uv.lock` as a transitive of `torch` (uv.lock:3618) — that is
correct and expected; the win is `pyvis` + its exclusive transitives (`ipython`, `jsonpickle`)
leaving the image. Verify `tree-memory`'s own requires list in `uv.lock` names neither.

**8. Move the tests** (call the `/squid-testing-python` skill). Relocate from
`tests/unit/mcp/test_graph_app.py` to `tests/unit/memory/query/test_visualize.py` the 18 tests for
moved code: the payload-mapping suite (current lines 53–256), `test_render_graph_file_*` (:258,
:286, :511), `test_slugify_*` (:307, :313), `test_default_graph_path_is_unique_html_under_graphs_dir`
(:319) — imports retargeted to `tree.memory.query.visualize`. In `test_visualize.py`, DELETE the
networkx cases (the `build_networkx_graph` suite — its import breaks by design) but KEEP the
`_truncate` cases. `test_graph_app.py` keeps the MCP-layer tests (tool result channels :369–:465,
`graphs://` resource + unsafe-name rejection :467–:502,
`test_graph_html_reads_content_blocks_before_structured_content` :504). No coverage may be lost;
add a small `visualize_query_result` case (writes a file to `tmp_path`, returns its `Path`,
default-path branch hits `.tree/graphs/`). Do NOT invent a broad new suite.

## Acceptance criteria

- [ ] `grep -rn "pyvis\|networkx" apps/memory/src apps/memory/scripts apps/memory/tests` returns
      nothing (code, docstrings, AND comments — including graph_app.py's three stale
      "pyvis renderer" comments).
- [ ] `grep -n "pyvis\|networkx" apps/memory/pyproject.toml` returns nothing, and
      `grep -n 'name = "pyvis"' apps/memory/uv.lock` returns nothing after `uv lock`. (`networkx`
      may remain in `uv.lock` ONLY under `torch`'s dependency list — confirm the `tree-memory`
      package block lists neither.)
- [ ] `grep -rn "from tree.mcp\|import tree.mcp" apps/memory/src/tree/memory/` returns nothing
      (dependency direction memory ← mcp preserved), and
      `grep -n "from tree.memory.query.visualize import" apps/memory/src/tree/mcp/graph_app.py`
      shows `to_graph_payload` and the shared template/file helpers imported (not redefined).
- [ ] `visualize_query_result(result)` with no `output` writes
      `.tree/graphs/<query-slug>-<UTC-stamp>.html`; an explicit `output` still wins — both covered
      by unit tests in `tests/unit/memory/query/test_visualize.py`.
- [ ] The 18 relocated tests live in `tests/unit/memory/query/test_visualize.py` importing from
      `tree.memory.query.visualize`; `tests/unit/mcp/test_graph_app.py` retains its 8 MCP-layer
      tests; `grep -n "build_networkx_graph" apps/memory/tests -r` returns nothing.
- [ ] E2E (local env: `make env-status` → local): `make memory-query-graph QUERY="quantization"`
      logs a `.tree/graphs/<slug>-<stamp>.html` path, creates NO `knowledge_graph.html` under
      `apps/memory/`, and the file opened in a browser (online) draws nodes + edges with legend,
      search, and hover tooltips.
- [ ] E2E: `uv run python -c "import tree.mcp.tools"` succeeds (server wiring imports clean), and
      `make memory-serve-mcp` boots; a fastmcp client call to `visualize_memory_graph` (no UI
      extension) still returns the `.tree/graphs/` path + `graphs://` resource link.
- [ ] `make memory-format-fix && make memory-lint-fix && make memory-format-check &&
      make memory-lint-check`, `make pre-commit`, and `make memory-tests` all clean/green.

## User stories

### Story: Operator visualizes a query from the terminal
1. Operator runs `make memory-query-graph QUERY="quantization"` (local env, data already ingested
   for "Paul Iusztin").
2. The log shows `Wrote self-contained graph HTML (N nodes, M edges) to
   <repo>/.tree/graphs/quantization-<UTC-stamp>.html` and the browser opens it (best-effort).
3. The page renders the "Tree: Your Rooted Memory" Sigma view — coloured nodes, legend, search box,
   hover tooltips — identical to the `visualize_memory_graph` fallback file.
4. `ls apps/memory/knowledge_graph.html` → no such file; `git status` shows no new untracked HTML.

### Story: Operator pins the output location
1. Operator runs `uv run python scripts/query_graph.py --query "MLOps" -o /tmp/mlops.html --no-open`
   from `apps/memory/`.
2. The file is written to `/tmp/mlops.html` exactly (override wins over the `.tree/graphs/`
   default), no browser opens.

### Story: Full-graph render with no query
1. Operator runs `make memory-query-graph` (no QUERY).
2. The full graph for the current-session user is written to `.tree/graphs/graph-<UTC-stamp>.html`
   (empty-query slug falls back to `graph`) and renders.
3. With an empty database the CLI still exits 1 with the existing "No data found" error — no file
   written.

### Story: Terminal-only MCP client is unaffected by the move
1. A fastmcp client without the MCP Apps UI extension calls `visualize_memory_graph(query="MLOps")`
   against `make memory-serve-mcp`.
2. The result text carries the `.tree/graphs/` path and the `graphs://<name>` resource link, and
   reading that resource returns the self-contained HTML — byte-for-byte the same behaviour as
   before the move.

## Out of scope

- Any change to `query_memory`/`search_memory` tool RESULT shape — in this task they still return
  `str` with the file-path note; their dual MCP App ∥ HTML-file behaviour is #104.
- `mcp/dashboard_app.py` (draws no graph), docs/tutorials (verified: zero mentions of pyvis,
  networkx, or `knowledge_graph.html`), vendoring the JS bundles (ADR-005 settles on CDN).

## Log
