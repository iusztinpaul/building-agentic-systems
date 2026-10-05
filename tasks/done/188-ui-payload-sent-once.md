---
id: 188-ui-payload-sent-once
status: done
feature: request-scoped-users
---

# The MCP App payload is sent once: drop `structured_content` from `_graph_tool_result` and `memory_dashboard`, delete the iframes' `structuredContent` fallback; Claude Desktop renders both views inline (manual check, with a restore rule)

Tags: `mcp`, `viz`, `ui`, `horizon`
Depends on: —
Blocks: 189 (the Horizon leg of the render check)
Implements: ADR-014 §5

## Problem

`_graph_tool_result` (inline branch) and `memory_dashboard` return the payload TWICE: in a `content` JSON
block marked `audience=["user"]` (the channel the custom iframes read via `ontoolresult`) AND as
`structured_content` ("kept for any host that forwards it too"). Both copies count against Horizon's 6 MB
response cap, so the inline view hits the limit at roughly half the payload the file path does.

## Scope

**Human decision (final):** remove `structured_content=…` from both tools; keep only the `content` JSON
block with `audience=["user"]`; delete the `structuredContent` fallback in both iframes' JS. Acceptance
includes a MANUAL check that Claude Desktop renders the graph view and the dashboard inline (stdio
locally here; Horizon in task 189). If an iframe renders empty → restore `structured_content` and drop the
`content` block instead (switch, never both), and record the observed host behaviour in the Log.

1. `viz_app.py`: `_graph_tool_result`'s inline `ToolResult` loses `structured_content=payload`; the branch
   comment and the module docstring's last paragraph say the payload rides ONLY in the `audience=["user"]`
   content block; `_GRAPH_HTML_TEMPLATE`'s `ontoolresult` loses the `r.structuredContent` fallback and its
   comment reads "the payload is the first JSON `content` block whose `nodes` is an array".
2. `dashboard_app.py`: `memory_dashboard` loses `structured_content=app_payload`; the same JS deletion in
   `_DASHBOARD_HTML_TEMPLATE`; the module docstring's "ships the payload in a `content` JSON block" stays,
   the "kept for any host" comment goes.
3. **Tests:** `test_viz_app.py:107` → `result.structured_content is None`; `:400-404` → `_GRAPH_HTML`
   does not contain `structuredContent`; `test_dashboard_app.py:132-144` → `structured_content is None`;
   `:243-248` → `_DASHBOARD_HTML` does not contain `structuredContent`; `test_graph_tools.py:244, 351, 400`
   read the `audience=["user"]` content block (`json.loads(block.text)`) instead of `structured_content`.
   Source guard: `structured_content` appears nowhere under `src/tree/mcp`.
4. **[HUMAN] Manual check, stdio (Claude Desktop → `tree-memory-local`, graphrag mode so the dashboard
   exists):** `visualize_memory_structure`, `visualize_memory_embeddings` and `memory_dashboard` each
   render inline (nodes drawn / KPI cards filled). Evidence: a sentence per tool in the Log (screenshots
   optional). If ANY iframe shows "No graph data in tool result." / "No dashboard data in tool result.",
   apply the restore rule above for BOTH tools (the two iframes must agree) and say which host behaviour
   was observed; the PA records the flip in ADR-014 §5.

## Acceptance criteria

- [x] `_graph_tool_result`'s inline result and `memory_dashboard`'s result have `structured_content is
      None` and exactly one `audience=["user"]` JSON block carrying the payload (tests).
- [x] Neither `_GRAPH_HTML` nor `_DASHBOARD_HTML` contains `structuredContent`; the `content`-block read
      stays (tests + source guard).
- [x] `test_graph_tools.py` assertions read the content block; `make memory-tests` green.
- [ ] [HUMAN] Claude Desktop (stdio, graphrag) renders the graph view (structure + map) and the dashboard
      inline with the single-copy result — or the restore rule was applied to both tools and the Log says
      what was seen.
- [x] `make memory-format-check`, `make memory-lint-check`, `make pre-commit` green.

