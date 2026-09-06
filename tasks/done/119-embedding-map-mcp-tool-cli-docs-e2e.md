---
id: 119-embedding-map-mcp-tool-cli-docs-e2e
feature: embedding-clusters-viz
status: done
---

# `visualize_memory_embeddings` MCP tool (both modes) + `make memory-visualize-embeddings` CLI + docs/skills + live e2e

Tags: `mcp`, `scripts`, `docs`, `e2e`
Depends on: #117, #118
Blocks: —
Implements: ADR-007 — Decision 8 (surfaces + warning contract)

## Scope

**1. MCP tool** — in `tree/mcp/tools.py` (registered in BOTH modes; READS only, never computes):
```python
@mcp.tool(app=AppConfig(resource_uri=GRAPH_VIEW_URI))
@track(tags=TAGS_RETRIEVAL_MCP, name="visualize_memory_embeddings", create_duplicate_root_span=False)
async def visualize_memory_embeddings(ctx: Context, hulls: bool = False, as_html_file: bool = False) -> str | ToolResult
```
Docstring (model-facing): "Show the memory's embedding space as a 2D map: every child chunk is a
point, coloured by its cluster from the latest clustering run, with an LLM-written label per
cluster. Use when the user wants to *see* what topics the memory holds or how it is organised.
`hulls=true` outlines each cluster. If no clustering run exists, this returns a message telling
the operator which command to run; if the map is stale, the answer starts with a warning line."
Body: `_set_retrieval_thread(ctx, "visualize_memory_embeddings")` → `load_embedding_map(lc["client"],
lc["database"], lc["user_id"])`; `None` → return `NO_CLUSTERING_RUN_MESSAGE` (plain `str`); else
`payload = to_embedding_map_payload(map, hulls=hulls)`; `summary = payload["summary"]`, prefixed by
`payload["warning"] + "\n"` when present; `return _graph_tool_result(ctx, payload, summary,
query="embedding-map", as_html_file=as_html_file)` (dual delivery identical to the graph).
`tools.py` imports `GRAPH_VIEW_URI, _graph_tool_result` from `tree.mcp.viz_app` (this registers
the `ui://` + `graphs://` resources in rag mode too). Rag mode must still NOT import
`tree.mcp.graph_tools` / `dashboard_app`, nor `umap`/`sklearn`. `mcp.instructions` (both texts)
mention the tool in one sentence. `viz_app.graph_view`'s docstring → "Interactive Sigma.js viewer
for knowledge graphs and embedding maps (read-only)".

**2. CLI (glue only)** — `scripts/visualize_embeddings.py` (`init_logger()`, `user_options`,
`--hulls/--no-hulls`, `--output/-o`, `--no-open`): connects (`init_mongodb`), resolves the user,
`load_embedding_map`; `None` → `click.echo(NO_CLUSTERING_RUN_MESSAGE)` + exit 1; else prints the
warning line FIRST when present, then `render_embedding_map_file(payload, output)` and best-effort
`webbrowser.open` unless `--no-open`, prints `Wrote <path>` and the summary line.
`apps/memory/Makefile`: `visualize-embeddings` target (`HULLS=true`, `OUTPUT=…`, `USER_ID` /
`USER_IDENTIFIER`).

**3. Docs & skills:**
- `apps/memory/README.md`: "Embedding map" subsection under Query CLI (command, what it shows,
  the warning contract, the no-run message), "Tools exposed" tables → both modes 7 / graphrag 14
  with `visualize_memory_embeddings` described, Layout block (`clustering/`, `visualize/`,
  `mcp/viz_app.py`), "Memory clustering" section cross-link.
- Repo-root `README.md` pipeline comment gains `→ (optional) cluster child embeddings → embedding map`.
- `.agents/skills/tree-memory/SKILL.md`: tool table (both modes) + "Visualization" section: use
  `visualize_memory_embeddings` for "what topics are in my memory / show me a map" in EITHER
  mode; graph visualisation stays graphrag-only; relay the warning line verbatim and the
  `make memory-run-clustering-pipeline` hint.
- `.agents/skills/run-pipelines-e2e/SKILL.md`: step 2 adds `make memory-run-clustering-pipeline`
  (after indexing; note the ~40 s cold numba import on a fresh env, and
  `TREE_MEMORY__CLUSTERING__HDBSCAN__MIN_CLUSTER_SIZE=5` for a small local corpus); step 3 adds
  `make memory-visualize-embeddings` + the MCP tool, and the stale-warning check.
- `docs/notes/prefect-execution-topologies.md`: the four phases of `offline-pipeline`.
- Verify code identifiers match the glossary (**Memory cluster**, **Embedding map**,
  **Clustering run**, **Offline phase**): `MemoryCluster`, `memory_clusters`, `EmbeddingMap`,
  `run_id`, `run_clustering`.

**4. Live e2e (per `.agents/skills/run-pipelines-e2e/SKILL.md`, local env, "Paul Iusztin" user,
served FROM THIS WORKTREE, evidence in `## Log`)** — in ONE mode (rag; clustering is
mode-orthogonal) plus a graphrag smoke of the tool count:
1. Ingest enough for ≥ 2 clusters (e.g. `make memory-run-pipeline SOURCE_FILE=sources/listen.yaml`
   or 4–5 `MODE=online` articles) — or export `TREE_MEMORY__CLUSTERING__HDBSCAN__MIN_CLUSTER_SIZE=5`
   in the serving shell; record which.
2. `make memory-run-clustering-pipeline` → paste the summary log line and `mongosh` output:
   `memory_clusters` rows (`label`, `size`, `keywords`) and one child row's `cluster_id`/`viz`.
3. `make memory-visualize-embeddings HULLS=true` → file path + screenshot; no warning line.
4. `make memory-serve-mcp TRANSPORT=streamable-http` → `uv run fastmcp call … visualize_memory_embeddings hulls=true` → paste the tool result (file path + `graphs://` resource link; the summary text).
5. Ingest ONE new document (`make memory-run-pipeline MODE=online SOURCE=<url>`), re-run steps 3
   and 4 → both outputs START with `N of M chunks have no cluster assignment (or a stale one) —
   run make memory-run-clustering-pipeline`; the legend shows the unclustered row.
6. Drop `memory_clusters` and clear the chunk fields (`db.memory.updateMany({}, {$unset:
   {cluster_id: "", viz: ""}})`) → CLI exits 1 with the no-run message; tool returns the same text.
7. graphrag: boot the MCP server without the override → `fastmcp list` shows 14 tools incl.
   `visualize_memory_embeddings`.

## Acceptance Criteria

