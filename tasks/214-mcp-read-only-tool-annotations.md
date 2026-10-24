---
id: 214-mcp-read-only-tool-annotations
status: pending
feature: mcp-read-only-annotations
---

# Advertise honest MCP tool annotations on every `tree-memory` tool — `readOnlyHint: true` on the pure reads (`search_memory` in rag, `search_web`, `scrape_web`, `review_list_pending`), `readOnlyHint: false` + `destructiveHint: false` on the tools that write a **Graph file** or a receipt — metadata only, no behaviour change

Tags: `mcp`, `annotations`, `permissions`, `enhancement`
Depends on: None
Blocks: —
Implements: the follow-up ADR-016 §5 records ("the memory server's tools carry no `readOnlyHint` …
annotating them is a separate memory-app task") — inside the contracts of ADR-008 §2/§5 (tool names,
parameters and answer shapes untouched), ADR-013 and ADR-014 §4 (the **Graph file** write stays exactly as
it is; this task only tells the truth about it). No new ADR: annotations are tool metadata, not a design
decision.

## Scope

**Why.** The FastMCP server in `apps/memory/src/tree/mcp/` registers every tool with no MCP
`annotations` (verified: no `annotations=` in `server.py`, `tools.py`, `graph_tools.py`). **tree-agent**'s
permission gate (ADR-016 §5, spec §11, task 206) exempts an MCP tool from the gate only when the server
asserts `annotations.readOnlyHint === true` (task 209: `isReadOnly = annotations.readOnlyHint === true`).
Today, therefore, `search_memory` — a pure read — is classified by the **Decision model** in the `auto`
**Permission mode** and prompted once in `ask`. Task 209 §5 records that behaviour as a consequence; this
task removes it on the server side. The raw task also named `tree.mcp.viz_app` and a `memory_dashboard`
tool: neither exists any more (commit f4791e4 dropped the MCP App views; `graph_download.py`: "No MCP App
view"), so there is nothing to annotate there.

**The rule.** Annotations are declared per TOOL, not per argument, so a tool is `readOnlyHint: true`
only if NO argument combination writes anything beyond logs and Opik telemetry. "Does not modify its
environment" (MCP spec 2025-06-18, `ToolAnnotations.readOnlyHint`) is read at face value: a MongoDB insert,
a file on the user's disk and a `webbrowser.open` are modifications, however cheap. The harness treats the
flag as "skip the gate entirely", so when in doubt the server claims LESS.

- **Graph-file writers are not read-only.** `visualize_memory_structure` (both mode forms),
  `visualize_memory_embeddings`, and — because of their `visualize: bool` knob routed through
  `_dual_graph_result` → `_graph_tool_result` — graphrag's `query_memory` and `search_memory`, insert one
  **Graph file** row per call (`GraphFile(...).insert()`, ADR-014 §4) and on stdio ALSO write
  `.tree/graphs/<slug>-<stamp>.html` and open a browser. They are additive (`destructiveHint: false`: a
  TTL-expiring row, a new file) and not idempotent (`idempotentHint: false`: a fresh token per call).
  Upgrade trigger: if rendering ever becomes a pure resource read with no row write, flip them to
  `readOnlyHint: true`. Consequence in the harness: `visualize_*` (already `deferred` in 209's
  `.tree-agent/mcp.json`) stays classified in `auto` (expected `dangerous: false`) and prompted once in
  `ask` — one `s` per session for a rarely used tool, and the gate's premise stays honest.
- **Ingest tools are additive and idempotent.** ADR-008 §1: a repeat submit on the same natural key
  `(user_id, source_uri)` answers `duplicate: true` and dispatches nothing — `destructiveHint: false`,
  `idempotentHint: true`.
- **`deep_search_memory` writes `.memory/<session_id>/*.md`** (`write_deep_search_results`) — not
  read-only; a new session dir per call unless `session_id` is passed, so `idempotentHint: false`.
- **Review tools:** `review_list_pending` reads only; `review_confirm` merges and tombstones (destructive);
  `review_reject` overwrites an edge's status (destructive by the spec's "may overwrite" wording).