## User Stories

### Story: The inline map fits Horizon's response cap
1. A Claude Desktop user on `tree-memory` asks "what topics does my memory hold?".
2. `visualize_memory_embeddings` answers the summary line plus ONE JSON block for the iframe; the response
   is half the size it was, and the map renders inline.

### Story: The dashboard still fills its cards
1. A Claude Desktop user on `tree-memory-local` (graphrag) asks for "a summary of what's in memory".
2. `memory_dashboard` answers the summary plus one JSON block; the KPI cards, the bar chart and the tables
   render.

### Story: A host that only forwards `structuredContent` (the restore rule)
1. During the manual check the iframe says "No graph data in tool result.".
2. The SWE restores `structured_content=payload`, drops the `content` JSON block in BOTH tools, re-checks,
   and records the host behaviour in the Log; the payload still travels once.

---

Blocked by: (none)

## Log

### [PA] 2026-10-05 16:40 — Grooming

**Summary**
Send the iframe payload once — the `audience=["user"]` content block the custom iframes already read — and
delete the `structuredContent` fallback; verify on Claude Desktop before merging, with a defined flip if a
host forwards only the other channel.

**Key decisions**
- `content` block over `structuredContent`: it is the channel the module docstrings record as the one that
  works with real hosts; the MCP Apps spec's preference for `structuredContent` is not followed without a
  live test (ADR-014 §5).
- The two tools flip together or not at all.
- Ordered after 187 only to avoid merge churn in `viz_app.py`; no code dependency.

**Dependencies**
- None.

**User stories**
- 3 stories: inline map within the cap, dashboard renders, the restore rule.

Ready for implementation.

### [SWE] 2026-10-05 21:23 — Implementation

**Files modified**
- `apps/memory/src/tree/mcp/viz_app.py` — `_graph_tool_result`'s inline `ToolResult` drops `structured_content=payload`; branch comment + module docstring's last paragraph say the payload rides ONLY in the `audience=["user"]` block (sent once, ADR-014 §5); `_GRAPH_HTML_TEMPLATE`'s `ontoolresult` loses the `r.structuredContent` fallback, comment now "The payload is the first JSON `content` block whose `nodes` is an array."
- `apps/memory/src/tree/mcp/dashboard_app.py` — `memory_dashboard` drops `structured_content=app_payload`; the "kept for any host" comment goes; same JS deletion + comment in `_DASHBOARD_HTML_TEMPLATE`. Module docstring unchanged.
- `apps/memory/tests/unit/mcp/test_viz_app.py` — inline result asserts `structured_content is None` (the 2-block unpack pins exactly one `audience=["user"]` block); `test_graph_html_reads_only_the_content_blocks` (`r.content` in, `structuredContent` not in `_GRAPH_HTML`); new source guard `test_no_mcp_source_sends_structured_content` (no `.py` under `src/tree/mcp` contains `structured_content`).
- `apps/memory/tests/unit/mcp/test_dashboard_app.py` — `test_dashboard_keeps_structured_content_for_forwarding_hosts` → `test_dashboard_sends_the_payload_once` (exactly one `audience=["user"]` block carrying the payload, `structured_content is None`); `test_dashboard_html_reads_only_the_content_blocks`.
- `apps/memory/tests/unit/mcp/test_graph_tools.py` — the three `structured_content` assertions (query/search dual delivery, closure-marker leak, `visualize_memory_structure`) now read the `audience=["user"]` block via `json.loads(block.text)` and assert `structured_content is None`.

**Tests**
- Red first: 9 failing on assertions (not import errors) before the source change.
- Unit: `make memory-tests` (default/rag mode) — 5084 passed, 0 failed.
- Unit, graphrag: `tests/unit/mcp` with `TREE_MEMORY__MODE=graphrag` (env loaded from `.env`) — 681 passed. A full `TREE_MEMORY__MODE=graphrag make memory-tests` gives 5083 passed + 1 failed: `test_app_config.py::TestMemoryModeConfig::test_memory_mode_rag_loaded_from_yaml`. That test asserts the YAML default (`rag`), and the env override changes it, so the failure comes from how I invoked it, not from this change.
- Integration: N/A (the project has no integration suite).

