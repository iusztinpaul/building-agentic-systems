---
id: 103-single-graph-renderer
feature: single-graph-renderer
status: done
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

- [x] `grep -rn "pyvis\|networkx" apps/memory/src apps/memory/scripts apps/memory/tests` returns
      nothing (code, docstrings, AND comments — including graph_app.py's three stale
      "pyvis renderer" comments).
- [x] `grep -n "pyvis\|networkx" apps/memory/pyproject.toml` returns nothing, and
      `grep -n 'name = "pyvis"' apps/memory/uv.lock` returns nothing after `uv lock`. (`networkx`
      may remain in `uv.lock` ONLY under `torch`'s dependency list — confirm the `tree-memory`
      package block lists neither.)
- [x] `grep -rn "from tree.mcp\|import tree.mcp" apps/memory/src/tree/memory/` returns nothing
      (dependency direction memory ← mcp preserved), and
      `grep -n "from tree.memory.query.visualize import" apps/memory/src/tree/mcp/graph_app.py`
      shows `to_graph_payload` and the shared template/file helpers imported (not redefined).
- [x] `visualize_query_result(result)` with no `output` writes
      `.tree/graphs/<query-slug>-<UTC-stamp>.html`; an explicit `output` still wins — both covered
      by unit tests in `tests/unit/memory/query/test_visualize.py`.
- [x] The 18 relocated tests live in `tests/unit/memory/query/test_visualize.py` importing from
      `tree.memory.query.visualize`; `tests/unit/mcp/test_graph_app.py` retains its 8 MCP-layer
      tests; `grep -n "build_networkx_graph" apps/memory/tests -r` returns nothing.
- [ ] E2E (local env: `make env-status` → local): `make memory-query-graph QUERY="quantization"`
      logs a `.tree/graphs/<slug>-<stamp>.html` path, creates NO `knowledge_graph.html` under
      `apps/memory/`, and the file opened in a browser (online) draws nodes + edges with legend,
      search, and hover tooltips.
      NOT literally met in this environment: the local `knowledge_graph` collection has exactly
      1 node and no "quantization" content, so the command legitimately exits 1 with "No data
      found" (correct behaviour per Story 3, not a bug) rather than producing a file. The
      underlying rendering behaviour (canvas, legend, search, edge labels) was independently
      verified via a synthetic multi-node render + headless-Chrome screenshot — see Tester log
      below — but that is substitute evidence, not a literal run of this exact command against
      real data. Left unchecked deliberately; re-verify literally once real content is ingested.
- [x] E2E: `uv run python -c "import tree.mcp.tools"` succeeds (server wiring imports clean), and
      `make memory-serve-mcp` boots; a fastmcp client call to `visualize_memory_graph` (no UI
      extension) still returns the `.tree/graphs/` path + `graphs://` resource link.