- [x] `test_tool_gating.py`: rag registers exactly the 7 tools `ingest_conversation, ingest_file, ingest_url, scrape_web, search_memory, search_web, visualize_memory_embeddings`; graphrag registers 14; rag's `sys.modules` contains `tree.mcp.viz_app` but none of `tree.mcp.graph_tools`, `tree.mcp.dashboard_app`, `umap`, `sklearn`; the tool's schema properties are exactly `{hulls, as_html_file}`.
- [x] Both mode instruction texts mention `visualize_memory_embeddings`; the rag text still does not mention `query_memory` / `deep_search_memory`.
- [x] Unit tests (`test_tools.py`): with `load_embedding_map` patched to `None` the tool returns `NO_CLUSTERING_RUN_MESSAGE` as a plain `str` and `_graph_tool_result` is NOT called; with a map (`unclustered=0`) and a UI-capable ctx the result is a `ToolResult` whose model-visible text starts with `Embedding map:` and whose `audience=["user"]` block JSON has `layout == "fixed"` and `hulls == True` when `hulls=True`; with `unclustered=2` the model-visible text STARTS with `2 of 12 chunks have no cluster assignment`; with a non-UI ctx the result carries a `graphs://embedding-map-…html` resource link; `as_html_file=True` forces the file branch.
- [x] `tests/unit/scripts/test_visualize_embeddings.py` (pattern of `test_query_graph.py`): no run → prints `NO_CLUSTERING_RUN_MESSAGE`, exit code 1, no file written; a run with `unclustered=3` → the FIRST stdout line is the warning, `render_embedding_map_file` is called with `hulls` forwarded, `--output` honoured, `--no-open` skips `webbrowser.open`.
- [x] `grep -n "visualize-embeddings" apps/memory/Makefile` shows the target with `HULLS`/`OUTPUT` wiring; `make memory-help` lists it.
- [x] `apps/memory/README.md` contains `visualize_memory_embeddings`, the strings `7 tools` and `14 total` (or the tables' equivalents), an `Embedding map` heading, and `have no cluster assignment`; `.agents/skills/tree-memory/SKILL.md` lists the tool under both modes; `.agents/skills/run-pipelines-e2e/SKILL.md` mentions `run-clustering-pipeline` and `visualize-embeddings`.
- [x] `grep -rn "graph_app" apps/memory .agents docs/glossary.md README.md` returns nothing (ADR prose excluded).
- [x] `make memory-format-check && make memory-lint-check && make memory-tests` green; test count ≥ post-#118.
- [x] [HUMAN] e2e evidence steps 1–4 in `## Log`: clustering summary line; `mongosh` `memory_clusters` rows with real labels; the CLI file path + screenshot with hulls; the MCP tool result text.
- [x] [HUMAN] e2e evidence step 5: CLI and MCP outputs both beginning with the warning line after ingesting one new document; legend shows the unclustered count.
- [x] [HUMAN] e2e evidence steps 6–7: the no-run message from both surfaces; graphrag `fastmcp list` → 14 tools.

## User Stories

### Story: Reader asks the assistant what their memory is about
1. In Claude Code (rag mode server), the user asks "show me a map of what's in my memory".
2. The `tree-memory` skill picks `visualize_memory_embeddings`; the client cannot render MCP Apps, so the answer is `Embedding map: 412 chunks in 7 clusters (+63 noise). Since this client does not render inline MCP App UIs, I saved a self-contained interactive graph to: …/.tree/graphs/embedding-map-20260906-101500.html …` plus a `graphs://` link.
3. Opening the file shows the coloured map with the legend `Agent memory design · 84`, …

### Story: Same question in a UI-capable client
1. The user asks the same in a client supporting MCP Apps, adding "outline the clusters".
2. The agent calls `visualize_memory_embeddings(hulls=true)`; the map renders inline in an iframe with hulls on; the model only sees the summary line.

### Story: The map is stale
1. Two days after clustering, the user has ingested more articles and asks for the map.
2. The tool answer starts with `37 of 449 chunks have no cluster assignment (or a stale one) — run make memory-run-clustering-pipeline`; the agent relays that line and offers to run the command.

### Story: Nobody has clustered yet
1. A fresh user calls the tool (or `make memory-visualize-embeddings`).
2. They get `No clustering run found for this user — run make memory-run-clustering-pipeline to build the embedding map.` — no empty canvas, and the CLI exits 1.

### Story: Operator renders from the terminal with hulls
1. Operator runs `make memory-visualize-embeddings HULLS=true USER_IDENTIFIER=paul`.
2. The terminal prints `Wrote …/embedding-map-<stamp>.html` then `Embedding map: 412 chunks in 7 clusters (+63 noise)`; the browser opens on the map with hulls drawn.

## Out of scope

- Computing clusters from the tool or CLI (they read only).
- Harness changes; a new deployment; run comparison.

---

Blocked by: #117, #118

## Log

### [SWE] 2026-09-06 19:00 — Implementation

**Files modified**
- `apps/memory/src/tree/mcp/tools.py` — `visualize_memory_embeddings(ctx, hulls, as_html_file)` registered in BOTH modes; reads `load_embedding_map`, returns `NO_CLUSTERING_RUN_MESSAGE` (plain `str`) with no run, else `_graph_tool_result(..., query="embedding-map")` with the warning line prefixed to the summary. Imports `GRAPH_VIEW_URI` / `_graph_tool_result` from `tree.mcp.viz_app` (never `graph_tools`).
- `apps/memory/src/tree/mcp/server.py` — both `instructions` texts name the tool; the "six tools" / "13 tools" comments now read seven / 14.
- `apps/memory/src/tree/mcp/viz_app.py` — `graph_view` docstring → "Interactive Sigma.js viewer for knowledge graphs and embedding maps (read-only)"; `_graph_tool_result`'s `payload` annotation widened to `dict[str, Any]` (a map payload's `layout` / `hulls` / `summary` values are not lists) and its docstring names the map.
- `apps/memory/src/tree/memory/visualize/graph.py` — **#118 tester note 1**: on the fixed-layout branch the header count line reads the map's own summary ("1623 chunks in 37 clusters (+99 noise)") instead of "N nodes · 0 edges"; graph payloads are byte-identical.
- `apps/memory/src/tree/memory/visualize/embeddings.py` — **#118 tester note 2**: `render_embedding_map_file(payload, output: str | Path | None)` coerces `str` → `Path` (the CLI's `--output` hands over a string).
- `apps/memory/scripts/visualize_embeddings.py` (NEW) — glue only: `init_logger()`, `user_options`, `--hulls/--no-hulls`, `--output/-o`, `--no-open`; no run → `click.echo(NO_CLUSTERING_RUN_MESSAGE)` + exit 1; warning printed FIRST; `render_embedding_map_file`; best-effort `webbrowser.open`; prints `Wrote <path>` then the summary.
- `apps/memory/Makefile` — `visualize-embeddings` target (`USER_FLAGS`, `HULLS=true`, `OUTPUT=…`); listed by `make memory-help`.
- `apps/memory/README.md` — "Embedding map" subsection under Query CLI (command, contract, both messages), tool tables → `7 tools` / `7 more, 14 total` with the tool row, layout block (`mcp/viz_app.py`, `scripts/visualize_embeddings`), cross-link from "Memory clustering".
- `README.md` (root) — pipeline block gains `make memory-run-clustering-pipeline # (optional) cluster child embeddings → embedding map` and `make memory-visualize-embeddings`.
- `.agents/skills/tree-memory/SKILL.md` — tool-table row (✅/✅), `allowed-tools`, and a rewritten "Visualization" section: use the map in EITHER mode, relay the warning line and the no-run message verbatim, graph visualisation stays graphrag-only.
- `.agents/skills/run-pipelines-e2e/SKILL.md` — step 2 gains the clustering phase (cold ~40 s numba note + `TREE_MEMORY__CLUSTERING__HDBSCAN__MIN_CLUSTER_SIZE=5` for small corpora, in the SERVING shell); step 3 gains `make memory-visualize-embeddings`, the stale-warning check and the MCP call; tool counts 7 / 14.
- `docs/notes/prefect-execution-topologies.md` — the FOUR `offline-pipeline` phases as a table (flag, what runs, single-step command) + why phase ④ is off by default.
- Tests: `apps/memory/tests/unit/mcp/test_tool_gating.py`, `tests/unit/mcp/test_tools.py`, `tests/unit/scripts/test_visualize_embeddings.py` (NEW), `tests/unit/test_embedding_map_docs.py` (NEW), `tests/unit/memory/visualize/test_{graph,embeddings}.py`.