- **`openWorldHint`** is set on EVERY tool: the spec default is `true`, which is wrong for a memory tool
  (the spec's own example: "the world of a web search tool is open, whereas that of a memory tool is not").
  `true` only for `search_web`, `scrape_web`, `ingest_url` (the open web via Bright Data).
- **Which hints to emit — bias to least.** `readOnlyHint` and `openWorldHint` on all 13 registrations
  (explicit `false` where false, so a test can assert "present everywhere"); `destructiveHint` and
  `idempotentHint` ONLY on the non-read-only tools (the spec: meaningful only when `readOnlyHint` is
  false). No `title`.

**The table (the contract; both modes, 13 registrations):**

| Tool (module)                                               | readOnlyHint | destructiveHint | idempotentHint | openWorldHint |
| ----------------------------------------------------------- | ------------ | --------------- | -------------- | ------------- |
| `search_memory` — rag form (`tools.py`)                     | `true`       | —               | —              | `false`       |
| `search_web` (`tools.py`)                                   | `true`       | —               | —              | `true`        |
| `scrape_web` (`tools.py`)                                   | `true`       | —               | —              | `true`        |
| `visualize_memory_structure` — rag form (`tools.py`)        | `false`      | `false`         | `false`        | `false`       |
| `visualize_memory_embeddings` (`tools.py`)                  | `false`      | `false`         | `false`        | `false`       |
| `ingest_url` (`tools.py`)                                   | `false`      | `false`         | `true`         | `true`        |
| `ingest_file` (`tools.py`)                                  | `false`      | `false`         | `true`         | `false`       |
| `ingest_conversation` (`tools.py`)                          | `false`      | `false`         | `true`         | `false`       |
| `visualize_memory_structure` — graph form (`graph_tools.py`) | `false`      | `false`         | `false`        | `false`       |
| `query_memory` (`graph_tools.py`)                           | `false`      | `false`         | `false`        | `false`       |
| `search_memory` — graph form (`graph_tools.py`)             | `false`      | `false`         | `false`        | `false`       |
| `deep_search_memory` (`graph_tools.py`)                     | `false`      | `false`         | `false`        | `false`       |
| `review_list_pending` (`graph_tools.py`)                    | `true`       | —               | —              | `false`       |
| `review_confirm` (`graph_tools.py`)                         | `false`      | `true`          | `false`        | `false`       |
| `review_reject` (`graph_tools.py`)                          | `false`      | `true`          | `false`        | `false`       |

(rag registers the first eight rows; graphrag registers rows 2–3, 5–8 and 9–15 — 13 tools, the set
`tests/unit/mcp/test_tool_gating.py` already pins.) graphrag's `search_memory` being NOT read-only while
rag's is, is the per-tool rule applied honestly, not an oversight — note it in the module docstring.

**Mechanism (FastMCP 3.2.0, installed; confirm against the FastMCP `llms.txt` via the `tech-docs` skill
before coding).** `FastMCP.tool(..., annotations: ToolAnnotations | dict[str, Any] | None = None)`. Two
registration forms are live and BOTH must be covered: the decorator (`@mcp.tool` → `@mcp.tool(annotations=…)`,
stacked above `@track` as today) and the functional rag-only registrations
`mcp.tool(search_memory)` / `mcp.tool(visualize_memory_structure)` in `tools.py` →
`mcp.tool(search_memory, annotations=…)` (verified working in 3.2.0). Use `mcp.types.ToolAnnotations`
instances (Pydantic — project rule), e.g. a handful of module-level constants in `tools.py`
(`READ_ONLY_MEMORY`, `READ_ONLY_WEB`, `GRAPH_FILE_WRITER`, `INGEST`, …) that `graph_tools.py` imports, so
no hint is spelled twice; the SWE picks the names. On the wire FastMCP serialises with `exclude_none`, so an
unset hint is simply absent (`{"readOnlyHint": true, "openWorldHint": false}` for rag `search_memory`).

**Tests** (`/squid-testing-python`; `make memory-tests`, `make env-status` → local). Extend the
fresh-interpreter `_probe` in `apps/memory/tests/unit/mcp/test_tool_gating.py` with one key —
`"annotations": {t.name: t.to_mcp_tool().model_dump(by_alias=True, exclude_none=True).get("annotations")
for t in await server.mcp.list_tools()}` — the WIRE shape the harness reads, not the FastMCP object — and
assert the table above per mode (rag: 8 entries; graphrag: 13). No other test needs editing: the existing
tool-set, parameter, `meta`, instruction and answer-shape assertions are the "no behaviour change" guard.

**Cross-task ordering (for the orchestrator, recorded here):** task 209 §5 and two of its `[HUMAN]` ACs
assert the CURRENT behaviour (a `permission` event with `source: "classifier"` for `search_memory`; "in the
TUI (`ask`), the first `search_memory` prompts; `s` silences it"), and task 213 line 32 documents it. Once
this task merges those read `source: "readOnly"` / no prompt. Either sequence 214 after 209/213, or amend
those two ACs and the 213 sentence at 209's pick-up — one line each.

**Docs, PA-applied at pick-up (the SWE edits none of these):** the **Permission mode** row of
`docs/glossary.md` keeps its current last Notes sentence ("The memory server's tools carry no
`readOnlyHint`, so `search_memory` is classified in `auto` and prompted once in `ask`.") until this task
ships; at pick-up the PA replaces it with: "The memory server asserts `readOnlyHint: true` on its pure
reads (`search_memory` in rag, `search_web`, `scrape_web`, `review_list_pending`; task 214), so they never
prompt; its `visualize_*` tools (and graphrag's `query_memory` / `search_memory`, via `visualize`) write a
**Graph file** and advertise `readOnlyHint: false, destructiveHint: false`, so they are classified in
`auto` and prompted once in `ask`." No new glossary term — tool annotations are MCP vocabulary, not a Tree
concept. No ADR Status-line amendment.

## Out of scope (intentional)

- Any change to a tool's name, parameters, docstring text, answer shape, the **Tool error envelope**, the
  **Graph file** write or `DOWNLOAD_CONTRACT` (ADR-008/013/014 govern them; this is metadata only).
- Splitting graphrag's `visualize` knob into a separate tool so `query_memory` / graphrag `search_memory`
  could be read-only — a graphrag surface change for Chapter 8 (ADR-008 Consequences), not this task.
- The harness side (206's gate, 209's registration) — this feature never touches `apps/harness`.
- `title` annotations, MCP `_meta`, and any docs/README tool table (annotations are not user-visible copy).
- `tree.mcp.viz_app` / `memory_dashboard` — they no longer exist (see Scope).

## Acceptance Criteria

- [ ] In a fresh-interpreter rag boot (`TREE_MEMORY__MODE=rag`, the `test_tool_gating.py` probe), every one
      of the 8 registered tools serialises `annotations` through `to_mcp_tool().model_dump(by_alias=True,
      exclude_none=True)` EXACTLY as the table's rag rows — `search_memory` → `{"readOnlyHint": true,
      "openWorldHint": false}`, `search_web` / `scrape_web` → `{"readOnlyHint": true, "openWorldHint": true}`,
      `visualize_memory_structure` / `visualize_memory_embeddings` → `{"readOnlyHint": false,
      "destructiveHint": false, "idempotentHint": false, "openWorldHint": false}`, the three ingest tools →
      `{"readOnlyHint": false, "destructiveHint": false, "idempotentHint": true, "openWorldHint": <true for
      ingest_url, false otherwise>}`; no tool carries `title`.
- [ ] In a fresh-interpreter graphrag boot, all 13 registered tools serialise exactly the table's graphrag
      rows; in particular graphrag `search_memory` and `query_memory` carry `readOnlyHint: false` (the
      `visualize` knob writes a **Graph file**) and `review_list_pending` carries `readOnlyHint: true`.
- [ ] In both modes every registered tool has `readOnlyHint` and `openWorldHint` present, and
      `destructiveHint` / `idempotentHint` are present iff `readOnlyHint` is `false` (a single parametrised
      assertion, so a 14th tool added without annotations fails the suite).
- [ ] Nothing but metadata changed: the existing `tests/unit/mcp` suite (tool sets per mode,
      `parameters["properties"]`, `meta`, instructions, every answer-shape and envelope test) passes with no
      edits other than the new probe key and its assertions; `git diff --stat` touches only
      `tree/mcp/tools.py`, `tree/mcp/graph_tools.py` (and, if the SWE chooses constants, nothing outside
      `tree/mcp/`), `tests/unit/mcp/test_tool_gating.py`, and this task file.
- [ ] End-to-end over real stdio (`make env-status` → local, `make local-start`): a `fastmcp.Client`
      against `apps/memory/scripts/serve_mcp.py` (the same command `.mcp.json` / 209's
      `.tree-agent/mcp.json` spawn) calls `list_tools()` and the returned `mcp.types.Tool` for
      `search_memory` has `annotations.readOnlyHint is True`, for `visualize_memory_structure`
      `annotations.readOnlyHint is False and annotations.destructiveHint is False`; the one-liner and its
      output go in the Log.
- [ ] `make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check`,
      `make pre-commit`, `make memory-tests` green (env-status local).

## User Stories

### Story: A tree-agent user in `ask` mode asks a memory question and is never prompted

1. The harness spawns `tree-memory-local` (stdio, rag) and sends `tools/list`.
2. The `search_memory` entry in the response carries `"annotations": {"readOnlyHint": true,
   "openWorldHint": false}`; the registry (task 209) sets `isReadOnly = true`.
3. The user asks "what did I decide about Modal?"; the model calls
   `mcp__tree_memory_local__search_memory`; the gate (task 206) resolves `allow`, `source: "readOnly"` —
   no prompt in `ask`, no classifier call in `auto`; the answer JSON (`{"parents", "outcome",
   "search_mode"}`) is byte-for-byte what it was before this task.

### Story: The same user asks for a picture and the server does not pretend it is a read

1. The user asks "show me how my memory is organised"; `tool_search` activates
   `visualize_memory_structure`, whose `tools/list` entry carries `{"readOnlyHint": false,
   "destructiveHint": false, "idempotentHint": false, "openWorldHint": false}`.
2. In `auto` the Decision model classifies the call (`dangerous: false`, allowed); in `ask` the user is
   prompted once and answers `s`.
3. The tool runs exactly as before: one **Graph file** row, the `graphs://<token>.html.gz` link, the
   `.tree/graphs/…html` file and the browser on stdio.

### Story: An operator boots the server in graphrag and the whole surface is annotated

1. `TREE_MEMORY__MODE=graphrag make memory-serve-mcp`; a client lists tools.
2. All 13 tools carry `readOnlyHint` and `openWorldHint`; `review_list_pending` is read-only;
   `search_memory` and `query_memory` are NOT (`visualize=true` writes a Graph file); `review_confirm` /
   `review_reject` show `destructiveHint: true`.
3. `search_memory(query, visualize=true)` still answers the serialized rows plus the graph link, unchanged.

### Story: A Claude Code user notices nothing

1. The `tree-memory` skill's search loop calls `search_memory` three times and `ingest_url` once, as today.
2. Every answer — the parents JSON, the **Ingest receipt**, any **Tool error envelope** — is identical in
   shape and text; `/mcp` lists the same 8 tools with the same descriptions and parameters.

---

Blocked by: (none)

## Log

### [PA] 2026-10-24 21:10 — Grooming

**Summary**
Declare MCP tool annotations on all 13 `tree-memory` registrations so tree-agent's permission gate can
exempt the pure reads (`search_memory` in rag, `search_web`, `scrape_web`, `review_list_pending`) and
every other client learns which tools are additive, idempotent or open-world. Metadata only: no name,
parameter, docstring, answer shape or Graph-file write changes.

**Key decisions**

- `visualize_*` (and graphrag's `query_memory` / `search_memory`, via their `visualize` knob) are NOT
  `readOnlyHint: true`: each call inserts a **Graph file** row (ADR-014 §4) and on stdio writes a file and
  opens a browser — "does not modify its environment" is read at face value because the harness treats the
  flag as "skip the gate". They are `destructiveHint: false, idempotentHint: false`. Cost: one classifier
  call (`auto`) or one `s` (`ask`) on tools 209 already puts on `deferred`. Upgrade trigger: a render path
  with no row write.
- Annotations are per tool, not per argument — the reason graphrag `search_memory` differs from rag's, and
  the reason all 13 registrations are in scope (a half-annotated surface cannot carry the "every tool is
  annotated" drift guard). Scope-shrink to the rag eight is possible if the human prefers; the graphrag
  rows are seven one-line decorator edits.
- Hints by least: `readOnlyHint` + `openWorldHint` everywhere (the spec's `openWorldHint` default `true`
  is wrong for a memory tool); `destructiveHint` / `idempotentHint` only where the spec makes them
  meaningful. Ingest is `idempotentHint: true` on the strength of ADR-008 §1's natural-key dedup.
- `search_web` / `scrape_web` as read-only means the harness never prompts for a billable Bright Data
  request — spec-correct (cost is not environment modification) and mitigated by 209's exposure config
  (`codemode`, reachable only from a gated script). Recorded, not changed.
- The raw task's `viz_app` / `memory_dashboard` are gone (f4791e4) — nothing to annotate.
- Tests assert the WIRE shape (`to_mcp_tool().model_dump(by_alias=True, exclude_none=True)`), extending
  the existing per-mode fresh-interpreter probe rather than adding a new file.
- Glossary: the **Permission mode** row's Notes sentence about the missing `readOnlyHint` is replaced at
  pick-up, not at grooming, so the glossary describes the shipped state (text in Scope); no new term — tool
  annotations are MCP vocabulary, not a Tree concept. No ADR Status-line amendment: ADR-016 §5 already
  names this follow-up and its gate decision is unchanged; ADR-008/013/014 see no change.

**Dependencies**

- None. Independent of the harness plan (202–213); see Scope for the 209/213 AC wording that flips once
  this merges.

**User stories**

- 4 stories covering: `ask`-mode search with no prompt, a picture that is honestly not a read, a fully
  annotated graphrag surface, and an unchanged Claude Code flow.

**Open questions**

- None blocking. Sequencing preference (214 before or after 209/213) is the orchestrator's call; either
  way is one line of AC text in 209 and one sentence in 213.

Ready for implementation.
