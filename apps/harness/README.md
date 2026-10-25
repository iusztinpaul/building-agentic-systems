# tree-agent — Coding-Agent Harness

The agent half of **Tree: Your Rooted Personal Assistant**. `tree-agent` is a light [pi](https://github.com/earendil-works/pi) clone in TypeScript: one process, one agent loop, one `AgentSession` that both the TUI and the headless CLI drive. Permissions, compaction, prompt assembly, tool search, MCP and skills attach to the loop through hooks, never inside it.

What you get:

- four built-in tools: `read`, `bash`, `edit`, `write`;
- an MCP client (stdio + streamable HTTP) with four tool exposure levels and `tool_search`;
- a QuickJS codemode sandbox whose scripts call tools and `llm()`;
- two permission modes, `ask` and `auto` (a decision model flags dangerous calls);
- Agent Skills (`SKILL.md`), `AGENTS.md` / `CLAUDE.md` context files;
- compaction by summarisation at a ratio of the context window;
- append-only JSONL sessions you can resume.

Not built, on purpose: subagents, plan mode, session forks, an extensions API, MCP resources / prompts, permission rule files.

The contract is the spec, [`docs/notes/tree-agent-harness-spec.md`](../../docs/notes/tree-agent-harness-spec.md); the architectural decisions are [ADR-016](../../docs/adrs/016_tree_agent_harness.md). This README describes what the code does today; where it differs from the spec, the code is what is documented. The harness reaches memory only through MCP (ADR-008 §5): nothing here imports `apps/memory`.

## Install

Prerequisites: Node ≥ 22.19 (`engines` in `package.json`) with corepack, and an [OpenRouter](https://openrouter.ai/settings/keys) key.

```bash
corepack enable pnpm          # once: installs the pnpm shim; package.json pins pnpm@10.34.8
cp .env.example .env          # repo root; set OPENROUTER_API_KEY (the harness's only provider)
make harness-install          # pnpm install --frozen-lockfile inside apps/harness
```

- **No corepack** (`corepack --version` fails; Node 25 and later no longer bundle it) **or `corepack enable` cannot write to `/usr/local/bin`?** Run `npm i -g corepack`, then `corepack enable --install-directory ~/.local/bin pnpm` (any directory on your `PATH`).
- **Run pnpm from `apps/harness`, never from the repo root.** At the root corepack resolves a different pnpm version than the one pinned in `apps/harness/package.json` and fails. Use the `make harness-*` targets (they `cd` for you) or `cd apps/harness` first.

Every `make harness-<target>` runs `apps/harness/Makefile`, which loads and exports the root `.env` (or `.env.prod` when `.env.target` says `prod`). `make -C apps/harness help` lists the targets.

| Target | What it does |
|---|---|
| `make harness-install` | `pnpm install --frozen-lockfile` |
| `make harness-run PROMPT="…" ARGS="…"` | `node src/cli.ts` without a build (Node's type stripping); `PROMPT` is the positional prompt, `ARGS` the flags |
| `make harness-dev ARGS="…"` | the same under `node --watch` (restarts on save) |
| `make harness-build` | `tsc -p tsconfig.build.json` → `dist/`, `dist/cli.js` executable |
| `make harness-tests` / `harness-test-watch` | vitest once / in watch mode |
| `make harness-typecheck` | `tsc --noEmit` over `src/` and `tests/` |
| `make harness-lint-fix` / `harness-lint-check` | Biome check (lint + format + imports) with / without writes |
| `make harness-format-fix` / `harness-format-check` | Biome format only |
| `make harness-clean` | remove `dist/` and `node_modules/` |

The Makefile targets run with `apps/harness` as the working directory. To use the repo's MCP config, point the session at the repo root: `ARGS="--cwd ../.."` (see [MCP](#mcp)). On Node 22.x, `node src/cli.ts` may print a type-stripping `ExperimentalWarning` on stderr; the built `dist/cli.js` does not.

**`tree-agent` on your PATH.** `package.json` declares `bin: {"tree-agent": "dist/cli.js"}`. Build once and symlink it (the entry point resolves symlinks):

```bash
make harness-build
ln -s "$PWD/apps/harness/dist/cli.js" ~/.local/bin/tree-agent   # from the repo root; any dir on PATH
```

Re-run `make harness-build` after pulling changes. Launched directly, `tree-agent` reads `OPENROUTER_API_KEY` from your shell environment, not from `.env`. The repo's direnv setup exports it inside the repo (see the root [README](../../README.md#environment-loading-with-direnv)); elsewhere, export it yourself.

Smoke test: `tree-agent "say hi"` from the repo root (or any shell where `OPENROUTER_API_KEY` is exported) answers and exits 0.

## CLI

`--help` / `-h` print the usage text and exit 0; an unknown option prints its error and the usage text on stderr and exits 1. The usage text (`src/cli.ts`):

```
usage: tree-agent [flags]              (TUI, needs a terminal)
       tree-agent [flags] "<prompt>"
       <command> | tree-agent [flags]   (line 1: prompt, rest: follow-ups)
flags: --help | -h (this text)
       --resume <id> | --continue  --model <id>  --cwd <dir>
       --max-turns <n> (50)  --trust  --json
       --no-skills  --no-mcp (no mcp.json read, no server started)
       --permission-mode ask|auto (TUI: settings, ask; headless: auto)
       --on-blocked stop|continue (headless deny: exit 2, or go on)
       --compact-at <ratio> (0.3–0.95; compact at ratio × context)
       --mcp-login <server> (OAuth login to an HTTP MCP server)
       --mcp-logout <server> (delete its mcp-auth/ token file)
```

### Modes

| Invocation | Mode |
|---|---|
| `tree-agent` | TUI, new session (needs a TTY; without one and without a prompt: usage, exit 1) |
| `tree-agent --resume <id>` / `tree-agent --continue` | TUI on an existing session |
| `tree-agent "<prompt>"` | headless: run, print the final answer, exit |
| `tree-agent "<prompt>" --json` | headless: one `AgentEvent` per line (JSONL) on stdout |
| `cat task.md \| tree-agent` | headless, prompt from stdin: line 1 is the prompt, every further non-blank line a follow-up in the same session |
| `tree-agent "<prompt>" --resume <id>` | headless on an existing session |

A positional prompt wins over stdin. Headless text mode prints each final answer on stdout and tool activity on stderr (`> <tool> <args>`, `! <error>`, `activated: …`, `compacted <before> → <after> tokens`, `run ended: <reason>`).

### Flags

| Flag | Default | Effect |
|---|---|---|
| `--help`, `-h` | — | print the usage text on stdout and exit 0, whatever else is on the line, except an unknown option (its error and the usage text on stderr, exit 1) |
| `--model <id>` | `settings.model` | OpenRouter model id, checked against `GET /api/v1/models` (unknown id: exit 1). On a resumed session it replaces the recorded model. |
| `--permission-mode ask\|auto` | TUI: `settings.permissions.mode` (`ask`); headless: `auto` | see [Permissions](#permissions) |
| `--on-blocked stop\|continue` | `stop` | headless only: on a permission deny, stop with exit 2, or hand the model the blocked error and go on |
| `--compact-at <ratio>` | `settings.compaction.triggerRatio` (0.8) | compact at `ratio × contextLength`; 0.3–0.95 |
| `--max-turns <n>` | 50 | positive integer; reaching it ends the run with exit 1 |
| `--resume <id>` | — | open session `<id>` (any cwd's); unknown id: one line `no session <id>`, exit 1 |
| `--continue` | off | newest session of this cwd; none: a new session. Not combinable with `--resume`. |
| `--cwd <dir>` | the process cwd | working directory for tools, context files, project config and MCP stdio children |
| `--trust` | off | trust the cwd for this run only (see [Trust](#trust)) |
| `--json` | off | headless JSONL event stream on stdout; alone, with no prompt and no stdin, it prints usage |
| `--no-skills` | off | no skill discovery, no `skills` prompt section, `/skill:` answers "skills are disabled" |
| `--no-mcp` | off | no `mcp.json` read, no server started, so no `tool_search` and no `codemode` tool |
| `--mcp-login <server>` | — | the [MCP login](#the-hosted-tree-memory-server) for an HTTP server of `mcp.json` (user, plus project for a trusted cwd / `--trust`); needs no `OPENROUTER_API_KEY`; prints the URL on stderr, opens the browser on a TTY, waits ≤ 5 min, prints `logged in to "<server>" (<file>)`, exit 0 — also when the `initialize` after the stored login fails (one warning on stderr). A server that answers `initialize` without a 401: `"<server>" did not ask for a login`, exit 0. Unknown or stdio server, timeout, Ctrl+C or a refused login: exit 1. A server with a configured `Authorization` header: "nothing to log in to", exit 0. |
| `--mcp-logout <server>` | — | delete `mcp-auth/<server>.json` (`logged out of …`, or `not logged in to …`), exit 0 |

### Exit codes

| Code | Meaning |
|---|---|
| 0 | completed (TUI: `/exit`, or Ctrl+C while idle), or `--help` |
| 1 | error, usage error, `max_turns`, an unusable `/skill:` line, a headless run aborted by Ctrl+C / SIGTERM, or Ctrl+C in the TUI before the session opened (`starting…`, e.g. during an MCP login) |
| 2 | headless run blocked by the permission gate (`--on-blocked stop`) |
| 130 / 143 | a second Ctrl+C / SIGTERM during a headless run: Node's default exit; MCP children are killed |

### TUI

Built on [`@earendil-works/pi-tui`](https://www.npmjs.com/package/@earendil-works/pi-tui): a streamed transcript with one tool card per call (`llm_call` rows nested under a `codemode` card), the permission prompt, and a status bar with model · mode · `tokens / contextLength (NN %)` · session cost (including `llm()` calls and danger checks) · MCP servers · activated tools.

| Key | Action |
|---|---|
| `Enter` | send; during a run: a steering message (lands before the next model call) |
| `Alt+Enter` | follow-up message (runs after the current turn completes) |
| `Shift+Enter` | new line |
| `Up` / `Down` | input history |
| `Esc` | abort the current run |
| `Tab` (empty input) | expand / collapse tool results |
| `Shift+Tab` | toggle the permission mode `ask` ↔ `auto` |
| `Ctrl+C` | abort the run; when idle, quit; while the session opens (`starting…`), quit with exit 1 |

Slash commands: `/help`, `/model <id>`, `/compact [instructions]`, `/mode ask|auto`, `/tools`, `/mcp`, `/reload` (re-read `SYSTEM.md`, context files and skills), `/clear` (new session), `/exit`, `/skill:<name> [args]`. `/model`, `/clear` and `/compact` are refused while a prompt runs.

## Config

User-level state lives in `~/.tree-agent/`, or in `$TREE_AGENT_HOME` when set. Project-level config lives in `<cwd>/.tree-agent/` and loads only for a [trusted](#trust) cwd.

| Path (user: `~/.tree-agent/…`) | Purpose |
|---|---|
| `settings.json` | settings (below) |
| `mcp.json` | user MCP servers (see [MCP](#mcp)) |
| `mcp-auth/<server>.json` | OAuth tokens and client registration per HTTP MCP server (0600) |
| `trusted.json` | JSON array of trusted absolute directories |
| `AGENTS.md` | user context file, always injected |
| `SYSTEM.md` | replaces the prompt preamble when present |
| `skills/` | user skills |
| `sessions/` | JSONL sessions (see [Sessions](#sessions)) |
| `tmp/` | full tool output that was truncated for the model |
| `<cwd>/.tree-agent/settings.json`, `<cwd>/.tree-agent/mcp.json`, `<cwd>/.tree-agent/skills/` | project layer, trusted cwd only |

Context files: `~/.tree-agent/AGENTS.md`, then (trusted cwd only) every ancestor of the cwd from the filesystem root down to the cwd, taking the first of `AGENTS.md`, `CLAUDE.md` per directory. Each file is capped at `contextFiles.maxChars` (total cap 3×), re-read on `/reload`.

Environment variables (see `.env.example`):

| Variable | Effect |
|---|---|
| `OPENROUTER_API_KEY` | required; the run exits 1 without it |
| `TREE_AGENT_HOME` | replaces `~/.tree-agent` |
| `TREE_AGENT_LOG` | diagnostic log level on stderr: `debug`, `info`, `warn` (default), `error`, `silent` |
| `TREE_AGENT_TRUST=1` | trust every cwd for this run (like `--trust`); keep it out of `.env`, or every `make harness-*` run is trusted |

### `settings.json`

Layers merge key by key, later wins: defaults → `~/.tree-agent/settings.json` → `<cwd>/.tree-agent/settings.json` (trusted cwd only) → CLI flags. Every object is strict: an unknown key or a bad value fails the run with exit 1 and names the file and the key path.

| Key | Default | Meaning |
|---|---|---|
| `model` | `anthropic/claude-sonnet-5.5` | main model |
| `decisionModel` | `typesafe/jev-1.14` | the `auto` classifier's decision model (output modality `decisions`, checked at start); see [Permissions](#permissions) |
| `smallModel` | `anthropic/claude-haiku-5.5` | compaction summary and default `llm()` model; must support `structured_outputs` (checked at start) |
| `permissions.mode` | `ask` | the TUI's starting mode; headless is `auto` unless `--permission-mode` |
| `permissions.dangerThreshold` | 0.3 | 0.05–0.95; a call is dangerous when the decision model's `p(dangerous)` ≥ this |
| `compaction.triggerRatio` | 0.8 | 0.3–0.95; `--compact-at` overrides it |
| `compaction.keepRecentTokens` | 20 000 | tail kept verbatim, capped at half the trigger budget |
| `codemode.inlineBudgetTokens` | 3 000 | tool declarations inlined in the `codemode` tool's description |
| `codemode.timeoutMs` | 60 000 | per script |
| `codemode.memoryMb` | 256 | QuickJS memory limit |
| `codemode.maxToolCalls` | 50 | `tools.*()` calls per script |
| `codemode.outputMaxBytes` | 20 000 | script output to the model, middle-truncated, full copy in `tmp/` |
| `codemode.concurrency` | 4 | parallel host calls (`tools.*`, `llm()`) per script |
| `codemode.llm.model` | unset (= `smallModel`) | default model of `llm()` |
| `codemode.llm.depth` | 1 | 1 or 2 (see [Codemode and `llm()`](#codemode-and-llm)) |
| `codemode.llm.maxCalls` | 64 | `llm()` calls per script |
| `codemode.llm.maxInputChars` | 200 000 | per call |
| `codemode.llm.maxTotalTokens` | 400 000 | per script, summed usage |
| `codemode.llm.maxSubTurns` | 8 | depth-2 sub-loop turns |
| `mcp.initializeTimeoutMs` | 60 000 | wait for each MCP server's `initialize` at startup; one value for every server (`tools/call` stays 60 s) |
| `contextFiles.maxChars` | 40 000 | per context file |
| `skills.listingBudgetRatio` | 0.01 | the skills section's share of `contextLength` |

`codemode.memoryMb` and `codemode.outputMaxBytes` are settings in the code; the spec's §16 listing names them only in §10.4.

### Trust

Project files (the `AGENTS.md` / `CLAUDE.md` ancestor walk, `.tree-agent/settings.json`, `.tree-agent/mcp.json`, `.agents/skills`, `.tree-agent/skills`) load only for a trusted cwd. A cwd is trusted when it is listed in `~/.tree-agent/trusted.json`, or for one run with `--trust` / `TREE_AGENT_TRUST=1`.

- The TUI asks once per untrusted cwd: `y` writes the cwd to `trusted.json`, `n` runs on user-level config only.
- Headless never asks, and `--trust` does not persist anything. Answer `y` once in the TUI (or add the path to `trusted.json`) and later headless runs in that cwd need no flag.

## MCP

### `mcp.json` schema

Sources, merged by server name (the later file replaces a server whole): `~/.tree-agent/mcp.json`, then `<cwd>/.tree-agent/mcp.json` for a trusted cwd. The repo-root `.mcp.json` is Claude Code's file (another schema and owner) and `tree-agent` never reads it.

```ts
const McpServer = z.union([
  z.object({ type: z.literal("stdio").optional(),
             command: z.string(), args: z.string().array().default([]),
             env: z.record(z.string()).default({}) }),                  // stdio
  z.object({ type: z.literal("http").optional(),
             url: z.string().url(),
             headers: z.record(z.string()).default({}) }),              // streamable HTTP
]).and(z.object({
  // glob → exposure, first match in declaration order wins; unmatched: codemode
  toolExposure: z.record(z.enum(["direct", "deferred", "codemode", "hidden"])).default({}),
}));
export const McpConfig = z.object({ mcpServers: z.record(McpServer) });
```

- A server with both `command` and `url` is an error, and so is a `type` that does not match its transport (`"sse"`, or `"stdio"` with a `url`): `type must be http or stdio`. Globs support `*` only and are case-sensitive.
- stdio children start in the session cwd and inherit its environment plus `env`. HTTP adds `Accept: application/json, text/event-stream` and echoes `Mcp-Session-Id`.
- Timeouts: `initialize` 60 s (`mcp.initializeTimeoutMs`), every other call 60 s. Only `initialize`, `tools/list` (paginated) and `tools/call` are used; OAuth 2.1 for HTTP servers (below), no resources or prompts.
- Tools register as `mcp__<server>__<tool>`. The server segment is sanitised to `^[a-z][a-z0-9_]*$` (lowercased, other characters → `_`), so `tree-memory-local` becomes `tree_memory_local` and every tool is a valid `tools.<name>` property in scripts.
- `/mcp` in the TUI lists each configured server with its tool counts per exposure and its connection state.

### The committed `.tree-agent/mcp.json`

The repo commits one server, the local stdio memory server (the same command as `.mcp.json`'s `tree-memory-local`):

```json
{
  "mcpServers": {
    "tree-memory-local": {
      "command": "uv",
      "args": ["--directory", "apps/memory", "run", "--env-file", "../../.env",
               "python", "scripts/serve_mcp.py"],
      "env": { "ENV_FILE_PATH": "../../.env" },
      "toolExposure": {
        "search_memory": "direct",
        "visualize_*": "deferred"
      }
    }
  }
}
```

The paths are relative to the repo root, so the session cwd must be the repo root: run `tree-agent` there, or `make harness-run ARGS="--cwd ../.." …`. Why each exposure:

- `search_memory: direct` — the hot read path. It costs one prompt line and the model can call it on the first turn.
- `visualize_*: deferred` — `visualize_memory_structure` and `visualize_memory_embeddings` are rarely needed; they stay out of the tool list until `tool_search` finds them, which also exercises `tool_search` against a real server.
- everything else (`search_web`, `scrape_web`, `ingest_*`, the graphrag-only tools): the default `codemode` — bulk data and many small calls, reached from scripts at zero prompt cost.

With this file and the memory stack up (`make local-start`), the model sees `read`, `bash`, `edit`, `write`, `mcp__tree_memory_local__search_memory`, `tool_search` and `codemode`:

```bash
make harness-run PROMPT="what is in my memory about agents?" ARGS="--cwd ../.. --trust"
```

A server that does not answer `initialize` within 60 s logs `mcp "<name>": initialize timed out after 60 s (settings.json: mcp.initializeTimeoutMs)` and the session starts without its tools. The memory server answers in ~11–17 s warm and up to ~28 s on a slow start (Python imports, Opik setup), longer on a cold `uv` sync; when you see this warning, raise `mcp.initializeTimeoutMs` in `~/.tree-agent/settings.json` (e.g. `{"mcp": {"initializeTimeoutMs": 120000}}`).

The memory server's tools declare no `readOnlyHint`, so the gate treats them as writes: `search_memory` goes to the classifier in `auto` and prompts once in `ask` (answer `s` to allow it for the session).

### The hosted `tree-memory` server

The hosted server sits behind Prefect Horizon OAuth. Add it to your user file `~/.tree-agent/mcp.json`
(it is not committed to the repo's `.tree-agent/mcp.json`: a repo-root run by someone not logged in
would warn or wait every time, and its tools would duplicate `tree-memory-local`'s):

```json
{ "mcpServers": { "tree-memory": { "type": "http", "url": "https://tree-memory.fastmcp.app/mcp",
  "toolExposure": { "search_memory": "direct", "visualize_*": "deferred" } } } }
```

`tree-agent` follows the MCP authorization flow: on the server's 401 it discovers the authorization
server, registers itself once, and opens your browser (printing the URL as well: on stderr, or in the
TUI on its screen, where Ctrl+C quits the wait) for a one-time Horizon login; the token and the
registered client are stored in `~/.tree-agent/mcp-auth/tree-memory.json` (mode 0600) and refreshed
silently afterwards. Log in once with `tree-agent --mcp-login tree-memory` (no `OPENROUTER_API_KEY`
needed); every later run — TUI or headless — reuses the token. The first request after idle can take
tens of seconds while Horizon cold-starts the server (hence the 60 s `mcp.initializeTimeoutMs`
default). Headless runs without a terminal (piped stdin, CI) never wait for a login: a server that
needs one is skipped with `mcp "tree-memory": needs a login; run tree-agent --mcp-login tree-memory
once, then re-run`. The interactive wait is bounded at 5 minutes. Log out with
`tree-agent --mcp-logout tree-memory` (it deletes the file). Its tools register as
`mcp__tree_memory__*`.
A static token also works and disables the flow: `"headers": {"Authorization": "Bearer <token>"}`. A
personal Horizon API key (`fmcp_…`) is reported to work as that bearer (`apps/memory/src/tree/mcp/
client.py`); this is unverified from the harness — `.env.example` describes the key as a platform
credential, not a client one. Never commit a token.

## Permissions

Every tool call (from the model or from a codemode script) passes one gate, first match wins:

1. Read-only tool → allow, in both modes: `read`, `tool_search`, MCP tools with `readOnlyHint: true`.
2. Tool name already allowed for this session (`s`) → allow.
3. Mode `ask` → prompt.
4. Mode `auto` → the decision model (`decisionModel`, through OpenRouter's Decisions API) answers two questions about the call: `dangerous` as a probability and `risk` as `low`/`medium`/`high`. `p(dangerous)` < `permissions.dangerThreshold` (0.3) → allow; ≥ the threshold, or any failure (provider error, invalid answer, 10 s timeout) → prompt with the reason. Decision models return no text, so the reason is generated: `decision model: p(dangerous)=0.95, risk high (0.98)`. The classifier never denies on its own. Probabilities differ between models: both `typesafe/jev-1.14` and `liquid/d1-ultra` are tested (safe calls ≈ 0.01–0.02, `rm -rf ~/Downloads/*` ≈ 0.87–0.99); retune the threshold per model — raise it if a model prompts on benign writes, never past 0.5.
5. Prompt: TUI `y` once, `s` this tool name for the rest of the session, `n` deny. Headless has no prompter: the prompt is a deny.

| | TUI | Headless |
|---|---|---|
| Default mode | `settings.permissions.mode` (`ask`) | `auto` |
| Override | `--permission-mode`, `/mode`, `Shift+Tab` | `--permission-mode` |
| A prompt | dialog | deny → exit 2, or with `--on-blocked continue` the model gets the blocked error and goes on |

A danger check costs ≈ $0.00002 on `typesafe/jev-1.14` — input billed, output free — and is added to the session cost (in memory only: a `--resume` rebuilds the cost without the classifier's share, a few cents at most). A cached verdict (the same tool and input again in the session) costs nothing.

Every decision is written to the session JSONL as a `permission` entry. Nothing else persists across sessions: no allow lists, only `permissions.mode` in `settings.json`. The classifier is told that writes outside the cwd are dangerous, so a write to `/tmp` can be flagged and prompted; that is expected.

## Exposure

| Exposure | In the model's tool list | Callable by the model | Callable from scripts | Use for |
|---|---|---|---|---|
| `direct` | yes | yes | yes | the four built-ins, `tool_search`, `codemode`, MCP tools you want always visible |
| `deferred` | after `tool_search` activates it | after activation | yes | big servers' occasional tools |
| `codemode` | never | no | yes | the MCP default: bulk data, many small calls |
| `hidden` | never | no | no | blocked tools (e.g. `delete_*`) |

- With no MCP servers the model sees exactly the four built-ins.
- `tool_search` (BM25 over the `deferred` tools' names and descriptions) is registered only when at least one `deferred` tool exists. Its matches become callable from the next request; activation lasts for the session, is written to the JSONL and restored by `--resume`.
- The `codemode` tool is registered only when at least one `codemode` tool exists.

Built-in tools:

| Tool | Params | Notes |
|---|---|---|
| `read` | `path`, `offset?`, `limit?` | read-only; at most 2 000 lines / 50 KB per call, tells the model how to page |
| `bash` | `command`, `timeout?` (s, 1–3600, default 120) | runs in the cwd with the inherited env; each stream keeps its first and last 25 KB; output over 50 KB is spilled in full to `<home>/tmp/<uuid>.txt` (capped at 100 MiB) and the path appended |
| `edit` | `path`, `oldText`, `newText` | exact unique match, else an error with the match count |
| `write` | `path`, `content` | creates parent directories |

## Codemode and `llm()`

The `codemode` tool runs a JavaScript (ES2020, async) script in a fresh [QuickJS](https://www.npmjs.com/package/quickjs-emscripten) sandbox per call. Only `console.log` output and the returned value reach the model; the data the script handles stays in its variables.

Inside the sandbox:

| Name | What it does |
|---|---|
| `tools.<name>(args)` | calls a `codemode`, `direct` or activated tool; returns its structured result |
| `searchTools(query)` | BM25 over the `codemode` tools: `[{name, description}]` (activates nothing) |
| `describeTool(name)` | `{name, description, parameters}` |
| `llm(prompt, {schema?, model?, system?, maxTokens?, tools?})` | a model call; text, or parsed JSON with a `schema` |

Result shapes: `read` → `{path, lines}`, `bash` → `{exitCode, stdout, stderr}` (each stream cut like the text result), `edit` / `write` → their status string, MCP tools → `structuredContent` (FastMCP's `{result: "<json string>"}` wrapper is unwrapped to the parsed JSON), else the text parsed as JSON. There is no `require`, `fetch` or `fs`.

Limits come from `settings.codemode` (`timeoutMs` 60 000, `memoryMb` 256, `maxToolCalls` 50, `outputMaxBytes` 20 000, `concurrency` 4, `inlineBudgetTokens` 3 000). `await Promise.all([...])` fans tool and `llm()` calls out up to `concurrency` at once.

Permissions inside a script: every `tools.x()` passes the same gate with origin `codemode`, and prompts are shown one at a time. A deny rejects the promise, so the script can `catch` it, except in headless mode, where a deny aborts the script and the run (exit 2) and cannot be caught. `codemode` itself is not read-only: in `ask` mode the script is prompted once (or once per session with `s`). `llm()` never prompts.

### `llm()`: depth and budgets

| Depth | A sub-call can | Setting |
|---|---|---|
| 1 | answer from its prompt only (plain completion) | default |
| 2 | with `{tools: true}`, run a mini loop (≤ `maxSubTurns` turns) that calls tools (the registry minus the `codemode` tool itself, so no nested scripts) through the same gate | `codemode.llm.depth: 2` |

A call beyond the configured depth is a script error. Budgets per script: `maxCalls` 64, `maxInputChars` 200 000 per call, `maxTotalTokens` 400 000; exceeding one throws inside the script (`llm budget exceeded: maxCalls=64 …`). Each call emits an `llm_call` event (depth, model, usage) and its cost is added to the session cost.

### Worked example: map-reduce over memory (RLM)

The spec's §10.6 example, adapted to the committed server's tool name and the `rag`-mode result shape `{parents: [{content, …}], outcome, search_mode}` (graphrag returns a different shape):

```js
// 1. pull: data stays in the sandbox
const { parents } = await tools.mcp__tree_memory_local__search_memory({
  query: "pricing decisions", top_k: 40 });
// 2. map: one cheap sub-call per slice, in parallel
const facts = await Promise.all(parents.map(p => llm(
  `Extract pricing facts as bullets:\n${p.content}`)));
// 3. reduce: one call over the partial answers
const answer = await llm(
  `Merge into one list, dedupe:\n${facts.join("\n")}`,
  { schema: { type: "object", properties: {
      facts: { type: "array", items: { type: "string" } } },
    required: ["facts"] } });
// 4. return only the result
return answer.facts;
```

The main context grows by the script and the result; the 40 hits and 40 sub-answers never enter it. The example's 41 calls fit the default `maxCalls` (64); `maxTotalTokens` (400 000) and `timeoutMs` (60 000) are the cost and time bounds.

## Compaction

Between turns, when the estimated context reaches `triggerRatio` (default 0.8) × the current model's `contextLength`, the head of the conversation is summarised on the small model (`smallModel`) and the context becomes `[system prompt][summary][tail]`. The kept tail is about `keepRecentTokens` (20 000, capped at half the trigger budget), cut at a user message so tool calls stay with their results; a previous summary is merged into the new one. The original messages stay on disk, and a `compaction` entry is appended to the session.

- Knobs: `compaction.triggerRatio` and `compaction.keepRecentTokens` in `settings.json`, `--compact-at <0.3–0.95>` per run. The trigger is re-evaluated after `/model`.
- `/compact [instructions]` compacts now, with optional guidance for the summary.
- A provider context-overflow error compacts and retries the request once.
- Summaries are not added to the session cost.
- After 3 consecutive failed summaries automatic compaction stops (one error); a manual `/compact` still works.
- Both front ends print `compacted <before> → <after> tokens`.

## Sessions

Every session (TUI and headless) is one append-only JSONL file:

```
<TREE_AGENT_HOME>/sessions/--<cwd-slug>--/<timestamp>_<id>.jsonl
```

- The cwd slug is the absolute cwd without its leading `/`, with `/`, `\` and `:` mapped to `-` (`/Users/me/repo` → `--Users-me-repo--`). Two cwds can share a slug (`/a/b-c` and `/a/b/c` both give `--a-b-c--`), so `--continue` in one can pick the other's newest session; session ids stay unique.
- The file name's timestamp is ISO 8601 UTC with `:` and `.` mapped to `-` (`2026-10-24T12-00-00-000Z_<uuid>.jsonl`); the entries' own `timestamp` fields stay ISO 8601.
- Entries use pi's envelope (`id`, `parentId`, `timestamp`, `type`), written linearly: `session` (first line), `message`, `compaction`, `permission`, `tool_activation`, `model_change`, `llm_call`. Read them with `jq`.
- `--resume <id>` takes the id after the `_` and searches every slug directory; `--continue` takes the newest session of this cwd. A resume restores the messages after the last compaction (prefixed by its summary), the activated tools, the model and the cost.
- A last line cut by a crash is skipped with one warning when the session is loaded (`--resume` and `--continue` alike); the next entry is written on a fresh line.

## Skills

A skill is a directory with a `SKILL.md` whose YAML frontmatter has `name`, `description` and optionally `disable-model-invocation: true` ([Agent Skills](https://agentskills.io/)). Roots, in order, later wins on a name clash:

1. `~/.tree-agent/skills/*/`
2. `~/.agents/skills/*/`
3. trusted cwd only: every `.agents/skills/*/` from the git root down to the cwd (the nearest wins)
4. trusted cwd only: `<cwd>/.tree-agent/skills/*/`

The prompt lists only name, description (capped at 250 characters) and path; the model `read`s the body when a task matches. The section is capped at `skills.listingBudgetRatio` × `contextLength`; skills past the cap are dropped with a startup warning. `disable-model-invocation: true` hides a skill from the listing, but `/skill:` still runs it. Bad frontmatter and duplicate names become warnings, never crashes.

`/skill:<name> [args]` (TUI, a headless prompt, or any stdin line) injects, as one user message, `# <absolute SKILL.md path>` on the first line and then the full `SKILL.md` body with every `$ARGUMENTS` replaced by the args (appended after a blank line when the body has none), so relative links in the body resolve against the skill dir. The `/skill:` prefix is case-insensitive, the name is matched exactly. An unknown name prints the known names and sends nothing (headless: exit 1). `--no-skills` turns discovery off.

## Tests

- `apps/harness/tests/` mirrors `src/`: one vitest file per module (`tests/<module>/<file>.test.ts`), plus `tests/fixtures/` (recorded OpenRouter streams, a fake MCP server) and `tests/helpers/`.
- Everything runs offline on `FakeProvider` (`src/provider/fake.ts`) except four live OpenRouter tests in `tests/provider/openrouter.test.ts` (a streamed tool turn, `complete<T>` on the small model, the danger questions on `typesafe/jev-1.14` and on `liquid/d1-ultra`), which skip when `OPENROUTER_API_KEY` is unset. CI sets no key.
- `make harness-build` compiles all of `src/`; `tsconfig.build.json` excludes nothing, so `dist/provider/fake.js` is emitted too. No runtime code imports it.
- There is no integration suite; e2e is a real headless run (below).

## Cadence

From [`AGENTS.md`](../../AGENTS.md#verification-cadence) ("instead" = instead of the memory app's cadence):

For a harness commit run instead: `make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests && make pre-commit` — the Makefile exports `.env`, so the live OpenRouter tests run; a bare `pnpm vitest` sees no `OPENROUTER_API_KEY`, silently skips them, and a broken provider looks green. Good: `make harness-tests`. Bad: `cd apps/harness && pnpm vitest run`.

For the harness, e2e is a headless `tree-agent "<prompt>"` run against `tree-memory-local` (`make local-start`) with a live `OPENROUTER_API_KEY`, recorded as a `[HUMAN]` acceptance criterion in the task.
