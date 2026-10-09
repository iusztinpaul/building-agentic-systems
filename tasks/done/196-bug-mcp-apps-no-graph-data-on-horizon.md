---
id: 196-bug-mcp-apps-no-graph-data-on-horizon
status: done
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

- [x] **Regression test** added at `apps/memory/tests/unit/mcp/test_viz_app_stateless.py` (name the test after the symptom, e.g. `test_ui_client_on_stateless_http_gets_the_graph_payload`) that drives a REAL stateless streamable-http app in-process (e.g. FastMCP's `http_app(stateless_http=True)` over an httpx ASGI transport — no network, no Mongo beyond the existing fakes) with a client that advertises `io.modelcontextprotocol/ui` at initialize, calls a graph tool, and asserts the result carries the `audience=["user"]` payload block with `nodes`. It FAILS on `main` (`af5088c`) and PASSES on the fix branch. A companion assertion covers `memory_dashboard`'s payload under the same conditions.
- [x] Reproducer steps 1–4 above: the STATELESS server (8766) answers with the payload block, and the iframe handler renders the graph (re-run the handler replay or open the view in Claude Desktop against the local HTTP server).
- [x] When the capability is UNKNOWN (no initialize-time client params on this request), the result still carries a `graphs://` Graph download link as well, so a text-only client on Horizon keeps a working file path; when the client's params are present and it did NOT advertise the extension, the result is unchanged from today (file branch only). The existing mocked tests in `test_viz_app.py`, `test_graph_tools.py`, `test_tools.py`, `test_dashboard_app.py`, `test_tools_request_user.py` are updated only where the three-state rule requires it.
- [x] Every app-declaring tool's description (rag `visualize_memory_structure` / `visualize_memory_embeddings`, graphrag `visualize_memory_structure` / `query_memory` / `search_memory`'s `visualize`, `memory_dashboard` if it has a file path) gains ONE sentence telling clients that cannot display interactive MCP App views (e.g. a terminal client such as Claude Code) to pass `as_html_file=true` — which answers file-only, so those clients never receive the payload block. Pinned by a test that reads the registered tool descriptions.
- [x] Live check of the cost: record in the Log whether Claude Code (a text-only client) shows an `audience=["user"]` block to its model (e.g. call the tool with `as_html_file=false` from Claude Code against the local stateless HTTP server and inspect what the model receives).
- [x] No unrelated behaviour changes: `make memory-format-check`, `make memory-lint-check`, `make pre-commit`, `make memory-tests` green (env-status local).
- [x] Docs: ADR-014 Status-line amendment for §5/§6 (the gate is three-state; the deferred "file fallback in the Horizon `client_supports_extension` check" is resolved by this task), the `_graph_tool_result` docstring's "never one or the other" rule updated, `docs/notes/mcp_server_design.md:72-80`'s "Unverified on Horizon" note resolved, and task 189's "No graph data in tool result. → restore rule" note corrected (PA-owned files go through the PA).
- [x] [HUMAN] After merge (Horizon redeploys from `main`): in claude.ai / Claude Desktop, `visualize_memory_structure` against `tree-memory` renders the tree inline; evidence in the task Log.

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

### [SWE] 2026-10-09 17:24 — Implementation

