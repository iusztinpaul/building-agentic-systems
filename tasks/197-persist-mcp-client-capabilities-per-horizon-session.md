---
id: 197-persist-mcp-client-capabilities-per-horizon-session
status: pending
feature: bug-mcp-apps-no-graph-data-on-horizon
---

# Persist initialize-time MCP client capabilities per Horizon `mcp-session-id`, so a text-only client on stateless Horizon gets the **Graph file** only and a UI client the inline view only — after verifying that Horizon's gateway forwards a stable session id upstream

Tags: `mcp`, `horizon`, `enhancement`
Depends on: 196 (the three-state `viz_app._ui_capability` helper and the in-process stateless harness
`apps/memory/tests/unit/mcp/test_viz_app_stateless.py`)
Blocks: —
Implements: ADR-014 §6's deferred "session capabilities carried in the request" (named in 014's task-196
Status-line note). This task adds NEW state (a store keyed on a gateway session id), so it gets its own
ADR-014 Status-line clause — PA-applied at pick-up, after the verification AC below has an answer.

## Problem

Task 196 made the capability gate three-state. On stateless streamable-http (what Horizon runs) the MCP
SDK builds a fresh `ServerSession(stateless=True)` per request whose `client_params` stay `None`
(`mcp/server/streamable_http_manager.py:152-193`, `mcp/server/session.py:88-98`), so EVERY `tools/call`
on Horizon is `unknown`, and every visualize answer carries BOTH the `audience=["user"]` **Graph payload**
block and the `graphs://` **Graph download** link. The human's 2026-10-09 decision pre-approved this
follow-up on one condition: "only if text-only clients measurably still receive the payload". The SWE
measured it as met (task 196 Log, Claude Code 2.1.295, local stateless server running the fix):

- `visualize_memory_structure(query='agents', top_k=1, as_html_file=false)`: Claude Code merged the
  blocks into one text result and its model quoted the first 3 payload node ids verbatim — ids that occur
  only in the `audience=["user"]` block. Claude Code does NOT hide `audience=["user"]` from its model.
- `visualize_memory_structure(as_html_file=false)` on the whole memory: 280,593 characters; Claude Code
  reported "result … exceeded the token limit and was saved to a file", so the model saw nothing — not
  the summary, not the link.
- Mitigation in place: every app tool's description tells text-only clients to pass `as_html_file=true`
  (file only, no payload). Prompted "Show me how my memory is organised, as a picture", haiku and sonnet
  each chose `{"as_html_file": true}` — one run each, so encouraging, not proof. An explicit
  `as_html_file=false`, or a model that ignores the hint, still gets the payload.

The cure the plan foresaw: remember what the client said at `initialize` and read it back on
`tools/call`, keyed on the session id Horizon's gateway issues — then a Horizon `tools/call` is
`supported` or `declined` like a stateful one, and `unknown` only for a session the store never saw.

## Open risk — verify FIRST, before writing any store

The SDK issues NO `mcp-session-id` in stateless mode (`streamable_http_manager.py:167-171` passes
`mcp_session_id=None`); the id a Horizon client sees is minted by the GATEWAY. The store only has a key
if the gateway forwards the SAME id to the upstream server on the `initialize` request AND on every later
`tools/call` of that client session. Task 196 inferred, not observed, Horizon's transport ("Horizon
issues the `mcp-session-id` at its gateway, does not guarantee instance affinity"). Unknowns, in order:

1. Does the upstream `initialize` request carry an `mcp-session-id` at all? (The MCP spec has the SERVER
   mint the id on the initialize RESPONSE; a gateway that mints it the same way has no id to forward on
   the initialize request — the store would then have nothing to key the capture on.)
2. Does the following `tools/call` carry the same value as that `initialize`?
3. Which `mcp-protocol-version` do Claude Desktop / claude.ai / Claude Code send? A client on a
   protocol version or transport that carries no session id (plain sessionless POSTs) can never be looked
   up — it stays `unknown`, today's behaviour.
