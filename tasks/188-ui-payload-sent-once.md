---
id: 188-ui-payload-sent-once
status: pending
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

- [ ] `_graph_tool_result`'s inline result and `memory_dashboard`'s result have `structured_content is
      None` and exactly one `audience=["user"]` JSON block carrying the payload (tests).
- [ ] Neither `_GRAPH_HTML` nor `_DASHBOARD_HTML` contains `structuredContent`; the `content`-block read
      stays (tests + source guard).
- [ ] `test_graph_tools.py` assertions read the content block; `make memory-tests` green.
- [ ] [HUMAN] Claude Desktop (stdio, graphrag) renders the graph view (structure + map) and the dashboard
      inline with the single-copy result — or the restore rule was applied to both tools and the Log says
      what was seen.
- [ ] `make memory-format-check`, `make memory-lint-check`, `make pre-commit` green.

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