**Files modified**
- `apps/memory/src/tree/mcp/viz_app.py`: new `_ui_capability(ctx) -> "supported" | "declined" | "unknown"`. It returns unknown when `ctx.request_context is None` (the guard for `ctx.session` outside a request) or `ctx.session.client_params is None`; otherwise it calls `client_supports_extension`. `_graph_tool_result` now has three branches: supported → inline only (unchanged); declined or `as_html_file` → file only (unchanged, same text); unknown → summary text, the `audience=["user"]` payload block and the `graphs://` link (no stdio local file or browser). The module docstring and the `_graph_tool_result` docstring now describe the three-state rule instead of "never one or the other".
- `apps/memory/src/tree/mcp/dashboard_app.py`: `memory_dashboard` uses the same helper. Declined → the text string (unchanged). Supported or unknown → the payload `ToolResult`. Unknown adds a "if this client cannot show it, use query_memory / search_memory" clause.
- `apps/memory/src/tree/mcp/tools.py`: rag `visualize_memory_structure` and `visualize_memory_embeddings` each get one new description sentence (pass `as_html_file=true`).
- `apps/memory/src/tree/mcp/graph_tools.py`: graphrag `visualize_memory_structure` gets the same sentence. `query_memory` and `search_memory` get a variant (see Deviations). The `_dual_graph_result` docstring now names the three states.
- `apps/memory/tests/unit/mcp/test_viz_app_stateless.py` (new): runs a real in-process `http_app(stateless_http=…, json_response=True)` over `httpx.ASGITransport` and speaks raw JSON-RPC like a host (initialize with or without the UI extension, then `tools/list` / `tools/call`). It runs the real rag `visualize_memory_structure` and `memory_dashboard` with their readers patched. Tests:
  - the regression test and the dashboard companion
  - stateful+UI → inline only
  - stateful without UI → file only
  - stateless + `as_html_file` → file only
  - a 5-way test that reads the registered descriptions (`Tool.from_function(fn).description`)
- `apps/memory/tests/unit/mcp/test_viz_app.py`: a three-state table for `_ui_capability`; unknown (stateless session, or no request context) × (http, stdio) → payload + link, text never says "does not render", no local file or browser; unknown + `as_html_file` → file only. No existing test changed.
- `apps/memory/tests/unit/mcp/test_dashboard_app.py`: new test for unknown → payload. No existing test changed.

**Tests**
- Unit: 5311 passed, 0 failed (`make memory-tests`, env-status local).
- `make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check && make pre-commit`: all clean.
- Integration: N/A (no integration suite).

**Acceptance criteria**
- [x] Regression test: `test_viz_app_stateless.py::test_ui_client_on_stateless_http_gets_the_graph_payload` and `::test_ui_client_on_stateless_http_gets_the_dashboard_payload`. RED on the unfixed source, GREEN on the fix (evidence below). The unfixed source is `git diff af5088c HEAD -- apps/memory/src` = empty: the branch only adds the task file.
- [x] Reproducer: the stateless server answers payload + link, and the real `ontoolresult` handler renders it (evidence below).
- [x] Unknown → payload + `graphs://` link; declined → file only (unchanged). Pinned by `test_viz_app.py::test_an_unknown_capability_answers_the_payload_and_the_download`, `::test_the_capability_has_three_states` and `test_viz_app_stateless.py::test_non_ui_client_on_stateful_http_gets_only_the_download`. The existing mocked suites pass unedited: a `MagicMock` ctx has a non-None `session.client_params`, so it still lands on supported/declined.
- [x] Description sentence: `test_viz_app_stateless.py::test_every_graph_tool_description_tells_text_only_clients_to_ask_for_the_file` (5 cases).
- [x] Live check of the cost: done; see "Claude Code" below.
- [x] format / lint / pre-commit / memory-tests all pass.
- [ ] Docs: done for the docstrings in my remit. The ADR-014, `docs/notes/mcp_server_design.md` and task-189 edits are PA-owned; proposed wording is below.
- [ ] [HUMAN] post-merge claude.ai / Desktop check.