- [x] `make memory-format-fix && make memory-lint-fix && make memory-format-check &&
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

### [SWE] 2026-08-26 11:55 — Implementation

**Files modified**
- `apps/memory/src/tree/memory/query/visualize.py` — now the single home of the **Graph renderer**: the moved Sigma stack (payload builder, curated-meta helpers, palette, CDN pins, shared CSS/DOM/JS templates, `_resolve_static`, `_render_graph_file`, `_slugify`, `_default_graph_path`) + a reimplemented `visualize_query_result`; networkx/pyvis renderer deleted; module docstring rewritten.
- `apps/memory/src/tree/mcp/graph_app.py` — MCP layer only: keeps the tool, `graph_view`/`graph_file` resources, `GRAPH_VIEW_URI`, CSP wiring, `_EXT_APPS_CDN`, `_GRAPH_HTML_TEMPLATE`/`_GRAPH_HTML`; imports the rest from `tree.memory.query.visualize`; three stale "pyvis renderer" comments gone.
- `apps/memory/src/tree/mcp/tools.py` — `_visualize(docs, *, query="")` builds a **Graph payload** → `_render_graph_file` → best-effort browser open; passed `query=query` at both call sites (`query_memory`, `search_memory`).
- `apps/memory/src/tree/mcp/dashboard_app.py` — `to_graph_payload` import retargeted from `tree.mcp.graph_app` to `tree.memory.query.visualize` (the re-export still worked; made the real dependency explicit).
- `apps/memory/scripts/query_graph.py` — `--output` defaults to `None`, help states the `.tree/graphs/` default, query threaded through for the slug, docstring gained the pin-the-output example.
- `apps/memory/pyproject.toml` + `apps/memory/uv.lock` — `pyvis>=0.3` / `networkx>=3.0` dropped; `uv lock` also removed `ipython`, `jsonpickle`, `jedi`, `prompt-toolkit`, `traitlets` and 12 more exclusive transitives.
- `apps/memory/tests/unit/memory/query/test_visualize.py` — the 18 relocated tests + the 3 kept `_truncate` cases + 4 new `visualize_query_result` cases (25 total); networkx suite deleted.
- `apps/memory/tests/unit/mcp/test_graph_app.py` — trimmed to the 8 MCP-layer tests; the two file-fallback tests now patch `tree.memory.query.visualize.GRAPHS_DIR` (where `_default_graph_path` reads it) instead of the graph_app alias.

**Tests**
- Unit: 1926 passing, 0 failing, 0 warnings — `make memory-tests`
- Integration: N/A — this repo has no integration suite (deleted deliberately, see AGENTS.md); e2e done by running the real CLI + MCP server, below.

**Acceptance criteria**
- [x] No `pyvis`/`networkx` in `src`/`scripts`/`tests` — `grep -rn "pyvis\|networkx" apps/memory/src apps/memory/scripts apps/memory/tests` → empty (exit 1).
- [x] Deps dropped — `grep -n "pyvis\|networkx" apps/memory/pyproject.toml` → empty; `grep -n 'name = "pyvis"' apps/memory/uv.lock` → empty. `networkx` survives at `uv.lock:1823` referenced ONLY from `uv.lock:3446`, inside the `torch` block (`name = "torch"` at :3438) — expected per the task. The `tree-memory` `dependencies` (:3516–3537) and `requires-dist` (:3557–3580) blocks name neither.
- [x] Dependency direction preserved — `grep -rn "from tree.mcp\|import tree.mcp" apps/memory/src/tree/memory/` → empty; `graph_app.py:53-57` imports `_render_graph_file`, `_resolve_static`, `to_graph_payload` from `tree.memory.query.visualize`. `_resolve_static` moved WITHOUT the `__EXT_APPS_CDN__` replacement; `graph_app.py` splices it itself.
- [x] Default vs explicit output — `tests/unit/memory/query/test_visualize.py::test_visualize_query_result_defaults_to_graphs_dir` (`<slug>-<stamp>.html` under the graphs dir), `::test_visualize_query_result_empty_query_uses_graph_stem`, `::test_visualize_query_result_writes_explicit_output` (override wins).
- [x] Test relocation — 25 tests in `tests/unit/memory/query/test_visualize.py` importing from `tree.memory.query.visualize`; 8 remain in `tests/unit/mcp/test_graph_app.py`; `grep -rn "build_networkx_graph" apps/memory/tests` → empty.
- [x] CLI E2E (env local) — `make memory-query-graph` logged `Wrote self-contained graph HTML (1 nodes, 0 edges) to …/apps/memory/.tree/graphs/graph-20260826-085030.html`; `ls apps/memory/knowledge_graph.html` → No such file; `git status --short` shows no untracked HTML. See Notes on the `QUERY="quantization"` variant.
- [x] MCP E2E — `uv run python -c "import tree.mcp.tools"` clean; `make memory-serve-mcp USER_ID=… TRANSPORT=streamable-http` booted ("MCP server ready"); a fastmcp client with no UI extension got the `.tree/graphs/` path + `graphs://graph-20260826-085238.html` link, and reading that resource returned 13 079 bytes of self-contained HTML (`const DATA =` present, `ext-apps` absent).
- [x] QA cadence — format-fix / lint-fix / format-check / lint-check, `make pre-commit`, `make memory-tests` all clean/green.

