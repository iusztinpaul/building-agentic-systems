# MCP Apps in Tree

How Tree's MCP server renders **interactive UIs directly inside the chat** — graphs,
dashboards, tables — instead of dumping JSON and hoping the model summarizes it well.

This document covers what an MCP App is, the problem it solves, the four ways FastMCP
lets you build one, how our two live surfaces (`graph_app.py`, `dashboard_app.py`) are
implemented, and why this changes how we ship software.

---

## 1. What is an MCP App?

The **Model Context Protocol (MCP)** is the open standard that lets an AI host (Claude
Desktop, Claude Code, the web app, IDE extensions) talk to external **tools, resources,
and prompts**. A normal MCP tool takes arguments and returns **text** (or structured
JSON) that the model reads.

An **MCP App** is an extension to that contract: a tool can return not just text but a
**UI** — a self-contained, sandboxed web view that the host renders **inline in the
conversation**. The user sees an actual interactive widget (a force-directed graph, a
sortable table, a bar chart) embedded in the chat transcript, right where the tool was
called.

Mechanically, the host:

1. Calls the tool like any other tool.
2. Receives a result whose `structuredContent` carries the UI payload (and a short text
   `content` summary for the model).
3. Mounts a **sandboxed iframe**, loads the app's HTML/JS resource, and pushes the tool
   result into it over the MCP Apps **`ontoolresult`** channel.
4. The iframe renders the view client-side.

The model still gets a short text summary (so it keeps reasoning), while the **human gets
a real UI**. Capability is negotiated: the host advertises a UI extension
(`UI_EXTENSION_ID`); if it's absent, the tool falls back to text/file (see §4).

---

## 2. What problem does it solve?

Tool results are traditionally a wall of JSON. That has three failure modes:

- **The model has to narrate data it's bad at narrating.** A 142-node / 127-edge
  knowledge graph is _topology_ — you cannot usefully read it as prose. A table of 50
  nodes with six metadata fields each is _tabular_ — the model burns tokens
  pretty-printing it and still loses the ability to sort/filter.
- **Context bloat.** Dumping every node + edge + property into the transcript floods the
  context window. (Our `deep_search_memory` exists precisely to avoid this — it writes
  results to disk and returns a lightweight index.)
- **The human is stuck.** Even if the model summarizes well, the user can't _interact_ —
  can't hover a node for its metadata, can't sort a column, can't zoom a cluster.

MCP Apps fix all three: the **data goes to a UI**, the **model gets a one-line summary**,
and the **human gets to explore**. "Your data goes from a JSON blob to a searchable,
sortable table" — and from an edge list to a graph you can actually see.

---

## 3. Four ways to build one with FastMCP

FastMCP (`fastmcp[apps]` extra, pinned in `pyproject.toml` as `fastmcp[apps]>=3.1.0`)
offers four paradigms, from highest-level/declarative to lowest-level/raw-HTML. They
trade authoring effort against control.

| Paradigm | Decorator / API | You write | Best for |
|---|---|---|---|
| **Interactive tool** (Prefab) | `@mcp.tool(app=True)` → `PrefabApp` | Declarative Python components | Read-only dashboards, tables, charts |
| **Dashboard UI** (FastMCPApp) | `FastMCPApp` + `@app.ui()` / `@app.tool()` | Same Prefab UI, but as a provider | UIs that call **backend** tools (forms, CRUD, search), multi-tool apps, composition |
| **Generative UI** | `mcp.add_provider(GenerativeUI())` | Nothing — the model authors the UI at runtime | Bespoke, one-off layouts decided per request |
| **Custom HTML** (low-level) | `@mcp.tool(app=AppConfig(...))` + `@mcp.resource("ui://…")` | Raw HTML/JS/CSS | Anything Prefab can't express — bespoke renderers (WebGL, D3, Sigma) |

### 3a. Interactive tool — Prefab (`@mcp.tool(app=True)`)