**Evidence**
```
# RED: new tests on the unfixed src (HEAD 70d7723; src identical to af5088c)
E       ValueError: not enough values to unpack (expected 1, got 0)   # _payload_blocks(result) == []
FAILED tests/unit/mcp/test_viz_app_stateless.py::test_ui_client_on_stateless_http_gets_the_graph_payload
FAILED tests/unit/mcp/test_viz_app_stateless.py::test_ui_client_on_stateless_http_gets_the_dashboard_payload
2 failed, 3 passed            # the 3 controls (stateful UI / stateful no-UI / as_html_file) already pass on main
# RED: the description pin before the docstring edits (5 parametrized cases)
FAILED tests/unit/mcp/test_viz_app_stateless.py::test_every_graph_tool_description_tells_text_only_clients_to_ask_for_the_file[rag-visualize_memory_structure]
FAILED …[visualize_memory_embeddings]  FAILED …[graphrag-visualize_memory_structure]  FAILED …[query_memory]  FAILED …[search_memory]
5 failed, 230 passed          # (with test_viz_app.py + test_dashboard_app.py)

# GREEN after the fix
$ make memory-tests
======================= 5311 passed in 103.09s (0:01:43) =======================

# Live reproducer: fixed code, local servers on 8765 (stateful) and 8766 (stateless, log shows "(stateless)").
# UI-capable fastmcp client with the horizon-actor-email header, visualize_memory_structure()
B stateless + UI caps : text(len=555) + text(audience=['user'], payload, len=259931) + resource_link graphs://…html.gz
A stateful  + UI caps : text(len=179) + text(audience=['user'], payload, len=259931)          # inline only, unchanged
C stateful, NO caps   : text(len=558 "…Since this client does not render…") + resource_link   # declined, unchanged
D stateless, NO caps  : text + payload + resource_link                                         # unknown (the known cost)
# The real ontoolresult handler (byte-identical to the served viz_app.py source) replayed on the wire results:
result_stateful.json:  iframe shows -> "rendered 417 nodes"
result_stateless.json: iframe shows -> "rendered 417 nodes"     # was "No graph data in tool result." before the fix
# stateless + as_html_file=true: [text, resource_link], no payload; the graphs:// link read back on a
# separate stateless request: 311295-char page
```

**Claude Code (text-only client): does it show `audience=["user"]` blocks to its model? Yes.**
Setup: Claude Code 2.1.295, headless `claude -p --strict-mcp-config --mcp-config {"treelocal": http://127.0.0.1:8766/mcp + horizon-actor-email}`, against the local stateless server running the fix.
- `visualize_memory_structure(query='agents', top_k=1, as_html_file=false)`: the model reported ONE text block (Claude Code merged the blocks) and quoted the first 3 payload node ids verbatim. Those ids appear only in the `audience=["user"]` block, and I cross-checked them against the server's own answer. So Claude Code does NOT filter `audience=["user"]`.
- `visualize_memory_structure(as_html_file=false)` on the whole memory (about 280 KB) was worse: the model got no content at all. Claude Code reported "result (280,593 characters) exceeded the token limit and was saved to a file", so the summary and the download link ended up behind a file read.
- The description hint works when Claude Code chooses the arguments itself. Prompt: "Show me how my memory is organised, as a picture." Both haiku and sonnet called `visualize_memory_structure` with `{"as_html_file": true}`, i.e. file-only with no payload.
- Takeaway for the PA and the human: text-only clients DO measurably receive the payload whenever they don't pass `as_html_file=true`. Per the 2026-10-09 decision, this is the trigger to file the out-of-scope follow-up (persist capabilities per Horizon session). The hint covers the natural-prompt case tested here; an explicit `as_html_file=false` or a model that ignores the hint still gets the payload.

**Deviations / decisions (no new architecture)**
- `query_memory` / `search_memory` have no `as_html_file` parameter, and `_dual_graph_result` never passes one. I did not add one, because that would change their signatures. Their sentence instead tells text-only clients to leave `visualize` false and call `visualize_memory_structure` with `as_html_file=true` for a picture. The `as_html_file=true` literal is still in all 5 descriptions. The test reads `Tool.from_function(fn).description` rather than `mcp.get_tool(...)`, because which `visualize_memory_structure` the shared `mcp` holds depends on test import order.
- `memory_dashboard` has no file renderer, so it has no `graphs://` leg and no description sentence (the AC says "if it has a file path"). Unknown → payload; declined → text, as today.
- Unknown on stdio writes no local file and opens no browser. This branch is unreachable in practice, since a stdio session always saw `initialize`; the docstring says so.
- If the Graph-file insert fails on the unknown branch, the answer is the `storage_unavailable` envelope (same as declined), not inline-only. One error path; the data read had to reach the same Mongo just before.
- The regression test uses `json_response=True` so `httpx.ASGITransport`, which buffers, needs no SSE parsing. It does not affect the gate. The ASGI lifespan runs via `app.router.lifespan_context(app)`, because `asgi_lifespan` is not a declared dependency.
- I also changed each visualize tool's `as_html_file` Args line: "…instead of the inline interactive view, or when this client cannot display it." Without that, it said "only when the user explicitly asks", which contradicts the new sentence.
- The mitigation sample is small: one natural-prompt run per model (haiku and sonnet). The result is encouraging, not proof.
- The worktree venv was missing the `local-models` extra, so 8 test modules failed to collect. I ran `uv sync --extra local-models` (environment only, nothing tracked changed). The `.env` symlink in the worktree root is gitignored; it is local only and must never be committed.

