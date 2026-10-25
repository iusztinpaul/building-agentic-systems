#!/usr/bin/env node
// tree-agent entry: argument parsing and mode dispatch (spec §15).
// A positional prompt, piped stdin or --json run headless; a TTY
// with no prompt is the TUI. Both open the same AgentSession over
// the JSONL session store, new or resumed (--resume, --continue).

import { fstatSync, realpathSync, statSync } from "node:fs";
import { resolve } from "node:path";
import { text } from "node:stream/consumers";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";
import { ProcessTerminal, type Terminal } from "@earendil-works/pi-tui";
import { userPaths } from "./config/paths.ts";
import {
  checkModels,
  loadSettings,
  requireApiKey,
} from "./config/settings.ts";
import { isTrusted, trustDirectory } from "./config/trust.ts";
import { type Out, runHeadless, type Signals } from "./headless/print.ts";
import { hasAuthorization, type LoginUi, openBrowser } from "./mcp/auth.ts";
import {
  authFilePath,
  deleteAuthFile,
  readAuthFile,
} from "./mcp/auth-store.ts";
import { McpClient, type StartMcp, startMcp } from "./mcp/client.ts";
import { loadMcpConfig } from "./mcp/config.ts";
import { type McpServerInfo, registerMcpTools } from "./mcp/register.ts";
import { OpenRouterProvider } from "./provider/openrouter.ts";
import type { Provider } from "./provider/types.ts";
import { AgentSession } from "./session.ts";
import { SessionStore } from "./session-store/jsonl.ts";
import type { DiscoverSkills } from "./skills/skills.ts";
import { builtinRegistry } from "./tools/builtins.ts";
import { registerToolSearch } from "./tools/tool-search.ts";
import { runTui } from "./tui/app.ts";
import type { PermissionMode, PermissionPrompter } from "./types.ts";

export const DEFAULT_MAX_TURNS = 50;

const USAGE = `usage: tree-agent [flags]              (TUI, needs a terminal)
       tree-agent [flags] "<prompt>"
       <command> | tree-agent [flags]   (line 1: prompt, rest: follow-ups)
flags: --help | -h (this text)
       --resume <id> | --continue  --model <id>  --cwd <dir>
       --max-turns <n> (${DEFAULT_MAX_TURNS})  --trust  --json
       --no-skills  --no-mcp (no mcp.json read, no server started)
       --permission-mode ask|auto (TUI: settings, ask; headless: auto)
       --on-blocked stop|continue (headless deny: exit 2, or go on)
       --compact-at <ratio> (0.3–0.95; compact at ratio × context)
       --mcp-login <server> (OAuth login to an HTTP MCP server)
       --mcp-logout <server> (delete its mcp-auth/ token file)`;

export interface CliArgs {
  prompt: string;
  model?: string;
  maxTurns: number;
  cwd?: string;
  trust: boolean;
  json: boolean;
  resume?: string; // a session id
  continue: boolean; // the newest session of this cwd
  permissionMode?: PermissionMode;
  onBlocked: "stop" | "continue";
  skills: boolean; // false: --no-skills
  mcp: boolean; // false: --no-mcp
  compactAt?: number; // settings.compaction.triggerRatio
}

// --mcp-login / --mcp-logout: no model key, no session.
export interface McpAuthMode {
  kind: "mcp-login" | "mcp-logout";
  server: string;
  cwd?: string;
  trust: boolean;
}

export type Mode =
  | { kind: "headless"; args: CliArgs; from: "argv" | "stdin" }
  | { kind: "tui"; args: CliArgs }
  | McpAuthMode
  | { kind: "help" }
  | { kind: "usage"; error?: string };

export interface Stdin {
  isTTY: boolean;
  isPipe: boolean; // a pipe or a redirected file
  read(): Promise<string>;
}

export interface CliDeps {
  env: NodeJS.ProcessEnv;
  stdout: Out;
  stderr: Out;
  stdin?: Stdin; // default: not a TTY, nothing piped
  // With stdin a TTY too, an MCP login may prompt (URL + wait).
  stderrIsTTY?: boolean; // default: process.stderr.isTTY
  openUrl?: (url: string) => void; // the browser; tests play it
  signals?: Signals; // SIGINT / SIGTERM abort the headless run
  terminal?: Terminal; // the TUI's; default: process stdin/stdout
  // The provider seam: tests inject a FakeProvider here.
  createProvider?: (apiKey: string) => Provider;
  discoverSkills?: DiscoverSkills; // the skills walker; tests spy
  startMcp?: StartMcp; // reads mcp.json, connects; tests spy
}