The simplest. Decorate a normal tool with `app=True`, build a UI from declarative
[Prefab](https://gofastmcp.com/apps/prefab) components, and return it. The framework
handles rendering (client-side via Pyodide), sandboxing, and CSP.

```python
@mcp.tool(app=True)
async def memory_dashboard(query: str, ctx: Context, top_k: int = 20) -> ToolResult:
    payload = await _fetch_payload(ctx, query, top_k, max_hops=2)
    return ToolResult(
        content=_summary(payload, query),                # text → the model
        structured_content=build_dashboard(payload, query),  # PrefabApp → the human
    )
```

Interactivity (sort, search, paginate) is **client-side state** — no server round-trips
after the first render. Read-only by design: the UI **cannot call backend tools**.

**What FastMCP does for you here.** You compose from **100+ declarative components** (tables,
six chart types, `Metric`/`Badge`/`Separator`/`Icon`, layout `Row`/`Column`, form inputs)
with `with`-blocks where indentation _is_ the layout — no HTML/JS. FastMCP handles
serialization, the **sandboxed iframe** (`sandbox="allow-scripts allow-same-origin"` + a
strict deny-by-default CSP), and lifecycle automatically; the only ceremony is `app=True`.
Prefab is genuinely **reactive client-side**: initialise `PrefabApp(state={...})`, bind
components with `Rx(...)` expressions (arithmetic, comparisons, formatting pipes like
`.currency()` / `.percent()`, ternaries), gate visibility with `If(Rx(...))`, and derive
values with the `let` attribute — all evaluated in the browser, updating instantly as the
user interacts. By default the model only sees a `"[Rendered Prefab UI]"` placeholder; wrap
the view in a `ToolResult(content=…, structured_content=…)` (as we do) so the model still
gets a text summary to reason over. Preview locally with `fastmcp dev apps`.

### 3b. Dashboard UI — FastMCPApp provider (`@app.ui()`)

Same Prefab UI, but registered through a
[`FastMCPApp`](https://gofastmcp.com/apps/fastmcp-app) provider that separates
**model-visible entry points** (`@app.ui()`) from **UI-only backend tools** (`@app.tool()`).

```python
dashboard_provider = FastMCPApp("Tree Memory Dashboard")

@dashboard_provider.ui()
async def memory_dashboard_app(query: str, ctx: Context, top_k: int = 20) -> PrefabApp:
    payload = await _fetch_payload(ctx, query, top_k, max_hops=2)
    return build_dashboard(payload, query)

mcp.add_provider(dashboard_provider)
```

Choose this over a plain interactive tool when the rendered UI must **call back into the
server** — submit a form, run a server-side search, mutate data — via `@app.tool()`
handlers. It's the "classic UI ↔ backend" shape, and FastMCP adds three things you'd
otherwise hand-build:

- **Managed visibility.** `@app.ui()` entry points default to `visibility=["model"]` (the
  model can launch them); `@app.tool()` backends default to `visibility=["app"]` (UI-only)
  — add `model=True` to expose one to both. No accidental tool-surface leakage.
- **Composition safety via function references.** The UI calls backends with
  `CallTool(save_contact, …)` — a **function reference that resolves to a globally stable
  identifier**, so it keeps working when the server is mounted under a namespace and the
  tool becomes `contacts_save_contact`. String names (`CallTool("save_contact")`) silently
  break there. Multiple apps coexist without key collisions.
- **Declarative async wiring.** Calls feed reactive state: `RESULT` / `ERROR` references
  capture returns/failures, `on_success` / `on_error` callback chains short-circuit on
  error, `result_key="contacts"` is shorthand for "store the return value in state", and
  `SetState` / `ToggleState` / `AppendState` mutate client-side.

Add it with `mcp.add_provider(app)` (or `FastMCP(providers=[app])`); `FastMCPApp.run()`
wraps itself in a throwaway server for standalone testing.

### 3c. Generative UI

Register one provider and the **model itself authors the Prefab UI at runtime**:

```python
from fastmcp.apps.generative import GenerativeUI
mcp.add_provider(GenerativeUI())   # registers generate_prefab_ui + search_prefab_components
```

The model calls `search_prefab_components` to discover what's available and
`generate_prefab_ui` (passing real Python — loops, f-strings, computation — plus a `data`
argument that becomes globals in the sandbox) to emit a bespoke layout for _this_ request.

The mechanics are the striking part: the renderer iframe is created **in parallel** with
the tool call, and as the model streams tokens the host forwards partial code via
`ontoolinputpartial` — **browser-side Pyodide renders the UI progressively, components
materialising as the model "types".** On completion the server re-runs the full code in a
**server-side Pyodide sandbox** (Deno, auto-installed on first use) to validate it, then
swaps the streaming preview for the validated result. Safe by construction: the model is
constrained to **Prefab components + the Python stdlib only** — no NumPy/pandas/requests —
so "the AI writes your UI" can't reach arbitrary code. Maximum flexibility, no fixed design.

### 3d. Custom HTML — low-level (`ui://` resource)

When Prefab can't express it, drop to raw HTML/JS. Two pieces, following the
[low-level pattern](https://gofastmcp.com/apps/low-level):

```python
@mcp.tool(app=AppConfig(resource_uri=GRAPH_VIEW_URI))   # entry point
async def visualize_memory_graph(query: str, ctx: Context, ...) -> ToolResult:
    payload = to_graph_payload(result)
    return ToolResult(content=[...summary...], structured_content=payload)

@mcp.resource(                                          # the iframe's HTML
    GRAPH_VIEW_URI,
    app=AppConfig(csp=ResourceCSP(resource_domains=["https://cdn.jsdelivr.net", ...])),
)
def graph_view() -> str:
    return _GRAPH_HTML        # a full HTML doc with <script> that draws the graph
```

The tool returns the data on `structured_content`; the `ui://` resource (auto-served with
MIME `text/html;profile=mcp-app`) is the HTML shell that receives that data and renders it.
The mechanics, all standard MCP Apps that FastMCP exposes ergonomically:

- **`AppConfig`** links a tool to its UI resource and sets `visibility`, `csp`,
  `permissions` (camera/clipboard), and a stable sandbox `domain`.
- **`ResourceCSP`** is **deny-by-default** with granular allowlists — `resource_domains`
  (scripts/images/styles/fonts), `connect_domains` (fetch/XHR/WebSocket), `frame_domains`,
  `base_uri_domains`. We allow only jsdelivr (graphology + Sigma.js + ForceAtlas2).
- The browser side uses the **`@modelcontextprotocol/ext-apps`** SDK: an `App` instance
  exposes **`ontoolresult`** (host pushes the tool result over `postMessage`),
  **`callServerTool({name, arguments})`** (UI → server), plus `onhostcontextchanged` /
  `getHostContext()`.
- Capability negotiation: the Apps extension is identified by **`UI_EXTENSION_ID`**;
  `ctx.client_supports_extension(UI_EXTENSION_ID)` is exactly what we gate the rich-vs-file
  fallback on (§4). It's the open MCP Apps standard underneath — FastMCP is the ergonomic
  layer, and the same app runs across every MCP host.

---

## 4. How it works in our codebase

Tree exposes **two live MCP App surfaces**, both over the same knowledge-graph query
engine. The MCP server is defined in `server.py` (`FastMCP("Tree Memory", …)` with a
lifespan that wires MongoDB + models + the pinned `user_id`); `tools.py` registers
everything via side-effect imports.

### The shared data chokepoint

Both surfaces are fed by **one function**, `to_graph_payload` in `graph_app.py`. It
flattens a `QueryResult` into `{nodes, edges}` and attaches a **curated, JSON-safe `meta`
dict** to every node and edge (`_curated_meta` — subtype, confidence, description,
aliases, timestamps; datetimes→ISO, floats rounded, empties dropped). Fix metadata once
there, and **both** the graph hover and the dashboard tables get it. This is the key
design lesson: **normalize to one payload, render it many ways.**

### Surface 1 — Custom HTML graph (`graph_app.py`)

`visualize_memory_graph` is the **custom-HTML / low-level** paradigm (§3d). It runs
semantic + text search with graph expansion and returns the `{nodes, edges}` payload on
`structured_content`. The `ui://tree-memory/graph.html` resource is a self-contained
**Sigma.js** WebGL renderer (graphology + ForceAtlas2 layout, loaded as ESM from
jsdelivr under a `ResourceCSP` allowlist). The UI provides: live counts, node search, a
per-type colour legend, **hover cards** (name + type + subtype + curated metadata, on
both nodes _and_ edges via Sigma `enableEdgeEvents`), click-to-highlight, drag, and zoom.

Node colours come from `_NODE_COLOURS`, mapped onto the brand palette enum
`tree.entities.colours.Colours` (a `StrEnum` where each member _is_ its hex string).

**Fallback (Option B).** Not every client renders MCP App UIs (e.g. a text-only agent, or
the terminal). The tool checks `ctx.client_supports_extension(UI_EXTENSION_ID)`; when the
UI extension is absent — or the caller passes `as_html_file=True` — it renders the _same_
graph into a **self-contained HTML file** under `.tree/graphs/` (data embedded inline, no
ext-apps round-trip) and returns the path. The model never hand-authors HTML.

### Surface 2 — Prefab interactive dashboard (`dashboard_app.py`)

`memory_dashboard` is the **interactive-tool / Prefab** paradigm (§3a). Where the graph
shows _topology_, the dashboard shows a _summary_: KPI `Metric`s (node/edge/type counts),
a node-type `BarChart`, and two `DataTable`s — one for nodes, one for relationships — with
curated metadata columns, all **sortable / searchable / paginated client-side**.

```python
@mcp.tool(app=True)
async def memory_dashboard(query, ctx, top_k=20, max_hops=2) -> ToolResult:
    payload = await _fetch_payload(ctx, query, top_k, max_hops)   # → to_graph_payload
    return ToolResult(content=_summary(payload, query),
                      structured_content=build_dashboard(payload, query))
```

This is read-only, which is exactly why the **interactive tool** fits and the heavier
**FastMCPApp provider** doesn't — the dashboard never calls backend tools, so the provider
ceremony would buy nothing. We **deliberately removed** the FastMCPApp variant
(`memory_dashboard_app`) and the Generative UI provider during cleanup: they were kept
only as a paradigm comparison, and §3b/§3c machinery is unused for a read-only view. If we
later add inline actions — e.g. a "merge these duplicates" button wired to our existing
`review_confirm` tool — _that's_ when the surface should graduate to FastMCPApp `@app.ui()`.

### Graceful degradation of the whole feature

Both apps need the `fastmcp[apps]` extra (`prefab-ui`). The dashboard import in `tools.py`
is wrapped in `try/except ImportError` so a missing extra logs a warning and skips the
dashboard rather than taking down the entire server (the custom-HTML graph has no such
dependency and stays up).

### Trigger phrasing (which query routes to which surface)

- _"Visualize my entire knowledge graph as an interactive graph"_ → `visualize_memory_graph` (broad `top_k`).
- _"Show the subgraph around Tree Labs"_ → `visualize_memory_graph` (scoped seed, `max_hops=1`).
- _"Open a dashboard: bar chart of node types + a searchable table"_ → `memory_dashboard`.
- _"Generate a custom UI on the fly to explore my memory"_ → `generate_prefab_ui` (only if the GenerativeUI provider is re-added).

---

## 5. Why this changes how we write software

The conventional path to "show the user something interactive" is: build a web app — a
frontend, a server, routes, auth, hosting, a deploy pipeline — and send the user a link to
a browser tab. The UI lives **outside** the conversation; the AI can only hand off to it.

MCP Apps collapse that. The **UI ships from the same tool that produces the data**, and it
renders **inside the chat** — Claude Desktop, the web app, IDE extensions. Consequences:

- **No separate frontend to build, host, or deploy.** Our entire graph explorer is a
  Python tool plus an HTML string in `graph_app.py`. The whole dashboard is ~40 lines of
  declarative Prefab. There is no `apps/web/`, no bundler, no CDN of our own, no auth layer
  — the host _is_ the runtime, the chat _is_ the surface.
- **The chat becomes the app shell.** The user stays in one place. They ask a question,
  get a graph they can drag and hover, ask a follow-up, get a dashboard — without ever
  leaving the conversation or opening a browser tab. The context (what they asked, what the
  model reasoned) travels _with_ the UI.
- **Generative UI dissolves the design step entirely.** With the Generative UI provider,
  the model **builds the interface per request**. There is no fixed screen to design ahead
  of time; the UI is synthesized to fit the data and the question. The notion of "we need a
  page for X" gives way to "the assistant renders whatever X needs, now."
- **One backend, many hosts.** The same FastMCP server drives the UI in every MCP host
  with zero per-host frontend work. Write the tool once; it renders in Claude Desktop, the
  web app, and IDE extensions alike — and degrades cleanly to a file/text where it can't.

The shift: a UI stops being a _separate product_ you build and deploy, and becomes a
**return value** — something a tool emits alongside its data, rendered wherever the
conversation already lives. For an assistant like Tree, that means the knowledge graph
isn't behind a dashboard you have to go open; it surfaces, explorable, right where you're
already talking to it.

---

## 6. Builder's notes & content angles

Raw material for write-ups (blog / LinkedIn). Everything below is from actually building
this — the journey, the traps, the numbers, and the honest caveats — not theory.

### The build journey (a small story)

- We implemented the dashboard **three ways at once** — interactive tool, FastMCPApp
  provider, _and_ generative UI — **on purpose**, as a side-by-side comparison of FastMCP's
  paradigms.
- Then we asked the real question — _which one fits a read-only view?_ — and **deleted
  two.** The interactive tool survived.
- Lesson: **build to compare, then delete.** Carrying three paradigms forever is expensive;
  deleting two once you know the answer is cheap. "More machinery isn't more correct."
- A second iteration loop: graph node details first showed on **click** (a detail panel),
  then the ask became "show on **hover** instead." Moving details into the hover card (and
  adding type + subtype) actually _simplified_ the code — we deleted the whole click-panel.
  UX simplification and code simplification pointed the same way.

### Non-obvious gotchas (the credible, specific stuff)

- **The test that lied.** A unit test asserted the dashboard's table rows appeared in
  `PrefabApp.model_dump()`. It failed — `model_dump()` doesn't deep-serialize child
  components. Fix: stop introspecting the rendered widget; unit-test the **pure row-builder
  transforms** (`_node_rows` / `_edge_rows`) instead. Generalizes to: _test the data
  transform, not the UI object._
- **One line unblocks edge hovers.** Sigma.js v3 won't fire `enterEdge` / `leaveEdge` until
  you set `enableEdgeEvents: true`. Without it, edge tooltips silently do nothing.
- **CSP will block your CDN.** Custom-HTML apps must whitelist every external origin via
  `ResourceCSP(resource_domains=[...])`, or the sandbox refuses to load graphology / Sigma.
  Pin CDN versions while you're at it.
- **`</script>` in user data breaks the inline-data HTML.** The self-contained file embeds
  the payload as `const DATA = …`; a stray `</` in a node name closes the script tag early.
  Escape `</` at serialize time and HTML-escape values before `innerHTML`.
- **Prefab is read-only by contract.** The UI can't call back into the server. If you want
  a button that mutates data, that's the _signal to switch to FastMCPApp_ — not a Prefab
  workaround.

### By the numbers / before → after

- Graph explorer = **1 Python tool + 1 HTML string** (no separate repo).
- Whole dashboard = **~40 lines of declarative Prefab**.
- Net new infra to ship an interactive UI: **zero** — no `apps/web/`, no bundler, no
  hosting, no CDN of our own, no auth layer, no deploy pipeline.
- **One backend → every host**: the same FastMCP server renders the UI in Claude Desktop,
  the web app, and IDE extensions, with no per-host frontend work.

### Honest limitations (keep the post credible, not hype)

- Needs a host that implements the MCP Apps **UI extension**; otherwise it degrades to a
  file/text (we built that fallback on purpose — `client_supports_extension`).
- Requires the `fastmcp[apps]` extra (`prefab-ui`); **generative UI additionally needs
  Deno** at runtime.
- It's **bleeding edge** — MCP ext-apps `0.4.0`, FastMCP `3.1`. Expect APIs to move.
- Prefab interactivity is **client-side only**; anything stateful/mutating needs the
  heavier FastMCPApp path.
- The UI renders client-side via **Pyodide** — there's a cold-start cost.

### Framing & analogies (steal these)

- "A UI becomes a **return value**, not a separate product."
- "The **chat is the new app shell**."
- Like the browser absorbed installable desktop apps for many use cases, the **AI host can
  absorb the frontend** for AI-native tools.
- For a memory product specifically: your data isn't behind a dashboard you go open — it
  **surfaces, explorable, where you're already talking**.

### Who should care (generalize beyond Tree)

- Any MCP tool returning **graph / tabular / list** data is a candidate: logs, query
  results, search hits, diffs, dashboards.
- **Internal-tooling teams**: ship an interactive view without standing up a web app.
- The reusable pattern: **normalize to one payload, render it many ways, degrade
  gracefully.**

### Quotable one-liners (ready to lift)

- "I built an interactive knowledge-graph explorer with **no frontend, no bundler, no
  hosting** — it renders inside the chat."
- "Three implementations, one survivor: I built the dashboard three ways to learn which
  paradigm fit, then **deleted two**."
- "My UI clearly rendered the data, but the test couldn't see it. Lesson: **test the
  transform, not the widget**."
- "We're about to stop building dashboards you open, and start building **dashboards that
  appear when you ask**."

---

### Reference map

| File | Role |
|---|---|
| `server.py` | `FastMCP("Tree Memory")` + lifespan (Mongo, models, pinned `user_id`) |
| `tools.py` | Registers query/ingest/web/review tools; side-effect-imports the two App modules (dashboard guarded by `try/except ImportError`) |
| `graph_app.py` | **Custom-HTML** Sigma graph: `visualize_memory_graph` tool + `ui://` resource + file fallback; `to_graph_payload` + `_curated_meta` (shared payload) |
| `dashboard_app.py` | **Prefab interactive tool** `memory_dashboard`: bar chart + node/edge tables |
| `tree/entities/colours.py` | `Colours` brand-palette `StrEnum` used by the graph node colours |
| `deep_search.py`, `ingest.py` | Non-UI tool helpers (progressive-disclosure search; ingestion pipeline) |

**Docs:** [Prefab](https://gofastmcp.com/apps/prefab) ·
[FastMCPApp](https://gofastmcp.com/apps/fastmcp-app) ·
[Generative UI](https://gofastmcp.com/apps/generative) ·
[Low-level / custom HTML](https://gofastmcp.com/apps/low-level)
