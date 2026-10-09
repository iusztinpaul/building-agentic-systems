---
id: 196-bug-mcp-apps-no-graph-data-on-horizon
status: pending
feature: bug-mcp-apps-no-graph-data-on-horizon
---

# Bug: On Horizon, every MCP App view shows "No graph data in tool result." and the model gets a download link instead

**Severity:** S2 — a documented feature (inline MCP App views, ADR-005 decision 4) is broken on the production golden path.
**Affected component(s):** `apps/memory/src/tree/mcp/viz_app.py` (`_graph_tool_result`), `apps/memory/src/tree/mcp/dashboard_app.py` (`memory_dashboard`)
**First observed:** 2026-10-09 on the Horizon server (`https://tree-memory.fastmcp.app/mcp`) from claude.ai. Most likely present since the capability gate landed (`ed599ac`, 2026-06-03): no `tasks/done` record shows an inline render ever succeeding on Horizon (task 189's [HUMAN] checks (e)/(f) and task 188's Desktop check were never ticked).

## Summary

From claude.ai / Claude Desktop against the production server, `visualize_memory_structure` mounts the "Tree: Your Rooted Memory" iframe, but the iframe stays empty with "No graph data in tool result.", and the model receives the fallback text ("…Since this client does not render inline MCP App UIs, I rendered a self-contained interactive graph as a download… The link expires in about 5 minutes."). The same tools render inline against the local stdio server. Every app-declaring tool is affected: rag `visualize_memory_structure` / `visualize_memory_embeddings`, graphrag `visualize_memory_structure` / `query_memory(visualize=true)` / `search_memory(visualize=true)`, and graphrag `memory_dashboard` ("No dashboard data in tool result.").

## Reproducer (deterministic)

Local env (`make env-status` → local, `make local-start` up). Docker holds port 8000, so use 8765/8766.

1. Serve the MCP server STATEFUL: `cd apps/memory && MCP_SKIP_INDEX_BOOTSTRAP=true FASTMCP_PORT=8765 uv run --env-file ../../.env python scripts/serve_mcp.py --transport streamable-http`
2. Serve it STATELESS (what Horizon effectively runs — see Notes): same command with `FASTMCP_STATELESS_HTTP=true FASTMCP_PORT=8766` (the log says "(stateless)").
3. Connect to each with a `fastmcp.Client(StreamableHttpTransport(url, headers={"horizon-actor-email": <.env TREE_USER_IDENTIFIER>}))` whose `initialize` advertises the MCP Apps extension: `capabilities = {"extensions": {"io.modelcontextprotocol/ui": {"mimeTypes": ["text/html;profile=mcp-app"]}}}` (`fastmcp.apps.config.UI_EXTENSION_ID` / `UI_MIME_TYPE`).
4. Call `visualize_memory_structure()` on each.

Expected: both servers answer the INLINE branch — a summary block plus a `TextContent` block with `annotations.audience == ["user"]` whose JSON has `nodes: [...]` — so the iframe's `ontoolresult` handler (`viz_app.py:378-390`) renders the graph.
Actual: the stateful server answers inline (≈256 KB payload, 413 nodes rendered); the STATELESS server answers the file branch — summary + `resource_link graphs://<token>.html.gz application/gzip`, NO payload block — while `tools/list` still declares `_meta.ui.resourceUri = "ui://tree-memory/graph.html"`, so a UI host mounts the iframe and its handler prints "No graph data in tool result." (replayed the real handler on both wire results: inline → "rendered 413 nodes"; stateless → "No graph data in tool result.").

Root mechanism (verified, MCP SDK 1.26.0 / FastMCP 3.2.0): stateless streamable-http builds a fresh `ServerSession(stateless=True)` per request (`mcp/server/streamable_http_manager.py:152-193`) whose `_client_params` stays `None` (`mcp/server/session.py:88-98`; set only on an `InitializeRequest` in the same session, `:166-168`), so `ctx.client_supports_extension(UI_EXTENSION_ID)` (`fastmcp/server/context.py:579-604` → `fastmcp/server/low_level.py:55-72`) is ALWAYS `False` on `tools/call`, whatever the client advertised at initialize.

## Suspected localisation

