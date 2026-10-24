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
  checkDecisionModel,
  loadSettings,
  requireApiKey,
} from "./config/settings.ts";
import { isTrusted, trustDirectory } from "./config/trust.ts";
import { type Out, runHeadless, type Signals } from "./headless/print.ts";
import { OpenRouterProvider } from "./provider/openrouter.ts";
import type { Provider } from "./provider/types.ts";
import { AgentSession } from "./session.ts";
import { SessionStore } from "./session-store/jsonl.ts";
import { builtinRegistry } from "./tools/builtins.ts";
import { runTui } from "./tui/app.ts";

export const DEFAULT_MAX_TURNS = 50;

const USAGE = `usage: tree-agent [flags]              (TUI, needs a terminal)
       tree-agent [flags] "<prompt>"
       <command> | tree-agent [flags]   (line 1: prompt, rest: follow-ups)
flags: --resume <id> | --continue  --model <id>  --cwd <dir>
       --max-turns <n> (${DEFAULT_MAX_TURNS})  --trust  --json`;

export interface CliArgs {
  prompt: string;
  model?: string;
  maxTurns: number;
  cwd?: string;
  trust: boolean;
  json: boolean;
  resume?: string; // a session id
  continue: boolean; // the newest session of this cwd
}

export type Mode =
  | { kind: "headless"; args: CliArgs; from: "argv" | "stdin" }
  | { kind: "tui"; args: CliArgs }
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
  signals?: Signals; // SIGINT / SIGTERM abort the headless run
  terminal?: Terminal; // the TUI's; default: process stdin/stdout
  // The provider seam: tests inject a FakeProvider here.
  createProvider?: (apiKey: string) => Provider;
}

// Pure: which front end runs, from argv and what stdin is.
export function resolveMode(
  argv: string[],
  isTTY: boolean,
  stdinIsPipe: boolean,
): Mode {
  let args: CliArgs;
  try {
    args = parseCli(argv);
  } catch (err) {
    return { kind: "usage", error: (err as Error).message };
  }
  if (args.prompt) return { kind: "headless", args, from: "argv" };
  if (stdinIsPipe) return { kind: "headless", args, from: "stdin" };
  if (args.json || !isTTY) return { kind: "usage" };
  return { kind: "tui", args };
}

// Returns the process exit code: 0 completed, 1 error or max_turns.
export async function main(argv: string[], deps: CliDeps): Promise<number> {
  const { env, stdout, stderr } = deps;
  const stdin = deps.stdin ?? noStdin;
  const mode = resolveMode(argv, stdin.isTTY, stdin.isPipe);
  if (mode.kind === "usage") {
    const { error } = mode;
    stderr.write(`${error ? `${error}\n` : ""}${USAGE}\n`);
    return 1;
  }
  const { args } = mode;
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

  try {
    const apiKey = requireApiKey(env);
    const cwd = resolve(args.cwd ?? process.cwd());
    if (!statSync(cwd, { throwIfNoEntry: false })?.isDirectory()) {
      throw new Error(`--cwd ${cwd} is not a directory`);
    }
    const trusted = await isTrusted(cwd, { env, flag: args.trust });
    const store = SessionStore.jsonl(userPaths(env).sessions);
    // Before the TUI starts: an unknown id is one line and exit 1.
    const resume = args.continue ? store.list(cwd)[0]?.id : args.resume;
    if (resume) store.load(resume);

    const open = async (trustNow: boolean): Promise<AgentSession> => {
      if (trustNow) await trustDirectory(cwd, env);
      const projectTrusted = trusted || trustNow;
      const settings = await loadSettings({
        env,
        flags: { model: args.model },
        projectDir: projectTrusted ? cwd : undefined,
      });
      const provider = (deps.createProvider ?? openRouter)(apiKey);
      await checkDecisionModel(provider, settings);
      const session = await AgentSession.create({
        provider,
        model: settings.model,
        tools: builtinRegistry({ tmpDir: userPaths(env).tmp, env }),
        cwd,
        maxTurns: args.maxTurns,
        trusted: projectTrusted,
        contextMaxChars: settings.contextFiles.maxChars,
        env,
        store,
        resume,
        // Headless runs in auto (spec §11); the gate is task 206.
        permissionMode:
          mode.kind === "tui" ? settings.permissions.mode : "auto",
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
    const session = await open(false);
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
  }
}

function parseCli(argv: string[]): CliArgs {
  const { values, positionals } = parseArgs({
    args: argv,
    options: {
      model: { type: "string" },
      "max-turns": { type: "string" },
      cwd: { type: "string" },
      trust: { type: "boolean", default: false },
      json: { type: "boolean", default: false },
      resume: { type: "string" },
      continue: { type: "boolean", default: false },
    },
    allowPositionals: true,
    strict: true,
  });
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
  return {
    prompt: positionals.join(" ").trim(),
    model: values.model,
    maxTurns,
    cwd: values.cwd,
    trust: values.trust,
    json: values.json,
    resume: values.resume?.trim(),
    continue: values.continue,
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