// Pure: which front end runs, from argv and what stdin is.
export function resolveMode(
  argv: string[],
  isTTY: boolean,
  stdinIsPipe: boolean,
): Mode {
  let args: CliArgs | McpAuthMode | "help";
  try {
    args = parseCli(argv);
  } catch (err) {
    return { kind: "usage", error: (err as Error).message };
  }
  if (args === "help") return { kind: "help" };
  if ("kind" in args) return args;
  if (args.prompt) return { kind: "headless", args, from: "argv" };
  if (stdinIsPipe) return { kind: "headless", args, from: "stdin" };
  if (args.json || !isTTY) return { kind: "usage" };
  return { kind: "tui", args };
}

// Returns the process exit code: 0 completed, 1 error or max_turns,
// 2 a headless run blocked by the permission gate.
export async function main(argv: string[], deps: CliDeps): Promise<number> {
  const { env, stdout, stderr } = deps;
  const stdin = deps.stdin ?? noStdin;
  const mode = resolveMode(argv, stdin.isTTY, stdin.isPipe);
  if (mode.kind === "help") {
    stdout.write(`${USAGE}\n`);
    return 0;
  }
  if (mode.kind === "usage") {
    const { error } = mode;
    stderr.write(`${error ? `${error}\n` : ""}${USAGE}\n`);
    return 1;
  }
  if ("server" in mode) {
    try {
      return await mcpAuthCommand(mode, deps);
    } catch (err) {
      stderr.write(`${(err as Error).message}\n`);
      return 1;
    }
  }
  const { args } = mode;
  // TTYs on both ends: a server that needs a login may prompt.
  const interactive =
    stdin.isTTY && (deps.stderrIsTTY ?? process.stderr.isTTY === true);
  let followUps: string[] = [];
  if (mode.kind === "headless" && mode.from === "stdin") {
    const lines = (await stdin.read())
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter(Boolean);
    if (lines.length === 0) {
      stderr.write(`${USAGE}\n`);
      return 1;
    }
    [args.prompt = "", ...followUps] = lines;
  }

  // Connected at startup, closed (children ended) when main returns.
  const mcpClients: McpClient[] = [];
  try {
    const apiKey = requireApiKey(env);
    const cwd = resolve(args.cwd ?? process.cwd());
    if (!statSync(cwd, { throwIfNoEntry: false })?.isDirectory()) {
      throw new Error(`--cwd ${cwd} is not a directory`);
    }
    const trusted = await isTrusted(cwd, { env, flag: args.trust });
    const store = SessionStore.jsonl(userPaths(env).sessions);
    // Before the TUI starts: an unknown id is one line and exit 1.
    // Loaded once here and handed to the session (one warning per
    // crash-cut line, not two).
    const resumeId = args.continue ? store.list(cwd)[0]?.id : args.resume;
    const resume = resumeId ? store.load(resumeId) : undefined;

    const open = async (
      trustNow: boolean,
      prompter: PermissionPrompter | null,
    ): Promise<AgentSession> => {
      if (trustNow) await trustDirectory(cwd, env);
      const projectTrusted = trusted || trustNow;
      const settings = await loadSettings({
        env,
        flags: {
          model: args.model,
          compaction:
            args.compactAt === undefined
              ? undefined
              : { triggerRatio: args.compactAt },
        },
        projectDir: projectTrusted ? cwd : undefined,
      });
      const provider = (deps.createProvider ?? openRouter)(apiKey);
      await checkModels(provider, settings);
      const { tmp: tmpDir } = userPaths(env);
      const tools = builtinRegistry({ tmpDir, env });
      let mcpServers: McpServerInfo[] = [];
      if (args.mcp) {
        const start = deps.startMcp ?? startMcp;
        const clients = await start({
          env,
          cwd,
          trusted: projectTrusted,
          initializeTimeoutMs: settings.mcp.initializeTimeoutMs,
          login: interactive ? loginUi(deps, true) : undefined,
        });
        mcpClients.push(...clients);
        // Before create(): a resumed session re-activates by name.
        mcpServers = registerMcpTools(tools, clients, { tmpDir });
        registerToolSearch(tools);
      }
      const session = await AgentSession.create({
        provider,
        model: settings.model,
        tools,
        mcpServers,
        codemode: { ...settings.codemode, tmpDir },
        compaction: settings.compaction,
        cwd,
        maxTurns: args.maxTurns,
        trusted: projectTrusted,
        contextMaxChars: settings.contextFiles.maxChars,
        env,
        store,
        resume,
        // settings.permissions.mode is the TUI's; headless is auto.
        permissionMode:
          args.permissionMode ??
          (mode.kind === "tui" ? settings.permissions.mode : "auto"),
        prompter,
        onBlocked: args.onBlocked,
        decisionModel: settings.decisionModel,
        dangerThreshold: settings.permissions.dangerThreshold,
        smallModel: settings.smallModel,
        skills: args.skills
          ? {
              budgetRatio: settings.skills.listingBudgetRatio,
              discover: deps.discoverSkills,
            }
          : null,
      });
      // A resumed session keeps its model unless --model says.
      if (args.model && args.model !== session.model) {
        await session.setModel(args.model);
      }
      return session;
    };

    if (mode.kind === "tui") {
      return await runTui({
        terminal: deps.terminal ?? new ProcessTerminal(),
        cwd,
        askTrust: !trusted,
        open,
      });
    }
    const session = await open(false, null);
    return await runHeadless({
      session,
      prompt: args.prompt,
      followUps,
      json: args.json,
      stdout,
      stderr,
      signals: deps.signals,
    });
  } catch (err) {
    stderr.write(`${(err as Error).message}\n`);
    return 1;
  } finally {
    await Promise.all(mcpClients.map((client) => client.close()));
  }
}