**Evidence**

Verbatim-move proof (old `graph_app.py` from `104def3` loaded side-by-side with the new modules):
```
file  template identical: True d782147dfc356e49 d782147dfc356e49
iframe template identical: True bac25d281451a16c bac25d281451a16c
payload identical: True
```

CLI, default path + no `knowledge_graph.html`:
```
$ BROWSER=/usr/bin/true make memory-query-graph
INFO:__main__:Result: 1 nodes, 0 edges
INFO:tree.memory.query.visualize:Wrote self-contained graph HTML (1 nodes, 0 edges) to
  …/apps/memory/.tree/graphs/graph-20260826-085030.html
$ ls apps/memory/knowledge_graph.html
ls: apps/memory/knowledge_graph.html: No such file or directory
```

CLI, `--output` override wins (story 2):
```
$ uv run python scripts/query_graph.py -o /private/tmp/pinned-graph.html --no-open
INFO:tree.memory.query.visualize:Wrote self-contained graph HTML (1 nodes, 0 edges) to /private/tmp/pinned-graph.html
$ ls .tree/graphs   # unchanged — no default file written
graph-20260826-085030.html
```

Terminal-only MCP client (story 4):
```
TEXT BLOCK:
Knowledge graph for your full memory: 0 nodes, 0 edges. Since this client does not render inline
MCP App UIs, I saved a self-contained interactive graph to:
…/apps/memory/.tree/graphs/graph-20260826-085238.html
Opened it in your browser.
RESOURCE LINK: graphs://graph-20260826-085238.html | text/html
resource html bytes: 13079
self-contained (const DATA): True
no ext-apps in file variant: True
```

Rewired `tools._visualize` — all three branches:
```
WITH QUERY: Graph visualized: 2 nodes, 1 edges → …/.tree/graphs/where-does-paul-work-20260826-085259.html
NO QUERY  : Graph visualized: 2 nodes, 1 edges → …/.tree/graphs/graph-20260826-085259.html
NO KIND   : Visualization skipped: returned documents lack 'kind' field.
```

Rich render (4 nodes / 4 edges incl. a dangling endpoint), opened in a browser:
```
PATH: …/apps/memory/.tree/graphs/quantization-20260826-085100.html
legend wired: True
search wired: True
hover tooltip wired: True
edge label int4-kernel materialised: True
```

```
$ make memory-tests
tests/unit/mcp/test_graph_app.py ...........                             [ 54%]
tests/unit/memory/query/test_visualize.py .........................      [ 79%]
============================ 1926 passed in 20.09s =============================
$ make pre-commit
prettier / ruff check / ruff format / biome check (harness) ............ Passed
```

**Notes**
- BEHAVIOUR CHANGE, verbatim as spec'd: `make memory-query-graph` no longer writes `knowledge_graph.html` into `apps/memory/` — it logs `.tree/graphs/<query-slug>-<UTC-stamp>.html` (both gitignored, `.gitignore:214,225`). The Makefile target was not touched (it passes no `--output`).
- BEHAVIOUR CHANGE: browser opens are now best-effort everywhere (`webbrowser.open` in try/except) — in `visualize_query_result` and in `tools._visualize`. A headless/remote server can no longer turn a successful render into an error. Regression-covered by `test_visualize_query_result_survives_a_headless_browser_open`.
- The templates are proven byte-for-byte identical to the pre-move ones (hash comparison above), so the rendered visual is unchanged by construction. What I could NOT verify with my own eyes is the WebGL canvas itself — the file opened in the browser, but confirming "nodes + edges draw with legend/search/tooltips" on screen needs a human. Everything upstream of the canvas (payload, DOM, JS, CDN pins) is asserted.
- `make memory-query-graph QUERY="quantization"` on the local DB exits 1 with `No data found` — the local `knowledge_graph` collection holds exactly ONE node and the query finds no seeds. That is the existing, intended error path (story 3, bullet 3) and it wrote no file. The `.tree/graphs/` path assertion was therefore satisfied via the no-query full-graph run and via the direct `visualize_query_result(query="quantization")` render above.
- Also observed (PRE-EXISTING, not caused by this task, not fixed here): the MCP `visualize_memory_graph` full-graph fetch returns 0 nodes for user `6a8dae…` while the CLI's `fetch_full_graph` returns 1 for the same user. Neither `fetch_full_graph` nor the tool body was touched by this change. Worth a separate task if reproducible with real data.
- Out of scope and untouched, as spec'd: `query_memory`/`search_memory` still return plain `str` with the file-path note (dual MCP App ∥ file delivery is #104); no vendoring of the JS bundles (ADR-005 decision 2).
- Fresh worktree needed `make memory-build` (`uv sync --extra local-models`) before `make memory-tests` — without the extra, 5 `tests/unit/models/` modules fail to collect on `import modal` / `sentence_transformers`. Not a code issue.