4. Are `initialize` requests forwarded upstream at all, or answered by the gateway? (Horizon advertises
   the server's tools; if it answers `initialize` itself, the server never sees the capabilities.)

Measure them on a PR preview (never on the production server) with ONE throwaway FastMCP `Middleware`
(`on_request` — NOT `resolve_request_user`, which runs only inside tools and the `graphs://` resource and
never sees `initialize`, so it cannot answer (1) or (4)) whose single `warning` log line prints the
JSON-RPC method, the `mcp-session-id` and `mcp-protocol-version` header values and the NAMES (never the
values) of every other request header, for one `initialize` + one `visualize_memory_structure` call from
Claude Desktop (or claude.ai) and one from Claude Code; read the Horizon server logs; revert the line.
Record the four answers in the Log. The task's first AC is this measurement, and the decision table is:

| Finding | Then |
|---|---|
| (1) yes and (2) yes | build the store as scoped below, keyed on that id |
| (1) no, but the gateway adds another per-session correlation header that (2) repeats | key on that header instead; the PA amends this spec with the name |
| (1) no and nothing else correlates | STOP at AC 1 with a Log entry; the PA closes this task as won't-do (the `as_html_file=true` hint stays the only mitigation) or re-grooms around a different mechanism |
| (4) no | STOP, same as above — there is nothing to capture |

Do NOT key on `horizon-actor-email` + `clientInfo.name`: one user runs Claude Desktop (UI) and Claude
Code (text) at the same time, and the two would overwrite each other.

## Scope (only after the verification AC passes)

1. **Capture.** A FastMCP `Middleware` (`fastmcp/server/middleware/middleware.py:149`, `on_initialize`)
   registered on the `mcp` server in `tree.mcp.server`: on every `initialize` whose request carries the
   session-id header, compute "does this client advertise `io.modelcontextprotocol/ui`?" from the
   `InitializeRequestParams.capabilities` the SAME way `client_supports_extension` does
   (`fastmcp/server/low_level.py:55-72` — reuse it; do not re-implement the extension check), and
   upsert one row `{session_id, ui_supported: bool, created_at}`. No session id on the request → no
   write. A failed write logs a warning and `initialize` still succeeds — never a protocol error, never a
   **Tool error envelope** (there is no tool here).
2. **Store.** A Beanie document in `tree.entities` (e.g. `McpClientSession`, collection
   `mcp_client_sessions`), indexes declared ON the model: unique `session_id`; a TTL on `created_at` from
   a new YAML knob `mcp.client_session_ttl_seconds` (default 86400 — a Desktop conversation can run for
   hours; a day bounds the collection; override `TREE_MCP__CLIENT_SESSION_TTL_SECONDS`, not documented in
   `.env.example`), reconciled with `collMod` BEFORE `init_beanie` through the SAME helper the
   **Graph file** TTL uses (ADR-014 §4) — one reconcile path, not two. *Why Mongo, not an in-process
   dict:* Horizon has no instance affinity (compute-model.md), so the `tools/call` lands on an instance
   that never saw the `initialize`; an in-memory map is dead on arrival. *Why not the Graph file
   collection:* different lifetime and a different key.
3. **Read.** `_ui_capability(ctx)` (becomes `async` — both callers already are) consults the store ONLY
   when `client_params is None` AND the request carries the session-id header: a row → `supported` /
   `declined`; no row, expired, no header, or the store unreachable (log a warning) → `unknown`, exactly
   today's answer. Skip the lookup when `as_html_file` is set (the answer is file-only whatever the
   capability — 196 computes the capability before that check, so reorder). One Mongo read per APP-tool
   call that can use it, none for the text tools. stdio and stateful HTTP never reach the store
   (`client_params` is set). The helper stays the ONE owner of the rule (196's design); 196's direct
   `_ui_capability` tests change only to await it.
4. **Effect on Horizon.** A client that declined → file only (the ~500-char "Since this client does not
   render inline MCP App UIs …" text + link), so Claude Code / Codex never receive the payload block
   even with `as_html_file=false`; a client that advertised the extension → inline only, no Graph-file
   insert, no link; a session the store never saw → both, as today.
5. **Docs, PA-applied at pick-up (the SWE edits none of these):** ADR-014 Status-line clause for this
   task; a glossary row for the new concept (working name **Client session record**; the PA settles the
   term); `docs/notes/mcp_server_design.md` §4 one sentence.

**Out of scope (intentional):** making Horizon stateful or adding sticky sessions (Horizon owns routing;
a stateful server on a fresh instance answers hard 404s); any change to the `as_html_file=true`
description hint or a new tool argument; a short-lived in-process cache in front of the store (add only
with a measured latency cost, like ADR-014's per-request user lookup); capturing anything beyond the one
boolean (no `clientInfo`, no full capabilities blob — nothing reads them); the Claude Code overflow
below.

## Related concern (not fixed here): a whole-memory result overflows Claude Code's tool-result limit

Measured in task 196: `visualize_memory_structure(as_html_file=false)` on the whole memory (≈280 KB,
280,593 characters as Claude Code counted it) exceeded Claude Code's tool-result limit; Claude Code wrote
the result to a file and the model received nothing — the summary and the `graphs://` link vanished with
the payload. This task's store removes the payload for a client that DECLINED the extension on a session
the store saw. It does nothing for (a) a session the store never saw, (b) a UI-capable host whose own
limit the inline payload exceeds, or (c) a text-only client whose `initialize` never reached the server.
If the post-merge [HUMAN] check still shows the overflow, file a separate task: a payload-size ceiling on
the `unknown` branch (above N KB answer the file only, with one sentence saying why) is the least
mechanism; reordering blocks does not help, the host truncates the whole result.

## Acceptance Criteria

- [ ] [HUMAN] **Verification first.** On a PR preview, the temporary header log is read for one
      `initialize` + one `visualize_memory_structure` from Claude Desktop (or claude.ai) and one pair from
      Claude Code; the Log records, per client: whether `mcp-session-id` is on the upstream `initialize`
      request, whether the following `tools/call` carries the same value, the `mcp-protocol-version`, and
      whether `initialize` reached the server at all. The log line is reverted before any other commit.
      If the answer is "no key", the task STOPS here with a Log entry and the PA decides.
- [ ] A `Middleware.on_initialize` stores `{session_id, ui_supported, created_at}` through the SDK's own
      extension check (no second implementation of "does this capabilities object advertise
      `io.modelcontextprotocol/ui`"); `initialize` succeeds when the write fails (warning logged) and
      writes nothing when the request has no session-id header. Pinned in
      `test_viz_app_stateless.py` by sending the header on `initialize` against the real stateless app
      with the store's write patched the way `GraphFile.insert` is.
- [ ] `_ui_capability` on a stateless request: stored `ui_supported=True` → inline only (the
      `audience=["user"]` block, no `graphs://` link, no Graph-file write); stored `False` → file only
      (today's "does not render inline MCP App UIs" text + link, no payload block); no row / expired /
      no header / store raising → `unknown` (payload + link, byte-for-byte 196's answer). Pinned in the
      same harness by sending the same header on `tools/call`; the `memory_dashboard` companion covers
      `declined` → text, `supported` → payload.
- [ ] A client that sends no session-id header on a stateless server (sessionless protocol version)
      answers exactly as in 196 — the existing stateless tests keep their assertions (edited only to
      patch the store).
- [ ] stdio and stateful HTTP never read the store: the existing `test_viz_app.py`, `test_graph_tools.py`,
      `test_tools.py`, `test_dashboard_app.py` tests keep every behavioural pin (edited only where the
      now-async `_ui_capability` signature requires an `await`), and a test asserts the store's read is
      not called when `client_params` is set, nor when `as_html_file` is set.
- [ ] `mcp.client_session_ttl_seconds` exists in the YAML config with default 86400, is overridable via
      `TREE_MCP__CLIENT_SESSION_TTL_SECONDS`, and a changed value is reconciled with `collMod` by the
      SAME helper the Graph-file TTL uses (one reconcile path; a test covers the second collection).
- [ ] No new tool argument; the `as_html_file` description sentences from task 196 are unchanged
      (`test_every_graph_tool_description_tells_text_only_clients_to_ask_for_the_file` passes unedited).
- [ ] Docs PA-applied at pick-up: ADR-014 Status-line clause, the glossary row, the design-note §4
      sentence; `git diff --numstat docs/adrs` is `1 1` on 014 only.
- [ ] [HUMAN] Post-merge on Horizon: Claude Code `visualize_memory_structure(as_html_file=false)` on the
      whole memory answers the file-only text + link (no payload block, no "exceeded the token limit");
      claude.ai / Claude Desktop renders the tree inline with NO `graphs://` link and no new
      `graph_files` row (check with `mongosh`); evidence (Traffic-Log rows) in the Log.
- [ ] `make memory-format-check`, `make memory-lint-check`, `make pre-commit`, `make memory-tests` green
      (env-status local).

## User Stories

### Story: An operator verifies what Horizon's gateway forwards before anyone writes a store
1. The operator deploys the PR preview carrying the one temporary header-log line.
2. From Claude Desktop they open the `tree-memory` server and call `visualize_memory_structure`; from
   Claude Code they run `/mcp` against the same preview and call the same tool.
3. The Horizon server logs show, per client, two lines: `initialize` and `tools/call`, each with the
   `mcp-session-id` value (or `<absent>`), the `mcp-protocol-version` and the other header names.
4. The operator pastes the four lines into the task Log and ticks or STOPS AC 1.

### Story: A Claude Code user on Horizon asks for a picture and explicitly turns the file off
1. Claude Code (text-only; its `initialize` declined the UI extension) calls
   `visualize_memory_structure(as_html_file=false)` on the whole memory.
2. The server finds the session's row (`ui_supported=False`) and answers the file branch: the summary,
   "Since this client does not render inline MCP App UIs, I rendered … as a download …", and the
   `graphs://<token>.html.gz` link — about 500 characters, no payload block.
3. The model relays the summary and the download recipe; Claude Code shows no "exceeded the token limit".

### Story: A claude.ai user sees the tree inline and no download link
1. claude.ai's `initialize` advertised `io.modelcontextprotocol/ui`; the gateway forwarded it with its
   session id; the store holds `ui_supported=True`.
2. The user asks "show me how my memory is organised"; the model calls `visualize_memory_structure()`.
3. The response carries the summary and the `audience=["user"]` block only; the mounted iframe renders
   the tree; the model's text names no `graphs://` link; `mongosh` shows no new `graph_files` row.

### Story: A long-running Desktop conversation outlives the record
1. A Desktop session started 25 hours ago (TTL 86400 s) calls `visualize_memory_embeddings()`.
2. The row is gone; the capability is `unknown`; the answer carries the payload block AND the link, as
   in 196 — the iframe still renders, nothing errors, one Graph-file row is written.
3. The next `initialize` (a new conversation) writes a fresh row and the inline-only answer returns.

### Story: A client with no session id keeps working
1. A script speaks sessionless streamable-http to the server (no `mcp-session-id` on any request).
2. `initialize` writes nothing; `tools/call` reads nothing; the answer is 196's `unknown` answer.
3. Nothing in the logs above `warning`, and only when the store itself is unreachable.

---

Blocked by: 196

## Log

### [PA] 2026-10-09 17:40 — Grooming (pre-approved conditional follow-up to 196; condition measured as met)

**Summary**
On stateless Horizon every `tools/call` is `unknown` to the three-state gate, so every visualize answer
carries the payload block as well as the link, and Claude Code shows that block to its model (or, for a
whole-memory view, overflows and shows nothing). Persist the client's initialize-time UI capability per
gateway session id so a Horizon `tools/call` resolves to `supported` / `declined` like a stateful one.

**Key decisions**
- Verification gates the build: the SDK mints no session id in stateless mode, so whether a usable key
  reaches the upstream server on BOTH `initialize` and `tools/call` is an open question the PR-preview
  header log must answer first (AC 1 with a STOP outcome).
- Mongo with a TTL, not an in-process map — no instance affinity; reuse the Graph-file TTL reconcile
  helper, one new YAML knob, one boolean per row.
- `unknown` stays the fallback for every miss: the task narrows 196's answer, it never removes it.
- The whole-memory overflow is recorded as a related concern with its measurement, not folded into the
  scope: the store cannot reach a client whose `initialize` the server never saw.
- Glossary and ADR-014 clause are PA-applied at pick-up once the key is known (the term depends on it).

**Dependencies**
- 196 — the `_ui_capability` helper, the stateless harness, and the description hint this task leaves
  unchanged.

**User stories**
- 5 stories covering: the gateway verification, Claude Code with an explicit `as_html_file=false`,
  claude.ai inline without a link, a TTL-expired session, and a sessionless client.

**Open questions**
- Whether Horizon's gateway forwards `initialize` and a stable `mcp-session-id` upstream (AC 1). If not,
  this task closes as won't-do and the description hint remains the only mitigation.

Ready for the verification step; implementation waits on AC 1.