**Tests**
- Unit: 2876 passing, 0 failing, 0 warnings (`make memory-tests`) — baseline post-#118 was 2831 (+45).
- Integration: N/A — this repo has no integration suite; e2e is the real-pipeline run below.

**Acceptance criteria**
- [x] `test_tool_gating.py` — `TestRegisteredToolSet::test_rag_mode_registers_only_the_seven_shared_tools` (exact sorted list incl. `visualize_memory_embeddings`), `::test_graphrag_mode_adds_exactly_the_seven_graph_tools` (== 14), `::test_rag_mode_never_imports_the_graph_modules`, `::test_no_mode_imports_the_clustering_stack_to_read_a_map[rag|graphrag]` (`umap`/`sklearn` absent), `::test_both_modes_import_the_neutral_mcp_app_layer` (`tree.mcp.viz_app` present), `TestEmbeddingMapToolSignature::test_the_tool_advertises_exactly_hulls_and_as_html_file[rag|graphrag]`.
- [x] Instruction texts — `TestModeAwareInstructions::test_both_modes_announce_the_embedding_map_tool[rag|graphrag]`; the existing `test_rag_instructions_name_no_graph_tool[query_memory|deep_search_memory|…]` still guards the rag text.
- [x] Tool unit tests — `tests/unit/mcp/test_tools.py::TestVisualizeMemoryEmbeddings::{test_no_clustering_run_answers_with_the_command_to_run, test_a_ui_capable_client_gets_the_map_inline_with_hulls_on, test_hulls_default_off_leaves_the_toggle_unchecked, test_a_stale_map_answers_with_the_warning_line_first, test_a_non_ui_client_gets_a_file_and_its_graphs_resource_link, test_as_html_file_forces_the_file_branch_for_a_ui_client, test_the_docstring_tells_the_model_when_to_draw_a_map}`.
- [x] CLI tests — `tests/unit/scripts/test_visualize_embeddings.py::TestNoClusteringRun::{test_it_prints_the_command_to_run_and_exits_one, test_it_writes_no_file}`, `::TestRenderedMap::{test_a_stale_run_warns_on_the_very_first_line, test_the_hulls_flag_reaches_the_payload[--hulls|--no-hulls], test_output_pins_the_destination_file, test_no_open_skips_the_browser, test_by_default_it_opens_the_map_in_a_browser, test_it_prints_the_written_path_and_the_summary, test_a_fresh_run_prints_no_warning_line}`.
- [x] Makefile — `::TestMakefileWiring::test_the_make_target_is_wired_to_this_script`; `make memory-help` lists `visualize-embeddings` (output below).
- [x] Docs / skills — `tests/unit/test_embedding_map_docs.py` (`TestMemoryReadme`, `TestTreeMemorySkill`, `TestE2eSkill`).
- [x] No `graph_app` — `tests/unit/test_embedding_map_docs.py::test_the_mcp_app_layer_is_named_viz_app_in_the_code_and_its_docs` (git grep over `apps/memory .agents docs/glossary.md README.md`).
- [x] Format / lint / tests green (evidence below).
- [x] [HUMAN] e2e steps 1–4, 5, 6–7 — evidence below.

**Evidence**

QA loop:
```
$ make memory-format-check && make memory-lint-check
288 files already formatted
All checks passed!

$ make pre-commit
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed

$ make memory-tests
============================ 2876 passed in 44.50s =============================

$ make memory-help | grep visualize-embeddings
visualize-embeddings: Render the EMBEDDING MAP of one user (the latest clustering run) as an interactive HTML file under `.tree/graphs/` and open it. READS only: run `make memory-run-clustering-pipeline` first — with no run it prints the message and exits 1; chunks ingested since it warn on the first output line. HULLS=true outlines each cluster, OUTPUT=<path> pins the file. Defaults to the current user; override with USER_ID=<oid> or USER_IDENTIFIER=<handle>.
```

LIVE E2E — local env, user `paul@example.com` (`6a8ea9579a7aeb13175955c8`), `make memory-serve-workflows` run FROM this worktree with the Dockerized `tree-prefect-worker` stopped; MCP served from the same worktree. Corpus: the existing 71-document corpus (1448 embedded child chunks) — step 1 needed no extra ingest and no `MIN_CLUSTER_SIZE` override (defaults, `min_cluster_size=15`).