**Proposed PA wording (I did not edit these PA-owned files)**
- ADR-014 Status line, append: "— §5/§6 amended by task 196: the capability gate is THREE-state (`viz_app._ui_capability`). supported → inline payload block only; declined (the client's initialize params are known and lack `io.modelcontextprotocol/ui`) → Graph file only; unknown (`client_params is None`, i.e. every `tools/call` on stateless streamable-http such as Horizon) → the `audience=["user"]` payload block AND the `graphs://` link in one response. The payload still travels once. The cost on Horizon: one Graph-file insert per visualize call, and text-only clients receive the payload block. Claude Code shows it to the model; it opts out with `as_html_file=true`, which every app tool's description now asks for. §6's deferred 'file fallback in the Horizon `client_supports_extension` check' is resolved: it was real (always file on stateless Horizon), and it caused the empty 'No graph data in tool result.' iframe, not a `structuredContent` host behaviour. The §5 restore rule was NOT triggered."
- ADR-014 Consequences, "What would justify upgrading" clause: replace "a file fallback in the Horizon `client_supports_extension` check → session capabilities carried in the request (follow-up task)" with "text-only clients receiving the payload block on stateless Horizon (measured in task 196: Claude Code shows it to the model, and the 280 KB full-memory view overflows its tool-result limit) → persist initialize capabilities per Horizon session (follow-up task)".
- `docs/notes/mcp_server_design.md:72-80` (§4): replace the "**Unverified on Horizon** — one Claude Desktop call settles it." sentence with "**Verified (task 196):** on stateless streamable-http every request's session has `client_params is None`, so the two-state check always picked the file branch on Horizon, and the host's mounted iframe showed 'No graph data in tool result.'. The gate is now three-state; an unknown capability answers the payload block and the `graphs://` link." Also, §3 ("The UI payload is sent twice") is stale since ADR-014 §5 / PR #46. Suggest marking it resolved.
- `tasks/done/189…md:260` note (e): replace '"No graph data in tool result." means task 188\'s restore rule applies.' with '"No graph data in tool result." meant the stateless capability gate took the file branch (task 196), not that the host drops `content` blocks. The restore rule does not apply.'

### [Tester] 2026-10-09 18:10 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check && make memory-lint-check && make pre-commit`, all clean, env-status local)
- Unit tests: 5311 passed / 0 failed (`make memory-tests`, own run). No integration suite exists.
- Warnings: 1 pre-existing third-party UserWarning at collection (opik, "Core Pydantic V1 ... Python 3.14"); none from this change.

**Red-first (independent)**: `git archive af5088c` src+tests into a scratch dir, added the new `test_viz_app_stateless.py`, ran with `PYTHONPATH=<scratch>/src` (confirmed `tree.__file__` in scratch): 7 failed / 3 passed — the graph + dashboard regression tests and the 5 description cases fail; the 3 controls pass. Scratch removed.