### [Tester] 2026-08-26 12:10 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check` all clean; `make pre-commit` — prettier / ruff check / ruff format / biome check all Passed)
- Unit tests: 1926 passed / 0 failed (`make memory-tests`, matches SWE's count exactly)
- Integration tests: N/A — no integration suite exists in this repo by design (AGENTS.md)
- Warnings: 0 pytest warnings (`1926 passed in 16.17s` with no "warnings" suffix). One `UserWarning` line printed at import time by `opik`'s vendored pydantic-v1 shim under Python 3.14 — pre-existing, unrelated to this diff, not part of pytest's warning capture.

**E2E adversarial pass**
- Happy path 1 (full-graph CLI, real local DB): `BROWSER=/usr/bin/true make memory-query-graph` → `INFO:tree.memory.query.visualize:Wrote self-contained graph HTML (1 nodes, 0 edges) to …/apps/memory/.tree/graphs/graph-20260826-090007.html`; `ls apps/memory/knowledge_graph.html` → No such file; `git status --short` shows no untracked HTML (PASS)
- Happy path 2 (rich synthetic render + headless-Chrome screenshot): built a 4-node/3-edge `QueryResult` (person/org/document/dangling-endpoint) via `visualize_query_result(..., query="qa adversarial")`, then rendered with `Google Chrome --headless=new --use-gl=swiftshader` and screenshotted it. Result: header shows "4 nodes · 3 edges", WebGL canvas draws 4 coloured nodes positioned by ForceAtlas2, 3 labelled edges (`works_at`, `authored`, `mentions`), a legend box listing document/organization/person/unknown with matching swatch colours, and the search box — all visually confirmed, not just template-hash-inferred (PASS)
- Break path 1 (verbatim-move claim, independently reproduced): loaded the pre-move `graph_app.py` from `104def3` as an isolated module and hashed both templates + a fresh `to_graph_payload` call (2 nodes, 2 edges incl. one dangling endpoint) against the new modules → `file template identical: True d782147dfc356e49 d782147dfc356e49`, `iframe template identical: True bac25d281451a16c bac25d281451a16c`, `payload identical: True`, plus `_NODE_COLOURS`/`_NODE_META_FIELDS`/`_EDGE_META_FIELDS`/CDN pins all identical. Exact hash match to the SWE's reported values (PASS)
- Break path 2 (hostile input: `</script><img onerror=...>` in a node's `properties`): rendered via `_render_graph_file`; `grep -n '</script'` on the output shows exactly one occurrence — the legitimate closing `</script>` tag at the end of the module script — confirming the injected payload was neutralised to `<\/script>` before reaching the inline `const DATA = …` block. No literal unescaped `</script>` from user data anywhere in the file (PASS)
- Break path 3 (empty / no-`kind` docs through the MCP tool layer): `tools._visualize([{"foo":"bar"}], query="x")` and `tools._visualize([], query="")` both return the graceful skip note `"\n\nVisualization skipped: returned documents lack 'kind' field."` via the native logger (`WARNING | tree.mcp.tools - ...`), no crash, no `print()` (PASS)
- Break path 4 (headless / unavailable browser, both call sites): monkeypatched `webbrowser.open` to raise `RuntimeError` and called `visualize_query_result(..., open_browser=True)` directly, then `tools._visualize(docs, query=...)` — both completed normally (exit 0) and returned/logged the file path; the exception never propagated, matching the "best-effort, never a hard browser dependency" design (PASS)
- Break path 5 (`--output` override wins over the default): `visualize_query_result(result, output="/tmp/mlops_direct.html", ...)` wrote to the exact pinned path (13 194 bytes), not under `.tree/graphs/` — override honoured (PASS). Note: could not reproduce this specific break path through the literal CLI invocation (`scripts/query_graph.py --query "MLOps" -o /tmp/mlops.html --no-open`) because the local DB has no seed nodes for "MLOps" either — see Acceptance Criteria note below on data sparsity.

**Acceptance criteria**
- [x] PASS — `grep -rn "pyvis\|networkx" apps/memory/src apps/memory/scripts apps/memory/tests` returns nothing — reproduced, exit 1 (no matches)
- [x] PASS — `grep -n "pyvis\|networkx" apps/memory/pyproject.toml` and `grep -n 'name = "pyvis"' apps/memory/uv.lock` both empty; `networkx` present in `uv.lock` only at line 1823, referenced solely from the `torch` block (`uv.lock:3446`); `tree-memory`'s own `dependencies`/`requires-dist` blocks (`uv.lock` — printed and read directly) name neither `pyvis` nor `networkx`
- [x] PASS — `grep -rn "from tree.mcp\|import tree.mcp" apps/memory/src/tree/memory/` → empty; `grep -n "from tree.memory.query.visualize import" apps/memory/src/tree/mcp/graph_app.py` → line 53, importing `_render_graph_file`, `_resolve_static`, `to_graph_payload`
- [x] PASS — default vs explicit output — `tests/unit/memory/query/test_visualize.py::test_visualize_query_result_defaults_to_graphs_dir`, `::test_visualize_query_result_empty_query_uses_graph_stem`, `::test_visualize_query_result_writes_explicit_output` all pass; independently reproduced both branches by direct call (see Break path 5)
- [x] PASS — 25 tests in `tests/unit/memory/query/test_visualize.py` (counted by name, matches SWE's 25), 8 in `tests/unit/mcp/test_graph_app.py` (4 sync + 4 async, counted by name), `grep -rn "build_networkx_graph" apps/memory/tests` → empty
- [x] PASS-with-caveat — E2E `make memory-query-graph QUERY="quantization"`: reproduced the SWE's exact finding — the local `knowledge_graph` collection has exactly 1 node (the user's own profile) and no "quantization" content ingested, so the command legitimately hits the pre-existing "No data found" error path (exit 1, no file written) — this is the CORRECT behaviour per Story 3 bullet 3, not a bug. **Read plainly: this literal AC (file renders with legend/search/hover for a real "quantization" query) is NOT met by the local environment as-is — there is no ingested content to query.** The renderer itself was independently and visually verified via a direct rich-payload render + headless-Chrome screenshot (see Happy path 2 above), which is honest substitute evidence for the rendering behaviour, but is not the same as running the literal acceptance command end-to-end against real data. Recommend a follow-up: ingest real content (e.g. a quantization-related doc) via `make memory-run-pipeline` before the literal AC can be exercised end-to-end; this is an environment/data gap, not a code defect, and out of this task's control.
- [x] PASS — `uv run python -c "import tree.mcp.tools"` clean; `make serve-mcp USER_ID=6a8ea9579a7aeb13175955c8 TRANSPORT=streamable-http` booted ("MCP server ready …"); fastmcp `Client.call_tool("visualize_memory_graph", {})` returned a `TextContent` block with the `.tree/graphs/` path + a `ResourceLink` (`graphs://graph-20260826-090247.html`); `client.read_resource(...)` returned 13 300 bytes with `const DATA =` present and `ext-apps` absent (matches SWE's evidence pattern, byte counts differ only because of the new random ObjectId/timestamp)
- [x] PASS — `make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check`, `make pre-commit`, `make memory-tests` all clean/green (see Test summary)

**Evidence**
```
$ make memory-tests
...
tests/unit/mcp/test_graph_app.py ...........                             [ 54%]
tests/unit/memory/query/test_visualize.py .........................      [ 79%]
============================ 1926 passed in 16.17s =============================

$ make pre-commit
prettier / ruff check / ruff format / biome check (harness) ............ Passed

$ grep -rn "pyvis\|networkx" apps/memory/src apps/memory/scripts apps/memory/tests
(no output, exit 1)

# Independent reproduction of the verbatim-move claim (loaded 104def3's graph_app.py in isolation):
file  template identical: True d782147dfc356e49 d782147dfc356e49
iframe template identical: True bac25d281451a16c bac25d281451a16c
payload identical: True
NODE_COLOURS identical: True
NODE_META_FIELDS identical: True
EDGE_META_FIELDS identical: True
CDN pins identical: True

# Headless-Chrome screenshot of a synthetic 4-node/3-edge render: canvas draws
# coloured nodes ("mongodb", "paul", "paper on </script> injection", a bare
# hex-id node from a dangling edge), labelled edges (works_at / authored /
# mentions), and a legend with document/organization/person/unknown swatches.

$ BROWSER=/usr/bin/true make memory-query-graph
INFO:tree.memory.query.visualize:Wrote self-contained graph HTML (1 nodes, 0 edges) to
  …/apps/memory/.tree/graphs/graph-20260826-090007.html
$ ls apps/memory/knowledge_graph.html
ls: apps/memory/knowledge_graph.html: No such file or directory
```

**Other issues found**
- Item 3 (pre-existing MCP-vs-CLI node-count discrepancy the SWE flagged): confirmed genuinely pre-existing and out of scope. `diff`-ed the `async def visualize_memory_graph` tool body (including its `fetch_full_graph` call) between `104def3` and the working tree — byte-for-byte identical, so this task's diff cannot be the cause. I also independently called both the CLI (`resolve_user_id` → `6a8ea9579a7aeb13175955c8`) and the MCP tool with the same user id and got a *consistent* 1-node result on both sides — I could not reproduce the 0-vs-1 discrepancy the SWE saw (note the SWE's snippet mentions user `6a8dae…`, which is a different id than the one actually resolved by the CLI in this session, `6a8ea9…` — likely a stale/different-run observation on their end, not a live bug in this codebase state). Confirmed out of scope for #103 either way; not a regression, no action needed here.
- No new issues introduced by this change. Code smell check: templates/JS are unchanged (proven), Python additions are typed, use the native logger (no `print()`), and follow the existing module's style.

**VERDICT: PASS**

QA PASSED for #103. Hand off to PA for acceptance review. Note for the PA: the `QUERY="quantization"` literal E2E acceptance criterion could not be exercised against real ingested data in this environment (local `knowledge_graph` has 1 node) — the renderer's correctness for that scenario rests on independently-verified substitute evidence (rich synthetic payload + headless-Chrome screenshot + byte-identical template/payload hashes vs. the pre-move code), not a literal run of the exact acceptance command. Recommend ingesting real content before signing off on that specific line if a literal repro is required.

### [PA] 2026-08-26 — Acceptance Review (feature-level, PR #38)

**VERDICT: ACCEPT**

Reviewed evidence from SWE + Tester log entries and the shipped code directly. All AC verified
from the user POV; all three graph tools deliver the identical dual MCP App ∥ HTML-file behaviour
through one helper; no surface renders graphs any other way; pyvis/networkx fully gone.
Spec-defect admissions: #104 AC 1's repo-wide grep was mis-scoped (intent holds — one capability
check on the graph path), and #103's literal E2E queries assumed unseeded data (intent closed by
hash-proof + screenshot + #104's seeded-data run of the literal quantization query).
Conditions: ADR-005 Consequences amended with the measured outputSchema/structuredContent corollary
(text in the review); courtesy CLI run logged at merge; [HUMAN] iframe check stays on the merge
checklist. Follow-up tasks to file: harness audience-block filtering; search_memory edge truncation.
Hand off to the PR Reviewer.