*Step 2 — `make memory-run-clustering-pipeline`* (flow run `53a30140-…`, Completed):
```
Flow run 'mighty-mussel' - clustering run d154064e-90ee-491f-b779-81038fe4e1e6: 37 clusters, 1306 chunks, 142 noise, 0 fallback summaries
```
`mongosh` (top rows by size + one child):
```
memory_clusters rows: 37
{"_id":"…:cluster:11","label":"Frontier Open-Weight Model Architectures","size":100,"keywords":["open-weight models","mixture of experts","post-training optimization","agentic workflows","llm distillation"],"run_id":"d154064e-…"}
{"_id":"…:cluster:6","label":"Simulating Human Behavior and Populations","size":96,…}
{"_id":"…:cluster:23","label":"Building custom coding agent harnesses","size":93,…}
{"_id":"…:cluster:33","label":"Agent Knowledge Graph Memory Architectures","size":77,…}
{"_id":"…:cluster:2","label":"Large Language Model Weight Quantization","size":49,…}
--- one child row ---
{"_id":"…:chunk:https://www.decodingai.com/p/llm-structured-outputs-the-only-way#parent-0#child-0","cluster_id":32,"viz":{"x":5.288695812225342,"y":13.686663627624512,"run_id":"d154064e-…"}}
children with viz of latest run: 1448   embedded children total: 1448
```