- `apps/memory/src/tree/mcp/viz_app.py:194-195` — `ui_supported = ctx.client_supports_extension(UI_EXTENSION_ID)` is a two-state gate on initialize-time capabilities; on a stateless request it is always `False`, so the file branch (`:216-284`) answers with no payload in any channel.
- `apps/memory/src/tree/mcp/dashboard_app.py:121` — the same gate for `memory_dashboard` (graphrag only; not registered on today's rag prod, same failure when it is).
- App declarations that make the host mount the iframe regardless of the per-call branch: `tools.py:554`, `tools.py:562`, `graph_tools.py:149/255/321`, `dashboard_app.py:89` (SEP-1865: the HOST decides to render from `tools/list` `_meta.ui.resourceUri`).

> Hypotheses about Horizon's transport are inferred, not observed (Horizon issues the `mcp-session-id` at its gateway, does not guarantee instance affinity, and the prod symptom is a silent fallback rather than the 404 "Session not found" a stateful server on a fresh instance answers). The local stateless reproducer is exact either way: any request whose session never saw `initialize` takes the file branch.

## Out of scope

- Moving the payload to `structured_content` (ADR-014 §5's "restore rule", referenced by task 189's note on "No graph data in tool result.") — the gate is wrong, not the channel; the file branch carries no payload in ANY channel, so this does not fix the bug.
- Making Horizon stateful (`FASTMCP_STATELESS_HTTP=false`) or adding sticky sessions — Horizon owns routing without affinity; a stateful server on a fresh instance answers hard 404s.
- Persisting client capabilities across requests (middleware keyed on Horizon's `mcp-session-id`) — new state, depends on unverified gateway behaviour, fails for sessionless protocol versions; revisit only if the chosen fix measurably fails a host.
- The 6 MB / payload-size behaviour of the inline view, Graph-file TTL, and the text-only `search_memory` result (unaffected).

## Acceptance criteria

- [ ] **Regression test** added at `apps/memory/tests/unit/mcp/test_viz_app_stateless.py` (name the test after the symptom, e.g. `test_ui_client_on_stateless_http_gets_the_graph_payload`) that drives a REAL stateless streamable-http app in-process (e.g. FastMCP's `http_app(stateless_http=True)` over an httpx ASGI transport — no network, no Mongo beyond the existing fakes) with a client that advertises `io.modelcontextprotocol/ui` at initialize, calls a graph tool, and asserts the result carries the `audience=["user"]` payload block with `nodes`. It FAILS on `main` (`af5088c`) and PASSES on the fix branch. A companion assertion covers `memory_dashboard`'s payload under the same conditions.
- [ ] Reproducer steps 1–4 above: the STATELESS server (8766) answers with the payload block, and the iframe handler renders the graph (re-run the handler replay or open the view in Claude Desktop against the local HTTP server).
- [ ] When the capability is UNKNOWN (no initialize-time client params on this request), the result still carries a `graphs://` Graph download link as well, so a text-only client on Horizon keeps a working file path; when the client's params are present and it did NOT advertise the extension, the result is unchanged from today (file branch only). The existing mocked tests in `test_viz_app.py`, `test_graph_tools.py`, `test_tools.py`, `test_dashboard_app.py`, `test_tools_request_user.py` are updated only where the three-state rule requires it.
- [ ] Every app-declaring tool's description (rag `visualize_memory_structure` / `visualize_memory_embeddings`, graphrag `visualize_memory_structure` / `query_memory` / `search_memory`'s `visualize`, `memory_dashboard` if it has a file path) gains ONE sentence telling clients that cannot display interactive MCP App views (e.g. a terminal client such as Claude Code) to pass `as_html_file=true` — which answers file-only, so those clients never receive the payload block. Pinned by a test that reads the registered tool descriptions.
- [ ] Live check of the cost: record in the Log whether Claude Code (a text-only client) shows an `audience=["user"]` block to its model (e.g. call the tool with `as_html_file=false` from Claude Code against the local stateless HTTP server and inspect what the model receives).
- [ ] No unrelated behaviour changes: `make memory-format-check`, `make memory-lint-check`, `make pre-commit`, `make memory-tests` green (env-status local).
- [ ] Docs: ADR-014 Status-line amendment for §5/§6 (the gate is three-state; the deferred "file fallback in the Horizon `client_supports_extension` check" is resolved by this task), the `_graph_tool_result` docstring's "never one or the other" rule updated, `docs/notes/mcp_server_design.md:72-80`'s "Unverified on Horizon" note resolved, and task 189's "No graph data in tool result. → restore rule" note corrected (PA-owned files go through the PA).
- [ ] [HUMAN] After merge (Horizon redeploys from `main`): in claude.ai / Claude Desktop, `visualize_memory_structure` against `tree-memory` renders the tree inline; evidence in the task Log.

## Notes for the SWE

- **Decision (human, 2026-10-09): ship the three-state "unknown → payload block + download link" fix now**, plus the `as_html_file=true` description hint for text-only clients. Persisting capabilities per Horizon session (Out of scope) stays a follow-up only if text-only clients measurably still receive the payload.

- Preferred fix shape (triage recommendation — confirm with the PA, it amends ADR-014): make the gate THREE-state in `_graph_tool_result` (and the same rule in `memory_dashboard`): **supported** → inline only (today); **declined** (`ctx.session.client_params` present, extension absent) → file only (today); **unknown** (`client_params is None`, i.e. stateless/no session) → the `audience=["user"]` payload block AND the `graphs://` link. `client_params` is a public property (`mcp/server/session.py:106-108`); guard `ctx.session` outside a request context. One helper still owns both paths (ADR-005 decision 4); the JSON travels once per response and a `ResourceLink` is not a copy (ADR-014 §5 holds).
- Known cost on Horizon, to state in the ADR amendment: every visualize call then pays the Mongo Graph-file insert plus the inline payload (≈256 KB for 14 docs locally), and text-only Horizon clients (Claude Code, Codex) also receive the `audience=["user"]` JSON block — whether each host hides it from the model is unverified; check Claude Code's behaviour live and record it.
- Forbidden fix shapes: switching the payload to `structured_content` as "the fix"; making the server stateful; swallowing the capability check in a try/except that defaults to file-only.
- Evidence and scripts from triage (session scratchpad, not in the repo): `repro_ui_gate.py` (UI-capable client, cells stateful/stateless/no-caps), `iframe_sim.mjs` (replays the real `ontoolresult` handler), `probe_session.py` (`client_params` None only when stateless). Re-create what the regression test needs inside the repo.

---

Blocked by: (none)

## Log

### [Triage] 2026-10-09 — Groomed from the prod report

**Summary**
User report with two claude.ai screenshots: `search_memory` (text) works against `tree-memory`; `visualize_memory_structure` mounts the app iframe showing "No graph data in tool result." and the model receives the download-link fallback. Localised to the initialize-time capability gate under stateless streamable-http; reproduced deterministically with a local stateful vs stateless server pair and a UI-capable client (inline vs fallback), and the iframe half by replaying the served `ontoolresult` handler on both wire results. PR #46 ("send the MCP App payload once") removed nothing that ever made prod work — the file branch never carried a payload.