**E2E adversarial pass** (servers: stateful 8765, stateless 8766, killed afterwards; raw JSON-RPC probe, `visualize_memory_structure`, query=agents top_k=2)
- Happy path stateless + UI caps: text + `audience=["user"]` payload (11980 chars) + `graphs://` link. PASS. Stateful + UI: text + payload only. PASS.
- Iframe: real `ontoolresult` loop (copied from viz_app.py:430-441) replayed on the stateless wire result -> "rendered 19 nodes". PASS.
- Link read-back on a separate stateless client: html page 63344 chars, starts with doctype. A different user (`someoneelse@example.com`) is refused (unknown-user error, no content). PASS.
- Capability variants, stateful: proper / empty mimeTypes / wrong mimeType / `{}` extension value -> inline only (supported); `extensions:{}` / other extension / no caps -> file only (declined, NO payload leaked). PASS (note below on mimeTypes).
- Capability variants, stateless: all 7 initialize shapes AND a tools/call with no initialize at all -> text + payload + link (unknown). PASS. Stateful with no initialize -> HTTP 400 "Missing session ID" (unchanged).
- `as_html_file=true` and `"yes"` (lax-coerced) on stateless with UI caps -> text + link, no payload. PASS.
- Hostile/boundary: shell/XSS/traversal query -> "No results ... nothing to draw." (no echo issue, no crash); no-match query, `max_docs=-5`, `top_k=0` -> clean `invalid_input` envelopes / no-results text. PASS.
- Concurrency: 10 parallel stateless calls -> each text+text+link, 10 unique link names. PASS.
- Failure mode: unknown capability + `GraphFile.insert` raising -> `storage_unavailable` envelope string (ad hoc test, file removed). Matches the SWE-documented decision; not covered by a committed test (see note).
- Outside request context: `ctx.request_context is None` -> unknown, covered by the committed `test_an_unknown_capability_answers_the_payload_and_the_download[no-request-context]`.
- Claude Code spot-check: `claude -p --model haiku` with "Show me how my memory is organised, as a picture." against the stateless server called `visualize_memory_structure {"as_html_file": true}`. SWE's claim reproduced (n=1).

**Acceptance criteria**
- [x] PASS — regression test (red on af5088c, green here; dashboard companion included) — see red-first above.
- [x] PASS — reproducer 1-4: stateless answers payload + link; handler renders it.
- [x] PASS — unknown -> payload + link; declined -> file only (probe cells above); existing suites pass unedited.
- [x] PASS — description sentence in all 5 app-declaring tools; `test_every_graph_tool_description_...` (5 cases) green and red on af5088c. `memory_dashboard` has no file path (no sentence needed).
- [x] PASS — live cost recorded by the SWE (Claude Code shows `audience=["user"]` blocks to the model; 280 KB overflow; haiku opt-out reproduced by me).
- [x] PASS — format / lint / pre-commit / memory-tests green.
- [x] PASS — Docs: ADR-014 Status-line carries the three-state amendment and the file-fallback resolution, `_graph_tool_result` docstring no longer says "never one or the other", `docs/notes/mcp_server_design.md` section 4 resolved, task 189 correction appended, glossary rows updated.
- [ ] Awaiting human verification — [HUMAN] post-merge Horizon check.

**Other issues found (non-blocking)**
- `client_supports_extension` only checks that the `io.modelcontextprotocol/ui` key exists, so a stateful client advertising empty/wrong `mimeTypes` is treated as supported. Pre-existing, unchanged by this fix; follow-up candidate only.
- On stateless Horizon a client that explicitly declined at initialize cannot be told apart (inherent to the unknown state): it receives the payload block. That is the documented cost; task 197 owns the fix.
- Unknown branch + Graph-file insert failure returns `storage_unavailable` with no payload, so a UI host's iframe would still show nothing even though the data read succeeded. Same outcome as the declined path, documented by the SWE; there is no committed test for this exact combination (the existing failed-insert test uses declined). Suggest a one-line parametrization of `test_a_failed_insert_answers_storage_unavailable` with an unknown ctx if the SWE touches the file again.
- Importing `tree.mcp.viz_app` as the first import raises a circular ImportError (via `tree.mcp.tools`). The import list is identical on af5088c, so pre-existing; tests and servers import `tools` first.

**VERDICT: PASS**

### [Orchestrator] 2026-10-09 18:30 — Post-merge Horizon check

Horizon's live deployment is `main 2383b58`. The user's Claude Desktop screenshot (18:29) shows `visualize_memory_embeddings` rendering the Embedding map INLINE (3 clusters, legend, stale-map warning) against `tree-memory` on Horizon — no more "No graph data in tool result.". Horizon traffic logs confirm the cause: Desktop connects via `mcp-remote 0.14.3` (protocol 2025-11-25) and advertises `io.modelcontextprotocol/ui` at `initialize`, but its `tools/call` requests carry no `_meta` and reach a session without client params (→ `unknown`); Claude Code 2.1.295 speaks 2026-07-28 and sends `io.modelcontextprotocol/clientCapabilities` (no UI extension) in every request's `_meta`, so it is classified `declined` and gets file-only — the text-only cost does not apply to it in prod.