// --mcp-logout deletes the token file (mcp.json not read: a removed
// server can still be logged out of). --mcp-login runs the flow
// whatever the TTYs, with cached tokens ignored and the registered
// client kept; Ctrl+C cancels it (exit 1).
async function mcpAuthCommand(
  mode: McpAuthMode,
  deps: CliDeps,
): Promise<number> {
  const { env, stdout, stderr } = deps;
  const { server: name } = mode;
  const file = authFilePath(env, name);
  if (mode.kind === "mcp-logout") {
    const removed = await deleteAuthFile(file);
    stdout.write(
      removed
        ? `logged out of "${name}" (${file})\n`
        : `not logged in to "${name}"\n`,
    );
    return 0;
  }
  const cwd = resolve(mode.cwd ?? process.cwd());
  const trusted = await isTrusted(cwd, { env, flag: mode.trust });
  const servers = await loadMcpConfig({
    env,
    projectDir: trusted ? cwd : undefined,
  });
  const server = servers[name];
  if (!server) {
    const known = Object.keys(servers).join(", ") || "none";
    stderr.write(`no MCP server "${name}" in mcp.json; known: ${known}\n`);
    return 1;
  }
  if (!("url" in server)) {
    stderr.write(`"${name}" is not an HTTP server in mcp.json\n`);
    return 1;
  }
  if (hasAuthorization(server.headers)) {
    stdout.write(
      `"${name}" uses a static Authorization header; ` +
        "nothing to log in to\n",
    );
    return 0;
  }
  const cancel = new AbortController();
  const onSignal = () => cancel.abort();
  deps.signals?.once("SIGINT", onSignal);
  deps.signals?.once("SIGTERM", onSignal);
  const tty =
    (deps.stdin ?? noStdin).isTTY &&
    (deps.stderrIsTTY ?? process.stderr.isTTY === true);
  const client = new McpClient(name, server, {
    env,
    cwd,
    freshLogin: true,
    login: { ...loginUi(deps, tty), signal: cancel.signal },
  });
  try {
    await client.listTools();
  } catch (err) {
    const message = (err as Error).message;
    const label = `mcp "${name}": `;
    const line = message.startsWith(label) ? message : label + message;
    stderr.write(`${line}\n`);
    return 1;
  } finally {
    deps.signals?.off("SIGINT", onSignal);
    deps.signals?.off("SIGTERM", onSignal);
    await client.close();
  }
  if (!(await readAuthFile(file, server.url))?.tokens) {
    stdout.write(`"${name}" did not ask for a login\n`);
    return 0;
  }
  stdout.write(`logged in to "${name}" (${file})\n`);
  return 0;
}