**Acceptance criteria**
- [x] Single copy on both tools — `test_viz_app.py::test_graph_tool_result_keeps_summary_model_visible_and_payload_user_only`, `test_dashboard_app.py::test_dashboard_sends_the_payload_once`, `test_graph_tools.py::test_visualize_ships_payload_in_content_block_for_ui_clients`, `TestGraphToolsDualDelivery::test_visualize_ships_the_graph_payload_to_the_iframe_only`
- [x] No `structuredContent` in either iframe; `content` read stays — `test_graph_html_reads_only_the_content_blocks`, `test_dashboard_html_reads_only_the_content_blocks`, `test_no_mcp_source_sends_structured_content`
- [x] `test_graph_tools.py` reads the content block; `make memory-tests` green
- [ ] [HUMAN] Claude Desktop render check — see "Manual check for the human" below
- [x] `make memory-format-check`, `make memory-lint-check`, `make pre-commit` green

**Evidence**
```
$ make memory-tests
======================= 5084 passed in 60.74s (0:01:00) ========================
$ TREE_MEMORY__MODE=graphrag uv run pytest tests/unit/mcp -q   # after `set -a; . ../../.env`
681 passed in 11.49s
$ make memory-format-check / memory-lint-check / pre-commit
341 files already formatted / All checks passed! / prettier, ruff check, ruff format, biome check: Passed
# No tool sets an output_schema, so FastMCP cannot build structuredContent on the wire by itself:
memory_dashboard None / visualize_memory_structure None / visualize_memory_embeddings None / query_memory None
```

Headless proxy (machine stand-in for the [HUMAN] check). The script is a scratch file and is not committed. It builds the REAL tool results: `_graph_tool_result` for a seed Graph payload and for an Embedding map payload, and `memory_dashboard` with the query patched. It serializes each result as the wire `CallToolResult` (`ToolResult.to_mcp_result()`). It loads the REAL `_GRAPH_HTML` / `_DASHBOARD_HTML`, which use the real ext-apps 0.4.0 `App` from unpkg, in an iframe inside a host page. That page plays the MCP Apps host over postMessage: it answers `ui/initialize` (protocol `2025-11-21`), and after `ui/notifications/initialized` it posts `ui/notifications/tool-result`. Headless Chromium 1155 (playwright 1.50, via `uv run --with`, not added to the project). To re-run it: `cd apps/memory && set -a && . ../../.env && set +a && TREE_MEMORY__MODE=graphrag PYTHONPATH=. uv run --with playwright==1.50.0 python <scratchpad>/proxy/render_proxy.py <outdir>`. It needs the cached Chromium 1155 and network access to unpkg and jsdelivr.
```
structure:  wire bytes=921   blocks=2 structuredContent=False
embeddings: wire bytes=12247 blocks=2 structuredContent=False
dashboard:  wire bytes=1016  blocks=2 structuredContent=False
[graph/structure (single copy)]         2 nodes · 1 edges                                  pageerrors=[]
[graph/structure structured-only]       No graph data in tool result.                      pageerrors=[]
[graph/embeddings (single copy)]        47 of 47 chunks (...) in 2 clusters (+5 noise)     pageerrors=[]
[graph/embeddings structured-only]      No graph data in tool result.                      pageerrors=[]
[dashboard (single copy)]               metrics='2 NODES 1 EDGES 2 TYPES' empty=''         pageerrors=[]
[dashboard structured-only]             metrics='' empty='No dashboard data in tool result.'
host saw: ui/initialize, ui/notifications/initialized, ui/notifications/size-changed
```
I looked at the screenshots for the embedding map (legend, 47 points drawn) and for the dashboard (KPI cards, type bars, node and relationship tables filled). The structure view is confirmed only by its `2 nodes · 1 edges` counts text. The "structured-only" rows send the same payload ONLY as `structuredContent`. They show the fallback is really gone, and they show exactly what a host that forwards only `structuredContent` would display (the restore-rule trigger).