*Step 3 — `make memory-visualize-embeddings HULLS=true`* (no warning line):
```
Wrote /…/building-agentic-systems-embedding-clusters-viz/apps/memory/.tree/graphs/embedding-map-20260906-184601.html
Embedding map: 1448 chunks in 37 clusters (+142 noise)
```
Screenshot: `apps/memory/.tree/e2e-119/step3-hulls.png` — header reads `1448 chunks in 37 clusters (+142 noise)` (the #118 tester note, not "1448 nodes · 0 edges"), "Cluster hulls" checked, 37 coloured legend rows with the LLM labels + sizes, hull outlines drawn.

*Step 4 — rag MCP server* (`TREE_MEMORY__MODE=rag make memory-serve-mcp TRANSPORT=streamable-http USER_ID=6a8ea9579a7aeb13175955c8`):
```
$ uv run fastmcp list http://127.0.0.1:8000/mcp --auth none   # 7 tools
ingest_conversation ingest_file ingest_url scrape_web search_memory search_web visualize_memory_embeddings

$ uv run fastmcp call http://127.0.0.1:8000/mcp --auth none visualize_memory_embeddings hulls=true
Embedding map: 1448 chunks in 37 clusters (+142 noise). Since this client does not render inline MCP App UIs, I saved a self-contained interactive graph to:
/…/apps/memory/.tree/graphs/embedding-map-20260906-184907.html
Opened it in your browser.
name='embedding-map-20260906-184907.html' uri=AnyUrl('graphs://embedding-map-20260906-184907.html') mimeType='text/html' type='resource_link'
```

*Step 5 — one new document* (`make memory-run-pipeline MODE=online SOURCE="https://en.wikipedia.org/wiki/Word_embedding"`, flow `dc390b72-…` Completed; `documents` gains "Word embedding", embedded children 1448 → 1623, 175 with `cluster_id: null`). Both surfaces START with the warning:
```
$ make memory-visualize-embeddings HULLS=true
175 of 1623 chunks have no cluster assignment (or a stale one) — run make memory-run-clustering-pipeline
Wrote /…/apps/memory/.tree/graphs/embedding-map-20260906-185059.html
Embedding map: 1448 chunks in 37 clusters (+142 noise)

$ uv run fastmcp call … visualize_memory_embeddings hulls=true
175 of 1623 chunks have no cluster assignment (or a stale one) — run make memory-run-clustering-pipeline
Embedding map: 1448 chunks in 37 clusters (+142 noise). Since this client does not render inline MCP App UIs, I saved a self-contained interactive graph to:
/…/apps/memory/.tree/graphs/embedding-map-20260906-185106.html
```
Screenshots: `apps/memory/.tree/e2e-119/step5-stale.png` (yellow warning banner in the header) and `apps/memory/.tree/e2e-119/step5-stale-legend.png` (legend bottom: `noise · 142`, `unclustered / stale (not shown) · 175`).

*Step 6 — no run at all* (`db.memory_clusters.drop()` + `db.memory.updateMany({}, {$unset:{cluster_id:"", viz:""}})` → 1448 modified):
```
$ uv run python scripts/visualize_embeddings.py --no-open ; echo $?
No clustering run found for this user — run make memory-run-clustering-pipeline to build the embedding map.
1
(make memory-visualize-embeddings → same line, make exits 2 on the child's 1; no file written)

$ uv run fastmcp call … visualize_memory_embeddings
No clustering run found for this user — run make memory-run-clustering-pipeline to build the embedding map.
```

*Step 7 — graphrag smoke* (same server booted without the override):
```
$ uv run fastmcp list http://127.0.0.1:8000/mcp --auth none   # 14 tools
deep_search_memory ingest_conversation ingest_file ingest_url memory_dashboard query_memory
review_confirm review_list_pending review_reject scrape_web search_memory search_web
visualize_memory_embeddings visualize_memory_graph
```

*Cleanup* — re-ran `make memory-run-clustering-pipeline`, so the DB ends on a FRESH valid run covering the new document:
```
Flow run 'fearless-lion' - clustering run a704df96-f3a9-4fcd-a765-26fba370b0fb: 37 clusters, 1524 chunks, 99 noise, 0 fallback summaries
clusters: 37   children with this run viz: 1623   embedded children: 1623
$ make memory-visualize-embeddings
Embedding map: 1623 chunks in 37 clusters (+99 noise)      # no warning line
```
MCP servers and the worktree `serve-workflows` process stopped; `docker start tree-prefect-worker` back up; `git status` shows only the intended source/doc/test changes (`.tree/` is gitignored — the rendered maps and screenshots stay on disk).

**Notes**
- **Judgement call — the pipeline ran in the DEFAULT (graphrag) mode; only the MCP server was booted with `TREE_MEMORY__MODE=rag`.** The local `memory` collection is graphrag-populated (edges present), and ingesting the new document in rag mode would have left a half-rag/half-graphrag collection for no gain: clustering, the map payload and both surfaces read only child chunks + `memory_clusters` and never branch on the mode. The mode-sensitive part of the e2e (7 vs 14 tools, the rag tool call) WAS exercised in both modes.
- Screenshots were taken with headless Chrome + software WebGL (`--enable-unsafe-swiftshader --use-angle=swiftshader`). Without a GL context Sigma throws and the canvas + legend come up empty — that is a headless artefact, not a renderer bug (the first attempt in this run looked exactly like that).
- `summarise-cluster` was served entirely from the Prefect `INPUTS` cache on the first re-run (`Cached(type=COMPLETED)` ×37) — expected: the samples and prompt version were unchanged. The final re-cluster (1623 chunks) recomputed the affected clusters and reported `0 fallback summaries`.
- `_graph_tool_result`'s `payload` annotation was `dict[str, list[dict[str, Any]]]`, which the map payload does not satisfy (`layout: "fixed"`, `hulls: bool`, `summary: str`). Widened to `dict[str, Any]`; no runtime behaviour changed (the repo type-checks nothing beyond ruff).
- `make memory-visualize-embeddings` with no run exits **2** (make's own code for a failed recipe); the script itself exits **1** as specified — asserted directly in the unit test and verified above with `uv run python scripts/visualize_embeddings.py`.

### [Tester] 2026-09-06 22:10 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check && make memory-lint-check` → "All checks passed!"; `make pre-commit` → all green)
- Unit tests: 2876 passed / 0 failed (`make memory-tests`, local env) — matches SWE's claim, ≥ post-#118 baseline (2831)
- Integration tests: N/A — no integration suite in this repo (per AGENTS.md)
- Warnings: 1 pytest `UserWarning` (opik/pydantic-v1 on Python 3.14, from `opik/rest_api/core/pydantic_utilities.py`) — pre-existing, unrelated to this change (also appears on plain live-script runs, not introduced by any diff in `git diff --stat`)

**E2E adversarial pass**
- Happy path (CLI): `make memory-visualize-embeddings HULLS=true OUTPUT=... USER_ID=6a8ea9579a7aeb13175955c8` → `Embedding map: 1623 chunks in 37 clusters (+99 noise)`, no warning; headless-Chrome (swiftshader) screenshot shows header `1623 chunks in 37 clusters (+99 noise)`, "Cluster hulls" checked, 37-row legend with LLM labels + hull outlines (PASS)
- Happy path (graph, regression check): `make memory-query-graph QUERY="Paul Iusztin"` in graphrag → header renders `72 nodes · 67 edges`, confirming the fixed-layout header change in `graph.py` does NOT leak into the graph branch (PASS)
- Happy path (MCP, rag): booted rag MCP server from this worktree, `fastmcp list` → `Tools (7)`: `ingest_conversation, ingest_file, ingest_url, scrape_web, search_memory, search_web, visualize_memory_embeddings`; `fastmcp call … visualize_memory_embeddings hulls=true` → text starts `Embedding map: 1623 chunks in 37 clusters (+99 noise). Since this client does not render inline MCP App UIs…` + `graphs://embedding-map-….html` resource link (PASS)
- Break path 1 (malformed input — unexpected kwarg): `fastmcp call … visualize_memory_embeddings foo=bar` → clean pydantic validation error (`Unexpected keyword argument`), no crash/traceback leak (PASS)
- Break path 2 (boundary/coercion — `hulls` as a JSON string): raw `fastmcp.Client().call_tool("visualize_memory_embeddings", {"hulls": "true"})` → accepted, coerced to `hulls: true` in the rendered payload (verified by reading the embedded `DATA` JSON in the output HTML); the `fastmcp` CLI's own arg parser rejects a quoted `"true"` before it reaches the server (client-side, expected) (PASS — documented coercion behaviour, no crash either way)
- Break path 3 (hostile input — quotes/backslash/script tag in a cluster label): built an `EmbeddingMap` with `label = 'Weird "quoted" \\ label <script>alert(1)</script>'`, ran `to_embedding_map_payload` → `render_embedding_map_file`; the embedded `DATA` JSON escapes the quotes/backslash correctly and `</script>` is emitted as `<\/script>`, so the payload cannot break out of the inline `<script>` tag (PASS — no injection)
- Break path 4 (state edge — stale map without ingest): flipped `viz.run_id` to a bogus value on 3 real children directly in Mongo (no ingest) → CLI (`3 of 1623 chunks have no cluster assignment (or a stale one) — run make memory-run-clustering-pipeline`) and the rag MCP tool call both start with the identical line; restored the 3 stamps to the latest `run_id` → warning disappears, output returns to `1623 chunks in 37 clusters (+99 noise)` on both surfaces (PASS)
- Break path 5 (state edge — genuinely fresh user, no clustering run): `make memory-signup USER_IDENTIFIER=qa-119-throwaway@example.com` → new user with zero children/clusters; `uv run python scripts/visualize_embeddings.py --user-id <new-id> --no-open` → prints `NO_CLUSTERING_RUN_MESSAGE`, exit code 1; booted the (graphrag) MCP server scoped to that user, called `visualize_memory_embeddings` → identical message, no `ToolResult`/file (PASS — supersedes reliance on the SWE's own no-run evidence; this is a live, this-session repro against a genuinely unclustered user)
- Break path 6 (concurrency — read-only guarantee): ran 3 concurrent `visualize_embeddings.py` invocations (2 default, 1 `--hulls`) against the same user; all 3 completed with identical summaries and distinct output files; `db.memory.countDocuments({})`, `db.memory_clusters.countDocuments({})` and one sampled child's `updated_at` were byte-identical before/after (6144 / 37 / unchanged timestamp) — confirms the tool is read-only under concurrent access (PASS)
- graphrag smoke: booted the MCP server without the rag override → `fastmcp list` → `Tools (14)` including both `visualize_memory_embeddings` and `visualize_memory_graph` (PASS)

**Acceptance criteria**
- [x] PASS — `test_tool_gating.py` exact 7/14 tool sets, `sys.modules` gating, `viz_app` neutral-module import in both modes, tool schema `{hulls, as_html_file}` — read all assertions in `apps/memory/tests/unit/mcp/test_tool_gating.py` (`_SHARED_TOOLS`, `_GRAPH_MODULES`, `_LAZY_MODULES`, `TestEmbeddingMapToolSignature`); re-verified live: rag `fastmcp list` → 7 named tools, graphrag → 14 incl. both visualize tools
- [x] PASS — both instruction texts mention `visualize_memory_embeddings`; rag text still excludes `query_memory`/`deep_search_memory` — `test_tool_gating.py::TestModeAwareInstructions::{test_both_modes_announce_the_embedding_map_tool, test_rag_instructions_name_no_graph_tool}`
- [x] PASS — the seven named `TestVisualizeMemoryEmbeddings` unit tests in `apps/memory/tests/unit/mcp/test_tools.py:247-369` cover exactly what the AC lists (plain `str` + `_graph_tool_result` not called on no-run; `ToolResult` w/ `layout=="fixed"`/`hulls==True`; `unclustered=2` → text starts with the warning; non-UI client gets a `graphs://embedding-map-….html` link; `as_html_file=True` forces the file branch) — all pass; behaviour re-confirmed live above
- [x] PASS — `tests/unit/scripts/test_visualize_embeddings.py`: no-run → prints message, exit 1, `render_embedding_map_file` never called; stale run → warning is `result.output.splitlines()[0]`; `--hulls`/`--no-hulls` forwarded; `--output` honoured; `--no-open` skips `webbrowser.open` — all 11 tests pass; re-confirmed live (CLI + throwaway user)
- [x] PASS — `grep -n "visualize-embeddings" apps/memory/Makefile` shows the target wired to `$(USER_FLAGS)`, `HULLS`, `OUTPUT`; `make memory-help | grep visualize-embeddings` prints the full one-line description
- [x] PASS — `apps/memory/README.md` contains `visualize_memory_embeddings`, `7 tools`, `14 total`, `#### Embedding map` heading, `have no cluster assignment` (grep confirmed at README.md:343,353,355,304,323); `tree-memory` SKILL lists the tool with two ✅ columns; `run-pipelines-e2e` SKILL mentions `run-clustering-pipeline` and `visualize-embeddings`
- [ ] FAIL — `grep -rn "graph_app" apps/memory .agents docs/glossary.md README.md` returns nothing (ADR prose excluded)
      Expected: the grep (run literally, not via `git grep`) returns no hits outside ADR files.
      Actual: `grep -rln "graph_app" apps/memory .agents docs/glossary.md README.md` → `apps/memory/tests/unit/test_embedding_map_docs.py` (lines 10, 94, 103) — the new test's own docstring/comment names `tree.mcp.graph_app` to explain the ADR-007 §7 rename. The test itself calls `git grep -n "graph_app" -- apps/memory .agents docs/glossary.md README.md` and asserts the output is empty; today that passes ONLY because the file is untracked (`git grep` skips untracked files). The moment this file is `git add`-ed and committed — which happens right after this QA pass — `git grep` will match the test's own docstring/comment and `test_the_mcp_app_layer_is_named_viz_app_in_the_code_and_its_docs` will fail permanently in every future CI run. Verified by simulating `git add -N` on the three new files and re-running the exact `git grep` command from the test: it now returns 3 hits, all inside this test file.
      Fix: rephrase the docstring/comment (module docstring line 10, and the comment at line 94) so the literal substring `graph_app` does not appear verbatim — e.g. spell it as `` `tree.mcp.` + `graph_app` `` split across a concatenation, or reword without naming the exact old identifier (say "the pre-#… graph-only MCP App module name" instead), or add `":(exclude)apps/memory/tests/unit/test_embedding_map_docs.py"` to the `git grep` pathspec inside the test so the test excludes itself the same way it excludes the ADRs. Re-run `git add -N` + `git grep -n graph_app -- apps/memory .agents docs/glossary.md README.md` after the fix to confirm zero hits.
- [x] PASS — `make memory-format-check && make memory-lint-check && make memory-tests` green, 2876 ≥ 2831 (evidence above)
- [x] PASS (HUMAN, re-verified) — e2e steps 1–4: read the pasted clustering summary line, `mongosh` rows and screenshot references in the SWE's Log entry, then independently re-derived the same DB state (`clusters: 37`, `children total: 1623`, `children with viz.run_id: 1623`, `noise: 99`) live via `mongosh`; opened `apps/memory/.tree/e2e-119/step3-hulls.png` — header/legend/hulls match the pasted evidence exactly; independently reproduced the CLI + rag-MCP-tool happy path myself (see E2E pass above)
- [x] PASS (HUMAN, re-verified) — e2e step 5: opened `apps/memory/.tree/e2e-119/step5-stale.png` and `step5-stale-legend.png` — yellow warning banner (`175 of 1623 chunks have no cluster assignment…`) in the header and `unclustered / stale (not shown) · 175` in the legend, matching the pasted text; independently reproduced the same warning contract myself with a live 3-child staleness injection (see Break path 4 above) — CLI and MCP tool both start with the warning, both clear after restoring the stamps
- [x] PASS (HUMAN, re-verified) — e2e steps 6–7: independently reproduced step 6 with a genuinely fresh (zero-cluster) throwaway user rather than relying solely on the SWE's drop-collection evidence (Break path 5 above) — both the raw script and the MCP tool answer with `NO_CLUSTERING_RUN_MESSAGE`; reproduced step 7 myself — graphrag `fastmcp list` → `Tools (14)` incl. `visualize_memory_embeddings` and `visualize_memory_graph`

**Evidence**
```
$ make memory-format-check && make memory-lint-check
288 files already formatted
All checks passed!

$ make pre-commit
ruff check...............................................................Passed
ruff format..............................................................Passed

$ make memory-tests
============================ 2876 passed in 43.26s =============================

$ grep -rln "graph_app" apps/memory .agents docs/glossary.md README.md
apps/memory/tests/unit/test_embedding_map_docs.py

$ git add -N apps/memory/tests/unit/test_embedding_map_docs.py apps/memory/scripts/visualize_embeddings.py apps/memory/tests/unit/scripts/test_visualize_embeddings.py
$ git grep -n "graph_app" -- apps/memory .agents docs/glossary.md README.md
apps/memory/tests/unit/test_embedding_map_docs.py:10:...`tree.mcp.graph_app` is `tree.mcp.viz_app`
apps/memory/tests/unit/test_embedding_map_docs.py:94:    # ADR-007 §7 renamed ``tree.mcp.graph_app`` -> ``tree.mcp.viz_app``...
apps/memory/tests/unit/test_embedding_map_docs.py:103:            "graph_app",
$ git reset apps/memory/tests/unit/test_embedding_map_docs.py apps/memory/scripts/visualize_embeddings.py apps/memory/tests/unit/scripts/test_visualize_embeddings.py
```

**Other issues found**
- Judgement call (1) — the live e2e's ingest + clustering steps ran in the DEFAULT graphrag mode rather than the "ONE mode (rag)" the scope text asks for; only the MCP server boot + tool call were done in rag. I accept this: the code path is verifiably mode-orthogonal (neither clustering, the payload builder, nor `_graph_tool_result` branch on `MEMORY_MODE`), and the mode-sensitive surface (tool registration count, a rag-mode tool call) WAS exercised in both modes. Not a blocker, but noted per the review brief's instruction to record this judgement.
- Judgement call (4) — confirmed independently (see reproduction in this review, not just the SWE's claim): GNU Make itself always exits 2 on ANY failed recipe regardless of the child's own exit code (tested with a scratch Makefile: a target that does `exit 1` still yields `make` exit code 2). The AC/Story text's "the CLI exits 1" refers to the script (labelled "CLI" throughout the task spec), which the unit tests assert directly via `CliRunner` — not the `make` wrapper. Not a bug.
- No other issues found in the diff, docs, or live behaviour.

**VERDICT: FAIL**

One concrete, actionable issue: the self-referential `git grep "graph_app"` test (`apps/memory/tests/unit/test_embedding_map_docs.py::test_the_mcp_app_layer_is_named_viz_app_in_the_code_and_its_docs`) will fail on every run once the new test file itself is committed, because its own docstring/comments spell out the old module name it is checking for. Everything else — code, the other 44 new tests, docs, live e2e (independently re-verified, including two live reproductions the SWE's evidence only implied: a genuinely fresh no-run user, and read-only concurrency) — is solid and ready. Fix the self-match, re-run `make memory-tests` plus the `git add -N` simulation above to confirm the grep is clean, and this should pass QA.

### [SWE] 2026-09-06 23:05 — Fixes (QA round 1)

Addresses the single Tester FAIL: the rename guard
`test_embedding_map_docs.py::test_the_mcp_app_layer_is_named_viz_app_in_the_code_and_its_docs`
was both **self-matching** (its own docstring/comment spelled the old identifier, so it would go
red the moment the file was committed) and **vacuous** (plain `git grep` skips untracked files, so
it could never have caught that — or any other new file carrying the old name).

**Files modified**
- `apps/memory/tests/unit/test_embedding_map_docs.py` — (a) the module docstring and the guard's
  comment now say "the pre-ADR-007, graph-only MCP App module" instead of spelling the old
  identifier; the needle itself is built at runtime as `_PRE_ADR_007_APP_MODULE = "graph" + "_app"`,
  so the file's source no longer contains the substring it greps for. (b) the guard now runs
  `git grep --untracked`, covering tracked **and** untracked files, excluding nothing.

Nothing else changed — no source, no docs, no other test.

**Tests**
- Unit: 2876 passing, 0 failing (`make memory-tests`, `make env-status` → local) — unchanged count,
  as expected for a test-body-only fix.
- Integration: N/A — no integration suite in this repo.

**Acceptance criteria**
- [x] `grep -rn "graph_app" apps/memory .agents docs/glossary.md README.md` returns nothing — the
  literal AC command, run verbatim over the exact AC scope (no `docs/adrs`, no exclusions), exits 1
  with empty output. Also empty via the test's own `git grep --untracked` with all three new files
  `git add -N`-staged (the Tester's failing simulation).
- All other criteria remain as ticked; nothing else was touched.

**Evidence**

Literal AC grep — empty (exit 1 = no match), no exclusions:
```
$ grep -rn "graph_app" apps/memory .agents docs/glossary.md README.md
exit: 1
```

The Tester's `git add -N` simulation, then the test's own command — empty:
```
$ git add -N apps/memory/tests/unit/test_embedding_map_docs.py \
             apps/memory/scripts/visualize_embeddings.py \
             apps/memory/tests/unit/scripts/test_visualize_embeddings.py
$ git grep --untracked -n "graph_app" -- apps/memory .agents docs/glossary.md README.md
git grep exit: 1
```
(staging was reset afterwards; the working tree is back to 3 untracked + the same modified files)

Non-vacuity probe — an untracked file carrying the old name must turn the guard red. The OLD
tracked-only command misses it (this is exactly the vacuous pass the Tester predicted); the new
`--untracked` one catches it and the test fails:
```
$ printf '# tree.mcp.graph_app\n' > apps/memory/src/tree/_probe_rename_guard.py
$ git grep --untracked -n "graph_app" -- apps/memory .agents docs/glossary.md README.md
apps/memory/src/tree/_probe_rename_guard.py:1:# tree.mcp.graph_app
$ git grep -n "graph_app" -- apps/memory .agents docs/glossary.md README.md   # old, tracked-only
tracked-only exit: 1          # <- vacuous: sees nothing

$ make memory-tests
>       assert hits == ""
E       AssertionError: assert 'apps/memory/...p.graph_app\n' == ''
E         + apps/memory/src/tree/_probe_rename_guard.py:1:# tree.mcp.graph_app
tests/unit/test_embedding_map_docs.py:125: AssertionError
FAILED tests/unit/test_embedding_map_docs.py::test_the_mcp_app_layer_is_named_viz_app_in_the_code_and_its_docs
======================= 1 failed, 2875 passed in 45.44s ========================
$ rm -f apps/memory/src/tree/_probe_rename_guard.py   # probe removed
```

QA loop, clean:
```
$ make env-status
Env target: local (.env)

$ make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check
288 files left unchanged
All checks passed!
288 files already formatted
All checks passed!

$ make pre-commit
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed

$ make memory-tests
============================ 2876 passed in 45.40s =============================
```

E2E re-smoke (CLI happy path, unchanged behaviour — matches the Tester's live numbers):
```
$ make memory-visualize-embeddings HULLS=true OUTPUT=<scratch>/e2e-119-refix.html USER_ID=6a8ea9579a7aeb13175955c8
Resolved target user: id=6a8ea9579a7aeb13175955c8 identifier=paul@example.com
Wrote self-contained graph HTML (1623 nodes, 0 edges) to <scratch>/e2e-119-refix.html
Embedding map: 1623 chunks in 37 clusters (+99 noise)
```

**Notes**
- The guard is now committable: the source contains no literal occurrence of the old name, and it
  greps tracked + untracked, so it cannot pass vacuously on a brand-new file.
- The task file (`tasks/…`) still names the old identifier in the spec and in the QA history — that
  is intentional and out of the AC's grep scope (`apps/memory .agents docs/glossary.md README.md`),
  same as the ADRs.
- NOT re-run — the full 7-step live e2e ritual (ingest → clustering → stale/no-run states → both MCP
  modes). This round changed one test body only; the Tester independently re-verified all of it in
  the previous round, and the CLI re-smoke above confirms the runtime path is untouched.
- Uncommitted, per the workflow. Handing back to the Tester.

### [Tester] 2026-09-06 23:40 — QA round 2

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check && make memory-lint-check` → "All checks passed!"; `make pre-commit` → all 5 hooks Passed)
- Unit tests: 2876 passed / 0 failed (`make memory-tests`, `make env-status` → local) — unchanged from round 1, as expected for a test-body-only fix
- Integration tests: N/A — no integration suite in this repo (per AGENTS.md)
- Warnings: 1 pre-existing opik/pydantic-v1-on-Python-3.14 `UserWarning` (unrelated, same as round 1)

**Re-verification of the round-1 fix (single-file diff: `apps/memory/tests/unit/test_embedding_map_docs.py`)**
- Read the file directly: the module docstring (line 10) and the guard's comment (line 100) now say "the pre-ADR-007, graph-only MCP App module" — no literal `graph_app` substring in the file's source; the needle is built at runtime as `_PRE_ADR_007_APP_MODULE = "graph" + "_app"` (line 24); the guard's `git grep` invocation (lines 107-123) now passes `--untracked`. Matches the SWE's description exactly.
- Literal AC grep, run myself, no exclusions: `grep -rn "graph_app" apps/memory .agents docs/glossary.md README.md` → empty, exit 1 (PASS)
- `git add -N` simulation + the test's own command, run myself: staged the 3 untracked files (`git add -N apps/memory/tests/unit/test_embedding_map_docs.py apps/memory/scripts/visualize_embeddings.py apps/memory/tests/unit/scripts/test_visualize_embeddings.py`), then `git grep --untracked -n "graph_app" -- apps/memory .agents docs/glossary.md README.md` → empty, exit 1 (PASS) — confirms the file no longer self-matches once staged. Unstaged afterward (`git reset <the 3 files>`); `git status --short` showed no leftover `A`/`AM` entries.
- Non-vacuity probe, reproduced myself independently (own filename to avoid collision with the SWE's evidence): `printf '# tree.mcp.graph_app\n' > apps/memory/src/tree/_probe_rename_guard_qa.py` → ran only `tests/unit/test_embedding_map_docs.py::test_the_mcp_app_layer_is_named_viz_app_in_the_code_and_its_docs` → **FAILED** (`AssertionError: assert 'apps/memory/...p.graph_app\n' == ''`, hit reported at `_probe_rename_guard_qa.py:1`) — proves the `--untracked` guard is non-vacuous. Removed the probe (`rm -f apps/memory/src/tree/_probe_rename_guard_qa.py`); `git status --short | grep -i probe` → no leftovers. Re-ran `make memory-tests` → 2876 passed (guard is green again with the probe gone).
- `git status --short`: 16 ` M` + 3 `??`, identical file list and shape to the pre-round-1 snapshot (`.agents/skills/run-pipelines-e2e/SKILL.md`, `.agents/skills/tree-memory/SKILL.md`, `README.md`, `apps/memory/Makefile`, `apps/memory/README.md`, `apps/memory/src/tree/mcp/{server,tools,viz_app}.py`, `apps/memory/src/tree/memory/visualize/{embeddings,graph}.py`, `apps/memory/tests/unit/mcp/{test_tool_gating,test_tools}.py`, `apps/memory/tests/unit/memory/visualize/{test_embeddings,test_graph}.py`, `docs/notes/prefect-execution-topologies.md`, `tasks/119-...md`; `??` = `apps/memory/scripts/visualize_embeddings.py`, `apps/memory/tests/unit/scripts/test_visualize_embeddings.py`, `apps/memory/tests/unit/test_embedding_map_docs.py`) — no probe leftovers, no stray files.
- `git diff --stat`: same 16 tracked files as round 1 (809 insertions / 55 deletions across the codebase, plus the task file's own log growth); no file outside the claimed single test file changed. The two other untracked files (`scripts/visualize_embeddings.py`, `tests/unit/scripts/test_visualize_embeddings.py`) carry mtimes (21:36, 21:42) that predate the round-1 QA pass (22:10) and their content matches round-1 evidence — untouched.
- Spot-checked the other docs-guard tests in the same file are still meaningful, not weakened: ran the full file (`uv run pytest tests/unit/test_embedding_map_docs.py -v`) → all 15 pass, including `TestMemoryReadme` (5 real-string README checks + 2 tool-count checks), `TestTreeMemorySkill` (2 checks against `.agents/skills/tree-memory/SKILL.md`), `TestE2eSkill` (4 checks against `.agents/skills/run-pipelines-e2e/SKILL.md`) — none of these were touched by the fix; all assert real substrings against real files, no weakening.
- CLI happy-path re-smoke: `make memory-visualize-embeddings HULLS=true OUTPUT=<scratch>/e2e-119-round2.html USER_ID=6a8ea9579a7aeb13175955c8` → `Embedding map: 1623 chunks in 37 clusters (+99 noise)` — matches round-1 and the SWE's re-smoke exactly; runtime path unaffected.

**Acceptance criteria**
- [x] PASS (previously FAIL, now fixed) — `grep -rn "graph_app" apps/memory .agents docs/glossary.md README.md` returns nothing (ADR prose excluded) — literal grep run myself: empty/exit 1; `git add -N` + the test's own `git grep --untracked` command: empty/exit 1; non-vacuity probe reproduced myself independently and confirmed it turns the guard red, then green again once removed; all 15 tests in `test_embedding_map_docs.py` pass, including the rename guard
- [x] PASS — `make memory-format-check && make memory-lint-check && make memory-tests` green, 2876 passed (unchanged from round 1, expected for a test-body-only fix)
- All other criteria were verified in round 1 and are untouched by this round's single-file fix (confirmed via `git diff --stat` — no other file changed); spot-checked they still pass by re-running the full suite

**Evidence**
```
$ grep -rn "graph_app" apps/memory .agents docs/glossary.md README.md
exit: 1

$ git add -N apps/memory/tests/unit/test_embedding_map_docs.py apps/memory/scripts/visualize_embeddings.py apps/memory/tests/unit/scripts/test_visualize_embeddings.py
$ git grep --untracked -n "graph_app" -- apps/memory .agents docs/glossary.md README.md
exit: 1
$ git reset apps/memory/tests/unit/test_embedding_map_docs.py apps/memory/scripts/visualize_embeddings.py apps/memory/tests/unit/scripts/test_visualize_embeddings.py

$ printf '# tree.mcp.graph_app\n' > apps/memory/src/tree/_probe_rename_guard_qa.py
$ uv run pytest tests/unit/test_embedding_map_docs.py::test_the_mcp_app_layer_is_named_viz_app_in_the_code_and_its_docs
FAILED tests/unit/test_embedding_map_docs.py::test_the_mcp_app_layer_is_named_viz_app_in_the_code_and_its_docs
E       AssertionError: assert 'apps/memory/...p.graph_app\n' == ''
$ rm -f apps/memory/src/tree/_probe_rename_guard_qa.py

$ make memory-tests
============================ 2876 passed in 50.48s =============================

$ make memory-format-check && make memory-lint-check
288 files already formatted
All checks passed!

$ make pre-commit
ruff check...............................................................Passed
ruff format..............................................................Passed

$ make memory-visualize-embeddings HULLS=true OUTPUT=<scratch>/e2e-119-round2.html USER_ID=6a8ea9579a7aeb13175955c8
Embedding map: 1623 chunks in 37 clusters (+99 noise)
```

**Other issues found**
- None. The round-1 fix is scoped exactly as described (one file), correctly resolves the self-match and vacuity issues, and does not regress anything else.

**VERDICT: PASS**

Round-1's single actionable issue (the self-referential, vacuous `graph_app` rename guard) is fixed and independently re-verified from scratch: the literal AC grep is clean, the `git add -N` staging simulation is clean, and a freshly-planted probe file (different name from the SWE's own evidence, to rule out a stale artifact) proves the guard is non-vacuous — it fails red with the probe present and returns green once removed. Full suite is green at 2876, format/lint/pre-commit clean, `git status`/`git diff --stat` match the pre-round-1 shape with no leftover probe files, and the CLI happy path is unaffected. Ready to commit.