// The URL always goes to stderr (the TUI is not up yet); the
// browser opens only when there is a terminal to come back to.
function loginUi(deps: CliDeps, browser: boolean): LoginUi {
  return {
    notify: (line) => deps.stderr.write(`${line}\n`),
    open: browser ? (deps.openUrl ?? openBrowser) : undefined,
  };
}

// "help" for --help / -h, whatever else is on the line (an unknown
// option still throws first: parseArgs is strict).
function parseCli(argv: string[]): CliArgs | McpAuthMode | "help" {
  const { values, positionals } = parseArgs({
    args: argv,
    options: {
      help: { type: "boolean", short: "h", default: false },
      model: { type: "string" },
      "max-turns": { type: "string" },
      cwd: { type: "string" },
      trust: { type: "boolean", default: false },
      json: { type: "boolean", default: false },
      resume: { type: "string" },
      continue: { type: "boolean", default: false },
      "permission-mode": { type: "string" },
      "on-blocked": { type: "string", default: "stop" },
      "no-skills": { type: "boolean", default: false },
      "no-mcp": { type: "boolean", default: false },
      "compact-at": { type: "string" },
      "mcp-login": { type: "string" },
      "mcp-logout": { type: "string" },
    },
    allowPositionals: true,
    strict: true,
  });
  if (values.help) return "help";
  const login = values["mcp-login"];
  const logout = values["mcp-logout"];
  if (login !== undefined && logout !== undefined) {
    throw new Error("use --mcp-login or --mcp-logout, not both");
  }
  if (login !== undefined || logout !== undefined) {
    const kind = login !== undefined ? "mcp-login" : "mcp-logout";
    const server = (login ?? logout ?? "").trim();
    if (!server) throw new Error(`--${kind} needs a server name`);
    return { kind, server, cwd: values.cwd, trust: values.trust };
  }
  const turns = values["max-turns"] ?? String(DEFAULT_MAX_TURNS);
  const maxTurns = Number(turns);
  if (!Number.isInteger(maxTurns) || maxTurns < 1) {
    throw new Error(`--max-turns must be a positive integer: ${turns}`);
  }
  if (values.resume !== undefined && values.continue) {
    throw new Error("use --resume <id> or --continue, not both");
  }
  if (values.resume !== undefined && !values.resume.trim()) {
    throw new Error("--resume needs a session id");
  }
  const mode = values["permission-mode"];
  if (mode !== undefined && mode !== "ask" && mode !== "auto") {
    throw new Error(`--permission-mode must be ask or auto: ${mode}`);
  }
  const onBlocked = values["on-blocked"];
  if (onBlocked !== "stop" && onBlocked !== "continue") {
    throw new Error(`--on-blocked must be stop or continue: ${onBlocked}`);
  }
  const compactAt = values["compact-at"];
  const ratio = Number(compactAt);
  if (compactAt !== undefined && !(ratio >= 0.3 && ratio <= 0.95)) {
    throw new Error(`--compact-at must be from 0.3 to 0.95: ${compactAt}`);
  }
  return {
    prompt: positionals.join(" ").trim(),
    model: values.model,
    maxTurns,
    cwd: values.cwd,
    trust: values.trust,
    json: values.json,
    resume: values.resume?.trim(),
    continue: values.continue,
    permissionMode: mode,
    onBlocked,
    skills: !values["no-skills"],
    mcp: !values["no-mcp"],
    ...(compactAt !== undefined && { compactAt: ratio }),
  };
}

const noStdin: Stdin = {
  isTTY: false,
  isPipe: false,
  read: async () => "",
};

function processStdin(): Stdin {
  let isPipe = false;
  try {
    const stat = fstatSync(0);
    isPipe = stat.isFIFO() || stat.isFile();
  } catch {
    // no stdin at all
  }
  return {
    isTTY: process.stdin.isTTY === true,
    isPipe,
    read: () => text(process.stdin),
  };
}

function openRouter(apiKey: string): Provider {
  return new OpenRouterProvider({ apiKey });
}

// Run only when executed (node src/cli.ts, dist/cli.js, the bin
// symlink), not when a test imports main().
function isEntryPoint(): boolean {
  const script = process.argv[1];
  if (!script) return false;
  try {
    return realpathSync(script) === fileURLToPath(import.meta.url);
  } catch {
    return false;
  }
}

if (isEntryPoint()) {
  process.exitCode = await main(process.argv.slice(2), {
    env: process.env,
    stdout: process.stdout,
    stderr: process.stderr,
    stdin: processStdin(),
    signals: process,
  });
}