**Manual check for the human ([HUMAN] AC, still open)**
1. Claude Desktop → `claude_desktop_config.json` → the `tree-memory-local` entry (same shape as `.mcp.json`: `uv --directory apps/memory run --env-file ../../.env python scripts/serve_mcp.py`). **Add `"TREE_MEMORY__MODE": "graphrag"` to its `env`.** `default.yaml` has `memory.mode: rag`, and in rag mode `memory_dashboard` is not registered. If the server then fails to start, make `--directory` absolute (`<repo>/apps/memory`). Run `make env-local`, keep the local Mongo up, and fully quit and restart Claude Desktop.
2. Make sure local memory has data. For the map, a Clustering run must exist (`make memory-run-clustering-pipeline`). Without one, `visualize_memory_embeddings` returns plain text and has no iframe.
3. In a new chat, ask for each tool by name:
   - "Call visualize_memory_structure" → inline graph view, header reads `N nodes · M edges`, nodes drawn.
   - "Call visualize_memory_embeddings" → inline map, header reads `X of Y chunks (...) in K clusters`, points coloured by cluster with a legend.
   - "Call memory_dashboard" → KPI cards (NODES / EDGES / TYPES) filled, type bar chart, node and relationship tables.
   - Optional: open the tool-call details and check the result holds the summary text and ONE JSON block, with no `structuredContent`.
4. Pass: all three render. Write one sentence per tool in this Log and tick the AC.
5. Fail: if ANY iframe shows `No graph data in tool result.` or `No dashboard data in tool result.`, apply the restore rule to BOTH tools. In `_graph_tool_result` and in `memory_dashboard`, put back `structured_content=payload` / `structured_content=app_payload` and delete the `audience=["user"]` JSON block. Point both iframes' `ontoolresult` at `r.structuredContent`. Flip the tests and the source guard. Record which host behaviour you saw so the PA can amend ADR-014 §5.

**Notes**
- The camelCase `structuredContent` stays in prose that explains why the Prefab channel does not reach a custom iframe: the `viz_app` and `dashboard_app` docstrings and the test-module docstrings. The source guard checks only snake_case `structured_content` in `src/tree/mcp`, as the spec says.
- The harness (`apps/harness/src/mcp/client.ts`) reads `structuredContent` only when there are no text parts. It does not claim the UI extension, so it gets the file branch. No change.

### [Tester] 2026-10-05 21:35 — QA

**Test summary**
- Format / lint / pre-commit: PASS (341 files formatted; ruff clean; prettier, ruff, biome Passed)
- Unit tests: 5084 passed / 0 failed (`make memory-tests`, local env target); graphrag `tests/unit/mcp`: 681 passed
- Integration tests: N/A (project has none, per AGENTS.md)
- Warnings: 0 new in the touched suites
- The SWE's graphrag full-suite failure (`test_app_config.py::TestMemoryModeConfig::test_memory_mode_rag_loaded_from_yaml`) is an invocation artifact: it fails identically with the work stashed (baseline) under `TREE_MEMORY__MODE=graphrag`, and the file passes 163/163 without the override.

**E2E adversarial pass** (real `scripts/serve_mcp.py` over stdio, graphrag mode, local Mongo with 14 docs / 413 nodes / 372-chunk clustering run; raw JSON-RPC client in the scratchpad, `qa/wire.py`, `qa/wire2.py`; not committed)
- Happy path, UI-capable client (initialize advertises `io.modelcontextprotocol/ui`): `visualize_memory_structure`, `memory_dashboard`, `visualize_memory_embeddings` → each `CallToolResult` has keys `['content','isError']` only (NO `structuredContent`), exactly 2 blocks: block 0 = plain summary, no annotations (131 / 121 / 129 chars); block 1 = `annotations.audience=["user"]` JSON payload (156 KB / 156 KB / 188 KB). Total wire 171 KB / 171 KB / 202 KB, i.e. one copy. `tools/list` shows `outputSchema` None for all three. PASS
- Break 1, non-UI client (no extension): all three return only summary text (+ `resource_link` for graph/map), no payload block, no `structuredContent`; 265-794 bytes. PASS
- Break 2, boundary/malformed queries: no-match query `zzzz-no-such-thing-qwxv` (structure + dashboard), 20,000-char query, empty query → all well-formed 2-block results, no `structuredContent`, no crash. PASS
- Break 3, hostile input: dashboard query `'; db.dropDatabase(); </script><img src=x onerror=alert(1)> ☃ $(rm -rf /)` → returned normally, query is JSON-escaped in the user block (`☃`), summary block does not start with JSON; the JS renders via `esc()` before `innerHTML`. PASS
- Break 4, other graph tools: `search_memory(visualize=True)` and `query_memory(visualize=True)` → summary/rows block + 1 user block, no `structuredContent`. PASS
- Break 5, fallback really gone (headless proxy re-run, real viewer HTML + ext-apps 0.4.0 in a fake host): single-copy → "2 nodes · 1 edges", "47 of 47 chunks…", dashboard KPI cards, pageerrors=[]; structuredContent-only → "No graph data in tool result." / "No dashboard data in tool result.". Reproduces the SWE's numbers exactly. PASS
- Caveat: the proxy renders the small seed payloads, not the 156-190 KB real-data payload; the real-data payload is verified only on the wire (shape, single block, valid JSON), not rendered in an iframe. Not blocking; the HUMAN check covers it.

**Acceptance criteria**
- [x] PASS — `structured_content is None` + exactly one `audience=["user"]` block: unit tests named in the SWE log pass; wire check above on real data.
- [x] PASS — no `structuredContent` in `_GRAPH_HTML` / `_DASHBOARD_HTML`, `r.content` read stays: `test_graph_html_reads_only_the_content_blocks`, `test_dashboard_html_reads_only_the_content_blocks`, `test_no_mcp_source_sends_structured_content`; the diff shows the JS fallback deleted in both templates.
- [x] PASS — `test_graph_tools.py` reads the content block (diff: 3 assertions); `make memory-tests` green.
- [ ] AWAITING HUMAN — [HUMAN] Claude Desktop render check. Not verifiable by the Tester. The SWE's instructions were reviewed and are complete and correct: `TREE_MEMORY__MODE=graphrag` in the `tree-memory-local` env is needed (`default.yaml` `memory.mode: rag`, dashboard unregistered in rag; confirmed the `.mcp.json` entry has no such env), clustering run needed for the map (`make memory-run-clustering-pipeline` exists), restart Claude Desktop, three tool prompts with the expected headers, and a restore rule that flips BOTH tools + tests + source guard. The wire evidence above also shows the exact result shape Desktop will receive.
- [x] PASS — `make memory-format-check`, `make memory-lint-check`, `make pre-commit` green.

**Other issues found (not blocking)**
- A 20,000-char query is echoed verbatim into the model-visible summary line (`visualize_memory_structure`); pre-existing, outside this task's scope.
- `dashboard_app.py` / `viz_app.py` docstrings still mention camelCase `structuredContent` as prose explaining why Prefab does not work; accurate and intentional (the guard checks only `structured_content`).
- The proxy cannot prove what Claude Desktop forwards; that is exactly the pending [HUMAN] AC.

**VERDICT: PASS** (all machine-verifiable ACs; the [HUMAN] Claude Desktop AC remains explicitly PENDING and must be ticked by the human before the task is considered done).

### [PA] 2026-10-05 22:10 — Acceptance Review

**VERDICT: ACCEPT**

Feature-level review of `request-scoped-users` (PR #46, tasks 185–189, ADR-014, glossary). Reviewed evidence from the Tester log entry; all machine-verifiable acceptance criteria verified from the user's POV. Reviewed the single-copy payload. The [HUMAN] Claude Desktop inline-render check (stdio, graphrag) stays open and is not grounds for REJECT; the restore rule is recorded in ADR-014 §5. Hand off to the PR Reviewer.
